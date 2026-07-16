"""Connector tests: fixture fallback, normalization, live-mode gating, and the
RestAdapter transport (retries wired, JSON/text parsing round-trips) — all
using a monkeypatched ``requests.Session.request`` rather than real network
access.
"""

from datetime import UTC, datetime

import pytest
from shared_tools.rest_adapter import RestAdapter

from agent_parity.config import SAMPLE_DATA_DIR
from agent_parity.connectors import (
    BitDefenderConnector,
    CarbonBlackConnector,
    ConnectorError,
    SentinelOneConnector,
)
from agent_parity.connectors.base import infer_machine_type, infer_platform

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
def test_fixture_inventory_normalizes_platform_and_machine_type_to_s1_wording(connector_cls):
    """Regardless of vendor, platform/machine_type must read as SentinelOne's
    own wording ("windows"/"server"/"desktop") — that's the whole point of
    normalizing them, not just that they're non-empty."""
    devices = connector_cls(credentials={}, fixture_dir=ACME).fetch_inventory()
    assert devices
    for device in devices:
        assert device.platform == "windows", device
        assert device.machine_type in ("server", "desktop"), device


def test_carbonblack_lowercases_its_uppercase_os_enum():
    # CBC's real "os" field is an uppercase enum ("WINDOWS"); SentinelOne's
    # own osType wording is lowercase — this is a straight casing fix, no
    # inference needed since Carbon Black does report the field directly.
    devices = CarbonBlackConnector(credentials={}, fixture_dir=ACME).fetch_inventory()
    assert all(d.platform == "windows" for d in devices)


def test_bitdefender_maps_its_numeric_machine_type_enum_to_s1_wording():
    # GravityZone's machineType is a numeric enum (2 = server in our
    # fixtures); mapped to SentinelOne's string wording, not inferred, since
    # BitDefender does report this directly.
    devices = BitDefenderConnector(credentials={}, fixture_dir=ACME).fetch_inventory()
    servers = [d for d in devices if "Server" in d.os]
    desktops = [d for d in devices if "Server" not in d.os]
    assert servers and all(d.machine_type == "server" for d in servers)
    assert desktops and all(d.machine_type == "desktop" for d in desktops)


def test_sentinelone_parses_a_windows_build_number_from_its_revision_field():
    """The whole point of capturing os_build at all: SentinelOne devices
    with the identical free-text "Windows 11 Enterprise" name resolve to
    different, real build numbers."""
    devices = SentinelOneConnector(credentials={}, fixture_dir=ACME).fetch_inventory()
    windows_11 = [d for d in devices if d.os == "Windows 11 Enterprise"]
    assert windows_11
    assert all(d.os_build is not None for d in windows_11)
    # The fixtures deliberately spread these across more than one feature
    # update (22H2/23H2/24H2) — if this collapses to one value, the fixture
    # augmentation (or the parsing) regressed.
    assert len({d.os_build for d in windows_11}) > 1


@pytest.mark.parametrize("connector_cls", [CarbonBlackConnector, BitDefenderConnector])
def test_carbonblack_and_bitdefender_never_set_os_build(connector_cls):
    """Neither vendor's real API exposes a build-number-carrying field —
    os_build must stay unset rather than something guessed."""
    devices = connector_cls(credentials={}, fixture_dir=ACME).fetch_inventory()
    assert devices
    assert all(d.os_build is None for d in devices)


@pytest.mark.parametrize(
    "os_text,expected",
    [
        ("Windows Server 2022 Datacenter", "windows"),
        ("Windows 11 Enterprise", "windows"),
        ("Ubuntu 22.04 LTS", "linux"),
        ("macOS Sonoma", "macos"),
        ("", ""),
    ],
)
def test_infer_platform(os_text, expected):
    assert infer_platform(os_text) == expected


@pytest.mark.parametrize(
    "os_text,expected",
    [
        ("Windows Server 2022 Datacenter", "server"),
        ("Windows 11 Enterprise", "desktop"),
        ("", "desktop"),
    ],
)
def test_infer_machine_type(os_text, expected):
    assert infer_machine_type(os_text) == expected


@pytest.mark.parametrize("connector_cls", CONNECTORS)
def test_fixture_timestamps_are_rebased_to_now(connector_cls):
    """Static fixture dates are shifted so the newest check-in is ~now,
    keeping the authored stale/recent split stable over time."""
    devices = connector_cls(credentials={}, fixture_dir=ACME).fetch_inventory()
    newest = max(d.last_seen for d in devices if d.last_seen)
    assert abs((datetime.now(UTC) - newest).total_seconds()) < 300


def test_deploy_and_run_fixture_returns_ad_csv():
    connector = SentinelOneConnector(credentials={}, fixture_dir=ACME)
    output = connector.deploy_and_run("Export-ADDevices.ps1", "ACME-DC01")
    assert output.splitlines()[0].startswith("Name,")
    assert "ACME-DC01" in output


