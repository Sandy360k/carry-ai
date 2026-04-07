# carry-ai -- Carry Your AI on a USB Stick

Portable AI assistant that injects into any Windows or Linux machine from a USB drive.
Runs entirely from RAM, never permanently installs, and wipes all traces on eject.

## Quick Start

**Windows:** Double-click `start.bat` or run:
```bash
python carry-ai/launcher.py
```

**Linux:** Run from terminal:
```bash
bash start.sh
# or
python3 carry-ai/launcher.py
```

**Options:**
```
python launcher.py                  # Auto-detect everything and boot
python launcher.py --dry-run        # Test without USB hardware
python launcher.py --mode api       # Force API mode
python launcher.py --mode local     # Force local mode (needs GGUF model)
python launcher.py --mode hybrid    # Local first, API fallback
python launcher.py --port 9090      # Custom web UI port
python launcher.py --no-ui          # CLI-only, no web server
python launcher.py --download-model # Download GGUF model from HuggingFace
python launcher.py --verbose        # Debug logging
```

## Three Modes

| Mode | How it works | Requirements |
|------|-------------|-------------|
| **Local** | Offline via llama.cpp + GGUF models | 3GB+ RAM, llama-server binary |
| **API** | Cloud providers with auto-failover | Internet + at least one API key |
| **Hybrid** | Local first, API fallback on failure | Both of the above |

## Model Tiers (Local Mode)

The system auto-selects the best model your RAM can handle (12 tiers):

| RAM | Model | Context | Features |
|-----|-------|---------|----------|
| 32GB+ | Qwen3 30B Q6_K | 16384 | Tool calling |
| 24GB+ | Llama 4 Scout 17B Q6_K | 16384 | Tool calling, multimodal |
| 20GB+ | Qwen3 14B Q8_0 | 12288 | Tool calling |
| 16GB+ | Gemma 4 12B Q4_K_M | 12288 | Tool calling, multimodal |
| 12GB+ | Qwen3 14B Q4_K_M | 8192 | Tool calling |
| 10GB+ | Qwen3 8B Q8_0 | 8192 | Tool calling |
| 8GB+ | Qwen3 8B Q4_K_M | 8192 | Tool calling |
| 6GB+ | Gemma 4 E4B Q4_K_M | 8192 | Multimodal |
| 5GB+ | Qwen3.5 4B Q4_K_M | 4096 | Tool calling |
| 4GB+ | Phi-4-mini Q4_K_M | 4096 | Tool calling |
| 3GB+ | Gemma 4 E2B Q4_K_M | 4096 | |
| <3GB | Gemma 3 1B Q4_K_M | 2048 | Emergency fallback |

Quantization-aware matching ensures Q8 vs Q4 files map to the correct tier.

## HuggingFace Model Downloader

Browse, search, and download GGUF models directly onto the USB:

```bash
python models/downloader.py interactive          # Interactive wizard
python models/downloader.py search "Qwen3 8B"    # Search HuggingFace
python models/downloader.py list bartowski/Qwen3-8B-GGUF  # List quants
python models/downloader.py suggest --ram 16      # RAM-based suggestions
python models/downloader.py local                 # List downloaded models
```

Features: dual backend (huggingface_hub or pure requests), resume support,
quantization-level sorting, RAM-based suggestions for all 12 tiers.

## API Providers

Seven providers with automatic failover chain and health tracking:

