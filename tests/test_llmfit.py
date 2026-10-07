"""
llmfit advisor tests — command lines, JSON parsing and unified-memory
handling in integrations/llmfit_advisor.py. No llmfit binary needed:
subprocess.run is replaced with a fake.
"""

import json
import subprocess

import integrations.llmfit_advisor as adv


class FakeRun:
    """Records argv; returns canned stdout per subcommand."""

    def __init__(self, outputs: dict[str, str], rc: int = 0):
        self.outputs = outputs
        self.rc = rc
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub not in self.outputs:
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, self.rc, self.outputs[sub], "")


def _system(**over) -> str:
    s = {
        "total_ram_gb": 32.0, "available_ram_gb": 20.5, "cpu_cores": 16,
        "cpu_name": "AMD Ryzen 9 7950X", "has_gpu": True,
        "gpu_vram_gb": 16.0, "gpu_name": "AMD Radeon RX 7800 XT",
        "unified_memory": False, "backend": "Vulkan",
        "gpus": [{"name": "AMD Radeon RX 7800 XT", "vram_gb": 16.0,
                  "backend": "Vulkan", "count": 1, "unified_memory": False}],
    }
    s.update(over)
    return json.dumps({"system": s})


def _use_llmfit(monkeypatch, fake):
    monkeypatch.setattr(adv, "LLMFIT_BINARY", "/usb/bin/llmfit")
    monkeypatch.setattr(adv, "LLMFIT_AVAILABLE", True)
    monkeypatch.setattr(adv.subprocess, "run", fake)
    monkeypatch.setattr(adv.platform, "processor", lambda: "x86_64")


def test_detect_hardware_uses_system_json(monkeypatch):
    fake = FakeRun({"system": _system()})
    _use_llmfit(monkeypatch, fake)
    hw = adv.detect_hardware()
    assert fake.calls[0] == ["/usb/bin/llmfit", "system", "--json"]
    assert hw.ram_gb == 20.5 and hw.cpu_cores == 16
    assert hw.gpu_vendor == "amd" and hw.vram_gb == 16.0
    assert not hw.unified_memory and hw.run_mode == "gpu"
    assert len(fake.calls) == 1            # no nvidia-smi/sysfs fallback


def test_unified_memory_reports_no_vram(monkeypatch):
    fake = FakeRun({"system": _system(
        gpu_name="Apple M3 Max", gpu_vram_gb=48.0, unified_memory=True,
        backend="Metal",
        gpus=[{"name": "Apple M3 Max", "vram_gb": 48.0, "backend": "Metal",
               "count": 1, "unified_memory": True}])})
    _use_llmfit(monkeypatch, fake)
    hw = adv.detect_hardware()
    assert hw.unified_memory and hw.vram_gb == 0.0
    assert hw.gpu_vendor == "apple" and hw.run_mode == "cpu"


def test_integrated_gpu_name_counts_as_unified(monkeypatch):
    # llmfit reports a plain APU iGPU with its BIOS carve-out as "VRAM".
    fake = FakeRun({"system": _system(
        gpu_name="AMD Radeon 780M Graphics", gpu_vram_gb=8.0, backend="Vulkan",
        gpus=[{"name": "AMD Radeon 780M Graphics", "vram_gb": 8.0,
               "backend": "Vulkan", "count": 1, "unified_memory": False}])})
    _use_llmfit(monkeypatch, fake)
    hw = adv.detect_hardware()
    assert hw.unified_memory and hw.vram_gb == 0.0 and hw.gpu_vendor == "amd"


def test_vendor_from_backend():
    assert adv._vendor_from("CUDA", "") == "nvidia"
    assert adv._vendor_from("ROCm", "") == "amd"
    assert adv._vendor_from("Metal", "") == "apple"
    assert adv._vendor_from("SYCL", "") == "intel"
    assert adv._vendor_from("Vulkan", "Intel Arc A770") == "intel"
    assert adv._vendor_from("Vulkan", "NVIDIA GeForce RTX 4060") == "nvidia"


