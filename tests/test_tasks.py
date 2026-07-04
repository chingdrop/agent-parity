"""Celery chord behavior: fan-out/fan-in, partial failure, idempotency.

Tasks run eagerly (in-process) — no broker needed — via the ``eager_celery``
fixture; the semantics under test (results-tolerant callback, pre-created
run ID as idempotency key) are identical either way.
"""

import pytest

from agent_parity.config import load_config
from agent_parity.connectors import CarbonBlackConnector
from agent_parity.connectors.base import ConnectorError
from dashboard import services, tasks
from dashboard.models import CorrelationRun, CoverageSnapshot

pytestmark = pytest.mark.django_db


def _raise_connector_error(self):
    raise ConnectorError("carbonblack: API returned 503")


def test_chord_produces_partial_run_when_one_vendor_fails(
        eager_celery, monkeypatch, django_capture_on_commit_callbacks, db_config
):
    """One flaky vendor API must not prevent the CorrelationRun: the run
    completes as PARTIAL with the failure recorded, and the vendors that
    succeeded still produce snapshots. Acme has two Carbon Black tenants
    (see config.yaml) — both fail identically here, independently keyed."""
    monkeypatch.setattr(CarbonBlackConnector, "fetch_inventory", _raise_connector_error)

    with django_capture_on_commit_callbacks(execute=True):
        run_id = tasks.dispatch_client(db_config, db_config.client("acme"))

    run = CorrelationRun.objects.get(pk=run_id)
    assert run.status == CorrelationRun.RunStatus.PARTIAL
    assert run.vendor_status["carbonblack:0"].startswith("error")
    assert run.vendor_status["carbonblack:branch"].startswith("error")
    assert run.vendor_status["sentinelone"] == "ok"
    assert run.vendor_status["ad:ACME-DC01"] == "ok"
    # SentinelOne/BitDefender results were still correlated and persisted.
    assert run.snapshots.filter(vendor="sentinelone").exists()
    assert run.snapshots.filter(vendor="bitdefender").exists()
    assert not run.snapshots.filter(vendor="carbonblack").exists()


def test_chord_completes_cleanly_when_all_vendors_succeed(
        eager_celery, django_capture_on_commit_callbacks, db_config
):
    """Globex has two AD domains (see config.yaml) — both domains' export
    tasks must fire and both must show up in vendor_status."""
    with django_capture_on_commit_callbacks(execute=True):
        run_id = tasks.dispatch_client(db_config, db_config.client("globex"))

    run = CorrelationRun.objects.get(pk=run_id)
    assert run.status == CorrelationRun.RunStatus.COMPLETE
    assert set(run.vendor_status) == {
        "ad:GLOBEX-DC01", "ad:GLOBEX-BR-DC01", "sentinelone", "bitdefender",
    }
    assert run.snapshots.count() > 0


def test_run_failed_when_ad_export_is_missing(eager_celery):
    """No AD export means nothing to reconcile against: FAILED, not partial."""
    config = load_config()
    client = services.sync_client_from_config(config.client("acme"))
    run = CorrelationRun.objects.create(client=client, stale_days=config.stale_days)

    results = [
        {
            "source": "ad",
            "target_device": "ACME-DC01",
            "ok": False,
            "error": "target endpoint offline",
        },
        {"source": "sentinelone", "key": "sentinelone", "ok": True, "records": []},
    ]
    tasks.correlate_client(results, run_id=run.pk)

    run.refresh_from_db()
    assert run.status == CorrelationRun.RunStatus.FAILED
    assert run.snapshots.count() == 0


def test_callback_is_idempotent_on_duplicate_delivery(eager_celery):
    """A retried/double-fired callback must not double-count snapshots —
    the pre-created CorrelationRun ID is the idempotency key."""
    config = load_config()
    client = services.sync_client_from_config(config.client("globex"))
    run = CorrelationRun.objects.create(client=client, stale_days=config.stale_days)

    csv_text = services.collect_ad_csv(config, "globex", "GLOBEX-DC01")
    records, _ = services.collect_vendor_inventory(config, "globex", "sentinelone")
    results = [
        {"source": "ad", "target_device": "GLOBEX-DC01", "ok": True, "csv": csv_text},
        {
            "source": "sentinelone",
            "key": "sentinelone",
            "ok": True,
            "records": [r.to_dict() for r in records],
        },
    ]

    first = tasks.correlate_client(results, run_id=run.pk)
    count_after_first = CoverageSnapshot.objects.filter(run=run).count()
    second = tasks.correlate_client(results, run_id=run.pk)

    assert count_after_first > 0
    assert CoverageSnapshot.objects.filter(run=run).count() == count_after_first
    assert second.get("duplicate") is True
    assert first.get("duplicate") is None


def test_dispatch_all_clients_respects_per_client_cadence(
        eager_celery, django_capture_on_commit_callbacks, db_config
):
    with django_capture_on_commit_callbacks(execute=True):
        first = tasks.dispatch_all_clients()
    assert sorted(first) == sorted(db_config.clients)

    # Immediately re-dispatching: nobody is due yet (acme=6h, globex=12h).
    with django_capture_on_commit_callbacks(execute=True):
        second = tasks.dispatch_all_clients()
    assert second == []

    # force=True overrides the cadence check.
    with django_capture_on_commit_callbacks(execute=True):
        forced = tasks.dispatch_all_clients(force=True)
    assert sorted(forced) == sorted(db_config.clients)
