"""
carry-ai/mcp/client.py -- MCP Client Transport Layer
=====================================================

Manages connections to MCP servers via stdio (subprocess) or HTTP/SSE.
Each connected server exposes tools that the agent can call.

Stdio protocol:
    - Launch server as subprocess
    - Send JSON-RPC messages over stdin
    - Read JSON-RPC responses from stdout
    - stderr for logging only

HTTP/SSE protocol:
    - POST JSON-RPC to server URL
    - SSE for server-initiated notifications

Connection lifecycle:
    connect() -> initialize handshake -> list tools -> ready
    call_tool(name, args) -> result
    disconnect() -> shutdown notification -> kill process
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import requests as _requests
except ImportError:
    _requests = None

from mcp.config import McpServerConfig
from mcp.registry import McpToolRegistry

logger = logging.getLogger("carry-ai.mcp.client")

# JSON-RPC helpers
_MSG_ID = 0
_MSG_LOCK = threading.Lock()


def _next_id() -> int:
    global _MSG_ID
    with _MSG_LOCK:
        _MSG_ID += 1
        return _MSG_ID


def _jsonrpc_request(method: str, params: dict = None, id: int = None) -> str:
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if id is not None:
        msg["id"] = id
    return json.dumps(msg)


# ---------------------------------------------------------------------------
# Stdio Transport
# ---------------------------------------------------------------------------

class StdioTransport:
    """Communicate with an MCP server via subprocess stdin/stdout."""

    def __init__(self, config: McpServerConfig):
        self.config = config
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Launch the MCP server subprocess."""
        resolved = self.config.resolve_env()
        cmd = [resolved.command] + resolved.args

        env = dict(os.environ)
        env.update(resolved.env)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,
            )
            logger.info("Started MCP server '%s': PID %d, cmd=%s",
                        self.config.name, self._proc.pid, cmd)
            return True
        except FileNotFoundError:
            logger.error("MCP server command not found: %s", cmd)
            return False
        except Exception as e:
            logger.error("Failed to start MCP server '%s': %s", self.config.name, e)
            return False

    def send(self, method: str, params: dict = None) -> dict:
        """Send a JSON-RPC request and wait for response."""
        if not self._proc or self._proc.poll() is not None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not running")

        msg_id = _next_id()
        req = _jsonrpc_request(method, params, id=msg_id) + "\n"

        with self._lock:
            try:
                self._proc.stdin.write(req.encode("utf-8"))
                self._proc.stdin.flush()

                # Read until we get the response matching our request id.
                # Servers may interleave notifications (no "id") or
                # server-initiated requests on stdout — skip those.
                while True:
                    line = self._proc.stdout.readline()
                    if not line:
                        raise RuntimeError("MCP server closed stdout")

                    try:
                        resp = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        logger.debug("Skipping non-JSON stdout line from '%s'",
                                     self.config.name)
                        continue

                    if resp.get("id") != msg_id or "method" in resp:
                        logger.debug("Skipping non-matching message from '%s': %s",
                                     self.config.name, resp.get("method", resp.get("id")))
                        continue

                    if "error" in resp:
                        err = resp["error"]
                        raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")

                    return resp.get("result", {})

            except (BrokenPipeError, OSError) as e:
                raise RuntimeError(f"MCP transport error: {e}") from e

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        """Terminate the server subprocess."""
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            logger.info("Stopped MCP server '%s'", self.config.name)


# ---------------------------------------------------------------------------
# HTTP/SSE Transport
# ---------------------------------------------------------------------------

class HttpTransport:
    """Communicate with an MCP server via HTTP POST (JSON-RPC)."""

    def __init__(self, config: McpServerConfig):
        self.config = config
        self._session = None

    def start(self) -> bool:
        if _requests is None:
            logger.error("requests library required for HTTP MCP transport")
            return False
        resolved = self.config.resolve_env()
        self._session = _requests.Session()
        self._session.headers.update(resolved.headers)
        self._session.headers["Content-Type"] = "application/json"
        logger.info("HTTP transport ready for '%s': %s", self.config.name, resolved.url)
        return True

    def send(self, method: str, params: dict = None) -> dict:
        if not self._session:
            raise RuntimeError("HTTP transport not started")

        resolved = self.config.resolve_env()
        msg_id = _next_id()
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": msg_id,
        }
        if params is not None:
            payload["params"] = params

        try:
            resp = self._session.post(
                resolved.url,
                json=payload,
                timeout=self.config.timeout_ms / 1000,
            )
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                err = data["error"]
                raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")

            return data.get("result", {})

        except _requests.RequestException as e:
            raise RuntimeError(f"MCP HTTP error: {e}") from e

    def is_alive(self) -> bool:
        return self._session is not None

    def stop(self):
        if self._session:
            self._session.close()
            self._session = None


# ---------------------------------------------------------------------------
# MCP Client
# ---------------------------------------------------------------------------

