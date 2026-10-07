"""
carry-ai/setup_usb.py — USB Portable Environment Setup
=======================================================

Run this ONCE after copying carry-ai to a USB drive:

    python setup_usb.py

What it does:
  - Creates python-env/windows/  → portable Python interpreter + all packages
  - Creates python-env/linux/    → packages only (uses host Python 3.10+ interpreter)
  - Writes start.bat / start.sh  → launchers that use the USB-local environment
  - Optionally downloads a GGUF model onto the USB

After setup:
  Windows: the USB needs NO Python installed on the host machine at all.
  Linux:   the USB needs only a Python 3.10+ interpreter on the host.
           All packages are served from the USB — no pip install required.

Architecture:
  USB root/
  ├── carry-ai/           ← this repo
  ├── python-env/
  │   ├── windows/        ← embeddable Python + Lib/site-packages/
  │   └── linux/          ← site-packages/ only
  ├── models/             ← GGUF model files (optional)
  ├── start.bat           ← Windows: uses python-env/windows/python.exe
  └── start.sh            ← Linux:   sets PYTHONPATH, uses system python3
"""

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, DownloadColumn, TransferSpeedColumn
    from rich.table import Table
    from rich.prompt import Prompt, Confirm
    from rich import print as rprint
    _RICH = True
except ImportError:
    _RICH = False

# ---------------------------------------------------------------------------
# Fallback console (plain text) when rich is not installed
# ---------------------------------------------------------------------------
class _Plain:
    def print(self, *a, **kw):
        text = " ".join(str(x) for x in a)
        # strip rich markup tags
        import re
        text = re.sub(r'\[/?[^\]]+\]', '', text)
        print(text)
    def rule(self, title=""):
        print(f"\n{'─' * 60}")
        if title:
            print(f"  {title}")
        print()

if _RICH:
    console = Console()
else:
    console = _Plain()

def _panel(title, body="", style="blue"):
    if _RICH:
        console.print(Panel(body or title, title=title if body else "", border_style=style))
    else:
        console.rule(title)
        if body:
            for line in body.strip().splitlines():
                print(f"  {line}")
        print()

def _confirm(prompt, default=True):
    if _RICH:
        return Confirm.ask(prompt, default=default)
    ans = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")

def _prompt(prompt, default=""):
    if _RICH:
        return Prompt.ask(prompt, default=default)
    val = input(f"{prompt} [{default}]: ").strip()
    return val if val else default

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Package groups
CORE_PACKAGES = [
    "psutil>=5.9.0",
    "cryptography>=41.0.0",
    "flask>=3.0.0",
    "requests>=2.31.0",
    "rich>=13.7.0",
    "customtkinter>=5.2.0",
    "qrcode>=7.4",
]
PROVIDER_PACKAGES = [
    "anthropic>=0.30.0",
    "openai>=1.30.0",
    "google-auth>=2.29.0",
    "google-auth-oauthlib>=1.2.0",
]
MODEL_PACKAGES = [
    "huggingface-hub>=0.23.0",
]
TOOL_PACKAGES = [
    "pyautogui>=0.9.54",
    "pyperclip>=1.8.2",
    "pydantic-monty>=1.1.0",   # run_python sandbox
]
VOICE_PACKAGES = [
    "sherpa-onnx>=1.13.8",   # offline speech-to-text / text-to-speech
    "pyaudio>=0.2.14",       # mic + speaker on Windows (no Linux wheel; Linux
                             # records via sherpa-onnx ALSA and plays via aplay)
    "elevenlabs>=2.0.0",     # optional cloud TTS
    "Pillow>=10.0.0",
]

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from portable import runtime  # noqa: E402


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def find_usb_root(target_override: str | None = None) -> Path:
    """Resolve the USB root directory.

    Priority:
      1. --target <path> CLI argument (passed in as target_override)
      2. Parent of this script when laid out as USB_ROOT/carry-ai/setup_usb.py
    """
    if target_override:
        p = Path(target_override).resolve()
        if not p.exists():
            console.print(f"  [red]✗ --target path does not exist: {p}[/red]")
            sys.exit(1)
        return p

    candidate = PROJECT_ROOT.parent
    if (candidate / "carry-ai").is_dir() or candidate != PROJECT_ROOT:
        return candidate
    return PROJECT_ROOT.parent


