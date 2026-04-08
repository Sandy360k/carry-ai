# Getting Started with carry-ai

carry-ai is a portable AI assistant that lives on a USB drive. Plug it into any Windows or Linux machine, run one command, and you have a full AI assistant at `localhost:8080`. When you eject the USB, it wipes every trace from the host.

---

## Prerequisites

- **Python 3.10+** on the host machine (or bundled in the USB package)
- **USB drive** — 8 GB+ recommended for local model mode; 1 GB is sufficient for API-only mode
- **For local mode**: `llama-server` binary from [llama.cpp](https://github.com/ggerganov/llama.cpp/releases)
- **For API mode**: at least one API key from a supported provider

---

## Option A — Run the Onboarding Wizard (Recommended)

```bash
python onboard.py
```

The interactive wizard handles everything in one pass:

1. **System check** — verifies Python version, detects OS, probes available RAM
2. **Dependency install** — installs packages from `requirements.txt`
3. **Mode selection** — Local, API, or Hybrid
4. **Key / model setup** — walks you through API key entry or model download
5. **Voice setup** (optional) — AssemblyAI and ElevenLabs keys for push-to-talk
6. **Validation** — does a `--dry-run` boot to confirm everything works

This is the fastest path to a working install. Skip to [First Boot](#5-first-boot) when the wizard finishes.

---

## Option B — Manual Setup

### 1. Install dependencies

**Full install** (local + API + all integrations):

```bash
pip install -r requirements.txt
```

**Minimal install** (API-only, no local inference):

```bash
pip install flask requests cryptography psutil
```

Optional extras — install only what you need:

```bash
pip install huggingface-hub   # For the model downloader
pip install scrapling          # For anti-bot web scraping tools
pip install pyautogui          # For screenshot / input tools
```

---

### 2. Choose your mode

| Mode | Requirements | Pros | Cons |
|------|-------------|------|------|
| **Local** | llama-server binary + GGUF model on USB | Fully offline, no API costs, private | Slower on low-RAM machines; model file is large |
| **API** | Internet connection + at least one API key | Fast, no model storage needed, latest models | Requires connectivity; API costs apply |
| **Hybrid** | Both of the above | Falls back to local if API is unavailable | Requires both setups |

carry-ai auto-detects the best mode on boot. Use `--mode` to force a specific mode.

---

### 3a. API Mode setup

Run the interactive key wizard:

```bash
python crypto/keystore.py setup
```

You will be prompted to set an encryption passphrase, then enter keys for whichever providers you want. Keys are stored encrypted in `config/providers.enc` on the USB — they never leave the drive in plaintext.

**Supported providers:**

| Provider | Description | Key prefix |
|----------|-------------|-----------|
| **Anthropic** | Claude Opus, Sonnet, Haiku — Anthropic's own models | `sk-ant-...` |
| **OpenAI** | GPT-4o, o4-mini, o3 — OpenAI's model family | `sk-...` |
| **Google** | Gemini 2.5 Pro, 2.5 Flash, 2.0 Flash — API key or OAuth | — |
| **Groq** | Llama 3.3 70B, Gemma2, Qwen — LPU hardware, very fast, free tier | `gsk_...` |
| **OpenRouter** | Unified gateway to 200+ models from all major providers | `sk-or-...` |
| **G0DM0D3** | Multi-model racing — race up to 51 models in parallel | — |
| **Onyx** | RAG-enhanced answers grounded in your connected data sources | — |

To add or update a single key later:

```bash
python crypto/keystore.py add anthropic
python crypto/keystore.py list
python crypto/keystore.py remove groq
```

---

### 3b. Local Mode setup

**Step 1 — Get the llama-server binary**

Download the appropriate build for your OS from the [llama.cpp releases page](https://github.com/ggerganov/llama.cpp/releases). Place the binary somewhere on your `PATH`, or note the full path — carry-ai will locate it automatically if it is in `PATH`.

**Step 2 — Download a model**

Use the interactive model downloader:

```bash
python models/downloader.py interactive
```

The wizard detects your available RAM, suggests suitable models, searches HuggingFace, and downloads directly into the `models/` directory on the USB. You can also search and suggest from the command line:

```bash
python models/downloader.py search "Qwen3 8B"
python models/downloader.py suggest --ram 8
python models/downloader.py local              # list already-downloaded models
```

**RAM-to-model tier reference** — carry-ai automatically selects the best model that fits your available RAM:

| Available RAM | Recommended Model | Quantization |
|--------------|-------------------|-------------|
| 32 GB+ | Qwen3 30B | Q6_K |
| 24 GB+ | Llama 4 Scout 17B | Q6_K |
| 20 GB+ | Qwen3 14B | Q8_0 |
| 16 GB+ | Gemma 4 12B | Q4_K_M |
| 12 GB+ | Qwen3 14B | Q4_K_M |
| 10 GB+ | Qwen3 8B | Q8_0 |
| 8 GB+ | Qwen3 8B | Q4_K_M |
| 6 GB+ | Gemma 4 E4B | Q4_K_M |
| 5 GB+ | Qwen3.5 4B | Q4_K_M |
| 4 GB+ | Phi-4-mini | Q4_K_M |
| 3 GB+ | Gemma 4 E2B | Q4_K_M |
| Under 3 GB | Gemma 3 1B | Q4_K_M |

Place `.gguf` files in the `models/` directory. carry-ai matches by filename stem — no configuration required.

---

### 4. Configure (optional)

Create `config/settings.json` on the USB to override defaults. Any field you omit falls back to the built-in default. You only need to include what you want to change.

```json
{
  "mode": "auto",
  "port": 8080,

  "providers": {
    "anthropic": {
      "enabled": true,
      "model": "claude-sonnet-4-6"
    },
    "groq": {
      "enabled": true,
      "model": "llama-3.3-70b-versatile"
    }
  },

  "agent": {
    "max_iterations": 20,
    "permission_mode": "ask",
    "context_window": 8192
  },

  "local": {
    "gpu_layers": 0,
    "threads": 0
  },

  "ui": {
    "theme": "dark",
    "stream": true
  },

  "voice": {
    "enabled": false,
    "assemblyai_api_key": "",
    "elevenlabs_api_key": "",
    "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM"
  }
}
```

Key settings explained:

- **`mode`** — `"auto"` lets carry-ai decide; override with `"local"`, `"api"`, or `"hybrid"`
- **`permission_mode`** — `"ask"` prompts before dangerous operations; `"yolo"` skips all prompts; `"safe"` blocks them outright
- **`gpu_layers`** — number of model layers to offload to GPU; `0` = CPU only
- **`threads`** — llama-server CPU threads; `0` = auto-detect
- **`stream`** — stream responses token-by-token in the web UI

Settings can also be passed as environment variables with the `CARRY_AI_` prefix:

```bash
CARRY_AI_PORT=9090 CARRY_AI_MODE=api python launcher.py
```

---

### 5. First boot

**Normal boot (auto-detect everything):**

```bash
python launcher.py
```

**Dry run (test without USB hardware or actual injection):**

```bash
python launcher.py --dry-run
```

**Other useful flags:**

```bash
python launcher.py --mode api          # Force API mode
python launcher.py --mode local        # Force local mode
python launcher.py --download-model    # Download a model, then boot
python launcher.py --no-ui --verbose   # Headless with debug logging
python launcher.py --port 9090         # Use a different port
```

On a successful boot you will see the carry-ai banner, a summary of detected RAM and mode, and the line:

```
  Web UI: http://localhost:8080
  carry-ai is running. Press Ctrl+C to stop.
```

---

## Voice Mode (Clicky-Inspired)

carry-ai includes an optional push-to-talk voice pipeline implemented in `integrations/voice_tools.py`. The pipeline is:

```
Push-to-talk (hold key) → AssemblyAI transcription → Agent / Claude vision → ElevenLabs TTS
```

**How it works:**

1. Hold the configured push-to-talk key (default: Right Ctrl) to record audio from the microphone
2. Release to send the audio clip to AssemblyAI for real-time speech-to-text transcription
3. The transcribed text is sent to the agent loop as a normal user message
4. If the message references the screen, the agent captures a screenshot and attaches it to a vision-capable model (Claude or Gemini)
5. The agent's text response is sent to ElevenLabs and played back as audio

**Getting API keys:**

| Service | Where to get the key | Free tier |
|---------|---------------------|-----------|
| AssemblyAI | [app.assemblyai.com](https://app.assemblyai.com) | 5 hours of transcription/month |
| ElevenLabs | [elevenlabs.io](https://elevenlabs.io) | 10,000 characters of TTS/month |

**Adding keys via the onboarding wizard:**

Run `python onboard.py` and select the voice setup step. The wizard will prompt for both keys and write them to `config/settings.json` under the `voice` key.

**Adding keys manually** — add to `config/settings.json`:

```json
{
  "voice": {
    "enabled": true,
    "assemblyai_api_key": "your-assemblyai-key",
    "elevenlabs_api_key": "your-elevenlabs-key",
    "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM"
  }
}
```

Voice mode requires additional packages:

```bash
pip install pyaudio assemblyai elevenlabs
```

Voice mode is entirely optional. carry-ai works without it. The `integrations/voice_tools.py` module gracefully skips initialization if keys or audio dependencies are absent.

---

## Web UI

Open `http://localhost:8080` in any browser after launching carry-ai.

The interface is a single-page app with a collapsible sidebar. The sidebar panels are:

| Panel | Description |
|-------|-------------|
| **Chat** | Main conversation view with streaming responses and collapsible tool output |
| **Memory** | Browse and search the persistent SQLite memory store |
| **Tools** | List all registered tools (built-in + MCP + plugin) with descriptions |
| **Settings** | Runtime settings — mode, provider, theme, permission mode |
| **Logs** | Live activity log for the current session |

The header shows the current mode badge (LOCAL / API / HYBRID), provider health indicator, and a theme toggle (dark/light).

---

## First Conversation

Once the web UI is open, type a message and press Enter (or Shift+Enter for a newline). The agent uses a ReAct loop — it may invoke tools and show intermediate steps before giving a final answer.

```
carry-ai> /status

  Mode:     API
  Provider: anthropic (claude-sonnet-4-6)
  Memory:   14 stored facts
  Tools:    22 registered (18 built-in, 4 MCP)
  Session:  /tmp/ai_session/

carry-ai> /tools

  shell_exec      Execute a shell command on the host
  read_file       Read a file from disk
  write_file      Write content to a file
  web_fetch       Fetch a URL and return its text content
  screenshot      Capture a screenshot of the current screen
  clipboard_read  Read the current clipboard contents
  memory_store    Save a fact to long-term memory
  memory_search   Search long-term memory
  ...

carry-ai> What Python packages are installed in this environment?

  [tool: shell_exec] pip list --format=columns
  ...
  Found 47 packages. Highlights: Flask 3.0.3, requests 2.31.0,
  cryptography 42.0.5, psutil 5.9.8 ...

carry-ai> /help

  Slash commands:
    /status              Show current mode, provider, memory count, tools
    /tools               List all registered tools
    /memory <query>      Search long-term memory
    /clear               Clear conversation (memory is retained)
    /mode <mode>         Switch mode mid-session (local | api | hybrid)
    /provider <name>     Switch to a specific provider
    /help                Show this message
```

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| `No module named 'psutil'` | psutil not installed | `pip install psutil` |
| `No GGUF models found in models/` | models/ directory is empty | Run `python launcher.py --download-model` or copy a `.gguf` file into `models/` |
| `Failed to decrypt keystore` | Wrong passphrase | Re-run `python crypto/keystore.py setup` and re-enter keys — there is no passphrase recovery |
| `Address already in use` on port 8080 | Port conflict | Run with `--port 9090` (or any free port) |
| `python: command not found` | Python 3.10+ not on PATH | Use `python3` explicitly, or install Python 3.10+ |
| `AuthenticationError` on first message | API key invalid or expired | Check the key format in `docs/providers.md`, regenerate at the provider's console |
| `OSError: No space left on device` | USB is full | Free space or use a larger USB; models need 2–8 GB |
| `ModuleNotFoundError: flask` | Flask not installed | `pip install flask` or `pip install -r requirements.txt` |
| Agent stops after 20 tool calls | `max_iterations` limit reached | Increase `agent.max_iterations` in `config/settings.json` |
| carry-ai fails on macOS | macOS not supported | carry-ai supports Windows and Linux only |
