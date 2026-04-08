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
PYTHON_VERSION = "3.11.9"
PYTHON_EMBED_URLS = {
    "amd64": f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip",
    "win32": f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-win32.zip",
}
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# Package groups
CORE_PACKAGES = [
    "psutil>=5.9.0",
    "cryptography>=41.0.0",
    "flask>=3.0.0",
    "requests>=2.31.0",
    "rich>=13.7.0",
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
]
VOICE_PACKAGES = [
    "assemblyai>=0.24.0",
    "elevenlabs>=1.2.0",
    "Pillow>=10.0.0",
]
# pyaudio needs portaudio system lib — handled separately with instructions

PROJECT_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def find_usb_root() -> Path:
    """Find the USB root directory (parent of carry-ai/).

    In normal USB deployment the layout is:
        USB_ROOT/carry-ai/setup_usb.py   ← this file
        USB_ROOT/python-env/
        USB_ROOT/start.bat

    Falls back to PROJECT_ROOT.parent or PROJECT_ROOT itself (dev mode).
    """
    candidate = PROJECT_ROOT.parent
    if (candidate / "carry-ai").is_dir() or candidate != PROJECT_ROOT:
        return candidate
    return PROJECT_ROOT.parent


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
# Windows: embeddable Python
# ---------------------------------------------------------------------------

def setup_windows_embedded_python(dirs: dict) -> bool:
    """Download and extract the Windows embeddable Python package.

    Returns True if python.exe is available at the end.
    """
    win_dir = dirs["win"]
    python_exe = dirs["win_python"]

    if python_exe.is_file():
        console.print(f"  [green]✓[/green] Portable Python already present: {python_exe}")
        return True

    win_dir.mkdir(parents=True, exist_ok=True)

    # Detect architecture
    arch = "amd64" if platform.machine().lower() in ("amd64", "x86_64") else "win32"
    url = PYTHON_EMBED_URLS[arch]
    console.print(f"  Fetching portable Python {PYTHON_VERSION} ({arch}) ...")

    zip_data = _download_bytes(url, f"python-{PYTHON_VERSION}-embed-{arch}.zip")
    if not zip_data:
        console.print("  [red]✗ Failed to download portable Python.[/red]")
        return False

    console.print("  Extracting ...")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            zf.extractall(win_dir)
    except Exception as e:
        console.print(f"  [red]✗ Extraction failed: {e}[/red]")
        return False

    # Patch ._pth file to enable site-packages loading
    _patch_pth_file(win_dir)

    console.print(f"  [green]✓[/green] Portable Python extracted to {win_dir}")
    return python_exe.is_file()


def _patch_pth_file(win_dir: Path) -> None:
    """Patch pythonXX._pth to enable site.py and our site-packages dir."""
    pth_files = list(win_dir.glob("python*._pth"))
    if not pth_files:
        return
    pth = pth_files[0]
    content = pth.read_text(encoding="utf-8")
    # Uncomment 'import site' if commented out
    content = content.replace("#import site", "import site")
    # Add Lib/site-packages if not already there
    if "Lib\\site-packages" not in content:
        content += "\nLib\\site-packages\n"
    pth.write_text(content, encoding="utf-8")
    console.print(f"  Patched {pth.name} to enable site-packages.")


