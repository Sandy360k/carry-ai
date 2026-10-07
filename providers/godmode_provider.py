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
    so this provider extends OpenAICompatProvider and adds G0DM0D3's
    extension keys as plain top-level body fields (see API.md):
        godmode, parseltongue, autotune   (bool; all default TRUE server-side)
        stm_modules                       (hedge_reducer | direct_mode | casual_mode)
        openrouter_api_key                (unless the server has OPENROUTER_API_KEY)
    Virtual models "ultraplinian/<tier>" and "consortium/<tier>" (tiers
    fast | standard | smart | power | ultra) are passed through unchanged;
    the server does the racing/synthesis.

    Safety: server-side `godmode` injects a jailbreak system prompt and
    `parseltongue` obfuscates the user's input. carry-ai always sends both as
    false unless settings.providers.godmode.godmode / .parseltongue opt in,
    and sends autotune/stm_modules from settings (default off / none).

Setup:
    1. Self-host the G0DM0D3 API (Docker or HuggingFace Space, port 7860):
       docker run -p 7860:7860 -e OPENROUTER_API_KEY=... g0dm0d3-api
    2. Or use a public instance URL
    3. Store the endpoint URL + API key in carry-ai's keystore:
       python crypto/keystore.py add godmode

Reference: https://github.com/elder-plinius/G0DM0D3 (API.md, api/server.ts)
"""

import logging

from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.godmode")

# Default G0DM0D3 instance (self-hosted API server; HF Spaces port)
GODMODE_BASE_URL = "http://localhost:7860/v1"

RACE_TIERS = ["fast", "standard", "smart", "power", "ultra"]

# Virtual model names that trigger different racing strategies
AVAILABLE_MODELS = [
    # Standard models (routed via OpenRouter backend)
    "anthropic/claude-opus-5.5",
    "openai/gpt-6-luna",
    "google/gemini-3.8-flash",
    "google/gemma-4-31b-it:free",
    "openrouter/auto",
    # ULTRAPLINIAN: race N models, return the best (10/24/36/45/51 models)
    *(f"ultraplinian/{t}" for t in RACE_TIERS),
    # CONSORTIUM: collect all responses, synthesize ground truth
    *(f"consortium/{t}" for t in RACE_TIERS),
]

DEFAULT_MODEL = "anthropic/claude-opus-5.5"

# AutoTune context categories (detected automatically)
AUTOTUNE_CATEGORIES = [
    "code", "creative", "analytical", "conversational",
    "mathematical", "factual", "instruction", "translation",
]

# STM module names accepted by the server (post-processing filters)
STM_MODULES = [
    "hedge_reducer",    # Removes "I think", "perhaps", "probably"
    "direct_mode",      # Strips filler phrases, gets to the point
    "casual_mode",      # Relaxes formal tone
]


def _godmode_settings() -> dict:
    """settings.providers.godmode, or {} if settings can't be loaded."""
    try:
        from config.settings import load_settings
        cfg = (load_settings().to_dict().get("providers") or {}).get("godmode")
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        log.debug("Cannot load godmode settings: %s", e)
        return {}


class GodmodeProvider(OpenAICompatProvider):
    """G0DM0D3 multi-model racing provider.

    Usage:
        provider = GodmodeProvider(
            api_key="godmode-api-key",
            base_url="http://localhost:7860/v1",
            openrouter_api_key="sk-or-...",
        )
        response = provider.chat(messages)                          # single model
        response = provider.chat(messages, model="ultraplinian/fast")  # race 10
        response = provider.chat(messages, model="consortium/smart")   # synthesize
        response = provider.chat(messages, autotune=True, stm_modules=["direct_mode"])
    """

    def __init__(self, api_key: str, base_url: str | None = None,
                 autotune: bool | None = None, stm_modules: list[str] | None = None,
                 openrouter_api_key: str | None = None,
                 godmode: bool | None = None, parseltongue: bool | None = None):
        super().__init__(
            api_key=api_key,
            base_url=base_url or GODMODE_BASE_URL,
        )
        self._default_model = DEFAULT_MODEL
        self._available_models = AVAILABLE_MODELS
        # Virtual racing/synthesis model names aren't in any /models list
        self._discover_models = False
        cfg = _godmode_settings() if None in (autotune, stm_modules, godmode, parseltongue) else {}
        self._autotune = bool(cfg.get("autotune", False)) if autotune is None else bool(autotune)
        self._stm_modules = list(cfg.get("stm_modules") or []) if stm_modules is None else list(stm_modules)
        # Off unless the user explicitly opts in: these inject a jailbreak
        # prompt / perturb the input and default to true on the server.
        self._godmode = cfg.get("godmode") is True if godmode is None else bool(godmode)
        self._parseltongue = (cfg.get("parseltongue") is True
                              if parseltongue is None else bool(parseltongue))
        # Older carry-ai docs stored the OpenRouter key as the godmode api_key.
        if openrouter_api_key is None and api_key.startswith("sk-or-"):
            openrouter_api_key = api_key
        self._openrouter_api_key = openrouter_api_key or ""

    @property
    def provider_name(self) -> str:
        return "godmode"

    def _extra_headers(self) -> dict:
        return {
            "HTTP-Referer": "https://github.com/carry-ai",
            "X-Title": "carry-ai (G0DM0D3)",
        }

    def _build_payload(self, messages: list[dict], **kwargs) -> dict:
        """Build payload with G0DM0D3 extension fields (plain body keys)."""
        autotune = kwargs.pop("autotune", self._autotune)
        stm_modules = kwargs.pop("stm_modules", self._stm_modules)
        godmode = kwargs.pop("godmode", self._godmode)
        parseltongue = kwargs.pop("parseltongue", self._parseltongue)

        payload = super()._build_payload(messages, **kwargs)

        # Sent explicitly every time: the server defaults all of these to on.
        payload["godmode"] = bool(godmode)
        payload["parseltongue"] = bool(parseltongue)
        payload["autotune"] = bool(autotune)
        payload["stm_modules"] = [m for m in (stm_modules or []) if m in STM_MODULES]
        if self._openrouter_api_key:
            payload["openrouter_api_key"] = self._openrouter_api_key
        return payload

    def is_available(self) -> bool:
        """Check if the G0DM0D3 instance is reachable (GET /v1/health, no auth)."""
        try:
            resp = self._session.get(f"{self._base_url}/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def race(self, messages: list[dict], tier: str = "fast", **kwargs):
        """Convenience: ULTRAPLINIAN race at *tier* (fast|standard|smart|power|ultra).

        Returns:
            ChatResponse from the winning model.
        """
        tier = tier if tier in RACE_TIERS else "fast"
        return self.chat(messages, model=f"ultraplinian/{tier}", **kwargs)

    def synthesize(self, messages: list[dict], tier: str = "fast", **kwargs):
        """Convenience: CONSORTIUM synthesis at *tier*.

        Returns:
            ChatResponse with synthesized answer.
        """
        tier = tier if tier in RACE_TIERS else "fast"
        return self.chat(messages, model=f"consortium/{tier}", **kwargs)

    def autotune_chat(self, messages: list[dict], **kwargs):
        """Convenience: Chat with AutoTune enabled.

        AutoTune detects the query context (code, creative, analytical, etc.)
        and auto-optimizes temperature, top_p, frequency_penalty, etc.
        """
        return self.chat(messages, autotune=True, **kwargs)
