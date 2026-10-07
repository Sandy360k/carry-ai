"""
carry-ai/integrations/python_sandbox.py — run_python tool (pydantic-monty)
===========================================================================

Gives the agent a way to run the Python it writes (calculations, parsing,
data wrangling, quick algorithms) without giving that code the host PC.

Code runs in Monty (github.com/pydantic/monty), a Python interpreter in
Rust that executes in its own worker process:

    - no access to the host filesystem, network, environment or processes;
      the only folder it sees is /work, a scratch folder inside the
      wiped-on-eject session dir
    - time and memory limits enforced inside the worker, plus a host-side
      timeout that kills a hung worker
    - a crashing worker is replaced; carry-ai is never at risk
    - a subset of Python 3: json, re, math, datetime, collections,
      itertools, functools, random, dataclasses, typing, base64, pathlib,
      os.path, sys, time (no csv / statistics / hashlib / third-party
      packages, no subprocess or sockets)

Variables persist between calls (one REPL session per agent), so the model
can build up a computation step by step; ``reset`` starts fresh.

Settings (``sandbox.*``): enabled, timeout_s, max_memory_mb.

pydantic-monty is optional: without it (or its worker binary) the tool is
not registered. With ``pip install --target`` (the USB layout) the worker
binary lands in ``<site-packages>/bin``; ``find_monty_binary`` looks there.
"""

import atexit
import logging
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

try:
    import pydantic_monty as _monty
except ImportError:
    _monty = None

log = logging.getLogger("carry-ai.integrations.python_sandbox")

DEFAULT_TIMEOUT_S = 10
DEFAULT_MEMORY_MB = 256
MAX_OUTPUT_CHARS = 20_000
WORK_WRITE_LIMIT = 100 * 1024 * 1024      # bytes written to /work per run
VIRTUAL_WORK = "/work"


def find_monty_binary() -> str | None:
    """Path of the `monty` worker binary, or None.

    Order: $MONTY_BIN, then ``bin/`` or ``Scripts/`` next to the installed
    package (pip --target puts it there on the USB), then PATH.
    """
    env = os.environ.get("MONTY_BIN")
    if env and Path(env).is_file():
        return env
    names = ("monty.exe",) if sys.platform == "win32" else ("monty",)
    roots = [Path(p) for p in sys.path if p]
    if _monty is not None:
        roots.insert(0, Path(_monty.__file__).resolve().parent.parent)
    for root in roots:
        for sub in ("bin", "Scripts"):
            for name in names:
                candidate = root / sub / name
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("monty")


def sandbox_settings() -> dict:
    try:
        from config.settings import load_settings
        return load_settings().to_dict().get("sandbox", {}) or {}
    except Exception:
        return {}


def _work_dir() -> Path:
    """Scratch folder for /work: inside the session dir, wiped on eject."""
    session = os.environ.get("CARRY_AI_SESSION_DIR") or \
        os.path.join(tempfile.gettempdir(), "ai_session")
    path = Path(session) / "sandbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clip(text: str, limit: int | None = None) -> str:
    limit = limit or MAX_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [output truncated at {limit} characters]"


