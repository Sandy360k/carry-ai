"""
carry-ai/config/settings.py -- Configuration Management
=========================================================

Loads, merges, and validates carry-ai configuration from multiple sources.

Configuration Sources (merged in order, later overrides earlier):
    1. Built-in DEFAULTS dict              -- Hardcoded defaults
    2. {usb-root}/config/settings.json     -- User settings on USB
    3. Environment variables               -- CARRY_AI_* prefix
    4. CLI flags                           -- --mode, --port, etc.
"""

import json
import logging
import os
from copy import deepcopy
from pathlib import Path

logger = logging.getLogger("carry-ai.config")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULTS = {
    "mode": "auto",           # auto | local | api | hybrid
    "port": 8080,
    "model": "auto",          # auto | specific GGUF filename
    "verbose": False,

    "providers": {
        # Cloud model IDs go stale; providers verify these against the live
        # model list at runtime and fall back to their default if retired.
        "anthropic":  {"enabled": True,  "model": "claude-opus-5"},
        "openai":     {"enabled": True,  "model": "gpt-6-luna"},
        "google":     {"enabled": True,  "model": "gemini-3.5-flash-lite"},
        "groq":       {"enabled": True,  "model": "openai/gpt-oss-20b"},
        "openrouter": {"enabled": True,  "model": "openrouter/free"},
        "godmode":    {"enabled": False, "model": "anthropic/claude-opus-5.5",
                       "base_url": "http://localhost:7860/v1",
                       "autotune": False, "stm_modules": []},
        "onyx":       {"enabled": False, "model": "onyx/default",
                       "base_url": "http://localhost:3000/api"},
    },

    "agent": {
        "max_iterations": 20,
        "tool_timeout_s": 30,
        "permission_mode": "ask",        # ask | yolo | safe
        "context_window": 8192,
        "compaction_threshold": 0.75,    # compact at 75% of context window
    },

    "ui": {
        "theme": "dark",
        "show_tool_output": True,
        "stream": True,
    },

    "local": {
        "llama_server_args": [],         # Extra args for llama-server
        "gpu_layers": 0,                 # Number of layers to offload to GPU
        "threads": 0,                    # 0 = auto-detect
    },

    "mcp": {
        "servers": {},                   # name -> {command, args, env}
        "auto_connect": True,
    },

    "plugins": {
        "enabled": [],                   # List of enabled plugin names
        "auto_load": True,
    },

    "cowork": {
        "sharing_enabled": False,
        "team_name": "",
        "session_ttl_minutes": 60,
    },

    # Hugging Face sign-in (models/hf_auth.py). oauth_client_id is the id of
    # a *public* OAuth app (no secret, scope "gated-repos") registered once
    # at https://huggingface.co/settings/applications/new. Empty = the
    # "Sign in" button explains how to set it up; pasting a token still works.
    "huggingface": {
        "oauth_client_id": "",
    },

    # Voice (integrations/voice_tools.py). Each direction is "auto" (offline
    # sherpa-onnx model if downloaded, else the cloud service if its key is
    # in the keystore), "offline", "cloud" or "off". Cloud keys (AssemblyAI,
    # ElevenLabs) live in providers.enc, never here.
    "voice": {
        "stt_backend": "auto",
        "tts_backend": "auto",
        "stt_model": "moonshine-tiny-en",   # models/voice.py ids
        "tts_model": "kitten-nano-en",
        "tts_speaker": 0,
        "tts_speed": 1.0,
        "speak_replies": False,             # read agent replies aloud
        "auto_send": True,                  # send the transcript right away
        "vision_enabled": False,            # attach a screenshot (terminal loop)
        "max_recording_seconds": 60,
    },

    # run_python tool (integrations/python_sandbox.py, pydantic-monty):
    # sandboxed Python with no host filesystem/network; /work is a scratch
    # folder in the session dir. Registered only if pydantic-monty is installed.
    "sandbox": {
        "enabled": True,
        "timeout_s": 10,
        "max_memory_mb": 256,
        # Chat-window switch (🔒 Sandbox): on = only tools that can't touch
        # this PC; off = all tools under the permission policy, and
        # run_python can read host_folders at /host/<name>.
        "start_sandboxed": False,
        "host_folders": [],       # default: Desktop, Documents, Downloads
    },

    # Peripheral features, off by default. They don't fit the disposable,
    # offline-first USB session (extra services, host installs, or shared
    # state) and are opt-in. Enable in settings.json, e.g.
    #   {"experimental": {"onyx": true}}
    "experimental": {
        "godmode": False,           # providers/godmode_provider.py (model racing)
        "onyx": False,              # providers/onyx_provider.py (RAG search)
        "google_workspace": False,  # integrations/gworkspace_tools.py (needs gws CLI)
        "cowork": False,            # cowork/ (team session sharing)
    },

    "cleanup": {
        "wipe_clipboard": True,
        "scrub_recent_files": True,
        "kill_child_processes": True,
    },
}

