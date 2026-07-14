"""Configuration loading: client topology and the (client, vendor) ->
connector resolver.

``config.yaml`` holds topology: which vendors exist, whether their
credentials are ``global`` (one credential set for the whole organization,
e.g. SentinelOne) or ``per_client`` (a distinct credential set per client,
e.g. Carbon Black), and which vendors each client uses. Secret values in the
file are never literal — they are ``${VAR}`` references resolved from the
environment at load time. The ``${VAR}`` resolution rule itself lives in
``shared_tools.config`` (``py-shared-tools``), shared with other projects
that follow the same convention; this module only owns the ``AppConfig``
shape and its own section parsing.

The same file also declares a ``storage:`` section (object storage for the
AD-export handoff — see ``shared_tools.script_export``), resolved the same
way: unset ``${VAR}``s mean unconfigured, and ``get_storage()`` returns
``None`` rather than raising. ``None`` is only a valid state for clients
with no live vendor credentials at all (pure fixture/demo mode) —
``deployment.script_runner.run_ad_export`` treats a live connector with no
storage as a configuration error, not a fallback. ``StorageConfig``/
``get_storage`` themselves live in ``shared_tools.config`` too, so they
aren't redefined here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from shared_tools.config import ConfigError, StorageConfig, parse_storage_config, resolve_env_refs
from shared_tools.config import get_storage as _shared_get_storage

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
SAMPLE_DATA_DIR = REPO_ROOT / "sample_data"


@dataclass(frozen=True)
class VendorConfig:
    name: str
    scope: str  # "global" or "per_client"
    # Only meaningful for global scope — the one shared credential set every
    # client resolving to this vendor uses. per_client vendors declare no
    # credentials here at all; real credentials live on each client's own
    # vendor entry instead.
    credentials: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ClientConfig:
    name: str
    slug: str
    # One domain-joined endpoint per AD domain — a client spanning multiple
    # domains/forests needs the export script run separately in each (no
    # single domain controller can enumerate computer objects outside its
    # own domain); the resulting CSVs are concatenated into one master AD
    # frame before correlation (see pipeline.collect_ad_frame). A
    # single-domain client is just the len == 1 case of this same tuple,
    # not a special case.
    ad_target_devices: tuple[str, ...]
    # vendor name -> one dict per site/tenant this client has within that
    # vendor's console (almost always a single-element tuple). For a
    # per_client vendor (Carbon Black) each entry is a complete, independent
    # credential block — a second entry means a second, fully separate CB
    # org/tenant. For a global vendor (SentinelOne, BitDefender) each entry
    # is just an optional site filter (e.g. {"site_ids": "..."}), merged
    # onto the shared vendor-level credentials in AppConfig.sites_for — an
    # empty dict (the common case) means "the whole account, no site
    # filter." An optional "label" key names a site/tenant for display and
    # for the fixture file it maps to (see connectors/base.py); omitted for
    # the common single-site case.
    vendors: dict[str, tuple[dict, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    stale_days: int
    vendors: dict[str, VendorConfig]
    clients: dict[str, ClientConfig]
    storage: StorageConfig

    def client(self, slug: str) -> ClientConfig:
        try:
            return self.clients[slug]
        except KeyError:
            raise ConfigError(f"Unknown client {slug!r} in config.yaml") from None

    def sites_for(self, client_slug: str, vendor_name: str) -> tuple[dict, ...]:
        """One merged credential dict per site/tenant for a (client, vendor)
        pair — almost always a one-element tuple, more for a client with
        multiple sites (global scope) or tenants (per_client scope).
        ``global`` scope merges each of the client's site filters (if any)
        on top of the shared vendor-level credentials — every site shares
        the same secret, just scoped to a different slice of the account
        (e.g. SentinelOne's Sites). ``per_client`` scope returns each of the
        client's tenant blocks as-is: unlike global scope these are already
        complete, independent credential sets (e.g. separate Carbon Black
        orgs), so there's nothing to merge them with.
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
        sites = client.vendors[vendor_name]
        if vendor.scope == "global":
            return tuple({**vendor.credentials, **site} for site in sites)
        return tuple(dict(site) for site in sites)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml, resolving ``${VAR}`` secret references as we go."""
    config_path = Path(path or os.environ.get("AGENT_PARITY_CONFIG") or DEFAULT_CONFIG_PATH)
    with open(config_path) as fh:
        raw: dict = resolve_env_refs(yaml.safe_load(fh))

    vendors = {}
    for name, block in (raw.get("vendors") or {}).items():
        block = block or {}
        scope = block.get("scope", "global")
        if scope not in ("global", "per_client"):
            raise ConfigError(f"Vendor {name!r} has invalid scope {scope!r}")
        vendors[name] = VendorConfig(
            name=name, scope=scope, credentials=dict(block.get("credentials") or {})
        )

    clients = {}
    for entry in raw.get("clients") or []:
        # Each vendor's value is a list of site/tenant dicts — one element
        # for the common single-site case, more for a client spanning
        # multiple sites (global scope) or tenants (per_client scope). An
        # empty/missing list still means "enabled, one default site."
        client_vendors = {
            v: tuple((site or {}) for site in (sites or [{}]))
            for v, sites in (entry.get("vendors") or {}).items()
        }
        client = ClientConfig(
            name=entry["name"],
            slug=entry["slug"],
            ad_target_devices=tuple(entry.get("ad_target_devices") or ()),
            vendors=client_vendors,
        )
        for vendor_name in client.vendors:
            if vendor_name not in vendors:
                raise ConfigError(
                    f"Client {client.slug!r} references undeclared vendor {vendor_name!r}"
                )
        clients[client.slug] = client

    return AppConfig(
        stale_days=int(raw.get("stale_days", 14)),
        vendors=vendors,
        clients=clients,
        storage=parse_storage_config(raw),
    )


def pick_ad_export_vendor(client_cfg: ClientConfig) -> str:
    """Pick the vendor to carry ``client_cfg``'s AD export.

    Only vendors whose connector sets ``supports_remote_execution = True``
    are eligible — not every EDR vendor's API can push and run an arbitrary
    script, and picking one that can't would silently misrepresent it. Ties
    break by each connector's own ``ad_export_priority`` class attribute
    (see ``connectors/base.py``), then alphabetically — not just a
    technical preference: SentinelOne's default priority reflects that it
    covered the bulk of the original client base, Carbon Black a handful.
    """
    # Imported here (not at module level) for the same reason as in
    # get_connectors: keep topology-only config loading free of the
    # connector dependency chain (requests) for callers that don't need it.
    from agent_parity.connectors import CONNECTOR_CLASSES

    capable = [
        vendor_name
        for vendor_name in client_cfg.vendors
        if CONNECTOR_CLASSES[vendor_name].supports_remote_execution
    ]
    if not capable:
        raise ConfigError(
            f"Client {client_cfg.slug!r} has no vendor capable of remote script "
            f"execution (needed to carry the AD export); enabled vendors: "
            f"{sorted(client_cfg.vendors)}"
        )

    def preference_key(vendor_name: str) -> tuple[int, str]:
        return CONNECTOR_CLASSES[vendor_name].ad_export_priority, vendor_name

    return min(capable, key=preference_key)


def get_connectors(config: AppConfig, client_slug: str, vendor_name: str) -> tuple:
    """Build one configured connector per site/tenant for a (client, vendor)
    pair — almost always a one-element tuple. Connectors with no usable
    credentials fall back to the client's fixtures under
    ``sample_data/<client_slug>/``.
    """
    # Imported here to keep config loading importable without the connector
    # dependency chain (requests) in contexts that only need topology.
    from agent_parity.connectors import CONNECTOR_CLASSES

    try:
        connector_cls = CONNECTOR_CLASSES[vendor_name]
    except KeyError:
        raise ConfigError(f"No connector implemented for vendor {vendor_name!r}") from None

    fixture_dir = SAMPLE_DATA_DIR / client_slug
    return tuple(
        connector_cls(credentials=credentials, fixture_dir=fixture_dir)
        for credentials in config.sites_for(client_slug, vendor_name)
    )


def get_storage(config: AppConfig):
    """Build the object-storage client for the AD-export handoff, or None.

    None means "not configured." That's only a valid state for the uv demo
    path (no vendor has live credentials, so no script ever actually
    runs); ``deployment.script_runner.run_ad_export`` raises a clear error
    if a live connector reaches it with no storage configured, rather than
    falling back to the vendor's own (unreliable) output channel. Delegates
    to ``shared_tools.config.get_storage`` so the logic isn't redefined here.
    """
    return _shared_get_storage(config.storage)
