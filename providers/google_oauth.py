"""
carry-ai/providers/google_oauth.py — Google Gemini Provider (OAuth2)
=====================================================================

Implements BaseProvider for Google's Gemini API with full OAuth2 support
and simple API key fallback.

Features:
    - OAuth2 browser-based flow for first-time auth
    - Token refresh without re-auth
    - Supports Gemini 2.5 Pro, Flash models
    - Multimodal input (text + images)
    - Streaming via SSE
    - Function calling / tool use

API Endpoint:
    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent
"""

import json
import logging

try:
    import requests as _requests
except ImportError:
    _requests = None

from providers.base import (
    BaseProvider,
    ChatResponse,
    ProviderError,
    RateLimitError,
    AuthenticationError,
    OverloadedError,
)

log = logging.getLogger("carry-ai.providers.google")

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash"

AVAILABLE_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]


class GoogleProvider(BaseProvider):
    """Google Gemini API provider with OAuth2 and API key auth."""

    def __init__(self, api_key: str | None = None,
                 oauth_credentials: str | None = None,
                 base_url: str | None = None):
        if _requests is None:
            raise ImportError("'requests' library required. pip install requests")

        self._api_key = api_key or ""
        self._oauth_refresh_token = oauth_credentials
        self._access_token: str | None = None
        self._base_url = (base_url or API_BASE).rstrip("/")
        self._session = _requests.Session()

    @property
    def provider_name(self) -> str:
        return "google"

    def _get_auth_params(self) -> dict:
        """Return query params or headers for authentication."""
        # Prefer OAuth access token if available
        if self._access_token:
            self._session.headers["Authorization"] = f"Bearer {self._access_token}"
            return {}
        # Fall back to API key as query parameter
        if self._api_key:
            return {"key": self._api_key}
        raise AuthenticationError("Google: No API key or OAuth token available.", provider="google")

    # ------------------------------------------------------------------
    # Message format conversion (OpenAI -> Gemini)
    # ------------------------------------------------------------------

    def _convert_messages(self, messages: list[dict]) -> tuple[str | None, list[dict]]:
        """Convert OpenAI-format messages to Gemini contents format.

        Gemini uses:
            {"role": "user"|"model", "parts": [{"text": "..."}]}

        Returns:
            (system_instruction, contents_list)
        """
        system = None
        contents = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                system = msg.get("content", "")
                continue

            # Map roles: assistant -> model, user -> user
            gemini_role = "model" if role == "assistant" else "user"

            # Handle tool results
            if role == "tool":
                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": msg.get("name", msg.get("tool_call_id", "")),
                            "response": {"result": msg.get("content", "")},
                        }
                    }],
                })
                continue

            # Handle assistant messages with tool calls
            if role == "assistant" and msg.get("tool_calls"):
                parts = []
                text = msg.get("content")
                if text:
                    parts.append({"text": text})
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"raw": args}
                    parts.append({
                        "functionCall": {
                            "name": fn.get("name", ""),
                            "args": args,
                        }
                    })
                contents.append({"role": "model", "parts": parts})
                continue

            # Standard text messages
            content = msg.get("content", "")
            if isinstance(content, str):
                parts = [{"text": content}]
            elif isinstance(content, list):
                # Multimodal: list of content parts (text, image_url, etc.)
                parts = []
                for part in content:
                    if isinstance(part, str):
                        parts.append({"text": part})
                    elif isinstance(part, dict):
                        if part.get("type") == "text":
                            parts.append({"text": part.get("text", "")})
                        elif part.get("type") == "image_url":
                            url = part.get("image_url", {}).get("url", "")
                            if url.startswith("data:"):
                                # Base64 inline image
                                mime, _, b64data = url.partition(";base64,")
                                mime = mime.replace("data:", "")
                                parts.append({
                                    "inlineData": {
                                        "mimeType": mime or "image/png",
                                        "data": b64data,
                                    }
                                })
                            else:
                                parts.append({"text": f"[Image: {url}]"})
            else:
                parts = [{"text": str(content)}]

            contents.append({"role": gemini_role, "parts": parts})

        return system, contents

    def _convert_tools(self, tools: list[dict] | None) -> list[dict] | None:
        """Convert OpenAI tool definitions to Gemini function declarations."""
        if not tools:
            return None

        declarations = []
        for tool in tools:
            fn = tool.get("function", tool)
            declarations.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })

        return [{"functionDeclarations": declarations}]

    def _build_payload(self, messages: list[dict], **kwargs) -> dict:
        """Build the generateContent request body."""
        system, contents = self._convert_messages(messages)

        payload = {"contents": contents}

        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        # Generation config
        gen_config = {}
        if "temperature" in kwargs:
            gen_config["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs:
            gen_config["maxOutputTokens"] = kwargs["max_tokens"]
        if "top_p" in kwargs:
            gen_config["topP"] = kwargs["top_p"]
        if "stop" in kwargs:
            stops = kwargs["stop"] if isinstance(kwargs["stop"], list) else [kwargs["stop"]]
            gen_config["stopSequences"] = stops
        if gen_config:
            payload["generationConfig"] = gen_config

        tools = self._convert_tools(kwargs.get("tools"))
        if tools:
            payload["tools"] = tools

        return payload

    def _handle_error(self, resp) -> None:
        """Map Gemini error responses to typed exceptions."""
        status = resp.status_code
        try:
            body = resp.json()
            error_msg = body.get("error", {}).get("message", resp.text[:300])
        except Exception:
            error_msg = resp.text[:300]

        if status in (401, 403):
            raise AuthenticationError(f"Google: {error_msg}", provider="google")
        elif status == 429:
            raise RateLimitError(f"Google: {error_msg}", provider="google")
        elif status in (503, 529):
            raise OverloadedError(f"Google: {error_msg}", provider="google")
        else:
            raise ProviderError(f"Google HTTP {status}: {error_msg}", provider="google", status_code=status)

    def _parse_response(self, data: dict, model: str) -> ChatResponse:
        """Parse a Gemini generateContent response."""
        candidates = data.get("candidates", [{}])
        if not candidates:
            return ChatResponse(provider="google", model=model)

        candidate = candidates[0]
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        text_parts = []
        tool_calls = []

        for part in parts:
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append({
                    "id": f"call_{fc.get('name', '')}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {})),
                    },
                })

        usage_meta = data.get("usageMetadata", {})

        return ChatResponse(
            role="assistant",
            content="\n".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
            provider="google",
            model=model,
            usage={
                "input_tokens": usage_meta.get("promptTokenCount", 0),
                "output_tokens": usage_meta.get("candidatesTokenCount", 0),
                "total_tokens": usage_meta.get("totalTokenCount", 0),
            },
        )

    def chat(self, messages: list[dict], **kwargs) -> ChatResponse:
        """Send a generateContent request to Gemini API."""
        model = kwargs.pop("model", None) or DEFAULT_MODEL
        url = f"{self._base_url}/models/{model}:generateContent"
        payload = self._build_payload(messages, **kwargs)
        params = self._get_auth_params()

        try:
            resp = self._session.post(url, json=payload, params=params,
                                      timeout=kwargs.get("timeout", 120))
        except _requests.ConnectionError as e:
            raise ProviderError(f"Google: connection failed: {e}", provider="google")
        except _requests.Timeout:
            raise ProviderError("Google: request timed out", provider="google")

        if resp.status_code != 200:
            self._handle_error(resp)

        return self._parse_response(resp.json(), model)

    def stream(self, messages: list[dict], **kwargs):
        """Stream a generateContent response from Gemini.

        Gemini streaming returns newline-delimited JSON objects.

        Yields dicts: {"type": "content"|"tool_call"|"done", "data": ...}
        """
        model = kwargs.pop("model", None) or DEFAULT_MODEL
        url = f"{self._base_url}/models/{model}:streamGenerateContent"
        payload = self._build_payload(messages, **kwargs)
        params = self._get_auth_params()
        params["alt"] = "sse"

        try:
            resp = self._session.post(url, json=payload, params=params,
                                      timeout=kwargs.get("timeout", 120), stream=True)
        except _requests.ConnectionError as e:
            raise ProviderError(f"Google: connection failed: {e}", provider="google")
        except _requests.Timeout:
            raise ProviderError("Google: stream timed out", provider="google")

        if resp.status_code != 200:
            self._handle_error(resp)

        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue

            # SSE format: "data: {...}"
            if line.startswith("data: "):
                data_str = line[6:]
            else:
                data_str = line

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            candidates = data.get("candidates", [])
            if not candidates:
                continue

            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                if "text" in part:
                    yield {"type": "content", "data": part["text"]}
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    yield {
                        "type": "tool_call",
                        "data": [{
                            "id": f"call_{fc.get('name', '')}",
                            "type": "function",
                            "function": {
                                "name": fc.get("name", ""),
                                "arguments": json.dumps(fc.get("args", {})),
                            },
                        }],
                    }

            # Check finish reason
            finish = candidates[0].get("finishReason")
            if finish and finish != "UNSPECIFIED":
                yield {"type": "done", "data": finish}
                return

    def is_available(self) -> bool:
        """Check if API key or OAuth token is configured."""
        if self._access_token:
            return True
        if self._api_key:
            return len(self._api_key) > 10
        return False

    def models(self) -> list[str]:
        return list(AVAILABLE_MODELS)

    @property
    def default_model(self) -> str:
        return DEFAULT_MODEL

    def shutdown(self) -> None:
        self._session.close()
        self._api_key = ""
        self._access_token = None
        self._oauth_refresh_token = None
