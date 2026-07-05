"""Collect from the configured vendor + AD domains and correlate.

The one entrypoint every caller of this package should use for the
config.yaml-driven path: given an ``AppConfig``, collect the AD export
(across every domain the organization spans) and the configured vendor's
inventory, then hand the result to
``agent_parity.correlation.engine.correlate``. No persistence, no history —
that's the caller's job (a CLI writing a CSV, or a hub project persisting to
its own store). This module must stay importable without Django or Celery,
same as the rest of ``agent_parity``.
"""

from __future__ import annotations

import logging

import pandas as pd

from agent_parity.ad_sync.parser import concat_ad_frames, parse_ad_export
from agent_parity.agent_csv import parse_agent_csv
from agent_parity.config import AppConfig, ConfigError, get_connector, get_storage
from agent_parity.correlation.engine import CorrelationResult, agents_to_frame, correlate
from agent_parity.deployment.script_runner import run_ad_export
from agent_parity.models import AgentDevice

logger = logging.getLogger(__name__)


def collect_ad_csv(config: AppConfig, target_device: str) -> str:
    """Run the AD export through the configured vendor's connector, on one
    domain controller.

    The configured vendor must genuinely support remote script execution
    (see ``AgentConnector.supports_remote_execution``) — raises clearly if
    not. When object storage is configured (``config.storage``), the export
    is handed off through it instead of the vendor's own output channel;
    unconfigured, nothing changes. Returns the raw CSV text.

    Called once per entry in ``config.ad_target_devices`` — see
    ``collect_ad_frame``, which is what actually loops over every domain and
    concatenates the results.
    """
    connector = get_connector(config)
    if not connector.supports_remote_execution:
        raise ConfigError(
            f"{config.vendor}: does not support remote script execution "
            f"(needed to carry the AD export)"
        )
    storage = get_storage(config)
    return run_ad_export(connector, target_device, storage=storage)


def collect_ad_frame(config: AppConfig) -> tuple[pd.DataFrame | None, dict[str, str]]:
    """Collect, parse, and concatenate the AD export from every one of the
    organization's domain controllers into one master DataFrame.

    Tolerant of partial failure the same way vendor-inventory collection
    already is — one domain being unreachable doesn't sink the others; an
    organization with only one domain still goes through this same loop,
    just with one iteration. The returned frame is ``None`` only when
    *every* domain failed, meaning there's nothing at all to correlate
    against.
    """
    frames: list[pd.DataFrame] = []
    status: dict[str, str] = {}
    for target_device in config.ad_target_devices:
        key = f"ad:{target_device}"
        try:
            csv_text = collect_ad_csv(config, target_device)
            frames.append(parse_ad_export(csv_text))
            status[key] = "ok"
        except Exception as exc:  # noqa: BLE001 — one domain down must not sink the others
            logger.warning("AD export failed for domain %s: %s", target_device, exc)
            status[key] = f"error: {exc}"
    if not frames:
        return None, status
    return concat_ad_frames(frames), status


def collect_vendor_inventory(config: AppConfig) -> tuple[list[AgentDevice], dict[str, str]]:
    """Fetch the configured vendor's inventory. Returns a status dict keyed
    by vendor name, matching the shape ``collect_ad_frame``'s ``ad:<device>``
    keys use, so both merge cleanly into one ``vendor_status`` dict."""
    connector = get_connector(config)
    try:
        return connector.fetch_inventory(), {config.vendor: "ok"}
    except Exception as exc:  # noqa: BLE001 — reported via vendor_status, not raised
        logger.warning("%s inventory failed: %s", config.vendor, exc)
        return [], {config.vendor: f"error: {exc}"}


def run_correlation(
        config: AppConfig,
        stale_days: int | None = None,
) -> tuple[CorrelationResult | None, dict[str, str]]:
    """Collect (AD + the configured vendor) and correlate.

    Returns ``(None, vendor_status)`` when every AD domain failed to
    export — there's nothing to correlate against, the same case a
    persisting caller would otherwise have to mark a run FAILED for; here
    it's just left to the caller to decide what "no result" means for its
    own reporting/persistence.
    """
    ad_df, vendor_status = collect_ad_frame(config)
    agent_records, agent_status = collect_vendor_inventory(config)
    vendor_status.update(agent_status)

    if ad_df is None:
        return None, vendor_status

    result = correlate(
        ad_df,
        agents_to_frame(agent_records),
        stale_days=stale_days if stale_days is not None else config.stale_days,
    )
    return result, vendor_status


def correlate_from_csvs(
        ad_csv_text: str,
        agent_csv_text: str,
        stale_days: int = 14,
) -> CorrelationResult:
    """Correlate two CSVs directly — no config.yaml, no connectors, no
    credentials.

    ``ad_csv_text`` is ``Export-ADDevices.ps1``'s own output (same parser the
    connector-driven path uses); ``agent_csv_text`` is any EDR/agent
    inventory mapped into agent-parity's own column schema (see
    ``agent_parity.agent_csv``). This is the on-ramp for someone without a
    supported vendor connector configured at all — once collection needs to
    be repeatable/scheduled against a live API instead of a one-off export,
    ``run_correlation`` (config.yaml-driven) is the next step up.
    """
    return correlate(parse_ad_export(ad_csv_text), parse_agent_csv(agent_csv_text), stale_days=stale_days)
