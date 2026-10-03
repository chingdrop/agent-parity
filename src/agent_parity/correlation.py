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
         .pipe(resolve_ambiguous_join_keys)
         .pipe(classify_coverage, stale_days=14)

This module must stay importable without Celery or SQLAlchemy: it is called
identically from the pure ``compare`` CLI path, the persisted ``run`` path, and
the Celery chord callback.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

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

logger = logging.getLogger(__name__)

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


def resolve_ambiguous_join_keys(merged: pd.DataFrame) -> pd.DataFrame:
    """Stage 2b: handle a short hostname that exists in more than one AD domain.

    Domains are separate namespaces, so ``WS-001.corp`` and ``WS-001.branch``
    are two machines that share one join key. A short-name merge pairs every
    agent called ``WS-001`` with both, so one agent would make both look
    covered. Two things happen here, both exact (no fuzzy matching):

    * **FQDN tiebreak.** An agent that reported a full DNS name (``hostname``
      containing a dot) keeps only its pairing with the AD object whose
      ``DNSHostName`` equals it, marked ``match_method = "fqdn_exact"``. An AD
      object left with no agent becomes ``missing_agent``; an agent whose FQDN
      matches none of them becomes ``orphaned_agent``.
    * **Flag the rest.** Pairings that still rest on the short name alone get
      ``ambiguous_join_key = True`` — they may be crediting the wrong machine,
      and ``summarize`` counts them.

    An AD object's identity is its DNS name, else its distinguished name. With
    neither (a hand-built frame), there's nothing to tell objects apart and the
    frame passes through unflagged.
    """
    out = merged.copy()
    out["ambiguous_join_key"] = False
    if "dns_hostname" not in out.columns and "distinguished_name" not in out.columns:
        return out

    def _text(col: str) -> pd.Series:
        if col not in out.columns:
            return pd.Series("", index=out.index)
        return out[col].fillna("").astype(str).str.strip().str.lower()

    dns, dn = _text("dns_hostname"), _text("distinguished_name")
    ad_id = dns.where(dns != "", dn)
    has_ad = out["_merge"] != "right_only"
    objects_per_key = ad_id[has_ad & (ad_id != "")].groupby(out.loc[has_ad, "join_key"]).nunique()
    ambiguous = out["join_key"].isin(objects_per_key[objects_per_key > 1].index)
    if not ambiguous.any():
        return out

    agent_hostname = _text("hostname_agent" if "hostname_agent" in out.columns else "hostname")
    paired = ambiguous & (out["_merge"] == "both")
    by_fqdn = paired & agent_hostname.str.contains(".", regex=False)
    keep = by_fqdn & (agent_hostname == dns)
    drop = by_fqdn & ~keep

    agent_cols = [
        c + "_agent" if c + "_agent" in out.columns else c
        for c in AGENT_COLUMNS
        if c != "join_key" and (c in out.columns or c + "_agent" in out.columns)
    ]
    bookkeeping = {"join_key", "_merge", "ambiguous_join_key", "match_method"}
    ad_cols = [c for c in out.columns if c not in agent_cols and c not in bookkeeping]
    agent_id = out["vendor"].astype(str) + "/" + out["agent_id"].astype(str)

    kept = out[~drop].copy()
    kept["match_method"] = pd.Series("fqdn_exact", index=kept.index).where(keep[~drop])
    kept["ambiguous_join_key"] = (paired & ~by_fqdn)[~drop]

    still_paired_ad = set(ad_id[~drop & has_ad])
    unpaired_ad = out[drop & ~ad_id.isin(still_paired_ad)].copy()
    unpaired_ad = unpaired_ad.loc[~ad_id[unpaired_ad.index].duplicated()]
    unpaired_ad[agent_cols] = pd.NA
    unpaired_ad["_merge"] = "left_only"

    matched_agents = set(agent_id[keep])
    unmatched_agents = out[drop & ~agent_id.isin(matched_agents)].copy()
    unmatched_agents = unmatched_agents.loc[~agent_id[unmatched_agents.index].duplicated()]
    unmatched_agents[ad_cols] = pd.NA
    unmatched_agents["_merge"] = "right_only"

    resolved = pd.concat([kept, unpaired_ad, unmatched_agents], ignore_index=True)
    resolved["_merge"] = pd.Categorical(resolved["_merge"], categories=out["_merge"].cat.categories)
    return resolved


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
    # resolve_ambiguous_join_keys may already have marked a pairing fqdn_exact.
    preset = out["match_method"] if "match_method" in out.columns else pd.Series(pd.NA, index=out.index)
    out["match_method"] = np.where(out["_merge"] == "both", preset.fillna("hostname_exact"), "none")
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
        inferred = out["os_ad"].map(lambda text: infer_machine_type(text) if isinstance(text, str) else "")
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

    resolved_build = _coalesce(out, build_cols) if build_cols else pd.Series(pd.NA, index=out.index, dtype="object")
    out["os_build"] = resolved_build.map(lambda v: int(v) if pd.notna(v) else None)
    resolved_text = _coalesce(out, text_cols) if text_cols else pd.Series(pd.NA, index=out.index, dtype="object")
    # pd.NA doesn't behave like None in a boolean context (eol_status_for_device
    # does `os_text or ""`, which raises on pd.NA) — normalize before the loop.
    resolved_text = resolved_text.where(resolved_text.notna(), None)

    # eol_status_for_device's own lookup (agent_parity.os_eol) is a scalar
    # function over a small reference table, not something to vectorize —
    # this loop is over resolved, already-coalesced values (two plain
    # columns), not full-row Series reconstruction like .apply(axis=1) does.
    out["eol_status"] = [
        eol_status_for_device(text, build, as_of=as_of_date, warning_days=warning_days)
        for text, build in zip(resolved_text, out["os_build"], strict=True)
    ]
    return out


