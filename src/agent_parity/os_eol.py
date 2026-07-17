"""OS end-of-life reference data and matching.

Two datasets, two precisions, matching what's actually available per source:

* ``os_eol_data.json`` — free-text OS name -> end-of-life date. All any
  source has when there's no build number: Carbon Black and BitDefender's
  APIs report a product name string ("Windows 11 Enterprise") with nothing
  else, and AD-only ``missing_agent`` rows fall back to this too when no
  build was captured.
* ``os_eol_builds_data.json`` — Windows build number -> end-of-life date.
  Precise: it disambiguates *which* Windows 10/11 feature update a device
  is on, which free text alone can't. Available when AD's
  ``operatingSystemVersion`` attribute or SentinelOne's build-carrying field
  is present (see ``ADDevice``/``AgentDevice``'s docstrings in
  ``agent_parity.models``); ``eol_status_for_device`` prefers this whenever
  a build number is available and only falls back to the free-text table
  otherwise.

Both are hand-curated from endoflife.date's (https://endoflife.date/) public
lifecycle data, not fetched live — there's no running pipeline here that
would benefit from a live API call against a dataset that changes on the
order of years, not days. endoflife.date does expose a real, free public
JSON API (no credentials needed); wiring this up as a live-with-fixture-
fallback source (the same shape as every vendor connector in this project)
would be a natural, self-contained extension if ever needed, but isn't
built here since a static reference file already answers the question this
project actually asks. `scripts/check_eol_drift.py` covers the "is this
still accurate" question instead — a maintainer-run, dev-only script that
diffs the two committed JSON files against the live API on demand (not
part of the test suite or CI, since the data doesn't change often enough
to justify checking on every run).

endoflife.date splits most Windows 10/11 builds across several editions
(Workstation, Enterprise, LTSC, IoT) with different EOL dates for the same
build number — e.g. build 22621 (Windows 11 22H2) is 2024-10-08 for the
Workstation edition but 2025-10-14 for Enterprise. Both JSON files always
use the earliest EOL date across a build/version's editions, matching this
project's own risk-flagging bias (see "High-value assets" in the README) —
`scripts/check_eol_drift.py` reproduces this same "min EOL across editions"
rule rather than matching one hardcoded edition name.

The Windows 11 gap in the free-text table is deliberate, not an oversight:
its real end-of-life date depends on which feature update is installed, and
a bare "Windows 11 Enterprise" string carries no version. Assuming a single
date for it would be a guess dressed up as data, so free-text matching
leaves it unmatched (``OSLifecycleStatus.UNKNOWN``) rather than silently
wrong — the build-number table is what actually resolves it, when a build
is available.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from agent_parity.models import OSLifecycleStatus

_DATA_PATH = Path(__file__).resolve().parent / "os_eol_data.json"
_BUILDS_DATA_PATH = Path(__file__).resolve().parent / "os_eol_builds_data.json"

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
class BuildLifecycle:
    build: int
    name: str
    eol_date: date


def _load_lifecycles() -> list[OSLifecycle]:
    with open(_DATA_PATH) as fh:
        raw = json.load(fh)
    lifecycles = [
        OSLifecycle(
            name=entry["name"],
            match=entry["match"],
            eol_date=date.fromisoformat(entry["eol_date"]),
        )
        for entry in raw
    ]
    # Most-specific (longest) match pattern wins, in case a future entry's
    # pattern is a substring of another's (e.g. a generic "windows server"
    # bucket added alongside "windows server 2022").
    return sorted(lifecycles, key=lambda lc: len(lc.match), reverse=True)


def _load_build_lifecycles() -> dict[int, BuildLifecycle]:
    with open(_BUILDS_DATA_PATH) as fh:
        raw = json.load(fh)
    return {
        entry["build"]: BuildLifecycle(
            build=entry["build"],
            name=entry["name"],
            eol_date=date.fromisoformat(entry["eol_date"]),
        )
        for entry in raw
    }


LIFECYCLES: list[OSLifecycle] = _load_lifecycles()
BUILD_LIFECYCLES: dict[int, BuildLifecycle] = _load_build_lifecycles()


def eol_date_for(os_text: str | None) -> date | None:
    """Best-effort end-of-life date for a free-text OS name.

    None means no confident match — see the module docstring for why bare
    "Windows 11" is deliberately one such case, not a data gap to fill in.
    """
    text = (os_text or "").lower()
    for lifecycle in LIFECYCLES:
        if lifecycle.match in text:
            return lifecycle.eol_date
    return None


def eol_date_for_build(build: int | None) -> date | None:
    """End-of-life date for an exact Windows build number, or None if it's
    not in the reference table (e.g. a build older or newer than what's
    curated here)."""
    if build is None:
        return None
    lifecycle = BUILD_LIFECYCLES.get(build)
    return lifecycle.eol_date if lifecycle else None


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
    """Classify a device's OS lifecycle status, preferring an exact build
    number when one is available (AD, SentinelOne) and falling back to
    free-text OS name matching when it isn't (Carbon Black, BitDefender, or
    an AD row with no captured build) — see the module docstring for why
    the two datasets exist at all.
    """
    if os_build is not None:
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
