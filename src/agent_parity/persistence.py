"""Run history and idempotent persistence, layered on top of ``pipeline.py``.

``pipeline.run_correlation_for_client``/``correlate_from_csvs`` stay pure —
no persistence, no history — exactly as documented there. This module is
the layer that gives a caller (the ``sync`` CLI subcommand, or a Celery
chord callback — see ``agent_parity.tasks``) a place to record run history
and, critically, to make a chord callback firing twice a no-op rather than
double-counting data.

Historically this same split existed as two packages (``agent_parity`` and
a Django project, ``agent_parity_web``, that consumed it) — folded into one
package now, but the boundary is unchanged: collection/correlation knows
nothing about persistence, and persistence knows nothing about how a result
was collected.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_parity import splunk_export
from agent_parity.config import AppConfig, ClientConfig, SplunkConfig
from agent_parity.correlation import CorrelationResult, agents_to_frame, correlate
from agent_parity.db import Client, CorrelationRun, CoverageSnapshot, Device, RunStatus
from agent_parity.models import AgentDevice
from agent_parity.pipeline import collect_ad_frame, collect_vendor_inventory
from agent_parity.splunk_export import SplunkExportError

logger = logging.getLogger(__name__)


def sync_client_from_config(session: Session, client_cfg: ClientConfig) -> Client:
    """Upsert the ``Client`` identity row from its resolved ``ClientConfig``."""
    client = session.scalar(select(Client).where(Client.slug == client_cfg.slug))
    if client is None:
        client = Client(slug=client_cfg.slug, name=client_cfg.name)
        session.add(client)
    else:
        client.name = client_cfg.name
    session.flush()
    return client


def _first_valid(*values):
    """Return the first non-null value in ``values`` (all expected scalar)."""
    for value in values:
        if value is not None and not bool(pd.isna(value)):
            return value
    return None


def _naive_utc(value: datetime) -> datetime:
    """Strip tzinfo (converting to UTC first if aware).

    SQLite has no native timezone-aware datetime type — SQLAlchemy round-trips
    a value through it as naive, so an aware value freshly computed in this
    process and a value just read back from the database are never
    comparable as-is. Storing (and comparing) everything as naive UTC avoids
    that mismatch entirely rather than juggling aware/naive per call site.
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def persist_correlation(
    session: Session,
    run: CorrelationRun,
    result: CorrelationResult,
    vendor_status: dict[str, str],
) -> int:
    """Load a classified frame into ``CoverageSnapshot`` rows for ``run``.

    Idempotent: if the run has already been finalized (a Celery retry, a
    double-fired chord callback), this is a no-op — the pre-created
    ``CorrelationRun`` id is the idempotency key. Unlike the historical
    Postgres/Django version, SQLite has no real row-level lock to hold across
    the re-check-then-write below — this instead relies on SQLite's own
    writer serialization (one write transaction at a time on the whole
    database), which is adequate at this single-node/demo scale but is a
    real, disclosed difference from a `SELECT ... FOR UPDATE`-backed
    production database, not something to treat as equivalent.
    """
    current = session.get_one(CorrelationRun, run.id)
    if current.status != RunStatus.PENDING.value:
        logger.warning("Run %s already finalized (%s); skipping persist", run.id, current.status)
        return 0

    client = session.get_one(Client, current.client_id)
    frame = result.frame

    existing = {d.join_key: d for d in session.scalars(select(Device).where(Device.client_id == client.id))}
    now = datetime.now(UTC)
    device_rows: dict[str, dict] = {}
    for row in frame.itertuples(index=False):
        seen = _first_valid(getattr(row, "last_seen", None), getattr(row, "last_logon", None))
        info = device_rows.setdefault(str(row.join_key), {"hostname": None, "os": None, "last_seen": None})
        info["hostname"] = info["hostname"] or _first_valid(
            getattr(row, "hostname_ad", None), getattr(row, "hostname_agent", None)
        )
        info["os"] = info["os"] or _first_valid(getattr(row, "os_ad", None), getattr(row, "os_agent", None))
        if seen is not None and (info["last_seen"] is None or seen > info["last_seen"]):
            info["last_seen"] = seen

    devices: dict[str, Device] = {}
    for join_key, info in device_rows.items():
        hostname = str(info["hostname"] or join_key)
        os_name = str(info["os"] or "")
        last_seen = (
            _naive_utc(pd.Timestamp(info["last_seen"]).to_pydatetime()) if info["last_seen"] is not None else None
        )
        if join_key in existing:
            device = existing[join_key]
            device.hostname, device.os = hostname, os_name
            if last_seen and (device.last_seen is None or last_seen > device.last_seen):
                device.last_seen = last_seen
        else:
            device = Device(client_id=client.id, join_key=join_key, hostname=hostname, os=os_name, last_seen=last_seen)
            session.add(device)
        devices[join_key] = device
    session.flush()

    for row in frame.itertuples(index=False):
        session.add(
            CoverageSnapshot(
                run_id=current.id,
                device_id=devices[row.join_key].id,  # type: ignore[index]
                status=row.status,
                vendor="" if pd.isna(row.vendor) else str(row.vendor),
                match_method=row.match_method,
                agent_last_seen=(
                    None if pd.isna(row.last_seen) else _naive_utc(pd.Timestamp(row.last_seen).to_pydatetime())  # type: ignore[arg-type]
                ),
                platform="" if pd.isna(row.platform) else str(row.platform),
                machine_type="" if pd.isna(row.machine_type) else str(row.machine_type),
                eol_status=row.eol_status,
                os_build=None if pd.isna(row.os_build) else int(row.os_build),  # type: ignore[arg-type]
            )
        )

    failed = [name for name, state in vendor_status.items() if state != "ok"]
    current.vendor_status = vendor_status
    current.finished_at = now
    current.status = RunStatus.PARTIAL.value if failed else RunStatus.COMPLETE.value
    session.flush()
    count = len(frame)
    logger.info("Run %s for %s: %d snapshots, status=%s", current.id, client.slug, count, current.status)
    return count


