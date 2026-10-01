"""Celery chord behavior: fan-out/fan-in, partial failure, idempotency.

Tasks run eagerly (in-process) via the ``celery_eager`` fixture — no broker
needed; the semantics under test are identical either way. ``sqlite_db``
points every fresh engine ``agent_parity.scheduling.tasks`` opens (one per task
invocation) at the same tmp_path file.
"""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from agent_parity.config import load_config
from agent_parity.connectors import CarbonBlackConnector
from agent_parity.connectors.base import ConnectorError
from agent_parity.scheduling import tasks
from agent_parity.scheduling.db import CorrelationRun, get_engine, init_db, session_factory


def _raise_connector_error(self):
    raise ConnectorError("carbonblack: API returned 503")


def _sessionmaker(db_url):
    engine = get_engine(db_url)
    init_db(engine)
    return session_factory(engine)


def _get_run(db_url, run_id):
    """Fetch a run with its snapshots eagerly loaded so the caller can read
    both after this function's own session has closed."""
    with _sessionmaker(db_url)() as session:
        return session.scalar(
            select(CorrelationRun).options(selectinload(CorrelationRun.snapshots)).where(CorrelationRun.id == run_id)
        )


def test_chord_produces_partial_run_when_one_vendor_fails(celery_eager, sqlite_db, monkeypatch):
    """One flaky vendor API must not prevent the CorrelationRun: the run
    completes as PARTIAL with the failure recorded, and the vendors that
    succeeded still produce snapshots. Acme has two Carbon Black tenants
    (see config.yaml) — both fail identically here, independently keyed."""
    monkeypatch.setattr(CarbonBlackConnector, "fetch_inventory", _raise_connector_error)
    config = load_config()

    run_id = tasks.dispatch_client(config, config.client("acme"))

    run = _get_run(sqlite_db, run_id)
    assert run.status == "partial"
    assert run.vendor_status["carbonblack:0"].startswith("error")
    assert run.vendor_status["carbonblack:branch"].startswith("error")
    assert run.vendor_status["sentinelone"] == "ok"
    assert run.vendor_status["ad:ACME-DC01"] == "ok"
    vendors_persisted = {s.vendor for s in run.snapshots}
    assert "sentinelone" in vendors_persisted
    assert "bitdefender" in vendors_persisted
    assert "carbonblack" not in vendors_persisted


def test_start_client_run_resolves_to_the_callbacks_report(celery_eager, sqlite_db):
    """What `agent-parity run` prints comes back through the chord result —
    JSON-safe, with the classified frame as CSV text when asked for."""
    import io

    import pandas as pd

    config = load_config()

    run_id, pending = tasks.start_client_run(config, config.client("acme"), include_csv=True)
    report = pending.get()

    assert report["run_id"] == run_id
    assert report["status"] == "complete"
    assert report["rows"] == 51
    assert report["coverage_pct"] == 81.8
    assert set(report["vendor_status"]) == {
        "ad:ACME-DC01",
        "sentinelone",
        "carbonblack:0",
        "carbonblack:branch",
        "bitdefender",
    }
    assert len(pd.read_csv(io.StringIO(report["csv"]))) == 51
    assert len(_get_run(sqlite_db, run_id).snapshots) == 51


def test_chord_completes_cleanly_when_all_vendors_succeed(celery_eager, sqlite_db):
    """Globex has two AD domains (see config.yaml) — both domains' export
    tasks must fire and both must show up in vendor_status."""
    config = load_config()

    run_id = tasks.dispatch_client(config, config.client("globex"))

    run = _get_run(sqlite_db, run_id)
    assert run.status == "complete"
    assert set(run.vendor_status) == {
        "ad:GLOBEX-DC01",
        "ad:GLOBEX-BR-DC01",
        "sentinelone",
        "bitdefender",
    }
    assert len(run.snapshots) > 0


def test_chord_isolates_a_malformed_ad_export_to_its_own_domain(celery_eager, sqlite_db, monkeypatch):
    """One domain returning a malformed export fails that domain only: the
    run is PARTIAL with the other domain's devices still persisted."""
    from agent_parity import pipeline

    real_collect_ad_csv = pipeline.collect_ad_csv

    def collect_ad_csv(config, slug, target_device):
        if target_device == "GLOBEX-BR-DC01":
            return "Oops,Something\nbroke,badly\n"
        return real_collect_ad_csv(config, slug, target_device)

    monkeypatch.setattr(pipeline, "collect_ad_csv", collect_ad_csv)
    config = load_config()

    run_id = tasks.dispatch_client(config, config.client("globex"))

    run = _get_run(sqlite_db, run_id)
    assert run.status == "partial"
    assert run.vendor_status["ad:GLOBEX-DC01"] == "ok"
    assert run.vendor_status["ad:GLOBEX-BR-DC01"].startswith("error")
    assert len(run.snapshots) > 0


