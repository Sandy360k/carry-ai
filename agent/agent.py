"""
carry-ai/agent/agent.py — Main Agent Loop
===========================================

The core reasoning loop that connects the LLM to system tools.
Follows a ReAct-style pattern: Observe -> Think -> Act -> Observe.

Architecture:
    1. Receive user message (text, screenshot, or file)
    2. Build context: system prompt + conversation history + available tools
    3. Send to LLM (local llama.cpp or cloud API)
    4. Parse LLM response for tool calls
    5. Execute tool calls with permission checks
    6. Feed tool results back to LLM
    7. Repeat until LLM produces a final text response
    8. Return response to user (via web UI or CLI)
"""

import json
import logging
import platform
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from agent.tools import get_tool_definitions, execute_tool, TOOL_REGISTRY
from agent.memory import MemoryStore, get_memory_tools

log = logging.getLogger("carry-ai.agent")

# Max iterations in a single turn (prevents infinite tool loops)
MAX_ITERATIONS = 20

# Rough token estimate: 4 chars ~= 1 token
CHARS_PER_TOKEN = 4

# Default context window budget (tokens) before compaction triggers
DEFAULT_CONTEXT_BUDGET = 16_000

# Dangerous command patterns that require user confirmation
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b", r"\brmdir\b", r"\bformat\b", r"\bfdisk\b",
    r"\bdd\s+if=", r"\bmkfs\b", r"\bdel\s+/[sfq]", r"\bshutdown\b",
    r"\breboot\b", r"\bpkill\b", r"\bkillall\b", r"\btaskkill\b",
    r"\breg\s+delete\b", r"\bRemove-Item\s.*-Recurse",
    r"\bgit\s+push\s+.*--force", r"\bgit\s+reset\s+--hard",
    r"\bdrop\s+table\b", r"\bdrop\s+database\b", r"\btruncate\b",
]
_DANGEROUS_RE = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)


# ===================================================================
# System prompt
# ===================================================================

SYSTEM_PROMPT_TEMPLATE = """\
You are carry-ai, a portable AI assistant running from a USB drive on the user's machine.

## Environment
- OS: {os_name} {os_release} ({os_arch})
- Mode: {mode}
- Model: {model_name}

## Capabilities
You have access to tools for interacting with this machine. Use them to help the user.
Always prefer using tools over asking the user to do things manually.

## Guidelines
- Be concise and direct.
- When executing shell commands, prefer the host OS's native shell.
- For file operations, use the dedicated file tools (read_file, write_file, edit_file) \
rather than shell commands.
- If a task requires multiple steps, use tools iteratively — do not try to do everything \
in one command.
- If you are unsure about a destructive action, ask the user first.
- Report errors clearly and suggest fixes.

## Session
This session is temporary. When the USB is ejected, all traces will be wiped from this machine.

## Memory
You have access to persistent long-term memory that survives across sessions.
- Use the memory_store tool to save important facts about the user, their preferences,
  projects, and key decisions.
- Use the memory_search tool to recall previously stored information.
- Proactively remember things the user tells you about themselves or their work.
- When the user says "remember this" or similar, store it as a note.

{memory_block}
"""


def build_system_prompt(context: dict, memory_block: str = "") -> str:
    """Build the system prompt from boot context."""
    return SYSTEM_PROMPT_TEMPLATE.format(
        os_name=platform.system(),
        os_release=platform.release(),
        os_arch=platform.machine(),
        mode=context.get("mode", "unknown"),
        model_name=context.get("model_tier", {}).get("name", "unknown") if context.get("model_tier") else
                    context.get("model_path", "API"),
        memory_block=memory_block,
    )


# ===================================================================
# Conversation history
# ===================================================================

