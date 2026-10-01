"""Fetch Windows lifecycle data from endoflife.date and derive this project's tables.

The one place the rules for turning endoflife.date's public API into
``os_eol``'s lookup tables live, so the committed snapshot
(``scripts/check_eol_drift.py --write``) and the daily refresh can't disagree:

* **Builds, per product.** A build number alone doesn't identify an OS: 26100 is
  both Windows 11 24H2 and Windows Server 2025, and 17763 is Windows 10 1809 and
  Windows Server 2019. So build entries are keyed by product (``windows`` or
  ``windows-server``). Within a product, a build shared by several editions
  (Workstation, Enterprise, LTSC, ...) takes the *earliest* EOL date — the
  conservative choice for a tool that flags risk.
* **Free-text names.** One entry per Windows Server release (``Windows Server
  2019``; service packs fold into their release, taking the latest date) and per
  client major release that has one name (Windows 10, 8.1, 8, 7). Windows 10's
  date is its final feature update's. There is deliberately no bare Windows 11
  entry: its EOL depends on the feature update, which only a build resolves.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import requests

WINDOWS_API_URL = "https://endoflife.date/api/windows.json"
WINDOWS_SERVER_API_URL = "https://endoflife.date/api/windows-server.json"

#: Plausible Windows NT build numbers since Windows 10 (see os_eol); older
#: products (8.1's 9600, ...) are matched by name only.
_MIN_BUILD = 10000

#: Client major releases with a single, version-free name, cycle prefix -> name.
_CLIENT_NAMES = {"10": "Windows 10", "8.1": "Windows 8.1", "8": "Windows 8", "7": "Windows 7"}


class EOLFetchError(Exception):
    pass


def fetch_lifecycle_data(timeout: float = 10.0) -> dict:
    """Fetch both products from endoflife.date and derive the lookup tables.

    Raises ``EOLFetchError`` on any network or response-shape problem; callers
    fall back to the data they already have.
    """
    try:
        windows = _fetch(WINDOWS_API_URL, timeout)
        server = _fetch(WINDOWS_SERVER_API_URL, timeout)
        return derive_lifecycle_data(windows, server)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise EOLFetchError(f"endoflife.date fetch failed: {exc}") from exc


def _fetch(url: str, timeout: float) -> list[dict]:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list) or not data:
        raise ValueError(f"unexpected response from {url}")
    return data


def derive_lifecycle_data(windows: list[dict], server: list[dict]) -> dict:
    """endoflife.date cycles -> ``{"free_text": [...], "builds": [...]}``, sorted
    so the output is stable and diffs cleanly."""
    builds = _builds("windows", windows) + _builds("windows-server", server)
    free_text = _server_names(server) + _client_names(windows)
    return {
        "source": "https://endoflife.date",
        "generated_at": datetime.now(UTC).date().isoformat(),
        "free_text": sorted(free_text, key=lambda e: e["match"]),
        "builds": sorted(builds, key=lambda e: (e["product"], e["build"])),
    }


def _build_of(cycle: dict) -> int | None:
    tail = (cycle.get("latest") or "").rsplit(".", 1)[-1]
    return int(tail) if tail.isdigit() and int(tail) >= _MIN_BUILD else None


def _builds(product: str, cycles: list[dict]) -> list[dict]:
    earliest: dict[int, dict] = {}
    for cycle in cycles:
        build = _build_of(cycle)
        if build is None or not isinstance(cycle.get("eol"), str):
            continue
        if build not in earliest or cycle["eol"] < earliest[build]["eol_date"]:
            earliest[build] = {
                "product": product,
                "build": build,
                "name": _release_name(product, cycle),
                "eol_date": cycle["eol"],
            }
    return list(earliest.values())


def _release_name(product: str, cycle: dict) -> str:
    if product == "windows":
        label = re.sub(r"\s*\(.*\)$", "", cycle.get("releaseLabel") or cycle["cycle"])
        return f"Windows {label}"
    return cycle.get("releaseLabel") or f"Windows Server {_server_release(cycle['cycle'])}"


def _server_release(cycle: str) -> str:
    return re.sub(r"-sp\d+$", "", cycle).replace("-r2", " R2")


def _server_names(cycles: list[dict]) -> list[dict]:
    latest: dict[str, str] = {}
    for cycle in cycles:
        release = _server_release(cycle["cycle"])
        if re.fullmatch(r"\d{4}( R2)?", release) and isinstance(cycle.get("eol"), str):
            latest[release] = max(latest.get(release, ""), cycle["eol"])
    return [
        {"name": f"Windows Server {release}", "match": f"windows server {release.lower()}", "eol_date": eol}
        for release, eol in latest.items()
    ]


def _client_names(cycles: list[dict]) -> list[dict]:
    entries = []
    for prefix, name in _CLIENT_NAMES.items():
        family = [c for c in cycles if re.fullmatch(rf"{re.escape(prefix)}(-.*)?", c["cycle"])]
        if not family:
            continue
        final = max(family, key=lambda c: c.get("releaseDate") or "")
        # The final release's own date, conservatively the earliest across its editions.
        same_build = [c for c in family if c.get("latest") == final.get("latest")]
        entries.append({"name": name, "match": name.lower(), "eol_date": min(c["eol"] for c in same_build)})
    return entries
