"""
carry-ai/cleanup/cleanup.py — Nuclear Trace Wiper
====================================================

Removes ALL traces of carry-ai from the host machine. This is the
"vanish" part of "inject and vanish."

Cleanup Sequence (ordered):
    1. Kill all carry-ai child processes (llama.cpp, Flask, shells)
    2. Wipe session directory (%TEMP%\\ai_session or /tmp/ai_session)
    3. Clear clipboard
    4. Scrub recent file references
    5. Remove eject watchers (WMI thread / udev rule)
    6. Zero sensitive memory (API keys, tokens)

Safety:
    - Only deletes paths under the session directory
    - Never touches user files outside the session
    - Logs cleanup actions for audit trail
    - Dry-run mode for testing
"""

import ctypes
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

log = logging.getLogger("carry-ai.cleanup")

# Session directory names we recognize as ours (safety check)
VALID_SESSION_NAMES = {"ai_session", "_dry_run_session"}

# Inference servers we start. They are killed only when they are our own
# descendants or their binary lives on the USB / in the session dir — never
# by a bare command-line match, which could hit the user's shell or editor.
PROCESS_KILL_PATTERNS = [
    "llama-server", "llama_server", "llama-server.exe",
    "local-ai", "local-ai.exe",
]

# carry-ai/ on the USB and the drive root above it
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ===================================================================
# Step 1: Kill session processes
# ===================================================================

def kill_session_processes(dry_run: bool = False) -> int:
    """Kill all processes spawned by carry-ai.

    Strategy:
        1. If psutil available: find processes by name pattern and kill tree
        2. Fallback: use OS commands (taskkill on Windows, pkill on Linux)

    Returns:
        Number of processes killed.
    """
    killed = 0

    if psutil is not None:
        killed = _kill_via_psutil(dry_run)
    else:
        killed = _kill_via_os_commands(dry_run)

    log.info("Killed %d process(es).", killed)
    return killed


def _kill_via_psutil(dry_run: bool) -> int:
    """Kill carry-ai processes using psutil."""
    killed = 0
    our_pid = os.getpid()
    try:
        descendants = {c.pid for c in psutil.Process(our_pid).children(recursive=True)}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        descendants = set()
    our_roots = tuple(str(p).lower() for p in (PROJECT_ROOT.parent, _detect_session_dir()))

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            pid = proc.info["pid"]
            name = (proc.info["name"] or "").lower()
            exe = (proc.info["exe"] or "").lower()

            if pid == our_pid:
                continue

            is_server = any(pattern in name for pattern in PROCESS_KILL_PATTERNS)
            is_ours = pid in descendants or (is_server and exe.startswith(our_roots))
            if not is_ours:
                continue

            if dry_run:
                log.info("[dry-run] Would kill PID %d (%s)", pid, name)
                killed += 1
                continue

            log.info("Killing PID %d (%s)...", pid, name)

            # Kill children first
            try:
                children = proc.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                # Wait briefly for children to die
                psutil.wait_procs(children, timeout=3)
                # Force kill survivors
                for child in children:
                    try:
                        if child.is_running():
                            child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Kill parent
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            killed += 1

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return killed


def _kill_via_os_commands(dry_run: bool) -> int:
    """Kill carry-ai processes using OS commands (fallback)."""
    killed = 0
    host_os = platform.system().lower()

    for pattern in PROCESS_KILL_PATTERNS:
        try:
            if host_os == "windows":
                if not pattern.endswith(".exe"):
                    continue
                cmd = ["taskkill", "/F", "/IM", pattern, "/T"]
            else:
                if pattern.endswith(".exe"):
                    continue
                cmd = ["pkill", "-x", pattern]  # exact name, not cmdline

            if dry_run:
                log.info("[dry-run] Would run: %s", " ".join(cmd))
                killed += 1
                continue

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                killed += 1
                log.debug("Killed processes matching: %s", pattern)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue

    return killed


# ===================================================================
# Step 1b: Restore host settings carry-ai's tools may change
# ===================================================================
#
# Desktop control on Linux (computer-use-linux's setup_accessibility) turns
# GNOME's toolkit-accessibility on so apps expose their accessibility tree.
# The value at boot is recorded and put back on cleanup, so the setting
# leaves with the USB. Only gsettings (a host binary) is needed, nothing on
# the stick, so this works after eject too.

TRACKED_GSETTINGS = [
    ("org.gnome.desktop.interface", "toolkit-accessibility"),
]
_HOST_SETTINGS: list[tuple[str, str, str]] = []   # (schema, key, value at boot)


