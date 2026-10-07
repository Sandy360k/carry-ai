# Configuration Reference

## How Config Works

carry-ai uses a four-layer hierarchical configuration system. Each layer overrides the one before it, so you can always override a setting without editing the source.

```
Layer 1 — Built-in defaults     (always present, lowest priority)
Layer 2 — config/settings.json  (USB-resident file, optional)
Layer 3 — CARRY_AI_* env vars   (set in the host shell before launch)
Layer 4 — CLI flags             (passed at launch time, highest priority)
```

**Deep merge semantics.** Nested keys are merged, not replaced wholesale. If your `settings.json` only sets `agent.permission_mode`, all other `agent.*` values still come from defaults — you do not need to repeat them.

**Validation.** `config/settings.py` validates `mode`, `port`, `permission_mode`, and `max_iterations` on load. Errors are logged as warnings; carry-ai continues with the offending value replaced by the default.

---

## config/settings.json — All Settings

Place this file at `config/settings.json` on the USB drive (next to `carry-ai/`). All keys are optional — omit any you do not need to change. JSON does not support comments; the `//` annotations below are for documentation only and must be removed from a real file.

```json
{
  "mode": "auto",
  // Run mode. "auto" detects from available resources at boot.
  // "local"  — llama.cpp only (requires GGUF model + llama-server binary)
  // "api"    — cloud providers only (requires encrypted API keys)
  // "hybrid" — local first, falls back to API on failure
  // "auto"   — picks hybrid > local > api based on what is present

  "port": 8080,
  // TCP port for the Flask web UI at localhost:<port>.
  // Must be 1–65535. Override with --port or CARRY_AI_PORT.

  "model": "auto",
  // GGUF model selection for local/hybrid mode.
  // "auto" = pick best fit for available RAM from models/ directory.
  // Can be set to a specific filename stem, e.g. "qwen3-8b".

  "verbose": false,
  // Enable debug-level logging to stderr. Same as --verbose flag.

  "agent": {
    "max_iterations": 20,
    // Maximum tool-call iterations per conversation turn.
    // Prevents infinite tool loops. Range: 1–100.

    "tool_timeout_s": 30,
    // Seconds before a single tool call is considered timed out.
    // Shell commands and web fetches respect this limit.

    "permission_mode": "ask",
    // Controls which tool calls require user confirmation.
    // "ask"        — prompt before destructive commands (default)
    // "yolo"       — execute everything without asking
    // "safe"       — block dangerous patterns outright
    // See "Permission Modes" section below for full details.

    "context_window": 8192,
    // Token budget for the conversation context (local mode).
    // API mode uses a much larger budget (100,000 tokens).
    // Compaction triggers at 75% of this value by default.

    "compaction_threshold": 0.75
    // Fraction of context_window at which conversation history is
    // compacted. Older messages are summarized and pruned.
    // Range: 0.5–0.95.
  },

  "providers": {
    // Per-provider settings. "enabled" gates whether the provider is
    // loaded at all. "model" selects the default model for that provider.
    // Model IDs are checked against the provider's live model list at
    // runtime. A retired ID logs a warning and falls back to the
    // provider's default, so an old settings.json keeps working.

    "anthropic": {
      "enabled": true,
      "model": "claude-opus-5"
      // Claude models: claude-opus-5, claude-sonnet-5, claude-haiku-4-5, claude-fable-5-1
    },

    "openai": {
      "enabled": true,
      "model": "gpt-6-luna"
      // OpenAI models: gpt-6-luna, gpt-6-sol, gpt-6-astra, gpt-5.6-sol/-terra/-luna
    },

    "google": {
      "enabled": true,
      "model": "gemini-3.5-flash-lite"
      // Google models: gemini-3.5-flash-lite, gemini-3.8-flash,
      // gemini-3.1-pro-preview (no free tier), gemini-flash-latest
    },

    "groq": {
      "enabled": true,
      "model": "openai/gpt-oss-20b"
      // Groq models: openai/gpt-oss-20b, openai/gpt-oss-120b, qwen/qwen3.8-27b (vision)
    },

    "openrouter": {
      "enabled": true,
      "model": "openrouter/free"
      // Any OpenRouter model string. "openrouter/free" routes to free models,
      // "openrouter/auto" lets OpenRouter choose (paid).
    },

    "godmode": {
      "enabled": false,
      "model": "anthropic/claude-opus-5.5",
      // Base URL for your local G0DM0D3 server (OpenAI-compatible).
      // (the G0DM0D3 API server listens on 7860; health check is GET <base_url>/health)
      "base_url": "http://localhost:7860/v1",
      // autotune: let G0DM0D3 auto-detect query type and optimize sampling.
      "autotune": false,
      // stm_modules: post-processing modules ("hedge_reducer", "direct_mode", "casual_mode").
      "stm_modules": []
      // carry-ai always sends "godmode": false and "parseltongue": false (both
      // default to true server-side: a jailbreak system prompt and input
      // obfuscation). Set either to true here only if you really want them.
    },

    "onyx": {
      "enabled": false,
      "model": "onyx/default",
      // URL of your Onyx instance API.
      "base_url": "http://localhost:3000/api"
      // api_key is stored in config/providers.enc, not here.
    }
  },

  "local": {
    "llama_server_args": [],
    // Extra command-line arguments passed verbatim to llama-server.
    // Example: ["--log-disable", "--numa", "distribute"]

    "gpu_layers": 0,
    // Number of model layers to offload to GPU (0 = CPU only).
    // Set to -1 to offload all layers. Requires GPU + compatible llama-server.

    "threads": 0
    // CPU threads for inference. 0 = auto-detect from psutil.cpu_count().
  },

  "ui": {
    "theme": "dark",
    // Web UI color scheme. "dark" or "light".

    "stream": true,
    // Stream assistant responses token-by-token in the UI.
    // false = wait for the full response before displaying.

    "show_tool_output": true
    // Display tool call results inline in the chat UI.
  },

  "mcp": {
    "auto_connect": true,
    // Automatically connect to all configured MCP servers at boot.

    "servers": {}
    // MCP server definitions. See "MCP Server Configuration" below.
  },

  "plugins": {
    "auto_load": true,
    // Automatically load all plugins found in plugins/bundled/.

    "enabled": []
    // Explicit list of plugin names to enable when auto_load is false.
    // Example: ["my-plugin", "another-plugin"]
  },

  "voice": {
    // Push-to-talk (🎤 / Ctrl+M in the desktop app). Each direction:
    // "auto" (offline model if downloaded, else cloud if its key is set),
    // "offline" (sherpa-onnx on this PC), "cloud" (AssemblyAI / ElevenLabs), "off".
    // Cloud keys are kept in providers.enc ("assemblyai", "elevenlabs"), not here.
    "stt_backend": "auto",
    "tts_backend": "auto",
    "stt_model": "moonshine-tiny-en",   // or "moonshine-base-en"
    "tts_model": "kitten-nano-en",      // or "kokoro-multi"
    "tts_speaker": 0,                   // voice index (Kitten has 8: 0-7)
    "tts_speed": 1.0,
    "speak_replies": false,             // read agent replies aloud
    "auto_send": true,                  // send the transcript without editing
    "vision_enabled": false,            // attach a screenshot (terminal voice loop)
    "max_recording_seconds": 60
  },

  "cowork": {
    "sharing_enabled": false,
    // Enable session sharing (JSON/Markdown export and live LAN URL).

    "team_name": "",
    // Default team name for task tracking (config/teams.json).

    "session_ttl_minutes": 60
    // How long a shared session URL stays active (minutes).
  },

  "huggingface": {
    "oauth_client_id": ""
    // Client id of a *public* Hugging Face OAuth app (no secret, scope
    // "gated-repos"), registered once at
    // https://huggingface.co/settings/applications/new.
    // Enables "Sign in with Hugging Face" in the Model Manager: the user
    // approves a short code on their phone, no browser opens on this PC.
    // Empty = the button explains how to set it up; pasting a token works.
  },

  "experimental": {
    // Peripheral features, off by default. They don't fit the disposable,
    // offline-first USB session, so they're opt-in.
    "godmode": false,           // G0DM0D3 multi-model racing provider
    "onyx": false,              // Onyx RAG provider
    "google_workspace": false,  // Drive/Gmail/Sheets tools (needs the gws CLI)
    "cowork": false             // team session-sharing API
  },

  "cleanup": {
    "wipe_clipboard": true,
    // Clear the system clipboard on USB eject.

    "scrub_recent_files": true,
    // Remove carry-ai references from OS recent-files lists
    // (Windows Recent folder, Linux recently-used.xbel).

    "kill_child_processes": true
    // Kill llama-server, Flask, and all carry-ai child processes on eject.
  }
}
```

