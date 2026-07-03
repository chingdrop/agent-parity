"""Connector tests: fixture fallback, normalization, and live-mode gating.

These run entirely against sample_data/ — no network access anywhere.
"""

from datetime import datetime, timezone

import pytest

from agent_parity.config import SAMPLE_DATA_DIR
from agent_parity.connectors import (
    BitDefenderConnector,
    CarbonBlackConnector,
    ConnectorError,
    SentinelOneConnector,
)

ACME = SAMPLE_DATA_DIR / "acme"
GLOBEX = SAMPLE_DATA_DIR / "globex"

CONNECTORS = [SentinelOneConnector, CarbonBlackConnector, BitDefenderConnector]


@pytest.mark.parametrize("connector_cls", CONNECTORS)
def test_fixture_inventory_is_normalized(connector_cls):
    connector = connector_cls(credentials={}, fixture_dir=ACME)
    devices = connector.fetch_inventory()
    assert devices, "fixture should yield at least one device"
    for device in devices:
        assert device.vendor == connector_cls.vendor
        assert device.hostname
        assert device.agent_id
        assert device.last_seen is None or device.last_seen.tzinfo is not None


@pytest.mark.parametrize("connector_cls", CONNECTORS)
def test_fixture_timestamps_are_rebased_to_now(connector_cls):
    """Static fixture dates are shifted so the newest check-in is ~now,
    keeping the authored stale/recent split stable over time."""
    devices = connector_cls(credentials={}, fixture_dir=ACME).fetch_inventory()
    newest = max(d.last_seen for d in devices if d.last_seen)
    assert abs((datetime.now(timezone.utc) - newest).total_seconds()) < 300


def test_deploy_and_run_fixture_returns_ad_csv():
    connector = SentinelOneConnector(credentials={}, fixture_dir=ACME)
    output = connector.deploy_and_run("Export-ADDevices.ps1", "ACME-DC01")
    assert output.splitlines()[0].startswith("Name,")
    assert "ACME-DC01" in output


def test_is_live_requires_all_credentials():
    partial = CarbonBlackConnector(
        credentials={"api_url": "https://example", "api_id": "X", "api_key": None, "org_key": "Y"}
    )
    complete = CarbonBlackConnector(
        credentials={"api_url": "https://example", "api_id": "X", "api_key": "K", "org_key": "Y"}
    )
    assert not partial.is_live
    assert complete.is_live


def test_missing_fixture_raises_clear_error():
    # Globex doesn't use Carbon Black, so it has no CB fixture.
    connector = CarbonBlackConnector(credentials={}, fixture_dir=GLOBEX)
    with pytest.raises(ConnectorError, match="fixture not found"):
        connector.fetch_inventory()


def test_no_credentials_and_no_fixture_dir_raises():
    connector = SentinelOneConnector(credentials={})
    with pytest.raises(ConnectorError, match="no fixture_dir"):
        connector.fetch_inventory()
