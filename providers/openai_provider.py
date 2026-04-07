"""
carry-ai/providers/openai_provider.py — OpenAI Provider
=========================================================

Implements BaseProvider for OpenAI's Chat Completions API.
Uses the shared OpenAI-compatible base class.

Features:
    - Supports GPT-4o, GPT-4o-mini, o1, o3, o4-mini model families
    - Streaming via SSE
    - Function calling / tool use
    - JSON mode for structured output
    - Compatible base URL override (for Azure OpenAI, local proxies)

API Endpoint:
    POST https://api.openai.com/v1/chat/completions
"""

import logging

from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.openai")

DEFAULT_MODEL = "gpt-4o-mini"

AVAILABLE_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "o4-mini",
    "o3",
    "o3-mini",
    "o1",
    "o1-mini",
    "gpt-4-turbo",
]


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

    def is_available(self) -> bool:
        """Check key format and optionally ping /models."""
        if not self._api_key:
            return False
        # OpenAI keys start with sk-
        if not self._api_key.startswith("sk-"):
            return False
        return super().is_available()