def install_pip_into_embedded(python_exe: Path) -> bool:
    """Download get-pip.py and install pip into the embedded Python."""
    pip_check = subprocess.run(
        [str(python_exe), "-m", "pip", "--version"],
        capture_output=True
    )
    if pip_check.returncode == 0:
        console.print("  [green]✓[/green] pip already available in embedded Python.")
        return True

    console.print("  Installing pip into portable Python ...")
    get_pip_data = _download_bytes(GET_PIP_URL, "get-pip.py")
    if not get_pip_data:
        return False

    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
        f.write(get_pip_data)
        get_pip_path = f.name

    try:
        result = subprocess.run(
            [str(python_exe), get_pip_path, "--no-warn-script-location"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            console.print(f"  [red]pip install failed:[/red] {result.stderr[-500:]}")
            return False
        console.print("  [green]✓[/green] pip installed.")
        return True
    finally:
        os.unlink(get_pip_path)


# ---------------------------------------------------------------------------
# Package installation
# ---------------------------------------------------------------------------

def install_packages(packages: list[str], target_dir: Path,
                     python_exe: Path | None = None) -> list[str]:
    """Install packages into target_dir using pip --target.

    Returns list of packages that failed to install.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    failed = []

    interpreter = str(python_exe) if python_exe else sys.executable

    for pkg in packages:
        pkg_name = pkg.split(">=")[0].split("==")[0].strip()
        console.print(f"  Installing [cyan]{pkg_name}[/cyan] ...", end=" ")
        result = subprocess.run(
            [
                interpreter, "-m", "pip", "install",
                pkg,
                "--target", str(target_dir),
                "--upgrade",
                "--no-warn-script-location",
                "--quiet",
            ],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            console.print("[green]✓[/green]")
        else:
            console.print("[red]✗[/red]")
            if result.stderr:
                console.print(f"    [dim]{result.stderr.strip()[-200:]}[/dim]")
            failed.append(pkg_name)

    return failed


def install_from_requirements(req_file: Path, target_dir: Path,
                               python_exe: Path | None = None) -> list[str]:
    """Install all packages from a requirements.txt (skips comments)."""
    packages = []
    for line in req_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            packages.append(line)
    return install_packages(packages, target_dir, python_exe)


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
        "assemblyai": "assemblyai",
        "elevenlabs": "elevenlabs",
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

    usb_root = find_usb_root()
    dirs = get_env_dirs(usb_root)

    console.print(f"  USB root detected : [bold]{usb_root}[/bold]")
    console.print(f"  Env target        : [bold]{dirs['env']}[/bold]")
    console.print(f"  Running on        : [bold]{current_os}[/bold]")
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
    install_voice     = _confirm("  Voice pipeline (assemblyai, elevenlabs, Pillow)?", default=False)

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

    # ---- Linux setup -------------------------------------------------------
    if setup_linux:
        console.rule("Linux packages")
        site = dirs["linux_site"]
        site.mkdir(parents=True, exist_ok=True)
        console.print(f"  Target: {site}\n")

        failed_linux = install_packages(packages_to_install, site)

        if failed_linux:
            console.print(f"\n  [yellow]⚠ {len(failed_linux)} package(s) failed on Linux:[/yellow]")
            for p in failed_linux:
                console.print(f"    - {p}")
        else:
            console.print(f"\n  [green]✓ All packages installed for Linux.[/green]")

        size = _dir_size_mb(dirs["linux"])
        console.print(f"  Linux env size: {size:.0f} MB")

    # ---- Windows setup -----------------------------------------------------
    if setup_win:
        console.rule("Windows portable Python")

        ok = setup_windows_embedded_python(dirs)
        if not ok:
            console.print("  [red]✗ Could not set up portable Python. Skipping Windows packages.[/red]")
        else:
            ok = install_pip_into_embedded(dirs["win_python"])
            if ok:
                console.print("\n  Installing packages into portable Python ...\n")
                failed_win = install_packages(
                    packages_to_install,
                    dirs["win_site"],
                    python_exe=dirs["win_python"]
                )
                if failed_win:
                    console.print(f"\n  [yellow]⚠ {len(failed_win)} package(s) failed on Windows:[/yellow]")
                    for p in failed_win:
                        console.print(f"    - {p}")
                else:
                    console.print(f"\n  [green]✓ All packages installed for Windows.[/green]")

                size = _dir_size_mb(dirs["win"])
                console.print(f"  Windows env size: {size:.0f} MB")

    # ---- Launcher scripts --------------------------------------------------
    console.rule("Launcher scripts")
    write_launchers(usb_root)

    # ---- pyaudio note ------------------------------------------------------
    if install_voice:
        print()
        _panel(
            "pyaudio (voice recording)",
            "pyaudio requires a system-level PortAudio library and cannot be\n"
            "bundled on the USB. Users will need to install it once:\n\n"
            "  Linux:  sudo apt install portaudio19-dev && pip install pyaudio\n"
            "  macOS:  brew install portaudio && pip install pyaudio\n"
            "  Windows: pip install pyaudio  (wheel includes PortAudio)\n\n"
            "All other voice deps (assemblyai, elevenlabs, Pillow) are bundled.",
            style="yellow"
        )

    # ---- Summary -----------------------------------------------------------
    console.rule("Done")

    total_size = _dir_size_mb(dirs["env"])
    _panel(
        "USB environment ready",
        f"Total python-env/ size : {total_size:.0f} MB\n\n"
        "To launch carry-ai from the USB:\n\n"
        "  Windows  →  double-click start.bat\n"
        "             or: python-env\\windows\\python.exe carry-ai\\launcher.py\n\n"
        "  Linux    →  bash start.sh\n"
        "             or: PYTHONPATH=python-env/linux/site-packages python3 carry-ai/launcher.py\n\n"
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
