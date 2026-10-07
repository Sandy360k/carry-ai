"""
Hugging Face device-code sign-in and gated-repo checks (models/hf_auth.py).
Offline: HTTP is faked. Response shapes match live probes of huggingface.co
(device: 400 {"error":"invalid_client"}, token: 400 {"error":"invalid_grant"},
gated file without/with a bad token: 401, ungated: 302, missing: 404).
"""

import io
import threading
from urllib.error import HTTPError

import pytest

from conftest import PROJECT_ROOT  # noqa: F401
from models import hf_auth as h


def test_start_device_flow_parses_code(monkeypatch):
    sent = {}

    def fake_post(url, fields):
        sent.update(url=url, fields=fields)
        return {"device_code": "dev", "user_code": "ABCD-EFGH",
                "verification_uri": "https://huggingface.co/oauth/device",
                "expires_in": 900, "interval": 5}
    monkeypatch.setattr(h, "_post_form", fake_post)
    code = h.start_device_flow("cid")
    assert code.user_code == "ABCD-EFGH" and code.interval == 5
    assert sent["url"] == h.DEVICE_URL
    assert sent["fields"] == {"client_id": "cid", "scope": "openid profile gated-repos"}


def test_start_device_flow_surfaces_errors(monkeypatch):
    monkeypatch.setattr(h, "_post_form", lambda u, f: {
        "error": "invalid_client", "error_description": "Client not found"})
    with pytest.raises(h.HfAuthError, match="Client not found"):
        h.start_device_flow("bad")
    with pytest.raises(h.HfAuthError, match="client id"):
        h.start_device_flow("")


def _code(**kw):
    return h.DeviceCode(device_code="dev", user_code="X", verification_uri="u",
                        expires_in=kw.get("expires_in", 60), interval=1)


def test_poll_waits_through_pending_and_slow_down(monkeypatch):
    replies = iter([{"error": "authorization_pending"}, {"error": "slow_down"},
                    {"access_token": "hf_oauth_x", "scope": "gated-repos"}])
    monkeypatch.setattr(h, "_post_form", lambda u, f: next(replies))
    sleeps = []
    tok = h.poll_for_token("cid", _code(), sleep=sleeps.append)
    assert tok.access_token == "hf_oauth_x"
    assert sleeps == [1, 6]                    # slow_down adds 5 s


@pytest.mark.parametrize("error,match", [("access_denied", "declined"),
                                         ("expired_token", "expired"),
                                         ("invalid_grant", "invalid_grant")])
def test_poll_ends_on_terminal_errors(monkeypatch, error, match):
    monkeypatch.setattr(h, "_post_form", lambda u, f: {"error": error})
    with pytest.raises(h.HfAuthError, match=match):
        h.poll_for_token("cid", _code(), sleep=lambda s: None)


def test_poll_can_be_cancelled(monkeypatch):
    monkeypatch.setattr(h, "_post_form", lambda u, f: {"error": "authorization_pending"})
    stop = threading.Event()
    stop.set()
    with pytest.raises(h.HfAuthError, match="cancelled"):
        h.poll_for_token("cid", _code(), cancel=stop, sleep=lambda s: None)


class _Opener:
    def __init__(self, code):
        self.code = code
        self.req = None

    def open(self, req, timeout=None):
        self.req = req
        if self.code == 200:
            return io.BytesIO(b"")
        raise HTTPError(req.full_url, self.code, "x", {}, None)


@pytest.mark.parametrize("status,token,expected", [
    (302, None, "ok"),
    (200, None, "ok"),
    (401, None, "auth_required"),
    (401, "hf_expired", "auth_required"),
    (403, "hf_valid", "no_access"),
    (404, None, "not_found"),
    (500, None, "unknown"),
])
def test_check_access_mapping(monkeypatch, status, token, expected):
    opener = _Opener(status)
    monkeypatch.setattr(h, "build_opener", lambda *a: opener)
    assert h.check_access("org/repo", "m.gguf", token) == expected
    assert opener.req.get_method() == "HEAD"
    assert opener.req.full_url.endswith("/org/repo/resolve/main/m.gguf")
    assert (opener.req.get_header("Authorization") == f"Bearer {token}") if token \
        else opener.req.get_header("Authorization") is None


def test_client_id_comes_from_settings_and_defaults_empty():
    assert h.configured_client_id() == ""
