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

Install: ``pip install llmfit`` (PyPI ships the native binary in
platform wheels; with ``pip install --target`` it lands in ``<target>/bin``,
which _find_llmfit() checks). CLI used (llmfit 1.1.x):
    llmfit system --json                      -> {"system": {...}}
    llmfit recommend --json --force-runtime llamacpp --use-case <cat> -n N
                                              -> {"system": {...}, "models": [...]}

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
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("carry-ai.integrations.llmfit")

# Hide the console window of helper processes on Windows (no flashing cmd box).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _find_llmfit() -> str | None:
    """Locate the llmfit binary.

    ``pip install llmfit`` ships a native binary as a wheel script. With
    ``pip install --target`` (how flash_usb.py/setup_usb.py install onto the
    USB) it lands in ``<target>/bin`` (``Scripts`` on Windows), which is not on
    PATH, so look next to every sys.path entry as well.
    """
    found = shutil.which("llmfit")
    if found:
        return found
    exe = "llmfit.exe" if sys.platform == "win32" else "llmfit"
    for entry in sys.path:
        if not entry:
            continue
        for sub in ("bin", "Scripts"):
            cand = Path(entry) / sub / exe
            try:
                if cand.is_file() and (sys.platform == "win32" or os.access(cand, os.X_OK)):
                    return str(cand)
            except OSError:
                continue
    return None


LLMFIT_BINARY = _find_llmfit()
LLMFIT_AVAILABLE = LLMFIT_BINARY is not None


@dataclass
class HardwareProfile:
    """Detected hardware capabilities.

    ``vram_gb`` is *dedicated* VRAM only. On unified-memory machines (Apple
    Silicon, AMD APUs, NVIDIA Grace) the GPU shares system RAM, so vram_gb
    is 0.0 and ``unified_memory`` is True — models/fit.py adds RAM + VRAM,
    and counting the shared pool twice would over-recommend.
    """
    ram_gb: float = 0.0
    vram_gb: float = 0.0
    gpu_name: str = ""
    gpu_vendor: str = ""           # nvidia, amd, intel, apple, none
    cpu_cores: int = 0
    cpu_name: str = ""
    bandwidth_gbps: float = 0.0   # Memory bandwidth estimate
    has_gpu: bool = False
    run_mode: str = "cpu"          # cpu, gpu, hybrid, moe_offload
    unified_memory: bool = False


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
    gpu_layers: int = 0            # llmfit doesn't report this; compute_gpu_layers() fills it
    estimated_tps: float = 0.0     # Tokens per second
    context_size: int = 4096
    reason: str = ""
    fit_level: str = ""
    best_quant: str = ""
    memory_required_gb: float = 0.0


# Below this, a "GPU" is an integrated chip sharing system RAM; offloading to
# it gains little, so treat it as no dedicated VRAM.
_MIN_DEDICATED_VRAM_GB = 1.0

_PCI_VENDORS = {"0x1002": "amd", "0x10de": "nvidia", "0x8086": "intel"}

# PCI device IDs of AMD APU integrated GPUs (amdgpu/Mesa ID tables). On these
# mem_info_vram_total is the BIOS UMA carve-out taken *out of* system RAM, not
# dedicated memory (llmfit issues #810/#964).
_AMD_APU_DEVICE_IDS = frozenset({
    "0x9874",                      # Carrizo / Bristol Ridge
    "0x98e4",                      # Stoney Ridge
    "0x15dd", "0x15d8",            # Raven Ridge / Picasso
    "0x1636", "0x1638", "0x164c",  # Renoir / Cezanne / Lucienne
    "0x15e7",                      # Barcelo
    "0x163f",                      # Van Gogh (Steam Deck)
    "0x1681",                      # Rembrandt (680M/660M)
    "0x164e",                      # Raphael (Ryzen 7000 desktop iGPU)
    "0x13c0",                      # Granite Ridge (Ryzen 9000 desktop iGPU)
    "0x1506",                      # Mendocino
    "0x15bf", "0x15c8",            # Phoenix (780M/760M, 740M)
    "0x1900", "0x1901",            # Hawk Point / Phoenix refresh
    "0x150e",                      # Strix Point (890M/880M)
    "0x1114",                      # Krackan Point
    "0x1586",                      # Strix Halo (Ryzen AI MAX, 8060S/8050S)
})


def _is_amd_mobile_igpu_name(name: str) -> bool:
    """"Radeon 780M Graphics" style names (3 digits + M, no RX) — llmfit's
    is_amd_mobile_igpu_name; no discrete card uses that shape."""
    low = name.lower()
    if "radeon" not in low or "rx" in low:
        return False
    tokens = "".join(c if c.isalnum() else " " for c in low).split()
    return any(len(t) == 4 and t.endswith("m") and t[:3].isdigit() for t in tokens)