# Env var prefix
ENV_PREFIX = "CARRY_AI_"

# ---------------------------------------------------------------------------
# Deep merge utility
# ---------------------------------------------------------------------------
def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Returns new dict."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Settings class
# ---------------------------------------------------------------------------
class Settings:
    """
    Carry-ai configuration with hierarchical merging and dot-access.

    Usage:
        settings = Settings.load(usb_root="/mnt/usb")
        print(settings.mode)
        print(settings.agent.max_iterations)
        settings.port = 9090
        settings.save()
    """

    def __init__(self, data: dict = None, config_path: str = None):
        # `data if data is not None` — an empty dict is a valid settings node
        # (e.g. settings.mcp.servers); `or` would silently swap in DEFAULTS.
        object.__setattr__(self, "_data", data if data is not None else deepcopy(DEFAULTS))
        object.__setattr__(self, "_config_path", config_path)

    # -- Dot access --------------------------------------------------------

    def __getattr__(self, name):
        data = object.__getattribute__(self, "_data")
        if name in data:
            val = data[name]
            if isinstance(val, dict):
                return Settings(val)
            return val
        raise AttributeError(f"No setting: {name}")

    def __setattr__(self, name, value):
        data = object.__getattribute__(self, "_data")
        data[name] = value

    def __contains__(self, name):
        return name in object.__getattribute__(self, "_data")

    def __repr__(self):
        return f"Settings({object.__getattribute__(self, '_data')})"

    # -- Dict-like access --------------------------------------------------

    def get(self, key, default=None):
        data = object.__getattribute__(self, "_data")
        val = data.get(key, default)
        if isinstance(val, dict):
            return Settings(val)
        return val

    def to_dict(self) -> dict:
        """Return a plain dict copy of all settings."""
        return deepcopy(object.__getattribute__(self, "_data"))

    def keys(self):
        return object.__getattribute__(self, "_data").keys()

    def items(self):
        return object.__getattribute__(self, "_data").items()

    # -- Load / Save -------------------------------------------------------

    @classmethod
    def load(cls, usb_root: str = None, cli_overrides: dict = None) -> "Settings":
        """
        Load settings from all sources in priority order:
        1. Built-in defaults
        2. settings.json on USB
        3. Environment variables
        4. CLI overrides dict
        """
        merged = deepcopy(DEFAULTS)

        # Layer 2: settings.json from USB or project root
        json_path = None
        if usb_root:
            candidate = os.path.join(usb_root, "config", "settings.json")
            if os.path.isfile(candidate):
                json_path = candidate

        if json_path is None:
            # Try relative to this file
            project_root = Path(__file__).parent.parent
            candidate = project_root / "config" / "settings.json"
            if candidate.is_file():
                json_path = str(candidate)

        if json_path:
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                merged = _deep_merge(merged, user_cfg)
                logger.info("Loaded settings from %s", json_path)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load %s: %s", json_path, e)

        # Layer 3: Environment variables (CARRY_AI_MODE, CARRY_AI_PORT, etc.)
        env_overrides = _load_env_vars()
        if env_overrides:
            merged = _deep_merge(merged, env_overrides)
            logger.debug("Applied %d env var overrides", len(env_overrides))

        # Layer 4: CLI overrides
        if cli_overrides:
            merged = _deep_merge(merged, cli_overrides)
            logger.debug("Applied CLI overrides: %s", list(cli_overrides.keys()))

        return cls(merged, config_path=json_path)

    def save(self, path: str = None):
        """Save current settings to JSON file."""
        save_path = path or object.__getattribute__(self, "_config_path")
        if not save_path:
            raise RuntimeError("No config path set -- pass path= or load from file first")

        data = object.__getattribute__(self, "_data")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Settings saved to %s", save_path)

    # -- Validation --------------------------------------------------------

    def validate(self) -> list:
        """
        Validate settings. Returns list of error strings (empty = valid).
        """
        errors = []
        data = object.__getattribute__(self, "_data")

        mode = data.get("mode", "auto")
        if mode not in ("auto", "local", "api", "hybrid"):
            errors.append(f"Invalid mode: {mode}")

        port = data.get("port", 8080)
        if not isinstance(port, int) or port < 1 or port > 65535:
            errors.append(f"Invalid port: {port}")

        agent = data.get("agent", {})
        perm = agent.get("permission_mode", "ask")
        if perm not in ("ask", "yolo", "safe"):
            errors.append(f"Invalid permission_mode: {perm}")

        max_iter = agent.get("max_iterations", 20)
        if not isinstance(max_iter, int) or max_iter < 1 or max_iter > 100:
            errors.append(f"Invalid max_iterations: {max_iter}")

        return errors


