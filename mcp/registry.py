"""
carry-ai/mcp/registry.py -- MCP Tool Registry
==============================================

Global registry tracking all tools from connected MCP servers.
Thread-safe. Tools are named: mcp__{server}__{tool}

The agent's tool system queries this registry to discover MCP tools
and dispatches calls to the correct server's client.
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable

logger = logging.getLogger("carry-ai.mcp.registry")


def _normalize(name: str) -> str:
    """Normalize a name for tool naming: lowercase, replace non-alnum with _."""
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")


@dataclass
class RegisteredTool:
    """An MCP tool registered in the global registry."""
    server_name: str           # Which MCP server provides this
    tool_name: str             # Original tool name from server
    full_name: str             # mcp__{server}__{tool}
    description: str = ""
    input_schema: dict = field(default_factory=dict)  # JSON Schema for parameters
    _call_fn: Optional[Callable] = None  # fn(arguments) -> result

    def schema(self) -> dict:
        """Return OpenAI function calling compatible schema."""
        return {
            "type": "function",
            "function": {
                "name": self.full_name,
                "description": f"[MCP:{self.server_name}] {self.description}",
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class McpToolRegistry:
    """
    Thread-safe global registry of MCP-provided tools.

    Usage:
        registry = McpToolRegistry()

        # When MCP server connects and reports its tools:
        registry.register_tools("github", [
            {"name": "get_issue", "description": "...", "inputSchema": {...}},
            {"name": "create_pr", "description": "...", "inputSchema": {...}},
        ], call_fn=github_client.call_tool)

        # Agent discovers tools:
        schemas = registry.get_all_schemas()

        # Agent dispatches a call:
        result = registry.dispatch("mcp__github__get_issue", {"number": 42})

        # Server disconnects:
        registry.unregister_server("github")
    """

    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}  # full_name -> tool
        self._servers: dict[str, list[str]] = {}       # server -> [full_names]
        self._lock = threading.Lock()

    def register_tools(self, server_name: str, tool_defs: list[dict],
                       call_fn: Callable = None):
        """
        Register tools from an MCP server.

        Args:
            server_name: Server identifier
            tool_defs: List of tool definitions from tools/list response
                       Each: {"name": str, "description": str, "inputSchema": dict}
            call_fn: Callable(tool_name, arguments) -> result for this server
        """
        norm_server = _normalize(server_name)
        registered = []

        with self._lock:
            # Remove existing tools for this server first (reconnect scenario)
            if server_name in self._servers:
                for old_name in self._servers[server_name]:
                    self._tools.pop(old_name, None)

            for tdef in tool_defs:
                tool_name = tdef.get("name", "")
                norm_tool = _normalize(tool_name)
                full_name = f"mcp__{norm_server}__{norm_tool}"

                tool = RegisteredTool(
                    server_name=server_name,
                    tool_name=tool_name,
                    full_name=full_name,
                    description=tdef.get("description", ""),
                    input_schema=tdef.get("inputSchema", tdef.get("input_schema", {})),
                    _call_fn=call_fn,
                )
                self._tools[full_name] = tool
                registered.append(full_name)

            self._servers[server_name] = registered

        logger.info("Registered %d tools from MCP server '%s': %s",
                     len(registered), server_name,
                     [t.split("__")[-1] for t in registered[:5]])

    def unregister_server(self, server_name: str) -> int:
        """Remove all tools from a server. Returns count removed."""
        with self._lock:
            tool_names = self._servers.pop(server_name, [])
            for name in tool_names:
                self._tools.pop(name, None)
        if tool_names:
            logger.info("Unregistered %d tools from '%s'", len(tool_names), server_name)
        return len(tool_names)

    def get_tool(self, full_name: str) -> Optional[RegisteredTool]:
        """Look up a tool by its full name."""
        with self._lock:
            return self._tools.get(full_name)

    def dispatch(self, full_name: str, arguments: dict) -> str:
        """
        Route a tool call to the correct MCP server.

        Returns the tool result as a string.
        Raises RuntimeError if tool not found or call fails.
        """
        with self._lock:
            tool = self._tools.get(full_name)

        if not tool:
            raise RuntimeError(f"MCP tool not found: {full_name}")

        if not tool._call_fn:
            raise RuntimeError(f"MCP tool '{full_name}' has no call handler")

        try:
            result = tool._call_fn(tool.tool_name, arguments)
            if isinstance(result, dict):
                return result.get("content", [{}])[0].get("text", str(result))
            return str(result)
        except Exception as e:
            logger.error("MCP tool call failed: %s(%s) -> %s", full_name, arguments, e)
            raise RuntimeError(f"MCP tool error: {e}") from e

    def get_all_schemas(self) -> list[dict]:
        """Return OpenAI-compatible tool schemas for all registered MCP tools."""
        with self._lock:
            return [tool.schema() for tool in self._tools.values()]

    def list_tools(self, server_name: str = None) -> list[RegisteredTool]:
        """List all tools, optionally filtered by server."""
        with self._lock:
            if server_name:
                names = self._servers.get(server_name, [])
                return [self._tools[n] for n in names if n in self._tools]
            return list(self._tools.values())

    def list_servers(self) -> list[str]:
        """List all connected server names."""
        with self._lock:
            return list(self._servers.keys())

    @property
    def tool_count(self) -> int:
        with self._lock:
            return len(self._tools)

    @property
    def server_count(self) -> int:
        with self._lock:
            return len(self._servers)
