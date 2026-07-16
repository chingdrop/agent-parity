"""Tests for the classification logic itself — the four CoverageStatus
outcomes and the merge invariants — not re-verification of pd.merge."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from agent_parity.correlation.engine import agents_to_frame, correlate
from agent_parity.models import AgentDevice, CoverageStatus

AS_OF: pd.Timestamp = pd.Timestamp("2026-07-03T00:00:00Z")
RECENT = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)  # < 1 day old
STALE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)  # > 14 days old


def ad_frame(*hostnames: str, os: str = "Windows 11", os_build: int | None = None) -> pd.DataFrame:
    """Mirrors ad_sync.parser's real output shape (os_build included, even
    when None) — without this column, a merge against agents_to_frame's
    output (which always has os_build) leaves it unsuffixed, which is not
    what production ever actually produces."""
    return pd.DataFrame(
        {
            "hostname": list(hostnames),
            "os": [os] * len(hostnames),
            "os_build": [os_build] * len(hostnames),
            "last_logon": [AS_OF - timedelta(days=1)] * len(hostnames),
        }
    )


def agent(
    hostname: str,
    last_seen: datetime | None = RECENT,
    vendor="sentinelone",
    os: str = "",
    os_build: int | None = None,
) -> AgentDevice:
    return AgentDevice(
        vendor=vendor,
        agent_id=f"id-{hostname}",
        hostname=hostname,
        last_seen=last_seen,
        os=os,
        os_build=os_build,
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
            agent("WS-01"),  # covered
            agent("WS-02", last_seen=STALE),  # stale
            agent("GHOST-9"),  # orphaned
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


def test_platform_and_machine_type_survive_the_merge():
    """Not overlapping with any ad_df column, so pd.merge doesn't suffix
    them — they should reach the classified frame unchanged, ready for
    CoverageSnapshot persistence."""
    device = AgentDevice(
        vendor="carbonblack",
        agent_id="id-WS-01",
        hostname="WS-01",
        last_seen=RECENT,
        platform="windows",
        machine_type="desktop",
    )
    result = correlate(ad_frame("WS-01"), agents_to_frame([device]), as_of=AS_OF)
    row = result.frame.iloc[0]
    assert row["platform"] == "windows"
    assert row["machine_type"] == "desktop"


def test_missing_agent_rows_have_no_platform_but_get_a_backfilled_machine_type():
    """platform has no AD-side equivalent to derive from, so it stays blank
    for missing_agent rows; machine_type does — a missing Domain Controller
    must still show up as a server, not as "no criticality signal at all"."""
    result = correlate(ad_frame("WS-01", os="Windows 11 Enterprise"), agents_to_frame([]), as_of=AS_OF)
    row = result.frame.iloc[0]
    assert pd.isna(row["platform"])
    assert row["machine_type"] == "desktop"


def test_missing_agent_server_is_backfilled_as_a_server():
    """The actual point: a missing Domain Controller (or any Windows Server
    SKU) must be identifiable as high-value even with zero agent data —
    that's the whole reason backfill_machine_type exists."""
    result = correlate(
        ad_frame("ACME-DC01", os="Windows Server 2022 Datacenter"),
        agents_to_frame([]),
        as_of=AS_OF,
    )
    row = result.frame.iloc[0]
    assert row["status"] == CoverageStatus.MISSING_AGENT
    assert row["machine_type"] == "server"


def test_backfill_never_overwrites_an_agent_reported_machine_type():
    """A matched device's machine_type comes from the agent (already
    vendor-normalized); AD's OS text must never override it, even if they
    somehow disagreed."""
    device = AgentDevice(
        vendor="sentinelone",
        agent_id="id-WS-01",
        hostname="WS-01",
        last_seen=RECENT,
        machine_type="desktop",
    )
    # AD says "Server" in the OS text; the agent's own report must win.
    result = correlate(
        ad_frame("WS-01", os="Windows Server 2022 Datacenter"),
        agents_to_frame([device]),
        as_of=AS_OF,
    )
    assert result.frame.iloc[0]["machine_type"] == "desktop"


def test_orphaned_agent_keeps_its_own_machine_type_with_no_ad_row_to_backfill_from():
    device = AgentDevice(
        vendor="bitdefender",
        agent_id="id-ghost",
        hostname="GHOST-9",
        last_seen=RECENT,
        machine_type="server",
    )
    result = correlate(ad_frame("WS-01"), agents_to_frame([device]), as_of=AS_OF)
    orphan = result.frame[result.frame["join_key"] == "ghost-9"].iloc[0]
    assert orphan["status"] == CoverageStatus.ORPHANED_AGENT
    assert orphan["machine_type"] == "server"


