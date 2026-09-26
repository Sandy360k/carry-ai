"""
carry-ai/models/catalog.py — Local model catalogue (single source of truth)
============================================================================

Every place that needs to know "which GGUF for how much RAM" reads this list:
the boot-time auto-selector (modes/local_mode.py, launcher.py), the USB
flasher (flash_usb.py), the desktop Model Manager (ui/desktop.py) and the HF
downloader's suggestions (models/downloader.py).

Entries are ordered best-first. ``ram_gb`` is the *available* RAM (what
psutil reports free at boot) needed to run the model with headroom for the
OS and carry-ai itself. Repo IDs and filenames were verified on Hugging Face
in September 2026; none are gated. MoE models with few active parameters
(gpt-oss-20b, Gemma 4 26B-A4B, Qwen3.6-35B-A3B) are preferred at the top end
because they stay fast on CPU-only hosts.

``mmproj`` names the vision projector in the same repo. It is stored next to
the model as ``<model stem>.mmproj.gguf`` so projectors from different
repos (all called ``mmproj-F16.gguf`` upstream) never collide; llama-server
is started with ``--mmproj`` when that file is present.

Stdlib only — flash_usb.py imports this on hosts with a bare Python.
"""

MMPROJ_SUFFIX = ".mmproj.gguf"

CATALOG: list[dict] = [
    {
        "name": "Qwen3.6 35B-A3B Q8_0",
        "ram_gb": 40, "size_gb": 36.9, "context_size": 32768,
        "description": "Near-lossless flagship, MoE (3B active) — 48 GB+ hosts",
        "hf_repo": "ggml-org/Qwen3.6-35B-A3B-GGUF",
        "hf_file": "Qwen3.6-35B-A3B-Q8_0.gguf",
        "mmproj": "mmproj-Qwen3.6-35B-A3B-Q8_0.gguf",
        "match_patterns": ["qwen3.6-35b-a3b.*q8"],
        "tools": True, "gated": False,
    },
    {
        "name": "Qwen3.6 35B-A3B Q4_K_M",
        "ram_gb": 23, "size_gb": 20.4, "context_size": 32768,
        "description": "Best all-rounder for 32 GB hosts, MoE (3B active), vision",
        "hf_repo": "ggml-org/Qwen3.6-35B-A3B-GGUF",
        "hf_file": "Qwen3.6-35B-A3B-Q4_K_M.gguf",
        "mmproj": "mmproj-Qwen3.6-35B-A3B-Q8_0.gguf",
        "match_patterns": ["qwen3.6-35b-a3b"],
        "tools": True, "gated": False,
    },
    {
        "name": "Gemma 4 26B-A4B QAT",
        "ram_gb": 17, "size_gb": 14.3, "context_size": 16384,
        "description": "MoE (4B active), fast on CPU, vision — 24 GB hosts",
        "hf_repo": "unsloth/gemma-4-26B-A4B-it-qat-GGUF",
        "hf_file": "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf",
        "mmproj": "mmproj-F16.gguf",
        "match_patterns": ["gemma-4-26b-a4b"],
        "tools": True, "gated": False,
    },
    {
        "name": "gpt-oss 20B MXFP4",
        "ram_gb": 14, "size_gb": 12.1, "context_size": 16384,
        "description": "MoE (3.6B active), excellent tool calling — 16 GB+ hosts",
        "hf_repo": "ggml-org/gpt-oss-20b-GGUF",
        "hf_file": "gpt-oss-20b-MXFP4.gguf",
        "mmproj": None,
        "match_patterns": ["gpt-oss-20b"],
        "tools": True, "gated": False,
    },
    {
        "name": "Gemma 4 12B Q4_K_M",
        "ram_gb": 9, "size_gb": 7.1, "context_size": 12288,
        "description": "Strong dense assistant with vision — 12–16 GB hosts",
        "hf_repo": "unsloth/gemma-4-12b-it-GGUF",
        "hf_file": "gemma-4-12b-it-Q4_K_M.gguf",
        "mmproj": "mmproj-F16.gguf",
        "match_patterns": ["gemma-4-12b"],
        "tools": True, "gated": False,
    },
    {
        "name": "Qwen3.5 9B Q4_K_M",
        "ram_gb": 7, "size_gb": 5.7, "context_size": 8192,
        "description": "Best sub-10B agent model, vision — 8–12 GB hosts",
        "hf_repo": "unsloth/Qwen3.5-9B-GGUF",
        "hf_file": "Qwen3.5-9B-Q4_K_M.gguf",
        "mmproj": "mmproj-F16.gguf",
        "match_patterns": ["qwen3.5-9b"],
        "tools": True, "gated": False,
    },
    {
        "name": "Gemma 4 E4B QAT",
        "ram_gb": 5.5, "size_gb": 4.2, "context_size": 8192,
        "description": "Quantisation-aware 4B-class model, vision + audio",
        "hf_repo": "unsloth/gemma-4-E4B-it-qat-GGUF",
        "hf_file": "gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf",
        "mmproj": "mmproj-F16.gguf",
        "match_patterns": ["gemma-4-e4b"],
        "tools": True, "gated": False,
    },
    {
        "name": "Qwen3.5 4B Q4_K_M",
        "ram_gb": 3.5, "size_gb": 2.7, "context_size": 8192,
        "description": "Best CPU-only all-rounder, tool calling, vision",
        "hf_repo": "unsloth/Qwen3.5-4B-GGUF",
        "hf_file": "Qwen3.5-4B-Q4_K_M.gguf",
        "mmproj": "mmproj-F16.gguf",
        "match_patterns": ["qwen3.5-4b"],
        "tools": True, "gated": False,
    },
    {
        "name": "Qwen3.5 2B Q4_K_M",
        "ram_gb": 0, "size_gb": 1.3, "context_size": 4096,
        "description": "Tiny fallback — fits any machine, still calls tools",
        "hf_repo": "unsloth/Qwen3.5-2B-GGUF",
        "hf_file": "Qwen3.5-2B-Q4_K_M.gguf",
        "mmproj": "mmproj-F16.gguf",
        "match_patterns": ["qwen3.5-2b"],
        "tools": True, "gated": False,
    },
]


def mmproj_filename(model: dict) -> str | None:
    """Local filename for a model's vision projector, or None if text-only."""
    if not model.get("mmproj"):
        return None
    stem = model["hf_file"].rsplit(".gguf", 1)[0]
    return stem + MMPROJ_SUFFIX


def mmproj_url(model: dict) -> str | None:
    """Download URL for a model's vision projector, or None if text-only."""
    if not model.get("mmproj"):
        return None
    return f"https://huggingface.co/{model['hf_repo']}/resolve/main/{model['mmproj']}"


def recommend_for_ram(ram_gb: float) -> dict:
    """Best catalogue entry that fits in *ram_gb* of available RAM."""
    return next((m for m in CATALOG if m["ram_gb"] <= ram_gb), CATALOG[-1])
