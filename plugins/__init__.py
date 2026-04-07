"""
carry-ai/plugins/ — Plugin System Package
============================================

Extensible plugin architecture inspired by claw-code's plugin system.
Plugins can add new tools, commands, and hooks to carry-ai.

Plugin Types:
    - Bundled:  Shipped with carry-ai (in plugins/bundled/)
    - External: User-installed from local path or URL

Plugin Manifest Format (plugin.json):
    {
        "name": "my-plugin",
        "version": "0.1.0",
        "description": "Does something useful",
        "permissions": ["read", "write", "execute"],
        "tools": [...],
        "hooks": {"pre_tool_use": ["./hooks/pre.sh"]},
        "lifecycle": {"init": ["./setup.sh"], "shutdown": ["./cleanup.sh"]}
    }

Submodules:
    manager — Install, enable, disable, uninstall plugins
    loader  — Discover and load plugin manifests, register tools/hooks
"""
