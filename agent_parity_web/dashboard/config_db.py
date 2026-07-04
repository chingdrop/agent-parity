"""Build/populate an ``agent_parity.config.AppConfig`` from the DB instead of
``config.yaml`` — the DB-backed half of the config-resolution boundary.

``agent_parity/config.py``'s dataclasses (``AppConfig``, ``ClientConfig``,
``VendorConfig``, ``StorageConfig``) stay the single contract the pipeline
consumes: ``credentials_for()``, ``get_connector()``, ``get_storage()``, and
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


def import_app_config(config: AppConfig) -> None:
    """Upsert Client/VendorCredential rows from an already-loaded AppConfig."""
    for vendor_name, vendor_cfg in config.vendors.items():
        if vendor_cfg.scope == "global":
            VendorCredential.objects.update_or_create(
                client=None,
                vendor=vendor_name,
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
        for vendor_name, creds in client_cfg.vendors.items():
            if VENDOR_SCOPE.get(vendor_name) == "per_client":
                VendorCredential.objects.update_or_create(
                    client=client, vendor=vendor_name, defaults={"credentials": creds}
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
        per_client_creds = {row.vendor: row.credentials for row in client.vendor_credentials.all()}
        vendors = {}
        for vendor_name in client.enabled_vendors:
            if VENDOR_SCOPE.get(vendor_name) == "per_client":
                vendors[vendor_name] = per_client_creds.get(vendor_name, {})
            else:
                vendors[vendor_name] = {}
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
