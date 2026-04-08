"""
carry-ai/flash_usb.py — USB Drive Flasher
==========================================
Rufus-style one-shot script that turns a USB drive into a fully
self-contained carry-ai device. Run this on your main computer:

    python flash_usb.py

What it does:
  1. Detects all removable USB drives plugged into this machine
  2. You pick which drive to flash
  3. Copies carry-ai source code onto the USB
  4. Downloads + installs Python packages into USB-local site-packages
     (so the target machine needs NO pip install)
  5. On Windows target: downloads a portable Python interpreter onto the USB
     (so Windows hosts need NO Python installed at all)
  6. Asks which GGUF models to download and saves them to USB/models/
  7. Asks if voice mode is needed (downloads assemblyai, elevenlabs, Pillow)
  8. Writes start.bat, start.sh, autorun.inf
  9. Verifies the flash succeeded

After flashing, the USB is ready. Plug it into any machine and run:
  Windows → start.bat    (no Python needed if portable Python was included)
  Linux   → bash start.sh (needs Python 3.10+ on host)
"""

import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

try:
    import psutil
except ImportError:
    psutil = None

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt, Confirm
    from rich.rule import Rule
    _RICH = True
    console = Console()
except ImportError:
    _RICH = False

    class _Plain:
        def print(self, *a, **kw):
            import re as _re
            text = " ".join(str(x) for x in a)
            text = _re.sub(r'\[/?[^\]]+\]', '', text)
            print(text)
        def rule(self, title=""):
            print(f"\n{'─'*60}")
            if title:
                print(f"  {title}")

    console = _Plain()

PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON_VERSION = "3.11.9"
PYTHON_EMBED_URL_WIN64 = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# ---------------------------------------------------------------------------
# GGUF model catalogue
# ---------------------------------------------------------------------------
# NOTE: Only non-gated models here. Gemma/Llama models on HuggingFace are
# gated (require accepting a license + auth token). Use Qwen/Phi which are
# freely downloadable without authentication.
GGUF_MODELS = [
    {
        "name": "Qwen2.5 1.5B Q4_K_M",
        "ram_gb": 2, "size_gb": 1.1,
        "description": "Tiny fallback — fits any machine",
        "hf_repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "hf_file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
    },
    {
        "name": "Qwen2.5 3B Q4_K_M",
        "ram_gb": 4, "size_gb": 2.0,
        "description": "Good quality, 4 GB RAM",
        "hf_repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "hf_file": "qwen2.5-3b-instruct-q4_k_m.gguf",
    },
    {
        "name": "Phi-4-mini Q4_K_M",
        "ram_gb": 5, "size_gb": 2.4,
        "description": "Strong reasoning + tool calling, 5 GB RAM",
        "hf_repo": "bartowski/Phi-4-mini-instruct-GGUF",
        "hf_file": "Phi-4-mini-instruct-Q4_K_M.gguf",
    },
    {
        "name": "Qwen2.5 7B Q4_K_M",
        "ram_gb": 6, "size_gb": 4.7,
        "description": "Strong all-around model, 6 GB RAM",
        "hf_repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "hf_file": "qwen2.5-7b-instruct-q4_k_m.gguf",
    },
    {
        "name": "Qwen2.5 7B Q8_0",
        "ram_gb": 10, "size_gb": 8.1,
        "description": "High quality 7B, 10 GB RAM",
        "hf_repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "hf_file": "qwen2.5-7b-instruct-q8_0.gguf",
    },
    {
        "name": "Qwen2.5 14B Q4_K_M",
        "ram_gb": 12, "size_gb": 9.0,
        "description": "Best quality, 12 GB RAM, tool calling",
        "hf_repo": "Qwen/Qwen2.5-14B-Instruct-GGUF",
        "hf_file": "qwen2.5-14b-instruct-q4_k_m.gguf",
    },
]

