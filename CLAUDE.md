# CLAUDE.md — Carry-AI Codebase Guide

This file provides context for AI assistants working in this repository.

---

## Project Overview

**Carry-AI** is a portable, USB-resident AI assistant built in pure Python 3.10+. It runs from a USB drive, injects itself into a host OS session (Windows or Linux), routes to local GGUF models or cloud LLM APIs, and wipes all traces on USB eject. It exposes a ReAct-pattern agent loop with a Flask web UI at `localhost:8080`.

---

## Repository Structure

```
carry-ai/
├── launcher.py              # Single entry point — boot orchestrator
├── onboard.py               # Interactive setup wizard (run this first)
├── flash_usb.py             # Rufus-style USB flasher — run on host, writes everything to USB
├── setup_usb.py             # USB package env builder — run from USB after copy
├── bootstrap.py             # sys.path injector — called by start scripts, prepends USB packages
├── start.bat                # Windows launcher — uses python-env\windows\python.exe if present
├── start.sh                 # Linux launcher — sets PYTHONPATH to python-env/linux/site-packages
├── package.py               # USB packaging & distribution script
├── requirements.txt         # Python dependencies
├── README.md                # End-user documentation

├── docs/
│   ├── getting-started.md   # Step-by-step onboarding guide
│   ├── configuration.md     # Full config/settings.json reference
│   ├── providers.md         # API key setup for all 9 providers
│   └── architecture.md     # System design & data-flow diagrams

├── modes/
│   ├── api_mode.py          # Cloud API router with provider health tracking
│   └── local_mode.py        # llama.cpp local inference runner

├── providers/               # LLM provider integrations (7 providers)
│   ├── base.py              # Abstract base class & exception hierarchy
│   ├── openai_compat.py     # Shared OpenAI-compatible base class
│   ├── anthropic_provider.py
│   ├── google_oauth.py
│   ├── openai_provider.py
│   ├── groq_provider.py
│   ├── openrouter_provider.py
│   ├── godmode_provider.py
│   └── onyx_provider.py

├── agent/
│   ├── agent.py             # ReAct agent loop with conversation history
│   ├── tools.py             # 14+ built-in tools + extensible registry
│   └── memory.py            # SQLite-backed persistent memory (FTS5, v2)

├── ui/
│   └── app.py               # Flask SPA at localhost:8080 (HTML/CSS/JS embedded)

├── mcp/
│   ├── client.py            # JSON-RPC transport (stdio, HTTP, SSE)
│   ├── config.py            # MCP server configuration loader
│   └── registry.py          # Thread-safe MCP tool registry

├── plugins/
│   ├── loader.py            # Plugin discovery & manifest parsing
│   └── manager.py           # Plugin install/enable/disable/uninstall

├── integrations/
│   ├── scrapling_tools.py   # Adaptive web scraping (anti-bot bypass)
│   ├── gworkspace_tools.py  # Google Workspace API (Drive, Gmail, Sheets, Calendar)
│   ├── llmfit_advisor.py    # Hardware-aware model selection
│   └── voice_tools.py       # Voice pipeline: push-to-talk → ASR → vision → TTS (farzaa/clicky)

├── cowork/
│   ├── session_sharing.py   # Session export (JSON/Markdown)
│   └── teams.py             # Team management & task tracking

├── inject/
│   ├── inject_windows.py    # Windows: copy to %TEMP%, WMI eject watcher
│   └── inject_linux.sh      # Linux: tmpfs mount + udev rules

├── cleanup/
│   └── cleanup.py           # 6-step trace wiper triggered on USB eject

├── models/
│   └── downloader.py        # HuggingFace GGUF browser & downloader

├── crypto/
│   └── keystore.py          # Fernet encryption, PBKDF2 key derivation

└── config/
    ├── settings.py          # Hierarchical config loader
    ├── settings.json        # (USB-resident, optional) runtime overrides
    └── providers.enc        # (USB-resident) Fernet-encrypted API keys
```

---

## Technology Stack

