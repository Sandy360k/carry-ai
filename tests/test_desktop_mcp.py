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
    assert set(cfg.exclude_tools) == {"setup_accessibility", "setup_window_targeting"}


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
    assert "serve" in cfg.args and "--exclude-tools" in cfg.args
    excluded = cfg.args[cfg.args.index("--exclude-tools") + 1].split(",")
    assert {"PowerShell", "Registry", "Process", "Notification"} <= set(excluded)
    assert cfg.exclude_tools == excluded             # also filtered on our side
    assert cfg.env["ANONYMIZED_TELEMETRY"] == "false" and cfg.env["POSTHOG_API_KEY"] == ""


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
