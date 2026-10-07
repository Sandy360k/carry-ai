"""
carry-ai/models/voice.py — Offline voice models (sherpa-onnx)
===============================================================

Catalogue and downloader for the speech models the offline voice backend
(integrations/voice_tools.py, ``voice.stt_backend`` / ``voice.tts_backend``
= "offline") runs with sherpa-onnx. Nothing here talks to a cloud service
at run time: the models are fetched once onto the USB, then used offline.

Layout on the USB::

    models/voice/<model id>/...   (files as in the Hugging Face repo)

Each entry is pinned to a Hugging Face commit, so the files never change
under us; large (LFS) files are checked against the SHA-256 the Hub
publishes for them. The repos are the sherpa-onnx author's conversions
(csukuangfj), the same files the sherpa-onnx releases ship as tarballs.

    STT  moonshine-tiny-en   Moonshine v2 tiny, English      ~44 MB  (default)
         moonshine-base-en   Moonshine v2 base, English     ~141 MB  (more accurate)
    TTS  kitten-nano-en      KittenTTS nano v0.2, English    ~42 MB  (default)
         kokoro-multi        Kokoro 82M int8, multi-lingual ~215 MB  (most natural)

Stdlib only (also used by flash_usb.py / setup_usb.py on a bare Python).
"""

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

log = logging.getLogger("carry-ai.models.voice")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICE_DIR = PROJECT_ROOT / "models" / "voice"
HF_BASE = "https://huggingface.co"
_UA = {"User-Agent": "carry-ai/voice-models"}
_CHUNK = 1 << 20


@dataclass(frozen=True)
class VoiceModel:
    id: str
    kind: str            # "stt" | "tts"
    family: str          # sherpa-onnx loader: moonshine_v2 | kitten | kokoro
    label: str
    repo: str
    revision: str        # pinned commit
    size_mb: int
    languages: str = "en"
    skip: tuple = ("test_wavs/", ".gitattributes")


VOICE_MODELS: list[VoiceModel] = [
    VoiceModel("moonshine-tiny-en", "stt", "moonshine_v2", "Moonshine tiny (English, fast)",
               "csukuangfj2/sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27",
               "d1e6c30921780b8508d04b492dfb3ce8a51605d4", 44),
    VoiceModel("moonshine-base-en", "stt", "moonshine_v2", "Moonshine base (English, more accurate)",
               "csukuangfj2/sherpa-onnx-moonshine-base-en-quantized-2026-02-27",
               "8f4d6c58c03d40bcea40043bb7120a878f2bbef6", 141),
    VoiceModel("kitten-nano-en", "tts", "kitten", "KittenTTS nano (English, small)",
               "csukuangfj/kitten-nano-en-v0_2-fp16",
               "7d14a97d38072576ddca7a673ab5bb49c43bb169", 42),
    VoiceModel("kokoro-multi", "tts", "kokoro", "Kokoro (natural, multi-lingual)",
               "csukuangfj/kokoro-int8-multi-lang-v1_1",
               "155831f1b4ba23b1f5c058be6a61df90cefb2a37", 215, languages="en, zh, es, fr, ja, …"),
]

DEFAULT_STT = "moonshine-tiny-en"
DEFAULT_TTS = "kitten-nano-en"

_BY_ID = {m.id: m for m in VOICE_MODELS}


def get_model(model_id: str) -> VoiceModel | None:
    return _BY_ID.get(model_id)


def models_of(kind: str) -> list[VoiceModel]:
    return [m for m in VOICE_MODELS if m.kind == kind]


def model_dir(model_id: str, root: Path | None = None) -> Path:
    return Path(root or VOICE_DIR) / model_id


_MARKER = ".complete"


def is_installed(model_id: str, root: Path | None = None) -> bool:
    """True once every file of the model has been downloaded and verified."""
    return (model_dir(model_id, root) / _MARKER).is_file()


