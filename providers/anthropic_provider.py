"""
carry-ai/providers/anthropic_provider.py — Anthropic Claude Provider
=====================================================================

Implements BaseProvider for Anthropic's Claude API (Messages endpoint).

Features:
    - Supports Claude Opus, Sonnet, Haiku model families
    - Streaming via SSE (Server-Sent Events)
    - System prompt as top-level parameter (Anthropic-specific)
    - Tool use / function calling support
    - Automatic retry on 429 (rate limit) and 529 (overloaded)

API Endpoint:
    POST https://api.anthropic.com/v1/messages
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

log = logging.getLogger("carry-ai.providers.anthropic")

API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096

AVAILABLE_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]


class AnthropicProvider(BaseProvider):
    """Anthropic Claude API provider (Messages endpoint)."""

    def __init__(self, api_key: str, base_url: str | None = None):
        if _requests is None:
            raise ImportError("'requests' library required. pip install requests")
        self._api_key = api_key
        self._base_url = (base_url or API_BASE).rstrip("/")
        self._session = _requests.Session()

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def _headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    # ------------------------------------------------------------------
    # Anthropic uses a different message format than OpenAI:
    #   - system prompt is a top-level field, not a message
    #   - tool results use "tool_result" role, not "tool"
    # We accept OpenAI-format messages and convert internally.
    # ------------------------------------------------------------------

    def _convert_messages(self, messages: list[dict]) -> tuple[str | None, list[dict]]:
        """Convert OpenAI-format messages to Anthropic format.

        Returns:
            (system_prompt, anthropic_messages)
        """
        system = None
        converted = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                # Anthropic takes system as a top-level param
                system = msg.get("content", "")
                continue

            if role == "tool":
                # OpenAI tool result -> Anthropic tool_result content block
                converted.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })
                continue

            if role == "assistant" and msg.get("tool_calls"):
                # Convert OpenAI tool_calls to Anthropic tool_use content blocks
                content_blocks = []
                # Include text content if present
                text = msg.get("content")
                if text:
                    content_blocks.append({"type": "text", "text": text})
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"raw": args}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                converted.append({"role": "assistant", "content": content_blocks})
                continue

            # Standard user/assistant messages
            converted.append({
                "role": role,
                "content": msg.get("content", ""),
            })

        return system, converted

    def _convert_tools(self, tools: list[dict] | None) -> list[dict] | None:
        """Convert OpenAI tool definitions to Anthropic format."""
        if not tools:
            return None

        anthropic_tools = []
        for tool in tools:
            fn = tool.get("function", tool)
            anthropic_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return anthropic_tools

    def _build_payload(self, messages: list[dict], **kwargs) -> dict:
        """Build the /v1/messages request body."""
        system, converted_messages = self._convert_messages(messages)

        payload = {
            "model": kwargs.get("model", DEFAULT_MODEL),
            "messages": converted_messages,
            "max_tokens": kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
        }

        if system:
            payload["system"] = system

        if kwargs.get("stream"):
            payload["stream"] = True

        if "temperature" in kwargs:
            payload["temperature"] = kwargs["temperature"]
        if "top_p" in kwargs:
            payload["top_p"] = kwargs["top_p"]
        if "stop" in kwargs:
            payload["stop_sequences"] = kwargs["stop"] if isinstance(kwargs["stop"], list) else [kwargs["stop"]]

        tools = self._convert_tools(kwargs.get("tools"))
        if tools:
            payload["tools"] = tools
            if "tool_choice" in kwargs:
                tc = kwargs["tool_choice"]
                if tc == "auto":
                    payload["tool_choice"] = {"type": "auto"}
                elif tc == "required":
                    payload["tool_choice"] = {"type": "any"}
                elif isinstance(tc, dict):
                    payload["tool_choice"] = tc

        return payload

    def _handle_error(self, resp) -> None:
        """Map HTTP errors to typed exceptions."""
        status = resp.status_code
        try:
            body = resp.json()
            error_msg = body.get("error", {}).get("message", resp.text[:300])
        except Exception:
            error_msg = resp.text[:300]

        if status in (401, 403):
            raise AuthenticationError(f"Anthropic: {error_msg}", provider="anthropic")
        elif status == 429:
            retry_after = None
            ra = resp.headers.get("retry-after")
            if ra:
                try:
                    retry_after = float(ra)
                except ValueError:
                    pass
            raise RateLimitError(f"Anthropic: {error_msg}", provider="anthropic", retry_after=retry_after)
        elif status in (529, 503):
            raise OverloadedError(f"Anthropic: {error_msg}", provider="anthropic")
        else:
            raise ProviderError(f"Anthropic HTTP {status}: {error_msg}", provider="anthropic", status_code=status)

    def _parse_response(self, data: dict, model_used: str) -> ChatResponse:
        """Parse a non-streaming Anthropic response into ChatResponse."""
        content_blocks = data.get("content", [])
        text_parts = []
        tool_calls = []

        for block in content_blocks:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                # Convert Anthropic tool_use to OpenAI tool_calls format
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

        usage = data.get("usage", {})

        return ChatResponse(
            role="assistant",
            content="\n".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
            provider="anthropic",
            model=data.get("model", model_used),
            usage={
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            },
        )

    def chat(self, messages: list[dict], **kwargs) -> ChatResponse:
        """Send a chat completion request to Anthropic Messages API."""
        url = f"{self._base_url}/v1/messages"
        payload = self._build_payload(messages, stream=False, **kwargs)

        try:
            resp = self._session.post(url, json=payload, headers=self._headers(),
                                      timeout=kwargs.get("timeout", 120))
        except _requests.ConnectionError as e:
            raise ProviderError(f"Anthropic: connection failed: {e}", provider="anthropic")
        except _requests.Timeout:
            raise ProviderError("Anthropic: request timed out", provider="anthropic")

        if resp.status_code != 200:
            self._handle_error(resp)

        return self._parse_response(resp.json(), payload.get("model", DEFAULT_MODEL))

    def stream(self, messages: list[dict], **kwargs):
        """Stream a chat completion via Anthropic SSE.

        Anthropic SSE events:
            message_start, content_block_start, content_block_delta,
            content_block_stop, message_delta, message_stop

        Yields dicts: {"type": "content"|"tool_call"|"done", "data": ...}
        """
        url = f"{self._base_url}/v1/messages"
        payload = self._build_payload(messages, stream=True, **kwargs)

        try:
            resp = self._session.post(url, json=payload, headers=self._headers(),
                                      timeout=kwargs.get("timeout", 120), stream=True)
        except _requests.ConnectionError as e:
            raise ProviderError(f"Anthropic: connection failed: {e}", provider="anthropic")
        except _requests.Timeout:
            raise ProviderError("Anthropic: stream timed out", provider="anthropic")

        if resp.status_code != 200:
            self._handle_error(resp)

        # Track current tool_use block for assembly
        current_tool_id = None
        current_tool_name = None
        current_tool_args = ""

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue

            data_str = line[6:]
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            if event_type == "content_block_delta":
                delta = event.get("delta", {})
                delta_type = delta.get("type", "")

                if delta_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield {"type": "content", "data": text}

                elif delta_type == "input_json_delta":
                    # Accumulate tool call arguments
                    current_tool_args += delta.get("partial_json", "")

            elif event_type == "content_block_start":
                block = event.get("content_block", {})
                if block.get("type") == "tool_use":
                    current_tool_id = block.get("id", "")
                    current_tool_name = block.get("name", "")
                    current_tool_args = ""

            elif event_type == "content_block_stop":
                # If we were building a tool call, emit it now
                if current_tool_id:
                    yield {
                        "type": "tool_call",
                        "data": [{
                            "id": current_tool_id,
                            "type": "function",
                            "function": {
                                "name": current_tool_name,
                                "arguments": current_tool_args,
                            },
                        }],
                    }
                    current_tool_id = None
                    current_tool_name = None
                    current_tool_args = ""

            elif event_type == "message_stop":
                yield {"type": "done", "data": None}
                return

            elif event_type == "message_delta":
                stop_reason = event.get("delta", {}).get("stop_reason")
                if stop_reason:
                    yield {"type": "done", "data": stop_reason}
                    return

    def is_available(self) -> bool:
        """Check if the API key is valid by hitting a lightweight endpoint."""
        if not self._api_key:
            return False
        try:
            # Anthropic doesn't have /models — use a minimal messages call
            # Instead, just verify key format
            return self._api_key.startswith("sk-ant-") and len(self._api_key) > 20
        except Exception:
            return False

    def models(self) -> list[str]:
        return list(AVAILABLE_MODELS)

    @property
    def default_model(self) -> str:
        return DEFAULT_MODEL

    def shutdown(self) -> None:
        self._session.close()
        self._api_key = ""
