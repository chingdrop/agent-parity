"""Celery tasks: the scaled-mode pipeline.

Shape: one *group* of fan-out tasks per client — the AD export plus one
inventory pull per enabled vendor — feeding a *chord* callback that runs the
pandas correlation exactly once, against that client's complete result set.

Three deliberate design points:

* **Idempotency** — the CorrelationRun row is created (empty, PENDING)
  *before* the chord is dispatched, and its ID rides through the callback
  signature. A retried or double-fired callback finds the run already
  finalized and no-ops (enforced under a row lock in
  ``services.persist_correlation``). The chord is dispatched from
  ``transaction.on_commit`` so a worker can never observe a run ID whose row
  hasn't committed yet — the classic Celery+Django race.

* **Partial-failure tolerance** — fan-out tasks never raise; they return a
  ``{"ok": False, "error": ...}`` payload instead, so one throttled or
  broken vendor API can't stop the chord from firing. The callback records
  per-vendor outcomes on the run (COMPLETE vs PARTIAL) rather than silently
  dropping the whole run. ``link_error`` on the callback is the backstop for
  the callback itself blowing up: the run gets marked FAILED instead of
  hanging in PENDING forever.

* **Rate limits** — each vendor gets its own task so Celery's per-task
  ``rate_limit`` can encode that vendor's real-world API throttling.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import chord, shared_task
from django.db import transaction
from django.utils import timezone

from agent_parity.ad_sync.parser import parse_ad_export
from agent_parity.config import AppConfig, ClientConfig, load_config
from agent_parity.models import AgentDevice
from dashboard import services
from dashboard.models import CorrelationRun

logger = logging.getLogger(__name__)


# --- fan-out: one task per (client, vendor) -----------------------------------


def _vendor_payload(client_slug: str, vendor_name: str) -> dict:
    """Fetch one vendor's inventory, returning a JSON-safe result envelope.

    Failures are *returned*, not raised — the chord callback must always fire
    with whatever succeeded. (Transient-error retries would slot in here with
    autoretry; omitted to keep the failure semantics easy to follow.)
    """
    try:
        records = services.collect_vendor_inventory(load_config(), client_slug, vendor_name)
        return {
            "source": vendor_name,
            "ok": True,
            "records": [record.to_dict() for record in records],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s inventory failed for %s: %s", vendor_name, client_slug, exc)
        return {"source": vendor_name, "ok": False, "error": str(exc)}


# Rate limits reflect each vendor's practical API budget: SentinelOne's
# management API is generous; Carbon Black Live Response sessions are a
# scarce per-org resource; GravityZone's JSON-RPC endpoint throttles hard.
@shared_task(rate_limit="30/m")
def fetch_sentinelone_inventory(client_slug: str) -> dict:
    return _vendor_payload(client_slug, "sentinelone")


@shared_task(rate_limit="10/m")
def fetch_carbonblack_inventory(client_slug: str) -> dict:
    return _vendor_payload(client_slug, "carbonblack")


@shared_task(rate_limit="6/m")
def fetch_bitdefender_inventory(client_slug: str) -> dict:
    return _vendor_payload(client_slug, "bitdefender")


VENDOR_TASKS = {
    "sentinelone": fetch_sentinelone_inventory,
    "carbonblack": fetch_carbonblack_inventory,
    "bitdefender": fetch_bitdefender_inventory,
}


@shared_task(rate_limit="10/m")
def collect_ad_export(client_slug: str) -> dict:
    """The AD export leg of the fan-out (remote script execution is slow)."""
    try:
        raw_csv = services.collect_ad_csv(load_config(), client_slug)
        return {"source": "ad", "ok": True, "csv": raw_csv}
    except Exception as exc:  # noqa: BLE001
        logger.warning("AD export failed for %s: %s", client_slug, exc)
        return {"source": "ad", "ok": False, "error": str(exc)}


# --- fan-in: the chord callback ------------------------------------------------


@shared_task
def correlate_client(results: list[dict], run_id: int) -> dict:
    """Correlate one client's complete fan-out results and persist them.

    Runs once per client per run, against everything the group returned —
    correlation never races partial state from another worker.
    """
    run = CorrelationRun.objects.select_related("client").get(pk=run_id)
    if run.status != CorrelationRun.RunStatus.PENDING:
        logger.warning("Run %s already finalized; ignoring duplicate callback", run_id)
        return {"run_id": run_id, "status": run.status, "duplicate": True}

    config = load_config()
    vendor_status: dict[str, str] = {}
    ad_csv: str | None = None
    agent_records: list[AgentDevice] = []
    for payload in results:
        source = payload["source"]
        if not payload.get("ok"):
            vendor_status[source] = f"error: {payload.get('error', 'unknown')}"
            continue
        vendor_status[source] = "ok"
        if source == "ad":
            ad_csv = payload["csv"]
        else:
            agent_records.extend(AgentDevice.from_dict(r) for r in payload["records"])

    # No AD export means there is nothing to reconcile against — that run is
    # failed, not partial.
    if ad_csv is None:
        run.status = CorrelationRun.RunStatus.FAILED
        run.vendor_status = vendor_status
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "vendor_status", "finished_at"])
        return {"run_id": run_id, "status": run.status}

    count = services.finalize_run(
        run, parse_ad_export(ad_csv), agent_records, vendor_status,
        splunk_config=config.splunk,
    )
    run.refresh_from_db()
    return {"run_id": run_id, "status": run.status, "snapshots": count}


@shared_task
def mark_run_failed(_request, exc, _traceback, run_id: int) -> None:
    """link_error backstop: never leave a run stuck in PENDING.

    Celery invokes error callbacks with ``(request, exc, traceback)``; only
    the exception is used here, but all three must stay in the signature to
    match what Celery calls.
    """
    updated = CorrelationRun.objects.filter(
        pk=run_id, status=CorrelationRun.RunStatus.PENDING
    ).update(status=CorrelationRun.RunStatus.FAILED, finished_at=timezone.now())
    if updated:
        logger.error("Run %s marked failed after callback error: %s", run_id, exc)


# --- orchestration ---------------------------------------------------------------


def dispatch_client(config: AppConfig, client_cfg: ClientConfig) -> int | None:
    """Create the pending run and dispatch the group+chord for one client."""
    client = services.sync_client_from_config(client_cfg)
    if not client.is_active:
        logger.info("Client %s is inactive; skipping", client.slug)
        return None

    with transaction.atomic():
        run = CorrelationRun.objects.create(client=client, stale_days=config.stale_days)
        header = [collect_ad_export.s(client_cfg.slug)] + [
            VENDOR_TASKS[vendor].s(client_cfg.slug) for vendor in sorted(client_cfg.vendors)
        ]
        callback = correlate_client.s(run_id=run.pk).on_error(mark_run_failed.s(run_id=run.pk))
        # Dispatch only after the CorrelationRun row commits — otherwise a
        # worker can pick up the callback before the run it references exists.
        transaction.on_commit(lambda: chord(header)(callback))
    return run.pk


def _client_is_due(client_cfg: ClientConfig) -> bool:
    latest = (
        CorrelationRun.objects.filter(client__slug=client_cfg.slug)
        .order_by("-started_at")
        .first()
    )
    if latest is None:
        return True
    return latest.started_at <= timezone.now() - timedelta(hours=client_cfg.sync_interval_hours)


@shared_task
def dispatch_all_clients(force: bool = False) -> list[str]:
    """Beat entrypoint: kick off the group+chord for every client that is due.

    Beat ticks hourly; each client's own ``sync_interval_hours`` from
    config.yaml decides whether it actually runs this tick.
    """
    config = load_config()
    dispatched = []
    for slug, client_cfg in sorted(config.clients.items()):
        if not force and not _client_is_due(client_cfg):
            continue
        if dispatch_client(config, client_cfg) is not None:
            dispatched.append(slug)
    logger.info("Dispatched sync for: %s", ", ".join(dispatched) or "no clients due")
    return dispatched
