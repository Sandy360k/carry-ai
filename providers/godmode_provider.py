"""
carry-ai/providers/godmode_provider.py -- G0DM0D3 Provider
===========================================================

Integrates with G0DM0D3 (https://github.com/elder-plinius/G0DM0D3) --
a multi-model AI gateway that races queries across 50+ models and
picks the best response.

Features (from G0DM0D3):
    - ULTRAPLINIAN: Race N models in parallel, score & pick the best
    - CONSORTIUM: Collect all responses, synthesize ground truth
    - AutoTune: Auto-detect query context and optimize sampling params
    - STM (Semantic Transformation Modules): Post-process output
      (hedge_reducer, direct_mode, casual_mode)
    - Parseltongue: Input perturbation for red-team robustness testing

Architecture:
    G0DM0D3 exposes an OpenAI-compatible /v1/chat/completions endpoint,
    so this provider extends OpenAICompatProvider with G0DM0D3-specific
    extension fields in the request body.

Setup:
    1. Self-host G0DM0D3 API (Docker or HuggingFace Space):
       docker run -p 3000:3000 -e OPENROUTER_API_KEY=... godmode
    2. Or use a public instance URL
    3. Store the endpoint URL + API key in carry-ai's keystore:
       python crypto/keystore.py add godmode

Reference: https://github.com/elder-plinius/G0DM0D3
"""

import logging

from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.godmode")

# Default G0DM0D3 instance (self-hosted)
GODMODE_BASE_URL = "http://localhost:3000/v1"

# Virtual model names that trigger different racing strategies
AVAILABLE_MODELS = [
    # Standard models (routed via OpenRouter backend)
    "anthropic/claude-opus-5.5",
    "openai/gpt-6-luna",
    "google/gemini-3.8-flash",
    "google/gemma-4-31b-it:free",
    "openrouter/auto",
    # ULTRAPLINIAN racing tiers (race N models, pick best)
    "ultraplinian/fast",           # Race 10 fast models
    "ultraplinian/smart",          # Race 20 quality models
    "ultraplinian/all",            # Race all 51 models
    # CONSORTIUM synthesis (collect all, synthesize answer)
    "consortium/default",          # Synthesize from top 10
    "consortium/deep",             # Synthesize from top 20
]

DEFAULT_MODEL = "anthropic/claude-opus-5.5"

# AutoTune context categories (detected automatically)
AUTOTUNE_CATEGORIES = [
    "code", "creative", "analytical", "conversational",
    "mathematical", "factual", "instruction", "translation",
]

# STM module names (post-processing filters)
STM_MODULES = [
    "hedge_reducer",    # Removes "I think", "perhaps", "probably"
    "direct_mode",      # Strips filler phrases, gets to the point
    "casual_mode",      # Relaxes formal tone
    "concise_mode",     # Shortens verbose responses
]


class GodmodeProvider(OpenAICompatProvider):
    """G0DM0D3 multi-model racing provider.

    Extends OpenAI-compatible base with G0DM0D3's extension fields:
    - autotune: Auto-optimize sampling parameters per query context
    - stm_modules: Post-processing filters on model output
    - race_count: Number of models to race (ULTRAPLINIAN)
    - synthesis: Enable CONSORTIUM synthesis mode

    Usage:
        provider = GodmodeProvider(
            api_key="sk-or-...",
            base_url="http://localhost:3000/v1",
        )
        # Standard request
        response = provider.chat(messages)

        # ULTRAPLINIAN: Race 10 models
        response = provider.chat(messages, model="ultraplinian/fast")

        # With AutoTune + STM post-processing
        response = provider.chat(messages, autotune=True, stm_modules=["direct_mode"])
    """

    def __init__(self, api_key: str, base_url: str | None = None,
                 autotune: bool = False, stm_modules: list[str] | None = None):
        super().__init__(
            api_key=api_key,
            base_url=base_url or GODMODE_BASE_URL,
        )
        self._default_model = DEFAULT_MODEL
        self._available_models = AVAILABLE_MODELS
        # Virtual racing/synthesis model names aren't in any /models list
        self._discover_models = False
        self._autotune = autotune
        self._stm_modules = stm_modules or []

    @property
    def provider_name(self) -> str:
        return "godmode"

    def _extra_headers(self) -> dict:
        return {
            "HTTP-Referer": "https://github.com/carry-ai",
            "X-Title": "carry-ai (G0DM0D3)",
        }

    def _build_payload(self, messages: list[dict], **kwargs) -> dict:
        """Build payload with G0DM0D3 extension fields."""
        # Extract G0DM0D3-specific params before passing to base
        autotune = kwargs.pop("autotune", self._autotune)
        stm_modules = kwargs.pop("stm_modules", self._stm_modules)
        race_count = kwargs.pop("race_count", None)
        synthesis = kwargs.pop("synthesis", None)

        payload = super()._build_payload(messages, **kwargs)

        # G0DM0D3 extensions (sent as extra fields in the request body)
        if autotune:
            payload["x_godmode_autotune"] = True

        if stm_modules:
            valid = [m for m in stm_modules if m in STM_MODULES]
            if valid:
                payload["x_godmode_stm"] = valid

        if race_count and isinstance(race_count, int) and race_count > 1:
            payload["x_godmode_race_count"] = race_count

        if synthesis:
            payload["x_godmode_synthesis"] = True

        # Auto-detect ULTRAPLINIAN/CONSORTIUM from model name
        model = payload.get("model", "")
        if model.startswith("ultraplinian/"):
            tier_map = {"fast": 10, "smart": 20, "all": 51}
            tier = model.split("/")[-1]
            payload["x_godmode_race_count"] = tier_map.get(tier, 10)
            # Override to a concrete model for the backend
            payload["model"] = "openrouter/auto"
        elif model.startswith("consortium/"):
            payload["x_godmode_synthesis"] = True
            tier_map = {"default": 10, "deep": 20}
            tier = model.split("/")[-1]
            payload["x_godmode_race_count"] = tier_map.get(tier, 10)
            payload["model"] = "openrouter/auto"

        return payload

    def is_available(self) -> bool:
        """Check if the G0DM0D3 instance is reachable."""
        if not self._api_key:
            return False
        try:
            import requests
            resp = requests.get(
                f"{self._base_url.rstrip('/v1')}/health",
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            # Fall back to standard models endpoint check
            return super().is_available()

    def race(self, messages: list[dict], count: int = 10, **kwargs):
        """Convenience: ULTRAPLINIAN race with explicit count.

        Args:
            messages: Chat messages.
            count: Number of models to race (default 10).

        Returns:
            ChatResponse from the winning model.
        """
        return self.chat(messages, race_count=count, **kwargs)

    def synthesize(self, messages: list[dict], count: int = 10, **kwargs):
        """Convenience: CONSORTIUM synthesis.

        Args:
            messages: Chat messages.
            count: Number of models to collect from.

        Returns:
            ChatResponse with synthesized answer.
        """
        return self.chat(messages, synthesis=True, race_count=count, **kwargs)

    def autotune_chat(self, messages: list[dict], **kwargs):
        """Convenience: Chat with AutoTune enabled.

        AutoTune detects the query context (code, creative, analytical, etc.)
        and auto-optimizes temperature, top_p, frequency_penalty, etc.
        """
        return self.chat(messages, autotune=True, **kwargs)
