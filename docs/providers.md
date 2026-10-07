# API Provider Setup

carry-ai supports 12 LLM providers (10 in the catalogue `providers/catalog.py` plus the experimental G0DM0D3 and Onyx) and 2 voice service integrations. All chat providers participate in an automatic failover chain: if your primary provider returns a rate-limit, auth error, or overload response, carry-ai tries the next healthy provider in the chain without interrupting your conversation.

Provider health is tracked per-session (success rate, average latency, consecutive failures). Providers with auth errors are skipped immediately; providers that are rate-limited are retried with exponential backoff before falling back.

### Adding keys in the app

The desktop app's **API Keys** window (and `onboard.py`) list every provider in `providers/catalog.py`, free-to-start ones first. For each one:

- **Get key** shows the provider's key page as a QR code to open on your phone, a **Copy link** button (the clipboard is wiped on eject), or opens it in a throwaway private browser window. The host's own browser is never used.
- **Test** makes one cheap authenticated call: `GET /models` for most providers, `GET /api/v1/key` for OpenRouter (its model list is public), `GET /v1/models` with `x-api-key` for Anthropic, and a 16-token chat request for NVIDIA. A 429 or 402 response still means the key is valid.
- Pasted keys are cleaned up (`export FOO="…";` → `…`). Keys found in the environment are offered as "(detected)".
- **Use this session** keeps keys in RAM only. **Save & remember** encrypts them into `config/providers.enc`.

New keys take effect immediately. A local-only session becomes hybrid: the local model stays available as "Local (llama.cpp)" next to the cloud providers.

### Free-tier providers (no credit card)

| Provider | Base URL | Key page | Notes |
|---|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | https://openrouter.ai/settings/keys | `openrouter/free` routes to a free model with tool support; ~50 req/day |
| Groq | `https://api.groq.com/openai/v1` | https://console.groq.com/keys | ~1000 req/day per model |
| Google Gemini | native API (`providers/google_oauth.py`) | https://aistudio.google.com/apikey | free-tier prompts may be used for training |
| Cerebras | `https://api.cerebras.ai/v1` | https://cloud.cerebras.ai | ~1M tokens/day |
| Mistral | `https://api.mistral.ai/v1` | https://console.mistral.ai/api-keys | "Experiment" plan: phone verification, data used for training |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | https://build.nvidia.com/settings/api-keys | ~40 req/min, phone verification |