def is_removable_drive(path: Path) -> bool:
    """Return True if *path* lives on a removable/USB drive."""
    system = platform.system().lower()
    if system == "windows":
        try:
            import ctypes
            drive = str(path)[:3]          # e.g. "E:\\"
            DRIVE_REMOVABLE = 2
            return ctypes.windll.kernel32.GetDriveTypeW(drive) == DRIVE_REMOVABLE
        except Exception:
            return False
    elif system == "linux":
        try:
            import subprocess as _sp
            # Walk up to find the real block device mount
            result = _sp.run(
                ["lsblk", "-no", "RM", "--raw", str(path)],
                capture_output=True, text=True, timeout=3,
            )
            return "1" in result.stdout
        except Exception:
            return False
    return False


def get_env_dirs(usb_root: Path) -> dict:
    """Return a dict of all relevant path objects."""
    env = usb_root / "python-env"
    return {
        "usb_root": usb_root,
        "env": env,
        "win": env / "windows",
        "win_python": env / "windows" / "python.exe",
        "win_site": env / "windows" / "Lib" / "site-packages",
        "linux": env / "linux",
        "linux_site": env / "linux" / "site-packages",
    }


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _download_bytes(url: str, label: str = "") -> bytes:
    """Download a URL to bytes, showing progress."""
    console.print(f"  Downloading {label or url} ...")
    try:
        req = Request(url, headers={"User-Agent": "carry-ai/setup_usb"})
        with urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            buf = io.BytesIO()
            downloaded = 0
            chunk = 65536
            while True:
                data = resp.read(chunk)
                if not data:
                    break
                buf.write(data)
                downloaded += len(data)
                if total:
                    pct = downloaded * 100 // total
                    mb = downloaded / 1_048_576
                    print(f"\r    {mb:.1f} MB / {total/1_048_576:.1f} MB  ({pct}%)", end="", flush=True)
            print()
            return buf.getvalue()
    except (URLError, HTTPError) as e:
        console.print(f"  [red]Download failed:[/red] {e}")
        return b""


def _download_file(url: str, dest: Path, label: str = "") -> bool:
    """Download URL to a file. Returns True on success."""
    data = _download_bytes(url, label or dest.name)
    if not data:
        return False
    dest.write_bytes(data)
    return True


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_packages(package_names: list[str], site_dir: Path) -> dict[str, bool]:
    """Check whether packages are importable from site_dir.

    Uses a subprocess with PYTHONPATH set to site_dir so we test the USB env
    independently of whatever is installed on the host.
    """
    results = {}
    env = os.environ.copy()
    env["PYTHONPATH"] = str(site_dir)

    for pkg in package_names:
        import_name = _pkg_to_import(pkg)
        proc = subprocess.run(
            [sys.executable, "-c", f"import {import_name}"],
            capture_output=True, env=env
        )
        results[pkg] = proc.returncode == 0

    return results


def _pkg_to_import(pkg_name: str) -> str:
    """Map PyPI package name to Python import name."""
    mapping = {
        "Pillow": "PIL",
        "google-auth": "google.auth",
        "google-auth-oauthlib": "google_auth_oauthlib",
        "huggingface-hub": "huggingface_hub",
        "pyautogui": "pyautogui",
        "pyperclip": "pyperclip",
        "cryptography": "cryptography",
        "flask": "flask",
        "requests": "requests",
        "psutil": "psutil",
        "rich": "rich",
        "anthropic": "anthropic",
        "openai": "openai",
        "elevenlabs": "elevenlabs",
        "sherpa-onnx": "sherpa_onnx",
        "pyaudio": "pyaudio",
    }
    return mapping.get(pkg_name, pkg_name.replace("-", "_"))


# ---------------------------------------------------------------------------
# Launcher scripts
# ---------------------------------------------------------------------------

START_BAT = r"""@echo off
:: carry-ai Windows Launcher
:: Uses the USB-bundled portable Python if present;
:: falls back to system Python as a safety net.
SETLOCAL

SET "SCRIPT_DIR=%~dp0"
SET "USB_PYTHON=%SCRIPT_DIR%python-env\windows\python.exe"
SET "CARRY_AI=%SCRIPT_DIR%carry-ai\launcher.py"

IF EXIST "%USB_PYTHON%" (
    ECHO [carry-ai] Using USB-local Python: %USB_PYTHON%
    "%USB_PYTHON%" "%CARRY_AI%" %*
) ELSE (
    ECHO [carry-ai] USB Python not found, falling back to system Python.
    ECHO             Run: python carry-ai\setup_usb.py   to set up the USB env.
    python "%CARRY_AI%" %*
)
ENDLOCAL
"""

