"""
carry-ai/inject/inject_windows.py — Windows Session Injection
===============================================================

Sets up a temporary session environment on Windows and monitors for
USB eject events to trigger cleanup.

Injection Steps:
    1. Create session directory at %TEMP%\\ai_session\\
    2. Copy minimal runtime files from USB to session directory
    3. Start eject watcher thread (WMI preferred, ctypes polling fallback)
    4. When USB removal is detected:
       a. Signal the agent to stop
       b. Trigger cleanup/cleanup.py
       c. Self-terminate

WMI Event Monitoring (preferred):
    Uses Win32_VolumeChangeEvent with EventType:
        1 = Configuration changed
        2 = Device arrival
        3 = Device removal  ← trigger
        4 = Docking

Fallback (ctypes polling):
    Polls GetLogicalDrives() every 2 seconds via ctypes.windll.kernel32.
    When the USB drive letter disappears from the bitmask, triggers cleanup.
"""

import ctypes
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

# Imported up front: _on_eject runs after the USB is gone, when a lazy
# import of cleanup.cleanup from the drive would fail.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cleanup.cleanup import full_cleanup  # noqa: E402

log = logging.getLogger("carry-ai.inject.windows")

# Where we came from (the carry-ai/ directory on the USB)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Session directory under %TEMP%
SESSION_DIR_NAME = "ai_session"

# Files/dirs to copy from USB into the session (relative to PROJECT_ROOT)
RUNTIME_COPY_LIST = [
    "launcher.py",
    "modes",
    "providers",
    "agent",
    "cleanup",
    "ui",
    "mcp",
    "plugins",
    "cowork",
    # "config" is deliberately NOT copied: it holds providers.enc and
    # memory.db (the full conversation history), which must stay on the USB
    # rather than be duplicated into the host's %TEMP%.
    "crypto",
]


# ===================================================================
# USB drive detection
# ===================================================================

def get_drive_letter_for_path(path: Path) -> str | None:
    """Extract the drive letter (e.g. 'E') from an absolute Windows path."""
    drive = path.drive  # e.g. 'E:'
    if drive and len(drive) >= 2 and drive[1] == ":":
        return drive[0].upper()
    return None


def get_logical_drives_bitmask() -> int:
    """Return the bitmask of logical drives via kernel32.GetLogicalDrives().

    Bit 0 = A:, Bit 1 = B:, Bit 2 = C:, etc.
    """
    return ctypes.windll.kernel32.GetLogicalDrives()


def is_drive_present(letter: str) -> bool:
    """Check if a drive letter is currently present."""
    bit = ord(letter.upper()) - ord("A")
    return bool(get_logical_drives_bitmask() & (1 << bit))


def get_drive_type(letter: str) -> int:
    """Get the drive type via GetDriveTypeW.

    Returns:
        0 = DRIVE_UNKNOWN
        1 = DRIVE_NO_ROOT_DIR
        2 = DRIVE_REMOVABLE  ← USB drives
        3 = DRIVE_FIXED
        4 = DRIVE_REMOTE
        5 = DRIVE_CDROM
        6 = DRIVE_RAMDISK
    """
    return ctypes.windll.kernel32.GetDriveTypeW(f"{letter.upper()}:\\")


DRIVE_REMOVABLE = 2


def detect_usb_drive_letter() -> str | None:
    """Auto-detect the USB drive letter that contains carry-ai.

    First tries to derive it from PROJECT_ROOT's path. If that fails
    or isn't removable, scans all removable drives for a carry-ai/ folder.
    """
    # Try from our own path
    letter = get_drive_letter_for_path(PROJECT_ROOT)
    if letter and is_drive_present(letter):
        dtype = get_drive_type(letter)
        if dtype == DRIVE_REMOVABLE:
            log.info("USB drive detected from script path: %s:", letter)
            return letter
        else:
            log.debug("Drive %s: is type %d (not removable), scanning others.", letter, dtype)

    # Scan all drives for removable ones containing carry-ai/
    bitmask = get_logical_drives_bitmask()
    for i in range(26):
        if not (bitmask & (1 << i)):
            continue
        dl = chr(ord("A") + i)
        if get_drive_type(dl) != DRIVE_REMOVABLE:
            continue
        candidate = Path(f"{dl}:\\carry-ai")
        if candidate.is_dir():
            log.info("Found carry-ai on removable drive %s:", dl)
            return dl

    log.warning("Could not detect USB drive letter. Eject watcher will be disabled.")
    return None


