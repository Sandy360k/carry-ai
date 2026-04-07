"""
carry-ai/package.py — USB Drive Packaging Script
=================================================

Prepares a complete carry-ai USB drive from a development checkout.
Copies all necessary runtime files, binaries, and configurations
into a target directory (typically the root of a USB drive).

CLI Usage:
    python package.py --target E:\\              # Package to USB drive E:
    python package.py --target ./usb-test       # Local folder for testing
    python package.py --include-models          # Include GGUF models (large!)
    python package.py --strip-models            # Skip models (API-only)
    python package.py --validate-only           # Check components without copying
"""

import argparse
import logging
import os
import platform
import shutil
import sys
from pathlib import Path

log = logging.getLogger("carry-ai.package")

PROJECT_ROOT = Path(__file__).resolve().parent

# Directories/files that form the carry-ai source runtime
SOURCE_DIRS = [
    "modes",
    "providers",
    "agent",
    "inject",
    "cleanup",
    "ui",
    "mcp",
    "plugins",
    "cowork",
    "config",
    "crypto",
]

SOURCE_FILES = [
    "launcher.py",
    "package.py",
    "requirements.txt",
    "README.md",
]

# Files/dirs to exclude from copy
EXCLUDE_PATTERNS = {
    "__pycache__", ".pyc", ".pyo", ".git", ".gitignore",
    "_dry_run_session", ".env", "*.egg-info",
}

# Windows autorun template
AUTORUN_INF = """\
[autorun]
label=carry-ai
icon=carry-ai\\icon.ico
open=carry-ai\\start.bat
action=Launch carry-ai Portable AI Assistant
"""

# Windows batch launcher
START_BAT = """\
@echo off
title carry-ai
echo Starting carry-ai...
echo.

:: Try portable Python first, then system Python
if exist "%~dp0python\\python.exe" (
    "%~dp0python\\python.exe" "%~dp0carry-ai\\launcher.py" %*
) else if exist "%~dp0python\\Scripts\\python.exe" (
    "%~dp0python\\Scripts\\python.exe" "%~dp0carry-ai\\launcher.py" %*
) else (
    python "%~dp0carry-ai\\launcher.py" %*
)

if %ERRORLEVEL% neq 0 (
    echo.
    echo carry-ai exited with error %ERRORLEVEL%.
    pause
)
"""

# Linux shell launcher
START_SH = """\
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Try portable Python first, then system Python
if [[ -x "$SCRIPT_DIR/python/bin/python3" ]]; then
    PYTHON="$SCRIPT_DIR/python/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo "[carry-ai] Error: Python 3 not found."
    echo "Install Python 3.11+ or place a portable Python in $SCRIPT_DIR/python/"
    exit 1
fi

echo "[carry-ai] Using Python: $PYTHON"
exec "$PYTHON" "$SCRIPT_DIR/carry-ai/launcher.py" "$@"
"""

