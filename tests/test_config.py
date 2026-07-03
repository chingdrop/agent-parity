"""Config resolver tests: ${VAR} resolution and credential scoping."""

import pytest

from agent_parity.config import ConfigError, get_connector, load_config
from agent_parity.connectors import CarbonBlackConnector, SentinelOneConnector


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