- **Language**: Python 3.10+ (no Node.js, no build step)
- **Web server**: Flask 3.0+ at `localhost:8080`
- **Local inference**: llama.cpp (external binary, managed via `modes/local_mode.py`)
- **Encryption**: `cryptography` library — Fernet (AES-128-CBC + HMAC), PBKDF2 600k iterations
- **Memory**: SQLite + FTS5 via `agent/memory.py`
- **HTTP**: `requests` for provider API calls and HuggingFace downloads
- **System**: `psutil` for RAM/CPU detection, `pyautogui` for screenshot/input tools
- **Optional**: `scrapling`, `llmfit`, MCP SDK, `wmi` (Windows), `pyudev` (Linux)

---

## Boot Sequence (`launcher.py`)

1. Detect OS (Windows/Linux)
2. Probe RAM via `psutil` (12 tiers: `<3 GB` → `32+ GB`)
3. Inject session: copy runtime to `%TEMP%\ai_session\` (Windows) or tmpfs at `/tmp/ai_session/` (Linux)
4. Select mode: `local` | `api` | `hybrid` | `auto`
5. Auto-select GGUF model if local/hybrid (RAM-tier + quantization-aware)
6. Decrypt `config/providers.enc` in RAM if api/hybrid
7. Connect MCP servers (`mcp/`)
8. Load plugins (`plugins/`)
9. Start Flask UI + agent loop
10. Block on eject event → run `cleanup/cleanup.py`

**CLI flags:**
```
--dry-run              Test without USB hardware
--mode local|api|hybrid  Force mode
--port PORT            Override port (default 8080)
--no-ui                Headless CLI
--verbose              Debug logging
--download-model       Launch interactive HF model downloader
```

---

## Configuration System (`config/settings.py`)

Four-layer hierarchy (later overrides earlier):
1. Built-in `DEFAULTS` dict in `settings.py`
2. `config/settings.json` on USB
3. Environment variables (`CARRY_AI_*` prefix — e.g., `CARRY_AI_PORT=9090`)
4. CLI flags

Key settings path examples:
- `settings.mode` — `"auto"` | `"local"` | `"api"` | `"hybrid"`
- `settings.port` — default `8080`
- `settings.agent.max_iterations`
- `settings.agent.permission_mode` — `"ask"` | `"yolo"` | `"safe"`
- `settings.providers.anthropic.enabled`
- `settings.local.gpu_layers`
- `settings.ui.theme` — `"dark"` | `"light"`
- `settings.mcp.servers`

---

## Provider Pattern

All providers extend `providers/base.py:BaseProvider`. Required interface:

```python
class MyProvider(BaseProvider):
    @property
    def provider_name(self) -> str: ...

    def chat(self, messages: list[dict], **kwargs) -> ChatResponse: ...
    def stream(self, messages: list[dict], **kwargs) -> Generator: ...
    def is_available(self) -> bool: ...
    def models(self) -> list[str]: ...
