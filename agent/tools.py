"""
carry-ai/agent/tools.py — Tool Definitions & Execution
========================================================

Defines the tools available to the agent for system interaction.
Each tool has:
    - A JSON schema (OpenAI function calling format) for the LLM
    - An execute function that runs the tool and returns a string result

Tool Categories:
    Shell & Process:  shell
    File Operations:  read_file, write_file, edit_file, list_files, search_files
    Screen/Input:     screenshot, click, type_text
    Clipboard:        clipboard_read, clipboard_write
    Web:              web_fetch, browse
    Utility:          get_system_info

The registry is extensible — plugins and MCP servers add tools at runtime
via register_tool().
"""

import base64
import fnmatch
import glob as glob_module
import io
import json
import logging
import os
import platform
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("carry-ai.agent.tools")

# Default timeout for shell commands (seconds)
SHELL_TIMEOUT = 30

# Max file read size (chars)
MAX_READ_SIZE = 100_000

# Max search results
MAX_SEARCH_RESULTS = 50


# ===================================================================
# Tool registry
# ===================================================================

@dataclass
class Tool:
    """A registered tool with schema and executor."""
    name: str
    description: str
    parameters: dict           # JSON Schema for parameters
    execute_fn: callable       # fn(**kwargs) -> str
    required: list[str] = None # Required parameter names

    def schema(self) -> dict:
        """Return the OpenAI function calling schema."""
        s = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
        return s


# Global tool registry: name -> Tool
TOOL_REGISTRY: dict[str, Tool] = {}

# Tools that cannot touch this PC's files, programs, screen or input, so they
# stay available in sandbox mode (Agent.set_sandboxed). Everything else —
# shell, file tools, click/type, clipboard, browse, plugins, MCP tools — is
# hidden from the model and refused while sandboxed. Allow-list on purpose:
# a new tool is host-touching until it is marked safe here or registered
# with sandbox_safe=True.
SANDBOX_SAFE_TOOLS: set[str] = {
    "run_python",            # Monty sandbox; /work only (no /host while sandboxed)
    "web_fetch", "scrape",   # network reads, nothing local
    "get_system_info", "model_recommend",
    "memory_store", "memory_search", "memory_list",
}


def register_tool(name: str, description: str, parameters: dict,
                  execute_fn: callable, required: list[str] | None = None,
                  sandbox_safe: bool = False) -> None:
    """Register a tool (used by built-ins, plugins, and MCP bridge).

    sandbox_safe: the tool cannot reach the host PC (see SANDBOX_SAFE_TOOLS).
    """
    TOOL_REGISTRY[name] = Tool(
        name=name,
        description=description,
        parameters=parameters,
        execute_fn=execute_fn,
        required=required,
    )
    if sandbox_safe:
        SANDBOX_SAFE_TOOLS.add(name)


def is_sandbox_safe(name: str) -> bool:
    return name in SANDBOX_SAFE_TOOLS


def get_tool_definitions(sandboxed: bool = False) -> list[dict]:
    """Return registered tool schemas for the LLM (only safe ones if sandboxed)."""
    return [tool.schema() for tool in TOOL_REGISTRY.values()
            if not sandboxed or tool.name in SANDBOX_SAFE_TOOLS]


def execute_tool(name: str, args: dict | str) -> str:
    """Execute a named tool with given arguments.

    Args:
        name: Tool name.
        args: Dict of arguments, or JSON string.

    Returns:
        String result from the tool.

    Raises:
        KeyError: If tool not found.
        Exception: Propagated from tool execution.
    """
    if name not in TOOL_REGISTRY:
        return f"Error: Unknown tool '{name}'. Available: {', '.join(TOOL_REGISTRY.keys())}"

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"raw": args}

    tool = TOOL_REGISTRY[name]
    try:
        result = tool.execute_fn(**args)
        return str(result)
    except Exception as e:
        log.error("Tool '%s' failed: %s", name, e, exc_info=True)
        return f"Error executing {name}: {e}"


# ===================================================================
# Tool implementations
# ===================================================================

# --- Shell ---

