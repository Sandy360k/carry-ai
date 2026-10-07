"""
carry-ai/launcher.py — Main Entry Point
========================================

The single entry point for the entire carry-ai system. When a user plugs in the
USB and runs this script, it orchestrates the full boot sequence:

Boot Sequence:
    1. Detect host OS (Windows / Linux)
    2. Probe available RAM via psutil
    3. Inject session environment:
       - Windows: copy runtime to %TEMP%\\ai_session\\, register WMI eject watcher
       - Linux: mount tmpfs at /tmp/ai_session/, register udev rule
    4. Present mode selection UI (Local / API / Hybrid)
    5. If Local or Hybrid: auto-select best GGUF model for available RAM
    6. If API or Hybrid: decrypt API keys from config/providers.enc into RAM
    7. Initialize MCP connections and load plugins
    8. Boot the agent loop + Flask web UI on localhost:8080
    9. Block until eject event fires, then trigger cleanup/cleanup.py

CLI Flags:
    --dry-run         Simulate full boot without actual USB hardware or injection
    --mode            Force a mode: local | api | hybrid
    --port            Override web UI port (default: 8080)
    --no-ui           Headless mode, skip Flask web UI
    --verbose         Enable debug logging
    --download-model  Interactive model download from HuggingFace before boot
"""

import argparse
import logging
import os
import platform
import secrets
import signal
import sys
import threading
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

# ---------------------------------------------------------------------------
# Resolve project root (the directory containing this file = USB carry-ai/)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("carry-ai")

BANNER = r"""
   ____                                _    ___
  / ___|__ _ _ __ _ __ _   _          / \  |_ _|
 | |   / _` | '__| '__| | | |  ___  / _ \  | |
 | |__| (_| | |  | |  | |_| | |___| / ___ \ | |
  \____\__,_|_|  |_|   \__, |      /_/   \_\___|
                        |___/
  Portable AI Assistant — inject, assist, vanish.
"""

# ===================================================================
# Core detection functions
# ===================================================================

def detect_os() -> str:
    """Detect host operating system.

    Returns:
        'windows' or 'linux'. Raises SystemExit on unsupported OS.
    """
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    elif system == "linux":
        return "linux"
    else:
        log.error("Unsupported OS: %s. carry-ai supports Windows and Linux only.", system)
        sys.exit(1)


def probe_ram_gb() -> float:
    """Return available system RAM in GB.

    Uses psutil if installed, otherwise falls back to platform-specific
    methods. Returns *available* RAM (not total).
    """
    if psutil is not None:
        mem = psutil.virtual_memory()
        available_gb = mem.available / (1024 ** 3)
        log.info("RAM — total: %.1f GB, available: %.1f GB", mem.total / (1024 ** 3), available_gb)
        return available_gb

    # Fallback: try /proc/meminfo on Linux
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    available_gb = kb / (1024 ** 2)
                    log.info("RAM (from /proc/meminfo) — available: %.1f GB", available_gb)
                    return available_gb
    except (FileNotFoundError, PermissionError):
        pass

    log.warning("Cannot detect RAM (psutil not installed). Defaulting to 4 GB.")
    return 4.0


def find_usb_root() -> Path:
    """Attempt to find the USB drive root that contains this script.

    Walks up from PROJECT_ROOT looking for telltale signs of a USB root
    (e.g. being on a removable drive, or the root of a mount point).
    Falls back to PROJECT_ROOT itself.
    """
    # Simple heuristic: PROJECT_ROOT is already the carry-ai/ directory on the USB
    # Its parent is the USB root.
    candidate = PROJECT_ROOT.parent
    # Sanity check: the carry-ai folder should be directly under USB root
    if (candidate / "carry-ai").is_dir():
        return candidate
    return PROJECT_ROOT


