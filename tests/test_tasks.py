"""Celery chord behavior: fan-out/fan-in, partial failure, idempotency.

Tasks run eagerly (in-process) via the ``celery_eager`` fixture — no broker
needed; the semantics under test are identical either way. ``sqlite_db``
points every fresh engine ``agent_parity.tasks`` opens (one per task
invocation) at the same tmp_path file.
"""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from agent_parity import tasks
from agent_parity.config import load_config
from agent_parity.connectors import CarbonBlackConnector
from agent_parity.connectors.base import ConnectorError
from agent_parity.db import CorrelationRun, get_engine, init_db, session_factory


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


def test_run_failed_when_ad_export_is_missing(celery_eager, sqlite_db):
    """No AD export means nothing to reconcile against: FAILED, not partial."""
    config = load_config()
    from agent_parity.persistence import sync_client_from_config

    with _sessionmaker(sqlite_db)() as session:
        client = sync_client_from_config(session, config.client("acme"))
        run = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
        session.add(run)
        session.commit()
        run_id = run.id

    results = [
        {
            "source": "ad",
            "target_device": "ACME-DC01",
            "ok": False,
            "error": "target endpoint offline",
        },
        {"source": "sentinelone", "key": "sentinelone", "ok": True, "records": []},
    ]
    tasks.correlate_client(results, run_id=run_id)

    run = _get_run(sqlite_db, run_id)
    assert run.status == "failed"
    assert len(run.snapshots) == 0


def test_callback_is_idempotent_on_duplicate_delivery(celery_eager, sqlite_db):
    """A retried/double-fired callback must not double-count snapshots —
    the pre-created CorrelationRun id is the idempotency key."""
    config = load_config()
    from agent_parity.persistence import sync_client_from_config
    from agent_parity.pipeline import collect_ad_csv, collect_vendor_inventory

    with _sessionmaker(sqlite_db)() as session:
        client = sync_client_from_config(session, config.client("globex"))
        run = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
        session.add(run)
        session.commit()
        run_id = run.id

    csv_text = collect_ad_csv(config, "globex", "GLOBEX-DC01")
    records, _ = collect_vendor_inventory(config, "globex", "sentinelone")
    results = [
        {"source": "ad", "target_device": "GLOBEX-DC01", "ok": True, "csv": csv_text},
        {
            "source": "sentinelone",
            "key": "sentinelone",
            "ok": True,
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
