"""Standalone entrypoint, no server required.

    uv run agent-parity run --all                       # config.yaml + connectors (live or fixture)
    uv run agent-parity run --client acme
    uv run agent-parity compare ad_export.csv agent_export.csv   # two CSVs, zero config

``run`` collects from every configured client/vendor (``sample_data/``
fixtures when no live credentials are set) and correlates. ``compare`` skips
config.yaml/connectors/credentials entirely — hand it an AD export and any
EDR's inventory mapped into agent-parity's own column schema (see
``agent_parity.agent_csv``) and it correlates those two files directly; a
good first step before setting up ``config.yaml`` for repeatable/scheduled
runs against a live API.

Both write ``output/<name>.csv`` (the full classified frame) and print a
one-line summary. Neither has persistence or history; a caller that needs
either (a dashboard, a scheduler) is expected to import
``agent_parity.pipeline`` directly rather than shell out to this CLI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_parity.ad_sync.parser import ADParseError
from agent_parity.agent_csv import AgentCSVParseError
from agent_parity.config import ConfigError, load_config
from agent_parity.pipeline import correlate_from_csvs, run_correlation_for_client

OUT_DIR = Path("output")


def _run(args: argparse.Namespace) -> int:
    config = load_config()
    if not config.clients:
        print("No clients configured in config.yaml.", file=sys.stderr)
        return 1

    if args.all:
        slugs = sorted(config.clients)
    elif args.client:
        if args.client not in config.clients:
            print(
                f"Unknown client {args.client!r}; configured: {', '.join(sorted(config.clients))}",
                file=sys.stderr,
            )
            return 1
        slugs = [args.client]
    else:
        slugs = [sorted(config.clients)[0]]

    OUT_DIR.mkdir(exist_ok=True)
    exit_code = 0
    for slug in slugs:
        try:
            result, vendor_status = run_correlation_for_client(config, config.client(slug))
        except ConfigError as exc:
            print(f"[{slug}] config error: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        status_summary = ", ".join(f"{name}={state}" for name, state in sorted(vendor_status.items()))
        if result is None:
            print(f"[{slug}] FAILED: every AD domain export failed ({status_summary})", file=sys.stderr)
            exit_code = 1
            continue

        out_path = OUT_DIR / f"{slug}.csv"
        result.frame.to_csv(out_path, index=False)
        counts = ", ".join(f"{k}={v}" for k, v in sorted(result.summary["status_counts"].items()))
        print(
            f"[{slug}] {len(result.frame)} rows -> {out_path} "
            f"(coverage {result.summary['coverage_pct']}%; {counts}; {status_summary})"
        )
    return exit_code


def _compare(args: argparse.Namespace) -> int:
    try:
        result = correlate_from_csvs(
            args.ad_csv.read_text(), args.agent_csv.read_text(), stale_days=args.stale_days
        )
    except (OSError, ADParseError, AgentCSVParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_path = args.out or OUT_DIR / f"{args.agent_csv.stem}_correlated.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.frame.to_csv(out_path, index=False)
    counts = ", ".join(f"{k}={v}" for k, v in sorted(result.summary["status_counts"].items()))
    print(f"{len(result.frame)} rows -> {out_path} (coverage {result.summary['coverage_pct']}%; {counts})")
    print(
        "For repeatable, scheduled runs against a live vendor API instead of a "
        "one-off export, see config.yaml and `agent-parity run` in the README."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="mode", required=True)

    run_parser = subparsers.add_parser("run", help="Collect + correlate via config.yaml and connectors.")
    run_parser.add_argument("--client", help="Client slug (default: the first client, alphabetically).")
    run_parser.add_argument("--all", action="store_true", help="Run for every client instead of just one.")
    run_parser.set_defaults(func=_run)

    compare_parser = subparsers.add_parser(
        "compare", help="Correlate two CSVs directly — no config.yaml, no connectors, no credentials."
    )
    compare_parser.add_argument("ad_csv", type=Path, help="Export-ADDevices.ps1 output.")
    compare_parser.add_argument(
        "agent_csv", type=Path, help="Agent/EDR inventory mapped into agent-parity's column schema."
    )
    compare_parser.add_argument("--stale-days", type=int, default=14)
    compare_parser.add_argument("--out", type=Path, default=None, help="Output CSV path (default: output/<agent_csv stem>_correlated.csv).")
    compare_parser.set_defaults(func=_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
