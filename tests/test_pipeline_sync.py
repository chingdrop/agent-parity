"""End-to-end tests of the collect+correlate pipeline against the fixtures.

These pin the deliberate gap scenarios authored into sample_data/ — if a
fixture edit breaks a scenario, one of these fails by name.
"""

from agent_parity.config import load_config
from agent_parity.models import CoverageStatus
from agent_parity.pipeline import run_correlation


def run_default():
    config = load_config()
    result, vendor_status = run_correlation(config)
    assert result is not None
    return result


def test_fixture_run_exercises_every_coverage_status():
    result = run_default()
    statuses = set(result.frame["status"])
    assert statuses == {s.value for s in CoverageStatus}


def test_known_scenario_devices_classify_as_authored():
    result = run_default()
    frame = result.frame

    def status(join_key):
        return set(frame.loc[frame["join_key"] == join_key, "status"])

    assert status("acme-sql02") == {CoverageStatus.MISSING_AGENT}  # new server, no agent
    assert status("acme-ws-023") == {CoverageStatus.STALE_COVERAGE}  # agent silent
    assert status("acme-byod-lt1") == {CoverageStatus.ORPHANED_AGENT}  # shadow-IT laptop
    assert status("acme-dc02") == {CoverageStatus.COVERED}
