"""Tests for the flat, single-console config dialect (agent_parity.config's
_load_simple_config, dispatched from load_config() on a top-level "vendor:"
key) — a temp YAML file per test, not the repo's committed config.yaml
(which always uses the nested, multi-client dialect).
"""

import pytest

from agent_parity.config import ConfigError, load_config
from agent_parity.connectors import SentinelOneConnector


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_global_scope_vendor_credentials_land_in_a_default_account(tmp_path):
    path = _write(
        tmp_path,
        """
        vendor: sentinelone
        credentials:
          api_url: https://usea1.sentinelone.net
          api_token: s1-token
        ad_target_devices: [DC01]
        """,
    )
    config = load_config(path)

    assert config.vendors["sentinelone"].scope == "global"
    assert config.vendors["sentinelone"].accounts == {
        "default": {"api_url": "https://usea1.sentinelone.net", "api_token": "s1-token"}
    }
    assert config.sites_for("default", "sentinelone") == (
        {"api_url": "https://usea1.sentinelone.net", "api_token": "s1-token"},
    )


def test_per_client_scope_vendor_credentials_land_on_the_client_directly(tmp_path):
    path = _write(
        tmp_path,
        """
        vendor: carbonblack
        credentials:
          api_url: https://defense.conferdeploy.net
          api_id: MYID
          api_key: my-secret
          org_key: MYORG
        ad_target_devices: [DC01]
        """,
    )
    config = load_config(path)

    assert config.vendors["carbonblack"].scope == "per_client"
    assert config.vendors["carbonblack"].accounts == {}
    assert config.sites_for("default", "carbonblack") == (
        {"api_url": "https://defense.conferdeploy.net", "api_id": "MYID", "api_key": "my-secret", "org_key": "MYORG"},
    )


def test_unknown_vendor_raises_with_the_registered_list(tmp_path):
    path = _write(tmp_path, "vendor: not-a-real-vendor\n")
    with pytest.raises(ConfigError, match="Unknown vendor.*not-a-real-vendor"):
        load_config(path)


def test_omitted_fields_fall_back_to_sensible_defaults(tmp_path):
    path = _write(tmp_path, "vendor: sentinelone\n")
    config = load_config(path)

    assert config.stale_days == 14
    assert list(config.clients) == ["default"]
    client = config.client("default")
    assert client.name == "Default"
    assert client.ad_target_devices == ()
    assert client.sync_interval_hours == 24


def test_name_and_slug_are_overridable(tmp_path):
    path = _write(tmp_path, "vendor: sentinelone\nname: My Org\nslug: my-org\n")
    config = load_config(path)

    assert list(config.clients) == ["my-org"]
    assert config.client("my-org").name == "My Org"


def test_env_var_resolution_and_fixture_mode_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_S1_API_URL", "https://usea1.sentinelone.net")
    monkeypatch.setenv("TEST_S1_API_TOKEN", "resolved-token")
    path = _write(
        tmp_path,
        """
        vendor: sentinelone
        credentials:
          api_url: ${TEST_S1_API_URL}
          api_token: ${TEST_S1_API_TOKEN}
        """,
    )
    config = load_config(path)
    creds = config.sites_for("default", "sentinelone")[0]
    connector = SentinelOneConnector(credentials=creds)
    assert connector.is_live
    assert creds["api_token"] == "resolved-token"


def test_unset_env_var_falls_back_to_fixture_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_S1_UNSET_URL", raising=False)
    monkeypatch.delenv("TEST_S1_UNSET_TOKEN", raising=False)
    path = _write(
        tmp_path,
        """
        vendor: sentinelone
        credentials:
          api_url: ${TEST_S1_UNSET_URL}
          api_token: ${TEST_S1_UNSET_TOKEN}
        """,
    )
    config = load_config(path)
    creds = config.sites_for("default", "sentinelone")[0]
    connector = SentinelOneConnector(credentials=creds, fixture_dir=tmp_path)
    assert not connector.is_live
