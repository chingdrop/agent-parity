"""OS end-of-life reference data and matching.

``os_eol_data.json`` is a snapshot of endoflife.date's Windows and Windows
Server lifecycle data, derived by ``agent_parity.os_eol_live`` (regenerate it
with ``scripts/check_eol_drift.py --write``). It holds two tables, two
precisions, matching what's actually available per source:

* ``free_text`` — OS name -> end-of-life date ("Windows Server 2019", "Windows
  10"). All a source has when there's no build number: Carbon Black and
  BitDefender report only a product name, and AD-only rows fall back to it
  when no build was captured.
* ``builds`` — (product, build number) -> end-of-life date. Precise: it pins
  *which* Windows 10/11 feature update a device is on, which free text alone
  can't. A build number alone doesn't identify an OS (26100 is both Windows 11
  24H2 and Windows Server 2025), so entries are keyed by product.

``eol_status_for_device`` picks per device: for a **server** (OS text naming
Windows Server), the named release wins, since "Windows Server 2019" is exact
while its build is shared with a short-lived semi-annual release; the server
build table only covers servers whose name carries no year. For a **client**,
the build wins, then the free-text name.

The Windows 11 gap in the free-text table is deliberate: its real end-of-life
date depends on the feature update, and a bare "Windows 11 Enterprise" string
carries no version. Assuming one date would be a guess dressed up as data, so
free-text matching leaves it ``unknown`` and only a build resolves it.

Where shared builds span editions (Workstation, Enterprise, LTSC), the table
holds the earliest EOL date, matching this project's risk-flagging bias (see
"High-value assets" in docs/architecture.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from agent_parity.models import OSLifecycleStatus

_DATA_PATH = Path(__file__).resolve().parent / "os_eol_data.json"

#: How close to its EOL date an OS has to be to count as "eol_soon" rather
#: than "supported" — long enough to actually plan and execute a migration.
DEFAULT_WARNING_DAYS = 180

#: Windows NT build numbers have lived in this range since Windows 10 (build
#: 10240) launched — used to tell a genuine build number apart from other
#: digit runs (a UBR/revision suffix, a version-string component) that might
#: appear in the same raw field.
_MIN_PLAUSIBLE_BUILD = 10000
_MAX_PLAUSIBLE_BUILD = 99999


@dataclass(frozen=True)
class OSLifecycle:
    name: str
    match: str
    eol_date: date


@dataclass(frozen=True)
class LifecycleTables:
    #: Most specific (longest) match first, so "windows server 2012 r2" wins
    #: over "windows server 2012" and "windows 8.1" over "windows 8".
    free_text: list[OSLifecycle]
    #: (product, build) -> EOL date; product is "windows" or "windows-server".
    builds: dict[tuple[str, int], date]


def tables_from_data(data: dict) -> LifecycleTables:
    """Build lookup tables from the JSON shape ``os_eol_live`` produces."""
    free_text = [
        OSLifecycle(name=e["name"], match=e["match"], eol_date=date.fromisoformat(e["eol_date"]))
        for e in data["free_text"]
    ]
    builds = {(e["product"], int(e["build"])): date.fromisoformat(e["eol_date"]) for e in data["builds"]}
    return LifecycleTables(free_text=sorted(free_text, key=lambda lc: len(lc.match), reverse=True), builds=builds)


def _load_bundled() -> LifecycleTables:
    with open(_DATA_PATH) as fh:
        return tables_from_data(json.load(fh))


_BUNDLED: LifecycleTables = _load_bundled()


def _tables() -> LifecycleTables:
    return _BUNDLED


def is_server_os(os_text: str | None) -> bool:
    return "server" in (os_text or "").lower()


def eol_date_for(os_text: str | None) -> date | None:
    """Best-effort end-of-life date for a free-text OS name.

    None means no confident match — see the module docstring for why bare
    "Windows 11" is deliberately one such case, not a data gap to fill in.
    """
    text = (os_text or "").lower()
    for lifecycle in _tables().free_text:
        if lifecycle.match in text:
            return lifecycle.eol_date
    return None


def eol_date_for_build(build: int | None, server: bool = False) -> date | None:
    """End-of-life date for an exact build of Windows (``server=False``) or
    Windows Server (``server=True``), or None if it's not in the table."""
    if build is None:
        return None
    return _tables().builds.get(("windows-server" if server else "windows", build))


def extract_build_number(text: str | None) -> int | None:
    """Pull a plausible Windows build number out of a raw version string.

    Different sources format this differently — AD's ``operatingSystemVersion``
    looks like ``"10.0 (22631)"``; a vendor might report a full internal
    version string like ``"10.0.22631.3155"`` (major.minor.build.revision,
    the same shape ``ver`` prints locally on Windows) that needs the build
    component pulled out of it, not just read off a clean field. Rather than
    hand-parsing each source's exact shape, this looks for any 5-digit run in
    the plausible Windows build range and returns the first one — build
    numbers and revision/UBR suffixes don't collide in that range (a
    revision like ``3155`` is 4 digits, well below the 10000 floor).
    """
    if not text:
        return None
    for match in re.findall(r"\d{4,6}", text):
        value = int(match)
        if _MIN_PLAUSIBLE_BUILD <= value <= _MAX_PLAUSIBLE_BUILD:
            return value
    return None


def eol_status(
    os_text: str | None,
    as_of: date | None = None,
    warning_days: int = DEFAULT_WARNING_DAYS,
) -> str:
    """Classify an OS's lifecycle status from free-text name alone, as of
    ``as_of`` (default: today). Prefer ``eol_status_for_device`` when a
    build number might be available — this is the deliberately coarser
    fallback for when one isn't."""
    return _classify(eol_date_for(os_text), as_of, warning_days)


def eol_status_for_device(
    os_text: str | None,
    os_build: int | None = None,
    as_of: date | None = None,
    warning_days: int = DEFAULT_WARNING_DAYS,
) -> str:
    """Classify a device's OS lifecycle status.

    Servers: the named release, else the server build table. Clients: the
    exact build when one is available (AD, SentinelOne), else the free-text
    name (Carbon Black, BitDefender, or an AD row with no captured build).
    See the module docstring for why servers and clients differ.
    """
    if is_server_os(os_text):
        named = eol_date_for(os_text)
        if named is not None:
            return _classify(named, as_of, warning_days)
        return _classify(eol_date_for_build(os_build, server=True), as_of, warning_days)
    build_eol = eol_date_for_build(os_build)
    if build_eol is not None:
        return _classify(build_eol, as_of, warning_days)
    return eol_status(os_text, as_of, warning_days)


def _classify(eol: date | None, as_of: date | None, warning_days: int) -> str:
    as_of = as_of or date.today()
    if eol is None:
        return OSLifecycleStatus.UNKNOWN.value
    if eol <= as_of:
        return OSLifecycleStatus.END_OF_LIFE.value
    if eol <= as_of + timedelta(days=warning_days):
        return OSLifecycleStatus.EOL_SOON.value
    return OSLifecycleStatus.SUPPORTED.value