START_SH = r"""#!/usr/bin/env bash
# carry-ai Linux Launcher
# Injects USB-bundled site-packages into PYTHONPATH so the host machine
# needs only a Python 3.10+ interpreter — no pip install required.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USB_SITE="$SCRIPT_DIR/python-env/linux/site-packages"
CARRY_AI="$SCRIPT_DIR/carry-ai/launcher.py"

if [ -d "$USB_SITE" ]; then
    export PYTHONPATH="$USB_SITE${PYTHONPATH:+:$PYTHONPATH}"
    echo "[carry-ai] USB packages: $USB_SITE"
else
    echo "[carry-ai] No USB packages found. Run: python3 carry-ai/setup_usb.py"
fi

exec python3 "$CARRY_AI" "$@"
"""

AUTORUN_INF = """[autorun]
open=start.bat
icon=carry-ai\\ui\\icon.ico
label=carry-ai
"""

def write_launchers(usb_root: Path) -> None:
    """Write / overwrite start.bat, start.sh, autorun.inf at the USB root."""
    bat = usb_root / "start.bat"
    sh = usb_root / "start.sh"
    inf = usb_root / "autorun.inf"

    bat.write_text(START_BAT, encoding="utf-8")
    console.print(f"  [green]✓[/green] Written {bat}")

    sh.write_text(START_SH, encoding="utf-8")
    sh.chmod(0o755)
    console.print(f"  [green]✓[/green] Written {sh}")

    if not inf.exists():
        inf.write_text(AUTORUN_INF, encoding="utf-8")
        console.print(f"  [green]✓[/green] Written {inf}")


# ---------------------------------------------------------------------------
# Size estimation
# ---------------------------------------------------------------------------

def _dir_size_mb(path: Path) -> float:
    if not path.is_dir():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / 1_048_576


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------

