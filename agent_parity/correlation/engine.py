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

from agent_parity.models import AgentDevice, CoverageStatus, normalize_hostname

#: Columns every agents frame carries into the merge.
AGENT_COLUMNS = ["join_key", "hostname", "os", "vendor", "agent_id", "last_seen", "agent_version"]


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
            "vendor": d.vendor,
            "agent_id": d.agent_id,
            "last_seen": d.last_seen,
            "agent_version": d.agent_version,
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

    return {
        "total_rows": int(len(frame)),
        "unique_devices": int(frame["join_key"].nunique()),
        "status_counts": {k: int(v) for k, v in status_counts.items()},
        "coverage_pct": round(100.0 * covered / denominator, 1) if denominator else 0.0,
        "by_vendor": by_vendor,
    }


def correlate(
        ad_df: pd.DataFrame,
        agents_df: pd.DataFrame,
        stale_days: int = 14,
        as_of: pd.Timestamp | None = None,
) -> CorrelationResult:
    """Run the full chain and return the classified frame plus aggregates."""
    frame = (
        ad_df.pipe(add_join_key)
        .pipe(merge_with_agents, agents_df)
        .pipe(classify_coverage, stale_days=stale_days, as_of=as_of)
    )
    return CorrelationResult(frame=frame, summary=summarize(frame))