# Linux .desktop file
DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name=carry-ai
Comment=Portable AI Assistant
Exec=bash %s/start.sh
Icon=utilities-terminal
Terminal=true
Categories=Utility;
"""


# ===================================================================
# Validation
# ===================================================================

def validate_components(include_models: bool = False) -> list[str]:
    """Check that all required components exist in the project.

    Returns:
        List of warning/error strings. Empty = all good.
    """
    issues = []

    # Source files
    for f in SOURCE_FILES:
        if not (PROJECT_ROOT / f).is_file():
            issues.append(f"Missing source file: {f}")

    # Source dirs
    for d in SOURCE_DIRS:
        path = PROJECT_ROOT / d
        if not path.is_dir():
            issues.append(f"Missing source directory: {d}/")
        else:
            py_files = list(path.glob("*.py"))
            if not py_files:
                issues.append(f"Directory {d}/ has no .py files")

    # requirements.txt should be non-empty
    req = PROJECT_ROOT / "requirements.txt"
    if req.is_file() and req.stat().st_size < 10:
        issues.append("requirements.txt appears empty")

    # Models (optional)
    models_dir = PROJECT_ROOT / "models"
    gguf_files = list(models_dir.glob("*.gguf")) if models_dir.is_dir() else []
    if include_models and not gguf_files:
        issues.append("No .gguf models found in models/ (use --download-model first)")
    elif not include_models:
        if gguf_files:
            total_gb = sum(f.stat().st_size for f in gguf_files) / (1024 ** 3)
            issues.append(f"INFO: {len(gguf_files)} model(s) found ({total_gb:.1f} GB) — use --include-models to bundle")

    # llama.cpp binary (optional)
    bin_dir = PROJECT_ROOT / "bin"
    if bin_dir.is_dir():
        bins = list(bin_dir.iterdir())
        if bins:
            log.info("Found binaries: %s", ", ".join(b.name for b in bins))
    else:
        issues.append("INFO: No bin/ directory. llama-server must be on PATH or added later.")

    # Encrypted keys (optional)
    enc_file = PROJECT_ROOT / "config" / "providers.enc"
    if enc_file.is_file():
        content = enc_file.read_text(encoding="utf-8").strip()
        if content.startswith("#"):
            issues.append("INFO: config/providers.enc is placeholder — keys not configured yet")
        else:
            log.info("Encrypted API keys found.")

    return issues


# ===================================================================
# Copy operations
# ===================================================================

def _should_exclude(path: Path) -> bool:
    """Check if a path should be excluded from copy."""
    name = path.name
    for pat in EXCLUDE_PATTERNS:
        if pat.startswith("*"):
            if name.endswith(pat[1:]):
                return True
        elif name == pat:
            return True
    return False


def copy_source(target_dir: Path) -> int:
    """Copy carry-ai source tree to target.

    Returns:
        Number of files copied.
    """
    carry_ai_dst = target_dir / "carry-ai"
    carry_ai_dst.mkdir(parents=True, exist_ok=True)

    file_count = 0

    # Copy individual source files
    for f in SOURCE_FILES:
        src = PROJECT_ROOT / f
        if src.is_file():
            shutil.copy2(src, carry_ai_dst / f)
            file_count += 1

    # Copy source directories
    for d in SOURCE_DIRS:
        src = PROJECT_ROOT / d
        dst = carry_ai_dst / d
        if not src.is_dir():
            continue

        if dst.exists():
            shutil.rmtree(dst)

        shutil.copytree(
            src, dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".git"),
        )
        file_count += sum(1 for _ in dst.rglob("*") if _.is_file())

    log.info("Copied source tree: %d files -> %s", file_count, carry_ai_dst)
    return file_count


def copy_models(target_dir: Path) -> int:
    """Copy GGUF models to target.

    Returns:
        Number of models copied.
    """
    models_src = PROJECT_ROOT / "models"
    models_dst = target_dir / "carry-ai" / "models"
    models_dst.mkdir(parents=True, exist_ok=True)

    # Always copy .gitkeep and downloader
    for f in ["__init__.py", "downloader.py", ".gitkeep"]:
        src = models_src / f
        if src.is_file():
            shutil.copy2(src, models_dst / f)

    gguf_files = sorted(models_src.glob("*.gguf"))
    if not gguf_files:
        log.info("No GGUF models to copy.")
        return 0

    for model in gguf_files:
        size_gb = model.stat().st_size / (1024 ** 3)
        log.info("Copying model: %s (%.2f GB)...", model.name, size_gb)
        shutil.copy2(model, models_dst / model.name)

    log.info("Copied %d model(s).", len(gguf_files))
    return len(gguf_files)


def copy_binaries(target_dir: Path) -> int:
    """Copy llama.cpp and other binaries.

    Returns:
        Number of binaries copied.
    """
    bin_src = PROJECT_ROOT / "bin"
    if not bin_src.is_dir():
        return 0

    bin_dst = target_dir / "carry-ai" / "bin"
    bin_dst.mkdir(parents=True, exist_ok=True)

    count = 0
    for item in bin_src.iterdir():
        if _should_exclude(item):
            continue
        shutil.copy2(item, bin_dst / item.name)
        count += 1

    log.info("Copied %d binarie(s).", count)
    return count


# ===================================================================
# Launcher generation
# ===================================================================

def generate_launchers(target_dir: Path) -> None:
    """Generate platform-specific launch scripts at the USB root."""

    # Windows: autorun.inf + start.bat
    autorun_path = target_dir / "autorun.inf"
    autorun_path.write_text(AUTORUN_INF, encoding="utf-8")
    log.info("Generated: %s", autorun_path)

    bat_path = target_dir / "start.bat"
    bat_path.write_text(START_BAT, encoding="utf-8")
    log.info("Generated: %s", bat_path)

    # Linux: start.sh + .desktop
    sh_path = target_dir / "start.sh"
    sh_path.write_text(START_SH, encoding="utf-8")
    # Make executable (best-effort on Windows)
    try:
        sh_path.chmod(0o755)
    except (OSError, AttributeError):
        pass
    log.info("Generated: %s", sh_path)

    desktop_path = target_dir / "carry-ai.desktop"
    desktop_path.write_text(DESKTOP_ENTRY % str(target_dir), encoding="utf-8")
    try:
        desktop_path.chmod(0o755)
    except (OSError, AttributeError):
        pass
    log.info("Generated: %s", desktop_path)


# ===================================================================
# Verification
# ===================================================================

def verify_structure(target_dir: Path) -> tuple[bool, list[str]]:
    """Verify the packaged USB structure is complete.

    Returns:
        (valid, issues) — valid is True if all critical files are present.
    """
    issues = []
    carry_ai = target_dir / "carry-ai"

    # Critical files
    critical = [
        "launcher.py",
        "modes/__init__.py",
        "modes/local_mode.py",
        "modes/api_mode.py",
        "providers/__init__.py",
        "providers/base.py",
        "agent/__init__.py",
        "agent/agent.py",
        "agent/tools.py",
        "inject/__init__.py",
        "cleanup/__init__.py",
        "cleanup/cleanup.py",
        "ui/__init__.py",
        "ui/app.py",
        "config/__init__.py",
        "crypto/__init__.py",
        "crypto/keystore.py",
    ]

    for f in critical:
        if not (carry_ai / f).is_file():
            issues.append(f"Missing critical file: carry-ai/{f}")

    # Launcher scripts
    if not (target_dir / "start.bat").is_file():
        issues.append("Missing: start.bat")
    if not (target_dir / "start.sh").is_file():
        issues.append("Missing: start.sh")

    valid = len([i for i in issues if not i.startswith("INFO:")]) == 0
    return valid, issues


def calculate_size(target_dir: Path) -> dict:
    """Calculate size breakdown of the packaged USB."""
    sizes = {}
    total = 0

    for item in target_dir.rglob("*"):
        if not item.is_file():
            continue
        size = item.stat().st_size
        total += size

        # Categorize
        rel = item.relative_to(target_dir)
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "carry-ai":
            category = parts[1]
            if category == "models":
                cat = "models"
            elif category == "bin":
                cat = "binaries"
            else:
                cat = "source"
        else:
            cat = "launchers"

        sizes[cat] = sizes.get(cat, 0) + size

    sizes["total"] = total
    return sizes


# ===================================================================
# Main
# ===================================================================

def package(target_dir: Path, include_models: bool = False,
            validate_only: bool = False) -> bool:
    """Execute the full packaging pipeline.

    Returns:
        True if packaging succeeded.
    """
    print(f"\n  carry-ai USB Packager")
    print(f"  Target: {target_dir}")
    print()

    # Step 1: Validate
    print("  [1/5] Validating components...")
    issues = validate_components(include_models=include_models)
    for issue in issues:
        level = "INFO" if issue.startswith("INFO:") else "WARN"
        print(f"    [{level}] {issue}")

    errors = [i for i in issues if not i.startswith("INFO:")]
    if errors:
        print(f"\n  Found {len(errors)} issue(s). Fix them before packaging.")
        if validate_only:
            return False
        print("  Continuing anyway...\n")

    if validate_only:
        print("  Validation complete." + (" All good!" if not errors else ""))
        return not errors

    # Step 2: Copy source
    print("  [2/5] Copying source tree...")
    target_dir.mkdir(parents=True, exist_ok=True)
    file_count = copy_source(target_dir)
    print(f"    {file_count} files copied.")

    # Step 3: Copy models (optional)
    if include_models:
        print("  [3/5] Copying models...")
        model_count = copy_models(target_dir)
        print(f"    {model_count} model(s) copied.")
    else:
        print("  [3/5] Skipping models (use --include-models to bundle).")
        # Still copy the models directory structure
        models_dst = target_dir / "carry-ai" / "models"
        models_dst.mkdir(parents=True, exist_ok=True)
        gitkeep = PROJECT_ROOT / "models" / ".gitkeep"
        if gitkeep.is_file():
            shutil.copy2(gitkeep, models_dst / ".gitkeep")
        for f in ["__init__.py", "downloader.py"]:
            src = PROJECT_ROOT / "models" / f
            if src.is_file():
                shutil.copy2(src, models_dst / f)

    # Step 4: Copy binaries
    print("  [4/5] Copying binaries...")
    bin_count = copy_binaries(target_dir)
    print(f"    {bin_count} binarie(s) copied.")

    # Step 5: Generate launchers
    print("  [5/5] Generating launchers...")
    generate_launchers(target_dir)

    # Verify
    print("\n  Verifying package...")
    valid, verify_issues = verify_structure(target_dir)
    for issue in verify_issues:
        print(f"    [!] {issue}")

    # Size summary
    sizes = calculate_size(target_dir)
    print("\n  Size Summary:")
    for cat, size in sorted(sizes.items()):
        if cat == "total":
            continue
        print(f"    {cat:<12} {size / (1024**2):>8.1f} MB")
    print(f"    {'-' * 25}")
    total_mb = sizes.get("total", 0) / (1024 ** 2)
    print(f"    {'TOTAL':<12} {total_mb:>8.1f} MB")

    if valid:
        print(f"\n  Packaging complete! USB is ready at: {target_dir}")
        print(f"  Windows: Double-click start.bat (or plug in for autorun)")
        print(f"  Linux:   bash start.sh")
    else:
        print(f"\n  Packaging completed with issues. Review warnings above.")

    print()
    return valid


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="carry-ai-packager",
        description="Package carry-ai onto a USB drive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python package.py --target E:\\              # Package to USB drive
  python package.py --target ./usb-test       # Local folder test
  python package.py --include-models          # Bundle GGUF models
  python package.py --validate-only           # Check without copying
        """,
    )
    parser.add_argument(
        "--target", type=str, required=True,
        help="Target directory (USB drive root or test folder)",
    )
    parser.add_argument(
        "--include-models", action="store_true", default=False,
        help="Include GGUF model files (can be several GB)",
    )
    parser.add_argument(
        "--strip-models", action="store_true", default=False,
        help="Explicitly exclude models (API-only package)",
    )
    parser.add_argument(
        "--validate-only", action="store_true", default=False,
        help="Only validate components, don't copy anything",
    )
    parser.add_argument(
        "--verbose", action="store_true", default=False,
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    target = Path(args.target).resolve()
    include_models = args.include_models and not args.strip_models

    success = package(
        target_dir=target,
        include_models=include_models,
        validate_only=args.validate_only,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
