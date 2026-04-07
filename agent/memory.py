"""
carry-ai/agent/memory.py -- Persistent Agent Memory (v2)
=========================================================

SQLite-backed long-term memory inspired by claude-mem.
Survives across sessions on USB. Features:

  - Observation types: fact, discovery, decision, bugfix, note, summary
  - Concept tags: how-it-works, problem-solution, gotcha, pattern, trade-off, preference
  - SHA-256 content-hash deduplication (30s window)
  - SQLite FTS5 full-text search
  - Structured session summaries (request, learned, completed, next_steps)
  - Relevance decay + access tracking
  - Progressive context injection (recent + query-relevant)
  - Per-project working context
  - Privacy tag stripping (<private>...</private>)
  - Export/import JSON for backup
  - Token economics tracking (discovery_tokens)

Storage: SQLite DB on USB at config/memory.db (zero external deps beyond stdlib)
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("carry-ai.agent.memory")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Observation types (inspired by claude-mem)
OBSERVATION_TYPES = {
    "fact",          # Discrete knowledge about user/project
    "discovery",     # Learning about existing system/codebase
    "decision",      # Architectural/design choice with rationale
    "bugfix",        # Something was broken, now fixed
    "note",          # User-created persistent note
    "summary",       # Compressed session summary
    "preference",    # User preference (style, tools, workflow)
}

# Concept categories (orthogonal to types)
CONCEPT_TAGS = {
    "how-it-works",      # Understanding of a system/process
    "why-it-exists",     # Rationale behind a choice
    "what-changed",      # Delta from previous state
    "problem-solution",  # Problem encountered and how it was solved
    "gotcha",            # Non-obvious pitfall or edge case
    "pattern",           # Reusable approach or convention
    "trade-off",         # Pros/cons of a decision
    "preference",        # User style/workflow preference
}

# Privacy tag regex
_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL)

# Dedup window (seconds)
DEDUP_WINDOW_S = 30

DEFAULT_DB_FILE = "config/memory.db"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """A single memory observation."""
    id: int = 0                          # SQLite rowid
    obs_type: str = "fact"               # From OBSERVATION_TYPES
    title: str = ""                      # Short title
    content: str = ""                    # Main content / narrative
    facts: list = field(default_factory=list)    # Extracted atomic facts
    concepts: list = field(default_factory=list) # From CONCEPT_TAGS
    tags: list = field(default_factory=list)     # Free-form tags
    source: str = "agent"                # "agent", "user", "auto", "tool"
    files_read: list = field(default_factory=list)
    files_modified: list = field(default_factory=list)
    content_hash: str = ""               # SHA-256 for dedup
    discovery_tokens: int = 0            # Cost to produce this observation
    relevance: float = 1.0              # 0.0-1.0, decays over time
    access_count: int = 0
    last_accessed: float = 0.0
    created_at: float = 0.0
    session_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Observation":
        d = dict(row)
        for key in ("facts", "concepts", "tags", "files_read", "files_modified"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    d[key] = []
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SessionSummary:
    """Structured session summary (inspired by claude-mem)."""
    id: int = 0
    session_id: str = ""
    request: str = ""          # What the user asked for
    investigated: str = ""     # What was explored/researched
    learned: str = ""          # Key insights gained
    completed: str = ""        # What was accomplished
    next_steps: str = ""       # Suggested follow-ups
    notes: str = ""            # Additional context
    turn_count: int = 0
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SessionSummary":
        d = dict(row)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ProjectContext:
    """Per-project working context."""
    name: str = ""
    working_dir: str = ""
    tech_stack: list = field(default_factory=list)
    recent_files: list = field(default_factory=list)
    key_facts: list = field(default_factory=list)
    last_active: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Privacy helpers
# ---------------------------------------------------------------------------

def strip_private(text: str) -> str:
    """Remove <private>...</private> blocks before storage."""
    if not text:
        return text
    return _PRIVATE_RE.sub("[REDACTED]", text)


def content_hash(session_id: str, title: str, content: str) -> str:
    """SHA-256 hash for deduplication."""
    payload = f"{session_id}|{title}|{content}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# SQLite Memory Store
# ---------------------------------------------------------------------------

class MemoryStore:
    """
    SQLite-backed persistent agent memory with FTS5 search.

    Usage:
        mem = MemoryStore("config/memory.db")
        mem.open()

        # Store observations
        mem.add_observation("fact", "User prefers Python", content="Always uses type hints",
                           concepts=["preference"])
        mem.add_observation("decision", "Chose Flask over FastAPI",
                           content="Flask is simpler for embedded SPA, no async needed",
                           concepts=["trade-off", "why-it-exists"])

        # Structured session summary
        mem.add_session_summary(session_id="abc123", request="Build USB AI assistant",
                                completed="Implemented 12 core modules",
                                learned="WMI polling needs ctypes fallback on Windows")

        # Search
        results = mem.search("Flask")
        results = mem.search_fts("Python type hints")

        # Context injection
        block = mem.build_context_block(max_tokens=800)

        mem.close()
    """

    MAX_OBSERVATIONS = 500
    MAX_SUMMARIES = 100
    DECAY_RATE = 0.98
    MIN_RELEVANCE = 0.05

    def __init__(self, db_path: str = None):
        if db_path is None:
            project_root = Path(__file__).resolve().parent.parent
            db_path = str(project_root / DEFAULT_DB_FILE)

        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._session_id = f"s_{int(time.time())}"
        self._projects: dict[str, ProjectContext] = {}

    # ------------------------------------------------------------------
    # Database lifecycle
    # ------------------------------------------------------------------

    def open(self):
        """Open (or create) the SQLite database."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # Performance: WAL mode, memory-map, bigger cache
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA mmap_size=67108864")   # 64MB
        self._conn.execute("PRAGMA cache_size=-8000")      # 8MB
        self._migrate()
        self._load_projects()
        logger.info("Memory DB opened: %s", self.db_path)

    # Alias for backward compat
    def load(self) -> bool:
        """Open DB and return True. Alias for open()."""
        try:
            self.open()
            return True
        except Exception as e:
            logger.warning("Failed to open memory DB: %s", e)
            return False

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _migrate(self):
        """Create tables if they don't exist."""
        c = self._conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                obs_type TEXT NOT NULL DEFAULT 'fact',
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                facts TEXT DEFAULT '[]',
                concepts TEXT DEFAULT '[]',
                tags TEXT DEFAULT '[]',
                source TEXT DEFAULT 'agent',
                files_read TEXT DEFAULT '[]',
                files_modified TEXT DEFAULT '[]',
                content_hash TEXT DEFAULT '',
                discovery_tokens INTEGER DEFAULT 0,
                relevance REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 0,
                last_accessed REAL DEFAULT 0.0,
                created_at REAL NOT NULL,
                session_id TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                request TEXT DEFAULT '',
                investigated TEXT DEFAULT '',
                learned TEXT DEFAULT '',
                completed TEXT DEFAULT '',
                next_steps TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                turn_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                working_dir TEXT DEFAULT '',
                tech_stack TEXT DEFAULT '[]',
                recent_files TEXT DEFAULT '[]',
                key_facts TEXT DEFAULT '[]',
                last_active REAL DEFAULT 0.0
            );

            CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(obs_type);
            CREATE INDEX IF NOT EXISTS idx_obs_hash ON observations(content_hash);
            CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_obs_relevance ON observations(relevance DESC);
            CREATE INDEX IF NOT EXISTS idx_sum_created ON session_summaries(created_at DESC);
        """)

        # FTS5 virtual tables (if supported)
        try:
            c.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts
                    USING fts5(title, content, facts, tags, content=observations, content_rowid=id);

                -- Sync triggers
                CREATE TRIGGER IF NOT EXISTS obs_ai AFTER INSERT ON observations BEGIN
                    INSERT INTO observations_fts(rowid, title, content, facts, tags)
                    VALUES (new.id, new.title, new.content, new.facts, new.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS obs_ad AFTER DELETE ON observations BEGIN
                    INSERT INTO observations_fts(observations_fts, rowid, title, content, facts, tags)
                    VALUES ('delete', old.id, old.title, old.content, old.facts, old.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS obs_au AFTER UPDATE ON observations BEGIN
                    INSERT INTO observations_fts(observations_fts, rowid, title, content, facts, tags)
                    VALUES ('delete', old.id, old.title, old.content, old.facts, old.tags);
                    INSERT INTO observations_fts(rowid, title, content, facts, tags)
                    VALUES (new.id, new.title, new.content, new.facts, new.tags);
                END;
            """)
            self._has_fts = True
        except sqlite3.OperationalError:
            logger.info("FTS5 not available -- falling back to LIKE search")
            self._has_fts = False

        c.commit()

    def _load_projects(self):
        """Load project contexts from DB."""
        rows = self._conn.execute("SELECT * FROM projects").fetchall()
        for row in rows:
            d = dict(row)
            for key in ("tech_stack", "recent_files", "key_facts"):
                if isinstance(d[key], str):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        d[key] = []
            self._projects[d["name"]] = ProjectContext(**{
                k: v for k, v in d.items() if k in ProjectContext.__dataclass_fields__
            })

    # ------------------------------------------------------------------
    # Add observations
    # ------------------------------------------------------------------

    def add_observation(self, obs_type: str, title: str, content: str = "",
                        facts: list = None, concepts: list = None, tags: list = None,
                        source: str = "agent", files_read: list = None,
                        files_modified: list = None, discovery_tokens: int = 0) -> Optional[Observation]:
        """
        Store an observation with content-hash deduplication.

        Args:
            obs_type: One of OBSERVATION_TYPES
            title: Short descriptive title
            content: Full narrative / detail
            facts: List of atomic fact strings
            concepts: List from CONCEPT_TAGS
            tags: Free-form tags
            source: "agent", "user", "auto", "tool"
            files_read: Files examined
            files_modified: Files changed
            discovery_tokens: Tokens spent to produce this

        Returns:
            The Observation, or None if deduplicated away.
        """
        # Sanitize
        title = strip_private(title)
        content = strip_private(content)
        facts = facts or []
        concepts = concepts or []
        tags = tags or []

        # Content-hash dedup
        chash = content_hash(self._session_id, title, content)
        existing = self._conn.execute(
            "SELECT id, created_at FROM observations WHERE content_hash = ? ORDER BY created_at DESC LIMIT 1",
            (chash,)
        ).fetchone()

        if existing:
            age = time.time() - existing["created_at"]
            if age < DEDUP_WINDOW_S:
                logger.debug("Dedup skip (hash=%s, age=%.0fs)", chash[:8], age)
                return None
            # Older duplicate: boost relevance instead of re-adding
            self._conn.execute(
                "UPDATE observations SET relevance = MIN(1.0, relevance + 0.1), access_count = access_count + 1 WHERE id = ?",
                (existing["id"],)
            )
            self._conn.commit()
            return None

        now = time.time()
        cur = self._conn.execute(
            """INSERT INTO observations
               (obs_type, title, content, facts, concepts, tags, source,
                files_read, files_modified, content_hash, discovery_tokens,
                relevance, access_count, last_accessed, created_at, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 0, 0.0, ?, ?)""",
            (obs_type, title, content,
             json.dumps(facts), json.dumps(concepts), json.dumps(tags),
             source,
             json.dumps(files_read or []), json.dumps(files_modified or []),
             chash, discovery_tokens, now, self._session_id)
        )
        self._conn.commit()
        self._enforce_limits()

        obs = Observation(
            id=cur.lastrowid, obs_type=obs_type, title=title, content=content,
            facts=facts, concepts=concepts, tags=tags, source=source,
            content_hash=chash, created_at=now, session_id=self._session_id,
        )
        logger.debug("Added observation [%s]: %s", obs_type, title[:60])
        return obs

    # Convenience aliases (backward-compatible with v1 API)
    def add_fact(self, content: str, tags: list = None, source: str = "agent") -> Optional[Observation]:
        return self.add_observation("fact", content[:80], content, tags=tags, source=source)

    def add_note(self, content: str, tags: list = None) -> Optional[Observation]:
        return self.add_observation("note", content[:80], content, tags=tags, source="user")

    def add_summary(self, content: str, source: str = "auto") -> Optional[Observation]:
        return self.add_observation("summary", "Session summary", content, source=source)

    # ------------------------------------------------------------------
    # Structured session summaries
    # ------------------------------------------------------------------

    def add_session_summary(self, session_id: str = "", request: str = "",
                            investigated: str = "", learned: str = "",
                            completed: str = "", next_steps: str = "",
                            notes: str = "", turn_count: int = 0) -> SessionSummary:
        """Store a structured session summary."""
        session_id = session_id or self._session_id
        now = time.time()

        cur = self._conn.execute(
            """INSERT INTO session_summaries
               (session_id, request, investigated, learned, completed, next_steps, notes, turn_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, strip_private(request), strip_private(investigated),
             strip_private(learned), strip_private(completed),
             strip_private(next_steps), strip_private(notes),
             turn_count, now)
        )
        self._conn.commit()

        summary = SessionSummary(
            id=cur.lastrowid, session_id=session_id, request=request,
            investigated=investigated, learned=learned, completed=completed,
            next_steps=next_steps, notes=notes, turn_count=turn_count, created_at=now,
        )
        logger.debug("Added session summary: %s", request[:60])
        return summary

    def get_recent_summaries(self, limit: int = 10) -> list[SessionSummary]:
        rows = self._conn.execute(
            "SELECT * FROM session_summaries ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [SessionSummary.from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Project context
    # ------------------------------------------------------------------

    def set_project(self, name: str, working_dir: str, tech_stack: list = None) -> ProjectContext:
        if name in self._projects:
            proj = self._projects[name]
            proj.working_dir = working_dir
            if tech_stack:
                proj.tech_stack = tech_stack
            proj.last_active = time.time()
        else:
            proj = ProjectContext(
                name=name, working_dir=working_dir,
                tech_stack=tech_stack or [], last_active=time.time(),
            )
            self._projects[name] = proj

        self._conn.execute(
            """INSERT OR REPLACE INTO projects (name, working_dir, tech_stack, recent_files, key_facts, last_active)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (proj.name, proj.working_dir, json.dumps(proj.tech_stack),
             json.dumps(proj.recent_files), json.dumps(proj.key_facts), proj.last_active)
        )
        self._conn.commit()
        return proj

    def track_file(self, project_name: str, file_path: str):
        if project_name not in self._projects:
            return
        proj = self._projects[project_name]
        if file_path in proj.recent_files:
            proj.recent_files.remove(file_path)
        proj.recent_files.insert(0, file_path)
        proj.recent_files = proj.recent_files[:20]
        self._conn.execute(
            "UPDATE projects SET recent_files = ? WHERE name = ?",
            (json.dumps(proj.recent_files), project_name)
        )
        self._conn.commit()

    def get_active_project(self) -> Optional[ProjectContext]:
        if not self._projects:
            return None
        return max(self._projects.values(), key=lambda p: p.last_active)

    @property
    def projects(self) -> dict:
        return self._projects

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_fts(self, query: str, limit: int = 10, obs_type: str = None) -> list[Observation]:
        """Full-text search using FTS5 (with fallback to LIKE)."""
        if not query.strip():
            return self.get_recent(limit)

        results = []

        if self._has_fts:
            # FTS5 search with rank
            try:
                type_filter = "AND o.obs_type = ?" if obs_type else ""
                params = [query, limit] if not obs_type else [query, obs_type, limit]

                sql = f"""
                    SELECT o.*, rank
                    FROM observations_fts fts
                    JOIN observations o ON o.id = fts.rowid
                    WHERE observations_fts MATCH ?
                    {type_filter}
                    ORDER BY rank
                    LIMIT ?
                """
                rows = self._conn.execute(sql, params).fetchall()
                results = [Observation.from_row(r) for r in rows]
            except sqlite3.OperationalError as e:
                logger.debug("FTS5 query failed, falling back to LIKE: %s", e)

        # Fallback: LIKE search
        if not results:
            results = self._search_like(query, limit, obs_type)

        # Update access counts
        now = time.time()
        for obs in results:
            self._conn.execute(
                "UPDATE observations SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (now, obs.id)
            )
        if results:
            self._conn.commit()

        return results

    # Backward-compatible alias
    def search(self, query: str, limit: int = 10) -> list[Observation]:
        return self.search_fts(query, limit)

    def _search_like(self, query: str, limit: int, obs_type: str = None) -> list[Observation]:
        """Fallback LIKE search when FTS5 is unavailable."""
        words = query.lower().split()
        if not words:
            return []

        conditions = []
        params = []
        for word in words:
            conditions.append("(LOWER(title) LIKE ? OR LOWER(content) LIKE ? OR LOWER(facts) LIKE ? OR LOWER(tags) LIKE ?)")
            pattern = f"%{word}%"
            params.extend([pattern] * 4)

        if obs_type:
            conditions.append("obs_type = ?")
            params.append(obs_type)

        params.append(limit)
        where = " AND ".join(conditions)

        rows = self._conn.execute(
            f"SELECT * FROM observations WHERE {where} ORDER BY relevance DESC, created_at DESC LIMIT ?",
            params
        ).fetchall()
        return [Observation.from_row(r) for r in rows]

    def get_by_category(self, category: str) -> list[Observation]:
        rows = self._conn.execute(
            "SELECT * FROM observations WHERE obs_type = ? ORDER BY relevance DESC, created_at DESC",
            (category,)
        ).fetchall()
        return [Observation.from_row(r) for r in rows]

    def get_recent(self, limit: int = 20) -> list[Observation]:
        rows = self._conn.execute(
            "SELECT * FROM observations ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Observation.from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Context injection (for system prompt)
    # ------------------------------------------------------------------

    def build_context_block(self, max_tokens: int = 800, query_hint: str = "") -> str:
        """
        Build a memory block for system prompt injection.

        Progressive context: recent observations (chronological timeline)
        plus query-relevant hits if a hint is provided.
        """
        max_chars = max_tokens * 4
        sections = []
        used = 0

        # 1. Key facts and preferences (highest priority)
        facts = self._conn.execute(
            """SELECT * FROM observations
               WHERE obs_type IN ('fact', 'preference', 'decision')
               ORDER BY relevance DESC, created_at DESC LIMIT 15"""
        ).fetchall()
        if facts:
            fact_lines = []
            for row in facts:
                line = f"- [{row['obs_type']}] {row['title']}"
                if row['content'] and row['content'] != row['title']:
                    line += f": {row['content'][:120]}"
                if used + len(line) > max_chars * 0.35:
                    break
                fact_lines.append(line)
                used += len(line)
            if fact_lines:
                sections.append("## Known Facts & Decisions\n" + "\n".join(fact_lines))

        # 2. Active project context
        active_proj = self.get_active_project()
        if active_proj:
            proj_lines = [f"- Project: {active_proj.name}"]
            proj_lines.append(f"- Directory: {active_proj.working_dir}")
            if active_proj.tech_stack:
                proj_lines.append(f"- Tech: {', '.join(active_proj.tech_stack)}")
            if active_proj.recent_files:
                proj_lines.append(f"- Recent: {', '.join(active_proj.recent_files[:5])}")
            block = "## Active Project\n" + "\n".join(proj_lines)
            if used + len(block) < max_chars * 0.55:
                sections.append(block)
                used += len(block)

        # 3. Recent session summaries (structured)
        summaries = self.get_recent_summaries(5)
        if summaries:
            sum_lines = []
            for s in summaries:
                parts = []
                if s.request:
                    parts.append(f"Request: {s.request[:80]}")
                if s.completed:
                    parts.append(f"Done: {s.completed[:80]}")
                if s.learned:
                    parts.append(f"Learned: {s.learned[:80]}")
                if s.next_steps:
                    parts.append(f"Next: {s.next_steps[:60]}")
                if parts:
                    line = "- " + " | ".join(parts)
                    if used + len(line) > max_chars * 0.75:
                        break
                    sum_lines.append(line)
                    used += len(line)
            if sum_lines:
                sections.append("## Previous Sessions\n" + "\n".join(sum_lines))

        # 4. Discoveries, gotchas, and patterns
        insights = self._conn.execute(
            """SELECT * FROM observations
               WHERE obs_type IN ('discovery', 'bugfix')
                  OR concepts LIKE '%gotcha%' OR concepts LIKE '%pattern%'
               ORDER BY relevance DESC, created_at DESC LIMIT 8"""
        ).fetchall()
        if insights:
            insight_lines = []
            for row in insights:
                line = f"- [{row['obs_type']}] {row['title']}"
                if used + len(line) > max_chars * 0.85:
                    break
                insight_lines.append(line)
                used += len(line)
            if insight_lines:
                sections.append("## Discoveries & Patterns\n" + "\n".join(insight_lines))

        # 5. User notes
        notes = self._conn.execute(
            "SELECT * FROM observations WHERE obs_type = 'note' ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        if notes:
            note_lines = []
            for row in notes:
                line = f"- {row['content'][:100]}"
                if used + len(line) > max_chars * 0.92:
                    break
                note_lines.append(line)
                used += len(line)
            if note_lines:
                sections.append("## User Notes\n" + "\n".join(note_lines))

        # 6. Query-relevant hits (semantic boost for current conversation)
        if query_hint and used < max_chars * 0.95:
            relevant = self.search_fts(query_hint, limit=5)
            seen_ids = set()
            # Collect IDs from sections above to avoid repeats
            for row_list in [facts, insights, notes]:
                for row in (row_list or []):
                    if hasattr(row, "keys"):
                        seen_ids.add(row["id"])
            relevant = [r for r in relevant if r.id not in seen_ids]
            if relevant:
                rel_lines = []
                for r in relevant:
                    line = f"- [{r.obs_type}] {r.title}: {r.content[:80]}"
                    if used + len(line) > max_chars:
                        break
                    rel_lines.append(line)
                    used += len(line)
                if rel_lines:
                    sections.append("## Relevant to Current Query\n" + "\n".join(rel_lines))

        if not sections:
            return ""

        return "<memory>\n" + "\n\n".join(sections) + "\n</memory>"

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def decay_relevance(self):
        """Apply time-based relevance decay."""
        now = time.time()
        self._conn.execute("""
            UPDATE observations SET relevance = MAX(?, relevance * POWER(?, (? - CASE
                WHEN last_accessed > 0 THEN last_accessed ELSE created_at END) / 86400.0))
        """, (self.MIN_RELEVANCE, self.DECAY_RATE, now))
        self._conn.commit()

    def prune(self, min_relevance: float = None):
        """Remove entries below relevance threshold (preserves user notes)."""
        threshold = min_relevance or self.MIN_RELEVANCE
        cur = self._conn.execute(
            "DELETE FROM observations WHERE relevance < ? AND source != 'user'",
            (threshold,)
        )
        if cur.rowcount > 0:
            logger.info("Pruned %d low-relevance memories", cur.rowcount)
            self._conn.commit()

    def _enforce_limits(self):
        """Drop oldest low-relevance entries if over limit."""
        count = self._conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        if count > self.MAX_OBSERVATIONS:
            excess = count - self.MAX_OBSERVATIONS
            self._conn.execute("""
                DELETE FROM observations WHERE id IN (
                    SELECT id FROM observations
                    WHERE source != 'user'
                    ORDER BY relevance ASC, created_at ASC
                    LIMIT ?
                )
            """, (excess,))
            self._conn.commit()

    def clear(self, category: str = None):
        """Clear all memories, or just a specific type."""
        if category:
            self._conn.execute("DELETE FROM observations WHERE obs_type = ?", (category,))
        else:
            self._conn.execute("DELETE FROM observations")
            self._conn.execute("DELETE FROM session_summaries")
            self._conn.execute("DELETE FROM projects")
            self._projects.clear()
        self._conn.commit()

    # ------------------------------------------------------------------
    # Export / Import
    # ------------------------------------------------------------------

    def export_json(self) -> str:
        """Export all memories as JSON for backup."""
        observations = [Observation.from_row(r).to_dict()
                        for r in self._conn.execute("SELECT * FROM observations ORDER BY created_at").fetchall()]
        summaries = [SessionSummary.from_row(r).to_dict()
                     for r in self._conn.execute("SELECT * FROM session_summaries ORDER BY created_at").fetchall()]
        projects = {k: v.to_dict() for k, v in self._projects.items()}

        return json.dumps({
            "version": 2,
            "exported_at": time.time(),
            "observations": observations,
            "session_summaries": summaries,
            "projects": projects,
        }, indent=2)

    def import_json(self, json_str: str, merge: bool = True):
        """Import memories from JSON backup."""
        data = json.loads(json_str)

        if not merge:
            self.clear()

        # Import observations
        for obs_dict in data.get("observations", []):
            # Also handle v1 format (entries with "category" field)
            obs_type = obs_dict.get("obs_type", obs_dict.get("category", "fact"))
            title = obs_dict.get("title", obs_dict.get("content", "")[:80])
            content = obs_dict.get("content", "")
            self.add_observation(
                obs_type=obs_type, title=title, content=content,
                facts=obs_dict.get("facts", []),
                concepts=obs_dict.get("concepts", []),
                tags=obs_dict.get("tags", []),
                source=obs_dict.get("source", "import"),
            )

        # Import v1 entries
        for entry_dict in data.get("entries", []):
            cat = entry_dict.get("category", "fact")
            content = entry_dict.get("content", "")
            self.add_observation(cat, content[:80], content,
                                tags=entry_dict.get("tags", []), source="import")

        # Import summaries
        for sum_dict in data.get("session_summaries", []):
            self.add_session_summary(**{k: v for k, v in sum_dict.items()
                                        if k in SessionSummary.__dataclass_fields__ and k not in ("id", "created_at")})

        # Import projects
        for name, proj_dict in data.get("projects", {}).items():
            self.set_project(name, proj_dict.get("working_dir", ""),
                            proj_dict.get("tech_stack", []))

    # ------------------------------------------------------------------
    # Stats & token economics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return memory statistics with token economics."""
        type_counts = {}
        for row in self._conn.execute("SELECT obs_type, COUNT(*) as cnt FROM observations GROUP BY obs_type").fetchall():
            type_counts[row["obs_type"]] = row["cnt"]

        total = self._conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        sum_count = self._conn.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0]
        total_tokens = self._conn.execute("SELECT COALESCE(SUM(discovery_tokens), 0) FROM observations").fetchone()[0]

        avg_rel = 0.0
        if total > 0:
            avg_rel = self._conn.execute("SELECT AVG(relevance) FROM observations").fetchone()[0] or 0.0

        return {
            "total_observations": total,
            "type_counts": type_counts,
            "session_summaries": sum_count,
            "projects": len(self._projects),
            "avg_relevance": round(avg_rel, 3),
            "total_discovery_tokens": total_tokens,
            "db_size_kb": round(os.path.getsize(self.db_path) / 1024, 1) if os.path.isfile(self.db_path) else 0,
        }

    def auto_save(self):
        """No-op for SQLite (writes are immediate). Kept for API compat."""
        pass

    def save(self):
        """Explicit checkpoint for WAL mode."""
        if self._conn:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    @property
    def entries(self) -> list[Observation]:
        """Backward-compat property: return all observations."""
        return self.get_recent(100)


# ---------------------------------------------------------------------------
# Agent memory tools (registered as agent tools)
# ---------------------------------------------------------------------------

def get_memory_tools(memory_store: MemoryStore) -> list[dict]:
    """
    Return tool definitions that let the agent read/write memory.
    """
    return [
        {
            "name": "memory_store",
            "description": (
                "Store an observation in long-term memory. Use this when you learn something "
                "important about the user, their project, a decision made, a bug fixed, or a "
                "pattern discovered. Memories persist across sessions on the USB drive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short title for the memory (max 80 chars)"
                    },
                    "content": {
                        "type": "string",
                        "description": "Full detail of what to remember"
                    },
                    "obs_type": {
                        "type": "string",
                        "enum": list(OBSERVATION_TYPES),
                        "description": "Type: fact, discovery, decision, bugfix, note, preference, summary"
                    },
                    "concepts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Concept tags: how-it-works, problem-solution, gotcha, pattern, trade-off, preference"
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Free-form tags for categorization"
                    }
                },
                "required": ["title", "content"]
            },
            "_handler": lambda title, content, obs_type="fact", concepts=None, tags=None: (
                _tool_memory_store(memory_store, title, content, obs_type, concepts, tags)
            ),
        },
        {
            "name": "memory_search",
            "description": (
                "Search long-term memory using full-text search. Find relevant facts, decisions, "
                "discoveries, and notes from current and previous sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (supports natural language)"
                    },
                    "obs_type": {
                        "type": "string",
                        "enum": list(OBSERVATION_TYPES),
                        "description": "Filter by type (optional)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10)"
                    }
                },
                "required": ["query"]
            },
            "_handler": lambda query, obs_type=None, limit=10: (
                _tool_memory_search(memory_store, query, obs_type, limit)
            ),
        },
        {
            "name": "memory_list",
            "description": "List stored memories, optionally filtered by type. Shows recent entries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "obs_type": {
                        "type": "string",
                        "enum": list(OBSERVATION_TYPES) + ["project"],
                        "description": "Filter by type (omit for all recent)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max entries (default 20)"
                    }
                },
            },
            "_handler": lambda obs_type=None, limit=20: _tool_memory_list(memory_store, obs_type, limit),
        },
    ]