def scan_models(models_dir: Path) -> list[Path]:
    """Scan models/ directory for .gguf files.

    Returns:
        Sorted list of .gguf file paths (largest first by file size).
    """
    if not models_dir.is_dir():
        return []
    gguf_files = sorted(
        (p for p in models_dir.glob("*.gguf") if "mmproj" not in p.name.lower()),
        key=lambda p: p.stat().st_size, reverse=True)
    for f in gguf_files:
        size_gb = f.stat().st_size / (1024 ** 3)
        log.info("  Found model: %s (%.2f GB)", f.name, size_gb)
    return gguf_files


def select_model_for_ram(available_ram_gb: float, available_models: list[Path]) -> tuple[dict | None, Path | None]:
    """Pick the best model that fits the available RAM.

    Delegates to modes.local_mode.select_model, whose tiers come from the
    shared catalogue in models/catalog.py.

    Returns:
        (tier_dict, model_path) or (None, None) if no models available.
    """
    from dataclasses import asdict
    from modes.local_mode import select_model
    tier, model_path = select_model(available_ram_gb, available_models)
    return (asdict(tier) if tier else None), model_path


# ===================================================================
# Mode selection
# ===================================================================

def has_encrypted_keys(config_dir: Path) -> bool:
    """Check if encrypted API keys exist."""
    enc_file = config_dir / "providers.enc"
    if not enc_file.is_file():
        return False
    content = enc_file.read_text(encoding="utf-8").strip()
    # The placeholder file starts with '#'; real encrypted data does not
    return bool(content) and not content.startswith("#")


def select_mode(available_ram_gb: float, available_models: list[Path],
                has_api_keys: bool, forced_mode: str | None = None) -> str:
    """Determine execution mode.

    Priority:
        1. If --mode flag provided, use that (validate requirements)
        2. If models available AND keys available → hybrid
        3. If models available → local
        4. If keys available → api
        5. Otherwise → interactive prompt

    Returns:
        'local', 'api', or 'hybrid'
    """
    if forced_mode:
        mode = forced_mode.lower()
        if mode not in ("local", "api", "hybrid"):
            log.error("Invalid mode '%s'. Choose: local, api, hybrid", mode)
            sys.exit(1)
        if mode in ("local", "hybrid") and not available_models:
            log.warning("Mode '%s' requested but no GGUF models found in models/. "
                        "Use --download-model to get one.", mode)
            if mode == "hybrid":
                log.info("Falling back to api mode.")
                return "api"
            sys.exit(1)
        if mode in ("api", "hybrid") and not has_api_keys:
            log.warning("Mode '%s' requested but no API keys configured.", mode)
            if mode == "hybrid":
                log.info("Falling back to local mode.")
                return "local"
            sys.exit(1)
        return mode

    # Auto-detect
    has_models = bool(available_models) and available_ram_gb >= 3
    if has_models and has_api_keys:
        log.info("Auto-selected mode: hybrid (models + API keys available)")
        return "hybrid"
    elif has_models:
        log.info("Auto-selected mode: local (models available, no API keys)")
        return "local"
    elif has_api_keys:
        log.info("Auto-selected mode: api (API keys available, no local models)")
        return "api"
    else:
        # Nothing configured yet — start in API mode so the web UI launches.
        # The user can enter an API key or download a model from the browser UI.
        print("  No local models or API keys found.")
        print("  Starting in API mode — configure your provider in the web UI.")
        return "api"


# ===================================================================
# Injection wrappers
# ===================================================================

def inject_session(host_os: str, dry_run: bool = False) -> Path:
    """Set up session environment on the host.

    Returns:
        Path to the session directory.
    """
    if dry_run:
        # Dry-run: use a temp directory inside the project
        session_dir = PROJECT_ROOT / "_dry_run_session"
        session_dir.mkdir(exist_ok=True)
        log.info("[dry-run] Session directory: %s", session_dir)
        return session_dir

    if host_os == "windows":
        from inject.inject_windows import inject as win_inject
        session_dir, _watcher = win_inject(dry_run=dry_run)
        return session_dir
    else:
        # /dev/shm is a RAM-backed tmpfs on virtually every distro and needs
        # no root, so session files never touch the host's disk.
        # start.sh may already have created it (noexec USB → Python copied
        # into the session dir); reuse that so one wipe covers both.
        preset = os.environ.get("CARRY_AI_SESSION_DIR")
        shm = Path("/dev/shm")
        base = shm if shm.is_dir() and os.access(shm, os.W_OK) else Path("/tmp")
        session_dir = Path(preset) if preset and Path(preset).name == "ai_session" \
            else base / "ai_session"
        if not session_dir.exists():
            log.info("Creating session directory: %s", session_dir)
            session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir


