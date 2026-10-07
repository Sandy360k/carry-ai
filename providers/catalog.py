"""
carry-ai/providers/catalog.py — Cloud providers the user can add a key for
==========================================================================

Single source of truth for the "API keys" settings screen (ui/desktop.py),
the onboarding wizard (onboard.py) and the generic OpenAI-compatible
provider (providers/generic_provider.py).

Each entry says what the provider is, whether it can be used for free,
where to get a key, its OpenAI-compatible base URL and a sensible default
model. Model ids here are only starting points: providers discover the
live list at runtime and replace retired ids (BaseProvider.resolve_model).

Free tiers were checked in October 2026 (see the cheahjs/free-llm-api-resources
list and each provider's docs). They change often, so the notes stay vague
about exact quotas.

Layout borrowed from OpenClaw's onboarding (featured providers first,
"(detected)" for keys already in the environment, paste clean-up).

Key check (``validate_key``) makes one cheap authenticated call:
    401/403 (or 400 "bad key")  -> invalid
    429                         -> valid, rate-limited right now
    2xx                         -> valid

Stdlib only.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("carry-ai.providers.catalog")

_TIMEOUT = 15
_UA = "carry-ai/key-check"


@dataclass(frozen=True)
class CloudProvider:
    key: str                  # keystore / router name
    label: str
    tier: str                 # "free" (free tier, no card) | "paid"
    blurb: str                # one line: what it's good for
    key_url: str              # where the user creates a key
    base_url: str = ""        # OpenAI-compatible base ("" = own module)
    default_model: str = ""
    models: list[str] = field(default_factory=list)
    free_note: str = ""       # what "free" means here
    env_var: str = ""         # picked up as "(detected)" if set
    check: str = "models"     # how validate_key probes: models|openrouter|anthropic|chat


PROVIDERS: list[CloudProvider] = [
    # ---- Free to start (no credit card) --------------------------------
    CloudProvider(
        key="openrouter", label="OpenRouter", tier="free",
        blurb="One key, hundreds of models — including free ones",
        key_url="https://openrouter.ai/settings/keys",
        base_url="https://openrouter.ai/api/v1",
        default_model="openrouter/free",
        models=["openrouter/free", "google/gemma-4-31b-it:free",
                "nvidia/nemotron-3-super-120b-a12b:free", "openrouter/auto"],
        free_note="Free: 'openrouter/free' picks a free model with tool support. "
                  "About 50 requests/day (1000/day after a one-time $10 top-up).",
        env_var="OPENROUTER_API_KEY", check="openrouter"),
    CloudProvider(
        key="groq", label="Groq", tier="free",
        blurb="Very fast open models (GPT-OSS, Llama)",
        key_url="https://console.groq.com/keys",
        base_url="https://api.groq.com/openai/v1",
        default_model="openai/gpt-oss-20b",
        models=["openai/gpt-oss-20b", "openai/gpt-oss-120b", "llama-3.3-70b-versatile"],
        free_note="Free tier: about 1000 requests/day per model, no card.",
        env_var="GROQ_API_KEY"),
    CloudProvider(
        key="google", label="Google Gemini", tier="free",
        blurb="Gemini Flash / Flash-Lite, long context, vision",
        key_url="https://aistudio.google.com/apikey",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-3.5-flash-lite",
        models=["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-pro-preview"],
        free_note="Free tier in AI Studio: a few hundred requests/day on Flash-Lite, "
                  "fewer on Flash. Free-tier prompts may be used to improve Google's models.",
        env_var="GEMINI_API_KEY"),
    CloudProvider(
        key="cerebras", label="Cerebras", tier="free",
        blurb="Fastest inference for GPT-OSS 120B",
        key_url="https://cloud.cerebras.ai",
        base_url="https://api.cerebras.ai/v1",
        default_model="gpt-oss-120b",
        models=["gpt-oss-120b", "qwen-3.8-27b"],
        free_note="Free tier: about 1M tokens/day, low requests/minute.",
        env_var="CEREBRAS_API_KEY"),
    CloudProvider(
        key="mistral", label="Mistral", tier="free",
        blurb="Mistral Small / Medium, EU-hosted",
        key_url="https://console.mistral.ai/api-keys",
        base_url="https://api.mistral.ai/v1",
        default_model="mistral-small-latest",
        models=["mistral-small-latest", "mistral-medium-latest", "mistral-large-latest"],
        free_note="Free 'Experiment' plan: phone verification, and you agree to your "
                  "prompts being used for training.",
        env_var="MISTRAL_API_KEY"),
    CloudProvider(
        key="nvidia", label="NVIDIA NIM", tier="free",
        blurb="Nemotron, GPT-OSS and other open models",
        key_url="https://build.nvidia.com/settings/api-keys",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="openai/gpt-oss-20b",
        models=["openai/gpt-oss-20b", "nvidia/nemotron-3-super-120b-a12b"],
        free_note="Free for development: about 40 requests/minute, phone verification.",
        env_var="NVIDIA_API_KEY", check="chat"),

    # ---- Paid (pay as you go) -----------------------------------------
    CloudProvider(
        key="anthropic", label="Anthropic Claude", tier="paid",
        blurb="Claude Opus / Sonnet / Haiku — strongest at agentic tool use",
        key_url="https://platform.claude.com/settings/keys",
        default_model="claude-opus-5",
        models=["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
        env_var="ANTHROPIC_API_KEY", check="anthropic"),
    CloudProvider(
        key="openai", label="OpenAI", tier="paid",
        blurb="GPT-6 family",
        key_url="https://platform.openai.com/api-keys",
        base_url="https://api.openai.com/v1",
        default_model="gpt-6-luna",
        models=["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"],
        env_var="OPENAI_API_KEY"),
    CloudProvider(
        key="deepseek", label="DeepSeek", tier="paid",
        blurb="Very low-cost strong reasoning models",
        key_url="https://platform.deepseek.com/api_keys",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        models=["deepseek-chat", "deepseek-reasoner"],
        env_var="DEEPSEEK_API_KEY"),
    CloudProvider(
        key="xai", label="xAI Grok", tier="paid",
        blurb="Grok models",
        key_url="https://console.x.ai",
        base_url="https://api.x.ai/v1",
        default_model="grok-4",
        models=["grok-4"],
        env_var="XAI_API_KEY"),
]

_BY_KEY = {p.key: p for p in PROVIDERS}


def get_provider(name: str) -> CloudProvider | None:
    return _BY_KEY.get(name)


def free_providers() -> list[CloudProvider]:
    return [p for p in PROVIDERS if p.tier == "free"]


def paid_providers() -> list[CloudProvider]:
    return [p for p in PROVIDERS if p.tier == "paid"]


def detected_key(name: str, environ: dict | None = None) -> str:
    """Key already set in the environment for *name* ('' if none)."""
    import os
    p = _BY_KEY.get(name)
    env = os.environ if environ is None else environ
    return normalize_key(env.get(p.env_var, "")) if p and p.env_var else ""


_EXPORT_RE = re.compile(r"^\s*(?:export\s+|set\s+)?[A-Z_][A-Z0-9_]*\s*=\s*", re.I)


def normalize_key(raw: str) -> str:
    """Clean a pasted key: 'export FOO="sk-…";' -> 'sk-…'."""
    s = (raw or "").strip()
    s = _EXPORT_RE.sub("", s).strip().rstrip(";").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


# ---------------------------------------------------------------------------
# Key check
# ---------------------------------------------------------------------------

def _probe(name: str, key: str) -> Request:
    p = _BY_KEY[name]
    headers = {"User-Agent": _UA, "Authorization": f"Bearer {key}"}
    if p.check == "anthropic":
        return Request("https://api.anthropic.com/v1/models",
                       headers={"User-Agent": _UA, "x-api-key": key,
                                "anthropic-version": "2023-06-01"})
    if p.check == "openrouter":
        # /models is public on OpenRouter; /key needs a valid key
        return Request(f"{p.base_url}/key", headers=headers)
    if p.check == "chat":
        # /models is public here; smallest real request (some reject < 16 tokens)
        body = json.dumps({"model": p.default_model, "max_tokens": 16,
                           "messages": [{"role": "user", "content": "Hi"}]}).encode()
        return Request(f"{p.base_url}/chat/completions", data=body,
                       headers={**headers, "Content-Type": "application/json"})
    return Request(f"{p.base_url}/models", headers=headers)


def validate_key(name: str, key: str, opener=urlopen) -> tuple[str, str]:
    """Check *key* against provider *name* with one cheap call.

    Returns (state, message): state is "ok", "invalid", "limited" or "error".
    """
    key = normalize_key(key)
    if name not in _BY_KEY:
        return "error", "unknown provider"
    if not key:
        return "invalid", "no key"
    try:
        with opener(_probe(name, key), timeout=_TIMEOUT):
            return "ok", "✓ key works"
    except HTTPError as e:
        if e.code in (401, 403):
            return "invalid", "✗ key rejected"
        if e.code == 400:
            # Google and Cerebras answer a bad key with 400
            try:
                text = e.read().decode("utf-8", "replace").lower()
            except OSError:
                text = ""
            if "key" in text or "auth" in text:
                return "invalid", "✗ key rejected"
            return "error", "HTTP 400 — try again later"
        if e.code == 429:
            return "limited", "✓ key works (rate-limited right now)"
        if e.code == 402:
            return "limited", "✓ key works (no credit left)"
        return "error", f"HTTP {e.code} — try again later"
    except (URLError, OSError) as e:
        return "error", f"can't reach the server ({getattr(e, 'reason', e)})"
