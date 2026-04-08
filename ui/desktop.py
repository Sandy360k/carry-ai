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

import importlib
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
     "models": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"]},
    {"name": "OpenAI", "key": "openai",
     "models": ["gpt-4o", "o4-mini", "o3"]},
    {"name": "Google (Gemini)", "key": "google",
     "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]},
    {"name": "Groq", "key": "groq",
     "models": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"]},
    {"name": "OpenRouter", "key": "openrouter",
     "models": ["auto"]},
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

    def __init__(self):
        self.response_queue: queue.Queue = queue.Queue()
        self._running = False
        self._cancel = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running

    def send_message(self, conversation: list[dict], provider_key: str, model: str) -> None:
        self._running = True
        self._cancel.clear()
        t = threading.Thread(
            target=self._run_inference,
            args=(list(conversation), provider_key, model),
            daemon=True,
        )
        t.start()

    def cancel(self):
        self._cancel.set()

    def _run_inference(self, messages: list[dict], provider_key: str, model: str) -> None:
        try:
            t0 = time.time()
            text = self._call_provider(messages, provider_key, model)
            elapsed = time.time() - t0
            for i in range(0, len(text), 3):
                if self._cancel.is_set():
                    self.response_queue.put(("done", ""))
                    return
                self.response_queue.put(("chunk", text[i:i+3]))
                time.sleep(0.012)
            self.response_queue.put(("done", f"{elapsed:.1f}s"))
        except Exception as e:
            self.response_queue.put(("error", str(e)))
        finally:
            self._running = False

    def _call_provider(self, messages: list[dict], provider_key: str, model: str) -> str:
        provider_map = {
            "anthropic": ("providers.anthropic_provider", "AnthropicProvider"),
            "openai":    ("providers.openai_provider",    "OpenAIProvider"),
            "google":    ("providers.google_oauth",       "GoogleProvider"),
            "groq":      ("providers.groq_provider",      "GroqProvider"),
            "openrouter":("providers.openrouter_provider", "OpenRouterProvider"),
        }
        if provider_key == "local":
            return ("Local mode requires a running llama-server.\n"
                    "Start with: python launcher.py --mode local\n"
                    "Then use the Web UI at localhost:8080.")
        if provider_key not in provider_map:
            return f"Provider '{provider_key}' not supported yet."

        module_path, class_name = provider_map[provider_key]
        key = self._get_key(provider_key)
        if not key:
            return (f"No {provider_key} API key configured.\n\n"
                    "Set one up:\n  python onboard.py\n  python crypto/keystore.py setup")
        try:
            mod = importlib.import_module(module_path)
            ProviderClass = getattr(mod, class_name)
        except ImportError as e:
            return f"Missing dependency: {e}\nRun: pip install -r requirements.txt"
        except AttributeError:
            return f"Provider class {class_name} not found."
        try:
            prov = ProviderClass(api_key=key)
            resp = prov.chat(messages, model=model)
            return resp.content
        except Exception as e:
            return f"Provider error ({provider_key}): {e}"

    def _get_key(self, name: str) -> str | None:
        env = os.environ.get(f"CARRY_AI_{name.upper()}_KEY")
        if env:
            return env
        try:
            from crypto.keystore import KeyStore
            enc = PROJECT_ROOT / "config" / "providers.enc"
            if enc.is_file():
                ks = KeyStore(enc_path=enc)
                return ks.decrypt_interactive().get(name)
        except Exception:
            pass
        return None

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
        self._active_conv: Conversation | None = None
        self._new_conversation()

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

        self.backend.send_message(self._active_conv.messages, prov["key"], model)

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
        """Remove the thinking indicator, keep the ai_name header."""
        if hasattr(self, "_thinking_idx"):
            self._chat_text.configure(state="normal")
            self._chat_text.delete(self._thinking_idx, "end")
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

    def run(self):
        self._root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    CarryAIApp().run()

if __name__ == "__main__":
    main()
