"""The correlation engine: an outer pandas merge, classified.

The whole reconciliation reduces to one analytical move: outer-merge the AD
inventory against the concatenated agent inventories on a normalized
hostname join key, then read coverage straight off the merge indicator —

* ``left_only``  -> ``missing_agent``   (AD knows it, no agent reports it)
* ``right_only`` -> ``orphaned_agent``  (agent reports it, AD has no record)
* ``both`` + recent check-in -> ``covered``
* ``both`` + stale check-in  -> ``stale_coverage``

The flow is a ``.pipe()`` chain so each stage (normalize -> merge ->
classify) is independently testable and reads top to bottom:

    ad_df.pipe(add_join_key)
         .pipe(merge_with_agents, agents_df)
         .pipe(classify_coverage, stale_days=14)

This module must stay importable without Django or Celery: it is called
identically from the synchronous management command and the Celery chord
callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from agent_parity.models import (
    AgentDevice,
    CoverageStatus,
    OSLifecycleStatus,
    infer_machine_type,
    normalize_hostname,
)
from agent_parity.os_eol import DEFAULT_WARNING_DAYS as DEFAULT_EOL_WARNING_DAYS
from agent_parity.os_eol import eol_status_for_device

#: Columns every agents frame carries into the merge. platform/machine_type
#: are worded to match SentinelOne's own vocabulary regardless of which
#: vendor actually reported the device (see AgentDevice's docstring).
#: os_build is only ever set by SentinelOne (see AgentDevice's docstring).
AGENT_COLUMNS = [
    "join_key",
    "hostname",
    "os",
    "os_build",
    "vendor",
    "agent_id",
    "last_seen",
    "agent_version",
    "platform",
    "machine_type",
]


@dataclass(frozen=True)
class CorrelationResult:
    """The full classified frame plus the aggregates reporting needs."""

    frame: pd.DataFrame
    summary: dict


def agents_to_frame(devices: Iterable[AgentDevice]) -> pd.DataFrame:
    """Normalized AgentDevice records (any mix of vendors) -> one agents_df."""
    rows = [
        {
            "hostname": d.hostname,
            "os": d.os,
            "os_build": d.os_build,
            "vendor": d.vendor,
            "agent_id": d.agent_id,
            "last_seen": d.last_seen,
            "agent_version": d.agent_version,
            "platform": d.platform,
            "machine_type": d.machine_type,
        }
        for d in devices
    ]
    frame = pd.DataFrame(rows, columns=[c for c in AGENT_COLUMNS if c != "join_key"])
    frame["last_seen"] = pd.to_datetime(frame["last_seen"], utc=True)
    return add_join_key(frame)


def add_join_key(df: pd.DataFrame, source_col: str = "hostname") -> pd.DataFrame:
    """Stage 1: derive the normalized join key (idempotent)."""
    out = df.copy()
    out["join_key"] = out[source_col].map(normalize_hostname)
    return out[out["join_key"] != ""].reset_index(drop=True)


def merge_with_agents(ad_df: pd.DataFrame, agents_df: pd.DataFrame) -> pd.DataFrame:
    """Stage 2: outer merge with the indicator column that drives everything.

    A device reporting to two vendors yields two rows (one per vendor match);
    an AD device no vendor reports yields exactly one ``left_only`` row.
    """
    return pd.merge(
        ad_df,
        agents_df,
        on="join_key",
        how="outer",
        suffixes=("_ad", "_agent"),
        indicator=True,
    )


def classify_coverage(
        merged: pd.DataFrame,
        stale_days: int = 14,
        as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Stage 3: merge indicator + last_seen staleness -> CoverageStatus."""
    as_of = as_of or pd.Timestamp.now(tz="UTC")
    cutoff = as_of - timedelta(days=stale_days)

    out = merged.copy()
    # NaT compares False, so a matched agent with no last_seen counts as stale
    # rather than silently covered — the conservative call for a coverage tool.
    recent = out["last_seen"] >= cutoff
    out["status"] = np.select(
        [
            out["_merge"] == "left_only",
            out["_merge"] == "right_only",
            (out["_merge"] == "both") & recent,
        ],
        [
            CoverageStatus.MISSING_AGENT.value,
            CoverageStatus.ORPHANED_AGENT.value,
            CoverageStatus.COVERED.value,
        ],
        default=CoverageStatus.STALE_COVERAGE.value,
    )
    out["match_method"] = np.where(out["_merge"] == "both", "hostname_exact", "none")
    return out


