# API Provider Setup

carry-ai supports 7 LLM providers plus 2 voice service integrations. All chat providers participate in an automatic failover chain: if your primary provider returns a rate-limit, auth error, or overload response, carry-ai tries the next healthy provider in the chain without interrupting your conversation.

Provider health is tracked per-session (success rate, average latency, consecutive failures). Providers with auth errors are skipped immediately; providers that are rate-limited are retried with exponential backoff before falling back.

---

## Anthropic (Claude)

| | |
|---|---|
| **Models** | `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` |
| **Default model** | `claude-sonnet-4-6` |
| **Get key** | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| **Key format** | `sk-ant-api03-...` |
| **Settings key** | `providers.anthropic.enabled`, `providers.anthropic.model` |

Claude uses the Messages API (`POST /v1/messages`) with a top-level `system` parameter. Streaming is via Server-Sent Events. Tool use (function calling) is fully supported.

```bash
python crypto/keystore.py add anthropic
# Prompted: Enter anthropic.api_key: sk-ant-...
```

---

## OpenAI

| | |
|---|---|
| **Models** | `gpt-4o`, `gpt-4o-mini`, `o4-mini`, `o3`, `o3-mini`, `o1`, `gpt-4-turbo` |
| **Default model** | `gpt-4o-mini` |
| **Get key** | [platform.openai.com](https://platform.openai.com) → API Keys |
| **Key format** | `sk-...` |
| **Settings key** | `providers.openai.enabled`, `providers.openai.model` |

OpenAI uses the Chat Completions endpoint (`POST /v1/chat/completions`). The `base_url` can be overridden in `config/settings.json` for Azure OpenAI or local proxy setups.

```bash
python crypto/keystore.py add openai
# Prompted: Enter openai.api_key: sk-...
```

---

## Google (Gemini)

| | |
|---|---|
| **Models** | `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.0-flash`, `gemini-2.0-flash-lite` |
| **Default model** | `gemini-2.5-flash` |
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

---

## Groq

| | |
|---|---|
| **Models** | `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `gemma2-9b-it`, `qwen-qwq-32b`, `mistral-saba-24b` |
| **Default model** | `llama-3.3-70b-versatile` |
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
| **Models** | `anthropic/claude-opus-4-6`, `anthropic/claude-sonnet-4-6`, `openai/gpt-4o`, `openai/o4-mini`, `google/gemini-2.5-pro`, `meta-llama/llama-3.3-70b-instruct`, `qwen/qwen3-32b`, `deepseek/deepseek-r1`, and 200+ more |
| **Default model** | `anthropic/claude-sonnet-4-6` |
| **Get key** | [openrouter.ai/keys](https://openrouter.ai/keys) |
| **Key format** | `sk-or-...` |
| **Settings key** | `providers.openrouter.enabled`, `providers.openrouter.model` |

OpenRouter is a unified gateway that aggregates models from OpenAI, Anthropic, Google, Meta, Mistral, and many others under a single API key. It is useful for comparing outputs across providers and for accessing models from providers you haven't set up individually.

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

**Three operating modes:**

| Mode | Virtual model name | What it does |
|------|--------------------|-------------|
| **ULTRAPLINIAN fast** | `ultraplinian/fast` | Races 10 fast models in parallel, returns the winner |
| **ULTRAPLINIAN smart** | `ultraplinian/smart` | Races 20 quality models |
| **ULTRAPLINIAN all** | `ultraplinian/all` | Races all 51 models |
| **CONSORTIUM default** | `consortium/default` | Collects responses from top 10 models, synthesizes a ground-truth answer |
| **CONSORTIUM deep** | `consortium/deep` | Synthesizes from top 20 models |
| **AutoTune** | any model | Auto-detects query context (code, creative, analytical, etc.) and optimizes sampling |

**Setup:**

```bash
# 1. Self-host G0DM0D3 (requires Docker)
docker run -p 3000:3000 -e OPENROUTER_API_KEY=sk-or-... godmode

# 2. Add to keystore
python crypto/keystore.py add godmode
# Prompted for: api_key and base_url (default: http://localhost:3000/v1)
```

Enable in `config/settings.json`:

```json
{
  "providers": {
    "godmode": {
      "enabled": true,
      "base_url": "http://localhost:3000/v1",
      "autotune": false,
      "stm_modules": ["direct_mode"]
    }
  }
}
```

**STM (Semantic Transformation Modules)** post-process model output: `hedge_reducer` removes filler phrases like "I think", `direct_mode` gets to the point, `casual_mode` relaxes formal tone, `concise_mode` shortens verbose responses.

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

Available Onyx personas: `onyx/default` (general RAG), `onyx/research` (deep multi-step research), `onyx/code` (code-aware assistant).

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