def coverage_pct(status_counts: dict[str, int]) -> float:
    """Covered rows as a share of the rows AD knows about (covered, stale or
    missing; orphaned agents have no AD record, so they don't count). The one
    definition of coverage, shared by ``summarize`` and the quarterly report's
    history so the two can never disagree."""
    covered = status_counts.get(CoverageStatus.COVERED.value, 0)
    known = covered + sum(
        status_counts.get(s.value, 0) for s in (CoverageStatus.STALE_COVERAGE, CoverageStatus.MISSING_AGENT)
    )
    return round(100.0 * covered / known, 1) if known else 0.0


def summarize(frame: pd.DataFrame) -> dict:
    """Aggregates for reporting — plain value_counts/groupby, nothing clever."""
    status_counts = {str(k): int(v) for k, v in frame["status"].value_counts().items()}

    matched = frame[frame["vendor"].notna()]
    by_vendor = {vendor: group["status"].value_counts().to_dict() for vendor, group in matched.groupby("vendor")}

    # Servers stand in for "high-value assets" (Domain Controllers, file/
    # storage servers, ...) — reliably identifiable by OS SKU, unlike
    # hostname naming conventions. Reported the same shape as the overall
    # coverage stats so a quarterly report can show "coverage is improving"
    # and "the assets that matter most are covered" side by side.
    servers = frame[frame["machine_type"] == "server"]
    server_status_counts = {str(k): int(v) for k, v in servers["status"].value_counts().items()}

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
        "coverage_pct": coverage_pct(status_counts),
        "by_vendor": by_vendor,
        "server_status_counts": {k: int(v) for k, v in server_status_counts.items()},
        "server_coverage_pct": coverage_pct(server_status_counts),
        "eol_status_counts": {k: int(v) for k, v in eol_counts.items()},
        "at_risk_status_counts": {k: int(v) for k, v in at_risk_status_counts.items()},
        # Join keys whose agent pairing rests on a short hostname that exists in
        # more than one AD domain (see resolve_ambiguous_join_keys).
        "ambiguous_join_keys": (
            int(frame.loc[frame["ambiguous_join_key"].astype(bool), "join_key"].nunique())
            if "ambiguous_join_key" in frame.columns
            else 0
        ),
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
        .pipe(resolve_ambiguous_join_keys)
        .pipe(classify_coverage, stale_days=stale_days, as_of=as_of)
        .pipe(backfill_machine_type)
        .pipe(classify_eol_status, as_of=as_of, warning_days=eol_warning_days)
    )
    if frame["ambiguous_join_key"].any():
        keys = sorted(frame.loc[frame["ambiguous_join_key"], "join_key"].unique())
        logger.warning(
            "%d hostname(s) exist in more than one AD domain and an agent reported only the short name, "
            "so its match can't be attributed to one machine: %s",
            len(keys),
            ", ".join(keys),
        )
    return CorrelationResult(frame=frame, summary=summarize(frame))