# ---------------------------------------------------------------------------
# Package groups
# ---------------------------------------------------------------------------
PACKAGE_GROUPS = {
    "core": [
        "psutil>=5.9.0",
        "cryptography>=41.0.0",
        "flask>=3.0.0",
        "requests>=2.31.0",
        "rich>=13.7.0",
    ],
    "providers": [
        "anthropic>=0.30.0",
        "openai>=1.30.0",
        "google-auth>=2.29.0",
        "google-auth-oauthlib>=1.2.0",
    ],
    "models": ["huggingface-hub>=0.23.0"],
    "tools": ["pyautogui>=0.9.54", "pyperclip>=1.8.2"],
    "voice": ["assemblyai>=0.24.0", "elevenlabs>=1.2.0", "Pillow>=10.0.0"],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _c(text: str) -> str:
    """Strip rich markup for plain-text output."""
    return re.sub(r'\[/?[^\]]+\]', '', text)

def _confirm(prompt: str, default: bool = True) -> bool:
    if _RICH:
        return Confirm.ask(prompt, default=default)
    tag = "Y/n" if default else "y/N"
    ans = input(f"{_c(prompt)} [{tag}]: ").strip().lower()
    return default if not ans else ans in ("y", "yes")

def _prompt(prompt: str, default: str = "") -> str:
    if _RICH:
        return Prompt.ask(prompt, default=default)
    val = input(f"{_c(prompt)}{' [' + default + ']' if default else ''}: ").strip()
    return val or default

def _panel(title: str, body: str = "", style: str = "blue") -> None:
    if _RICH:
        console.print(Panel(body or title, title=title if body else "", border_style=style))
    else:
        console.rule(title)
        if body:
            for line in body.strip().splitlines():
                print(f"  {line}")
        print()

def _step(n: int, title: str) -> None:
    if _RICH:
        from rich.rule import Rule
        console.print()
        console.print(Rule(f"[bold cyan]Step {n}[/bold cyan] — {title}", style="cyan"))
        console.print()
    else:
        print(f"\n{'═'*60}")
        print(f"  Step {n} — {title}")
        print(f"{'═'*60}\n")


# ---------------------------------------------------------------------------
# Drive detection
# ---------------------------------------------------------------------------

def detect_usb_drives() -> list[dict]:
    """Return removable drives. Uses psutil if available."""
    drives = []

    if psutil is not None:
        for part in psutil.disk_partitions(all=False):
            # Windows: 'removable' in opts; Linux: check /sys/block
            is_removable = False
            opts = part.opts.lower() if part.opts else ""

            if platform.system().lower() == "windows":
                is_removable = "removable" in opts
            else:
                # Linux: derive block device name from mountpoint
                dev = part.device  # e.g. /dev/sdb1
                block = re.sub(r'\d+$', '', dev.replace('/dev/', ''))  # sdb
                removable_path = Path(f"/sys/block/{block}/removable")
                if removable_path.exists():
                    is_removable = removable_path.read_text().strip() == "1"

            if not is_removable:
                continue

            try:
                usage = psutil.disk_usage(part.mountpoint)
                total_gb = usage.total / (1024 ** 3)
                free_gb = usage.free / (1024 ** 3)
            except (PermissionError, OSError):
                total_gb = free_gb = 0.0

            label = _get_drive_label(part.mountpoint, part.device)

            drives.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype or "unknown",
                "label": label,
                "total_gb": total_gb,
                "free_gb": free_gb,
                "display": f"{part.mountpoint} — {label} ({free_gb:.1f} GB free)",
            })
    else:
        # Minimal fallback: list common mount points
        candidates = []
        if platform.system().lower() == "windows":
            import string
            for letter in string.ascii_uppercase:
                mp = f"{letter}:\\"
                if os.path.exists(mp) and mp != os.path.splitdrive(sys.executable)[0] + "\\":
                    candidates.append(mp)
        else:
            for mp in ["/media", "/mnt", "/run/media"]:
                if os.path.isdir(mp):
                    for sub in Path(mp).iterdir():
                        candidates.append(str(sub))

        for mp in candidates:
            try:
                usage = shutil.disk_usage(mp)
                drives.append({
                    "device": mp,
                    "mountpoint": mp,
                    "fstype": "unknown",
                    "label": Path(mp).name or mp,
                    "total_gb": usage.total / (1024**3),
                    "free_gb": usage.free / (1024**3),
                    "display": f"{mp} ({usage.free/(1024**3):.1f} GB free)",
                })
            except OSError:
                pass

    return drives


