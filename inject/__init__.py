"""
carry-ai/inject/ — Host System Injection Package
==================================================

Handles setting up a temporary session environment on the host machine
and registering cleanup triggers for when the USB is ejected.

Platform-specific modules:
    inject_windows — Uses %TEMP%\\ai_session\\ + WMI eject watcher
    inject_linux   — Uses tmpfs (RAM disk) + udev rule for USB removal

The injection is designed to be completely reversible — cleanup removes
every trace of carry-ai from the host system.
"""
