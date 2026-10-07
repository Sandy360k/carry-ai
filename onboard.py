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
    "anthropic": "Anthropic Claude (Opus 5, Sonnet 5, Haiku 4.5, …)",
    "openai": "OpenAI GPT-6 (luna / sol / astra)",
    "groq": "Groq — ultra-fast open-model inference (GPT-OSS, Qwen)",
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
# Step 2: Dependencies
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
    # Step 2 — Dependencies
    # ------------------------------------------------------------------
    print_step_header(2, STEPS[1])
    dep_info = check_dependencies()

    if dep_info["missing_required"]:
        install_missing(dep_info["missing_required"])
    else:
        _print("[green]All required packages are present.[/green]\n")

    # ------------------------------------------------------------------
    # Step 3 — Mode Selection
    # ------------------------------------------------------------------
    print_step_header(3, STEPS[2])

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
    # Step 4 — Provider / Model Setup
    # ------------------------------------------------------------------
    print_step_header(4, STEPS[3])

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
    # Step 5 — Voice Setup
    # ------------------------------------------------------------------
    print_step_header(5, STEPS[4])
    voice_config = setup_voice_mode()

    # ------------------------------------------------------------------
    # Step 6 — Config & Validate
    # ------------------------------------------------------------------
    print_step_header(6, STEPS[5])
    generate_config(mode, voice_config)
    run_validation()

    # ------------------------------------------------------------------
    # Step 7 — Ready
    # ------------------------------------------------------------------
    print_step_header(7, STEPS[6])
    port = 8080
    voice_enabled = bool(voice_config.get("enabled"))
    print_summary(mode, port, voice_enabled)


if __name__ == "__main__":
    main()
