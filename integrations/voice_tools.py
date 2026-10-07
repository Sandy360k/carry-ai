"""
carry-ai voice pipeline — inspired by farzaa/clicky.

Push-to-talk recording, speech-to-text, optional screenshot for vision,
agent reply, text-to-speech. Speech runs either offline (sherpa-onnx on
the USB, nothing leaves the PC) or in the cloud (AssemblyAI / ElevenLabs),
chosen per direction in settings:

    voice.stt_backend / voice.tts_backend = "auto" | "offline" | "cloud" | "off"

"auto" uses the offline model when sherpa-onnx and the model are on the
USB, else the cloud service when its key is set, else nothing.

Architecture
------------
AudioRecorder      — captures PCM audio from the default mic via PyAudio
                     (start()/stop() for a GUI button, or Enter in a terminal).
Transcriber        — cloud STT: uploads audio to AssemblyAI and polls.
OfflineTranscriber — offline STT: sherpa-onnx Moonshine (models/voice.py).
ScreenCapture      — grabs a screenshot and returns it as a base64 PNG string.
TTSPlayer          — cloud TTS: ElevenLabs, played via PyAudio.
OfflineTTS         — offline TTS: sherpa-onnx Kitten / Kokoro.
make_transcriber / make_tts — pick the backend from VoiceConfig.
VoicePipeline      — orchestrates a full turn: record → transcribe →
                     screenshot → agent → speak.

All third-party imports are guarded by try/except so the module loads
even when the optional packages are absent.
"""

from __future__ import annotations

import base64
import contextlib
import io
import logging
import os
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field

log = logging.getLogger("carry-ai.voice")

# ---------------------------------------------------------------------------
# Optional imports — each failure degrades one feature but nothing crashes.
# ---------------------------------------------------------------------------

try:
    import pyaudio as _pyaudio  # type: ignore[import]
    PyAudio = _pyaudio.PyAudio
    paInt16 = _pyaudio.paInt16
except ImportError:
    _pyaudio = None  # type: ignore[assignment]
    PyAudio = None   # type: ignore[assignment]
    paInt16 = None   # type: ignore[assignment]

try:
    from elevenlabs import ElevenLabs, VoiceSettings  # type: ignore[import]
    _elevenlabs_sdk = True
except ImportError:
    ElevenLabs = None       # type: ignore[assignment]
    VoiceSettings = None    # type: ignore[assignment]
    _elevenlabs_sdk = False

try:
    from PIL import ImageGrab as _ImageGrab  # type: ignore[import]
    _pil_available = True
except ImportError:
    _ImageGrab = None   # type: ignore[assignment]
    _pil_available = False

try:
    import pyautogui as _pyautogui  # type: ignore[import]
    _pyautogui_available = True
except ImportError:
    _pyautogui = None           # type: ignore[assignment]
    _pyautogui_available = False

try:
    import requests as _requests  # type: ignore[import]
except ImportError:
    _requests = None  # type: ignore[assignment]

try:
    import sherpa_onnx as _sherpa  # type: ignore[import]
except ImportError:
    _sherpa = None  # type: ignore[assignment]

from models.voice import DEFAULT_STT, DEFAULT_TTS, get_model, is_installed, model_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 16000
CHANNELS: int = 1
CHUNK_SIZE: int = 1024
ASSEMBLYAI_API_URL: str = "https://api.assemblyai.com/v2"
# eleven_monolingual_v1 was retired 2026-07-09; Flash v2.5 is the low-latency
# successor. pcm_16000 = raw 16-bit mono PCM at SAMPLE_RATE, which is what
# _play_pcm_stream() writes straight to PyAudio (the API default is MP3).
ELEVENLABS_MODEL_ID: str = "eleven_flash_v2_5"
ELEVENLABS_OUTPUT_FORMAT: str = "pcm_16000"

# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass
class VoiceConfig:
    """Runtime configuration for the voice pipeline."""

    assemblyai_key: str = ""
    elevenlabs_key: str = ""
    # Rachel — a natural-sounding English voice included in all ElevenLabs plans
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    tts_enabled: bool = True
    vision_enabled: bool = True
    max_recording_seconds: int = 30
    # "auto" | "offline" | "cloud" | "off" (see module docstring)
    stt_backend: str = "auto"
    tts_backend: str = "auto"
    stt_model: str = DEFAULT_STT          # models/voice.py ids
    tts_model: str = DEFAULT_TTS
    tts_speaker: int = 0                  # voice index for multi-speaker models
    tts_speed: float = 1.0
    num_threads: int = 2