# ===================================================================
# Session directory setup
# ===================================================================

def create_session_dir() -> Path:
    """Create the session directory under %TEMP%.

    Returns:
        Path to the created session directory.
    """
    temp_root = Path(os.environ.get("TEMP", os.environ.get("TMP", "C:\\Temp")))
    session_dir = temp_root / SESSION_DIR_NAME

    if session_dir.exists():
        log.warning("Session directory already exists: %s — cleaning up old session.", session_dir)
        shutil.rmtree(session_dir, ignore_errors=True)

    session_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (session_dir / "runtime").mkdir(exist_ok=True)
    (session_dir / "cache").mkdir(exist_ok=True)
    (session_dir / "logs").mkdir(exist_ok=True)

    log.info("Session directory created: %s", session_dir)
    return session_dir


def copy_runtime(session_dir: Path) -> None:
    """Copy minimal carry-ai runtime from USB to session directory.

    Only copies Python source files — models and large binaries stay on the USB
    and are accessed directly from the drive.
    """
    runtime_dir = session_dir / "runtime"

    for item_name in RUNTIME_COPY_LIST:
        src = PROJECT_ROOT / item_name
        dst = runtime_dir / item_name

        if not src.exists():
            log.debug("Skipping missing item: %s", src)
            continue

        if src.is_file():
            shutil.copy2(src, dst)
            log.debug("Copied file: %s", item_name)
        elif src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            log.debug("Copied directory: %s/", item_name)

    log.info("Runtime copied to session directory (%d items).", len(RUNTIME_COPY_LIST))


# ===================================================================
# Eject watcher — WMI (preferred)
# ===================================================================

def _start_wmi_watcher(drive_letter: str, on_eject: callable) -> threading.Thread:
    """Start a WMI-based eject watcher thread.

    Monitors Win32_VolumeChangeEvent for EventType == 3 (device removal)
    on the specified drive letter.
    """
    import pythoncom
    import wmi

    def _watcher():
        log.info("WMI eject watcher started for drive %s:", drive_letter)
        # WMI requires COM initialization per thread
        pythoncom.CoInitialize()
        try:
            c = wmi.WMI()
            watcher = c.Win32_VolumeChangeEvent.watch_for(
                notification_type="Creation",
                EventType=3,  # Device removal
            )
            while True:
                try:
                    event = watcher(timeout_ms=5000)
                except wmi.x_wmi_timed_out:
                    # Timeout is normal — just means no event yet.
                    # Double-check the drive is still present.
                    if not is_drive_present(drive_letter):
                        log.warning("Drive %s: disappeared (detected via poll).", drive_letter)
                        on_eject(drive_letter)
                        return
                    continue

                # Got a removal event — check if it's our drive
                event_drive = getattr(event, "DriveName", "")
                log.info("WMI event: drive removal — DriveName=%s", event_drive)

                if not is_drive_present(drive_letter):
                    log.info("USB drive %s: removed. Triggering cleanup.", drive_letter)
                    on_eject(drive_letter)
                    return
        finally:
            pythoncom.CoUninitialize()

    t = threading.Thread(target=_watcher, name="wmi-eject-watcher", daemon=True)
    t.start()
    return t


# ===================================================================
# Eject watcher — ctypes polling fallback
# ===================================================================

