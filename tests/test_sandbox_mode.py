"""
Sandbox mode switch (chat window / web UI / /sandbox): which tools the model
sees and may run, and run_python's read-only /host folders.
"""

import json
import types

import pytest

from conftest import PROJECT_ROOT  # noqa: F401
from agent import tools as T
from agent.agent import Agent


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    calls = []
    import integrations.python_sandbox as ps
    monkeypatch.setattr(ps, "set_host_access", lambda on: calls.append(on))
    a = Agent(boot_context={"mode": "api", "memory_path": str(tmp_path / "m.db"),
                            "permission_mode": "yolo"})
    a.host_calls = calls
    return a


def test_sandboxed_tool_list_is_an_allow_list():
    names = {d["function"]["name"] for d in T.get_tool_definitions(sandboxed=True)}
    assert names <= T.SANDBOX_SAFE_TOOLS
    assert "shell" not in names and "write_file" not in names and "click" not in names
    assert "web_fetch" in names
    every = {d["function"]["name"] for d in T.get_tool_definitions()}
    assert "shell" in every


def test_new_tools_are_host_tools_unless_marked(monkeypatch):
    monkeypatch.setattr(T, "SANDBOX_SAFE_TOOLS", set(T.SANDBOX_SAFE_TOOLS))
    T.register_tool("x_host", "d", {"type": "object", "properties": {}}, lambda: "")
    T.register_tool("x_safe", "d", {"type": "object", "properties": {}}, lambda: "",
                    sandbox_safe=True)
    try:
        assert not T.is_sandbox_safe("x_host") and T.is_sandbox_safe("x_safe")
    finally:
        T.TOOL_REGISTRY.pop("x_host"), T.TOOL_REGISTRY.pop("x_safe")


def test_switch_gates_tools_and_host_folders(agent):
    agent.set_sandboxed(True)
    ok, why = agent._check_tool("shell", {"command": "ls"})
    assert not ok and "sandbox mode is on" in why
    assert agent._check_tool("run_python", {"code": "1"}) == (True, "")
    agent.set_sandboxed(False)
    assert agent._check_tool("shell", {"command": "ls"}) == (True, "")
    assert agent.host_calls[-2:] == [False, True]       # /host off, then on
    assert agent.get_status()["sandboxed"] is False


def test_sandboxed_turn_hides_and_refuses_host_tools(agent, monkeypatch):
    ran = []
    monkeypatch.setitem(T.TOOL_REGISTRY, "shell", T.Tool(
        "shell", "d", {"type": "object", "properties": {}}, lambda **kw: ran.append(kw) or "x"))
    seen_tools = []

    class Runner:
        turns = 0

        def stream(self, messages, tools=None, **kw):
            seen_tools.append({t["function"]["name"] for t in tools})
            Runner.turns += 1
            if Runner.turns == 1:     # the model tries the shell anyway
                yield {"type": "tool_call", "data": [{"id": "c1", "function": {
                    "name": "shell", "arguments": json.dumps({"command": "dir"})}}]}
            else:
                yield {"type": "content", "data": "ok"}
            yield {"type": "done"}

    agent._api_runner = Runner()
    agent.set_sandboxed(True)
    events = list(agent.stream_turn("list my files"))
    assert "shell" not in seen_tools[0]
    assert ran == []
    result = next(e for e in events if e["type"] == "tool_result")["data"]["result"]
    assert "sandbox mode is on" in result


def test_slash_command(agent, capsys):
    agent._handle_slash_command("/sandbox on")
    assert agent.sandboxed and "on" in capsys.readouterr().out
    agent._handle_slash_command("/sandbox off")
    assert not agent.sandboxed and "host access" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run_python /host mounts
# ---------------------------------------------------------------------------

def test_host_folders_default_and_configured(monkeypatch, tmp_path):
    import integrations.python_sandbox as ps
    home = tmp_path / "home"
    for d in ("Desktop", "Documents"):
        (home / d).mkdir(parents=True)
    monkeypatch.setattr(ps.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(ps, "sandbox_settings", lambda: {})
    assert [p.name for p in ps.host_folders()] == ["Desktop", "Documents"]  # no Downloads dir
    (tmp_path / "proj").mkdir()
    monkeypatch.setattr(ps, "sandbox_settings", lambda: {"host_folders": [str(tmp_path / "proj"),
                                                                          str(tmp_path / "nope")]})
    assert [p.name for p in ps.host_folders()] == ["proj"]


def test_host_mounts_follow_the_switch(monkeypatch, tmp_path):
    import integrations.python_sandbox as ps
    mounts = []

    class MountDir:
        def __init__(self, **kw):
            self.kw = kw
            mounts.append(kw)

        def close(self):
            self.kw["closed"] = True

    monkeypatch.setattr(ps, "_monty", types.SimpleNamespace(MountDir=MountDir))
    monkeypatch.setattr(ps, "host_folders", lambda: [tmp_path])
    sb = ps.PythonSandbox.__new__(ps.PythonSandbox)
    sb._mount, sb._host_mounts, sb.host_access = "work", [], False
    import threading
    sb._lock = threading.Lock()

    assert sb._mounts() == ["work"]
    sb.set_host_access(True)
    got = sb._mounts()
    assert got[0] == "work" and got[1].kw == {"host_path": tmp_path,
                                              "virtual_path": f"/host/{tmp_path.name}",
                                              "mode": "read-only"}
    sb.set_host_access(False)
    assert sb._mounts() == ["work"] and mounts[0].get("closed")


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

def test_web_ui_sandbox_route(tmp_path):
    pytest.importorskip("flask")
    from ui.app import create_app
    app = create_app(config={"ui_token": "t", "mode": "api",
                             "memory_path": str(tmp_path / "m.db")})
    c = app.test_client()
    c.get("/?t=t")
    assert c.post("/api/sandbox", json={"sandboxed": "yes"}).status_code == 400
    assert c.post("/api/sandbox", json={"sandboxed": True}).get_json() == {"sandboxed": True}
    assert c.get("/api/status").get_json()["agent"]["sandboxed"] is True
    assert c.post("/api/sandbox", json={"sandboxed": False}).get_json() == {"sandboxed": False}
