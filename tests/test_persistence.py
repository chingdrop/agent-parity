"""Persistence-layer tests: idempotency and the FAILED-on-no-AD-data case.

These exercise agent_parity/persistence.py directly (no Celery involved —
that's tests/test_tasks.py's job once Stage 4b lands); same known scenarios
tests/test_pipeline_sync.py already pins for the pure (unpersisted) path.
"""

from agent_parity.config import load_config
from agent_parity.db import CorrelationRun, CoverageSnapshot, RunStatus, get_engine, init_db, session_factory
from agent_parity.persistence import finalize_run, persist_correlation, run_and_persist_for_client, sync_client_from_config


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
            "ad:ACME-DC01", "sentinelone", "carbonblack:0", "carbonblack:branch", "bitdefender",
        }


def test_persist_correlation_is_idempotent_on_duplicate_call():
    config = load_config()
    with _session() as session:
        run = run_and_persist_for_client(session, config, config.client("acme"))
        first_count = session.query(CoverageSnapshot).filter_by(run_id=run.id).count()
        assert first_count > 0

        # Re-correlate the same inputs and try to persist again against the
        # already-finalized run — must no-op, not double the snapshot count.
        from agent_parity.correlation.engine import agents_to_frame, correlate
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
