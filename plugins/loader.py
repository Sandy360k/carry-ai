"""
carry-ai/plugins/loader.py -- Plugin Discovery & Loading
==========================================================

Discovers plugins, loads manifests, and registers their tools/hooks.

Plugin manifest (plugin.json):
    {
        "name": "my-plugin",
        "version": "0.1.0",
        "description": "Does something useful",
        "author": "someone",
        "permissions": ["read", "write", "execute"],
        "tools": [
            {
                "name": "my_tool",
                "description": "Does a thing",
                "parameters": {"type": "object", "properties": {...}},
                "command": "./tools/my_tool.py"  # or inline "handler"
            }
        ],
        "hooks": {
            "pre_tool_use": ["./hooks/pre_check.py"],
            "post_tool_use": ["./hooks/log_result.py"]
        },
        "lifecycle": {
            "init": ["./setup.py"],
            "shutdown": ["./cleanup.py"]
        }
    }
"""

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger("carry-ai.plugins.loader")

VALID_PERMISSIONS = {"read", "write", "execute", "network", "clipboard"}
VALID_HOOKS = {"pre_tool_use", "post_tool_use", "post_tool_use_failure",
               "pre_chat", "post_chat", "on_startup", "on_shutdown"}


@dataclass
class PluginManifest:
    """Parsed plugin.json manifest."""
    name: str
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    permissions: list = field(default_factory=list)
    tools: list = field(default_factory=list)       # Tool definitions
    hooks: dict = field(default_factory=dict)        # hook_name -> [script_paths]
    lifecycle: dict = field(default_factory=dict)    # init/shutdown -> [script_paths]
    plugin_dir: str = ""                             # Absolute path to plugin directory
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "permissions": self.permissions,
            "tool_count": len(self.tools),
            "hook_count": sum(len(v) for v in self.hooks.values()),
            "plugin_dir": self.plugin_dir,
            "enabled": self.enabled,
        }

    def validate(self) -> list:
        """Return validation errors (empty = valid)."""
        errors = []
        if not self.name:
            errors.append("Missing 'name'")
        if not self.name.replace("-", "").replace("_", "").isalnum():
            errors.append(f"Invalid plugin name: {self.name}")
        for perm in self.permissions:
            if perm not in VALID_PERMISSIONS:
                errors.append(f"Unknown permission: {perm}")
        for hook in self.hooks:
            if hook not in VALID_HOOKS:
                errors.append(f"Unknown hook: {hook}")
        return errors


