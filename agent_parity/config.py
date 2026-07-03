"""Configuration loading and the (client, vendor) -> connector resolver.

The credential model is split in two on purpose:

* ``config.yaml`` holds *topology*: which vendors exist, whether their
  credentials are ``global`` (one credential set for the whole organization,
  e.g. SentinelOne) or ``per_client`` (a distinct credential set per client,
  e.g. Carbon Black), and which vendors each client uses. Secret values in
  the file are never literal — they are ``${VAR}`` references.
* ``.env`` / the process environment holds the actual secret values.

``${VAR}`` references that point at an unset environment variable resolve to
``None``, which is what puts a connector into fixture mode — so a fresh
checkout with no ``.env`` runs the entire pipeline against ``sample_data/``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
SAMPLE_DATA_DIR = REPO_ROOT / "sample_data"

_ENV_REF = re.compile(r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}$")


class ConfigError(Exception):
    """Raised for structural problems in config.yaml (never for unset secrets)."""


def _resolve_env_refs(value):
    """Recursively replace ``${VAR}`` strings with their environment value.

    A reference to an unset variable becomes ``None`` — deliberately not an
    error, because "no credentials" is the valid fixture-mode configuration.
    """
    if isinstance(value, dict):
        return {k: _resolve_env_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_refs(v) for v in value]
    if isinstance(value, str):
        match = _ENV_REF.match(value.strip())
        if match:
            return os.environ.get(match.group("name")) or None
    return value


@dataclass(frozen=True)
class VendorConfig:
    name: str
    scope: str  # "global" or "per_client"
    credentials: dict = field(default_factory=dict)  # only for global scope


@dataclass(frozen=True)
class ClientConfig:
    name: str
    slug: str
    ad_target_device: str
    sync_interval_hours: int
    # vendor name -> that client's credential block (empty dict for
    # global-scoped vendors, which carry credentials at the vendor level).
    vendors: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SplunkConfig:
    hec_url: str | None = None
    hec_token: str | None = None
    index: str = "security_coverage"
    sourcetype: str = "agent_parity:coverage_delta"

    @property
    def enabled(self) -> bool:
        return bool(self.hec_url and self.hec_token)


@dataclass(frozen=True)
class AppConfig:
    stale_days: int
    vendors: dict  # name -> VendorConfig
    clients: dict  # slug -> ClientConfig
    splunk: SplunkConfig

    def client(self, slug: str) -> ClientConfig:
        try:
            return self.clients[slug]
        except KeyError:
            raise ConfigError(f"Unknown client {slug!r} in config.yaml") from None

    def credentials_for(self, client_slug: str, vendor_name: str) -> dict:
        """Return the credential dict for a (client, vendor) pair.

        ``global`` scope ignores the client and returns the vendor-level
        block; ``per_client`` scope requires the client to declare its own.
        """
        try:
            vendor = self.vendors[vendor_name]
        except KeyError:
            raise ConfigError(f"Unknown vendor {vendor_name!r} in config.yaml") from None

        client = self.client(client_slug)
        if vendor_name not in client.vendors:
            raise ConfigError(
                f"Client {client_slug!r} does not enable vendor {vendor_name!r}"
            )
        if vendor.scope == "global":
            return dict(vendor.credentials)
        return dict(client.vendors[vendor_name])


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml, resolving ``${VAR}`` secret references as we go."""
    config_path = Path(path or os.environ.get("AGENT_PARITY_CONFIG") or DEFAULT_CONFIG_PATH)
    with open(config_path) as fh:
        raw = _resolve_env_refs(yaml.safe_load(fh))

    vendors = {}
    for name, block in (raw.get("vendors") or {}).items():
        block = block or {}
        scope = block.get("scope", "global")
        if scope not in ("global", "per_client"):
            raise ConfigError(f"Vendor {name!r} has invalid scope {scope!r}")
        credentials = {k: v for k, v in block.items() if k != "scope"}
        vendors[name] = VendorConfig(name=name, scope=scope, credentials=credentials)

    clients = {}
    for entry in raw.get("clients") or []:
        client = ClientConfig(
            name=entry["name"],
            slug=entry["slug"],
            ad_target_device=entry.get("ad_target_device", ""),
            sync_interval_hours=int(entry.get("sync_interval_hours", 24)),
            vendors={v: (creds or {}) for v, creds in (entry.get("vendors") or {}).items()},
        )
        for vendor_name in client.vendors:
            if vendor_name not in vendors:
                raise ConfigError(
                    f"Client {client.slug!r} references undeclared vendor {vendor_name!r}"
                )
        clients[client.slug] = client

    splunk_raw = raw.get("splunk") or {}
    splunk = SplunkConfig(
        hec_url=splunk_raw.get("hec_url"),
        hec_token=splunk_raw.get("hec_token"),
        index=splunk_raw.get("index") or "security_coverage",
        sourcetype=splunk_raw.get("sourcetype") or "agent_parity:coverage_delta",
    )

    return AppConfig(
        stale_days=int(raw.get("stale_days", 14)),
        vendors=vendors,
        clients=clients,
        splunk=splunk,
    )


def get_connector(config: AppConfig, client_slug: str, vendor_name: str):
    """Build a configured connector for a (client, vendor) pair.

    This is the single place that knows how credentials map onto connectors,
    and it is what both the management command and the Celery fan-out tasks
    call. Connectors with no usable credentials fall back to the client's
    fixtures under ``sample_data/<client_slug>/``.
    """
    # Imported here to keep config loading importable without the connector
    # dependency chain (requests) in contexts that only need topology.
    from agent_parity.connectors import CONNECTOR_CLASSES

    try:
        connector_cls = CONNECTOR_CLASSES[vendor_name]
    except KeyError:
        raise ConfigError(f"No connector implemented for vendor {vendor_name!r}") from None

    credentials = config.credentials_for(client_slug, vendor_name)
    fixture_dir = SAMPLE_DATA_DIR / client_slug
    return connector_cls(credentials=credentials, fixture_dir=fixture_dir)
