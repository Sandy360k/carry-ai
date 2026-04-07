"""
carry-ai/providers/base.py — Base Provider Interface
=====================================================

Abstract base class that all LLM provider implementations must follow.
Ensures a uniform interface for the API router regardless of which
cloud provider is being used.

All providers receive their API key (already decrypted) at __init__ time.
Keys are never written to disk -- they exist only in RAM.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ===================================================================
# Exceptions
# ===================================================================

class ProviderError(Exception):
    """Base exception for provider failures."""

    def __init__(self, message: str, provider: str = "", status_code: int | None = None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(message)


class RateLimitError(ProviderError):
    """429 — rate limited. May include retry_after hint."""

    def __init__(self, message: str, provider: str = "", retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(message, provider=provider, status_code=429)


class AuthenticationError(ProviderError):
    """401/403 — invalid or expired credentials."""

    def __init__(self, message: str, provider: str = ""):
        super().__init__(message, provider=provider, status_code=401)


class OverloadedError(ProviderError):
    """529/503 — provider is overloaded."""

    def __init__(self, message: str, provider: str = ""):
        super().__init__(message, provider=provider, status_code=529)


# ===================================================================
# Normalized response
# ===================================================================

@dataclass
class ChatResponse:
    """Normalized chat completion response across all providers."""
    role: str = "assistant"
    content: str = ""
    tool_calls: list[dict] | None = None
    provider: str = ""
    model: str = ""
    usage: dict = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    })

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "provider": self.provider,
            "model": self.model,
            "usage": self.usage,
        }


# ===================================================================
# Base provider ABC
# ===================================================================

class BaseProvider(ABC):
    """Abstract base class for all LLM provider implementations.

    Subclasses must implement chat(), stream(), is_available(), and models().
    The provider_name property identifies this provider in logs and responses.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short identifier: 'anthropic', 'openai', 'google', 'groq', 'openrouter'."""
        ...

    @abstractmethod
    def chat(self, messages: list[dict], **kwargs) -> ChatResponse:
        """Send a chat completion request and return a normalized response.

        Args:
            messages: OpenAI-format message list.
            **kwargs: model, temperature, max_tokens, tools, tool_choice, etc.

        Returns:
            ChatResponse with normalized fields.

        Raises:
            RateLimitError: On 429.
            AuthenticationError: On 401/403.
            OverloadedError: On 529/503.
            ProviderError: On other failures.
        """
        ...

    @abstractmethod
    def stream(self, messages: list[dict], **kwargs):
        """Stream a chat completion response.

        Yields dicts: {"type": "content"|"tool_call"|"done", "data": ...}

        Args:
            messages: OpenAI-format message list.
            **kwargs: Same as chat().
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is configured and reachable.

        Should be fast (< 5s) — e.g. verify key format, ping /models.
        """
        ...

    @abstractmethod
    def models(self) -> list[str]:
        """List available model identifiers for this provider."""
        ...

    @property
    def default_model(self) -> str | None:
        """Default model to use when none is specified. Override in subclass."""
        available = self.models()
        return available[0] if available else None
