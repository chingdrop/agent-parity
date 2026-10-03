"""Read run history back for reporting.

The persistence layer only ever wrote history (``CorrelationRun`` and its
``CoverageSnapshot`` rows); this module reads it back as the plain data the
quarterly report renders (``agent_parity.quarterly_report``). Each quarter is
represented by its last *finished* run (``complete`` or ``partial``) — the state
the client was in at quarter end. Pending and failed runs never count.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from agent_parity.correlation import coverage_pct
from agent_parity.quarterly_report import DeviceRow, Quarter, QuarterlyReport, QuarterPoint
from agent_parity.scheduling.db import Client, CorrelationRun, CoverageSnapshot, RunStatus

FINISHED = (RunStatus.COMPLETE.value, RunStatus.PARTIAL.value)


def last_finished_run(session: Session, client_id: int, quarter: Quarter) -> CorrelationRun | None:
    return session.scalar(
        select(CorrelationRun)
        .where(
            CorrelationRun.client_id == client_id,
            CorrelationRun.status.in_(FINISHED),
            CorrelationRun.started_at >= quarter.start,
            CorrelationRun.started_at < quarter.end,
        )
        .order_by(CorrelationRun.started_at.desc())
        .limit(1)
    )


def quarter_point(session: Session, run: CorrelationRun) -> QuarterPoint:
    rows = session.execute(
        select(CoverageSnapshot.status, CoverageSnapshot.machine_type).where(CoverageSnapshot.run_id == run.id)
    ).all()
    counts = Counter(status for status, _ in rows)
    server_counts = Counter(status for status, machine_type in rows if machine_type == "server")
    return QuarterPoint(
        quarter=Quarter.of(run.started_at),
        run_id=run.id,
        run_started_at=run.started_at,
        coverage_pct=coverage_pct(counts),
        server_coverage_pct=coverage_pct(server_counts),
        status_counts=dict(counts),
        server_status_counts=dict(server_counts),
    )


def device_rows(session: Session, run_id: int) -> list[DeviceRow]:
    snapshots = session.scalars(
        select(CoverageSnapshot).where(CoverageSnapshot.run_id == run_id).options(selectinload(CoverageSnapshot.device))
    )
    return [
        DeviceRow(
            hostname=s.device.hostname,
            os=s.device.os,
            status=s.status,
            vendor=s.vendor,
            agent_last_seen=s.agent_last_seen,
            machine_type=s.machine_type,
            eol_status=s.eol_status,
            os_build=s.os_build,
        )
        for s in snapshots
    ]


def build_quarterly_report(
    session: Session, client_slug: str, quarter: Quarter, quarters: int = 4, client_name: str | None = None
) -> QuarterlyReport | None:
    """The report for ``client_slug`` in ``quarter``, with up to ``quarters``
    quarters of trend ending there. ``None`` when the client has no finished
    run in ``quarter`` itself — there's nothing to report on. Earlier quarters
    with no finished run are left out of the trend rather than shown as zero."""
    client = session.scalar(select(Client).where(Client.slug == client_slug))
    if client is None:
        return None
    report_run = last_finished_run(session, client.id, quarter)
    if report_run is None:
        return None

    trend = []
    q = quarter
    for _ in range(quarters):
        run = last_finished_run(session, client.id, q)
        if run is not None:
            trend.append(quarter_point(session, run))
        q = q.previous()

    return QuarterlyReport(
        client_slug=client_slug,
        client_name=client_name or client.name,
        quarter=quarter,
        trend=list(reversed(trend)),
        rows=device_rows(session, report_run.id),
        generated_on=date.today(),
    )


def latest_finished_quarter(session: Session, client_slug: str) -> Quarter | None:
    """The quarter of the client's most recent finished run."""
    started_at = session.scalar(
        select(CorrelationRun.started_at)
        .join(Client)
        .where(Client.slug == client_slug, CorrelationRun.status.in_(FINISHED))
        .order_by(CorrelationRun.started_at.desc())
        .limit(1)
    )
    return Quarter.of(started_at) if started_at else None
