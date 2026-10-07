"""
Desktop backend tests: the ChatBackend must drive the shared Agent and
translate its stream events into the GUI queue, including the permission
round-trip. GUI-free — skipped where tkinter is unavailable.
"""

import queue
import threading
import types

import pytest

from conftest import PROJECT_ROOT  # noqa: F401

pytest.importorskip("tkinter")  # ui.desktop imports tkinter at module load

import ui.desktop as desktop  # noqa: E402


class _FakeAgent:
    def __init__(self, boot_context=None, confirm_in_stream=False):
        self._mode = "api"
        self._policy = types.SimpleNamespace(_confirm_fn=None)
        self.loaded = None
        self.seen = None
        self._confirm_in_stream = confirm_in_stream

    def load_history(self, msgs):
        self.loaded = list(msgs)

    def stream_turn(self, user_message, provider=None, model=None):
        self.seen = {"msg": user_message, "provider": provider, "model": model}
        yield {"type": "text", "data": "hi "}
        yield {"type": "tool_start", "data": {"name": "web_fetch", "args": {}}}
        if self._confirm_in_stream:
            allowed = self._policy._confirm_fn("shell", {"command": "ls"}, "run?")
            yield {"type": "text", "data": f"allowed={allowed}"}
        yield {"type": "tool_result", "data": {"name": "web_fetch", "result": "ok"}}
        yield {"type": "done", "data": None}


def _install_fake(monkeypatch, **kw):
    import agent.agent as agentmod
    monkeypatch.setattr(agentmod, "Agent",
                        lambda boot_context=None: _FakeAgent(boot_context, **kw))


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_backend_translates_agent_events_and_steers_provider(monkeypatch):
    _install_fake(monkeypatch)
    be = desktop.ChatBackend()
    be._run_agent("conv1", [], "hello", "groq", "openai/gpt-oss-20b")
    items = _drain(be.response_queue)
    kinds = [k for k, _ in items]
    assert "chunk" in kinds and "tool" in kinds and kinds[-1] == "done"
    # provider/model were forwarded (api mode)
    assert be._agent.seen == {"msg": "hello", "provider": "groq",
                              "model": "openai/gpt-oss-20b"}
    # tool lines name the tool
    tool_lines = [d for k, d in items if k == "tool"]
    assert any("web_fetch" in t for t in tool_lines)


def test_backend_replays_history_only_on_conversation_switch(monkeypatch):
    _install_fake(monkeypatch)
    be = desktop.ChatBackend()
    prior = [{"role": "user", "content": "earlier"}]
    be._run_agent("conv1", prior, "first", "groq", "m")
    assert be._agent.loaded == prior          # loaded on first use
    be._agent.loaded = None
    be._run_agent("conv1", prior, "second", "groq", "m")
    assert be._agent.loaded is None           # same conv → no reload
    be._run_agent("conv2", [], "third", "groq", "m")
    assert be._agent.loaded == []             # switched → reload (fresh)


def test_local_choice_routes_to_local_without_model(monkeypatch):
    # "local" goes to the agent (hybrid sessions use it to pick llama-server);
    # the cloud model name is not forwarded.
    import agent.agent as agentmod

    def _make(boot_context=None):
        a = _FakeAgent(boot_context)
        a._mode = "local"
        return a
    monkeypatch.setattr(agentmod, "Agent", _make)
    be = desktop.ChatBackend()
    be._run_agent("c", [], "hello", "local", "ignored")
    assert be._agent.seen == {"msg": "hello", "provider": "local", "model": None}


def test_permission_request_marshals_through_queue(monkeypatch):
    _install_fake(monkeypatch, confirm_in_stream=True)
    be = desktop.ChatBackend()
    t = threading.Thread(target=be._run_agent,
                         args=("c", [], "hi", "groq", "m"), daemon=True)
    t.start()
    # The agent thread blocks on a ("permission", req) item; answer it yes.
    req = None
    for _ in range(200):
        try:
            k, d = be.response_queue.get(timeout=0.05)
        except queue.Empty:
            continue
        if k == "permission":
            req = d
            break
    assert req is not None, "no permission request was queued"
    req["result"]["allowed"] = True
    req["event"].set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_menu_providers_lists_keyed_cloud_providers(monkeypatch):
    monkeypatch.setattr(desktop.ChatBackend, "preloaded_keys",
                        {"groq": {"api_key": "gsk_x"}, "huggingface": {"api_key": "hf"}})
    monkeypatch.setattr(desktop.ChatBackend, "boot_context", {"mode": "api"})
    assert [p["key"] for p in desktop.menu_providers()] == ["groq", "local"]
    monkeypatch.setattr(desktop.ChatBackend, "boot_context", {"mode": "local"})
    assert [p["key"] for p in desktop.menu_providers()] == ["local", "groq"]
    assert desktop.ChatBackend.key_for("huggingface") == "hf"
    assert desktop.ChatBackend.key_for("openai") == ""
