"""
MCP client tests (no network): Streamable HTTP stateless mode, SSE/JSON
body parsing, and the fallback to the legacy initialize handshake.
"""

import json

import pytest

from conftest import PROJECT_ROOT  # noqa: F401

from mcp.client import (HttpTransport, McpClient, PROTOCOL_VERSION,
                        LEGACY_PROTOCOL_VERSION, _parse_streamable_body)
from mcp.config import McpServerConfig
from mcp.registry import McpToolRegistry


class _Resp:
    def __init__(self, body, content_type="application/json", status=200):
        self._body = body
        self.headers = {"Content-Type": content_type}
        self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise _FakeRequestError(f"HTTP {self.status_code}")
    def json(self):
        return self._body
    @property
    def text(self):
        return self._body if isinstance(self._body, str) else json.dumps(self._body)


class _FakeRequestError(Exception):
    pass


class _FakeSession:
    """Records POSTs and replies from a scripted queue of _Resp/Exception."""
    def __init__(self, script):
        self.headers = {}
        self.script = list(script)
        self.calls = []
    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_requests(monkeypatch):
    import mcp.client as mc
    fake = type("R", (), {"Session": None, "RequestException": _FakeRequestError})
    monkeypatch.setattr(mc, "_requests", fake)
    return fake


def _cfg():
    return McpServerConfig(name="svc", transport="streamable-http",
                           url="https://h/mcp")


def test_parse_streamable_prefers_last_result_frame():
    body = ('data: {"jsonrpc":"2.0","id":1,"result":{"a":1}}\n'
            'data: {"jsonrpc":"2.0","id":1,"result":{"a":2}}\n'
            'data: [DONE]\n')
    out = _parse_streamable_body(_Resp(body, "text/event-stream"))
    assert out["result"] == {"a": 2}


def test_stateless_send_sets_headers_and_meta(monkeypatch):
    import mcp.client as mc
    sess = _FakeSession([_Resp({"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}})])
    monkeypatch.setattr(mc._requests, "Session", lambda: sess, raising=False)
    t = HttpTransport(_cfg(), stateless=True)
    assert t.start()
    t.send("tools/call", {"name": "x"}, name="x")
    call = sess.calls[0]
    assert call["headers"]["MCP-Protocol-Version"] == PROTOCOL_VERSION
    assert call["headers"]["Mcp-Method"] == "tools/call"
    assert call["headers"]["Mcp-Name"] == "x"
    meta = call["json"]["_meta"]["io.modelcontextprotocol/clientInfo"]
    assert meta["protocolVersion"] == PROTOCOL_VERSION
    # No initialize was sent — the very first call is the real method.
    assert call["json"]["method"] == "tools/call"


def test_connect_uses_stateless_first_no_handshake(monkeypatch):
    import mcp.client as mc
    sess = _FakeSession([
        _Resp({"jsonrpc": "2.0", "id": 1,
               "result": {"tools": [{"name": "ping"}]}}),
    ])
    monkeypatch.setattr(mc._requests, "Session", lambda: sess, raising=False)
    client = McpClient(_cfg(), McpToolRegistry())
    assert client.connect()
    assert client._protocol == PROTOCOL_VERSION
    assert [t["name"] for t in client._tools] == ["ping"]
    assert sess.calls[0]["json"]["method"] == "tools/list"   # no initialize


def test_connect_falls_back_to_legacy_initialize(monkeypatch):
    import mcp.client as mc
    # First session: stateless tools/list fails. Second: legacy handshake.
    sessions = [
        _FakeSession([_FakeRequestError("stateless not supported")]),
        _FakeSession([
            _Resp({"jsonrpc": "2.0", "id": 1,
                   "result": {"serverInfo": {"name": "old"},
                              "protocolVersion": LEGACY_PROTOCOL_VERSION}}),
            _Resp({"jsonrpc": "2.0", "id": 2, "result": {}}),      # initialized
            _Resp({"jsonrpc": "2.0", "id": 3,
                   "result": {"tools": [{"name": "legacy_tool"}]}}),
        ]),
    ]
    monkeypatch.setattr(mc._requests, "Session", lambda: sessions.pop(0),
                        raising=False)
    client = McpClient(_cfg(), McpToolRegistry())
    assert client.connect()
    assert client._protocol == LEGACY_PROTOCOL_VERSION
    assert [t["name"] for t in client._tools] == ["legacy_tool"]
