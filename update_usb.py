"""
carry-ai/update_usb.py — Smart USB Updater
===========================================
Updates an existing carry-ai USB installation without re-flashing.
Only changed or new files are copied; packages are upgraded only if
requirements.txt changed; models and Python runtime are never touched.

Usage:
    python update_usb.py                   # interactive drive picker
    python update_usb.py --target H:\\      # Windows explicit drive
    python update_usb.py --target /media/usb # Linux explicit mount
    python update_usb.py --dry-run         # show what would change, no writes
    python update_usb.py --packages-only   # skip files, only update packages
    python update_usb.py --files-only      # skip packages
    python update_usb.py --verbose         # detailed per-file output

How it works:
  1. Locate the carry-ai installation on the target USB
  2. Compare SHA-256 hashes of every source file against the USB copy
  3. Copy only new/modified files (skip models/, python-env/)
  4. Compare requirements.txt package versions against USB site-packages
  5. Install only new or upgraded packages into USB site-packages
  6. Write an updated .carry_ai_manifest.json for next-run comparison
  7. Print a concise diff summary

A manifest file (.carry_ai_manifest.json) is written to USB/carry-ai/
after each successful update. It stores per-file SHA-256 hashes and
the requirements.txt content hash so the next update run can skip
files that haven't changed since the last update (not just since the
initial flash).
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt, Confirm
    from rich.rule import Rule
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    _RICH = True
    console = Console()
except ImportError:
    _RICH = False

    class _Plain:
        def print(self, *a, **kw):
            text = " ".join(str(x) for x in a)
            text = re.sub(r'\[/?[^\]]+\]', '', text)
            print(text)
        def rule(self, title=""):
            print(f"\n{'─'*60}")
            if title:
                print(f"  {title}")

    console = _Plain()


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from portable import runtime  # noqa: E402
MANIFEST_FILENAME = ".carry_ai_manifest.json"

# Files/dirs to never update (user data, secrets, models, runtime)
NEVER_UPDATE = {
    "config/providers.enc",
    "config/settings.json",
    "config/memory.db",
}

# Dirs to skip entirely during file sync
SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".gitignore",
    "python-env",       # Python runtime — never overwrite
    "models",           # GGUF files — never overwrite
    "_dry_run_session",
}

# Glob patterns that are never copied
SKIP_PATTERNS = {"*.pyc", "*.pyo", "*.egg-info", ".DS_Store", "Thumbs.db"}


# ---------------------------------------------------------------------------
# Rich / plain helpers
# ---------------------------------------------------------------------------

def _c(text: str) -> str:
    """Strip rich markup for plain-text output."""
    return re.sub(r'\[/?[^\]]+\]', '', text)


def _confirm(prompt: str, default: bool = True) -> bool:
    if _RICH:
        return Confirm.ask(prompt, default=default)
    tag = "Y/n" if default else "y/N"
    ans = input(f"{_c(prompt)} [{tag}]: ").strip().lower()
    return default if not ans else ans in ("y", "yes")


def _prompt(prompt: str, default: str = "") -> str:
    if _RICH:
        return Prompt.ask(prompt, default=default) if default else Prompt.ask(prompt)
    val = input(f"{_c(prompt)}{' [' + default + ']' if default else ''}: ").strip()
    return val or default


def _panel(title: str, body: str = "", style: str = "blue") -> None:
    if _RICH:
        console.print(Panel(body or title, title=title if body else "", border_style=style))
    else:
        console.rule(title)
        if body:
            for line in body.strip().splitlines():
                print(f"  {line}")
        print()


def _ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def _warn(msg: str) -> None:
    console.print(f"  [yellow]![/yellow] {msg}")


def _err(msg: str) -> None:
    console.print(f"  [red]✗[/red] {msg}")


def _info(msg: str) -> None:
    console.print(f"  [cyan]→[/cyan] {msg}")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

@dataclass
class Manifest:
    """Records what was last written to the USB so future updates are fast."""
    version: str = "2"
    updated_at: str = ""
    source_root: str = ""          # where the update was run from
    file_hashes: dict = field(default_factory=dict)    # rel_path → sha256
    requirements_hash: str = ""    # sha256 of requirements.txt
    installed_packages: dict = field(default_factory=dict)  # pkg → version

    @staticmethod
    def load(usb_carry_ai: Path) -> "Manifest":
        path = usb_carry_ai / MANIFEST_FILENAME
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                m = Manifest()
                m.version = data.get("version", "1")
                m.updated_at = data.get("updated_at", "")
                m.source_root = data.get("source_root", "")
                m.file_hashes = data.get("file_hashes", {})
                m.requirements_hash = data.get("requirements_hash", "")
                m.installed_packages = data.get("installed_packages", {})
                return m
            except Exception:
                pass
        return Manifest()

    def save(self, usb_carry_ai: Path) -> None:
        path = usb_carry_ai / MANIFEST_FILENAME
        self.updated_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.source_root = str(PROJECT_ROOT)
        data = asdict(self)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Drive detection  (reused from flash_usb.py)
# ---------------------------------------------------------------------------

def detect_usb_drives() -> list[dict]:
    """Return removable drives."""
    drives = []

    if psutil is not None:
        for part in psutil.disk_partitions(all=False):
            is_removable = False
            opts = part.opts.lower() if part.opts else ""

            if platform.system().lower() == "windows":
                is_removable = "removable" in opts
            else:
                dev = part.device
                block = re.sub(r'\d+$', '', dev.replace('/dev/', ''))
                removable_path = Path(f"/sys/block/{block}/removable")
                if removable_path.exists():
                    is_removable = removable_path.read_text().strip() == "1"

            if not is_removable:
                continue

            try:
                usage = psutil.disk_usage(part.mountpoint)
                free_gb = usage.free / (1024 ** 3)
                total_gb = usage.total / (1024 ** 3)
            except (PermissionError, OSError):
                free_gb = total_gb = 0.0

            drives.append({
                "mountpoint": part.mountpoint,
                "device": part.device,
                "label": Path(part.mountpoint).name or "USB",
                "total_gb": total_gb,
                "free_gb": free_gb,
            })
    else:
        # Minimal fallback
        candidates: list[str] = []
        if platform.system().lower() == "windows":
            import string
            for letter in string.ascii_uppercase:
                mp = f"{letter}:\\"
                if os.path.exists(mp):
                    candidates.append(mp)
        else:
            for base in ["/media", "/mnt", "/run/media"]:
                if os.path.isdir(base):
                    for sub in Path(base).iterdir():
                        candidates.append(str(sub))
        for mp in candidates:
            try:
                usage = shutil.disk_usage(mp)
                drives.append({
                    "mountpoint": mp,
                    "device": mp,
                    "label": Path(mp).name or "USB",
                    "total_gb": usage.total / (1024**3),
                    "free_gb": usage.free / (1024**3),
                })
            except OSError:
                pass

    return drives


def pick_drive(explicit_target: Optional[str] = None) -> Optional[Path]:
    """
    Resolve target USB root.  If explicit_target is given, use it directly.
    Otherwise list removable drives and ask the user to pick.
    Returns the carry-ai directory on the USB (not the USB root itself).
    """
    if explicit_target:
        target = Path(explicit_target).resolve()
        # Accept both USB root and carry-ai subdir
        if (target / "carry-ai").is_dir():
            return target / "carry-ai"
        if (target / "launcher.py").exists():
            return target  # already pointing at carry-ai dir
        _err(f"No carry-ai installation found at '{target}'.")
        _info(f"Expected: {target}/carry-ai/launcher.py  OR  {target}/launcher.py")
        return None

    drives = detect_usb_drives()

    # Filter to drives that already have a carry-ai install
    carry_ai_drives = []
    for d in drives:
        mp = Path(d["mountpoint"])
        if (mp / "carry-ai" / "launcher.py").exists():
            carry_ai_drives.append(d)

    if not carry_ai_drives and drives:
        # Show all removable drives and let user pick even without carry-ai dir
        console.print()
        _warn("No carry-ai installation detected on any removable drive.")
        _info("Showing all removable drives — pick one to install/update carry-ai:")
        carry_ai_drives = drives

    if not carry_ai_drives:
        _err("No removable drives detected.  Plug in the USB and try again.")
        _info("Or use:  python update_usb.py --target /path/to/usb")
        return None

    console.print()
    if _RICH:
        t = Table(show_header=True, header_style="bold cyan")
        t.add_column("#", style="cyan", width=3)
        t.add_column("Drive")
        t.add_column("Label")
        t.add_column("Free", justify="right")
        t.add_column("carry-ai?", justify="center")
        for i, d in enumerate(carry_ai_drives, 1):
            has_install = (Path(d["mountpoint"]) / "carry-ai" / "launcher.py").exists()
            t.add_row(
                str(i),
                d["mountpoint"],
                d["label"],
                f"{d['free_gb']:.1f} GB",
                "[green]✓[/green]" if has_install else "[yellow]new[/yellow]",
            )
        console.print(t)
    else:
        print(f"  {'#':<3} {'Drive':<20} {'Label':<15} {'Free':>8}  {'carry-ai?'}")
        print(f"  {'-'*65}")
        for i, d in enumerate(carry_ai_drives, 1):
            has_install = (Path(d["mountpoint"]) / "carry-ai" / "launcher.py").exists()
            print(f"  {i:<3} {d['mountpoint']:<20} {d['label']:<15} {d['free_gb']:>6.1f} GB  "
                  f"{'yes' if has_install else 'new'}")

    choice = _prompt("\nWhich drive?", default="1")
    try:
        idx = int(choice) - 1
        if not 0 <= idx < len(carry_ai_drives):
            raise ValueError
    except ValueError:
        _err("Invalid choice.")
        return None

    usb_root = Path(carry_ai_drives[idx]["mountpoint"])
    return usb_root / "carry-ai"


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _should_skip_path(rel: Path) -> bool:
    """True if this relative path should never be synced."""
    parts = set(rel.parts)
    if parts & SKIP_DIRS:
        return True
    # Top-level dir check
    if rel.parts and rel.parts[0] in SKIP_DIRS:
        return True
    name = rel.name
    for pat in SKIP_PATTERNS:
        if pat.startswith("*"):
            if name.endswith(pat[1:]):
                return True
        elif name == pat:
            return True
    # Never-update specific files
    rel_str = str(rel).replace("\\", "/")
    if rel_str in NEVER_UPDATE:
        return True
    # Skip the manifest itself — we write it at the end
    if name == MANIFEST_FILENAME:
        return True
    return False


# ---------------------------------------------------------------------------
# File diff + sync
# ---------------------------------------------------------------------------

@dataclass
class FileDiff:
    added: list[Path] = field(default_factory=list)
    modified: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)    # NEVER_UPDATE / SKIP


def compute_file_diff(source: Path, dest: Path, manifest: Manifest,
                      verbose: bool = False) -> FileDiff:
    """
    Walk source dir, compare each file against dest and manifest hashes.
    Returns a FileDiff with relative paths.
    """
    diff = FileDiff()

    for src_file in sorted(source.rglob("*")):
        if not src_file.is_file():
            continue

        rel = src_file.relative_to(source)

        if _should_skip_path(rel):
            diff.skipped.append(rel)
            continue

        rel_str = str(rel).replace("\\", "/")
        dest_file = dest / rel
        src_hash = sha256_file(src_file)

        if not dest_file.exists():
            diff.added.append(rel)
            if verbose:
                _info(f"  NEW     {rel_str}")
            continue

        # Compare against manifest hash first (fast path), then actual file
        manifest_hash = manifest.file_hashes.get(rel_str, "")
        if manifest_hash and manifest_hash == src_hash:
            diff.unchanged.append(rel)
            if verbose:
                console.print(f"    [dim]skip    {rel_str}[/dim]")
            continue

        dest_hash = sha256_file(dest_file)
        if dest_hash != src_hash:
            diff.modified.append(rel)
            if verbose:
                _info(f"  CHANGED {rel_str}")
        else:
            diff.unchanged.append(rel)
            if verbose:
                console.print(f"    [dim]skip    {rel_str}[/dim]")

    return diff


def apply_file_diff(source: Path, dest: Path, diff: FileDiff,
                    dry_run: bool = False) -> int:
    """Copy added/modified files. Returns count of files written."""
    count = 0
    to_copy = diff.added + diff.modified

    if not to_copy:
        return 0

    if _RICH and not dry_run:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Copying files...", total=len(to_copy))
            for rel in to_copy:
                dest_file = dest / rel
                if not dry_run:
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / rel, dest_file)
                count += 1
                progress.advance(task)
    else:
        for rel in to_copy:
            dest_file = dest / rel
            if not dry_run:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / rel, dest_file)
            count += 1

    return count


# ---------------------------------------------------------------------------
# Package diff + install
# ---------------------------------------------------------------------------

def _parse_requirements(req_file: Path) -> dict[str, str]:
    """
    Parse requirements.txt into {pkg_name_lower: version_spec}.
    Skips comments, blanks, extras (# ...) lines.
    Returns e.g. {"flask": ">=3.0.0", "psutil": ">=5.9.0"}
    """
    pkgs: dict[str, str] = {}
    if not req_file.exists():
        return pkgs
    for raw_line in req_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip inline comments
        line = line.split("#")[0].strip()
        if not line:
            continue
        # Skip options like -r, -e, --index-url
        if line.startswith("-"):
            continue
        # Parse pkg>=ver or pkg==ver etc.
        m = re.match(r'^([A-Za-z0-9_.\-]+)\s*([><=!~][><=!~0-9.*,\s]*)?', line)
        if m:
            name = m.group(1).lower().replace("-", "_")
            spec = (m.group(2) or "").strip()
            pkgs[name] = spec
    return pkgs


def _installed_packages_in(site_pkgs: Path) -> dict[str, str]:
    """
    Scan site-packages for .dist-info/METADATA to find installed versions.
    Returns {pkg_name_lower: version_string}
    """
    installed: dict[str, str] = {}
    if not site_pkgs.is_dir():
        return installed
    for dist_info in site_pkgs.glob("*.dist-info"):
        meta = dist_info / "METADATA"
        if not meta.exists():
            meta = dist_info / "PKG-INFO"
        if not meta.exists():
            continue
        name_ver = dist_info.name.rsplit("-", 1)  # "flask-3.0.3.dist-info"
        if len(name_ver) >= 1:
            # Prefer reading from METADATA for accuracy
            pkg_name = ""
            pkg_ver = ""
            try:
                for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("Name:"):
                        pkg_name = line.split(":", 1)[1].strip().lower().replace("-", "_")
                    elif line.startswith("Version:"):
                        pkg_ver = line.split(":", 1)[1].strip()
                    if pkg_name and pkg_ver:
                        break
            except OSError:
                pass
            if pkg_name and pkg_ver:
                installed[pkg_name] = pkg_ver
    return installed


def _version_tuple(ver: str) -> tuple:
    """Convert "3.0.3" → (3, 0, 3) for simple comparisons."""
    parts = []
    for p in re.split(r'[.\-]', ver):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _needs_install(pkg_name: str, spec: str, installed: dict[str, str]) -> bool:
    """
    True if pkg_name is not installed or version doesn't satisfy spec.
    Uses simple min-version check (>=) only; for == checks exact match.
    """
    norm = pkg_name.lower().replace("-", "_")
    if norm not in installed:
        return True
    if not spec:
        return False  # any version is fine
    cur = installed[norm]
    # Handle >=
    m = re.match(r'^>=\s*([0-9][0-9.\-]*)', spec)
    if m:
        required = _version_tuple(m.group(1))
        current = _version_tuple(cur)
        return current < required
    # Handle ==
    m = re.match(r'^==\s*([0-9][0-9.\-]*)', spec)
    if m:
        return cur.strip() != m.group(1).strip()
    # For complex specs just check presence
    return False


@dataclass
class PackageDiff:
    to_install: list[str] = field(default_factory=list)    # pkg==ver specs
    up_to_date: list[str] = field(default_factory=list)
    site_pkgs: Optional[Path] = None
    usb_root: Optional[Path] = None
    os_name: str = ""


def compute_package_diff(req_file: Path, site_pkgs: Path,
                         manifest: Manifest) -> PackageDiff:
    diff = PackageDiff(site_pkgs=site_pkgs)

    required = _parse_requirements(req_file)
    installed = _installed_packages_in(site_pkgs)

    for pkg, spec in required.items():
        if _needs_install(pkg, spec, installed):
            diff.to_install.append(f"{pkg}{spec}" if spec else pkg)
        else:
            diff.up_to_date.append(pkg)

    return diff


def apply_package_diff(diff: PackageDiff, dry_run: bool = False) -> bool:
    """
    pip install --target <site_pkgs> <packages>
    Returns True on success.
    """
    if not diff.to_install or diff.usb_root is None:
        return True

    if dry_run:
        for pkg in diff.to_install:
            _info(f"  [DRY RUN] would install ({diff.os_name}): {pkg}")
        return True

    # Install for the USB's bundled interpreter (cross-platform wheels),
    # not the host's — see portable/runtime.py.
    console.print(f"  Installing {len(diff.to_install)} package(s) for {diff.os_name}...")
    failed = runtime.install_packages(diff.to_install, diff.usb_root, diff.os_name)
    if failed:
        _err(f"pip install failed for: {', '.join(failed)}")
        return False
    return True


# ---------------------------------------------------------------------------
# Start scripts  (re-write if they changed)
# ---------------------------------------------------------------------------

def update_start_scripts(usb_root: Path, source_root: Path,
                         dry_run: bool = False) -> int:
    """Sync start.bat and start.sh from source to USB root."""
    updated = 0
    for script in ("start.bat", "start.sh"):
        src = source_root / script
        dst = usb_root / script
        if not src.exists():
            continue
        src_hash = sha256_file(src)
        dst_hash = sha256_file(dst) if dst.exists() else ""
        if src_hash != dst_hash:
            if not dry_run:
                shutil.copy2(src, dst)
                if script == "start.sh":
                    try:
                        os.chmod(dst, 0o755)
                    except OSError:
                        pass
            updated += 1
            _ok(f"Updated {script}")
    return updated


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(diff_files: FileDiff, diff_pkgs: PackageDiff,
                  scripts_updated: int, elapsed: float,
                  dry_run: bool) -> None:
    tag = "[DRY RUN] " if dry_run else ""
    console.print()

    if _RICH:
        from rich.rule import Rule
        console.print(Rule(f"[bold green]{tag}Update Summary[/bold green]", style="green"))
        console.print()

        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("Category", style="bold")
        t.add_column("Count", justify="right", style="cyan")
        t.add_column("Detail")

        t.add_row(
            "Files added",
            str(len(diff_files.added)),
            ", ".join(str(p) for p in diff_files.added[:5])
            + (" ..." if len(diff_files.added) > 5 else ""),
        )
        t.add_row(
            "Files updated",
            str(len(diff_files.modified)),
            ", ".join(str(p) for p in diff_files.modified[:5])
            + (" ..." if len(diff_files.modified) > 5 else ""),
        )
        t.add_row("Files unchanged", str(len(diff_files.unchanged)), "")
        t.add_row(
            "Packages installed",
            str(len(diff_pkgs.to_install)),
            ", ".join(diff_pkgs.to_install[:4])
            + (" ..." if len(diff_pkgs.to_install) > 4 else ""),
        )
        t.add_row("Packages up-to-date", str(len(diff_pkgs.up_to_date)), "")
        if scripts_updated:
            t.add_row("Start scripts updated", str(scripts_updated), "")
        console.print(t)
        console.print()
        console.print(f"  Completed in [cyan]{elapsed:.1f}s[/cyan]")
    else:
        print(f"\n  {tag}Update Summary")
        print(f"  {'─'*40}")
        print(f"  Files added:          {len(diff_files.added)}")
        print(f"  Files updated:        {len(diff_files.modified)}")
        print(f"  Files unchanged:      {len(diff_files.unchanged)}")
        print(f"  Packages installed:   {len(diff_pkgs.to_install)}")
        print(f"  Packages up-to-date:  {len(diff_pkgs.up_to_date)}")
        if scripts_updated:
            print(f"  Start scripts:        {scripts_updated} updated")
        print(f"\n  Completed in {elapsed:.1f}s")

    if not dry_run and (diff_files.added or diff_files.modified or diff_pkgs.to_install):
        console.print()
        _ok("[bold green]USB carry-ai is up to date.[/bold green]")
    elif dry_run:
        console.print()
        _info("Dry run complete — no changes written.")
    else:
        console.print()
        _ok("Already up to date — nothing to do.")


# ---------------------------------------------------------------------------
# Main update flow
# ---------------------------------------------------------------------------

def run_update(
    usb_carry_ai: Path,
    dry_run: bool = False,
    packages_only: bool = False,
    files_only: bool = False,
    verbose: bool = False,
) -> bool:
    import time
    t_start = time.monotonic()

    usb_root = usb_carry_ai.parent
    console.print()
    _panel(
        "carry-ai USB Updater",
        f"Source :  {PROJECT_ROOT}\n"
        f"Target :  {usb_carry_ai}\n"
        f"Mode   :  {'DRY RUN — no writes' if dry_run else 'live update'}",
        style="cyan",
    )

    # ── Load manifest ────────────────────────────────────────────────────────
    manifest = Manifest.load(usb_carry_ai)
    if manifest.updated_at:
        _info(f"Last updated: {manifest.updated_at}")
    else:
        _info("No previous manifest — treating as first update after flash.")

    # ── File diff ────────────────────────────────────────────────────────────
    diff_files = FileDiff()
    if not packages_only:
        console.print()
        console.print("  [bold]Scanning files...[/bold]" if _RICH else "  Scanning files...")
        diff_files = compute_file_diff(PROJECT_ROOT, usb_carry_ai, manifest, verbose=verbose)

        total_changed = len(diff_files.added) + len(diff_files.modified)
        _info(f"Files: {len(diff_files.added)} new, {len(diff_files.modified)} changed, "
              f"{len(diff_files.unchanged)} unchanged")

        if total_changed:
            _info(f"Copying {total_changed} file(s)...")
            written = apply_file_diff(PROJECT_ROOT, usb_carry_ai, diff_files, dry_run=dry_run)
            if not dry_run:
                _ok(f"Copied {written} file(s).")
        else:
            _ok("All files are already up to date.")

    # ── Start scripts ────────────────────────────────────────────────────────
    scripts_updated = 0
    if not packages_only and not files_only:
        scripts_updated = update_start_scripts(usb_root, PROJECT_ROOT, dry_run=dry_run)

    # ── Package diff ─────────────────────────────────────────────────────────
    diff_pkgs = PackageDiff()
    if not files_only:
        req_file = PROJECT_ROOT / "requirements.txt"
        req_hash = sha256_file(req_file) if req_file.exists() else ""

        console.print()
        console.print("  [bold]Checking packages...[/bold]" if _RICH else "  Checking packages...")

        # Update every bundled interpreter present on the USB (both OSes if
        # the stick was flashed for both), using cross-platform wheels.
        present = [(o, runtime.site_packages(usb_root, o))
                   for o in runtime.OS_NAMES
                   if runtime.site_packages(usb_root, o).is_dir()]
        if not present:
            _warn("No USB site-packages found under python-env/.")
            _warn("Run setup_usb.py or flash_usb.py to create the runtime first.")
        for os_name, site_pkgs in present:
            diff_pkgs = compute_package_diff(req_file, site_pkgs, manifest)
            diff_pkgs.usb_root = usb_root
            diff_pkgs.os_name = os_name
            _info(f"{os_name}: {len(diff_pkgs.to_install)} to install, "
                  f"{len(diff_pkgs.up_to_date)} up-to-date")
            if diff_pkgs.to_install:
                ok = apply_package_diff(diff_pkgs, dry_run=dry_run)
                if ok and not dry_run:
                    _ok(f"Installed {len(diff_pkgs.to_install)} package(s) for {os_name}.")
                elif not ok:
                    _err(f"Some packages failed to install for {os_name}.")
                    return False
            else:
                _ok(f"All packages up to date for {os_name}.")
            if not dry_run:
                manifest.installed_packages = _installed_packages_in(site_pkgs)
        manifest.requirements_hash = req_hash

    # ── Write manifest ───────────────────────────────────────────────────────
    if not dry_run and not packages_only:
        # Rebuild file hash snapshot
        for src_file in PROJECT_ROOT.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(PROJECT_ROOT)
            if _should_skip_path(rel):
                continue
            rel_str = str(rel).replace("\\", "/")
            manifest.file_hashes[rel_str] = sha256_file(src_file)
        manifest.save(usb_carry_ai)
        _ok("Manifest saved.")

    elapsed = time.monotonic() - t_start
    print_summary(diff_files, diff_pkgs, scripts_updated, elapsed, dry_run)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="update_usb.py",
        description="Smart carry-ai USB updater — syncs only changed files & packages.",
    )
    parser.add_argument(
        "--target", "-t",
        help="Path to USB drive root (e.g. H:\\ or /media/user/USB).  "
             "Auto-detected if omitted.",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Show what would change without writing anything.",
    )
    parser.add_argument(
        "--packages-only", action="store_true",
        help="Only update Python packages; skip file sync.",
    )
    parser.add_argument(
        "--files-only", action="store_true",
        help="Only sync source files; skip package updates.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print each file as it is evaluated.",
    )
    args = parser.parse_args()

    if args.packages_only and args.files_only:
        parser.error("--packages-only and --files-only are mutually exclusive.")

    _panel(
        "carry-ai USB Smart Updater",
        "Updates your USB drive without re-flashing — only changed files\n"
        "and packages are written.  Models and Python runtime are never touched.",
        style="blue",
    )

    usb_carry_ai = pick_drive(args.target)
    if usb_carry_ai is None:
        return 1

    ok = run_update(
        usb_carry_ai=usb_carry_ai,
        dry_run=args.dry_run,
        packages_only=args.packages_only,
        files_only=args.files_only,
        verbose=args.verbose,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