class ConversationHistory:
    """Manages the conversation message list with token-aware compaction.

    Messages follow OpenAI format:
        {"role": "system"|"user"|"assistant"|"tool", "content": "...", ...}
    """

    def __init__(self, system_prompt: str, context_budget: int = DEFAULT_CONTEXT_BUDGET):
        self._system = {"role": "system", "content": system_prompt}
        self._messages: list[dict] = []
        self._context_budget = context_budget

    @property
    def messages(self) -> list[dict]:
        """Full message list including system prompt."""
        return [self._system] + self._messages

    @property
    def user_messages(self) -> list[dict]:
        """Only user and assistant messages (no system, no tool)."""
        return [m for m in self._messages if m["role"] in ("user", "assistant")]

    def add(self, message: dict) -> None:
        """Append a message to history."""
        self._messages.append(message)

    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str, tool_calls: list[dict] | None = None) -> None:
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._messages.append(msg)

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self._messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        })

    @property
    def estimated_tokens(self) -> int:
        """Rough token estimate of the entire conversation."""
        total_chars = sum(len(json.dumps(m)) for m in self.messages)
        return total_chars // CHARS_PER_TOKEN

    @property
    def needs_compaction(self) -> bool:
        return self.estimated_tokens > self._context_budget

    def compact(self) -> None:
        """Reduce conversation size by summarizing older messages.

        Strategy: keep system prompt, last N user/assistant exchanges,
        and drop old tool call/result pairs entirely.
        """
        if not self.needs_compaction:
            return

        original_len = len(self._messages)

        # Keep the last 6 messages (3 exchanges) unconditionally
        keep_tail = 6
        if len(self._messages) <= keep_tail:
            return

        old = self._messages[:-keep_tail]
        kept = self._messages[-keep_tail:]

        # Summarize old messages into a single context note
        user_msgs = [m["content"] for m in old if m["role"] == "user" and m.get("content")]
        assistant_msgs = [m["content"] for m in old if m["role"] == "assistant" and m.get("content")]

        summary_parts = []
        if user_msgs:
            summary_parts.append(f"Previous user topics: {'; '.join(m[:100] for m in user_msgs[-5:])}")
        if assistant_msgs:
            # Keep last assistant summary
            last_assist = assistant_msgs[-1]
            if len(last_assist) > 300:
                last_assist = last_assist[:300] + "..."
            summary_parts.append(f"Last assistant context: {last_assist}")

        summary = "\n".join(summary_parts) if summary_parts else "(earlier conversation compacted)"

        self._messages = [
            {"role": "user", "content": f"[Context from earlier in conversation]\n{summary}"},
        ] + kept

        log.info("Compacted conversation: %d -> %d messages (est. %d tokens)",
                 original_len, len(self._messages), self.estimated_tokens)

    def clear(self) -> None:
        """Clear all messages except system prompt."""
        self._messages.clear()

    def to_list(self) -> list[dict]:
        """Export full message list (for serialization)."""
        return list(self.messages)


# ===================================================================
# Permission policy
# ===================================================================

