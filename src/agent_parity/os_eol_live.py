"""Fetch Windows lifecycle data from endoflife.date and derive this project's tables.

The one place the rules for turning endoflife.date's public API into
``os_eol``'s lookup tables live, so the committed snapshot
(``tools/check_eol_drift.py --write``) and the daily refresh
(``refresh_cache``: beat at 06:30, and ``agent-parity run`` when the cache is
over a day old) can't disagree:

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

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import requests

from agent_parity import os_eol
from agent_parity.shared.atomic_io import atomic_write, ensure_dir

logger = logging.getLogger(__name__)

WINDOWS_API_URL = "https://endoflife.date/api/windows.json"
WINDOWS_SERVER_API_URL = "https://endoflife.date/api/windows-server.json"

#: Plausible Windows NT build numbers since Windows 10 (see os_eol); older
#: products (8.1's 9600, ...) are matched by name only.
_MIN_BUILD = 10000

#: Client major releases with a single, version-free name, cycle prefix -> name.
_CLIENT_NAMES = {"10": "Windows 10", "8.1": "Windows 8.1", "8": "Windows 8", "7": "Windows 7"}


class EOLFetchError(Exception):
    pass


@dataclass(frozen=True)
class RefreshOutcome:
    #: "fresh" (cache younger than max_age, nothing fetched), "updated" (data
    #: changed), "unchanged" (fetched, same data) or "failed" (kept current data).
    result: str
    changes: list[str] = field(default_factory=list)
    error: str | None = None


def refresh_cache(max_age: timedelta | None = None, timeout: float = 10.0) -> RefreshOutcome:
    """Fetch endoflife.date and write the result to ``os_eol.cache_path()``.

    Skips the fetch when the cache is younger than ``max_age``. On success the
    cache is rewritten atomically (which also marks it fresh) and any
    differences from the data in use are logged. On failure nothing is written
    and lookups keep using what they had — the previous cache, else the bundled
    snapshot — so an endoflife.date outage never stops a run.
    """
    path = os_eol.cache_path()
    if max_age is not None and path.exists() and time.time() - path.stat().st_mtime < max_age.total_seconds():
        return RefreshOutcome("fresh")
    try:
        live = fetch_lifecycle_data(timeout=timeout)
    except EOLFetchError as exc:
        logger.warning("Keeping the current OS EOL data: %s", exc)
        return RefreshOutcome("failed", error=str(exc))

    changes = diff_lifecycle_data(os_eol.load_current_data(), live)
    ensure_dir(path.parent)
    atomic_write(path, json.dumps(live, indent=2) + "\n")
    if changes:
        logger.info("OS EOL data updated from endoflife.date (%d changes): %s", len(changes), "; ".join(changes))
        return RefreshOutcome("updated", changes)
    return RefreshOutcome("unchanged")


def diff_lifecycle_data(old: dict, new: dict) -> list[str]:
    """Readable differences between two lifecycle datasets (added/removed/changed entries)."""
    before, after = _keyed(old), _keyed(new)
    changes = [f"added {key}: {after[key]}" for key in sorted(after.keys() - before.keys())]
    changes += [f"removed {key} (was {before[key]})" for key in sorted(before.keys() - after.keys())]
    changed = [key for key in sorted(before.keys() & after.keys()) if before[key] != after[key]]
    changes += [f"changed {key}: {before[key]} -> {after[key]}" for key in changed]
    return changes


def _keyed(data: dict) -> dict[str, str]:
    entries = {f"name {e['name']!r}": e["eol_date"] for e in data["free_text"]}
    entries.update({f"{e['product']} build {e['build']} ({e['name']})": e["eol_date"] for e in data["builds"]})
    return entries


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