class McpClient:
    """
    Manages a connection to a single MCP server.

    Usage:
        client = McpClient(config, registry)
        if client.connect():
            result = client.call_tool("get_issue", {"number": 42})
        client.disconnect()
    """

    MAX_RETRIES = 3
    RETRY_DELAY = 2.0

    def __init__(self, config: McpServerConfig, registry: McpToolRegistry):
        self.config = config
        self.registry = registry
        self._transport = None
        self._connected = False
        self._server_info: dict = {}
        self._tools: list[dict] = []

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_connected(self) -> bool:
        return self._connected and self._transport and self._transport.is_alive()

    def connect(self) -> bool:
        """
        Connect to the MCP server:
        1. Start transport (subprocess or HTTP)
        2. Send initialize handshake
        3. List available tools
        4. Register tools in the global registry
        """
        # Create transport
        if self.config.transport == "stdio":
            self._transport = StdioTransport(self.config)
        elif self.config.transport in ("sse", "http"):
            self._transport = HttpTransport(self.config)
        else:
            logger.error("Unsupported transport: %s", self.config.transport)
            return False

        if not self._transport.start():
            return False

        # Initialize handshake
        try:
            result = self._transport.send("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "carry-ai", "version": "1.0.0"},
            })
            self._server_info = result.get("serverInfo", {})
            logger.info("MCP '%s' initialized: %s", self.name,
                        self._server_info.get("name", "unknown"))

            # Send initialized notification (no response expected for notifications)
            try:
                self._transport.send("notifications/initialized")
            except Exception:
                pass  # Some servers don't respond to notifications

        except Exception as e:
            logger.error("MCP '%s' handshake failed: %s", self.name, e)
            self._transport.stop()
            return False

        # List tools
        try:
            result = self._transport.send("tools/list")
            self._tools = result.get("tools", [])
            logger.info("MCP '%s' provides %d tools", self.name, len(self._tools))
        except Exception as e:
            logger.warning("MCP '%s' tools/list failed: %s", self.name, e)
            self._tools = []

        # Register tools in global registry
        if self._tools:
            self.registry.register_tools(
                self.name, self._tools,
                call_fn=self.call_tool,
            )

        self._connected = True
        return True

    def call_tool(self, tool_name: str, arguments: dict = None) -> dict:
        """
        Call a tool on this MCP server.

        Args:
            tool_name: Tool name (as reported by the server, not the full mcp__ name)
            arguments: Tool arguments dict

        Returns dict with result content.
        """
        if not self.is_connected:
            raise RuntimeError(f"MCP server '{self.name}' not connected")

        for attempt in range(self.MAX_RETRIES):
            try:
                result = self._transport.send("tools/call", {
                    "name": tool_name,
                    "arguments": arguments or {},
                })
                return result

            except RuntimeError as e:
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning("MCP tool call retry %d/%d for %s.%s: %s",
                                   attempt + 1, self.MAX_RETRIES, self.name, tool_name, e)
                    time.sleep(self.RETRY_DELAY * (attempt + 1))
                else:
                    raise

    def disconnect(self):
        """Disconnect from the MCP server and unregister tools."""
        self.registry.unregister_server(self.name)
        if self._transport:
            self._transport.stop()
            self._transport = None
        self._connected = False
        self._tools = []
        logger.info("Disconnected from MCP server '%s'", self.name)

    def get_status(self) -> dict:
        return {
            "name": self.name,
            "connected": self.is_connected,
            "transport": self.config.transport,
            "server_info": self._server_info,
            "tool_count": len(self._tools),
            "tools": [t.get("name", "") for t in self._tools],
        }


# ---------------------------------------------------------------------------
# MCP Manager (orchestrates multiple clients)
# ---------------------------------------------------------------------------

class McpManager:
    """
    Manages all MCP server connections.

    Usage:
        from mcp.config import McpConfigLoader
        from mcp.registry import McpToolRegistry

        loader = McpConfigLoader()
        loader.load()
        registry = McpToolRegistry()

        mgr = McpManager(registry)
        mgr.connect_all(loader.get_auto_connect())

        # Tools are now available in registry
        schemas = registry.get_all_schemas()

        mgr.shutdown()
    """

    def __init__(self, registry: McpToolRegistry):
        self.registry = registry
        self._clients: dict[str, McpClient] = {}

    def connect(self, config: McpServerConfig) -> bool:
        """Connect to a single MCP server."""
        if config.name in self._clients:
            self._clients[config.name].disconnect()

        client = McpClient(config, self.registry)
        if client.connect():
            self._clients[config.name] = client
            return True
        return False

    def connect_all(self, configs: list[McpServerConfig]) -> dict:
        """Connect to multiple servers. Returns {name: success_bool}."""
        results = {}
        for cfg in configs:
            results[cfg.name] = self.connect(cfg)
        return results

    def disconnect(self, name: str):
        """Disconnect a single server."""
        client = self._clients.pop(name, None)
        if client:
            client.disconnect()

    def shutdown(self):
        """Disconnect all servers."""
        for name in list(self._clients.keys()):
            self.disconnect(name)
        logger.info("MCP manager shut down")

    def get_client(self, name: str) -> Optional[McpClient]:
        return self._clients.get(name)

    def list_connections(self) -> list[dict]:
        return [c.get_status() for c in self._clients.values()]

    @property
    def connected_count(self) -> int:
        return sum(1 for c in self._clients.values() if c.is_connected)
