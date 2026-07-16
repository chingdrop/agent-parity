"""Parse a generic, vendor-agnostic agent/EDR inventory CSV.

Every real EDR vendor exports differently (SentinelOne's console export vs.
CrowdStrike's vs. Microsoft Defender's), so unlike ``ad_export.py``
(which parses exactly one script's known output), this expects the caller to
have already mapped their tool's export into agent-parity's own column
schema — a one-time exercise per tool, not something this module guesses at.
This is the agent-side counterpart that makes ``agent_parity.pipeline
.correlate_from_csvs`` possible with zero connectors, config.yaml, or
credentials: bring two CSVs, get a classified frame.
"""

from __future__ import annotations

import io

import pandas as pd

from agent_parity.correlation import AGENT_COLUMNS, add_join_key

#: Every column ``parse_agent_csv`` understands. Only "hostname" is required;
#: the rest default to blank/None/NaT when the column is absent entirely —
#: the same "optional column, defaults for the whole frame" pattern
#: ad_export.py already uses for the AD side.
AGENT_CSV_COLUMNS = [c for c in AGENT_COLUMNS if c != "join_key"]


class AgentCSVParseError(Exception):
    pass


def parse_agent_csv(raw_csv: str) -> pd.DataFrame:
    """Raw agent-inventory CSV text -> DataFrame shaped like ``agents_to_frame``'s
    output, so ``correlate()`` can't tell the difference between this and a
    connector-collected frame.

    Required column: ``hostname``. Optional: ``os``, ``os_build``, ``vendor``,
    ``agent_id``, ``last_seen``, ``agent_version``, ``platform``,
    ``machine_type`` — each defaults to blank/``None``/``NaT`` for the whole
    frame when the column is missing entirely (a per-row blank value is
    always fine; an absent column just means "this export doesn't carry that
    field at all").
    """
    try:
        frame = pd.read_csv(io.StringIO(raw_csv), dtype=str, keep_default_na=False)
    except Exception as exc:
        raise AgentCSVParseError(f"agent CSV is not parseable: {exc}") from exc

    if "hostname" not in frame.columns:
        raise AgentCSVParseError(f"agent CSV is missing the required 'hostname' column (got: {list(frame.columns)})")

    def optional(col: str) -> pd.Series:
        return frame[col] if col in frame.columns else pd.Series([""] * len(frame))

    parsed = pd.DataFrame(
        {
            "hostname": frame["hostname"].str.strip(),
            "os": optional("os"),
            "os_build": (
                frame["os_build"].map(lambda v: int(v) if v else None) if "os_build" in frame.columns else None
            ),
            "vendor": optional("vendor"),
            "agent_id": optional("agent_id"),
            "last_seen": (
                pd.to_datetime(frame["last_seen"], errors="coerce", utc=True)
                if "last_seen" in frame.columns
                else pd.NaT
            ),
            "agent_version": optional("agent_version"),
            "platform": optional("platform"),
            "machine_type": optional("machine_type"),
        }
    )
    return add_join_key(parsed)[["join_key", *AGENT_CSV_COLUMNS]]
