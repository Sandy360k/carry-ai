"""
carry-ai/portable/runtime.py — Bundled runtimes for a zero-install USB
=======================================================================

Puts everything carry-ai needs to run onto the USB so the host needs
nothing installed — not even Python on Linux:

    USB_ROOT/
    ├── python-env/
    │   ├── windows/            python.exe + full stdlib (python-build-standalone)
    │   │   └── Lib/site-packages/
    │   └── linux/
    │       ├── python/bin/python3.12
    │       └── site-packages/
    └── bin/llama/
        ├── windows-vulkan/     llama-server.exe (GPU via any Vulkan driver)
        ├── windows-cpu/        llama-server.exe (fallback)
        ├── linux-vulkan/       llama-server
        └── linux-cpu/          llama-server

Design notes:
    - Every download is pinned to an exact release and verified against a
      SHA-256 recorded here, so a compromised or changed upstream asset is
      rejected instead of landing on the stick.
    - Python comes from astral-sh/python-build-standalone (glibc ≥ 2.17 on
      Linux, tkinter + sqlite FTS5 + OpenSSL included on both OSes).
    - USB sticks are usually exFAT/FAT32, which cannot store symlinks, so
      archive symlinks are materialised as plain copies on extraction.
    - Packages for *both* OSes are installed from whatever machine runs the
      flasher, using pip's cross-platform mode (--platform/--python-version)
      against the bundled interpreter's version, never the host's.

Stdlib only — imported by flash_usb.py on hosts with a bare Python.
"""

import contextlib
import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

log = logging.getLogger("carry-ai.portable")

OS_NAMES = ("linux", "windows")

# ---------------------------------------------------------------------------
# Pinned releases (bump together with the hashes; see docs/getting-started.md)
# ---------------------------------------------------------------------------

PYTHON_VERSION = "3.12.15"
PYTHON_RELEASE = "20261003"
_PBS_BASE = ("https://github.com/astral-sh/python-build-standalone/releases/"
             f"download/{PYTHON_RELEASE}/")

PYTHON_ASSETS: dict[str, tuple[str, str]] = {
    "linux": (
        f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        "731af898886c5f821890dc901eca3c651cca8e51fa7308c159d12a1194aeac91",
    ),
    "windows": (
        f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
        "6fba7f2ae506facf41d457ea8293c7497910a675c69a4e954875169410a50402",
    ),
}

LLAMA_BUILD = "b11461"
_LLAMA_BASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/"

# Per OS, most preferred first. Vulkan uses the GPU through the normal
# display driver (NVIDIA, AMD, Intel); the CPU build is the fallback.
LLAMA_ASSETS: dict[str, list[tuple[str, str, str]]] = {
    "linux": [
        ("vulkan", f"llama-{LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz",
         "6e1e169c5b045cf2ce83bb017448f11929ef40a02e028b5e7bdda9835fdc80fe"),
        ("cpu", f"llama-{LLAMA_BUILD}-bin-ubuntu-x64.tar.gz",
         "9462d9b8243df6de8b6bf1baded7008a97e57d9f88611705a535b64d0682d87a"),
    ],
    "windows": [
        ("vulkan", f"llama-{LLAMA_BUILD}-bin-win-vulkan-x64.zip",
         "6b9ca8ad3e43ac3e166d1997875709e4547214bb5bf1c2ebfdd17a2350a8380c"),
        ("cpu", f"llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip",
         "e283199134e74405a3684cfaa3911708ca7d051a8c2a8dcb10b3dfa1678913c3"),
    ],
}

# pip tags for cross-installing wheels that match the bundled interpreter
PIP_PLATFORMS: dict[str, list[str]] = {
    # 2_28 last: pip prefers earlier tags, so it is used only for packages
    # with no older wheel (pydantic-monty-runtime). Needs glibc 2.28+
    # (Debian 10 / Ubuntu 18.10 / RHEL 8 or newer).
    "linux": ["manylinux_2_17_x86_64", "manylinux2014_x86_64", "manylinux_2_28_x86_64"],
    "windows": ["win_amd64"],
}

# Packages published only as source. They are pure Python, so they are built
# in an isolated environment and installed with their (also pure) deps listed
# explicitly, since pip cannot resolve deps cross-platform from source.
SOURCE_ONLY: dict[str, dict[str, list[str]]] = {
    "pyautogui": {
        "linux": ["pymsgbox", "pytweening", "pyscreeze", "mouseinfo", "python3-xlib"],
        "windows": ["pymsgbox", "pytweening", "pyscreeze", "mouseinfo",
                    "pygetwindow", "pyrect"],
    },
}

# Packages that only have wheels for some OSes. PyAudio has no Linux wheel
# (it needs the PortAudio dev headers); on Linux the voice pipeline records
# through sherpa-onnx's own ALSA reader and plays through aplay instead.
ONLY_ON: dict[str, set[str]] = {
    "pyaudio": {"windows"},
}


# ===================================================================
# USB layout
# ===================================================================

