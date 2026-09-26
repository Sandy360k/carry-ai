"""
carry-ai/providers/openai_compat.py — OpenAI-Compatible Base Provider
======================================================================

Shared implementation for providers that expose an OpenAI-compatible
/v1/chat/completions endpoint: OpenAI, Groq, and OpenRouter.

Handles the common HTTP request/response patterns, SSE streaming,
error mapping, and response normalization. Subclasses only need to
set base_url, default_model, and model list.

Model discovery: GET {base_url}/models -> data[].id, filtered through
_is_chat_model() (override per provider), cached in memory by
BaseProvider.  The hardcoded _available_models list is the offline
fallback.  Set _discover_models = False for self-hosted gateways whose
model names are virtual (G0DM0D3) or discovered elsewhere (LocalAI).
"""

import json
import logging
import time

try:
    import requests as _requests
except ImportError:
    _requests = None

from providers.base import (
    DISCOVERY_TIMEOUT,
    BaseProvider,
    ChatResponse,
    ProviderError,
    RateLimitError,
    AuthenticationError,
    OverloadedError,
)

log = logging.getLogger("carry-ai.providers.openai_compat")

# Default timeout for API calls (seconds)
DEFAULT_TIMEOUT = 120


class OpenAICompatProvider(BaseProvider):
    """Base class for providers with OpenAI-compatible chat completions API.

    Subclasses must set:
        - provider_name (property)
        - _base_url
        - _default_model
        - _available_models
    And optionally override _extra_headers() for provider-specific headers.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1"):
        if _requests is None:
            raise ImportError("'requests' library required. pip install requests")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._session = _requests.Session()
        self._default_model: str = ""
        self._available_models: list[str] = []
        self._discover_models: bool = True

    def _headers(self) -> dict:
        """Build request headers."""
        h = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        h.update(self._extra_headers())
        return h

    def _extra_headers(self) -> dict:
        """Override in subclass to add provider-specific headers."""
        return {}

    def _build_payload(self, messages: list[dict], **kwargs) -> dict:
        """Build the chat completions request body."""
        model = self.resolve_model(kwargs.pop("model", None) or self._default_model)

        payload = {
            "model": model,
            "messages": messages,
            "stream": kwargs.get("stream", False),
        }

        # Standard optional fields
        for key in ("temperature", "max_tokens", "top_p", "stop",
                     "frequency_penalty", "presence_penalty", "seed",
                     "response_format", "tools", "tool_choice"):
            if key in kwargs:
                payload[key] = kwargs[key]

        return payload

    def _handle_error_response(self, resp) -> None:
        """Map HTTP error codes to typed exceptions."""
        status = resp.status_code

        try:
            body = resp.json()
            error_msg = body.get("error", {}).get("message", resp.text[:200])
        except Exception:
            error_msg = resp.text[:200]

        if status == 401 or status == 403:
            raise AuthenticationError(
                f"{self.provider_name}: {error_msg}",
                provider=self.provider_name,
            )
        elif status == 429:
            retry_after = None
            ra_header = resp.headers.get("retry-after")
            if ra_header:
                try:
                    retry_after = float(ra_header)
                except ValueError:
                    pass
            raise RateLimitError(
                f"{self.provider_name}: {error_msg}",
                provider=self.provider_name,
                retry_after=retry_after,
            )
        elif status in (503, 529):
            raise OverloadedError(
                f"{self.provider_name}: {error_msg}",
                provider=self.provider_name,
            )
        else:
            raise ProviderError(
                f"{self.provider_name} HTTP {status}: {error_msg}",
                provider=self.provider_name,
                status_code=status,
            )

    def chat(self, messages: list[dict], **kwargs) -> ChatResponse:
        """Send a chat completion request."""
        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(messages, stream=False, **kwargs)
        timeout = kwargs.get("timeout", DEFAULT_TIMEOUT)

        try:
            resp = self._session.post(url, json=payload, headers=self._headers(), timeout=timeout)
        except _requests.ConnectionError as e:
            raise ProviderError(f"{self.provider_name}: connection failed: {e}",
                                provider=self.provider_name)
        except _requests.Timeout:
            raise ProviderError(f"{self.provider_name}: request timed out ({timeout}s)",
                                provider=self.provider_name)

        if resp.status_code != 200:
            self._handle_error_response(resp)

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        return ChatResponse(
            role=message.get("role", "assistant"),
            content=message.get("content", "") or "",
            tool_calls=message.get("tool_calls"),
            provider=self.provider_name,
            model=data.get("model", payload.get("model", "")),
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        )

    def stream(self, messages: list[dict], **kwargs):
        """Stream a chat completion response via SSE.

        Yields dicts: {"type": "content"|"tool_call"|"done", "data": ...}
        """
        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(messages, stream=True, **kwargs)
        timeout = kwargs.get("timeout", DEFAULT_TIMEOUT)

        try:
            resp = self._session.post(
                url, json=payload, headers=self._headers(),
                timeout=timeout, stream=True,
            )
        except _requests.ConnectionError as e:
            raise ProviderError(f"{self.provider_name}: connection failed: {e}",
                                provider=self.provider_name)
        except _requests.Timeout:
            raise ProviderError(f"{self.provider_name}: request timed out",
                                provider=self.provider_name)

        if resp.status_code != 200:
            self._handle_error_response(resp)

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue

            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                yield {"type": "done", "data": None}
                return

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            choice = data.get("choices", [{}])[0]
            delta = choice.get("delta", {})

            content = delta.get("content")
            if content:
                yield {"type": "content", "data": content}

            tool_calls = delta.get("tool_calls")
            if tool_calls:
                yield {"type": "tool_call", "data": tool_calls}

            finish = choice.get("finish_reason")
            if finish:
                yield {"type": "done", "data": finish}
                return

    def is_available(self) -> bool:
        """Quick availability check — verify the key looks valid and endpoint responds."""
        if not self._api_key:
            return False
        try:
            resp = self._session.get(
                f"{self._base_url}/models",
                headers=self._headers(),
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    def _is_chat_model(self, model_id: str) -> bool:
        """Filter for chat-capable IDs from /models. Override per provider."""
        return True

    def _fetch_model_ids(self) -> list[str] | None:
        """GET {base_url}/models -> data[].id (OpenAI-compatible schema)."""
        if not self._discover_models or not self._api_key:
            return None
        resp = self._session.get(
            f"{self._base_url}/models",
            headers=self._headers(),
            timeout=DISCOVERY_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [m["id"] for m in data
                if isinstance(m, dict) and m.get("id") and self._is_chat_model(m["id"])]

    def _fallback_models(self) -> list[str]:
        return list(self._available_models)

    def models(self) -> list[str]:
        """Return available models (live list when reachable, else built-in)."""
        return self.discover_models()

    @property
    def default_model(self) -> str:
        return self._default_model

    def shutdown(self) -> None:
        """Close the HTTP session."""
        self._session.close()
        self._api_key = ""