def _get_drive_label(mountpoint: str, device: str) -> str:
    """Best-effort volume label."""
    if platform.system().lower() == "windows":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            ctypes.windll.kernel32.GetVolumeInformationW(
                mountpoint, buf, ctypes.sizeof(buf),
                None, None, None, None, 0
            )
            if buf.value:
                return buf.value
        except Exception:
            pass
    elif platform.system().lower() == "linux":
        try:
            result = subprocess.run(
                ["lsblk", "-no", "LABEL", device],
                capture_output=True, text=True, timeout=3
            )
            label = result.stdout.strip()
            if label:
                return label
        except Exception:
            pass
    return Path(mountpoint).name or "USB Drive"


def print_drive_table(drives: list[dict]) -> None:
    if _RICH:
        t = Table(show_header=True, header_style="bold")
        t.add_column("#", style="cyan", width=3)
        t.add_column("Label")
        t.add_column("Mount")
        t.add_column("Total", justify="right")
        t.add_column("Free", justify="right")
        t.add_column("FS")
        for i, d in enumerate(drives, 1):
            t.add_row(
                str(i), d["label"], d["mountpoint"],
                f"{d['total_gb']:.1f} GB", f"{d['free_gb']:.1f} GB", d["fstype"]
            )
        console.print(t)
    else:
        print(f"  {'#':<3} {'Label':<20} {'Mount':<20} {'Free':>8}")
        print(f"  {'-'*55}")
        for i, d in enumerate(drives, 1):
            print(f"  {i:<3} {d['label']:<20} {d['mountpoint']:<20} {d['free_gb']:>6.1f} GB")


# ---------------------------------------------------------------------------
# Host RAM detection
# ---------------------------------------------------------------------------

def detect_host_ram() -> float:
    if psutil is not None:
        return psutil.virtual_memory().available / (1024 ** 3)
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 ** 2)
    except OSError:
        pass
    return 8.0

# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def copy_carry_ai(source_dir: Path, dest_dir: Path) -> None:
    """Copy carry-ai source to dest_dir, skipping cache/dev artifacts."""
    ignore = shutil.ignore_patterns(
        "__pycache__", "*.pyc", "*.pyo", ".git", ".gitignore",
        "_dry_run_session", "python-env", "*.egg-info",
    )
    if dest_dir.exists():
        if not _confirm(
            f"  [yellow]{dest_dir}[/yellow] already exists. Overwrite?", default=True
        ):
            console.print("  Skipping copy.")
            return
        shutil.rmtree(dest_dir)

    console.print(f"  Copying {source_dir} → {dest_dir} ...")
    shutil.copytree(source_dir, dest_dir, ignore=ignore)
    console.print(f"  [green]✓[/green] Copied ({_dir_mb(dest_dir):.0f} MB)")


def _dir_mb(path: Path) -> float:
    if not path.is_dir():
        return 0.0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

