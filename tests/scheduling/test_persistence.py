"""Persistence-layer tests: idempotency, the FAILED-on-no-AD-data case, and
the Splunk export.

These exercise agent_parity/scheduling/persistence.py directly (no Celery involved —
that's tests/scheduling/test_tasks.py's job); same known scenarios
tests/test_pipeline_sync.py already pins for the pure (unpersisted) path.
"""

import json
from datetime import UTC

from agent_parity.config import SplunkConfig, load_config
from agent_parity.scheduling.db import CorrelationRun, CoverageSnapshot, RunStatus, get_engine, init_db, session_factory
from agent_parity.scheduling.persistence import (
    SplunkExportError,
    export_run_to_splunk,
    finalize_run,
    persist_correlation,
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

        result = finalize_run(session, run, None, [], {"ad:ACME-DC01": "error: offline"})
        session.commit()

        assert result is None
        assert run.status == RunStatus.FAILED.value
        assert run.finished_at is not None
        assert session.query(CoverageSnapshot).filter_by(run_id=run.id).count() == 0


def _finalize_acme_run(session, splunk=None):
    """Collect acme in-process and finalize a fresh run with it — what the
    chord callback does, minus Celery."""
    from agent_parity.pipeline import collect_ad_frame, collect_vendor_inventory

    config = load_config()
    client = sync_client_from_config(session, config.client("acme"))
    run = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
    session.add(run)
    session.flush()

    ad_df, vendor_status = collect_ad_frame(config, "acme")
    agent_records = []
    for vendor_name in sorted(config.client("acme").vendors):
        records, site_status = collect_vendor_inventory(config, "acme", vendor_name)
        agent_records.extend(records)
        vendor_status.update(site_status)

    result = finalize_run(session, run, ad_df, agent_records, vendor_status, splunk=splunk)
    session.commit()
    return run, result


def test_finalize_run_persists_acmes_fixture_run():
    with _session() as session:
        run, result = _finalize_acme_run(session)

        assert result is not None
        assert len(result.frame) == 51
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
    with _session() as session:
        run, result = _finalize_acme_run(session)
        first_count = session.query(CoverageSnapshot).filter_by(run_id=run.id).count()
        assert first_count > 0
        assert result is not None

        # Persist the same result again against the already-finalized run —
        # must no-op, not double the snapshot count.
        vendor_status = dict(run.vendor_status)
        second_return = persist_correlation(session, run, result, vendor_status)
        session.commit()

        assert second_return == 0
        assert session.query(CoverageSnapshot).filter_by(run_id=run.id).count() == first_count


# --- Splunk export: the whole run, one event per row plus a summary --------


def _splunk_config() -> SplunkConfig:
    return SplunkConfig(hec_url="https://splunk.example:8088", hec_token="tok")


def _capture_send_run(monkeypatch):
    calls = []

    def _send(rows, summary, splunk, *, event_time):
        calls.append({"rows": rows, "summary": summary, "event_time": event_time})
        return len(rows) + 1

    monkeypatch.setattr("agent_parity.scheduling.persistence.splunk_export.send_run", _send)
    return calls


def test_a_finalized_run_is_sent_as_one_event_per_row_plus_a_summary(monkeypatch):
    calls = _capture_send_run(monkeypatch)

    with _session() as session:
        run, result = _finalize_acme_run(session, splunk=_splunk_config())
        started_at = run.started_at.replace(tzinfo=UTC)

    assert len(calls) == 1
    rows, summary = calls[0]["rows"], calls[0]["summary"]
    assert len(rows) == len(result.frame) == 51
    assert {(r["client"], r["run_id"]) for r in rows} == {("acme", run.id)}
    assert {r["run_started_at"] for r in rows} == {started_at.isoformat()}
    assert "_merge" not in rows[0]
    assert {"join_key", "status", "vendor", "machine_type", "eol_status"} <= rows[0].keys()
    assert next(r for r in rows if r["join_key"] == "acme-sql02")["status"] == "missing_agent"
    assert summary["client"] == "acme"
    assert summary["run_id"] == run.id
    assert summary["status"] == RunStatus.COMPLETE.value
    assert summary["coverage_pct"] == 81.8
    assert summary["status_counts"]["missing_agent"] == 5
    assert set(summary["vendor_status"]) >= {"ad:ACME-DC01", "sentinelone"}
    assert calls[0]["event_time"] == started_at.timestamp()
    # Everything must survive the trip to HEC as plain JSON.
    json.dumps(rows)
    json.dumps(summary)


def test_a_run_after_a_failed_run_sends_exactly_its_own_rows(monkeypatch):
    """No previous-run baseline is involved, so a FAILED run (no snapshots)
    can't make the next run re-send or mislabel anything."""
    calls = _capture_send_run(monkeypatch)
    config = load_config()

    with _session() as session:
        client = sync_client_from_config(session, config.client("acme"))
        failed = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
        session.add(failed)
        session.flush()
        finalize_run(session, failed, None, [], {"ad:ACME-DC01": "error: offline"}, splunk=_splunk_config())
        session.commit()

        run, _ = _finalize_acme_run(session, splunk=_splunk_config())

    assert len(calls) == 1  # the failed run sends nothing
    assert len(calls[0]["rows"]) == 51
    assert {r["run_id"] for r in calls[0]["rows"]} == {run.id}


def test_export_run_to_splunk_is_a_noop_when_splunk_is_not_configured(monkeypatch):
    calls = _capture_send_run(monkeypatch)

    with _session() as session:
        run, result = _finalize_acme_run(session)
        assert export_run_to_splunk(session, run, result, SplunkConfig()) == 0

    assert calls == []


def test_finalize_run_does_not_fail_when_splunk_export_raises(monkeypatch):
    def _raise(*args, **kwargs):
        raise SplunkExportError("HEC unreachable")

    monkeypatch.setattr("agent_parity.scheduling.persistence.export_run_to_splunk", _raise)

    with _session() as session:
        run, result = _finalize_acme_run(session, splunk=_splunk_config())

    assert result is not None
    assert len(result.frame) == 51
    assert run.status == RunStatus.COMPLETE.value
