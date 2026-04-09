"""
carry-ai/providers/localai_provider.py — LocalAI Provider
==========================================================

Integrates carry-ai with a locally-running LocalAI server
(https://github.com/mudler/LocalAI).

LocalAI is an OpenAI-compatible inference server that runs GGUF models
(and many other formats) on CPU or GPU.  It is a single binary — no
Python dependencies, no Docker required for USB use.

Usage:
    provider = LocalAIProvider(host="127.0.0.1", port=8081)
    if provider.is_available():
        response = provider.chat([{"role": "user", "content": "Hello"}])

The LocalAI server is started and managed by modes/local_mode.LocalAIRunner,
not by this class.  This provider only handles HTTP communication.
"""

import logging
import time

try:
    import requests as _requests
except ImportError:
    _requests = None

from providers.base import ChatResponse, ProviderError
from providers.openai_compat import OpenAICompatProvider

log = logging.getLogger("carry-ai.providers.localai")

# Endpoints LocalAI exposes for health checks
_HEALTH_ENDPOINTS = ("/readyz", "/healthz", "/v1/models")


class LocalAIProvider(OpenAICompatProvider):
    """
    Provider that communicates with a locally running LocalAI server.

    LocalAI exposes a full OpenAI-compatible REST API at
    http://host:port/v1/, so we inherit all HTTP logic from
    OpenAICompatProvider and only override identity + availability checks.

    No API key is required (a placeholder string is used internally to
    satisfy the parent class).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8081,
                 model: str | None = None):
        """
        Args:
            host: LocalAI server host (default 127.0.0.1).
            port: LocalAI server port (default 8081).
            model: Model name to use for requests.  If None, carry-ai
                   will attempt to discover available models via /v1/models.
        """
        # LocalAI doesn't need a real API key — use a placeholder
        super().__init__(api_key="localai-no-key",
                         base_url=f"http://{host}:{port}/v1")
        self._host = host
        self._port = port
        self._forced_model = model
        self._default_model = model or "auto"
        self._cached_models: list[str] = []
        self._last_model_fetch: float = 0.0

    # ── Identity ────────────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "localai"

    # ── Auth header ─────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        """LocalAI doesn't require Authorization; send minimal headers."""
        return {"Content-Type": "application/json"}

    # ── Availability ────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True if the LocalAI server is reachable and ready."""
        if _requests is None:
            return False
        for endpoint in _HEALTH_ENDPOINTS:
            try:
                resp = _requests.get(
                    f"http://{self._host}:{self._port}{endpoint}",
                    timeout=3,
                )
                if resp.status_code in (200, 206):
                    return True
            except Exception:
                continue
        return False

    def wait_until_ready(self, timeout: float = 120.0,
                         poll_interval: float = 1.0) -> bool:
        """Block until LocalAI becomes ready or timeout expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_available():
                return True
            time.sleep(poll_interval)
        return False

    # ── Model discovery ─────────────────────────────────────────────────────

    def models(self) -> list[str]:
        """
        Return available models by querying GET /v1/models.
        Results are cached for 30 seconds.
        """
        now = time.monotonic()
        if self._cached_models and (now - self._last_model_fetch) < 30:
            return list(self._cached_models)

        if _requests is None:
            return [self._default_model] if self._default_model != "auto" else []

        try:
            resp = _requests.get(
                f"http://{self._host}:{self._port}/v1/models",
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._cached_models = [m["id"] for m in data.get("data", [])]
                self._last_model_fetch = now
                log.debug("LocalAI models: %s", self._cached_models)
                return list(self._cached_models)
        except Exception as e:
            log.debug("Could not fetch LocalAI models: %s", e)

        return [self._default_model] if self._default_model != "auto" else []

    def pick_model(self) -> str:
        """
        Choose the best model to use for a request.

        Priority:
            1. User-forced model (set at construction time)
            2. First model returned by /v1/models
            3. Fallback string "auto"
        """
        if self._forced_model:
            return self._forced_model
        available = self.models()
        if available:
            return available[0]
        return "auto"

    # ── Request building ─────────────────────────────────────────────────────

    def _build_payload(self, messages: list[dict], **kwargs) -> dict:
        """Build payload, auto-selecting model if not specified."""
        kwargs.setdefault("model", self.pick_model())
        return super()._build_payload(messages, **kwargs)

    # ── Chat / stream ────────────────────────────────────────────────────────

    def chat(self, messages: list[dict], **kwargs) -> ChatResponse:
        if not self.is_available():
            raise ProviderError(
                "LocalAI server is not running. "
                "Start it with: modes/local_mode.LocalAIRunner.start()",
                provider=self.provider_name,
            )
        return super().chat(messages, **kwargs)

    def stream(self, messages: list[dict], **kwargs):
        if not self.is_available():
            raise ProviderError(
                "LocalAI server is not running.",
                provider=self.provider_name,
            )
        yield from super().stream(messages, **kwargs)

    # ── Info ────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (f"LocalAIProvider(host={self._host!r}, port={self._port}, "
                f"model={self._forced_model or 'auto'})")