def download_file_with_progress(url: str, dest: Path, label: str = "",
                                hf_token: str = "") -> bool:
    """Download url → dest with a simple inline progress bar."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    label = label or dest.name
    try:
        headers = {"User-Agent": "carry-ai/flash_usb"}
        if hf_token:
            headers["Authorization"] = f"Bearer {hf_token}"
        req = Request(url, headers=headers)
        with urlopen(req, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            written = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    if total:
                        pct = written * 100 // total
                        mb = written / 1_048_576
                        tmb = total / 1_048_576
                        print(f"\r    {label}: {mb:.1f}/{tmb:.1f} MB ({pct}%)", end="", flush=True)
            print()  # newline after progress
        return True
    except (URLError, HTTPError) as e:
        print()
        console.print(f"  [red]✗ Download failed:[/red] {e}")
        if dest.exists():
            dest.unlink()
        return False


def download_gguf_model(model: dict, models_dir: Path, hf_token: str = "") -> bool:
    """Download a GGUF model from HuggingFace onto the USB."""
    models_dir.mkdir(parents=True, exist_ok=True)
    dest = models_dir / model["hf_file"]
    if dest.exists():
        size_gb = dest.stat().st_size / (1024**3)
        console.print(f"  [green]✓[/green] Already present: {dest.name} ({size_gb:.1f} GB)")
        return True
    url = f"https://huggingface.co/{model['hf_repo']}/resolve/main/{model['hf_file']}"
    console.print(f"  Downloading {model['name']} (~{model['size_gb']:.1f} GB) ...")
    console.print(f"    URL: {url}")
    ok = download_file_with_progress(url, dest, model["hf_file"], hf_token=hf_token)
    if not ok:
        console.print(
            f"  [yellow]Tip:[/yellow] If this is a gated model (Gemma, Llama), you need a\n"
            f"  HuggingFace token. Get one at https://huggingface.co/settings/tokens\n"
            f"  Then set: HF_TOKEN=hf_xxx python flash_usb.py"
        )
    return ok


# ---------------------------------------------------------------------------
# Portable Python (Windows)
# ---------------------------------------------------------------------------

def setup_portable_python_windows(win_env_dir: Path) -> bool:
    """Download + extract embeddable Python for Windows onto the USB."""
    python_exe = win_env_dir / "python.exe"
    if python_exe.exists():
        console.print(f"  [green]✓[/green] Portable Python already present.")
        return True

    win_env_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"  Downloading portable Python {PYTHON_VERSION} (~10 MB) ...")

    zip_dest = win_env_dir / "python-embed.zip"
    if not download_file_with_progress(PYTHON_EMBED_URL_WIN64, zip_dest, "python-embed.zip"):
        return False

    console.print("  Extracting ...")
    with zipfile.ZipFile(zip_dest) as zf:
        zf.extractall(win_env_dir)
    zip_dest.unlink()

    # Patch ._pth to enable site-packages
    for pth in win_env_dir.glob("python*._pth"):
        txt = pth.read_text(encoding="utf-8")
        txt = txt.replace("#import site", "import site")
        if "Lib\\site-packages" not in txt:
            txt += "\nLib\\site-packages\n"
        pth.write_text(txt, encoding="utf-8")

    console.print("  Installing pip into portable Python ...")
    get_pip_dest = win_env_dir / "get-pip.py"
    if not download_file_with_progress(GET_PIP_URL, get_pip_dest, "get-pip.py"):
        return False
    subprocess.run([str(python_exe), str(get_pip_dest), "--no-warn-script-location", "-q"],
                   check=False)
    get_pip_dest.unlink(missing_ok=True)

    console.print(f"  [green]✓[/green] Portable Python ready.")
    return python_exe.exists()


# ---------------------------------------------------------------------------
# Package installation
# ---------------------------------------------------------------------------

def install_packages(packages: list[str], target_dir: Path,
                     python_exe: Path | None = None) -> list[str]:
    """pip install --target each package. Returns names of failures."""
    target_dir.mkdir(parents=True, exist_ok=True)
    interp = str(python_exe) if python_exe else sys.executable
    failed = []
    for pkg in packages:
        name = re.split(r'[><=!]', pkg)[0].strip()
        console.print(f"  Installing [cyan]{name}[/cyan] ...", end=" ")
        r = subprocess.run(
            [interp, "-m", "pip", "install", pkg,
             "--target", str(target_dir),
             "--upgrade", "--no-warn-script-location", "-q"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            console.print("[green]✓[/green]")
        else:
            console.print("[red]✗[/red]")
            failed.append(name)
    return failed


# ---------------------------------------------------------------------------
# Launcher scripts
# ---------------------------------------------------------------------------

_START_BAT = r"""@echo off
SETLOCAL
SET "USB_ROOT=%~dp0"
SET "USB_ROOT=%USB_ROOT:~0,-1%"
SET "USB_PYTHON=%USB_ROOT%\python-env\windows\python.exe"
SET "BOOTSTRAP=%USB_ROOT%\carry-ai\bootstrap.py"
IF EXIST "%USB_PYTHON%" (
    "%USB_PYTHON%" "%BOOTSTRAP%" %*
) ELSE (
    WHERE python >/dev/null 2>&1 && python "%BOOTSTRAP%" %* || (
        echo [carry-ai] Python not found. Install from https://python.org or run setup_usb.py.
        PAUSE
    )
)
ENDLOCAL
"""

_START_SH = """#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USB_SITE="$SCRIPT_DIR/python-env/linux/site-packages"
BOOTSTRAP="$SCRIPT_DIR/carry-ai/bootstrap.py"
[ -d "$USB_SITE" ] && export PYTHONPATH="$USB_SITE${PYTHONPATH:+:$PYTHONPATH}"
PY=$(command -v python3 || command -v python || echo "")
[ -z "$PY" ] && { echo "[carry-ai] Python 3.10+ required."; exit 1; }
exec "$PY" "$BOOTSTRAP" "$@"
"""

_AUTORUN = "[autorun]\nopen=start.bat\nlabel=carry-ai\n"

def write_launcher_scripts(usb_root: Path) -> None:
    (usb_root / "start.bat").write_text(_START_BAT, encoding="utf-8")
    sh = usb_root / "start.sh"
    sh.write_text(_START_SH, encoding="utf-8")
    try:
        sh.chmod(0o755)
    except OSError:
        pass
    inf = usb_root / "autorun.inf"
    if not inf.exists():
        inf.write_text(_AUTORUN, encoding="utf-8")
    console.print("  [green]✓[/green] start.bat, start.sh, autorun.inf written.")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_flash(usb_root: Path, want_win: bool, want_linux: bool) -> dict[str, bool]:
    results = {
        "carry-ai/launcher.py":  (usb_root / "carry-ai" / "launcher.py").is_file(),
        "start.bat":             (usb_root / "start.bat").is_file(),
        "start.sh":              (usb_root / "start.sh").is_file(),
    }
    if want_linux:
        results["python-env/linux/site-packages/"] = (
            usb_root / "python-env" / "linux" / "site-packages"
        ).is_dir()
    if want_win:
        results["python-env/windows/python.exe"] = (
            usb_root / "python-env" / "windows" / "python.exe"
        ).is_file()
    return results

# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------

BANNER = r"""
  ____                            _    ___   _   _ ____  ____
 / ___|__ _ _ __ _ __ _   _      / \  |_ _| | | | / ___|| __ )
