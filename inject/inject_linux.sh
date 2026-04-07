#!/usr/bin/env bash
# =================================================================
# carry-ai/inject/inject_linux.sh — Linux Session Injection
# =================================================================
#
# Sets up a RAM-backed session on Linux using tmpfs. The AI runtime
# literally never touches the host disk — everything lives in RAM.
#
# Usage:
#   sudo bash inject_linux.sh [--dry-run] [--size 512M] [--no-udev]
#
# Environment Variables:
#   USB_MOUNT    — USB mount point (auto-detected if not set)
#   SESSION_SIZE — tmpfs size (default: 512M)
#   SESSION_DIR  — session path (default: /tmp/ai_session)
#
# Dependencies:
#   mount, udevadm, cp, python3, lsblk (for auto-detect)
# =================================================================

set -euo pipefail

# -----------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

SESSION_DIR="${SESSION_DIR:-/tmp/ai_session}"
SESSION_SIZE="${SESSION_SIZE:-512M}"
USB_MOUNT="${USB_MOUNT:-}"
DRY_RUN=0
NO_UDEV=0
UDEV_RULE_PATH="/etc/udev/rules.d/99-carry-ai-cleanup.rules"
CLEANUP_SCRIPT="${SESSION_DIR}/cleanup.sh"
PID_FILE="${SESSION_DIR}/carry-ai.pid"

# Items to copy from USB into tmpfs session (relative to PROJECT_DIR)
RUNTIME_ITEMS=(
    launcher.py
    modes
    providers
    agent
    cleanup
    ui
    mcp
    plugins
    cowork
    config
    crypto
)

# -----------------------------------------------------------------
# Logging helpers
# -----------------------------------------------------------------
_log()  { echo "[carry-ai] $*"; }
_info() { _log "INFO:  $*"; }
_warn() { _log "WARN:  $*"; }
_err()  { _log "ERROR: $*" >&2; }
_die()  { _err "$*"; exit 1; }

# -----------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)  DRY_RUN=1; shift ;;
            --no-udev)  NO_UDEV=1; shift ;;
            --size)
                [[ -n "${2:-}" ]] || _die "--size requires a value (e.g. 512M)"
                SESSION_SIZE="$2"; shift 2 ;;
            --size=*)   SESSION_SIZE="${1#*=}"; shift ;;
            --help|-h)
                echo "Usage: sudo bash inject_linux.sh [--dry-run] [--size SIZE] [--no-udev]"
                echo ""
                echo "Options:"
                echo "  --dry-run     Create session dir without tmpfs mount or udev rule"
                echo "  --size SIZE   tmpfs size (default: 512M)"
                echo "  --no-udev     Skip udev rule registration"
                echo ""
                echo "Environment:"
                echo "  USB_MOUNT     USB mount point (auto-detected if not set)"
                echo "  SESSION_DIR   Session path (default: /tmp/ai_session)"
                echo "  SESSION_SIZE  tmpfs size (default: 512M)"
                exit 0
                ;;
            *)  _die "Unknown option: $1. Use --help for usage." ;;
        esac
    done
}

# -----------------------------------------------------------------
# USB auto-detection
# -----------------------------------------------------------------
detect_usb_mount() {
    # If USB_MOUNT is already set and valid, use it
    if [[ -n "$USB_MOUNT" && -d "$USB_MOUNT/carry-ai" ]]; then
        _info "Using provided USB_MOUNT: $USB_MOUNT"
        return 0
    fi

    # Strategy 1: derive from script location
    # If this script is on the USB, PROJECT_DIR's parent is the USB root
    local candidate
    candidate="$(dirname "$PROJECT_DIR")"
    if [[ -d "$candidate/carry-ai" ]]; then
        # Verify it's actually a removable device
        local dev
        dev="$(df --output=source "$candidate" 2>/dev/null | tail -1)" || true
        if [[ -n "$dev" ]]; then
            local removable
            removable="$(lsblk -ndo RM "$dev" 2>/dev/null)" || true
            if [[ "$removable" == "1" ]]; then
                USB_MOUNT="$candidate"
                _info "Auto-detected USB mount (from script path): $USB_MOUNT"
                return 0
            fi
        fi
        # Even if not detected as removable, use it (could be testing from HDD)
        USB_MOUNT="$candidate"
        _info "Using parent directory as USB root: $USB_MOUNT"
        return 0
    fi

    # Strategy 2: scan mounted removable devices for carry-ai/
    local mount_point
    while IFS= read -r mount_point; do
        if [[ -d "$mount_point/carry-ai" ]]; then
            USB_MOUNT="$mount_point"
            _info "Found carry-ai on removable device: $USB_MOUNT"
            return 0
        fi
    done < <(lsblk -nro MOUNTPOINT,RM 2>/dev/null | awk '$2 == "1" && $1 != "" {print $1}')

    # Strategy 3: check common USB mount paths
    local common_paths=("/media" "/mnt" "/run/media")
    for base in "${common_paths[@]}"; do
        [[ -d "$base" ]] || continue
        while IFS= read -r mount_point; do
            if [[ -d "$mount_point/carry-ai" ]]; then
                USB_MOUNT="$mount_point"
                _info "Found carry-ai at: $USB_MOUNT"
                return 0
            fi
        done < <(find "$base" -maxdepth 2 -type d -name "carry-ai" 2>/dev/null \
                 | while read -r d; do dirname "$d"; done)
    done

    _warn "Could not auto-detect USB mount point."
    _warn "Set USB_MOUNT=/path/to/usb or ensure carry-ai/ is on a mounted USB."
    return 1
}

