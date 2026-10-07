"""
Tests for the bundled-runtime helpers (portable/runtime.py). No network:
archive handling is exercised with in-memory tar/zip fixtures.
"""

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from conftest import PROJECT_ROOT  # noqa: F401  (adds project root to sys.path)
from portable import runtime as r


def test_pins_and_hashes_are_present():
    assert r.PYTHON_VERSION and r.PYTHON_RELEASE and r.LLAMA_BUILD
    for os_name in r.OS_NAMES:
        name, sha = r.PYTHON_ASSETS[os_name]
        assert name.endswith(".tar.gz") and len(sha) == 64
        assert r.LLAMA_ASSETS[os_name], os_name
        for backend, asset, lsha in r.LLAMA_ASSETS[os_name]:
            assert backend in ("vulkan", "cpu")
            assert len(lsha) == 64


def test_layout_helpers_differ_by_os(tmp_path):
    lin = r.python_exe(tmp_path, "linux")
    win = r.python_exe(tmp_path, "windows")
    assert lin.name.startswith("python3") and win.name == "python.exe"
    assert r.site_packages(tmp_path, "windows").parts[-2:] == ("Lib", "site-packages")
    assert r.site_packages(tmp_path, "linux").name == "site-packages"
    assert r.llama_dir(tmp_path, "linux", "vulkan").name == "linux-vulkan"


def test_download_rejects_bad_checksum(tmp_path, monkeypatch):
    payload = b"hello world"

    class _Resp:
        headers = {"Content-Length": str(len(payload))}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): 
            data, self._done = (payload if not getattr(self, "_done", False) else b""), True
            return data

    monkeypatch.setattr(r, "urlopen", lambda *a, **k: _Resp())
    dest = tmp_path / "f.bin"
    with pytest.raises(ValueError):
        r.download("https://example/f", dest, sha256="0" * 64)
    assert not dest.exists()           # partial file removed
    # correct checksum succeeds
    import hashlib
    good = hashlib.sha256(payload).hexdigest()
    assert r.download("https://example/f", dest, sha256=good).read_bytes() == payload


def test_extract_tar_is_symlink_free_and_safe(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("top/bin/real"); info.size = len(data); info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("top/bin/alias"); link.type = tarfile.SYMTYPE; link.linkname = "real"
        tf.addfile(link)
    buf.seek(0)
    archive = tmp_path / "a.tar.gz"; archive.write_bytes(buf.getvalue())
    out = tmp_path / "out"
    r.extract(archive, out, strip="top")
    assert (out / "bin" / "real").is_file()
    alias = out / "bin" / "alias"
    assert alias.is_file() and not alias.is_symlink()      # link became a copy
    assert alias.read_bytes() == (out / "bin" / "real").read_bytes()


def test_extract_zip_rejects_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.txt", "x")
    archive = tmp_path / "a.zip"; archive.write_bytes(buf.getvalue())
    with pytest.raises(ValueError):
        r.extract(archive, tmp_path / "out")


def test_pip_command_targets_bundled_interpreter_version():
    cmd = r.pip_install_command(["flask"], Path("/x"), "windows")
    assert "--python-version" in cmd and "3.12" in cmd
    assert "win_amd64" in cmd and "--only-binary=:all:" in cmd
    # source-only packages build from sdist with deps listed by hand
    src = r.pip_install_command(["pyautogui"], Path("/x"), "linux", source_build=True)
    assert "--no-deps" in src and "--use-pep517" in src


def test_find_llama_server_prefers_bundled_vulkan(tmp_path, monkeypatch):
    """Bundled builds under bin/llama/<os>-<backend>/ win over PATH, Vulkan first."""
    import modes.local_mode as lm
    carry = tmp_path / "carry-ai"
    carry.mkdir()
    for backend in ("vulkan", "cpu"):
        d = tmp_path / "bin" / "llama" / f"linux-{backend}"
        d.mkdir(parents=True)
        (d / "llama-server").write_text("#!/bin/sh\n")
    monkeypatch.setattr(lm, "PROJECT_ROOT", carry)
    monkeypatch.setattr(lm.platform, "system", lambda: "Linux")
    found = lm.find_llama_server()
    assert found is not None
    assert found.parent.name == "linux-vulkan"