| |   / _` | '__| '__| | | |    / _ \  | |  | | | \___ \|  _ \
| |__| (_| | |  | |  | |_| |   / ___ \ | |  | |_| |___) | |_) |
 \____\__,_|_|  |_|   \__, |  /_/   \_\___|  \___/|____/|____/
                       |___/          F L A S H E R
"""

def main() -> None:
    print(BANNER)
    _panel(
        "carry-ai USB Flasher",
        "Writes carry-ai + Python packages + GGUF models onto a USB drive.\n"
        "After flashing, the USB runs on any Windows or Linux machine\n"
        "with zero installation on the host.",
        style="cyan",
    )

    host_os = platform.system().lower()
    host_ram = detect_host_ram()

    # ── STEP 1: Detect drives ────────────────────────────────────────────────
    _step(1, "Select USB Drive")

    drives = detect_usb_drives()
    if not drives:
        _panel(
            "No removable drives detected",
            "Insert a USB drive and re-run, or check that it is mounted.\n"
            "If the drive is mounted but not detected as removable,\n"
            "pass its mount path directly:\n\n"
            "  python flash_usb.py --target /media/myusb",
            style="red",
        )
        # Allow --target override
        if "--target" in sys.argv:
            idx = sys.argv.index("--target")
            target_mount = sys.argv[idx + 1]
            drives = [{
                "mountpoint": target_mount,
                "label": Path(target_mount).name,
                "total_gb": 0, "free_gb": 0, "fstype": "unknown",
                "device": target_mount,
                "display": target_mount,
            }]
        else:
            sys.exit(1)

    print_drive_table(drives)
    print()
    console.print(
        "  [yellow]WARNING:[/yellow] flash_usb does NOT format the drive.\n"
        "  It copies files onto the existing filesystem.\n"
        "  Make sure you select the correct drive.\n"
    )

    while True:
        try:
            choice = input("  Select drive number (or q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if choice.lower() == "q":
            sys.exit(0)
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(drives):
                target = drives[idx]
                break
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(drives)}.")

    usb_root = Path(target["mountpoint"])
    console.print(f"\n  [green]Target:[/green] {usb_root}  ({target['label']})\n")

    # ── STEP 2: Configuration ────────────────────────────────────────────────
    _step(2, "Configure Flash")

    console.print(f"  Host RAM available : [bold]{host_ram:.1f} GB[/bold]")
    recommended = next(
        (m for m in reversed(GGUF_MODELS) if m["ram_gb"] <= host_ram),
        GGUF_MODELS[0],
    )
    console.print(f"  Recommended model  : [bold]{recommended['name']}[/bold] ({recommended['description']})\n")

    want_win   = _confirm("  Bundle portable Python for Windows? (~400 MB, no Python needed on host)", default=True)
    want_linux = _confirm("  Bundle Linux packages? (~200 MB, no pip install on host)", default=True)
    want_prov  = _confirm("  Include LLM provider SDKs? (anthropic, openai, google-auth)", default=True)
    want_tools = _confirm("  Include agent tools? (pyautogui, pyperclip)", default=True)
    want_voice = _confirm("  Include voice pipeline? (assemblyai, elevenlabs, Pillow)", default=False)
    print()

    # Model selection
    console.print("  [bold]Available GGUF models:[/bold]")
    for i, m in enumerate(GGUF_MODELS, 1):
        tag = " [green]← recommended[/green]" if m["name"] == recommended["name"] else ""
        console.print(f"  [{i}] {m['name']}  ~{m['size_gb']:.1f} GB  |  {m['ram_gb']} GB RAM  —  {m['description']}{tag}")
    print()
    model_input = input(
        "  Download models? (comma-separated numbers, Enter to skip, r for recommended): "
    ).strip().lower()

    chosen_models: list[dict] = []
    if model_input == "r":
        chosen_models = [recommended]
    elif model_input:
        for part in model_input.split(","):
            try:
                mi = int(part.strip()) - 1
                if 0 <= mi < len(GGUF_MODELS):
                    chosen_models.append(GGUF_MODELS[mi])
            except ValueError:
                pass

    # Build package list
    packages: list[str] = list(PACKAGE_GROUPS["core"])
    if want_prov:
        packages += PACKAGE_GROUPS["providers"]
    if want_tools:
        packages += PACKAGE_GROUPS["tools"]
    if want_voice:
        packages += PACKAGE_GROUPS["voice"]

    # Disk space estimate
    est_gb = 0.2  # carry-ai source
    if want_linux:
        est_gb += 0.3 + (0.3 if want_prov else 0) + (0.1 if want_voice else 0)
    if want_win:
        est_gb += 0.4 + (0.3 if want_prov else 0) + 0.1  # portable python
    est_gb += sum(m["size_gb"] for m in chosen_models)

    console.print(f"\n  Estimated USB usage : [bold]~{est_gb:.1f} GB[/bold]")
    if target["free_gb"] > 0 and est_gb > target["free_gb"]:
        console.print(f"  [red]✗ Drive only has {target['free_gb']:.1f} GB free — may not fit![/red]")
        if not _confirm("  Continue anyway?", default=False):
            sys.exit(1)

    if not _confirm("\n  Ready to flash. Proceed?", default=True):
        sys.exit(0)

    # ── STEP 3: Flash ────────────────────────────────────────────────────────
    _step(3, "Flashing USB")

    # 3a — copy carry-ai source
    console.print("[bold]3a.[/bold] Copying carry-ai source ...")
    copy_carry_ai(PROJECT_ROOT, usb_root / "carry-ai")

    # 3b — launcher scripts
    console.print("\n[bold]3b.[/bold] Writing launcher scripts ...")
    write_launcher_scripts(usb_root)

    # 3c — Linux packages
    if want_linux:
        console.print("\n[bold]3c.[/bold] Installing Linux packages ...")
        linux_site = usb_root / "python-env" / "linux" / "site-packages"
        failed_linux = install_packages(packages, linux_site)
        if failed_linux:
            console.print(f"  [yellow]⚠ Failed:[/yellow] {', '.join(failed_linux)}")

    # 3d — Windows portable Python + packages
    if want_win:
        console.print("\n[bold]3d.[/bold] Setting up Windows portable Python ...")
        win_dir = usb_root / "python-env" / "windows"
        ok = setup_portable_python_windows(win_dir)
        if ok:
            console.print("\n  Installing packages into portable Python ...")
            win_site = win_dir / "Lib" / "site-packages"
            failed_win = install_packages(packages, win_site, python_exe=win_dir / "python.exe")
            if failed_win:
                console.print(f"  [yellow]⚠ Failed:[/yellow] {', '.join(failed_win)}")

    # 3e — GGUF models
    if chosen_models:
        console.print(f"\n[bold]3e.[/bold] Downloading {len(chosen_models)} GGUF model(s) ...")
        hf_token = os.environ.get("HF_TOKEN", "")
        models_dir = usb_root / "models"
        for m in chosen_models:
            ok = download_gguf_model(m, models_dir, hf_token=hf_token)
            if not ok:
                console.print(f"  [yellow]⚠ Failed to download {m['name']}[/yellow]")

    # ── STEP 4: Verify ───────────────────────────────────────────────────────
    _step(4, "Verification")

    results = verify_flash(usb_root, want_win=want_win, want_linux=want_linux)
    all_ok = True
    for check, passed in results.items():
        icon = "[green]✓[/green]" if passed else "[red]✗[/red]"
        console.print(f"  {icon}  {check}")
        if not passed:
            all_ok = False

    # ── STEP 5: Done ─────────────────────────────────────────────────────────
    _step(5, "Done")

    total_mb = _dir_mb(usb_root / "python-env") + _dir_mb(usb_root / "carry-ai")
    status = "green" if all_ok else "yellow"

    body = (
        f"USB written: {usb_root}\n"
        f"Total size:  ~{total_mb/1024:.1f} GB\n\n"
        "To launch carry-ai from the USB:\n\n"
        "  Windows  →  insert USB, double-click start.bat\n"
        "              (no Python required — portable Python is bundled)\n\n"
        "  Linux    →  insert USB, open terminal:\n"
        "              bash /media/<user>/<drive>/start.sh\n\n"
    )
    if want_voice:
        body += (
            "Voice mode note:\n"
            "  pyaudio needs PortAudio on the host (cannot be bundled):\n"
            "    Linux:   sudo apt install portaudio19-dev && pip install pyaudio\n"
            "    Windows: pip install pyaudio\n\n"
        )
    body += "Run  python carry-ai/onboard.py  on the target machine for first-time setup."

    _panel("Your USB is ready!" if all_ok else "Flash complete (with warnings)", body, style=status)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(0)