| Provider | Models | Auth | Reference |
|----------|--------|------|-----------|
| **Anthropic** | Claude Opus/Sonnet/Haiku 4 | API key | |
| **Google** | Gemini 2.5 Pro/Flash, 2.0 Flash | API key or OAuth | |
| **OpenAI** | GPT-4o, o4-mini, o3 | API key | |
| **Groq** | Llama 3.3 70B, Mixtral, Gemma2 | API key | |
| **OpenRouter** | 13+ models (multi-provider) | API key | |
| **G0DM0D3** | ULTRAPLINIAN racing, CONSORTIUM synthesis, AutoTune | API key | [elder-plinius/G0DM0D3](https://github.com/elder-plinius/G0DM0D3) |
| **Onyx** | RAG-enhanced (50+ data connectors) | API key | [onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) |

Per-provider health tracking: success rate, rolling latency, rate limit detection.
Automatic retry with exponential backoff and full fallback chain.

### G0DM0D3 Integration

Multi-model racing and synthesis via [G0DM0D3](https://github.com/elder-plinius/G0DM0D3):
- **ULTRAPLINIAN**: Race 10-51 models in parallel, score and pick the best response
- **CONSORTIUM**: Collect all responses, synthesize ground truth via orchestrator
- **AutoTune**: Auto-detect query context (code, creative, analytical) and optimize sampling
- **STM Modules**: Post-process output (hedge_reducer, direct_mode, concise_mode)

### Onyx RAG Integration

Connect to [Onyx](https://github.com/onyx-dot-app/onyx) for retrieval-augmented generation:
- Route queries through Onyx for answers grounded in your organization's data
- 50+ data connectors: Google Drive, Slack, Confluence, Notion, GitHub, etc.
- Deep research mode for multi-step investigation
- Also usable as MCP server for tool-level access to connectors

## Encrypted Key Storage

API keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a
passphrase-derived key (PBKDF2, 600K iterations). Keys are only decrypted in RAM.

```bash
python crypto/keystore.py setup        # Interactive key setup wizard
python crypto/keystore.py list         # List configured providers
python crypto/keystore.py add openai   # Add a single key
python crypto/keystore.py remove groq  # Remove a provider
```

## How Injection Works

**Windows:**
- Extracts to `%TEMP%\ai_session\` with runtime/cache/logs subdirs
- WMI watcher (preferred, event-driven) or ctypes polling fallback (2s interval)
- On eject: kills process tree, wipes session dir, clears clipboard, zeros memory

**Linux:**
- Mounts tmpfs at `/tmp/ai_session/` (never touches host disk)
- udev rule (3 tiers: vendor:product, kernel device, generic USB) triggers cleanup
- Wipes tmpfs, removes udev rule, scrubs recently-used.xbel, clears clipboard

## Agent & Tools

ReAct-pattern agent (Observe -> Think -> Act -> Observe) with 30+ tools across 8 categories:

| Category | Tools | Source |
|----------|-------|--------|
| **System** | `shell`, `get_system_info`, `model_recommend` | built-in + [llmfit](https://github.com/AlexsJones/llmfit) |
| **Files** | `read_file`, `write_file`, `edit_file`, `list_files`, `search_files` | built-in |
| **Screen** | `screenshot`, `click`, `type_text` | built-in |
| **Clipboard** | `clipboard_read`, `clipboard_write` | built-in |
| **Web** | `web_fetch` (Scrapling-enhanced), `browse`, `scrape`, `scrape_stealth` | built-in + [Scrapling](https://github.com/D4Vinci/Scrapling) |
| **Google** | `gdrive_list`, `gdrive_upload`, `gmail_search`, `gmail_send`, `gmail_read`, `gsheets_read`, `gsheets_append`, `gcalendar_agenda`, `gcalendar_create`, `gworkspace` | [googleworkspace/cli](https://github.com/googleworkspace/cli) |
| **Memory** | `memory_store`, `memory_search`, `memory_list` | built-in + [claude-mem](https://github.com/thedotmack/claude-mem) |

Three permission modes: `ask` (confirm dangerous ops), `yolo` (allow all), `safe` (block dangerous).
16 dangerous command patterns detected (rm -rf, format, drop table, git push --force, etc.).

### Scrapling Web Scraping

Enhanced web fetching via [Scrapling](https://github.com/D4Vinci/Scrapling):
- **Adaptive element tracking**: auto-relocates elements after site layout changes
- **Anti-bot bypass**: Cloudflare Turnstile, TLS fingerprint impersonation
- **Three tiers**: Fetcher (HTTP), StealthyFetcher (headless+CF bypass), DynamicFetcher (Playwright)
- **Rich selectors**: CSS (with `::text`, `::attr(href)`), XPath, regex
- Falls back to `requests.get()` if Scrapling not installed

### Google Workspace CLI

Access Google Workspace via [GWS CLI](https://github.com/googleworkspace/cli):
- **Drive**: list, upload, download, share files
- **Gmail**: search, send, read emails
- **Sheets**: read, write, append spreadsheet data
- **Calendar**: view agenda, create events
- **Generic**: any Workspace API via `gworkspace` tool
- Auth: `gws auth login` once, or service account

### Hardware-Aware Model Selection

Powered by [llmfit](https://github.com/AlexsJones/llmfit):
- **4-dimensional scoring**: Quality, Speed, Fit, Context (each 0-100)
- **GPU detection**: NVIDIA, AMD, Intel, Apple Silicon with VRAM profiling
- **Auto GPU offload**: computes optimal `n_gpu_layers` for hybrid inference
- **Speed estimation**: memory bandwidth-based tokens/sec prediction
- Falls back to carry-ai's static 12-tier RAM matching if llmfit not installed

### Agent Slash Commands

```
/status     -- Show agent status (mode, model, turns, memory stats)
/clear      -- Clear conversation history
/tools      -- List all available tools
/permission -- Set permission mode (permissive/ask/strict)
/history    -- Show recent conversation
/remember   -- Store a note in long-term memory
/memory     -- View/search/manage memories
/forget     -- Remove a memory entry
/help       -- Show all commands
```

## Persistent Memory

SQLite-backed long-term memory that survives across sessions (inspired by
[claude-mem](https://github.com/thedotmack/claude-mem)):

- **7 observation types**: fact, discovery, decision, bugfix, note, preference, summary
- **8 concept tags**: how-it-works, problem-solution, gotcha, pattern, trade-off, etc.
- **FTS5 full-text search** with LIKE fallback
- **SHA-256 content-hash deduplication** (30s window)
- **Structured session summaries**: request, investigated, learned, completed, next_steps
- **Privacy tags**: `<private>...</private>` stripped before storage
- **Relevance decay**: older unused memories fade, frequently accessed ones persist
- **Progressive context injection**: facts, project context, session summaries,
  discoveries, notes, and query-relevant hits injected into system prompt
- **Per-project tracking**: working dir, tech stack, recent files
- **Export/import**: JSON backup with v1 backward-compatible import

The agent proactively stores observations via `memory_store` tool and retrieves
context via `memory_search`. Memory is auto-saved on shutdown with session summary.

## MCP Integration

Connect to external MCP (Model Context Protocol) servers for additional tools:

```json
// In config/settings.json
{
  "mcp": {
    "servers": {
      "github": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-github"],
        "env": {"GITHUB_TOKEN": "$GITHUB_TOKEN"}
      },
      "web-search": {
        "transport": "sse",
        "url": "http://localhost:3001/sse"
      }
    }
  }
}
```

- **Transports**: stdio (subprocess), HTTP, SSE
- **Auto-discovery**: tools/list on connect, registered as `mcp__{server}__{tool}`
- **JSON-RPC**: Full protocol with initialize handshake, tool calls, retries
- **Thread-safe registry**: concurrent access from agent + UI threads
- **Env var resolution**: `$VAR` references expanded from environment

## Plugin System

Extensible plugin architecture with manifest-based tools and hooks:

```json
// plugins/my-plugin/plugin.json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "Does something useful",
  "permissions": ["read", "write"],
  "tools": [{"name": "greet", "description": "Say hello", "command": "./greet.py"}],
  "hooks": {"pre_tool_use": ["./hooks/safety_check.py"]},
  "lifecycle": {"init": ["./setup.py"], "shutdown": ["./cleanup.py"]}
}
```

- **7 hook types**: pre_tool_use, post_tool_use, post_tool_use_failure,
  pre_chat, post_chat, on_startup, on_shutdown
- **Plugin manager**: install, enable, disable, uninstall with persistent state
- **Permission model**: read, write, execute, network, clipboard
- **Discovery**: scans bundled + user-installed directories
- **Tool registration**: plugin tools available as `plugin__{name}__{tool}`

## Cowork Features

### Session Sharing
- **JSON export**: sanitized (API keys stripped, paths relativized, system messages removed)
- **Markdown export**: human-readable chat transcript
- **Live sharing**: temporary localhost URL for LAN viewing
- **Import**: round-trip fidelity from JSON exports

### Team Management
- Create teams with named members
- Task tracking: pending, in_progress, completed, blocked (with priority)
- Simple cron scheduler: hourly, daily, weekly recurring prompts
- All data persisted to USB in config/teams.json

## Configuration

Hierarchical config from 4 sources (later overrides earlier):

1. Built-in defaults
2. `config/settings.json` on USB
3. Environment variables (`CARRY_AI_MODE`, `CARRY_AI_PORT`, etc.)
4. CLI flags (`--mode`, `--port`, etc.)

Dot-access settings object with validation:
```python
from config.settings import load_settings
settings = load_settings(cli_overrides={"mode": "api", "port": 9090})
print(settings.agent.permission_mode)  # "ask"
print(settings.ui.theme)              # "dark"
```

## Web UI

Chat interface at `http://localhost:8080` inspired by [Skales](https://github.com/skalesapp/skales):

- **Sidebar navigation**: Chat, Memory, Tools, Settings, Logs panels (collapsible)
- **Real-time streaming**: SSE with collapsible tool execution cards
- **Memory browser**: search, view, and manage persistent memories
- **Tools panel**: categorized tool registry with descriptions and parameters
- **Settings panel**: live mode switching, provider list, model inventory
- **Activity log**: timestamped event log for debugging
- **Dark/light theme** with keyboard shortcut (Ctrl+K to focus input)
- **Enhanced markdown**: code blocks with language labels, headings, lists, links
- Memory API (`/api/memory`, `/api/memory/search`, `/api/memory/add`)
- Status monitoring (`/api/status`, `/api/tools`, `/api/models`)

Embedded single-page app (vanilla HTML/CSS/JS) -- no build step, no node_modules.
UI architecture inspired by Skales' app-shell + sidebar pattern.

## Packaging to USB

```bash
python package.py --target E:\                    # Package to USB drive
python package.py --target ./test --verbose        # Test locally
python package.py --target E:\ --include-models    # Bundle GGUF models too
python package.py --target E:\ --strip-models      # API-only package
python package.py --validate-only --target .        # Check components only
```

Generated launchers: `autorun.inf`, `start.bat`, `start.sh`, `carry-ai.desktop`

## Cleanup (Nuclear Wipe)

6-step trace removal on USB eject:

1. **Kill processes**: psutil tree kill (children first) or taskkill/pkill fallback
2. **Wipe session directory**: tmpfs umount on Linux, shutil.rmtree on Windows
3. **Clear clipboard**: ctypes EmptyClipboard / xclip / xsel / wl-copy
4. **Scrub recent files**: recently-used.xbel, shell history entries
5. **Remove eject watchers**: WMI watcher / udev rule
6. **Zero sensitive memory**: ctypes.memset on CPython string/bytes internals

## Project Structure

```
carry-ai/
  launcher.py                # Entry point: OS detect, RAM probe, boot sequence
  package.py                 # USB packaging: validate, copy, generate launchers
  requirements.txt           # Python dependencies

  modes/
    local_mode.py            # llama.cpp subprocess, 12-tier model auto-selector
    api_mode.py              # Provider router with failover + health tracking

  providers/
    base.py                  # ABC, exception hierarchy, ChatResponse dataclass
    openai_compat.py         # Shared OpenAI-compatible base (chat + SSE stream)
    anthropic_provider.py    # Anthropic Messages API (content blocks format)
    google_oauth.py          # Gemini generateContent API (parts format)
    openai_provider.py       # OpenAI GPT models
    groq_provider.py         # Groq (OpenAI-compatible, fast inference)
    openrouter_provider.py   # OpenRouter (multi-provider aggregator)
    godmode_provider.py      # G0DM0D3 multi-model racing + AutoTune
    onyx_provider.py         # Onyx RAG-enhanced provider (50+ connectors)

  agent/
    agent.py                 # ReAct loop, conversation history, permissions
    tools.py                 # 14 built-in tools + extensible registry
    memory.py                # SQLite + FTS5 persistent memory (v2)

  inject/
    inject_windows.py        # %TEMP% session, WMI/ctypes eject watcher
    inject_linux.sh          # tmpfs mount, udev rule, cleanup on eject

  cleanup/
    cleanup.py               # 6-step nuclear wipe on eject

  ui/
    app.py                   # Flask web UI (embedded SPA, 12 API routes)

  models/
    downloader.py            # HuggingFace GGUF browser/downloader

  mcp/
    client.py                # MCP client: stdio + HTTP transports, McpManager
    registry.py              # Thread-safe global MCP tool registry
    config.py                # MCP server config loader + env var resolution

  plugins/
    loader.py                # Plugin discovery, manifest parsing, hook registry
    manager.py               # Install/enable/disable/uninstall + state persistence

  cowork/
    session_sharing.py       # Session export (JSON/Markdown), sharing, import
    teams.py                 # Team management, task tracking, cron scheduler

  integrations/
    scrapling_tools.py       # Scrapling web scraping tools (D4Vinci/Scrapling)
    gworkspace_tools.py      # Google Workspace CLI tools (googleworkspace/cli)
    llmfit_advisor.py        # Hardware-aware model selection (AlexsJones/llmfit)

  config/
    providers.enc            # Encrypted API keys (Fernet)
    settings.py              # Hierarchical config: defaults + JSON + env + CLI

  crypto/
    keystore.py              # Fernet encryption, PBKDF2 key derivation, secure wipe
```

## Referenced Projects

carry-ai integrates ideas and tools from these open-source projects:

| Project | How It's Used | Module |
|---------|---------------|--------|
| [claude-mem](https://github.com/thedotmack/claude-mem) | Memory architecture: FTS5, dedup, decay | `agent/memory.py` |
| [Skales](https://github.com/skalesapp/skales) | UI design: sidebar, panels, tool cards | `ui/app.py` |
| [Scrapling](https://github.com/D4Vinci/Scrapling) | Adaptive web scraping with anti-bot bypass | `integrations/scrapling_tools.py` |
| [G0DM0D3](https://github.com/elder-plinius/G0DM0D3) | Multi-model racing, AutoTune, synthesis | `providers/godmode_provider.py` |
| [Onyx](https://github.com/onyx-dot-app/onyx) | RAG provider with 50+ data connectors | `providers/onyx_provider.py` |
| [Google Workspace CLI](https://github.com/googleworkspace/cli) | Drive, Gmail, Sheets, Calendar tools | `integrations/gworkspace_tools.py` |
| [llmfit](https://github.com/AlexsJones/llmfit) | Hardware-aware model selection scoring | `integrations/llmfit_advisor.py` |

## Requirements

- Python 3.10+
- For local mode: llama-server binary (from [llama.cpp](https://github.com/ggerganov/llama.cpp)) + GGUF model
- For API mode: at least one provider API key
- See `requirements.txt` for Python packages

## License

MIT
