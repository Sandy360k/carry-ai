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
  7. Asks if voice mode is needed (sherpa-onnx offline speech + its models,
     PyAudio on Windows, optional ElevenLabs SDK)
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

# ---------------------------------------------------------------------------
# GGUF model catalogue — shared with the boot selector (models/catalog.py),
# listed smallest-first for the picker.
# ---------------------------------------------------------------------------
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from models.catalog import CATALOG, mmproj_filename, mmproj_url, recommend_for_ram  # noqa: E402
from portable import runtime  # noqa: E402

GGUF_MODELS = sorted(CATALOG, key=lambda m: m["ram_gb"])

# ---------------------------------------------------------------------------
# LocalAI binary catalogue
# ---------------------------------------------------------------------------
LOCALAI_VERSION = "v2.25.0"
LOCALAI_BINARIES = {
    "linux_x86_64": {
        "url": (f"https://github.com/mudler/LocalAI/releases/download/"
                f"{LOCALAI_VERSION}/local-ai-Linux-x86_64"),
        "filename": "local-ai",
        "size_mb": 300,
        "description": "Linux x86_64 (most desktops/servers)",
    },
    "linux_arm64": {
        "url": (f"https://github.com/mudler/LocalAI/releases/download/"
                f"{LOCALAI_VERSION}/local-ai-Linux-arm64"),
        "filename": "local-ai",
        "size_mb": 280,
        "description": "Linux ARM64 (Raspberry Pi 5, Jetson, etc.)",
    },
    "windows_x86_64": {
        "url": (f"https://github.com/mudler/LocalAI/releases/download/"
                f"{LOCALAI_VERSION}/local-ai-Windows-x86_64.exe"),
        "filename": "local-ai.exe",
        "size_mb": 310,
        "description": "Windows 10/11 x86_64",
    },
}

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
        "customtkinter>=5.2.0",
        "qrcode>=7.4",
    ],
    "providers": [
        "anthropic>=0.30.0",
        "openai>=1.30.0",
        "google-auth>=2.29.0",
        "google-auth-oauthlib>=1.2.0",
    ],
    "models": ["huggingface-hub>=0.23.0"],
    "tools": ["pyautogui>=0.9.54", "pyperclip>=1.8.2",
              "pydantic-monty>=1.1.0"],   # run_python sandbox
    # Offline speech (sherpa-onnx) + mic/speaker (PyAudio, Windows only) +
    # ElevenLabs SDK for the optional cloud voice. AssemblyAI is plain REST.
    "voice": ["sherpa-onnx>=1.13.8", "pyaudio>=0.2.14", "elevenlabs>=2.0.0", "Pillow>=10.0.0"],
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
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except OSError:
        pass
    return 8.0

# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def copy_carry_ai(source_dir: Path, dest_dir: Path) -> None:
    """Copy carry-ai source to dest_dir, skipping cache/dev artifacts.

    Downloaded models (models/*.gguf, models/voice/, registry.json) are
    neither copied from this machine nor deleted on the USB.
    """
    base_ignore = shutil.ignore_patterns(
        "__pycache__", "*.pyc", "*.pyo", ".git", ".gitignore",
        "_dry_run_session", "python-env", "*.egg-info",
    )

    def ignore(directory, names):
        skipped = set(base_ignore(directory, names))
        if Path(directory).name == "models":
            skipped |= {n for n in names
                        if n.endswith(".gguf") or n in ("voice", "registry.json")}
        return skipped

    if dest_dir.exists():
        if not _confirm(
            f"  [yellow]{dest_dir}[/yellow] already exists. Overwrite?", default=True
        ):
            console.print("  Skipping copy.")
            return
        # Replace the source but keep downloaded models
        for child in dest_dir.iterdir():
            if child.name == "models":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    console.print(f"  Copying {source_dir} → {dest_dir} ...")
    shutil.copytree(source_dir, dest_dir, ignore=ignore, dirs_exist_ok=True)
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
    if ok and model.get("mmproj"):
        # Vision projector, stored as <model stem>.mmproj.gguf (see catalog)
        proj_dest = models_dir / mmproj_filename(model)
        if not proj_dest.exists():
            console.print("  Downloading vision projector ...")
            if not download_file_with_progress(mmproj_url(model), proj_dest,
                                               proj_dest.name, hf_token=hf_token):
                console.print("  [yellow]Vision projector failed — text chat still works.[/yellow]")
    if not ok:
        console.print(
            f"  [yellow]Tip:[/yellow] If this is a gated model (Gemma, Llama), you need a\n"
            f"  HuggingFace token. Get one at https://huggingface.co/settings/tokens\n"
            f"  Then set: HF_TOKEN=hf_xxx python flash_usb.py"
        )
    return ok


