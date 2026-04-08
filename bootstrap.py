"""
carry-ai/bootstrap.py — USB Package Injector
=============================================

Thin entrypoint that inserts the USB-local site-packages directory at the
front of sys.path *before* any carry-ai module is imported, then hands off
to launcher.main().

Why this exists:
  When setup_usb.py has pre-installed packages onto the USB under
  python-env/{windows,linux}/site-packages/, this script ensures Python
  finds those packages first — even if the same package is installed (at
  a different version) on the host machine.

Usage:
  # From USB root
  python carry-ai/bootstrap.py [launcher args...]

  # start.bat / start.sh call this automatically when env is present.
  # You can also call launcher.py directly; it will use system packages.

USB layout expected:
  USB_ROOT/
  ├── carry-ai/
  │   └── bootstrap.py     ← this file
  └── python-env/
      ├── windows/
      │   └── Lib/site-packages/
      └── linux/
          └── site-packages/
"""

import os
import platform
import sys
from pathlib import Path


def _inject_usb_packages() -> bool:
    """Prepend USB-local site-packages to sys.path.

    Returns True if a USB package directory was found and injected.
    """
    # This file lives at carry-ai/bootstrap.py.
    # USB root is its grandparent (USB_ROOT/carry-ai/).
    carry_ai_dir = Path(__file__).resolve().parent
    usb_root = carry_ai_dir.parent

    os_name = platform.system().lower()
    if os_name == "windows":
        site_pkgs = usb_root / "python-env" / "windows" / "Lib" / "site-packages"
    else:
        # Linux / macOS
        site_pkgs = usb_root / "python-env" / "linux" / "site-packages"

    if site_pkgs.is_dir():
        sys.path.insert(0, str(site_pkgs))
        # Also add the carry-ai/ dir itself so relative imports work
        if str(carry_ai_dir) not in sys.path:
            sys.path.insert(1, str(carry_ai_dir))
        return True

    return False


def main() -> None:
    injected = _inject_usb_packages()

    if injected:
        # Optional: print to stderr so it doesn't pollute stdout pipelines
        print("[carry-ai] USB packages injected.", file=sys.stderr)
    else:
        print(
            "[carry-ai] No USB package env found — using system packages.\n"
            "           Run 'python carry-ai/setup_usb.py' to build the USB env.",
            file=sys.stderr,
        )

    # Hand off to the real launcher — pass through all CLI args
    try:
        from launcher import main as launcher_main
        launcher_main(sys.argv[1:])
    except ImportError as e:
        print(f"[carry-ai] Cannot import launcher: {e}", file=sys.stderr)
        print("           Ensure you are running from the carry-ai/ directory.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
