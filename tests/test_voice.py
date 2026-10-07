"""
Offline voice (sherpa-onnx) and the backend toggle: model catalogue and
downloader (HTTP faked), Auto/Offline/Cloud/Off resolution, PCM helpers,
and the USB tooling that ships the models. No audio devices, no network.
"""

import hashlib
import io
import re
import wave
from pathlib import Path

import pytest

from conftest import PROJECT_ROOT  # noqa: F401
import integrations.voice_tools as vt
from models import voice as vm


# ---------------------------------------------------------------------------
# Model catalogue + downloader
# ---------------------------------------------------------------------------

def test_catalogue_is_pinned_and_has_defaults():
    ids = [m.id for m in vm.VOICE_MODELS]
    assert len(ids) == len(set(ids))
    assert vm.get_model(vm.DEFAULT_STT).kind == "stt"
    assert vm.get_model(vm.DEFAULT_TTS).kind == "tts"
    for m in vm.VOICE_MODELS:
        assert re.fullmatch(r"[0-9a-f]{40}", m.revision), m.id   # immutable files
        assert m.family in ("moonshine_v2", "kitten", "kokoro")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_repo(monkeypatch, files: dict[str, bytes], lfs=("model.onnx",)):
    listing = [{"path": p, "size": len(b),
                "sha256": hashlib.sha256(b).hexdigest() if p in lfs else ""}
               for p, b in files.items()]
    monkeypatch.setattr(vm, "list_files", lambda model: [dict(f) for f in listing])
    fetched = []

    def opener(req, timeout=None):
        path = req.full_url.split("/resolve/", 1)[1].split("/", 1)[1]
        fetched.append(path)
        return _Resp(files[path])
    return opener, fetched


def test_download_verifies_and_marks_complete(monkeypatch, tmp_path):
    files = {"model.onnx": b"weights" * 100, "tokens.txt": b"a 0\n",
             "espeak-ng-data/phontab": b"x"}
    opener, fetched = _fake_repo(monkeypatch, files)
    seen = []
    vm.download("kitten-nano-en", root=tmp_path, opener=opener,
                progress=lambda d, t, p: seen.append((d, t)))
    d = tmp_path / "kitten-nano-en"
    assert (d / "espeak-ng-data" / "phontab").read_bytes() == b"x"
    assert vm.is_installed("kitten-nano-en", tmp_path)
    assert seen[-1][0] == seen[-1][1] == sum(len(b) for b in files.values())
    assert sorted(fetched) == sorted(files)

    # Resume: complete files are not fetched again
    fetched.clear()
    vm.download("kitten-nano-en", root=tmp_path, opener=opener)
    assert fetched == []


def test_download_rejects_checksum_mismatch(monkeypatch, tmp_path):
    opener, _ = _fake_repo(monkeypatch, {"model.onnx": b"good"})
    monkeypatch.setattr(vm, "list_files", lambda m: [
        {"path": "model.onnx", "size": 4, "sha256": "0" * 64}])
    with pytest.raises(ValueError, match="Checksum"):
        vm.download("kitten-nano-en", root=tmp_path, opener=opener)
    assert not vm.is_installed("kitten-nano-en", tmp_path)
    assert not (tmp_path / "kitten-nano-en" / "model.onnx").exists()


def test_download_refuses_paths_outside_model_dir(monkeypatch, tmp_path):
    opener, _ = _fake_repo(monkeypatch, {"../evil.txt": b"x"})
    with pytest.raises(ValueError, match="outside"):
        vm.download("kitten-nano-en", root=tmp_path, opener=opener)


# ---------------------------------------------------------------------------
# Backend toggle
# ---------------------------------------------------------------------------

@pytest.fixture()
def offline(monkeypatch):
    """sherpa-onnx importable + chosen models installed (both switchable)."""
    state = {"sherpa": True, "installed": True}
    monkeypatch.setattr(vt, "_sherpa", object() if state["sherpa"] else None)
    monkeypatch.setattr(vt, "is_installed", lambda mid, root=None: state["installed"])

    def set_(sherpa=True, installed=True):
        monkeypatch.setattr(vt, "_sherpa", object() if sherpa else None)
        state["installed"] = installed
    return set_


def test_auto_prefers_offline_then_cloud(offline):
    cfg = vt.VoiceConfig(assemblyai_key="aai", elevenlabs_key="el")
    assert vt.resolve_backend("stt", cfg) == "offline"
    assert vt.resolve_backend("tts", cfg) == "offline"
    offline(installed=False)
    assert vt.resolve_backend("stt", cfg) == "cloud"
    offline(sherpa=False)
    assert vt.resolve_backend("tts", cfg) == "cloud"
    assert vt.resolve_backend("stt", vt.VoiceConfig()) == "off"


def test_explicit_choice_is_kept_and_explained(offline):
    offline(installed=False)
    cfg = vt.VoiceConfig(stt_backend="offline", tts_backend="cloud")
    assert vt.resolve_backend("stt", cfg) == "offline"
    assert "isn't downloaded" in vt.backend_problem("stt", cfg)
    assert "ElevenLabs" in vt.backend_problem("tts", cfg)
    offline(sherpa=False)
    assert "sherpa-onnx" in vt.backend_problem("stt", cfg)
    assert vt.resolve_backend("tts", vt.VoiceConfig(tts_backend="off")) == "off"
    assert vt.resolve_backend("tts", vt.VoiceConfig(tts_enabled=False)) == "off"