def test_bitdefender_does_not_support_remote_execution():
    """GravityZone has no real equivalent to S1/CB's remote script execution —
    BitDefender is fetch_inventory-only and must refuse deploy_and_run outright,
    in both fixture and live mode, rather than quietly handing back a fixture."""
    assert BitDefenderConnector.supports_remote_execution is False

    fixture_mode = BitDefenderConnector(credentials={}, fixture_dir=ACME)
    with pytest.raises(ConnectorError, match="does not support remote script execution"):
        fixture_mode.deploy_and_run("Export-ADDevices.ps1", "ACME-DC01")

    live_mode = BitDefenderConnector(credentials={"api_url": "https://example", "api_key": "k"})
    with pytest.raises(ConnectorError, match="does not support remote script execution"):
        live_mode.deploy_and_run("Export-ADDevices.ps1", "ACME-DC01")


@pytest.mark.parametrize("connector_cls", [SentinelOneConnector, CarbonBlackConnector])
def test_other_vendors_still_support_remote_execution(connector_cls):
    assert connector_cls.supports_remote_execution is True


# --- connector registry (@register_connector) -----------------------------------


def test_every_connector_registers_itself_under_its_own_vendor_name():
    from agent_parity.connectors import CONNECTOR_CLASSES

    for connector_cls in CONNECTORS:
        assert CONNECTOR_CLASSES[connector_cls.vendor] is connector_cls


def test_scope_and_ad_export_priority_match_committed_topology():
    """Pins the real business facts these class attributes drive
    AppConfig.sites_for/pick_ad_export_vendor with."""
    assert SentinelOneConnector.scope == "global"
    assert CarbonBlackConnector.scope == "per_client"
    assert BitDefenderConnector.scope == "global"
    assert SentinelOneConnector.ad_export_priority < CarbonBlackConnector.ad_export_priority


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
    connector = SentinelOneConnector(credentials={"api_url": "https://usea1.example", "api_token": "tok"})
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
    monkeypatch.setattr(connector.session.session, "request", lambda **kwargs: _FakeResponse(json_data=payload))

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


# --- multi-site/tenant filtering (site_ids / company_id) ------------------------


def _s1_item(agent_id: str, site_id: str) -> dict:
    return {"id": agent_id, "computerName": f"HOST-{agent_id}", "siteId": site_id}


def test_sentinelone_no_site_filter_returns_every_item():
    connector = SentinelOneConnector(credentials={"api_url": "x", "api_token": "y"})
    payload = {"data": [_s1_item("1", "site-a"), _s1_item("2", "site-b")]}
    devices = connector._parse_inventory(payload)
    assert {d.agent_id for d in devices} == {"1", "2"}


def test_sentinelone_site_filter_narrows_to_matching_sites():
    connector = SentinelOneConnector(credentials={"api_url": "x", "api_token": "y", "site_ids": "site-a"})
    payload = {"data": [_s1_item("1", "site-a"), _s1_item("2", "site-b"), _s1_item("3", "site-a")]}
    devices = connector._parse_inventory(payload)
    assert {d.agent_id for d in devices} == {"1", "3"}


def test_sentinelone_site_filter_accepts_a_comma_separated_list():
    connector = SentinelOneConnector(credentials={"api_url": "x", "api_token": "y", "site_ids": "site-a,site-c"})
    payload = {"data": [_s1_item("1", "site-a"), _s1_item("2", "site-b"), _s1_item("3", "site-c")]}
    devices = connector._parse_inventory(payload)
    assert {d.agent_id for d in devices} == {"1", "3"}


def test_sentinelone_live_fetch_passes_site_ids_query_param(monkeypatch):
    connector = SentinelOneConnector(
        credentials={"api_url": "https://usea1.sentinelone.net", "api_token": "t", "site_ids": "site-a"}
    )
    captured = {}

    def fake_request_json(method, url, headers=None, params=None):
        captured["params"] = params
        return {"data": [], "pagination": {"nextCursor": None}}

    monkeypatch.setattr(connector, "_request_json", fake_request_json)
    connector._live_fetch_inventory()
    assert captured["params"]["siteIds"] == "site-a"


def _bd_item(agent_id: str, company_id: str) -> dict:
    return {"id": agent_id, "name": f"HOST-{agent_id}", "companyId": company_id}


def test_bitdefender_no_company_filter_returns_every_item():
    connector = BitDefenderConnector(credentials={"api_url": "x", "api_key": "y"})
    payload = {"result": {"items": [_bd_item("1", "co-a"), _bd_item("2", "co-b")]}}
    devices = connector._parse_inventory(payload)
    assert {d.agent_id for d in devices} == {"1", "2"}


def test_bitdefender_company_filter_narrows_to_matching_company():
    connector = BitDefenderConnector(credentials={"api_url": "x", "api_key": "y", "company_id": "co-a"})
    payload = {"result": {"items": [_bd_item("1", "co-a"), _bd_item("2", "co-b")]}}
    devices = connector._parse_inventory(payload)
    assert {d.agent_id for d in devices} == {"1"}
