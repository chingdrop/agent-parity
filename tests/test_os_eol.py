"""Tests for the OS end-of-life reference dataset and matching."""

from datetime import date

import pytest

from agent_parity.models import OSLifecycleStatus
from agent_parity.os_eol import (
    eol_date_for,
    eol_date_for_build,
    eol_status,
    eol_status_for_device,
    extract_build_number,
)


@pytest.mark.parametrize(
    "os_text,expected",
    [
        ("Windows Server 2022 Datacenter", date(2031, 10, 14)),
        ("Windows Server 2019 Standard", date(2029, 1, 9)),
        ("Windows Server 2016 Standard", date(2027, 1, 12)),
        ("Windows Server 2012 R2 Standard", date(2023, 10, 10)),
        ("Windows 10 Enterprise", date(2025, 10, 14)),
        ("WINDOWS SERVER 2022 DATACENTER", date(2031, 10, 14)),  # case-insensitive
    ],
)
def test_eol_date_for_known_os(os_text, expected):
    assert eol_date_for(os_text) == expected


@pytest.mark.parametrize(
    "os_text",
    [
        "Windows 11 Enterprise",  # deliberately unmatched — see os_eol.py docstring
        "Ubuntu 22.04 LTS",
        "macOS Sonoma",
        "",
        None,
    ],
)
def test_eol_date_for_unknown_os_returns_none(os_text):
    assert eol_date_for(os_text) is None


def test_eol_status_is_end_of_life_after_the_date():
    status = eol_status("Windows 10 Enterprise", as_of=date(2026, 1, 1))
    assert status == OSLifecycleStatus.END_OF_LIFE


def test_eol_status_is_supported_well_before_the_date():
    status = eol_status("Windows Server 2022 Datacenter", as_of=date(2026, 1, 1))
    assert status == OSLifecycleStatus.SUPPORTED


def test_eol_status_is_eol_soon_within_the_warning_window():
    status = eol_status("Windows 10 Enterprise", as_of=date(2025, 5, 1), warning_days=180)
    assert status == OSLifecycleStatus.EOL_SOON


def test_eol_status_boundary_is_inclusive():
    """The EOL date itself counts as already end-of-life, not "one more day
    of grace" — matches how classify_coverage treats its own stale-days
    cutoff (>= is recent, not > )."""
    assert eol_status("Windows 10 Enterprise", as_of=date(2025, 10, 14)) == OSLifecycleStatus.END_OF_LIFE


def test_eol_status_unknown_when_os_has_no_match_regardless_of_as_of():
    assert eol_status("Windows 11 Enterprise", as_of=date(2099, 1, 1)) == OSLifecycleStatus.UNKNOWN


def test_eol_status_already_past_eol_is_stable_regardless_of_when_tests_run():
    """Windows Server 2012 R2's EOL (2023-10-10) is safely in the past for
    any real wall-clock date this suite could plausibly run at."""
    assert eol_status("Windows Server 2012 R2 Standard") == OSLifecycleStatus.END_OF_LIFE


# --- extract_build_number ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("10.0 (22631)", 22631),  # AD's operatingSystemVersion format
        ("10.0.22631.3155", 22631),  # full internal version string
        ("10.0 (20348)", 20348),
        ("", None),
        (None, None),
        ("Ubuntu 22.04.3 LTS", None),  # "22" / "04" / "3" — none reach 4 digits
    ],
)
def test_extract_build_number(text, expected):
    assert extract_build_number(text) == expected


def test_extract_build_number_ignores_a_revision_suffix_below_the_build_floor():
    """"3155" in "10.0.22631.3155" is the UBR/revision, not the build — must
    not be mistaken for one just because it's also several digits long."""
    assert extract_build_number("10.0.22631.3155") == 22631


# --- eol_date_for_build / eol_status_for_device -------------------------------------


@pytest.mark.parametrize(
    "build,expected",
    [
        (19045, date(2025, 10, 14)),  # Windows 10 22H2
        (22631, date(2025, 11, 11)),  # Windows 11 23H2
        (26100, date(2026, 10, 13)),  # Windows 11 24H2
        (20348, date(2031, 10, 14)),  # Windows Server 2022
        (99999, None),  # not in the table
        (None, None),
    ],
)
def test_eol_date_for_build(build, expected):
    assert eol_date_for_build(build) == expected


def test_eol_status_for_device_prefers_build_over_free_text():
    """A bare "Windows 11 Enterprise" string alone is unknown (Stage 1), but
    with a build number attached (AD/SentinelOne), it resolves precisely —
    this is the whole point of capturing the build at all."""
    status = eol_status_for_device(
        "Windows 11 Enterprise", os_build=22621, as_of=date(2026, 1, 1)
    )
    assert status == OSLifecycleStatus.END_OF_LIFE  # 22H2 EOL'd 2024-10-08


def test_eol_status_for_device_distinguishes_feature_updates():
    """The reason build-number precision exists at all: two devices with the
    identical free-text OS name can be in completely different lifecycle
    states depending on which feature update they're actually running."""
    as_of = date(2026, 7, 4)
    eol_22h2 = eol_status_for_device("Windows 11 Enterprise", os_build=22621, as_of=as_of)
    eol_23h2 = eol_status_for_device("Windows 11 Enterprise", os_build=22631, as_of=as_of)
    eol_24h2 = eol_status_for_device("Windows 11 Enterprise", os_build=26100, as_of=as_of)

    assert eol_22h2 == OSLifecycleStatus.END_OF_LIFE  # EOL 2024-10-08, long past
    assert eol_23h2 == OSLifecycleStatus.END_OF_LIFE  # EOL 2025-11-11, past
    assert eol_24h2 == OSLifecycleStatus.EOL_SOON  # EOL 2026-10-13, ~101 days out


def test_eol_status_for_device_falls_back_to_free_text_without_a_build():
    """Carbon Black/BitDefender devices (no build number captured at all)
    get the coarser, Stage 1 free-text answer instead of "unknown by
    default" — machine_type=server-style graceful degradation, not a
    missing feature."""
    status = eol_status_for_device("Windows 10 Enterprise", os_build=None, as_of=date(2026, 1, 1))
    assert status == OSLifecycleStatus.END_OF_LIFE  # free-text Windows 10 entry


def test_eol_status_for_device_falls_back_when_build_is_unrecognized():
    """A build number that isn't in the (necessarily incomplete) reference
    table shouldn't produce "unknown" if the free-text name still matches
    something — still better than nothing."""
    status = eol_status_for_device("Windows 10 Enterprise", os_build=12345, as_of=date(2026, 1, 1))
    assert status == OSLifecycleStatus.END_OF_LIFE  # falls back to the free-text Windows 10 entry
