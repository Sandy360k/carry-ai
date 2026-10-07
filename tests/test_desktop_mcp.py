"""
Default desktop-control MCP server (mcp/defaults.py) and its packaging
(portable/runtime.py): when it's offered, how it's launched, what's
excluded, and noexec staging. No network, no real server.
"""

import os
from pathlib import Path

import pytest

from conftest import PROJECT_ROOT  # noqa: F401
import mcp.defaults as d
from portable import runtime as r


def _linux_usb(tmp_path, arch="x86_64"):
    binary = r.desktop_linux_binary(tmp_path, arch)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o755)
    return binary


def test_nothing_offered_when_not_on_the_usb(tmp_path, monkeypatch):
    assert d.linux_server(tmp_path) is None
    assert d.windows_server(tmp_path) is None


def test_linux_server_config(tmp_path, monkeypatch):
    binary = _linux_usb(tmp_path)
    monkeypatch.setattr(d.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(r, "glibc_version", lambda: (2, 39))
    cfg = d.linux_server(tmp_path)
    assert cfg.name == "desktop" and cfg.command == str(binary) and cfg.args == ["mcp"]
    # setup_accessibility stays (its setting is restored by cleanup)
    assert cfg.exclude_tools == ["setup_window_targeting"]


@pytest.mark.parametrize("glibc", [(2, 35), None])
def test_linux_server_needs_new_glibc(tmp_path, monkeypatch, glibc):
    _linux_usb(tmp_path)
    monkeypatch.setattr(d.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(r, "glibc_version", lambda: glibc)
    assert d.linux_server(tmp_path) is None


def test_windows_server_is_isolated_and_quiet(tmp_path):
    (r.server_dir(tmp_path, "windows", "windows-mcp") / "windows_mcp").mkdir(parents=True)
    cfg = d.windows_server(tmp_path)
    assert cfg.args[:2] == ["-I", "-S"]              # none of carry-ai's packages
    assert cfg.args[-3:] == ["serve", "--transport", "stdio"]
    assert "--exclude-tools" not in cfg.args and cfg.exclude_tools == []   # every tool
    assert cfg.env["ANONYMIZED_TELEMETRY"] == "false" and cfg.env["POSTHOG_API_KEY"] == ""


def test_windows_toasts_are_tagged_for_cleanup(tmp_path):
    """The boot code puts every toast in group carry-ai before it is shown."""
    import subprocess
    import sys
    srv = tmp_path / "srv"
    (srv / "windows_mcp" / "notifications").mkdir(parents=True)
    (srv / "windows_mcp" / "__init__.py").write_text("")
    (srv / "windows_mcp" / "notifications" / "__init__.py").write_text("")
    (srv / "windows_mcp" / "notifications" / "service.py").write_text(
        "class PowerShellExecutor:\n"
        "    @staticmethod\n"
        "    def execute_command(script, shell=None):\n"
        "        print(script)\n"
        "        return '', 0\n"
        "def send(app):\n"
        "    return PowerShellExecutor.execute_command('$toast = 1\\n$notifier.Show($toast)')\n")
    (srv / "windows_mcp" / "__main__.py").write_text(
        "import windows_mcp.notifications.service as s\n"
        "s.PowerShellExecutor.execute_command('$toast = 1\\n$notifier.Show($toast)')\n")
    out = subprocess.run([sys.executable, "-I", "-S", "-c", d._WINDOWS_BOOT, str(srv), "serve"],
                         capture_output=True, text=True).stdout.splitlines()
    assert out[0] == "$toast = 1"
    assert out[1] == f"$toast.Group = '{d.TOAST_GROUP}'"
    assert out[2].startswith("$toast.Tag = ") and out[3] == "$notifier.Show($toast)"


def test_toast_app_ids_are_recorded_and_cleaned(monkeypatch):
    from cleanup import cleanup as c
    monkeypatch.setattr(c, "_TOAST_APPS", set())
    d.on_tool_call("desktop", "Click", {"app_id": "x"})            # other tools: ignored
    d.on_tool_call("desktop", "Notification", {"app_id": "O'Brien.App"})
    assert c._TOAST_APPS == {"O'Brien.App"}
    ran = []
    monkeypatch.setattr(c.sys, "platform", "win32")
    monkeypatch.setattr(c.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    assert c.remove_toasts() == 1
    script = ran[0][-1]
    assert "History.RemoveGroup('carry-ai', 'O''Brien.App')" in script   # only our group
    assert ran[0][0] == "powershell" and "-NoProfile" in ran[0]
    assert c.TOAST_GROUP == d.TOAST_GROUP


def test_desktop_control_switch(monkeypatch):
    monkeypatch.setattr(d, "_enabled", lambda: False)
    assert d.default_servers() == []


def test_isolated_launch_sees_only_its_folder(tmp_path):
    """-I -S + addsitedir: the server's .pth files work, carry-ai's mcp/ can't shadow."""
    import subprocess
    import sys
    srv = tmp_path / "srv"
    (srv / "windows_mcp").mkdir(parents=True)
    (srv / "extra").mkdir()
    (srv / "fake.pth").write_text("extra\n")
    (srv / "extra" / "pthmod.py").write_text("X = 1\n")
    (srv / "windows_mcp" / "__init__.py").write_text("")
    (srv / "windows_mcp" / "__main__.py").write_text(
        "import sys, pthmod\nprint(sys.argv[1:], pthmod.X)\n"
        "import importlib.util\nprint(importlib.util.find_spec('mcp'))\n")
    out = subprocess.run(
        [sys.executable, "-I", "-S", "-c", d._WINDOWS_BOOT, str(srv), "serve", "--x"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}).stdout.splitlines()
    assert out == ["['serve', '--x'] 1", "None"]


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def test_linux_install_verifies_and_places_binaries(tmp_path, monkeypatch):
    fetched = []

    def fake_download(url, dest, sha256=None, progress=None):
        fetched.append((url, sha256))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"bin")
        return Path(dest)

    monkeypatch.setattr(r, "download", fake_download)
    monkeypatch.setattr(r, "sha256_file", lambda p: "x")
    written = r.install_desktop_control(tmp_path / "usb", "linux", tmp_path / "cache")
    assert [p.parent.name for p in written] == ["linux-x86_64", "linux-aarch64"]
    assert all(sha and url.endswith(name) for (url, sha), (name, _) in
               zip(fetched, r.DESKTOP_LINUX_ASSETS.values()))
    assert all(p.read_bytes() == b"bin" for p in written)


def test_windows_install_goes_to_its_own_folder(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(r.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or
                        r.subprocess.CompletedProcess(cmd, 0, "", ""))
    (target,) = r.install_desktop_control(tmp_path, "windows", tmp_path / "cache")
    assert target == r.server_dir(tmp_path, "windows", "windows-mcp")
    assert target != r.site_packages(tmp_path, "windows")
    cmd = ran[0]
    assert cmd[cmd.index("--target") + 1] == str(target)
    assert cmd[-3:] == r.WINDOWS_MCP_REQUIREMENTS                # one resolve, pinned
    r.install_desktop_control(tmp_path, "windows", tmp_path / "cache")
    assert len(ran) == 1                                         # idempotent


@pytest.mark.skipif(os.name == "nt", reason="noexec is a Linux mount option")
def test_ensure_executable_stages_on_noexec(tmp_path, monkeypatch):
    folder = tmp_path / "usb" / "llama"
    folder.mkdir(parents=True)
    (folder / "llama-server").write_text("bin")
    (folder / "libllama.so").write_text("lib")
    session = tmp_path / "session"
    monkeypatch.setattr(r.os, "access", lambda p, mode: False)    # like a noexec mount

    single = r.ensure_executable(folder / "llama-server", str(session))
    assert single == session / "bin" / "llama-server"
    staged = r.ensure_executable(folder / "llama-server", str(session), whole_dir=True)
    assert staged == session / "bin" / "llama" / "llama-server"
    assert (staged.parent / "libllama.so").read_text() == "lib"

    monkeypatch.setattr(r.os, "access", lambda p, mode: True)
    assert r.ensure_executable(folder / "llama-server", str(session)) == folder / "llama-server"


# ---------------------------------------------------------------------------
# Permission prompts for system tools
# ---------------------------------------------------------------------------

def test_registry_writes_and_process_kills_ask_first():
    from agent.agent import PermissionPolicy
    asked = []
    ask = PermissionPolicy(mode="ask", confirm_fn=lambda t, a, why: asked.append(why) or True)
    assert ask.check("mcp__desktop__registry", {"mode": "get", "path": "HKCU:\\X"}) == (True, "")
    assert ask.check("mcp__desktop__registry", {"mode": "delete", "path": "HKCU:\\X"}) == (True, "")
    assert ask.check("mcp__desktop__process", {"mode": "kill", "name": "notepad"}) == (True, "")
    assert ask.check("mcp__desktop__process", {"mode": "list"}) == (True, "")
    assert asked == ["Registry delete: HKCU:\\X", "Killing a process: notepad"]
    safe = PermissionPolicy(mode="safe")
    assert safe.check("mcp__desktop__registry", {"mode": "set", "path": "HKLM:\\Y"})[0] is False
    yolo = PermissionPolicy(mode="yolo")
    assert yolo.check("mcp__desktop__process", {"mode": "kill", "pid": 4}) == (True, "")


# ---------------------------------------------------------------------------
# Host settings restored on cleanup (GNOME accessibility)
# ---------------------------------------------------------------------------

def test_changed_host_settings_are_restored(monkeypatch):
    from cleanup import cleanup as c
    store = {("org.gnome.desktop.interface", "toolkit-accessibility"): "false"}
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "get":
            return store.get((args[1], args[2]))
        store[(args[1], args[2])] = args[3]
        return ""

    monkeypatch.setattr(c, "_gsettings", fake)
    monkeypatch.setattr(c.sys, "platform", "linux")
    assert c.snapshot_host_settings() == 1
    store[("org.gnome.desktop.interface", "toolkit-accessibility")] = "true"  # tool turned it on
    assert c.restore_host_settings() == 1
    assert store[("org.gnome.desktop.interface", "toolkit-accessibility")] == "false"
    assert c.restore_host_settings() == 0                                      # unchanged: no write
    assert sum(1 for a in calls if a[0] == "set") == 1
    assert "settings_restored" in c.full_cleanup(session_dir=None, dry_run=True)


def test_filesystem_tool_follows_write_file_rules(tmp_path):
    from agent.agent import PermissionPolicy
    existing = tmp_path / "a.txt"
    existing.write_text("x")
    safe = PermissionPolicy(mode="safe")
    fs = "mcp__desktop__filesystem"
    assert safe.check(fs, {"mode": "read", "path": str(existing)}) == (True, "")
    assert safe.check(fs, {"mode": "write", "path": str(tmp_path / "new.txt")}) == (True, "")
    assert safe.check(fs, {"mode": "write", "path": str(existing)})[0] is False
    assert safe.check(fs, {"mode": "copy", "path": "x", "destination": str(existing)})[0] is False
    assert safe.check(fs, {"mode": "delete", "path": str(tmp_path / "gone")})[0] is False
