"""
Integration wrapper tests — exact command lines and request payloads for
voice (ElevenLabs/AssemblyAI), Google Workspace CLI, Onyx, G0DM0D3 and
Scrapling. Offline: subprocess and HTTP are replaced with fakes.
"""

import json
import os
import subprocess
import types

import pytest

pytest.importorskip("requests")


# ---------------------------------------------------------------------------
# Voice: ElevenLabs SDK 2.x / REST, AssemblyAI REST
# ---------------------------------------------------------------------------

def _tts(monkeypatch):
    import integrations.voice_tools as vt
    cfg = vt.VoiceConfig(elevenlabs_key="el-key")
    player = vt.TTSPlayer(cfg)
    played = []
    monkeypatch.setattr(player, "_play_pcm_stream", lambda it: played.append(list(it)))
    return vt, cfg, player, played


def test_elevenlabs_sdk_uses_text_to_speech_stream(monkeypatch):
    vt, cfg, player, played = _tts(monkeypatch)
    calls = {}

    class FakeTTS:
        def stream(self, voice_id, **kw):
            calls.update(voice_id=voice_id, **kw)
            return iter([b"pcm"])

    class FakeClient:
        def __init__(self, api_key):
            calls["api_key"] = api_key
            self.text_to_speech = FakeTTS()

    monkeypatch.setattr(vt, "ElevenLabs", FakeClient)
    monkeypatch.setattr(vt, "_elevenlabs_sdk", True)
    player.speak("hello")
    assert calls == {"api_key": "el-key", "voice_id": cfg.elevenlabs_voice_id,
                     "text": "hello", "model_id": "eleven_flash_v2_5",
                     "output_format": "pcm_16000"}
    assert played == [[b"pcm"]]


def test_elevenlabs_rest_requests_pcm(monkeypatch):
    vt, cfg, player, played = _tts(monkeypatch)
    monkeypatch.setattr(vt, "_elevenlabs_sdk", False)
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            return iter([b"a", b"b"])

    def fake_post(url, json=None, headers=None, **kw):
        seen.update(url=url, json=json, headers=headers)
        return Resp()

    monkeypatch.setattr(vt._requests, "post", fake_post)
    player.speak("hi")
    assert seen["url"].endswith(f"/v1/text-to-speech/{cfg.elevenlabs_voice_id}/stream"
                                "?output_format=pcm_16000")
    assert seen["json"]["model_id"] == "eleven_flash_v2_5"
    assert vt.ELEVENLABS_MODEL_ID == "eleven_flash_v2_5"


def test_transcription_does_not_need_assemblyai_sdk(monkeypatch):
    import integrations.voice_tools as vt
    assert not hasattr(vt, "_assemblyai")
    t = vt.Transcriber(vt.VoiceConfig(assemblyai_key="aai"))
    monkeypatch.setattr(t, "_upload", lambda path: "https://up")
    monkeypatch.setattr(t, "_submit", lambda url: "tid")
    monkeypatch.setattr(t, "_poll", lambda tid: "hello world")
    assert t.transcribe("x.wav") == "hello world"


# ---------------------------------------------------------------------------
# Google Workspace CLI (gws)
# ---------------------------------------------------------------------------

@pytest.fixture()
def gws(monkeypatch):
    import integrations.gworkspace_tools as gw
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(gw, "GWS_BINARY", "gws")
    monkeypatch.setattr(gw, "GWS_AVAILABLE", True)
    monkeypatch.setattr(gw.subprocess, "run", fake_run)
    return gw, calls


def _params(cmd):
    return json.loads(cmd[cmd.index("--params") + 1])


def test_gws_gmail_and_sheets_paths(gws):
    gw, calls = gws
    gw._tool_gmail_search("is:unread", 5)
    gw._tool_gmail_read("m1")
    gw._tool_gsheets_read("S1", "Sheet1!A1:B2")
    assert calls[0][:5] == ["gws", "gmail", "users", "messages", "list"]
    assert _params(calls[0]) == {"userId": "me", "q": "is:unread", "maxResults": 5}
    assert calls[1][:5] == ["gws", "gmail", "users", "messages", "get"]
    assert _params(calls[1])["id"] == "m1"
    assert calls[2][:5] == ["gws", "sheets", "spreadsheets", "values", "get"]
    assert _params(calls[2]) == {"spreadsheetId": "S1", "range": "Sheet1!A1:B2"}


def test_gws_sheets_append_helper(gws):
    gw, calls = gws
    gw._tool_gsheets_append("S1", "Sheet2!A1", '[["a", 1]]')
    c = calls[0]
    assert c[:3] == ["gws", "sheets", "+append"]
    assert c[c.index("--spreadsheet") + 1] == "S1"
    assert json.loads(c[c.index("--json-values") + 1]) == [["a", 1]]
    assert c[c.index("--range") + 1] == "Sheet2!A1"
    assert "--spreadsheet-id" not in c and "--values" not in c