Paid additions: DeepSeek (`https://api.deepseek.com`, https://platform.deepseek.com/api_keys) and xAI (`https://api.x.ai/v1`, https://console.x.ai). The free tiers were checked in October 2026 against cheahjs/free-llm-api-resources and each provider's docs. They change often. GitHub Models (retired July 2026) and Together (no free tier) are not included.

### Model discovery (why the lists below are only a fallback)

Cloud model IDs are retired every few months, and a USB stick can sit in a drawer for longer than that. So each cloud provider asks its own model-list endpoint at runtime (5-second timeout) the first time it needs the list, filters it down to chat models, and keeps it **in memory only** for the session. Nothing is written to disk. If the endpoint can't be reached, the short built-in lists below are used instead.

If the model in `providers.<name>.model` (or one picked in the UI) is no longer offered, carry-ai logs a warning and uses that provider's default model instead of failing. When the router falls back to another provider, that provider gets its own configured model, not the one meant for the first provider.

---

## Anthropic (Claude)

| | |
|---|---|
| **Models** | `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`, `claude-fable-5-1` (live list from `GET /v1/models`) |
| **Default model** | `claude-opus-5` |
| **Get key** | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| **Key format** | `sk-ant-api03-...` |
| **Settings key** | `providers.anthropic.enabled`, `providers.anthropic.model` |

Claude uses the Messages API (`POST /v1/messages`) with a top-level `system` parameter. Streaming is via Server-Sent Events. Tool use (function calling) is fully supported.

Notes on the current models:
- `claude-opus-5`, `claude-sonnet-5` and `claude-fable-5-1` reject `temperature` / `top_p` / `top_k`, so carry-ai doesn't send them. Only `claude-haiku-4-5` still gets them.
- These models also reject assistant-message prefill (carry-ai drops a trailing assistant message), and `claude-fable-5-1` rejects forced `tool_choice`, which carry-ai turns into `auto`.
- If Claude declines a request (`stop_reason: "refusal"`), the chat shows a clear message rather than an empty or partial reply.

```bash
python crypto/keystore.py add anthropic
# Prompted: Enter anthropic.api_key: sk-ant-...
```

---

## OpenAI

| | |
|---|---|
| **Models** | `gpt-6-luna` (cheap, vision), `gpt-6-sol` (balanced), `gpt-6-astra` (flagship), `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` (live list from `GET /v1/models`) |
| **Default model** | `gpt-6-luna` |
| **Get key** | [platform.openai.com](https://platform.openai.com) → API Keys |
| **Key format** | `sk-...` |
| **Settings key** | `providers.openai.enabled`, `providers.openai.model` |

OpenAI uses the Chat Completions endpoint (`POST /v1/chat/completions`). The `base_url` can be overridden in `config/settings.json` for Azure OpenAI or local proxy setups.

Request rules carry-ai applies automatically:
- It sends `max_completion_tokens`, not `max_tokens`.
- Reasoning models (`gpt-5*`, `gpt-6*`, `o*`) don't get `temperature` or `top_p`.
- When tools are passed, `gpt-6-sol` and `gpt-6-luna` get `reasoning_effort: "none"`, which Chat Completions requires for function calling on those models.
- `gpt-6-astra` can only call tools through the Responses API, which carry-ai doesn't use. Tool-using agent turns therefore run on `gpt-6-luna` (a warning is logged), and plain chat turns still use astra.

```bash
python crypto/keystore.py add openai
# Prompted: Enter openai.api_key: sk-...
```

---

## Google (Gemini)

| | |
|---|---|
| **Models** | `gemini-3.5-flash-lite`, `gemini-3.8-flash` (quality), `gemini-3.1-pro-preview` (no free tier), alias `gemini-flash-latest` (live list from `GET /v1beta/models`) |
| **Default model** | `gemini-3.5-flash-lite` |
| **Get key (API key)** | [aistudio.google.com](https://aistudio.google.com) → Get API Key |
| **Get key (OAuth)** | Google Cloud Console → OAuth 2.0 credentials |
| **Key format** | No fixed prefix (API key); OAuth uses a refresh token |
| **Settings key** | `providers.google.enabled`, `providers.google.model` |

Google supports two authentication methods:

**Method 1 — API key (simpler):**

```bash
python crypto/keystore.py add google
# Prompted: Enter google.api_key: AIza...
```

**Method 2 — OAuth 2.0 (no quota restrictions):**

```python
from providers.google_oauth import GoogleProvider
GoogleProvider.auth()
# Opens a browser window for Google sign-in, saves refresh token
```

The OAuth refresh token is stored in the keystore under `google.oauth_refresh_token` and refreshed automatically during sessions.

Gemini 2.x models can't be used with new keys, so they have been removed. Gemini 3 models are tuned for the default temperature (1.0), so carry-ai doesn't send a custom `temperature` to them.

---

## Groq

| | |
|---|---|
| **Models** | `openai/gpt-oss-20b`, `openai/gpt-oss-120b` (flagship), `qwen/qwen3.8-27b` (vision, preview) (live list from `GET /openai/v1/models`, speech and guard models filtered out) |
| **Default model** | `openai/gpt-oss-20b` |
| **Get key** | [console.groq.com](https://console.groq.com) → API Keys |
| **Key format** | `gsk_...` |
| **Settings key** | `providers.groq.enabled`, `providers.groq.model` |

Groq runs models on custom LPU (Language Processing Unit) hardware, delivering inference speeds significantly faster than GPU-based providers. A free tier is available with rate limits. Groq is first in the default failover chain — it makes a good fast fallback.

```bash
python crypto/keystore.py add groq
# Prompted: Enter groq.api_key: gsk_...
```

---

## OpenRouter

| | |
|---|---|
| **Models** | `openrouter/free` (routes to free models), `google/gemma-4-31b-it:free`, `openrouter/auto`, and paid models such as `openai/gpt-6-luna`, `google/gemini-3.8-flash`, `anthropic/claude-opus-5.5` (live list from the public `GET /api/v1/models`) |
| **Default model** | `openrouter/free` |
| **Get key** | [openrouter.ai/keys](https://openrouter.ai/keys) |
| **Key format** | `sk-or-...` |
| **Settings key** | `providers.openrouter.enabled`, `providers.openrouter.model` |

OpenRouter is a unified gateway that aggregates models from OpenAI, Anthropic, Google, Meta, Mistral, and many others under a single API key. It is useful for comparing outputs across providers and for accessing models from providers you haven't set up individually.

The default, `openrouter/free`, works on a brand-new key with no credit. During model discovery, carry-ai skips `:batch` variants and any model whose `expiration_date` is in the past or less than 14 days away.

```bash
python crypto/keystore.py add openrouter
# Prompted: Enter openrouter.api_key: sk-or-...
```

---

## G0DM0D3 (Multi-model Racing)

| | |
|---|---|
| **Reference** | [github.com/elder-plinius/G0DM0D3](https://github.com/elder-plinius/G0DM0D3) |
| **Requires** | Self-hosted G0DM0D3 instance + at least one underlying provider key (e.g. OpenRouter) |
| **Settings key** | `providers.godmode.enabled`, `providers.godmode.base_url`, `providers.godmode.autotune` |

G0DM0D3 is a multi-model AI gateway that races queries across many models simultaneously and returns the best response. carry-ai integrates it as a provider via its OpenAI-compatible endpoint.

**Operating modes** (virtual model names are passed to the server unchanged; tiers are `fast` (10 models), `standard` (24), `smart` (36), `power` (45), `ultra` (51) — higher tiers need a Pro/Enterprise G0DM0D3 key):

| Mode | Virtual model name | What it does |
|------|--------------------|-------------|
| **ULTRAPLINIAN** | `ultraplinian/<tier>` (e.g. `ultraplinian/fast`) | Races the tier's models in parallel, returns the winner |
| **CONSORTIUM** | `consortium/<tier>` (e.g. `consortium/smart`) | Collects every response in the tier, synthesizes a ground-truth answer |
| **Single model** | any OpenRouter model ID | Plain OpenAI-compatible completion |
| **AutoTune** | any model | Auto-detects query context (code, creative, analytical, etc.) and optimizes sampling |

**Setup:**

```bash
# 1. Self-host the G0DM0D3 API (requires Docker; HF Spaces port 7860)
docker run -p 7860:7860 -e OPENROUTER_API_KEY=sk-or-... g0dm0d3-api

# 2. Add to keystore
python crypto/keystore.py add godmode
# Prompted for: api_key and base_url (default: http://localhost:7860/v1)
```

If the server has no `OPENROUTER_API_KEY`, carry-ai sends your OpenRouter key in the request body as `openrouter_api_key` (an `sk-or-...` value stored as the godmode `api_key` is used for this).

Enable in `config/settings.json`:

```json
{
  "providers": {
    "godmode": {
      "enabled": true,
      "base_url": "http://localhost:7860/v1",
      "autotune": false,
      "stm_modules": ["direct_mode"]
    }
  }
}
```

**STM (Semantic Transformation Modules)** post-process model output: `hedge_reducer` removes filler phrases like "I think", `direct_mode` gets to the point, `casual_mode` relaxes formal tone.

**Safety defaults:** G0DM0D3 turns `godmode` (a jailbreak system prompt) and `parseltongue` (input obfuscation) **on** by default. carry-ai always sends `"godmode": false, "parseltongue": false`, plus your `autotune`/`stm_modules` settings, so none of that pipeline runs unless you set `providers.godmode.godmode` / `.parseltongue` to `true` yourself.

---

## Onyx (RAG)

| | |
|---|---|
| **Reference** | [github.com/onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) |
| **Requires** | A running Onyx instance (self-hosted or [onyx.app](https://www.onyx.app) cloud) |
| **Settings key** | `providers.onyx.enabled`, `providers.onyx.base_url`, `providers.onyx.api_key` |

Onyx provides Retrieval-Augmented Generation (RAG) over 50+ data connectors — Google Drive, Slack, Confluence, Notion, GitHub, and more. When carry-ai routes a query through Onyx, Onyx searches your connected sources, builds a grounded prompt with retrieved documents, and returns an answer with source citations.

**Integration modes:**

- **As a provider** — route queries through Onyx's RAG pipeline for answers grounded in your data
- **As an MCP server** — register Onyx in `config/settings.json` under `mcp.servers` to expose its connectors as tools

**Setup:**

```bash
# 1. Deploy Onyx via Docker
docker compose -f docker-compose.dev.yml up -d

# 2. Configure connectors in Onyx's web UI (port 3000 by default)

# 3. Add to carry-ai keystore
python crypto/keystore.py add onyx
# Prompted for: api_key and base_url (default: http://localhost:3000/api)
```

Enable in `config/settings.json`:

```json
{
  "providers": {
    "onyx": {
      "enabled": true,
      "base_url": "http://localhost:3000/api",
      "model": "onyx/default"
    }
  }
}
```

Available Onyx models: `onyx/default` (general RAG via the default persona) and `onyx/research` (sends `deep_research: true` for multi-step research). carry-ai talks to Onyx's `POST /api/chat/send-chat-message` endpoint and reuses the returned `chat_session_id` for follow-up turns.

---

## AssemblyAI (Voice — transcription)

| | |
|---|---|
| **Used by** | `integrations/voice_tools.py` |
| **Purpose** | Push-to-talk speech-to-text transcription |
| **Get key** | [app.assemblyai.com](https://app.assemblyai.com) |
| **Free tier** | 5 hours of transcription per month |

AssemblyAI is not a chat provider — it is used exclusively by the voice pipeline to transcribe microphone audio into text before it is sent to the agent. Keys are stored under the `voice` key in `config/settings.json`, not in the provider keystore.

```json
{
  "voice": {
    "assemblyai_api_key": "your-key-here"
  }
}
```

---

## ElevenLabs (TTS — voice output)

| | |
|---|---|
| **Used by** | `integrations/voice_tools.py` |
| **Purpose** | Text-to-speech for agent responses in voice mode |
| **Get key** | [elevenlabs.io](https://elevenlabs.io) → Profile → API Key |
| **Free tier** | 10,000 characters per month |

ElevenLabs is not a chat provider — it converts the agent's text responses into spoken audio during voice mode sessions. Keys are stored under the `voice` key in `config/settings.json`.

```json
{
  "voice": {
    "elevenlabs_api_key": "your-key-here",
    "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM"
  }
}
```

The `voice_id` controls which ElevenLabs voice is used. Find voice IDs in the ElevenLabs voice library at [elevenlabs.io/voice-library](https://elevenlabs.io/voice-library).

---

## Managing Keys

The keystore CLI manages all API keys for chat providers:

```bash
python crypto/keystore.py setup           # Interactive setup wizard (first-time or reconfigure)
python crypto/keystore.py list            # List configured providers
python crypto/keystore.py add anthropic   # Add or update a single provider key
python crypto/keystore.py remove groq     # Remove a provider's keys
```

**Encryption details:**

- Storage format: `{"salt": "<base64 16-byte salt>", "data": "<Fernet token>"}`
- Cipher: Fernet (AES-128-CBC + HMAC-SHA256)
- Key derivation: PBKDF2-HMAC-SHA256, 600,000 iterations, random 16-byte salt (re-randomized on every save)
- Keys are decrypted into RAM at boot and zeroed from memory on USB eject via `cleanup/cleanup.py`
- `config/providers.enc` lives on the USB drive and is never committed to version control

There is no passphrase recovery mechanism. If the passphrase is lost, re-run `python crypto/keystore.py setup` to create a new keystore and re-enter all keys.

---

## Failover Chain

When a chat request comes in, `modes/api_mode.py` builds a prioritized list of providers to try:

1. The explicitly requested provider (if specified)
2. The configured primary provider
3. The default fallback order: `groq → openrouter → anthropic → openai → google → godmode → onyx`
4. Any remaining loaded providers

**Error handling per provider:**

| Error type | Behavior |
|-----------|----------|
| `RateLimitError` (HTTP 429) | Retry up to 2 times with exponential backoff, then fall to next provider |
| `AuthenticationError` (HTTP 401/403) | Skip provider immediately — no retries |
| `OverloadedError` (HTTP 503/529) | Skip provider immediately, try next |
| Generic `ProviderError` | Retry up to 2 times, then fall to next provider |
| Unexpected exception | Skip provider immediately |

**Health tracking** — each provider has a `ProviderHealth` record tracking total requests, success rate, average latency (rolling 20-request window), rate-limit count, and auth error count. A provider is marked unhealthy if it has any auth errors or more than 5 consecutive rate limits with no successes. Unhealthy providers are still included in the fallback chain but appear last.

Check provider health at runtime:

```
carry-ai> /status

  Providers:
    groq         healthy  success_rate=0.98  avg_latency=312ms
    anthropic    healthy  success_rate=1.00  avg_latency=1840ms
    openai       healthy  success_rate=0.95  avg_latency=2100ms
```

To switch the primary provider mid-session:

```
carry-ai> /provider groq
```
