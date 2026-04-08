"""
carry-ai/models/registry.py -- JSON-backed GGUF Model Registry
===============================================================

Tracks downloaded GGUF models with SHA-256 integrity verification, active
model selection, and download metadata. All state is persisted to
models/registry.json on the USB drive.

Registry JSON schema:
    {
      "active_model": "qwen2.5-7b-instruct-q4_k_m.gguf",
      "models": {
        "qwen2.5-7b-instruct-q4_k_m.gguf": {
          "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
          "size_bytes": 4678123456,
          "sha256": "abc123...",
          "downloaded_at": "2026-04-08T12:00:00Z",
          "last_verified": "2026-04-08T12:00:00Z",
          "verified_ok": true,
          "quant": "Q4_K_M",
          "gated": false
        }
      }
    }

Architecture:
    - Thread-safe: threading.Lock guards all reads and writes
    - No external dependencies: stdlib only (json, hashlib, datetime, threading, pathlib)
    - Singleton: get_registry() returns a shared instance rooted at PROJECT_ROOT/models/
    - scan() reconciles disk state with the registry (adds new files, removes missing)
    - verify() computes SHA-256 and stores it on first run; compares on subsequent runs
"""

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("carry-ai.models.registry")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_REGISTRY_FILENAME = "registry.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    """Return current UTC time as an ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA-256 digest of a file.  Returns hex string."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        log.error("SHA-256 computation failed for %s: %s", path, exc)
        return ""


def _empty_entry(filename: str) -> dict:
    """Return a skeleton registry entry for a newly discovered file."""
    return {
        "repo_id": "",
        "size_bytes": 0,
        "sha256": "",
        "downloaded_at": _utcnow(),
        "last_verified": "",
        "verified_ok": False,
        "quant": "",
        "gated": False,
    }


# ---------------------------------------------------------------------------
# ModelRegistry
# ---------------------------------------------------------------------------