def test_integrated_name_heuristic():
    assert adv._is_integrated_gpu_name("AMD Radeon(TM) Graphics")
    assert adv._is_integrated_gpu_name("AMD Radeon 890M")
    assert adv._is_integrated_gpu_name("Intel(R) UHD Graphics 770")
    assert adv._is_integrated_gpu_name("Intel(R) Graphics")
    assert not adv._is_integrated_gpu_name("Intel(R) Arc(TM) A770 Graphics")
    assert not adv._is_integrated_gpu_name("AMD Radeon RX 7900 XTX")
    assert not adv._is_integrated_gpu_name("NVIDIA GeForce RTX 4090")


def test_recommend_command_and_parsing(monkeypatch):
    out = json.dumps({"system": {}, "models": [{
        "name": "Qwen/Qwen3.5-9B", "score": 87.4,
        "score_components": {"quality": 90, "speed": 70.5, "fit": 95, "context": 80},
        "estimated_tps": 22.3, "run_mode": "gpu", "fit_level": "good",
        "fit_label": "Good", "best_quant": "Q4_K_M", "memory_required_gb": 6.1,
        "effective_context_length": 32768,
    }]})
    fake = FakeRun({"recommend": out})
    _use_llmfit(monkeypatch, fake)
    recs = adv.get_llmfit_recommendation(use_case="code", count=4)
    assert fake.calls[0] == ["/usb/bin/llmfit", "recommend", "--json",
                             "--force-runtime", "llamacpp",
                             "--use-case", "coding", "-n", "4"]
    r = recs[0]
    assert r.name == "Qwen/Qwen3.5-9B" and r.overall_score == 87.4
    assert (r.quality_score, r.speed_score, r.fit_score, r.context_score) == (90, 70.5, 95, 80)
    assert r.context_size == 32768 and r.best_quant == "Q4_K_M"
    assert r.memory_required_gb == 6.1 and r.gpu_layers == 0


def test_use_case_mapping():
    assert adv.llmfit_use_case("code") == "coding"
    assert adv.llmfit_use_case("creative") == "general"
    assert adv.llmfit_use_case("chat") == "chat"
    assert adv.llmfit_use_case("reasoning") == "reasoning"
    assert adv.llmfit_use_case("whatever") == "general"


def test_fallback_recommendation_uses_catalog(monkeypatch):
    from models.catalog import CATALOG
    monkeypatch.setattr(adv, "LLMFIT_AVAILABLE", False)
    monkeypatch.setattr(adv, "detect_hardware",
                        lambda: adv.HardwareProfile(ram_gb=10.0, cpu_cores=8))
    text = adv._tool_model_recommend("chat")
    names = {m["name"] for m in CATALOG}
    line = next(ln for ln in text.splitlines() if ln.startswith("Recommended:"))
    assert any(n in line for n in names)
    assert "Gemma 4 12B" in line                 # ram_gb 9 entry fits 10 GB


def _card(root, name, vram_bytes, vendor="0x1002", device=None):
    d = root / name / "device"
    d.mkdir(parents=True)
    (d / "mem_info_vram_total").write_text(str(vram_bytes))
    (d / "vendor").write_text(vendor + "\n")
    if device:
        (d / "device").write_text(device + "\n")


def _fake_drm(monkeypatch, tmp_path, cpu="AMD Ryzen 9 7950X"):
    real_path = adv.Path
    monkeypatch.setattr(adv.sys, "platform", "linux")
    monkeypatch.setattr(adv, "Path", lambda p: tmp_path if p == "/sys/class/drm" else real_path(p))
    monkeypatch.setattr(adv, "_cpu_brand", lambda: cpu)


