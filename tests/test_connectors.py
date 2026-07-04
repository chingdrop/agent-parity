"""Connector tests: fixture fallback, normalization, live-mode gating, and the
RestAdapter transport (retries wired, JSON/text parsing round-trips) — all
using a monkeypatched ``requests.Session.request`` rather than real network
access.
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
from agent_parity.rest_adapter import RestAdapter

ACME = SAMPLE_DATA_DIR / "acme"
GLOBEX = SAMPLE_DATA_DIR / "globex"

CONNECTORS = [SentinelOneConnector, CarbonBlackConnector, BitDefenderConnector]


class _FakeResponse:
    """Stands in for ``requests.Response`` so tests never touch the network."""

    def __init__(self, *, json_data=None, text=None, content_type="application/json"):
        self._json_data = json_data
        self.text = text if text is not None else ""
        self.content = self.text.encode()
        self.status_code = 200
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


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


@pytest.mark.parametrize("connector_cls", CONNECTORS)
def test_live_connector_uses_rest_adapter_with_retries(connector_cls):
    connector = connector_cls(credentials={})
    assert isinstance(connector.session, RestAdapter)
    adapter = connector.session.session.get_adapter("https://example.invalid")
    assert adapter.max_retries.total == 3
    assert set(adapter.max_retries.status_forcelist) == {429, 500, 502, 503, 504}


def test_live_fetch_inventory_round_trips_json_through_rest_adapter(monkeypatch):
    """Proves the connector -> _request_json -> RestAdapter -> parsed-dict path
    end to end, not just that fixture mode (which never touches RestAdapter)
    still works."""
    connector = SentinelOneConnector(
        credentials={"api_url": "https://usea1.example", "api_token": "tok"}
    )
    payload = {
        "data": [
            {
                "id": "12345",
                "computerName": "acme-ws-099",
                "osName": "Windows 11 Enterprise",
                "agentVersion": "24.1.2.199",
                "lastActiveDate": "2026-01-01T00:00:00Z",
            }
        ],
        "pagination": {"nextCursor": None},
    }
    monkeypatch.setattr(
        connector.session.session, "request", lambda **kwargs: _FakeResponse(json_data=payload)
    )

    devices = connector.fetch_inventory()

    assert len(devices) == 1
    assert devices[0].agent_id == "12345"
    assert devices[0].hostname == "acme-ws-099"
    assert devices[0].last_seen is not None


def test_request_json_rejects_non_dict_payload(monkeypatch):
    """_request_json exists so JSON-endpoint call sites don't need to
    re-narrow the dict|str|bytes union RestAdapter returns at every site."""
    connector = SentinelOneConnector(credentials={})
    monkeypatch.setattr(connector, "_request", lambda *a, **k: "unexpected text")
    with pytest.raises(ConnectorError, match="expected a JSON object"):
        connector._request_json("GET", "https://example.invalid")


def test_as_text_coerces_bytes_and_rejects_dicts():
    connector = SentinelOneConnector(credentials={})
    assert connector._as_text("already text") == "already text"
    assert connector._as_text(b"raw bytes") == "raw bytes"
    with pytest.raises(ConnectorError, match="expected text output"):
        connector._as_text({"unexpected": "dict"})
