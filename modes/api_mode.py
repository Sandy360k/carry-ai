"""
carry-ai/modes/api_mode.py — Cloud API Router
===============================================

Routes LLM requests to cloud providers based on user configuration.
Handles provider instantiation from decrypted keys, request routing,
health tracking, retries, and transparent fallback between providers.

Supported Providers:
    - Anthropic (Claude) — API key auth
    - Google Gemini — OAuth2 or API key
    - OpenAI (GPT) — API key auth
    - Groq — API key auth (free tier, fast inference)
    - OpenRouter — API key auth (unified gateway to all models)

Model selection:
    Each provider gets its own model on every attempt: the caller's
    ``model`` kwarg is only sent to the first provider in the fallback
    chain; fallbacks use ``settings.providers.<name>.model`` (or the
    provider default).  Providers validate the ID against their live
    model list (see providers/base.py) and fall back to their default if
    it has been retired, so a stale settings.json never breaks chat.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from providers.base import (
    BaseProvider,
    ChatResponse,
    ProviderError,
    RateLimitError,
    AuthenticationError,
    OverloadedError,
    clear_model_cache,
)

log = logging.getLogger("carry-ai.modes.api")

# Provider preference order for fallback chain (fastest / cheapest first for fallback)
DEFAULT_FALLBACK_ORDER = ["groq", "openrouter", "anthropic", "openai", "google", "godmode", "onyx"]

# Max retries per provider before falling back to next
MAX_RETRIES_PER_PROVIDER = 2

# Delay between retries (seconds)
BASE_RETRY_DELAY = 1.0


# ===================================================================
# Provider health tracking
# ===================================================================

@dataclass
class ProviderHealth:
    """Tracks health metrics for a single provider."""
    provider_name: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rate_limited_count: int = 0
    auth_errors: int = 0
    last_error: str | None = None
    last_error_time: float | None = None
    last_success_time: float | None = None
    avg_latency_ms: float = 0.0
    _latencies: list[float] = field(default_factory=list, repr=False)

    def record_success(self, latency_ms: float) -> None:
        self.total_requests += 1
        self.successful_requests += 1
        self.last_success_time = time.monotonic()
        # A successful call proves credentials work again — clear the
        # auth-error flag so a past transient 401/403 doesn't disable
        # the provider permanently.
        self.auth_errors = 0
        self._latencies.append(latency_ms)
        # Rolling average over last 20 requests
        if len(self._latencies) > 20:
            self._latencies = self._latencies[-20:]
        self.avg_latency_ms = sum(self._latencies) / len(self._latencies)

    def record_failure(self, error: str, is_rate_limit: bool = False,
                       is_auth: bool = False) -> None:
        self.total_requests += 1
        self.failed_requests += 1
        self.last_error = error
        self.last_error_time = time.monotonic()
        if is_rate_limit:
            self.rate_limited_count += 1
        if is_auth:
            self.auth_errors += 1

    @property
    def is_healthy(self) -> bool:
        """Provider is considered healthy if it has no recent auth errors
        and hasn't been rate-limited excessively."""
        if self.auth_errors > 0:
            return False
        # Unhealthy if more than 5 rate limits with no successes
        if self.rate_limited_count > 5 and self.successful_requests == 0:
            return False
        return True

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return self.successful_requests / self.total_requests

    def to_dict(self) -> dict:
        return {
            "provider": self.provider_name,
            "healthy": self.is_healthy,
            "total_requests": self.total_requests,
            "success_rate": round(self.success_rate, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "rate_limited": self.rate_limited_count,
            "last_error": self.last_error,
        }


# ===================================================================
# Provider factory
# ===================================================================

# Map of provider name -> (module_path, class_name)
PROVIDER_REGISTRY = {
    "anthropic":  ("providers.anthropic_provider", "AnthropicProvider"),
    "google":     ("providers.google_oauth", "GoogleProvider"),
    "openai":     ("providers.openai_provider", "OpenAIProvider"),
    "groq":       ("providers.groq_provider", "GroqProvider"),
    "openrouter": ("providers.openrouter_provider", "OpenRouterProvider"),
    "godmode":    ("providers.godmode_provider", "GodmodeProvider"),
    "onyx":       ("providers.onyx_provider", "OnyxProvider"),
}


def _instantiate_provider(name: str, keys: dict) -> BaseProvider | None:
    """Dynamically import and instantiate a provider from decrypted keys.

    Args:
        name: Provider name (e.g. 'anthropic').
        keys: Decrypted key dict for this provider (e.g. {"api_key": "sk-..."}).

    Returns:
        Provider instance, or None if import/init fails.
    """
    if name not in PROVIDER_REGISTRY:
        log.warning("Unknown provider: %s", name)
        return None

    module_path, class_name = PROVIDER_REGISTRY[name]

    try:
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
    except (ImportError, AttributeError) as e:
        log.error("Cannot load provider %s: %s", name, e)
        return None

    try:
        # Pass the keys dict — each provider unpacks what it needs
        api_key = keys.get("api_key", "")
        if name == "google":
            return cls(
                api_key=api_key,
                oauth_credentials=keys.get("oauth_refresh_token"),
            )
        elif name in ("openai", "openrouter", "groq", "godmode"):
            base_url = keys.get("base_url")
            return cls(api_key=api_key, base_url=base_url) if base_url else cls(api_key=api_key)
        elif name == "onyx":
            base_url = keys.get("base_url", "http://localhost:3000/api")
            return cls(api_key=api_key, base_url=base_url)
        else:
            return cls(api_key=api_key)
    except NotImplementedError:
        log.info("Provider %s not yet implemented.", name)
        return None
    except Exception as e:
        log.error("Failed to instantiate provider %s: %s", name, e)
        return None


def load_configured_models() -> dict[str, str]:
    """Read ``settings.providers.<name>.model`` for every provider.

    Returns an empty dict if settings can't be loaded.
    """
    try:
        from config.settings import load_settings
        providers_cfg = load_settings().to_dict().get("providers") or {}
    except Exception as e:
        log.debug("Cannot load provider model settings: %s", e)
        return {}
    return {
        name: cfg["model"]
        for name, cfg in providers_cfg.items()
        if isinstance(cfg, dict) and isinstance(cfg.get("model"), str) and cfg["model"]
    }


def load_providers(decrypted_keys: dict) -> dict[str, BaseProvider]:
    """Instantiate all providers for which we have decrypted keys.

    Args:
        decrypted_keys: Dict from keystore, e.g.:
            {"anthropic": {"api_key": "..."}, "groq": {"api_key": "..."}, ...}

    Returns:
        Dict of provider_name -> provider_instance (only successfully loaded ones).
    """
    providers = {}
    # Peripheral providers are opt-in (see config experimental.*); skip
    # them even if a key happens to be present, unless explicitly enabled.
    experimental = {"godmode", "onyx"}
    # Credentials kept in the same keystore that are not chat providers.
    not_providers = {"huggingface", "assemblyai", "elevenlabs"}

    for name, keys in decrypted_keys.items():
        if name.startswith("_") or name in not_providers:
            continue  # Internal keys (_dry_run) and non-LLM credentials
        if not isinstance(keys, dict):
            log.debug("Skipping non-dict key entry: %s", name)
            continue
        if name in experimental:
            try:
                from config.settings import is_experimental_enabled
            except ImportError:
                is_experimental_enabled = lambda *_a, **_k: False  # noqa: E731
            if not is_experimental_enabled(name):
                log.info("Provider '%s' is experimental and disabled; skipping. "
                         "Enable with experimental.%s in settings.json.", name, name)
                continue

        provider = _instantiate_provider(name, keys)
        if provider is not None:
            providers[name] = provider
            log.info("Loaded provider: %s", name)

    if not providers:
        log.warning("No providers loaded. API mode will not function.")

    return providers


# ===================================================================
# APIRouter — main public interface
# ===================================================================

class APIRouter:
    """Routes chat requests to configured cloud LLM providers.

    Usage:
        router = APIRouter(decrypted_keys={"anthropic": {"api_key": "sk-..."}})
        router.start()
        response = router.chat(messages, provider="anthropic")
        router.shutdown()
    """

    def __init__(self, decrypted_keys: dict | None = None,
                 primary_provider: str | None = None,
                 fallback_order: list[str] | None = None,
                 provider_models: dict[str, str] | None = None):
        """
        Args:
            decrypted_keys: Decrypted API keys dict from keystore.
            primary_provider: Preferred provider name. First available if None.
            fallback_order: Provider names in fallback priority order.
            provider_models: provider_name -> model ID.  Loaded from
                settings (providers.<name>.model) when None.
        """
        self._decrypted_keys = decrypted_keys or {}
        self._primary_provider_name = primary_provider
        self._fallback_order = fallback_order or DEFAULT_FALLBACK_ORDER
        self._provider_models = provider_models

        self._providers: dict[str, BaseProvider] = {}
        self._health: dict[str, ProviderHealth] = {}
        self._started = False

    def start(self) -> None:
        """Load and validate all providers."""
        self._providers = load_providers(self._decrypted_keys)
        if self._provider_models is None:
            self._provider_models = load_configured_models()

        # Initialize health trackers
        for name in self._providers:
            self._health[name] = ProviderHealth(provider_name=name)

        # Resolve primary provider
        if self._primary_provider_name and self._primary_provider_name in self._providers:
            log.info("Primary provider: %s", self._primary_provider_name)
        elif self._providers:
            # Pick first available from fallback order
            for name in self._fallback_order:
                if name in self._providers:
                    self._primary_provider_name = name
                    break
            if not self._primary_provider_name:
                self._primary_provider_name = next(iter(self._providers))
            log.info("Auto-selected primary provider: %s", self._primary_provider_name)
        else:
            log.warning("No providers available.")

        self._started = True
        log.info("API router started with %d provider(s): %s",
                 len(self._providers), ", ".join(self._providers.keys()))

    @property
    def primary_provider(self) -> str | None:
        return self._primary_provider_name

    @property
    def available_providers(self) -> list[str]:
        return list(self._providers.keys())

    def _get_provider(self, name: str | None) -> tuple[str, BaseProvider]:
        """Resolve a provider by name, falling back to primary.

        Raises ProviderError if no provider is available.
        """
        if not self._providers:
            raise ProviderError("No providers loaded. Configure API keys first.")

        if name and name in self._providers:
            return name, self._providers[name]

        if self._primary_provider_name and self._primary_provider_name in self._providers:
            return self._primary_provider_name, self._providers[self._primary_provider_name]

        # Fall back to first available
        name = next(iter(self._providers))
        return name, self._providers[name]

    def _build_fallback_chain(self, preferred: str | None) -> list[str]:
        """Build ordered list of providers to try.

        Starts with preferred, then follows fallback_order, skipping
        unhealthy providers.
        """
        chain = []
        seen = set()

        # Preferred first
        if preferred and preferred in self._providers:
            chain.append(preferred)
            seen.add(preferred)

        # Then primary
        if self._primary_provider_name and self._primary_provider_name not in seen:
            if self._primary_provider_name in self._providers:
                chain.append(self._primary_provider_name)
                seen.add(self._primary_provider_name)

        # Then fallback order
        for name in self._fallback_order:
            if name not in seen and name in self._providers:
                chain.append(name)
                seen.add(name)

        # Any remaining providers
        for name in self._providers:
            if name not in seen:
                chain.append(name)

        return chain

    def _kwargs_for(self, provider_name: str, kwargs: dict, first: bool) -> dict:
        """Per-provider kwargs: a caller-supplied model only applies to the
        first provider tried; others use their configured model/default."""
        out = dict(kwargs)
        if first and out.get("model"):
            return out
        configured = (self._provider_models or {}).get(provider_name)
        if configured:
            out["model"] = configured
        else:
            out.pop("model", None)
        return out

    def chat(self, messages: list[dict], provider: str | None = None,
             **kwargs) -> ChatResponse:
        """Send a chat completion request with automatic fallback.

        Tries the requested provider first. On failure, falls back through
        the chain. Rate limit errors trigger retry with backoff before
        moving to the next provider.

        Args:
            messages: OpenAI-format message list.
            provider: Specific provider to use (falls back if it fails).
            **kwargs: model, temperature, max_tokens, tools, etc.

        Returns:
            ChatResponse from whichever provider succeeded.

        Raises:
            ProviderError: If all providers in the chain fail.
        """
        if not self._started:
            raise ProviderError("APIRouter not started. Call start() first.")

        chain = self._build_fallback_chain(provider)
        if not chain:
            raise ProviderError("No providers available.")

        last_error = None

        for provider_name in chain:
            prov = self._providers[provider_name]
            health = self._health[provider_name]
            call_kwargs = self._kwargs_for(provider_name, kwargs,
                                           first=provider_name == chain[0])

            for attempt in range(MAX_RETRIES_PER_PROVIDER):
                try:
                    t0 = time.monotonic()
                    response = prov.chat(messages, **call_kwargs)
                    latency = (time.monotonic() - t0) * 1000

                    health.record_success(latency)
                    log.debug("Chat success: %s (%.0fms)", provider_name, latency)
                    return response

                except RateLimitError as e:
                    health.record_failure(str(e), is_rate_limit=True)
                    delay = e.retry_after if e.retry_after else BASE_RETRY_DELAY * (2 ** attempt)
                    log.warning("Rate limited by %s (attempt %d/%d). Retrying in %.1fs.",
                                provider_name, attempt + 1, MAX_RETRIES_PER_PROVIDER, delay)
                    time.sleep(delay)
                    last_error = e

                except AuthenticationError as e:
                    health.record_failure(str(e), is_auth=True)
                    log.error("Auth error from %s: %s. Skipping provider.", provider_name, e)
                    last_error = e
                    break  # Don't retry auth errors — skip to next provider

                except OverloadedError as e:
                    health.record_failure(str(e))
                    log.warning("Provider %s overloaded. Trying next.", provider_name)
                    last_error = e
                    break  # Skip to next provider

                except ProviderError as e:
                    health.record_failure(str(e))
                    log.warning("Provider %s error: %s (attempt %d/%d)",
                                provider_name, e, attempt + 1, MAX_RETRIES_PER_PROVIDER)
                    last_error = e

                except Exception as e:
                    health.record_failure(str(e))
                    log.error("Unexpected error from %s: %s", provider_name, e, exc_info=True)
                    last_error = ProviderError(str(e), provider=provider_name)
                    break  # Unknown error — skip to next provider

        raise ProviderError(
            f"All providers failed. Last error: {last_error}",
            provider=chain[-1] if chain else "",
        )

    def stream(self, messages: list[dict], provider: str | None = None, **kwargs):
        """Stream a chat completion with fallback (non-streaming fallback on error).

        Unlike chat(), streaming doesn't retry mid-stream — it tries each provider
        once for streaming and falls back on connection errors.

        Yields dicts: {"type": "content"|"tool_call"|"done", "data": ...}
        """
        if not self._started:
            raise ProviderError("APIRouter not started. Call start() first.")

        chain = self._build_fallback_chain(provider)
        last_error = None

        for provider_name in chain:
            prov = self._providers[provider_name]
            health = self._health[provider_name]

            call_kwargs = self._kwargs_for(provider_name, kwargs,
                                           first=provider_name == chain[0])
            try:
                t0 = time.monotonic()
                for chunk in prov.stream(messages, **call_kwargs):
                    yield chunk
                latency = (time.monotonic() - t0) * 1000
                health.record_success(latency)
                return  # Stream completed successfully

            except AuthenticationError as e:
                health.record_failure(str(e), is_auth=True)
                log.error("Auth error from %s during stream. Trying next.", provider_name)
                last_error = e

            except (RateLimitError, OverloadedError) as e:
                health.record_failure(str(e), is_rate_limit=isinstance(e, RateLimitError))
                log.warning("Provider %s unavailable for stream: %s. Trying next.", provider_name, e)
                last_error = e

            except Exception as e:
                health.record_failure(str(e))
                log.warning("Stream error from %s: %s. Trying next.", provider_name, e)
                last_error = ProviderError(str(e), provider=provider_name)

        raise ProviderError(
            f"All providers failed to stream. Last error: {last_error}",
            provider=chain[-1] if chain else "",
        )

    def set_primary(self, provider_name: str) -> None:
        """Switch the primary provider."""
        if provider_name not in self._providers:
            raise ProviderError(f"Provider '{provider_name}' not loaded.")
        self._primary_provider_name = provider_name
        log.info("Primary provider changed to: %s", provider_name)

    def health_check(self) -> dict[str, dict]:
        """Return health status for all loaded providers."""
        result = {}
        for name, health in self._health.items():
            info = health.to_dict()
            # Also check is_available if provider supports it
            try:
                info["reachable"] = self._providers[name].is_available()
            except Exception:
                info["reachable"] = False
            result[name] = info
        return result

    def list_models(self, provider_name: str | None = None) -> dict[str, list[str]]:
        """List available models, optionally filtered by provider.

        Returns:
            Dict of provider_name -> [model_id, ...]
        """
        result = {}
        targets = [provider_name] if provider_name else self._providers.keys()
        for name in targets:
            if name not in self._providers:
                continue
            try:
                result[name] = self._providers[name].models()
            except Exception as e:
                log.debug("Cannot list models for %s: %s", name, e)
                result[name] = []
        return result

    def shutdown(self) -> None:
        """Release all provider resources and zero sensitive state."""
        for name, prov in self._providers.items():
            # Providers may hold API keys in memory — help GC
            try:
                if hasattr(prov, "shutdown"):
                    prov.shutdown()
            except Exception:
                pass

        self._providers.clear()
        self._health.clear()
        clear_model_cache()  # in-memory only, but drop it with the keys

        # Zero the keys reference
        if self._decrypted_keys:
            for k in list(self._decrypted_keys.keys()):
                self._decrypted_keys[k] = None
            self._decrypted_keys = {}

        self._started = False
        log.info("API router shut down. Keys zeroed.")

    def get_status(self) -> dict:
        """Return status summary for the UI."""
        return {
            "started": self._started,
            "primary_provider": self._primary_provider_name,
            "providers": list(self._providers.keys()),
            "health": {name: h.to_dict() for name, h in self._health.items()},
        }

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False
