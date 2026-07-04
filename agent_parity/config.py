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

The same file also declares a ``storage:`` section (object storage for the
AD-export handoff — see ``agent_parity.storage``), resolved the same way:
unset ``${VAR}``s mean unconfigured, and ``get_storage()`` returns ``None``
rather than raising. ``None`` is only a valid state for clients with no live
vendor credentials at all (pure fixture/demo mode) — ``deployment.script_runner
.run_ad_export`` treats a live connector with no storage as a configuration
error, not a fallback.
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
    # Only meaningful for global scope: account name -> credential dict.
    # Always named, even when there's only one (e.g. "default") — there were
    # genuinely two separate SentinelOne consoles in practice (one for MSSP
    # clients, one for DFIR clients under active incident response), and a
    # client's site entry picks which one it's in via an "account" key (see
    # AppConfig.sites_for). Unused for per_client scope (real credentials
    # live on each client's own site entries instead).
    accounts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ClientConfig:
    name: str
    slug: str
    # One domain-joined endpoint per AD domain — a client spanning multiple
    # domains/forests needs the export script run separately in each (no
    # single domain controller can enumerate computer objects outside its
    # own domain); the resulting CSVs are concatenated into one master AD
    # frame before correlation (see dashboard/services.py's
    # collect_ad_frame). A single-domain client is just the len == 1 case
    # of this same tuple, not a special case.
    ad_target_devices: tuple[str, ...]
    sync_interval_hours: int
    # vendor name -> one dict per site/tenant this client has within that
    # vendor's console (almost always a single-element tuple). What the
    # dict holds depends on VENDOR_SCOPE: for a per_client vendor (Carbon
    # Black) each entry is a complete, independent credential block — a
    # second entry means a second, fully separate CB org/tenant. For a
    # global vendor (SentinelOne, BitDefender) each entry is just an
    # optional site filter (e.g. {"site_ids": "..."}), merged onto the
    # shared vendor-level credentials in AppConfig.sites_for — an empty
    # dict (the common case) means "the whole account, no site filter."
    # An optional "label" key names a site/tenant for display and for the
    # DB row it maps to; omitted for the common single-site case.
    vendors: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StorageConfig:
    """S3-compatible object storage for the AD-export handoff.

    Required for any client with live vendor credentials — vendor
    remote-execution output channels don't reliably preserve a CSV's exact
    formatting, so ``deployment.script_runner.run_ad_export`` refuses to run
    a live export without it. Unconfigured by default (every field ``None``,
    ``enabled`` False) is only valid for the uv demo path, where no vendor
    has live credentials either, so no script ever actually executes.
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
    vendors: dict  # name -> VendorConfig
    clients: dict  # slug -> ClientConfig
    storage: StorageConfig

    def client(self, slug: str) -> ClientConfig:
        try:
            return self.clients[slug]
        except KeyError:
            raise ConfigError(f"Unknown client {slug!r} in config.yaml") from None

    def sites_for(self, client_slug: str, vendor_name: str) -> tuple[dict, ...]:
        """Return one merged config dict per site/tenant for a (client, vendor) pair.

        ``global`` scope resolves which of the vendor's named accounts each
        site uses (an explicit ``"account"`` key, or the vendor's sole
        account when it only has one — ambiguous otherwise, see
        ``_resolve_account``) and merges that account's credentials with the
        site's own filter (e.g. SentinelOne's ``site_ids``) on top — every
        site under the same account shares that account's secret, just
        scoped to a different slice of it. ``per_client`` scope returns each
        of the client's tenant blocks as-is: unlike global scope these are
        already complete, independent credential sets (e.g. separate Carbon
        Black orgs), so there's nothing to merge them with.
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
            return tuple(
                {**self._resolve_account(client_slug, vendor, site), **site} for site in sites
            )
        return tuple(dict(site) for site in sites)

    def _resolve_account(self, client_slug: str, vendor: VendorConfig, site: dict) -> dict:
        """The credential dict for one global-scope site's chosen account."""
        if not vendor.accounts:
            return {}  # nothing configured yet -> fixture mode, same as today
        account_name = site.get("account")
        if account_name is None:
            if len(vendor.accounts) == 1:
                return next(iter(vendor.accounts.values()))
            raise ConfigError(
                f"Client {client_slug!r} must specify which {vendor.name!r} account "
                f"to use (multiple configured: {sorted(vendor.accounts)})"
            )
        try:
            return vendor.accounts[account_name]
        except KeyError:
            raise ConfigError(
                f"Client {client_slug!r} references unknown {vendor.name!r} "
                f"account {account_name!r}; configured: {sorted(vendor.accounts)}"
            ) from None


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml, resolving ``${VAR}`` secret references as we go."""
    config_path = Path(path or os.environ.get("AGENT_PARITY_CONFIG") or DEFAULT_CONFIG_PATH)
    with open(config_path) as fh:
        raw: dict = _resolve_env_refs(yaml.safe_load(fh))

    vendors = {}
    for name, block in (raw.get("vendors") or {}).items():
        block = block or {}
        scope = block.get("scope", "global")
        if scope not in ("global", "per_client"):
            raise ConfigError(f"Vendor {name!r} has invalid scope {scope!r}")
        # Only meaningful for global scope — named accounts (always named,
        # even a lone "default" one; see VendorConfig.accounts). per_client
        # vendors declare no accounts at all; real credentials live on each
        # client's own site entries instead.
        accounts = {
            account_name: dict(account_block or {})
            for account_name, account_block in (block.get("accounts") or {}).items()
        }
        vendors[name] = VendorConfig(name=name, scope=scope, accounts=accounts)

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
            sync_interval_hours=int(entry.get("sync_interval_hours", 24)),
            vendors=client_vendors,
        )
        for vendor_name in client.vendors:
            if vendor_name not in vendors:
                raise ConfigError(
                    f"Client {client.slug!r} references undeclared vendor {vendor_name!r}"
                )
        clients[client.slug] = client

    storage_raw = raw.get("storage") or {}
    storage = StorageConfig(
        backend=storage_raw.get("backend") or "s3",
        endpoint_url=storage_raw.get("endpoint_url") or None,
        bucket=storage_raw.get("bucket") or None,
        access_key=storage_raw.get("access_key") or None,
        secret_key=storage_raw.get("secret_key") or None,
        region=storage_raw.get("region") or "us-east-1",
    )

    return AppConfig(
        stale_days=int(raw.get("stale_days", 14)),
        vendors=vendors,
        clients=clients,
        storage=storage,
    )


#: Preference order for the vendor that carries a client's AD export, among
#: whichever of its enabled vendors genuinely support remote script execution
#: (see ``AgentConnector.supports_remote_execution``). This isn't just
#: alphabetical: it reflects real deployment prevalence — SentinelOne covered
#: the bulk of the client base, Carbon Black a handful of clients, and
#: BitDefender (never eligible here) exactly one. A vendor not in this tuple
#: still sorts after these two, alphabetically, so adding a 4th capable
#: vendor doesn't require touching this list.
AD_EXPORT_VENDOR_PREFERENCE = ("sentinelone", "carbonblack")

#: Whether each vendor's credentials are shared across every client
#: ("global" — one API token for the whole organization, e.g. SentinelOne's
#: management API) or distinct per client ("per_client", e.g. Carbon Black
#: Cloud, where each environment has its own API ID/secret/org key). This is
#: a fixed fact about how each vendor's API is provisioned, not something a
#: config.yaml entry or a DB-backed setup form should be able to override —
#: ``VendorConfig.scope`` (parsed from config.yaml) and
#: ``dashboard.config_db`` (parsed from the DB) both agree with this table;
#: it's the single place either one is checked against.
VENDOR_SCOPE = {
    "sentinelone": "global",
    "carbonblack": "per_client",
    "bitdefender": "global",
}


def pick_ad_export_vendor(client_cfg: ClientConfig) -> str:
    """Pick the vendor to carry ``client_cfg``'s AD export.

    Only vendors whose connector sets ``supports_remote_execution = True``
    are eligible — not every EDR vendor's API can push and run an arbitrary
    script, and picking one that can't would silently misrepresent it.
    """
    # Imported here (not at module level) for the same reason as in
    # get_connector: keep topology-only config loading free of the connector
    # dependency chain (requests) for callers that don't need it.
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
        try:
            return AD_EXPORT_VENDOR_PREFERENCE.index(vendor_name), vendor_name
        except ValueError:
            return len(AD_EXPORT_VENDOR_PREFERENCE), vendor_name

    return min(capable, key=preference_key)


def get_connectors(config: AppConfig, client_slug: str, vendor_name: str) -> tuple:
    """Build one configured connector per site/tenant for a (client, vendor) pair.

    This is the single place that knows how credentials map onto connectors,
    and it is what both the management command and the Celery fan-out tasks
    call. Almost always a one-element tuple; more than one for a client with
    multiple sites (global scope) or tenants (per_client scope) — see
    ``AppConfig.sites_for``. Connectors with no usable credentials fall back
    to the client's fixtures under ``sample_data/<client_slug>/``.
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
    path (no vendor has live credentials, so no script ever actually runs);
    ``deployment.script_runner.run_ad_export`` raises a clear error if a live
    connector reaches it with no storage configured, rather than falling
    back to the vendor's own (unreliable) output channel.
    """
    if not config.storage.enabled:
        return None

    # Imported here, not at module level, for the same reason as in
    # get_connector: keep topology-only config loading free of the boto3
    # dependency chain for callers that don't need it.
    from agent_parity.storage import ObjectStorage

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