# ---------------------------------------------------------------------------
# AudioRecorder
# ---------------------------------------------------------------------------


class AudioRecorder:
    """Captures raw PCM audio from the default microphone via PyAudio."""

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self._frames: list[bytes] = []
        self._recording = False

    def start(self) -> None:
        """Start recording on a background thread (stop() returns the audio).

        PyAudio when installed (bundled on Windows); on Linux, where PyAudio
        has no wheel, sherpa-onnx's own ALSA reader (no system package needed).
        """
        if _pyaudio is None:
            if _alsa_available():
                return self._start_alsa()
            raise ImportError(
                "Voice recording needs PyAudio (pip install pyaudio) or, on Linux, "
                "the sherpa-onnx package."
            )
        self._frames = []
        self._pa = PyAudio()
        try:
            self._stream = self._pa.open(
                format=paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
            )
        except Exception as exc:
            self._pa.terminate()
            raise RuntimeError(f"Could not open audio input: {exc}") from exc

        self._recording = True
        max_chunks = int(SAMPLE_RATE / CHUNK_SIZE * self.config.max_recording_seconds)

        def _capture() -> None:
            count = 0
            while self._recording and count < max_chunks:
                try:
                    self._frames.append(
                        self._stream.read(CHUNK_SIZE, exception_on_overflow=False))
                except Exception as exc:
                    log.warning("Audio read error: %s", exc)
                    break
                count += 1
            self._recording = False

        self._thread = threading.Thread(target=_capture, daemon=True)
        self._thread.start()

    def _start_alsa(self) -> None:
        self._frames = []
        try:
            mic = _sherpa.Alsa("default")
        except Exception as exc:
            raise RuntimeError(f"Could not open the microphone (ALSA): {exc}") from exc
        rate = mic.actual_sample_rate or SAMPLE_RATE
        self._alsa_rate = rate
        self._recording = True
        block = rate // 10                     # 100 ms reads
        max_blocks = 10 * self.config.max_recording_seconds

        def _capture() -> None:
            count = 0
            while self._recording and count < max_blocks:
                try:
                    self._frames.append(float_to_pcm16(mic.read(block)))
                except Exception as exc:
                    log.warning("ALSA read error: %s", exc)
                    break
                count += 1
            self._recording = False

        self._thread = threading.Thread(target=_capture, daemon=True)
        self._thread.start()

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def sample_rate(self) -> int:
        """Rate of the PCM stop() returns (16 kHz unless ALSA forced another)."""
        return getattr(self, "_alsa_rate", SAMPLE_RATE)

    def stop(self) -> bytes:
        """Stop recording; returns 16 kHz 16-bit mono PCM."""
        self._recording = False
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.join(timeout=2)
        with contextlib.suppress(Exception):
            self._stream.stop_stream()
            self._stream.close()
        with contextlib.suppress(Exception):
            self._pa.terminate()
        raw_pcm = b"".join(self._frames)
        log.debug("Recorded %d bytes of PCM audio.", len(raw_pcm))
        return raw_pcm

    def record_until_keypress(self) -> bytes:
        """Record until the user presses Enter (terminal push-to-talk)."""
        self.start()
        print("  [Recording…] Press Enter to stop.")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
        return self.stop()

    @staticmethod
    def save_wav(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        """
        Persist raw PCM bytes as a 16-bit mono WAV file.

        Returns the path to the temporary file.
        """
        # The wiped-on-eject session dir (RAM-backed on Linux), not host %TEMP%
        session_dir = os.environ.get("CARRY_AI_SESSION_DIR")
        fd, path = tempfile.mkstemp(suffix=".wav", dir=session_dir if session_dir
                                    and os.path.isdir(session_dir) else None)
        os.close(fd)

        with wave.open(path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # 16-bit = 2 bytes per sample
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)

        log.debug("Saved WAV to %s (%d bytes PCM).", path, len(pcm_bytes))
        return path


# ---------------------------------------------------------------------------
# Transcriber
# ---------------------------------------------------------------------------

_TRANSCRIPT_POLL_INTERVAL: float = 2.0  # seconds between status polls
_TRANSCRIPT_TIMEOUT: float = 120.0      # give up after 2 minutes


class Transcriber:
    """Converts audio files to text via the AssemblyAI REST API."""

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": self.config.assemblyai_key,
            "content-type": "application/json",
        }

    def _upload(self, audio_path: str) -> str:
        """Upload the audio file to AssemblyAI and return the upload URL."""
        upload_url = f"{ASSEMBLYAI_API_URL}/upload"
        with open(audio_path, "rb") as fh:
            response = _requests.post(
                upload_url,
                headers={"authorization": self.config.assemblyai_key},
                data=fh,
                timeout=60,
            )
        response.raise_for_status()
        return response.json()["upload_url"]

    def _submit(self, upload_url: str) -> str:
        """Submit a transcription job and return the transcript ID."""
        response = _requests.post(
            f"{ASSEMBLYAI_API_URL}/transcript",
            json={"audio_url": upload_url},
            headers=self._headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["id"]

    def _poll(self, transcript_id: str) -> str:
        """Poll until the transcript is complete or an error occurs."""
        deadline = time.monotonic() + _TRANSCRIPT_TIMEOUT
        while time.monotonic() < deadline:
            response = _requests.get(
                f"{ASSEMBLYAI_API_URL}/transcript/{transcript_id}",
                headers=self._headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            status = data.get("status")

            if status == "completed":
                return data.get("text", "")
            elif status == "error":
                raise RuntimeError(
                    f"AssemblyAI transcription error: {data.get('error')}"
                )
            # status is 'queued' or 'processing' — keep waiting
            time.sleep(_TRANSCRIPT_POLL_INTERVAL)

        raise TimeoutError("AssemblyAI transcription timed out.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def transcribe(self, audio_path: str) -> str:
        """
        Transcribe *audio_path* using AssemblyAI.

        Talks to the AssemblyAI REST API with ``requests`` (the assemblyai
        SDK is not needed). Falls back to a placeholder string when requests
        or the key is missing, so the rest of the pipeline can continue gracefully.

        Parameters
        ----------
        audio_path:
            Path to a WAV (or other audio) file.

        Returns
        -------
        str
            Transcript text, or a fallback message on failure.
        """
        if not self.config.assemblyai_key:
            log.warning("No AssemblyAI API key configured; transcription unavailable.")
            return "[voice input not available — configure assemblyai_key]"

        if _requests is None:
            log.warning("requests package not installed; transcription unavailable.")
            return "[voice input not available — install requests]"

        try:
            log.debug("Uploading audio to AssemblyAI…")
            upload_url = self._upload(audio_path)

            log.debug("Submitting transcription job…")
            transcript_id = self._submit(upload_url)

            log.debug("Polling for transcript %s…", transcript_id)
            text = self._poll(transcript_id)
            log.info("Transcription complete: %r", text[:80])
            return text

        except _requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code == 429:
                log.error("AssemblyAI rate limit hit.")
                return "[voice input unavailable — rate limit reached]"
            log.error("AssemblyAI HTTP error %s: %s", status_code, exc)
            return "[voice input unavailable — API error]"
        except Exception as exc:
            log.error("Transcription failed: %s", exc)
            return "[voice input unavailable — transcription error]"


# ---------------------------------------------------------------------------
# ScreenCapture
# ---------------------------------------------------------------------------


class ScreenCapture:
    """Takes a screenshot and returns it as a base64-encoded PNG string."""

    def capture(self) -> str | None:
        """
        Capture the full screen.

        Tries PIL.ImageGrab first, then falls back to pyautogui.
        Returns None if neither library is available.

        Returns
        -------
        str | None
            Base64-encoded PNG bytes, or None if capture is impossible.
        """
        img = None

        if _pil_available and _ImageGrab is not None:
            try:
                img = _ImageGrab.grab()
            except Exception as exc:
                log.warning("PIL screenshot failed: %s", exc)

        if img is None and _pyautogui_available and _pyautogui is not None:
            try:
                img = _pyautogui.screenshot()
            except Exception as exc:
                log.warning("pyautogui screenshot failed: %s", exc)

        if img is None:
            log.debug("No screenshot library available; skipping vision.")
            return None

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        log.debug("Screenshot captured (%d bytes base64).", len(b64))
        return b64


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


_stop_playback = threading.Event()


def stop_speaking() -> None:
    """Interrupt whatever is being played (e.g. the user starts talking)."""
    _stop_playback.set()
    proc = _player_proc
    if proc is not None:
        with contextlib.suppress(OSError):
            proc.terminate()
    import sys
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import winsound
            winsound.PlaySound(None, 0)


_player_proc = None   # aplay/paplay while it plays (so stop_speaking can end it)


def _alsa_available() -> bool:
    import sys
    return sys.platform.startswith("linux") and _sherpa is not None \
        and hasattr(_sherpa, "Alsa")


def _pcm_to_wav_bytes(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _play_without_pyaudio(pcm: bytes, rate: int) -> None:
    """Windows: winsound from memory. Linux: aplay/paplay fed on stdin.

    Neither writes the audio to disk.
    """
    global _player_proc
    import shutil
    import subprocess
    import sys
    if sys.platform == "win32":
        import winsound
        winsound.PlaySound(_pcm_to_wav_bytes(pcm, rate), winsound.SND_MEMORY)
        return
    for cmd in (["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(rate)],
                ["paplay", "--raw", "--format=s16le", "--channels=1", f"--rate={rate}"]):
        if shutil.which(cmd[0]):
            _player_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
            try:
                _player_proc.communicate(pcm)
            finally:
                _player_proc = None
            return
    log.warning("No audio output: install PyAudio, or aplay (alsa-utils) on Linux.")


def play_pcm(audio_iter, rate: int = SAMPLE_RATE) -> None:
    """Play 16-bit mono PCM chunks through the default output device."""
    _stop_playback.clear()
    if _pyaudio is None:
        pcm = b"".join(c for c in audio_iter if c)
        if not _stop_playback.is_set():
            _play_without_pyaudio(pcm, rate)
        return
    pa = PyAudio()
    stream = pa.open(format=paInt16, channels=1, rate=rate, output=True)
    try:
        for chunk in audio_iter:
            if _stop_playback.is_set():
                break
            if chunk:
                stream.write(chunk)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


def float_to_pcm16(samples) -> bytes:
    """[-1.0, 1.0] floats (sherpa-onnx output) -> 16-bit little-endian PCM."""
    import array
    clipped = (max(-1.0, min(1.0, float(x))) for x in samples)
    return array.array("h", (int(x * 32767) for x in clipped)).tobytes()


# ---------------------------------------------------------------------------
# TTSPlayer
# ---------------------------------------------------------------------------


class TTSPlayer:
    """Synthesises speech from text using ElevenLabs and plays it via PyAudio."""

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config

    def _play_pcm_stream(self, audio_iter, rate: int = SAMPLE_RATE) -> None:
        """Feed a streaming audio iterator into PyAudio for real-time playback."""
        play_pcm(audio_iter, rate)

    def speak(self, text: str) -> None:
        """
        Convert *text* to speech and play it aloud.

        Uses the ElevenLabs SDK when available; otherwise falls back to a
        direct REST call.  Silently no-ops when keys/packages are missing.

        Parameters
        ----------
        text:
            The string to synthesise and play.
        """
        if not self.config.tts_enabled:
            log.debug("TTS disabled; would have spoken: %r", text[:60])
            return

        if not self.config.elevenlabs_key:
            log.debug("No ElevenLabs key; skipping TTS.  Would say: %r", text[:60])
            return

        # --- ElevenLabs Python SDK path ---
        if _elevenlabs_sdk and ElevenLabs is not None:
            try:
                client = ElevenLabs(api_key=self.config.elevenlabs_key)
                # SDK >= 2.0: client.generate() is gone; stream() yields bytes.
                audio_iter = client.text_to_speech.stream(
                    voice_id=self.config.elevenlabs_voice_id,
                    text=text,
                    model_id=ELEVENLABS_MODEL_ID,
                    output_format=ELEVENLABS_OUTPUT_FORMAT,
                )
                self._play_pcm_stream(audio_iter)
                return
            except Exception as exc:
                log.warning("ElevenLabs SDK error: %s — falling back to REST.", exc)

        # --- Direct REST fallback ---
        if _requests is None:
            log.warning("requests not installed; cannot call ElevenLabs REST API.")
            return

        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/"
            f"{self.config.elevenlabs_voice_id}/stream"
            f"?output_format={ELEVENLABS_OUTPUT_FORMAT}"
        )
        payload = {
            "text": text,
            "model_id": ELEVENLABS_MODEL_ID,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        headers = {
            "xi-api-key": self.config.elevenlabs_key,
            "Content-Type": "application/json",
        }

        try:
            response = _requests.post(
                url, json=payload, headers=headers, stream=True, timeout=30
            )
            response.raise_for_status()
            self._play_pcm_stream(response.iter_content(chunk_size=CHUNK_SIZE))
        except Exception as exc:
            log.error("ElevenLabs REST TTS error: %s", exc)


# ---------------------------------------------------------------------------
# Offline backends (sherpa-onnx, models from models/voice.py)
# ---------------------------------------------------------------------------


class OfflineTranscriber:
    """Speech-to-text on this PC with sherpa-onnx; audio never leaves it."""

    def __init__(self, config: VoiceConfig, models_root=None) -> None:
        self.config = config
        self._root = models_root
        self._recognizer = None
        self._lock = threading.Lock()

    def _load(self):
        if self._recognizer is None:
            model = get_model(self.config.stt_model)
            if model is None or model.family != "moonshine_v2":
                raise ValueError(f"Not an offline STT model: {self.config.stt_model}")
            d = model_dir(model.id, self._root)
            self._recognizer = _sherpa.OfflineRecognizer.from_moonshine_v2(
                encoder=str(d / "encoder_model.ort"),
                decoder=str(d / "decoder_model_merged.ort"),
                tokens=str(d / "tokens.txt"),
                num_threads=self.config.num_threads,
            )
            log.info("Offline STT loaded: %s", model.id)
        return self._recognizer

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        """Transcribe 16-bit mono PCM straight from the recorder (no file)."""
        import array
        if not pcm:
            return ""
        samples = [x / 32768.0 for x in array.array("h", pcm)]
        with self._lock:
            recognizer = self._load()
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def transcribe(self, audio_path: str) -> str:
        """Same interface as the cloud Transcriber (16-bit WAV file)."""
        with wave.open(audio_path, "rb") as wf:
            return self.transcribe_pcm(wf.readframes(wf.getnframes()), wf.getframerate())


class OfflineTTS:
    """Text-to-speech on this PC with sherpa-onnx (Kitten or Kokoro)."""

    def __init__(self, config: VoiceConfig, models_root=None) -> None:
        self.config = config
        self._root = models_root
        self._tts = None
        self._lock = threading.Lock()

    def _load(self):
        if self._tts is None:
            model = get_model(self.config.tts_model)
            if model is None or model.kind != "tts":
                raise ValueError(f"Not an offline TTS model: {self.config.tts_model}")
            d = model_dir(model.id, self._root)
            common = dict(voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
                          data_dir=str(d / "espeak-ng-data"))
            if model.family == "kitten":
                cfg = _sherpa.OfflineTtsModelConfig(
                    kitten=_sherpa.OfflineTtsKittenModelConfig(
                        model=str(d / "model.fp16.onnx"), **common),
                    num_threads=self.config.num_threads)
            elif model.family == "kokoro":
                cfg = _sherpa.OfflineTtsModelConfig(
                    kokoro=_sherpa.OfflineTtsKokoroModelConfig(
                        model=str(d / "model.int8.onnx"), dict_dir=str(d / "dict"),
                        lexicon=f"{d / 'lexicon-us-en.txt'},{d / 'lexicon-zh.txt'}",
                        **common),
                    num_threads=self.config.num_threads)
            else:
                raise ValueError(f"Unknown TTS family: {model.family}")
            self._tts = _sherpa.OfflineTts(_sherpa.OfflineTtsConfig(model=cfg))
            log.info("Offline TTS loaded: %s (%d voices)", model.id, self._tts.num_speakers)
        return self._tts

    def synthesize(self, text: str) -> tuple[bytes, int]:
        """Text -> (16-bit mono PCM, sample rate)."""
        with self._lock:
            audio = self._load().generate(text, sid=self.config.tts_speaker,
                                          speed=self.config.tts_speed)
        return float_to_pcm16(audio.samples), audio.sample_rate

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        try:
            pcm, rate = self.synthesize(text)
        except Exception as exc:
            log.error("Offline TTS failed: %s", exc)
            return
        play_pcm((pcm[i:i + 8192] for i in range(0, len(pcm), 8192)), rate)


def _offline_ready(kind: str, config: VoiceConfig, models_root=None) -> bool:
    model_id = config.stt_model if kind == "stt" else config.tts_model
    return _sherpa is not None and is_installed(model_id, models_root)


def resolve_backend(kind: str, config: VoiceConfig, models_root=None) -> str:
    """Which backend a direction ("stt" / "tts") will use: offline|cloud|off.

    "auto" prefers offline (private, works without internet), then cloud
    if its key is set. An explicit choice is kept even if it isn't ready,
    so the UI can say what is missing.
    """
    choice = (config.stt_backend if kind == "stt" else config.tts_backend) or "auto"
    if kind == "tts" and not config.tts_enabled:
        return "off"
    if choice != "auto":
        return choice
    if _offline_ready(kind, config, models_root):
        return "offline"
    key = config.assemblyai_key if kind == "stt" else config.elevenlabs_key
    return "cloud" if key else "off"


def backend_problem(kind: str, config: VoiceConfig, models_root=None) -> str:
    """Why the chosen backend can't run ('' if it can)."""
    backend = resolve_backend(kind, config, models_root)
    if backend == "offline":
        if _sherpa is None:
            return "Offline voice needs the sherpa-onnx package (pip install sherpa-onnx)."
        model_id = config.stt_model if kind == "stt" else config.tts_model
        if not is_installed(model_id, models_root):
            return f"Offline model '{model_id}' isn't downloaded yet."
    elif backend == "cloud":
        if kind == "stt" and not config.assemblyai_key:
            return "Cloud speech-to-text needs an AssemblyAI key."
        if kind == "tts" and not config.elevenlabs_key:
            return "Cloud text-to-speech needs an ElevenLabs key."
    elif backend == "off" and kind == "stt":
        return "No speech-to-text: download an offline model or add an AssemblyAI key."
    return ""


def make_transcriber(config: VoiceConfig, models_root=None):
    """Transcriber for the configured backend (None when off)."""
    backend = resolve_backend("stt", config, models_root)
    if backend == "offline":
        return OfflineTranscriber(config, models_root)
    if backend == "cloud":
        return Transcriber(config)
    return None


def make_tts(config: VoiceConfig, models_root=None):
    """TTS player for the configured backend (None when off)."""
    backend = resolve_backend("tts", config, models_root)
    if backend == "offline":
        return OfflineTTS(config, models_root)
    if backend == "cloud":
        return TTSPlayer(config)
    return None


def transcribe_pcm(transcriber, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
    """Recorded PCM -> text with either backend ('' if there is none).

    Offline STT reads the audio from memory; only the cloud backend needs
    a WAV file (in the session dir, deleted straight after the upload).
    """
    if transcriber is None:
        return ""
    if hasattr(transcriber, "transcribe_pcm"):
        return transcriber.transcribe_pcm(pcm, sample_rate)
    wav_path = AudioRecorder.save_wav(pcm, sample_rate)
    try:
        return transcriber.transcribe(wav_path)
    finally:
        with contextlib.suppress(OSError):
            os.remove(wav_path)


def load_voice_config(api_keys: dict | None = None) -> VoiceConfig:
    """VoiceConfig from settings ``voice.*`` plus keys from the keystore.

    Cloud keys live in the encrypted keystore (entries "assemblyai" and
    "elevenlabs"), never in settings.json.
    """
    cfg = VoiceConfig()
    try:
        from config.settings import load_settings
        voice = load_settings().to_dict().get("voice", {}) or {}
    except Exception as exc:
        log.debug("Cannot read voice settings: %s", exc)
        voice = {}
    for name in ("stt_backend", "tts_backend", "stt_model", "tts_model", "tts_speaker",
                 "tts_speed", "tts_enabled", "vision_enabled", "max_recording_seconds",
                 "elevenlabs_voice_id", "num_threads"):
        if name in voice:
            setattr(cfg, name, voice[name])

    def _key(provider: str) -> str:
        entry = (api_keys or {}).get(provider)
        return (entry.get("api_key", "") if isinstance(entry, dict) else entry) or ""
    cfg.assemblyai_key = _key("assemblyai")
    cfg.elevenlabs_key = _key("elevenlabs")
    return cfg


# ---------------------------------------------------------------------------
# VoicePipeline
# ---------------------------------------------------------------------------


class VoicePipeline:
    """
    Orchestrates a full voice interaction turn.

    Parameters
    ----------
    config:
        VoiceConfig instance with keys and feature flags.
    agent_fn:
        Callable with signature ``(text: str, screenshot_b64: str | None) -> str``.
        Receives the transcribed user query and an optional screenshot, and
        returns the agent's text response.
    """

    def __init__(self, config: VoiceConfig, agent_fn: callable) -> None:  # type: ignore[type-arg]
        self.config = config
        self.agent_fn = agent_fn
        self._recorder = AudioRecorder(config)
        self._transcriber = make_transcriber(config)
        self._capture = ScreenCapture()
        self._tts = make_tts(config)

    def transcribe(self, pcm: bytes) -> str:
        """Recorded PCM -> text with the configured STT backend."""
        return transcribe_pcm(self._transcriber, pcm, self._recorder.sample_rate)

    def speak(self, text: str) -> None:
        if self._tts is not None:
            self._tts.speak(text)

    def run_once(self) -> str:
        """
        Execute a single voice turn end-to-end (terminal push-to-talk).

        Record until Enter → transcribe → optional screenshot → agent_fn →
        speak the reply. Returns the agent's text response.
        """
        if self._transcriber is None:
            return backend_problem("stt", self.config)
        try:
            pcm = self._recorder.record_until_keypress()
        except (ImportError, RuntimeError) as exc:
            log.error("Recording unavailable: %s", exc)
            return str(exc)

        transcript = self.transcribe(pcm)
        log.info("User said: %r", transcript[:120])

        screenshot_b64: str | None = None
        if self.config.vision_enabled:
            screenshot_b64 = self._capture.capture()

        response = self.agent_fn(transcript, screenshot_b64)
        log.info("Agent response: %r", response[:120])
        self.speak(response)
        return response

    def run_loop(self) -> None:
        """
        Run voice turns in a continuous loop until Ctrl-C.

        Prints a prompt before each turn and exits gracefully on
        KeyboardInterrupt.
        """
        print("\nVoice pipeline active.  Press Ctrl-C to stop.\n")
        try:
            while True:
                print("Press Enter to speak…")
                response = self.run_once()
                print(f"\nAgent: {response}\n")
        except KeyboardInterrupt:
            print("\nVoice loop stopped.")
            log.info("Voice loop exited by user.")


# ---------------------------------------------------------------------------
# Module-level convenience helpers
# ---------------------------------------------------------------------------


def create_voice_pipeline(
    config: VoiceConfig,
    agent_fn: callable,  # type: ignore[type-arg]
) -> VoicePipeline:
    """
    Convenience constructor for VoicePipeline.

    Parameters
    ----------
    config:
        VoiceConfig with API keys and feature flags.
    agent_fn:
        Callable ``(text: str, screenshot_b64: str | None) -> str``.

    Returns
    -------
    VoicePipeline
    """
    return VoicePipeline(config=config, agent_fn=agent_fn)


def is_voice_available() -> dict[str, bool]:
    """
    Report which voice pipeline components are ready to use.

    Returns
    -------
    dict with keys: recorder, transcriber, tts, vision, offline

    Each value is True only when the required third-party package is
    installed ("offline" = sherpa-onnx; the models are checked separately
    with models.voice.is_installed).
    """
    return {
        "recorder": _pyaudio is not None or _alsa_available(),
        "transcriber": _requests is not None or _sherpa is not None,
        "tts": _elevenlabs_sdk or _requests is not None or _sherpa is not None,
        "vision": _pil_available or _pyautogui_available,
        "offline": _sherpa is not None,
    }