# ---------------------------------------------------------------------------
# Bundled runtimes (portable/runtime.py): Python + llama.cpp per OS
# ---------------------------------------------------------------------------

def _progress(label: str):
    def _cb(written: int, total: int) -> None:
        if total:
            print(f"\r    {label}: {written/1_048_576:.1f}/{total/1_048_576:.1f} MB "
                  f"({written * 100 // total}%)", end="", flush=True)
    return _cb


def _runtime_cache(usb_root: Path) -> Path:
    """Downloads are cached on the flashing host, not on the USB."""
    return Path.home() / ".cache" / "carry-ai-flash"


def setup_portable_python(usb_root: Path, os_name: str) -> bool:
    """Put the pinned, checksum-verified Python for *os_name* onto the USB."""
    console.print(f"  Python {runtime.PYTHON_VERSION} for {os_name} "
                  f"(~{35 if os_name == 'windows' else 25} MB download) ...")
    try:
        exe = runtime.install_python(usb_root, os_name, _runtime_cache(usb_root),
                                     progress=_progress(f"python-{os_name}"))
    except (OSError, ValueError) as e:
        print()
        console.print(f"  [red]✗ Portable Python failed:[/red] {e}")
        return False
    print()
    console.print(f"  [green]✓[/green] {exe.relative_to(usb_root)}")
    return True


def setup_llama_server(usb_root: Path, os_name: str) -> bool:
    """Put the pinned llama-server builds (Vulkan GPU + CPU) onto the USB."""
    console.print(f"  llama.cpp {runtime.LLAMA_BUILD} for {os_name} (Vulkan + CPU) ...")
    try:
        dirs = runtime.install_llama(usb_root, os_name, _runtime_cache(usb_root),
                                     progress=_progress(f"llama-{os_name}"))
    except (OSError, ValueError) as e:
        print()
        console.print(f"  [red]✗ llama.cpp download failed:[/red] {e}")
        return False
    print()
    for d in dirs:
        console.print(f"  [green]✓[/green] {d.relative_to(usb_root)}")
    return True


# ---------------------------------------------------------------------------
# LocalAI binary download
# ---------------------------------------------------------------------------

def download_localai_binary(usb_root: Path) -> bool:
    """
    Download the LocalAI binary for the current OS and place it in USB/bin/.

    The binary is placed at:
        Windows → USB/bin/local-ai.exe
        Linux   → USB/bin/local-ai  (chmod +x)

    Returns True on success.
    """
    import platform as _platform
    os_name = _platform.system().lower()
    arch = _platform.machine().lower()

    if os_name == "windows":
        key = "windows_x86_64"
    elif os_name == "linux" and "arm" in arch or "aarch" in arch:
        key = "linux_arm64"
    else:
        key = "linux_x86_64"

    info = LOCALAI_BINARIES.get(key)
    if not info:
        console.print(f"  [yellow]![/yellow] No LocalAI binary for {os_name}/{arch}")
        return False

    bin_dir = usb_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / info["filename"]

    if dest.exists():
        console.print(f"  [green]✓[/green] LocalAI already present: {dest}")
        return True

    console.print(f"  Downloading LocalAI {LOCALAI_VERSION} for {info['description']}...")
    console.print(f"    Size: ~{info['size_mb']} MB")

    ok = download_file_with_progress(info["url"], dest, label=f"LocalAI {LOCALAI_VERSION}")
    if ok and os_name != "windows":
        try:
            import os as _os
            _os.chmod(dest, 0o755)
        except OSError:
            pass
    return ok


