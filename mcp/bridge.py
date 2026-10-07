"""
carry-ai/mcp/bridge.py — Connect MCP servers and hand their tools to the agent
==============================================================================

Boot (launcher._init_mcp) calls ``start_mcp``:

    1. server configs = built-in defaults (mcp/defaults.py, e.g. the desktop
       control server for this OS) overlaid by ``mcp.servers`` from
       settings.json, so a user entry with the same name replaces or
       disables ({"enabled": false}) a default
    2. connect them on a background thread (a slow server never delays the
       UI; its tools appear for the next turn)
    3. register every MCP tool in the agent's TOOL_REGISTRY as
       ``mcp__<server>__<tool>``

MCP tools count as host tools: they are hidden in sandbox mode
(agent.tools.SANDBOX_SAFE_TOOLS) and go through the permission policy.
``McpBridge.shutdown`` stops the server processes (launcher exit path;
the eject cleanup also kills any carry-ai child process).
"""

import logging
import threading
from pathlib import Path

from mcp.client import McpManager
from mcp.config import McpConfigLoader, McpServerConfig
from mcp.registry import McpToolRegistry

log = logging.getLogger("carry-ai.mcp.bridge")


class McpBridge:
    def __init__(self, configs: list[McpServerConfig], register_tool, unregister_tool=None):
        self.registry = McpToolRegistry()
        self.manager = McpManager(self.registry)
        self._configs = configs
        self._register_tool = register_tool
        self._unregister_tool = unregister_tool
        self._agent_tools: list[str] = []
        self.ready = threading.Event()
        self.results: dict[str, bool] = {}

    def start(self, background: bool = True) -> "McpBridge":
        if background:
            threading.Thread(target=self._connect_all, daemon=True, name="mcp-connect").start()
        else:
            self._connect_all()
        return self

    def _connect_all(self) -> None:
        try:
            for cfg in self._configs:
                try:
                    self.results[cfg.name] = self.manager.connect(cfg)
                except Exception as e:     # one broken server never blocks the rest
                    log.warning("MCP server '%s' failed to connect: %s", cfg.name, e)
                    self.results[cfg.name] = False
            self._publish()
            ok = [n for n, r in self.results.items() if r]
            log.info("MCP: %d/%d server(s) connected %s, %d tool(s)",
                     len(ok), len(self.results), ok, len(self._agent_tools))
        finally:
            self.ready.set()

    def _publish(self) -> None:
        """Copy the MCP registry into the agent's tool registry."""
        for tool in self.registry.list_tools():
            schema = tool.input_schema or {"type": "object", "properties": {}}
            if schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}

            def call(_full=tool.full_name, **kwargs):
                try:
                    return self.registry.dispatch(_full, kwargs)
                except RuntimeError as e:
                    return f"Error: {e}"

            label = f"[{tool.server_name}] {tool.description}".strip()
            self._register_tool(name=tool.full_name, description=label[:1024],
                                parameters=schema, execute_fn=call)
            self._agent_tools.append(tool.full_name)

    def tool_summaries(self) -> list[dict]:
        return [{"name": t.full_name, "server": t.server_name}
                for t in self.registry.list_tools()]

    def shutdown(self) -> None:
        if self._unregister_tool:
            for name in self._agent_tools:
                self._unregister_tool(name)
        self._agent_tools = []
        self.manager.shutdown()


def load_server_configs(settings_path: Path | None = None,
                        include_defaults: bool = True) -> list[McpServerConfig]:
    """Defaults for this OS overlaid by settings.json ``mcp.servers``."""
    servers: dict[str, McpServerConfig] = {}
    if include_defaults:
        try:
            from mcp.defaults import default_servers
            servers.update({c.name: c for c in default_servers()})
        except Exception as e:
            log.debug("No default MCP servers: %s", e)
    loader = McpConfigLoader(str(settings_path) if settings_path else None)
    servers.update(loader.load())
    return [c for c in servers.values() if c.enabled and c.auto_connect]


def start_mcp(settings_path: Path | None = None, background: bool = True) -> McpBridge:
    from agent.tools import TOOL_REGISTRY, register_tool

    def unregister(name: str) -> None:
        TOOL_REGISTRY.pop(name, None)

    configs = load_server_configs(settings_path)
    return McpBridge(configs, register_tool, unregister).start(background=background)
