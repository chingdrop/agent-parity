"""VendorConnector tests: live/fixture dispatch, polling, registry — all
using a monkeypatched RestAdapter/requests layer, no real network access.
"""

import pytest

from agent_parity.shared.remote_exec import ConnectorError, ConnectorRegistry, VendorConnector


class _FakeVendor(VendorConnector):
    """A minimal concrete connector exercising the base's own behavior,
    without any project-specific fetch/fixture logic layered on top."""

    vendor = "fakevendor"
    required_credentials = ("api_url", "api_token")


class _LiveVendor(_FakeVendor):
    """Overrides both hooks so live/fixture dispatch can be told apart."""

    def _live_deploy_and_run(self, script_path, target_id, script_args):
        return f"live:{target_id}:{script_args}"

    def _fixture_deploy_and_run(self, script_path, target_id, script_args):
        return f"fixture:{target_id}"


def test_is_live_requires_all_credentials():
    partial = _FakeVendor(credentials={"api_url": "https://example", "api_token": None})
    complete = _FakeVendor(credentials={"api_url": "https://example", "api_token": "tok"})
    assert not partial.is_live
    assert complete.is_live


def test_session_is_rest_adapter_with_retries():
    connector = _FakeVendor(credentials={})
    adapter = connector.session.session.get_adapter("https://example.invalid")
    assert adapter.max_retries.total == 3
    assert set(adapter.max_retries.status_forcelist) == {429, 500, 502, 503, 504}


def test_deploy_and_run_dispatches_live_vs_fixture():
    live = _LiveVendor(credentials={"api_url": "https://example", "api_token": "tok"})
    assert live.deploy_and_run("script.ps1", "TARGET1") == "live:TARGET1:{}"

    fixture = _LiveVendor(credentials={})
    assert fixture.deploy_and_run("script.ps1", "TARGET1") == "fixture:TARGET1"


def test_deploy_and_run_refuses_when_remote_execution_unsupported():
    class _NoRemoteExec(_FakeVendor):
        supports_remote_execution = False

    connector = _NoRemoteExec(credentials={})
    with pytest.raises(ConnectorError, match="does not support remote script execution"):
        connector.deploy_and_run("script.ps1", "TARGET1")


def test_live_deploy_and_run_default_raises_not_implemented():
    connector = _FakeVendor(credentials={"api_url": "https://example", "api_token": "tok"})
    with pytest.raises(ConnectorError, match="not implemented"):
        connector.deploy_and_run("script.ps1", "TARGET1")


def test_fixture_deploy_and_run_default_raises_clear_error():
    connector = _FakeVendor(credentials={})
    with pytest.raises(ConnectorError, match="no fixture behavior defined"):
        connector.deploy_and_run("script.ps1", "TARGET1")


def test_fixture_path_missing_dir_raises():
    connector = _FakeVendor(credentials={})
    with pytest.raises(ConnectorError, match="no fixture_dir"):
        connector._fixture_path("whatever.csv")


def test_fixture_path_missing_file_raises(tmp_path):
    connector = _FakeVendor(credentials={}, fixture_dir=tmp_path)
    with pytest.raises(ConnectorError, match="fixture not found"):
        connector._fixture_path("whatever.csv")


def test_fixture_path_returns_existing_file(tmp_path):
    (tmp_path / "found.csv").write_text("data")
    connector = _FakeVendor(credentials={}, fixture_dir=tmp_path)
    assert connector._fixture_path("found.csv") == tmp_path / "found.csv"


def test_poll_until_returns_first_non_none_result():
    connector = _FakeVendor(credentials={})
    connector.poll_interval = 0.01
    calls = iter([None, None, "done"])
    result = connector._poll_until(lambda: next(calls), "test op")
    assert result == "done"


def test_poll_until_times_out():
    connector = _FakeVendor(credentials={})
    connector.poll_interval = 0.01
    connector.poll_timeout = 0.03
    with pytest.raises(ConnectorError, match="timed out waiting for test op"):
        connector._poll_until(lambda: None, "test op")


def test_request_wraps_transport_errors(monkeypatch):
    import requests

    connector = _FakeVendor(credentials={})

    def boom(**kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(connector.session.session, "request", boom)
    with pytest.raises(ConnectorError, match="API request failed"):
        connector._request("GET", "https://example.invalid")


def test_request_json_rejects_non_dict_payload():
    connector = _FakeVendor(credentials={})
    connector._request = lambda *a, **k: "unexpected text"
    with pytest.raises(ConnectorError, match="expected a JSON object"):
        connector._request_json("GET", "https://example.invalid")


def test_as_text_coerces_bytes_and_rejects_dicts():
    connector = _FakeVendor(credentials={})
    assert connector._as_text("already text") == "already text"
    assert connector._as_text(b"raw bytes") == "raw bytes"
    with pytest.raises(ConnectorError, match="expected text output"):
        connector._as_text({"unexpected": "dict"})


def test_connector_registry_registers_by_vendor_attribute():
    registry = ConnectorRegistry()
    register = registry.register

    @register
    class OneVendor(_FakeVendor):
        vendor = "one"

    @register
    class TwoVendor(_FakeVendor):
        vendor = "two"

    assert registry["one"] is OneVendor
    assert registry["two"] is TwoVendor


def test_separate_registry_instances_do_not_collide():
    registry_a = ConnectorRegistry()
    registry_b = ConnectorRegistry()

    @registry_a.register
    class SameNameVendor(_FakeVendor):
        vendor = "shared-name"

    assert "shared-name" in registry_a
    assert "shared-name" not in registry_b
