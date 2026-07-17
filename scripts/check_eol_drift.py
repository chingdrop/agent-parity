"""Maintainer-only drift check: compare the committed OS EOL reference data
(``src/agent_parity/os_eol_data.json``, ``os_eol_builds_data.json``) against
endoflife.date's live public API, and report any dates that no longer match.

Not part of the runtime, the test suite, or CI — the reference data changes
on the order of years (see ``agent_parity/os_eol.py``'s module docstring),
so this is a manual, occasional check a maintainer runs by hand, not a
scheduled job. Nothing it fetches is written to disk or cached; it holds
the live response in memory just long enough to diff against the committed
files, which stay the source of truth either way.

endoflife.date splits most Windows 10/11 builds across several editions
(Workstation, Enterprise, LTSC, IoT) with different EOL dates for the same
build number. This project's committed data always uses the earliest EOL
date across a build's editions — the conservative choice for a coverage
tool flagging risk — so this script reproduces that same "min EOL per
build" rule rather than matching one hardcoded edition name.

Usage: uv run python scripts/check_eol_drift.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = REPO_ROOT / "src" / "agent_parity" / "os_eol_data.json"
BUILDS_DATA_PATH = REPO_ROOT / "src" / "agent_parity" / "os_eol_builds_data.json"

WINDOWS_API_URL = "https://endoflife.date/api/windows.json"
WINDOWS_SERVER_API_URL = "https://endoflife.date/api/windows-server.json"


def _fetch(url: str) -> list[dict]:
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def _live_build_eol_dates() -> dict[int, str]:
    """Build number -> earliest EOL date across all editions of that build.

    Windows Server builds (e.g. 20348 for Server 2022) only appear in the
    windows-server.json product, not windows.json, so both are merged here.
    """
    dates: dict[int, str] = {}
    for entry in _fetch(WINDOWS_API_URL) + _fetch(WINDOWS_SERVER_API_URL):
        latest = entry.get("latest", "")
        build_str = latest.rsplit(".", 1)[-1] if latest else ""
        if not build_str.isdigit():
            continue
        build = int(build_str)
        if build not in dates or entry["eol"] < dates[build]:
            dates[build] = entry["eol"]
    return dates


def _live_windows_server_eol_dates() -> dict[str, str]:
    return {entry["cycle"]: entry["eol"] for entry in _fetch(WINDOWS_SERVER_API_URL)}


def check_builds() -> list[str]:
    committed = json.loads(BUILDS_DATA_PATH.read_text())
    live = _live_build_eol_dates()
    problems = []
    for entry in committed:
        build = entry["build"]
        live_eol = live.get(build)
        if live_eol is None:
            problems.append(f"build {build} ({entry['name']}): not found in live data")
        elif live_eol != entry["eol_date"]:
            problems.append(f"build {build} ({entry['name']}): committed {entry['eol_date']} != live {live_eol}")
    return problems


def check_windows_server_free_text() -> list[str]:
    """Only the "windows server ..." rows have a direct API cycle match —
    the generic "Windows 10" free-text row is a curated convenience value
    (the current version's own build-table date), not a distinct API cycle,
    so it's intentionally left out of this automated check."""
    committed = json.loads(DATA_PATH.read_text())
    live = _live_windows_server_eol_dates()
    problems = []
    for entry in committed:
        if not entry["match"].startswith("windows server "):
            continue
        cycle = entry["match"].removeprefix("windows server ").replace(" ", "-")
        live_eol = live.get(cycle)
        if live_eol is None:
            problems.append(f"{entry['name']}: cycle {cycle!r} not found in live data")
        elif live_eol != entry["eol_date"]:
            problems.append(f"{entry['name']}: committed {entry['eol_date']} != live {live_eol}")
    return problems


def main() -> int:
    problems = check_builds() + check_windows_server_free_text()
    if not problems:
        print("No drift: all committed EOL dates match endoflife.date.")
        return 0

    print("Drift detected against endoflife.date:")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
