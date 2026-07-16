"""Splunk HEC forwarder tests: no-op-when-unconfigured, batching, HEC shape,
and error propagation. No real network — requests.post is monkeypatched.
"""

import json

import pytest

from agent_parity.config import SplunkConfig
from agent_parity.splunk_export import BATCH_SIZE, SplunkExportError, send_deltas


def _splunk(**overrides) -> SplunkConfig:
    defaults = dict(hec_url="https://splunk.example:8088", hec_token="test-token")
    defaults.update(overrides)
    return SplunkConfig(**defaults)


def _refuse_to_post(*args, **kwargs):
    raise AssertionError("requests.post should not have been called")


def test_disabled_when_unconfigured_makes_no_request(monkeypatch):
    monkeypatch.setattr("agent_parity.splunk_export.requests.post", _refuse_to_post)
    sent = send_deltas([{"client": "acme"}], SplunkConfig())
    assert sent == 0


def test_empty_deltas_makes_no_request(monkeypatch):
    monkeypatch.setattr("agent_parity.splunk_export.requests.post", _refuse_to_post)
    sent = send_deltas([], _splunk())
    assert sent == 0


class _FakeResponse:
    def raise_for_status(self):
        pass


def test_posts_correctly_shaped_hec_envelope(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        return _FakeResponse()

    monkeypatch.setattr("agent_parity.splunk_export.requests.post", fake_post)
    delta = {"client": "acme", "join_key": "acme-ws-014", "status": "missing_agent"}

    sent = send_deltas([delta], _splunk())

    assert sent == 1
    assert captured["url"] == "https://splunk.example:8088/services/collector/event"
    assert captured["headers"] == {"Authorization": "Splunk test-token"}
    envelope = json.loads(captured["data"])
    assert envelope["index"] == "security_coverage"
    assert envelope["sourcetype"] == "agent_parity:coverage_delta"
    assert envelope["source"] == "agent-parity"
    assert envelope["event"] == delta


def test_batches_above_batch_size(monkeypatch):
    posts = []

    def fake_post(url, headers=None, data=None, timeout=None):
        posts.append(data)
        return _FakeResponse()

    monkeypatch.setattr("agent_parity.splunk_export.requests.post", fake_post)
    deltas = [{"i": i} for i in range(BATCH_SIZE + 1)]

    sent = send_deltas(deltas, _splunk())

    assert sent == BATCH_SIZE + 1
    assert len(posts) == 2
    assert posts[0].count("\n") == BATCH_SIZE - 1  # BATCH_SIZE envelopes joined by newlines
    assert posts[1].count("\n") == 0  # the remaining single envelope


def test_request_exception_raises_splunk_export_error(monkeypatch):
    import requests

    def fake_post(url, headers=None, data=None, timeout=None):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("agent_parity.splunk_export.requests.post", fake_post)

    with pytest.raises(SplunkExportError, match="HEC POST failed"):
        send_deltas([{"client": "acme"}], _splunk())
