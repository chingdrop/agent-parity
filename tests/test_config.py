"""Config resolver tests: ${VAR} resolution, credential scoping, and picking
the vendor that carries a client's AD export."""

import pytest

from agent_parity.config import ClientConfig, ConfigError, get_connector, load_config, pick_ad_export_vendor
from agent_parity.connectors import CarbonBlackConnector, SentinelOneConnector


def _client(vendors: tuple[str, ...]) -> ClientConfig:
    return ClientConfig(
        name="Test Client",
        slug="test",
        ad_target_device="TEST-DC01",
        sync_interval_hours=24,
        vendors={v: {} for v in vendors},
    )


@pytest.fixture
def config_with_creds(monkeypatch):
    monkeypatch.setenv("SENTINELONE_API_URL", "https://usea1.sentinelone.net")
    monkeypatch.setenv("SENTINELONE_API_TOKEN", "s1-global-token")
    monkeypatch.setenv("ACME_CB_API_URL", "https://defense.conferdeploy.net")
    monkeypatch.setenv("ACME_CB_API_ID", "ACMEID")
    monkeypatch.setenv("ACME_CB_API_KEY", "acme-cb-secret")
    monkeypatch.setenv("ACME_CB_ORG_KEY", "ACMEORG")
    return load_config()


def test_global_scope_returns_same_credentials_for_every_client(config_with_creds):
    acme = config_with_creds.credentials_for("acme", "sentinelone")
    globex = config_with_creds.credentials_for("globex", "sentinelone")
    assert acme == globex == {
        "api_url": "https://usea1.sentinelone.net",
        "api_token": "s1-global-token",
    }


def test_per_client_scope_returns_that_clients_block(config_with_creds):
    creds = config_with_creds.credentials_for("acme", "carbonblack")
    assert creds["api_id"] == "ACMEID"
    assert creds["api_key"] == "acme-cb-secret"
    assert creds["org_key"] == "ACMEORG"


def test_client_without_vendor_enabled_is_rejected(config_with_creds):
    # Globex doesn't declare carbonblack at all.
    with pytest.raises(ConfigError, match="does not enable"):
        config_with_creds.credentials_for("globex", "carbonblack")


def test_unset_env_vars_resolve_to_none_enabling_fixture_mode(monkeypatch):
    for var in ("SENTINELONE_API_URL", "SENTINELONE_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    config = load_config()
    connector = get_connector(config, "acme", "sentinelone")
    assert isinstance(connector, SentinelOneConnector)
    assert not connector.is_live
    assert connector.fixture_dir.name == "acme"


def test_get_connector_wires_live_credentials(config_with_creds):
    connector = get_connector(config_with_creds, "acme", "carbonblack")
    assert isinstance(connector, CarbonBlackConnector)
    assert connector.is_live
    assert connector.credentials["org_key"] == "ACMEORG"


def test_unknown_client_and_vendor_raise(config_with_creds):
    with pytest.raises(ConfigError, match="Unknown client"):
        config_with_creds.credentials_for("nope", "sentinelone")
    with pytest.raises(ConfigError, match="Unknown vendor"):
        config_with_creds.credentials_for("acme", "nope")


def test_ad_export_prefers_sentinelone_over_carbonblack():
    client = _client(("bitdefender", "carbonblack", "sentinelone"))
    assert pick_ad_export_vendor(client) == "sentinelone"


def test_ad_export_falls_back_to_carbonblack_without_sentinelone():
    client = _client(("bitdefender", "carbonblack"))
    assert pick_ad_export_vendor(client) == "carbonblack"


def test_ad_export_raises_when_only_bitdefender_is_enabled():
    client = _client(("bitdefender",))
    with pytest.raises(ConfigError, match="no vendor capable of remote script execution"):
        pick_ad_export_vendor(client)


def test_ad_export_vendor_selection_matches_committed_topology(config_with_creds):
    # acme (sentinelone+carbonblack+bitdefender) and globex (sentinelone+bitdefender)
    # both resolve to sentinelone now — previously "first alphabetically" picked
    # bitdefender for both, which doesn't genuinely support remote execution.
    assert pick_ad_export_vendor(config_with_creds.client("acme")) == "sentinelone"
    assert pick_ad_export_vendor(config_with_creds.client("globex")) == "sentinelone"