def test_gws_calendar(gws):
    gw, calls = gws
    gw._tool_gcalendar_agenda(3, "Work")
    gw._tool_gcalendar_agenda(7, "primary")
    gw._tool_gcalendar_create("Sync", "2026-10-08T10:00:00Z", "2026-10-08T11:00:00Z", "notes")
    assert calls[0] == ["gws", "calendar", "+agenda", "--days", "3", "--calendar", "Work"]
    assert calls[1] == ["gws", "calendar", "+agenda", "--days", "7"]
    c = calls[2]
    assert c[:4] == ["gws", "calendar", "events", "insert"]
    assert _params(c) == {"calendarId": "primary"}
    body = json.loads(c[c.index("--json") + 1])
    assert body["summary"] == "Sync" and body["start"] == {"dateTime": "2026-10-08T10:00:00Z"}
    assert body["description"] == "notes"


def test_gws_drive(gws):
    gw, calls = gws
    gw._tool_gdrive_upload("/tmp/r.pdf", folder_id="F1", name="Report")
    gw._tool_gdrive_download("D1", "/tmp/out.bin")
    up = calls[0]
    assert up[:4] == ["gws", "drive", "files", "create"]
    assert up.count("--json") == 1 and "--params" not in up
    assert json.loads(up[up.index("--json") + 1]) == {"name": "Report", "parents": ["F1"]}
    assert up[up.index("--upload") + 1] == "/tmp/r.pdf"
    down = calls[1]
    assert _params(down) == {"fileId": "D1", "alt": "media"}
    assert down[down.index("--output") + 1] == "/tmp/out.bin"


def test_gws_generic_splits_dotted_method(gws):
    gw, calls = gws
    gw._tool_gworkspace("gmail", "users.messages.list", '{"userId": "me"}')
    gw._tool_gworkspace("chat", "spaces.messages.create", '{"parent": "spaces/x"}',
                        body='{"text": "hi"}')
    assert calls[0] == ["gws", "gmail", "users", "messages", "list",
                        "--params", '{"userId": "me"}']
    c = calls[1]
    assert c[:5] == ["gws", "chat", "spaces", "messages", "create"]
    assert json.loads(c[c.index("--json") + 1]) == {"text": "hi"}


def test_gws_install_hint(monkeypatch):
    import integrations.gworkspace_tools as gw
    monkeypatch.setattr(gw, "GWS_AVAILABLE", False)
    msg = gw._run_gws(["drive", "files", "list"])
    assert "@googleworkspace/cli" in msg and "@anthropic" not in msg


# ---------------------------------------------------------------------------
# Onyx
# ---------------------------------------------------------------------------

class FakeHTTP:
    def __init__(self, status=200, payload=None, lines=None):
        self.status_code = status
        self._payload = payload
        self._lines = lines or []
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


class RecordingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.gets = []

    def post(self, url, json=None, **kw):
        self.posts.append((url, json, kw))
        return self.responses.pop(0)

    def get(self, url, **kw):
        self.gets.append(url)
        return FakeHTTP(200, {})

    def close(self):
        pass


def test_onyx_non_stream_payload_and_session_reuse():
    from providers.onyx_provider import OnyxProvider
    p = OnyxProvider(api_key="k", base_url="http://onyx:3000/api/")
    first = FakeHTTP(200, {"answer": "Paris.", "chat_session_id": "abc",
                           "top_documents": [{"semantic_identifier": "geo.pdf"}],
                           "citation_info": [], "error_msg": None})
    second = FakeHTTP(200, {"answer": "Yes.", "chat_session_id": "abc",
                            "top_documents": [], "citation_info": []})
    p._session = RecordingSession([first, second])

    r = p.chat([{"role": "user", "content": "Capital of France?"}],
               document_sets=["wiki"])
    url, body, _ = p._session.posts[0]
    assert url == "http://onyx:3000/api/chat/send-chat-message"
    assert body == {"message": "Capital of France?", "stream": False,
                    "deep_research": False, "chat_session_info": {"persona_id": 0},
                    "internal_search_filters": {"document_set": ["wiki"]}}
    assert r.content.startswith("Paris.") and "geo.pdf" in r.content

    p.chat([{"role": "user", "content": "Capital of France?"},
            {"role": "assistant", "content": "Paris."},
            {"role": "user", "content": "Sure?"}], model="onyx/research")
    _, body2, _ = p._session.posts[1]
    assert body2["chat_session_id"] == "abc" and "chat_session_info" not in body2
    assert body2["message"] == "Sure?" and body2["deep_research"] is True


def test_onyx_error_msg_raises():
    from providers.base import ProviderError
    from providers.onyx_provider import OnyxProvider
    p = OnyxProvider()
    p._session = RecordingSession([FakeHTTP(200, {"answer": "", "error_msg": "boom"})])
    with pytest.raises(ProviderError):
        p.chat([{"role": "user", "content": "q"}])