def python_home(usb_root: Path, os_name: str) -> Path:
    """Directory holding the bundled interpreter for *os_name*."""
    env = Path(usb_root) / "python-env" / os_name
    return env if os_name == "windows" else env / "python"


def python_exe(usb_root: Path, os_name: str) -> Path:
    """Bundled interpreter path (symlink-free name, see module docstring)."""
    home = python_home(usb_root, os_name)
    if os_name == "windows":
        return home / "python.exe"
    major_minor = ".".join(PYTHON_VERSION.split(".")[:2])
    return home / "bin" / f"python{major_minor}"


def site_packages(usb_root: Path, os_name: str) -> Path:
    """Where carry-ai's packages go (matches bootstrap.py)."""
    env = Path(usb_root) / "python-env" / os_name
    return env / "Lib" / "site-packages" if os_name == "windows" else env / "site-packages"


def llama_dir(usb_root: Path, os_name: str, backend: str) -> Path:
    return Path(usb_root) / "bin" / "llama" / f"{os_name}-{backend}"


# ===================================================================
# Download + verify
# ===================================================================

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, sha256: str | None = None,
             progress=None) -> Path:
    """Download *url* to *dest*, verifying *sha256* if given.

    Args:
        progress: optional callable(written_bytes, total_bytes).

    Raises:
        ValueError: checksum mismatch (the partial file is removed).
        OSError / URLError: network or disk failure.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sha256 and dest.is_file() and sha256_file(dest) == sha256:
        return dest  # cached from an earlier run

    tmp = dest.with_name(dest.name + ".part")
    req = Request(url, headers={"User-Agent": "carry-ai/portable"})
    try:
        with urlopen(req, timeout=600) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length", 0))
            written = 0
            for chunk in iter(lambda: resp.read(1 << 16), b""):
                f.write(chunk)
                written += len(chunk)
                if progress:
                    progress(written, total)
        if sha256:
            actual = sha256_file(tmp)
            if actual != sha256:
                raise ValueError(f"Checksum mismatch for {dest.name}: "
                                 f"expected {sha256}, got {actual}")
        tmp.replace(dest)
        return dest
    finally:
        tmp.unlink(missing_ok=True)


# ===================================================================
# Safe, symlink-free extraction
# ===================================================================

def _safe_relpath(name: str, strip: str | None) -> PurePosixPath | None:
    """Archive member name → relative path inside dest, or None to skip."""
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".")]
    if strip:
        if not parts or parts[0] != strip:
            return None
        parts = parts[1:]
    if not parts:
        return None
    if any(p == ".." for p in parts) or parts[0].endswith(":"):
        raise ValueError(f"Unsafe path in archive: {name!r}")
    return PurePosixPath(*parts)


def extract(archive: Path, dest: Path, strip: str | None = None,
            links: bool = True) -> None:
    """Extract a .tar.gz or .zip into *dest* without path traversal.

    Symlinks are replaced by copies of their target file, because exFAT and
    FAT32 (what most USB sticks use) cannot store links. Exec bits from tar
    archives are preserved where the filesystem supports them.

    Args:
        strip: top-level directory name to drop (e.g. "python").
        links: materialise symlinks as copies (needed for shared-library
            sonames); False skips them, e.g. python3 -> python3.12 aliases.
    """
    archive, dest = Path(archive), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    pending_links: list[tuple[PurePosixPath, PurePosixPath]] = []

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                rel = _safe_relpath(info.filename, strip)
                if rel is None or info.is_dir():
                    continue
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        return

    with tarfile.open(archive, "r:*") as tf:
        for member in tf:
            rel = _safe_relpath(member.name, strip)
            if rel is None:
                continue
            target = dest / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.issym() or member.islnk():
                if not links:
                    continue
                link = PurePosixPath(member.linkname)
                if member.issym():
                    link = rel.parent / link
                else:  # hard link names are archive paths
                    link = _safe_relpath(member.linkname, strip) or link
                pending_links.append((rel, link))
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                if member.mode & 0o111:
                    _make_executable(target)

    # Materialise links after all regular files exist; chains resolve in
    # order because each pass copies whatever already exists.
    pending = pending_links
    for _ in range(8):
        remaining = []
        for rel, link in pending:
            source = dest / _normalize(link)
            if source.is_file():
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                remaining.append((rel, link))
        if not remaining or len(remaining) == len(pending):
            pending = remaining
            break
        pending = remaining
    for rel, link in pending:
        log.debug("Skipping link %s -> %s (target not a file)", rel, link)


def _normalize(path: PurePosixPath) -> PurePosixPath:
    out: list[str] = []
    for part in path.parts:
        if part == "..":
            if out:
                out.pop()
        elif part not in ("", "."):
            out.append(part)
    return PurePosixPath(*out)


def _make_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass  # exFAT/FAT: no permission bits; start.sh copies to RAM instead


# ===================================================================
# Installers
# ===================================================================

def install_python(usb_root: Path, os_name: str, cache_dir: Path,
                   progress=None) -> Path:
    """Put python-build-standalone for *os_name* onto the USB.

    Returns the interpreter path. Idempotent: an existing install of the
    pinned version is left alone.
    """
    exe = python_exe(usb_root, os_name)
    marker = python_home(usb_root, os_name) / ".carry-ai-python"
    if exe.is_file() and marker.is_file() and marker.read_text().strip() == PYTHON_VERSION:
        return exe

    name, sha = PYTHON_ASSETS[os_name]
    archive = download(_PBS_BASE + name.replace("+", "%2B"),
                       Path(cache_dir) / name, sha, progress)
    home = python_home(usb_root, os_name)
    if os_name == "windows":
        # Replace an old python.org embeddable install (its ._pth file
        # would otherwise restrict sys.path) but keep installed packages.
        for pth in home.glob("python*._pth"):
            pth.unlink()
    # Links are only aliases (python3 -> python3.12, libpython3.12.so ->
    # .so.1.0); skipping them avoids duplicate copies of the interpreter.
    extract(archive, home, strip="python", links=False)
    marker.write_text(PYTHON_VERSION)
    return exe


def install_llama(usb_root: Path, os_name: str, cache_dir: Path,
                  progress=None) -> list[Path]:
    """Put the pinned llama-server builds for *os_name* onto the USB.

    Returns the directories written, most preferred backend first.
    """
    written = []
    for backend, name, sha in LLAMA_ASSETS[os_name]:
        target = llama_dir(usb_root, os_name, backend)
        marker = target / ".carry-ai-llama"
        if not (marker.is_file() and marker.read_text().strip() == LLAMA_BUILD):
            archive = download(_LLAMA_BASE + name, Path(cache_dir) / name, sha, progress)
            if target.exists():
                shutil.rmtree(target)
            # Linux tarballs have a llama-<build>/ top dir; Windows zips are flat
            extract(archive, target,
                    strip=f"llama-{LLAMA_BUILD}" if name.endswith(".tar.gz") else None)
            marker.write_text(LLAMA_BUILD)
        written.append(target)
    return written


def pip_install_command(packages: list[str], target: Path, os_name: str,
                        source_build: bool = False) -> list[str]:
    """pip argv that installs *packages* for the bundled interpreter of *os_name*."""
    major_minor = ".".join(PYTHON_VERSION.split(".")[:2])
    cmd = [sys.executable, "-m", "pip", "install", "--target", str(target),
           "--upgrade", "--no-warn-script-location", "--disable-pip-version-check",
           "--python-version", major_minor, "--implementation", "cp", "-q"]
    for plat in PIP_PLATFORMS[os_name]:
        cmd += ["--platform", plat]
    # pip only cross-installs binary wheels, unless deps are listed by hand
    cmd += ["--no-deps", "--use-pep517"] if source_build else ["--only-binary=:all:"]
    return cmd + list(packages)


def install_packages(packages: list[str], usb_root: Path, os_name: str,
                     report=None) -> list[str]:
    """Install *packages* into the USB site-packages for *os_name*.

    Works from any host OS. Args:
        report: optional callable(name, ok) for per-package progress.

    Returns:
        Names of packages that failed.
    """
    target = site_packages(usb_root, os_name)
    target.mkdir(parents=True, exist_ok=True)
    failed = []
    for pkg in packages:
        with _keep_scripts(target):
            _install_one(pkg, target, os_name, failed, report)
    return failed


@contextlib.contextmanager
def _keep_scripts(target: Path):
    """Keep earlier packages' scripts across one ``pip --target --upgrade``.

    pip replaces the whole ``<target>/bin`` (``Scripts`` on Windows) folder
    with the one from the package it is installing, so a later package with
    a script (cffi's cffi-gen-src) wiped an earlier one's binary (the monty
    worker, llmfit). Copies of what was there are put back afterwards.
    """
    saved = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("bin", "Scripts"):
            folder = target / name
            if folder.is_dir():
                backup = Path(tmp) / name
                shutil.copytree(folder, backup)
                saved.append((folder, backup))
        try:
            yield
        finally:
            for folder, backup in saved:
                folder.mkdir(exist_ok=True)
                for item in backup.iterdir():
                    dest = folder / item.name
                    if not dest.exists():
                        shutil.copy2(item, dest)


def _install_one(pkg: str, target: Path, os_name: str, failed: list, report) -> None:
    name = re.split(r"[<>=!~\[ ]", pkg, maxsplit=1)[0].strip()
    if os_name not in ONLY_ON.get(name.lower(), {os_name}):
        return
    if name.lower() in SOURCE_ONLY:
        deps = SOURCE_ONLY[name.lower()][os_name]
        cmd = pip_install_command([pkg, *deps], target, os_name, source_build=True)
    else:
        cmd = pip_install_command([pkg], target, os_name)
    result = subprocess.run(cmd, capture_output=True, text=True)
    ok = result.returncode == 0
    if not ok:
        failed.append(name)
        log.warning("pip failed for %s (%s): %s", name, os_name,
                    (result.stderr or result.stdout).strip()[-300:])
    if report:
        report(name, ok)
