# carry-ai

> Plug in. Chat. Eject. Leave no trace.

A portable AI assistant that lives on a USB drive. Plug it into any Windows or Linux machine, run one command, and you have a full AI agent at `localhost:8080` — with local GGUF models, cloud API failover, voice mode, persistent memory, and 30+ tools. Eject the USB and everything it created is wiped.

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
- [Three Modes](#three-modes)
- [Features](#features)
- [Voice Mode](#voice-mode)
- [API Providers](#api-providers)
- [Local Models](#local-models)
- [Agent & Tools](#agent--tools)
- [Persistent Memory](#persistent-memory)
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

Run this on any machine that has Python installed:

```bash
python flash_usb.py
```

**Works like Rufus.** Detects your USB drives, you pick one, and it:
- Copies carry-ai onto the USB
- Downloads and installs all Python packages *onto the USB* (no pip install needed on target machines)
- Bundles a portable Python interpreter for **both** Windows and Linux hosts (python-build-standalone) plus llama.cpp (Vulkan GPU + CPU) — nothing to install on either OS
- Optionally downloads GGUF models and voice pipeline deps onto the USB

After flashing, the USB is fully self-contained:

```
Windows host  →  insert USB, double-click start.bat    (nothing to install)
Linux host    →  insert USB, bash /media/usb/start.sh  (nothing to install)
```

---

### Option 2 — Set up an existing USB (already has carry-ai)

If carry-ai is already on the USB and you want to bundle dependencies:

```bash
python setup_usb.py
```

---

### Option 3 — Manual / dev mode

```bash
python onboard.py      # interactive wizard: deps, keys, mode, voice, dry-run
python launcher.py     # direct launch (uses system Python packages)
```

**Launcher flags:**

```
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
- **5 cloud providers** — Anthropic, OpenAI, Google, Groq, OpenRouter (plus G0DM0D3 and Onyx behind the experimental flag)
- **Automatic failover** — health-tracked provider chain with exponential backoff
- **Voice mode** — push-to-talk; speech-to-text and text-to-speech offline (sherpa-onnx) or in the cloud (AssemblyAI / ElevenLabs), switchable
- **30+ agent tools** — shell, files, web scraping, screenshots, clipboard (Google Workspace behind the experimental flag)
- **Persistent memory** — SQLite + FTS5, survives sessions, dedup + relevance decay
- **MCP support** — connect any MCP server via stdio or Streamable HTTP (MCP 2026-07-28; legacy HTTP+SSE still works)
- **Plugin system** — manifest-based tools, hooks, and lifecycle scripts
- **Encrypted keys** — Fernet + PBKDF2 (600k iterations), decrypted in RAM only
- **Automatic cleanup** — 6-step wipe on exit or USB eject (processes, RAM session, browser profile, clipboard, recent files)
- **Web UI** — Flask SPA at `localhost:8080`, dark/light theme, real-time streaming

---

## USB Self-Hosting

The USB drive is not just storage — it hosts its own runtime.

```
USB:/
├── carry-ai/              ← source code
├── python-env/
│   ├── windows/           ← bundled Python interpreter + all packages
│   │   ├── python.exe     ← no install needed on Windows host
│   │   └── Lib/site-packages/
│   └── linux/
│       ├── python/        ← bundled Python interpreter (no install needed)
│       └── site-packages/ ← all packages, injected via PYTHONPATH
├── bin/llama/<os>-vulkan|cpu/  ← llama-server (GPU via any driver, + CPU)
├── models/                ← GGUF model files
├── start.bat              ← Windows entry point
└── start.sh               ← Linux entry point
```

**How it works on each platform:**

| Platform | Python source | Package source | Requirement on host |
|----------|--------------|----------------|---------------------|
| Windows | `python-env/windows/python.exe` (bundled) | `python-env/windows/Lib/site-packages/` | Nothing |
| Linux | `python-env/linux/python/` (bundled) | `python-env/linux/site-packages/` via `PYTHONPATH` | Nothing (host `python3` only if the bundle is absent) |

On a USB mounted `noexec` (common for Linux exFAT/FAT automounts), `start.sh` copies the bundled Python and packages into the RAM session directory and runs from there, so compiled extensions can load; the copy is wiped on eject with the rest of the session.

`start.sh` automatically sets `PYTHONPATH` before launching, so the host machine's site-packages are never touched. `start.bat` calls the bundled `python.exe` directly.

**Scripts:**

| Script | Purpose |
|--------|---------|
| `flash_usb.py` | Run on host — detects drives, copies carry-ai, downloads packages + models |
| `setup_usb.py` | Run from USB — sets up / updates the `python-env/` on an existing USB |
| `bootstrap.py` | Called by start scripts — injects USB packages into `sys.path` |

---

## Voice Mode

Push-to-talk, ported from [farzaa/clicky](https://github.com/farzaa/clicky): click **🎤** next to the message box (or press **Ctrl+M**), speak, click **■**. What you said is sent to the agent, and replies can be read aloud.

Each direction can run **offline** (on this PC, nothing leaves it) or in the **cloud**, switchable in the app's **Voice** settings (Auto / Offline / Cloud / Off):

| | Offline (sherpa-onnx, models on the USB) | Cloud (needs a key + internet) |
|---|---|---|
| Speech → text | Moonshine v2 tiny (44 MB) or base (141 MB), English | AssemblyAI |
| Text → speech | KittenTTS nano (42 MB) or Kokoro (215 MB, multi-lingual) | ElevenLabs |

**Auto** (the default) uses the offline model when it's downloaded, otherwise the cloud service if you've added its key. Models are downloaded from the Voice settings, `onboard.py`, the flasher, or `python models/voice.py get moonshine-tiny-en`. They're pinned to a Hugging Face commit and checksum-verified, and stored in `models/voice/`.

- **Recording:** PyAudio on Windows (bundled). On Linux it uses sherpa-onnx's own ALSA reader, so no PortAudio is needed.
- **Playback:** PyAudio, or Windows' built-in `winsound` / Linux `aplay` (or `paplay`). Audio is played from memory and never written to the host's disk.
- **Offline transcription** reads audio straight from memory. Cloud transcription writes a WAV to the session folder and deletes it right after upload.
- **Cloud keys** (AssemblyAI, ElevenLabs) go into the encrypted keystore, never `settings.json`.

Implementation: [`integrations/voice_tools.py`](integrations/voice_tools.py), [`ui/voice_ui.py`](ui/voice_ui.py), [`models/voice.py`](models/voice.py).

---

## API Providers

Ten cloud providers with automatic failover and per-session health tracking (success rate, latency, consecutive failures). Add keys in the desktop app with **API Keys** (or in `onboard.py`): each provider has a **Get key** button (QR code for your phone, or a throwaway private window) and a **Test** button. Keys stay in RAM unless you choose **Save & remember** (encrypted into `providers.enc` on the USB). Keys already in the environment (`OPENROUTER_API_KEY`, `GROQ_API_KEY`, …) are offered as "(detected)".

**Free to start — no credit card:**

| Provider | Good free models | Free limits (Oct 2026, approx.) |
|----------|------------------|------------------|
| **OpenRouter** | `openrouter/free` (auto-picks a free model with tool support), Gemma 4 31B, Nemotron 3 Super | ~50 req/day (1000/day after a one-time $10 top-up) |
| **Groq** | gpt-oss 20B / 120B, Llama 3.3 70B | ~1000 req/day per model |
| **Google Gemini** | Gemini 3.5 Flash-Lite, 3.8 Flash | a few hundred req/day on Flash-Lite |
| **Cerebras** | gpt-oss 120B | ~1M tokens/day |
| **Mistral** | Mistral Small / Medium | "Experiment" plan (phone verification, prompts used for training) |
| **NVIDIA NIM** | gpt-oss 20B, Nemotron 3 Super | ~40 req/min |

**Paid:** Anthropic (Claude Opus 5 / Sonnet 5 / Haiku 4.5), OpenAI (GPT-6), DeepSeek, xAI (Grok).
**Experimental:** G0DM0D3 ⚑ (multi-model racing), Onyx ⚑ (RAG over 50+ connectors).

The list lives in `providers/catalog.py`; the providers there that speak the plain OpenAI API share `providers/generic_provider.py`.

⚑ Experimental — off by default (needs an extra self-hosted service). Enable with `{"experimental": {"godmode": true}}` / `{"onyx": true}` in `config/settings.json`.

Manage keys:
```bash
python crypto/keystore.py setup    # Interactive wizard
python crypto/keystore.py list     # Show configured providers
python crypto/keystore.py add openai
python crypto/keystore.py remove groq
```

Keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using PBKDF2 (600,000 iterations). They live in `config/providers.enc` on the USB and are only decrypted into RAM at runtime.

See [`docs/providers.md`](docs/providers.md) for per-provider setup details.

### G0DM0D3 — Multi-Model Racing (experimental)

Via [G0DM0D3](https://github.com/elder-plinius/G0DM0D3):
- **ULTRAPLINIAN** — race 10–51 models in parallel, score and pick the best response
- **CONSORTIUM** — collect all responses, synthesize a ground-truth answer
- **AutoTune** — auto-detect query context (code / creative / analytical) and optimize sampling

### Onyx — RAG Integration (experimental)

Via [Onyx](https://github.com/onyx-dot-app/onyx):
- Answers grounded in your organization's data (Google Drive, Slack, Confluence, Notion, GitHub, etc.)
- 50+ connectors, deep research mode, also usable as an MCP server

---

## Local Models

The system auto-selects the best GGUF model for your **available** RAM. The list lives in `models/catalog.py` (shared by the boot selector, the USB flasher and the Model Manager); every repo was verified on Hugging Face in Sept 2026 and none needs a token. Mixture-of-experts models with few active parameters are used at the top end because they stay fast on CPU-only machines.

| Available RAM | Model | Size | Vision |
|-----|-------|------|--------|
| 40 GB+ | Qwen3.6 35B-A3B Q8_0 (MoE, 3B active) | 36.9 GB | ✓ |
| 23 GB+ | Qwen3.6 35B-A3B Q4_K_M (MoE, 3B active) | 20.4 GB | ✓ |
| 17 GB+ | Gemma 4 26B-A4B QAT (MoE, 4B active) | 14.3 GB | ✓ |
| 14 GB+ | gpt-oss 20B MXFP4 (MoE, 3.6B active) | 12.1 GB | — |
| 9 GB+ | Gemma 4 12B Q4_K_M | 7.1 GB | ✓ |
| 7 GB+ | Qwen3.5 9B Q4_K_M | 5.7 GB | ✓ |
| 5.5 GB+ | Gemma 4 E4B QAT | 4.2 GB | ✓ |
| 3.5 GB+ | Qwen3.5 4B Q4_K_M | 2.7 GB | ✓ |
| any | Qwen3.5 2B Q4_K_M | 1.3 GB | ✓ |

All tiers support tool calling (`llama-server --jinja`). Vision models get their projector saved as `<model>.mmproj.gguf` next to the model and loaded with `--mmproj` automatically. Files over 4 GB need an exFAT/NTFS stick (not FAT32).

**Download a model:**
```bash
python models/downloader.py interactive          # Guided wizard
python models/downloader.py search "Qwen3 8B"   # Search HuggingFace
python models/downloader.py suggest --ram 16     # Get RAM-based suggestion
python models/downloader.py local                # List downloaded models
```

Dual backend (huggingface_hub or pure requests), resume support, quantization-aware sorting.

**In the desktop app** the Model Manager labels every file with whether it fits this PC (free RAM + dedicated VRAM, including the KV cache) and warns before downloading one that doesn't.

**Gated models (Gemma, Llama…)** — click **Sign in with Hugging Face** in the Model Manager. It shows a short code and a QR code; approve it on your phone and the token lands in the app (optionally saved, encrypted, in `providers.enc`). No browser opens on the host PC. If a model still needs its licence accepted, the app shows the model page as a QR code to agree on your phone, then **Check again** starts the download. This needs a one-time public OAuth app — set its id in `huggingface.oauth_client_id` (see [docs/configuration.md](docs/configuration.md)). Pasting a token still works without it.

---

## Agent & Tools

ReAct-pattern agent loop (Observe → Think → Act → Observe) with 30+ tools:

| Category | Tools |
|----------|-------|
| **System** | `shell`, `get_system_info`, `model_recommend` |
| **Code** | `run_python` — sandboxed Python ([Monty](https://github.com/pydantic/monty)): no network or processes; `/work` scratch folder in the session dir; with host access on, your Desktop/Documents/Downloads are readable at `/host`; 10 s / 256 MB limits; variables persist between calls |

**Sandbox switch** — the **Access** button in the chat window (also in the web UI's Settings, or `/sandbox on|off` in the terminal) chooses what the AI may touch:

- **🔒 Sandboxed:** only tools that can't reach this PC — `run_python` on `/work`, web fetch, memory.
- **🔓 Host access:** files, shell, screen and clipboard as well, with the usual permission prompts.

Set the starting state with `sandbox.start_sandboxed`.
| **Files** | `read_file`, `write_file`, `edit_file`, `list_files`, `search_files` |
| **Screen** | `screenshot`, `click`, `type_text` |
| **Clipboard** | `clipboard_read`, `clipboard_write` |
| **Web** | `web_fetch`, `browse`, `scrape`, `scrape_stealth` (Scrapling-enhanced) |
| **Google** | `gdrive_list/upload`, `gmail_search/send/read`, `gsheets_read/append`, `gcalendar_agenda/create` |
| **Memory** | `memory_store`, `memory_search`, `memory_list` |

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

### Google Workspace (experimental)

> Off by default — needs the `gws` CLI installed on the host, which breaks the no-install model. Enable with `{"experimental": {"google_workspace": true}}` in `config/settings.json`.

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

- **Same engine as the web UI** — the desktop app drives the full ReAct agent, so it has tools, local GGUF **and** API routing, persistent memory, and the permission policy (dangerous tool calls pop a confirm dialog). It is no longer a provider-only chat box.
- **Claude-like chat interface** — dark theme, streaming responses, code blocks, inline tool-activity lines
- **Sidebar** — provider selector (Anthropic, OpenAI, Google, Groq, OpenRouter, Local), model picker; in API/hybrid mode these steer which provider and model the agent tries first
- **Zero config launch** — double-click start.bat on USB, chat window appears
- **Keyboard shortcuts** — Enter to send, Shift+Enter for newline, Escape to clear

Launched by `launcher.py` (so it gets the booted agent, eject watcher and wipe); falls back to the web UI when `tkinter` or a display is unavailable. Uses `customtkinter` for a modern look (falls back to plain `tkinter`). File: [`ui/desktop.py`](ui/desktop.py).

```bash
bash start.sh              # or double-click start.bat on the USB
python launcher.py --ui desktop   # explicit
```

---

## Web UI

Chat interface at `http://localhost:8080`:

- **Sidebar panels** — Chat, Memory, Tools, Settings, Logs (all collapsible)
- **Real-time streaming** — SSE with collapsible tool execution cards
- **Memory browser** — search, view, and manage stored memories
- **Settings panel** — live mode switching, provider list, model inventory
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
        "transport": "streamable-http",
        "url": "http://localhost:3001/mcp"
      }
    }
  }
}
```

- **Transports** — stdio (subprocess) and Streamable HTTP (MCP 2026-07-28: stateless, no handshake, `MCP-Protocol-Version`/`Mcp-Method`/`Mcp-Name` headers). A `"transport": "sse"` entry is accepted but treated as Streamable HTTP, and the client falls back to the legacy `initialize` handshake for 2025-era servers.
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

## Cowork Features (experimental)

> Off by default — team session sharing doesn't fit a disposable, anonymous USB session. Enable with `{"experimental": {"cowork": true}}` in `config/settings.json`.

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

**One lifecycle for every start path** — `start.bat` / `start.sh` always boot through `launcher.py`, whichever UI you end up in, so session setup, eject detection and the wipe always run.

**Session in RAM where possible** — Linux uses `/dev/shm/ai_session` (RAM-backed, no root needed); Windows uses `%TEMP%\ai_session\`. Your conversation history (`memory.db`) and encrypted keys stay on the USB and are never copied to the host.

**Trace-free UI** — the web UI opens in a separate app window with a throwaway browser profile inside the session dir (never your everyday browser profile), and is locked to the session with a one-time token plus a localhost-only Host check.

**Eject detection** — a portable poller notices within ~2 s when the drive disappears (plus WMI events on Windows). The wipe code is loaded at boot, so it still runs after the USB is gone:

1. Kill processes carry-ai started (its own children and inference servers on the USB)
2. Wipe the session directory (incl. the throwaway browser profile)
3. Clear the clipboard
4. Scrub carry-ai entries from recent files
5. Remove eject watchers
6. Drop decrypted keys from memory

### What "no trace" covers — and what it can't

carry-ai removes **everything it creates in user space**: session files, browser profile, clipboard, its recent-file entries. Without administrator rights **no program can erase the records Windows/Linux keep on their own** that a program ran and a USB drive was attached — e.g. Prefetch, Amcache/ShimCache, BAM, USBSTOR/MountedDevices and `setupapi.dev.log` on Windows, or journald/udisks logs on Linux — nor data the OS paged to `pagefile.sys`/swap. Think of it as *"leaves no user-visible trace and none of your data"*, not forensic invisibility.

---

## Packaging to USB

```bash
python package.py --target E:\                   # Package to USB drive
python package.py --target ./test --verbose       # Test locally
python package.py --target E:\ --include-models   # Bundle GGUF models
python package.py --target E:\ --strip-models     # API-only package
python package.py --validate-only --target .       # Validate only
```

Generated launchers: `autorun.inf`, `start.bat`, `start.sh`, `carry-ai.desktop`

---

## Project Structure

```
carry-ai/
├── launcher.py              # Entry point: OS detect, RAM probe, boot
├── onboard.py               # Interactive setup wizard
├── flash_usb.py             # Rufus-style USB flasher (run on host machine)
├── setup_usb.py             # USB package env setup (run from USB)
├── bootstrap.py             # sys.path patcher — injects USB packages
├── start.bat                # Windows launcher (uses USB-local Python)
├── start.sh                 # Linux launcher (injects USB PYTHONPATH)
├── package.py               # USB packaging
├── requirements.txt

├── docs/
│   ├── getting-started.md   # Zero-to-running guide
│   ├── configuration.md     # Full settings.json reference
│   ├── providers.md         # API key setup for all 9 providers
│   └── architecture.md      # System design & data-flow diagrams

├── modes/
│   ├── api_mode.py          # Cloud provider router + health tracking
│   └── local_mode.py        # llama.cpp subprocess + 12-tier model selection

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

├── agent/
│   ├── agent.py             # ReAct loop, permissions, conversation history
│   ├── tools.py             # 30+ tools + extensible registry
│   └── memory.py            # SQLite + FTS5 persistent memory

├── ui/
│   └── app.py               # Flask SPA at localhost:8080

├── integrations/
│   ├── voice_tools.py       # Push-to-talk voice: offline (sherpa-onnx) or cloud STT/TTS
│   ├── scrapling_tools.py   # Adaptive web scraping
│   ├── gworkspace_tools.py  # Google Workspace API
│   ├── llmfit_advisor.py    # Hardware-aware model selection
│   └── python_sandbox.py    # run_python tool (pydantic-monty sandbox)

├── mcp/                     # MCP client (stdio/HTTP/SSE)
├── plugins/                 # Plugin loader + manager
├── cowork/                  # Session sharing + team management
├── inject/                  # OS injection (Windows + Linux)
├── cleanup/                 # 6-step trace wiper
├── models/                  # HuggingFace GGUF downloader
├── crypto/                  # Fernet keystore
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
- For voice mode: `pip install sherpa-onnx` (+ `pyaudio` on Windows), then download the voice models in the app

```bash
pip install -r requirements.txt        # Full install
pip install psutil cryptography flask requests  # Minimal (API mode only)
```

---

## License

MIT
