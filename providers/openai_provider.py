"""
carry-ai/providers/openai_provider.py — OpenAI Provider
=========================================================

Implements BaseProvider for OpenAI's Chat Completions API.
Uses the shared OpenAI-compatible base class.

Features:
    - GPT-6 (astra / sol / luna) and GPT-5.6 model families
    - Runtime model discovery via GET /v1/models (hardcoded list = fallback)
    - Streaming via SSE
    - Function calling / tool use
    - JSON mode for structured output
    - Compatible base URL override (for Azure OpenAI, local proxies)

Request-parameter rules (Chat Completions, Sept 2026):
    - Always send ``max_completion_tokens`` (``max_tokens`` is legacy).
    - Reasoning models (IDs starting ``gpt-5``, ``gpt-6`` or ``o<digit>``)
      reject ``temperature`` / ``top_p`` -- they are dropped.
    - ``gpt-6-sol`` / ``gpt-6-luna`` only support function calling in Chat
      Completions with ``reasoning_effort: "none"`` -- set automatically
      when tools are passed.
    - ``gpt-6-astra`` tool calling needs the Responses API, which this
      provider doesn't speak.  Tool turns are therefore sent to
      ``gpt-6-luna`` (TOOL_FALLBACK_MODEL) with a logged warning, so the
      agent loop keeps working; plain chat turns still use astra.

API Endpoint:
    POST https://api.openai.com/v1/chat/completions
"""

import logging
import re

from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.openai")

DEFAULT_MODEL = "gpt-6-luna"

# Offline fallback only -- the live list comes from GET /v1/models.
AVAILABLE_MODELS = [
    "gpt-6-luna",      # cheap, vision (default)
    "gpt-6-sol",       # balanced
    "gpt-6-astra",     # flagship
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
]

# Models that need reasoning_effort="none" for Chat Completions tool calls
_TOOLS_NEED_EFFORT_NONE = ("gpt-6-sol", "gpt-6-luna")

# Models whose tool calling is Responses-API only
_TOOLS_RESPONSES_ONLY = ("gpt-6-astra",)

# Substitute used for tool turns on Responses-only models
TOOL_FALLBACK_MODEL = "gpt-6-luna"

_REASONING_RE = re.compile(r"^(gpt-5|gpt-6|o\d)")

# /v1/models returns embeddings, audio, image, moderation... keep chat only.
_CHAT_PREFIX_RE = re.compile(r"^(gpt-|o\d|chatgpt-)")
_NON_CHAT_MARKERS = (
    "audio", "realtime", "tts", "transcribe", "whisper", "image", "dall-e",
    "embedding", "moderation", "search", "instruct", "codex", "deep-research",
    "computer-use", "-pro",
)


def is_reasoning_model(model: str) -> bool:
    """True for OpenAI reasoning models (no temperature/top_p)."""
    return bool(_REASONING_RE.match(model or ""))


class OpenAIProvider(OpenAICompatProvider):
    """OpenAI Chat Completions API provider."""

    def __init__(self, api_key: str, base_url: str | None = None):
        super().__init__(
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
        )
        self._default_model = DEFAULT_MODEL
        self._available_models = AVAILABLE_MODELS

    @property
    def provider_name(self) -> str:
        return "openai"

    def _is_chat_model(self, model_id: str) -> bool:
        if not _CHAT_PREFIX_RE.match(model_id):
            return False
        return not any(marker in model_id for marker in _NON_CHAT_MARKERS)

    def _build_payload(self, messages: list[dict], **kwargs) -> dict:
        """Apply OpenAI-specific parameter rules on top of the base payload."""
        payload = super()._build_payload(messages, **kwargs)
        model = payload.get("model", "")

        # max_tokens is deprecated/rejected for current models
        if "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")

        if payload.get("tools"):
            if model.startswith(_TOOLS_RESPONSES_ONLY):
                log.warning("OpenAI: %s tool calling requires the Responses API; "
                            "using %s for this tool turn.", model, TOOL_FALLBACK_MODEL)
                model = TOOL_FALLBACK_MODEL
                payload["model"] = model
            if model.startswith(_TOOLS_NEED_EFFORT_NONE):
                payload["reasoning_effort"] = "none"

        if is_reasoning_model(model):
            for key in ("temperature", "top_p"):
                payload.pop(key, None)

        return payload

    def is_available(self) -> bool:
        """Check key format and optionally ping /models."""
        if not self._api_key:
            return False
        # OpenAI keys start with sk-
        if not self._api_key.startswith("sk-"):
            return False
        return super().is_available()
