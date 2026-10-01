"""Celery tasks: how every persisted run is collected.

``agent-parity run`` and beat's ``dispatch_all_clients`` both go through
``start_client_run`` — the same chord either way. ``run`` executes it
in-process by default (``celery_app.run_eagerly``) or on real workers with
``--workers``; beat always uses workers.

Shape: one *group* of fan-out tasks per client — one AD export task per
domain controller (a client with multiple AD domains has more than one),
one inventory-pull task per (vendor, site/tenant) the client has within
that vendor (almost always just one), feeding a *chord* callback that runs
the pandas correlation exactly once, against that client's complete result
set. Persistence goes through ``agent_parity.scheduling.persistence``.

Three deliberate design points:

* **Idempotency** — the ``CorrelationRun`` row is created (empty, PENDING)
  and committed *before* the chord is dispatched, and its id rides through
  the callback signature. A retried or double-fired callback finds the run
  already finalized and no-ops (``persistence.persist_correlation``'s own
  status re-check). A plain ``session.commit()`` before dispatching the
  chord is enough, since a committed SQLite write is immediately visible to any
  connection opened afterward (including a worker picking up the chord).

* **Partial-failure tolerance** — fan-out tasks never raise; they return a
  payload whose ``status`` is ``"error: ..."`` instead (the same per-domain /
  per-site helpers ``agent_parity.pipeline.run_correlation_for_client`` loops over), so one throttled or
  broken vendor API can't stop the chord from firing. The callback records
  per-vendor outcomes on the run (COMPLETE vs PARTIAL) rather than silently
  dropping the whole run. ``link_error`` on the callback is the backstop for
  the callback itself blowing up: the run gets marked FAILED instead of
  hanging in PENDING forever.

* **Rate limits** — each vendor gets its own task so Celery's per-task
  ``rate_limit`` can encode that vendor's real-world API throttling. The
  tasks are built from the connector registry, each connector supplying its
  own ``inventory_rate_limit``, so adding a vendor needs no change here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from celery import chord
from celery.result import AsyncResult
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_parity import os_eol_live
from agent_parity.config import AppConfig, ClientConfig, load_config
from agent_parity.connectors import CONNECTOR_CLASSES
from agent_parity.models import AgentDevice
from agent_parity.pipeline import ad_frame_from_csvs, collect_ad_domain, collect_vendor_site
from agent_parity.scheduling import persistence
from agent_parity.scheduling.celery_app import app
from agent_parity.scheduling.db import CorrelationRun, RunStatus, get_engine, init_db, session_factory
from agent_parity.scheduling.persistence import finalize_run, sync_client_from_config

logger = logging.getLogger(__name__)


@contextmanager
def _session() -> Iterator[Session]:
    """A session on a fresh engine, disposed on exit so each task closes its
    own SQLite connections."""
    engine = get_engine()
    init_db(engine)
    try:
        with session_factory(engine)() as session:
            yield session
    finally:
        engine.dispose()


# --- fan-out: one task per (client, vendor, site/tenant) -----------------------


def _vendor_payload(client_slug: str, vendor_name: str, site_index: int) -> dict:
    """Fetch one (vendor, site/tenant)'s inventory as a JSON-safe envelope.

    ``pipeline.collect_vendor_site`` does the work (and the error handling)
    for both this and ``pipeline.collect_vendor_inventory``. Failures are *returned*, not
    raised — the chord callback must always fire with whatever succeeded.
    """
    key, records, status = collect_vendor_site(load_config(), client_slug, vendor_name, site_index)
    return {
        "source": vendor_name,
        "key": key,
        "status": status,
        "records": [record.to_dict() for record in records] if records is not None else None,
    }


def _register_inventory_task(vendor_name: str, rate_limit: str | None):
    """One inventory-pull task per registered connector, named
    ``fetch_<vendor>_inventory``. A task per vendor (rather than one generic
    task) is what lets Celery apply each vendor's own ``rate_limit``, taken
    from the connector's ``inventory_rate_limit``."""

    def fetch_inventory(client_slug: str, site_index: int) -> dict:
        return _vendor_payload(client_slug, vendor_name, site_index)

    fetch_inventory.__name__ = f"fetch_{vendor_name}_inventory"
    return app.task(name=f"{__name__}.{fetch_inventory.__name__}", rate_limit=rate_limit)(fetch_inventory)


#: vendor name -> its inventory task, built from the connector registry so a
#: new connector needs no change here.
VENDOR_TASKS = {
    vendor: _register_inventory_task(vendor, connector_cls.inventory_rate_limit)
    for vendor, connector_cls in CONNECTOR_CLASSES.items()
}


@app.task(rate_limit="10/m")
def collect_ad_export(client_slug: str, target_device: str) -> dict:
    """The AD export leg of the fan-out (remote script execution is slow).

    One task per (client, domain controller); ``pipeline.collect_ad_domain``
    does the work and error handling, same as for ``pipeline.collect_ad_frame``.
    """
    key, csv_text, status = collect_ad_domain(load_config(), client_slug, target_device)
    return {"source": "ad", "key": key, "status": status, "csv": csv_text}


# --- fan-in: the chord callback ------------------------------------------------