def test_run_failed_when_ad_export_is_missing(celery_eager, sqlite_db):
    """No AD export means nothing to reconcile against: FAILED, not partial."""
    config = load_config()
    from agent_parity.scheduling.persistence import sync_client_from_config

    with _sessionmaker(sqlite_db)() as session:
        client = sync_client_from_config(session, config.client("acme"))
        run = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
        session.add(run)
        session.commit()
        run_id = run.id

    results = [
        {"source": "ad", "key": "ad:ACME-DC01", "status": "error: target endpoint offline", "csv": None},
        {"source": "sentinelone", "key": "sentinelone", "status": "ok", "records": []},
    ]
    tasks.correlate_client(results, run_id=run_id)

    run = _get_run(sqlite_db, run_id)
    assert run.status == "failed"
    assert len(run.snapshots) == 0


def test_callback_is_idempotent_on_duplicate_delivery(celery_eager, sqlite_db):
    """A retried/double-fired callback must not double-count snapshots —
    the pre-created CorrelationRun id is the idempotency key."""
    config = load_config()
    from agent_parity.pipeline import collect_ad_csv, collect_vendor_inventory
    from agent_parity.scheduling.persistence import sync_client_from_config

    with _sessionmaker(sqlite_db)() as session:
        client = sync_client_from_config(session, config.client("globex"))
        run = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
        session.add(run)
        session.commit()
        run_id = run.id

    csv_text = collect_ad_csv(config, "globex", "GLOBEX-DC01")
    records, _ = collect_vendor_inventory(config, "globex", "sentinelone")
    results = [
        {"source": "ad", "key": "ad:GLOBEX-DC01", "status": "ok", "csv": csv_text},
        {
            "source": "sentinelone",
            "key": "sentinelone",
            "status": "ok",
            "records": [r.to_dict() for r in records],
        },
    ]

    first = tasks.correlate_client(results, run_id=run_id)
    count_after_first = len(_get_run(sqlite_db, run_id).snapshots)
    second = tasks.correlate_client(results, run_id=run_id)

    assert count_after_first > 0
    assert len(_get_run(sqlite_db, run_id).snapshots) == count_after_first
    assert second.get("duplicate") is True
    assert first.get("duplicate") is None


def test_every_registered_connector_gets_a_rate_limited_inventory_task():
    """Vendor tasks are built from the connector registry: one per vendor,
    under a stable task name, with the connector's own rate limit."""
    from agent_parity.connectors import CONNECTOR_CLASSES

    assert set(tasks.VENDOR_TASKS) == set(CONNECTOR_CLASSES)
    for vendor, task in tasks.VENDOR_TASKS.items():
        assert task.name == f"agent_parity.scheduling.tasks.fetch_{vendor}_inventory"
        assert task.rate_limit == CONNECTOR_CLASSES[vendor].inventory_rate_limit


def test_registered_inventory_task_is_a_thin_wrapper_over_collect_vendor_site(celery_eager):
    payload = tasks.VENDOR_TASKS["carbonblack"].delay("acme", 1).get()

    assert payload["source"] == "carbonblack"
    assert payload["key"] == "carbonblack:branch"
    assert payload["status"] == "ok"
    assert payload["records"]


def test_dispatch_all_clients_respects_per_client_cadence(celery_eager, sqlite_db):
    config = load_config()

    first = tasks.dispatch_all_clients()
    assert sorted(first) == sorted(config.clients)

    # Immediately re-dispatching: nobody is due yet (acme=6h, globex=12h).
    second = tasks.dispatch_all_clients()
    assert second == []

    # force=True overrides the cadence check.
    forced = tasks.dispatch_all_clients(force=True)
    assert sorted(forced) == sorted(config.clients)


def test_dispatch_all_clients_first_fails_runs_abandoned_past_the_timeout(celery_eager, sqlite_db):
    from datetime import UTC, datetime, timedelta

    from agent_parity.scheduling.persistence import sync_client_from_config

    config = load_config()
    with _sessionmaker(sqlite_db)() as session:
        client = sync_client_from_config(session, config.client("acme"))
        stuck = CorrelationRun(client_id=client.id, started_at=datetime.now(UTC) - timedelta(hours=48))
        session.add(stuck)
        session.commit()
        stuck_id = stuck.id

    tasks.dispatch_all_clients()

    stuck = _get_run(sqlite_db, stuck_id)
    assert stuck.status == "failed"
    assert stuck.vendor_status["run"].startswith("error: abandoned")


def test_refresh_os_eol_data_task_refreshes_the_cache(celery_eager, monkeypatch):
    from agent_parity import os_eol, os_eol_live

    monkeypatch.setattr(os_eol_live, "fetch_lifecycle_data", lambda timeout: os_eol.load_bundled_data())

    assert tasks.refresh_os_eol_data() == "unchanged"
    assert os_eol.cache_path().exists()


def test_refresh_os_eol_data_task_does_nothing_when_disabled(celery_eager, monkeypatch):
    from dataclasses import replace

    from agent_parity import os_eol

    config = replace(load_config(), refresh_os_eol=False)
    monkeypatch.setattr(tasks, "load_config", lambda: config)

    assert tasks.refresh_os_eol_data() == "disabled"
    assert not os_eol.cache_path().exists()
