"""
carry-ai/cowork/session_sharing.py -- Session Sharing & Export
================================================================

Export, import, and share chat sessions. Sessions can be saved as
JSON or Markdown for offline sharing, or served via a temp localhost
URL for LAN access.

Privacy: Shared sessions strip API keys, system prompts, and
sensitive tool outputs. Absolute paths are relativized.
"""

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("carry-ai.cowork.sharing")

# Patterns to strip from shared sessions
_SENSITIVE_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),       # Anthropic keys
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),         # OpenAI keys
    re.compile(r"gsk_[A-Za-z0-9_-]+"),             # Groq keys
    re.compile(r"sk-or-[A-Za-z0-9_-]+"),           # OpenRouter keys
    re.compile(r"hf_[A-Za-z0-9_-]+"),              # HuggingFace tokens
    re.compile(r"AIzaSy[A-Za-z0-9_-]+"),           # Google API keys
    re.compile(r"Bearer\s+[A-Za-z0-9_.-]+"),       # Bearer tokens
]


@dataclass
class SharedSession:
    """A shareable session snapshot."""
    session_id: str
    title: str = ""
    created_at: float = 0.0
    mode: str = ""
    model: str = ""
    messages: list = field(default_factory=list)
    tools_used: list = field(default_factory=list)
    shared_by: str = ""
    shared_status: str = "private"       # private | public
    share_url: str = ""
    exported_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "mode": self.mode,
            "model": self.model,
            "messages": self.messages,
            "tools_used": self.tools_used,
            "shared_by": self.shared_by,
            "shared_status": self.shared_status,
            "exported_at": self.exported_at,
        }


def _sanitize_content(text: str) -> str:
    """Strip sensitive tokens and absolutize paths."""
    if not text:
        return text
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    # Relativize common absolute paths
    text = re.sub(r"[A-Z]:\\Users\\[^\\]+\\", "~/", text)
    text = re.sub(r"/home/[^/]+/", "~/", text)
    return text


def _sanitize_messages(messages: list) -> list:
    """Deep-sanitize a message list for sharing."""
    cleaned = []
    for msg in messages:
        m = dict(msg)
        # Skip system messages
        if m.get("role") == "system":
            continue
        # Sanitize content
        if isinstance(m.get("content"), str):
            m["content"] = _sanitize_content(m["content"])
        # Sanitize tool results
        if m.get("role") == "tool":
            if isinstance(m.get("content"), str):
                m["content"] = _sanitize_content(m["content"])
                # Truncate very long tool outputs
                if len(m["content"]) > 2000:
                    m["content"] = m["content"][:2000] + "\n[... truncated for sharing]"
        cleaned.append(m)
    return cleaned


