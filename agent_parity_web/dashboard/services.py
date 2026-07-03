"""Shared pipeline plumbing between the two entrypoints.

The management command (synchronous demo path) and the Celery chord callback
(scaled path) both call these functions — collection, correlation, and
persistence live here exactly once, so the parallelism in tasks.py is purely
additive infrastructure, not a second implementation of the pipeline.
"""

from __future__ import annotations

import logging

import pandas as pd
from dashboard.models import Client, CorrelationRun, CoverageSnapshot, Device
from django.db import transaction
from django.utils import timezone

from agent_parity.ad_sync.parser import parse_ad_export
from agent_parity.config import AppConfig, ClientConfig, get_connector
from agent_parity.correlation.engine import CorrelationResult, agents_to_frame, correlate
from agent_parity.deployment.script_runner import run_ad_export
from agent_parity.models import AgentDevice
from agent_parity.reporting import splunk_export

logger = logging.getLogger(__name__)


# --- collection --------------------------------------------------------------


def sync_client_from_config(client_cfg: ClientConfig) -> Client:
    """Upsert the ORM Client row from its config.yaml entry."""
    client, _ = Client.objects.update_or_create(
        slug=client_cfg.slug,
        defaults={"name": client_cfg.name, "enabled_vendors": sorted(client_cfg.vendors)},
    )
    return client


def collect_ad_csv(config: AppConfig, client_slug: str) -> str:
    """Run the AD export through one of the client's vendor channels.

    Any enabled vendor's remote-execution capability can carry the script;
    we use the first (alphabetically, so it's deterministic). Returns the
    raw CSV text — JSON-safe, so the Celery fan-out task can ship it as-is.
    """
    client_cfg = config.client(client_slug)
    vendor_name = sorted(client_cfg.vendors)[0]
    connector = get_connector(config, client_slug, vendor_name)
    return run_ad_export(connector, client_cfg.ad_target_device)


def collect_ad_frame(config: AppConfig, client_slug: str) -> pd.DataFrame:
    return parse_ad_export(collect_ad_csv(config, client_slug))


def collect_vendor_inventory(
        config: AppConfig, client_slug: str, vendor_name: str
) -> list[AgentDevice]:
    connector = get_connector(config, client_slug, vendor_name)
    return connector.fetch_inventory()


# --- persistence --------------------------------------------------------------


def _first_valid(*values):
    for value in values:
        if value is not None and not pd.isna(value):
            return value
    return None


@transaction.atomic
def persist_correlation(
        run: CorrelationRun,
        result: CorrelationResult,
        vendor_status: dict[str, str],
) -> int:
    """Load a classified frame into CoverageSnapshot rows for ``run``.

    Idempotent: if the run has already been finalized (a Celery retry, a
    double-fired callback), this is a no-op — the pre-created CorrelationRun
    ID is the idempotency key.
    """
    # Re-read under the transaction so two racing workers can't both persist.
    current = CorrelationRun.objects.select_for_update().get(pk=run.pk)
    if current.status != CorrelationRun.RunStatus.PENDING:
        logger.warning("Run %s already finalized (%s); skipping persist", run.pk, current.status)
        return 0

    client = current.client
    frame = result.frame

    # Upsert device identities for every join key in this run.
    existing = {d.join_key: d for d in client.devices.all()}
    now = timezone.now()
    to_create, to_update = [], []
    device_rows: dict[str, dict] = {}
    for row in frame.itertuples(index=False):
        seen = _first_valid(getattr(row, "last_seen", None), getattr(row, "last_logon", None))
        info = device_rows.setdefault(
            row.join_key,
            {"hostname": None, "os": None, "last_seen": None},
        )
        info["hostname"] = info["hostname"] or _first_valid(
            getattr(row, "hostname_ad", None), getattr(row, "hostname_agent", None)
        )
        info["os"] = info["os"] or _first_valid(
            getattr(row, "os_ad", None), getattr(row, "os_agent", None)
        )
        if seen is not None and (info["last_seen"] is None or seen > info["last_seen"]):
            info["last_seen"] = seen

    for join_key, info in device_rows.items():
        hostname = str(info["hostname"] or join_key)
        os_name = str(info["os"] or "")
        last_seen = info["last_seen"].to_pydatetime() if info["last_seen"] is not None else None
        if join_key in existing:
            device = existing[join_key]
            device.hostname, device.os = hostname, os_name
            if last_seen and (device.last_seen is None or last_seen > device.last_seen):
                device.last_seen = last_seen
            to_update.append(device)
        else:
            to_create.append(
                Device(
                    client=client,
                    join_key=join_key,
                    hostname=hostname,
                    os=os_name,
                    last_seen=last_seen,
                )
            )
    Device.objects.bulk_create(to_create)
    Device.objects.bulk_update(to_update, ["hostname", "os", "last_seen"])
    devices = {d.join_key: d for d in client.devices.all()}

    snapshots = [
        CoverageSnapshot(
            run=current,
            device=devices[row.join_key],
            status=row.status,
            vendor="" if pd.isna(row.vendor) else str(row.vendor),
            match_method=row.match_method,
            agent_last_seen=(
                None if pd.isna(row.last_seen) else row.last_seen.to_pydatetime()
            ),
        )
        for row in frame.itertuples(index=False)
    ]
    CoverageSnapshot.objects.bulk_create(snapshots)

    failed = [name for name, state in vendor_status.items() if state != "ok"]
    current.vendor_status = vendor_status
    current.finished_at = now
    current.status = (
        CorrelationRun.RunStatus.PARTIAL if failed else CorrelationRun.RunStatus.COMPLETE
    )
    current.save(update_fields=["vendor_status", "finished_at", "status"])
    logger.info(
        "Run %s for %s: %d snapshots, status=%s", current.pk, client.slug, len(snapshots),
        current.status,
    )
    return len(snapshots)


