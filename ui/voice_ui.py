"""
carry-ai/ui/voice_ui.py — Voice in the desktop app
===================================================

VoiceController   — mic button (click to talk, click again to send) and
                    "speak replies"; recording, transcription and speech run
                    on worker threads, results come back via ``after``.
VoiceSettingsDialog — per-direction backend (Auto / Offline / Cloud / Off),
                    offline model downloads with progress, cloud keys
                    (AssemblyAI / ElevenLabs, kept with the other API keys),
                    and a "Test voice" button.

The pipeline itself lives in integrations/voice_tools.py; models in
models/voice.py. Everything degrades: no PyAudio → the mic explains what
is missing instead of failing.
"""

import logging
import re
import threading
import tkinter as tk
from tkinter import messagebox

from integrations import voice_tools as vt
from models import voice as vm

log = logging.getLogger("carry-ai.ui.voice")

FONT = ("Segoe UI", 10)

BACKENDS = [("auto", "Auto"), ("offline", "Offline (on this PC)"),
            ("cloud", "Cloud"), ("off", "Off")]


def voice_settings() -> dict:
    try:
        from config.settings import load_settings
        return load_settings().to_dict().get("voice", {}) or {}
    except Exception:
        return {}


def speakable(text: str) -> str:
    """Strip what shouldn't be read aloud: code blocks, markdown marks, URLs."""
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*_#>|]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


class VoiceController:
    """Owns the recorder and the STT/TTS backends for one app window."""

    def __init__(self, root, get_keys, on_transcript, on_status):
        """
        Args:
            root: Tk root (for ``after``).
            get_keys: fn() -> the session's API-key dict.
            on_transcript: fn(text) on the Tk thread when speech was recognised.
            on_status: fn(text, ok) for the status line.
        """
        self._root = root
        self._get_keys = get_keys
        self._on_transcript = on_transcript
        self._on_status = on_status
        self._recorder = None
        self._active = False      # between the two mic clicks
        self.reload()

    def reload(self):
        """Re-read settings and keys (after the Voice settings dialog)."""
        self.config = vt.load_voice_config(self._get_keys())
        prefs = voice_settings()
        self.speak_replies = bool(prefs.get("speak_replies", False))
        self.auto_send = bool(prefs.get("auto_send", True))
        self._stt = vt.make_transcriber(self.config)
        self._tts = vt.make_tts(self.config)
        self._recorder = vt.AudioRecorder(self.config)

    @property
    def recording(self) -> bool:
        return self._active

    def toggle_recording(self) -> bool:
        """Start or stop push-to-talk. Returns True while recording.

        The second click always transcribes what was captured, even if the
        recorder already stopped at max_recording_seconds.
        """
        if self._active:
            self._active = False
            self._finish()
            return False
        problem = vt.backend_problem("stt", self.config)
        if problem:
            self._on_status(problem, False)
            return False
        vt.stop_speaking()
        try:
            self._recorder.start()
        except (ImportError, RuntimeError) as e:
            self._on_status(str(e), False)
            return False
        self._active = True
        self._on_status("Listening… click ■ when done", True)
        return True

    def _finish(self):
        pcm = self._recorder.stop()
        rate = self._recorder.sample_rate
        self._on_status("Transcribing…", True)
        stt = self._stt

        def work():
            try:
                text = vt.transcribe_pcm(stt, pcm, rate)
                ok = bool(text) and not text.startswith("[")
                self._root.after(0, lambda: self._done(text, ok))
            except Exception as e:
                log.error("Transcription failed: %s", e)
                self._root.after(0, lambda: self._on_status(f"Transcription failed: {e}", False))
        threading.Thread(target=work, daemon=True).start()

    def _done(self, text: str, ok: bool):
        if ok:
            self._on_status("Ready", True)
            self._on_transcript(text)
        else:
            self._on_status(text or "Didn't catch that — try again.", False)

    def speak(self, text: str):
        """Read *text* aloud in the background (if a TTS backend is set)."""
        tts = self._tts
        said = speakable(text)
        if tts is None or not said:
            return
        threading.Thread(target=tts.speak, args=(said,), daemon=True).start()

    def maybe_speak_reply(self, text: str):
        if self.speak_replies:
            self.speak(text)