class PythonSandbox:
    """One Monty worker pool + a persistent REPL session."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_memory_mb: int = DEFAULT_MEMORY_MB,
                 work_dir: Path | None = None, binary_path: str | None = None):
        if _monty is None:
            raise ImportError("pydantic-monty is not installed")
        self.timeout_s = float(timeout_s)
        self.max_memory_mb = int(max_memory_mb)
        self._work_dir = work_dir
        self._binary = binary_path or find_monty_binary()
        if not self._binary:
            raise FileNotFoundError("monty worker binary not found")
        self._pool = None
        self._session = None
        self._mount = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------

    def _ensure_session(self):
        if self._pool is None:
            self._pool = _monty.Monty(
                binary_path=self._binary, min_processes=1, max_processes=1,
                request_timeout=self.timeout_s + 5)
            self._pool.__enter__()
        if self._mount is None:
            self._mount = _monty.MountDir(
                host_path=self._work_dir or _work_dir(), virtual_path=VIRTUAL_WORK,
                mode="read-write", write_bytes_limit=WORK_WRITE_LIMIT)
        if self._session is None:
            self._session = self._pool.checkout(
                script_name="agent.py",
                limits={"max_feed_duration_secs": self.timeout_s,
                        "max_memory": self.max_memory_mb * 1024 * 1024})
            self._session.__enter__()
        return self._session

    def reset(self) -> None:
        """Forget all variables (the next run starts a fresh session)."""
        session, self._session = self._session, None
        if session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception as e:
                log.debug("Closing sandbox session: %s", e)

    def close(self) -> None:
        self.reset()
        for obj in (self._mount, self._pool):
            if obj is None:
                continue
            try:
                obj.close() if obj is self._mount else obj.__exit__(None, None, None)
            except Exception as e:
                log.debug("Closing sandbox: %s", e)
        self._mount = self._pool = None

    # -- run -------------------------------------------------------------

    def run(self, code: str, reset: bool = False) -> str:
        """Run *code*; returns printed output plus the last expression's value."""
        with self._lock:
            if reset:
                self.reset()
            output = _monty.CollectString(max_bytes=MAX_OUTPUT_CHARS * 4)
            try:
                value = self._ensure_session().feed_run(
                    code, print_callback=output, mount=self._mount, cwd=VIRTUAL_WORK)
            except _monty.MontyCrashedError as e:
                self.reset()
                return _clip(output.output + f"\n[sandbox stopped: {e}] "
                             "Variables were reset.").strip()
            except _monty.MontyError as e:
                text = e.display() if hasattr(e, "display") else str(e)
                if "TimeoutError" in text or "MemoryError" in text:
                    # A limit stops code mid-operation; don't trust that heap
                    self.reset()
                    text += "\n(Variables were reset after hitting a limit.)"
                return _clip((output.output + text).strip())

            parts = [output.output.rstrip("\n")] if output.output else []
            if value is not None:
                parts.append(f"→ {value!r}")
            return _clip("\n".join(parts) or "(no output)")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

_SANDBOX: PythonSandbox | None = None


def _shutdown() -> None:
    global _SANDBOX
    if _SANDBOX is not None:
        _SANDBOX.close()
        _SANDBOX = None


def is_available() -> bool:
    return _monty is not None and find_monty_binary() is not None


TOOL_DESCRIPTION = (
    "Run Python code in a secure sandbox and get its printed output and the value "
    "of the last expression. Use it for calculations, parsing JSON/text, data "
    "transformations and small algorithms — prefer it over the shell for anything "
    "computational. Variables and functions persist between calls (pass reset=true "
    "to start fresh). It is a subset of Python 3 with these modules: json, re, math, "
    "datetime, collections, itertools, functools, random, dataclasses, typing, "
    "base64, pathlib, os.path, sys, time (no csv, statistics, hashlib, third-party "
    "packages, network or subprocess). The only folder is /work, a scratch folder "
    "(the working directory) that is wiped when the USB is removed. "
    "Limits: {timeout} s and {memory} MB per run."
)


def register_python_sandbox_tool(register_tool) -> bool:
    """Register run_python if pydantic-monty and its worker are present."""
    global _SANDBOX
    prefs = sandbox_settings()
    if not prefs.get("enabled", True):
        log.info("Python sandbox disabled in settings.")
        return False
    if not is_available():
        log.debug("Python sandbox not available (pydantic-monty not installed).")
        return False
    timeout = prefs.get("timeout_s", DEFAULT_TIMEOUT_S)
    memory = prefs.get("max_memory_mb", DEFAULT_MEMORY_MB)

    def run_python(code: str, reset: bool = False) -> str:
        global _SANDBOX
        if _SANDBOX is None:
            _SANDBOX = PythonSandbox(timeout_s=timeout, max_memory_mb=memory)
        return _SANDBOX.run(code, reset=bool(reset))

    register_tool(
        name="run_python",
        description=TOOL_DESCRIPTION.format(timeout=timeout, memory=memory),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to run"},
                "reset": {"type": "boolean",
                          "description": "Forget earlier variables first (default false)"},
            },
            "required": ["code"],
        },
        execute_fn=run_python,
        required=["code"],
    )
    atexit.register(_shutdown)
    return True
