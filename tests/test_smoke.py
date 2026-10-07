"""
Smoke tests — fast checks that the boot path, web UI guard and safety
policy behave. Run with:  python -m pytest -q
"""

import compileall
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PROJECT_ROOT


def test_all_modules_compile():
    assert compileall.compile_dir(str(PROJECT_ROOT), quiet=1, force=True,
                                  rx=re.compile(r"[\\/](\.git|python-env|tests)[\\/]"))


def test_default_settings_are_valid():
    from config.settings import load_settings
    settings = load_settings()
    assert settings.agent.get("permission_mode") in ("ask", "yolo", "safe")


def test_headless_dry_run_exits_on_eof():
    """--no-ui must end the session when stdin closes (scripted use)."""
    result = subprocess.run(
        [sys.executable, "launcher.py", "--dry-run", "--no-ui"],
        cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "Goodbye" in result.stdout
    assert not (PROJECT_ROOT / "_dry_run_session").exists()


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "rm -rf /", "git push --force origin main", "DROP TABLE users",
])
def test_safe_mode_blocks_destructive_shell(command):
    from agent.agent import PermissionPolicy
    allowed, reason = PermissionPolicy("safe").check("shell", {"command": command})
    assert not allowed and reason


def test_safe_mode_allows_harmless_shell():
    from agent.agent import PermissionPolicy
    allowed, _ = PermissionPolicy("safe").check("shell", {"command": "ls -la"})
    assert allowed


# ---------------------------------------------------------------------------
# Web UI access guard
# ---------------------------------------------------------------------------

@pytest.fixture
def ui_client():
    pytest.importorskip("flask")
    from ui.app import create_app
    app = create_app(config={"ui_token": "s3cret", "mode": "api"})
    return app.test_client()


def test_ui_rejects_requests_without_token(ui_client):
    assert ui_client.get("/api/status").status_code == 403
    assert ui_client.get("/").status_code == 403


def test_ui_rejects_foreign_host(ui_client):
    """DNS-rebinding guard: a non-loopback Host header is refused."""
    resp = ui_client.get("/?t=s3cret", headers={"Host": "evil.example:8080"})
    assert resp.status_code == 403


def test_ui_rejects_wrong_token(ui_client):
    assert ui_client.get("/?t=wrong").status_code == 403


def test_ui_token_link_sets_cookie_and_unlocks(ui_client):
    resp = ui_client.get("/?t=s3cret")
    assert resp.status_code == 302
    assert "HttpOnly" in resp.headers["Set-Cookie"]
    assert ui_client.get("/").status_code == 200
    assert ui_client.get("/api/status").status_code == 200


# ---------------------------------------------------------------------------
# Trace-free browser launch
# ---------------------------------------------------------------------------

def test_browser_profile_lives_in_session_dir(tmp_path):
    from ui.browser import chromium_command
    argv = chromium_command("msedge", "http://127.0.0.1:8080/?t=x", tmp_path)
    assert f"--user-data-dir={tmp_path}" in argv
    assert any(a.startswith("--app=") for a in argv)


# ---------------------------------------------------------------------------
# Local model catalogue
# ---------------------------------------------------------------------------

def test_catalog_entries_are_complete_and_best_first():
    from models.catalog import CATALOG
    keys = {"name", "ram_gb", "size_gb", "hf_repo", "hf_file", "match_patterns"}
    for m in CATALOG:
        assert keys <= m.keys(), m["name"]
        assert m["hf_file"].endswith(".gguf")
        assert m["size_gb"] < max(m["ram_gb"], 2), f"{m['name']} leaves no headroom"
    rams = [m["ram_gb"] for m in CATALOG]
    assert rams == sorted(rams, reverse=True)
    assert CATALOG[-1]["ram_gb"] == 0, "need a fallback that fits any machine"


def test_every_catalog_model_matches_its_own_tier():
    from pathlib import Path
    from models.catalog import CATALOG
    from modes.local_mode import _match_path_to_tier
    for m in CATALOG:
        tier = _match_path_to_tier(Path(m["hf_file"]))
        assert tier is not None and tier.name == m["name"], m["hf_file"]


def test_mmproj_is_skipped_as_model_and_passed_to_server(tmp_path):
    from models.catalog import CATALOG, mmproj_filename
    from modes.local_mode import (LlamaServer, ServerConfig,
                                  scan_available_models)
    model = next(m for m in CATALOG if m.get("mmproj"))
    (tmp_path / model["hf_file"]).write_bytes(b"x")
    (tmp_path / mmproj_filename(model)).write_bytes(b"x")
    found = scan_available_models(tmp_path)
    assert [p.name for p in found] == [model["hf_file"]]
    cmd = LlamaServer(Path("llama-server"), ServerConfig(model_path=found[0])).build_command()
    assert "--jinja" in cmd
    assert cmd[cmd.index("--mmproj") + 1].endswith(mmproj_filename(model))


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def test_eject_poller_fires_when_usb_disappears(tmp_path, monkeypatch):
    import threading
    import launcher
    (tmp_path / "launcher.py").write_text("")
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    context = {"shutdown_event": threading.Event()}
    launcher._start_eject_poller(context, interval=0.05)
    assert not context["shutdown_event"].wait(0.2)
    (tmp_path / "launcher.py").unlink()  # "pull the USB"
    assert context["shutdown_event"].wait(2)
    assert context["ejected"] is True


def test_real_boot_plugin_and_mcp_steps_do_not_crash():
    import launcher
    assert isinstance(launcher._load_plugins(dry_run=False), list)
    assert isinstance(launcher._init_mcp(PROJECT_ROOT / "config", dry_run=False), list)


def test_cleanup_only_targets_inference_servers_by_name():
    from cleanup.cleanup import PROCESS_KILL_PATTERNS
    assert "carry-ai" not in PROCESS_KILL_PATTERNS
    assert "ai_session" not in PROCESS_KILL_PATTERNS