def backfill_machine_type(classified: pd.DataFrame) -> pd.DataFrame:
    """Stage 4: give every row a ``machine_type``, even a ``missing_agent`` one.

    ``machine_type`` normally only comes from the agent side (see
    ``AgentDevice``'s docstring) — a ``missing_agent`` row has no agent
    record at all, so without this it would carry no criticality signal
    whatsoever. That's exactly backwards for a coverage tool: a missing
    Domain Controller is the row that most needs to stand out. AD's own OS
    text gets the same ``infer_machine_type()`` heuristic connectors already
    use for vendors with no native ``machineType`` field — a Windows Server
    SKU is a reliable signal on its own; hostname naming conventions (what a
    file/storage server might be called) are not, so this deliberately
    doesn't try to guess from the hostname at all.
    """
    out = classified.copy()
    has_machine_type = out["machine_type"].notna() & (out["machine_type"] != "")
    if "os_ad" in out.columns:
        inferred = out["os_ad"].map(
            lambda text: infer_machine_type(text) if isinstance(text, str) else ""
        )
        out["machine_type"] = out["machine_type"].where(has_machine_type, inferred)
    return out


def _coalesce(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    """First non-null, non-empty value across ``cols``, in priority order —
    a column-wise ``fillna`` chain (each column is a single vectorized pass
    over every row) rather than a per-row Python-level scan. Empty strings
    count as "missing" the same way a real gap would (an agent reporting
    ``os=""`` shouldn't win over AD's real value); ``replace`` is a no-op on
    numeric columns, so the same helper coalesces both os_build and os text.
    """
    result = pd.Series(pd.NA, index=df.index, dtype="object")
    for col in cols:
        candidate = df[col].replace("", pd.NA) if df[col].dtype == object else df[col]
        result = result.fillna(candidate)
    return result


def classify_eol_status(
        classified: pd.DataFrame,
        as_of: pd.Timestamp | None = None,
        warning_days: int = DEFAULT_EOL_WARNING_DAYS,
) -> pd.DataFrame:
    """Stage 5: classify every row's OS lifecycle status (end_of_life /
    eol_soon / supported / unknown) — an independent prioritization signal
    alongside coverage status and machine_type: a missing, end-of-life
    Domain Controller is a very different priority than a missing,
    actively-supported one.

    Prefers the agent's own reported build number and OS text (freshest,
    live data, and the only source that ever has a build number at all —
    SentinelOne; Carbon Black/BitDefender don't), falling back to AD's,
    which is captured for every device now, not just missing_agent rows.
    Free-text OS-name matching (agent_parity.os_eol) is the last resort when
    neither side has a build number, not the first. The resolved build
    number is also kept as its own column (``os_build``) — not just an
    intermediate value — so persistence has one clean field to write,
    matching whichever source actually determined the status.
    """
    out = classified.copy()
    as_of_date = (as_of or pd.Timestamp.now(tz="UTC")).date()
    build_cols = [c for c in ("os_build_agent", "os_build_ad") if c in out.columns]
    text_cols = [c for c in ("os_agent", "os_ad") if c in out.columns]

    if out.empty:
        out["os_build"] = pd.Series(dtype="object")
        out["eol_status"] = pd.Series(dtype="object")
        return out

    resolved_build = _coalesce(out, build_cols) if build_cols else pd.Series(
        pd.NA, index=out.index, dtype="object"
    )
    out["os_build"] = resolved_build.map(lambda v: int(v) if pd.notna(v) else None)
    resolved_text = _coalesce(out, text_cols) if text_cols else pd.Series(
        pd.NA, index=out.index, dtype="object"
    )
    # pd.NA doesn't behave like None in a boolean context (eol_status_for_device
    # does `os_text or ""`, which raises on pd.NA) — normalize before the loop.
    resolved_text = resolved_text.where(resolved_text.notna(), None)

    # eol_status_for_device's own lookup (agent_parity.os_eol) is a scalar
    # function over a small reference table, not something to vectorize —
    # this loop is over resolved, already-coalesced values (two plain
    # columns), not full-row Series reconstruction like .apply(axis=1) does.
    out["eol_status"] = [
        eol_status_for_device(text, build, as_of=as_of_date, warning_days=warning_days)
        for text, build in zip(resolved_text, out["os_build"])
    ]
    return out


def summarize(frame: pd.DataFrame) -> dict:
    """Aggregates for reporting — plain value_counts/groupby, nothing clever."""
    status_counts = frame["status"].value_counts().to_dict()
    covered = status_counts.get(CoverageStatus.COVERED.value, 0)
    stale = status_counts.get(CoverageStatus.STALE_COVERAGE.value, 0)
    missing = status_counts.get(CoverageStatus.MISSING_AGENT.value, 0)
    denominator = covered + stale + missing  # AD-known device rows

    matched = frame[frame["vendor"].notna()]
    by_vendor = {
        vendor: group["status"].value_counts().to_dict()
        for vendor, group in matched.groupby("vendor")
    }

    # Servers stand in for "high-value assets" (Domain Controllers, file/
    # storage servers, ...) — reliably identifiable by OS SKU, unlike
    # hostname naming conventions. Reported the same shape as the overall
    # coverage stats so a quarterly report can show "coverage is improving"
    # and "the assets that matter most are covered" side by side.
    servers = frame[frame["machine_type"] == "server"]
    server_status_counts = servers["status"].value_counts().to_dict()
    server_covered = server_status_counts.get(CoverageStatus.COVERED.value, 0)
    server_stale = server_status_counts.get(CoverageStatus.STALE_COVERAGE.value, 0)
    server_missing = server_status_counts.get(CoverageStatus.MISSING_AGENT.value, 0)
    server_denominator = server_covered + server_stale + server_missing

    # OS lifecycle status is a third, independent prioritization axis: a
    # device whose OS is already end-of-life (or close to it) is worth
    # flagging regardless of coverage status — an uncovered end-of-life
    # server is the worst case, but a *covered* one still means the OS
    # itself needs upgrading, which no agent fixes.
    eol_counts = frame["eol_status"].value_counts().to_dict() if "eol_status" in frame else {}
    at_risk = (
        frame[frame["eol_status"].isin([OSLifecycleStatus.END_OF_LIFE, OSLifecycleStatus.EOL_SOON])]
        if "eol_status" in frame
        else frame.iloc[0:0]
    )
    at_risk_status_counts = at_risk["status"].value_counts().to_dict() if len(at_risk) else {}

    return {
        "total_rows": int(len(frame)),
        "unique_devices": int(frame["join_key"].nunique()),
        "status_counts": {k: int(v) for k, v in status_counts.items()},
        "coverage_pct": round(100.0 * covered / denominator, 1) if denominator else 0.0,
        "by_vendor": by_vendor,
        "server_status_counts": {k: int(v) for k, v in server_status_counts.items()},
        "server_coverage_pct": round(100.0 * server_covered / server_denominator, 1)
        if server_denominator
        else 0.0,
        "eol_status_counts": {k: int(v) for k, v in eol_counts.items()},
        "at_risk_status_counts": {k: int(v) for k, v in at_risk_status_counts.items()},
    }


def correlate(
        ad_df: pd.DataFrame,
        agents_df: pd.DataFrame,
        stale_days: int = 14,
        as_of: pd.Timestamp | None = None,
        eol_warning_days: int = DEFAULT_EOL_WARNING_DAYS,
) -> CorrelationResult:
    """Run the full chain and return the classified frame plus aggregates."""
    frame = (
        ad_df.pipe(add_join_key)
        .pipe(merge_with_agents, agents_df)
        .pipe(classify_coverage, stale_days=stale_days, as_of=as_of)
        .pipe(backfill_machine_type)
        .pipe(classify_eol_status, as_of=as_of, warning_days=eol_warning_days)
    )
    return CorrelationResult(frame=frame, summary=summarize(frame))
