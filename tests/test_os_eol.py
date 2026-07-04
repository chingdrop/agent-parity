"""Tests for the OS end-of-life reference dataset and matching."""

from datetime import date

import pytest

from agent_parity.models import OSLifecycleStatus
from agent_parity.os_eol import eol_date_for, eol_status


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
