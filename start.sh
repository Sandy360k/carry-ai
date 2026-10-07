#!/usr/bin/env bash
# ==============================================================
# carry-ai — Linux / macOS Launcher
# ==============================================================
# Boots carry-ai through launcher.py with the Python bundled on the USB,
# so nothing needs to be installed on the host. The session lives in RAM
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

# ---- Resolve Python interpreter ------------------------------
# 1. Python bundled on the USB (portable/runtime.py) — nothing to install.
# 2. If the stick is mounted noexec (common for exFAT/FAT automounts), run a
#    copy from RAM inside the session dir; the wipe removes it with the rest.
# 3. Host python3 as a last resort.
USB_PY_HOME="$SCRIPT_DIR/python-env/linux/python"
USB_PY="$USB_PY_HOME/bin/python3.12"
PY=""
if [ -f "$USB_PY" ]; then
    if "$USB_PY" -c "" 2>/dev/null; then
        PY="$USB_PY"
    else
        for base in /dev/shm "${XDG_RUNTIME_DIR:-}" /tmp; do
            { [ -n "$base" ] && [ -d "$base" ] && [ -w "$base" ]; } || continue
            SESSION="$base/ai_session"
            mkdir -p "$SESSION" && rm -rf "$SESSION/python"
            cp -r "$USB_PY_HOME" "$SESSION/python" 2>/dev/null || continue
            chmod +x "$SESSION/python/bin/python3.12" 2>/dev/null || true
            if "$SESSION/python/bin/python3.12" -c "" 2>/dev/null; then
                PY="$SESSION/python/bin/python3.12"
                export CARRY_AI_SESSION_DIR="$SESSION"
                # Compiled wheels (cryptography, psutil, ...) can't mmap from a
                # noexec mount either, so stage site-packages in RAM too.
                if [ -d "$USB_SITE" ]; then
                    cp -r "$USB_SITE" "$SESSION/site-packages" 2>/dev/null \
                        && USB_SITE="$SESSION/site-packages"
                fi
                echo "[carry-ai] USB is mounted noexec — running from $SESSION" >&2
                break
            fi
            rm -rf "$SESSION/python"
        done
    fi
fi
if [ -z "$PY" ]; then
    if command -v python3 &>/dev/null; then
        PY=python3
    elif command -v python &>/dev/null; then
        PY=python
    else
        echo "[carry-ai] ERROR: no Python found."
        echo "  Re-run flash_usb.py with the Linux runtime, or install python3."
        exit 1
    fi
    echo "[carry-ai] Bundled Python not available — using host $PY" >&2
fi

# ---- Inject USB packages -------------------------------------
if [ -d "$USB_SITE" ]; then
    export PYTHONPATH="$USB_SITE${PYTHONPATH:+:$PYTHONPATH}"
    echo "[carry-ai] USB packages: $USB_SITE" >&2
fi

# ---- Launch (desktop app, falling back to the web UI) --------
exec "$PY" "$BOOTSTRAP" --ui desktop "$@"
