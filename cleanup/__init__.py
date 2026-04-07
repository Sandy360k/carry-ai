"""
carry-ai/cleanup/ — Trace Wiper Package
=========================================

Contains the nuclear cleanup routine that removes all traces of carry-ai
from the host machine. Triggered automatically on USB eject, or manually.

The cleanup must be thorough and ordered:
    1. Kill processes first (so they don't recreate files)
    2. Wipe files and directories
    3. Clear system artifacts (clipboard, temp, recent files)
    4. Remove cleanup triggers (udev rules, WMI watchers)
    5. Self-delete last
"""
