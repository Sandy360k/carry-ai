"""
carry-ai/providers/openrouter_provider.py — OpenRouter Provider
=================================================================

Implements BaseProvider for OpenRouter's unified gateway API.
Uses the shared OpenAI-compatible base class.

Features:
    - Unified access to hundreds of models from all major providers
    - Automatic model routing (``openrouter/auto``) and a free-model
      router (``openrouter/free``, the default -- works on a new key)
    - Runtime model discovery from the public model list (no key needed),
      skipping ``:batch`` variants and models that expire within 14 days
    - Custom HTTP-Referer and X-Title headers for tracking

API Endpoints:
    POST https://openrouter.ai/api/v1/chat/completions
    GET  https://openrouter.ai/api/v1/models   (public)
"""

import logging
from datetime import date, datetime, timedelta, timezone

from providers.base import DISCOVERY_TIMEOUT
from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.openrouter")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openrouter/free"

# Offline fallback only -- the live list comes from GET /models.
AVAILABLE_MODELS = [
    "openrouter/free",              # auto-routes to free models
    "google/gemma-4-31b-it:free",   # strong free model
    "openrouter/auto",              # auto-routes across paid models
    "openai/gpt-6-luna",
    "google/gemini-3.8-flash",
    "anthropic/claude-opus-5.5",
]

# Router pseudo-models: always accepted even if absent from /models
ROUTER_ALIASES = {"openrouter/free", "openrouter/auto"}

# Skip models retiring within this window
EXPIRY_GRACE_DAYS = 14


def _parse_date(value) -> date | None:
    """Parse an OpenRouter expiration_date (ISO date/datetime or epoch)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc).date()
        text = str(value).strip().replace("Z", "+00:00")
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text).date()
    except (ValueError, OSError, OverflowError):
        return None


def filter_openrouter_models(records: list[dict], today: date | None = None) -> list[str]:
    """Return usable chat model IDs from an OpenRouter /models payload.

    Drops ``:batch`` variants and models whose ``expiration_date`` is in
    the past or within EXPIRY_GRACE_DAYS of today.
    """
    today = today or datetime.now(timezone.utc).date()
    cutoff = today + timedelta(days=EXPIRY_GRACE_DAYS)
    ids: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        model_id = rec.get("id")
        if not model_id or model_id.endswith(":batch"):
            continue
        expires = _parse_date(rec.get("expiration_date"))
        if expires is not None and expires <= cutoff:
            continue
        arch = rec.get("architecture") or {}
        out_mods = arch.get("output_modalities")
        if out_mods and "text" not in out_mods:
            continue  # image/audio-only generators
        ids.append(model_id)
    return ids


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

    def _model_aliases(self) -> set[str]:
        return set(ROUTER_ALIASES)

    def _fetch_model_ids(self) -> list[str] | None:
        """Public model list (no key required) with expiry / :batch filtering."""
        resp = self._session.get(
            f"{self._base_url}/models",
            headers=self._extra_headers(),
            timeout=DISCOVERY_TIMEOUT,
        )
        resp.raise_for_status()
        return filter_openrouter_models(resp.json().get("data", []))

    def is_available(self) -> bool:
        """Check key format."""
        if not self._api_key:
            return False
        # OpenRouter keys start with sk-or-
        if not self._api_key.startswith("sk-or-"):
            return False
        return super().is_available()
