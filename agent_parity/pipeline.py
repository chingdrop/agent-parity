"""Collect from every configured source and correlate for one client.

The one entrypoint every caller of this package should use: given an
``AppConfig`` and a ``ClientConfig``, collect the AD export (across every
domain the client spans) and every enabled vendor's inventory (across every
site/tenant/account it has), then hand the result to
``agent_parity.correlation.engine.correlate``. No persistence, no history —
that's the caller's job (a CLI writing a CSV, or a hub project persisting to
its own store). This module must stay importable without Django or Celery,
same as the rest of ``agent_parity``.
"""

from __future__ import annotations

import logging

import pandas as pd

from agent_parity.ad_sync.parser import concat_ad_frames, parse_ad_export
from agent_parity.config import (
    AppConfig,
    ClientConfig,
    get_connectors,
    get_storage,
    pick_ad_export_vendor,
)
from agent_parity.correlation.engine import CorrelationResult, agents_to_frame, correlate
from agent_parity.deployment.script_runner import run_ad_export
from agent_parity.models import AgentDevice

logger = logging.getLogger(__name__)


def collect_ad_csv(config: AppConfig, client_slug: str, target_device: str) -> str:
    """Run the AD export through one of the client's vendor channels, on one
    domain controller.

    Not every enabled vendor can carry it — only ones whose connector
    genuinely supports remote script execution (see
    ``agent_parity.config.pick_ad_export_vendor``, which also raises clearly
    if a client has none). When object storage is configured
    (``config.storage``), the export is handed off through it instead of the
    vendor's own output channel; unconfigured, nothing changes. Returns the
    raw CSV text.

    Called once per entry in ``client_cfg.ad_target_devices`` — see
    ``collect_ad_frame``, which is what actually loops over a client's
    domains and concatenates the results.
    """
    client_cfg = config.client(client_slug)
    vendor_name = pick_ad_export_vendor(client_cfg)
    # Remote execution runs against an explicit target_device, not a
    # site/tenant-scoped query — any of this vendor's connectors (they
    # differ only by site filter/tenant, not by whether they can reach the
    # target) can carry the script, so the first is as good as any other.
    connector = get_connectors(config, client_slug, vendor_name)[0]
    storage = get_storage(config)
    return run_ad_export(connector, target_device, storage=storage)


def collect_ad_frame(config: AppConfig, client_slug: str) -> tuple[pd.DataFrame | None, dict[str, str]]:
    """Collect, parse, and concatenate the AD export from every one of this
    client's domain controllers into one master DataFrame.

    Tolerant of partial failure the same way vendor-inventory collection
    already is — one domain being unreachable doesn't sink the others; a
    client with only one domain still goes through this same loop, just with
    one iteration. The returned frame is ``None`` only when *every* domain
    failed, meaning there's nothing at all to correlate against.
    """
    client_cfg = config.client(client_slug)
    frames: list[pd.DataFrame] = []
    status: dict[str, str] = {}
    for target_device in client_cfg.ad_target_devices:
        key = f"ad:{target_device}"
        try:
            csv_text = collect_ad_csv(config, client_slug, target_device)
            frames.append(parse_ad_export(csv_text))
            status[key] = "ok"
        except Exception as exc:  # noqa: BLE001 — one domain down must not sink the others
            logger.warning("AD export failed for %s domain %s: %s", client_slug, target_device, exc)
            status[key] = f"error: {exc}"
    if not frames:
        return None, status
    return concat_ad_frames(frames), status


def site_status_key(vendor_name: str, site: dict, index: int, total: int) -> str:
    """The ``vendor_status`` key for one of a vendor's site/tenant entries.

    A real ``label`` (see ``ClientConfig.vendors``) wins; otherwise an index
    only when there's more than one site/tenant to distinguish — the common
    single-site case keeps today's plain vendor-name key unchanged.
    """
    label = site.get("label") or (str(index) if total > 1 else None)
    return f"{vendor_name}:{label}" if label else vendor_name


def collect_vendor_inventory(
        config: AppConfig, client_slug: str, vendor_name: str
) -> tuple[list[AgentDevice], dict[str, str]]:
    """Fetch and concatenate this vendor's inventory across every
    site/tenant the client has (see ``AppConfig.sites_for``) — almost
    always exactly one. Tolerant of partial failure the same way
    ``collect_ad_frame`` already is for AD domains: one site/tenant failing
    doesn't sink the others.
    """
    sites = config.client(client_slug).vendors[vendor_name]
    connectors = get_connectors(config, client_slug, vendor_name)
    records: list[AgentDevice] = []
    status: dict[str, str] = {}
    for index, (site, connector) in enumerate(zip(sites, connectors)):
        key = site_status_key(vendor_name, site, index, len(sites))
        try:
            records.extend(connector.fetch_inventory())
            status[key] = "ok"
        except Exception as exc:  # noqa: BLE001 — one site/tenant down must not sink the others
            logger.warning("%s inventory failed for %s (%s): %s", vendor_name, client_slug, key, exc)
            status[key] = f"error: {exc}"
    return records, status


def run_correlation_for_client(
        config: AppConfig,
        client_cfg: ClientConfig,
        stale_days: int | None = None,
) -> tuple[CorrelationResult | None, dict[str, str]]:
    """Collect (AD + every enabled vendor) and correlate for one client.

    Returns ``(None, vendor_status)`` when every one of the client's AD
    domains failed to export — there's nothing to correlate against, the
    same case a persisting caller would otherwise have to mark a run
    FAILED for; here it's just left to the caller to decide what "no result"
    means for its own reporting/persistence.
    """
    ad_df, vendor_status = collect_ad_frame(config, client_cfg.slug)

    agent_records: list[AgentDevice] = []
    for vendor_name in sorted(client_cfg.vendors):
        records, site_status = collect_vendor_inventory(config, client_cfg.slug, vendor_name)
        agent_records.extend(records)
        vendor_status.update(site_status)

    if ad_df is None:
        return None, vendor_status

    result = correlate(
        ad_df,
        agents_to_frame(agent_records),
        stale_days=stale_days if stale_days is not None else config.stale_days,
    )
    return result, vendor_status