---

## Environment Variables

Set these in the host shell before running `launcher.py`. They override `settings.json` but are overridden by CLI flags.

| Variable | Maps to | Type | Example |
|---|---|---|---|
| `CARRY_AI_MODE` | `mode` | string | `CARRY_AI_MODE=api` |
| `CARRY_AI_PORT` | `port` | integer | `CARRY_AI_PORT=9090` |
| `CARRY_AI_VERBOSE` | `verbose` | boolean (1/true/yes) | `CARRY_AI_VERBOSE=1` |
| `CARRY_AI_MODEL` | `model` | string | `CARRY_AI_MODEL=qwen3-8b` |
| `CARRY_AI_PERMISSION_MODE` | `agent.permission_mode` | string | `CARRY_AI_PERMISSION_MODE=yolo` |
| `CARRY_AI_MAX_ITERATIONS` | `agent.max_iterations` | integer | `CARRY_AI_MAX_ITERATIONS=10` |
| `CARRY_AI_THEME` | `ui.theme` | string | `CARRY_AI_THEME=light` |
| `CARRY_AI_GPU_LAYERS` | `local.gpu_layers` | integer | `CARRY_AI_GPU_LAYERS=35` |

**API keys are not set via environment variables.** They are stored in `config/providers.enc` and managed with `python crypto/keystore.py`. This is intentional — environment variables can be logged by the OS, and carry-ai's threat model treats the host as untrusted.