def _tool_shell(command: str, timeout: int = SHELL_TIMEOUT) -> str:
    """Execute a shell command and return stdout + stderr."""
    host_os = platform.system().lower()

    if host_os == "windows":
        shell_cmd = ["powershell", "-NoProfile", "-Command", command]
    else:
        shell_cmd = ["bash", "-c", command]

    try:
        result = subprocess.run(
            shell_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.getcwd(),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            if output:
                output += "\n"
            output += f"[stderr] {result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# --- File Operations ---

def _tool_read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read file contents with line numbers."""
    p = Path(path).expanduser()
    if not p.is_file():
        return f"Error: File not found: {path}"

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return f"Error: Permission denied: {path}"
    except Exception as e:
        return f"Error reading file: {e}"

    if len(content) > MAX_READ_SIZE:
        content = content[:MAX_READ_SIZE]
        truncated = True
    else:
        truncated = False

    lines = content.splitlines()

    if offset > 0:
        lines = lines[offset:]
    if limit > 0:
        lines = lines[:limit]

    numbered = [f"{i + offset + 1:>5}\t{line}" for i, line in enumerate(lines)]
    result = "\n".join(numbered)

    if truncated:
        result += f"\n\n[Truncated at {MAX_READ_SIZE} chars. Use offset/limit for large files.]"

    return result


def _tool_write_file(path: str, content: str) -> str:
    """Write content to a file (creates parent dirs if needed)."""
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {path}"
    except PermissionError:
        return f"Error: Permission denied: {path}"
    except Exception as e:
        return f"Error writing file: {e}"


def _tool_edit_file(path: str, old_string: str, new_string: str) -> str:
    """Find and replace text in a file."""
    p = Path(path).expanduser()
    if not p.is_file():
        return f"Error: File not found: {path}"

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"

    count = content.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {path}"

    new_content = content.replace(old_string, new_string, 1)

    try:
        p.write_text(new_content, encoding="utf-8")
        return f"Replaced 1 occurrence in {path} ({count} total matches)"
    except Exception as e:
        return f"Error writing file: {e}"


def _tool_list_files(pattern: str, path: str = ".") -> str:
    """List files matching a glob pattern."""
    base = Path(path).expanduser()

    if not base.is_dir():
        return f"Error: Directory not found: {path}"

    full_pattern = str(base / pattern)
    matches = sorted(glob_module.glob(full_pattern, recursive=True))

    if not matches:
        return f"No files matching '{pattern}' in {path}"

    # Limit results
    total = len(matches)
    if total > MAX_SEARCH_RESULTS:
        matches = matches[:MAX_SEARCH_RESULTS]

    lines = [str(Path(m).relative_to(base)) for m in matches]
    result = "\n".join(lines)
    if total > MAX_SEARCH_RESULTS:
        result += f"\n\n[Showing {MAX_SEARCH_RESULTS} of {total} matches]"

    return result


def _tool_search_files(pattern: str, path: str = ".", glob_filter: str = "") -> str:
    """Search file contents with regex pattern."""
    base = Path(path).expanduser()
    if not base.is_dir():
        return f"Error: Directory not found: {path}"

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Error: Invalid regex: {e}"

    results = []
    files_searched = 0

    for root, dirs, files in os.walk(base):
        # Skip hidden dirs and common noise
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git")]

        for fname in files:
            if glob_filter and not fnmatch.fnmatch(fname, glob_filter):
                continue

            fpath = Path(root) / fname
            files_searched += 1

            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except (PermissionError, OSError):
                continue

            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = fpath.relative_to(base)
                    results.append(f"{rel}:{i}: {line.strip()}")

                    if len(results) >= MAX_SEARCH_RESULTS:
                        break

            if len(results) >= MAX_SEARCH_RESULTS:
                break
        if len(results) >= MAX_SEARCH_RESULTS:
            break

    if not results:
        return f"No matches for '{pattern}' in {files_searched} files"

    header = f"Found {len(results)} match(es) in {files_searched} files:\n"
    return header + "\n".join(results)


# --- Screen / Input ---

def _tool_screenshot() -> str:
    """Capture a screenshot and return as base64-encoded PNG."""
    try:
        import pyautogui
        img = pyautogui.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except ImportError:
        return "Error: pyautogui not installed. pip install pyautogui"
    except Exception as e:
        return f"Error capturing screenshot: {e}"


def _tool_click(x: int, y: int, button: str = "left") -> str:
    """Click at screen coordinates."""
    try:
        import pyautogui
        pyautogui.click(x, y, button=button)
        return f"Clicked ({x}, {y}) with {button} button"
    except ImportError:
        return "Error: pyautogui not installed"
    except Exception as e:
        return f"Error: {e}"


def _tool_type_text(text: str, interval: float = 0.02) -> str:
    """Type text via keyboard input."""
    try:
        import pyautogui
        pyautogui.typewrite(text, interval=interval) if text.isascii() else pyautogui.write(text)
        return f"Typed {len(text)} character(s)"
    except ImportError:
        return "Error: pyautogui not installed"
    except Exception as e:
        return f"Error: {e}"


# --- Clipboard ---

def _tool_clipboard_read() -> str:
    """Read current clipboard contents."""
    try:
        import pyperclip
        content = pyperclip.paste()
        return content if content else "(clipboard is empty)"
    except ImportError:
        # Fallback: platform commands
        host_os = platform.system().lower()
        try:
            if host_os == "windows":
                r = subprocess.run(["powershell", "-Command", "Get-Clipboard"],
                                   capture_output=True, text=True, timeout=5)
                return r.stdout.strip() or "(clipboard is empty)"
            else:
                r = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                                   capture_output=True, text=True, timeout=5)
                return r.stdout.strip() or "(clipboard is empty)"
        except Exception:
            return "Error: Cannot read clipboard (install pyperclip)"


def _tool_clipboard_write(text: str) -> str:
    """Write text to clipboard."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return f"Copied {len(text)} chars to clipboard"
    except ImportError:
        host_os = platform.system().lower()
        try:
            if host_os == "windows":
                subprocess.run(["powershell", "-Command", f"Set-Clipboard '{text}'"],
                               capture_output=True, timeout=5)
            else:
                proc = subprocess.Popen(["xclip", "-selection", "clipboard"],
                                        stdin=subprocess.PIPE)
                proc.communicate(text.encode(), timeout=5)
            return f"Copied {len(text)} chars to clipboard"
        except Exception:
            return "Error: Cannot write to clipboard (install pyperclip)"


# --- Web ---

def _tool_web_fetch(url: str, max_length: int = 50000) -> str:
    """Fetch a URL and return the text content."""
    try:
        import requests
        resp = requests.get(url, timeout=30, headers={"User-Agent": "carry-ai/1.0"})
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")

        if "json" in content_type:
            text = json.dumps(resp.json(), indent=2)
        else:
            text = resp.text

        if len(text) > max_length:
            text = text[:max_length] + f"\n\n[Truncated at {max_length} chars]"

        return text
    except ImportError:
        return "Error: requests not installed. pip install requests"
    except Exception as e:
        return f"Error fetching {url}: {e}"


def _tool_browse(url: str) -> str:
    """Open a URL in a throwaway-profile browser window.

    The host's default browser would keep the URL in its history after
    eject; ui.browser.open_private puts the profile in the session dir.
    """
    try:
        from ui.browser import open_private
        session_dir = (os.environ.get("CARRY_AI_SESSION_DIR")
                       or os.path.join(tempfile.gettempdir(), "ai_session"))
        how = open_private(url, session_dir)
        return f"Opened {url} ({how})"
    except Exception as e:
        return f"Error opening browser: {e}"


# --- Utility ---

def _tool_get_system_info() -> str:
    """Return system information for context."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        ram_info = f"RAM: {mem.total / (1024**3):.1f} GB total, {mem.available / (1024**3):.1f} GB available"
        cpu_info = f"CPU: {psutil.cpu_count()} cores, {psutil.cpu_percent()}% usage"
    except ImportError:
        ram_info = "RAM: (psutil not installed)"
        cpu_info = "CPU: (psutil not installed)"

    return "\n".join([
        f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python: {platform.python_version()}",
        cpu_info,
        ram_info,
        f"CWD: {os.getcwd()}",
        f"User: {os.environ.get('USER', os.environ.get('USERNAME', 'unknown'))}",
    ])


# ===================================================================
# Register all built-in tools
# ===================================================================

def _register_builtins() -> None:
    """Register all built-in tools in the global registry."""

    register_tool(
        name="shell",
        description="Execute a shell command (PowerShell on Windows, Bash on Linux). Returns stdout, stderr, and exit code.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)", "default": 30},
            },
            "required": ["command"],
        },
        execute_fn=_tool_shell,
    )

    register_tool(
        name="read_file",
        description="Read a file's contents with line numbers. Use offset and limit for large files.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
                "offset": {"type": "integer", "description": "Line offset to start from (0-based)", "default": 0},
                "limit": {"type": "integer", "description": "Max lines to return (0 = all)", "default": 0},
            },
            "required": ["path"],
        },
        execute_fn=_tool_read_file,
    )

    register_tool(
        name="write_file",
        description="Write content to a file. Creates the file and parent directories if they don't exist.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write to"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        execute_fn=_tool_write_file,
    )

    register_tool(
        name="edit_file",
        description="Find and replace text in a file. Replaces the first occurrence of old_string with new_string.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit"},
                "old_string": {"type": "string", "description": "Text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        execute_fn=_tool_edit_file,
    )

    register_tool(
        name="list_files",
        description="List files matching a glob pattern (e.g., '**/*.py', '*.txt'). Searches recursively.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match"},
                "path": {"type": "string", "description": "Base directory to search in", "default": "."},
            },
            "required": ["pattern"],
        },
        execute_fn=_tool_list_files,
    )

    register_tool(
        name="search_files",
        description="Search file contents using a regex pattern. Returns matching lines with file paths and line numbers.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Base directory to search in", "default": "."},
                "glob_filter": {"type": "string", "description": "Glob filter for filenames (e.g., '*.py')", "default": ""},
            },
            "required": ["pattern"],
        },
        execute_fn=_tool_search_files,
    )

    register_tool(
        name="screenshot",
        description="Capture a screenshot of the entire screen. Returns a base64-encoded PNG image.",
        parameters={"type": "object", "properties": {}},
        execute_fn=_tool_screenshot,
    )

    register_tool(
        name="click",
        description="Click the mouse at screen coordinates (x, y).",
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            },
            "required": ["x", "y"],
        },
        execute_fn=_tool_click,
    )

    register_tool(
        name="type_text",
        description="Type text using keyboard input. Simulates keypresses.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["text"],
        },
        execute_fn=_tool_type_text,
    )

    register_tool(
        name="clipboard_read",
        description="Read the current system clipboard contents.",
        parameters={"type": "object", "properties": {}},
        execute_fn=_tool_clipboard_read,
    )

    register_tool(
        name="clipboard_write",
        description="Write text to the system clipboard.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to copy to clipboard"},
            },
            "required": ["text"],
        },
        execute_fn=_tool_clipboard_write,
    )

    register_tool(
        name="web_fetch",
        description="Fetch a URL and return the text/HTML content. Supports JSON (auto-formatted).",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "max_length": {"type": "integer", "description": "Max response length in chars", "default": 50000},
            },
            "required": ["url"],
        },
        execute_fn=_tool_web_fetch,
    )

    register_tool(
        name="browse",
        description="Open a URL in the host machine's default web browser.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open"},
            },
            "required": ["url"],
        },
        execute_fn=_tool_browse,
    )

    register_tool(
        name="get_system_info",
        description="Get system information: OS, CPU, RAM, Python version, current directory, user.",
        parameters={"type": "object", "properties": {}},
        execute_fn=_tool_get_system_info,
    )


# Auto-register builtins on import
_register_builtins()


# ===================================================================
# Integration tools (from referenced repos)
# ===================================================================

def _register_integrations() -> None:
    """Register tools from external integrations.

    Loads tools from:
        - Scrapling (D4Vinci/Scrapling) — adaptive web scraping
        - Google Workspace CLI (googleworkspace/cli) — Drive, Gmail, Sheets, Calendar
        - llmfit (AlexsJones/llmfit) — hardware-aware model recommendations
        - Monty (pydantic/monty) — run_python, a sandboxed Python interpreter

    Each integration gracefully degrades if its dependency is not installed.
    """
    # Scrapling: upgrades web_fetch + adds scrape/scrape_stealth tools
    try:
        from integrations.scrapling_tools import register_scrapling_tools
        register_scrapling_tools()
    except ImportError:
        log.debug("Scrapling integration not loaded (module not found)")
    except Exception as e:
        log.debug("Scrapling integration error: %s", e)

    # Google Workspace CLI: adds gdrive_*, gmail_*, gsheets_*, gcalendar_*
    # tools. Opt-in — it needs a host-installed `gws` CLI, which doesn't fit
    # the no-install USB model (see config experimental.google_workspace).
    try:
        from config.settings import is_experimental_enabled
        if is_experimental_enabled("google_workspace"):
            from integrations.gworkspace_tools import register_gworkspace_tools
            register_gworkspace_tools()
        else:
            log.debug("Google Workspace integration disabled (experimental).")
    except ImportError:
        log.debug("Google Workspace integration not loaded (module not found)")
    except Exception as e:
        log.debug("Google Workspace integration error: %s", e)

    # Python sandbox (pydantic/monty): adds run_python
    try:
        from integrations.python_sandbox import register_python_sandbox_tool
        register_python_sandbox_tool(register_tool)
    except Exception as e:
        log.debug("Python sandbox not loaded: %s", e)

    # llmfit: adds model_recommend tool
    try:
        from integrations.llmfit_advisor import register_llmfit_tools
        register_llmfit_tools()
    except ImportError:
        log.debug("llmfit integration not loaded (module not found)")
    except Exception as e:
        log.debug("llmfit integration error: %s", e)


# Register integration tools (safe — each one handles its own errors)
_register_integrations()
