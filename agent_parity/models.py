"""Pipeline-internal data shapes.

These are plain dataclasses, not Django models: they are the normalization
boundary between vendor APIs and the rest of the pipeline. Vendor-specific
field names never leak past a connector — every connector returns
``AgentDevice``, and the AD parser produces rows shaped like ``ADDevice``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class CoverageStatus(StrEnum):
    COVERED = "covered"
    MISSING_AGENT = "missing_agent"
    ORPHANED_AGENT = "orphaned_agent"
    STALE_COVERAGE = "stale_coverage"


class Vendor(StrEnum):
    SENTINELONE = "sentinelone"
    CARBONBLACK = "carbonblack"
    BITDEFENDER = "bitdefender"


def normalize_hostname(hostname: str | None) -> str:
    """Build the correlation join key from a raw hostname.

    Strips any DNS domain suffix, lowercases, and trims whitespace so that
    ``ACME-WS-014.corp.acme.example`` and ``acme-ws-014`` correlate.
    """
    if not hostname:
        return ""
    return hostname.strip().split(".", 1)[0].lower()


@dataclass(frozen=True)
class ADDevice:
    """One computer object from the Active Directory export."""

    hostname: str
    dns_hostname: str = ""
    os: str = ""
    last_logon: datetime | None = None
    enabled: bool = True
    distinguished_name: str = ""

    @property
    def join_key(self) -> str:
        return normalize_hostname(self.hostname)


@dataclass(frozen=True)
class AgentDevice:
    """One endpoint record from a security vendor's inventory, normalized."""

    vendor: str
    agent_id: str
    hostname: str
    os: str = ""
    last_seen: datetime | None = None
    agent_version: str = ""

    @property
    def join_key(self) -> str:
        return normalize_hostname(self.hostname)

    def to_dict(self) -> dict:
        """JSON-safe representation, used to pass records through Celery."""
        return {
            "vendor": self.vendor,
            "agent_id": self.agent_id,
            "hostname": self.hostname,
            "os": self.os,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "agent_version": self.agent_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentDevice:
        last_seen = data.get("last_seen")
        return cls(
            vendor=data["vendor"],
            agent_id=data["agent_id"],
            hostname=data["hostname"],
            os=data.get("os", ""),
            last_seen=datetime.fromisoformat(last_seen) if last_seen else None,
            agent_version=data.get("agent_version", ""),
        )
