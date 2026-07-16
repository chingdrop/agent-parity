"""Pipeline-internal data shapes.

These are plain dataclasses, not Django models: they are the normalization
boundary between vendor APIs and the rest of the pipeline. Vendor-specific
field names never leak past a connector — every connector returns
``AgentDevice``, and the AD parser produces rows shaped like ``ADDevice``.
"""

from __future__ import annotations

from dataclasses import dataclass
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


class OSLifecycleStatus(StrEnum):
    """Where an OS sits relative to its vendor-published end-of-life date
    (see ``agent_parity.os_eol``) — an extra, independent prioritization
    signal alongside coverage status and machine_type: an uncovered,
    end-of-life server is a very different priority than a covered,
    actively-supported one.
    """

    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    EOL_SOON = "eol_soon"
    END_OF_LIFE = "end_of_life"


def normalize_hostname(hostname: str | None) -> str:
    """Build the correlation join key from a raw hostname.

    Strips any DNS domain suffix, lowercases, and trims whitespace so that
    ``ACME-WS-014.corp.acme.example`` and ``acme-ws-014`` correlate.
    """
    if not hostname:
        return ""
    return hostname.strip().split(".", 1)[0].lower()


def infer_platform(os_text: str | None) -> str:
    """Best-effort ``platform`` derivation from a free-text OS name, for
    sources with no equivalent to SentinelOne's ``osType`` field (Carbon
    Black/BitDefender's connectors; AD's own export has no such field
    either, so ``correlation`` uses this too, for AD-only rows).

    Wording matches SentinelOne's own lowercase convention (``"windows"``,
    ``"linux"``, ``"macos"``) so a device's platform reads the same
    regardless of which vendor — or AD itself — actually reported it.
    """
    text = (os_text or "").lower()
    if "windows" in text:
        return "windows"
    if "mac" in text or "darwin" in text:
        return "macos"
    if any(name in text for name in ("linux", "ubuntu", "centos", "rhel", "debian")):
        return "linux"
    return ""


def infer_machine_type(os_text: str | None) -> str:
    """Best-effort ``machine_type`` derivation from a free-text OS name, for
    sources with no equivalent to SentinelOne's ``machineType`` field.

    Standing in for asset criticality: a Windows Server SKU is the reliable
    signal, not hostname naming conventions (a file/storage server can be
    named anything; a Windows Server SKU can't lie about being one). Wording
    matches SentinelOne's own convention (``"server"`` / ``"desktop"``).
    """
    return "server" if "server" in (os_text or "").lower() else "desktop"


@dataclass(frozen=True)
class ADDevice:
    """One computer object from the Active Directory export.

    ``os_build`` is the Windows build number parsed out of AD's own
    ``operatingSystemVersion`` attribute (e.g. ``10.0 (22631)`` -> ``22631``)
    — a real, stock AD schema attribute distinct from ``OperatingSystem``,
    giving an exact build rather than a coarse product name. See
    ``agent_parity.os_eol`` for what that buys: unlike a free-text OS name,
    a build number disambiguates *which* Windows 10/11 feature update a
    device is on, which is what its actual end-of-life date depends on.
    """

    hostname: str
    dns_hostname: str = ""
    os: str = ""
    os_build: int | None = None
    last_logon: datetime | None = None
    enabled: bool = True
    distinguished_name: str = ""

    @property
    def join_key(self) -> str:
        return normalize_hostname(self.hostname)


@dataclass(frozen=True)
class AgentDevice:
    """One endpoint record from a security vendor's inventory, normalized.

    ``platform`` and ``machine_type`` are worded to match SentinelOne's own
    API vocabulary (``osType`` values like ``"windows"``; ``machineType``
    values like ``"server"``/``"desktop"``) — most of the historical client
    base was on SentinelOne, so its wording is the one everyone downstream
    (reports, dashboards) is used to reading, and Carbon Black/BitDefender's
    connectors translate their own raw values into it. ``agent_version`` is
    deliberately left alone: each vendor has its own real versioning scheme
    for its own software, so there's no honest way to make one look like
    another's — that would be fabricating a number, not normalizing one.

    ``os_build`` is the Windows build number, when the vendor reports one —
    SentinelOne does (see ``connectors/sentinelone.py``); Carbon Black and
    BitDefender don't expose anything equivalent, so it stays ``None`` for
    them, same as AD-only ``missing_agent`` rows with no build info at all.
    ``agent_parity.os_eol`` uses it for precise end-of-life lookups when
    present, falling back to free-text OS name matching when it's ``None``.
    """

    vendor: str
    agent_id: str
    hostname: str
    os: str = ""
    os_build: int | None = None
    last_seen: datetime | None = None
    agent_version: str = ""
    platform: str = ""
    machine_type: str = ""

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
            "os_build": self.os_build,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "agent_version": self.agent_version,
            "platform": self.platform,
            "machine_type": self.machine_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentDevice:
        last_seen = data.get("last_seen")
        return cls(
            vendor=data["vendor"],
            agent_id=data["agent_id"],
            hostname=data["hostname"],
            os=data.get("os", ""),
            os_build=data.get("os_build"),
            last_seen=datetime.fromisoformat(str(last_seen)) if last_seen else None,
            agent_version=data.get("agent_version", ""),
            platform=data.get("platform", ""),
            machine_type=data.get("machine_type", ""),
        )
