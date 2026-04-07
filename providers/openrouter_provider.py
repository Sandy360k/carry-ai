"""
carry-ai/providers/openrouter_provider.py — OpenRouter Provider
=================================================================

Implements BaseProvider for OpenRouter's unified gateway API.
Uses the shared OpenAI-compatible base class.

Features:
    - Unified access to 200+ models from all major providers
    - Automatic model routing and fallback
    - Pay-per-token pricing, no per-provider keys needed
    - Custom HTTP-Referer and X-Title headers for tracking

API Endpoint:
    POST https://openrouter.ai/api/v1/chat/completions
"""

import logging

from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.openrouter")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"

AVAILABLE_MODELS = [
    "anthropic/claude-opus-4-6",
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5-20251001",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/o4-mini",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    "meta-llama/llama-3.3-70b-instruct",
    "mistralai/mistral-large",
    "qwen/qwen3-8b",
    "qwen/qwen3-32b",
    "deepseek/deepseek-r1",
]


class OpenRouterProvider(OpenAICompatProvider):
    """OpenRouter unified gateway provider (OpenAI-compatible)."""

    def __init__(self, api_key: str, base_url: str | None = None):
        super().__init__(
            api_key=api_key,
            base_url=base_url or OPENROUTER_BASE_URL,
        )
        self._default_model = DEFAULT_MODEL
        self._available_models = AVAILABLE_MODELS

    @property
    def provider_name(self) -> str:
        return "openrouter"

    def _extra_headers(self) -> dict:
        """OpenRouter recommends HTTP-Referer and X-Title headers."""
        return {
            "HTTP-Referer": "https://github.com/carry-ai",
            "X-Title": "carry-ai",
        }

    def is_available(self) -> bool:
        """Check key format."""
        if not self._api_key:
            return False
        # OpenRouter keys start with sk-or-
        if not self._api_key.startswith("sk-or-"):
            return False
        return super().is_available()
