"""
carry-ai/providers/groq_provider.py — Groq Provider
=====================================================

Implements BaseProvider for Groq's inference API (OpenAI-compatible).
Uses the shared OpenAI-compatible base class.

Features:
    - Extremely fast inference (LPU hardware)
    - Free tier available (rate-limited)
    - GPT-OSS and Qwen open-weight models
    - Runtime model discovery via GET /openai/v1/models
      (speech / guard models filtered out; hardcoded list = fallback)
    - Streaming support
    - Function calling / tool use

API Endpoint:
    POST https://api.groq.com/openai/v1/chat/completions
"""

import logging

from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.groq")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"

# Offline fallback only -- the live list comes from GET /models.
AVAILABLE_MODELS = [
    "openai/gpt-oss-20b",    # default: fast, cheap
    "openai/gpt-oss-120b",   # flagship
    "qwen/qwen3.8-27b",      # vision (preview)
]

# Non-chat model families served by the same /models endpoint
_NON_CHAT_MARKERS = ("whisper", "orpheus", "guard", "safeguard", "tts", "playai")


class GroqProvider(OpenAICompatProvider):
    """Groq LPU inference API provider (OpenAI-compatible)."""

    def __init__(self, api_key: str, base_url: str | None = None):
        super().__init__(
            api_key=api_key,
            base_url=base_url or GROQ_BASE_URL,
        )
        self._default_model = DEFAULT_MODEL
        self._available_models = AVAILABLE_MODELS

    @property
    def provider_name(self) -> str:
        return "groq"

    def _is_chat_model(self, model_id: str) -> bool:
        lowered = model_id.lower()
        return not any(marker in lowered for marker in _NON_CHAT_MARKERS)

    def is_available(self) -> bool:
        """Check key format."""
        if not self._api_key:
            return False
        # Groq keys start with gsk_
        if not self._api_key.startswith("gsk_"):
            return False
        return super().is_available()
