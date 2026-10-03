"""Seed a demo database with a deterministic quarterly coverage history.

The fixtures in ``sample_data/`` are static, so every real run lands on the same
numbers and a trend report would be a flat line. This writes a believable
history instead, matching the original tool's result: coverage climbing from
about 47% to about 80% over two quarters, with servers (the high-value assets)
fixed first.

For each client it correlates the fixtures once (today's state, used for the
final quarter) and derives the two earlier quarters by taking agents back off
devices: whole devices revert to ``missing_agent``, servers less often than
workstations. Which devices revert is decided by a hash of their join key, so the
history is identical on every run. Each run's timestamps are shifted to that
run's date.

Writes only to the database you name, never the default ``agent_parity.db``:

    uv run python scripts/seed_history.py output/demo_history.db
    uv run --env-file /dev/null env AGENT_PARITY_DB_URL=sqlite:///output/demo_history.db \\
        agent-parity report --client acme --quarter 2026-Q3
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from agent_parity.config import load_config
from agent_parity.correlation import CorrelationResult, coverage_pct, summarize
from agent_parity.models import CoverageStatus
from agent_parity.pipeline import run_correlation_for_client
from agent_parity.quarterly_report import Quarter
from agent_parity.scheduling.db import CorrelationRun, get_engine, init_db, session_factory
from agent_parity.scheduling.persistence import persist_correlation, sync_client_from_config

#: Earlier quarters' coverage as a fraction of the final quarter's: overall
#: (47% when the final is 81.8%) and servers, which recover sooner.
OVERALL_SHAPE = (0.575, 0.79)
SERVER_SHAPE = (0.75, 0.95)
AGENT_COLUMNS = ["hostname_agent", "os_agent", "os_build_agent", "vendor", "agent_id", "last_seen", "agent_version"]
TIME_COLUMNS = ["last_logon", "last_seen"]


def _rank(join_key: str) -> str:
    return hashlib.sha256(join_key.encode()).hexdigest()


def _revert(frame: pd.DataFrame, join_keys: set[str]) -> pd.DataFrame:
    """Collapse each device in ``join_keys`` to one AD-only ``missing_agent`` row."""
    hit = frame["join_key"].isin(join_keys)
    reverted = frame[hit].drop_duplicates("join_key").copy()
    for column in AGENT_COLUMNS:
        if column in reverted.columns:
            reverted[column] = None
    reverted["status"] = CoverageStatus.MISSING_AGENT.value
    reverted["match_method"] = "none"
    reverted["_merge"] = "left_only"
    return pd.concat([frame[~hit], reverted], ignore_index=True)


def _pct(frame: pd.DataFrame, servers_only: bool = False) -> float:
    rows = frame[frame["machine_type"] == "server"] if servers_only else frame
    return coverage_pct({str(k): int(v) for k, v in rows["status"].value_counts().items()})


def _earlier_state(final: pd.DataFrame, overall_target: float, server_target: float) -> pd.DataFrame:
    """Revert fully covered devices, servers then workstations, in hash order,
    stopping at whichever step lands closest to each group's target coverage."""
    by_device = final.groupby("join_key")
    fully_covered = by_device["status"].apply(lambda s: (s == CoverageStatus.COVERED.value).all())
    is_server = by_device["machine_type"].first() == "server"
    frame = final
    for servers, target in ((True, server_target), (False, overall_target)):
        pool = sorted((k for k in fully_covered.index if fully_covered[k] and is_server[k] == servers), key=_rank)
        for key in pool:
            current = _pct(frame, servers_only=servers)
            candidate = _revert(frame, {key})
            if abs(_pct(candidate, servers_only=servers) - target) >= abs(current - target):
                break
            frame = candidate
    return frame


def _shift_times(frame: pd.DataFrame, by: timedelta) -> pd.DataFrame:
    out = frame.copy()
    for column in TIME_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True) + by
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db_path", type=Path, help="SQLite file to write (must not exist yet, unless --replace)")
    parser.add_argument(
        "--through",
        default=str(Quarter.of(date.today()).previous()),
        help="last quarter of the history (default: the last complete quarter)",
    )
    parser.add_argument("--replace", action="store_true", help="delete db_path first if it exists")
    args = parser.parse_args()

    if args.db_path.exists():
        if not args.replace:
            print(f"{args.db_path} exists; pass --replace to overwrite it.", file=sys.stderr)
            return 1
        args.db_path.unlink()
    args.db_path.parent.mkdir(parents=True, exist_ok=True)

    through = Quarter.parse(args.through)
    quarters = [through.previous().previous(), through.previous(), through]
    now = datetime.now(UTC)
    config = load_config()
    engine = get_engine(f"sqlite:///{args.db_path}")
    init_db(engine)

    with session_factory(engine)() as session:
        for slug in sorted(config.clients):
            client_cfg = config.client(slug)
            result, vendor_status = run_correlation_for_client(config, client_cfg)
            if result is None:
                print(f"[{slug}] skipped: fixture collection failed", file=sys.stderr)
                continue
            final = result.frame
            final_pct, final_server = _pct(final), _pct(final, servers_only=True)
            states = [
                *(
                    _earlier_state(final, final_pct * overall, final_server * server)
                    for overall, server in zip(OVERALL_SHAPE, SERVER_SHAPE, strict=True)
                ),
                final,
            ]
            client = sync_client_from_config(session, client_cfg)
            for quarter, state in zip(quarters, states, strict=True):
                # Two weeks before quarter end: the quarter's last run.
                run_at = (quarter.end - timedelta(days=14)).replace(tzinfo=UTC)
                frame = _shift_times(state, run_at - now)
                frame["_merge"] = pd.Categorical(frame["_merge"], categories=["left_only", "right_only", "both"])
                frame = frame.replace({np.nan: None})
                run = CorrelationRun(client_id=client.id, started_at=run_at, stale_days=config.stale_days)
                session.add(run)
                session.flush()
                persist_correlation(session, run, CorrelationResult(frame, summarize(frame)), dict(vendor_status))
                session.commit()
                print(f"[{slug}] {quarter}: coverage {_pct(state):.1f}%, servers {_pct(state, servers_only=True):.1f}%")
    engine.dispose()
    print(f"Wrote {args.db_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