```

Exception hierarchy from `providers/base.py`:
```
ProviderError
├── RateLimitError       (HTTP 429)
├── AuthenticationError  (HTTP 401/403)
└── OverloadedError      (HTTP 529/503)
```

OpenAI-compatible providers inherit from `providers/openai_compat.py` (shared base for OpenAI, Groq, OpenRouter).

---

## Tool Registration (`agent/tools.py`)

Tools are registered via:
```python
register_tool(
    name="tool_name",
    description="What this tool does",
    parameters={"type": "object", "properties": {...}},
    execute_fn=lambda **kwargs: "result",
    required=["param1"]
)
```

Built-in tools include: shell execution, file read/write, web fetch, screenshot, clipboard, memory recall, and more. MCP tools are merged into the same registry at runtime via `mcp/registry.py`.

---

## Memory System (`agent/memory.py`)

- **Storage**: SQLite with FTS5 full-text search at `config/memory.db` (survives sessions)
- **Deduplication**: SHA-256 content hash with 30-second window
- **Observation types**: `fact`, `discovery`, `decision`, `bugfix`, `note`, `summary`, `preference`
- **Concept tags**: `how-it-works`, `why-it-exists`, `what-changed`, `problem-solution`, `gotcha`, `pattern`, `trade-off`
- **Relevance decay**: Algorithm deprioritizes stale memories
- Schema created automatically on first access (no migration tooling)

---

## Encryption (`crypto/keystore.py`)

- API keys stored in `config/providers.enc` (stays on USB)
- Format: `{"salt": <base64 16-byte salt>, "data": <fernet encrypted JSON>}`
- Key derivation: PBKDF2-HMAC-SHA256, 600,000 iterations
- Decrypted only in RAM during active session; wiped on eject

Manage keys:
```bash
python crypto/keystore.py setup    # Interactive wizard
python crypto/keystore.py list
python crypto/keystore.py add openai
python crypto/keystore.py remove groq
```

---

## Packaging & Distribution (`package.py`)

```bash
python package.py --target E:\                    # Package to USB
python package.py --target ./test --verbose       # Local test
python package.py --target E:\ --include-models   # Include GGUF models
python package.py --validate-only --target .      # Validate only
```

---

## Model Management (`models/downloader.py`)

```bash
python models/downloader.py interactive          # Wizard
python models/downloader.py search "Qwen3 8B"   # Search HuggingFace
python models/downloader.py suggest --ram 16     # RAM-based suggestion
python models/downloader.py local                # List downloaded models
```

RAM-to-model tier map (in `launcher.py`): 12 tiers from `≥32 GB → Qwen3 30B-Q8` down to `<3 GB → Gemma 1B-Q4`.

---

## Code Conventions

**Naming:**
- Files/modules: `snake_case.py`
- Classes: `PascalCase`
- Functions/variables: `snake_case`
- Constants: `UPPER_CASE`

**Imports (order):**
1. stdlib
2. third-party
3. local (absolute from project root, e.g., `from providers.base import BaseProvider`)

**Optional dependencies** use try/except guard:
```python
try:
    import psutil
except ImportError:
    psutil = None
