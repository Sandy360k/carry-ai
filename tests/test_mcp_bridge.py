"""
MCP wiring end to end: a real stdio MCP server subprocess (stdlib, written
below) → McpClient → McpBridge → the agent's TOOL_REGISTRY. Also the
stdio timeout, notifications, defaults overlay and sandbox gating.
"""

import json
import sys
import textwrap

import pytest

from conftest import PROJECT_ROOT  # noqa: F401
from mcp.bridge import McpBridge, load_server_configs
from mcp.config import McpServerConfig

SERVER = textwrap.dedent(r'''
    import json, sys, time
    seen_notification = False
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:                      # notification: never answered
            seen_notification = msg["method"] == "notifications/initialized"
            continue
        m, rid = msg["method"], msg["id"]
        print("noise on stderr", file=sys.stderr, flush=True)
        if m == "initialize":
            result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake"},
                      "capabilities": {"tools": {}}}
        elif m == "tools/list":
            result = {"tools": [
                {"name": "Echo-Tool", "description": "echo text",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "slow", "description": "never returns in time",
                 "inputSchema": {"type": "object", "properties": {}}}]}
        elif m == "tools/call":
            if msg["params"]["name"] == "slow":
                time.sleep(5)
            text = f"{msg['params']['arguments'].get('text')} (notified={seen_notification})"
            result = {"content": [{"type": "text", "text": text}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601, "message": m}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
''')


@pytest.fixture()
def server_cfg(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    return McpServerConfig(name="fake", command=sys.executable, args=["-u", str(script)],
                           timeout_ms=2000)


def test_stdio_server_tools_reach_the_agent(server_cfg):
    registry = {}

    def register(name, description, parameters, execute_fn):
        registry[name] = (description, parameters, execute_fn)

    bridge = McpBridge([server_cfg], register, lambda n: registry.pop(n, None))
    bridge.start(background=False)
    try:
        assert bridge.results == {"fake": True}
        assert set(registry) == {"mcp__fake__echo_tool", "mcp__fake__slow"}
        desc, params, call = registry["mcp__fake__echo_tool"]
        assert desc.startswith("[fake] echo text")
        assert params["properties"]["text"]["type"] == "string"
        # the initialized notification arrived without an id (no hang)
        assert call(text="hi") == "hi (notified=True)"
        # a server that doesn't answer in time fails the call, not the agent
        assert "did not answer" in registry["mcp__fake__slow"][2]()
    finally:
        bridge.shutdown()
    assert registry == {}


def test_broken_server_does_not_block_others(server_cfg, tmp_path):
    bad = McpServerConfig(name="missing", command=str(tmp_path / "nope"))
    seen = []
    bridge = McpBridge([bad, server_cfg], lambda **kw: seen.append(kw["name"]))
    bridge.start(background=False)
    try:
        assert bridge.results == {"missing": False, "fake": True}
        assert "mcp__fake__echo_tool" in seen
    finally:
        bridge.shutdown()


def test_settings_overlay_and_disable_defaults(monkeypatch, tmp_path):
    import mcp.defaults as defaults
    monkeypatch.setattr(defaults, "default_servers", lambda: [
        McpServerConfig(name="desktop", command="py"),
        McpServerConfig(name="other", command="py")])
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"mcp": {"servers": {
        "desktop": {"enabled": False},
        "mine": {"command": "node", "args": ["srv.js"]}}}}))
    names = [c.name for c in load_server_configs(settings)]
    assert sorted(names) == ["mine", "other"]


def test_mcp_tools_are_hidden_in_sandbox_mode():
    from agent import tools as T
    T.register_tool("mcp__x__y", "d", {"type": "object", "properties": {}}, lambda: "")
    try:
        assert not T.is_sandbox_safe("mcp__x__y")
        assert "mcp__x__y" not in {d["function"]["name"]
                                   for d in T.get_tool_definitions(sandboxed=True)}
    finally:
        T.TOOL_REGISTRY.pop("mcp__x__y")


def test_dangerous_commands_are_checked_on_any_tool():
    from agent.agent import PermissionPolicy
    safe = PermissionPolicy(mode="safe")
    ok, why = safe.check("mcp__windows__powershell", {"command": "rm -rf /"})
    assert not ok and "Blocked" in why
    assert safe.check("mcp__desktop__start_process", {"cmd": ["ls", "-la"]}) == (True, "")
    asked = []
    ask = PermissionPolicy(mode="ask", confirm_fn=lambda *a: asked.append(a) or False)
    assert ask.check("mcp__linux__run", {"script": "rm -rf ~"})[0] is False and asked
    # run_python code never runs on the host
    assert safe.check("run_python", {"code": "s = 'rm -rf /'"}) == (True, "")


def test_call_hook_sees_every_call(server_cfg):
    seen = []
    reg = {}
    bridge = McpBridge([server_cfg], lambda **kw: reg.__setitem__(kw["name"], kw),
                       on_call=lambda srv, tool, args: seen.append((srv, tool, args)))
    bridge.start(background=False)
    try:
        reg["mcp__fake__echo_tool"]["execute_fn"](text="x")
        assert seen == [("fake", "Echo-Tool", {"text": "x"})]
    finally:
        bridge.shutdown()
