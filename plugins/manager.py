"""
carry-ai/plugins/manager.py -- Plugin Manager
===============================================

High-level plugin lifecycle: install, enable, disable, uninstall.
Persists plugin state to config/plugin_state.json.
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from plugins.loader import PluginLoader, PluginManifest

logger = logging.getLogger("carry-ai.plugins.manager")


class PluginManager:
    """
    Manages plugin installation, enable/disable state, and lifecycle.

    Usage:
        pm = PluginManager("plugins/", "config/plugin_state.json")
        pm.load_state()

        # Discover and load
        manifests = pm.loader.discover()
        pm.loader.load_enabled(manifests, pm.enabled_list())

        # Install from path
        pm.install("/path/to/my-plugin/")

        # Enable/disable
        pm.enable("my-plugin")
        pm.disable("my-plugin")

        # Uninstall
        pm.uninstall("my-plugin")
    """

    def __init__(self, plugins_dir: str = None, state_path: str = None):
        project_root = Path(__file__).resolve().parent.parent

        if plugins_dir is None:
            plugins_dir = str(project_root / "plugins" / "installed")
        if state_path is None:
            state_path = str(project_root / "config" / "plugin_state.json")

        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_path

        # State: {plugin_name: {"enabled": bool, "installed_at": float, "version": str}}
        self._state: dict = {}

        # Loader handles discovery and loading
        self.loader = PluginLoader([
            str(project_root / "plugins" / "bundled"),
            str(self.plugins_dir),
        ])

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def load_state(self):
        """Load plugin enabled/disabled state from JSON."""
        if os.path.isfile(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self._state = json.load(f)
                logger.info("Loaded plugin state: %d entries", len(self._state))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load plugin state: %s", e)

    def save_state(self):
        """Persist plugin state to JSON."""
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2)

    # ------------------------------------------------------------------
    # Install / Uninstall
    # ------------------------------------------------------------------

    def install(self, source_path: str) -> Optional[str]:
        """
        Install a plugin from a local directory.
        Copies the plugin directory into plugins/installed/.

        Args:
            source_path: Path to plugin directory containing plugin.json

        Returns plugin name on success, None on failure.
        """
        src = Path(source_path)
        manifest_file = src / "plugin.json"

        if not manifest_file.is_file():
            logger.error("No plugin.json found in %s", source_path)
            return None

        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("name", src.name)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Invalid plugin manifest: %s", e)
            return None

        # Copy to installed dir
        dest = self.plugins_dir / name
        if dest.exists():
            shutil.rmtree(dest)

        shutil.copytree(src, dest)

        import time
        self._state[name] = {
            "enabled": True,
            "installed_at": time.time(),
            "version": data.get("version", "0.0.0"),
            "source": str(source_path),
        }
        self.save_state()

        logger.info("Installed plugin: %s -> %s", name, dest)
        return name

    def uninstall(self, name: str) -> bool:
        """Remove a plugin and its state."""
        # Unload first
        self.loader.unload_plugin(name)

        # Remove directory
        dest = self.plugins_dir / name
        if dest.exists():
            shutil.rmtree(dest)

        self._state.pop(name, None)
        self.save_state()

        logger.info("Uninstalled plugin: %s", name)
        return True

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------

    def enable(self, name: str) -> bool:
        """Enable a plugin."""
        if name not in self._state:
            # Check if discoverable
            manifests = self.loader.discover()
            found = next((m for m in manifests if m.name == name), None)
            if not found:
                logger.warning("Plugin '%s' not found", name)
                return False
            self._state[name] = {"enabled": True}
        else:
            self._state[name]["enabled"] = True

        self.save_state()
        logger.info("Enabled plugin: %s", name)
        return True

    def disable(self, name: str) -> bool:
        """Disable a plugin."""
        if name in self._state:
            self._state[name]["enabled"] = False
            self.save_state()
            self.loader.unload_plugin(name)
            logger.info("Disabled plugin: %s", name)
            return True
        return False

    def is_enabled(self, name: str) -> bool:
        return self._state.get(name, {}).get("enabled", False)

    def enabled_list(self) -> list:
        """Return list of enabled plugin names."""
        return [name for name, state in self._state.items() if state.get("enabled")]

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def list_plugins(self) -> list[dict]:
        """List all known plugins with state."""
        manifests = self.loader.discover()
        result = []
        for m in manifests:
            info = m.to_dict()
            state = self._state.get(m.name, {})
            info["enabled"] = state.get("enabled", False)
            info["installed_at"] = state.get("installed_at")
            result.append(info)
        return result

    def get_plugin(self, name: str) -> Optional[dict]:
        """Get detailed info about a plugin."""
        manifests = self.loader.discover()
        m = next((m for m in manifests if m.name == name), None)
        if m:
            info = m.to_dict()
            info["state"] = self._state.get(name, {})
            return info
        return None

    # ------------------------------------------------------------------
    # Boot integration
    # ------------------------------------------------------------------

    def boot(self) -> list[PluginManifest]:
        """
        Full boot sequence: load state, discover, load enabled plugins.
        Returns list of loaded manifests.
        """
        self.load_state()
        manifests = self.loader.discover()
        enabled = self.enabled_list()
        loaded = self.loader.load_enabled(manifests, enabled if enabled else None)
        return loaded

    def shutdown(self):
        """Shutdown all loaded plugins."""
        self.loader.shutdown()
