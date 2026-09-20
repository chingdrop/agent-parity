"""``${VAR}`` secret resolution for YAML-based configs, and the shared
S3-compatible object-storage config shape those configs commonly declare.

Every consumer of this module follows the same convention: a committed
``config.yaml`` holds topology/tuning, with every secret value written as a
``${VAR}`` reference; ``.env`` / the process environment holds the actual
values. A ``${VAR}`` pointing at an *unset* environment variable resolves to
``None`` — deliberately not an error, since "no credentials configured" is a
valid state each consumer uses to fall back to a fixture/offline mode
(agent-parity's connectors, credential-audit's HIBP client). This module is
the one place that resolution rule is implemented; each consumer's own
``config.py`` still owns its own ``AppConfig`` shape and section parsing.

``StorageConfig``/``parse_storage_config``/``get_storage`` are the same
addition for the object-storage handoff both ``agent-parity`` and
``credential-audit`` use (see ``agent_parity.shared.script_export``) — the config
shape and the "build an ``ObjectStorage`` or ``None``" logic were
byte-for-byte identical in both projects' own ``config.py`` before this was
extracted; each consumer's own ``AppConfig`` still embeds ``StorageConfig``
and calls these directly, it just no longer redefines them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_ENV_REF = re.compile(r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}$")


class ConfigError(Exception):
    """Raised for structural problems in a config file (never for unset secrets)."""


def resolve_env_refs(value):
    """Recursively replace ``${VAR}`` strings with their environment value.

    A reference to an unset variable becomes ``None`` — deliberately not an
    error, because "no credentials configured" is a valid fixture/offline
    configuration, not a mistake.
    """
    if isinstance(value, dict):
        return {k: resolve_env_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env_refs(v) for v in value]
    if isinstance(value, str):
        match = _ENV_REF.match(value.strip())
        if match:
            return os.environ.get(match.group("name")) or None
    return value


@dataclass(frozen=True)
class StorageConfig:
    """S3-compatible object storage for a presigned-upload-URL script-export
    handoff (see ``agent_parity.shared.script_export``).

    A live remote-execution connector's own output channel (SentinelOne
    RSO's fetch-files, Carbon Black Live Response's command output) doesn't
    reliably preserve a CSV's exact formatting, so a live export hands the
    script a presigned PUT URL instead and fetches the real output from
    object storage. Unconfigured by default (every field ``None``,
    ``enabled`` False) is only valid when the connector has no live
    credentials either (fixture mode), where no script ever actually
    executes.
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


def parse_storage_config(raw: dict) -> StorageConfig:
    """Parse a config.yaml ``storage:`` section into a :class:`StorageConfig`."""
    section = raw.get("storage") or {}
    return StorageConfig(
        backend=section.get("backend") or "s3",
        endpoint_url=section.get("endpoint_url") or None,
        bucket=section.get("bucket") or None,
        access_key=section.get("access_key") or None,
        secret_key=section.get("secret_key") or None,
        region=section.get("region") or "us-east-1",
    )


def get_storage(storage_config: StorageConfig):
    """Build the object-storage client for a script-export handoff, or ``None``.

    ``None`` means "not configured." That's only a valid state when the
    connector has no live credentials either (fixture mode, where no script
    ever actually runs) — ``agent_parity.shared.script_export.run_script_export``
    raises a clear error if a live connector reaches it with no storage
    configured, rather than falling back to the vendor's own (unreliable)
    output channel.
    """
    if not storage_config.enabled:
        return None
    # `enabled` already confirmed these are set; asserting narrows them from
    # `str | None` to `str` for the type checker (a property check alone
    # doesn't narrow the attributes it read).
    assert storage_config.bucket and storage_config.access_key and storage_config.secret_key  # noqa: S101 - type narrowing

    # Imported here, not at module level, so callers that don't need the
    # storage extra (boto3) aren't forced to have it installed.
    from agent_parity.shared.storage import ObjectStorage

    if storage_config.backend != "s3":
        raise ConfigError(f"Unsupported storage backend {storage_config.backend!r}; only 's3' is implemented")
    return ObjectStorage(
        bucket=storage_config.bucket,
        endpoint_url=storage_config.endpoint_url,
        access_key=storage_config.access_key,
        secret_key=storage_config.secret_key,
        region=storage_config.region,
    )
