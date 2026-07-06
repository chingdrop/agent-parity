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
AD-export handoff — see ``shared_tools.storage``), resolved the same way:
unset ``${VAR}``s mean unconfigured, and ``get_storage()`` returns ``None``
rather than raising. ``None`` is only a valid state with no live vendor
credentials either (pure fixture/demo mode) — ``deployment.script_runner
.run_ad_export`` treats a live connector with no storage as a configuration
error, not a fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from shared_tools.config import ConfigError, resolve_env_refs

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
SAMPLE_DATA_DIR = REPO_ROOT / "sample_data"


@dataclass(frozen=True)
class StorageConfig:
    """S3-compatible object storage for the AD-export handoff.

    Required for any live vendor connector — vendor remote-execution output
    channels don't reliably preserve a CSV's exact formatting, so
    ``deployment.script_runner.run_ad_export`` refuses to run a live export
    without it. Unconfigured by default (every field ``None``, ``enabled``
    False) is only valid for the uv demo path, where the vendor has no live
    credentials either, so no script ever actually executes.
    """

    backend: str = "s3"
    endpoint_url: str | None = None  # unset -> real AWS S3; set for MinIO/other S3-compatible services
    bucket: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    region: str = "us-east-1"

    @property
    def enabled(self) -> bool:
        return bool(self.bucket and self.access_key and self.secret_key)


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


def _parse_storage(raw: dict) -> StorageConfig:
    storage_raw = raw.get("storage") or {}
    return StorageConfig(
        backend=storage_raw.get("backend") or "s3",
        endpoint_url=storage_raw.get("endpoint_url") or None,
        bucket=storage_raw.get("bucket") or None,
        access_key=storage_raw.get("access_key") or None,
        secret_key=storage_raw.get("secret_key") or None,
        region=storage_raw.get("region") or "us-east-1",
    )


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
        storage=_parse_storage(raw),
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
    channel.
    """
    if not config.storage.enabled:
        return None

    # Imported here, not at module level, for the same reason as in
    # get_connector: keep topology-only config loading free of the boto3
    # dependency chain for callers that don't need it.
    from shared_tools.storage import ObjectStorage

    if config.storage.backend != "s3":
        raise ConfigError(
            f"Unsupported storage backend {config.storage.backend!r}; only 's3' is implemented"
        )
    return ObjectStorage(
        bucket=config.storage.bucket,
        endpoint_url=config.storage.endpoint_url,
        access_key=config.storage.access_key,
        secret_key=config.storage.secret_key,
        region=config.storage.region,
    )