def test_onyx_stream_parses_packets():
    from providers.onyx_provider import OnyxProvider
    lines = [
        json.dumps({"chat_session_id": "s1"}),
        json.dumps({"placement": {"turn_index": 0},
                    "obj": {"type": "message_start",
                            "final_documents": [{"semantic_identifier": "doc A"}]}}),
        json.dumps({"placement": {"turn_index": 0},
                    "obj": {"type": "message_delta", "content": "Hel"}}),
        json.dumps({"placement": {"turn_index": 0},
                    "obj": {"type": "message_delta", "content": "lo"}}),
        json.dumps({"placement": {"turn_index": 0}, "obj": {"type": "stop"}}),
    ]
    p = OnyxProvider()
    p._session = RecordingSession([FakeHTTP(200, lines=lines)])
    chunks = list(p.stream([{"role": "user", "content": "hi"}]))
    _, body, kw = p._session.posts[0]
    assert body["stream"] is True and kw["stream"] is True
    text = "".join(c["data"] for c in chunks if c["type"] == "content")
    assert text.startswith("Hello") and "doc A" in text
    assert chunks[-1]["type"] == "done"
    assert p._chat_session_id == "s1"


# ---------------------------------------------------------------------------
# G0DM0D3
# ---------------------------------------------------------------------------

def _godmode(monkeypatch, cfg=None, **kw):
    import providers.godmode_provider as gm
    monkeypatch.setattr(gm, "_godmode_settings", lambda: dict(cfg or {}))
    return gm, gm.GodmodeProvider(**{"api_key": "gm-key", **kw})


def test_godmode_defaults_disable_jailbreak_pipeline(monkeypatch):
    gm, p = _godmode(monkeypatch)
    assert p._base_url == "http://localhost:7860/v1"
    body = p._build_payload([{"role": "user", "content": "hi"}])
    assert body["godmode"] is False and body["parseltongue"] is False
    assert body["autotune"] is False and body["stm_modules"] == []
    assert "openrouter_api_key" not in body
    assert not any(k.startswith("x_godmode") for k in body)


def test_godmode_settings_and_virtual_models(monkeypatch):
    gm, p = _godmode(monkeypatch, {"autotune": True,
                                   "stm_modules": ["direct_mode", "concise_mode"],
                                   "godmode": True},
                     openrouter_api_key="sk-or-1")
    body = p._build_payload([{"role": "user", "content": "hi"}], model="consortium/smart")
    assert body["model"] == "consortium/smart"            # passed through unchanged
    assert body["autotune"] is True and body["godmode"] is True
    assert body["parseltongue"] is False
    assert body["stm_modules"] == ["direct_mode"]          # unknown module dropped
    assert body["openrouter_api_key"] == "sk-or-1"
    body = p._build_payload([{"role": "user", "content": "hi"}], model="ultraplinian/power")
    assert body["model"] == "ultraplinian/power"


def test_godmode_legacy_openrouter_key_as_api_key(monkeypatch):
    gm, p = _godmode(monkeypatch, api_key="sk-or-legacy")
    body = p._build_payload([{"role": "user", "content": "hi"}])
    assert body["openrouter_api_key"] == "sk-or-legacy"


def test_godmode_health_url(monkeypatch):
    gm, p = _godmode(monkeypatch, base_url="http://h:7860/v1")
    sess = RecordingSession([])
    p._session = sess
    assert p.is_available()
    assert sess.gets == ["http://h:7860/v1/health"]


def test_godmode_settings_default_port():
    from config.settings import DEFAULTS
    assert DEFAULTS["providers"]["godmode"]["base_url"] == "http://localhost:7860/v1"


# ---------------------------------------------------------------------------
# Scrapling
# ---------------------------------------------------------------------------

def test_scrapling_browsers_path_on_usb():
    import integrations.scrapling_tools as sc
    expected = sc.PROJECT_ROOT.parent / "bin" / "ms-playwright"
    assert sc.PLAYWRIGHT_BROWSERS_DIR == expected
    assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    assert "Adaptor" not in open(sc.__file__, encoding="utf-8").read()
    assert 'pip install "scrapling[fetchers]"' in sc.INSTALL_HINT


def test_scrapling_stealth_needs_chromium(tmp_path, monkeypatch):
    import integrations.scrapling_tools as sc
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert not sc._browsers_installed()
    (tmp_path / "ffmpeg-1011").mkdir()
    assert not sc._browsers_installed()
    (tmp_path / "chromium-1187").mkdir()
    assert sc._browsers_installed()


def test_scrapling_stealth_tool_reports_missing_browser(monkeypatch):
    import integrations.scrapling_tools as sc
    monkeypatch.setattr(sc, "_SCRAPLING_STEALTH", False)
    msg = sc._tool_scrape_stealth("https://example.com")
    assert "scrapling install" in msg and "ms-playwright" in msg
