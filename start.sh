#!/usr/bin/env bash
# ==============================================================
# carry-ai — Linux / macOS Launcher
# ==============================================================
# Boots carry-ai through launcher.py so the session lives in RAM
# (/dev/shm) and is wiped on exit or when the USB is pulled.
#
# Priority for UI (decided by launcher.py --ui desktop):
#   1. Desktop app  (ui/desktop.py)  — native window
#   2. Web UI       — isolated browser window, throwaway profile
# ==============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USB_SITE="$SCRIPT_DIR/python-env/linux/site-packages"
BOOTSTRAP="$SCRIPT_DIR/carry-ai/bootstrap.py"

# ---- Inject USB packages if present --------------------------
if [ -d "$USB_SITE" ]; then
    export PYTHONPATH="$USB_SITE${PYTHONPATH:+:$PYTHONPATH}"
    echo "[carry-ai] USB packages: $USB_SITE" >&2
fi

# ---- Resolve Python interpreter ------------------------------
PY=""
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "[carry-ai] ERROR: Python 3.10+ not found."
    echo "  Install: sudo apt install python3  (or brew install python3)"
    exit 1
fi

# ---- Launch (desktop app, falling back to the web UI) --------
exec "$PY" "$BOOTSTRAP" --ui desktop "$@"
