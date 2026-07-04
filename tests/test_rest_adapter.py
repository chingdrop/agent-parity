"""Tests for agent_parity/rest_adapter.py directly — the shared HTTP
transport connectors build on. Exercised indirectly via test_connectors.py's
RestAdapter-wiring checks, but nothing there tests RestAdapter's own
content-type parsing, header merging, or retry configuration in isolation.

Monkeypatches the underlying requests.Session.request, never touches the
network.
"""

import pytest
import requests

from agent_parity.rest_adapter import RestAdapter, RestAdapterConfig


class _FakeResponse:
    def __init__(self, *, json_data=None, text=None, content_type="application/json", status_code=200):
        self._json_data = json_data
        self.text = text if text is not None else ""
        self.content = self.text.encode()
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json_data


def _adapter(**config_kwargs) -> RestAdapter:
    return RestAdapter(RestAdapterConfig(base_url="https://api.example.test", **config_kwargs))


def test_json_content_type_returns_dict(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        adapter.session, "request", lambda **kw: _FakeResponse(json_data={"ok": True})
    )
    assert adapter.get("/agents") == {"ok": True}


def test_text_content_type_returns_str(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        adapter.session, "request",
        lambda **kw: _FakeResponse(text="Name,Enabled\nWS-01,True\n", content_type="text/csv"),
    )
    result = adapter.get("/export")
    assert isinstance(result, str)
    assert result.startswith("Name,")


def test_html_content_type_returns_str(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        adapter.session, "request",
        lambda **kw: _FakeResponse(text="<html></html>", content_type="text/html; charset=utf-8"),
    )
    assert adapter.get("/page") == "<html></html>"


def test_unrecognized_content_type_returns_raw_bytes(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        adapter.session, "request",
        lambda **kw: _FakeResponse(text="binary-ish", content_type="application/octet-stream"),
    )
    result = adapter.get("/blob")
    assert isinstance(result, bytes)
    assert result == b"binary-ish"


def test_http_error_status_raises(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter.session, "request", lambda **kw: _FakeResponse(status_code=500))
    with pytest.raises(requests.HTTPError):
        adapter.get("/broken")


@pytest.mark.parametrize(
    "method_name,expected_verb",
    [("get", "GET"), ("post", "POST"), ("put", "PUT"), ("delete", "DELETE")],
)
def test_convenience_methods_use_correct_http_verb(monkeypatch, method_name, expected_verb):
    adapter = _adapter()
    seen = {}

    def fake_request(**kw):
        seen["method"] = kw["method"]
        return _FakeResponse(json_data={})

    monkeypatch.setattr(adapter.session, "request", fake_request)
    getattr(adapter, method_name)("/x")
    assert seen["method"] == expected_verb


def test_per_call_headers_override_session_headers(monkeypatch):
    adapter = _adapter(headers={"Authorization": "ApiToken default"})
    seen = {}
    monkeypatch.setattr(
        adapter.session, "request",
        lambda **kw: (seen.update(headers=kw["headers"]), _FakeResponse(json_data={}))[1],
    )
    adapter.get("/x", headers={"Authorization": "ApiToken override"})
    assert seen["headers"]["Authorization"] == "ApiToken override"


def test_endpoint_joins_against_base_url(monkeypatch):
    adapter = _adapter()
    seen = {}
    monkeypatch.setattr(
        adapter.session, "request",
        lambda **kw: (seen.update(url=kw["url"]), _FakeResponse(json_data={}))[1],
    )
    adapter.get("/web/api/v2.1/agents")
    assert seen["url"] == "https://api.example.test/web/api/v2.1/agents"


def test_absolute_endpoint_overrides_base_url(monkeypatch):
    """Connectors pass fully-qualified URLs (base_url built per-request from
    credentials); urljoin must not mangle those against the config base_url."""
    adapter = _adapter()
    seen = {}
    monkeypatch.setattr(
        adapter.session, "request",
        lambda **kw: (seen.update(url=kw["url"]), _FakeResponse(json_data={}))[1],
    )
    adapter.get("https://other-host.example/path")
    assert seen["url"] == "https://other-host.example/path"


def test_timeout_falls_back_to_config_default(monkeypatch):
    adapter = _adapter(timeout=42.0)
    seen = {}
    monkeypatch.setattr(
        adapter.session, "request",
        lambda **kw: (seen.update(timeout=kw["timeout"]), _FakeResponse(json_data={}))[1],
    )
    adapter.get("/x")
    assert seen["timeout"] == 42.0


def test_files_kwarg_passed_through(monkeypatch):
    adapter = _adapter()
    seen = {}
    monkeypatch.setattr(
        adapter.session, "request",
        lambda **kw: (seen.update(files=kw["files"]), _FakeResponse(json_data={}))[1],
    )
    adapter.post("/upload", files={"file": ("script.ps1", b"contents")})
    assert seen["files"] == {"file": ("script.ps1", b"contents")}


def test_retries_mounted_with_configured_total(monkeypatch):
    adapter = _adapter(retries=5)
    http_adapter = adapter.session.get_adapter("https://api.example.test")
    assert http_adapter.max_retries.total == 5
    assert set(http_adapter.max_retries.status_forcelist) == {429, 500, 502, 503, 504}


def test_auth_and_proxies_applied_to_session():
    adapter = _adapter(auth=("user", "pass"), proxies={"https": "https://proxy.example"})
    assert adapter.session.auth == ("user", "pass")
    assert adapter.session.proxies["https"] == "https://proxy.example"
