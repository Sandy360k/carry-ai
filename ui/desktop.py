"""
carry-ai/ui/desktop.py — Native Desktop Chat Application
=========================================================
A Claude-style desktop GUI for carry-ai. Launch via:

    python -m ui.desktop          # from carry-ai/
    python ui/desktop.py          # direct

Features:
  - Dark / light theme with toggle
  - Chat history sidebar with multiple conversations
  - Provider + model selection
  - Streaming responses with typing indicator
  - Markdown rendering (bold, italic, code, headers, lists)
  - Copy code blocks, export chat, keyboard shortcuts
  - Connects to carry-ai's provider backend

Uses customtkinter if installed (modern look), falls back to tkinter.
"""

import json
import logging
import os
import platform
import queue
import re
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

log = logging.getLogger("carry-ai.desktop")

# ---------------------------------------------------------------------------
# GUI toolkit
# ---------------------------------------------------------------------------
try:
    import customtkinter as ctk
    CTK = True
except ImportError:
    CTK = False

import tkinter as tk
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------
DARK = {
    "bg": "#1a1a2e", "bg2": "#16213e", "bg3": "#0f0e1a",
    "fg": "#e8e8e8", "fg2": "#8888aa", "fg3": "#555570",
    "user_bg": "#1a3a5c", "ai_bg": "#1e1e3a",
    "accent": "#4ecca3", "accent2": "#533483",
    "border": "#2a2a4a", "input_bg": "#12112a",
    "code_bg": "#0d1117", "error": "#e94560",
    "hover": "#252548", "selected": "#1f3d5e",
    "header_fg": "#7eb8da",
}
LIGHT = {
    "bg": "#ffffff", "bg2": "#f0f2f5", "bg3": "#e8eaed",
    "fg": "#1a1a1a", "fg2": "#666680", "fg3": "#999999",
    "user_bg": "#e3f2fd", "ai_bg": "#f7f7f8",
    "accent": "#2a9d6f", "accent2": "#7c4dff",
    "border": "#d0d0d0", "input_bg": "#ffffff",
    "code_bg": "#f6f8fa", "error": "#d32f2f",
    "hover": "#e8e8f0", "selected": "#d0e4f5",
    "header_fg": "#1565c0",
}

PROVIDERS = [
    {"name": "Anthropic (Claude)", "key": "anthropic",
     "models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1"]},
    {"name": "OpenAI", "key": "openai",
     "models": ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"]},
    {"name": "Google (Gemini)", "key": "google",
     "models": ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-pro-preview"]},
    {"name": "Groq", "key": "groq",
     "models": ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]},
    {"name": "OpenRouter", "key": "openrouter",
     "models": ["openrouter/free", "openrouter/auto", "google/gemma-4-31b-it:free"]},
    {"name": "Local (llama.cpp)", "key": "local",
     "models": ["auto-detect"]},
]

FONT_FAMILY = "Segoe UI" if platform.system() == "Windows" else "Helvetica"
MONO_FAMILY = "Consolas" if platform.system() == "Windows" else "DejaVu Sans Mono"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Conversation:
    id: str = ""
    title: str = "New chat"
    messages: list = field(default_factory=list)
    created: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.id:
            self.id = f"chat_{int(self.created * 1000)}"

    def auto_title(self):
        """Generate title from first user message."""
        for m in self.messages:
            if m["role"] == "user":
                text = m["content"][:50].strip()
                self.title = text + ("..." if len(m["content"]) > 50 else "")
                return


# ---------------------------------------------------------------------------
# Chat backend
# ---------------------------------------------------------------------------
class ChatBackend:
    """Manages LLM inference in background threads."""

    # Keys already decrypted by launcher.py for this session (in RAM only),
    # so the user is not asked for the passphrase again on every message.
    preloaded_keys: dict = {}
    # Boot context from launcher.py (mode, model_path, api_keys, ...); the
    # Agent is built from it so the desktop app shares the web UI's engine.
    boot_context: dict = {}

    def __init__(self):
        self.response_queue: queue.Queue = queue.Queue()
        self._running = False
        self._cancel = threading.Event()
        self._agent = None
        self._loaded_conv_id: str | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def send_message(self, conv_id: str, history: list[dict], user_text: str,
                     provider_key: str, model: str) -> None:
        self._running = True
        self._cancel.clear()
        t = threading.Thread(
            target=self._run_agent,
            args=(conv_id, list(history), user_text, provider_key, model),
            daemon=True,
        )
        t.start()

    def cancel(self):
        self._cancel.set()

    # -- Shared agent ----------------------------------------------------

    def _get_agent(self):
        """Build the ReAct Agent once, from the launcher's boot context.

        Same engine the web UI uses: tools, local GGUF or API routing,
        persistent memory and the permission policy. Starting a local
        llama-server can block, so this runs on the worker thread.
        """
        if self._agent is None:
            from agent.agent import Agent
            ctx = dict(self.boot_context)
            if self.preloaded_keys and not ctx.get("api_keys"):
                ctx["api_keys"] = self.preloaded_keys
            agent = Agent(boot_context=ctx)
            agent._policy._confirm_fn = self._gui_confirm   # GUI, not stdin
            self._agent = agent
        return self._agent

    def _gui_confirm(self, tool_name: str, args: dict, reason: str) -> bool:
        """Permission prompt marshalled to the Tk main thread.

        The agent runs on a worker thread, so we hand the request to the UI
        via the queue and block on an Event until the dialog is answered.
        """
        done = threading.Event()
        result = {"allowed": False}
        self.response_queue.put(("permission", {
            "tool": tool_name, "args": args, "reason": reason,
            "event": done, "result": result,
        }))
        done.wait()
        return result["allowed"]

    def _run_agent(self, conv_id: str, history: list[dict], user_text: str,
                   provider_key: str, model: str) -> None:
        try:
            agent = self._get_agent()
            # Keep the agent's in-memory history in step with the GUI's
            # active conversation: replay prior turns only on a switch.
            if conv_id != self._loaded_conv_id:
                agent.load_history(history)
                self._loaded_conv_id = conv_id

            # In local mode the loaded GGUF is used; provider/model steer
            # only API routing.
            route = {}
            if agent._mode in ("api", "hybrid") and provider_key != "local":
                route = {"provider": provider_key, "model": model}

            t0 = time.time()
            produced = False
            for event in agent.stream_turn(user_text, **route):
                if self._cancel.is_set():
                    break
                etype = event.get("type")
                if etype == "text":
                    produced = True
                    self.response_queue.put(("chunk", event["data"]))
                elif etype == "tool_start":
                    name = event["data"].get("name", "tool")
                    self.response_queue.put(("tool", f"→ {name}…"))
                elif etype == "tool_result":
                    name = event["data"].get("name", "tool")
                    self.response_queue.put(("tool", f"✓ {name}"))
                elif etype == "done":
                    break
            if not produced:
                self.response_queue.put(("chunk", ""))
            self.response_queue.put(("done", f"{time.time() - t0:.1f}s"))
        except Exception as e:
            self.response_queue.put(("error", str(e)))
        finally:
            self._running = False

# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
class CarryAIApp:

    def __init__(self):
        self.backend = ChatBackend()
        self._streaming = False
        self._has_messages = False
        self._dark = True
        self.C = dict(DARK)  # active color dict

        # Chat history
        self._conversations: list[Conversation] = []
        # First conversation is created data-only; _new_conversation() also
        # redraws widgets, which don't exist until _build_ui() has run.
        self._active_conv = Conversation()
        self._conversations.append(self._active_conv)

        # ── Window ──
        if CTK:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("green")
            self._root = ctk.CTk()
        else:
            self._root = tk.Tk()
        self._root.title("carry-ai")
        self._root.geometry("1200x800")
        self._root.minsize(900, 550)
        self._root.configure(bg=self.C["bg"])
        try:
            icon = PROJECT_ROOT / "ui" / "icon.ico"
            if icon.exists():
                self._root.iconbitmap(str(icon))
        except Exception:
            pass

        # ── Layout ──
        self._build_ui()

        # ── Keyboard shortcuts ──
        self._root.bind("<Control-n>", lambda e: self._new_conversation())
        self._root.bind("<Control-l>", lambda e: self._clear_chat())
        self._root.bind("<Control-e>", lambda e: self._export_chat())
        self._root.bind("<Escape>", lambda e: self._on_escape())

        # ── Start ──
        self._poll_queue()
        self._add_welcome()

    # ==================================================================
    # Build UI
    # ==================================================================
    def _build_ui(self):
        # History sidebar (left)
        self._history_frame = tk.Frame(self._root, width=200, bg=self.C["bg3"])
        self._history_frame.pack(side="left", fill="y")
        self._history_frame.pack_propagate(False)
        self._build_history_panel()

        # Settings sidebar (right of history)
        self._sidebar = tk.Frame(self._root, width=220, bg=self.C["bg2"])
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)
        self._build_sidebar()

        # Main area
        main = tk.Frame(self._root, bg=self.C["bg"])
        main.pack(side="right", fill="both", expand=True)

        # Header bar
        self._header = tk.Frame(main, bg=self.C["bg2"], height=44)
        self._header.pack(fill="x")
        self._header.pack_propagate(False)
        self._build_header()

        # Chat area
        self._chat_frame = tk.Frame(main, bg=self.C["bg"])
        self._chat_frame.pack(fill="both", expand=True)
        self._build_chat_area()

        # Input area
        self._input_frame = tk.Frame(main, bg=self.C["bg3"])
        self._input_frame.pack(fill="x")
        self._build_input_area()

    # ── History panel ──
    def _build_history_panel(self):
        for w in self._history_frame.winfo_children():
            w.destroy()

        # Title + New Chat button
        top = tk.Frame(self._history_frame, bg=self.C["bg3"])
        top.pack(fill="x", padx=8, pady=(10, 5))
        tk.Label(top, text="Chats", font=(FONT_FAMILY, 12, "bold"),
                 fg=self.C["fg2"], bg=self.C["bg3"]).pack(side="left")

        new_btn_opts = dict(text="+", command=self._new_conversation,
                            font=(FONT_FAMILY, 14, "bold"), relief="flat")
        if CTK:
            ctk.CTkButton(top, text="+", command=self._new_conversation,
                          width=30, height=26, fg_color=self.C["accent"],
                          text_color="#111").pack(side="right")
        else:
            tk.Button(top, bg=self.C["accent"], fg="#111", **new_btn_opts).pack(side="right")

        # Scrollable conversation list
        canvas = tk.Canvas(self._history_frame, bg=self.C["bg3"],
                           highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True, padx=4)

        for conv in reversed(self._conversations):
            is_active = conv.id == (self._active_conv.id if self._active_conv else "")
            bg = self.C["selected"] if is_active else self.C["bg3"]
            fg = self.C["fg"] if is_active else self.C["fg2"]

            btn = tk.Label(canvas, text=conv.title, font=(FONT_FAMILY, 11),
                           fg=fg, bg=bg, anchor="w", padx=10, pady=6, cursor="hand2")
            btn.pack(fill="x", pady=1)
            btn.bind("<Button-1>", lambda e, c=conv: self._switch_conversation(c))
            btn.bind("<Enter>", lambda e, b=btn: b.config(bg=self.C["hover"]) if not is_active else None)
            btn.bind("<Leave>", lambda e, b=btn, bg_=bg: b.config(bg=bg_))

    # ── Settings sidebar ──
    def _build_sidebar(self):
        sb = self._sidebar
        # Title
        tk.Label(sb, text="carry-ai", font=(FONT_FAMILY, 18, "bold"),
                 fg=self.C["accent"], bg=self.C["bg2"]).pack(pady=(16, 2), padx=14, anchor="w")
        tk.Label(sb, text="Portable AI Assistant", font=(FONT_FAMILY, 10),
                 fg=self.C["fg2"], bg=self.C["bg2"]).pack(padx=14, anchor="w")
        self._sep(sb)

        # Provider
        self._lbl(sb, "Provider")
        prov_names = [p["name"] for p in PROVIDERS]
        self._provider_var = tk.StringVar(value=prov_names[0])
        if CTK:
            self._prov_menu = ctk.CTkOptionMenu(sb, variable=self._provider_var,
                values=prov_names, command=self._on_provider_change, width=190)
        else:
            self._prov_menu = tk.OptionMenu(sb, self._provider_var, *prov_names,
                command=self._on_provider_change)
            self._prov_menu.config(bg=self.C["bg3"], fg=self.C["fg"],
                                    highlightthickness=0, font=(FONT_FAMILY, 11))
        self._prov_menu.pack(padx=14, pady=(0, 8), fill="x")

        # Model
        self._lbl(sb, "Model")
        first_models = PROVIDERS[0]["models"]
        self._model_var = tk.StringVar(value=first_models[0])
        if CTK:
            self._model_menu = ctk.CTkOptionMenu(sb, variable=self._model_var,
                values=first_models, width=190)
        else:
            self._model_menu = tk.OptionMenu(sb, self._model_var, *first_models)
            self._model_menu.config(bg=self.C["bg3"], fg=self.C["fg"],
                                     highlightthickness=0, font=(FONT_FAMILY, 11))
        self._model_menu.pack(padx=14, pady=(0, 8), fill="x")

        self._sep(sb)

        # Status
        self._lbl(sb, "Status")
        sf = tk.Frame(sb, bg=self.C["bg2"])
        sf.pack(padx=14, fill="x")
        self._dot = tk.Canvas(sf, width=10, height=10, bg=self.C["bg2"], highlightthickness=0)
        self._dot.pack(side="left", padx=(0, 6))
        self._dot.create_oval(1, 1, 9, 9, fill=self.C["accent"], outline="")
        self._status_lbl = tk.Label(sf, text="Ready", font=(FONT_FAMILY, 11),
                                     fg=self.C["fg2"], bg=self.C["bg2"])
        self._status_lbl.pack(side="left")

        # Token/timing info
        self._timing_lbl = tk.Label(sb, text="", font=(FONT_FAMILY, 10),
                                     fg=self.C["fg3"], bg=self.C["bg2"])
        self._timing_lbl.pack(padx=14, pady=(4, 0), anchor="w")

        # RAM
        ram = "RAM: unknown"
        try:
            import psutil
            ram = f"RAM: {psutil.virtual_memory().available / (1024**3):.1f} GB free"
        except ImportError:
            pass
        tk.Label(sb, text=ram, font=(FONT_FAMILY, 10),
                 fg=self.C["fg3"], bg=self.C["bg2"]).pack(padx=14, pady=(2, 0), anchor="w")

        # Bottom buttons
        bf = tk.Frame(sb, bg=self.C["bg2"])
        bf.pack(side="bottom", fill="x", padx=10, pady=10)
        btns = [
            ("Models", self._open_model_manager, self.C["accent2"]),
            ("Export Chat", self._export_chat, self.C["border"]),
            ("Web UI", self._open_web_ui, self.C["border"]),
        ]
        for text, cmd, color in btns:
            if CTK:
                ctk.CTkButton(bf, text=text, command=cmd, fg_color=color,
                              hover_color=self.C["hover"], height=28).pack(fill="x", pady=2)
            else:
                tk.Button(bf, text=text, command=cmd, bg=color, fg=self.C["fg"],
                          relief="flat", font=(FONT_FAMILY, 10)).pack(fill="x", pady=2)

    # ── Header bar ──
    def _build_header(self):
        h = self._header
        self._conv_title_lbl = tk.Label(h, text="New chat", font=(FONT_FAMILY, 13, "bold"),
                                         fg=self.C["fg"], bg=self.C["bg2"])
        self._conv_title_lbl.pack(side="left", padx=16)

        # Theme toggle
        self._theme_btn = tk.Button(h, text="Light", font=(FONT_FAMILY, 10),
                                     bg=self.C["bg2"], fg=self.C["fg2"],
                                     relief="flat", command=self._toggle_theme,
                                     cursor="hand2", bd=0)
        self._theme_btn.pack(side="right", padx=12)

        # Shortcuts hint
        tk.Label(h, text="Ctrl+N new  |  Ctrl+L clear  |  Ctrl+E export",
                 font=(FONT_FAMILY, 9), fg=self.C["fg3"],
                 bg=self.C["bg2"]).pack(side="right", padx=8)

    # ── Chat area ──
    def _build_chat_area(self):
        self._chat_text = tk.Text(
            self._chat_frame, wrap="word", bg=self.C["bg"], fg=self.C["fg"],
            font=(FONT_FAMILY, 13), insertbackground=self.C["fg"],
            selectbackground=self.C["accent2"], relief="flat",
            padx=28, pady=18, cursor="arrow", state="disabled",
            spacing1=2, spacing3=2,
        )
        sb = tk.Scrollbar(self._chat_frame, command=self._chat_text.yview,
                          bg=self.C["bg"], troughcolor=self.C["bg3"])
        self._chat_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._chat_text.pack(side="left", fill="both", expand=True)
        self._setup_tags()

        # Right-click context menu
        self._ctx_menu = tk.Menu(self._chat_text, tearoff=0,
                                  bg=self.C["bg2"], fg=self.C["fg"])
        self._ctx_menu.add_command(label="Copy", command=self._copy_selection)
        self._ctx_menu.add_command(label="Select All", command=self._select_all)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Export Chat", command=self._export_chat)
        self._chat_text.bind("<Button-3>", self._show_ctx_menu)

    def _setup_tags(self):
        t = self._chat_text
        t.tag_configure("user_name", font=(FONT_FAMILY, 13, "bold"),
                        foreground=self.C["header_fg"], spacing1=14)
        t.tag_configure("ai_name", font=(FONT_FAMILY, 13, "bold"),
                        foreground=self.C["accent"], spacing1=14)
        t.tag_configure("user_text", font=(FONT_FAMILY, 13), foreground=self.C["fg"])
        t.tag_configure("ai_text", font=(FONT_FAMILY, 13), foreground=self.C["fg"])
        t.tag_configure("bold", font=(FONT_FAMILY, 13, "bold"), foreground=self.C["fg"])
        t.tag_configure("italic", font=(FONT_FAMILY, 13, "italic"), foreground=self.C["fg"])
        t.tag_configure("inline_code", font=(MONO_FAMILY, 12),
                        foreground=self.C["accent"], background=self.C["code_bg"])
        t.tag_configure("code", font=(MONO_FAMILY, 12), foreground="#c9d1d9",
                        background=self.C["code_bg"],
                        lmargin1=16, lmargin2=16, rmargin=16, spacing1=4, spacing3=4)
        t.tag_configure("code_header", font=(MONO_FAMILY, 10, "bold"),
                        foreground=self.C["fg3"], background=self.C["code_bg"],
                        lmargin1=16, spacing1=6)
        t.tag_configure("heading", font=(FONT_FAMILY, 15, "bold"),
                        foreground=self.C["fg"], spacing1=10, spacing3=4)
        t.tag_configure("bullet", font=(FONT_FAMILY, 13), foreground=self.C["fg"],
                        lmargin1=20, lmargin2=32)
        t.tag_configure("error_text", font=(FONT_FAMILY, 13), foreground=self.C["error"])
        t.tag_configure("system", font=(FONT_FAMILY, 12), foreground=self.C["fg2"],
                        justify="center", spacing1=8, spacing3=8)
        t.tag_configure("timestamp", font=(FONT_FAMILY, 9), foreground=self.C["fg3"])
        t.tag_configure("thinking", font=(FONT_FAMILY, 12, "italic"),
                        foreground=self.C["fg3"])

    # ── Input area ──
    def _build_input_area(self):
        inner = tk.Frame(self._input_frame, bg=self.C["bg3"])
        inner.pack(fill="x", padx=20, pady=12)

        self._input_text = tk.Text(
            inner, height=2, wrap="word",
            bg=self.C["input_bg"], fg=self.C["fg"],
            insertbackground=self.C["fg"], selectbackground=self.C["accent2"],
            font=(FONT_FAMILY, 13), relief="flat", padx=14, pady=10,
        )
        self._input_text.pack(side="left", fill="both", expand=True)

        # Auto-grow input
        self._input_text.bind("<KeyRelease>", self._auto_grow_input)

        # Placeholder
        self._placeholder_on = True
        self._input_text.insert("1.0", "Type a message... (Enter to send, Shift+Enter for newline)")
        self._input_text.config(fg=self.C["fg3"])
        self._input_text.bind("<FocusIn>", self._on_focus_in)
        self._input_text.bind("<FocusOut>", self._on_focus_out)

        # Button frame (stacked vertically for send + stop)
        btn_f = tk.Frame(inner, bg=self.C["bg3"])
        btn_f.pack(side="right", padx=(10, 0))

        if CTK:
            self._send_btn = ctk.CTkButton(btn_f, text="Send ▶", width=80, height=36,
                command=self._send_message,
                fg_color=self.C["accent"], hover_color="#3ba885",
                text_color="#111", font=ctk.CTkFont(size=13, weight="bold"))
            self._stop_btn = ctk.CTkButton(btn_f, text="Stop ■", width=80, height=36,
                command=self._stop_generation,
                fg_color=self.C["error"], hover_color="#c23050",
                text_color="#fff", font=ctk.CTkFont(size=13, weight="bold"))
        else:
            self._send_btn = tk.Button(btn_f, text="Send ▶", width=8,
                command=self._send_message, bg=self.C["accent"], fg="#111",
                activebackground="#3ba885", relief="flat", font=(FONT_FAMILY, 12, "bold"))
            self._stop_btn = tk.Button(btn_f, text="Stop ■", width=8,
                command=self._stop_generation, bg=self.C["error"], fg="#fff",
                activebackground="#c23050", relief="flat", font=(FONT_FAMILY, 12, "bold"))

        self._send_btn.pack()
        # Stop button hidden by default

        self._input_text.bind("<Return>", self._on_enter)
        self._input_text.bind("<Shift-Return>", lambda e: None)

    def _auto_grow_input(self, event=None):
        lines = int(self._input_text.index("end-1c").split(".")[0])
        new_h = max(2, min(lines, 8))
        self._input_text.configure(height=new_h)

    def _on_focus_in(self, event=None):
        if self._placeholder_on:
            self._input_text.delete("1.0", "end")
            self._input_text.config(fg=self.C["fg"])
            self._placeholder_on = False

    def _on_focus_out(self, event=None):
        if not self._input_text.get("1.0", "end").strip():
            self._input_text.insert("1.0", "Type a message... (Enter to send, Shift+Enter for newline)")
            self._input_text.config(fg=self.C["fg3"])
            self._placeholder_on = True

    def _on_enter(self, event):
        self._send_message()
        return "break"

    def _on_escape(self):
        if self._streaming:
            self._stop_generation()
        else:
            self._input_text.delete("1.0", "end")

    # ==================================================================
    # Send / Stop / Display
    # ==================================================================
    def _send_message(self, event=None):
        if self._placeholder_on or self._streaming:
            return
        text = self._input_text.get("1.0", "end").strip()
        if not text:
            return
        self._input_text.delete("1.0", "end")
        self._input_text.configure(height=2)
        self._on_focus_in()

        prov = next((p for p in PROVIDERS if p["name"] == self._provider_var.get()), PROVIDERS[0])
        model = self._model_var.get()

        # Prior turns (before this new user message) — replayed into the
        # agent only if the GUI has switched conversations.
        prior = list(self._active_conv.messages)

        # Save to conversation
        self._active_conv.messages.append({"role": "user", "content": text,
                                            "time": time.time()})
        if len(self._active_conv.messages) == 1:
            self._active_conv.auto_title()
            self._conv_title_lbl.config(text=self._active_conv.title)
            self._build_history_panel()

        self._display_message("user", text)
        self._show_thinking()

        # Swap send ↔ stop
        self._send_btn.pack_forget()
        self._stop_btn.pack()
        self._update_status("Generating...", True)

        self.backend.send_message(self._active_conv.id, prior, text,
                                  prov["key"], model)

    def _stop_generation(self):
        self.backend.cancel()

    def _show_thinking(self):
        """Insert animated thinking indicator."""
        self._chat_text.configure(state="normal")
        if self._has_messages:
            self._chat_text.insert("end", "\n")
        self._chat_text.insert("end", "carry-ai\n", "ai_name")
        self._thinking_idx = self._chat_text.index("end-1c")
        self._chat_text.insert("end", "thinking...", "thinking")
        self._chat_text.configure(state="disabled")
        self._chat_text.see("end")
        self._has_messages = True

    def _clear_thinking(self):
        """Remove the thinking indicator, keep the ai_name header.

        One-shot: the insertion mark is dropped after the first call so
        later tool/stream events don't delete content inserted since.
        """
        idx = self.__dict__.pop("_thinking_idx", None)
        if idx is not None:
            self._chat_text.configure(state="normal")
            self._chat_text.delete(idx, "end")
            self._chat_text.configure(state="disabled")

    def _display_message(self, role: str, content: str, ts: float | None = None):
        self._chat_text.configure(state="normal")
        if self._has_messages:
            self._chat_text.insert("end", "\n")

        timestamp = datetime.fromtimestamp(ts or time.time()).strftime("%H:%M")

        if role == "user":
            self._chat_text.insert("end", f"You  ", "user_name")
            self._chat_text.insert("end", timestamp + "\n", "timestamp")
            self._chat_text.insert("end", content + "\n", "user_text")
        elif role == "assistant":
            self._chat_text.insert("end", f"carry-ai  ", "ai_name")
            self._chat_text.insert("end", timestamp + "\n", "timestamp")
            self._render_markdown(content)
            self._chat_text.insert("end", "\n")
        elif role == "error":
            self._chat_text.insert("end", "Error: ", "ai_name")
            self._chat_text.insert("end", content + "\n", "error_text")
        elif role == "system":
            self._chat_text.insert("end", content + "\n", "system")

        self._has_messages = True
        self._chat_text.configure(state="disabled")
        self._chat_text.see("end")

    # ==================================================================
    # Markdown rendering
    # ==================================================================
    def _render_markdown(self, text: str):
        """Parse and render markdown: code blocks, bold, italic, inline code, headers, bullets."""
        parts = text.split("```")
        for i, part in enumerate(parts):
            if i % 2 == 1:
                # Code block
                lines = part.split("\n", 1)
                lang = lines[0].strip() if lines[0].strip() else "code"
                code = lines[1] if len(lines) > 1 else lines[0]
                self._chat_text.insert("end", f"\n  {lang}\n", "code_header")
                self._chat_text.insert("end", code.rstrip() + "\n", "code")
            else:
                self._render_inline_markdown(part)

    def _render_inline_markdown(self, text: str):
        """Render inline markdown: **bold**, *italic*, `code`, # headings, - bullets."""
        for line in text.split("\n"):
            stripped = line.strip()
            # Headings
            if stripped.startswith("### "):
                self._chat_text.insert("end", stripped[4:] + "\n", "bold")
                continue
            if stripped.startswith("## ") or stripped.startswith("# "):
                content = stripped.lstrip("# ").strip()
                self._chat_text.insert("end", content + "\n", "heading")
                continue
            # Bullet points
            if stripped.startswith("- ") or stripped.startswith("* ") or stripped.startswith("• "):
                self._chat_text.insert("end", "  •  " + stripped[2:] + "\n", "bullet")
                continue
            # Numbered lists
            if re.match(r'^\d+\.\s', stripped):
                self._chat_text.insert("end", "  " + stripped + "\n", "bullet")
                continue

            # Inline formatting: process **bold**, *italic*, `code`
            self._render_inline_spans(line + "\n")

    def _render_inline_spans(self, text: str):
        """Parse inline **bold**, *italic*, `code` spans."""
        pattern = re.compile(r'(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`)')
        pos = 0
        for m in pattern.finditer(text):
            # Text before match
            if m.start() > pos:
                self._chat_text.insert("end", text[pos:m.start()], "ai_text")
            if m.group(2):  # **bold**
                self._chat_text.insert("end", m.group(2), "bold")
            elif m.group(3):  # *italic*
                self._chat_text.insert("end", m.group(3), "italic")
            elif m.group(4):  # `inline code`
                self._chat_text.insert("end", m.group(4), "inline_code")
            pos = m.end()
        # Remaining text
        if pos < len(text):
            self._chat_text.insert("end", text[pos:], "ai_text")

    # ==================================================================
    # Response polling
    # ==================================================================
    def _poll_queue(self):
        try:
            while True:
                msg_type, data = self.backend.response_queue.get_nowait()
                if msg_type == "chunk":
                    if not self._streaming:
                        self._clear_thinking()
                        self._streaming = True
                        self._stream_buf = []
                    self._chat_text.configure(state="normal")
                    self._chat_text.insert("end", data, "ai_text")
                    self._chat_text.configure(state="disabled")
                    self._chat_text.see("end")
                    self._stream_buf.append(data)
                elif msg_type == "tool":
                    # Agent tool activity (→ calling / ✓ done) shown as a
                    # dim status line in the transcript.
                    self._clear_thinking()
                    self._chat_text.configure(state="normal")
                    self._chat_text.insert("end", f"{data}\n", "thinking")
                    self._chat_text.configure(state="disabled")
                    self._chat_text.see("end")
                elif msg_type == "permission":
                    self._handle_permission(data)
                elif msg_type == "done":
                    if not self._streaming:
                        self._clear_thinking()
                    full_text = "".join(getattr(self, "_stream_buf", []))
                    self._chat_text.configure(state="normal")
                    self._chat_text.insert("end", "\n")
                    self._chat_text.configure(state="disabled")
                    self._streaming = False
                    self._stream_buf = []

                    # Save to conversation
                    if full_text:
                        self._active_conv.messages.append({
                            "role": "assistant", "content": full_text,
                            "time": time.time()
                        })
                    # Word count + timing
                    words = len(full_text.split())
                    timing = f"{words} words"
                    if data:  # data contains elapsed time like "1.5s"
                        timing += f"  |  {data}"
                    self._timing_lbl.config(text=timing)

                    # Swap stop → send
                    self._stop_btn.pack_forget()
                    self._send_btn.pack()
                    self._update_status("Ready", True)

                elif msg_type == "error":
                    if self._streaming:
                        self._chat_text.configure(state="normal")
                        self._chat_text.insert("end", "\n")
                        self._chat_text.configure(state="disabled")
                        self._streaming = False
                    else:
                        self._clear_thinking()
                    self._display_message("error", data)
                    self._stop_btn.pack_forget()
                    self._send_btn.pack()
                    self._update_status("Error", False)
        except queue.Empty:
            pass
        self._root.after(30, self._poll_queue)

    def _handle_permission(self, req: dict):
        """Show a modal yes/no dialog for a tool the agent wants to run.

        Runs on the Tk main thread; unblocks the waiting agent thread by
        setting the result and the Event the request carries.
        """
        from tkinter import messagebox
        detail = req.get("reason", "Run this tool?")
        args = req.get("args") or {}
        if args:
            preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
            detail = f"{detail}\n\n{preview}"
        try:
            allowed = messagebox.askyesno(
                f"Allow tool: {req.get('tool', '?')}", detail, icon="warning")
        except Exception:
            allowed = False
        req["result"]["allowed"] = bool(allowed)
        req["event"].set()

    # ==================================================================
    # Conversation management
    # ==================================================================
    def _new_conversation(self, event=None):
        conv = Conversation()
        self._conversations.append(conv)
        self._active_conv = conv
        self._has_messages = False
        self._streaming = False
        # Clear chat display
        self._chat_text.configure(state="normal")
        self._chat_text.delete("1.0", "end")
        self._chat_text.configure(state="disabled")
        if hasattr(self, "_conv_title_lbl"):
            self._conv_title_lbl.config(text="New chat")
        self._build_history_panel()
        self._add_welcome()

    def _switch_conversation(self, conv: Conversation):
        if conv.id == self._active_conv.id:
            return
        self._active_conv = conv
        self._has_messages = False
        self._streaming = False

        # Rebuild chat display from conversation messages
        self._chat_text.configure(state="normal")
        self._chat_text.delete("1.0", "end")
        self._chat_text.configure(state="disabled")

        for msg in conv.messages:
            self._display_message(msg["role"], msg["content"], msg.get("time"))

        self._conv_title_lbl.config(text=conv.title)
        self._build_history_panel()

    # ==================================================================
    # Theme toggle
    # ==================================================================
    def _toggle_theme(self):
        self._dark = not self._dark
        self.C = dict(DARK if self._dark else LIGHT)

        if CTK:
            ctk.set_appearance_mode("dark" if self._dark else "light")

        self._theme_btn.config(text="Light" if self._dark else "Dark")

        # Refresh all colors
        self._root.configure(bg=self.C["bg"])
        self._history_frame.config(bg=self.C["bg3"])
        self._sidebar.config(bg=self.C["bg2"])
        self._header.config(bg=self.C["bg2"])
        self._chat_frame.config(bg=self.C["bg"])
        self._input_frame.config(bg=self.C["bg3"])
        self._chat_text.config(bg=self.C["bg"], fg=self.C["fg"])
        self._input_text.config(bg=self.C["input_bg"], fg=self.C["fg"],
                                 insertbackground=self.C["fg"])
        self._conv_title_lbl.config(bg=self.C["bg2"], fg=self.C["fg"])
        self._theme_btn.config(bg=self.C["bg2"], fg=self.C["fg2"])
        self._status_lbl.config(bg=self.C["bg2"])
        self._timing_lbl.config(bg=self.C["bg2"], fg=self.C["fg3"])

        # Re-setup text tags with new colors
        self._setup_tags()
        # Rebuild history with new colors
        self._build_history_panel()

    # ==================================================================
    # Export
    # ==================================================================
    def _export_chat(self, event=None):
        if not self._active_conv or not self._active_conv.messages:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("JSON", "*.json"), ("All", "*.*")],
            initialfile=f"{self._active_conv.title[:30]}.md",
        )
        if not path:
            return
        if path.endswith(".json"):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "title": self._active_conv.title,
                    "messages": self._active_conv.messages,
                    "exported": time.time(),
                }, f, indent=2)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {self._active_conv.title}\n\n")
                for msg in self._active_conv.messages:
                    ts = datetime.fromtimestamp(msg.get("time", 0)).strftime("%H:%M")
                    name = "You" if msg["role"] == "user" else "carry-ai"
                    f.write(f"**{name}** ({ts})\n\n{msg['content']}\n\n---\n\n")
        self._update_status(f"Exported to {Path(path).name}", True)

    # ==================================================================
    # Helpers
    # ==================================================================
    def _lbl(self, parent, text):
        tk.Label(parent, text=text, font=(FONT_FAMILY, 11, "bold"),
                 fg=self.C["fg2"], bg=self.C["bg2"]).pack(padx=14, pady=(10, 3), anchor="w")

    def _sep(self, parent):
        tk.Frame(parent, height=1, bg=self.C["border"]).pack(fill="x", padx=10, pady=8)

    def _on_provider_change(self, _=None):
        prov = next((p for p in PROVIDERS if p["name"] == self._provider_var.get()), PROVIDERS[0])
        models = prov["models"]
        self._model_var.set(models[0])
        if CTK:
            self._model_menu.configure(values=models)
        else:
            menu = self._model_menu["menu"]
            menu.delete(0, "end")
            for m in models:
                menu.add_command(label=m, command=lambda v=m: self._model_var.set(v))

    def _update_status(self, text: str, online: bool = True):
        color = self.C["accent"] if online else self.C["error"]
        self._dot.delete("all")
        self._dot.create_oval(1, 1, 9, 9, fill=color, outline="")
        self._status_lbl.config(text=text)

    def _clear_chat(self, event=None):
        self._active_conv.messages.clear()
        self._active_conv.title = "New chat"
        self._chat_text.configure(state="normal")
        self._chat_text.delete("1.0", "end")
        self._chat_text.configure(state="disabled")
        self._has_messages = False
        self._streaming = False
        self._conv_title_lbl.config(text="New chat")
        self._timing_lbl.config(text="")
        self._add_welcome()
        self._update_status("Ready", True)

    def _open_web_ui(self):
        webbrowser.open("http://localhost:8080")

    def _copy_selection(self):
        try:
            sel = self._chat_text.get(tk.SEL_FIRST, tk.SEL_LAST)
            self._root.clipboard_clear()
            self._root.clipboard_append(sel)
        except tk.TclError:
            pass

    def _select_all(self):
        self._chat_text.tag_add(tk.SEL, "1.0", "end")

    def _show_ctx_menu(self, event):
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _add_welcome(self):
        self._display_message("system",
            "Welcome to carry-ai!\n\n"
            "Select a provider and model from the sidebar, then start chatting.\n\n"
            "Shortcuts:  Ctrl+N new chat  |  Ctrl+L clear  |  Ctrl+E export  |  Esc stop/clear\n"
        )

    # ==================================================================
    # Model Manager
    # ==================================================================
    def _open_model_manager(self):
        ModelManagerWindow(self._root, self.C)

    def run(self):
        self._root.mainloop()

    def close_when(self, event: threading.Event, interval_ms: int = 500):
        """Close the window once *event* is set (e.g. USB ejected, Ctrl+C)."""
        def _check():
            if event.is_set():
                self._root.destroy()
            else:
                self._root.after(interval_ms, _check)
        self._root.after(interval_ms, _check)


