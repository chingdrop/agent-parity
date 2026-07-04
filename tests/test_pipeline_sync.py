"""End-to-end tests of the synchronous (demo-mode) path against the fixtures.

These pin the deliberate gap scenarios authored into sample_data/ — if a
fixture edit breaks a scenario, one of these fails by name.
"""

import pytest
from django.core.management import call_command

from agent_parity.config import load_config
from agent_parity.models import CoverageStatus
from dashboard import services
from dashboard.models import CorrelationRun, Device

pytestmark = pytest.mark.django_db


def run_acme() -> CorrelationRun:
    config = load_config()
    return services.run_pipeline_for_client(config, config.client("acme"))


def test_fixture_run_exercises_every_coverage_status():
    run = run_acme()
    assert run.status == CorrelationRun.RunStatus.COMPLETE
    statuses = set(run.snapshots.values_list("status", flat=True))
    assert statuses == {s.value for s in CoverageStatus}


def test_known_scenario_devices_classify_as_authored():
    run = run_acme()

    def status(join_key):
        return set(
            run.snapshots.filter(device__join_key=join_key).values_list("status", flat=True)
        )

    assert status("acme-sql02") == {CoverageStatus.MISSING_AGENT}  # new server, no agent
    assert status("acme-ws-023") == {CoverageStatus.STALE_COVERAGE}  # agent silent 25d
    assert status("acme-fs-old") == {CoverageStatus.ORPHANED_AGENT}  # decommissioned
    assert status("acme-ws-014") == {CoverageStatus.COVERED}  # FQDN normalization win
    assert status("acme-dc02") == {CoverageStatus.COVERED}  # reports to two vendors
    assert run.snapshots.filter(device__join_key="acme-dc02").count() == 2


def test_sync_and_correlate_command_defaults_to_first_client(db_config, capsys=None):
    # sync_and_correlate assumes the DB is already populated (via seed_demo,
    # `manage.py import_config`, or the setup page) — it doesn't bootstrap
    # from config.yaml itself.
    call_command("sync_and_correlate")
    run = CorrelationRun.objects.get()
    assert run.client.slug == "acme"  # first alphabetically
    assert run.status == CorrelationRun.RunStatus.COMPLETE


def test_seed_demo_creates_two_runs_with_drift():
    call_command("seed_demo")
    for slug in ("acme", "globex"):
        runs = CorrelationRun.objects.filter(client__slug=slug).order_by("started_at")
        assert runs.count() == 2
        run1, run2 = runs
        assert run1.started_at < run2.started_at

        # Drift scenario 4: a brand-new AD-only workstation appears in run 2.
        new_device = Device.objects.get(client__slug=slug, join_key__endswith="-ws-new1")
        assert not run1.snapshots.filter(device=new_device).exists()
        assert set(
            run2.snapshots.filter(device=new_device).values_list("status", flat=True)
        ) == {CoverageStatus.MISSING_AGENT}

    # Drift scenario 1: acme's first missing device got remediated in run 2.
    run1, run2 = CorrelationRun.objects.filter(client__slug="acme").order_by("started_at")

    def statuses(run, join_key):
        return set(run.snapshots.filter(device__join_key=join_key).values_list("status", flat=True))

    remediated = "acme-lt-009"  # first AD-only join key alphabetically
    assert statuses(run1, remediated) == {CoverageStatus.MISSING_AGENT}
    assert statuses(run2, remediated) == {CoverageStatus.COVERED}
