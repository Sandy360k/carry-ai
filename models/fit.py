"""
carry-ai/models/fit.py — Will this GGUF run on this PC?
========================================================

One estimate shared by the Model Manager (labels + the pre-download
warning) so users see the same answer everywhere.

Memory a model needs at run time ≈ the GGUF file (weights)
                                  + KV cache for the context window
                                  + runtime overhead (llama-server buffers)
It has to fit in what is free now: available RAM plus usable dedicated
VRAM. llama-server (Vulkan build, ``-ngl auto``) puts as many layers as
fit on the GPU and keeps the rest in RAM, so both pools count.

The KV-cache figure is a rule of thumb (it really depends on layer count,
KV heads and cache quantisation, which a filename doesn't tell us); it is
scaled from the weights size so small models get small caches.

Stdlib only.
"""

from dataclasses import dataclass

# Reserved for the OS, the app itself and llama-server buffers.
RUNTIME_OVERHEAD_GB = 1.0
# VRAM the driver/desktop keeps for itself.
VRAM_RESERVE_GB = 0.5
# KV cache per GB of weights at an 8k context (fp16 cache), and a floor.
KV_GB_PER_WEIGHT_GB_AT_8K = 0.08
KV_FLOOR_GB = 0.25
# Above this share of the budget a model "fits" but will feel squeezed.
COMFORT_SHARE = 0.85


@dataclass
class FitResult:
    verdict: str      # "fits" | "tight" | "too_big"
    needed_gb: float  # estimated memory at run time
    budget_gb: float  # free RAM + usable VRAM
    placement: str    # "gpu" | "split" | "cpu"

    @property
    def ok(self) -> bool:
        return self.verdict != "too_big"

    def label(self) -> str:
        """Short text for list rows, e.g. '✓ GPU' or '✗ needs 21 GB'."""
        where = {"gpu": "GPU", "split": "GPU+RAM", "cpu": "CPU"}[self.placement]
        if self.verdict == "fits":
            return f"✓ {where}"
        if self.verdict == "tight":
            return f"~ tight ({where})"
        return f"✗ needs {self.needed_gb:.0f} GB, {self.budget_gb:.0f} free"


def kv_cache_gb(weights_gb: float, context_tokens: int = 8192) -> float:
    return max(KV_FLOOR_GB, weights_gb * KV_GB_PER_WEIGHT_GB_AT_8K) * context_tokens / 8192


def estimate_fit(weights_gb: float, ram_free_gb: float, vram_gb: float = 0.0,
                 context_tokens: int = 8192) -> FitResult:
    """Estimate whether a model of *weights_gb* runs here.

    Args:
        weights_gb: GGUF file size (plus its vision projector, if any).
        ram_free_gb: currently available system RAM.
        vram_gb: dedicated GPU memory (0 for none / integrated).
        context_tokens: context window the model will be started with.
    """
    needed = weights_gb + kv_cache_gb(weights_gb, context_tokens) + RUNTIME_OVERHEAD_GB
    usable_vram = max(0.0, vram_gb - VRAM_RESERVE_GB)
    budget = max(0.0, ram_free_gb) + usable_vram

    if needed <= budget * COMFORT_SHARE:
        verdict = "fits"
    elif needed <= budget:
        verdict = "tight"
    else:
        verdict = "too_big"

    if usable_vram > 0 and needed - RUNTIME_OVERHEAD_GB <= usable_vram:
        placement = "gpu"
    elif usable_vram > 0:
        placement = "split"
    else:
        placement = "cpu"

    return FitResult(verdict, round(needed, 1), round(budget, 1), placement)
