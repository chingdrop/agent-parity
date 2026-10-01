"""Tests for agent_parity/os_eol_live.py: deriving the lookup tables from
endoflife.date's response shape, and failing cleanly. No real network — the
cycles below are hand-built in the API's shape."""

import pytest
import requests

from agent_parity import os_eol_live
from agent_parity.os_eol_live import EOLFetchError, derive_lifecycle_data, fetch_lifecycle_data

WINDOWS = [
    {"cycle": "11-24h2-e", "releaseLabel": "11 24H2 (E)", "latest": "10.0.26100", "eol": "2027-10-12"},
    {"cycle": "11-24h2-w", "releaseLabel": "11 24H2 (W)", "latest": "10.0.26100", "eol": "2026-10-13"},
    {
        "cycle": "10-22h2",
        "releaseLabel": "10 22H2",
        "latest": "10.0.19045",
        "eol": "2025-10-14",
        "releaseDate": "2022-10-18",
    },
    {
        "cycle": "10-21h2-w",
        "releaseLabel": "10 21H2 (W)",
        "latest": "10.0.19044",
        "eol": "2023-06-13",
        "releaseDate": "2021-11-16",
    },
    {"cycle": "8.1", "releaseLabel": "8.1", "latest": "6.3.9600", "eol": "2023-01-10", "releaseDate": "2013-10-17"},
]
SERVER = [
    {"cycle": "2025", "releaseLabel": None, "latest": "10.0.26100", "eol": "2034-11-14"},
    {"cycle": "1809-sac", "releaseLabel": "Windows Server 1809 SAC", "latest": "10.0.17763", "eol": "2020-11-10"},
    {"cycle": "2019", "releaseLabel": None, "latest": "10.0.17763", "eol": "2029-01-09"},
    {"cycle": "2012-r2", "releaseLabel": None, "latest": "6.3.9600", "eol": "2023-10-10"},
    {"cycle": "2008-sp2", "releaseLabel": None, "latest": "6.0.6003", "eol": "2020-01-14"},
    {"cycle": "2008", "releaseLabel": None, "latest": "6.0.6001", "eol": "2011-07-12"},
]


def _builds(data):
    return {(e["product"], e["build"]): (e["name"], e["eol_date"]) for e in data["builds"]}


def _names(data):
    return {e["name"]: e["eol_date"] for e in data["free_text"]}


def test_builds_are_keyed_by_product_so_a_shared_build_keeps_both_dates():
    builds = _builds(derive_lifecycle_data(WINDOWS, SERVER))

    assert builds[("windows", 26100)] == ("Windows 11 24H2", "2026-10-13")  # earliest edition
    assert builds[("windows-server", 26100)] == ("Windows Server 2025", "2034-11-14")


def test_a_build_shared_within_a_product_takes_the_earliest_date():
    assert _builds(derive_lifecycle_data(WINDOWS, SERVER))[("windows-server", 17763)][1] == "2020-11-10"


def test_pre_windows_10_builds_are_left_to_name_matching():
    builds = _builds(derive_lifecycle_data(WINDOWS, SERVER))

    assert ("windows", 9600) not in builds
    assert ("windows-server", 9600) not in builds


def test_free_text_names_cover_server_releases_and_single_name_client_releases():
    names = _names(derive_lifecycle_data(WINDOWS, SERVER))

    assert names["Windows Server 2019"] == "2029-01-09"
    assert names["Windows Server 2012 R2"] == "2023-10-10"
    assert names["Windows Server 2008"] == "2020-01-14"  # the latest service pack's date
    assert names["Windows 10"] == "2025-10-14"  # the final feature update's date
    assert names["Windows 8.1"] == "2023-01-10"
    assert not any(name.startswith("Windows 11") for name in names)  # deliberately unmatched
    assert "Windows Server 1809" not in str(names)  # semi-annual releases have no year to match on


def test_output_is_sorted_so_regenerating_it_diffs_cleanly():
    data = derive_lifecycle_data(list(reversed(WINDOWS)), list(reversed(SERVER)))

    assert data == {**derive_lifecycle_data(WINDOWS, SERVER), "generated_at": data["generated_at"]}


def test_a_network_error_raises_eol_fetch_error(monkeypatch):
    def offline(url, timeout):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(os_eol_live.requests, "get", offline)

    with pytest.raises(EOLFetchError, match="endoflife.date fetch failed"):
        fetch_lifecycle_data()


def test_an_unexpected_response_shape_raises_eol_fetch_error(monkeypatch):
    class EmptyResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "not a list"}

    monkeypatch.setattr(os_eol_live.requests, "get", lambda url, timeout: EmptyResponse())

    with pytest.raises(EOLFetchError):
        fetch_lifecycle_data()


# --- refresh_cache: the daily refresh and the cache lookups read ---------------


def _bundled_with(**eol_by_name):
    """The bundled snapshot with some free-text dates changed, as a fetch would return it."""
    from agent_parity import os_eol

    data = os_eol.load_bundled_data()
    data["free_text"] = [{**e, "eol_date": eol_by_name.get(e["name"], e["eol_date"])} for e in data["free_text"]]
    return data


def test_a_refresh_with_new_dates_writes_the_cache_and_lookups_use_it(monkeypatch):
    from datetime import date

    from agent_parity import os_eol

    monkeypatch.setattr(
        os_eol_live, "fetch_lifecycle_data", lambda timeout: _bundled_with(**{"Windows Server 2019": "2030-01-01"})
    )
    assert os_eol.eol_date_for("Windows Server 2019 Standard") == date(2029, 1, 9)

    outcome = os_eol_live.refresh_cache()

    assert outcome.result == "updated"
    assert outcome.changes == ["changed name 'Windows Server 2019': 2029-01-09 -> 2030-01-01"]
    assert os_eol.cache_path().exists()
    assert os_eol.eol_date_for("Windows Server 2019 Standard") == date(2030, 1, 1)


def test_a_refresh_with_the_same_data_is_unchanged_but_marks_the_cache_fresh(monkeypatch):
    from datetime import timedelta

    from agent_parity import os_eol

    calls = []
    monkeypatch.setattr(os_eol_live, "fetch_lifecycle_data", lambda timeout: calls.append(1) or _bundled_with())

    assert os_eol_live.refresh_cache().result == "unchanged"
    assert os_eol.cache_path().exists()
    assert os_eol_live.refresh_cache(max_age=timedelta(days=1)).result == "fresh"
    assert len(calls) == 1  # the second call didn't fetch


def test_a_failed_refresh_keeps_the_current_data_and_writes_nothing():
    from datetime import date

    from agent_parity import os_eol

    outcome = os_eol_live.refresh_cache()  # the autouse fixture makes every fetch fail

    assert outcome.result == "failed"
    assert "network disabled in tests" in outcome.error
    assert not os_eol.cache_path().exists()
    assert os_eol.eol_date_for("Windows Server 2019 Standard") == date(2029, 1, 9)


def test_an_unreadable_cache_falls_back_to_the_bundled_data(caplog):
    from datetime import date

    from agent_parity import os_eol

    os_eol.cache_path().write_text("{not json")

    with caplog.at_level("WARNING", logger="agent_parity.os_eol"):
        assert os_eol.eol_date_for("Windows Server 2019 Standard") == date(2029, 1, 9)
    assert "Ignoring unreadable OS EOL cache" in caplog.text
