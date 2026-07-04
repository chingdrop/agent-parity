"""Build/populate an ``agent_parity.config.AppConfig`` from the DB instead of
``config.yaml`` — the DB-backed half of the config-resolution boundary.

``agent_parity/config.py``'s dataclasses (``AppConfig``, ``ClientConfig``,
``VendorConfig``, ``StorageConfig``) stay the single contract the pipeline
consumes: ``sites_for()``, ``get_connectors()``, ``get_storage()``, and
``pick_ad_export_vendor()`` are all reused completely unchanged by every
production entrypoint. This module is the only place that knows those
dataclasses can also be built from the DB rather than parsed from YAML —
``agent_parity/`` itself never learns the DB exists, preserving the
Django-free boundary documented in CLAUDE.md.

Two symmetric functions:

* ``import_app_config(config)`` — one-time write: DB rows from an
  already-loaded ``AppConfig`` (used by ``manage.py import_config`` and,
  later, the setup page's YAML upload). Idempotent (``update_or_create``),
  safe to run more than once — re-importing an unchanged config.yaml is a
  no-op.
* ``build_app_config_from_db()`` — the inverse, read at every pipeline-run
  entrypoint now that the DB, not config.yaml, is authoritative.

``stale_days`` and ``storage`` are explicitly out of scope for this feature
(see CLAUDE.md) — both stay sourced from Django settings / the process
environment, never from DB rows, exactly as they are today.
"""

from __future__ import annotations

import os

from django.conf import settings

from agent_parity.config import (
    VENDOR_SCOPE,
    AppConfig,
    ClientConfig,
    StorageConfig,
    VendorConfig,
)
from dashboard.models import Client, VendorCredential


def _site_label(site: dict, index: int, total: int) -> str:
    """The DB row's site_label for one of a vendor's site/tenant entries —
    purely a storage-identity key so multiple rows for the same (client,
    vendor) don't collide; an explicit ``label`` key in the site dict wins,
    otherwise an index only when there's more than one. This value is never
    reintroduced as a ``label`` on read — ``credentials`` (stored verbatim,
    "label" key included or not exactly as configured) is the only thing
    that determines whether a site is "labeled" downstream (fixture
    filenames, vendor_status keys) — see ``build_app_config_from_db``.
    """
    if "label" in site:
        return str(site["label"])
    return str(index) if total > 1 else ""


def import_app_config(config: AppConfig) -> None:
    """Upsert Client/VendorCredential rows from an already-loaded AppConfig."""
    for vendor_name, vendor_cfg in config.vendors.items():
        if vendor_cfg.scope == "global":
            VendorCredential.objects.update_or_create(
                client=None,
                vendor=vendor_name,
                site_label="",
                defaults={"credentials": vendor_cfg.credentials},
            )

    for slug, client_cfg in config.clients.items():
        client, _ = Client.objects.update_or_create(
            slug=slug,
            defaults={
                "name": client_cfg.name,
                "enabled_vendors": sorted(client_cfg.vendors),
                "ad_target_devices": list(client_cfg.ad_target_devices),
                "sync_interval_hours": client_cfg.sync_interval_hours,
            },
        )
        for vendor_name, sites in client_cfg.vendors.items():
            scope = VENDOR_SCOPE.get(vendor_name)
            for index, site in enumerate(sites):
                # Per-client scope: every site entry is a real, distinct
                # tenant credential, always worth a row. Global scope: only
                # worth a row when there's an actual site filter to
                # remember — a lone empty dict means "the whole account,"
                # already covered by the shared global row above.
                if scope != "per_client" and not site and len(sites) == 1:
                    continue
                # Stored verbatim -- including "label" if the site dict has
                # one, excluding it otherwise -- so an unlabeled site never
                # gains one through the DB round-trip (see build_app_config_from_db).
                VendorCredential.objects.update_or_create(
                    client=client,
                    vendor=vendor_name,
                    site_label=_site_label(site, index, len(sites)),
                    defaults={"credentials": dict(site)},
                )


def build_app_config_from_db() -> AppConfig:
    """Read Client/VendorCredential rows into an AppConfig.

    Every production entrypoint (management commands, Celery tasks) calls
    this instead of ``load_config()``.
    """
    vendor_configs: dict[str, VendorConfig] = {
        row.vendor: VendorConfig(name=row.vendor, scope="global", credentials=row.credentials)
        for row in VendorCredential.objects.filter(client__isnull=True)
    }
    # A vendor with no global credential row yet (fresh install, nothing
    # imported/added through the setup page) still needs an entry so any
    # client enabling it resolves — credentials_for() only rejects vendors
    # it's never heard of at all, not ones with empty credentials.
    for vendor_name, scope in VENDOR_SCOPE.items():
        vendor_configs.setdefault(
            vendor_name, VendorConfig(name=vendor_name, scope=scope, credentials={})
        )

    clients: dict[str, ClientConfig] = {}
    for client in Client.objects.all():
        # Group this client's own VendorCredential rows by vendor. For a
        # per_client vendor these are real, distinct tenant credentials; for
        # a global vendor they're just site filters (see the model
        # docstring) merged onto the shared secret in sites_for(). No rows
        # at all -- for either scope -- means one default site with an
        # empty dict, exactly today's single-site/single-tenant behavior.
        rows_by_vendor: dict[str, list] = {}
        for row in client.vendor_credentials.order_by("site_label", "pk"):
            rows_by_vendor.setdefault(row.vendor, []).append(row)

        vendors = {}
        for vendor_name in client.enabled_vendors:
            rows = rows_by_vendor.get(vendor_name)
            if not rows:
                vendors[vendor_name] = ({},)
            else:
                # credentials was stored verbatim (see import_app_config) —
                # a "label" key survives here only if the site was actually
                # configured with one; site_label itself (which may be an
                # auto-assigned index) never leaks into this shape.
                vendors[vendor_name] = tuple(dict(row.credentials) for row in rows)
        clients[client.slug] = ClientConfig(
            name=client.name,
            slug=client.slug,
            ad_target_devices=tuple(client.ad_target_devices),
            sync_interval_hours=client.sync_interval_hours,
            vendors=vendors,
        )

    return AppConfig(
        stale_days=settings.STALE_DAYS,
        vendors=vendor_configs,
        clients=clients,
        storage=_storage_config_from_env(),
    )


def _storage_config_from_env() -> StorageConfig:
    """Mirrors config.yaml's ``storage:`` block, but reads the process
    environment directly — the same way ``REDIS_URL``/``DATABASE_URL``
    already work in ``config/settings/base.py``. Storage is out of scope for
    this feature (see CLAUDE.md), so it stays env-driven either way; this
    just removes the YAML indirection now that config.yaml is a one-time
    import source rather than something read at run time.
    """
    return StorageConfig(
        backend=os.environ.get("STORAGE_BACKEND") or "s3",
        endpoint_url=os.environ.get("STORAGE_ENDPOINT_URL") or None,
        bucket=os.environ.get("STORAGE_BUCKET") or None,
        access_key=os.environ.get("STORAGE_ACCESS_KEY") or None,
        secret_key=os.environ.get("STORAGE_SECRET_KEY") or None,
        region=os.environ.get("STORAGE_REGION") or "us-east-1",
    )
