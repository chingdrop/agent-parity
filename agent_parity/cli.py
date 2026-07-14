"""Standalone entrypoint, no server required.

    uv run agent-parity run --all                       # config.yaml + connectors (live or fixture)
    uv run agent-parity run --client acme
    uv run agent-parity compare ad_export.csv agent_export.csv   # two CSVs, zero config
    uv run agent-parity sync --all                       # config.yaml + connectors, persisted to SQLite

``run`` collects from every configured client/vendor (``sample_data/``
fixtures when no live credentials are set) and correlates; it writes
``output/<name>.csv`` and prints a one-line summary, with zero persistence —
a fresh ``CorrelationResult`` every time, nothing remembered between runs.
``compare`` skips config.yaml/connectors/credentials entirely — hand it an AD
export and any EDR's inventory mapped into agent-parity's own column schema
(see ``agent_parity.agent_csv``) and it correlates those two files directly;
a good first step before setting up ``config.yaml`` for repeatable/scheduled
runs against a live API. ``sync`` is the persisted counterpart of ``run`` —
same collection/correlation, but recorded as a ``CorrelationRun`` (see
``agent_parity.persistence``) in a SQLite-backed history, the same
entrypoint Celery's chord callback (``agent_parity.tasks``) uses when
scheduled instead of run by hand.
"""

from __future__ import annotations

from pathlib import Path

import click

from agent_parity.ad_sync.parser import ADParseError
from agent_parity.agent_csv import AgentCSVParseError
from agent_parity.config import ConfigError, load_config
from agent_parity.db import get_engine, init_db, session_factory
from agent_parity.persistence import run_and_persist_for_client
from agent_parity.pipeline import correlate_from_csvs, run_correlation_for_client

OUT_DIR = Path("output")


@click.group()
def cli() -> None:
    """Collect + correlate device coverage, no server required."""


@cli.command()
@click.option("--client", help="Client slug (default: the first client, alphabetically).")
@click.option("--all", "run_all", is_flag=True, help="Run for every client instead of just one.")
def run(client: str | None, run_all: bool) -> None:
    """Collect + correlate via config.yaml and connectors."""
    config = load_config()
    if not config.clients:
        raise click.ClickException("No clients configured in config.yaml.")

    if run_all:
        slugs = sorted(config.clients)
    elif client:
        if client not in config.clients:
            raise click.ClickException(
                f"Unknown client {client!r}; configured: {', '.join(sorted(config.clients))}"
            )
        slugs = [client]
    else:
        slugs = [sorted(config.clients)[0]]

    OUT_DIR.mkdir(exist_ok=True)
    had_failure = False
    for slug in slugs:
        try:
            result, vendor_status = run_correlation_for_client(config, config.client(slug))
        except ConfigError as exc:
            click.echo(f"[{slug}] config error: {exc}", err=True)
            had_failure = True
            continue

        status_summary = ", ".join(f"{name}={state}" for name, state in sorted(vendor_status.items()))
        if result is None:
            click.echo(f"[{slug}] FAILED: every AD domain export failed ({status_summary})", err=True)
            had_failure = True
            continue

        out_path = OUT_DIR / f"{slug}.csv"
        result.frame.to_csv(out_path, index=False)
        counts = ", ".join(f"{k}={v}" for k, v in sorted(result.summary["status_counts"].items()))
        click.echo(
            f"[{slug}] {len(result.frame)} rows -> {out_path} "
            f"(coverage {result.summary['coverage_pct']}%; {counts}; {status_summary})"
        )
    if had_failure:
        raise SystemExit(1)


@cli.command()
@click.option("--client", help="Client slug (default: the first client, alphabetically).")
@click.option("--all", "run_all", is_flag=True, help="Run for every client instead of just one.")
def sync(client: str | None, run_all: bool) -> None:
    """Collect + correlate via config.yaml and connectors, persisted to SQLite.

    Same collection/correlation as ``run``, but each invocation is recorded
    as a ``CorrelationRun`` (see ``agent_parity.persistence``) instead of
    just a CSV — the synchronous, single-process counterpart of what
    ``agent_parity.tasks``'s Celery chord does when scheduled.
    """
    config = load_config()
    if not config.clients:
        raise click.ClickException("No clients configured in config.yaml.")

    if run_all:
        slugs = sorted(config.clients)
    elif client:
        if client not in config.clients:
            raise click.ClickException(
                f"Unknown client {client!r}; configured: {', '.join(sorted(config.clients))}"
            )
        slugs = [client]
    else:
        slugs = [sorted(config.clients)[0]]

    engine = get_engine()
    init_db(engine)
    Session = session_factory(engine)

    had_failure = False
    with Session() as session:
        for slug in slugs:
            try:
                run_row = run_and_persist_for_client(session, config, config.client(slug))
            except ConfigError as exc:
                click.echo(f"[{slug}] config error: {exc}", err=True)
                had_failure = True
                continue

            status_summary = ", ".join(f"{name}={state}" for name, state in sorted(run_row.vendor_status.items()))
            snapshot_count = len(run_row.snapshots)
            click.echo(
                f"[{slug}] run {run_row.id}: {run_row.status} "
                f"({snapshot_count} snapshots; {status_summary})"
            )
            if run_row.status == "failed":
                had_failure = True
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
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.frame.to_csv(out_path, index=False)
    counts = ", ".join(f"{k}={v}" for k, v in sorted(result.summary["status_counts"].items()))
    click.echo(f"{len(result.frame)} rows -> {out_path} (coverage {result.summary['coverage_pct']}%; {counts})")
    click.echo(
        "For repeatable, scheduled runs against a live vendor API instead of a "
        "one-off export, see config.yaml and `agent-parity run` in the README."
    )


if __name__ == "__main__":
    cli()
