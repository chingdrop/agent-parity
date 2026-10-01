"""Splunk HEC forwarder tests: no-op-when-unconfigured, HEC envelope shape,
rows-then-summary ordering, batching, and error propagation. No real
network — requests.post is monkeypatched.
"""

import json

import pytest

from agent_parity.config import SplunkConfig
from agent_parity.splunk_export import BATCH_SIZE, SplunkExportError, send_run

RUN_TIME = 1_790_000_000.0


def _splunk(**overrides) -> SplunkConfig:
    defaults = dict(hec_url="https://splunk.example:8088", hec_token="test-token")
    defaults.update(overrides)
    return SplunkConfig(**defaults)


def _refuse_to_post(*args, **kwargs):
    raise AssertionError("requests.post should not have been called")


class _FakeResponse:
    def raise_for_status(self):
        pass


def _capture_posts(monkeypatch):
    posts = []

    def fake_post(url, headers=None, data=None, timeout=None):
        posts.append({"url": url, "headers": headers, "data": data})
        return _FakeResponse()

    monkeypatch.setattr("agent_parity.splunk_export.requests.post", fake_post)
    return posts


def _envelopes(posts):
    return [json.loads(line) for post in posts for line in post["data"].split("\n")]


def test_disabled_when_unconfigured_makes_no_request(monkeypatch):
    monkeypatch.setattr("agent_parity.splunk_export.requests.post", _refuse_to_post)

    assert send_run([{"client": "acme"}], {"client": "acme"}, SplunkConfig(), event_time=RUN_TIME) == 0


def test_sends_each_row_then_the_summary_with_the_run_timestamp(monkeypatch):
    posts = _capture_posts(monkeypatch)
    rows = [{"join_key": "acme-ws-001", "status": "covered"}, {"join_key": "acme-sql02", "status": "missing_agent"}]
    summary = {"coverage_pct": 81.8}

    sent = send_run(rows, summary, _splunk(), event_time=RUN_TIME)

    assert sent == 3
    assert posts[0]["url"] == "https://splunk.example:8088/services/collector/event"
    assert posts[0]["headers"] == {"Authorization": "Splunk test-token"}
    envelopes = _envelopes(posts)
    assert [e["event"] for e in envelopes] == [*rows, summary]
    assert [e["sourcetype"] for e in envelopes] == [
        "agent_parity:coverage",
        "agent_parity:coverage",
        "agent_parity:coverage_summary",
    ]
    assert {e["time"] for e in envelopes} == {RUN_TIME}
    assert {e["index"] for e in envelopes} == {"security_coverage"}
    assert {e["source"] for e in envelopes} == {"agent-parity"}


def test_a_run_with_no_rows_still_sends_its_summary(monkeypatch):
    posts = _capture_posts(monkeypatch)

    assert send_run([], {"coverage_pct": 0.0}, _splunk(), event_time=RUN_TIME) == 1
    assert [e["sourcetype"] for e in _envelopes(posts)] == ["agent_parity:coverage_summary"]


def test_batches_above_batch_size_with_the_summary_last(monkeypatch):
    posts = _capture_posts(monkeypatch)
    rows = [{"i": i} for i in range(BATCH_SIZE)]

    sent = send_run(rows, {"summary": True}, _splunk(), event_time=RUN_TIME)

    assert sent == BATCH_SIZE + 1
    assert len(posts) == 2
    assert posts[0]["data"].count("\n") == BATCH_SIZE - 1  # BATCH_SIZE envelopes joined by newlines
    assert _envelopes(posts)[-1]["event"] == {"summary": True}


def test_request_exception_raises_splunk_export_error(monkeypatch):
    import requests

    def fake_post(url, headers=None, data=None, timeout=None):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("agent_parity.splunk_export.requests.post", fake_post)

    with pytest.raises(SplunkExportError, match="HEC POST failed"):
        send_run([{"client": "acme"}], {}, _splunk(), event_time=RUN_TIME)
