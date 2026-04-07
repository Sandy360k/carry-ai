"""
carry-ai/integrations/llmfit_advisor.py -- Hardware-Aware Model Selection
==========================================================================

Integrates llmfit (https://github.com/AlexsJones/llmfit) for intelligent
model selection based on full hardware profiling, replacing simple
RAM-threshold matching with multi-dimensional scoring.

Features (from llmfit):
    - 4-dimensional scoring: Quality, Speed, Fit, Context
    - GPU detection: NVIDIA, AMD, Intel, Apple Silicon
    - Use-case-specific weights (chat, code, reasoning, creative)
    - Run mode classification: GPU-only, MoE offload, CPU+GPU hybrid, CPU-only
    - Memory bandwidth estimation for speed scoring
    - Database of 200+ models with metadata
    - REST API: llmfit serve (port 8787)
    - Interactive TUI with vim-style keybindings

Architecture:
    1. Try llmfit CLI/API for hardware-profiled recommendation
    2. Cross-reference against locally available .gguf files
    3. Use llmfit's fit score to set n_gpu_layers and context_size
    4. Fall back to carry-ai's static tier list if llmfit unavailable

Integration points in carry-ai:
    - modes/local_mode.py: Enhanced select_model() with GPU awareness
    - agent/tools.py: model_recommend tool for on-demand suggestions
    - launcher.py: Boot-time hardware profiling

Reference: https://github.com/AlexsJones/llmfit
"""

import json
import logging
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("carry-ai.integrations.llmfit")

# Check if llmfit binary is available
LLMFIT_BINARY = shutil.which("llmfit")
LLMFIT_AVAILABLE = LLMFIT_BINARY is not None


@dataclass
class HardwareProfile:
    """Detected hardware capabilities."""
    ram_gb: float = 0.0
    vram_gb: float = 0.0
    gpu_name: str = ""
    gpu_vendor: str = ""           # nvidia, amd, intel, apple, none
    cpu_cores: int = 0
    cpu_name: str = ""
    bandwidth_gbps: float = 0.0   # Memory bandwidth estimate
    has_gpu: bool = False
    run_mode: str = "cpu"          # cpu, gpu, hybrid, moe_offload


@dataclass
class ModelRecommendation:
    """A recommended model with scores."""
    name: str
    filename_pattern: str = ""
    quality_score: float = 0.0     # 0-100
    speed_score: float = 0.0       # 0-100
    fit_score: float = 0.0         # 0-100
    context_score: float = 0.0     # 0-100
    overall_score: float = 0.0
    run_mode: str = "cpu"
    gpu_layers: int = 0
    estimated_tps: float = 0.0     # Tokens per second
    context_size: int = 4096
    reason: str = ""


