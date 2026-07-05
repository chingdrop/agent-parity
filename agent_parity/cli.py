"""Standalone entrypoint: collect + correlate one or more clients, no server
required.

    uv run agent-parity --all
    uv run agent-parity --client acme

Writes ``out/<slug>.csv`` (the full classified frame) per client and prints a
one-line summary. With no credentials configured, every connector runs
against ``sample_data/`` fixtures — the same fixture-fallback behavior the
rest of the package always has. This has no persistence or history; a caller
that needs either (a dashboard, a scheduler) is expected to import
``agent_parity.pipeline.run_correlation_for_client`` directly rather than
shell out to this CLI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_parity.config import ConfigError, load_config
from agent_parity.pipeline import run_correlation_for_client

OUT_DIR = Path("out")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--client", help="Client slug (default: the first client, alphabetically).")
    parser.add_argument("--all", action="store_true", help="Run for every client instead of just one.")
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    raise SystemExit(main())
