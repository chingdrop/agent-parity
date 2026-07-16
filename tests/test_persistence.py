"""Persistence-layer tests: idempotency, the FAILED-on-no-AD-data case, and
Splunk coverage-delta export.

These exercise agent_parity/persistence.py directly (no Celery involved —
that's tests/test_tasks.py's job); same known scenarios
tests/test_pipeline_sync.py already pins for the pure (unpersisted) path.
"""

from datetime import UTC, datetime, timedelta

from agent_parity.config import SplunkConfig, load_config
from agent_parity.db import CorrelationRun, CoverageSnapshot, RunStatus, get_engine, init_db, session_factory
from agent_parity.persistence import (
    SplunkExportError,
    export_deltas_to_splunk,
    finalize_run,
    persist_correlation,
    run_and_persist_for_client,
    sync_client_from_config,
)


def _session():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    return session_factory(engine)()


def test_finalize_run_marks_failed_when_ad_data_is_missing():
    config = load_config()
    with _session() as session:
        client = sync_client_from_config(session, config.client("acme"))
        run = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
        session.add(run)
        session.flush()

        count = finalize_run(session, run, None, [], {"ad:ACME-DC01": "error: offline"})
        session.commit()

        assert count == 0
        assert run.status == RunStatus.FAILED.value
        assert run.finished_at is not None
        assert session.query(CoverageSnapshot).filter_by(run_id=run.id).count() == 0


def test_run_and_persist_for_client_persists_acmes_fixture_run():
    config = load_config()
    with _session() as session:
        run = run_and_persist_for_client(session, config, config.client("acme"))

        assert run.status == RunStatus.COMPLETE.value
        assert run.finished_at is not None
        snapshots = session.query(CoverageSnapshot).filter_by(run_id=run.id).all()
        assert len(snapshots) == 51  # matches run --client acme's row count
        assert set(run.vendor_status) == {
            "ad:ACME-DC01",
            "sentinelone",
            "carbonblack:0",
            "carbonblack:branch",
            "bitdefender",
        }


def test_persist_correlation_is_idempotent_on_duplicate_call():
    config = load_config()
    with _session() as session:
        run = run_and_persist_for_client(session, config, config.client("acme"))
        first_count = session.query(CoverageSnapshot).filter_by(run_id=run.id).count()
        assert first_count > 0

        # Re-correlate the same inputs and try to persist again against the
        # already-finalized run — must no-op, not double the snapshot count.
        from agent_parity.correlation import agents_to_frame, correlate
        from agent_parity.pipeline import collect_ad_frame, collect_vendor_inventory

        ad_df, vendor_status = collect_ad_frame(config, "acme")
        agent_records = []
        for vendor_name in sorted(config.client("acme").vendors):
            records, site_status = collect_vendor_inventory(config, "acme", vendor_name)
            agent_records.extend(records)
            vendor_status.update(site_status)
        result = correlate(ad_df, agents_to_frame(agent_records), stale_days=config.stale_days)

        second_return = persist_correlation(session, run, result, vendor_status)
        session.commit()

        assert second_return == 0
        assert session.query(CoverageSnapshot).filter_by(run_id=run.id).count() == first_count


# --- Splunk delta export ----------------------------------------------------


def _splunk_config() -> SplunkConfig:
    return SplunkConfig(hec_url="https://splunk.example:8088", hec_token="tok")


def _capturing_send_deltas(captured):
    def _send(deltas, splunk):
        captured["deltas"] = deltas
        return len(deltas)

    return _send


def _make_run(session, client, started_at, snapshots):
    """A CorrelationRun with a fixed set of (device, vendor, status) snapshots,
    built directly rather than through the full pipeline — export_deltas_to_splunk
    only cares about run history shape, not how it got there."""

    run = CorrelationRun(
        client_id=client.id,
        stale_days=14,
        started_at=started_at,
        status=RunStatus.COMPLETE.value,
        finished_at=started_at,
    )
    session.add(run)
    session.flush()
    for device, vendor, status in snapshots:
        session.add(CoverageSnapshot(run_id=run.id, device_id=device.id, status=status, vendor=vendor))
    session.flush()
    return run