---

## MCP Server Configuration

MCP servers are defined under `mcp.servers` in `settings.json`. Each key is a server name; the value is a transport configuration.

### stdio subprocess

Launches a local process and communicates via stdin/stdout JSON-RPC. Use this for most MCP servers installed with `uvx`, `npx`, or as Python packages.

```json
{
  "mcp": {
    "servers": {
      "github": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-github"],
        "env": {
          "GITHUB_TOKEN": "$GITHUB_TOKEN"
        }
      },
      "filesystem": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"]
      }
    }
  }
}
```

`$VAR` references in the `env` block are expanded from the host environment at connect time.

### Streamable HTTP

Connects to an already-running MCP server over HTTP (MCP 2026-07-28). Use
`"transport": "streamable-http"` (or `"http"`); a legacy `"sse"` entry is
accepted and treated the same way. The client connects statelessly first
(no `initialize` handshake; `MCP-Protocol-Version` / `Mcp-Method` /
`Mcp-Name` headers and client identity in `_meta`), and falls back to the
old `initialize` handshake for 2025-era servers. The response may be
`application/json` or `text/event-stream` — both are handled.

```json
{
  "mcp": {
    "servers": {
      "web-search": {
        "transport": "streamable-http",
        "url": "http://localhost:3001/mcp"
      },
      "custom-api": {
        "transport": "http",
        "url": "http://192.168.1.10:4000/mcp"
      }
    }
  }
}
```

Once connected, MCP tools appear in the agent registry as `mcp__{server}__{tool}` (e.g. `mcp__github__create_issue`).

---

## Permission Modes

Controlled by `agent.permission_mode`. The agent checks every tool call against this policy before executing it.

| Mode | Behavior |
|---|---|
| `ask` | Prompts the user before destructive shell commands and file overwrites. Default. |
| `yolo` | Executes all tools immediately without any confirmation prompt. |
| `safe` | Actively blocks dangerous patterns; no prompt, just a denial message. |

The following table shows how each mode handles example commands:

| Command / Action | `ask` | `yolo` | `safe` |
|---|---|---|---|
| `ls -la` | Allowed | Allowed | Allowed |
| `cat /etc/passwd` | Allowed | Allowed | Allowed |
| `write_file` to an existing path outside session | Prompt | Allowed | Blocked |
| `rm -rf /tmp/work` | Prompt | Allowed | Blocked |
| `git push --force` | Prompt | Allowed | Blocked |
| `DROP TABLE users` | Prompt | Allowed | Blocked |
| `shutdown -h now` | Prompt | Allowed | Blocked |
| `format C:` | Prompt | Allowed | Blocked |

**Dangerous patterns checked (16 total):** `rm -rf`, `rmdir`, `format`, `fdisk`, `dd if=`, `mkfs`, `del /s`, `shutdown`, `reboot`, `pkill`, `killall`, `taskkill`, `reg delete`, `Remove-Item -Recurse`, `git push --force`, `git reset --hard`, `DROP TABLE`, `DROP DATABASE`, `TRUNCATE`.

**In `ask` mode,** the agent prints the command and reason, then waits for `y` / `yes` on stdin (or the web UI confirmation dialog). If running `--no-ui`, the CLI prompt blocks until you respond.

**Changing permission mode at runtime** (REPL):
```
/permission yolo
/permission ask
/permission safe
```

---

## Per-Session Overrides

CLI flags are the highest-priority layer. They are applied after all other sources and always win.

```bash
# Force API mode on a machine without local models
python launcher.py --mode api

# Use a different port (e.g. 8080 is taken)
python launcher.py --port 9090

# Run headless (no Flask UI) with debug output
python launcher.py --no-ui --verbose

# Test the full boot sequence without USB hardware or injection
python launcher.py --dry-run

# Combine: API mode, custom port, verbose
python launcher.py --mode api --port 9090 --verbose

# Download a model from HuggingFace before booting
python launcher.py --download-model
```

**Available CLI flags:**

| Flag | Effect |
|---|---|
| `--mode local\|api\|hybrid` | Force run mode (skips auto-detection) |
| `--port PORT` | Override web UI port (default: 8080) |
| `--no-ui` | Skip Flask web UI; run as headless CLI REPL |
| `--verbose` | Enable DEBUG log level |
| `--dry-run` | Simulate boot without USB hardware, injection, or cleanup |
| `--download-model` | Open interactive HuggingFace model downloader before boot |