def _has_intel_arc_product_code(low: str) -> bool:
    """Discrete Intel Arc product code (A770, B580, A370M…); iGPUs carry none."""
    tokens = "".join(c if c.isalnum() else " " for c in low).split()
    for tok in tokens:
        if not 3 <= len(tok) <= 5:
            continue
        code = tok[:-1] if tok.endswith("m") else tok
        if len(code) >= 2 and code[0] in "ab" and code[1:].isdigit():
            return True
    return False


def _is_integrated_gpu_name(name: str) -> bool:
    """Name heuristic for integrated GPUs (shares system RAM).

    Mirrors llmfit's ``is_integrated_gpu_name`` (llmfit-core/src/hardware.rs):
    explicit "(integrated)", AMD mobile iGPU names, Intel GPUs without an Arc
    discrete product code, and "Radeon (TM) Graphics" without a discrete
    series tag (RX/PRO/Vega/VII/W).
    """
    low = name.lower()
    if "(integrated)" in low or _is_amd_mobile_igpu_name(name):
        return True
    if "intel" in low:
        return not _has_intel_arc_product_code(low)
    if "radeon" in low and "graphics" in low:
        return not any(tag in low for tag in ("rx ", "pro ", "vega", " vii", " w"))
    return False


def _vendor_from(backend: str, name: str) -> str:
    """Derive a vendor from llmfit's backend label, falling back to the name."""
    b, low = backend.lower(), name.lower()
    if "cuda" in b:
        return "nvidia"
    if "rocm" in b:
        return "amd"
    if "metal" in b:
        return "apple"
    if "sycl" in b:
        return "intel"
    if "nvidia" in low or "geforce" in low or "quadro" in low or "tesla" in low:
        return "nvidia"
    if "amd" in low or "radeon" in low or "instinct" in low:
        return "amd"
    if "intel" in low or "arc" in low.split():
        return "intel"
    if "apple" in low:
        return "apple"
    return "unknown" if name else ""


def _cpu_brand() -> str:
    """CPU brand string ("AMD Ryzen 7 8845HS w/ Radeon 780M Graphics")."""
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or ""


def _is_amd_apu_cpu(cpu_name: str) -> bool:
    """llmfit's is_amd_apu: Ryzen + Radeon in the CPU brand means an iGPU."""
    low = cpu_name.lower()
    return "ryzen" in low and "radeon" in low


def _linux_amd_cards() -> list[dict]:
    """AMD cards under /sys/class/drm with their VRAM and an APU verdict.

    A card is an APU iGPU when its PCI device ID is in the APU list, or —
    as llmfit does via the CPU brand — when the CPU is a Ryzen-with-Radeon
    and this is the machine's only AMD card (an APU + discrete RX laptop
    exposes two cards; the iGPU one is then caught by the ID list).
    """
    cards = []
    for vram_file in sorted(Path("/sys/class/drm").glob("card*/device/mem_info_vram_total")):
        if "-" in vram_file.parent.parent.name:         # cardN-DP-1 connectors
            continue
        dev = vram_file.parent
        try:
            gb = int(vram_file.read_text().strip()) / (1024 ** 3)
            vendor_id = (dev / "vendor").read_text().strip()
        except (OSError, ValueError):
            continue
        try:
            device_id = (dev / "device").read_text().strip().lower()
        except OSError:
            device_id = ""
        cards.append({"vendor": _PCI_VENDORS.get(vendor_id, "unknown"),
                      "gb": gb, "device_id": device_id,
                      "apu": device_id in _AMD_APU_DEVICE_IDS})
    amd = [c for c in cards if c["vendor"] == "amd"]
    if len(amd) == 1 and not amd[0]["apu"] and _is_amd_apu_cpu(_cpu_brand()):
        amd[0]["apu"] = True
    return cards


def _detect_vram_linux_sysfs() -> tuple[str, float, str] | None:
    """Largest *dedicated* VRAM from /sys/class/drm (amdgpu exposes
    mem_info_vram_total; works for any user, no tools needed).

    APU iGPUs are skipped: their figure is a carve-out of system RAM.
    """
    if not sys.platform.startswith("linux"):
        return None
    best = None
    for card in _linux_amd_cards():
        if card["apu"]:
            continue
        gb, vendor = card["gb"], card["vendor"]
        if gb >= _MIN_DEDICATED_VRAM_GB and (best is None or gb > best[1]):
            best = (f"{vendor.upper()} GPU", gb, vendor)
    return best


def _detect_apu_linux_sysfs() -> tuple[str, str] | None:
    """(name, vendor) of an AMD APU iGPU on Linux, or None."""
    if not sys.platform.startswith("linux"):
        return None
    if any(c["apu"] for c in _linux_amd_cards()):
        cpu = _cpu_brand()
        return (f"{cpu} (integrated)" if cpu else "AMD Radeon (integrated)", "amd")
    return None


