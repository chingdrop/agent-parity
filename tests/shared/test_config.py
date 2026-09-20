"""${VAR} env-resolution tests — no YAML/file I/O involved, just the resolver.

Also covers StorageConfig/parse_storage_config/get_storage, the same
config-shape-plus-builder pattern shared by agent-parity and
credential-audit's own storage-backed script-export handoff.
"""

import pytest

from agent_parity.shared.config import (
    ConfigError,
    StorageConfig,
    get_storage,
    parse_storage_config,
    resolve_env_refs,
)


def test_resolves_set_env_var(monkeypatch):
    monkeypatch.setenv("TEST_VAR", "resolved-value")
    assert resolve_env_refs("${TEST_VAR}") == "resolved-value"


def test_unset_env_var_resolves_to_none(monkeypatch):
    monkeypatch.delenv("TEST_UNSET_VAR", raising=False)
    assert resolve_env_refs("${TEST_UNSET_VAR}") is None


def test_empty_env_var_resolves_to_none(monkeypatch):
    # An explicitly empty value is treated the same as unset, not "".
    monkeypatch.setenv("TEST_EMPTY_VAR", "")
    assert resolve_env_refs("${TEST_EMPTY_VAR}") is None


def test_non_reference_string_passes_through_unchanged():
    assert resolve_env_refs("plain-value") == "plain-value"
    assert resolve_env_refs("not-${a}-full-match") == "not-${a}-full-match"


def test_non_string_scalars_pass_through_unchanged():
    assert resolve_env_refs(42) == 42
    assert resolve_env_refs(True) is True
    assert resolve_env_refs(None) is None


def test_recurses_into_nested_dicts_and_lists(monkeypatch):
    monkeypatch.setenv("NESTED_VAR", "nested-value")
    raw = {
        "top": "${NESTED_VAR}",
        "list": ["${NESTED_VAR}", "literal"],
        "nested": {"inner": "${NESTED_VAR}"},
    }
    resolved = resolve_env_refs(raw)
    assert resolved == {
        "top": "nested-value",
        "list": ["nested-value", "literal"],
        "nested": {"inner": "nested-value"},
    }


def test_config_error_is_a_plain_exception():
    with pytest.raises(ConfigError, match="boom"):
        raise ConfigError("boom")


# --- StorageConfig / parse_storage_config / get_storage --------------------


def test_parse_storage_config_defaults_for_empty_section():
    config = parse_storage_config({})
    assert config.backend == "s3"
    assert config.region == "us-east-1"
    assert config.bucket is None
    assert config.enabled is False


def test_parse_storage_config_reads_all_fields():
    config = parse_storage_config(
        {
            "storage": {
                "backend": "s3",
                "endpoint_url": "http://minio:9000",
                "bucket": "my-bucket",
                "access_key": "ak",
                "secret_key": "sk",
                "region": "us-west-2",
            }
        }
    )
    assert config == StorageConfig(
        backend="s3",
        endpoint_url="http://minio:9000",
        bucket="my-bucket",
        access_key="ak",
        secret_key="sk",
        region="us-west-2",
    )


def test_storage_config_enabled_requires_bucket_and_credentials():
    assert not StorageConfig().enabled
    assert not StorageConfig(bucket="b").enabled
    assert not StorageConfig(bucket="b", access_key="a").enabled
    assert StorageConfig(bucket="b", access_key="a", secret_key="s").enabled


def test_get_storage_returns_none_when_unconfigured():
    assert get_storage(StorageConfig()) is None


def test_get_storage_builds_object_storage_when_enabled():
    from agent_parity.shared.storage import ObjectStorage

    config = StorageConfig(bucket="my-bucket", access_key="ak", secret_key="sk")
    storage = get_storage(config)
    assert isinstance(storage, ObjectStorage)
    assert storage.bucket == "my-bucket"


def test_get_storage_rejects_unsupported_backend():
    config = StorageConfig(backend="azure_blob", bucket="b", access_key="a", secret_key="s")
    with pytest.raises(ConfigError, match="Unsupported storage backend"):
        get_storage(config)
