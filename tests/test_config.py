"""Config resolver tests: ${VAR} resolution, credential scoping, and picking
the vendor that carries a client's AD export."""

from dataclasses import replace

import pytest

from agent_parity.config import (
    AppConfig,
    ClientConfig,
    ConfigError,
    VendorConfig,
    get_connectors,
    get_storage,
    load_config,
    pick_ad_export_vendor,
)
from agent_parity.connectors import CarbonBlackConnector, SentinelOneConnector
from shared_tools.storage import ObjectStorage


def _client(vendors: tuple[str, ...]) -> ClientConfig:
    return ClientConfig(
        name="Test Client",
        slug="test",
        ad_target_devices=("TEST-DC01",),
        sync_interval_hours=24,
        vendors={v: ({},) for v in vendors},
    )


@pytest.fixture
def config_with_creds(monkeypatch):
    monkeypatch.setenv("SENTINELONE_MSSP_API_URL", "https://usea1.sentinelone.net")
    monkeypatch.setenv("SENTINELONE_MSSP_API_TOKEN", "s1-global-token")
    monkeypatch.setenv("SENTINELONE_DFIR_API_URL", "https://usea1-dfir.sentinelone.net")
    monkeypatch.setenv("SENTINELONE_DFIR_API_TOKEN", "s1-dfir-token")
    monkeypatch.setenv("ACME_CB_API_URL", "https://defense.conferdeploy.net")
    monkeypatch.setenv("ACME_CB_API_ID", "ACMEID")
    monkeypatch.setenv("ACME_CB_API_KEY", "acme-cb-secret")
    monkeypatch.setenv("ACME_CB_ORG_KEY", "ACMEORG")
    monkeypatch.setenv("ACME_CB2_API_URL", "https://defense.conferdeploy.net")
    monkeypatch.setenv("ACME_CB2_API_ID", "ACMEBRANCHID")
    monkeypatch.setenv("ACME_CB2_API_KEY", "acme-branch-secret")
    monkeypatch.setenv("ACME_CB2_ORG_KEY", "ACMEBRANCHORG")
    return load_config()


def test_global_scope_returns_same_credentials_for_every_client(config_with_creds):
    # Both acme and globex select the "mssp" account in config.yaml.
    acme = config_with_creds.sites_for("acme", "sentinelone")
    globex = config_with_creds.sites_for("globex", "sentinelone")
    assert acme == globex == (
        {
            "api_url": "https://usea1.sentinelone.net",
            "api_token": "s1-global-token",
            "account": "mssp",
        },
    )


def test_global_scope_with_multiple_accounts_requires_an_explicit_choice():
    """SentinelOne has two accounts (mssp/dfir) — a client that doesn't say
    which one gets a clear ConfigError, not a silent pick."""
    client = _client(("sentinelone",))
    config = load_config()
    config = replace(config, clients={**config.clients, "test": client})
    with pytest.raises(ConfigError, match="must specify which .* account"):
        config.sites_for("test", "sentinelone")


def test_global_scope_rejects_an_unknown_account_name():
    client = ClientConfig(
        name="Test Client",
        slug="test",
        ad_target_devices=("TEST-DC01",),
        sync_interval_hours=24,
        vendors={"sentinelone": ({"account": "nope"},)},
    )
    config = load_config()
    config = replace(config, clients={**config.clients, "test": client})
    with pytest.raises(ConfigError, match="unknown 'sentinelone' account"):
        config.sites_for("test", "sentinelone")


def test_global_scope_with_no_accounts_configured_resolves_to_empty_dict():
    """A fresh install with nothing in .env yet — same fixture-mode
    graceful degradation as before this feature existed."""
    vendor = VendorConfig(name="sentinelone", scope="global", accounts={})
    client = _client(("sentinelone",))
    config = AppConfig(
        stale_days=14,
        vendors={"sentinelone": vendor},
        clients={"test": client},
        storage=load_config().storage,
        splunk=load_config().splunk,
    )
    assert config.sites_for("test", "sentinelone") == ({},)


def test_per_client_scope_returns_that_clients_block(config_with_creds):
    sites = config_with_creds.sites_for("acme", "carbonblack")
    creds = sites[0]
    assert creds["api_id"] == "ACMEID"
    assert creds["api_key"] == "acme-cb-secret"
    assert creds["org_key"] == "ACMEORG"


def test_per_client_scope_returns_every_tenant_acme_has(config_with_creds):
    """Acme has two Carbon Black tenants in config.yaml (its primary org
    plus a labeled "branch" one) — both must come back, as fully
    independent credential blocks, not merged with each other."""
    sites = config_with_creds.sites_for("acme", "carbonblack")
    assert len(sites) == 2
    assert sites[0]["org_key"] == "ACMEORG"
    assert sites[1]["org_key"] == "ACMEBRANCHORG"
    assert sites[1]["label"] == "branch"


def test_client_without_vendor_enabled_is_rejected(config_with_creds):
    # Globex doesn't declare carbonblack at all.
    with pytest.raises(ConfigError, match="does not enable"):
        config_with_creds.sites_for("globex", "carbonblack")


def test_unset_env_vars_resolve_to_none_enabling_fixture_mode(monkeypatch):
    for var in (
        "SENTINELONE_MSSP_API_URL", "SENTINELONE_MSSP_API_TOKEN",
        "SENTINELONE_DFIR_API_URL", "SENTINELONE_DFIR_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    config = load_config()
    connector = get_connectors(config, "acme", "sentinelone")[0]
    assert isinstance(connector, SentinelOneConnector)
    assert not connector.is_live
    assert connector.fixture_dir.name == "acme"


def test_get_connectors_wires_live_credentials_for_every_tenant(config_with_creds):
    connectors = get_connectors(config_with_creds, "acme", "carbonblack")
    assert len(connectors) == 2
    assert all(isinstance(c, CarbonBlackConnector) for c in connectors)
    assert all(c.is_live for c in connectors)
    assert connectors[0].credentials["org_key"] == "ACMEORG"
    assert connectors[1].credentials["org_key"] == "ACMEBRANCHORG"


def test_unknown_client_and_vendor_raise(config_with_creds):
    with pytest.raises(ConfigError, match="Unknown client"):
        config_with_creds.sites_for("nope", "sentinelone")
    with pytest.raises(ConfigError, match="Unknown vendor"):
        config_with_creds.sites_for("acme", "nope")


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
    # both resolve to sentinelone.
    assert pick_ad_export_vendor(config_with_creds.client("acme")) == "sentinelone"
    assert pick_ad_export_vendor(config_with_creds.client("globex")) == "sentinelone"


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
    bad_config = replace(config, storage=replace(config.storage, backend="azure_blob"))
    with pytest.raises(ConfigError, match="Unsupported storage backend"):
        get_storage(bad_config)
