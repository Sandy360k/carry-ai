#!/usr/bin/env bash
# ==============================================================
# carry-ai — Linux / macOS Launcher
# ==============================================================
# Injects the USB-local site-packages into PYTHONPATH so the
# host machine needs only Python 3.10+ — no pip install needed.
#
# To build the USB-local package env, run once:
#   python3 carry-ai/setup_usb.py
# ==============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USB_SITE="$SCRIPT_DIR/python-env/linux/site-packages"
BOOTSTRAP="$SCRIPT_DIR/carry-ai/bootstrap.py"

# ---- Inject USB packages if present --------------------------
if [ -d "$USB_SITE" ]; then
    export PYTHONPATH="$USB_SITE${PYTHONPATH:+:$PYTHONPATH}"
    echo "[carry-ai] USB packages: $USB_SITE" >&2
else
    echo "[carry-ai] No USB package env found." >&2
    echo "           Run: python3 carry-ai/setup_usb.py  to build it." >&2
fi

# ---- Resolve Python interpreter ------------------------------
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo ""
    echo "[carry-ai] ERROR: Python 3.10+ not found on this machine."
    echo ""
    echo "  Install options:"
    echo "    Ubuntu/Debian : sudo apt install python3"
    echo "    Arch          : sudo pacman -S python"
    echo "    Fedora        : sudo dnf install python3"
    echo "    macOS         : brew install python3"
    echo ""
    exit 1
fi

# ---- Check Python version ------------------------------------
PY_VER=$("$PY" -c "import sys; print(sys.version_info[:2])" 2>/dev/null || echo "(0, 0)")
if "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
    : # OK
else
    echo "[carry-ai] WARNING: Python 3.10+ required. Found: $("$PY" --version 2>&1)" >&2
fi

# ---- Launch --------------------------------------------------
exec "$PY" "$BOOTSTRAP" "$@"
