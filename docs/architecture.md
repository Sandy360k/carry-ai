# Architecture Overview

carry-ai is a USB-resident AI assistant built around three principles: **zero host footprint**, **RAM-only secrets**, and **graceful cleanup on eject**. No installer, no system services, no disk writes beyond the USB itself.

---

## Design Philosophy

| Principle | Implementation |
|-----------|---------------|
| Minimal host footprint | Session lives in RAM-backed `/dev/shm` (Linux) or `%TEMP%` (Windows) — nothing is installed; OS-level execution/USB records can't be removed without admin (see README) |
| RAM-only secrets | API keys are decrypted from `providers.enc` into RAM at boot and zeroed on shutdown |
| Graceful eject | A portable eject poller (plus WMI on Windows) triggers the 6-step wipe; the wipe code is pre-loaded so it runs after the drive is gone |
| Portability | Pure Python 3.10+, no build step, optional deps gracefully degraded |
| Extensibility | Provider → BaseProvider, Tool → register_tool(), Plugin → plugin.json, MCP → settings.json |

---

## System Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        USB DRIVE                            │
│  carry-ai/                                                  │
│  ├── launcher.py   ← single entry point                     │
│  ├── onboard.py    ← first-run setup wizard                 │
│  ├── config/                                                │
│  │   ├── providers.enc   ← Fernet-encrypted API keys        │
│  │   ├── settings.json   ← runtime overrides                │
│  │   └── memory.db       ← SQLite persistent memory         │
│  └── models/  ← GGUF model files                           │
└────────────────┬────────────────────────────────────────────┘
                 │ python launcher.py
                 ▼
┌─────────────────────────────────────────────────────────────┐
│                   INJECTION LAYER                           │
│  inject/inject_windows.py → %TEMP%\ai_session\             │
│  inject/inject_linux.sh   → tmpfs at /tmp/ai_session/      │
│                                                             │
│  Eject watchers registered:                                 │
│    Windows: WMI event / ctypes polling (2s)                 │
│    Linux:   udev rule (3-tier: vendor → kernel → generic)   │
└───────────────┬─────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         CORE RUNTIME                                 │
│                                                                      │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────────────────┐  │
│  │  agent.py   │◄──►│  tools.py    │    │      memory.py         │  │
│  │  ReAct loop │    │  14+ tools   │    │  SQLite + FTS5         │  │
│  │  ConvHistory│    │  + MCP tools │    │  dedup · decay · FTS   │  │
│  └──────┬──────┘    └──────────────┘    └────────────────────────┘  │
│         │                                                            │
│         │ mode routing                                               │
│         ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    MODE ROUTER                               │   │
│  │                                                              │   │
│  │  local_mode.py          api_mode.py                         │   │
│  │  ├── llama-server       ├── AnthropicProvider               │   │
│  │  │   subprocess         ├── OpenAIProvider                  │   │
│  │  └── 12-tier RAM map    ├── GoogleProvider                  │   │
│  │                         ├── GroqProvider                    │   │
│  │                         ├── OpenRouterProvider              │   │
│  │                         ├── GodmodeProvider (racing)        │   │
│  │                         └── OnyxProvider (RAG)              │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌───────────────┐   ┌──────────────┐   ┌──────────────────────┐   │
│  │  ui/app.py    │   │  mcp/        │   │  integrations/        │   │
│  │  Flask SPA    │   │  JSON-RPC    │   │  voice_tools.py       │   │
│  │  :8080        │   │  stdio/HTTP  │   │  scrapling_tools.py   │   │
│  │  12 API routes│   │  SSE client  │   │  gworkspace_tools.py  │   │
│  └───────────────┘   └──────────────┘   └──────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
                │
                │ eject signal (WMI / udev)
                ▼