class VoiceSettingsDialog:
    """Voice settings: backends, offline models, cloud keys."""

    def __init__(self, app):
        self._app = app
        C = self.C = app.C
        prefs = voice_settings()
        self._win = tk.Toplevel(app._root)
        self._win.title("Voice")
        self._win.configure(bg=C["bg"])
        self._win.transient(app._root)
        self._progress: dict[str, tk.Label] = {}
        self._buttons: dict[str, tk.Button] = {}

        tk.Label(self._win, text="Voice", font=(FONT[0], 15, "bold"), fg=C["fg"],
                 bg=C["bg"]).pack(padx=20, pady=(16, 2), anchor="w")
        tk.Label(self._win, text="Offline voice runs on this PC from the USB — nothing is "
                                 "sent anywhere. Cloud voice can sound better but needs a key "
                                 "and internet.", font=FONT, fg=C["fg2"], bg=C["bg"],
                 wraplength=560, justify="left").pack(padx=20, anchor="w")

        self._stt_var = tk.StringVar(value=prefs.get("stt_backend", "auto"))
        self._tts_var = tk.StringVar(value=prefs.get("tts_backend", "auto"))
        self._stt_model = tk.StringVar(value=prefs.get("stt_model", vm.DEFAULT_STT))
        self._tts_model = tk.StringVar(value=prefs.get("tts_model", vm.DEFAULT_TTS))
        self._speak_var = tk.BooleanVar(value=bool(prefs.get("speak_replies", False)))
        self._send_var = tk.BooleanVar(value=bool(prefs.get("auto_send", True)))

        self._section("Speech to text (your voice → text)", "stt", self._stt_var,
                      self._stt_model, "assemblyai", "AssemblyAI key")
        self._section("Text to speech (replies read aloud)", "tts", self._tts_var,
                      self._tts_model, "elevenlabs", "ElevenLabs key")

        opts = tk.Frame(self._win, bg=C["bg"])
        opts.pack(fill="x", padx=20, pady=(8, 0))
        for text, var in (("Read replies aloud", self._speak_var),
                          ("Send what I said right away", self._send_var)):
            tk.Checkbutton(opts, text=text, variable=var, bg=C["bg"], fg=C["fg"],
                           selectcolor=C["bg3"], activebackground=C["bg"],
                           activeforeground=C["fg"], font=FONT).pack(anchor="w")

        self._status = tk.Label(self._win, text="", font=FONT, fg=C["fg2"], bg=C["bg"],
                                wraplength=560, justify="left")
        self._status.pack(padx=20, pady=(8, 0), anchor="w")

        bar = tk.Frame(self._win, bg=C["bg2"])
        bar.pack(fill="x", side="bottom", pady=(12, 0))
        for text, cmd, color in (("Close", self._win.destroy, C["bg3"]),
                                 ("Save", self._save, C["accent"]),
                                 ("Test voice", self._test, C["accent2"])):
            tk.Button(bar, text=text, command=cmd, bg=color,
                      fg=C["fg"] if color == C["bg3"] else "#111",
                      font=(FONT[0], 10, "bold"), relief="flat", padx=12,
                      cursor="hand2").pack(side="right", padx=6, pady=8)
        self._refresh_status()

    # -- layout --------------------------------------------------------

    def _section(self, title, kind, backend_var, model_var, key_name, key_label):
        C = self.C
        box = tk.Frame(self._win, bg=C["bg2"])
        box.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(box, text=title, font=(FONT[0], 11, "bold"), fg=C["accent"],
                 bg=C["bg2"]).pack(padx=10, pady=(8, 2), anchor="w")

        row = tk.Frame(box, bg=C["bg2"])
        row.pack(fill="x", padx=10)
        for value, label in BACKENDS:
            tk.Radiobutton(row, text=label, value=value, variable=backend_var,
                           command=self._refresh_status, bg=C["bg2"], fg=C["fg"],
                           selectcolor=C["bg3"], activebackground=C["bg2"],
                           activeforeground=C["fg"], font=FONT).pack(side="left", padx=(0, 8))

        tk.Label(box, text="Offline model:", font=FONT, fg=C["fg2"],
                 bg=C["bg2"]).pack(padx=10, pady=(6, 0), anchor="w")
        for m in vm.models_of(kind):
            line = tk.Frame(box, bg=C["bg2"])
            line.pack(fill="x", padx=18)
            tk.Radiobutton(line, text=f"{m.label} — {m.size_mb} MB", value=m.id,
                           variable=model_var, command=self._refresh_status, bg=C["bg2"],
                           fg=C["fg"], selectcolor=C["bg3"], activebackground=C["bg2"],
                           activeforeground=C["fg"], font=FONT).pack(side="left")
            btn = tk.Button(line, font=(FONT[0], 9), relief="flat", bg=C["bg3"], fg=C["fg"],
                            padx=6, command=lambda m=m: self._download(m))
            btn.pack(side="left", padx=6)
            prog = tk.Label(line, text="", font=(FONT[0], 9), fg=C["fg3"], bg=C["bg2"])
            prog.pack(side="left")
            self._buttons[m.id], self._progress[m.id] = btn, prog
            self._update_model_row(m.id)

        krow = tk.Frame(box, bg=C["bg2"])
        krow.pack(fill="x", padx=10, pady=(6, 8))
        tk.Label(krow, text=f"Cloud: {key_label}", font=FONT, fg=C["fg2"],
                 bg=C["bg2"]).pack(side="left")
        entry = tk.Entry(krow, show="•", width=34, bg=C["input_bg"], fg=C["fg"],
                         insertbackground=C["fg"], relief="flat")
        entry.insert(0, self._key(key_name))
        entry.pack(side="left", padx=8, ipady=2)
        setattr(self, f"_{key_name}_entry", entry)

    def _key(self, name: str) -> str:
        entry = self._app.backend.preloaded_keys.get(name)
        return (entry.get("api_key", "") if isinstance(entry, dict) else entry) or ""

    def _update_model_row(self, model_id: str):
        installed = vm.is_installed(model_id)
        self._buttons[model_id].config(text="Remove" if installed else "Download",
                                       state="normal")
        self._progress[model_id].config(text="✓ on USB" if installed else "")

    # -- actions -------------------------------------------------------

    def _download(self, m):
        if vm.is_installed(m.id):
            if messagebox.askyesno("Remove model", f"Delete {m.label} from the USB?",
                                   parent=self._win):
                vm.remove(m.id)
                self._update_model_row(m.id)
                self._refresh_status()
            return
        self._buttons[m.id].config(state="disabled")
        state = {"pct": 0, "error": None, "done": False}

        def progress(done, total, _path):
            state["pct"] = done * 100 // total if total else 100

        def work():
            try:
                vm.download(m.id, progress=progress)
            except Exception as e:
                state["error"] = str(e)
            state["done"] = True
        threading.Thread(target=work, daemon=True).start()

        def poll():
            if not self._win.winfo_exists():
                return
            if state["done"]:
                if state["error"]:
                    self._progress[m.id].config(text=f"✗ {state['error'][:60]}")
                    self._buttons[m.id].config(state="normal")
                else:
                    self._update_model_row(m.id)
                self._refresh_status()
                return
            self._progress[m.id].config(text=f"{state['pct']}%")
            self._win.after(300, poll)
        poll()

    def _config(self) -> vt.VoiceConfig:
        cfg = vt.VoiceConfig(stt_backend=self._stt_var.get(), tts_backend=self._tts_var.get(),
                             stt_model=self._stt_model.get(), tts_model=self._tts_model.get())
        cfg.assemblyai_key = self._assemblyai_entry.get().strip()
        cfg.elevenlabs_key = self._elevenlabs_entry.get().strip()
        return cfg

    def _refresh_status(self):
        cfg = self._config()
        lines = []
        for kind, name in (("stt", "Speech to text"), ("tts", "Text to speech")):
            backend = vt.resolve_backend(kind, cfg)
            problem = vt.backend_problem(kind, cfg)
            lines.append(f"{name}: {backend}" + (f" — {problem}" if problem else " ✓"))
        if not vt.is_voice_available()["recorder"]:
            lines.append("Microphone: needs PyAudio (Windows) or sherpa-onnx (Linux).")
        self._status.config(text="\n".join(lines))

    def _apply_keys(self) -> dict:
        """Put the cloud keys into the session's key dict; return what changed."""
        keys = self._app.backend.preloaded_keys
        changes = {}
        for name in ("assemblyai", "elevenlabs"):
            new = getattr(self, f"_{name}_entry").get().strip()
            if new == self._key(name):
                continue
            changes[name] = new
            if new:
                keys[name] = {"api_key": new}
            else:
                keys.pop(name, None)
        return changes

    def _save(self):
        from config.settings import save_user_settings
        save_user_settings({"voice": {
            "stt_backend": self._stt_var.get(), "tts_backend": self._tts_var.get(),
            "stt_model": self._stt_model.get(), "tts_model": self._tts_model.get(),
            "speak_replies": self._speak_var.get(), "auto_send": self._send_var.get(),
        }})
        changes = self._apply_keys()
        if changes and messagebox.askyesno(
                "Remember keys?", "Save the voice keys in the encrypted keystore on the USB?",
                parent=self._win):
            from ui.desktop import save_keys_encrypted
            save_keys_encrypted(self._win, changes)
        self._app.on_voice_settings_changed()
        self._win.destroy()

    def _test(self):
        cfg = self._config()
        problem = vt.backend_problem("tts", cfg)
        if problem:
            self._status.config(text=problem)
            return
        tts = vt.make_tts(cfg)
        self._status.config(text="Speaking…")
        threading.Thread(target=tts.speak, daemon=True,
                         args=("Hi! This is how carry-ai sounds.",)).start()