@dataclass
class PluginHook:
    """A registered hook with priority."""
    plugin_name: str
    hook_type: str           # pre_tool_use, post_tool_use, etc.
    script_path: str         # Absolute path to hook script
    priority: int = 100      # Lower = runs first

    def execute(self, context: dict) -> dict:
        """
        Run the hook script with context as JSON on stdin.
        Returns parsed JSON from stdout, or original context if script fails.
        """
        try:
            result = subprocess.run(
                [sys.executable, self.script_path],
                input=json.dumps(context).encode("utf-8"),
                capture_output=True,
                timeout=10,
                cwd=os.path.dirname(self.script_path),
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
            return context
        except Exception as e:
            logger.warning("Hook %s/%s failed: %s", self.plugin_name, self.hook_type, e)
            return context


# ---------------------------------------------------------------------------
# Hook Registry
# ---------------------------------------------------------------------------

class HookRegistry:
    """Global registry of plugin hooks, ordered by priority."""

    def __init__(self):
        self._hooks: dict[str, list[PluginHook]] = {h: [] for h in VALID_HOOKS}

    def register(self, hook: PluginHook):
        if hook.hook_type in self._hooks:
            self._hooks[hook.hook_type].append(hook)
            self._hooks[hook.hook_type].sort(key=lambda h: h.priority)

    def unregister_plugin(self, plugin_name: str):
        for hook_type in self._hooks:
            self._hooks[hook_type] = [
                h for h in self._hooks[hook_type] if h.plugin_name != plugin_name
            ]

    def run_hooks(self, hook_type: str, context: dict) -> dict:
        """Run all hooks of a given type in priority order. Returns modified context."""
        for hook in self._hooks.get(hook_type, []):
            context = hook.execute(context)
            # If hook sets "blocked": True, stop chain
            if context.get("blocked"):
                break
        return context

    def list_hooks(self, hook_type: str = None) -> list[PluginHook]:
        if hook_type:
            return list(self._hooks.get(hook_type, []))
        return [h for hooks in self._hooks.values() for h in hooks]


# ---------------------------------------------------------------------------
# Plugin Loader
# ---------------------------------------------------------------------------

class PluginLoader:
    """
    Discovers and loads plugins from configured directories.

    Usage:
        loader = PluginLoader(["plugins/bundled", "/mnt/usb/plugins"])
        manifests = loader.discover()
        loaded = loader.load_enabled(manifests)
    """

    def __init__(self, search_dirs: list = None):
        if search_dirs is None:
            project_root = Path(__file__).resolve().parent.parent
            search_dirs = [
                str(project_root / "plugins" / "bundled"),
                str(project_root.parent / "plugins"),  # USB root level
            ]
        self.search_dirs = [Path(d) for d in search_dirs]
        self.hook_registry = HookRegistry()
        self._loaded: dict[str, PluginManifest] = {}

    def discover(self) -> list[PluginManifest]:
        """Scan search directories for plugin.json manifests."""
        manifests = []

        for search_dir in self.search_dirs:
            if not search_dir.is_dir():
                continue

            for plugin_dir in sorted(search_dir.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                manifest_file = plugin_dir / "plugin.json"
                if not manifest_file.is_file():
                    continue

                try:
                    with open(manifest_file, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    manifest = PluginManifest(
                        name=data.get("name", plugin_dir.name),
                        version=data.get("version", "0.0.0"),
                        description=data.get("description", ""),
                        author=data.get("author", ""),
                        permissions=data.get("permissions", []),
                        tools=data.get("tools", []),
                        hooks=data.get("hooks", {}),
                        lifecycle=data.get("lifecycle", {}),
                        plugin_dir=str(plugin_dir),
                    )

                    errors = manifest.validate()
                    if errors:
                        logger.warning("Plugin '%s' has manifest errors: %s", manifest.name, errors)
                    else:
                        manifests.append(manifest)
                        logger.debug("Discovered plugin: %s v%s", manifest.name, manifest.version)

                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to load %s: %s", manifest_file, e)

        logger.info("Discovered %d plugins", len(manifests))
        return manifests

    def load_enabled(self, manifests: list[PluginManifest],
                     enabled_names: list = None) -> list[PluginManifest]:
        """
        Load enabled plugins: run init scripts, register tools and hooks.

        Args:
            manifests: Discovered manifests
            enabled_names: List of enabled plugin names (None = all)

        Returns list of successfully loaded manifests.
        """
        loaded = []

        for manifest in manifests:
            if enabled_names is not None and manifest.name not in enabled_names:
                manifest.enabled = False
                continue

            try:
                # Run lifecycle init scripts
                for script in manifest.lifecycle.get("init", []):
                    script_path = os.path.join(manifest.plugin_dir, script)
                    if os.path.isfile(script_path):
                        logger.debug("Running init script: %s", script_path)
                        subprocess.run(
                            [sys.executable, script_path],
                            timeout=30,
                            cwd=manifest.plugin_dir,
                            capture_output=True,
                        )

                # Register hooks
                for hook_type, scripts in manifest.hooks.items():
                    for script in scripts:
                        script_path = os.path.join(manifest.plugin_dir, script)
                        if os.path.isfile(script_path):
                            self.hook_registry.register(PluginHook(
                                plugin_name=manifest.name,
                                hook_type=hook_type,
                                script_path=script_path,
                            ))

                self._loaded[manifest.name] = manifest
                manifest.enabled = True
                loaded.append(manifest)
                logger.info("Loaded plugin: %s v%s (%d tools, %d hooks)",
                            manifest.name, manifest.version,
                            len(manifest.tools),
                            sum(len(v) for v in manifest.hooks.values()))

            except Exception as e:
                logger.error("Failed to load plugin '%s': %s", manifest.name, e)

        return loaded

    def register_plugin_tools(self, manifest: PluginManifest, tool_registry) -> int:
        """
        Register a plugin's tools into the agent's tool registry.

        Args:
            manifest: Plugin manifest with tool definitions
            tool_registry: The agent's TOOL_REGISTRY dict

        Returns number of tools registered.
        """
        from agent.tools import Tool
        count = 0

        for tdef in manifest.tools:
            tool_name = f"plugin__{manifest.name}__{tdef['name']}"
            command = tdef.get("command", "")
            script_path = os.path.join(manifest.plugin_dir, command) if command else ""

            def _make_handler(path, pdir):
                def handler(**kwargs):
                    try:
                        result = subprocess.run(
                            [sys.executable, path],
                            input=json.dumps(kwargs).encode("utf-8"),
                            capture_output=True,
                            timeout=60,
                            cwd=pdir,
                        )
                        if result.returncode == 0:
                            return result.stdout.decode("utf-8", errors="replace")
                        return f"Plugin tool error (exit {result.returncode}): {result.stderr.decode('utf-8', errors='replace')[:500]}"
                    except Exception as e:
                        return f"Plugin tool error: {e}"
                return handler

            if script_path and os.path.isfile(script_path):
                tool = Tool(
                    name=tool_name,
                    description=f"[Plugin:{manifest.name}] {tdef.get('description', '')}",
                    parameters=tdef.get("parameters", {"type": "object", "properties": {}}),
                    execute_fn=_make_handler(script_path, manifest.plugin_dir),
                )
                tool_registry[tool_name] = tool
                count += 1

        if count:
            logger.info("Registered %d tools from plugin '%s'", count, manifest.name)
        return count

    def unload_plugin(self, name: str):
        """Unload a plugin: run shutdown scripts, remove hooks."""
        manifest = self._loaded.pop(name, None)
        if not manifest:
            return

        # Run shutdown scripts
        for script in manifest.lifecycle.get("shutdown", []):
            script_path = os.path.join(manifest.plugin_dir, script)
            if os.path.isfile(script_path):
                try:
                    subprocess.run([sys.executable, script_path],
                                   timeout=10, cwd=manifest.plugin_dir, capture_output=True)
                except Exception:
                    pass

        self.hook_registry.unregister_plugin(name)
        logger.info("Unloaded plugin: %s", name)

    def list_loaded(self) -> list[dict]:
        return [m.to_dict() for m in self._loaded.values()]

    def shutdown(self):
        """Unload all plugins."""
        for name in list(self._loaded.keys()):
            self.unload_plugin(name)