┌─────────────────────────────────────────────────────────────┐
│                   cleanup/cleanup.py                        │
│  1. Kill process tree (psutil / taskkill / pkill)           │
│  2. Wipe session dir (tmpfs umount / shutil.rmtree)         │
│  3. Clear clipboard (ctypes / xclip / xsel / wl-copy)      │
│  4. Scrub recent files (recently-used.xbel, shell history)  │
│  5. Remove eject watchers (WMI / udev rule)                 │
│  6. Zero sensitive memory (ctypes.memset on key strings)    │
└─────────────────────────────────────────────────────────────┘
```

---

## Boot Sequence

| # | Step | Module | Notes |
|---|------|--------|-------|
| 1 | Detect OS | `launcher.py:detect_os()` | Windows or Linux only |
| 2 | Probe RAM | `launcher.py:probe_ram_gb()` | psutil → /proc/meminfo fallback |
| 3 | Inject session | `inject/inject_windows.py` or `inject/inject_linux.sh` | tmpfs or %TEMP% |
| 4 | Select mode | `launcher.py:select_mode()` | auto or forced via `--mode` |
| 5 | Auto-select model | `launcher.py:select_model_for_ram()` | 12-tier RAM map |
| 6 | Decrypt API keys | `crypto/keystore.py:KeyStore.decrypt_interactive()` | Fernet, PBKDF2 600k iters |
| 7 | Init MCP | `mcp/client.py:McpManager` | connect, tools/list, register |
| 8 | Load plugins | `plugins/loader.py:PluginLoader` | manifest parse, hook register |
| 9 | Start agent + UI | `agent/agent.py`, `ui/app.py` | daemon threads |
| 10 | Wait for eject | `threading.Event` + signal handler | → cleanup on exit |

---

## Agent Loop (ReAct Pattern)

The agent in `agent/agent.py` follows the Observe → Think → Act → Observe cycle used by ReAct-style agents.

```
User message
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  OBSERVE: inject memory context into system prompt  │
│    - session facts, recent discoveries, FTS hits    │
└────────────────────┬────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│  THINK: send messages[] to LLM provider             │
│    - includes tool schemas (JSON Schema format)     │
│    - provider selected by api_mode / local_mode     │
└────────────────────┬────────────────────────────────┘
                     │ response contains tool_use block?
              yes ───┤
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│  ACT: execute tool via tools.py registry            │
│    - PermissionPolicy checked first (16 patterns)   │
│    - execute_fn called with validated params        │
│    - result appended as tool_result message         │
└────────────────────┬────────────────────────────────┘
                     │
                     └──► loop back to OBSERVE (up to max_iterations)
                     │
              no tool_use
                     │
                     ▼
              Final text response → user + memory_store
```

**Iteration cap**: `settings.agent.max_iterations` (default 20). If exceeded, the agent returns a partial answer with a warning.

---

## Provider Routing

`modes/api_mode.py` maintains a priority-ordered list of enabled providers. Each provider exposes a health record:

```python
{
  "success_rate": float,      # rolling 10-call window
  "avg_latency_ms": float,    # exponential moving average
  "consecutive_failures": int,
  "last_error_type": str | None,
}
```

**Scoring**: `score = success_rate × (1 / log(avg_latency_ms + 1))`

**Failover logic**:
1. Skip providers with `consecutive_failures >= 3` (degraded)
2. Skip providers where last error was `AuthenticationError` (bad key — not retryable)
3. On `RateLimitError`: exponential backoff (1s, 2s, 4s) then try next provider
4. On `OverloadedError`: try next provider immediately, retry degraded one after 60s

---

## Memory Architecture

File: `agent/memory.py`

**Write path**:
```
observation text
     │
     ▼
SHA-256 hash → check memory.db (30s dedup window)
     │  duplicate? → skip
     │
     ▼
INSERT into memories table (FTS5)
     - obs_type, title, content, facts[], concepts[], tags[]
     - source, files_read/modified, session_id
     - relevance = 1.0 (fresh)
```

**Read path**:
```
query string
     │
     ▼