def export_deltas_to_splunk(session: Session, run: CorrelationRun, splunk: SplunkConfig) -> int:
    """Diff this run against the client's previous run and forward transitions.

    Splunk is a pure sink for state transitions, not a snapshot dump — see
    ``agent_parity.splunk_export``'s module docstring for why.
    """
    if not splunk.enabled:
        return 0

    previous = session.scalar(
        select(CorrelationRun)
        .where(
            CorrelationRun.client_id == run.client_id,
            CorrelationRun.started_at < run.started_at,
            CorrelationRun.status != RunStatus.PENDING.value,
        )
        .order_by(CorrelationRun.started_at.desc())
        .limit(1)
    )

    def snapshot_map(r: CorrelationRun | None) -> dict[tuple[int, str], CoverageSnapshot]:
        if r is None:
            return {}
        rows = session.scalars(select(CoverageSnapshot).where(CoverageSnapshot.run_id == r.id))
        return {(s.device_id, s.vendor): s for s in rows}

    before = snapshot_map(previous)
    current_snapshots = session.scalars(select(CoverageSnapshot).where(CoverageSnapshot.run_id == run.id)).all()
    client = session.get_one(Client, run.client_id)

    deltas = []
    for snap in current_snapshots:
        old = before.get((snap.device_id, snap.vendor))
        if old is not None and old.status == snap.status:
            continue
        device = session.get_one(Device, snap.device_id)
        deltas.append(
            {
                "client": client.slug,
                "join_key": device.join_key,
                "hostname": device.hostname,
                "vendor": snap.vendor or None,
                "previous_status": old.status if old else None,
                "status": snap.status,
                "run_id": run.id,
                "run_started_at": run.started_at.isoformat(),
            }
        )
    return splunk_export.send_deltas(deltas, splunk)


def finalize_run(
    session: Session,
    run: CorrelationRun,
    ad_df: pd.DataFrame | None,
    agent_records: list[AgentDevice],
    vendor_status: dict[str, str],
    splunk: SplunkConfig | None = None,
) -> int:
    """Correlate + persist + (optionally) forward deltas — the shared fan-in
    for both entrypoints below.

    ``ad_df`` is ``None`` when every one of a client's domains failed to
    export (see ``pipeline.collect_ad_frame``) — there's nothing to
    correlate against, so the run fails outright rather than partially.
    """
    if ad_df is None:
        run.status = RunStatus.FAILED.value
        run.vendor_status = vendor_status
        run.finished_at = datetime.now(UTC)
        session.flush()
        return 0
    result = correlate(ad_df, agents_to_frame(agent_records), stale_days=run.stale_days)
    count = persist_correlation(session, run, result, vendor_status)
    if count and splunk is not None:
        try:
            export_deltas_to_splunk(session, run, splunk)
        except SplunkExportError:
            # A reporting sink outage must never fail the run itself.
            logger.exception("Splunk delta export failed for run %s", run.id)
    return count


def run_and_persist_for_client(session: Session, config: AppConfig, client_cfg: ClientConfig) -> CorrelationRun:
    """Collect, correlate, and persist for one client, all in-process.

    This is what the ``sync`` CLI subcommand calls (demo/single-node path);
    ``agent_parity.tasks.correlate_client`` is the Celery chord callback that
    calls ``finalize_run`` the same way from fanned-out results instead.
    """
    client = sync_client_from_config(session, client_cfg)
    run = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
    session.add(run)
    session.flush()

    ad_df, vendor_status = collect_ad_frame(config, client_cfg.slug)

    agent_records: list[AgentDevice] = []
    for vendor_name in sorted(client_cfg.vendors):
        records, site_status = collect_vendor_inventory(config, client_cfg.slug, vendor_name)
        agent_records.extend(records)
        vendor_status.update(site_status)

    finalize_run(session, run, ad_df, agent_records, vendor_status, splunk=config.splunk)
    session.commit()
    return run