def _present_windows_adapters() -> set[str] | None:
    """Lower-cased names of the display adapters present right now, or None
    if the query failed (callers then accept every registry entry)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select -Expand Name"],
            capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    names = {ln.strip().lower() for ln in result.stdout.splitlines() if ln.strip()}
    return names or None


def _windows_registry_adapters() -> list[tuple[str, float]]:
    """(DriverDesc, GB) for present display adapters in the class key.

    HardwareInformation.qwMemorySize is a 64-bit value (WMI's AdapterRAM
    caps at 4 GB) and the key is readable without admin. The class key keeps
    entries for adapters that were removed, so only names matching a
    currently present Win32_VideoController are kept.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    base = (r"SYSTEM\CurrentControlSet\Control\Class"
            r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
    entries: list[tuple[str, float]] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    with root:
        for i in range(64):
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            try:
                with winreg.OpenKey(root, sub) as key:
                    size, _ = winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")
                    name, _ = winreg.QueryValueEx(key, "DriverDesc")
            except OSError:
                continue
            if isinstance(size, bytes):
                size = int.from_bytes(size[:8], "little")
            entries.append((str(name), int(size) / (1024 ** 3)))
    present = _present_windows_adapters()
    if present is not None:
        entries = [e for e in entries if e[0].strip().lower() in present]
    return entries


def _name_vendor(name: str) -> str:
    low = name.lower()
    return next((v for v in ("nvidia", "amd", "intel")
                 if v in low or (v == "amd" and "radeon" in low)), "unknown")


def _detect_vram_windows_registry() -> tuple[str, float, str] | None:
    """Largest dedicated VRAM among present, non-integrated adapters."""
    if sys.platform != "win32":
        return None
    best = None
    for name, gb in _windows_registry_adapters():
        if _is_integrated_gpu_name(name):
            continue
        if gb >= _MIN_DEDICATED_VRAM_GB and (best is None or gb > best[1]):
            best = (name, gb, _name_vendor(name))
    return best


def _detect_integrated_windows() -> tuple[str, str] | None:
    """(name, vendor) of a present integrated adapter on Windows, or None."""
    if sys.platform != "win32":
        return None
    for name, _gb in _windows_registry_adapters():
        if _is_integrated_gpu_name(name):
            return name, _name_vendor(name)
    return None


def _apply_llmfit_system(profile: HardwareProfile, s: dict) -> None:
    """Fill *profile* from the "system" object of ``llmfit system --json``."""
    if s.get("available_ram_gb"):
        profile.ram_gb = float(s["available_ram_gb"])
    if s.get("cpu_cores"):
        profile.cpu_cores = int(s["cpu_cores"])
    if s.get("cpu_name"):
        profile.cpu_name = str(s["cpu_name"])
    if not s.get("has_gpu"):
        return
    gpus = s.get("gpus") or []
    primary = gpus[0] if gpus else {}
    name = str(s.get("gpu_name") or primary.get("name") or "")
    backend = str(s.get("backend") or primary.get("backend") or "")
    vram = s.get("gpu_vram_gb")
    if vram is None:
        vram = primary.get("vram_gb")
    unified = bool(s.get("unified_memory") or primary.get("unified_memory")
                   or backend.lower() == "metal" or _is_integrated_gpu_name(name))
    profile.has_gpu = True
    profile.gpu_name = name
    profile.gpu_vendor = _vendor_from(backend, name)
    profile.unified_memory = unified
    profile.vram_gb = 0.0 if unified else float(vram or 0.0)
    bw = primary.get("memory_bandwidth_gbps")
    if isinstance(bw, (int, float)):
        profile.bandwidth_gbps = float(bw)


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

    # llmfit's own detection (`llmfit system --json`; `info` needs a model).
    llmfit_ok = False
    if LLMFIT_AVAILABLE:
        try:
            result = subprocess.run(
                [LLMFIT_BINARY, "system", "--json"],
                capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW,
            )
            if result.returncode == 0:
                _apply_llmfit_system(profile, json.loads(result.stdout)["system"])
                llmfit_ok = True
            else:
                log.debug("llmfit system failed: %s", result.stderr[:200])
        except Exception as e:
            log.debug("llmfit system failed: %s", e)

    # Fallback GPU detection, no admin needed. NVIDIA via nvidia-smi first,
    # then vendor-neutral sources so AMD/Intel cards (which the bundled
    # Vulkan llama-server can use) are counted too.
    if not llmfit_ok and not profile.has_gpu:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW,
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

    if not llmfit_ok and not profile.has_gpu:
        found = _detect_vram_linux_sysfs() or _detect_vram_windows_registry()
        if found:
            profile.gpu_name, profile.vram_gb, profile.gpu_vendor = found
            profile.has_gpu = True
        else:
            igpu = _detect_apu_linux_sysfs() or _detect_integrated_windows()
            if igpu:
                profile.gpu_name, profile.gpu_vendor = igpu
                profile.has_gpu = True
                profile.unified_memory = True
                profile.vram_gb = 0.0

    # Determine run mode
    if profile.has_gpu and profile.vram_gb >= 2.0:
        profile.run_mode = "hybrid" if profile.vram_gb < 8 else "gpu"
    else:
        profile.run_mode = "cpu"

    log.info("Hardware: RAM=%.1fGB, GPU=%s (%.1fGB VRAM%s), mode=%s",
             profile.ram_gb, profile.gpu_name or "none", profile.vram_gb,
             ", unified" if profile.unified_memory else "", profile.run_mode)

    return profile


# carry-ai use cases -> llmfit --use-case categories
# (general, coding, reasoning, chat, multimodal, embedding).
_USE_CASE_MAP = {
    "code": "coding", "coding": "coding",
    "creative": "general", "general": "general",
    "chat": "chat",
    "reasoning": "reasoning",
}


def llmfit_use_case(use_case: str) -> str:
    """Map a carry-ai use case onto llmfit's category names."""
    return _USE_CASE_MAP.get((use_case or "").lower(), "general")


def get_llmfit_recommendation(use_case: str = "chat",
                               runtime: str = "llamacpp",
                               count: int = 3) -> list[ModelRecommendation]:
    """Get model recommendations from ``llmfit recommend --json``.

    Args:
        use_case: Task type ("chat", "code", "reasoning", "creative").
        runtime: Runtime forced via --force-runtime (carry-ai runs llama.cpp).
        count: Number of recommendations to return.

    Returns:
        List of ModelRecommendation objects, or empty if llmfit unavailable.
    """
    if not LLMFIT_AVAILABLE:
        return []

    try:
        cmd = [
            LLMFIT_BINARY, "recommend", "--json",
            "--force-runtime", runtime,
            "--use-case", llmfit_use_case(use_case),
            "-n", str(count),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                creationflags=_NO_WINDOW)
        if result.returncode != 0:
            log.warning("llmfit recommend failed: %s", result.stderr[:200])
            return []

        data = json.loads(result.stdout)
        recs = []
        for item in data.get("models", []) if isinstance(data, dict) else []:
            comps = item.get("score_components") or {}
            fit_level = str(item.get("fit_label") or item.get("fit_level") or "")
            rec = ModelRecommendation(
                name=str(item.get("name", "")),
                quality_score=float(comps.get("quality") or 0),
                speed_score=float(comps.get("speed") or 0),
                fit_score=float(comps.get("fit") or 0),
                context_score=float(comps.get("context") or 0),
                overall_score=float(item.get("score") or 0),
                run_mode=str(item.get("run_mode") or "cpu"),
                estimated_tps=float(item.get("estimated_tps") or 0),
                context_size=int(item.get("effective_context_length") or 4096),
                fit_level=fit_level,
                best_quant=str(item.get("best_quant") or ""),
                memory_required_gb=float(item.get("memory_required_gb") or 0),
            )
            bits = [b for b in (fit_level and f"{fit_level} fit",
                                rec.best_quant,
                                rec.memory_required_gb and f"{rec.memory_required_gb:.1f} GB")
                    if b]
            rec.reason = ", ".join(bits)
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
        if hw.unified_memory:
            lines.append(f"- **GPU:** {hw.gpu_name} (unified memory, shares system RAM)")
        else:
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
            lines.append(f"   Run mode: {rec.run_mode}, ~{rec.estimated_tps:.0f} tok/s, "
                         f"ctx={rec.context_size}")
            if rec.reason:
                lines.append(f"   {rec.reason}")
            lines.append("")
    else:
        lines.append("*(llmfit not available -- basic RAM-based suggestion)*\n")
        total = hw.ram_gb + hw.vram_gb
        try:
            from models.catalog import CATALOG, recommend_for_ram
            best = recommend_for_ram(total)
            idx = CATALOG.index(best)
            picks = [best] + CATALOG[idx + 1:idx + 2]
            lines.append("Recommended: " + " or ".join(m["name"] for m in picks))
            lines.append(f"   {best['description']}")
        except Exception as e:     # catalogue missing on a partial install
            log.debug("catalog lookup failed: %s", e)
            lines.append("Recommended: run `python models/downloader.py suggest`")

        if hw.has_gpu and hw.vram_gb >= _MIN_DEDICATED_VRAM_GB:
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