def _start_poll_watcher(drive_letter: str, on_eject: callable,
                        poll_interval: float = 2.0) -> threading.Thread:
    """Start a polling-based eject watcher as fallback when WMI is unavailable.

    Polls GetLogicalDrives() every `poll_interval` seconds and fires
    on_eject when the drive letter disappears.
    """
    def _poller():
        log.info("Polling eject watcher started for drive %s: (interval=%.1fs)",
                 drive_letter, poll_interval)
        while True:
            time.sleep(poll_interval)
            if not is_drive_present(drive_letter):
                log.info("USB drive %s: no longer present. Triggering cleanup.", drive_letter)
                on_eject(drive_letter)
                return

    t = threading.Thread(target=_poller, name="poll-eject-watcher", daemon=True)
    t.start()
    return t


# ===================================================================
# Eject callback
# ===================================================================

# Global event that external code can wait on to detect eject
eject_event = threading.Event()

# Registered callbacks to invoke on eject (added via register_eject_callback)
_eject_callbacks: list[callable] = []


def register_eject_callback(fn: callable) -> None:
    """Register a function to call when USB eject is detected.

    Callbacks are called in order of registration. Each receives the
    drive letter as its only argument.
    """
    _eject_callbacks.append(fn)


def _on_eject(drive_letter: str) -> None:
    """Internal eject handler. Runs all registered callbacks, then signals event."""
    log.info("=== USB EJECT DETECTED: drive %s: ===", drive_letter)

    for i, cb in enumerate(_eject_callbacks):
        try:
            log.debug("Running eject callback %d: %s", i, cb.__name__)
            cb(drive_letter)
        except Exception as e:
            log.error("Eject callback %d failed: %s", i, e, exc_info=True)

    # Signal the main thread
    eject_event.set()

    # Attempt cleanup
    try:
        session_dir = Path(os.environ.get("TEMP", "")) / SESSION_DIR_NAME
        full_cleanup(session_dir=str(session_dir), dry_run=False)
    except NotImplementedError:
        log.warning("Cleanup module not yet implemented — manual cleanup needed.")
    except Exception as e:
        log.error("Cleanup failed: %s", e, exc_info=True)


# ===================================================================
# Main entry point
# ===================================================================

def inject(dry_run: bool = False) -> tuple[Path, threading.Thread | None]:
    """Set up Windows session environment and start eject watcher.

    Args:
        dry_run: If True, create session dir in project root, skip
                 runtime copy and eject watcher.

    Returns:
        (session_dir, watcher_thread) — watcher_thread is None in dry-run
        or if USB drive cannot be detected.
    """
    if dry_run:
        session_dir = PROJECT_ROOT / "_dry_run_session"
        session_dir.mkdir(exist_ok=True)
        (session_dir / "runtime").mkdir(exist_ok=True)
        (session_dir / "cache").mkdir(exist_ok=True)
        (session_dir / "logs").mkdir(exist_ok=True)
        log.info("[dry-run] Session directory: %s (no watcher)", session_dir)
        return session_dir, None

    # 1. Create session directory
    session_dir = create_session_dir()

    # 2. Copy runtime
    copy_runtime(session_dir)

    # 3. Detect USB drive
    drive_letter = detect_usb_drive_letter()

    if drive_letter is None:
        log.warning("No USB drive detected. Running without eject watcher.")
        return session_dir, None

    # 4. Start eject watcher (WMI preferred, polling fallback)
    watcher = None
    try:
        watcher = _start_wmi_watcher(drive_letter, _on_eject)
        log.info("Using WMI eject watcher.")
    except ImportError:
        log.info("WMI not available (pip install wmi pywin32). Using polling fallback.")
        watcher = _start_poll_watcher(drive_letter, _on_eject)
    except Exception as e:
        log.warning("WMI watcher failed (%s). Using polling fallback.", e)
        watcher = _start_poll_watcher(drive_letter, _on_eject)

    log.info("Windows injection complete. Session: %s, Watching: %s:", session_dir, drive_letter)
    return session_dir, watcher
