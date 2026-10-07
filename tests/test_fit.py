"""
Model-fit estimate (models/fit.py) and the no-admin VRAM detection
fallbacks in integrations/llmfit_advisor.py.
"""

from conftest import PROJECT_ROOT  # noqa: F401
from models.fit import estimate_fit, kv_cache_gb


def test_small_model_fits_on_cpu():
    r = estimate_fit(2.7, ram_free_gb=12, vram_gb=0)
    assert r.verdict == "fits" and r.placement == "cpu"
    assert r.label().startswith("✓")


def test_vram_extends_the_budget_and_places_on_gpu():
    # 7 GB model: too big for 6 GB free RAM alone, fits with an 12 GB GPU
    assert estimate_fit(7.1, ram_free_gb=6, vram_gb=0).verdict == "too_big"
    r = estimate_fit(7.1, ram_free_gb=6, vram_gb=12)
    assert r.ok and r.placement == "gpu"


def test_split_when_model_exceeds_vram():
    r = estimate_fit(20.4, ram_free_gb=24, vram_gb=8)
    assert r.ok and r.placement == "split"


def test_too_big_label_states_numbers():
    r = estimate_fit(36.9, ram_free_gb=14, vram_gb=0)
    assert not r.ok
    assert "needs" in r.label() and "free" in r.label()


def test_tight_band_between_comfort_and_budget():
    # budget 10; comfort 8.5; choose weights so needed ~9.2
    r = estimate_fit(7.5, ram_free_gb=10, vram_gb=0)
    assert r.verdict == "tight"


def test_kv_cache_scales_with_context():
    assert kv_cache_gb(10, 16384) == 2 * kv_cache_gb(10, 8192)


def test_linux_sysfs_vram_detection(tmp_path, monkeypatch):
    import integrations.llmfit_advisor as adv
    card = tmp_path / "card0" / "device"
    card.mkdir(parents=True)
    (card / "mem_info_vram_total").write_text(str(16 * 1024 ** 3))
    (card / "vendor").write_text("0x1002\n")
    igpu = tmp_path / "card1" / "device"          # tiny iGPU carve-out: ignored
    igpu.mkdir(parents=True)
    (igpu / "mem_info_vram_total").write_text(str(512 * 1024 ** 2))
    (igpu / "vendor").write_text("0x1002\n")

    real_path = adv.Path
    monkeypatch.setattr(adv.sys, "platform", "linux")
    monkeypatch.setattr(adv, "Path", lambda p: tmp_path if p == "/sys/class/drm" else real_path(p))
    name, gb, vendor = adv._detect_vram_linux_sysfs()
    assert vendor == "amd" and round(gb) == 16


def test_registry_detection_is_noop_off_windows(monkeypatch):
    import integrations.llmfit_advisor as adv
    monkeypatch.setattr(adv.sys, "platform", "linux")
    assert adv._detect_vram_windows_registry() is None