def test_export_deltas_emits_every_snapshot_as_new_when_no_previous_run(monkeypatch):
    from agent_parity.db import Client, Device

    captured: dict = {}
    monkeypatch.setattr("agent_parity.persistence.splunk_export.send_deltas", _capturing_send_deltas(captured))

    with _session() as session:
        client = Client(slug="acme", name="Acme Corp")
        session.add(client)
        session.flush()
        device = Device(client_id=client.id, join_key="acme-ws-001", hostname="ACME-WS-001")
        session.add(device)
        session.flush()

        run = _make_run(session, client, datetime.now(UTC), [(device, "sentinelone", "covered")])
        session.commit()

        count = export_deltas_to_splunk(session, run, _splunk_config())

    assert count == 1
    assert len(captured["deltas"]) == 1
    delta = captured["deltas"][0]
    assert delta["previous_status"] is None
    assert delta["status"] == "covered"
    assert delta["join_key"] == "acme-ws-001"
    assert delta["client"] == "acme"


def test_export_deltas_only_emits_changed_statuses(monkeypatch):
    from agent_parity.db import Client, Device

    captured: dict = {}
    monkeypatch.setattr("agent_parity.persistence.splunk_export.send_deltas", _capturing_send_deltas(captured))

    with _session() as session:
        client = Client(slug="acme", name="Acme Corp")
        session.add(client)
        session.flush()
        unchanged = Device(client_id=client.id, join_key="acme-ws-001", hostname="ACME-WS-001")
        changed = Device(client_id=client.id, join_key="acme-ws-002", hostname="ACME-WS-002")
        session.add_all([unchanged, changed])
        session.flush()

        now = datetime.now(UTC)
        _make_run(
            session,
            client,
            now - timedelta(hours=1),
            [(unchanged, "sentinelone", "covered"), (changed, "carbonblack", "missing_agent")],
        )
        second = _make_run(
            session,
            client,
            now,
            [(unchanged, "sentinelone", "covered"), (changed, "carbonblack", "orphaned_agent")],
        )
        session.commit()

        count = export_deltas_to_splunk(session, second, _splunk_config())

    assert count == 1
    assert len(captured["deltas"]) == 1
    delta = captured["deltas"][0]
    assert delta["join_key"] == "acme-ws-002"
    assert delta["previous_status"] == "missing_agent"
    assert delta["status"] == "orphaned_agent"


def test_export_deltas_returns_zero_when_nothing_changed(monkeypatch):
    from agent_parity.db import Client, Device

    captured: dict = {}
    monkeypatch.setattr("agent_parity.persistence.splunk_export.send_deltas", _capturing_send_deltas(captured))

    with _session() as session:
        client = Client(slug="acme", name="Acme Corp")
        session.add(client)
        session.flush()
        device = Device(client_id=client.id, join_key="acme-ws-001", hostname="ACME-WS-001")
        session.add(device)
        session.flush()

        now = datetime.now(UTC)
        _make_run(session, client, now - timedelta(hours=1), [(device, "sentinelone", "covered")])
        second = _make_run(session, client, now, [(device, "sentinelone", "covered")])
        session.commit()

        count = export_deltas_to_splunk(session, second, _splunk_config())

    assert count == 0
    assert captured["deltas"] == []


def test_export_deltas_is_a_noop_when_splunk_is_not_configured():
    from agent_parity.db import Client, Device

    with _session() as session:
        client = Client(slug="acme", name="Acme Corp")
        session.add(client)
        session.flush()
        device = Device(client_id=client.id, join_key="acme-ws-001", hostname="ACME-WS-001")
        session.add(device)
        session.flush()

        run = _make_run(session, client, datetime.now(UTC), [(device, "sentinelone", "covered")])
        session.commit()

        count = export_deltas_to_splunk(session, run, SplunkConfig())  # unconfigured

    assert count == 0


def test_finalize_run_does_not_fail_when_splunk_export_raises(monkeypatch):
    def _raise(*args, **kwargs):
        raise SplunkExportError("HEC unreachable")

    monkeypatch.setattr("agent_parity.persistence.export_deltas_to_splunk", _raise)

    config = load_config()
    with _session() as session:
        client = sync_client_from_config(session, config.client("acme"))
        run = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
        session.add(run)
        session.flush()

        from agent_parity.pipeline import collect_ad_frame, collect_vendor_inventory

        ad_df, vendor_status = collect_ad_frame(config, "acme")
        agent_records = []
        for vendor_name in sorted(config.client("acme").vendors):
            records, site_status = collect_vendor_inventory(config, "acme", vendor_name)
            agent_records.extend(records)
            vendor_status.update(site_status)

        count = finalize_run(session, run, ad_df, agent_records, vendor_status, splunk=_splunk_config())
        session.commit()

    assert count == 51
    assert run.status == RunStatus.COMPLETE.value
