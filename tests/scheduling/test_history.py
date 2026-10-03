"""Tests for agent_parity/scheduling/history.py: reading run history back as
quarterly report data."""

from datetime import datetime

from agent_parity.quarterly_report import Quarter
from agent_parity.scheduling.db import (
    Client,
    CorrelationRun,
    CoverageSnapshot,
    Device,
    RunStatus,
    get_engine,
    init_db,
    session_factory,
)
from agent_parity.scheduling.history import build_quarterly_report, latest_finished_quarter


def _session():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    return session_factory(engine)()


def _run(session, client, started_at, statuses, status=RunStatus.COMPLETE.value):
    """A run with one snapshot per (hostname, status, machine_type)."""
    run = CorrelationRun(client_id=client.id, started_at=started_at, status=status)
    session.add(run)
    session.flush()
    for hostname, row_status, machine_type in statuses:
        device = session.query(Device).filter_by(client_id=client.id, join_key=hostname.lower()).one_or_none()
        if device is None:
            device = Device(client_id=client.id, join_key=hostname.lower(), hostname=hostname, os="Windows 11")
            session.add(device)
            session.flush()
        session.add(CoverageSnapshot(run_id=run.id, device_id=device.id, status=row_status, machine_type=machine_type))
    session.flush()
    return run


def _client(session):
    client = Client(slug="acme", name="Acme Corp")
    session.add(client)
    session.flush()
    return client


def test_each_quarter_is_its_last_finished_run():
    with _session() as session:
        client = _client(session)
        _run(session, client, datetime(2026, 7, 5), [("WS-1", "missing_agent", "desktop")])
        last = _run(session, client, datetime(2026, 9, 20), [("WS-1", "covered", "desktop")])
        _run(session, client, datetime(2026, 9, 25), [("WS-1", "missing_agent", "desktop")], RunStatus.FAILED.value)
        _run(session, client, datetime(2026, 9, 26), [], RunStatus.PENDING.value)

        built = build_quarterly_report(session, "acme", Quarter(2026, 3))

    assert built.current.run_id == last.id
    assert built.current.coverage_pct == 100.0


def test_coverage_counts_ad_known_rows_and_servers_separately():
    with _session() as session:
        client = _client(session)
        _run(
            session,
            client,
            datetime(2026, 9, 1),
            [
                ("SRV-1", "covered", "server"),
                ("SRV-2", "missing_agent", "server"),
                ("WS-1", "covered", "desktop"),
                ("WS-2", "stale_coverage", "desktop"),
                ("ORPHAN", "orphaned_agent", "desktop"),  # no AD record: not in the denominator
            ],
        )

        point = build_quarterly_report(session, "acme", Quarter(2026, 3)).current

    assert point.coverage_pct == 50.0  # 2 covered of 4 AD-known rows
    assert point.server_coverage_pct == 50.0
    assert point.status_counts["orphaned_agent"] == 1


def test_the_trend_runs_oldest_first_and_skips_quarters_with_no_finished_run():
    with _session() as session:
        client = _client(session)
        _run(session, client, datetime(2026, 2, 1), [("WS-1", "missing_agent", "desktop")])
        _run(session, client, datetime(2026, 8, 1), [("WS-1", "covered", "desktop")])

        built = build_quarterly_report(session, "acme", Quarter(2026, 3), quarters=4)

    assert [str(p.quarter) for p in built.trend] == ["2026-Q1", "2026-Q3"]
    assert [p.coverage_pct for p in built.trend] == [0.0, 100.0]


def test_device_rows_come_from_the_report_quarters_run():
    with _session() as session:
        client = _client(session)
        _run(
            session,
            client,
            datetime(2026, 8, 1),
            [("WS-1", "covered", "desktop"), ("SRV-1", "missing_agent", "server")],
        )

        built = build_quarterly_report(session, "acme", Quarter(2026, 3), client_name="Acme (from config)")

    assert built.client_name == "Acme (from config)"
    assert {(r.hostname, r.status) for r in built.rows} == {("WS-1", "covered"), ("SRV-1", "missing_agent")}


def test_no_report_without_a_finished_run_in_the_quarter_or_an_unknown_client():
    with _session() as session:
        client = _client(session)
        _run(session, client, datetime(2026, 5, 1), [("WS-1", "covered", "desktop")])

        assert build_quarterly_report(session, "acme", Quarter(2026, 3)) is None
        assert build_quarterly_report(session, "nobody", Quarter(2026, 2)) is None


def test_latest_finished_quarter_ignores_failed_runs():
    with _session() as session:
        client = _client(session)
        assert latest_finished_quarter(session, "acme") is None
        _run(session, client, datetime(2026, 5, 1), [])
        _run(session, client, datetime(2026, 8, 1), [], RunStatus.FAILED.value)

        assert latest_finished_quarter(session, "acme") == Quarter(2026, 2)
