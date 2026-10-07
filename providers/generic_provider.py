"""
carry-ai/providers/generic_provider.py — Any OpenAI-compatible cloud API
=========================================================================

One class for the catalogue providers that need nothing beyond the
OpenAI chat-completions schema (Cerebras, Mistral, DeepSeek, xAI, …).
Name, base URL and models come from ``providers/catalog.py``; the shared
``OpenAICompatProvider`` does requests, streaming, tool calls, error
mapping (RateLimit / Authentication / Overloaded) and live model discovery.

Providers with their own quirks keep their own module (OpenAI reasoning
params, OpenRouter headers, Groq key format, Anthropic, Gemini).
"""

import logging

from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.generic")


class GenericOpenAIProvider(OpenAICompatProvider):
    """A catalogue entry served through the OpenAI-compatible base."""

    def __init__(self, name: str, api_key: str, base_url: str,
                 default_model: str = "", models: list[str] | None = None,
                 extra_headers: dict | None = None):
        super().__init__(api_key=api_key, base_url=base_url)
        self._name = name
        self._default_model = default_model
        self._available_models = list(models or ([default_model] if default_model else []))
        self._headers_extra = dict(extra_headers or {})

    @property
    def provider_name(self) -> str:
        return self._name

    def _extra_headers(self) -> dict:
        return dict(self._headers_extra)

    @classmethod
    def from_catalog(cls, name: str, api_key: str, base_url: str | None = None):
        """Build from the ``providers.catalog`` entry *name*."""
        from providers.catalog import get_provider
        entry = get_provider(name)
        if entry is None or not entry.base_url:
            raise ValueError(f"No OpenAI-compatible catalogue entry for '{name}'")
        return cls(name=name, api_key=api_key, base_url=base_url or entry.base_url,
                   default_model=entry.default_model, models=entry.models)
