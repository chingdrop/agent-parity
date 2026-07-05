"""Config resolver tests: ${VAR} resolution, fixture-mode fallback, and the
one-organization/one-vendor AppConfig shape."""

import pytest

from agent_parity.config import ConfigError, get_connector, get_storage, load_config
from agent_parity.connectors import SentinelOneConnector
from agent_parity.storage import ObjectStorage


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_vendor_and_credentials_and_ad_target_devices_parse(tmp_path):
    path = _write(
        tmp_path,
        """
        stale_days: 7
        vendor: sentinelone
        credentials:
          api_url: https://usea1.sentinelone.net
          api_token: s1-token
        ad_target_devices: [DC01, DC02]
        """,
    )
    config = load_config(path)

    assert config.stale_days == 7
    assert config.vendor == "sentinelone"
    assert config.credentials == {"api_url": "https://usea1.sentinelone.net", "api_token": "s1-token"}
    assert config.ad_target_devices == ("DC01", "DC02")


def test_omitted_fields_fall_back_to_sensible_defaults(tmp_path):
    path = _write(tmp_path, "vendor: sentinelone\n")
    config = load_config(path)

    assert config.stale_days == 14
    assert config.credentials == {}
    assert config.ad_target_devices == ()


def test_unknown_vendor_raises_with_the_registered_list(tmp_path):
    path = _write(tmp_path, "vendor: not-a-real-vendor\n")
    with pytest.raises(ConfigError, match="Unknown vendor.*not-a-real-vendor"):
        load_config(path)


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

    connector = get_connector(config)
    assert isinstance(connector, SentinelOneConnector)
    assert connector.is_live
    assert config.credentials["api_token"] == "resolved-token"


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

    connector = get_connector(config)
    assert not connector.is_live
    assert connector.fixture_dir is not None


def test_storage_unconfigured_by_default(monkeypatch):
    for var in ("STORAGE_BUCKET", "STORAGE_ACCESS_KEY", "STORAGE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    config = load_config()
    assert not config.storage.enabled
    assert get_storage(config) is None


def test_storage_enabled_when_fully_configured(monkeypatch):
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("STORAGE_BUCKET", "agent-parity-ad-exports")
    monkeypatch.setenv("STORAGE_ACCESS_KEY", "minio-access")
    monkeypatch.setenv("STORAGE_SECRET_KEY", "minio-secret")
    config = load_config()

    assert config.storage.enabled
    storage = get_storage(config)
    assert isinstance(storage, ObjectStorage)
    assert storage.bucket == "agent-parity-ad-exports"


def test_storage_rejects_unsupported_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BUCKET", "b")
    monkeypatch.setenv("STORAGE_ACCESS_KEY", "a")
    monkeypatch.setenv("STORAGE_SECRET_KEY", "s")
    config = load_config()
    from dataclasses import replace

    bad_config = replace(config, storage=replace(config.storage, backend="azure_blob"))
    with pytest.raises(ConfigError, match="Unsupported storage backend"):
        get_storage(bad_config)