def test_factories_pick_the_backend(offline):
    cfg = vt.VoiceConfig(assemblyai_key="aai", elevenlabs_key="el")
    assert isinstance(vt.make_transcriber(cfg), vt.OfflineTranscriber)
    assert isinstance(vt.make_tts(cfg), vt.OfflineTTS)
    cfg.stt_backend = cfg.tts_backend = "cloud"
    assert isinstance(vt.make_transcriber(cfg), vt.Transcriber)
    assert isinstance(vt.make_tts(cfg), vt.TTSPlayer)
    cfg.stt_backend = cfg.tts_backend = "off"
    assert vt.make_transcriber(cfg) is None and vt.make_tts(cfg) is None


def test_load_voice_config_takes_keys_from_keystore_dict(monkeypatch):
    cfg = vt.load_voice_config({"assemblyai": {"api_key": "aai"},
                                "elevenlabs": {"api_key": "el"}, "groq": {"api_key": "g"}})
    assert (cfg.assemblyai_key, cfg.elevenlabs_key) == ("aai", "el")
    assert cfg.stt_backend == "auto" and cfg.stt_model == vm.DEFAULT_STT


# ---------------------------------------------------------------------------
# PCM helpers / transcription paths
# ---------------------------------------------------------------------------

def test_float_to_pcm16_clips():
    import array
    out = array.array("h", vt.float_to_pcm16([0.0, 1.0, -1.0, 2.0, -3.0, 0.5]))
    assert list(out) == [0, 32767, -32767, 32767, -32767, 16383]


def test_offline_transcription_needs_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CARRY_AI_SESSION_DIR", str(tmp_path))

    class Offline:
        def transcribe_pcm(self, pcm, rate):
            return f"{len(pcm)}@{rate}"
    assert vt.transcribe_pcm(Offline(), b"\0\0" * 10, 24000) == "20@24000"
    assert list(tmp_path.iterdir()) == []


def test_cloud_transcription_wav_lives_in_session_dir_and_is_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("CARRY_AI_SESSION_DIR", str(tmp_path))
    seen = {}

    class Cloud:
        def transcribe(self, path):
            seen["path"] = Path(path)
            with wave.open(path) as wf:
                seen["rate"] = wf.getframerate()
            return "hi"
    assert vt.transcribe_pcm(Cloud(), b"\0\0" * 10) == "hi"
    assert seen["path"].parent == tmp_path and seen["rate"] == vt.SAMPLE_RATE
    assert not seen["path"].exists()


def test_offline_transcriber_reads_wav(monkeypatch, tmp_path):
    t = vt.OfflineTranscriber(vt.VoiceConfig())
    monkeypatch.setattr(t, "transcribe_pcm", lambda pcm, rate: (len(pcm), rate))
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\1\0" * 50)
    assert t.transcribe(str(path)) == (100, 16000)


def test_speakable_drops_code_and_links():
    pytest.importorskip("tkinter")
    from ui.voice_ui import speakable
    text = "See **this**:\n```py\nprint(1)\n```\nand https://x.y/z `ls`"
    assert speakable(text) == "See this: (code omitted) and a link ls"


# ---------------------------------------------------------------------------
# USB tooling
# ---------------------------------------------------------------------------

def test_pyaudio_is_only_installed_for_windows(monkeypatch, tmp_path):
    from portable import runtime
    ran = []
    monkeypatch.setattr(runtime.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd[-1]) or
                        runtime.subprocess.CompletedProcess(cmd, 0, "", ""))
    runtime.install_packages(["pyaudio>=0.2.14", "sherpa-onnx>=1.13.8"], tmp_path, "linux")
    assert ran == ["sherpa-onnx>=1.13.8"]
    ran.clear()
    runtime.install_packages(["pyaudio>=0.2.14"], tmp_path, "windows")
    assert ran == ["pyaudio>=0.2.14"]


def test_update_usb_syncs_model_code_but_not_model_data():
    import update_usb
    skip = update_usb._should_skip_path
    assert not skip(Path("models/voice.py"))
    assert not skip(Path("models/catalog.py"))
    assert skip(Path("models/qwen.gguf"))
    assert skip(Path("models/registry.json"))
    assert skip(Path("models/voice/kitten-nano-en/model.fp16.onnx"))


def test_reflash_keeps_downloaded_models(monkeypatch, tmp_path):
    flash_usb = pytest.importorskip("flash_usb")
    src, dest = tmp_path / "src", tmp_path / "usb" / "carry-ai"
    (src / "models").mkdir(parents=True)
    (src / "models" / "voice.py").write_text("new code")
    (src / "models" / "host-only.gguf").write_text("host model")
    (src / "launcher.py").write_text("new")
    (dest / "models" / "voice" / "kitten-nano-en").mkdir(parents=True)
    (dest / "models" / "voice" / "kitten-nano-en" / ".complete").write_text("x")
    (dest / "models" / "on-usb.gguf").write_text("usb model")
    (dest / "stale.py").write_text("old")
    monkeypatch.setattr(flash_usb, "_confirm", lambda *a, **k: True)

    flash_usb.copy_carry_ai(src, dest)

    assert (dest / "models" / "voice.py").read_text() == "new code"
    assert (dest / "models" / "on-usb.gguf").exists()
    assert (dest / "models" / "voice" / "kitten-nano-en" / ".complete").exists()
    assert not (dest / "models" / "host-only.gguf").exists()
    assert not (dest / "stale.py").exists()
