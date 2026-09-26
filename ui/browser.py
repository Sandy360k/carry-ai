"""
carry-ai/ui/browser.py — Trace-free browser launcher
=====================================================

Opens the web UI without touching the host user's normal browser profile.

Opening ``http://localhost:8080`` with ``webbrowser.open`` lands in the
user's everyday profile, which records history, cache, session restore and
autocomplete entries that outlive the USB session. Instead we launch a
Chromium-family browser (Edge ships with every Windows 10/11 install) as a
separate app-mode window whose profile lives inside the session directory,
so the cleanup sweep removes it together with the session. Firefox gets a
private window. Only if nothing suitable is found do we fall back to the
default browser, with a warning.
"""

import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

log = logging.getLogger("carry-ai.ui.browser")

# Executable names / well-known install paths, most preferred first.
_CHROMIUM_NAMES = [
    "msedge", "microsoft-edge", "microsoft-edge-stable",
    "chrome", "google-chrome", "google-chrome-stable",
    "chromium", "chromium-browser", "brave", "brave-browser",
]

_WINDOWS_PATHS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
]

_FIREFOX_PATHS = [
    r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
    r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
]


def _find(names: list[str], win_paths: list[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "win32":
        for raw in win_paths:
            path = Path(os.path.expandvars(raw))
            if path.is_file():
                return str(path)
    return None


def chromium_command(exe: str, url: str, profile_dir: Path) -> list[str]:
    """Build the argv for an isolated, history-free Chromium app window.

    The throwaway ``--user-data-dir`` is what keeps the host profile clean;
    ``--incognito``/``--inprivate`` are not added because they disable
    ``--app`` mode on some Chromium builds.
    """
    return [
        exe,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--disk-cache-size=1",
    ]


def open_private(url: str, session_dir: str | Path | None) -> str:
    """Open *url* in the most trace-free browser window available.

    Args:
        url: Address of the web UI (including the session token).
        session_dir: Wiped-on-eject session directory; the throwaway browser
            profile is created inside it.

    Returns:
        A short label describing how the URL was opened.
    """
    profile_dir = Path(session_dir or ".") / "browser-profile"
    popen_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}

    exe = _find(_CHROMIUM_NAMES, _WINDOWS_PATHS)
    if exe:
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(chromium_command(exe, url, profile_dir), **popen_kwargs)
            return f"isolated app window ({Path(exe).stem})"
        except OSError as e:
            log.warning("Could not launch %s: %s", exe, e)

    exe = _find(["firefox"], _FIREFOX_PATHS)
    if exe:
        try:
            subprocess.Popen([exe, "--private-window", url], **popen_kwargs)
            return "Firefox private window"
        except OSError as e:
            log.warning("Could not launch %s: %s", exe, e)

    log.warning("No Chromium/Firefox found — opening the default browser. "
                "Its history will record the carry-ai URL.")
    webbrowser.open(url)
    return "default browser (history NOT isolated)"
