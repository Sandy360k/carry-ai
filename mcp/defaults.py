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

Tools that duplicate carry-ai's own (shell, files, clipboard, web) or that
leave lasting changes on the host are excluded: Windows-MCP's PowerShell,
Registry, FileSystem, Process, Scrape, Clipboard and Notification (toasts
stay in the Action Center); computer-use-linux's setup_accessibility and
setup_window_targeting (they change GNOME settings / install an extension).
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

WINDOWS_EXCLUDE = ["PowerShell", "Registry", "FileSystem", "Process", "Scrape",
                   "Clipboard", "Notification"]
LINUX_EXCLUDE = ["setup_accessibility", "setup_window_targeting"]

# Windows-MCP runs with -I -S: none of carry-ai's packages (whose `mcp`
# folder would clash with the MCP SDK's), then its own folder is added as
# a site dir so pywin32's .pth file is honoured.
_WINDOWS_BOOT = ("import runpy, site, sys; site.addsitedir(sys.argv[1]); "
                 "sys.argv = ['windows-mcp'] + sys.argv[2:]; "
                 "runpy.run_module('windows_mcp', run_name='__main__')")


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
        args=["-I", "-S", "-c", _WINDOWS_BOOT, str(folder),
              "serve", "--transport", "stdio", "--exclude-tools", ",".join(WINDOWS_EXCLUDE)],
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