# -----------------------------------------------------------------
# Get USB block device (for udev rule)
# -----------------------------------------------------------------
get_usb_block_device() {
    # Returns the block device path (e.g., /dev/sdb1) for the USB mount
    local dev
    dev="$(df --output=source "$USB_MOUNT" 2>/dev/null | tail -1)" || true

    if [[ -z "$dev" || "$dev" == "Filesystem" ]]; then
        _warn "Could not determine block device for $USB_MOUNT"
        echo ""
        return 1
    fi

    echo "$dev"
}

get_usb_device_id() {
    # Returns USB vendor:product ID for udev matching (e.g., "0781:5583")
    local block_dev="$1"

    # Strip partition number to get base device (e.g., /dev/sdb1 -> /dev/sdb)
    local base_dev
    base_dev="$(echo "$block_dev" | sed 's/[0-9]*$//')"
    local dev_name
    dev_name="$(basename "$base_dev")"

    # Read from sysfs
    local vendor_id product_id
    local sysfs_path="/sys/block/$dev_name/device/../../"

    vendor_id="$(cat "${sysfs_path}/idVendor" 2>/dev/null)" || true
    product_id="$(cat "${sysfs_path}/idProduct" 2>/dev/null)" || true

    if [[ -n "$vendor_id" && -n "$product_id" ]]; then
        echo "${vendor_id}:${product_id}"
        return 0
    fi

    # Fallback: try udevadm
    local info
    info="$(udevadm info --query=property --name="$block_dev" 2>/dev/null)" || true
    vendor_id="$(echo "$info" | grep "^ID_VENDOR_ID=" | cut -d= -f2)" || true
    product_id="$(echo "$info" | grep "^ID_MODEL_ID=" | cut -d= -f2)" || true

    if [[ -n "$vendor_id" && -n "$product_id" ]]; then
        echo "${vendor_id}:${product_id}"
        return 0
    fi

    _warn "Could not determine USB device ID."
    echo ""
    return 1
}

# -----------------------------------------------------------------
# tmpfs mount
# -----------------------------------------------------------------
mount_tmpfs() {
    if mountpoint -q "$SESSION_DIR" 2>/dev/null; then
        _warn "$SESSION_DIR is already a mountpoint. Unmounting old session."
        umount -f "$SESSION_DIR" 2>/dev/null || true
    fi

    if [[ -d "$SESSION_DIR" ]]; then
        rm -rf "$SESSION_DIR"
    fi

    mkdir -p "$SESSION_DIR"

    mount -t tmpfs -o size="$SESSION_SIZE",mode=700,uid="$(id -u)",gid="$(id -g)" \
        tmpfs "$SESSION_DIR"

    _info "tmpfs mounted: $SESSION_DIR (size=$SESSION_SIZE, mode=700)"
}

# -----------------------------------------------------------------
# Copy runtime into tmpfs
# -----------------------------------------------------------------
copy_runtime() {
    local src_root="${USB_MOUNT}/carry-ai"
    local dst_root="${SESSION_DIR}/runtime"

    mkdir -p "$dst_root"

    local count=0
    for item in "${RUNTIME_ITEMS[@]}"; do
        local src="${src_root}/${item}"
        local dst="${dst_root}/${item}"

        if [[ ! -e "$src" ]]; then
            _warn "Skipping missing: $item"
            continue
        fi

        if [[ -d "$src" ]]; then
            cp -a "$src" "$dst"
        else
            cp -a "$src" "$dst"
        fi
        count=$((count + 1))
    done

    # Create cache and logs dirs
    mkdir -p "${SESSION_DIR}/cache" "${SESSION_DIR}/logs"

    _info "Runtime copied to tmpfs ($count items)."
}

