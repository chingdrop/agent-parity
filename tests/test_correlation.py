"""Tests for the classification logic itself — the four CoverageStatus
outcomes and the merge invariants — not re-verification of pd.merge."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from agent_parity.correlation.engine import agents_to_frame, correlate
from agent_parity.models import AgentDevice, CoverageStatus

AS_OF = pd.Timestamp("2026-07-03T00:00:00Z")
RECENT = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)  # < 1 day old
STALE = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # > 14 days old


def ad_frame(*hostnames: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "hostname": list(hostnames),
            "os": ["Windows 11"] * len(hostnames),
            "last_logon": [AS_OF - timedelta(days=1)] * len(hostnames),
        }
    )


def agent(hostname: str, last_seen=RECENT, vendor="sentinelone") -> AgentDevice:
    return AgentDevice(
        vendor=vendor, agent_id=f"id-{hostname}", hostname=hostname, last_seen=last_seen
    )


def status_of(result, join_key: str, vendor=None) -> str:
    frame = result.frame
    rows = frame[frame["join_key"] == join_key]
    if vendor is not None:
        rows = rows[rows["vendor"] == vendor]
    assert len(rows) == 1, f"expected one row for {join_key}, got {len(rows)}"
    return rows.iloc[0]["status"]


def test_covered_when_matched_and_recent():
    result = correlate(ad_frame("WS-01"), agents_to_frame([agent("WS-01")]), as_of=AS_OF)
    assert status_of(result, "ws-01") == CoverageStatus.COVERED


def test_missing_agent_when_in_ad_only():
    result = correlate(ad_frame("WS-01", "WS-02"), agents_to_frame([agent("WS-01")]), as_of=AS_OF)
    assert status_of(result, "ws-02") == CoverageStatus.MISSING_AGENT


def test_orphaned_agent_when_agent_only():
    result = correlate(ad_frame("WS-01"), agents_to_frame([agent("WS-01"), agent("GHOST-9")]), as_of=AS_OF)
    assert status_of(result, "ghost-9") == CoverageStatus.ORPHANED_AGENT


def test_stale_coverage_when_matched_but_old_checkin():
    result = correlate(ad_frame("WS-01"), agents_to_frame([agent("WS-01", last_seen=STALE)]), as_of=AS_OF)
    assert status_of(result, "ws-01") == CoverageStatus.STALE_COVERAGE


def test_matched_agent_with_no_last_seen_is_stale_not_covered():
    result = correlate(ad_frame("WS-01"), agents_to_frame([agent("WS-01", last_seen=None)]), as_of=AS_OF)
    assert status_of(result, "ws-01") == CoverageStatus.STALE_COVERAGE


def test_stale_threshold_is_configurable():
    twenty_days_old = agent("WS-01", last_seen=(AS_OF - timedelta(days=20)).to_pydatetime())
    lenient = correlate(ad_frame("WS-01"), agents_to_frame([twenty_days_old]), stale_days=30, as_of=AS_OF)
    strict = correlate(ad_frame("WS-01"), agents_to_frame([twenty_days_old]), stale_days=14, as_of=AS_OF)
    assert status_of(lenient, "ws-01") == CoverageStatus.COVERED
    assert status_of(strict, "ws-01") == CoverageStatus.STALE_COVERAGE


def test_hostname_normalization_matches_fqdn_and_case():
    """AD short name vs agent FQDN/lowercase must correlate to one device."""
    result = correlate(
        ad_frame("ACME-WS-014"),
        agents_to_frame([agent("acme-ws-014.corp.acme.example")]),
        as_of=AS_OF,
    )
    assert status_of(result, "acme-ws-014") == CoverageStatus.COVERED
    assert result.frame["join_key"].tolist() == ["acme-ws-014"]


def test_merged_row_count_equals_union_of_join_keys():
    """No silent row loss or duplication: with one agent row per key, the
    merged frame has exactly one row per unique join key across both sides."""
    ad = ad_frame("WS-01", "WS-02", "WS-03")  # WS-03 missing
    agents = agents_to_frame([agent("WS-01"), agent("WS-02"), agent("GHOST-9")])  # one orphan
    result = correlate(ad, agents, as_of=AS_OF)
    union = {"ws-01", "ws-02", "ws-03", "ghost-9"}
    assert len(result.frame) == len(union)
    assert set(result.frame["join_key"]) == union


def test_device_on_two_vendors_yields_one_row_per_vendor():
    result = correlate(
        ad_frame("WS-01"),
        agents_to_frame([agent("WS-01", vendor="sentinelone"), agent("WS-01", vendor="carbonblack")]),
        as_of=AS_OF,
    )
    assert len(result.frame) == 2
    assert set(result.frame["vendor"]) == {"sentinelone", "carbonblack"}
    assert set(result.frame["status"]) == {CoverageStatus.COVERED}


def test_summary_counts_and_coverage_pct():
    ad = ad_frame("WS-01", "WS-02", "WS-03", "WS-04")
    agents = agents_to_frame(
        [
            agent("WS-01"),                      # covered
            agent("WS-02", last_seen=STALE),     # stale
            agent("GHOST-9"),                    # orphaned
        ]
    )  # WS-03, WS-04 -> missing
    summary = correlate(ad, agents, as_of=AS_OF).summary
    assert summary["status_counts"] == {
        CoverageStatus.COVERED: 1,
        CoverageStatus.STALE_COVERAGE: 1,
        CoverageStatus.MISSING_AGENT: 2,
        CoverageStatus.ORPHANED_AGENT: 1,
    }
    # covered / (covered + stale + missing) = 1/4
    assert summary["coverage_pct"] == pytest.approx(25.0)


def test_empty_agent_inventory_marks_everything_missing():
    result = correlate(ad_frame("WS-01", "WS-02"), agents_to_frame([]), as_of=AS_OF)
    assert set(result.frame["status"]) == {CoverageStatus.MISSING_AGENT}
    assert len(result.frame) == 2