class ModelRegistry:
    """
    Persistent, thread-safe registry of downloaded GGUF models.

    All mutating operations immediately flush registry.json to disk so the
    state is durable across sudden process exits.
    """

    def __init__(self, models_dir: Path):
        """
        Load (or create) the registry for models_dir.

        Args:
            models_dir: Directory where *.gguf files and registry.json live.
        """
        self._dir = Path(models_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._dir / _REGISTRY_FILENAME
        self._lock = threading.Lock()

        self._data: dict = {"active_model": "", "models": {}}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Read registry.json from disk.  Silently starts fresh on any error."""
        if not self._registry_path.exists():
            return
        try:
            with open(self._registry_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._data = raw
                self._data.setdefault("active_model", "")
                self._data.setdefault("models", {})
                log.debug("Registry loaded: %d model(s)", len(self._data["models"]))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read registry.json (%s); starting fresh.", exc)

    def _save(self) -> None:
        """Flush current state to registry.json.  Must be called under self._lock."""
        try:
            tmp = self._registry_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            tmp.replace(self._registry_path)
        except OSError as exc:
            log.error("Failed to save registry.json: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> list[dict]:
        """
        Reconcile the registry with actual files on disk.

        - Any *.gguf file not in the registry is added with an empty entry.
        - Registry entries whose files no longer exist on disk are removed.
        - Returns the enriched list of all registered models (same as list_all()).
        """
        with self._lock:
            disk_files = {p.name for p in self._dir.glob("*.gguf")}
            registered = set(self._data["models"].keys())

            # Add newly discovered files
            for fname in disk_files - registered:
                log.info("Registry: discovered unregistered file %s", fname)
                entry = _empty_entry(fname)
                try:
                    entry["size_bytes"] = (self._dir / fname).stat().st_size
                except OSError:
                    pass
                self._data["models"][fname] = entry

            # Remove stale entries
            for fname in registered - disk_files:
                log.info("Registry: removing missing file entry %s", fname)
                del self._data["models"][fname]
                if self._data.get("active_model") == fname:
                    self._data["active_model"] = ""

            if (disk_files - registered) or (registered - disk_files):
                self._save()

            return self._list_all_unsafe()

    def add(
        self,
        filename: str,
        repo_id: str = "",
        size_bytes: int = 0,
        sha256: str = "",
        gated: bool = False,
        quant: str = "",
    ) -> None:
        """
        Add or update a model entry in the registry.

        If the entry already exists its metadata is updated rather than
        replaced so that previously verified hashes are preserved.

        Args:
            filename:   Basename of the .gguf file (no path).
            repo_id:    HuggingFace repo, e.g. "Qwen/Qwen2.5-7B-Instruct-GGUF".
            size_bytes: File size in bytes (0 = unknown).
            sha256:     Hex SHA-256 digest ("" = not yet computed).
            gated:      True if the model requires an HF token.
            quant:      Quantization label, e.g. "Q4_K_M".
        """
        with self._lock:
            existing = self._data["models"].get(filename, {})
            entry: dict = {
                "repo_id": repo_id or existing.get("repo_id", ""),
                "size_bytes": size_bytes if size_bytes else existing.get("size_bytes", 0),
                "sha256": sha256 if sha256 else existing.get("sha256", ""),
                "downloaded_at": existing.get("downloaded_at", _utcnow()),
                "last_verified": existing.get("last_verified", ""),
                "verified_ok": existing.get("verified_ok", False),
                "quant": quant or existing.get("quant", ""),
                "gated": gated if gated else existing.get("gated", False),
            }
            self._data["models"][filename] = entry
            log.debug("Registry: added/updated entry for %s", filename)
            self._save()

    def remove(self, filename: str) -> bool:
        """
        Delete the model file from disk and remove its registry entry.

        Args:
            filename: Basename of the .gguf file.

        Returns:
            True  — file existed (and was deleted, or deletion was attempted).
            False — file was not on disk and not in the registry.
        """
        target = self._dir / filename
        existed = target.exists()

        if existed:
            try:
                target.unlink()
                log.info("Registry: deleted file %s", target)
            except OSError as exc:
                log.error("Failed to delete %s: %s", target, exc)

        with self._lock:
            if filename in self._data["models"]:
                del self._data["models"][filename]
                if self._data.get("active_model") == filename:
                    self._data["active_model"] = ""
                self._save()
            elif not existed:
                return False

        return existed

    def verify(self, filename: str) -> tuple[bool, str]:
        """
        Verify the integrity of a downloaded model file.

        Behaviour:
        - If the registry has a stored SHA-256, compare against it.
        - If no stored SHA-256, compute the digest, store it, and confirm
          the file is non-empty (size > 0).
        - Updates last_verified timestamp and verified_ok flag.

        Args:
            filename: Basename of the .gguf file.

        Returns:
            (True, message)  on success.
            (False, message) on failure or file-not-found.
        """
        path = self._dir / filename

        if not path.exists():
            msg = f"File not found: {path}"
            log.warning("Registry verify: %s", msg)
            return False, msg

        log.info("Registry: computing SHA-256 for %s (this may take a moment)...", filename)
        computed = _sha256_file(path)
        if not computed:
            msg = "SHA-256 computation failed (IO error)"
            return False, msg

        now = _utcnow()

        with self._lock:
            entry = self._data["models"].get(filename)
            if entry is None:
                # File exists on disk but isn't registered; register it now.
                entry = _empty_entry(filename)
                try:
                    entry["size_bytes"] = path.stat().st_size
                except OSError:
                    pass
                self._data["models"][filename] = entry

            stored_hash = entry.get("sha256", "")

            if stored_hash:
                # Compare against stored hash
                ok = computed == stored_hash
                if ok:
                    msg = f"OK  sha256={computed}"
                else:
                    msg = (
                        f"MISMATCH  stored={stored_hash[:16]}...  "
                        f"computed={computed[:16]}..."
                    )
                    log.warning("Registry: integrity check failed for %s", filename)
            else:
                # No stored hash — store it and confirm file is non-empty
                size = path.stat().st_size if path.exists() else 0
                ok = size > 0
                entry["sha256"] = computed
                if ok:
                    msg = f"OK (hash stored)  sha256={computed}  size={size:,} bytes"
                else:
                    msg = "File is empty — download may be incomplete"

            entry["last_verified"] = now
            entry["verified_ok"] = ok
            self._save()

        log.info("Registry verify %s: %s", filename, "PASS" if ok else "FAIL")
        return ok, msg

    def set_active(self, filename: str) -> bool:
        """
        Mark a model as the active model carry-ai will load at startup.

        Args:
            filename: Basename of the .gguf file.

        Returns:
            True  — file exists on disk and active_model was updated.
            False — file not found on disk.
        """
        path = self._dir / filename
        if not path.exists():
            log.warning("Registry: cannot set active — file not found: %s", filename)
            return False

        with self._lock:
            self._data["active_model"] = filename
            # Ensure the file has an entry
            if filename not in self._data["models"]:
                entry = _empty_entry(filename)
                try:
                    entry["size_bytes"] = path.stat().st_size
                except OSError:
                    pass
                self._data["models"][filename] = entry
            self._save()

        log.info("Registry: active model set to %s", filename)
        return True

    def get_active(self) -> str | None:
        """
        Return the filename of the currently active model.

        Falls back to the first registered model if active_model is not set or
        its file is missing.  Returns None when no models are registered.
        """
        with self._lock:
            active = self._data.get("active_model", "")
            if active and (self._dir / active).exists():
                return active

            # Fall back to first available model on disk
            for fname in self._data["models"]:
                if (self._dir / fname).exists():
                    log.debug("Registry: active_model missing, falling back to %s", fname)
                    return fname

            return None

    def get_info(self, filename: str) -> dict | None:
        """
        Return the raw registry entry for filename, or None if not found.
        """
        with self._lock:
            entry = self._data["models"].get(filename)
            if entry is None:
                return None
            return dict(entry)  # Return a copy to avoid mutation under lock

    def list_all(self) -> list[dict]:
        """
        Return all registry entries enriched with runtime fields.

        Each item in the returned list includes:
            filename   (str)   — basename of the .gguf file
            exists     (bool)  — whether the file is currently on disk
            size_gb    (float) — size_bytes / 1 GiB
            is_active  (bool)  — True if this is the active_model
            ... plus all stored registry fields
        """
        with self._lock:
            return self._list_all_unsafe()

    def _list_all_unsafe(self) -> list[dict]:
        """list_all() implementation — caller must hold self._lock."""
        active = self._data.get("active_model", "")
        result = []
        for fname, entry in self._data["models"].items():
            size_bytes = entry.get("size_bytes", 0)
            enriched = {
                "filename": fname,
                "exists": (self._dir / fname).exists(),
                "size_gb": size_bytes / (1024 ** 3) if size_bytes else 0.0,
                "is_active": fname == active,
                **entry,
            }
            result.append(enriched)
        # Sort: active first, then alphabetical
        result.sort(key=lambda x: (not x["is_active"], x["filename"].lower()))
        return result


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_singleton: ModelRegistry | None = None
_singleton_lock = threading.Lock()


def get_registry() -> ModelRegistry:
    """
    Return the shared ModelRegistry instance rooted at PROJECT_ROOT/models/.

    Thread-safe — safe to call from multiple threads at startup.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ModelRegistry(PROJECT_ROOT / "models")
    return _singleton
