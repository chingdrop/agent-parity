"""Run history and idempotent persistence, layered on top of ``pipeline.py``.

``pipeline.run_correlation_for_client``/``correlate_from_csvs`` stay pure —
no persistence, no history — exactly as documented there. This module is
the layer that gives the Celery chord callback (``agent_parity.scheduling.tasks``,
which both ``agent-parity run`` and beat go through) a place to record run history
and, critically, to make a chord callback firing twice a no-op rather than
double-counting data.

The boundary runs both ways: collection/correlation knows nothing about
persistence, and persistence knows nothing about how a result was
collected.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_parity import splunk_export
from agent_parity.config import ClientConfig, SplunkConfig
from agent_parity.correlation import CorrelationResult, agents_to_frame, correlate
from agent_parity.models import AgentDevice
from agent_parity.scheduling.db import Client, CorrelationRun, CoverageSnapshot, Device, RunStatus
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
    ``CorrelationRun`` id is the idempotency key. Unlike Postgres
    (``SELECT ... FOR UPDATE``), SQLite has no real row-level lock to hold across
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


def fail_abandoned_runs(session: Session, older_than: timedelta, now: datetime | None = None) -> list[int]:
    """Mark PENDING runs that started more than ``older_than`` ago as FAILED.

    A run is left PENDING forever when its process dies before the chord
    callback runs (Ctrl-C during an in-process ``agent-parity run``, a worker
    killed mid-run). Returns the ids it marked. The reason is recorded under
    ``vendor_status["run"]`` so it shows wherever runs are read. If such a run's
    callback does fire later, ``persist_correlation`` sees it is no longer
    PENDING and discards the late result rather than reviving it.
    """
    now = _naive_utc(now or datetime.now(UTC))
    abandoned = session.scalars(
        select(CorrelationRun).where(
            CorrelationRun.status == RunStatus.PENDING.value,
            CorrelationRun.started_at < now - older_than,
        )
    ).all()
    hours = older_than.total_seconds() / 3600
    for run in abandoned:
        logger.warning("Run %s still pending after %g h; marking it failed", run.id, hours)
        run.status = RunStatus.FAILED.value
        run.finished_at = now
        run.vendor_status = {**(run.vendor_status or {}), "run": f"error: abandoned, still pending after {hours:g} h"}
    session.flush()
    return [run.id for run in abandoned]


def export_run_to_splunk(session: Session, run: CorrelationRun, result: CorrelationResult, splunk: SplunkConfig) -> int:
    """Send a finalized run to Splunk: one event per frame row, then a summary.

    The whole run, every time — the dashboard shows the latest run, and the
    summaries chart the trend (see ``agent_parity.splunk_export``). Each row
    and the summary carry the client, run id and run start, so rows from one
    run can be selected together.
    """
    if not splunk.enabled:
        return 0

    client = session.get_one(Client, run.client_id)
    started_at = _naive_utc(run.started_at).replace(tzinfo=UTC)
    context = {"client": client.slug, "run_id": run.id, "run_started_at": started_at.isoformat()}
    # to_json handles NaN -> null, timestamps -> ISO 8601 and numpy scalars in one pass.
    # _merge is pandas' merge indicator, already folded into status.
    frame = result.frame.drop(columns=["_merge"], errors="ignore")
    records = json.loads(frame.to_json(orient="records", date_format="iso"))
    rows = [{**context, **record} for record in records]
    summary = {
        **context,
        "status": run.status,
        "vendor_status": run.vendor_status,
        **json.loads(json.dumps(result.summary, default=_json_scalar)),
    }
    return splunk_export.send_run(rows, summary, splunk, event_time=started_at.timestamp())


def _json_scalar(value):
    """``json.dumps`` fallback for numpy scalars (``by_vendor``'s counts)."""
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def persist_result(
    session: Session,
    run: CorrelationRun,
    result: CorrelationResult | None,
    vendor_status: dict[str, str],
    splunk: SplunkConfig | None = None,
) -> int:
    """Persist an already-correlated result + (optionally) send it to Splunk.

    ``result`` is ``None`` when every one of a client's domains failed to
    export (see ``pipeline.collect_ad_frame``) — there's nothing to
    correlate against, so the run fails outright rather than partially.
    """
    if result is None:
        run.status = RunStatus.FAILED.value
        run.vendor_status = vendor_status
        run.finished_at = datetime.now(UTC)
        session.flush()
        return 0
    count = persist_correlation(session, run, result, vendor_status)
    if count and splunk is not None:
        try:
            export_run_to_splunk(session, run, result, splunk)
        except SplunkExportError:
            # A reporting sink outage must never fail the run itself.
            logger.exception("Splunk export failed for run %s", run.id)
    return count


def finalize_run(
    session: Session,
    run: CorrelationRun,
    ad_df: pd.DataFrame | None,
    agent_records: list[AgentDevice],
    vendor_status: dict[str, str],
    splunk: SplunkConfig | None = None,
) -> CorrelationResult | None:
    """Correlate + persist — the chord callback's fan-in.

    ``ad_df`` is ``None`` when every AD domain failed; see ``persist_result``.
    Returns the correlation result (``None`` in that case) so the callback
    can report on it without re-correlating.
    """
    result = correlate(ad_df, agents_to_frame(agent_records), stale_days=run.stale_days) if ad_df is not None else None
    persist_result(session, run, result, vendor_status, splunk=splunk)
    return result