@app.task
def correlate_client(results: list[dict], run_id: int, include_csv: bool = False) -> dict:
    """Correlate one client's complete fan-out results and persist them.

    Runs once per client per run, against everything the group returned —
    correlation never races partial state from another worker. Returns a
    JSON-safe report of the run (what ``agent-parity run`` prints); with
    ``include_csv`` it also carries the classified frame as CSV text, since
    a worker needn't share a filesystem with whoever asked for the run.
    """
    with _session() as session:
        run = session.get_one(CorrelationRun, run_id)
        if run.status != RunStatus.PENDING.value:
            logger.warning("Run %s already finalized; ignoring duplicate callback", run_id)
            return {"run_id": run_id, "status": run.status, "duplicate": True}

        vendor_status: dict[str, str] = {}
        ad_csvs: list[str] = []
        agent_records: list[AgentDevice] = []
        for payload in results:
            vendor_status[payload["key"]] = payload["status"]
            if payload["status"] != "ok":
                continue
            if payload["source"] == "ad":
                ad_csvs.append(payload["csv"])
            else:
                agent_records.extend(AgentDevice.from_dict(r) for r in payload["records"])

        # None when every domain failed; finalize_run then fails the run
        # outright — nothing to reconcile against.
        ad_df = ad_frame_from_csvs(ad_csvs)
        # No live AppConfig in scope here (this callback only receives the
        # fanned-out payloads + run_id) — loaded fresh, same as
        # _vendor_payload/collect_ad_export already do.
        splunk = load_config().splunk
        result = finalize_run(session, run, ad_df, agent_records, vendor_status, splunk=splunk)
        session.commit()
        report: dict = {
            "run_id": run_id,
            "status": run.status,
            "vendor_status": vendor_status,
            "rows": len(result.frame) if result is not None else 0,
            "coverage_pct": result.summary["coverage_pct"] if result is not None else None,
            "status_counts": result.summary["status_counts"] if result is not None else {},
            "ambiguous_join_keys": result.summary["ambiguous_join_keys"] if result is not None else 0,
        }
        if include_csv and result is not None:
            report["csv"] = result.frame.to_csv(index=False)
        return report


@app.task
def mark_run_failed(_request, exc, _traceback, run_id: int) -> None:
    """link_error backstop: never leave a run stuck in PENDING.

    Celery invokes error callbacks with ``(request, exc, traceback)``; only
    the exception is used here, but all three must stay in the signature to
    match what Celery calls.
    """
    with _session() as session:
        run = session.get(CorrelationRun, run_id)
        if run is not None and run.status == RunStatus.PENDING.value:
            run.status = RunStatus.FAILED.value
            run.finished_at = datetime.now(UTC)
            session.commit()
            logger.error("Run %s marked failed after callback error: %s", run_id, exc)


# --- orchestration ---------------------------------------------------------------


def fail_abandoned_runs(config: AppConfig) -> list[int]:
    """Mark runs stuck PENDING past ``config.pending_run_timeout_hours`` as FAILED.

    Called at the start of every beat tick (``dispatch_all_clients``) and every
    ``agent-parity run``; see ``persistence.fail_abandoned_runs``.
    """
    with _session() as session:
        failed = persistence.fail_abandoned_runs(session, timedelta(hours=config.pending_run_timeout_hours))
        session.commit()
    return failed


def start_client_run(
    config: AppConfig, client_cfg: ClientConfig, *, include_csv: bool = False
) -> tuple[int, AsyncResult]:
    """Create the pending run and dispatch the group+chord for one client.

    The one way a client gets collected and persisted, whether beat
    scheduled it (``dispatch_all_clients``) or ``agent-parity run`` asked for
    it. Returns the run id and the chord callback's result, which resolves
    to ``correlate_client``'s report once the fan-in has run.
    """
    with _session() as session:
        client = sync_client_from_config(session, client_cfg)
        run = CorrelationRun(client_id=client.id, stale_days=config.stale_days)
        session.add(run)
        session.commit()  # the run row must exist before the chord's callback can reference it
        run_id = run.id

    vendor_tasks = [
        VENDOR_TASKS[vendor].s(client_cfg.slug, index)
        for vendor in sorted(client_cfg.vendors)
        for index in range(len(client_cfg.vendors[vendor]))
    ]
    header = [
        collect_ad_export.s(client_cfg.slug, target_device) for target_device in client_cfg.ad_target_devices
    ] + vendor_tasks
    callback = correlate_client.s(run_id=run_id, include_csv=include_csv).on_error(mark_run_failed.s(run_id=run_id))
    return run_id, chord(header)(callback)


def dispatch_client(config: AppConfig, client_cfg: ClientConfig) -> int:
    """Fire-and-forget ``start_client_run`` for beat: just the run id."""
    run_id, _ = start_client_run(config, client_cfg)
    return run_id


def _client_is_due(client_cfg: ClientConfig) -> bool:
    with _session() as session:
        latest = session.scalar(
            select(CorrelationRun)
            .where(CorrelationRun.client.has(slug=client_cfg.slug))
            .order_by(CorrelationRun.started_at.desc())
            .limit(1)
        )
    if latest is None:
        return True
    started_at = latest.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return started_at <= datetime.now(UTC) - timedelta(hours=client_cfg.sync_interval_hours)


@app.task
def refresh_os_eol_data() -> str:
    """Beat entrypoint (daily, 06:30): refresh the OS end-of-life data from
    endoflife.date into the shared cache (see ``os_eol_live.refresh_cache``)."""
    if not load_config().refresh_os_eol:
        return "disabled"
    return os_eol_live.refresh_cache().result


@app.task
def dispatch_all_clients(force: bool = False) -> list[str]:
    """Beat entrypoint: kick off the group+chord for every client that is due.

    Beat ticks hourly; each client's own ``sync_interval_hours`` decides
    whether it actually runs this tick.
    """
    config = load_config()
    fail_abandoned_runs(config)
    dispatched = []
    for slug, client_cfg in sorted(config.clients.items()):
        if not force and not _client_is_due(client_cfg):
            continue
        dispatch_client(config, client_cfg)
        dispatched.append(slug)
    logger.info("Dispatched sync for: %s", ", ".join(dispatched) or "no clients due")
    return dispatched
