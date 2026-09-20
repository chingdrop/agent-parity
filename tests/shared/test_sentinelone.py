"""SentinelOneRSOMixin tests: the full upload -> execute -> poll -> fetch-files
sequence, error paths, and headers — all using a monkeypatched requests
layer, no real network access.
"""

import pytest

from agent_parity.shared.remote_exec import ConnectorError, VendorConnector
from agent_parity.shared.sentinelone import SentinelOneRSOMixin


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


class _TestSentinelOneConnector(SentinelOneRSOMixin, VendorConnector):
    """A minimal concrete connector — proves the mixin works when combined
    with the bare VendorConnector base, the same shape credential-audit's own
    connector uses (no inventory-fetching base layered on top)."""

    vendor = "sentinelone"
    required_credentials = ("api_url", "api_token")


def _connector():
    return _TestSentinelOneConnector(credentials={"api_url": "https://usea1.example", "api_token": "tok"})


def test_headers_use_api_token_scheme():
    connector = _connector()
    assert connector._headers == {"Authorization": "ApiToken tok"}


def test_live_deploy_and_run_round_trips_full_rso_sequence(monkeypatch, tmp_path):
    connector = _connector()
    connector.poll_interval = 0.01
    script = tmp_path / "script.ps1"
    script.write_text("# fake script")

    responses = iter(
        [
            _FakeResponse(json_data={"data": {"id": "script-123"}}),  # upload
            _FakeResponse(json_data={"data": {"parentTaskId": "task-456"}}),  # execute
            _FakeResponse(json_data={"data": [{"status": "running"}]}),  # poll: not done yet
            _FakeResponse(json_data={"data": [{"status": "completed", "id": "task-run-1"}]}),  # poll: done
            _FakeResponse(text="output text", content_type="text/plain"),  # fetch-files
        ]
    )
    calls = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(connector.session.session, "request", fake_request)

    output = connector.deploy_and_run(script, "AGENT1", {"UploadUrl": "https://x"})
    assert output == "output text"

    # Verify the RSO endpoints/shapes actually used, not just the final output.
    assert calls[0]["url"] == "https://usea1.example/web/api/v2.1/remote-scripts"
    assert calls[1]["url"] == "https://usea1.example/web/api/v2.1/remote-scripts/execute"
    assert calls[1]["json"]["data"]["scriptId"] == "script-123"
    assert calls[1]["json"]["data"]["inputParams"] == {"UploadUrl": "https://x"}
    assert calls[1]["json"]["filter"]["ids"] == ["AGENT1"]
    assert calls[2]["url"] == "https://usea1.example/web/api/v2.1/remote-scripts/status"
    assert calls[2]["params"]["parentTaskId"] == "task-456"
    assert calls[4]["url"] == "https://usea1.example/web/api/v2.1/remote-scripts/fetch-files"
    assert calls[4]["params"]["taskId"] == "task-run-1"


@pytest.mark.parametrize("state", ["failed", "canceled", "expired"])
def test_live_deploy_and_run_raises_on_terminal_failure_states(monkeypatch, tmp_path, state):
    connector = _connector()
    script = tmp_path / "script.ps1"
    script.write_text("# fake script")

    responses = iter(
        [
            _FakeResponse(json_data={"data": {"id": "script-123"}}),
            _FakeResponse(json_data={"data": {"parentTaskId": "task-456"}}),
            _FakeResponse(json_data={"data": [{"status": state}]}),
        ]
    )
    monkeypatch.setattr(connector.session.session, "request", lambda **kwargs: next(responses))

    with pytest.raises(ConnectorError, match=f"remote script {state} on AGENT1"):
        connector.deploy_and_run(script, "AGENT1")


def test_live_deploy_and_run_times_out_if_status_never_reports_tasks(monkeypatch, tmp_path):
    connector = _connector()
    connector.poll_interval = 0.01
    connector.poll_timeout = 0.03
    script = tmp_path / "script.ps1"
    script.write_text("# fake script")

    def fake_request(**kwargs):
        url = kwargs["url"]
        if url.endswith("/remote-scripts"):
            return _FakeResponse(json_data={"data": {"id": "script-123"}})
        if url.endswith("/remote-scripts/execute"):
            return _FakeResponse(json_data={"data": {"parentTaskId": "task-456"}})
        return _FakeResponse(json_data={"data": []})  # status never reports the task

    monkeypatch.setattr(connector.session.session, "request", fake_request)

    with pytest.raises(ConnectorError, match="timed out waiting for"):
        connector.deploy_and_run(script, "AGENT1")


def test_mixin_does_not_set_vendor_or_credentials_itself():
    assert not hasattr(SentinelOneRSOMixin, "vendor")
    assert not hasattr(SentinelOneRSOMixin, "required_credentials")
