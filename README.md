# carry-ai

> Plug in. Chat. Eject. Leave no trace.

A portable AI assistant that lives on a USB drive. Plug it into any Windows or Linux machine, run one command, and you have a full AI agent at `localhost:8080` — with local GGUF models, cloud API failover, voice mode, persistent memory, and 30+ tools. Eject the USB and everything is wiped.

```
   ____                                _    ___
  / ___|__ _ _ __ _ __ _   _          / \  |_ _|
 | |   / _` | '__| '__| | | |  ___  / _ \  | |
 | |__| (_| | |  | |  | |_| | |___| / ___ \ | |
  \____\__,_|_|  |_|   \__, |      /_/   \_\___|
                        |___/
  Portable AI Assistant — inject, assist, vanish.
```

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Table of Contents

- [Quick Start](#quick-start)
- [Scripts Guide](#scripts-guide)
- [Three Modes](#three-modes)
- [Features](#features)
- [USB Self-Hosting](#usb-self-hosting)
- [Voice Mode](#voice-mode)
- [API Providers](#api-providers)
- [Local Models](#local-models)
- [Agent & Tools](#agent--tools)
- [Persistent Memory](#persistent-memory)
- [Desktop App](#desktop-app)
- [Web UI](#web-ui)
- [MCP Integration](#mcp-integration)
- [Plugin System](#plugin-system)
- [Cowork Features](#cowork-features)
- [Configuration](#configuration)
- [Security & Cleanup](#security--cleanup)
- [Packaging to USB](#packaging-to-usb)
- [Project Structure](#project-structure)
- [Referenced Projects](#referenced-projects)
- [Requirements](#requirements)
- [License](#license)

---

## Quick Start

### Option 1 — Flash a USB drive (recommended)

Run this on any machine with Python installed:

```bash
python flash_usb.py
```

**Works like Rufus.** Detects your USB drives, you pick one, and it copies carry-ai, downloads packages, bundles a portable Python for Windows, and optionally downloads GGUF models. After flashing the USB is fully self-contained:

```text
Windows host  ->  insert USB, double-click start.bat   (no Python required)
Linux host    ->  insert USB, bash /media/usb/start.sh  (only needs Python 3.10+)
```

Repeated flashes are fast -- packages are cached locally after the first run.

---

### Option 2 — Set up an existing USB

If carry-ai is already on the USB and you just want to install dependencies:

```bash
python setup_usb.py
```

---

### Option 3 — Run the onboarding wizard

```bash
python onboard.py
```

Walks you through system checks, USB detection (update/overwrite/clean-flash an existing USB), dependency installation, mode selection, API key setup, voice config, and a dry-run validation. Good for first-time setup or when returning to a USB you haven't used in a while.

---

### Option 4 — Direct launch (dev mode)

```bash
python launcher.py                   # Auto-detect mode and boot
python launcher.py --dry-run         # Test without USB hardware
python launcher.py --mode api        # Force API-only mode
python launcher.py --mode local      # Force local GGUF mode
python launcher.py --mode hybrid     # Local first, API fallback
python launcher.py --download-model  # Download a GGUF model first
python launcher.py --port 9090       # Custom web UI port
python launcher.py --no-ui           # Headless CLI, no web server
python launcher.py --verbose         # Debug logging
```

See [`docs/getting-started.md`](docs/getting-started.md) for the full setup guide.

---

## Scripts Guide

carry-ai has several Python scripts, each with a distinct purpose. Here's when and why to use each one.

### `flash_usb.py` — Flash a USB drive from scratch

**When to use:** You have a blank USB drive (or want to overwrite one) and want a fully self-contained portable AI assistant.

**What it does:**

1. Detects all removable USB drives on your machine
2. Asks what to bundle: Windows portable Python, Linux packages, LLM provider SDKs, agent tools, voice pipeline, LocalAI binary
3. Shows the full GGUF model catalogue (14 models, 2 GB to 12 GB) and lets you pick which to download
4. Copies carry-ai source, installs packages onto the USB, downloads models, writes launcher scripts (`start.bat`, `start.sh`, `autorun.inf`)
5. Verifies the flash succeeded

**Package caching:** The first flash downloads all packages from PyPI and saves them in a local `.pkg-cache/` directory. Every subsequent flash installs from that cache -- no network needed, much faster.

```bash
python flash_usb.py                              # Interactive wizard
python flash_usb.py --add-models --target H:\    # Add models to existing USB
python flash_usb.py --add-localai --target H:\   # Add LocalAI binary
```

---

### `onboard.py` — Interactive setup wizard

**When to use:** First-time setup on any machine, or when you want to configure carry-ai from scratch. Also useful for managing an existing USB.

**What it does (8 steps):**

| Step | Name | What happens |
| ---- | ---- | ------------ |
| 1 | System Check | Probes Python version, OS, RAM, free disk |
| 2 | USB Drive Check | Scans for USB drives, detects existing carry-ai installations |
| 3 | Dependencies | Checks required/optional packages, offers install or update-all |
| 4 | Mode Selection | Recommends local/api/hybrid based on RAM, models, keys |
| 5 | Provider Setup | Collects API keys, encrypts them to `config/providers.enc` |
| 6 | Voice Setup | Configures push-to-talk pipeline (AssemblyAI + ElevenLabs) |
| 7 | Config & Validate | Writes `config/settings.json`, runs `launcher.py --dry-run` |
| 8 | Ready | Shows launch commands |

**USB Drive Check (Step 2)** detects existing carry-ai on a plugged-in USB and offers:

- **Update** -- sync source code + update all packages (preserves config, keys, models)
- **Overwrite** -- replace source code only (preserves everything else)
- **Clean flash** -- wipe everything for a fresh start
- **Update deps** -- update packages only, no source changes
- **Skip** -- continue without touching the USB

**Model download** can be skipped during onboarding -- the wizard shows instructions for downloading later via `flash_usb.py` or `models/downloader.py`.

```bash
python onboard.py    # Run the full wizard
```

---

### `launcher.py` — Boot orchestrator (main entry point)

**When to use:** You're ready to run carry-ai. This is what `start.bat` and `start.sh` ultimately call.

**What it does:**

1. Detects OS (Windows/Linux)
2. Probes RAM via `psutil` (12 tiers from `<3 GB` to `32+ GB`)
3. Injects session files to temp directory
4. Selects mode: `local` | `api` | `hybrid` | `auto`
5. Auto-selects GGUF model if local/hybrid
6. Decrypts API keys in RAM if api/hybrid
7. Connects MCP servers, loads plugins
8. Starts Flask web UI + agent loop at `localhost:8080`
9. Blocks on USB eject event, then runs cleanup

```bash
python launcher.py                 # Auto mode
python launcher.py --dry-run       # Test without USB
python launcher.py --mode hybrid   # Force hybrid
python launcher.py --verbose       # Debug output
```

---

### `setup_usb.py` — USB-side package installer

**When to use:** You manually copied carry-ai onto a USB (without `flash_usb.py`) and need to install packages.

**What it does:** Runs `pip install --target` to install all required packages into the USB's `python-env/` directory. Essentially the package-install portion of `flash_usb.py`, but designed to run from within the USB itself.

```bash
python setup_usb.py    # Run from USB root
```

---

### `bootstrap.py` — sys.path injector

**When to use:** You don't run this directly. It's called by `start.bat` / `start.sh`.

**What it does:** Prepends the USB's `python-env/*/site-packages` directory to `sys.path` so that Python finds the USB-resident packages instead of (missing) host packages. Then hands off to `launcher.py`.

---

### `package.py` — USB packaging and distribution

**When to use:** You want to create a distributable carry-ai USB image, optionally with or without models.

```bash
python package.py --target E:\                    # Package to USB
python package.py --target E:\ --include-models   # Include GGUF models
python package.py --target E:\ --strip-models     # API-only (no models)
python package.py --validate-only --target .       # Validate structure only
```

---

### `crypto/keystore.py` — API key encryption manager

**When to use:** You want to add, remove, or list encrypted API keys stored on the USB.

```bash
python crypto/keystore.py setup     # Interactive wizard
python crypto/keystore.py list      # Show configured providers
python crypto/keystore.py add openai
python crypto/keystore.py remove groq
```

Keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256, PBKDF2 600k iterations) and stored in `config/providers.enc`.

---

### `models/downloader.py` — GGUF model browser and downloader

**When to use:** You want to search, download, or manage local GGUF models.

```bash
python models/downloader.py interactive          # Guided wizard
python models/downloader.py search "Qwen2.5 7B" # Search HuggingFace
python models/downloader.py suggest --ram 16     # RAM-based suggestion
python models/downloader.py local                # List downloaded models
```

---

### Script decision tree

```text
"I have a blank USB"
  -> python flash_usb.py

"I have a USB with carry-ai but want to update it"
  -> python onboard.py  (Step 2 offers update/overwrite/clean)
  -> or: python flash_usb.py  (re-flash with overwrite prompt)

"I just want to run carry-ai on this machine"
  -> python onboard.py   (first time)
  -> python launcher.py  (after setup)

"I need to add/change API keys"
  -> python crypto/keystore.py setup

"I need to download a model"
  -> python models/downloader.py interactive
  -> or: python flash_usb.py --add-models --target H:\

"I want to update packages on the USB"
  -> python onboard.py  (Step 2: Update deps, or Step 3: Update all)

"I want to update packages in the web UI"
  -> Settings panel -> Dependencies card -> Update All
```

---

## Three Modes

| Mode | How it works | Requirements |
|------|-------------|--------------|
| **Local** | Fully offline via llama.cpp + GGUF models | 3 GB+ RAM, llama-server binary |
| **API** | Cloud providers with auto-failover chain | Internet + at least one API key |
| **Hybrid** | Local first, transparent API fallback | Both of the above |

Mode is auto-detected from what's available, or forced with `--mode`.

---

## Features

- **Portable** — runs from USB, zero permanent install on the host machine
- **Offline-capable** — local GGUF inference via llama.cpp, 12 RAM tiers auto-selected
- **7 cloud providers** — Anthropic, OpenAI, Google, Groq, OpenRouter, G0DM0D3, Onyx
- **Automatic failover** — health-tracked provider chain with exponential backoff
- **Voice mode** — push-to-talk → AssemblyAI transcription → Claude vision → ElevenLabs TTS
- **30+ agent tools** — shell, files, web scraping, screenshots, clipboard, Google Workspace
- **Persistent memory** — SQLite + FTS5, survives sessions, dedup + relevance decay
- **MCP support** — connect any MCP server via stdio, HTTP, or SSE
- **Plugin system** — manifest-based tools, hooks, and lifecycle scripts
- **Encrypted keys** — Fernet + PBKDF2 (600k iterations), decrypted in RAM only
- **Nuclear cleanup** — 6-step trace wipe on USB eject (processes, tmpfs, clipboard, history)
- **Web UI** — Flask SPA at `localhost:8080`, dark/light theme, real-time streaming

---

## USB Self-Hosting

The USB drive is not just storage — it hosts its own runtime.

```
USB:/
├── carry-ai/              ← source code
├── python-env/
│   ├── windows/           ← portable Python interpreter + all packages
│   │   ├── python.exe     ← no install needed on Windows host
│   │   └── Lib/site-packages/
│   └── linux/
│       └── site-packages/ ← all packages, injected via PYTHONPATH
├── models/                ← GGUF model files
├── start.bat              ← Windows entry point
└── start.sh               ← Linux entry point
```

**How it works on each platform:**

| Platform | Python source | Package source | Requirement on host |
|----------|--------------|----------------|---------------------|
| Windows | `python-env/windows/python.exe` (bundled) | `python-env/windows/Lib/site-packages/` | Nothing |
| Linux | System `python3` | `python-env/linux/site-packages/` via `PYTHONPATH` | Python 3.10+ |

`start.sh` automatically sets `PYTHONPATH` before launching, so the host machine's site-packages are never touched. `start.bat` calls the bundled `python.exe` directly.

**Package caching:** `flash_usb.py` saves downloaded wheels in a local `.pkg-cache/` directory. The first flash downloads from PyPI; every subsequent flash installs from cache with no network needed. Run `flash_usb.py` and select "Clear package cache" to force a fresh download.

---

## Voice Mode

Push-to-talk AI with screen awareness — ported from [farzaa/clicky](https://github.com/farzaa/clicky) (Swift/macOS) to Python:

```
Hold Enter → speak → release Enter
      │
      ▼  AssemblyAI
  transcript text
      │
      ├──► screenshot (base64 PNG) ──► Claude vision
      │
      ▼  Claude response
  spoken aloud via ElevenLabs
```

**Setup** (the onboarding wizard does this for you):
```bash
pip install pyaudio assemblyai elevenlabs Pillow
```

**Keys needed:**
- [AssemblyAI](https://assemblyai.com) — free 5 hours/month
- [ElevenLabs](https://elevenlabs.io) — free 10,000 characters/month

**Enable in `config/settings.json`:**
```json
{
  "voice": {
    "enabled": true,
    "assemblyai_key": "your-key",
    "elevenlabs_key": "your-key",
    "vision_enabled": true
  }
}
```

All voice dependencies are optional — carry-ai runs fully without them. Implementation: [`integrations/voice_tools.py`](integrations/voice_tools.py).

---

## API Providers

Seven providers with automatic failover and per-session health tracking (success rate, latency, consecutive failures):

| Provider | Models | Auth |
|----------|--------|------|
| **Anthropic** | Claude Opus/Sonnet/Haiku 4.x | API key |
| **OpenAI** | GPT-4o, o4-mini, o3 | API key |
| **Google** | Gemini 2.5 Pro/Flash, 2.0 Flash | API key or OAuth |
| **Groq** | Llama 3.3 70B, Mixtral, Gemma2 | API key |
| **OpenRouter** | 13+ models (multi-provider) | API key |
| **G0DM0D3** | ULTRAPLINIAN racing, CONSORTIUM synthesis, AutoTune | API key |
| **Onyx** | RAG-enhanced (50+ data connectors) | API key + Onyx instance |

Manage keys:
```bash
python crypto/keystore.py setup    # Interactive wizard
python crypto/keystore.py list     # Show configured providers
python crypto/keystore.py add openai
python crypto/keystore.py remove groq
```

Keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using PBKDF2 (600,000 iterations). They live in `config/providers.enc` on the USB and are only decrypted into RAM at runtime.

See [`docs/providers.md`](docs/providers.md) for per-provider setup details.

### G0DM0D3 — Multi-Model Racing

Via [G0DM0D3](https://github.com/elder-plinius/G0DM0D3):
- **ULTRAPLINIAN** — race 10–51 models in parallel, score and pick the best response
- **CONSORTIUM** — collect all responses, synthesize a ground-truth answer
- **AutoTune** — auto-detect query context (code / creative / analytical) and optimize sampling

### Onyx — RAG Integration

Via [Onyx](https://github.com/onyx-dot-app/onyx):
- Answers grounded in your organization's data (Google Drive, Slack, Confluence, Notion, GitHub, etc.)
- 50+ connectors, deep research mode, also usable as an MCP server

---

## Local Models

The system auto-selects the best GGUF model for your available RAM (12 tiers):

| RAM | Model | Context | Features |
|-----|-------|---------|----------|
| 32 GB+ | Qwen3 30B Q6_K | 16384 | Tool calling |
| 24 GB+ | Llama 4 Scout 17B Q6_K | 16384 | Tool calling, multimodal |
| 20 GB+ | Qwen3 14B Q8_0 | 12288 | Tool calling |
| 16 GB+ | Gemma 4 12B Q4_K_M | 12288 | Tool calling, multimodal |
| 12 GB+ | Qwen3 14B Q4_K_M | 8192 | Tool calling |
| 10 GB+ | Qwen3 8B Q8_0 | 8192 | Tool calling |
| 8 GB+ | Qwen3 8B Q4_K_M | 8192 | Tool calling |
| 6 GB+ | Gemma 4 E4B Q4_K_M | 8192 | Multimodal |
| 5 GB+ | Qwen3.5 4B Q4_K_M | 4096 | Tool calling |
| 4 GB+ | Phi-4-mini Q4_K_M | 4096 | Tool calling |
| 3 GB+ | Gemma 4 E2B Q4_K_M | 4096 | — |
| < 3 GB | Gemma 3 1B Q4_K_M | 2048 | Emergency fallback |

**Download a model:**
```bash
python models/downloader.py interactive          # Guided wizard
python models/downloader.py search "Qwen3 8B"   # Search HuggingFace
python models/downloader.py suggest --ram 16     # Get RAM-based suggestion
python models/downloader.py local                # List downloaded models
```

Dual backend (huggingface_hub or pure requests), resume support, quantization-aware sorting.

---

## Agent & Tools

ReAct-pattern agent loop (Observe → Think → Act → Observe) with 30+ tools:

| Category | Tools |
|----------|-------|
| **System** | `shell`, `get_system_info`, `model_recommend` |
| **Files** | `read_file`, `write_file`, `edit_file`, `list_files`, `search_files` |
| **Screen** | `screenshot`, `click`, `type_text` |
| **Clipboard** | `clipboard_read`, `clipboard_write` |
| **Web** | `web_fetch`, `browse`, `scrape`, `scrape_stealth` (Scrapling-enhanced) |
| **Google** | `gdrive_list/upload`, `gmail_search/send/read`, `gsheets_read/append`, `gcalendar_agenda/create` |
| **Memory** | `memory_store`, `memory_search`, `memory_list` |
| **Voice** | `voice_listen`, `voice_speak` (when voice mode enabled) |

**Permission modes:**

| Mode | Behavior |
|------|----------|
| `ask` | Prompt before dangerous operations (default) |
| `yolo` | Execute all tools without confirmation |
| `safe` | Block dangerous patterns outright |

16 dangerous patterns detected: `rm -rf`, `format`, `DROP TABLE`, `git push --force`, etc.

**Slash commands:**
```
/status     — agent status (mode, model, turns, memory stats)
/clear      — clear conversation history
/tools      — list all available tools
/permission — set permission mode
/memory     — view/search/manage memories
/remember   — store a note in long-term memory
/forget     — remove a memory entry
/history    — recent conversation
/help       — all commands
```

### Scrapling Web Scraping

Via [Scrapling](https://github.com/D4Vinci/Scrapling):
- **Adaptive tracking** — auto-relocates elements after layout changes
- **Anti-bot bypass** — Cloudflare Turnstile, TLS fingerprint impersonation
- **Three tiers** — `Fetcher` (HTTP) → `StealthyFetcher` (headless + CF bypass) → `DynamicFetcher` (Playwright)
- Falls back to `requests.get()` if not installed

### Google Workspace

Via [GWS CLI](https://github.com/googleworkspace/cli): Drive, Gmail, Sheets, Calendar — auth once with `gws auth login`.

### Hardware-Aware Model Selection

Via [llmfit](https://github.com/AlexsJones/llmfit): 4-dimensional scoring (Quality / Speed / Fit / Context), GPU detection (NVIDIA, AMD, Intel, Apple Silicon), auto GPU offload computation. Falls back to the static 12-tier RAM table if not installed.

---

## Persistent Memory

SQLite + FTS5 long-term memory that survives USB ejects and reboots (inspired by [claude-mem](https://github.com/thedotmack/claude-mem)):

- **7 observation types** — fact, discovery, decision, bugfix, note, preference, summary
- **8 concept tags** — how-it-works, problem-solution, gotcha, pattern, trade-off, etc.
- **FTS5 full-text search** with LIKE fallback
- **SHA-256 deduplication** — 30-second window prevents duplicate writes
- **Relevance decay** — stale memories fade; frequently accessed ones persist
- **Context injection** — relevant memories injected into system prompt automatically
- **Privacy tags** — `<private>...</private>` stripped before storage
- **Export/import** — JSON backup with v1 backward-compatible import

---

## Desktop App

Native chat window that launches when you click `start.bat` / `start.sh`:

- **Claude-like chat interface** — dark theme, streaming responses, code blocks
- **Sidebar** — provider selector (Anthropic, OpenAI, Google, Groq, OpenRouter, Local), model picker
- **Zero config launch** — double-click start.bat on USB, chat window appears
- **Keyboard shortcuts** — Enter to send, Shift+Enter for newline, Escape to clear

Uses `customtkinter` for a modern look (falls back to plain `tkinter` if not installed). File: [`ui/desktop.py`](ui/desktop.py).

```bash
python ui/desktop.py       # direct launch
# or just double-click start.bat / bash start.sh on the USB
```

---

## Web UI

Chat interface at `http://localhost:8080`:

- **Sidebar panels** — Chat, Memory, Tools, Settings, Logs (all collapsible)
- **Real-time streaming** — SSE with collapsible tool execution cards
- **Memory browser** — search, view, and manage stored memories
- **Settings panel** — live mode switching, provider list, model inventory, dependency manager
- **Dependency manager** — view installed/missing packages with versions, one-click "Update All" button
- **Dark / light theme** — toggle with `Ctrl+K` to focus input
- **Markdown rendering** — code blocks, headings, lists, links

Embedded SPA (vanilla HTML/CSS/JS) — no build step, no node_modules. Inspired by [Skales](https://github.com/skalesapp/skales).

---

## MCP Integration

Connect external MCP servers for additional tools:

```json
{
  "mcp": {
    "servers": {
      "github": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-github"],
        "env": { "GITHUB_TOKEN": "$GITHUB_TOKEN" }
      },
      "web-search": {
        "transport": "sse",
        "url": "http://localhost:3001/sse"
      }
    }
  }
}
```

- **Transports** — stdio (subprocess), HTTP, SSE
- **Auto-discovery** — tools registered as `mcp__{server}__{tool}`
- **Thread-safe registry** — concurrent access from agent and UI threads
- **Env var resolution** — `$VAR` references expanded at connect time

---

## Plugin System

Extend carry-ai with manifest-based plugins:

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "permissions": ["read", "write"],
  "tools": [{ "name": "greet", "description": "Say hello", "command": "./greet.py" }],
  "hooks": { "pre_tool_use": ["./hooks/safety_check.py"] },
  "lifecycle": { "init": ["./setup.py"], "shutdown": ["./cleanup.py"] }
}
```

7 hook types: `pre_tool_use`, `post_tool_use`, `post_tool_use_failure`, `pre_chat`, `post_chat`, `on_startup`, `on_shutdown`. Plugin tools registered as `plugin__{name}__{tool}`.

---

## Cowork Features

**Session Sharing**
- JSON export (sanitized — API keys stripped, paths relativized)
- Markdown export — human-readable chat transcript
- Live sharing — temporary localhost URL for LAN access

**Team Management**
- Named teams with member lists
- Task tracking: pending / in_progress / completed / blocked (with priority)
- Cron scheduler: hourly / daily / weekly recurring prompts
- Persisted to `config/teams.json` on USB

---

## Configuration

Four-layer hierarchy (later overrides earlier):

1. Built-in defaults
2. `config/settings.json` on USB
3. Environment variables (`CARRY_AI_MODE`, `CARRY_AI_PORT`, `CARRY_AI_GPU_LAYERS`, etc.)
4. CLI flags (`--mode`, `--port`, etc.)

```python
from config.settings import load_settings
settings = load_settings(cli_overrides={"mode": "api", "port": 9090})
print(settings.agent.permission_mode)  # "ask"
print(settings.ui.theme)              # "dark"
```

See [`docs/configuration.md`](docs/configuration.md) for the full settings reference.

---

## Security & Cleanup

**Encryption** — API keys stored in `config/providers.enc` (Fernet AES-128-CBC + HMAC-SHA256, PBKDF2 600k iterations). Decrypted in RAM only. Never touch disk on the host machine.

**Injection** — session runs in tmpfs (`/tmp/ai_session/` on Linux) or `%TEMP%\ai_session\` on Windows. Nothing is permanently installed.

**Eject watcher** — WMI event-driven (Windows) or udev rule (Linux). On eject, a 6-step nuclear wipe fires automatically:

1. Kill process tree (psutil / taskkill / pkill)
2. Wipe session directory (tmpfs umount / shutil.rmtree)
3. Clear clipboard (ctypes / xclip / xsel / wl-copy)
4. Scrub recent files (recently-used.xbel, shell history)
5. Remove eject watchers (WMI watcher / udev rule)
6. Zero sensitive memory (ctypes.memset on key bytes)

---

## Packaging to USB

See the [Scripts Guide](#scripts-guide) for details on each script. Summary:

```bash
python flash_usb.py                              # Full interactive flash (recommended)
python flash_usb.py --add-models --target H:\    # Add models to existing USB
python package.py --target E:\                   # Package to USB drive
python package.py --target E:\ --include-models  # Bundle GGUF models
python package.py --validate-only --target .     # Validate structure only
```

Generated launchers: `autorun.inf`, `start.bat`, `start.sh`, `carry-ai.desktop`

---

## Project Structure

```text
carry-ai/
├── launcher.py              # Boot orchestrator — the main entry point
├── onboard.py               # 8-step interactive setup wizard
├── flash_usb.py             # Rufus-style USB flasher with package caching
├── setup_usb.py             # USB-side package installer (run from USB)
├── bootstrap.py             # sys.path injector — called by start scripts
├── package.py               # USB packaging and distribution
├── start.bat                # Windows launcher (uses bundled Python)
├── start.sh                 # Linux launcher (injects USB PYTHONPATH)
├── requirements.txt
├── .pkg-cache/              # (git-ignored) local wheel cache for fast re-flashing
│
├── docs/
│   ├── getting-started.md   # Zero-to-running guide
│   ├── configuration.md     # Full settings.json reference
│   ├── providers.md         # API key setup for all 9 providers
│   └── architecture.md      # System design and data-flow diagrams
│
├── modes/
│   ├── api_mode.py          # Cloud provider router + health tracking
│   └── local_mode.py        # llama.cpp subprocess + 12-tier model selection
│
├── providers/               # 7 LLM provider integrations
│   ├── base.py              # Abstract base + exception hierarchy
│   ├── openai_compat.py     # Shared OpenAI-compatible base
│   ├── anthropic_provider.py
│   ├── google_oauth.py
│   ├── openai_provider.py
│   ├── groq_provider.py
│   ├── openrouter_provider.py
│   ├── godmode_provider.py  # G0DM0D3 multi-model racing
│   └── onyx_provider.py     # RAG provider
│
├── agent/
│   ├── agent.py             # ReAct loop, permissions, conversation history
│   ├── tools.py             # 30+ tools + extensible registry
│   └── memory.py            # SQLite + FTS5 persistent memory
│
├── ui/
│   ├── app.py               # Flask SPA at localhost:8080
│   └── desktop.py           # Native desktop app (customtkinter/tkinter)
│
├── integrations/
│   ├── voice_tools.py       # Push-to-talk voice pipeline (clicky port)
│   ├── scrapling_tools.py   # Adaptive web scraping
│   ├── gworkspace_tools.py  # Google Workspace API
│   └── llmfit_advisor.py    # Hardware-aware model selection
│
├── mcp/                     # MCP client (stdio/HTTP/SSE)
├── plugins/                 # Plugin loader + manager
├── cowork/                  # Session sharing + team management
├── inject/                  # OS injection (Windows + Linux)
├── cleanup/                 # 6-step trace wiper
├── models/                  # HuggingFace GGUF downloader
├── crypto/                  # Fernet keystore (API key encryption)
└── config/                  # Settings loader + encrypted keys
```

---

## Referenced Projects

| Project | How It's Used | Module |
|---------|---------------|--------|
| [clicky](https://github.com/farzaa/clicky) | Push-to-talk voice pipeline (Python port) | `integrations/voice_tools.py` |
| [claude-mem](https://github.com/thedotmack/claude-mem) | Memory: FTS5, dedup, decay | `agent/memory.py` |
| [Scrapling](https://github.com/D4Vinci/Scrapling) | Adaptive web scraping + anti-bot bypass | `integrations/scrapling_tools.py` |
| [G0DM0D3](https://github.com/elder-plinius/G0DM0D3) | Multi-model racing, AutoTune, synthesis | `providers/godmode_provider.py` |
| [Onyx](https://github.com/onyx-dot-app/onyx) | RAG with 50+ data connectors | `providers/onyx_provider.py` |
| [llmfit](https://github.com/AlexsJones/llmfit) | Hardware-aware model selection | `integrations/llmfit_advisor.py` |
| [Google Workspace CLI](https://github.com/googleworkspace/cli) | Drive, Gmail, Sheets, Calendar | `integrations/gworkspace_tools.py` |
| [Skales](https://github.com/skalesapp/skales) | Web UI sidebar design | `ui/app.py` |

---

## Requirements

- Python 3.10+
- For local mode: [llama-server](https://github.com/ggerganov/llama.cpp) binary + a GGUF model file
- For API mode: at least one provider API key
- For voice mode: `pip install pyaudio assemblyai elevenlabs Pillow`

```bash
pip install -r requirements.txt        # Full install
pip install psutil cryptography flask requests  # Minimal (API mode only)
```

---

## License

MIT