def _tool_memory_store(store: MemoryStore, title: str, content: str,
                        obs_type: str = "fact", concepts=None, tags=None) -> str:
    obs = store.add_observation(obs_type, title, content,
                                 concepts=concepts, tags=tags, source="agent")
    if obs:
        return f"Stored [{obs_type}]: {title}"
    return f"Memory deduplicated (already stored similar content)"


def _tool_memory_search(store: MemoryStore, query: str, obs_type: str = None, limit: int = 10) -> str:
    results = store.search_fts(query, limit=limit, obs_type=obs_type)
    if not results:
        return "No matching memories found."
    lines = []
    for r in results:
        age_days = (time.time() - r.created_at) / 86400
        line = f"[{r.obs_type}] {r.title}"
        if r.content and r.content != r.title:
            line += f": {r.content[:120]}"
        line += f" ({age_days:.0f}d ago)"
        lines.append(line)
    return "\n".join(lines)


def _tool_memory_list(store: MemoryStore, obs_type: str = None, limit: int = 20) -> str:
    if obs_type == "project":
        projects = store.projects
        if not projects:
            return "No projects in memory."
        lines = []
        for name, proj in projects.items():
            lines.append(f"Project: {name}")
            lines.append(f"  Dir: {proj.working_dir}")
            if proj.tech_stack:
                lines.append(f"  Tech: {', '.join(proj.tech_stack)}")
            if proj.recent_files:
                lines.append(f"  Recent: {', '.join(proj.recent_files[:5])}")
        return "\n".join(lines)

    if obs_type:
        entries = store.get_by_category(obs_type)[:limit]
    else:
        entries = store.get_recent(limit)

    if not entries:
        return f"No memories found{f' of type {obs_type}' if obs_type else ''}."

    lines = []
    for e in entries:
        line = f"[{e.obs_type}] {e.title}"
        if e.concepts:
            line += f" ({', '.join(e.concepts)})"
        lines.append(line)
    return "\n".join(lines)