def test_server_coverage_pct_is_scoped_to_servers_only():
    ad = pd.concat(
        [
            ad_frame("DC01", os="Windows Server 2022 Datacenter"),  # missing -> server
            ad_frame("WS-01", os="Windows 11 Enterprise"),  # covered -> desktop
        ],
        ignore_index=True,
    )
    agents = agents_to_frame([agent("WS-01")])
    summary = correlate(ad, agents, as_of=AS_OF).summary

    assert summary["server_status_counts"] == {CoverageStatus.MISSING_AGENT: 1}
    assert summary["server_coverage_pct"] == pytest.approx(0.0)
    # Overall coverage_pct still blends both machine types together.
    assert summary["coverage_pct"] == pytest.approx(50.0)


def test_server_coverage_pct_is_zero_when_there_are_no_servers():
    result = correlate(ad_frame("WS-01", os="Windows 11 Enterprise"), agents_to_frame([agent("WS-01")]), as_of=AS_OF)
    assert result.summary["server_status_counts"] == {}
    assert result.summary["server_coverage_pct"] == 0.0


# --- classify_eol_status ------------------------------------------------------------
#
# AS_OF is 2026-07-03. Build 22621 (Windows 11 22H2) EOL'd 2024-10-08;
# 22631 (23H2) EOL'd 2025-11-11; 26100 (24H2) EOL's 2026-10-13, ~102 days
# out — within the 180-day warning window, so eol_soon, not end_of_life.


def test_eol_status_uses_agent_build_when_ad_has_none():
    device = agent("WS-01", os="Windows 11 Enterprise", os_build=26100)
    result = correlate(
        ad_frame("WS-01", os="Windows 11 Enterprise", os_build=None),
        agents_to_frame([device]),
        as_of=AS_OF,
    )
    row = result.frame.iloc[0]
    assert row["os_build"] == 26100
    assert row["eol_status"] == "eol_soon"


def test_eol_status_falls_back_to_ad_build_when_agent_has_none():
    """Matched device, agent reports no build (Carbon Black/BitDefender) —
    AD's own build (captured for every device now) still resolves it
    precisely instead of falling all the way back to free-text "unknown"."""
    device = agent("WS-01", os="Windows 11 Enterprise", os_build=None, vendor="carbonblack")
    result = correlate(
        ad_frame("WS-01", os="Windows 11 Enterprise", os_build=22621),
        agents_to_frame([device]),
        as_of=AS_OF,
    )
    row = result.frame.iloc[0]
    assert row["os_build"] == 22621
    assert row["eol_status"] == "end_of_life"


def test_eol_status_prefers_agent_build_over_ad_build_when_both_present():
    """The agent's own report is the freshest signal — even if it somehow
    disagreed with AD's, the agent wins, same precedence as machine_type."""
    device = agent("WS-01", os="Windows 11 Enterprise", os_build=26100)
    result = correlate(
        ad_frame("WS-01", os="Windows 11 Enterprise", os_build=22621),
        agents_to_frame([device]),
        as_of=AS_OF,
    )
    row = result.frame.iloc[0]
    assert row["os_build"] == 26100
    assert row["eol_status"] == "eol_soon"


def test_eol_status_falls_back_to_free_text_when_no_build_anywhere():
    """The genuine Carbon Black/BitDefender-only case: no build on either
    side, so a bare "Windows 11 Enterprise" resolves no further than the
    Stage 1 free-text table (which deliberately doesn't cover Windows 11)."""
    device = agent("WS-01", os="Windows 11 Enterprise", os_build=None, vendor="carbonblack")
    result = correlate(
        ad_frame("WS-01", os="Windows 11 Enterprise", os_build=None),
        agents_to_frame([device]),
        as_of=AS_OF,
    )
    assert result.frame.iloc[0]["eol_status"] == "unknown"


def test_missing_agent_row_uses_ad_build_for_precise_eol_status():
    """A missing_agent row has no agent data at all, but AD's own build
    (captured regardless of coverage) still gives a precise answer — not
    just the coarser free-text fallback."""
    result = correlate(
        ad_frame("ACME-DC01", os="Windows 11 Enterprise", os_build=22631),
        agents_to_frame([]),
        as_of=AS_OF,
    )
    row = result.frame.iloc[0]
    assert row["status"] == CoverageStatus.MISSING_AGENT
    assert row["os_build"] == 22631
    assert row["eol_status"] == "end_of_life"


def test_summary_eol_status_counts_and_at_risk_cross_tab():
    ad = pd.concat(
        [
            ad_frame("WS-01", os="Windows 11 Enterprise", os_build=22621),  # covered, EOL
            ad_frame("WS-02", os="Windows Server 2022 Datacenter", os_build=20348),  # missing, supported
        ],
        ignore_index=True,
    )
    agents = agents_to_frame([agent("WS-01", os="Windows 11 Enterprise", os_build=22621)])
    summary = correlate(ad, agents, as_of=AS_OF).summary

    assert summary["eol_status_counts"] == {"end_of_life": 1, "supported": 1}
    # Only WS-01 is at risk (end_of_life); it's covered, not missing —
    # the cross-tab is what lets a report distinguish "unsupported but at
    # least visible" from "unsupported and invisible."
    assert summary["at_risk_status_counts"] == {CoverageStatus.COVERED: 1}