# ---------------------------------------------------------------------------
# Model Manager — download / delete GGUF models from the GUI
# ---------------------------------------------------------------------------
class ModelManagerWindow:
    """
    Toplevel window for full GGUF model management.

    Tabs:
      My Models  — list downloaded models with Set-Active / Verify / Delete
      Find Models — search HuggingFace, browse quantizations, download
    """

    # ── class-level download state shared across methods ──────────────────
    _dl_progress: float = 0.0
    _dl_status: str = ""
    _dl_result: tuple | None = None

    def __init__(self, parent, colors: dict):
        self.C = colors
        self._models_dir = PROJECT_ROOT / "models"
        self._models_dir.mkdir(exist_ok=True)

        # Download state
        self._download_thread: threading.Thread | None = None
        self._cancel = threading.Event()

        # Search / browse state
        self._search_thread: threading.Thread | None = None
        self._search_repos: list = []          # list of RepoInfo dicts
        self._files_thread: threading.Thread | None = None
        self._files_result: list = []          # list of ModelFile dicts
        self._selected_repo: str = ""
        self._selected_file: dict | None = None

        # Active tab
        self._active_tab = tk.StringVar(value="my")

        # Free RAM + dedicated VRAM, for the fit labels and download warning
        try:
            import psutil
            self._ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        except ImportError:
            self._ram_gb = 0.0
        self._vram_gb, self._gpu_name = 0.0, ""
        try:
            from integrations.llmfit_advisor import detect_hardware
            hw = detect_hardware()
            self._vram_gb, self._gpu_name = hw.vram_gb, hw.gpu_name
        except Exception as e:
            log.debug("GPU detection unavailable: %s", e)

        self._win = tk.Toplevel(parent)
        self._win.title("carry-ai — Model Manager")
        self._win.geometry("860x680")
        self._win.configure(bg=self.C["bg"])
        self._win.transient(parent)
        self._win.grab_set()

        self._build_ui()
        self._show_tab("my")

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        from tkinter import ttk

        # ── Header ──────────────────────────────────────────────────────────
        hdr = tk.Frame(self._win, bg=self.C["bg2"], height=50)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Model Manager", font=(FONT_FAMILY, 15, "bold"),
                 fg=self.C["fg"], bg=self.C["bg2"]).pack(side="left", padx=16)
        if self._ram_gb > 0:
            gpu = (f"  |  GPU: {self._gpu_name} {self._vram_gb:.0f} GB"
                   if self._vram_gb else "  |  no dedicated GPU")
            tk.Label(hdr, text=f"RAM: {self._ram_gb:.1f} GB free{gpu}",
                     font=(FONT_FAMILY, 10), fg=self.C["fg2"],
                     bg=self.C["bg2"]).pack(side="right", padx=16)

        # ── HF Token ────────────────────────────────────────────────────────
        tf = tk.Frame(self._win, bg=self.C["bg3"])
        tf.pack(fill="x")
        tk.Label(tf, text="HF Token (Gemma/Llama):",
                 font=(FONT_FAMILY, 10), fg=self.C["fg2"],
                 bg=self.C["bg3"]).pack(side="left", padx=(16, 6), pady=6)
        self._token_entry = tk.Entry(tf, width=38, show="*",
                                     bg=self.C["input_bg"], fg=self.C["fg"],
                                     insertbackground=self.C["fg"],
                                     font=(FONT_FAMILY, 10), relief="flat")
        self._token_entry.pack(side="left", padx=4, pady=6)
        self._token_entry.insert(0, os.environ.get("HF_TOKEN", ""))
        tk.Label(tf, text="huggingface.co/settings/tokens",
                 font=(FONT_FAMILY, 9), fg=self.C["fg3"],
                 bg=self.C["bg3"]).pack(side="left", padx=8)

        # ── Tab bar ─────────────────────────────────────────────────────────
        tab_bar = tk.Frame(self._win, bg=self.C["bg3"], height=36)
        tab_bar.pack(fill="x")
        tab_bar.pack_propagate(False)

        self._tab_btns = {}
        for key, label in (("my", "My Models"), ("find", "Find & Download")):
            btn = tk.Button(
                tab_bar, text=label, font=(FONT_FAMILY, 10, "bold"),
                relief="flat", bd=0, padx=18,
                command=lambda k=key: self._show_tab(k),
            )
            btn.pack(side="left", fill="y")
            self._tab_btns[key] = btn

        # ── Tab content ──────────────────────────────────────────────────────
        self._tab_content = tk.Frame(self._win, bg=self.C["bg"])
        self._tab_content.pack(fill="both", expand=True)

        # My Models frame
        self._my_frame = tk.Frame(self._tab_content, bg=self.C["bg"])
        self._my_canvas = tk.Canvas(self._my_frame, bg=self.C["bg"],
                                     highlightthickness=0, bd=0)
        my_sb = tk.Scrollbar(self._my_frame, command=self._my_canvas.yview,
                              bg=self.C["bg"], troughcolor=self.C["bg3"])
        self._my_inner = tk.Frame(self._my_canvas, bg=self.C["bg"])
        self._my_inner.bind(
            "<Configure>",
            lambda e: self._my_canvas.configure(scrollregion=self._my_canvas.bbox("all"))
        )
        self._my_canvas.create_window((0, 0), window=self._my_inner, anchor="nw")
        self._my_canvas.configure(yscrollcommand=my_sb.set)
        my_sb.pack(side="right", fill="y")
        self._my_canvas.pack(side="left", fill="both", expand=True)

        # Find Models frame
        self._find_frame = tk.Frame(self._tab_content, bg=self.C["bg"])
        self._build_find_tab()

        # ── Progress bar (shared, always visible at bottom) ─────────────────
        prog_frame = tk.Frame(self._win, bg=self.C["bg3"], height=52)
        prog_frame.pack(fill="x")
        prog_frame.pack_propagate(False)

        self._progress_lbl = tk.Label(prog_frame, text="",
                                       font=(FONT_FAMILY, 9), fg=self.C["fg2"],
                                       bg=self.C["bg3"], anchor="w")
        self._progress_lbl.pack(side="left", padx=16, pady=4, fill="x", expand=True)

        self._progress_bar_var = tk.DoubleVar(value=0)
        style = ttk.Style()
        style.configure("carry.Horizontal.TProgressbar",
                        background=self.C["accent"], troughcolor=self.C["bg"])
        self._progress_bar = ttk.Progressbar(
            prog_frame, variable=self._progress_bar_var,
            maximum=100, style="carry.Horizontal.TProgressbar", length=240)
        self._progress_bar.pack(side="right", padx=16, pady=14)

    def _build_find_tab(self):
        """Build the Find & Download tab content (called once)."""
        C = self.C
        ff = self._find_frame

        # Search row
        sr = tk.Frame(ff, bg=C["bg3"])
        sr.pack(fill="x", padx=0, pady=0)
        tk.Label(sr, text="Search HF:", font=(FONT_FAMILY, 10), fg=C["fg2"],
                 bg=C["bg3"]).pack(side="left", padx=(16, 6), pady=8)
        self._search_var = tk.StringVar()
        se = tk.Entry(sr, textvariable=self._search_var, width=28,
                      bg=C["input_bg"], fg=C["fg"], insertbackground=C["fg"],
                      font=(FONT_FAMILY, 10), relief="flat")
        se.pack(side="left", padx=4, pady=8)
        se.bind("<Return>", lambda e: self._do_search())
        tk.Button(sr, text="Search", font=(FONT_FAMILY, 10, "bold"),
                  bg=C["accent"], fg="#111", relief="flat", padx=10,
                  command=self._do_search).pack(side="left", padx=6, pady=6)

        tk.Label(sr, text="or repo ID:", font=(FONT_FAMILY, 10), fg=C["fg2"],
                 bg=C["bg3"]).pack(side="left", padx=(20, 6), pady=8)
        self._repo_var = tk.StringVar()
        re_entry = tk.Entry(sr, textvariable=self._repo_var, width=28,
                             bg=C["input_bg"], fg=C["fg"], insertbackground=C["fg"],
                             font=(FONT_FAMILY, 10), relief="flat")
        re_entry.pack(side="left", padx=4, pady=8)
        re_entry.bind("<Return>", lambda e: self._do_list_repo())
        tk.Button(sr, text="List Files", font=(FONT_FAMILY, 10),
                  bg=C["bg2"], fg=C["fg"], relief="flat", padx=8,
                  command=self._do_list_repo).pack(side="left", padx=4, pady=6)

        # Two-column layout: left=repos, right=quants
        cols = tk.Frame(ff, bg=C["bg"])
        cols.pack(fill="both", expand=True, padx=0, pady=0)

        # Left: search results
        left = tk.Frame(cols, bg=C["bg"])
        left.pack(side="left", fill="both", expand=True, padx=(12, 4), pady=8)
        tk.Label(left, text="Search Results", font=(FONT_FAMILY, 10, "bold"),
                 fg=C["fg2"], bg=C["bg"]).pack(anchor="w", pady=(0, 4))
        lb_frame = tk.Frame(left, bg=C["bg"])
        lb_frame.pack(fill="both", expand=True)
        self._repo_lb = tk.Listbox(lb_frame, bg=C["bg2"], fg=C["fg"],
                                    selectbackground=C["accent"], selectforeground="#111",
                                    font=(FONT_FAMILY, 10), relief="flat",
                                    activestyle="none", bd=0)
        repo_sb = tk.Scrollbar(lb_frame, command=self._repo_lb.yview,
                                bg=C["bg"], troughcolor=C["bg3"])
        self._repo_lb.configure(yscrollcommand=repo_sb.set)
        repo_sb.pack(side="right", fill="y")
        self._repo_lb.pack(side="left", fill="both", expand=True)
        self._repo_lb.bind("<<ListboxSelect>>", self._on_repo_select)

        # Right: quantizations
        right = tk.Frame(cols, bg=C["bg"])
        right.pack(side="left", fill="both", expand=True, padx=(4, 12), pady=8)
        tk.Label(right, text="Quantizations", font=(FONT_FAMILY, 10, "bold"),
                 fg=C["fg2"], bg=C["bg"]).pack(anchor="w", pady=(0, 4))
        qf = tk.Frame(right, bg=C["bg"])
        qf.pack(fill="both", expand=True)
        self._quant_lb = tk.Listbox(qf, bg=C["bg2"], fg=C["fg"],
                                     selectbackground=C["accent"], selectforeground="#111",
                                     font=(FONT_FAMILY, 10), relief="flat",
                                     activestyle="none", bd=0)
        q_sb = tk.Scrollbar(qf, command=self._quant_lb.yview,
                             bg=C["bg"], troughcolor=C["bg3"])
        self._quant_lb.configure(yscrollcommand=q_sb.set)
        q_sb.pack(side="right", fill="y")
        self._quant_lb.pack(side="left", fill="both", expand=True)
        self._quant_lb.bind("<<ListboxSelect>>", self._on_quant_select)

        # Download button row
        dl_row = tk.Frame(ff, bg=C["bg3"])
        dl_row.pack(fill="x")
        self._dl_btn = tk.Button(dl_row, text="Download Selected",
                                  font=(FONT_FAMILY, 11, "bold"),
                                  bg=C["accent"], fg="#111", relief="flat",
                                  padx=20, pady=6,
                                  state="disabled",
                                  command=self._start_download_selected)
        self._dl_btn.pack(side="left", padx=16, pady=8)
        self._dl_info_lbl = tk.Label(dl_row, text="",
                                      font=(FONT_FAMILY, 9), fg=C["fg2"], bg=C["bg3"])
        self._dl_info_lbl.pack(side="left", padx=8)

    # ── Tab switching ──────────────────────────────────────────────────────

    def _show_tab(self, tab: str):
        self._active_tab.set(tab)
        # Update tab button colours
        for k, btn in self._tab_btns.items():
            if k == tab:
                btn.config(bg=self.C["accent"], fg="#111")
            else:
                btn.config(bg=self.C["bg3"], fg=self.C["fg2"])

        for frame in (self._my_frame, self._find_frame):
            frame.pack_forget()

        if tab == "my":
            self._my_frame.pack(fill="both", expand=True)
            self._refresh_my_models()
        else:
            self._find_frame.pack(fill="both", expand=True)

    # ── My Models tab ─────────────────────────────────────────────────────

    def _refresh_my_models(self):
        """Rebuild the My Models list from disk + registry."""
        for w in self._my_inner.winfo_children():
            w.destroy()

        # Load registry if available
        registry_info: dict[str, dict] = {}
        active_model = ""
        try:
            from models.registry import get_registry
            reg = get_registry()
            reg.scan()
            active_model = reg.get_active() or ""
            for entry in reg.list_all():
                registry_info[entry["filename"]] = entry
        except Exception:
            pass

        # Scan models dir directly as fallback
        found: list[dict] = []
        if self._models_dir.is_dir():
            for f in sorted(self._models_dir.glob("*.gguf")):
                size_gb = f.stat().st_size / (1024 ** 3)
                info = registry_info.get(f.name, {})
                found.append({
                    "filename": f.name,
                    "size_gb": size_gb,
                    "quant": info.get("quant", "") or self._extract_quant(f.name),
                    "verified_ok": info.get("verified_ok"),
                    "repo_id": info.get("repo_id", ""),
                    "active": f.name == active_model,
                })

        if not found:
            tk.Label(self._my_inner,
                     text="No models downloaded yet.\nSwitch to 'Find & Download' to get one.",
                     font=(FONT_FAMILY, 11), fg=self.C["fg2"], bg=self.C["bg"],
                     justify="center").pack(pady=40)
            return

        tk.Label(self._my_inner, text=f"{len(found)} model(s) on this USB",
                 font=(FONT_FAMILY, 11, "bold"), fg=self.C["accent"],
                 bg=self.C["bg"]).pack(anchor="w", padx=16, pady=(12, 4))

        for m in found:
            row = tk.Frame(self._my_inner,
                           bg=self.C["accent"] if m["active"] else self.C["bg2"],
                           pady=2)
            row.pack(fill="x", padx=16, pady=3)

            # Left: name + meta
            left = tk.Frame(row, bg=row["bg"])
            left.pack(side="left", fill="x", expand=True, padx=8, pady=4)

            name_label = m["filename"]
            if m["active"]:
                name_label = "★ " + name_label
            tk.Label(left, text=name_label,
                     font=(FONT_FAMILY, 10, "bold" if m["active"] else "normal"),
                     fg="#111" if m["active"] else self.C["fg"],
                     bg=row["bg"], anchor="w").pack(anchor="w")

            meta_parts = [f"{m['size_gb']:.1f} GB"]
            if m["quant"]:
                meta_parts.append(m["quant"])
            if self._ram_gb > 0:
                meta_parts.append(self._fit(m["size_gb"]).label())
            if m["verified_ok"] is True:
                meta_parts.append("verified ✓")
            elif m["verified_ok"] is False:
                meta_parts.append("CORRUPT!")

            tk.Label(left, text="  ".join(meta_parts),
                     font=(FONT_FAMILY, 9),
                     fg="#333" if m["active"] else self.C["fg2"],
                     bg=row["bg"], anchor="w").pack(anchor="w")

            # Right: action buttons
            btns = tk.Frame(row, bg=row["bg"])
            btns.pack(side="right", padx=8, pady=4)

            if not m["active"]:
                tk.Button(btns, text="Set Active", font=(FONT_FAMILY, 9),
                          bg=self.C["bg3"], fg=self.C["fg"], relief="flat", padx=6,
                          command=lambda fn=m["filename"]: self._set_active(fn)
                          ).pack(side="left", padx=2)

            tk.Button(btns, text="Verify", font=(FONT_FAMILY, 9),
                      bg=self.C["bg3"], fg=self.C["fg"], relief="flat", padx=6,
                      command=lambda fn=m["filename"]: self._verify_model(fn)
                      ).pack(side="left", padx=2)

            tk.Button(btns, text="Delete", font=(FONT_FAMILY, 9),
                      bg=self.C["error"], fg="#fff", relief="flat", padx=6,
                      command=lambda fn=m["filename"]: self._delete_model(fn)
                      ).pack(side="left", padx=2)

    def _set_active(self, filename: str):
        try:
            from models.registry import get_registry
            get_registry().set_active(filename)
            self._progress_lbl.config(text=f"Active model set to: {filename}")
        except Exception as e:
            self._progress_lbl.config(text=f"Could not set active: {e}")
        self._refresh_my_models()

    def _verify_model(self, filename: str):
        self._progress_lbl.config(text=f"Verifying {filename}…")
        self._progress_bar_var.set(0)

        def _worker():
            try:
                from models.registry import get_registry
                ok, msg = get_registry().verify(filename)
                self._dl_result = ("ok" if ok else "error", msg)
            except Exception as e:
                self._dl_result = ("error", f"Verify failed: {e}")

        threading.Thread(target=_worker, daemon=True).start()
        self._poll_verify()

    def _poll_verify(self):
        if self._dl_result:
            _, msg = self._dl_result
            self._dl_result = None
            self._progress_lbl.config(text=msg)
            self._refresh_my_models()
            return
        self._win.after(200, self._poll_verify)

    def _delete_model(self, filename: str):
        path = self._models_dir / filename
        if path.exists():
            path.unlink()
        try:
            from models.registry import get_registry
            get_registry().remove(filename)
        except Exception:
            pass
        self._progress_lbl.config(text=f"Deleted {filename}")
        self._refresh_my_models()

    @staticmethod
    def _extract_quant(filename: str) -> str:
        import re as _re
        m = _re.search(
            r"[_\-\.]((?:IQ[1-4]_[A-Z]+|Q[0-9]+(?:_[A-Z0-9]+)*|F16|F32|BF16))"
            r"(?:[_\-\.]|\.gguf)", filename, _re.IGNORECASE
        )
        return m.group(1).upper() if m else ""

    # ── Find & Download tab ────────────────────────────────────────────────

    def _do_search(self):
        query = self._search_var.get().strip()
        if not query:
            return
        self._repo_lb.delete(0, "end")
        self._quant_lb.delete(0, "end")
        self._search_repos = []
        self._progress_lbl.config(text=f"Searching HuggingFace for '{query}'…")
        self._progress_bar_var.set(0)
        self._dl_btn.config(state="disabled")

        def _worker():
            try:
                from models.downloader import ModelDownloader
                dl = ModelDownloader(hf_token=self._token_entry.get().strip() or None)
                results = dl.search(query, limit=15)
                self._search_repos = results
                self._dl_result = ("search_done", f"Found {len(results)} repos")
            except Exception as e:
                self._dl_result = ("error", f"Search failed: {e}")

        self._search_thread = threading.Thread(target=_worker, daemon=True)
        self._search_thread.start()
        self._poll_search()

    def _poll_search(self):
        if self._dl_result and self._dl_result[0] in ("search_done", "error"):
            _, msg = self._dl_result
            self._dl_result = None
            self._progress_lbl.config(text=msg)
            self._repo_lb.delete(0, "end")
            for r in self._search_repos:
                repo_id = r.repo_id if hasattr(r, "repo_id") else r.get("repo_id", "")
                downloads = r.downloads if hasattr(r, "downloads") else r.get("downloads", 0)
                label = f"{repo_id}  ({downloads:,} downloads)" if downloads else repo_id
                self._repo_lb.insert("end", label)
            return
        self._win.after(150, self._poll_search)

    def _do_list_repo(self):
        repo_id = self._repo_var.get().strip()
        if not repo_id:
            return
        self._selected_repo = repo_id
        self._list_quants_for(repo_id)

    def _on_repo_select(self, event):
        sel = self._repo_lb.curselection()
        if not sel:
            return
        # Extract repo_id from label (everything before first whitespace after two slashes)
        label = self._repo_lb.get(sel[0])
        repo_id = label.split("  ")[0].strip()
        self._selected_repo = repo_id
        self._repo_var.set(repo_id)
        self._list_quants_for(repo_id)

    def _list_quants_for(self, repo_id: str):
        self._quant_lb.delete(0, "end")
        self._files_result = []
        self._selected_file = None
        self._dl_btn.config(state="disabled")
        self._progress_lbl.config(text=f"Fetching files from {repo_id}…")

        def _worker():
            try:
                from models.downloader import ModelDownloader
                dl = ModelDownloader(hf_token=self._token_entry.get().strip() or None)
                files = dl.list_files(repo_id)
                self._files_result = files
                self._dl_result = ("files_done", f"{len(files)} quantizations found")
            except Exception as e:
                self._dl_result = ("error", f"Failed: {e}")

        self._files_thread = threading.Thread(target=_worker, daemon=True)
        self._files_thread.start()
        self._poll_files()

    def _poll_files(self):
        if self._dl_result and self._dl_result[0] in ("files_done", "error"):
            _, msg = self._dl_result
            self._dl_result = None
            self._progress_lbl.config(text=msg)
            self._quant_lb.delete(0, "end")
            existing = {f.name for f in self._models_dir.glob("*.gguf")} if self._models_dir.is_dir() else set()
            for f in self._files_result:
                fname = f.filename if hasattr(f, "filename") else f.get("filename", "")
                size_gb = (f.size_bytes if hasattr(f, "size_bytes") else f.get("size_bytes", 0)) / (1024**3)
                quant = f.quant if hasattr(f, "quant") else f.get("quant", "")
                ram_hint = f"  {self._fit(size_gb).label()}" if self._ram_gb > 0 else ""
                status = " [downloaded]" if fname in existing else ""
                label = f"[{quant or '?':>8}]  {size_gb:.1f} GB{ram_hint}  {fname}{status}"
                self._quant_lb.insert("end", label)
            return
        self._win.after(150, self._poll_files)

    def _on_quant_select(self, event):
        sel = self._quant_lb.curselection()
        if not sel or not self._files_result:
            return
        idx = sel[0]
        if idx < len(self._files_result):
            f = self._files_result[idx]
            self._selected_file = f
            fname = f.filename if hasattr(f, "filename") else f.get("filename", "")
            size_gb = (f.size_bytes if hasattr(f, "size_bytes") else f.get("size_bytes", 0)) / (1024**3)
            self._dl_info_lbl.config(text=f"{fname}  ({size_gb:.2f} GB)")
            self._dl_btn.config(state="normal")

    def _fit(self, size_gb: float):
        """Fit estimate against this PC's free RAM + dedicated VRAM."""
        from models.fit import estimate_fit
        return estimate_fit(size_gb, self._ram_gb, self._vram_gb)

    def _start_download_selected(self):
        if not self._selected_file or not self._selected_repo:
            return
        f = self._selected_file
        fname = f.filename if hasattr(f, "filename") else f.get("filename", "")
        size_gb = (f.size_bytes if hasattr(f, "size_bytes") else f.get("size_bytes", 0)) / (1024**3)
        if self._ram_gb > 0 and size_gb > 0:
            fit = self._fit(size_gb)
            if not fit.ok:
                from tkinter import messagebox
                if not messagebox.askyesno(
                        "Model too big for this PC",
                        f"{fname} needs about {fit.needed_gb:.0f} GB at run time, but "
                        f"this PC has about {fit.budget_gb:.0f} GB free (RAM"
                        f"{' + GPU' if self._vram_gb else ''}).\n\n"
                        "It may fail to load or be very slow. Download anyway?",
                        icon="warning", parent=self._win):
                    return
        gated = False  # unknown from search; rely on 401 handling
        model = {
            "name": fname,
            "hf_repo": self._selected_repo,
            "hf_file": fname,
            "gated": gated,
        }
        self._start_download(model)

    # ── Download engine (shared) ───────────────────────────────────────────

    def _start_download(self, model: dict):
        if self._download_thread and self._download_thread.is_alive():
            self._progress_lbl.config(text="A download is already in progress.")
            return
        hf_token = self._token_entry.get().strip()
        if model.get("gated") and not hf_token:
            self._progress_lbl.config(
                text="This model needs an HF token — paste it in the field above.")
            return
        self._cancel.clear()
        self._dl_progress = 0.0
        self._dl_status = ""
        self._dl_result = None
        self._progress_bar_var.set(0)
        self._progress_lbl.config(text=f"Starting download: {model.get('name', model['hf_file'])}…")
        self._download_thread = threading.Thread(
            target=self._download_worker, args=(model, hf_token), daemon=True)
        self._download_thread.start()
        self._poll_download()

    def _download_worker(self, model: dict, hf_token: str = ""):
        """Download a model in a background thread with resume + retry."""
        import time as _time
        from urllib.request import urlopen, Request
        from urllib.error import URLError, HTTPError

        filename = model["hf_file"]
        dest = self._models_dir / filename
        url = f"https://huggingface.co/{model['hf_repo']}/resolve/main/{filename}"
        display_name = model.get("name", filename)

        def _attempt(retry: bool = False) -> bool:
            try:
                # Resume support: check existing partial file
                existing = dest.stat().st_size if dest.exists() else 0
                headers = {"User-Agent": "carry-ai/desktop"}
                if hf_token:
                    headers["Authorization"] = f"Bearer {hf_token}"
                if existing > 0 and not retry:
                    headers["Range"] = f"bytes={existing}-"

                req = Request(url, headers=headers)
                with urlopen(req, timeout=600) as resp:
                    content_len = int(resp.headers.get("Content-Length", 0))
                    total = content_len + existing if existing > 0 and resp.status == 206 else content_len
                    written = existing
                    mode = "ab" if existing > 0 and resp.status == 206 else "wb"
                    if mode == "wb":
                        written = 0

                    with open(dest, mode) as f:
                        while True:
                            if self._cancel.is_set():
                                self._dl_result = ("cancelled", "Download cancelled.")
                                return True
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            written += len(chunk)
                            if total:
                                self._dl_progress = min(written * 100.0 / total, 99.9)
                                self._dl_status = (
                                    f"{display_name}: "
                                    f"{written/1_048_576:.0f}/{total/1_048_576:.0f} MB "
                                    f"({self._dl_progress:.0f}%)"
                                )

                self._dl_progress = 100.0
                self._dl_result = ("ok", f"{display_name} downloaded! ({dest.stat().st_size/(1024**3):.2f} GB)")

                # Register in registry
                try:
                    from models.registry import get_registry
                    reg = get_registry()
                    quant = self._extract_quant(filename)
                    reg.add(filename, repo_id=model.get("hf_repo", ""),
                            size_bytes=dest.stat().st_size,
                            gated=model.get("gated", False), quant=quant)
                    # Auto-set as active if it's the first model
                    if not reg.get_active():
                        reg.set_active(filename)
                except Exception:
                    pass
                return True

            except HTTPError as e:
                if dest.exists() and dest.stat().st_size == 0:
                    dest.unlink(missing_ok=True)
                if e.code == 401:
                    self._dl_result = ("error",
                        f"401 Unauthorized — HF token required for '{display_name}'.\n"
                        f"  1. huggingface.co/settings/tokens → create a token\n"
                        f"  2. Accept license: huggingface.co/{model.get('hf_repo','')}\n"
                        f"  3. Paste your token in the HF Token field above")
                    return True
                elif e.code in (500, 502, 503, 504) and not retry:
                    self._dl_status = f"Server error {e.code}, retrying in 5 s…"
                    _time.sleep(5)
                    return False
                else:
                    self._dl_result = ("error",
                        f"HTTP {e.code} — {e.reason}\nURL: {url}")
                    return True
            except (URLError, OSError) as e:
                self._dl_result = ("error", f"Download failed: {e}")
                return True

        if not _attempt(retry=False):
            _attempt(retry=True)

    def _poll_download(self):
        """Poll background download thread, update progress bar."""
        if self._dl_result:
            status, msg = self._dl_result
            self._dl_result = None
            self._progress_bar_var.set(100 if status == "ok" else 0)
            # Show only first line to fit label
            self._progress_lbl.config(text=msg.splitlines()[0])
            # Refresh My Models tab if it's visible
            if self._active_tab.get() == "my":
                self._refresh_my_models()
            return

        self._progress_bar_var.set(self._dl_progress)
        if self._dl_status:
            self._progress_lbl.config(text=self._dl_status)
        self._win.after(100, self._poll_download)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    CarryAIApp().run()

if __name__ == "__main__":
    main()