def finalize_run(
        run: CorrelationRun,
        ad_df: pd.DataFrame,
        agent_records: list[AgentDevice],
        vendor_status: dict[str, str],
        splunk_config=None,
) -> int:
    """Correlate + persist + (optionally) forward deltas — the shared fan-in."""
    result = correlate(ad_df, agents_to_frame(agent_records), stale_days=run.stale_days)
    count = persist_correlation(run, result, vendor_status)
    if count and splunk_config is not None:
        try:
            export_deltas_to_splunk(run, splunk_config)
        except splunk_export.SplunkExportError:
            # A reporting sink outage must never fail the run itself.
            logger.exception("Splunk delta export failed for run %s", run.pk)
    return count


# --- Splunk delta export -------------------------------------------------------


def export_deltas_to_splunk(run: CorrelationRun, splunk_config) -> int:
    """Diff this run against the client's previous run and forward transitions."""
    if not splunk_config.enabled:
        return 0
    previous = (
        CorrelationRun.objects.filter(client=run.client, started_at__lt=run.started_at)
        .exclude(status=CorrelationRun.RunStatus.PENDING)
        .order_by("-started_at")
        .first()
    )

    def snapshot_map(r) -> dict:
        return {
            (s.device_id, s.vendor): s
            for s in r.snapshots.select_related("device")
        }

    before = snapshot_map(previous) if previous else {}
    deltas = []
    for key, snap in snapshot_map(run).items():
        old = before.get(key)
        if old is not None and old.status == snap.status:
            continue
        deltas.append(
            {
                "client": run.client.slug,
                "join_key": snap.device.join_key,
                "hostname": snap.device.hostname,
                "vendor": snap.vendor or None,
                "previous_status": old.status if old else None,
                "status": snap.status,
                "run_id": run.pk,
                "run_started_at": run.started_at.isoformat(),
            }
        )
    return splunk_export.send_deltas(deltas, splunk_config)


# --- the synchronous path -------------------------------------------------------


def run_pipeline_for_client(
        config: AppConfig,
        client_cfg: ClientConfig,
        run: CorrelationRun | None = None,
        drift=None,
) -> CorrelationRun:
    """Collect, correlate, and persist for one client, all in-process.

    This is what the management command calls (demo mode). ``drift`` is an
    optional ``(ad_df, agent_records) -> (ad_df, agent_records)`` transform
    used by seed_demo to synthesize a second, evolved run from the same
    fixtures.
    """
    client = sync_client_from_config(client_cfg)
    if run is None:
        run = CorrelationRun.objects.create(client=client, stale_days=config.stale_days)

    vendor_status: dict[str, str] = {}
    ad_df = collect_ad_frame(config, client_cfg.slug)
    vendor_status["ad"] = "ok"

    agent_records: list[AgentDevice] = []
    for vendor_name in sorted(client_cfg.vendors):
        try:
            agent_records.extend(collect_vendor_inventory(config, client_cfg.slug, vendor_name))
            vendor_status[vendor_name] = "ok"
        except Exception as exc:  # noqa: BLE001 — one vendor down must not sink the run
            logger.warning("%s inventory failed for %s: %s", vendor_name, client_cfg.slug, exc)
            vendor_status[vendor_name] = f"error: {exc}"

    if drift is not None:
        ad_df, agent_records = drift(ad_df, agent_records)

    finalize_run(run, ad_df, agent_records, vendor_status, splunk_config=config.splunk)
    run.refresh_from_db()
    return run
