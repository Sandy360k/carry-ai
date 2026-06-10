"""
carry-ai/models/downloader.py -- HuggingFace GGUF Model Downloader
===================================================================

Browse, search, and download GGUF models from HuggingFace Hub directly
onto the USB drive's models/ directory.

Features:
    - Search HF Hub for GGUF repos by name/keyword
    - List available quantization variants with file sizes
    - Download with progress callback and resume support
    - Optional HF token for gated models (Llama, Gemma, etc.)
    - Auto-suggest models based on available RAM tier
    - Verify downloaded file integrity (size match)
    - Pure requests fallback when huggingface_hub not installed

Usage:
    dl = ModelDownloader("models/")
    results = dl.search("Qwen3 8B GGUF")
    files = dl.list_files("bartowski/Qwen3-8B-GGUF")
    path = dl.download("bartowski/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf")

    # Interactive CLI
    dl.interactive()
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger("carry-ai.models.downloader")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ModelFile:
    """A single GGUF file available for download."""
    repo_id: str             # e.g., "bartowski/Qwen3-8B-GGUF"
    filename: str            # e.g., "Qwen3-8B-Q4_K_M.gguf"
    size_bytes: int = 0
    quant: str = ""          # Extracted quantization level (Q4_K_M, Q8_0, etc.)
    branch: str = "main"

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

    @property
    def size_display(self) -> str:
        if self.size_bytes >= 1024 ** 3:
            return f"{self.size_gb:.2f} GB"
        return f"{self.size_bytes / (1024**2):.1f} MB"

    def to_dict(self) -> dict:
        return {
            "repo_id": self.repo_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "size_display": self.size_display,
            "quant": self.quant,
        }


@dataclass
class RepoInfo:
    """A HuggingFace model repository."""
    repo_id: str             # e.g., "bartowski/Qwen3-8B-GGUF"
    author: str = ""
    description: str = ""
    downloads: int = 0
    likes: int = 0
    tags: list = field(default_factory=list)
    last_modified: str = ""

    def to_dict(self) -> dict:
        return {
            "repo_id": self.repo_id,
            "author": self.author,
            "description": self.description[:200],
            "downloads": self.downloads,
            "likes": self.likes,
        }


# Quantization extraction regex
_QUANT_RE = re.compile(
    r"[_\-\.]((?:IQ[1-4]_[A-Z]+|Q[0-9]+(?:_[A-Z0-9]+)*|F16|F32|BF16))"
    r"(?:[_\-\.]|\.gguf)",
    re.IGNORECASE,
)

# Known GGUF quantization levels sorted by size (smallest to largest)
QUANT_ORDER = [
    "IQ1_S", "IQ1_M", "IQ2_XXS", "IQ2_XS", "IQ2_S", "IQ2_M",
    "Q2_K", "Q2_K_S",
    "IQ3_XXS", "IQ3_XS", "IQ3_S", "IQ3_M",
    "Q3_K_S", "Q3_K_M", "Q3_K_L",
    "IQ4_XS", "IQ4_NL",
    "Q4_0", "Q4_K_S", "Q4_K_M", "Q4_K_L", "Q4_1",
    "Q5_0", "Q5_K_S", "Q5_K_M", "Q5_K_L",
    "Q6_K",
    "Q8_0",
    "F16", "BF16", "F32",
]


def _extract_quant(filename: str) -> str:
    """Extract quantization level from filename."""
    m = _QUANT_RE.search(filename)
    if m:
        return m.group(1).upper()
    return ""


# ---------------------------------------------------------------------------
# API backends (huggingface_hub preferred, requests fallback)
# ---------------------------------------------------------------------------

def _has_hf_hub() -> bool:
    try:
        import huggingface_hub
        return True
    except ImportError:
        return False


class _HfHubBackend:
    """Use huggingface_hub library for API access."""

    def __init__(self, token: str = None):
        from huggingface_hub import HfApi
        self.api = HfApi(token=token)
        self.token = token

    def search(self, query: str, limit: int = 20) -> list[RepoInfo]:
        results = []
        for model in self.api.list_models(
            search=query,
            filter="gguf",
            sort="downloads",
            direction=-1,
            limit=limit,
        ):
            results.append(RepoInfo(
                repo_id=model.id,
                author=model.author or "",
                downloads=model.downloads or 0,
                likes=model.likes or 0,
                tags=list(model.tags or []),
                last_modified=str(model.last_modified or ""),
            ))
        return results

    def list_files(self, repo_id: str) -> list[ModelFile]:
        from huggingface_hub import list_repo_tree
        files = []
        for item in list_repo_tree(repo_id, repo_type="model", token=self.token):
            if hasattr(item, "rfilename") and item.rfilename.endswith(".gguf"):
                files.append(ModelFile(
                    repo_id=repo_id,
                    filename=item.rfilename,
                    size_bytes=item.size or 0,
                    quant=_extract_quant(item.rfilename),
                ))
        # Sort by quantization order
        files.sort(key=lambda f: (
            QUANT_ORDER.index(f.quant) if f.quant in QUANT_ORDER else 999,
            f.size_bytes,
        ))
        return files

    def download(self, repo_id: str, filename: str, target_dir: str,
                 progress_fn: Callable = None) -> Path:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            local_dir=target_dir,
            local_dir_use_symlinks=False,
            token=self.token,
        )
        path = Path(path)
        # Register the downloaded file (SHA-256 computed lazily via verify())
        try:
            from models.registry import ModelRegistry
            registry = ModelRegistry(path.parent)
            registry.add(
                filename=path.name,
                repo_id=repo_id,
                size_bytes=path.stat().st_size,
                sha256="",
            )
        except Exception as _reg_exc:
            logger.debug("Registry update skipped: %s", _reg_exc)
        return path


class _RequestsBackend:
    """Pure requests fallback when huggingface_hub is not installed."""

    HF_API = "https://huggingface.co/api"

    def __init__(self, token: str = None):
        import requests as _req
        self._req = _req
        self.token = token
        self._headers = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def search(self, query: str, limit: int = 20) -> list[RepoInfo]:
        resp = self._req.get(
            f"{self.HF_API}/models",
            params={"search": query, "filter": "gguf", "sort": "downloads",
                    "direction": "-1", "limit": limit},
            headers=self._headers,
            timeout=30,
        )
        resp.raise_for_status()
        results = []
        for m in resp.json():
            results.append(RepoInfo(
                repo_id=m.get("id", ""),
                author=m.get("author", ""),
                downloads=m.get("downloads", 0),
                likes=m.get("likes", 0),
                tags=m.get("tags", []),
                last_modified=m.get("lastModified", ""),
            ))
        return results

    def list_files(self, repo_id: str) -> list[ModelFile]:
        resp = self._req.get(
            f"{self.HF_API}/models/{repo_id}",
            params={"blobs": "true"},
            headers=self._headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        files = []
        for sib in data.get("siblings", []):
            fname = sib.get("rfilename", "")
            if fname.endswith(".gguf"):
                files.append(ModelFile(
                    repo_id=repo_id,
                    filename=fname,
                    size_bytes=sib.get("size", 0),
                    quant=_extract_quant(fname),
                ))
        files.sort(key=lambda f: (
            QUANT_ORDER.index(f.quant) if f.quant in QUANT_ORDER else 999,
            f.size_bytes,
        ))
        return files

    def download(self, repo_id: str, filename: str, target_dir: str,
                 progress_fn: Callable = None) -> Path:
        """Download with streaming + progress callback + resume support."""
        url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
        target = Path(target_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)

        # Resume support
        existing_size = target.stat().st_size if target.exists() else 0
        headers = dict(self._headers)
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"
            logger.info("Resuming download from %d bytes", existing_size)

        resp = self._req.get(url, headers=headers, stream=True, timeout=60)
        resp.raise_for_status()

        # If the server ignored the Range header (200 instead of 206) it is
        # sending the whole file — appending would corrupt the target.
        if existing_size > 0 and resp.status_code != 206:
            logger.info("Server does not support resume — restarting download")
            existing_size = 0

        total = int(resp.headers.get("content-length", 0)) + existing_size
        mode = "ab" if existing_size > 0 else "wb"
        downloaded = existing_size

        with open(target, mode) as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                f.write(chunk)
                downloaded += len(chunk)
                if progress_fn:
                    progress_fn(downloaded, total, filename)

        # Register the downloaded file (SHA-256 computed lazily via verify())
        try:
            from models.registry import ModelRegistry
            registry = ModelRegistry(target.parent)
            registry.add(
                filename=target.name,
                repo_id=repo_id,
                size_bytes=target.stat().st_size,
                sha256="",
            )
        except Exception as _reg_exc:
            logger.debug("Registry update skipped: %s", _reg_exc)

        return target


# ---------------------------------------------------------------------------
# Model Downloader
# ---------------------------------------------------------------------------

class ModelDownloader:
    """
    Browse and download GGUF models from HuggingFace Hub.

    Usage:
        dl = ModelDownloader("models/")
        results = dl.search("Qwen3 8B GGUF")
        files = dl.list_files("bartowski/Qwen3-8B-GGUF")
        path = dl.download("bartowski/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf")
    """

    # Well-known GGUF providers on HuggingFace
    RECOMMENDED_AUTHORS = [
        "bartowski",           # Huge GGUF collection, reliable quantizations
        "unsloth",             # Optimized quants
        "QuantFactory",        # Automated quant pipeline
        "mradermacher",        # Extensive GGUF library
        "lmstudio-community",  # LM Studio curated
    ]

    def __init__(self, models_dir: str = None, hf_token: str = None):
        """
        Args:
            models_dir: Directory to store downloaded models.
                        Defaults to carry-ai/models/
            hf_token: Optional HuggingFace token for gated models.
        """
        if models_dir is None:
            project_root = Path(__file__).resolve().parent.parent
            models_dir = str(project_root / "models")

        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.hf_token = hf_token

        # Pick backend
        if _has_hf_hub():
            self._backend = _HfHubBackend(token=hf_token)
            logger.info("Using huggingface_hub backend")
        else:
            self._backend = _RequestsBackend(token=hf_token)
            logger.info("Using requests fallback backend (pip install huggingface-hub for better experience)")

    # ------------------------------------------------------------------
    # Search & browse
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[RepoInfo]:
        """
        Search HuggingFace Hub for GGUF model repos.

        Args:
            query: Search term (e.g., "Qwen3 8B", "llama 4 scout")
            limit: Max results

        Returns list of RepoInfo sorted by downloads.
        """
        # Auto-append GGUF if not in query
        if "gguf" not in query.lower():
            query = f"{query} GGUF"

        try:
            results = self._backend.search(query, limit=limit)
            logger.info("Search '%s': %d results", query, len(results))
            return results
        except Exception as e:
            logger.error("Search failed: %s", e)
            return []

    def list_files(self, repo_id: str) -> list[ModelFile]:
        """
        List all GGUF files in a repository with sizes and quant levels.

        Args:
            repo_id: HuggingFace repo (e.g., "bartowski/Qwen3-8B-GGUF")

        Returns list of ModelFile sorted by quantization level.
        """
        try:
            files = self._backend.list_files(repo_id)
            logger.info("Repo %s: %d GGUF files", repo_id, len(files))
            return files
        except Exception as e:
            logger.error("Failed to list files for %s: %s", repo_id, e)
            return []

    def download(self, repo_id: str, filename: str,
                 progress_fn: Callable = None) -> Optional[Path]:
        """
        Download a specific GGUF file to models_dir.

        Args:
            repo_id: HuggingFace repo ID
            filename: GGUF filename to download
            progress_fn: Optional callback(downloaded_bytes, total_bytes, filename)

        Returns Path to downloaded file, or None on failure.
        """
        target = self.models_dir / filename

        # Skip if already exists and seems complete
        if target.exists():
            # Check if file matches expected size
            files = self.list_files(repo_id)
            expected = next((f for f in files if f.filename == filename), None)
            if expected and target.stat().st_size == expected.size_bytes:
                logger.info("Already downloaded: %s (%s)", filename, expected.size_display)
                return target

        logger.info("Downloading %s/%s -> %s", repo_id, filename, self.models_dir)
        try:
            path = self._backend.download(
                repo_id, filename, str(self.models_dir),
                progress_fn=progress_fn,
            )
            logger.info("Download complete: %s", path)
            return Path(path)
        except Exception as e:
            logger.error("Download failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # RAM-based suggestions
    # ------------------------------------------------------------------

    def suggest_for_ram(self, ram_gb: float) -> list[dict]:
        """
        Suggest models based on available RAM, using the tier definitions
        from local_mode.py.

        Returns list of dicts with repo_id, suggested_quant, and search hints.
        """
        suggestions = []

        # Map RAM to model names and suggested quants
        tier_map = [
            (32, "Qwen3 30B",          "Q6_K"),
            (24, "Llama 4 Scout 17B",  "Q6_K"),
            (20, "Qwen3 14B",          "Q8_0"),
            (16, "Gemma 4 12B",        "Q4_K_M"),
            (12, "Qwen3 14B",          "Q4_K_M"),
            (10, "Qwen3 8B",           "Q8_0"),
            (8,  "Qwen3 8B",           "Q4_K_M"),
            (6,  "Gemma 4 E4B",        "Q4_K_M"),
            (5,  "Qwen3.5 4B",         "Q4_K_M"),
            (4,  "Phi-4-mini",         "Q4_K_M"),
            (3,  "Gemma 4 E2B",        "Q4_K_M"),
            (0,  "Gemma 3 1B",         "Q4_K_M"),
        ]

        for min_ram, model_name, quant in tier_map:
            if ram_gb >= min_ram:
                suggestions.append({
                    "model_name": model_name,
                    "suggested_quant": quant,
                    "search_query": f"{model_name} GGUF",
                    "min_ram_gb": min_ram,
                })
                if len(suggestions) >= 3:
                    break

        return suggestions

    # ------------------------------------------------------------------
    # Local model inventory
    # ------------------------------------------------------------------

    def list_local(self) -> list[dict]:
        """List GGUF files already in the models directory."""
        models = []
        for f in sorted(self.models_dir.glob("*.gguf")):
            models.append({
                "filename": f.name,
                "size_bytes": f.stat().st_size,
                "size_display": f"{f.stat().st_size / (1024**3):.2f} GB",
                "quant": _extract_quant(f.name),
            })
        return models

    # ------------------------------------------------------------------
    # Registry-backed operations
    # ------------------------------------------------------------------

    def verify(self, filename: str) -> tuple[bool, str]:
        """
        Verify SHA-256 integrity of a downloaded model.

        On the first call the hash is computed and stored; subsequent calls
        compare against the stored value.  Delegates to ModelRegistry.

        Returns:
            (True, message)  if the file passes integrity check.
            (False, message) if the file is missing or the hash mismatches.
        """
        try:
            from models.registry import ModelRegistry
            registry = ModelRegistry(self.models_dir)
            return registry.verify(filename)
        except Exception as exc:
            logger.error("verify() failed: %s", exc)
            return False, str(exc)

    def delete(self, filename: str) -> bool:
        """
        Delete a model file from disk and remove it from the registry.

        Args:
            filename: Basename of the .gguf file.

        Returns:
            True if the file existed and was deleted; False otherwise.
        """
        try:
            from models.registry import ModelRegistry
            registry = ModelRegistry(self.models_dir)
            return registry.remove(filename)
        except Exception as exc:
            logger.error("delete() failed: %s", exc)
            return False

    def set_active(self, filename: str) -> bool:
        """
        Mark model as the active one carry-ai will use at startup.

        Args:
            filename: Basename of the .gguf file.

        Returns:
            True if the file exists on disk and was marked active; False otherwise.
        """
        try:
            from models.registry import ModelRegistry
            registry = ModelRegistry(self.models_dir)
            return registry.set_active(filename)
        except Exception as exc:
            logger.error("set_active() failed: %s", exc)
            return False

    def get_active(self) -> str | None:
        """
        Return the filename of the currently active model.

        Falls back to the first registered model when active_model is not
        explicitly set.  Returns None when no models are registered.
        """
        try:
            from models.registry import ModelRegistry
            registry = ModelRegistry(self.models_dir)
            return registry.get_active()
        except Exception as exc:
            logger.error("get_active() failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Interactive CLI
    # ------------------------------------------------------------------

    def interactive(self) -> Optional[Path]:
        """
        Interactive terminal wizard for browsing and downloading models.
        Called by: python launcher.py --download-model

        Returns Path to downloaded model, or None if cancelled.
        """
        print("\n  carry-ai Model Downloader")
        print("  " + "=" * 30)
        print()

        # Show existing models
        local = self.list_local()
        if local:
            print(f"  Models already on USB ({len(local)}):")
            for m in local:
                print(f"    {m['filename']} ({m['size_display']}, {m['quant']})")
            print()

        # Check RAM for suggestions
        try:
            import psutil
            ram_gb = psutil.virtual_memory().total / (1024 ** 3)
            suggestions = self.suggest_for_ram(ram_gb)
            if suggestions:
                print(f"  Suggested for your RAM ({ram_gb:.0f} GB):")
                for i, s in enumerate(suggestions, 1):
                    print(f"    {i}. {s['model_name']} {s['suggested_quant']}")
                print()
        except ImportError:
            ram_gb = 0
            suggestions = []

        # Search or direct repo
        while True:
            query = input("  Search (or repo_id, or 'quit'): ").strip()
            if not query or query.lower() in ("quit", "q", "exit"):
                return None

            # Direct repo_id (contains /)
            if "/" in query and " " not in query:
                repo_id = query
                break

            # Search
            print(f"  Searching HuggingFace for '{query}'...")
            results = self.search(query, limit=10)

            if not results:
                print("  No results found. Try a different search term.")
                continue

            print()
            for i, r in enumerate(results, 1):
                dl_str = f"{r.downloads:,}" if r.downloads else "?"
                print(f"  {i:>2}. {r.repo_id}")
                print(f"      Downloads: {dl_str}  Likes: {r.likes}")

            print()
            choice = input(f"  Select repo (1-{len(results)}, or new search): ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(results):
                    repo_id = results[idx].repo_id
                    break
            except ValueError:
                continue

        # List files in selected repo
        print(f"\n  Fetching files from {repo_id}...")
        files = self.list_files(repo_id)

        if not files:
            print("  No GGUF files found in this repository.")
            return None

        print(f"\n  Available quantizations ({len(files)} files):")
        for i, f in enumerate(files, 1):
            ram_hint = ""
            if f.size_gb > 0:
                needed = f.size_gb + 2.0  # ~2GB overhead for OS + llama.cpp
                ram_hint = f"  (needs ~{needed:.0f} GB RAM)"
            print(f"  {i:>2}. [{f.quant or '?':>8}] {f.filename}  ({f.size_display}){ram_hint}")

        print()
        choice = input(f"  Select file (1-{len(files)}, or 'cancel'): ").strip()
        if choice.lower() in ("cancel", "c", ""):
            return None

        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(files)):
                print("  Invalid selection.")
                return None
        except ValueError:
            print("  Invalid input.")
            return None

        selected = files[idx]

        # Confirm download
        print(f"\n  Download: {selected.filename}")
        print(f"  Size:     {selected.size_display}")
        print(f"  To:       {self.models_dir}")
        confirm = input("  Proceed? [Y/n] ").strip().lower()
        if confirm in ("n", "no"):
            return None

        # Download with progress
        start_time = time.time()

        def _progress(downloaded: int, total: int, fname: str):
            if total <= 0:
                return
            pct = downloaded / total * 100
            speed = downloaded / (time.time() - start_time + 0.001) / (1024 ** 2)
            bar_width = 30
            filled = int(bar_width * downloaded / total)
            bar = "#" * filled + "-" * (bar_width - filled)
            sys.stdout.write(
                f"\r  [{bar}] {pct:5.1f}%  {downloaded/(1024**3):.2f}/{total/(1024**3):.2f} GB  {speed:.1f} MB/s"
            )
            sys.stdout.flush()

        print()
        path = self.download(selected.repo_id, selected.filename, progress_fn=_progress)
        print()  # Newline after progress bar

        if path and path.exists():
            elapsed = time.time() - start_time
            print(f"\n  Download complete! ({elapsed:.0f}s)")
            print(f"  Saved to: {path}")
            print(f"  Size:     {path.stat().st_size / (1024**3):.2f} GB")
            return path
        else:
            print("\n  Download failed. Check your internet connection and try again.")
            return None


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="carry-ai-downloader",
        description="Download GGUF models from HuggingFace Hub.",
    )
    sub = parser.add_subparsers(dest="command")

    # interactive
    sub.add_parser("interactive", help="Interactive download wizard")

    # search
    p_search = sub.add_parser("search", help="Search for GGUF models")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10, help="Max results")

    # list
    p_list = sub.add_parser("list", help="List GGUF files in a repo")
    p_list.add_argument("repo_id", help="HuggingFace repo ID")

    # download
    p_dl = sub.add_parser("download", help="Download a specific file")
    p_dl.add_argument("repo_id", help="HuggingFace repo ID")
    p_dl.add_argument("filename", help="GGUF filename")
    p_dl.add_argument("--target", help="Target directory", default=None)

    # local
    sub.add_parser("local", help="List locally downloaded models")

    # suggest
    p_sug = sub.add_parser("suggest", help="Suggest models for your RAM")
    p_sug.add_argument("--ram", type=float, help="Override RAM in GB")

    # verify
    p_verify = sub.add_parser("verify", help="Verify SHA-256 integrity of a model file")
    p_verify.add_argument("filename", help="GGUF filename (basename only)")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a model file and remove from registry")
    p_delete.add_argument("filename", help="GGUF filename (basename only)")
    p_delete.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # set-active
    p_setactive = sub.add_parser("set-active", help="Mark a model as the active one")
    p_setactive.add_argument("filename", help="GGUF filename (basename only)")

    # active
    sub.add_parser("active", help="Show which model is currently active")

    parser.add_argument("--token", help="HuggingFace token for gated models")

    args = parser.parse_args()

    dl = ModelDownloader(
        models_dir=args.target if hasattr(args, "target") and args.target else None,
        hf_token=args.token,
    )

    if args.command == "interactive" or args.command is None:
        dl.interactive()

    elif args.command == "search":
        results = dl.search(args.query, limit=args.limit)
        for r in results:
            dl_str = f"{r.downloads:,}" if r.downloads else "?"
            print(f"  {r.repo_id}  (downloads: {dl_str}, likes: {r.likes})")

    elif args.command == "list":
        files = dl.list_files(args.repo_id)
        for f in files:
            print(f"  [{f.quant or '?':>8}] {f.filename}  ({f.size_display})")

    elif args.command == "download":
        start = time.time()

        def _prog(d, t, fn):
            if t > 0:
                pct = d / t * 100
                sys.stdout.write(f"\r  {pct:.1f}% ({d/(1024**3):.2f}/{t/(1024**3):.2f} GB)")
                sys.stdout.flush()

        path = dl.download(args.repo_id, args.filename, progress_fn=_prog)
        print()
        if path:
            print(f"  Downloaded: {path} ({time.time()-start:.0f}s)")
        else:
            print("  Download failed.")

    elif args.command == "local":
        models = dl.list_local()
        if models:
            for m in models:
                print(f"  {m['filename']}  ({m['size_display']}, {m['quant']})")
        else:
            print("  No models found.")

    elif args.command == "suggest":
        ram = args.ram
        if not ram:
            try:
                import psutil
                ram = psutil.virtual_memory().total / (1024 ** 3)
            except ImportError:
                ram = 8.0
        suggestions = dl.suggest_for_ram(ram)
        print(f"  Suggestions for {ram:.0f} GB RAM:")
        for s in suggestions:
            print(f"    {s['model_name']} {s['suggested_quant']}  (min {s['min_ram_gb']}GB)")
            print(f"      Search: {s['search_query']}")

    elif args.command == "verify":
        print(f"  Verifying {args.filename} ...")
        ok, msg = dl.verify(args.filename)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {msg}")
        sys.exit(0 if ok else 1)

    elif args.command == "delete":
        target = dl.models_dir / args.filename
        if not target.exists():
            print(f"  File not found: {target}")
            sys.exit(1)
        size_display = f"{target.stat().st_size / (1024**3):.2f} GB"
        if not args.yes:
            confirm = input(
                f"  Delete {args.filename} ({size_display})? [y/N] "
            ).strip().lower()
            if confirm not in ("y", "yes"):
                print("  Cancelled.")
                sys.exit(0)
        deleted = dl.delete(args.filename)
        if deleted:
            print(f"  Deleted: {args.filename}")
        else:
            print(f"  Could not delete {args.filename}.")
            sys.exit(1)

    elif args.command == "set-active":
        ok = dl.set_active(args.filename)
        if ok:
            print(f"  Active model set to: {args.filename}")
        else:
            print(f"  File not found: {args.filename}")
            sys.exit(1)

    elif args.command == "active":
        active = dl.get_active()
        if active:
            print(f"  Active model: {active}")
        else:
            print("  No active model set (no models downloaded yet).")


if __name__ == "__main__":
    main()