```

**Path resolution:**
```python
PROJECT_ROOT = Path(__file__).resolve().parent
```

**Logging:**
```python
log = logging.getLogger("carry-ai.module_name")
```

**Type hints:** Use Python 3.10+ union syntax (`dict | None`) and stdlib types (`list[dict]`, `tuple[str, int]`).

**Dataclasses** are used for data structures (`@dataclass`).

**Each module** has a docstring header describing its purpose and architecture.

---

## Permission System (`agent/agent.py`)

Three modes controlled by `settings.agent.permission_mode`:
- `ask` — Prompt user before executing dangerous operations
- `yolo` — Execute all tools without confirmation
- `safe` — Block dangerous patterns outright

`PermissionPolicy` checks 16 regex patterns before tool execution (e.g., `rm -rf`, `format`, `DROP TABLE`, `git push --force`).

---

## Testing

There is **no formal test framework** (no pytest, no test/ directory). Validation strategies:
- `--dry-run` flag: boots without USB hardware
- Plugin manifest validation in `plugins/loader.py`
- Settings validation in `config/settings.py`
- Provider config checks in `providers/base.py`

When making changes, use `python launcher.py --dry-run --verbose` to validate the boot sequence.

---

## Voice Pipeline (`integrations/voice_tools.py`)

Inspired by [farzaa/clicky](https://github.com/farzaa/clicky) — a push-to-talk macOS AI assistant. The Python port provides the same pipeline:

**Flow**: Push-to-talk recording → AssemblyAI transcription → screenshot (base64) → Claude vision → ElevenLabs TTS playback

**Key classes**:
- `VoiceConfig` — dataclass: keys, voice_id, vision_enabled, max_recording_seconds
- `AudioRecorder` — PyAudio recording on a daemon thread; main thread blocks on `input()` for PTT
- `Transcriber` — AssemblyAI REST (upload → submit → poll); falls back to placeholder string if unavailable
- `ScreenCapture` — `PIL.ImageGrab` → `pyautogui.screenshot()` fallback, returns base64 PNG
- `TTSPlayer` — ElevenLabs SDK → REST streaming fallback → silent no-op
- `VoicePipeline` — chains all four; `run_loop()` for continuous PTT session
- `is_voice_available()` — returns per-component availability dict for graceful degradation

All optional deps (pyaudio, assemblyai, elevenlabs, PIL) are guarded with try/except. The pipeline silently degrades when packages or keys are absent.

**Activation**: Set `voice.enabled = true` in `config/settings.json` or use the `onboard.py` Voice Setup step.

---

## Key External References

| Project | Used in | Purpose |
|---|---|---|
| [claude-mem](https://github.com/thedotmack/claude-mem) | `agent/memory.py` | FTS5 memory architecture |
| [Scrapling](https://github.com/D4Vinci/Scrapling) | `integrations/scrapling_tools.py` | Anti-bot web scraping |
| [G0DM0D3](https://github.com/elder-plinius/G0DM0D3) | `providers/godmode_provider.py` | Multi-model racing |
| [Onyx](https://github.com/onyx-dot-app/onyx) | `providers/onyx_provider.py` | RAG with 50+ connectors |
| [llmfit](https://github.com/AlexsJones/llmfit) | `integrations/llmfit_advisor.py` | Hardware-aware model selection |
| [clicky](https://github.com/farzaa/clicky) | `integrations/voice_tools.py` | Push-to-talk voice pipeline (Python port) |

---

## Documentation (`docs/`)

| File | Contents |
|------|----------|
| `docs/getting-started.md` | Zero-to-running guide: wizard, manual setup, first boot |
| `docs/configuration.md` | Every `settings.json` key with defaults and descriptions |
| `docs/providers.md` | API key setup for all 9 providers (7 LLM + AssemblyAI + ElevenLabs) |
| `docs/architecture.md` | System diagram, boot sequence, ReAct loop, security model |

---

## USB Self-Hosting Architecture

The USB drive carries its own Python runtime. No installation on the host machine is required (Windows) or minimal (Linux, needs Python 3.10+).

**Directory layout on USB:**
```
USB_ROOT/
├── carry-ai/              ← this repo
├── python-env/
│   ├── windows/           ← embeddable Python + site-packages
│   └── linux/
│       └── site-packages/ ← packages, injected via PYTHONPATH
├── models/                ← GGUF files
├── start.bat / start.sh   ← entry points
```

**Boot path on Windows:**
`start.bat` → `python-env\windows\python.exe bootstrap.py` → injects USB site-packages → `launcher.main()`

**Boot path on Linux:**
`start.sh` sets `PYTHONPATH=python-env/linux/site-packages` → `python3 bootstrap.py` → `launcher.main()`

**flash_usb.py** is the host-side "Rufus" equivalent: detects USB drives, copies carry-ai, downloads packages and GGUF models onto the USB, writes start scripts. Run it once on any machine with Python installed to prepare a USB.

**setup_usb.py** is the USB-side alternative: run it from within the USB after manually copying carry-ai. Offers the same package/model download flow.

---

## Important Notes for AI Assistants

1. **No build step** — Python only; changes take effect immediately.
2. **No migrations** — SQLite schema is auto-created by `agent/memory.py` on first run.
3. **USB-first design** — `config/providers.enc` and `config/settings.json` are expected to live on the USB, not in the repo. Never commit secrets.
4. **Trace wipe is destructive** — `cleanup/cleanup.py` performs a 6-step sweep; test changes to it carefully with `--dry-run`.
5. **Optional imports** — Many integrations are optional. Always guard third-party imports with try/except; never make optional deps required without updating `requirements.txt`.
6. **Provider additions** — New providers must extend `BaseProvider`, handle all three exception types, and be registered in `modes/api_mode.py`.
7. **Tool additions** — Register via `register_tool()` in `agent/tools.py`; keep `execute_fn` side-effect-safe when `permission_mode == "safe"`.
8. **Voice pipeline** — `integrations/voice_tools.py` is fully optional; all four deps (pyaudio, assemblyai, elevenlabs, Pillow) are guarded. Never make them required.
9. **Onboarding** — `onboard.py` uses `rich` for the visual experience but has a complete plain-text fallback; it must run with only stdlib if rich is not yet installed.
10. **USB self-hosting** — `flash_usb.py` and `setup_usb.py` install packages with `pip install --target` into the USB. `bootstrap.py` injects that directory via `sys.path.insert(0, ...)`. Never assume host site-packages are available; all imports that aren't stdlib should be guarded with try/except.
11. **Portable Python URL** — Windows embeddable Python is downloaded from `https://www.python.org/ftp/python/{VERSION}/python-{VERSION}-embed-amd64.zip`. The version string in the URL uses dots (e.g. `3.11.9`), not digits concatenated.
