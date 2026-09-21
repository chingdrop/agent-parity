"""``${VAR}`` secret resolution for YAML-based configs.

A committed ``config.yaml`` holds topology/tuning, with every secret value
written as a ``${VAR}`` reference; ``.env`` / the process environment holds
the actual values. A ``${VAR}`` pointing at an *unset* environment variable
resolves to ``None`` — deliberately not an error, since "no credentials
configured" is a valid state used to fall back to a fixture/offline mode
(agent-parity's connectors). This module is the one place that resolution
rule is implemented; ``agent_parity.config`` owns its own ``AppConfig``
shape and section parsing.
"""

from __future__ import annotations

import os
import re

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
