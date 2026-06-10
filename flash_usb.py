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
# Models marked gated=True require a HuggingFace token (Gemma, Llama).
# The user is prompted for their token at download time if any gated
# model is selected.  Get a token at: https://huggingface.co/settings/tokens
# and accept the model license on its HuggingFace page first.
GGUF_MODELS = [
    # ── Tiny / emergency (≤2 GB RAM) ──────────────────────────────────────
    {
        "name": "Qwen2.5 1.5B Q4_K_M",
        "ram_gb": 2, "size_gb": 1.1,
        "description": "Tiny fallback — fits any machine",
        "hf_repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "hf_file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "gated": False,
    },
    {
        "name": "Gemma 3 1B Q4_K_M",
        "ram_gb": 2, "size_gb": 0.8,
        "description": "Google Gemma, tiny, good quality [needs HF token]",
        "hf_repo": "bartowski/gemma-3-1b-it-GGUF",
        "hf_file": "gemma-3-1b-it-Q4_K_M.gguf",
        "gated": True,
    },
    # ── 3–4 GB RAM ──────────────────────────────────────────────────────────
    {
        "name": "Qwen2.5 3B Q4_K_M",
        "ram_gb": 4, "size_gb": 2.0,
        "description": "Good quality, 4 GB RAM",
        "hf_repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "hf_file": "qwen2.5-3b-instruct-q4_k_m.gguf",
        "gated": False,
    },
    {
        "name": "Phi-3.5 Mini Q4_K_M",
        "ram_gb": 4, "size_gb": 2.2,
        "description": "Microsoft Phi-3.5, great reasoning, 4 GB RAM",
        "hf_repo": "bartowski/Phi-3.5-mini-instruct-GGUF",
        "hf_file": "Phi-3.5-mini-instruct-Q4_K_M.gguf",
        "gated": False,
    },
    # ── 5–6 GB RAM ──────────────────────────────────────────────────────────
    {
        "name": "Phi-4-mini Q4_K_M",
        "ram_gb": 5, "size_gb": 2.4,
        "description": "Strong reasoning + tool calling, 5 GB RAM",
        "hf_repo": "bartowski/Phi-4-mini-instruct-GGUF",
        "hf_file": "Phi-4-mini-instruct-Q4_K_M.gguf",
        "gated": False,
    },
    {
        "name": "Mistral 7B v0.3 Q4_K_M",
        "ram_gb": 6, "size_gb": 4.4,
        "description": "Mistral's classic 7B — fast, reliable, 6 GB RAM",
        "hf_repo": "bartowski/Mistral-7B-Instruct-v0.3-GGUF",
        "hf_file": "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
        "gated": False,
    },
    {
        "name": "Gemma 4 E4B Q4_K_M",
        "ram_gb": 6, "size_gb": 3.1,
        "description": "Multimodal vision, 6 GB RAM [needs HF token]",
        "hf_repo": "bartowski/gemma-4-e4b-GGUF",
        "hf_file": "gemma-4-e4b-Q4_K_M.gguf",
        "gated": True,
    },
    {
        "name": "Qwen2.5 7B Q4_K_M",
        "ram_gb": 6, "size_gb": 4.7,
        "description": "Strong all-around model, 6 GB RAM",
        "hf_repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "hf_file": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "gated": False,
    },
    # ── 8–10 GB RAM ─────────────────────────────────────────────────────────
    {
        "name": "Llama 3.1 8B Q4_K_M",
        "ram_gb": 8, "size_gb": 4.9,
        "description": "Meta Llama 3.1 — excellent general purpose [needs HF token]",
        "hf_repo": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "hf_file": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        "gated": True,
        "license_url": "https://huggingface.co/meta-llama/Meta-Llama-3.1-8B-Instruct",
        "license_note": "Accept Meta license at meta-llama/Meta-Llama-3.1-8B-Instruct",
    },
    {
        "name": "DeepSeek-R1 7B Q4_K_M",
        "ram_gb": 8, "size_gb": 4.7,
        "description": "DeepSeek reasoning model, great for code+math, 8 GB RAM",
        "hf_repo": "bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        "hf_file": "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf",
        "gated": False,
    },
    {
        "name": "Qwen2.5 7B Q8_0",
        "ram_gb": 10, "size_gb": 8.1,
        "description": "High quality 7B, 10 GB RAM",
        "hf_repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "hf_file": "qwen2.5-7b-instruct-q8_0.gguf",
        "gated": False,
    },
    # ── 12+ GB RAM ──────────────────────────────────────────────────────────
    {
        "name": "Qwen2.5 14B Q4_K_M",
        "ram_gb": 12, "size_gb": 9.0,
        "description": "Best quality, 12 GB RAM, tool calling",
        "hf_repo": "Qwen/Qwen2.5-14B-Instruct-GGUF",
        "hf_file": "qwen2.5-14b-instruct-q4_k_m.gguf",
        "gated": False,
    },
    {
        "name": "Mistral Nemo 12B Q4_K_M",
        "ram_gb": 12, "size_gb": 7.1,
        "description": "Mistral + Nvidia 12B — multilingual, long context, 12 GB RAM",
        "hf_repo": "bartowski/Mistral-Nemo-Instruct-2407-GGUF",
        "hf_file": "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf",
        "gated": False,
    },
    # ── Coding specialist ───────────────────────────────────────────────────
    {
        "name": "DeepSeek-Coder-V2 Lite Q4_K_M",
        "ram_gb": 12, "size_gb": 9.7,
        "description": "Best open-source coding model, 12 GB RAM",
        "hf_repo": "bartowski/DeepSeek-Coder-V2-Lite-Instruct-GGUF",
        "hf_file": "DeepSeek-Coder-V2-Lite-Instruct-Q4_K_M.gguf",
        "gated": False,
    },
]

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
    result = subprocess.run([str(python_exe), str(get_pip_dest), "--no-warn-script-location", "-q"],
                            check=False, capture_output=True, text=True)
    get_pip_dest.unlink(missing_ok=True)
    if result.returncode != 0:
        console.print(f"  [red]✗ pip install into portable Python failed:[/red] "
                      f"{(result.stderr or result.stdout)[-300:]}")
        return False

    console.print(f"  [green]✓[/green] Portable Python ready.")
    return python_exe.exists()


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
    want_voice    = _confirm("  Include voice pipeline? (assemblyai, elevenlabs, Pillow)", default=False)
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
        models_dir = usb_root / "models"
        for m in chosen_models:
            ok = download_gguf_model(m, models_dir, hf_token=hf_token)
            if not ok:
                console.print(f"  [yellow]⚠ Failed to download {m['name']}[/yellow]")

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
    models_dir = usb_root / "models"

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
    recommended = next((m for m in reversed(GGUF_MODELS) if m["ram_gb"] <= ram), GGUF_MODELS[0])

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
