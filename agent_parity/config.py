"""Configuration loading: one organization, one vendor, one credential set.

``config.yaml`` holds topology (which vendor, which AD domains) with every
secret value written as a ``${VAR}`` reference; ``.env`` / the process
environment holds the actual values. A ``${VAR}`` pointing at an unset
environment variable resolves to ``None``, which is what puts the connector
into fixture mode — so a fresh checkout with no ``.env`` runs the entire
pipeline against ``sample_data/``. The ``${VAR}`` resolution rule itself lives
in ``shared_tools.config`` (``py-shared-tools``), shared verbatim with
``credential-audit``'s ``config.py`` rather than duplicated; this module only
owns the ``AppConfig`` shape and its own section parsing.

The same file also declares a ``storage:`` section (object storage for the
AD-export handoff — see ``shared_tools.script_export``), resolved the same
way: unset ``${VAR}``s mean unconfigured, and ``get_storage()`` returns
``None`` rather than raising. ``None`` is only a valid state with no live
vendor credentials either (pure fixture/demo mode) —
``deployment.script_runner.run_ad_export`` treats a live connector with no
storage as a configuration error, not a fallback. ``StorageConfig``/
``get_storage`` themselves live in ``shared_tools.config`` too — same
byte-for-byte logic ``credential-audit``'s own AD-metadata export handoff
needs, so it isn't redefined here either.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from shared_tools.config import ConfigError, StorageConfig, parse_storage_config, resolve_env_refs
from shared_tools.config import get_storage as _shared_get_storage

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
SAMPLE_DATA_DIR = REPO_ROOT / "sample_data"


@dataclass(frozen=True)
class AppConfig:
    stale_days: int
    vendor: str
    credentials: dict
    # One domain-joined endpoint per AD domain — an organization spanning
    # multiple domains/forests needs the export script run separately in
    # each (no single domain controller can enumerate computer objects
    # outside its own domain); the resulting CSVs are concatenated into one
    # master AD frame before correlation (see pipeline.collect_ad_frame). A
    # single-domain organization is just the len == 1 case of this same
    # tuple, not a special case.
    ad_target_devices: tuple[str, ...]
    storage: StorageConfig


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml, resolving ``${VAR}`` secret references as we go.

        vendor: sentinelone
        credentials: {api_url: ..., api_token: ...}
        ad_target_devices: [DC01]

    ``vendor:`` is validated against ``CONNECTOR_CLASSES`` (see
    ``connectors/base.py``'s registry) — any registered connector works
    here, not a hardcoded list, so adding vendor support is "write a
    connector class," not "edit this function."
    """
    # Imported here, not at module level, to keep topology-only config
    # loading free of the connector dependency chain (requests) for callers
    # that don't need it.
    from agent_parity.connectors import CONNECTOR_CLASSES

    config_path = Path(path or os.environ.get("AGENT_PARITY_CONFIG") or DEFAULT_CONFIG_PATH)
    with open(config_path) as fh:
        raw: dict = resolve_env_refs(yaml.safe_load(fh))

    vendor_name = raw["vendor"]
    if vendor_name not in CONNECTOR_CLASSES:
        raise ConfigError(
            f"Unknown vendor {vendor_name!r}; registered connectors: {sorted(CONNECTOR_CLASSES)}"
        )

    return AppConfig(
        stale_days=int(raw.get("stale_days", 14)),
        vendor=vendor_name,
        credentials=dict(raw.get("credentials") or {}),
        ad_target_devices=tuple(raw.get("ad_target_devices") or ()),
        storage=parse_storage_config(raw),
    )


def get_connector(config: AppConfig):
    """Build the configured connector, falling back to ``sample_data/``
    fixtures when it has no usable credentials."""
    # Imported here, not at module level, for the same reason as in
    # load_config: keep topology-only config loading free of the connector
    # dependency chain (requests) for callers that don't need it.
    from agent_parity.connectors import CONNECTOR_CLASSES

    try:
        connector_cls = CONNECTOR_CLASSES[config.vendor]
    except KeyError:
        raise ConfigError(f"No connector implemented for vendor {config.vendor!r}") from None
    return connector_cls(credentials=config.credentials, fixture_dir=SAMPLE_DATA_DIR)


def get_storage(config: AppConfig):
    """Build the object-storage client for the AD-export handoff, or None.

    None means "not configured." That's only a valid state for the uv demo
    path (the vendor has no live credentials either, so no script ever
    actually runs); ``deployment.script_runner.run_ad_export`` raises a
    clear error if a live connector reaches it with no storage configured,
    rather than falling back to the vendor's own (unreliable) output
    channel. Delegates to ``shared_tools.config.get_storage`` — same
    byte-for-byte logic ``credential-audit`` needs for its own AD-metadata
    export handoff, so it isn't redefined here.
    """
    return _shared_get_storage(config.storage)