# -----------------------------------------------------------------
# Generate cleanup script (embedded in tmpfs, called by udev)
# -----------------------------------------------------------------
generate_cleanup_script() {
    cat > "$CLEANUP_SCRIPT" << 'CLEANUP_EOF'
#!/usr/bin/env bash
# =================================================================
# carry-ai auto-cleanup — triggered on USB removal
# This script self-deletes after execution.
# =================================================================
set -uo pipefail

SESSION_DIR="${SESSION_DIR:-/tmp/ai_session}"
UDEV_RULE="/etc/udev/rules.d/99-carry-ai-cleanup.rules"
PID_FILE="${SESSION_DIR}/carry-ai.pid"
LOG="/tmp/carry-ai-cleanup.log"

log() { echo "$(date '+%H:%M:%S') [cleanup] $*" >> "$LOG" 2>/dev/null; }

log "=== Cleanup triggered ==="

# 1. Kill carry-ai processes
if [[ -f "$PID_FILE" ]]; then
    MAIN_PID="$(cat "$PID_FILE" 2>/dev/null)" || true
    if [[ -n "$MAIN_PID" ]]; then
        log "Killing process tree from PID $MAIN_PID"
        # Kill child processes first, then parent
        pkill -TERM -P "$MAIN_PID" 2>/dev/null || true
        sleep 1
        kill -TERM "$MAIN_PID" 2>/dev/null || true
        sleep 2
        # Force kill survivors
        pkill -KILL -P "$MAIN_PID" 2>/dev/null || true
        kill -KILL "$MAIN_PID" 2>/dev/null || true
    fi
fi

# Kill any remaining carry-ai processes by name
pkill -f "carry-ai" 2>/dev/null || true
pkill -f "llama-server" 2>/dev/null || true
pkill -f "ai_session" 2>/dev/null || true
sleep 1
pkill -9 -f "carry-ai" 2>/dev/null || true
pkill -9 -f "llama-server" 2>/dev/null || true

log "Processes killed."

# 2. Clear clipboard
if command -v xclip &>/dev/null; then
    echo -n "" | xclip -selection clipboard 2>/dev/null || true
    echo -n "" | xclip -selection primary 2>/dev/null || true
    log "Clipboard cleared (xclip)."
elif command -v xsel &>/dev/null; then
    xsel --clipboard --clear 2>/dev/null || true
    xsel --primary --clear 2>/dev/null || true
    log "Clipboard cleared (xsel)."
elif command -v wl-copy &>/dev/null; then
    wl-copy --clear 2>/dev/null || true
    log "Clipboard cleared (wl-copy)."
fi

# 3. Unmount and remove session directory
if mountpoint -q "$SESSION_DIR" 2>/dev/null; then
    umount -l "$SESSION_DIR" 2>/dev/null || umount -f "$SESSION_DIR" 2>/dev/null || true
    log "tmpfs unmounted: $SESSION_DIR"
fi
rm -rf "$SESSION_DIR" 2>/dev/null || true
log "Session directory removed."

# 4. Remove udev rule
if [[ -f "$UDEV_RULE" ]]; then
    rm -f "$UDEV_RULE"
    udevadm control --reload-rules 2>/dev/null || true
    log "udev rule removed and reloaded."
fi

# 5. Clear any stale recent file references
RECENT_FILE="${HOME}/.local/share/recently-used.xbel"
if [[ -f "$RECENT_FILE" ]]; then
    sed -i '/ai_session/d' "$RECENT_FILE" 2>/dev/null || true
    log "Cleaned recently-used.xbel."
fi

# 6. Remove this cleanup log after a short delay
(sleep 5 && rm -f "$LOG" 2>/dev/null) &

log "=== Cleanup complete ==="
CLEANUP_EOF

    chmod 755 "$CLEANUP_SCRIPT"
    _info "Cleanup script generated: $CLEANUP_SCRIPT"
}