def detect_hardware() -> HardwareProfile:
    """Detect system hardware capabilities.

    Uses psutil for CPU/RAM and platform-specific methods for GPU.
    If llmfit is available, delegates to it for accurate detection.
    """
    profile = HardwareProfile()

    # CPU + RAM via psutil
    try:
        import psutil
        mem = psutil.virtual_memory()
        profile.ram_gb = mem.available / (1024 ** 3)
        profile.cpu_cores = psutil.cpu_count(logical=True) or 4
    except ImportError:
        profile.ram_gb = 4.0
        profile.cpu_cores = 4

    profile.cpu_name = platform.processor() or "unknown"

    # Try llmfit for GPU detection
    if LLMFIT_AVAILABLE:
        try:
            result = subprocess.run(
                [LLMFIT_BINARY, "info", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                info = json.loads(result.stdout)
                gpus = info.get("gpus", [])
                if gpus:
                    gpu = gpus[0]
                    profile.gpu_name = gpu.get("name", "")
                    profile.vram_gb = gpu.get("vram_gb", 0.0)
                    profile.gpu_vendor = gpu.get("vendor", "").lower()
                    profile.has_gpu = True
                    profile.bandwidth_gbps = gpu.get("bandwidth_gbps", 0.0)
                profile.ram_gb = info.get("ram_gb", profile.ram_gb)
                profile.cpu_cores = info.get("cpu_cores", profile.cpu_cores)
        except Exception as e:
            log.debug("llmfit info failed: %s", e)

    # Fallback GPU detection (NVIDIA only via nvidia-smi)
    if not profile.has_gpu:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                line = result.stdout.strip().split("\n")[0]
                parts = line.split(", ")
                if len(parts) >= 2:
                    profile.gpu_name = parts[0].strip()
                    profile.vram_gb = float(parts[1].strip()) / 1024
                    profile.gpu_vendor = "nvidia"
                    profile.has_gpu = True
        except Exception:
            pass

    # Determine run mode
    if profile.has_gpu and profile.vram_gb >= 2.0:
        profile.run_mode = "hybrid" if profile.vram_gb < 8 else "gpu"
    else:
        profile.run_mode = "cpu"

    log.info("Hardware: RAM=%.1fGB, GPU=%s (%.1fGB VRAM), mode=%s",
             profile.ram_gb, profile.gpu_name or "none",
             profile.vram_gb, profile.run_mode)

    return profile


def get_llmfit_recommendation(use_case: str = "chat",
                               runtime: str = "llamacpp",
                               count: int = 3) -> list[ModelRecommendation]:
    """Get model recommendations from llmfit CLI.

    Args:
        use_case: Task type ("chat", "code", "reasoning", "creative").
        runtime: Target runtime ("llamacpp", "ollama", "mlx").
        count: Number of recommendations to return.

    Returns:
        List of ModelRecommendation objects, or empty if llmfit unavailable.
    """
    if not LLMFIT_AVAILABLE:
        return []

    try:
        cmd = [
            LLMFIT_BINARY, "fit",
            "--cli",
            "--force-runtime", runtime,
            "--use-case", use_case,
            "-n", str(count),
            "--json",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.warning("llmfit fit failed: %s", result.stderr[:200])
            return []

        data = json.loads(result.stdout)
        recs = []
        for item in data.get("recommendations", data) if isinstance(data, dict) else data:
            rec = ModelRecommendation(
                name=item.get("name", item.get("model", "")),
                quality_score=item.get("quality", 0),
                speed_score=item.get("speed", 0),
                fit_score=item.get("fit", 0),
                context_score=item.get("context", 0),
                overall_score=item.get("overall", item.get("score", 0)),
                run_mode=item.get("run_mode", "cpu"),
                gpu_layers=item.get("gpu_layers", 0),
                estimated_tps=item.get("estimated_tps", 0),
                context_size=item.get("context_size", 4096),
                reason=item.get("reason", ""),
            )
            recs.append(rec)

        return recs

    except json.JSONDecodeError:
        log.warning("llmfit returned non-JSON output")
        return []
    except Exception as e:
        log.warning("llmfit recommendation failed: %s", e)
        return []


def compute_gpu_layers(model_size_gb: float, vram_gb: float,
                        total_layers: int = 32) -> int:
    """Estimate optimal number of GPU layers to offload.

    Uses a simple heuristic: each layer uses roughly
    model_size_gb / total_layers of VRAM. Reserve ~500MB for KV cache.

    Args:
        model_size_gb: Model file size in GB.
        vram_gb: Available VRAM in GB.
        total_layers: Total transformer layers in the model.

    Returns:
        Recommended number of layers to offload (0 = CPU only).
    """
    if vram_gb < 1.0 or model_size_gb <= 0:
        return 0

    usable_vram = max(0, vram_gb - 0.5)  # Reserve 500MB
    per_layer_gb = model_size_gb / total_layers
    layers = int(usable_vram / per_layer_gb)
    return min(layers, total_layers)


def estimate_speed(model_size_gb: float, bandwidth_gbps: float,
                    is_gpu: bool = False) -> float:
    """Estimate tokens per second.

    Uses llmfit's formula: (bandwidth / model_size) * 0.55 efficiency.

    Args:
        model_size_gb: Model file size.
        bandwidth_gbps: Memory bandwidth in GB/s.
        is_gpu: Whether running on GPU (higher bandwidth).

    Returns:
        Estimated tokens per second.
    """
    if model_size_gb <= 0 or bandwidth_gbps <= 0:
        # Default estimates
        if is_gpu:
            bandwidth_gbps = 300  # Rough GPU default
        else:
            bandwidth_gbps = 30   # Rough DDR4/5 default

    efficiency = 0.55  # llmfit's efficiency factor
    tps = (bandwidth_gbps / model_size_gb) * efficiency
    return round(tps, 1)


def enhanced_select_model(available_models: list[Path],
                           hardware: HardwareProfile | None = None,
                           use_case: str = "chat") -> dict | None:
    """Enhanced model selection using llmfit + hardware profiling.

    Combines llmfit's multi-dimensional scoring with carry-ai's local
    model inventory for the best available match.

    Args:
        available_models: List of .gguf file paths on the USB.
        hardware: Pre-detected hardware profile (auto-detects if None).
        use_case: Intended task ("chat", "code", "reasoning", "creative").

    Returns:
        Dict with: model_path, gpu_layers, context_size, estimated_tps,
        scores, reason. Or None if no models available.
    """
    if not available_models:
        return None

    if hardware is None:
        hardware = detect_hardware()

    # Try llmfit first for smart recommendation
    llmfit_recs = get_llmfit_recommendation(use_case=use_case)

    if llmfit_recs:
        # Cross-reference llmfit recommendations against available models
        for rec in llmfit_recs:
            rec_name = rec.name.lower().replace(" ", "").replace("-", "")
            for model_path in available_models:
                model_stem = model_path.stem.lower().replace("-", "").replace("_", "")
                if rec_name in model_stem or model_stem in rec_name:
                    model_size_gb = model_path.stat().st_size / (1024 ** 3)
                    gpu_layers = rec.gpu_layers or compute_gpu_layers(
                        model_size_gb, hardware.vram_gb
                    )
                    return {
                        "model_path": model_path,
                        "gpu_layers": gpu_layers,
                        "context_size": rec.context_size,
                        "estimated_tps": rec.estimated_tps or estimate_speed(
                            model_size_gb,
                            hardware.bandwidth_gbps,
                            is_gpu=hardware.has_gpu,
                        ),
                        "scores": {
                            "quality": rec.quality_score,
                            "speed": rec.speed_score,
                            "fit": rec.fit_score,
                            "context": rec.context_score,
                            "overall": rec.overall_score,
                        },
                        "run_mode": rec.run_mode,
                        "reason": rec.reason or f"llmfit recommended (score={rec.overall_score})",
                        "source": "llmfit",
                    }

    # Fallback: Simple GPU-aware selection (no llmfit)
    # Pick the largest model that fits in available RAM + VRAM
    total_memory = hardware.ram_gb + hardware.vram_gb
    best_model = None
    best_size = 0

    for model_path in available_models:
        size_gb = model_path.stat().st_size / (1024 ** 3)
        # Model needs ~1.2x its file size in memory (KV cache overhead)
        if size_gb * 1.2 <= total_memory and size_gb > best_size:
            best_model = model_path
            best_size = size_gb

    if best_model is None:
        # Last resort: smallest model
        best_model = min(available_models, key=lambda p: p.stat().st_size)
        best_size = best_model.stat().st_size / (1024 ** 3)

    gpu_layers = compute_gpu_layers(best_size, hardware.vram_gb)

    return {
        "model_path": best_model,
        "gpu_layers": gpu_layers,
        "context_size": 4096 if best_size < 2 else 8192 if best_size < 8 else 16384,
        "estimated_tps": estimate_speed(
            best_size, hardware.bandwidth_gbps, is_gpu=hardware.has_gpu
        ),
        "scores": {},
        "run_mode": hardware.run_mode,
        "reason": f"Best fit for {total_memory:.0f}GB total memory (RAM+VRAM)",
        "source": "carry-ai-fallback",
    }


def _tool_model_recommend(use_case: str = "chat") -> str:
    """Get hardware-aware model recommendations for this machine.

    Uses llmfit (if available) for multi-dimensional scoring across
    Quality, Speed, Fit, and Context. Falls back to basic hardware
    profiling.

    Args:
        use_case: Task type - "chat", "code", "reasoning", or "creative".
    """
    hw = detect_hardware()
    lines = [
        "## Hardware Profile",
        f"- **RAM:** {hw.ram_gb:.1f} GB available",
        f"- **CPU:** {hw.cpu_name} ({hw.cpu_cores} cores)",
    ]
    if hw.has_gpu:
        lines.append(f"- **GPU:** {hw.gpu_name} ({hw.vram_gb:.1f} GB VRAM)")
        lines.append(f"- **Run mode:** {hw.run_mode}")
    else:
        lines.append("- **GPU:** None detected (CPU-only mode)")

    lines.append(f"\n## Recommendations (use_case={use_case})")

    recs = get_llmfit_recommendation(use_case=use_case, count=5)
    if recs:
        lines.append("*(via llmfit hardware-aware scoring)*\n")
        for i, rec in enumerate(recs, 1):
            lines.append(f"**{i}. {rec.name}** (score: {rec.overall_score:.0f}/100)")
            lines.append(f"   Quality={rec.quality_score:.0f} Speed={rec.speed_score:.0f} "
                         f"Fit={rec.fit_score:.0f} Context={rec.context_score:.0f}")
            lines.append(f"   GPU layers: {rec.gpu_layers}, ~{rec.estimated_tps:.0f} tok/s, "
                         f"ctx={rec.context_size}")
            if rec.reason:
                lines.append(f"   {rec.reason}")
            lines.append("")
    else:
        lines.append("*(llmfit not available -- basic RAM-based suggestion)*\n")
        total = hw.ram_gb + hw.vram_gb
        if total >= 32:
            lines.append("Recommended: Qwen3 30B Q6_K or Llama 4 Scout 17B Q6_K")
        elif total >= 16:
            lines.append("Recommended: Gemma 4 12B Q4_K_M or Qwen3 14B Q4_K_M")
        elif total >= 8:
            lines.append("Recommended: Qwen3 8B Q4_K_M or Qwen3 8B Q8_0")
        elif total >= 4:
            lines.append("Recommended: Phi-4-mini Q4_K_M or Qwen3.5 4B Q4_K_M")
        else:
            lines.append("Recommended: Gemma 3 1B Q4_K_M (emergency fallback)")

        if hw.has_gpu:
            lines.append(f"\nWith {hw.vram_gb:.1f}GB VRAM, partial GPU offload is possible.")

    return "\n".join(lines)


def register_llmfit_tools():
    """Register llmfit tools in the agent tool registry."""
    from agent.tools import register_tool

    register_tool(
        name="model_recommend",
        description=(
            "Get hardware-aware LLM model recommendations for this machine. "
            "Uses llmfit (if installed) for multi-dimensional scoring across "
            "Quality, Speed, Fit, and Context. Detects GPU for optimal offloading."
        ),
        parameters={
            "type": "object",
            "properties": {
                "use_case": {
                    "type": "string",
                    "enum": ["chat", "code", "reasoning", "creative"],
                    "description": "Intended use case",
                    "default": "chat",
                },
            },
        },
        execute_fn=_tool_model_recommend,
    )

    log.info("Registered llmfit tools (llmfit_available=%s)", LLMFIT_AVAILABLE)
