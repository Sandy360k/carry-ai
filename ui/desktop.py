"""
carry-ai/ui/desktop.py — Native Desktop Chat Application
=========================================================
A Claude-style desktop GUI for carry-ai. Launch via:

    python -m ui.desktop          # from carry-ai/
    python ui/desktop.py          # direct

Features:
  - Dark-theme chat interface (like Claude)
  - Sidebar with provider/model selection
  - Streaming-style message display
  - Connects to carry-ai's provider backend

Uses customtkinter if installed (modern look), falls back to tkinter.
"""

import logging
import os
import platform
import queue
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root — ensure carry-ai modules are importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

log = logging.getLogger("carry-ai.desktop")

# ---------------------------------------------------------------------------
# GUI toolkit — prefer customtkinter, fall back to tkinter
# ---------------------------------------------------------------------------
try:
    import customtkinter as ctk
    CTK = True
except ImportError:
    CTK = False

import tkinter as tk
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
COLORS = {
    "bg":       "#1a1a2e",
    "bg2":      "#16213e",
    "bg3":      "#0f0e1a",
    "fg":       "#e8e8e8",
    "fg2":      "#8888aa",
    "user_bg":  "#1a3a5c",
    "ai_bg":    "#1e1e3a",
    "accent":   "#4ecca3",
    "accent2":  "#533483",
    "border":   "#2a2a4a",
    "input_bg": "#12112a",
    "code_bg":  "#0d1117",
    "error":    "#e94560",
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

WINDOW_TITLE = "carry-ai"
WINDOW_SIZE = "1100x750"
SIDEBAR_W = 240
FONT_FAMILY = "Segoe UI" if platform.system() == "Windows" else "Helvetica"
MONO_FAMILY = "Consolas" if platform.system() == "Windows" else "DejaVu Sans Mono"

# ---------------------------------------------------------------------------
# Chat backend — connects to carry-ai providers
# ---------------------------------------------------------------------------

class ChatBackend:
    """Manages LLM inference in background threads."""

    def __init__(self):
        self.conversation: list[dict] = []
        self.response_queue: queue.Queue = queue.Queue()
        self._running = False

    def send_message(self, message: str, provider_key: str, model: str) -> None:
        """Enqueue a user message and start inference in a thread."""
        self.conversation.append({"role": "user", "content": message})
        self._running = True
        t = threading.Thread(
            target=self._run_inference,
            args=(provider_key, model),
            daemon=True,
        )
        t.start()

    def _run_inference(self, provider_key: str, model: str) -> None:
        try:
            text = self._call_provider(provider_key, model)
            # Simulate streaming — emit in small chunks
            for i in range(0, len(text), 3):
                self.response_queue.put(("chunk", text[i:i+3]))
                time.sleep(0.012)
            self.response_queue.put(("done", ""))
            self.conversation.append({"role": "assistant", "content": text})
        except Exception as e:
            self.response_queue.put(("error", str(e)))
        finally:
            self._running = False

    def _call_provider(self, provider_key: str, model: str) -> str:
        """Route to the correct carry-ai provider."""
        provider_map = {
            "anthropic": ("providers.anthropic_provider", "AnthropicProvider"),
            "openai":    ("providers.openai_provider",    "OpenAIProvider"),
            "google":    ("providers.google_oauth",       "GoogleProvider"),
            "groq":      ("providers.groq_provider",      "GroqProvider"),
            "openrouter":("providers.openrouter_provider", "OpenRouterProvider"),
        }

        if provider_key == "local":
            return (
                "Local mode requires a running llama-server.\n"
                "Start with: python launcher.py --mode local\n"
                "Then use the Web UI at localhost:8080."
            )

        if provider_key not in provider_map:
            return f"Provider '{provider_key}' is not supported yet."

        module_path, class_name = provider_map[provider_key]

        # Get API key
        key = self._get_key(provider_key)
        if not key:
            return (
                f"No {provider_key} API key configured.\n\n"
                "Set one up with:\n"
                "  python onboard.py\n"
                "or:\n"
                "  python crypto/keystore.py setup"
            )

        # Import provider
        try:
            import importlib
            mod = importlib.import_module(module_path)
            ProviderClass = getattr(mod, class_name)
        except ImportError as e:
            return f"Missing dependency: {e}\nRun: pip install -r requirements.txt"
        except AttributeError:
            return f"Provider class {class_name} not found in {module_path}."

        # Call the provider
        try:
            prov = ProviderClass(api_key=key)
            resp = prov.chat(self.conversation, model=model)
            return resp.content
        except Exception as e:
            return f"Provider error ({provider_key}): {e}"

    def _get_key(self, provider_name: str) -> str | None:
        """Try keystore first, then environment variable."""
        # Environment variable
        env_key = os.environ.get(f"CARRY_AI_{provider_name.upper()}_KEY")
        if env_key:
            return env_key

        # Keystore on USB
        try:
            from crypto.keystore import KeyStore
            enc_path = PROJECT_ROOT / "config" / "providers.enc"
            if enc_path.is_file():
                ks = KeyStore(enc_path=enc_path)
                keys = ks.decrypt_interactive()
                return keys.get(provider_name)
        except Exception:
            pass
        return None

    def clear(self):
        self.conversation.clear()

# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class CarryAIApp:
    """Native desktop chat application."""

    def __init__(self):
        self.backend = ChatBackend()
        self._streaming = False
        self._has_messages = False

        # ---- Window -------------------------------------------------------
        if CTK:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("green")
            self._root = ctk.CTk()
        else:
            self._root = tk.Tk()
            self._root.configure(bg=COLORS["bg"])

        self._root.title(WINDOW_TITLE)
        self._root.geometry(WINDOW_SIZE)
        self._root.minsize(800, 500)

        # Try to set icon
        try:
            icon_path = PROJECT_ROOT / "ui" / "icon.ico"
            if icon_path.exists():
                self._root.iconbitmap(str(icon_path))
        except Exception:
            pass

        # ---- Layout -------------------------------------------------------
        self._create_sidebar()
        self._create_chat_area()
        self._create_input_area()

        # ---- Bindings -----------------------------------------------------
        self._root.bind("<Escape>", lambda e: self._clear_input())

        # ---- Start polling + welcome message ------------------------------
        self._poll_response_queue()
        self._add_welcome()

    # ==================================================================
    # Sidebar
    # ==================================================================
    def _create_sidebar(self):
        if CTK:
            self._sidebar = ctk.CTkFrame(self._root, width=SIDEBAR_W,
                                          corner_radius=0, fg_color=COLORS["bg2"])
        else:
            self._sidebar = tk.Frame(self._root, width=SIDEBAR_W,
                                      bg=COLORS["bg2"])
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        # Title
        if CTK:
            ctk.CTkLabel(self._sidebar, text="carry-ai",
                         font=ctk.CTkFont(size=20, weight="bold"),
                         text_color=COLORS["accent"]).pack(pady=(20, 5), padx=15, anchor="w")
            ctk.CTkLabel(self._sidebar, text="Portable AI Assistant",
                         font=ctk.CTkFont(size=11),
                         text_color=COLORS["fg2"]).pack(padx=15, anchor="w")
        else:
            tk.Label(self._sidebar, text="carry-ai",
                     font=(FONT_FAMILY, 18, "bold"), fg=COLORS["accent"],
                     bg=COLORS["bg2"]).pack(pady=(20, 2), padx=15, anchor="w")
            tk.Label(self._sidebar, text="Portable AI Assistant",
                     font=(FONT_FAMILY, 10), fg=COLORS["fg2"],
                     bg=COLORS["bg2"]).pack(padx=15, anchor="w")

        # Separator
        self._sep(self._sidebar)

        # Provider selector
        self._add_label(self._sidebar, "Provider")
        provider_names = [p["name"] for p in PROVIDERS]
        self._provider_var = tk.StringVar(value=provider_names[0])
        if CTK:
            self._provider_menu = ctk.CTkOptionMenu(
                self._sidebar, variable=self._provider_var,
                values=provider_names, command=self._on_provider_change,
                width=SIDEBAR_W - 30)
        else:
            self._provider_menu = tk.OptionMenu(
                self._sidebar, self._provider_var, *provider_names,
                command=self._on_provider_change)
            self._provider_menu.config(bg=COLORS["bg3"], fg=COLORS["fg"],
                                        highlightthickness=0, font=(FONT_FAMILY, 11))
        self._provider_menu.pack(padx=15, pady=(0, 10), fill="x")

        # Model selector
        self._add_label(self._sidebar, "Model")
        first_models = PROVIDERS[0]["models"]
        self._model_var = tk.StringVar(value=first_models[0])
        if CTK:
            self._model_menu = ctk.CTkOptionMenu(
                self._sidebar, variable=self._model_var,
                values=first_models, width=SIDEBAR_W - 30)
        else:
            self._model_menu = tk.OptionMenu(
                self._sidebar, self._model_var, *first_models)
            self._model_menu.config(bg=COLORS["bg3"], fg=COLORS["fg"],
                                     highlightthickness=0, font=(FONT_FAMILY, 11))
        self._model_menu.pack(padx=15, pady=(0, 10), fill="x")

        # Separator
        self._sep(self._sidebar)

        # Status
        self._add_label(self._sidebar, "Status")
        status_frame = tk.Frame(self._sidebar, bg=COLORS["bg2"])
        status_frame.pack(padx=15, fill="x")
        self._status_dot = tk.Canvas(status_frame, width=10, height=10,
                                      bg=COLORS["bg2"], highlightthickness=0)
        self._status_dot.pack(side="left", padx=(0, 6))
        self._status_dot.create_oval(1, 1, 9, 9, fill=COLORS["accent"], outline="")
        self._status_label = tk.Label(status_frame, text="Ready",
                                       font=(FONT_FAMILY, 11), fg=COLORS["fg2"],
                                       bg=COLORS["bg2"])
        self._status_label.pack(side="left")

        # RAM info
        ram_text = "RAM: unknown"
        try:
            import psutil
            ram_gb = psutil.virtual_memory().available / (1024**3)
            ram_text = f"RAM: {ram_gb:.1f} GB free"
        except ImportError:
            pass
        if CTK:
            ctk.CTkLabel(self._sidebar, text=ram_text,
                         font=ctk.CTkFont(size=11),
                         text_color=COLORS["fg2"]).pack(padx=15, pady=(5, 0), anchor="w")
        else:
            tk.Label(self._sidebar, text=ram_text,
                     font=(FONT_FAMILY, 10), fg=COLORS["fg2"],
                     bg=COLORS["bg2"]).pack(padx=15, pady=(5, 0), anchor="w")

        # Bottom buttons
        btn_frame = tk.Frame(self._sidebar, bg=COLORS["bg2"])
        btn_frame.pack(side="bottom", fill="x", padx=10, pady=10)

        if CTK:
            ctk.CTkButton(btn_frame, text="New Chat", command=self._clear_chat,
                           fg_color=COLORS["accent2"], hover_color="#6a42a0",
                           height=30).pack(fill="x", pady=2)
            ctk.CTkButton(btn_frame, text="Web UI", command=self._open_web_ui,
                           fg_color=COLORS["border"], hover_color="#3a3a5a",
                           height=30).pack(fill="x", pady=2)
        else:
            tk.Button(btn_frame, text="New Chat", command=self._clear_chat,
                      bg=COLORS["accent2"], fg=COLORS["fg"],
                      relief="flat", font=(FONT_FAMILY, 11)).pack(fill="x", pady=2)
            tk.Button(btn_frame, text="Web UI", command=self._open_web_ui,
                      bg=COLORS["border"], fg=COLORS["fg"],
                      relief="flat", font=(FONT_FAMILY, 11)).pack(fill="x", pady=2)

    def _add_label(self, parent, text):
        if CTK:
            ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=12, weight="bold"),
                         text_color=COLORS["fg2"]).pack(padx=15, pady=(10, 3), anchor="w")
        else:
            tk.Label(parent, text=text, font=(FONT_FAMILY, 11, "bold"),
                     fg=COLORS["fg2"], bg=COLORS["bg2"]).pack(padx=15, pady=(10, 3), anchor="w")

    def _sep(self, parent):
        tk.Frame(parent, height=1, bg=COLORS["border"]).pack(fill="x", padx=10, pady=10)

    def _on_provider_change(self, selected_name=None):
        name = self._provider_var.get()
        prov = next((p for p in PROVIDERS if p["name"] == name), PROVIDERS[0])
        models = prov["models"]
        self._model_var.set(models[0])
        if CTK:
            self._model_menu.configure(values=models)
        else:
            menu = self._model_menu["menu"]
            menu.delete(0, "end")
            for m in models:
                menu.add_command(label=m, command=lambda v=m: self._model_var.set(v))

    # ==================================================================
    # Chat area
    # ==================================================================
    def _create_chat_area(self):
        chat_frame = tk.Frame(self._root, bg=COLORS["bg"])
        chat_frame.pack(side="top", fill="both", expand=True)

        self._chat_text = tk.Text(
            chat_frame,
            wrap="word",
            bg=COLORS["bg"],
            fg=COLORS["fg"],
            font=(FONT_FAMILY, 13),
            insertbackground=COLORS["fg"],
            selectbackground=COLORS["accent2"],
            relief="flat",
            padx=24,
            pady=16,
            cursor="arrow",
            state="disabled",
            spacing1=2,
            spacing3=2,
        )
        scrollbar = tk.Scrollbar(chat_frame, command=self._chat_text.yview,
                                  bg=COLORS["bg"], troughcolor=COLORS["bg3"])
        self._chat_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._chat_text.pack(side="left", fill="both", expand=True)

        # Text tags for styling
        self._chat_text.tag_configure("user_name",
            font=(FONT_FAMILY, 13, "bold"), foreground="#7eb8da",
            spacing1=12)
        self._chat_text.tag_configure("ai_name",
            font=(FONT_FAMILY, 13, "bold"), foreground=COLORS["accent"],
            spacing1=12)
        self._chat_text.tag_configure("user_text",
            font=(FONT_FAMILY, 13), foreground=COLORS["fg"],
            lmargin1=0, lmargin2=0)
        self._chat_text.tag_configure("ai_text",
            font=(FONT_FAMILY, 13), foreground=COLORS["fg"],
            lmargin1=0, lmargin2=0)
        self._chat_text.tag_configure("code",
            font=(MONO_FAMILY, 12), foreground="#c9d1d9",
            background=COLORS["code_bg"],
            lmargin1=16, lmargin2=16, rmargin=16,
            spacing1=4, spacing3=4)
        self._chat_text.tag_configure("error_text",
            font=(FONT_FAMILY, 13), foreground=COLORS["error"])
        self._chat_text.tag_configure("system",
            font=(FONT_FAMILY, 12), foreground=COLORS["fg2"],
            justify="center", spacing1=8, spacing3=8)
        self._chat_text.tag_configure("separator",
            font=(FONT_FAMILY, 1), foreground=COLORS["border"])

    # ==================================================================
    # Input area
    # ==================================================================
    def _create_input_area(self):
        input_frame = tk.Frame(self._root, bg=COLORS["bg3"], height=80)
        input_frame.pack(side="bottom", fill="x")
        input_frame.pack_propagate(False)

        inner = tk.Frame(input_frame, bg=COLORS["bg3"])
        inner.pack(fill="both", expand=True, padx=20, pady=12)

        self._input_text = tk.Text(
            inner,
            height=3,
            wrap="word",
            bg=COLORS["input_bg"],
            fg=COLORS["fg"],
            insertbackground=COLORS["fg"],
            selectbackground=COLORS["accent2"],
            font=(FONT_FAMILY, 13),
            relief="flat",
            padx=12,
            pady=8,
        )
        self._input_text.pack(side="left", fill="both", expand=True)

        # Placeholder
        self._placeholder_on = True
        self._input_text.insert("1.0", "Type a message...")
        self._input_text.config(fg=COLORS["fg2"])
        self._input_text.bind("<FocusIn>", self._on_focus_in)
        self._input_text.bind("<FocusOut>", self._on_focus_out)

        # Send button
        if CTK:
            self._send_btn = ctk.CTkButton(
                inner, text="Send ▶", width=80, height=36,
                command=self._send_message,
                fg_color=COLORS["accent"], hover_color="#3ba885",
                text_color="#111", font=ctk.CTkFont(size=13, weight="bold"))
        else:
            self._send_btn = tk.Button(
                inner, text="Send ▶", width=8,
                command=self._send_message,
                bg=COLORS["accent"], fg="#111",
                activebackground="#3ba885",
                relief="flat", font=(FONT_FAMILY, 12, "bold"))
        self._send_btn.pack(side="right", padx=(10, 0))

        # Enter to send, Shift+Enter for newline
        self._input_text.bind("<Return>", self._on_enter)
        self._input_text.bind("<Shift-Return>", lambda e: None)  # allow newline

    def _on_focus_in(self, event=None):
        if self._placeholder_on:
            self._input_text.delete("1.0", "end")
            self._input_text.config(fg=COLORS["fg"])
            self._placeholder_on = False

    def _on_focus_out(self, event=None):
        content = self._input_text.get("1.0", "end").strip()
        if not content:
            self._input_text.insert("1.0", "Type a message...")
            self._input_text.config(fg=COLORS["fg2"])
            self._placeholder_on = True

    def _on_enter(self, event):
        # Prevent newline, trigger send instead
        self._send_message()
        return "break"

    def _clear_input(self):
        self._input_text.delete("1.0", "end")

    # ==================================================================
    # Send message
    # ==================================================================
    def _send_message(self, event=None):
        if self._placeholder_on:
            return
        text = self._input_text.get("1.0", "end").strip()
        if not text:
            return
        self._clear_input()
        self._on_focus_in()  # clear placeholder state

        # Get provider/model from sidebar
        prov_name = self._provider_var.get()
        prov = next((p for p in PROVIDERS if p["name"] == prov_name), PROVIDERS[0])
        provider_key = prov["key"]
        model = self._model_var.get()

        # Display user message
        self._display_message("user", text)

        # Disable send, update status
        self._send_btn_disable()
        self._update_status("Generating...", online=True)

        # Start inference
        self.backend.send_message(text, provider_key, model)

    def _send_btn_disable(self):
        if CTK:
            self._send_btn.configure(state="disabled")
        else:
            self._send_btn.config(state="disabled")

    def _send_btn_enable(self):
        if CTK:
            self._send_btn.configure(state="normal")
        else:
            self._send_btn.config(state="normal")

    # ==================================================================
    # Message display
    # ==================================================================
    def _display_message(self, role: str, content: str):
        self._chat_text.configure(state="normal")

        # Separator between messages
        if self._has_messages:
            self._chat_text.insert("end", "\n")

        if role == "user":
            self._chat_text.insert("end", "You\n", "user_name")
            self._chat_text.insert("end", content + "\n", "user_text")
        elif role == "assistant":
            self._chat_text.insert("end", "carry-ai\n", "ai_name")
            self._render_content(content)
            self._chat_text.insert("end", "\n")
        elif role == "error":
            self._chat_text.insert("end", "Error: ", "ai_name")
            self._chat_text.insert("end", content + "\n", "error_text")
        elif role == "system":
            self._chat_text.insert("end", content + "\n", "system")

        self._has_messages = True
        self._chat_text.configure(state="disabled")
        self._chat_text.see("end")

    def _render_content(self, text: str):
        """Render text with basic code block detection."""
        parts = text.split("```")
        for i, part in enumerate(parts):
            if i % 2 == 0:
                # Normal text
                if part.strip():
                    self._chat_text.insert("end", part, "ai_text")
            else:
                # Code block — strip language tag from first line
                lines = part.split("\n", 1)
                code = lines[1] if len(lines) > 1 else lines[0]
                self._chat_text.insert("end", "\n" + code.rstrip() + "\n", "code")

    # ==================================================================
    # Response polling
    # ==================================================================
    def _poll_response_queue(self):
        try:
            while True:
                msg_type, data = self.backend.response_queue.get_nowait()
                if msg_type == "chunk":
                    if not self._streaming:
                        # First chunk — insert AI header
                        self._chat_text.configure(state="normal")
                        if self._has_messages:
                            self._chat_text.insert("end", "\n")
                        self._chat_text.insert("end", "carry-ai\n", "ai_name")
                        self._streaming = True
                        self._has_messages = True
                    self._chat_text.configure(state="normal")
                    self._chat_text.insert("end", data, "ai_text")
                    self._chat_text.configure(state="disabled")
                    self._chat_text.see("end")
                elif msg_type == "done":
                    self._chat_text.configure(state="normal")
                    self._chat_text.insert("end", "\n")
                    self._chat_text.configure(state="disabled")
                    self._streaming = False
                    self._update_status("Ready", online=True)
                    self._send_btn_enable()
                elif msg_type == "error":
                    if self._streaming:
                        self._chat_text.configure(state="normal")
                        self._chat_text.insert("end", "\n")
                        self._chat_text.configure(state="disabled")
                        self._streaming = False
                    self._display_message("error", data)
                    self._update_status("Error", online=False)
                    self._send_btn_enable()
        except queue.Empty:
            pass
        self._root.after(30, self._poll_response_queue)

    # ==================================================================
    # Helpers
    # ==================================================================
    def _update_status(self, text: str, online: bool = True):
        color = COLORS["accent"] if online else COLORS["error"]
        self._status_dot.delete("all")
        self._status_dot.create_oval(1, 1, 9, 9, fill=color, outline="")
        self._status_label.config(text=text)

    def _clear_chat(self):
        self._chat_text.configure(state="normal")
        self._chat_text.delete("1.0", "end")
        self._chat_text.configure(state="disabled")
        self._has_messages = False
        self._streaming = False
        self.backend.clear()
        self._add_welcome()
        self._update_status("Ready", online=True)

    def _open_web_ui(self):
        webbrowser.open("http://localhost:8080")

    def _add_welcome(self):
        welcome = (
            "Welcome to carry-ai!\n\n"
            "Select a provider and model from the sidebar, then start chatting.\n\n"
            "Quick tips:\n"
            "  •  Enter to send, Shift+Enter for a new line\n"
            "  •  Web UI button opens the full experience at localhost:8080\n"
            "  •  Run  python onboard.py  for first-time key/model setup\n"
        )
        self._display_message("system", welcome)

    def run(self):
        self._root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = CarryAIApp()
    app.run()


if __name__ == "__main__":
    main()