def installed(kind: str, root: Path | None = None) -> list[VoiceModel]:
    return [m for m in models_of(kind) if is_installed(m.id, root)]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _get_json(url: str):
    with urlopen(Request(url, headers=_UA), timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_files(model: VoiceModel) -> list[dict]:
    """Files to fetch: [{"path", "size", "sha256" (LFS files only)}]."""
    rev = model.revision or "main"
    entries = _get_json(f"{HF_BASE}/api/models/{model.repo}/tree/{rev}?recursive=1")
    files = []
    for e in entries:
        if e.get("type") != "file" or any(e["path"].startswith(s) for s in model.skip):
            continue
        lfs = e.get("lfs") or {}
        files.append({"path": e["path"], "size": e.get("size", 0),
                      "sha256": lfs.get("oid", "")})
    return files


def _safe_target(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if base.resolve() not in target.parents:
        raise ValueError(f"Refusing path outside the model folder: {rel}")
    return target


def _fetch(model: VoiceModel, f: dict, dest: Path, opener, on_bytes) -> None:
    target = _safe_target(dest, f["path"])
    if target.is_file() and target.stat().st_size == f["size"]:
        on_bytes(f["size"], f["path"])
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    url = f"{HF_BASE}/{model.repo}/resolve/{model.revision or 'main'}/{quote(f['path'])}"
    sha = hashlib.sha256()
    with opener(Request(url, headers=_UA), timeout=60) as resp, open(part, "wb") as out:
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            out.write(chunk)
            sha.update(chunk)
            on_bytes(len(chunk), f["path"])
    if f["sha256"] and sha.hexdigest() != f["sha256"]:
        part.unlink(missing_ok=True)
        raise ValueError(f"Checksum mismatch for {f['path']}")
    os.replace(part, target)


def download(model_id: str, root: Path | None = None, progress=None,
             opener=urlopen, workers: int = 6) -> Path:
    """Fetch *model_id* into models/voice/<id>/ (resumable: done files are kept).

    The TTS models carry a few hundred small espeak-ng data files, so
    files are fetched *workers* at a time.

    Args:
        progress: optional fn(done_bytes, total_bytes, current_path),
            called from worker threads.
    Returns the model folder. Raises on network errors or a hash mismatch.
    """
    from concurrent.futures import ThreadPoolExecutor
    model = _BY_ID[model_id]
    dest = model_dir(model_id, root)
    dest.mkdir(parents=True, exist_ok=True)
    files = list_files(model)
    total = sum(f["size"] for f in files)
    done = [0]
    lock = threading.Lock()

    def on_bytes(n: int, path: str) -> None:
        with lock:
            done[0] += n
            if progress:
                progress(done[0], total, path)

    # Big files first so they overlap with the many small ones
    files.sort(key=lambda f: -f["size"])
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for fut in [pool.submit(_fetch, model, f, dest, opener, on_bytes) for f in files]:
            fut.result()          # re-raises the first failure

    (dest / _MARKER).write_text(model.revision or "main", encoding="utf-8")
    log.info("Voice model %s ready in %s", model_id, dest)
    return dest


def remove(model_id: str, root: Path | None = None) -> None:
    import shutil
    shutil.rmtree(model_dir(model_id, root), ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI: python models/voice.py list | get <id> | remove <id>
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import sys
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "list"
    if cmd == "list":
        for m in VOICE_MODELS:
            mark = "✓" if is_installed(m.id) else " "
            default = " (default)" if m.id in (DEFAULT_STT, DEFAULT_TTS) else ""
            print(f" {mark} {m.kind.upper()}  {m.id:<18} {m.size_mb:>4} MB  {m.label}{default}")
        return 0
    if cmd in ("get", "remove") and len(args) == 2 and args[1] in _BY_ID:
        if cmd == "remove":
            remove(args[1])
            return 0

        def show(done, total, path):
            pct = done * 100 // total if total else 100
            print(f"\r  {pct:3d}%  {path[:50]:<50}", end="", flush=True)
        download(args[1], progress=show)
        print("\n  done.")
        return 0
    print("usage: python models/voice.py list | get <id> | remove <id>")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
