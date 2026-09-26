"""
carry-ai/providers/base.py — Base Provider Interface
=====================================================

Abstract base class that all LLM provider implementations must follow.
Ensures a uniform interface for the API router regardless of which
cloud provider is being used.

All providers receive their API key (already decrypted) at __init__ time.
Keys are never written to disk -- they exist only in RAM.

Runtime model discovery:
    Cloud model IDs go stale (models are retired every few months), so
    providers do not rely solely on hardcoded lists.  ``BaseProvider``
    implements a shared discovery flow:

        models()  ->  discover_models()
                         |-- in-memory cache hit  -> cached list
                         |-- _fetch_model_ids()   -> provider list endpoint
                         |                           (short timeout, ~5 s)
                         '-- any error / no data  -> _fallback_models()

    ``resolve_model()`` checks a requested/configured model against the
    *live* list and, if it has been retired, logs a warning and substitutes
    the provider default (or the first still-live fallback).  The cache is
    process memory only -- nothing is ever written to disk, so no trace is
    left on the host.
"""

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("carry-ai.providers.base")

# Timeout (seconds) for model-list discovery requests.
DISCOVERY_TIMEOUT = 5.0

# After a failed discovery, don't retry for this long (avoids a 5 s stall
# on every chat turn when offline).
DISCOVERY_RETRY_AFTER_S = 300.0

# In-memory, process-wide discovery cache:
#   (provider_name, base_url) -> (monotonic timestamp, list[str] | None)
# A None list records a failed attempt.  Never persisted to disk.
_MODEL_CACHE: dict[tuple[str, str], tuple[float, list[str] | None]] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def clear_model_cache() -> None:
    """Drop all discovered model lists (e.g. on eject or in tests)."""
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.clear()


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
        """List available model identifiers for this provider.

        Providers that support discovery should simply
        ``return self.discover_models()``.
        """
        ...

    @property
    def default_model(self) -> str | None:
        """Default model to use when none is specified. Override in subclass."""
        available = self.models()
        return available[0] if available else None

    # ------------------------------------------------------------------
    # Runtime model discovery (shared by all cloud providers)
    # ------------------------------------------------------------------

    def _fetch_model_ids(self) -> list[str] | None:
        """Query the provider's model-list endpoint.

        Override in subclasses.  Return chat-capable model IDs, or None if
        discovery isn't supported.  May raise -- errors are caught by
        discover_models() and trigger the hardcoded fallback.
        """
        return None

    def _fallback_models(self) -> list[str]:
        """Hardcoded offline fallback list. Override in subclasses."""
        return []

    def _model_aliases(self) -> set[str]:
        """Model IDs that are always accepted even if not in the live list
        (e.g. router pseudo-models like ``openrouter/auto``)."""
        return set()

    def _discovery_cache_key(self) -> tuple[str, str]:
        return (self.provider_name, str(getattr(self, "_base_url", "")))

    def _live_models(self) -> list[str] | None:
        """Return the discovered model list, or None if discovery failed or
        is unsupported.  Triggers (cached) discovery on first use."""
        key = self._discovery_cache_key()
        now = time.monotonic()
        with _MODEL_CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
        if cached is not None:
            ts, ids = cached
            if ids is not None or (now - ts) < DISCOVERY_RETRY_AFTER_S:
                return list(ids) if ids is not None else None

        ids: list[str] | None = None
        try:
            fetched = self._fetch_model_ids()
            if fetched:
                # De-duplicate, keep order
                ids = list(dict.fromkeys(m for m in fetched if m))
                log.debug("%s: discovered %d model(s)", self.provider_name, len(ids))
        except Exception as e:
            log.info("%s: model discovery failed (%s); using built-in list.",
                     self.provider_name, e)
            ids = None

        with _MODEL_CACHE_LOCK:
            _MODEL_CACHE[key] = (now, ids)
        return list(ids) if ids else None

    def discover_models(self) -> list[str]:
        """Live model list if discoverable, else the hardcoded fallback."""
        live = self._live_models()
        return live if live else list(self._fallback_models())

    def resolve_model(self, requested: str | None) -> str | None:
        """Validate a requested/configured model against the live list.

        If the live list is known and the model isn't in it (retired ID in
        an old settings.json, or a model meant for another provider), log a
        warning and substitute the provider default -- or, if that is gone
        too, the first built-in fallback still live, else the first live
        model.  When discovery is unavailable the request passes through.
        """
        model = requested or self.default_model
        if not model:
            return model
        aliases = self._model_aliases()
        if model in aliases:
            return model
        live = self._live_models()
        if not live or model in live:
            return model

        live_set = set(live)
        candidates = [self.default_model, *self._fallback_models()]
        replacement = next(
            (c for c in candidates if c and (c in live_set or c in aliases)),
            live[0],
        )
        log.warning("%s: model '%s' is not offered by the provider any more; "
                    "falling back to '%s'.", self.provider_name, model, replacement)
        return replacement