def test_sysfs_apu_by_device_id_is_not_dedicated(tmp_path, monkeypatch):
    _card(tmp_path, "card0", 8 * 1024 ** 3, device="0x15bf")     # Phoenix 780M, 8 GB UMA
    _fake_drm(monkeypatch, tmp_path)
    assert adv._detect_vram_linux_sysfs() is None
    assert adv._detect_apu_linux_sysfs()[1] == "amd"


def test_sysfs_apu_by_cpu_brand(tmp_path, monkeypatch):
    _card(tmp_path, "card0", 4 * 1024 ** 3, device="0xffff")
    _fake_drm(monkeypatch, tmp_path, cpu="AMD Ryzen 7 9999HS w/ Radeon 999M Graphics")
    assert adv._detect_vram_linux_sysfs() is None
    assert adv._detect_apu_linux_sysfs() is not None


def test_sysfs_discrete_next_to_apu(tmp_path, monkeypatch):
    _card(tmp_path, "card0", 2 * 1024 ** 3, device="0x1681")     # Rembrandt iGPU
    _card(tmp_path, "card1", 12 * 1024 ** 3, device="0x73df")    # RX 6700 XT
    _fake_drm(monkeypatch, tmp_path, cpu="AMD Ryzen 7 6800H with Radeon Graphics")
    name, gb, vendor = adv._detect_vram_linux_sysfs()
    assert vendor == "amd" and round(gb) == 12


def test_detect_hardware_fallback_apu_is_unified(tmp_path, monkeypatch):
    _card(tmp_path, "card0", 16 * 1024 ** 3, device="0x1586")    # Strix Halo
    _fake_drm(monkeypatch, tmp_path)
    monkeypatch.setattr(adv, "LLMFIT_AVAILABLE", False)
    monkeypatch.setattr(adv.subprocess, "run", FakeRun({}))      # no nvidia-smi
    monkeypatch.setattr(adv.platform, "processor", lambda: "x86_64")
    hw = adv.detect_hardware()
    assert hw.has_gpu and hw.unified_memory and hw.vram_gb == 0.0


def test_windows_registry_filters_absent_adapters(monkeypatch):
    import sys
    import types

    entries = {"0000": ("NVIDIA GeForce GTX 1060 6GB", 6 * 1024 ** 3),   # removed card
               "0001": ("AMD Radeon RX 7600", 8 * 1024 ** 3),
               "0002": ("AMD Radeon(TM) Graphics", 2 * 1024 ** 3)}
    keys = list(entries)

    class Key:
        def __init__(self, sub=None):
            self.sub = sub

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_winreg = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=lambda root, sub: Key(sub if isinstance(root, Key) else None),
        EnumKey=lambda root, i: keys[i] if i < len(keys) else (_ for _ in ()).throw(OSError()),
        QueryValueEx=lambda key, name: ((entries[key.sub][1], 11) if "Memory" in name
                                        else (entries[key.sub][0], 1)),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(adv.sys, "platform", "win32")
    fake = FakeRun({"-NoProfile": "AMD Radeon RX 7600\r\nAMD Radeon(TM) Graphics\r\n"})
    monkeypatch.setattr(adv.subprocess, "run", fake)

    name, gb, vendor = adv._detect_vram_windows_registry()
    assert name == "AMD Radeon RX 7600" and vendor == "amd" and round(gb) == 8
    assert fake.calls[0][:2] == ["powershell", "-NoProfile"]
    assert "Win32_VideoController" in fake.calls[0][-1]
    assert adv._detect_integrated_windows() == ("AMD Radeon(TM) Graphics", "amd")


def test_find_llmfit_in_pip_target_bin(tmp_path, monkeypatch):
    import os
    import sys
    site = tmp_path / "site-packages"
    exe = site / "bin" / ("llmfit.exe" if sys.platform == "win32" else "llmfit")
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    monkeypatch.setattr(adv.shutil, "which", lambda name: None)
    monkeypatch.setattr(adv.sys, "path", [str(site)])
    assert adv._find_llmfit() == str(exe)