class SessionExporter:
    """
    Export and import chat sessions.

    Usage:
        exporter = SessionExporter("exports/")

        # Export current session
        path = exporter.export_json(messages, mode="api", model="claude-sonnet-4-6")
        path = exporter.export_markdown(messages, title="Debug session")

        # Import
        session = exporter.import_json("exports/session_abc123.json")
    """

    def __init__(self, export_dir: str = None):
        if export_dir is None:
            project_root = Path(__file__).resolve().parent.parent
            export_dir = str(project_root / "exports")
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)

    def export_json(self, messages: list, mode: str = "", model: str = "",
                    title: str = "", shared_by: str = "") -> Path:
        """
        Export a session as sanitized JSON.

        Returns path to the exported file.
        """
        session_id = str(uuid.uuid4())[:8]
        sanitized = _sanitize_messages(messages)

        # Extract tools used
        tools_used = set()
        for msg in messages:
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                tools_used.add(fn.get("name", ""))

        session = SharedSession(
            session_id=session_id,
            title=title or f"Session {session_id}",
            created_at=time.time(),
            mode=mode,
            model=model,
            messages=sanitized,
            tools_used=sorted(tools_used),
            shared_by=shared_by,
            exported_at=time.time(),
        )

        filename = f"session_{session_id}.json"
        path = self.export_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session.to_dict(), f, indent=2)

        logger.info("Exported session to %s (%d messages)", path, len(sanitized))
        return path

    def export_markdown(self, messages: list, title: str = "",
                        mode: str = "", model: str = "") -> Path:
        """Export a session as readable Markdown."""
        session_id = str(uuid.uuid4())[:8]
        sanitized = _sanitize_messages(messages)

        lines = []
        lines.append(f"# {title or 'Chat Session'}")
        lines.append(f"")
        lines.append(f"- **Mode:** {mode or 'unknown'}")
        lines.append(f"- **Model:** {model or 'unknown'}")
        lines.append(f"- **Messages:** {len(sanitized)}")
        lines.append(f"- **Exported:** {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"")
        lines.append("---")
        lines.append("")

        for msg in sanitized:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if role == "user":
                lines.append(f"## User")
                lines.append(f"")
                lines.append(content)
                lines.append(f"")
            elif role == "assistant":
                lines.append(f"## Assistant")
                lines.append(f"")
                if content:
                    lines.append(content)
                # Tool calls
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    lines.append(f"")
                    lines.append(f"> **Tool:** `{fn.get('name', '')}`")
                    args = fn.get("arguments", "")
                    if args:
                        lines.append(f"> ```json")
                        lines.append(f"> {args[:500]}")
                        lines.append(f"> ```")
                lines.append(f"")
            elif role == "tool":
                tool_name = msg.get("name", "tool")
                lines.append(f"> **Result ({tool_name}):**")
                lines.append(f"> ```")
                content_preview = content[:1000] if content else "(empty)"
                for line in content_preview.split("\n"):
                    lines.append(f"> {line}")
                lines.append(f"> ```")
                lines.append(f"")

        filename = f"session_{session_id}.md"
        path = self.export_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info("Exported markdown to %s", path)
        return path

    def import_json(self, file_path: str) -> Optional[SharedSession]:
        """Import a session from JSON file."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return SharedSession(
                session_id=data.get("session_id", ""),
                title=data.get("title", ""),
                created_at=data.get("created_at", 0),
                mode=data.get("mode", ""),
                model=data.get("model", ""),
                messages=data.get("messages", []),
                tools_used=data.get("tools_used", []),
                shared_by=data.get("shared_by", ""),
                shared_status=data.get("shared_status", "private"),
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to import session: %s", e)
            return None

    def list_exports(self) -> list[dict]:
        """List all exported session files."""
        exports = []
        for f in sorted(self.export_dir.glob("session_*.json"), reverse=True):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                exports.append({
                    "filename": f.name,
                    "session_id": data.get("session_id", ""),
                    "title": data.get("title", ""),
                    "message_count": len(data.get("messages", [])),
                    "exported_at": data.get("exported_at", 0),
                })
            except Exception:
                pass
        return exports


class SessionSharer:
    """
    Manages live session sharing via localhost URL.
    Other carry-ai instances on the LAN can view shared sessions.
    """

    def __init__(self):
        self._shared: dict[str, SharedSession] = {}  # session_id -> session

    def share(self, session: SharedSession, port: int = 8080) -> str:
        """Make a session viewable at /api/cowork/sessions/<id>."""
        session.shared_status = "public"
        session.share_url = f"http://localhost:{port}/api/cowork/sessions/{session.session_id}"
        self._shared[session.session_id] = session
        logger.info("Shared session %s at %s", session.session_id, session.share_url)
        return session.share_url

    def unshare(self, session_id: str):
        """Remove a session from sharing."""
        session = self._shared.pop(session_id, None)
        if session:
            session.shared_status = "private"
            session.share_url = ""

    def get_shared(self, session_id: str) -> Optional[SharedSession]:
        return self._shared.get(session_id)

    def list_shared(self) -> list[dict]:
        return [
            {"session_id": s.session_id, "title": s.title,
             "message_count": len(s.messages), "share_url": s.share_url}
            for s in self._shared.values()
        ]
