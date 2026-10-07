"""
carry-ai voice pipeline — inspired by farzaa/clicky.

Push-to-talk recording, AssemblyAI transcription, Claude vision,
ElevenLabs TTS.

Architecture
------------
AudioRecorder   — captures PCM audio from the default mic via PyAudio
                  until the user presses Enter (push-to-talk).
Transcriber     — uploads audio to AssemblyAI and polls for the result.
ScreenCapture   — grabs a screenshot and returns it as a base64 PNG string.
TTSPlayer       — sends text to ElevenLabs and plays the audio via PyAudio.
VoicePipeline   — orchestrates a full turn: record → transcribe →
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


# ---------------------------------------------------------------------------
# AudioRecorder
# ---------------------------------------------------------------------------


class AudioRecorder:
    """Captures raw PCM audio from the default microphone via PyAudio."""

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self._frames: list[bytes] = []
        self._recording = False

    def record_until_keypress(self) -> bytes:
        """
        Record audio until the user presses Enter.

        The recording runs on a background thread while the main thread
        blocks on input().  Returns the concatenated raw PCM bytes.

        Raises
        ------
        ImportError
            If PyAudio is not installed.
        """
        if _pyaudio is None:
            raise ImportError(
                "PyAudio is required for voice recording.  "
                "Install it with: pip install pyaudio"
            )

        self._frames = []
        self._recording = True

        pa = PyAudio()

        try:
            stream = pa.open(
                format=paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
            )
        except Exception as exc:
            pa.terminate()
            raise RuntimeError(f"Could not open audio input: {exc}") from exc

        def _capture() -> None:
            """Background thread: reads chunks while self._recording is True."""
            max_chunks = int(
                SAMPLE_RATE / CHUNK_SIZE * self.config.max_recording_seconds
            )
            count = 0
            while self._recording and count < max_chunks:
                try:
                    data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                    self._frames.append(data)
                except Exception as exc:
                    log.warning("Audio read error: %s", exc)
                    break
                count += 1

        thread = threading.Thread(target=_capture, daemon=True)
        thread.start()

        print("  [Recording…] Press Enter to stop.")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self._recording = False

        thread.join(timeout=2)
        stream.stop_stream()
        stream.close()
        pa.terminate()

        raw_pcm = b"".join(self._frames)
        log.debug("Recorded %d bytes of PCM audio.", len(raw_pcm))
        return raw_pcm

    def save_wav(self, pcm_bytes: bytes) -> str:
        """
        Persist raw PCM bytes as a 16-bit mono WAV file.

        Returns the path to the temporary file.
        """
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        with wave.open(path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # 16-bit = 2 bytes per sample
            wf.setframerate(SAMPLE_RATE)
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
# TTSPlayer
# ---------------------------------------------------------------------------


class TTSPlayer:
    """Synthesises speech from text using ElevenLabs and plays it via PyAudio."""

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config

    def _play_pcm_stream(self, audio_iter) -> None:
        """Feed a streaming audio iterator into PyAudio for real-time playback."""
        if _pyaudio is None:
            log.warning("PyAudio not installed; cannot play audio.")
            return

        pa = PyAudio()
        stream = pa.open(
            format=paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            output=True,
        )
        try:
            for chunk in audio_iter:
                if chunk:
                    stream.write(chunk)
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()

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
        self._transcriber = Transcriber(config)
        self._capture = ScreenCapture()
        self._tts = TTSPlayer(config)

    def run_once(self) -> str:
        """
        Execute a single voice turn end-to-end.

        Steps:
          1. Record audio until Enter is pressed.
          2. Save to a temporary WAV file.
          3. Transcribe via AssemblyAI.
          4. Optionally capture a screenshot.
          5. Call agent_fn with transcript + screenshot.
          6. Speak the response via TTS.
          7. Return the response text.

        Returns
        -------
        str
            The agent's text response.
        """
        # 1 + 2 — record and persist
        try:
            pcm = self._recorder.record_until_keypress()
        except ImportError as exc:
            log.error("Recording unavailable: %s", exc)
            return str(exc)

        wav_path = self._recorder.save_wav(pcm)

        try:
            # 3 — transcribe
            transcript = self._transcriber.transcribe(wav_path)
            log.info("User said: %r", transcript[:120])

            # 4 — screenshot (optional)
            screenshot_b64: str | None = None
            if self.config.vision_enabled:
                screenshot_b64 = self._capture.capture()

            # 5 — call agent
            response = self.agent_fn(transcript, screenshot_b64)
            log.info("Agent response: %r", response[:120])

            # 6 — speak
            self._tts.speak(response)

            return response

        finally:
            # Clean up the temporary WAV file
            with contextlib.suppress(OSError):
                os.remove(wav_path)

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
    dict with keys: recorder, transcriber, tts, vision

    Each value is True only when the required third-party package is
    installed.
    """
    return {
        "recorder": _pyaudio is not None,
        "transcriber": _requests is not None,   # REST only; SDK not needed
        "tts": _elevenlabs_sdk or (_requests is not None),
        "vision": _pil_available or _pyautogui_available,
    }
