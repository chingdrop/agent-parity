"""Standalone entrypoint, no server required.

    uv run agent-parity run --all                       # config.yaml + connectors (live or fixture)
    uv run agent-parity run --client acme --csv         # ... and also write output/acme.csv
    uv run agent-parity compare ad_export.csv agent_export.csv   # two CSVs, zero config

``run`` collects from every configured client/vendor (``sample_data/``
fixtures when no live credentials are set), correlates, and records each
client's result as a ``CorrelationRun`` (see
``agent_parity.scheduling.persistence``) in a SQLite-backed history — the
synchronous, single-process counterpart of what Celery's chord callback
(``agent_parity.scheduling.tasks``) does when scheduled. ``--csv`` also writes
the full classified frame to ``output/<client>.csv``.
``compare`` skips config.yaml/connectors/credentials/persistence entirely —
hand it an AD export and any EDR's inventory mapped into agent-parity's own
column schema (see ``agent_parity.agent_csv``) and it correlates those two
files directly; a good first step before setting up ``config.yaml`` for
repeatable/scheduled runs against a live API.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from agent_parity.ad_export import ADParseError
from agent_parity.agent_csv import AgentCSVParseError
from agent_parity.config import ConfigError, load_config
from agent_parity.pipeline import correlate_from_csvs
from agent_parity.scheduling.db import get_engine, init_db, session_factory
from agent_parity.scheduling.persistence import run_and_persist_for_client
from agent_parity.shared.atomic_io import ensure_dir
from agent_parity.shared.logging_setup import setup_logging
from agent_parity.shared.tabular_io import write_structured_file

OUT_DIR = Path("output")


def _write_csv(frame, out_path: Path) -> None:
    """Write ``frame`` to ``out_path`` without a reader ever observing a
    truncated file — a plain ``to_csv(out_path)`` isn't atomic."""
    write_structured_file(frame, out_path, file_type="csv", index=False)


@click.group()
def cli() -> None:
    """Collect + correlate device coverage, no server required."""
    # WARNING, not the module's own INFO default: this only needs to make
    # existing logger.warning/.exception calls (a failed vendor API, a
    # Splunk outage) readable — click.echo already covers the per-client
    # summary an INFO level would otherwise duplicate.
    setup_logging(level=logging.WARNING)


@cli.command()
@click.option("--client", help="Client slug (default: the first client, alphabetically).")
@click.option("--all", "run_all", is_flag=True, help="Run for every client instead of just one.")
@click.option("--csv", "write_csv", is_flag=True, help="Also write each client's results to output/<client>.csv.")
def run(client: str | None, run_all: bool, write_csv: bool) -> None:
    """Collect + correlate via config.yaml and connectors, recorded in SQLite run history."""
    config = load_config()
    if not config.clients:
        raise click.ClickException("No clients configured in config.yaml.")

    if run_all:
        slugs = sorted(config.clients)
    elif client:
        if client not in config.clients:
            raise click.ClickException(f"Unknown client {client!r}; configured: {', '.join(sorted(config.clients))}")
        slugs = [client]
    else:
        slugs = [sorted(config.clients)[0]]

    engine = get_engine()
    init_db(engine)
    Session = session_factory(engine)
    if write_csv:
        ensure_dir(OUT_DIR)

    had_failure = False
    try:
        with Session() as session:
            for slug in slugs:
                try:
                    run_row, result = run_and_persist_for_client(session, config, config.client(slug))
                except ConfigError as exc:
                    click.echo(f"[{slug}] config error: {exc}", err=True)
                    had_failure = True
                    continue

                status_summary = ", ".join(f"{name}={state}" for name, state in sorted(run_row.vendor_status.items()))
                if result is None:
                    click.echo(
                        f"[{slug}] run {run_row.id}: {run_row.status}: "
                        f"every AD domain export failed ({status_summary})",
                        err=True,
                    )
                    had_failure = True
                    continue

                destination = ""
                if write_csv:
                    out_path = OUT_DIR / f"{slug}.csv"
                    _write_csv(result.frame, out_path)
                    destination = f" -> {out_path}"
                counts = ", ".join(f"{k}={v}" for k, v in sorted(result.summary["status_counts"].items()))
                click.echo(
                    f"[{slug}] run {run_row.id}: {run_row.status}, {len(result.frame)} rows{destination} "
                    f"(coverage {result.summary['coverage_pct']}%; {counts}; {status_summary})"
                )
    finally:
        engine.dispose()
    if had_failure:
        raise SystemExit(1)


@cli.command()
@click.argument("ad_csv", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("agent_csv", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--stale-days", type=int, default=14, show_default=True)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output CSV path (default: output/<agent_csv stem>_correlated.csv).",
)
def compare(ad_csv: Path, agent_csv: Path, stale_days: int, out_path: Path | None) -> None:
    """Correlate two CSVs directly — no config.yaml, no connectors, no credentials."""
    try:
        result = correlate_from_csvs(ad_csv.read_text(), agent_csv.read_text(), stale_days=stale_days)
    except (OSError, ADParseError, AgentCSVParseError) as exc:
        raise click.ClickException(str(exc)) from exc

    out_path = out_path or OUT_DIR / f"{agent_csv.stem}_correlated.csv"
    ensure_dir(out_path.parent)
    _write_csv(result.frame, out_path)
    counts = ", ".join(f"{k}={v}" for k, v in sorted(result.summary["status_counts"].items()))
    click.echo(f"{len(result.frame)} rows -> {out_path} (coverage {result.summary['coverage_pct']}%; {counts})")
    click.echo(
        "For repeatable, scheduled runs against a live vendor API instead of a "
        "one-off export, see config.yaml and `agent-parity run` in the README."
    )


if __name__ == "__main__":
    cli()