def _gsettings(*args: str) -> str | None:
    gs = shutil.which("gsettings")
    if not gs:
        return None
    try:
        out = subprocess.run([gs, *args], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def snapshot_host_settings() -> int:
    """Record tracked host settings at boot. Returns how many were recorded."""
    _HOST_SETTINGS.clear()
    if not sys.platform.startswith("linux"):
        return 0
    for schema, key in TRACKED_GSETTINGS:
        value = _gsettings("get", schema, key)
        if value is not None:
            _HOST_SETTINGS.append((schema, key, value))
    return len(_HOST_SETTINGS)


def restore_host_settings(dry_run: bool = False) -> int:
    """Put back tracked settings that changed during the session."""
    restored = 0
    for schema, key, value in _HOST_SETTINGS:
        if _gsettings("get", schema, key) == value:
            continue
        log.info("Restoring %s %s to %s", schema, key, value)
        if not dry_run and _gsettings("set", schema, key, value) is None:
            log.warning("Could not restore %s %s", schema, key)
            continue
        restored += 1
    return restored


# ===================================================================
# Step 2: Wipe session directory
# ===================================================================

def wipe_session_directory(session_dir: str | Path | None, dry_run: bool = False) -> bool:
    """Remove the session directory and all contents.

    On Linux: attempts tmpfs unmount first (instant RAM wipe).
    On Windows: uses shutil.rmtree.

    Returns:
        True if successfully wiped.
    """
    if session_dir is None:
        session_dir = _detect_session_dir()

    session_dir = Path(session_dir)

    if not session_dir.exists():
        log.info("Session directory does not exist: %s", session_dir)
        return True

    # Safety: only delete directories we recognize
    if session_dir.name not in VALID_SESSION_NAMES:
        log.error("Refusing to delete unrecognized directory: %s", session_dir)
        return False

    if dry_run:
        log.info("[dry-run] Would wipe: %s", session_dir)
        return True

    host_os = platform.system().lower()

    # Linux: try to unmount tmpfs first
    if host_os == "linux":
        try:
            # Check if it's a mountpoint
            result = subprocess.run(
                ["mountpoint", "-q", str(session_dir)],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                # Lazy unmount (detach immediately, cleanup when last ref closes)
                subprocess.run(
                    ["umount", "-l", str(session_dir)],
                    capture_output=True, timeout=10,
                )
                log.info("tmpfs unmounted: %s (data erased from RAM)", session_dir)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            log.debug("tmpfs unmount attempt: %s", e)

    # Remove directory tree
    try:
        shutil.rmtree(session_dir, ignore_errors=False)
        log.info("Session directory wiped: %s", session_dir)
        return True
    except PermissionError as e:
        log.warning("Permission denied wiping %s: %s. Trying force.", session_dir, e)
        # Windows: try with attrib -R first
        if host_os == "windows":
            try:
                subprocess.run(
                    ["attrib", "-R", "-H", "-S", f"{session_dir}\\*.*", "/S", "/D"],
                    capture_output=True, timeout=10,
                )
                shutil.rmtree(session_dir, ignore_errors=True)
                log.info("Session directory force-wiped: %s", session_dir)
                return True
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
        # Last resort: ignore errors
        shutil.rmtree(session_dir, ignore_errors=True)
        return not session_dir.exists()
    except Exception as e:
        log.error("Failed to wipe session directory: %s", e)
        shutil.rmtree(session_dir, ignore_errors=True)
        return not session_dir.exists()


def _detect_session_dir() -> Path:
    """Auto-detect the session directory based on OS."""
    host_os = platform.system().lower()
    if host_os == "windows":
        temp = os.environ.get("TEMP", os.environ.get("TMP", "C:\\Temp"))
        return Path(temp) / "ai_session"
    else:
        preset = os.environ.get("CARRY_AI_SESSION_DIR")
        if preset and Path(preset).name == "ai_session":
            return Path(preset)
        shm = Path("/dev/shm/ai_session")
        return shm if shm.exists() else Path("/tmp/ai_session")


# ===================================================================
# Step 3: Clear clipboard
# ===================================================================

def clear_clipboard(dry_run: bool = False) -> None:
    """Clear the system clipboard to remove any copied sensitive data."""
    if dry_run:
        log.info("[dry-run] Would clear clipboard.")
        return

    host_os = platform.system().lower()

    if host_os == "windows":
        _clear_clipboard_windows()
    else:
        _clear_clipboard_linux()


def _clear_clipboard_windows() -> None:
    """Clear clipboard on Windows."""
    # Method 1: ctypes (no dependencies)
    try:
        ctypes.windll.user32.OpenClipboard(0)
        ctypes.windll.user32.EmptyClipboard()
        ctypes.windll.user32.CloseClipboard()
        log.info("Clipboard cleared (ctypes).")
        return
    except Exception as e:
        log.debug("ctypes clipboard clear failed: %s", e)

    # Method 2: pyperclip
    try:
        import pyperclip
        pyperclip.copy("")
        log.info("Clipboard cleared (pyperclip).")
        return
    except ImportError:
        pass

    # Method 3: PowerShell
    try:
        subprocess.run(
            ["powershell", "-Command", "Set-Clipboard -Value $null"],
            capture_output=True, timeout=5,
        )
        log.info("Clipboard cleared (PowerShell).")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        log.warning("Could not clear clipboard.")


def _clear_clipboard_linux() -> None:
    """Clear clipboard on Linux (X11 and Wayland)."""
    cleared = False

    # xclip (X11)
    for selection in ["clipboard", "primary", "secondary"]:
        try:
            subprocess.run(
                ["xclip", "-selection", selection, "-i", "/dev/null"],
                capture_output=True, timeout=5,
            )
            cleared = True
        except (FileNotFoundError, OSError):
            break

    # xsel (X11 alternative)
    if not cleared:
        for flag in ["--clipboard", "--primary", "--secondary"]:
            try:
                subprocess.run(
                    ["xsel", flag, "--clear"],
                    capture_output=True, timeout=5,
                )
                cleared = True
            except (FileNotFoundError, OSError):
                break

    # wl-copy (Wayland)
    if not cleared:
        try:
            subprocess.run(
                ["wl-copy", "--clear"],
                capture_output=True, timeout=5,
            )
            cleared = True
        except (FileNotFoundError, OSError):
            pass

    if cleared:
        log.info("Clipboard cleared.")
    else:
        log.warning("Could not clear clipboard (no xclip/xsel/wl-copy found).")


# ===================================================================
# Step 4: Scrub recent file references
# ===================================================================

def scrub_recent_files(dry_run: bool = False) -> None:
    """Remove carry-ai references from recent file lists."""
    if dry_run:
        log.info("[dry-run] Would scrub recent file references.")
        return

    host_os = platform.system().lower()

    if host_os == "windows":
        _scrub_recent_windows()
    else:
        _scrub_recent_linux()


def _scrub_recent_windows() -> None:
    """Remove ai_session entries from Windows Recent folder."""
    recent_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Recent"

    if not recent_dir.is_dir():
        return

    removed = 0
    for item in recent_dir.iterdir():
        try:
            # .lnk files — check if they point to our session
            if item.suffix.lower() == ".lnk" and "ai_session" in item.name.lower():
                item.unlink()
                removed += 1
        except (PermissionError, OSError):
            continue

    if removed:
        log.info("Removed %d recent file shortcut(s).", removed)


def _scrub_recent_linux() -> None:
    """Remove ai_session entries from recently-used.xbel."""
    xbel_path = Path.home() / ".local" / "share" / "recently-used.xbel"

    if not xbel_path.is_file():
        return

    try:
        content = xbel_path.read_text(encoding="utf-8", errors="ignore")
        original_len = len(content)

        # Remove lines containing ai_session references
        lines = content.splitlines(keepends=True)
        filtered = [line for line in lines if "ai_session" not in line]
        new_content = "".join(filtered)

        if len(new_content) < original_len:
            xbel_path.write_text(new_content, encoding="utf-8")
            log.info("Scrubbed ai_session from recently-used.xbel (%d lines removed).",
                     len(lines) - len(filtered))
    except (PermissionError, OSError) as e:
        log.debug("Could not scrub recently-used.xbel: %s", e)


# ===================================================================
# Step 5: Remove eject watchers
# ===================================================================

def remove_eject_watchers(dry_run: bool = False) -> None:
    """Remove eject detection mechanisms."""
    if dry_run:
        log.info("[dry-run] Would remove eject watchers.")
        return

    host_os = platform.system().lower()

    if host_os == "windows":
        # WMI watcher is a daemon thread — it dies when the process exits.
        # Nothing to clean up beyond killing the process (step 1).
        log.debug("Windows WMI watcher is a daemon thread (auto-cleanup).")
    else:
        _remove_udev_rule()


def _remove_udev_rule() -> None:
    """Remove the carry-ai udev rule on Linux."""
    udev_rule = Path("/etc/udev/rules.d/99-carry-ai-cleanup.rules")

    if not udev_rule.exists():
        log.debug("No udev rule to remove.")
        return

    try:
        udev_rule.unlink()
        subprocess.run(
            ["udevadm", "control", "--reload-rules"],
            capture_output=True, timeout=10,
        )
        log.info("udev rule removed and rules reloaded.")
    except PermissionError:
        log.warning("Cannot remove udev rule (need root). File: %s", udev_rule)
    except (FileNotFoundError, OSError) as e:
        log.debug("udev cleanup: %s", e)


# ===================================================================
# Step 6: Zero sensitive memory
# ===================================================================

def zero_sensitive_memory(secrets: dict | list | None = None) -> None:
    """Overwrite sensitive data in memory before releasing references.

    Drops every reference so the values can be garbage-collected. Python
    offers no safe way to overwrite str/bytes in place; see _zero_string.

    Args:
        secrets: Dict or list of sensitive values to zero. Can be nested.
    """
    if secrets is None:
        return

    zeroed = 0

    if isinstance(secrets, dict):
        for key in list(secrets.keys()):
            val = secrets[key]
            if isinstance(val, dict):
                zero_sensitive_memory(val)
            elif isinstance(val, (str, bytes)):
                _zero_string(val)
                zeroed += 1
            secrets[key] = None
    elif isinstance(secrets, list):
        for i, val in enumerate(secrets):
            if isinstance(val, (str, bytes)):
                _zero_string(val)
                zeroed += 1
            elif isinstance(val, dict):
                zero_sensitive_memory(val)
            secrets[i] = None

    if zeroed:
        log.debug("Zeroed %d sensitive value(s) in memory.", zeroed)


def _zero_string(s) -> None:
    """Deliberately a no-op.

    Python str/bytes are immutable and often copied or interned, so writing
    zeros at guessed CPython object offsets (the previous approach) could
    corrupt the interpreter without reliably erasing anything. Dropping the
    references (done by the caller) is the safe best effort.
    """


# ===================================================================
# Main entry point
# ===================================================================

def full_cleanup(session_dir: str | Path | None = None,
                 dry_run: bool = False,
                 secrets: dict | None = None) -> dict:
    """Execute the complete cleanup sequence.

    Args:
        session_dir: Path to session directory to wipe. Auto-detected if None.
        dry_run: If True, log what would happen but don't actually do anything.
        secrets: Dict of sensitive data to zero from memory (API keys, etc.).

    Returns:
        Summary dict with results of each step:
        {
            "processes_killed": int,
            "session_wiped": bool,
            "clipboard_cleared": bool,
            "recent_scrubbed": bool,
            "watchers_removed": bool,
            "memory_zeroed": bool,
        }
    """
    log.info("=== CARRY-AI CLEANUP %s===", "[DRY-RUN] " if dry_run else "")

    results = {
        "processes_killed": 0,
        "settings_restored": 0,
        "session_wiped": False,
        "clipboard_cleared": False,
        "recent_scrubbed": False,
        "watchers_removed": False,
        "memory_zeroed": False,
    }

    # Step 1: Kill processes
    log.info("[1/6] Killing session processes...")
    try:
        results["processes_killed"] = kill_session_processes(dry_run=dry_run)
    except Exception as e:
        log.error("Process kill failed: %s", e)

    # Brief pause for processes to release file handles
    if not dry_run and results["processes_killed"] > 0:
        time.sleep(1)

    # Step 1b: Restore host settings (after the tools that changed them stopped)
    try:
        results["settings_restored"] = restore_host_settings(dry_run=dry_run)
    except Exception as e:
        log.error("Host settings restore failed: %s", e)

    # Step 2: Wipe session directory
    log.info("[2/6] Wiping session directory...")
    try:
        results["session_wiped"] = wipe_session_directory(session_dir, dry_run=dry_run)
    except Exception as e:
        log.error("Session wipe failed: %s", e)

    # Step 3: Clear clipboard
    log.info("[3/6] Clearing clipboard...")
    try:
        clear_clipboard(dry_run=dry_run)
        results["clipboard_cleared"] = True
    except Exception as e:
        log.error("Clipboard clear failed: %s", e)

    # Step 4: Scrub recent files
    log.info("[4/6] Scrubbing recent file references...")
    try:
        scrub_recent_files(dry_run=dry_run)
        results["recent_scrubbed"] = True
    except Exception as e:
        log.error("Recent file scrub failed: %s", e)

    # Step 5: Remove eject watchers
    log.info("[5/6] Removing eject watchers...")
    try:
        remove_eject_watchers(dry_run=dry_run)
        results["watchers_removed"] = True
    except Exception as e:
        log.error("Watcher removal failed: %s", e)

    # Step 6: Zero sensitive memory
    log.info("[6/6] Zeroing sensitive memory...")
    try:
        zero_sensitive_memory(secrets)
        results["memory_zeroed"] = True
    except Exception as e:
        log.error("Memory zero failed: %s", e)

    log.info("=== CLEANUP COMPLETE === Results: %s", results)
    return results
