"""End-to-end tests of the collect+correlate pipeline against the fixtures.

These pin the deliberate gap scenarios authored into sample_data/ — if a
fixture edit breaks a scenario, one of these fails by name.
"""

from agent_parity.config import load_config
from agent_parity.models import CoverageStatus
from agent_parity.pipeline import run_correlation_for_client


def run_acme():
    config = load_config()
    result, vendor_status = run_correlation_for_client(config, config.client("acme"))
    assert result is not None
    return result


def test_fixture_run_exercises_every_coverage_status():
    result = run_acme()
    statuses = set(result.frame["status"])
    assert statuses == {s.value for s in CoverageStatus}


def test_known_scenario_devices_classify_as_authored():
    result = run_acme()
    frame = result.frame

    def status(join_key):
        return set(frame.loc[frame["join_key"] == join_key, "status"])

    assert status("acme-sql02") == {CoverageStatus.MISSING_AGENT}  # new server, no agent
    assert status("acme-ws-023") == {CoverageStatus.STALE_COVERAGE}  # agent silent 25d
    assert status("acme-fs-old") == {CoverageStatus.ORPHANED_AGENT}  # decommissioned
    assert status("acme-ws-014") == {CoverageStatus.COVERED}  # FQDN normalization win
    assert status("acme-dc02") == {CoverageStatus.COVERED}  # reports to two vendors
    assert len(frame[frame["join_key"] == "acme-dc02"]) == 2
