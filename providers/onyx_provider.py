"""
carry-ai/providers/onyx_provider.py -- Onyx RAG Provider
==========================================================

Integrates with Onyx (https://github.com/onyx-dot-app/onyx) --
an open-source AI platform with Retrieval-Augmented Generation (RAG)
that connects LLMs to your organization's data.

Features (from Onyx):
    - Agentic RAG: Hybrid vector + keyword search over 50+ data sources
    - Deep Research: Multi-step research producing in-depth reports
    - Connector ecosystem: Google Drive, Slack, Confluence, Notion, GitHub, etc.
    - Custom agents with domain-specific knowledge bases
    - MCP-compatible actions for external integrations
    - Lite mode: Under 1GB RAM, chat-only

Architecture:
    Onyx (v4.x) answers via ``POST {base}/chat/send-chat-message``
    (backend/onyx/server/query_and_chat/chat_backend.py). Request:
        {message, stream, chat_session_id | chat_session_info:{persona_id},
         internal_search_filters:{document_set:[...]}, deep_research}
    stream=false returns ChatFullResponse JSON
        {answer, top_documents[], citation_info[], chat_session_id, error_msg}
    stream=true returns newline-delimited JSON packets, e.g.
        {"chat_session_id": "..."}                                (new session)
        {"placement": {...}, "obj": {"type": "message_delta", "content": "..."}}
        {"error": "..."}
    Onyx keeps the conversation server-side, so only the newest user
    message is sent and the chat_session_id is reused for follow-ups.

Setup:
    1. Deploy Onyx (Docker): docker compose up -d
       (or use Onyx Cloud at https://www.onyx.app)
    2. Configure connectors (Drive, Slack, etc.) in Onyx's web UI
    3. Add Onyx endpoint to carry-ai:
       python crypto/keystore.py add onyx
       (api_key = Onyx API key, base_url = http://your-onyx:3000/api)

Integration paths:
    A. As a provider: Route carry-ai queries through Onyx for RAG answers
    B. As MCP server: Register Onyx as an MCP server in config/settings.json
       for carry-ai to access Onyx's connectors as tools
    C. As document index: Use Onyx to index files on the USB drive

Reference: https://github.com/onyx-dot-app/onyx
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

log = logging.getLogger("carry-ai.providers.onyx")

# Default Onyx instance URL
ONYX_BASE_URL = "http://localhost:3000/api"

# Onyx persona (agent) ID; 0 = the default assistant.
DEFAULT_PERSONA = 0

AVAILABLE_MODELS = [
    "onyx/default",          # Default RAG assistant
    "onyx/research",         # Deep research mode (deep_research=true)
]

SEND_PATH = "/chat/send-chat-message"


def _latest_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):          # multimodal parts
                content = " ".join(p.get("text", "") for p in content
                                   if isinstance(p, dict) and p.get("type") == "text")
            return str(content or "")
    return ""


def _source_names(docs) -> list[str]:
    names = []
    for d in docs or []:
        if isinstance(d, dict):
            name = d.get("semantic_identifier") or d.get("link") or ""
            if name:
                names.append(str(name))
    return list(dict.fromkeys(names))[:5]


class OnyxProvider(BaseProvider):
    """Onyx RAG-enhanced provider.

    Routes carry-ai queries through an Onyx instance for answers
    grounded in the user's connected data sources (Drive, Slack,
    Notion, GitHub, etc.).

    Usage:
        provider = OnyxProvider(
            api_key="onyx-api-key",
            base_url="http://localhost:3000/api",
        )
        response = provider.chat(messages)                       # RAG answer
        response = provider.chat(messages, model="onyx/research")  # deep research
        response = provider.chat(messages, document_sets=["company-docs"])
    """

    def __init__(self, api_key: str = "", base_url: str | None = None,
                 persona_id: int | None = None):
        if _requests is None:
            raise ImportError("'requests' library required. pip install requests")
        self._api_key = api_key
        self._base_url = (base_url or ONYX_BASE_URL).rstrip("/")
        self._persona_id = DEFAULT_PERSONA if persona_id is None else persona_id
        self._session = _requests.Session()
        # Server-side conversation for multi-turn; reset on a new conversation.
        self._chat_session_id: str | None = None

    @property
    def provider_name(self) -> str:
        return "onyx"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def reset_session(self) -> None:
        """Forget the Onyx chat session; the next message starts a new one."""
        self._chat_session_id = None

    def _build_payload(self, messages: list[dict], stream: bool, kwargs: dict) -> dict:
        model = kwargs.pop("model", None) or ""
        persona = kwargs.pop("persona", None)
        document_sets = kwargs.pop("document_sets", None)
        deep_research = bool(kwargs.pop("deep_research", False)
                             or persona == "research" or model == "onyx/research")
        persona_id = kwargs.pop("persona_id", self._persona_id)

        # A history with no earlier assistant turn is a new conversation.
        if not any(m.get("role") == "assistant" for m in messages):
            self._chat_session_id = None

        payload: dict = {
            "message": _latest_user_text(messages),
            "stream": stream,
            "deep_research": deep_research,
        }
        if self._chat_session_id:
            payload["chat_session_id"] = self._chat_session_id
        else:
            payload["chat_session_info"] = {"persona_id": persona_id}
        if document_sets:
            payload["internal_search_filters"] = {"document_set": list(document_sets)}
        return payload

    def _check_status(self, resp) -> None:
        if resp.status_code in (401, 403):
            raise AuthenticationError("onyx: invalid API key", provider="onyx")
        if resp.status_code == 429:
            raise RateLimitError("onyx: rate limited", provider="onyx")
        if resp.status_code in (503, 529):
            raise OverloadedError("onyx: service overloaded", provider="onyx")
        if resp.status_code != 200:
            raise ProviderError(
                f"onyx: HTTP {resp.status_code}: {resp.text[:200]}",
                provider="onyx", status_code=resp.status_code,
            )

    def _post(self, payload: dict, timeout: float, stream: bool):
        try:
            resp = self._session.post(
                f"{self._base_url}{SEND_PATH}", json=payload,
                headers=self._headers(), timeout=timeout, stream=stream,
            )
        except _requests.ConnectionError as e:
            raise ProviderError(f"onyx: connection failed: {e}", provider="onyx")
        except _requests.Timeout:
            raise ProviderError("onyx: request timed out", provider="onyx")
        self._check_status(resp)
        return resp

    def chat(self, messages: list[dict], **kwargs) -> ChatResponse:
        """Send a chat request through Onyx's RAG pipeline (stream=false).

        Args:
            messages: OpenAI-format message list (only the newest user turn
                is sent; Onyx holds the history in its chat session).
            model: "onyx/default" or "onyx/research" (deep research).
            persona: "research" also enables deep research.
            document_sets: Document set names to restrict the search to.

        Returns:
            ChatResponse with RAG-grounded content and source names appended.
        """
        timeout = kwargs.pop("timeout", 120)
        model = kwargs.get("model") or "onyx/default"
        payload = self._build_payload(messages, False, kwargs)
        resp = self._post(payload, timeout, stream=False)

        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("onyx: non-JSON response", provider="onyx")
        if data.get("error_msg"):
            raise ProviderError(f"onyx: {data['error_msg']}", provider="onyx")
        if data.get("chat_session_id"):
            self._chat_session_id = str(data["chat_session_id"])

        content = data.get("answer") or ""
        sources = _source_names(data.get("top_documents"))
        if sources:
            content += "\n\n**Sources:** " + ", ".join(sources)

        return ChatResponse(
            role="assistant",
            content=content,
            provider="onyx",
            model=model if model in AVAILABLE_MODELS else "onyx/default",
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )

    def stream(self, messages: list[dict], **kwargs):
        """Stream a RAG response from Onyx.

        Parses newline-delimited JSON packets and yields
        ``{"type": "content", "data": str}`` chunks, then ``{"type": "done"}``.
        """
        timeout = kwargs.pop("timeout", 120)
        payload = self._build_payload(messages, True, kwargs)
        resp = self._post(payload, timeout, stream=True)

        sources: list[str] = []
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.strip():
                continue
            if line.startswith("data:"):              # tolerate SSE framing
                line = line[5:].strip()
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("chat_session_id"):
                self._chat_session_id = str(data["chat_session_id"])
            if data.get("error"):
                raise ProviderError(f"onyx: {data['error']}", provider="onyx")
            obj = data.get("obj")
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "message_delta" and obj.get("content"):
                yield {"type": "content", "data": obj["content"]}
            elif kind == "message_start":
                sources = _source_names(obj.get("final_documents")) or sources
            elif kind == "search_tool_documents_delta" and not sources:
                sources = _source_names(obj.get("documents"))

        if sources:
            yield {"type": "content", "data": "\n\n**Sources:** " + ", ".join(sources)}
        yield {"type": "done", "data": None}

    def is_available(self) -> bool:
        """Check if the Onyx instance is reachable."""
        try:
            resp = self._session.get(
                f"{self._base_url}/health",
                headers=self._headers(),
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def models(self) -> list[str]:
        return list(AVAILABLE_MODELS)

    @property
    def default_model(self) -> str:
        return "onyx/default"

    def list_connectors(self) -> list[dict]:
        """List configured data source connectors in the Onyx instance."""
        try:
            resp = self._session.get(
                f"{self._base_url}/manage/connector",
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log.warning("Cannot list Onyx connectors: %s", e)
        return []

    def list_document_sets(self) -> list[dict]:
        """List document sets available for targeted search."""
        try:
            resp = self._session.get(
                f"{self._base_url}/manage/document-set",
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log.warning("Cannot list Onyx document sets: %s", e)
        return []

    def shutdown(self) -> None:
        """Close the HTTP session."""
        self._chat_session_id = None
        self._session.close()
        self._api_key = ""
