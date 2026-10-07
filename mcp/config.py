"""
carry-ai/mcp/config.py -- MCP Server Configuration
====================================================

Loads, validates, and manages MCP server configurations.
Servers can be defined in config/settings.json under "mcp.servers"
or added dynamically via the web UI / CLI.

Config format per server:
    {
        "transport": "stdio" | "sse" | "http",
        "command": "uvx",              # stdio only
        "args": ["mcp-server-github"], # stdio only
        "env": {"TOKEN": "$TOKEN"},    # stdio env vars
        "url": "http://...",           # sse/http only
        "headers": {},                 # sse/http headers
        "timeout_ms": 30000,           # call timeout
        "auto_connect": true           # connect on boot
    }
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("carry-ai.mcp.config")

_ENV_VAR_RE = re.compile(r"\$([A-Z_][A-Z0-9_]*)")


@dataclass
class McpServerConfig:
    """Validated MCP server configuration."""
    name: str
    transport: str = "stdio"           # stdio | sse | http
    command: str = ""                  # Executable (stdio)
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    url: str = ""                      # Endpoint URL (sse/http)
    headers: dict = field(default_factory=dict)
    timeout_ms: int = 30000
    auto_connect: bool = True
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "url": self.url,
            "headers": self.headers,
            "timeout_ms": self.timeout_ms,
            "auto_connect": self.auto_connect,
            "enabled": self.enabled,
        }

    def validate(self) -> list:
        """Return list of validation errors (empty = valid)."""
        errors = []
        if self.transport not in ("stdio", "sse", "http", "streamable-http"):
            errors.append(f"Unknown transport: {self.transport}")
        if self.transport == "stdio" and not self.command:
            errors.append("stdio transport requires 'command'")
        if self.transport in ("sse", "http", "streamable-http") and not self.url:
            errors.append(f"{self.transport} transport requires 'url'")
        if self.timeout_ms < 1000:
            errors.append(f"timeout_ms too low: {self.timeout_ms}")
        return errors

    def resolve_env(self) -> "McpServerConfig":
        """
        Replace $VAR references in env, headers, url with actual env vars.
        Returns a new config with resolved values (original unchanged).
        """
        def _resolve(val):
            if isinstance(val, str):
                return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), val)
            return val

        resolved_env = {k: _resolve(v) for k, v in self.env.items()}
        resolved_headers = {k: _resolve(v) for k, v in self.headers.items()}
        resolved_url = _resolve(self.url)

        return McpServerConfig(
            name=self.name, transport=self.transport,
            command=self.command, args=list(self.args),
            env=resolved_env, url=resolved_url,
            headers=resolved_headers,
            timeout_ms=self.timeout_ms,
            auto_connect=self.auto_connect,
            enabled=self.enabled,
        )


class McpConfigLoader:
    """Loads and manages MCP server configurations."""

    def __init__(self, settings_path: str = None):
        if settings_path is None:
            project_root = Path(__file__).resolve().parent.parent
            settings_path = str(project_root / "config" / "settings.json")
        self.settings_path = settings_path
        self._servers: dict[str, McpServerConfig] = {}

    def load(self) -> dict:
        """Load MCP server configs from settings file."""
        if not os.path.isfile(self.settings_path):
            logger.info("No settings file at %s", self.settings_path)
            return self._servers

        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            mcp_section = data.get("mcp", {}).get("servers", {})
            for name, cfg in mcp_section.items():
                server = McpServerConfig(
                    name=name,
                    transport=cfg.get("transport", "stdio"),
                    command=cfg.get("command", ""),
                    args=cfg.get("args", []),
                    env=cfg.get("env", {}),
                    url=cfg.get("url", ""),
                    headers=cfg.get("headers", {}),
                    timeout_ms=cfg.get("timeout_ms", 30000),
                    auto_connect=cfg.get("auto_connect", True),
                    enabled=cfg.get("enabled", True),
                )
                errors = server.validate()
                if errors:
                    logger.warning("MCP server '%s' has config errors: %s", name, errors)
                else:
                    self._servers[name] = server
                    logger.info("Loaded MCP server config: %s (%s)", name, server.transport)

        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load MCP configs: %s", e)

        return self._servers

    def add_server(self, config: McpServerConfig):
        """Add or replace a server config at runtime."""
        errors = config.validate()
        if errors:
            raise ValueError(f"Invalid config for '{config.name}': {errors}")
        self._servers[config.name] = config
        logger.info("Added MCP server: %s", config.name)

    def remove_server(self, name: str) -> bool:
        if name in self._servers:
            del self._servers[name]
            return True
        return False

    def get_server(self, name: str) -> Optional[McpServerConfig]:
        return self._servers.get(name)

    def get_auto_connect(self) -> list[McpServerConfig]:
        """Return configs marked for auto-connect on boot."""
        return [s for s in self._servers.values() if s.auto_connect and s.enabled]

    def list_servers(self) -> list[McpServerConfig]:
        return list(self._servers.values())

    def save(self):
        """Persist current server configs back to settings file."""
        # Load existing settings to preserve other sections
        data = {}
        if os.path.isfile(self.settings_path):
            try:
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        if "mcp" not in data:
            data["mcp"] = {}
        data["mcp"]["servers"] = {
            name: {k: v for k, v in cfg.to_dict().items() if k != "name"}
            for name, cfg in self._servers.items()
        }

        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved %d MCP server configs", len(self._servers))