def _parse_target() -> str | None:
    """Extract --target <path> from sys.argv if present."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ("--target", "-t") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--target="):
            return a.split("=", 1)[1]
    return None


def main() -> None:
    current_os = platform.system().lower()
    is_windows = current_os == "windows"

    _panel(
        "carry-ai USB Environment Setup",
        "This script makes the USB drive self-hosting.\n"
        "After setup, the USB carries its own Python packages.\n"
        "On Windows it carries a full portable Python interpreter too.",
        style="cyan"
    )

    target_override = _parse_target()
    usb_root = find_usb_root(target_override)
    dirs = get_env_dirs(usb_root)

    console.print(f"  USB root detected : [bold]{usb_root}[/bold]")
    console.print(f"  Env target        : [bold]{dirs['env']}[/bold]")
    console.print(f"  Running on        : [bold]{current_os}[/bold]")
    print()

    # ---- Removable drive check --------------------------------------------
    if not target_override and not is_removable_drive(usb_root):
        _panel(
            "Not a USB drive",
            f"The detected root is:\n  {usb_root}\n\n"
            "This looks like a local folder, not a USB drive.\n\n"
            "Did you mean to use flash_usb.py instead?\n"
            "  python flash_usb.py\n\n"
            "flash_usb.py detects your USB drives automatically and copies\n"
            "everything onto the USB for you.\n\n"
            "If you DO want to install into this folder (e.g. for testing),\n"
            "re-run with an explicit --target flag:\n"
            f"  python setup_usb.py --target \"{usb_root}\"",
            style="yellow",
        )
        if not _confirm("Continue installing into this local folder anyway?", default=False):
            console.print("\n  Tip: Insert your USB drive and run  python flash_usb.py\n")
            sys.exit(0)
        print()

    print()

    # Disk space check
    try:
        total, used, free = shutil.disk_usage(usb_root)
        free_gb = free / (1024 ** 3)
        console.print(f"  Free space on USB : [bold]{free_gb:.1f} GB[/bold]")
        if free_gb < 0.5:
            console.print("  [red]✗ Less than 500 MB free — packages may not fit.[/red]")
            if not _confirm("Continue anyway?", default=False):
                sys.exit(1)
        elif free_gb < 1.5:
            console.print("  [yellow]⚠ Less than 1.5 GB — may be tight for all packages.[/yellow]")
    except Exception:
        pass

    print()

    # ---- Which platform to set up? ----------------------------------------
    setup_win = False
    setup_linux = False

    if is_windows:
        setup_win = _confirm("Set up Windows portable Python environment on this USB?", default=True)
        setup_linux = _confirm("Also set up Linux packages on this USB?", default=True)
    else:
        setup_linux = _confirm("Set up Linux packages on this USB?", default=True)
        setup_win = _confirm("Also set up Windows portable Python on this USB?\n"
                             "  (requires ~400 MB, lets Windows users run without installing Python)", default=False)

    # ---- Which package groups? --------------------------------------------
    print()
    console.print("[bold]Package groups to install:[/bold]")
    install_core      = True  # always
    install_providers = _confirm("  LLM provider SDKs (anthropic, openai, google-auth, groq, openrouter)?", default=True)
    install_models    = _confirm("  Model management (huggingface-hub)?", default=True)
    install_tools     = _confirm("  Agent tools (pyautogui, pyperclip)?", default=True)
    install_voice     = _confirm("  Voice (offline speech with sherpa-onnx + ~86 MB of models)?", default=False)
    setup_llama       = _confirm("  llama.cpp for local models (Vulkan GPU + CPU, ~140 MB/OS)?", default=True)

    packages_to_install = list(CORE_PACKAGES)
    if install_providers:
        packages_to_install += PROVIDER_PACKAGES
    if install_models:
        packages_to_install += MODEL_PACKAGES
    if install_tools:
        packages_to_install += TOOL_PACKAGES
    if install_voice:
        packages_to_install += VOICE_PACKAGES

    console.print(f"\n  [bold]{len(packages_to_install)}[/bold] packages selected.")

    # ---- Per-OS runtime: Python + packages + llama.cpp ---------------------
    cache = Path.home() / ".cache" / "carry-ai-setup"

    def _progress(label):
        def _cb(written, total):
            if total:
                print(f"\r    {label}: {written/1_048_576:.1f}/{total/1_048_576:.1f} MB "
                      f"({written * 100 // total}%)", end="", flush=True)
        return _cb

    for os_name, wanted in (("linux", setup_linux), ("windows", setup_win)):
        if not wanted:
            continue
        console.rule(f"{os_name.capitalize()} runtime")
        try:
            runtime.install_python(usb_root, os_name, cache, progress=_progress(f"python-{os_name}"))
            print()
        except (OSError, ValueError) as e:
            console.print(f"  [red]✗ Portable Python failed: {e}[/red]")
            continue

        def _report(name, ok):
            console.print(f"  {'[green]✓[/green]' if ok else '[red]✗[/red]'} {name}")
        failed = runtime.install_packages(packages_to_install, usb_root, os_name, report=_report)
        if failed:
            console.print(f"  [yellow]⚠ Failed:[/yellow] {', '.join(failed)}")
        else:
            console.print(f"  [green]✓ All packages installed for {os_name}.[/green]")

        if setup_llama:
            try:
                runtime.install_llama(usb_root, os_name, cache, progress=_progress(f"llama-{os_name}"))
                print()
                console.print(f"  [green]✓ llama.cpp (Vulkan + CPU) bundled for {os_name}.[/green]")
            except (OSError, ValueError) as e:
                console.print(f"  [yellow]⚠ llama.cpp download failed: {e}[/yellow]")

        size = _dir_size_mb(usb_root / "python-env" / os_name)
        console.print(f"  {os_name.capitalize()} env size: {size:.0f} MB")

    # ---- Launcher scripts --------------------------------------------------
    console.rule("Launcher scripts")
    write_launchers(usb_root)

    # ---- Offline voice models ---------------------------------------------
    if install_voice:
        try:
            from models import voice as vm
            for model_id in (vm.DEFAULT_STT, vm.DEFAULT_TTS):
                if not vm.is_installed(model_id):
                    console.print(f"  Downloading voice model {model_id}…")
                    vm.download(model_id)
            console.print("  [green]✓ Offline voice models ready[/green]")
        except Exception as e:
            console.print(f"  [yellow]Voice models not downloaded ({e}); "
                          "use the app's Voice settings later.[/yellow]")

    # ---- Summary -----------------------------------------------------------
    console.rule("Done")

    total_size = _dir_size_mb(dirs["env"])
    _panel(
        "USB environment ready",
        f"Total python-env/ size : {total_size:.0f} MB\n\n"
        "To launch carry-ai from the USB:\n\n"
        "  Windows  →  double-click start.bat  (bundled Python — nothing to install)\n\n"
        "  Linux    →  bash start.sh  (bundled Python — nothing to install)\n\n"
        "To re-run this setup (e.g. after updating requirements):\n"
        "  python carry-ai/setup_usb.py",
        style="green"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(0)
