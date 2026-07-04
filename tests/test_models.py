"""Tests for agent_parity/models.py: the plain-dataclass normalization
boundary (ADDevice, AgentDevice, normalize_hostname) — not exercised
directly by any other test file, only indirectly through connectors/
correlation.
"""

from datetime import datetime, timezone

import pytest

from agent_parity.models import ADDevice, AgentDevice, CoverageStatus, Vendor, normalize_hostname


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ACME-WS-014.corp.acme.example", "acme-ws-014"),
        ("acme-ws-014", "acme-ws-014"),
        ("  ACME-WS-014  ", "acme-ws-014"),
        ("ACME-WS-014.CORP.ACME.EXAMPLE", "acme-ws-014"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_hostname(raw, expected):
    assert normalize_hostname(raw) == expected


def test_ad_device_join_key_uses_hostname():
    device = ADDevice(hostname="ACME-DC01.corp.acme.example")
    assert device.join_key == "acme-dc01"


def test_ad_device_defaults():
    device = ADDevice(hostname="ACME-DC01")
    assert device.os == ""
    assert device.last_logon is None
    assert device.enabled is True
    assert device.distinguished_name == ""


def test_agent_device_join_key_uses_hostname():
    device = AgentDevice(vendor="sentinelone", agent_id="1", hostname="ACME-WS-001.corp.acme.example")
    assert device.join_key == "acme-ws-001"


def test_agent_device_to_dict_and_from_dict_round_trip():
    original = AgentDevice(
        vendor="carbonblack",
        agent_id="4197801",
        hostname="ACME-DC02",
        os="Windows Server 2022 Datacenter",
        last_seen=datetime(2026, 7, 2, 21, 13, tzinfo=timezone.utc),
        agent_version="4.0.1.128",
        platform="windows",
        machine_type="server",
    )

    restored = AgentDevice.from_dict(original.to_dict())

    assert restored == original


def test_agent_device_round_trip_with_no_last_seen():
    original = AgentDevice(vendor="bitdefender", agent_id="x", hostname="ACME-KIOSK7")
    restored = AgentDevice.from_dict(original.to_dict())
    assert restored == original
    assert restored.last_seen is None


def test_agent_device_to_dict_is_json_safe():
    device = AgentDevice(
        vendor="sentinelone",
        agent_id="1",
        hostname="ACME-DC01",
        last_seen=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    data = device.to_dict()
    # Every value must be a JSON primitive — no datetime objects leaking
    # across the Celery serialization boundary this method exists for.
    assert isinstance(data["last_seen"], str)
    assert all(isinstance(v, (str, type(None))) for v in data.values())


def test_coverage_status_values():
    assert CoverageStatus.COVERED.value == "covered"
    assert CoverageStatus.MISSING_AGENT.value == "missing_agent"
    assert CoverageStatus.ORPHANED_AGENT.value == "orphaned_agent"
    assert CoverageStatus.STALE_COVERAGE.value == "stale_coverage"


def test_vendor_enum_values():
    assert Vendor.SENTINELONE.value == "sentinelone"
    assert Vendor.CARBONBLACK.value == "carbonblack"
    assert Vendor.BITDEFENDER.value == "bitdefender"