FTS5 MATCH search  (fallback: LIKE search)
     │
     ▼
relevance decay: score × (0.95 ^ days_since_access)
     │
     ▼
top-K results injected into system prompt as XML blocks
```

**Session summary**: On shutdown, the agent writes a structured `summary` observation with: request, investigated, learned, completed, next_steps.

---

## Voice Pipeline (Clicky-Inspired)

Ported from [farzaa/clicky](https://github.com/farzaa/clicky) (Swift/macOS) to Python. File: `integrations/voice_tools.py`.

```
User holds Enter key
        │
        ▼
AudioRecorder.record_until_keypress()
  PyAudio → raw PCM → save_wav() → temp .wav file
        │
        ▼
Transcriber.transcribe()
  POST /v2/upload → AssemblyAI
  POST /v2/transcript → poll until completed
  Returns: transcript text string
        │
        ├──► ScreenCapture.capture()
        │      PIL.ImageGrab / pyautogui.screenshot()
        │      → base64 PNG string
        │
        ▼
agent_fn(transcript, screenshot_b64)
  Claude Messages API with vision:
  content: [{"type": "image", ...}, {"type": "text", ...}]
  Response may contain [POINT:x,y:label] for UI guidance
        │
        ▼
TTSPlayer.speak(response_text)
  ElevenLabs SDK  ──►  streaming audio bytes
  or REST /v1/text-to-speech/{voice_id}
  → PyAudio playback
        │
        ▼
loop → "Press Enter to speak, Ctrl+C to stop"
```

**Availability check**: `is_voice_available()` returns per-component booleans so the agent can gracefully degrade (e.g., text response without TTS if ElevenLabs key is missing).

---

## Security Model

| Threat | Mitigation |
|--------|-----------|
| API key exposure | Fernet AES-128-CBC + HMAC, PBKDF2-HMAC-SHA256 600k iters, RAM-only during session |
| Host disk persistence | /dev/shm (Linux, RAM) / %TEMP% (Windows) — wiped on exit and eject |
| Clipboard leakage | Wipe step 3 of cleanup.py clears clipboard on eject |
| Shell history | Cleanup step 4 removes session commands from ~/.bash_history |
| Dangerous tool use | PermissionPolicy: 16 regex patterns, 3 modes (ask/yolo/safe) |
| Recent files lists | Cleanup step 4 scrubs recently-used.xbel and Windows recent files |
| In-memory secrets | ctypes.memset used to zero key bytes before deallocation |

---

## Extension Points

### Adding a Provider

1. Create `providers/myprovider.py` extending `BaseProvider` (or `OpenAICompatProvider`)
2. Implement `provider_name`, `chat()`, `stream()`, `is_available()`, `models()`
3. Handle `RateLimitError`, `AuthenticationError`, `OverloadedError`
4. Register in `modes/api_mode.py` provider list
5. Add config entry under `settings.providers.myprovider`

### Adding a Tool

```python
# In agent/tools.py or your plugin
register_tool(
    name="my_tool",
    description="What it does",
    parameters={"type": "object", "properties": {"arg": {"type": "string"}}},
    execute_fn=lambda arg="": do_something(arg),
    required=["arg"],
)
```

Keep `execute_fn` side-effect-safe when `permission_mode == "safe"`.

### Adding a Plugin

Create `plugins/bundled/my-plugin/plugin.json`:
```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "permissions": ["read"],
  "tools": [{"name": "greet", "description": "Say hello", "command": "./greet.py"}],
  "hooks": {"pre_tool_use": ["./hooks/check.py"]}
}
```

### Adding an MCP Server

Add to `config/settings.json`:
```json
{
  "mcp": {
    "servers": {
      "my-server": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-name"],
        "env": {"API_KEY": "$MY_API_KEY"}
      }
    }
  }
}
```

Tools appear in the agent registry as `mcp__my-server__tool_name`.