# ---------------------------------------------------------------------------
# Environment variable loader
# ---------------------------------------------------------------------------
def _load_env_vars() -> dict:
    """
    Read CARRY_AI_* environment variables and map them to settings.

    Supported:
        CARRY_AI_MODE=api
        CARRY_AI_PORT=9090
        CARRY_AI_VERBOSE=1
        CARRY_AI_PERMISSION_MODE=yolo
        CARRY_AI_THEME=light
        CARRY_AI_GPU_LAYERS=35
    """
    overrides = {}

    mapping = {
        "MODE":            ("mode", str),
        "PORT":            ("port", int),
        "VERBOSE":         ("verbose", lambda v: v.lower() in ("1", "true", "yes")),
        "MODEL":           ("model", str),
        "PERMISSION_MODE": ("agent.permission_mode", str),
        "MAX_ITERATIONS":  ("agent.max_iterations", int),
        "THEME":           ("ui.theme", str),
        "GPU_LAYERS":      ("local.gpu_layers", int),
    }

    for env_suffix, (dotpath, cast) in mapping.items():
        val = os.environ.get(f"{ENV_PREFIX}{env_suffix}")
        if val is not None:
            try:
                parts = dotpath.split(".")
                d = overrides
                for part in parts[:-1]:
                    d = d.setdefault(part, {})
                d[parts[-1]] = cast(val)
            except (ValueError, TypeError):
                logger.warning("Invalid env var %s%s=%s", ENV_PREFIX, env_suffix, val)

    return overrides


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------
def get_default_settings() -> dict:
    """Return a copy of the default settings dict."""
    return deepcopy(DEFAULTS)


def load_settings(usb_root: str = None, cli_overrides: dict = None) -> Settings:
    """Convenience wrapper for Settings.load()."""
    return Settings.load(usb_root=usb_root, cli_overrides=cli_overrides)


def save_user_settings(patch: dict, usb_root: str = None) -> str:
    """Merge *patch* into config/settings.json (created if missing).

    Only the user's own overrides are stored, not the full defaults.
    Returns the path written.
    """
    base = Path(usb_root) / "config" if usb_root else Path(__file__).parent
    path = base / "settings.json"
    current = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Rewriting unreadable %s: %s", path, e)
    merged = _deep_merge(current, patch)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return str(path)


def is_experimental_enabled(feature: str, usb_root: str = None) -> bool:
    """True if a peripheral feature is opted in via ``experimental.<feature>``.

    Safe to call from anywhere (returns False on any load error) so gate
    points stay one-liners. See the ``experimental`` block in DEFAULTS.
    """
    try:
        exp = load_settings(usb_root=usb_root).to_dict().get("experimental", {})
        return bool(exp.get(feature, False))
    except Exception as e:
        logger.debug("experimental flag check failed for %s: %s", feature, e)
        return False
