"""OS end-of-life reference data and matching.

The dataset (``os_eol_data.json``) is hand-curated from endoflife.date's
(https://endoflife.date/) public lifecycle data for the Windows client/server
lines this project's fixtures actually use, not fetched live — there's no
running pipeline here that would benefit from a live API call against a
dataset that changes on the order of years, not days. endoflife.date does
expose a real, free public JSON API; wiring this up as a live-with-fixture-
fallback source (the same shape as every vendor connector in this project)
would be a natural, self-contained extension if ever needed, but isn't built
here since a static reference file already answers the question this project
actually asks.

Matches free-text OS names — all this pipeline ever has, from AD or from any
vendor API; no OS build/version number is captured anywhere in it — against
that dataset to answer one question: is this OS already past end-of-life, or
close to it? That's an independent prioritization signal alongside coverage
status and ``machine_type``: an uncovered, end-of-life server is a very
different priority than an uncovered, actively-supported one.

The Windows 11 gap is deliberate, not an oversight: its real end-of-life
date depends on which feature update (21H2/22H2/23H2/24H2/...) is installed,
and nothing in this pipeline reports that — a bare "Windows 11 Enterprise"
string carries no version. Assuming any single date for it would be a guess
dressed up as data, so it's left unmatched (``OSLifecycleStatus.UNKNOWN``)
rather than silently wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from agent_parity.models import OSLifecycleStatus

_DATA_PATH = Path(__file__).resolve().parent / "os_eol_data.json"

#: How close to its EOL date an OS has to be to count as "eol_soon" rather
#: than "supported" — long enough to actually plan and execute a migration.
DEFAULT_WARNING_DAYS = 180


@dataclass(frozen=True)
class OSLifecycle:
    name: str
    match: str
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


LIFECYCLES: list[OSLifecycle] = _load_lifecycles()


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


def eol_status(
    os_text: str | None,
    as_of: date | None = None,
    warning_days: int = DEFAULT_WARNING_DAYS,
) -> str:
    """Classify an OS's lifecycle status as of ``as_of`` (default: today)."""
    as_of = as_of or date.today()
    eol = eol_date_for(os_text)
    if eol is None:
        return OSLifecycleStatus.UNKNOWN.value
    if eol <= as_of:
        return OSLifecycleStatus.END_OF_LIFE.value
    if eol <= as_of + timedelta(days=warning_days):
        return OSLifecycleStatus.EOL_SOON.value
    return OSLifecycleStatus.SUPPORTED.value
