"""Standalone entrypoint, no server required.

    uv run agent-parity run --all                       # config.yaml + connectors (live or fixture)
    uv run agent-parity run --client acme --csv         # ... and also write output/acme.csv
    uv run agent-parity run --all --workers             # ... fanned out to running Celery workers
    uv run agent-parity compare ad_export.csv agent_export.csv   # two CSVs, zero config

``run`` collects from every configured client/vendor (``sample_data/``
fixtures when no live credentials are set), correlates, and records each
client's result as a ``CorrelationRun`` in a SQLite-backed history. It
executes exactly the Celery chord beat schedules
(``agent_parity.scheduling.tasks.start_client_run``): by default the tasks run
in-process, one after another, so no broker or worker is needed; with
``--workers`` they're dispatched to real workers and run in parallel, which
is what cut the original tool's hours-long collection runs down.
``--csv`` also writes the full classified frame to ``output/<client>.csv``.
``compare`` skips config.yaml/connectors/credentials/persistence entirely —
hand it an AD export and any EDR's inventory mapped into agent-parity's own
column schema (see ``agent_parity.agent_csv``) and it correlates those two
files directly; a good first step before setting up ``config.yaml`` for
repeatable/scheduled runs against a live API.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from pathlib import Path

import click

from agent_parity.ad_export import ADParseError
from agent_parity.agent_csv import AgentCSVParseError
from agent_parity.config import load_config
from agent_parity.pipeline import correlate_from_csvs
from agent_parity.scheduling.celery_app import app as celery_app
from agent_parity.scheduling.celery_app import run_eagerly
from agent_parity.scheduling.tasks import start_client_run
from agent_parity.shared.atomic_io import atomic_write, ensure_dir
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
@click.option(
    "--workers",
    is_flag=True,
    help="Dispatch to running Celery workers (in parallel) instead of running the tasks in-process.",
)
def run(client: str | None, run_all: bool, write_csv: bool, workers: bool) -> None:
    """Collect + correlate via config.yaml and connectors, recorded in SQLite run history.

    Runs the same Celery chord beat schedules: in-process by default, or on
    real workers with --workers.
    """
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

    if write_csv:
        ensure_dir(OUT_DIR)

    had_failure = False
    with nullcontext() if workers else run_eagerly():
        if workers:
            _require_workers()
        # Every client's chord is dispatched before any is waited on, so with
        # --workers the clients run concurrently, not just their fan-out tasks.
        started = [(slug, *start_client_run(config, config.client(slug), include_csv=write_csv)) for slug in slugs]
        for slug, run_id, pending in started:
            try:
                report = pending.get()
            except Exception as exc:  # noqa: BLE001 — report every client, not just the first failure
                click.echo(f"[{slug}] run {run_id}: failed: {exc}", err=True)
                had_failure = True
                continue
            if not _echo_report(slug, report, write_csv):
                had_failure = True
    if had_failure:
        raise SystemExit(1)


def _require_workers() -> None:
    """Fail fast with --workers when no worker would ever pick the tasks up."""
    try:
        replies = celery_app.control.ping(timeout=2.0)
    except Exception as exc:  # noqa: BLE001 — broker unreachable surfaces as many exception types
        raise click.ClickException(f"Can't reach the Celery broker ({exc}).") from exc
    if not replies:
        raise click.ClickException(
            "No Celery workers responded. Start one (docker compose -f docker/docker-compose.yml up -d redis worker) "
            "or drop --workers to run in-process."
        )


def _echo_report(slug: str, report: dict, write_csv: bool) -> bool:
    """Print one client's run summary (writing its CSV if asked); False if the run failed."""
    status_summary = ", ".join(f"{name}={state}" for name, state in sorted(report["vendor_status"].items()))
    if report["status"] == "failed":
        click.echo(
            f"[{slug}] run {report['run_id']}: failed: every AD domain export failed ({status_summary})", err=True
        )
        return False
    destination = ""
    if write_csv:
        out_path = OUT_DIR / f"{slug}.csv"
        atomic_write(out_path, report["csv"])
        destination = f" -> {out_path}"
    counts = ", ".join(f"{k}={v}" for k, v in sorted(report["status_counts"].items()))
    click.echo(
        f"[{slug}] run {report['run_id']}: {report['status']}, {report['rows']} rows{destination} "
        f"(coverage {report['coverage_pct']}%; {counts}; {status_summary})"
    )
    return True


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