# ---------------------------------------------------------------------------
# Package installation
# ---------------------------------------------------------------------------

def install_packages(packages: list[str], usb_root: Path, os_name: str) -> list[str]:
    """Install *packages* for the bundled Python of *os_name* (from any host).

    Returns names of failures.
    """
    def _report(name: str, ok: bool) -> None:
        console.print(f"  {'[green]✓[/green]' if ok else '[red]✗[/red]'} {name}")
    return runtime.install_packages(packages, usb_root, os_name, report=_report)


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
    "%USB_PYTHON%" "%BOOTSTRAP%" --ui desktop %*
) ELSE (
    WHERE python >nul 2>&1 && python "%BOOTSTRAP%" --ui desktop %* || (
        echo [carry-ai] Python not found. Install from https://python.org or run setup_usb.py.
        PAUSE
    )
)
ENDLOCAL
"""

# The repo's own start.sh (bundled Python, noexec fallback) is the template
_START_SH = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")

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

def verify_flash(usb_root: Path, want_win: bool, want_linux: bool,
                 want_llama: bool = False) -> dict[str, bool]:
    results = {
        "carry-ai/launcher.py":  (usb_root / "carry-ai" / "launcher.py").is_file(),
        "start.bat":             (usb_root / "start.bat").is_file(),
        "start.sh":              (usb_root / "start.sh").is_file(),
    }
    for os_name, wanted in (("linux", want_linux), ("windows", want_win)):
        if not wanted:
            continue
        exe = runtime.python_exe(usb_root, os_name)
        results[str(exe.relative_to(usb_root))] = exe.is_file()
        site = runtime.site_packages(usb_root, os_name)
        results[str(site.relative_to(usb_root)) + "/"] = site.is_dir()
        if want_llama:
            server = "llama-server.exe" if os_name == "windows" else "llama-server"
            for backend, _, _ in runtime.LLAMA_ASSETS[os_name]:
                path = runtime.llama_dir(usb_root, os_name, backend) / server
                results[str(path.relative_to(usb_root))] = path.is_file()
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
    recommended = recommend_for_ram(host_ram)
    console.print(f"  Recommended model  : [bold]{recommended['name']}[/bold] ({recommended['description']})\n")

    want_win   = _confirm("  Bundle Windows runtime? (Python + packages, ~250 MB — nothing to install on the PC)", default=True)
    want_linux = _confirm("  Bundle Linux runtime? (Python + packages, ~300 MB — nothing to install on the PC)", default=True)
    want_llama = _confirm("  Bundle llama.cpp for local models? (Vulkan GPU + CPU, ~140 MB per OS)", default=True)
    want_prov  = _confirm("  Include LLM provider SDKs? (anthropic, openai, google-auth)", default=True)
    want_tools = _confirm("  Include agent tools? (pyautogui, pyperclip)", default=True)
    want_voice    = _confirm("  Include voice? (offline speech with sherpa-onnx + ~86 MB of models)", default=False)
    want_localai  = _confirm(
        "  Download LocalAI binary? (~300 MB, replaces raw llama.cpp — multi-model, "
        "OpenAI-compatible API, optional Whisper STT)",
        default=False,
    )
    print()

    # Model selection
    console.print("  [bold]Available GGUF models:[/bold]")
    console.print("  [dim](Models marked [needs HF token] require a free HuggingFace token)[/dim]\n")
    for i, m in enumerate(GGUF_MODELS, 1):
        tag = " [green]← recommended[/green]" if m["name"] == recommended["name"] else ""
        gated_tag = " [yellow]🔑[/yellow]" if m.get("gated") else ""
        console.print(f"  [{i}] {m['name']}  ~{m['size_gb']:.1f} GB  |  {m['ram_gb']} GB RAM  —  {m['description']}{tag}{gated_tag}")
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

    # If any gated model is selected, prompt for HF token right here
    hf_token = os.environ.get("HF_TOKEN", "")
    needs_token = any(m.get("gated") for m in chosen_models)
    if needs_token and not hf_token:
        print()
        # Build owner-specific license instructions
        owners = set()
        for m in chosen_models:
            if m.get("gated"):
                repo = m["hf_repo"]
                # e.g. "bartowski/gemma-3-1b-it-GGUF" → base model is google/gemma-3-1b-it
                if "gemma" in repo.lower():
                    owners.add("google")
                elif "llama" in repo.lower():
                    owners.add("meta-llama")

        license_lines = ""
        if "google" in owners:
            license_lines += (
                "\n  [bold]Google Gemma[/bold] license:\n"
                "    → https://huggingface.co/google/gemma-3-1b-it\n"
                "    Click 'Agree and access repository' (requires Google account)\n"
            )
        if "meta-llama" in owners:
            license_lines += (
                "\n  [bold]Meta Llama[/bold] license:\n"
                "    → https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct\n"
                "    Click 'Agree and access repository' (requires Meta approval)\n"
            )

        _panel(
            "HuggingFace Token Required",
            "One or more selected models are gated.\n"
            "To download them you need a free HuggingFace token.\n\n"
            "Step 1 — Create a token:\n"
            "  1. Sign up at https://huggingface.co/join (free)\n"
            "  2. Go to https://huggingface.co/settings/tokens\n"
            "  3. Create a token with 'Read' access\n\n"
            "Step 2 — Accept the model license:"
            + license_lines +
            "\n"
            "Step 3 — Paste your token below:",
            style="yellow",
        )
        try:
            hf_token = input("  HuggingFace token (hf_...): ").strip()
        except (EOFError, KeyboardInterrupt):
            hf_token = ""
        if not hf_token:
            console.print("  [yellow]No token entered. Gated models will be skipped.[/yellow]")
            chosen_models = [m for m in chosen_models if not m.get("gated")]

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
        est_gb += 0.15 + 0.15 + (0.3 if want_prov else 0) + (0.1 if want_voice else 0)
    if want_win:
        est_gb += 0.1 + 0.15 + (0.3 if want_prov else 0) + (0.1 if want_voice else 0)
    if want_llama:
        est_gb += 0.19 * want_linux + 0.14 * want_win
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

    # 3c/3d — per-OS runtime: Python, packages, llama.cpp
    for step, os_name, wanted in (("3c", "linux", want_linux), ("3d", "windows", want_win)):
        if not wanted:
            continue
        console.print(f"\n[bold]{step}.[/bold] {os_name.capitalize()} runtime ...")
        if setup_portable_python(usb_root, os_name):
            failed = install_packages(packages, usb_root, os_name)
            if failed:
                console.print(f"  [yellow]⚠ Failed:[/yellow] {', '.join(failed)}")
        if want_llama:
            setup_llama_server(usb_root, os_name)

    # 3e — GGUF models
    if chosen_models:
        console.print(f"\n[bold]3e.[/bold] Downloading {len(chosen_models)} GGUF model(s) ...")
        models_dir = usb_root / "carry-ai" / "models"   # where the app looks
        for m in chosen_models:
            ok = download_gguf_model(m, models_dir, hf_token=hf_token)
            if not ok:
                console.print(f"  [yellow]⚠ Failed to download {m['name']}[/yellow]")

    # 3e2 — offline voice models (sherpa-onnx)
    if want_voice:
        console.print("\n[bold]3e.[/bold] Downloading offline voice models ...")
        try:
            from models import voice as vm
            root = usb_root / "carry-ai" / "models" / "voice"
            for model_id in (vm.DEFAULT_STT, vm.DEFAULT_TTS):
                vm.download(model_id, root=root)
                console.print(f"  [green]✓[/green] {model_id}")
        except Exception as e:
            console.print(f"  [yellow]⚠ Voice models not downloaded ({e}) — "
                          "get them later in the app's Voice settings[/yellow]")

    # 3f — LocalAI binary
    if want_localai:
        console.print("\n[bold]3f.[/bold] Downloading LocalAI binary ...")
        ok = download_localai_binary(usb_root)
        if ok:
            console.print("  [green]✓[/green] LocalAI binary saved to USB/bin/")
        else:
            console.print("  [yellow]⚠ LocalAI download failed — you can add it later[/yellow]")

    # ── STEP 4: Verify ───────────────────────────────────────────────────────
    _step(4, "Verification")

    results = verify_flash(usb_root, want_win=want_win, want_linux=want_linux,
                           want_llama=want_llama)
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
        "              (nothing to install — Python is bundled)\n\n"
        "  Linux    →  insert USB, open terminal:\n"
        "              bash /media/<user>/<drive>/start.sh\n"
        "              (nothing to install — Python is bundled)\n\n"
    )
    if want_voice:
        body += (
            "Voice: click 🎤 in the app (Ctrl+M). Offline speech works out of the box;\n"
            "  on Linux, playback uses aplay (alsa-utils, present on most desktops).\n\n"
        )
    body += "Run  python carry-ai/onboard.py  on the target machine for first-time setup."

    body += (
        "To download more models later:\n"
        "  python flash_usb.py --add-models --target " + str(usb_root) + "\n\n"
        "To update carry-ai (source code + packages only, models untouched):\n"
        "  python update_usb.py --target " + str(usb_root) + "\n"
    )

    _panel("Your USB is ready!" if all_ok else "Flash complete (with warnings)", body, style=status)


# ---------------------------------------------------------------------------
# Add-models mode — download extra models onto an existing USB
# ---------------------------------------------------------------------------

def add_models_wizard(target_path: str) -> None:
    """Interactive wizard to download GGUF models onto an existing USB."""
    usb_root = Path(target_path).resolve()
    models_dir = usb_root / "carry-ai" / "models"   # where the app looks

    _panel(
        "carry-ai — Download Models",
        f"Target: {usb_root}\n"
        f"Models directory: {models_dir}\n",
        style="cyan",
    )

    # Show existing models
    existing = list(models_dir.glob("*.gguf")) if models_dir.is_dir() else []
    if existing:
        console.print("  [bold]Already downloaded:[/bold]")
        for f in existing:
            size_gb = f.stat().st_size / (1024**3)
            console.print(f"    ✓ {f.name}  ({size_gb:.1f} GB)")
        print()

    # Show catalogue
    ram = detect_host_ram()
    recommended = recommend_for_ram(ram)

    console.print(f"  Host RAM: [bold]{ram:.1f} GB[/bold]")
    console.print(f"  Recommended: [bold]{recommended['name']}[/bold]\n")
    console.print("  [bold]Available models:[/bold]")
    console.print("  [dim](Models marked [needs HF token] require a free HuggingFace token)[/dim]\n")

    for i, m in enumerate(GGUF_MODELS, 1):
        tag = " [green]← recommended[/green]" if m["name"] == recommended["name"] else ""
        gated_tag = " [yellow]🔑[/yellow]" if m.get("gated") else ""
        # Mark if already downloaded
        dest = models_dir / m["hf_file"]
        dl_tag = " [dim](already downloaded)[/dim]" if dest.exists() else ""
        console.print(f"  [{i}] {m['name']}  ~{m['size_gb']:.1f} GB  |  {m['ram_gb']} GB RAM  —  {m['description']}{tag}{gated_tag}{dl_tag}")

    print()
    try:
        model_input = input(
            "  Download models? (comma-separated numbers, Enter to quit, r for recommended): "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    chosen: list[dict] = []
    if model_input == "r":
        chosen = [recommended]
    elif model_input:
        for part in model_input.split(","):
            try:
                mi = int(part.strip()) - 1
                if 0 <= mi < len(GGUF_MODELS):
                    chosen.append(GGUF_MODELS[mi])
            except ValueError:
                pass

    if not chosen:
        console.print("  No models selected.")
        return

    # HF token for gated models
    hf_token = os.environ.get("HF_TOKEN", "")
    needs_token = any(m.get("gated") for m in chosen)
    if needs_token and not hf_token:
        owners = set()
        for m in chosen:
            if m.get("gated"):
                if "gemma" in m["hf_repo"].lower():
                    owners.add("google")
                elif "llama" in m["hf_repo"].lower():
                    owners.add("meta-llama")

        license_lines = ""
        if "google" in owners:
            license_lines += (
                "\n  Google Gemma: https://huggingface.co/google/gemma-3-1b-it"
                "\n    → click 'Agree and access repository'\n"
            )
        if "meta-llama" in owners:
            license_lines += (
                "\n  Meta Llama: https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct"
                "\n    → click 'Agree and access repository'\n"
            )

        console.print(
            "\n  [yellow]Gated model selected.[/yellow] You need a HuggingFace token.\n"
            "  Get one at: https://huggingface.co/settings/tokens\n"
            "  Accept the license:" + license_lines
        )
        try:
            hf_token = input("  HuggingFace token (hf_...): ").strip()
        except (EOFError, KeyboardInterrupt):
            hf_token = ""
        if not hf_token:
            console.print("  [yellow]No token. Skipping gated models.[/yellow]")
            chosen = [m for m in chosen if not m.get("gated")]

    # Download
    print()
    for m in chosen:
        download_gguf_model(m, models_dir, hf_token=hf_token)

    # Summary
    all_models = list(models_dir.glob("*.gguf"))
    total_gb = sum(f.stat().st_size for f in all_models) / (1024**3)
    console.print(f"\n  [green]Done.[/green] {len(all_models)} model(s) on USB, {total_gb:.1f} GB total.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        # --add-models mode
        if "--add-models" in sys.argv:
            target = None
            if "--target" in sys.argv:
                idx = sys.argv.index("--target")
                if idx + 1 < len(sys.argv):
                    target = sys.argv[idx + 1]
            if not target:
                # Try to detect from current dir or parent
                if (Path(".") / "carry-ai").is_dir():
                    target = str(Path(".").resolve())
                elif (Path(".").parent / "carry-ai").is_dir():
                    target = str(Path(".").parent.resolve())
                else:
                    target = input("  USB path (e.g. H:\\): ").strip()
            if target:
                add_models_wizard(target)
            else:
                print("Usage: python flash_usb.py --add-models --target H:\\")
        elif "--add-localai" in sys.argv:
            # Download LocalAI binary onto an existing USB
            target = None
            if "--target" in sys.argv:
                idx = sys.argv.index("--target")
                if idx + 1 < len(sys.argv):
                    target = sys.argv[idx + 1]
            if not target:
                target = input("  USB path (e.g. H:\\): ").strip()
            if target:
                usb_path = Path(target).resolve()
                ok = download_localai_binary(usb_path)
                if ok:
                    print(f"  LocalAI binary downloaded to {usb_path / 'bin'}")
                else:
                    print("  Download failed.")
            else:
                print("Usage: python flash_usb.py --add-localai --target H:\\")
        elif "--update" in sys.argv:
            # Delegate to update_usb.py
            update_script = PROJECT_ROOT / "update_usb.py"
            if update_script.exists():
                # Pass through remaining args, replacing --update with nothing
                remaining = [a for a in sys.argv[1:] if a != "--update"]
                result = subprocess.run(
                    [sys.executable, str(update_script)] + remaining
                )
                sys.exit(result.returncode)
            else:
                print("update_usb.py not found.  Run: python update_usb.py")
        else:
            main()
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(0)
