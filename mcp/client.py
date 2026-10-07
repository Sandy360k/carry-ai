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

Streamable HTTP (MCP 2026-07-28):
    - POST JSON-RPC to one server URL; the response is application/json
      or text/event-stream (both parsed here)
    - Stateless: no initialize handshake and no Mcp-Session-Id. Each
      request carries its protocol version and client identity in `_meta`
      (io.modelcontextprotocol/clientInfo), and the MCP-Protocol-Version /
      Mcp-Method / Mcp-Name headers gateways route on.
    - Legacy fallback: if the stateless call fails, reconnect with the old
      initialize handshake (protocol 2024-11-05) that 2025-era servers and
      the deprecated HTTP+SSE transport expect.

Connection lifecycle:
    connect() -> (HTTP: stateless tools/list, else initialize) -> ready
    call_tool(name, args) -> result
    disconnect() -> kill transport, unregister tools
"""

import json
import logging
import os
import queue
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
    """Communicate with an MCP server via subprocess stdin/stdout.

    stdout is read on a background thread into a queue, so a server that
    stops answering times out (config.timeout_ms) instead of hanging the
    agent; stderr is drained to the debug log so a chatty server can't
    fill the pipe and block. No console window on Windows.
    """

    def __init__(self, config: McpServerConfig):
        self.config = config
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._lines: "queue.Queue[bytes | None]" = queue.Queue()

    def start(self) -> bool:
        """Launch the MCP server subprocess."""
        resolved = self.config.resolve_env()
        cmd = [resolved.command] + resolved.args

        env = dict(os.environ)
        env.update(resolved.env)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,
                creationflags=flags,
            )
        except FileNotFoundError:
            logger.error("MCP server command not found: %s", cmd)
            return False
        except Exception as e:
            logger.error("Failed to start MCP server '%s': %s", self.config.name, e)
            return False

        threading.Thread(target=self._pump_stdout, args=(self._proc,), daemon=True,
                         name=f"mcp-{self.config.name}-out").start()
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True,
                         name=f"mcp-{self.config.name}-err").start()
        logger.info("Started MCP server '%s': PID %d, cmd=%s",
                    self.config.name, self._proc.pid, cmd)
        return True

    def _pump_stdout(self, proc) -> None:
        try:
            for line in iter(proc.stdout.readline, b""):
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)                 # EOF

    def notify(self, method: str, params: dict = None) -> None:
        """Send a JSON-RPC notification (no id, no reply expected)."""
        if not self._proc or self._proc.poll() is not None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not running")
        with self._lock:
            try:
                self._proc.stdin.write((_jsonrpc_request(method, params) + "\n").encode("utf-8"))
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise RuntimeError(f"MCP transport error: {e}") from e

    def _drain_stderr(self, proc) -> None:
        try:
            for line in iter(proc.stderr.readline, b""):
                logger.debug("[%s] %s", self.config.name,
                             line.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass

    def send(self, method: str, params: dict = None, name: str = None) -> dict:
        """Send a JSON-RPC request and wait for response.

        ``name`` is accepted for a uniform transport interface (it becomes
        the Mcp-Name header on Streamable HTTP) and ignored over stdio.
        """
        if not self._proc or self._proc.poll() is not None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not running")

        msg_id = _next_id()
        req = _jsonrpc_request(method, params, id=msg_id) + "\n"
        deadline = time.monotonic() + max(1.0, self.config.timeout_ms / 1000)

        with self._lock:
            try:
                self._proc.stdin.write(req.encode("utf-8"))
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise RuntimeError(f"MCP transport error: {e}") from e

            # Read until we get the response matching our request id.
            # Servers may interleave notifications (no "id") or
            # server-initiated requests on stdout — skip those.
            while True:
                try:
                    line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    raise RuntimeError(
                        f"MCP server '{self.config.name}' did not answer {method} "
                        f"within {self.config.timeout_ms / 1000:.0f} s") from None
                if line is None:
                    self._lines.put(None)      # stay at EOF for later calls
                    raise RuntimeError("MCP server closed stdout")

                try:
                    resp = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
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

PROTOCOL_VERSION = "2026-07-28"       # Streamable HTTP, stateless requests
LEGACY_PROTOCOL_VERSION = "2024-11-05"  # initialize-handshake fallback
CLIENT_INFO = {"name": "carry-ai", "version": "1.0.0"}
_CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"


def _parse_streamable_body(resp) -> dict:
    """Parse a Streamable HTTP response, which is JSON or text/event-stream.

    An ``text/event-stream`` body carries one JSON-RPC message per ``data:``
    frame; the last one with a ``result``/``error`` is the reply.
    """
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in content_type:
        return resp.json()

    last = None
    for raw in resp.text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            msg = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            last = msg
    if last is None:
        raise RuntimeError("No JSON-RPC result in event-stream response")
    return last


class HttpTransport:
    """Communicate with an MCP server over Streamable HTTP (JSON-RPC).

    Defaults to the 2026-07-28 stateless mode: no ``initialize`` handshake,
    protocol version / client identity carried in each request's ``_meta``,
    and the ``Mcp-Method`` / ``Mcp-Name`` / ``MCP-Protocol-Version`` headers
    gateways route on. ``stateless=False`` falls back to the legacy
    initialize-handshake JSON-RPC used by 2025-era servers. Either way the
    response may be ``application/json`` or ``text/event-stream``.
    """

    def __init__(self, config: McpServerConfig, stateless: bool = True):
        self.config = config
        self.stateless = stateless
        self._session = None

    def start(self) -> bool:
        if _requests is None:
            logger.error("requests library required for HTTP MCP transport")
            return False
        resolved = self.config.resolve_env()
        self._session = _requests.Session()
        self._session.headers.update(resolved.headers)
        self._session.headers["Content-Type"] = "application/json"
        self._session.headers["Accept"] = "application/json, text/event-stream"
        logger.info("Streamable HTTP transport ready for '%s': %s (%s)",
                    self.config.name, resolved.url,
                    "stateless 2026-07-28" if self.stateless else "legacy handshake")
        return True

    def send(self, method: str, params: dict = None, name: str = None) -> dict:
        if not self._session:
            raise RuntimeError("HTTP transport not started")

        resolved = self.config.resolve_env()
        msg_id = _next_id()
        payload = {"jsonrpc": "2.0", "method": method, "id": msg_id}
        if params is not None:
            payload["params"] = params

        headers = None
        if self.stateless:
            # Stateless 2026-07-28: identity in _meta, routing in headers.
            payload["_meta"] = {
                _CLIENT_INFO_META_KEY: {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientInfo": CLIENT_INFO,
                    "capabilities": {},
                }
            }
            headers = {
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": method,
                "Mcp-Name": name or method,
            }

        try:
            resp = self._session.post(
                resolved.url,
                json=payload,
                headers=headers,
                timeout=self.config.timeout_ms / 1000,
            )
            resp.raise_for_status()
            data = _parse_streamable_body(resp)

            if "error" in data:
                err = data["error"]
                raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")

            return data.get("result", {})

        except _requests.RequestException as e:
            raise RuntimeError(f"MCP HTTP error: {e}") from e

    def notify(self, method: str, params: dict = None) -> None:
        """POST a JSON-RPC notification; the server answers 202 with no body."""
        if not self._session:
            raise RuntimeError("HTTP transport not started")
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._session.post(self.config.resolve_env().url, json=payload,
                               timeout=self.config.timeout_ms / 1000)
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

    def __init__(self, config: McpServerConfig, registry: McpToolRegistry):
        self.config = config
        self.registry = registry
        self._transport = None
        self._connected = False
        self._server_info: dict = {}
        self._tools: list[dict] = []
        self._protocol: str | None = None

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
        # HTTP-family transports try the 2026-07-28 stateless path first
        # (no handshake); on failure they fall back to the legacy
        # initialize handshake that 2025-era servers expect.
        if self.config.transport in ("sse", "http", "streamable-http"):
            if self.config.transport == "sse":
                logger.info("MCP '%s': 'sse' transport is deprecated; using "
                            "Streamable HTTP against the same URL.", self.name)
            if self._connect_http():
                self._connected = True
                return True
            return False

        if self.config.transport == "stdio":
            self._transport = StdioTransport(self.config)
        else:
            logger.error("Unsupported transport: %s", self.config.transport)
            return False

        if not self._transport.start():
            return False

        if not self._handshake():          # stdio keeps the initialize handshake
            self._transport.stop()
            return False

        self._list_and_register()
        self._connected = True
        return True

    def _connect_http(self) -> bool:
        """Connect over Streamable HTTP, stateless first then legacy."""
        # Attempt 1: stateless 2026-07-28 — a plain tools/list, no handshake.
        self._transport = HttpTransport(self.config, stateless=True)
        if not self._transport.start():
            return False
        try:
            self._tools = self._transport.send("tools/list").get("tools", [])
            self._protocol = PROTOCOL_VERSION
            logger.info("MCP '%s' connected (stateless %s): %d tools",
                        self.name, PROTOCOL_VERSION, len(self._tools))
            self._register()
            return True
        except Exception as e:
            logger.info("MCP '%s' stateless connect failed (%s); trying the "
                        "legacy initialize handshake.", self.name, e)

        # Attempt 2: legacy handshake.
        self._transport.stop()
        self._transport = HttpTransport(self.config, stateless=False)
        if not self._transport.start():
            return False
        if not self._handshake():
            self._transport.stop()
            return False
        self._list_and_register()
        return True

    def _handshake(self) -> bool:
        """Legacy initialize handshake (stdio, and the HTTP fallback)."""
        try:
            result = self._transport.send("initialize", {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            })
            self._server_info = result.get("serverInfo", {})
            self._protocol = result.get("protocolVersion", LEGACY_PROTOCOL_VERSION)
            logger.info("MCP '%s' initialized: %s (protocol %s)", self.name,
                        self._server_info.get("name", "unknown"), self._protocol)
            try:
                # A notification: no id, and no reply to wait for
                self._transport.notify("notifications/initialized")
            except Exception as e:
                logger.debug("MCP '%s' initialized-notification failed: %s", self.name, e)
            return True
        except Exception as e:
            logger.error("MCP '%s' handshake failed: %s", self.name, e)
            return False

    def _list_and_register(self) -> None:
        try:
            self._tools = self._transport.send("tools/list").get("tools", [])
            logger.info("MCP '%s' provides %d tools", self.name, len(self._tools))
        except Exception as e:
            logger.warning("MCP '%s' tools/list failed: %s", self.name, e)
            self._tools = []
        self._register()

    def _register(self) -> None:
        """Register this server's tools in the global registry."""
        if self._tools:
            self.registry.register_tools(
                self.name, self._tools,
                call_fn=self.call_tool,
            )

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

        # One attempt only: tool calls are actions (a click, a keypress, a
        # command), and repeating one after a timeout could do it twice.
        # name= sets the Mcp-Name header on Streamable HTTP so a gateway can
        # route/meter per tool (ignored by stdio).
        return self._transport.send(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            name=tool_name,
        )

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
            "protocol": self._protocol,
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
