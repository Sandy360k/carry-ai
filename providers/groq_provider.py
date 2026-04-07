"""
carry-ai/providers/groq_provider.py — Groq Provider
=====================================================

Implements BaseProvider for Groq's inference API (OpenAI-compatible).
Uses the shared OpenAI-compatible base class.

Features:
    - Extremely fast inference (LPU hardware)
    - Free tier available (rate-limited)
    - Supports Llama, Gemma, Qwen, Mixtral models
    - Streaming support
    - Function calling / tool use

API Endpoint:
    POST https://api.groq.com/openai/v1/chat/completions
"""

import logging

from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.groq")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"

AVAILABLE_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
    "qwen-qwq-32b",
    "mistral-saba-24b",
    "llama-3.2-90b-vision-preview",
    "llama-3.2-11b-vision-preview",
    "llama-3.2-3b-preview",
    "llama-3.2-1b-preview",
]


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

    def is_available(self) -> bool:
        """Check key format."""
        if not self._api_key:
            return False
        # Groq keys start with gsk_
        if not self._api_key.startswith("gsk_"):
            return False
        return super().is_available()
