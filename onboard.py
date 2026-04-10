"""
onboard.py — Interactive setup wizard for carry-ai.

Guides the user through system checks, dependency installation, mode
selection, provider key configuration, voice setup, config generation,
and a final dry-run validation.  Runs standalone:

    python onboard.py

No carry-ai runtime is imported here; the wizard only reads/writes
config files and spawns subprocesses.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import re as _re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Rich — optional; define a lightweight fallback so all call-sites work the
# same way whether rich is installed or not.
# ---------------------------------------------------------------------------
try:
    from rich.console import Console as _RichConsole
    from rich.panel import Panel
    from rich.table import Table
    from rich.rule import Rule

    _RICH = True

    console = _RichConsole()

    def _panel(content: str, title: str = "", style: str = "bold cyan") -> None:
        console.print(Panel(content, title=title, border_style=style))

    def _rule(title: str = "") -> None:
        console.print(Rule(title, style="bold blue"))

    def _table(headers: list[str], rows: list[list[str]]) -> None:
        t = Table(show_header=True, header_style="bold magenta")
        for h in headers:
            t.add_column(h)
        for row in rows:
            t.add_row(*row)
        console.print(t)

    def _print(text: str = "", **_kw) -> None:
        console.print(text)

except ImportError:
    _RICH = False

    class _FallbackConsole:  # type: ignore[no-redef]
        def print(self, text: str = "", **_kw) -> None:
            import re
            clean = re.sub(r"\[/?[^\]]*\]", "", str(text))
            print(clean)

        def input(self, prompt: str = "") -> str:
            import re
            clean = re.sub(r"\[/?[^\]]*\]", "", prompt)
            return input(clean)

    console = _FallbackConsole()  # type: ignore[assignment]

    def _panel(content: str, title: str = "", style: str = "") -> None:  # type: ignore[misc]
        import re
        border = "=" * 60
        print(f"\n{border}")
        if title:
            print(f"  {title}")
            print("-" * 60)
        clean = re.sub(r"\[/?[^\]]*\]", "", content)
        print(clean)
        print(border)

    def _rule(title: str = "") -> None:  # type: ignore[misc]
        if title:
            pad = max(0, (60 - len(title) - 2) // 2)
            print("\n" + "-" * pad + f" {title} " + "-" * pad)
        else:
            print("\n" + "-" * 60)

    def _table(headers: list[str], rows: list[list[str]]) -> None:  # type: ignore[misc]
        import re
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                clean = re.sub(r"\[/?[^\]]*\]", "", cell)
                if i < len(widths):
                    widths[i] = max(widths[i], len(clean))
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print(fmt.format(*headers))
        print("  ".join("-" * w for w in widths))
        for row in rows:
            cleaned = [re.sub(r"\[/?[^\]]*\]", "", c) for c in row]
            print(fmt.format(*cleaned))

    def _print(text: str = "", **_kw) -> None:  # type: ignore[misc]
        import re
        clean = re.sub(r"\[/?[^\]]*\]", "", str(text))
        print(clean)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent

STEPS = [
    "System Check",
    "USB Drive Check",
    "Dependencies",
    "Mode Selection",
    "Provider Setup",
    "Voice Setup",
    "Config & Validate",
    "Ready",
]

TICK = "[green]✔[/green]" if _RICH else "OK  "
CROSS = "[red]✘[/red]" if _RICH else "FAIL"
WARN = "[yellow]⚠[/yellow]" if _RICH else "WARN"

# Required packages: (import_name, pip_package_name)
REQUIRED_PACKAGES: list[tuple[str, str]] = [
    ("psutil", "psutil"),
    ("cryptography", "cryptography"),
    ("flask", "flask"),
    ("requests", "requests"),
]

# Optional packages: (import_name, pip_package_name)
OPTIONAL_PACKAGES: list[tuple[str, str]] = [
    ("anthropic", "anthropic"),
    ("openai", "openai"),
    ("google.auth", "google-auth"),
    ("huggingface_hub", "huggingface_hub"),
    ("pyautogui", "pyautogui"),
    ("pyperclip", "pyperclip"),
    ("rich", "rich"),
    ("pyaudio", "pyaudio"),
    ("assemblyai", "assemblyai"),
    ("elevenlabs", "elevenlabs"),
]

PROVIDER_DESCRIPTIONS: dict[str, str] = {
    "anthropic": "Anthropic Claude (claude-3.5-sonnet, claude-3-opus, …)",
    "openai": "OpenAI GPT-4o / GPT-4-turbo",
    "groq": "Groq — ultra-fast open-model inference (Llama, Mixtral)",
    "openrouter": "OpenRouter — unified gateway to 100+ models",
    "google": "Google Gemini via OAuth / API key",
}

# ---------------------------------------------------------------------------
# ASCII banner (Carry AI figlet — matches launcher.py)
# ---------------------------------------------------------------------------

ASCII_BANNER = r"""
   ____                         _      ___
  / ___|__ _ _ __ _ __ _   _  / \    |_ _|
 | |   / _` | '__| '__| | | |/ _ \    | |
 | |__| (_| | |  | |  | |_| / ___ \   | |
  \____\__,_|_|  |_|   \__, /_/   \_\ |___|
                        |___/
"""


# ---------------------------------------------------------------------------
# Banner + step header
# ---------------------------------------------------------------------------


def print_banner() -> None:
    """Print the carry-ai ASCII art banner and the wizard subtitle."""
    if _RICH:
        console.print(f"[bold cyan]{ASCII_BANNER}[/bold cyan]")
        console.print("[bold white]        Interactive Setup Wizard[/bold white]\n")
    else:
        print(ASCII_BANNER)
        print("        Interactive Setup Wizard\n")


def print_step_header(step_num: int, title: str) -> None:
    """Print a numbered step header surrounded by a visual divider."""
    total = len(STEPS)
    label = f"  Step {step_num}/{total} — {title}  "
    _rule(label)
    _print()


# ---------------------------------------------------------------------------
# Prompt helper — handles KeyboardInterrupt / EOFError gracefully
# ---------------------------------------------------------------------------


def _prompt(text: str) -> str:
    """Read a line of user input; exit cleanly on Ctrl-C / EOF."""
    try:
        if _RICH:
            return console.input(text)
        return input(text)
    except (KeyboardInterrupt, EOFError):
        _print()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Step 1: System Check
# ---------------------------------------------------------------------------


def check_system() -> dict:
    """
    Probe Python version, OS, RAM, and free disk space.

    Returns
    -------
    dict with keys: os, python_ok, python_version, ram_gb, disk_gb
    """
    # Python version
    py_ver = sys.version_info
    python_ok = py_ver >= (3, 10)
    python_version = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"

    # Operating system
    os_name = platform.system()  # 'Windows', 'Linux', 'Darwin'

    # RAM — try psutil first, fall back to /proc/meminfo on Linux
    ram_gb: float = 0.0
    try:
        import psutil  # type: ignore[import]
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    except ImportError:
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal"):
                        kb = int(line.split()[1])
                        ram_gb = kb / (1024 ** 2)
                        break
        except OSError:
            ram_gb = 0.0

    # Free disk space for the project directory
    disk_gb: float = 0.0
    try:
        import shutil
        usage = shutil.disk_usage(str(PROJECT_ROOT))
        disk_gb = usage.free / (1024 ** 3)
    except Exception:
        disk_gb = 0.0

    # Build and print results table
    rows = [
        ["Operating System", os_name, TICK],
        ["Python Version", python_version, TICK if python_ok else CROSS],
        ["Available RAM", f"{ram_gb:.1f} GB", TICK if ram_gb >= 2 else WARN],
        ["Free Disk Space", f"{disk_gb:.1f} GB", TICK if disk_gb >= 1 else WARN],
    ]

    _print("[bold]System Information[/bold]")
    _table(["Check", "Value", "Status"], rows)
    _print()

    if not python_ok:
        _panel(
            f"Python 3.10+ is required but {python_version} was detected.\n"
            "Please upgrade Python and re-run the wizard.",
            title="Python Version Error",
            style="bold red",
        )

    return {
        "os": os_name,
        "python_ok": python_ok,
        "python_version": python_version,
        "ram_gb": ram_gb,
        "disk_gb": disk_gb,
    }


# ---------------------------------------------------------------------------
# Step 2: USB Drive Check
# ---------------------------------------------------------------------------


def _detect_usb_drives() -> list[dict]:
    """Detect removable USB drives. Reuses logic from flash_usb.py."""
    drives: list[dict] = []
    try:
        import psutil as _psutil
        for part in _psutil.disk_partitions(all=False):
            is_removable = False
            opts = part.opts.lower() if part.opts else ""
            if platform.system().lower() == "windows":
                is_removable = "removable" in opts
            else:
                dev = part.device
                block = _re.sub(r'\d+$', '', dev.replace('/dev/', ''))
                removable_path = Path(f"/sys/block/{block}/removable")
                if removable_path.exists():
                    is_removable = removable_path.read_text().strip() == "1"
            if not is_removable:
                continue
            try:
                usage = _psutil.disk_usage(part.mountpoint)
                total_gb = usage.total / (1024 ** 3)
                free_gb = usage.free / (1024 ** 3)
            except (PermissionError, OSError):
                total_gb = free_gb = 0.0
            # Get volume label on Windows
            label = Path(part.mountpoint).name or "USB"
            if platform.system().lower() == "windows":
                try:
                    import ctypes
                    buf = ctypes.create_unicode_buffer(1024)
                    ctypes.windll.kernel32.GetVolumeInformationW(
                        part.mountpoint, buf, ctypes.sizeof(buf),
                        None, None, None, None, 0,
                    )
                    if buf.value:
                        label = buf.value
                except Exception:
                    pass
            drives.append({
                "mountpoint": part.mountpoint,
                "label": label,
                "total_gb": total_gb,
                "free_gb": free_gb,
            })
    except ImportError:
        # psutil not available — try common mount points
        candidates = []
        if platform.system().lower() == "windows":
            import string
            for letter in string.ascii_uppercase:
                mp = f"{letter}:\\"
                if os.path.exists(mp) and mp != os.path.splitdrive(sys.executable)[0] + "\\":
                    candidates.append(mp)
        else:
            for base in ["/media", "/mnt", "/run/media"]:
                if os.path.isdir(base):
                    for sub in Path(base).iterdir():
                        candidates.append(str(sub))
        for mp in candidates:
            try:
                usage = shutil.disk_usage(mp)
                drives.append({
                    "mountpoint": mp,
                    "label": Path(mp).name or mp,
                    "total_gb": usage.total / (1024 ** 3),
                    "free_gb": usage.free / (1024 ** 3),
                })
            except OSError:
                pass
    return drives


def _check_carry_ai(usb_path: Path) -> dict:
    """Check what carry-ai components exist on a USB drive."""
    result = {
        "has_carry_ai": False,
        "has_launcher": False,
        "has_settings": False,
        "has_keys": False,
        "has_models": False,
        "has_python_win": False,
        "has_linux_pkgs": False,
        "model_count": 0,
    }
    carry_dir = usb_path / "carry-ai"
    if not carry_dir.exists():
        return result
    result["has_carry_ai"] = True
    result["has_launcher"] = (carry_dir / "launcher.py").exists()
    result["has_settings"] = (carry_dir / "config" / "settings.json").exists()
    result["has_keys"] = (carry_dir / "config" / "providers.enc").exists()
    models_dir = usb_path / "models"
    if models_dir.exists():
        gguf_files = list(models_dir.glob("**/*.gguf"))
        result["has_models"] = len(gguf_files) > 0
        result["model_count"] = len(gguf_files)
    result["has_python_win"] = (usb_path / "python-env" / "windows" / "python.exe").exists()
    result["has_linux_pkgs"] = (usb_path / "python-env" / "linux" / "site-packages").exists()
    return result


def _sync_source(usb_path: Path) -> bool:
    """Copy carry-ai source to USB, preserving config and models."""
    src = PROJECT_ROOT
    dest = usb_path / "carry-ai"
    _print(f"\n[cyan]Syncing carry-ai source → {dest}[/cyan]")

    # Directories/files to skip (user data on USB)
    skip = {"__pycache__", ".git", ".claude", "config"}

    count = 0
    for item in src.iterdir():
        if item.name in skip:
            continue
        dest_item = dest / item.name
        try:
            if item.is_dir():
                if dest_item.exists():
                    shutil.rmtree(str(dest_item))
                shutil.copytree(str(item), str(dest_item))
            else:
                shutil.copy2(str(item), str(dest_item))
            count += 1
        except Exception as exc:
            _print(f"  [red]Error copying {item.name}: {exc}[/red]")
    _print(f"  {TICK} Synced {count} items.")
    return True


def _update_usb_dependencies(usb_path: Path) -> bool:
    """Reinstall/update Python packages on the USB drive."""
    _print("\n[cyan]Updating packages on USB...[/cyan]")
    all_packages = [pkg for _, pkg in REQUIRED_PACKAGES] + [pkg for _, pkg in OPTIONAL_PACKAGES]

    # Update Windows portable Python packages if present
    win_python = usb_path / "python-env" / "windows" / "python.exe"
    if win_python.exists():
        _print("\n  [bold]Windows portable Python:[/bold]")
        for pkg in all_packages:
            _print(f"    Updating {pkg}...", end="")
            try:
                result = subprocess.run(
                    [str(win_python), "-m", "pip", "install", "--upgrade",
                     "--no-warn-script-location", "-q", pkg],
                    capture_output=True, text=True, check=False,
                )
                if result.returncode == 0:
                    _print(f" {TICK}")
                else:
                    _print(f" [yellow]skip[/yellow]")
            except Exception:
                _print(f" [yellow]skip[/yellow]")

    # Update Linux site-packages if present
    linux_sp = usb_path / "python-env" / "linux" / "site-packages"
    if linux_sp.exists():
        _print("\n  [bold]Linux packages:[/bold]")
        for pkg in all_packages:
            _print(f"    Updating {pkg}...", end="")
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--upgrade",
                     "--target", str(linux_sp), "-q", pkg],
                    capture_output=True, text=True, check=False,
                )
                if result.returncode == 0:
                    _print(f" {TICK}")
                else:
                    _print(f" [yellow]skip[/yellow]")
            except Exception:
                _print(f" [yellow]skip[/yellow]")

    _print(f"\n  {TICK} Dependency update complete.")
    return True


def _clean_flash(usb_path: Path) -> None:
    """Wipe carry-ai data from USB for a clean re-flash."""
    _print(f"\n[yellow]Cleaning carry-ai from {usb_path}...[/yellow]")
    for name in ["carry-ai", "python-env", "models", "bin",
                  "start.bat", "start.sh", "autorun.inf"]:
        target = usb_path / name
        if target.is_dir():
            shutil.rmtree(str(target))
            _print(f"  Removed {name}/")
        elif target.is_file():
            target.unlink()
            _print(f"  Removed {name}")
    _print(f"  {TICK} USB cleaned. Run [bold]python flash_usb.py[/bold] to re-flash.")


def check_usb_drives() -> dict | None:
    """
    Step 2: Detect USB drives, check for existing carry-ai installation,
    and offer update / overwrite / clean flash options.

    Returns the USB info dict if a drive was selected, or None.
    """
    drives = _detect_usb_drives()

    if not drives:
        _print("[dim]No removable USB drives detected — skipping USB check.[/dim]")
        _print("[dim]If your drive is not detected, run:[/dim]")
        _print("[dim]  python flash_usb.py[/dim]\n")
        return None

    # Show detected drives
    _print("[bold]Detected USB Drives[/bold]")
    rows = []
    for i, d in enumerate(drives, 1):
        rows.append([
            str(i),
            d["label"],
            d["mountpoint"],
            f"{d['total_gb']:.1f} GB",
            f"{d['free_gb']:.1f} GB",
        ])
    _table(["#", "Label", "Mount", "Total", "Free"], rows)
    _print()

    choice = _prompt("  Check a drive for carry-ai? (number, or Enter to skip): ").strip()
    if not choice:
        _print("[dim]Skipping USB check.[/dim]\n")
        return None

    try:
        idx = int(choice) - 1
        drive = drives[idx]
    except (ValueError, IndexError):
        _print("[yellow]Invalid selection — skipping USB check.[/yellow]\n")
        return None

    usb_path = Path(drive["mountpoint"])
    info = _check_carry_ai(usb_path)

    if not info["has_carry_ai"]:
        _print(f"\n  No carry-ai found on [bold]{usb_path}[/bold].")
        _print("  Use [bold]python flash_usb.py[/bold] to flash this drive.\n")
        return {"path": usb_path, "info": info, "action": None}

    # Found existing install — show status
    _print(f"\n[bold]Existing carry-ai found on {usb_path}[/bold]")
    status_rows = [
        ["Source code", TICK if info["has_launcher"] else CROSS],
        ["Settings", TICK if info["has_settings"] else WARN],
        ["Encrypted keys", TICK if info["has_keys"] else WARN],
        ["GGUF models", f"{info['model_count']} model(s)" if info["has_models"] else "None"],
        ["Windows Python", TICK if info["has_python_win"] else CROSS],
        ["Linux packages", TICK if info["has_linux_pkgs"] else CROSS],
    ]
    _table(["Component", "Status"], status_rows)
    _print()

    _print("[bold]What would you like to do?[/bold]")
    _print("  [cyan]1[/cyan]  Update      — sync source code + update dependencies (keeps config, keys, models)")
    _print("  [cyan]2[/cyan]  Overwrite   — replace source code only (keeps config, keys, models)")
    _print("  [cyan]3[/cyan]  Clean flash — wipe everything and re-flash from scratch")
    _print("  [cyan]4[/cyan]  Update deps — update packages only (no source code changes)")
    _print("  [cyan]s[/cyan]  Skip        — continue onboarding without changes")
    _print()

    action = _prompt("  Choice [1/2/3/4/s]: ").strip().lower()

    if action == "1":
        _sync_source(usb_path)
        _update_usb_dependencies(usb_path)
        return {"path": usb_path, "info": info, "action": "update"}

    elif action == "2":
        _sync_source(usb_path)
        _print(f"\n  {TICK} Source code overwritten. Config, keys, and models preserved.")
        return {"path": usb_path, "info": info, "action": "overwrite"}

    elif action == "3":
        _print()
        _print("[bold red]WARNING: This will delete ALL carry-ai data on the USB![/bold red]")
        _print("  This includes: source code, packages, models, settings, and keys.")
        confirm = _prompt("  Type 'yes' to confirm: ").strip().lower()
        if confirm == "yes":
            _clean_flash(usb_path)
        else:
            _print("[dim]Clean flash cancelled.[/dim]")
        return {"path": usb_path, "info": info, "action": "clean"}

    elif action == "4":
        _update_usb_dependencies(usb_path)
        return {"path": usb_path, "info": info, "action": "deps"}

    else:
        _print("[dim]Skipping USB changes.[/dim]\n")
        return {"path": usb_path, "info": info, "action": None}


# ---------------------------------------------------------------------------
# Step 3: Dependencies
# ---------------------------------------------------------------------------


def _check_import(module_name: str) -> bool:
    """Return True if *module_name* can be imported."""
    import importlib
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False


def check_dependencies() -> dict:
    """
    Check all required and optional packages.

    Returns
    -------
    dict with keys: missing_required (list[str]), missing_optional (list[str])
    """
    missing_required: list[str] = []
    missing_optional: list[str] = []
    rows: list[list[str]] = []

    for module, pkg in REQUIRED_PACKAGES:
        ok = _check_import(module)
        rows.append([pkg, "required", TICK if ok else CROSS])
        if not ok:
            missing_required.append(pkg)

    for module, pkg in OPTIONAL_PACKAGES:
        ok = _check_import(module)
        rows.append([pkg, "optional", TICK if ok else WARN])
        if not ok:
            missing_optional.append(pkg)

    _print("[bold]Package Status[/bold]")
    _table(["Package", "Type", "Status"], rows)
    _print()

    if missing_required:
        _print(f"[red]Missing required packages:[/red] {', '.join(missing_required)}")
    if missing_optional:
        _print(
            f"[yellow]Missing optional packages:[/yellow] "
            f"{', '.join(missing_optional)} (install as needed)"
        )
    _print()

    return {
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }


def install_missing(packages: list[str]) -> None:
    """
    Offer to install each missing required package via pip.

    Prompts the user for confirmation before running any installs.
    """
    if not packages:
        return

    _print(f"\nRequired packages to install: [bold]{' '.join(packages)}[/bold]")
    answer = _prompt("Install them now? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        _print("[yellow]Skipping installation — some features may not work.[/yellow]")
        return

    for pkg in packages:
        _print(f"\n[cyan]Installing {pkg}…[/cyan]")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg],
                check=False,
            )
            if result.returncode == 0:
                _print(f"[green]  {pkg} installed successfully.[/green]")
            else:
                _print(f"[red]  Failed to install {pkg} (exit code {result.returncode}).[/red]")
        except Exception as exc:
            _print(f"[red]  Error installing {pkg}: {exc}[/red]")


# ---------------------------------------------------------------------------
# Step 3: Mode Selection
# ---------------------------------------------------------------------------


def select_mode(ram_gb: float, has_models: bool, has_keys: bool) -> str:
    """
    Present three operating modes and return the user's choice.

    Recommendation heuristic
    ------------------------
    - RAM >= 8 GB *and* GGUF models present  → local
    - API keys already configured             → api
    - Otherwise                              → hybrid

    Returns 'local', 'api', or 'hybrid'.
    """
    if ram_gb >= 8 and has_models:
        recommendation = "local"
    elif has_keys:
        recommendation = "api"
    else:
        recommendation = "hybrid"

    rec_label = {"local": "1", "api": "2", "hybrid": "3"}[recommendation]

    mode_info = [
        (
            "1. Local Mode",
            (
                "• Runs 100% offline using GGUF models stored on your USB drive\n"
                "• Pros: complete privacy, no internet required, no API costs\n"
                "• Cons: needs RAM (≥ 4 GB recommended), slower on CPU-only hardware\n"
                f"• Your available RAM: {ram_gb:.1f} GB"
            ),
            "cyan",
        ),
        (
            "2. API Mode",
            (
                "• Routes queries to cloud providers (Anthropic, OpenAI, Groq, …)\n"
                "• Pros: fast responses, access to largest models, minimal local RAM\n"
                "• Cons: requires internet connection + API keys, usage costs apply"
            ),
            "green",
        ),
        (
            "3. Hybrid Mode",
            (
                "• Tries local GGUF model first; falls back to cloud on failure\n"
                "• Pros: best of both worlds — offline-capable and cloud-resilient\n"
                "• Cons: needs both a local model *and* at least one API key"
            ),
            "yellow",
        ),
    ]

    for title, body, style in mode_info:
        _panel(body, title=title, style=style)
        _print()

    _print(f"[bold]Recommended mode:[/bold] {recommendation} (option {rec_label})")
    _print("Enter 1, 2, or 3 — or press Enter to accept the recommendation.")

    choice_raw = _prompt("Your choice: ").strip()

    mapping: dict[str, str] = {
        "1": "local",
        "2": "api",
        "3": "hybrid",
        "": recommendation,
    }
    selected = mapping.get(choice_raw)

    if selected is None:
        _print("[yellow]Unrecognised choice — using recommendation.[/yellow]")
        selected = recommendation

    _print(f"\n[green]Mode selected:[/green] {selected}\n")
    return selected


# ---------------------------------------------------------------------------
# Step 4: Provider Setup
# ---------------------------------------------------------------------------


def setup_api_keys() -> dict[str, str]:
    """
    Interactive wizard to collect API keys for supported cloud providers.

    Keys are stored only in memory during this session unless the user
    chooses to encrypt and persist them via the keystore CLI.

    Returns a dict mapping provider_name → api_key for entered keys.
    """
    _print(
        "Enter API keys for the providers you want to use.\n"
        "Press [bold]Enter[/bold] to skip any provider.\n"
        "Key input is hidden while you type.\n"
    )

    keys: dict[str, str] = {}

    for provider, description in PROVIDER_DESCRIPTIONS.items():
        _print(f"[cyan]{provider}[/cyan] — {description}")
        try:
            key = getpass.getpass(f"  {provider} API key (blank to skip): ").strip()
        except (KeyboardInterrupt, EOFError):
            _print()
            sys.exit(0)

        if key:
            keys[provider] = key
            _print(f"  [green]Key stored for {provider}.[/green]")
        else:
            _print(f"  [dim]Skipped {provider}.[/dim]")
        _print()

    if keys:
        _print(
            f"[green]{len(keys)} key(s) collected.[/green]  "
            "Would you like to encrypt and save them to config/providers.enc?"
        )
        save = _prompt("Encrypt & save? [Y/n]: ").strip().lower()
        if save not in ("n", "no"):
            _print("\n[cyan]Launching keystore setup wizard…[/cyan]")
            try:
                subprocess.run(
                    [sys.executable, "crypto/keystore.py", "setup"],
                    cwd=str(PROJECT_ROOT),
                    check=False,
                )
            except Exception as exc:
                _print(f"[red]Keystore error: {exc}[/red]")
    else:
        _print(
            "[yellow]No keys entered — add them later with:[/yellow]\n"
            "  python crypto/keystore.py setup"
        )

    return keys


# ---------------------------------------------------------------------------
# Step 5: Voice Setup
# ---------------------------------------------------------------------------


def setup_voice_mode() -> dict:
    """
    Explain the voice pipeline and collect voice-specific API keys.

    Pipeline (inspired by farzaa/clicky):
        Push-to-talk → AssemblyAI transcription → Claude vision → ElevenLabs TTS

    Returns a config dict (may be empty / {'enabled': False} if user opts out).
    """
    _panel(
        "carry-ai includes an optional push-to-talk voice pipeline:\n\n"
        "  1. [bold]Record[/bold]     — hold Enter to capture mic audio (PyAudio)\n"
        "  2. [bold]Transcribe[/bold] — AssemblyAI converts speech → text\n"
        "  3. [bold]Vision[/bold]     — optional screenshot attached to every query\n"
        "  4. [bold]Speak[/bold]      — ElevenLabs TTS reads the agent response aloud\n\n"
        "Inspired by farzaa/clicky (https://github.com/farzaa/clicky).",
        title="Voice Pipeline",
        style="magenta",
    )
    _print()

    # Show which supporting packages are already available
    has_pyaudio = _check_import("pyaudio")
    has_assemblyai = _check_import("assemblyai")
    has_elevenlabs = _check_import("elevenlabs")

    rows = [
        ["pyaudio", TICK if has_pyaudio else CROSS, "microphone recording"],
        ["assemblyai", TICK if has_assemblyai else CROSS, "speech-to-text (required for voice)"],
        ["elevenlabs", TICK if has_elevenlabs else WARN, "text-to-speech (optional)"],
    ]
    _table(["Package", "Status", "Purpose"], rows)
    _print()

    enable = _prompt("Enable voice mode? [y/N]: ").strip().lower()
    if enable not in ("y", "yes"):
        _print("[dim]Voice mode skipped.[/dim]\n")
        return {}

    voice_config: dict = {}

    # AssemblyAI key
    _print("\n[cyan]AssemblyAI[/cyan] — https://www.assemblyai.com  (free tier available)")
    try:
        aai_key = getpass.getpass("  AssemblyAI API key (blank to skip): ").strip()
    except (KeyboardInterrupt, EOFError):
        _print()
        sys.exit(0)

    if aai_key:
        voice_config["assemblyai_key"] = aai_key
        _print("  [green]AssemblyAI key stored.[/green]")
    else:
        _print("  [yellow]No AssemblyAI key — transcription will be unavailable.[/yellow]")

    # ElevenLabs key (optional)
    _print("\n[cyan]ElevenLabs[/cyan] — https://elevenlabs.io  (optional, for TTS)")
    try:
        el_key = getpass.getpass("  ElevenLabs API key (blank to skip): ").strip()
    except (KeyboardInterrupt, EOFError):
        _print()
        sys.exit(0)

    if el_key:
        voice_config["elevenlabs_key"] = el_key
        _print("  [green]ElevenLabs key stored.[/green]")
    else:
        _print("  [dim]No ElevenLabs key — TTS will be silent.[/dim]")

    if not has_pyaudio or not has_assemblyai:
        _print(
            "\n[yellow]Warning:[/yellow] one or more voice packages are missing.\n"
            "Install them with:\n"
            "  pip install pyaudio assemblyai elevenlabs\n"
        )

    voice_config["enabled"] = True
    _print("\n[green]Voice mode configured.[/green]\n")
    return voice_config


# ---------------------------------------------------------------------------
# Step 6a: Generate config/settings.json
# ---------------------------------------------------------------------------


def generate_config(mode: str, voice_config: dict) -> None:
    """
    Write config/settings.json with selected mode, UI defaults, and voice keys.

    Merges voice settings into the JSON structure when voice is enabled.
    Prints the generated JSON to the console for review.
    """
    settings: dict = {
        "mode": mode,
        "port": 8080,
        "ui": {
            "theme": "dark",
        },
        "agent": {
            "permission_mode": "ask",
            "max_iterations": 10,
        },
        "providers": {},
        "local": {
            "gpu_layers": 0,
        },
    }

    if voice_config.get("enabled"):
        settings["voice"] = {
            "enabled": True,
            "assemblyai_key": voice_config.get("assemblyai_key", ""),
            "elevenlabs_key": voice_config.get("elevenlabs_key", ""),
            "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM",  # Rachel
            "tts_enabled": bool(voice_config.get("elevenlabs_key")),
            "vision_enabled": True,
        }

    config_dir = PROJECT_ROOT / "config"
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "settings.json"

    json_str = json.dumps(settings, indent=2)

    try:
        config_path.write_text(json_str, encoding="utf-8")
        _print(f"[green]Config written to:[/green] {config_path}\n")
    except OSError as exc:
        _print(f"[red]Could not write config: {exc}[/red]")

    _panel(json_str, title="config/settings.json", style="dim")
    _print()


# ---------------------------------------------------------------------------
# Step 6b: Dry-run validation
# ---------------------------------------------------------------------------


def run_validation() -> bool:
    """
    Run `python launcher.py --dry-run --verbose` and display its output.

    Returns True if the subprocess exits with code 0.
    """
    _print("[cyan]Running dry-run validation…[/cyan]\n")

    try:
        result = subprocess.run(
            [sys.executable, "launcher.py", "--dry-run", "--verbose"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout + result.stderr).strip()
        _panel(output or "(no output)", title="Dry-run Output", style="dim")

        if result.returncode == 0:
            _print("[green]Dry-run passed.[/green]\n")
            return True
        else:
            _print(
                f"[yellow]Dry-run exited with code {result.returncode}. "
                "Review the output above.[/yellow]\n"
            )
            return False

    except subprocess.TimeoutExpired:
        _print("[red]Dry-run timed out after 60 seconds.[/red]\n")
        return False
    except FileNotFoundError:
        _print("[yellow]launcher.py not found — skipping validation.[/yellow]\n")
        return False
    except Exception as exc:
        _print(f"[red]Validation error: {exc}[/red]\n")
        return False


# ---------------------------------------------------------------------------
# Step 7: Summary
# ---------------------------------------------------------------------------


def print_summary(mode: str, port: int, voice_enabled: bool) -> None:
    """Print a final success panel with launch commands and useful tips."""
    ui_url = f"http://localhost:{port}"

    lines = [
        "[bold green]You're all set! carry-ai is ready to launch.[/bold green]\n",
        "[bold]Launch command:[/bold]",
        f"  python launcher.py --mode {mode}\n",
        "[bold]Web UI:[/bold]",
        f"  {ui_url}\n",
        "[bold]Useful commands:[/bold]",
        "  python crypto/keystore.py setup           # Manage encrypted API keys",
        "  python models/downloader.py interactive   # Download GGUF models",
        "  python launcher.py --dry-run --verbose    # Validate without USB hardware",
        "  python launcher.py --no-ui                # Headless CLI mode",
        "  python launcher.py --download-model       # Interactive model browser",
    ]

    if voice_enabled:
        lines.append(
            "\n[magenta]Voice mode is enabled — "
            "press Enter inside the UI to start push-to-talk.[/magenta]"
        )

    _panel("\n".join(lines), title="Setup Complete", style="bold green")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def main() -> None:
    """Run each wizard step in sequence."""
    print_banner()

    # ------------------------------------------------------------------
    # Step 1 — System Check
    # ------------------------------------------------------------------
    print_step_header(1, STEPS[0])
    sys_info = check_system()

    if not sys_info["python_ok"]:
        _print("[red]Aborting: Python 3.10+ is required.[/red]")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 2 — USB Drive Check
    # ------------------------------------------------------------------
    print_step_header(2, STEPS[1])
    usb_result = check_usb_drives()

    if usb_result and usb_result.get("action") == "clean":
        _print("\n[bold]USB wiped. Re-run [cyan]python flash_usb.py[/cyan] to flash it.[/bold]")
        _print("Continuing onboarding for local setup...\n")

    # ------------------------------------------------------------------
    # Step 3 — Dependencies
    # ------------------------------------------------------------------
    print_step_header(3, STEPS[2])
    dep_info = check_dependencies()

    if dep_info["missing_required"]:
        install_missing(dep_info["missing_required"])
    else:
        _print("[green]All required packages are present.[/green]\n")

    # Offer to update all deps (required + optional) even if none missing
    if not dep_info["missing_required"]:
        _print("[dim]Tip: You can also update all packages to their latest versions.[/dim]")
        upd = _prompt("  Update all packages now? [y/N]: ").strip().lower()
        if upd in ("y", "yes"):
            all_pkgs = ([p for _, p in REQUIRED_PACKAGES]
                        + [p for _, p in OPTIONAL_PACKAGES])
            _print("\n[cyan]Updating packages...[/cyan]")
            for pkg in all_pkgs:
                _print(f"  Updating {pkg}...", end="")
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "pip", "install", "--upgrade", "-q", pkg],
                        capture_output=True, text=True, check=False,
                    )
                    if result.returncode == 0:
                        _print(f" {TICK}")
                    else:
                        _print(f" [yellow]skip[/yellow]")
                except Exception:
                    _print(f" [yellow]skip[/yellow]")
            _print(f"\n{TICK} Package update complete.\n")

    # ------------------------------------------------------------------
    # Step 4 — Mode Selection
    # ------------------------------------------------------------------
    print_step_header(4, STEPS[3])

    # Detect whether any .gguf model files are present under models/
    models_dir = PROJECT_ROOT / "models"
    has_models = (
        any(models_dir.glob("**/*.gguf")) if models_dir.exists() else False
    )

    # Detect whether encrypted provider keys already exist on USB
    enc_path = PROJECT_ROOT / "config" / "providers.enc"
    has_keys = enc_path.exists()

    mode = select_mode(sys_info["ram_gb"], has_models, has_keys)

    # ------------------------------------------------------------------
    # Step 5 — Provider / Model Setup
    # ------------------------------------------------------------------
    print_step_header(5, STEPS[4])

    if mode in ("api", "hybrid"):
        _print("Cloud provider keys are needed for API/Hybrid mode.\n")
        setup_api_keys()
    else:
        _print("[dim]Local mode selected — skipping cloud provider key setup.[/dim]")

    # Offer to download a model if local/hybrid but no models found
    if mode in ("local", "hybrid") and not has_models:
        _print(
            "\n[yellow]No GGUF models found.[/yellow]  "
            "You need at least one model for local inference."
        )
        dl = _prompt("Open the model downloader now? [y/N]: ").strip().lower()
        if dl in ("y", "yes"):
            try:
                subprocess.run(
                    [sys.executable, "models/downloader.py", "interactive"],
                    cwd=str(PROJECT_ROOT),
                    check=False,
                )
            except Exception as exc:
                _print(f"[red]Downloader error: {exc}[/red]")

    _print()

    # ------------------------------------------------------------------
    # Step 6 — Voice Setup
    # ------------------------------------------------------------------
    print_step_header(6, STEPS[5])
    voice_config = setup_voice_mode()

    # ------------------------------------------------------------------
    # Step 7 — Config & Validate
    # ------------------------------------------------------------------
    print_step_header(7, STEPS[6])
    generate_config(mode, voice_config)
    run_validation()

    # ------------------------------------------------------------------
    # Step 8 — Ready
    # ------------------------------------------------------------------
    print_step_header(8, STEPS[7])
    port = 8080
    voice_enabled = bool(voice_config.get("enabled"))
    print_summary(mode, port, voice_enabled)


if __name__ == "__main__":
    main()
