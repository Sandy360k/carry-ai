#!/usr/bin/env bash
# ==============================================================
# carry-ai — Linux / macOS Launcher
# ==============================================================
# Launches the desktop chat app. Falls back to the web UI.
#
# Priority for UI:
#   1. Desktop app  (ui/desktop.py)  — native window
#   2. Web UI       (launcher.py)    — browser at localhost:8080
# ==============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USB_SITE="$SCRIPT_DIR/python-env/linux/site-packages"
DESKTOP="$SCRIPT_DIR/carry-ai/ui/desktop.py"
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

# ---- Launch desktop app (or fall back to web UI) -------------
if [ -f "$DESKTOP" ]; then
    echo "[carry-ai] Starting desktop app..." >&2
    exec "$PY" "$DESKTOP" "$@"
else
    echo "[carry-ai] Desktop app not found, starting web UI..." >&2
    exec "$PY" "$BOOTSTRAP" "$@"
fi