# ===================================================================
# Boot sequence
# ===================================================================

def boot(args: argparse.Namespace) -> None:
    """Full boot sequence."""
    print(BANNER)

    host_os = detect_os()
    log.info("Host OS: %s (%s)", host_os, platform.platform())

    # --- RAM ---
    ram_gb = probe_ram_gb()
    print(f"  OS:  {host_os.capitalize()} ({platform.machine()})")
    print(f"  RAM: {ram_gb:.1f} GB available")

    # --- Models ---
    models_dir = PROJECT_ROOT / "models"
    available_models = scan_models(models_dir)
    print(f"  Models: {len(available_models)} GGUF file(s) in models/")

    # --- Download model (if requested) ---
    if args.download_model:
        _run_model_download(models_dir)
        # Re-scan after download
        available_models = scan_models(models_dir)

    # --- API keys ---
    config_dir = PROJECT_ROOT / "config"
    api_keys_available = has_encrypted_keys(config_dir)
    print(f"  API Keys: {'configured' if api_keys_available else 'not configured'}")

    # --- Mode ---
    mode = select_mode(ram_gb, available_models, api_keys_available, args.mode)
    print(f"  Mode: {mode.upper()}")

    # --- Model selection (local/hybrid) ---
    selected_tier = None
    selected_model = None
    if mode in ("local", "hybrid"):
        selected_tier, selected_model = select_model_for_ram(ram_gb, available_models)
        if selected_model:
            print(f"  Model: {selected_model.name}" +
                  (f" ({selected_tier['name']})" if selected_tier else ""))
        else:
            log.error("No suitable model found for %.1f GB RAM.", ram_gb)
            if mode == "hybrid":
                mode = "api"
                log.info("Falling back to API mode.")
            else:
                sys.exit(1)

    print()

    # --- Inject session ---
    session_dir = inject_session(host_os, dry_run=args.dry_run)
    log.info("Session directory: %s", session_dir)
    # Tools (e.g. browse's throwaway browser profile) put files here too.
    os.environ["CARRY_AI_SESSION_DIR"] = str(session_dir)

    # --- Decrypt API keys (api/hybrid) ---
    decrypted_keys = None
    if mode in ("api", "hybrid"):
        decrypted_keys = _decrypt_keys(config_dir, args.dry_run)

    # --- Initialize MCP connections ---
    mcp_bridge = _init_mcp(config_dir, args.dry_run)

    # --- Load plugins ---
    plugin_tools = _load_plugins(args.dry_run)

    # --- Agent settings (settings.json / CARRY_AI_* env vars) ---
    permission_mode = "ask"
    try:
        from config.settings import load_settings
        permission_mode = load_settings().agent.get("permission_mode", "ask")
    except Exception as e:
        log.warning("Could not load settings (using permission_mode=ask): %s", e)

    # --- Build context for agent ---
    boot_context = {
        "permission_mode": permission_mode,
        "host_os": host_os,
        "ram_gb": ram_gb,
        "mode": mode,
        "session_dir": str(session_dir),
        "model_path": str(selected_model) if selected_model else None,
        "model_tier": selected_tier,
        "api_keys": decrypted_keys,
        "mcp_bridge": mcp_bridge,
        "plugin_tools": plugin_tools,
        "dry_run": args.dry_run,
        "port": args.port,
        "no_ui": args.no_ui,
        "ui_token": secrets.token_urlsafe(32),
        "shutdown_event": threading.Event(),
    }

    # Load the wipe code now: at eject time the USB (and its .py files) is
    # already gone, so a lazy import would fail exactly when it matters.
    from cleanup.cleanup import full_cleanup
    boot_context["full_cleanup"] = full_cleanup
    if not args.dry_run:
        _start_eject_poller(boot_context)

    use_desktop = not args.no_ui and args.ui == "desktop" and _desktop_available()

    # --- Start agent (terminal REPL; the desktop app has its own chat) ---
    agent_thread = None if use_desktop else _start_agent(boot_context)

    # --- Start web UI ---
    ui_thread = None
    if not args.no_ui and not use_desktop:
        ui_thread = _start_ui(boot_context)
        url = f"http://localhost:{args.port}/?t={boot_context['ui_token']}"
        print(f"  Web UI: {url}")

        # Auto-open an isolated browser window after a short delay (give
        # Flask time to bind); its profile lives in the session dir.
        def _open_browser():
            import time as _time
            from ui.browser import open_private
            _time.sleep(1.5)
            print(f"  Opened in: {open_private(url, boot_context['session_dir'])}")
        threading.Thread(target=_open_browser, daemon=True).start()

    print("\n  carry-ai is running. Press Ctrl+C to stop.\n")

    # --- Wait for shutdown signal (or the headless REPL exiting) ---
    shutdown_event = boot_context["shutdown_event"]

    def _signal_handler(signum, frame):
        log.info("Received signal %s — shutting down.", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        if use_desktop:
            _run_desktop(boot_context)
        else:
            shutdown_event.wait()
    except KeyboardInterrupt:
        pass

    # --- Cleanup ---
    _shutdown(boot_context, agent_thread, ui_thread)


# ===================================================================
# Sub-step helpers (delegate to other modules)
# ===================================================================

def _start_eject_poller(context: dict, interval: float = 2.0) -> threading.Thread:
    """Watch for the USB disappearing and trigger shutdown + cleanup.

    Works on every OS without admin rights or extra packages: once the drive
    is pulled, stat() on our own files fails. Complements the WMI watcher on
    Windows and replaces the root-only udev rule on Linux.
    """
    probe = PROJECT_ROOT / "launcher.py"
    shutdown_event = context["shutdown_event"]

    def _poll():
        while not shutdown_event.wait(interval):
            try:
                probe.stat()
            except OSError:
                log.warning("USB no longer reachable — cleaning up.")
                context["ejected"] = True
                shutdown_event.set()
                return

    t = threading.Thread(target=_poll, name="carry-ai-eject-poller", daemon=True)
    t.start()
    return t


def _desktop_available() -> bool:
    """True if the native desktop app can run (tkinter + a display)."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        log.info("tkinter not available — using the web UI instead.")
        return False
    if sys.platform != "win32" and not (os.environ.get("DISPLAY")
                                        or os.environ.get("WAYLAND_DISPLAY")):
        log.info("No display — using the web UI instead.")
        return False
    return True


def _run_desktop(context: dict) -> None:
    """Run the desktop app on the main thread until closed or ejected."""
    from ui.desktop import CarryAIApp, ChatBackend
    # One dict for the whole session: keys added in the app's API keys
    # window land here too, so they are zeroed with the rest on exit.
    if not isinstance(context.get("api_keys"), dict):
        context["api_keys"] = {}
    ChatBackend.preloaded_keys = context["api_keys"]
    # Share the full boot context so the desktop app's Agent runs in the
    # same mode (local/api/hybrid) with the same model as the rest of boot.
    ChatBackend.boot_context = context
    app = CarryAIApp()
    app.close_when(context["shutdown_event"])
    app.run()


def _run_model_download(models_dir: Path) -> None:
    """Interactive model download from HuggingFace."""
    try:
        from models.downloader import ModelDownloader
        downloader = ModelDownloader(models_dir=models_dir)
        downloader.interactive_download()
    except NotImplementedError:
        log.warning("Model downloader not yet implemented. Place .gguf files in models/ manually.")
    except ImportError as e:
        log.error("Missing dependency for model download: %s", e)
        log.info("Install with: pip install huggingface-hub")


def _decrypt_keys(config_dir: Path, dry_run: bool) -> dict | None:
    """Decrypt API keys into RAM."""
    if dry_run:
        log.info("[dry-run] Skipping API key decryption.")
        return {"_dry_run": True}

    try:
        from crypto.keystore import KeyStore
        ks = KeyStore(enc_path=config_dir / "providers.enc")
        keys = ks.decrypt_interactive()
        provider_names = list(keys.keys())
        log.info("Decrypted keys for providers: %s", ", ".join(provider_names))
        return keys
    except NotImplementedError:
        log.warning("KeyStore not yet implemented. API mode unavailable.")
        return None
    except Exception as e:
        log.error("Failed to decrypt API keys: %s", e)
        return None


def _init_mcp(config_dir: Path, dry_run: bool):
    """Connect MCP servers (defaults for this OS + settings.json mcp.servers).

    Connection runs in the background (mcp/bridge.py); tools reach the agent
    as mcp__<server>__<tool> as soon as each server is up. Returns the
    bridge (for status and shutdown), or None.
    """
    if dry_run:
        log.info("[dry-run] Skipping MCP initialization.")
        return None
    try:
        from config.settings import load_settings
        if not load_settings().to_dict().get("mcp", {}).get("auto_connect", True):
            log.info("MCP auto_connect is off.")
            return None
        from mcp.bridge import start_mcp
        return start_mcp(config_dir / "settings.json")
    except Exception as e:
        log.error("MCP initialization failed, continuing without MCP: %s", e)
        return None


def _load_plugins(dry_run: bool) -> list[dict]:
    """Load enabled plugins."""
    if dry_run:
        log.info("[dry-run] Skipping plugin loading.")
        return []

    try:
        from plugins.manager import PluginManager
        plugins = [m.to_dict() for m in PluginManager().boot()]
        log.info("Loaded %d plugin(s).", len(plugins))
        return plugins
    except ImportError:
        log.info("Plugin dependencies not installed. Skipping.")
        return []
    except Exception as e:
        # Plugins are optional — a broken one must never block boot
        log.error("Plugin loading failed, continuing without plugins: %s", e)
        return []


def _start_agent(context: dict) -> threading.Thread:
    """Start the agent loop in a background thread."""
    def _agent_worker():
        try:
            from agent.agent import Agent
            agent = Agent()
            agent.run(context)
        except NotImplementedError:
            log.info("Agent not yet implemented. Running in shell mode.")
            _fallback_shell(context)
        except Exception as e:
            log.error("Agent crashed: %s", e, exc_info=True)
        # Headless: the REPL is the whole app, so quitting it (or stdin
        # hitting EOF) ends the session. With a UI, keep serving.
        if context.get("no_ui"):
            context["shutdown_event"].set()

    t = threading.Thread(target=_agent_worker, name="carry-ai-agent", daemon=True)
    t.start()
    log.info("Agent thread started.")
    return t


def _start_ui(context: dict) -> threading.Thread:
    """Start the Flask web UI in a background thread."""
    def _ui_worker():
        try:
            from ui.app import create_app
            app = create_app(config=context)
            app.run(host="127.0.0.1", port=context["port"], use_reloader=False)
        except NotImplementedError:
            log.info("Web UI not yet implemented.")
        except Exception as e:
            log.error("Web UI crashed: %s", e, exc_info=True)

    t = threading.Thread(target=_ui_worker, name="carry-ai-ui", daemon=True)
    t.start()
    log.info("Web UI thread started on port %s.", context["port"])
    return t


def _fallback_shell(context: dict) -> None:
    """Minimal interactive shell when the agent module is not yet implemented."""
    print("  [fallback] Agent not implemented yet. Starting basic REPL.")
    print("  Type 'quit' to exit.\n")

    mode = context.get("mode", "unknown")
    model = Path(context["model_path"]).name if context.get("model_path") else "none"

    while True:
        try:
            prompt = input(f"  carry-ai ({mode}|{model})> ")
        except (EOFError, KeyboardInterrupt):
            break

        prompt = prompt.strip()
        if not prompt:
            continue
        if prompt.lower() in ("quit", "exit", "q"):
            break

        print(f"  [echo] {prompt}")
        print("  (Agent not yet implemented — this is a placeholder REPL)")
        print()


def _shutdown(context: dict, agent_thread: threading.Thread | None,
              ui_thread: threading.Thread | None) -> None:
    """Graceful shutdown and cleanup."""
    print("\n  Shutting down carry-ai...")

    # Stop MCP servers (the eject cleanup also kills any leftover children)
    bridge = context.get("mcp_bridge")
    if bridge is not None and not context.get("ejected"):
        try:
            bridge.shutdown()
        except Exception as e:
            log.debug("MCP shutdown: %s", e)

    # Attempt cleanup
    if not context.get("dry_run"):
        try:
            full_cleanup = context.get("full_cleanup")
            if full_cleanup is None:
                from cleanup.cleanup import full_cleanup
            full_cleanup(session_dir=context.get("session_dir"), dry_run=False)
        except NotImplementedError:
            log.info("Cleanup module not yet implemented.")
        except Exception as e:
            log.error("Cleanup failed: %s", e)
    else:
        # Dry-run: just remove the temp session dir
        session_dir = Path(context.get("session_dir", ""))
        if session_dir.exists() and session_dir.name == "_dry_run_session":
            import shutil
            shutil.rmtree(session_dir, ignore_errors=True)
            log.info("[dry-run] Cleaned up %s", session_dir)

    # Zero API keys from memory
    keys = context.get("api_keys")
    if keys and isinstance(keys, dict):
        for k in list(keys.keys()):
            keys[k] = None
        context["api_keys"] = None
        log.info("API keys zeroed from memory.")

    print("  Goodbye.\n")

    if context.get("ejected"):
        # The drive is gone: normal interpreter teardown would try to touch
        # files on it and trip over threads blocked on stdin. Leave now.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


# ===================================================================
# CLI argument parsing
# ===================================================================

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="carry-ai",
        description="Portable AI Assistant — inject, assist, vanish.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python launcher.py                    # Auto-detect everything and boot
  python launcher.py --dry-run          # Test without USB hardware
  python launcher.py --mode api         # Force API-only mode
  python launcher.py --mode local       # Force local-only mode
  python launcher.py --download-model   # Download a model first, then boot
  python launcher.py --no-ui --verbose  # Headless with debug logging
        """,
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate boot without injection or USB hardware",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["local", "api", "hybrid"],
        default=None,
        help="Force execution mode (default: auto-detect)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Web UI port (default: 8080)",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        default=False,
        help="Headless mode — skip Flask web UI",
    )
    parser.add_argument(
        "--ui",
        choices=["web", "desktop"],
        default="web",
        help="Interface: 'desktop' (native window, falls back to web) or 'web' "
             "(isolated browser window). Default: web",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable debug logging",
    )
    parser.add_argument(
        "--download-model",
        action="store_true",
        default=False,
        help="Interactive model download from HuggingFace before boot",
    )

    return parser.parse_args(argv)


# ===================================================================
# Entry point
# ===================================================================

def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the entire application."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, stream=sys.stderr)

    # Quiet noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    args = parse_args(argv)
    setup_logging(verbose=args.verbose)
    try:
        boot(args)
    except Exception:
        # Don't leave a half-built session behind on the host
        log.exception("Boot failed — wiping session before exit.")
        if not args.dry_run:
            from cleanup.cleanup import wipe_session_directory
            wipe_session_directory(None)
        raise


if __name__ == "__main__":
    main()