# -----------------------------------------------------------------
# Register udev rule
# -----------------------------------------------------------------
register_udev_rule() {
    local block_dev
    block_dev="$(get_usb_block_device)" || true

    if [[ -z "$block_dev" ]]; then
        _warn "Cannot determine USB block device. Falling back to generic rule."
        # Generic rule: fires on any USB mass storage removal
        cat > "$UDEV_RULE" << UDEV_EOF
# carry-ai: cleanup on USB mass storage removal
ACTION=="remove", SUBSYSTEM=="block", ENV{ID_BUS}=="usb", RUN+="/bin/bash -c 'SESSION_DIR=${SESSION_DIR} ${CLEANUP_SCRIPT}'"
UDEV_EOF
    else
        # Try to get specific device ID for targeted rule
        local device_id
        device_id="$(get_usb_device_id "$block_dev")" || true

        if [[ -n "$device_id" ]]; then
            local vendor product
            vendor="${device_id%%:*}"
            product="${device_id##*:}"

            cat > "$UDEV_RULE" << UDEV_EOF
# carry-ai: cleanup when specific USB device (${vendor}:${product}) is removed
ACTION=="remove", SUBSYSTEM=="block", ENV{ID_BUS}=="usb", ATTRS{idVendor}=="${vendor}", ATTRS{idProduct}=="${product}", RUN+="/bin/bash -c 'SESSION_DIR=${SESSION_DIR} ${CLEANUP_SCRIPT}'"
UDEV_EOF
            _info "udev rule targets device ${vendor}:${product}"
        else
            # Fallback: match by kernel device name
            local dev_name
            dev_name="$(basename "$block_dev")"

            cat > "$UDEV_RULE" << UDEV_EOF
# carry-ai: cleanup when USB block device ${dev_name} is removed
ACTION=="remove", SUBSYSTEM=="block", KERNEL=="${dev_name}", RUN+="/bin/bash -c 'SESSION_DIR=${SESSION_DIR} ${CLEANUP_SCRIPT}'"
UDEV_EOF
            _info "udev rule targets kernel device: $dev_name"
        fi
    fi

    udevadm control --reload-rules 2>/dev/null || true
    _info "udev rule installed: $UDEV_RULE"
}

# -----------------------------------------------------------------
# Write PID file (so cleanup knows what to kill)
# -----------------------------------------------------------------
write_pid_file() {
    # Write the parent shell's PID. The Python launcher (our child) will
    # overwrite this with its own PID once it starts.
    echo "$$" > "$PID_FILE"
    _info "PID file: $PID_FILE (PID=$$)"
}

# -----------------------------------------------------------------
# Dry-run mode
# -----------------------------------------------------------------
do_dry_run() {
    _info "[dry-run] Creating session directory (no tmpfs, no udev)."
    mkdir -p "$SESSION_DIR"/{runtime,cache,logs}
    chmod 700 "$SESSION_DIR"

    if [[ -n "$USB_MOUNT" ]]; then
        copy_runtime
    else
        _warn "[dry-run] No USB_MOUNT detected. Skipping runtime copy."
    fi

    generate_cleanup_script
    write_pid_file

    _info "[dry-run] Session ready at: $SESSION_DIR"
    _info "[dry-run] To clean up: bash $CLEANUP_SCRIPT"
}

# -----------------------------------------------------------------
# Full injection
# -----------------------------------------------------------------
do_inject() {
    # Check root/sudo for tmpfs mount and udev
    if [[ "$(id -u)" -ne 0 ]]; then
        _err "Root privileges required for tmpfs mount and udev rule."
        _err "Re-run with: sudo bash $0 $*"
        _err "Or use --dry-run for testing without root."
        exit 1
    fi

    # 1. Mount tmpfs
    mount_tmpfs

    # 2. Detect USB and copy runtime
    if [[ -n "$USB_MOUNT" ]]; then
        copy_runtime
    else
        _warn "No USB_MOUNT — session dir is empty. Copy files manually."
    fi

    # 3. Generate cleanup script
    generate_cleanup_script

    # 4. Write PID file
    write_pid_file

    # 5. Register udev rule (unless --no-udev)
    if [[ "$NO_UDEV" -eq 0 ]]; then
        register_udev_rule
    else
        _info "Skipping udev rule registration (--no-udev)."
    fi

    _info "============================================"
    _info "Linux injection complete!"
    _info "  Session:    $SESSION_DIR (tmpfs, ${SESSION_SIZE} RAM)"
    _info "  Runtime:    $SESSION_DIR/runtime/"
    _info "  Cleanup:    $CLEANUP_SCRIPT"
    _info "  udev rule:  ${UDEV_RULE_PATH}"
    _info "  PID file:   $PID_FILE"
    _info "============================================"
    _info ""
    _info "The AI session lives ENTIRELY in RAM."
    _info "Eject the USB to trigger automatic cleanup."
    _info ""
    _info "To start the agent:"
    _info "  cd $SESSION_DIR/runtime && python3 launcher.py"
}

# -----------------------------------------------------------------
# Main
# -----------------------------------------------------------------
main() {
    parse_args "$@"

    _info "carry-ai Linux injection"
    _info "  Session dir:  $SESSION_DIR"
    _info "  tmpfs size:   $SESSION_SIZE"

    # Detect USB mount
    detect_usb_mount || true

    if [[ "$DRY_RUN" -eq 1 ]]; then
        do_dry_run
    else
        do_inject
    fi
}

main "$@"
