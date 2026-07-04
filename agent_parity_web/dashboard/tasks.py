"""Celery tasks: the scaled-mode pipeline.

Shape: one *group* of fan-out tasks per client — one AD export task per
domain controller (a client with multiple AD domains has more than one),
one inventory-pull task per (vendor, site/tenant) the client has within
that vendor (almost always just one), feeding a *chord* callback that runs
the pandas correlation exactly once, against that client's complete result
set.

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

from agent_parity.ad_sync.parser import concat_ad_frames, parse_ad_export
from agent_parity.config import AppConfig, ClientConfig, get_connectors
from agent_parity.models import AgentDevice
from dashboard import services
from dashboard.config_db import build_app_config_from_db
from dashboard.models import CorrelationRun

logger = logging.getLogger(__name__)


# --- fan-out: one task per (client, vendor, site/tenant) -----------------------


def _vendor_payload(client_slug: str, vendor_name: str, site_index: int, key: str) -> dict:
    """Fetch one (vendor, site/tenant)'s inventory, returning a JSON-safe
    result envelope. ``key`` is precomputed at dispatch time
    (``dispatch_client``, via ``services.site_status_key``) since it needs
    to know how many sites/tenants this vendor has in total to decide
    whether an index suffix is even necessary — this task only needs to use
    it, not recompute it.

    Failures are *returned*, not raised — the chord callback must always fire
    with whatever succeeded. (Transient-error retries would slot in here with
    autoretry; omitted to keep the failure semantics easy to follow.)
    """
    try:
        connector = get_connectors(build_app_config_from_db(), client_slug, vendor_name)[site_index]
        records = connector.fetch_inventory()
        return {
            "source": vendor_name,
            "key": key,
            "ok": True,
            "records": [record.to_dict() for record in records],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s (%s) inventory failed for %s: %s", vendor_name, key, client_slug, exc)
        return {"source": vendor_name, "key": key, "ok": False, "error": str(exc)}


# Rate limits reflect each vendor's practical API budget: SentinelOne's
# management API is generous; Carbon Black Live Response sessions are a
# scarce per-org resource; GravityZone's JSON-RPC endpoint throttles hard.
@shared_task(rate_limit="30/m")
def fetch_sentinelone_inventory(client_slug: str, site_index: int, key: str) -> dict:
    return _vendor_payload(client_slug, "sentinelone", site_index, key)


@shared_task(rate_limit="10/m")
def fetch_carbonblack_inventory(client_slug: str, site_index: int, key: str) -> dict:
    return _vendor_payload(client_slug, "carbonblack", site_index, key)


@shared_task(rate_limit="6/m")
def fetch_bitdefender_inventory(client_slug: str, site_index: int, key: str) -> dict:
    return _vendor_payload(client_slug, "bitdefender", site_index, key)


VENDOR_TASKS = {
    "sentinelone": fetch_sentinelone_inventory,
    "carbonblack": fetch_carbonblack_inventory,
    "bitdefender": fetch_bitdefender_inventory,
}


@shared_task(rate_limit="10/m")
def collect_ad_export(client_slug: str, target_device: str) -> dict:
    """The AD export leg of the fan-out (remote script execution is slow).

    One task per (client, domain controller) — a client with multiple AD
    domains gets one of these per entry in ``ClientConfig.ad_target_devices``
    (see ``dispatch_client``); ``correlate_client`` concatenates whichever
    domains' exports succeed.
    """
    try:
        raw_csv = services.collect_ad_csv(build_app_config_from_db(), client_slug, target_device)
        return {"source": "ad", "target_device": target_device, "ok": True, "csv": raw_csv}
    except Exception as exc:  # noqa: BLE001
        logger.warning("AD export failed for %s domain %s: %s", client_slug, target_device, exc)
        return {"source": "ad", "target_device": target_device, "ok": False, "error": str(exc)}


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

    vendor_status: dict[str, str] = {}
    ad_csvs: list[str] = []
    agent_records: list[AgentDevice] = []
    for payload in results:
        source = payload["source"]
        # AD payloads are keyed per domain (ad:<target_device>) since a
        # client can have more than one; vendor payloads carry their own
        # precomputed key (services.site_status_key) — plain vendor name
        # for the common single-site/tenant case, vendor:label or
        # vendor:index when there's more than one.
        key = f"ad:{payload['target_device']}" if source == "ad" else payload["key"]
        if not payload.get("ok"):
            vendor_status[key] = f"error: {payload.get('error', 'unknown')}"
            continue
        vendor_status[key] = "ok"
        if source == "ad":
            ad_csvs.append(payload["csv"])
        else:
            agent_records.extend(AgentDevice.from_dict(r) for r in payload["records"])

    # concat_ad_frames/finalize_run handle "every domain failed" (ad_csvs
    # empty) by failing the run outright — nothing to reconcile against.
    ad_df = concat_ad_frames([parse_ad_export(csv) for csv in ad_csvs]) if ad_csvs else None
    count = services.finalize_run(run, ad_df, agent_records, vendor_status)
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
        vendor_tasks = []
        for vendor in sorted(client_cfg.vendors):
            sites = client_cfg.vendors[vendor]
            for index, site in enumerate(sites):
                key = services.site_status_key(vendor, site, index, len(sites))
                vendor_tasks.append(VENDOR_TASKS[vendor].s(client_cfg.slug, index, key))
        header = [
                     collect_ad_export.s(client_cfg.slug, target_device)
                     for target_device in client_cfg.ad_target_devices
                 ] + vendor_tasks
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

    Beat ticks hourly; each client's own ``sync_interval_hours`` decides
    whether it actually runs this tick.
    """
    config = build_app_config_from_db()
    dispatched = []
    for slug, client_cfg in sorted(config.clients.items()):
        if not force and not _client_is_due(client_cfg):
            continue
        if dispatch_client(config, client_cfg) is not None:
            dispatched.append(slug)
    logger.info("Dispatched sync for: %s", ", ".join(dispatched) or "no clients due")
    return dispatched
