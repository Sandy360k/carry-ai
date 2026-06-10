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
    Onyx exposes an OpenAI-compatible /api/chat endpoint. This provider
    wraps it so carry-ai can route queries through Onyx for RAG-enhanced
    answers grounded in the user's actual data.

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

# Onyx-specific persona (agent) IDs
DEFAULT_PERSONA = 0  # Default assistant

AVAILABLE_MODELS = [
    "onyx/default",          # Default RAG assistant
    "onyx/research",         # Deep research mode
    "onyx/code",             # Code-aware assistant
]


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
        # Standard RAG query (searches connected sources)
        response = provider.chat(messages)

        # Deep research mode
        response = provider.chat(messages, persona="research")

        # Query specific data sources
        response = provider.chat(messages, document_sets=["company-docs"])
    """

    def __init__(self, api_key: str = "", base_url: str | None = None,
                 persona_id: int | None = None):
        if _requests is None:
            raise ImportError("'requests' library required. pip install requests")
        self._api_key = api_key
        self._base_url = (base_url or ONYX_BASE_URL).rstrip("/")
        self._persona_id = persona_id or DEFAULT_PERSONA
        self._session = _requests.Session()

    @property
    def provider_name(self) -> str:
        return "onyx"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def chat(self, messages: list[dict], **kwargs) -> ChatResponse:
        """Send a chat request through Onyx's RAG pipeline.

        Onyx will:
        1. Search connected data sources for relevant context
        2. Build a grounded prompt with retrieved documents
        3. Generate a response using the configured LLM
        4. Return the answer with source citations

        Args:
            messages: OpenAI-format message list.
            persona: Onyx persona/agent name ("default", "research", "code").
            document_sets: List of document set names to search.
            retrieval_options: Dict of retrieval config overrides.

        Returns:
            ChatResponse with RAG-grounded content.
        """
        url = f"{self._base_url}/chat/send-message"

        # Extract the latest user message
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        # Build Onyx-specific payload
        persona = kwargs.pop("persona", None)
        document_sets = kwargs.pop("document_sets", None)
        retrieval_options = kwargs.pop("retrieval_options", None)

        payload = {
            "message": user_msg,
            "persona_id": self._persona_id,
            "prompt_id": 0,
            "retrieval_options": retrieval_options or {
                "run_search": "auto",
                "real_time": True,
            },
        }

        if document_sets:
            payload["retrieval_options"]["document_sets"] = document_sets

        # Map persona names to IDs if needed
        if persona == "research":
            payload["persona_id"] = 1
        elif persona == "code":
            payload["persona_id"] = 2

        # Include chat history for multi-turn
        if len(messages) > 1:
            history = []
            for m in messages[:-1]:  # All except latest
                if m.get("role") in ("user", "assistant"):
                    history.append({
                        "message": m.get("content", ""),
                        "message_type": "user" if m["role"] == "user" else "assistant",
                    })
            if history:
                payload["chat_session_id"] = None  # New session
                payload["parent_message_id"] = None

        try:
            resp = self._session.post(
                url, json=payload, headers=self._headers(),
                timeout=kwargs.get("timeout", 120),
            )
        except _requests.ConnectionError as e:
            raise ProviderError(f"onyx: connection failed: {e}", provider="onyx")
        except _requests.Timeout:
            raise ProviderError("onyx: request timed out", provider="onyx")

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

        # Parse Onyx response (may be streaming NDJSON)
        content = ""
        sources = []
        try:
            # Onyx streams responses as newline-delimited JSON
            for line in resp.text.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if "answer_piece" in data:
                        content += data["answer_piece"] or ""
                    if "source_documents" in data:
                        for doc in data["source_documents"]:
                            sources.append(doc.get("semantic_identifier", ""))
                except json.JSONDecodeError:
                    content += line
        except Exception:
            content = resp.text

        # Append source citations
        if sources:
            unique_sources = list(dict.fromkeys(sources))[:5]
            content += "\n\n**Sources:** " + ", ".join(unique_sources)

        return ChatResponse(
            role="assistant",
            content=content,
            provider="onyx",
            model=f"onyx/{persona or 'default'}",
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )

    def stream(self, messages: list[dict], **kwargs):
        """Stream a RAG response from Onyx.

        Yields chunks as Onyx processes the query through its
        retrieval + generation pipeline.
        """
        url = f"{self._base_url}/chat/send-message"

        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        payload = {
            "message": user_msg,
            "persona_id": self._persona_id,
            "prompt_id": 0,
            "retrieval_options": {"run_search": "auto", "real_time": True},
        }

        try:
            resp = self._session.post(
                url, json=payload, headers=self._headers(),
                timeout=kwargs.get("timeout", 120), stream=True,
            )
        except Exception as e:
            raise ProviderError(f"onyx: {e}", provider="onyx")

        if resp.status_code != 200:
            raise ProviderError(
                f"onyx: HTTP {resp.status_code}",
                provider="onyx", status_code=resp.status_code,
            )

        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                data = json.loads(line)
                if "answer_piece" in data and data["answer_piece"]:
                    yield {"type": "content", "data": data["answer_piece"]}
                if "source_documents" in data:
                    sources = [d.get("semantic_identifier", "") for d in data["source_documents"]]
                    if sources:
                        yield {"type": "content", "data": f"\n\n**Sources:** {', '.join(sources[:5])}"}
            except json.JSONDecodeError:
                yield {"type": "content", "data": line}

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
        self._session.close()
        self._api_key = ""