class PermissionPolicy:
    """Controls which tool calls require user confirmation.

    Modes:
        'permissive' / 'yolo' — execute everything without asking
        'ask'                 — prompt user before destructive commands (default)
        'safe'                — block destructive operations outright (no prompt)
        'strict'              — prompt user before every tool call
    """

    # settings.json uses ask|yolo|safe; normalize to internal names
    _MODE_ALIASES = {"yolo": "permissive"}

    def __init__(self, mode: str = "ask", confirm_fn: Optional[Callable] = None):
        """
        Args:
            mode: 'permissive'/'yolo', 'ask', 'safe', or 'strict'.
            confirm_fn: Function that prompts user and returns True/False.
                        Signature: confirm_fn(tool_name, args, reason) -> bool
                        If None, uses stdin prompt.
        """
        mode = self._MODE_ALIASES.get(mode, mode)
        if mode not in ("permissive", "ask", "safe", "strict"):
            log.warning("Unknown permission mode %r — falling back to 'ask'", mode)
            mode = "ask"
        self.mode = mode
        self._confirm_fn = confirm_fn or self._default_confirm

    def check(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """Check if a tool call is allowed.

        Returns:
            (allowed, reason) — reason is empty if allowed.
        """
        if self.mode == "permissive":
            return True, ""

        if self.mode == "strict":
            reason = f"Strict mode: confirm {tool_name}?"
            allowed = self._confirm_fn(tool_name, args, reason)
            return allowed, "" if allowed else "User denied"

        # 'ask' prompts for dangerous operations; 'safe' blocks them outright
        if tool_name == "shell":
            command = args.get("command", "")
            if _DANGEROUS_RE.search(command):
                if self.mode == "safe":
                    return False, f"Blocked destructive command (safe mode): {command[:100]}"
                reason = f"Potentially destructive command: {command[:100]}"
                allowed = self._confirm_fn(tool_name, args, reason)
                return allowed, "" if allowed else "User denied destructive command"

        if tool_name in ("write_file", "edit_file"):
            # Check if overwriting an existing file outside session
            path = args.get("path", "")
            if Path(path).exists() and "ai_session" not in path:
                if self.mode == "safe":
                    return False, f"Blocked overwrite of existing file (safe mode): {path}"
                reason = f"Overwrite existing file: {path}"
                allowed = self._confirm_fn(tool_name, args, reason)
                return allowed, "" if allowed else "User denied file overwrite"

        return True, ""

    @staticmethod
    def _default_confirm(tool_name: str, args: dict, reason: str) -> bool:
        """Default confirmation prompt via stdin."""
        print(f"\n  [Permission] {reason}")
        if tool_name == "shell":
            print(f"  Command: {args.get('command', '')[:200]}")
        elif tool_name in ("write_file", "edit_file"):
            print(f"  File: {args.get('path', '')}")
        try:
            answer = input("  Allow? [y/N]: ").strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False


# ===================================================================
# Agent
# ===================================================================

class Agent:
    """Main agent loop: LLM reasoning + tool execution.

    Usage:
        agent = Agent(boot_context)
        response = agent.turn("What files are in this directory?")
        # agent.turn() returns the final assistant text after all tool calls.

        # Or run interactive REPL:
        agent.run(boot_context)
    """

    def __init__(self, boot_context: dict | None = None):
        """
        Args:
            boot_context: Dict from launcher.py boot sequence containing:
                mode, model_path, model_tier, api_keys, port, dry_run, etc.
        """
        self._context = boot_context or {}
        self._mode = self._context.get("mode", "local")
        self._dry_run = self._context.get("dry_run", False)

        # Initialize persistent memory
        memory_path = self._context.get("memory_path")
        self.memory = MemoryStore(memory_path)
        self.memory.load()
        self.memory.decay_relevance()  # Apply time-based decay on boot

        # Build system prompt with memory context
        memory_block = self.memory.build_context_block(max_tokens=800)
        system_prompt = build_system_prompt(self._context, memory_block=memory_block)

        # Context budget depends on mode
        if self._mode == "local":
            tier = self._context.get("model_tier")
            ctx = tier.get("context_size", 4096) if isinstance(tier, dict) else 4096
            budget = max(ctx - 1024, 2048)  # Reserve tokens for response
        else:
            budget = 100_000  # API models have large context

        self._history = ConversationHistory(system_prompt, context_budget=budget)
        self._policy = PermissionPolicy(mode=self._context.get("permission_mode", "ask"))
        self._runner = None   # LocalRunner or APIRouter — initialized lazily
        self._total_turns = 0

        # Register memory tools into the tool registry
        self._memory_tools = get_memory_tools(self.memory)
        from agent.tools import register_tool as _register_tool
        for mt in self._memory_tools:
            handler = mt.pop("_handler")
            _register_tool(
                name=mt["name"],
                description=mt["description"],
                parameters=mt["parameters"],
                execute_fn=handler,
            )

        # Callbacks for UI integration
        self.on_tool_start: Optional[Callable] = None   # fn(tool_name, args)
        self.on_tool_end: Optional[Callable] = None      # fn(tool_name, result)
        self.on_stream_chunk: Optional[Callable] = None   # fn(text_chunk)

    def _get_runner(self):
        """Lazily initialize the LLM runner based on mode."""
        if self._runner is not None:
            return self._runner

        if self._mode == "local":
            from modes.local_mode import LocalRunner
            model_path = self._context.get("model_path")
            self._runner = LocalRunner(
                model_path=Path(model_path) if model_path else None,
                port=self._context.get("llama_port", 8081),
            )
            if not self._runner.is_running():
                self._runner.start()
        elif self._mode in ("api", "hybrid"):
            from modes.api_mode import APIRouter
            self._runner = APIRouter(
                decrypted_keys=self._context.get("api_keys") or {},
            )
            self._runner.start()
        else:
            raise RuntimeError(f"Unknown mode: {self._mode}")

        return self._runner

    def _call_llm(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Send messages to LLM and return normalized response dict.

        Returns:
            {"content": str, "tool_calls": list|None}
        """
        runner = self._get_runner()

        kwargs = {"tools": tools} if tools else {}

        if self._mode == "local":
            resp = runner.chat(messages, **kwargs)
            return {
                "content": resp.get("content", "") if isinstance(resp, dict) else resp.content,
                "tool_calls": resp.get("tool_calls") if isinstance(resp, dict) else resp.tool_calls,
            }
        else:
            # API mode — returns ChatResponse
            resp = runner.chat(messages, **kwargs)
            return {
                "content": resp.content if hasattr(resp, "content") else resp.get("content", ""),
                "tool_calls": resp.tool_calls if hasattr(resp, "tool_calls") else resp.get("tool_calls"),
            }

    def turn(self, user_message: str) -> str:
        """Execute a single conversation turn.

        Sends the user message to the LLM, executes any tool calls,
        feeds results back, and repeats until a final text response.

        Args:
            user_message: The user's input text.

        Returns:
            The final assistant text response.
        """
        self._total_turns += 1
        self._history.add_user(user_message)

        # Check if compaction needed
        if self._history.needs_compaction:
            self._history.compact()

        tools = get_tool_definitions()
        iteration = 0

        while iteration < MAX_ITERATIONS:
            iteration += 1
            log.debug("Turn %d, iteration %d (est. %d tokens)",
                      self._total_turns, iteration, self._history.estimated_tokens)

            # Call LLM
            try:
                response = self._call_llm(self._history.messages, tools=tools)
            except Exception as e:
                error_msg = f"LLM error: {e}"
                log.error(error_msg)
                self._history.add_assistant(error_msg)
                return error_msg

            content = response.get("content", "") or ""
            tool_calls = response.get("tool_calls")

            # No tool calls — final response
            if not tool_calls:
                self._history.add_assistant(content)
                return content

            # Has tool calls — execute them
            self._history.add_assistant(content, tool_calls=tool_calls)

            for tc in tool_calls:
                tc_id = tc.get("id", f"call_{iteration}")
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args_raw = fn.get("arguments", "{}")

                # Parse arguments
                if isinstance(tool_args_raw, str):
                    try:
                        tool_args = json.loads(tool_args_raw)
                    except json.JSONDecodeError:
                        tool_args = {"raw": tool_args_raw}
                else:
                    tool_args = tool_args_raw

                log.info("Tool call: %s(%s)", tool_name, _truncate_args(tool_args))

                # Permission check
                allowed, deny_reason = self._policy.check(tool_name, tool_args)
                if not allowed:
                    result = f"Permission denied: {deny_reason}"
                    log.info("Tool denied: %s — %s", tool_name, deny_reason)
                else:
                    # Execute
                    if self.on_tool_start:
                        self.on_tool_start(tool_name, tool_args)

                    result = execute_tool(tool_name, tool_args)

                    if self.on_tool_end:
                        self.on_tool_end(tool_name, result)

                # Truncate very long results to save context
                if len(result) > 10_000:
                    result = result[:10_000] + f"\n\n[Output truncated at 10000 chars]"

                self._history.add_tool_result(tc_id, tool_name, result)

            # Compact after tool results if needed
            if self._history.needs_compaction:
                self._history.compact()

        # Hit max iterations
        msg = f"(Reached max iterations: {MAX_ITERATIONS}. Stopping tool loop.)"
        self._history.add_assistant(msg)
        return msg

    def stream_turn(self, user_message: str):
        """Execute a turn with streaming response.

        Yields text chunks as the LLM generates them. Tool calls are
        executed between stream segments.

        Note: Only works when tools are not being used (text-only response).
        For tool-using turns, falls back to non-streaming turn().

        Yields:
            dict: {"type": "text"|"tool_start"|"tool_result"|"done", "data": ...}
        """
        self._total_turns += 1
        self._history.add_user(user_message)

        if self._history.needs_compaction:
            self._history.compact()

        tools = get_tool_definitions()
        iteration = 0

        while iteration < MAX_ITERATIONS:
            iteration += 1

            runner = self._get_runner()
            kwargs = {"tools": tools} if tools else {}

            # Attempt streaming
            collected_content = ""
            collected_tool_calls = []

            try:
                if self._mode == "local":
                    stream = runner.stream(self._history.messages, **kwargs)
                else:
                    stream = runner.stream(self._history.messages, **kwargs)

                for chunk in stream:
                    chunk_type = chunk.get("type", "")
                    if chunk_type == "content":
                        text = chunk["data"]
                        collected_content += text
                        yield {"type": "text", "data": text}
                    elif chunk_type == "tool_call":
                        collected_tool_calls.extend(chunk["data"])
                    elif chunk_type == "done":
                        break

            except Exception as e:
                error_msg = f"LLM error: {e}"
                log.error(error_msg)
                self._history.add_assistant(error_msg)
                yield {"type": "text", "data": error_msg}
                yield {"type": "done", "data": None}
                return

            # No tool calls — done
            if not collected_tool_calls:
                self._history.add_assistant(collected_content)
                yield {"type": "done", "data": None}
                return

            # Execute tool calls
            self._history.add_assistant(collected_content, tool_calls=collected_tool_calls)

            for tc in collected_tool_calls:
                tc_id = tc.get("id", f"call_{iteration}")
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args_raw = fn.get("arguments", "{}")

                if isinstance(tool_args_raw, str):
                    try:
                        tool_args = json.loads(tool_args_raw)
                    except json.JSONDecodeError:
                        tool_args = {"raw": tool_args_raw}
                else:
                    tool_args = tool_args_raw

                yield {"type": "tool_start", "data": {"name": tool_name, "args": tool_args}}

                allowed, deny_reason = self._policy.check(tool_name, tool_args)
                if not allowed:
                    result = f"Permission denied: {deny_reason}"
                else:
                    result = execute_tool(tool_name, tool_args)

                if len(result) > 10_000:
                    result = result[:10_000] + "\n\n[Output truncated]"

                self._history.add_tool_result(tc_id, tool_name, result)
                yield {"type": "tool_result", "data": {"name": tool_name, "result": result[:500]}}

            if self._history.needs_compaction:
                self._history.compact()

        yield {"type": "text", "data": f"(Reached max iterations: {MAX_ITERATIONS})"}
        yield {"type": "done", "data": None}

    def set_permission_mode(self, mode: str) -> None:
        """Change permission policy: 'permissive'/'yolo', 'ask', 'safe', 'strict'."""
        self._policy = PermissionPolicy(mode=mode, confirm_fn=self._policy._confirm_fn)
        log.info("Permission mode set to: %s", mode)

    def set_confirm_fn(self, fn: callable) -> None:
        """Set a custom confirmation function (for web UI integration)."""
        self._policy._confirm_fn = fn

    def get_history(self) -> list[dict]:
        """Export conversation history."""
        return self._history.to_list()

    def clear_history(self) -> None:
        """Clear conversation history (keeps system prompt)."""
        self._history.clear()
        self._total_turns = 0

    def get_status(self) -> dict:
        """Return agent status for UI."""
        return {
            "mode": self._mode,
            "total_turns": self._total_turns,
            "history_messages": len(self._history.messages),
            "estimated_tokens": self._history.estimated_tokens,
            "tools": len(TOOL_REGISTRY),
            "permission_mode": self._policy.mode,
        }

    def run(self, context: dict | None = None) -> None:
        """Interactive REPL loop (called from launcher.py fallback or --no-ui).

        Runs until user types 'quit', 'exit', or Ctrl+C.
        """
        if context:
            self._context = context

        print("  carry-ai agent ready. Type 'quit' to exit.\n")

        while True:
            try:
                user_input = input("  you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                break

            # Special commands
            if user_input.startswith("/"):
                self._handle_slash_command(user_input)
                continue

            # Run turn
            print()
            try:
                response = self.turn(user_input)
                print(f"  ai> {response}\n")
            except Exception as e:
                print(f"  [error] {e}\n")

    def _handle_slash_command(self, command: str) -> None:
        """Handle agent slash commands."""
        parts = command.strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/status":
            status = self.get_status()
            for k, v in status.items():
                print(f"  {k}: {v}")
        elif cmd == "/clear":
            self.clear_history()
            print("  Conversation cleared.")
        elif cmd == "/tools":
            for name in sorted(TOOL_REGISTRY.keys()):
                print(f"  {name}")
        elif cmd == "/permission":
            if arg in ("permissive", "ask", "strict"):
                self.set_permission_mode(arg)
                print(f"  Permission mode: {arg}")
            else:
                print(f"  Current: {self._policy.mode}. Options: permissive, ask, strict")
        elif cmd == "/history":
            for msg in self._history.user_messages[-10:]:
                role = msg["role"]
                content = msg.get("content", "")[:100]
                print(f"  [{role}] {content}")
        elif cmd == "/remember":
            if arg:
                self.memory.add_note(arg)
                self.memory.auto_save()
                print(f"  Remembered: {arg}")
            else:
                print("  Usage: /remember <something to remember>")
        elif cmd == "/memory":
            if arg == "clear":
                self.memory.clear()
                self.memory.save()
                print("  All memories cleared.")
            elif arg.startswith("search "):
                query = arg[7:].strip()
                results = self.memory.search(query)
                if results:
                    for r in results:
                        print(f"  [{r.obs_type}] {r.title}: {r.content[:80]}")
                else:
                    print("  No matching memories.")
            elif arg == "stats":
                stats = self.memory.stats()
                for k, v in stats.items():
                    print(f"  {k}: {v}")
            elif arg == "export":
                print(self.memory.export_json())
            else:
                # Default: list all
                entries = self.memory.get_recent(20)
                if entries:
                    for e in entries:
                        print(f"  [{e.obs_type}] {e.title}: {e.content[:80]}")
                else:
                    print("  No memories stored yet.")
                print(f"\n  Subcommands: /memory [search <q> | stats | clear | export]")
        elif cmd == "/forget":
            if arg:
                # Search and remove matching entries
                results = self.memory.search(arg, limit=1)
                if results:
                    obs = results[0]
                    self.memory._conn.execute("DELETE FROM observations WHERE id = ?", (obs.id,))
                    self.memory._conn.commit()
                    print(f"  Forgot: {obs.title}")
                else:
                    print(f"  No memory matching: {arg}")
            else:
                print("  Usage: /forget <search text>")
        elif cmd == "/help":
            print("  /status     -- Show agent status")
            print("  /clear      -- Clear conversation history")
            print("  /tools      -- List available tools")
            print("  /permission -- Set permission mode (permissive/ask/strict)")
            print("  /history    -- Show recent conversation")
            print("  /remember   -- Store a note in long-term memory")
            print("  /memory     -- View/search/manage memories")
            print("  /forget     -- Remove a memory entry")
            print("  /help       -- This help message")
        else:
            print(f"  Unknown command: {cmd}. Type /help for options.")

    def shutdown(self) -> None:
        """Shutdown agent and release resources."""
        # Auto-summarize session into structured memory before shutdown
        try:
            if self._total_turns > 0 and self.memory:
                user_msgs = [m.get("content", "") for m in self._history.messages
                             if m.get("role") == "user" and m.get("content")]
                if user_msgs:
                    request = user_msgs[0][:200] if user_msgs else ""
                    topics = "; ".join(msg[:60] for msg in user_msgs[:5])
                    self.memory.add_session_summary(
                        request=request,
                        completed=f"Session with {self._total_turns} turns covering: {topics}",
                        turn_count=self._total_turns,
                    )
                self.memory.save()
                self.memory.close()
                log.info("Session summary saved to memory")
        except Exception as e:
            log.warning("Failed to save session memory: %s", e)

        if self._runner:
            try:
                self._runner.shutdown()
            except Exception:
                pass
            self._runner = None
        self._history.clear()
        log.info("Agent shut down.")


# ===================================================================
# Helpers
# ===================================================================

def _truncate_args(args: dict, max_len: int = 100) -> str:
    """Truncate tool args for logging."""
    s = json.dumps(args)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s
