"""
carry-ai/mcp/defaults.py — MCP servers carry-ai connects to out of the box
==========================================================================

Desktop control: lets the AI see and operate apps on this PC through the
accessibility tree (buttons, fields, menus by name) instead of guessing
from screenshots, which also works for local models without vision.

    Windows  CursorTouch/Windows-MCP 0.8.5 (UI Automation), Python, run by
             the USB's interpreter from its own folder
             python-env/windows/servers/windows-mcp (portable/runtime.py)
    Linux    agent-sh/computer-use-linux 0.7.12 (AT-SPI; X11 and Wayland),
             a binary in bin/desktop/linux-<arch>/; needs glibc 2.39+

Both register as server "desktop" (tools mcp__desktop__*). A server is only
offered when its files are on the USB (the flasher's "desktop control"
option). Off with settings ``mcp.desktop_control: false`` or
``mcp.servers.desktop: {"enabled": false}``. Like every MCP tool these are
hidden in sandbox mode and go through the permission policy.

Windows-MCP: every tool is offered, under the permission policy. Its
toasts are tagged and removed from the Action Center by cleanup, and the
clipboard is wiped on eject. Linux: computer-use-linux's
setup_window_targeting (installs a GNOME Shell extension) stays out.
computer-use-linux's setup_accessibility stays too: the GNOME setting it
turns on is recorded at boot and restored by cleanup
(cleanup.snapshot_host_settings / restore_host_settings).
"""

import logging
import platform
import sys
from pathlib import Path

from mcp.config import McpServerConfig

log = logging.getLogger("carry-ai.mcp.defaults")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USB_ROOT = PROJECT_ROOT.parent
SERVER_NAME = "desktop"

# Windows-MCP: every tool is offered. "No trace" is about carry-ai, not
# about limiting it: system tools (PowerShell, Registry, Process,
# FileSystem) go through the permission policy; the clipboard is wiped on
# eject; toasts are tagged (group "carry-ai", see _WINDOWS_BOOT) and removed
# from the Action Center by cleanup. Linux: setup_window_targeting installs
# a GNOME Shell extension into the home folder, so it stays out.
WINDOWS_EXCLUDE: list[str] = []
LINUX_EXCLUDE = ["setup_window_targeting"]

# Windows-MCP runs with -I -S: none of carry-ai's packages (whose `mcp`
# folder would clash with the MCP SDK's), then its own folder is added as
# a site dir so pywin32's .pth file is honoured. Before the server starts,
# its PowerShell runner is wrapped so every toast it shows gets
# Group "carry-ai" (TOAST_GROUP) and a unique Tag; cleanup removes exactly
# that group, never the user's own notifications.
TOAST_GROUP = "carry-ai"
_WINDOWS_BOOT = (
    "import runpy, site, sys\n"
    "site.addsitedir(sys.argv[1])\n"
    "try:\n"
    "    import windows_mcp.notifications.service as _svc\n"
    "    _PS = _svc.PowerShellExecutor\n"
    "    class _Tagged:\n"
    "        @staticmethod\n"
    "        def execute_command(script, *a, **k):\n"
    "            script = script.replace('$notifier.Show($toast)', "
    "\"$toast.Group = '" + TOAST_GROUP + "'\\n"
    "$toast.Tag = [guid]::NewGuid().ToString('N').Substring(0, 16)\\n"
    "$notifier.Show($toast)\")\n"
    "            return _PS.execute_command(script, *a, **k)\n"
    "    _svc.PowerShellExecutor = _Tagged\n"
    "except Exception as e:\n"
    "    print('carry-ai: toast tagging unavailable:', e, file=sys.stderr)\n"
    "sys.argv = ['windows-mcp'] + sys.argv[2:]\n"
    "runpy.run_module('windows_mcp', run_name='__main__')\n"
)


def on_tool_call(server: str, tool: str, args: dict) -> None:
    """Bridge hook: remember toast app ids so cleanup can remove our toasts."""
    if server == SERVER_NAME and tool == "Notification" and isinstance(args, dict):
        from cleanup.cleanup import record_toast_app
        record_toast_app(str(args.get("app_id", "")))


def _enabled() -> bool:
    try:
        from config.settings import load_settings
        return bool(load_settings().to_dict().get("mcp", {}).get("desktop_control", True))
    except Exception:
        return True


def windows_server(usb_root: Path = USB_ROOT) -> McpServerConfig | None:
    from portable.runtime import server_dir
    folder = server_dir(usb_root, "windows", "windows-mcp")
    if not (folder / "windows_mcp").is_dir():
        return None
    return McpServerConfig(
        name=SERVER_NAME,
        command=sys.executable,
        args=["-I", "-S", "-c", _WINDOWS_BOOT, str(folder), "serve", "--transport", "stdio"]
             + (["--exclude-tools", ",".join(WINDOWS_EXCLUDE)] if WINDOWS_EXCLUDE else []),
        env={
            "ANONYMIZED_TELEMETRY": "false",   # no PostHog client, no user-id file
            "POSTHOG_API_KEY": "",
            "WINDOWS_MCP_DISABLE_FLASH": "1",  # no highlight border on screenshots
        },
        timeout_ms=60000,
        exclude_tools=list(WINDOWS_EXCLUDE),
    )


def linux_server(usb_root: Path = USB_ROOT) -> McpServerConfig | None:
    from portable.runtime import (DESKTOP_LINUX_MIN_GLIBC, desktop_linux_binary,
                                  ensure_executable, glibc_version)
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(),
                                                        platform.machine().lower())
    binary = desktop_linux_binary(usb_root, arch)
    if not binary.is_file():
        return None
    glibc = glibc_version()
    if glibc is None or glibc < DESKTOP_LINUX_MIN_GLIBC:
        log.info("Desktop control needs glibc %d.%d+ (this PC: %s); not starting it.",
                 *DESKTOP_LINUX_MIN_GLIBC, glibc)
        return None
    return McpServerConfig(
        name=SERVER_NAME,
        command=str(ensure_executable(binary)),   # noexec USB → RAM copy
        args=["mcp"],
        timeout_ms=60000,
        exclude_tools=list(LINUX_EXCLUDE),
    )


def default_servers() -> list[McpServerConfig]:
    if not _enabled():
        return []
    if sys.platform == "win32":
        server = windows_server()
    elif sys.platform.startswith("linux"):
        server = linux_server()
    else:
        server = None
    return [server] if server else []
