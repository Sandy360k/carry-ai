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
