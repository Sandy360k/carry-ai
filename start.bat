@echo off
:: ============================================================
:: carry-ai — Windows Launcher
:: ============================================================
:: Launches the desktop chat app. Falls back to the web UI.
::
:: Priority for Python:
::   1. USB-local portable Python  (python-env\windows\python.exe)
::   2. System Python              (python / python3)
::
:: Priority for UI:
::   1. Desktop app  (ui\desktop.py)  — native window
::   2. Web UI       (launcher.py)    — browser at localhost:8080
:: ============================================================
SETLOCAL EnableDelayedExpansion

SET "USB_ROOT=%~dp0"
SET "USB_ROOT=%USB_ROOT:~0,-1%"
SET "USB_PYTHON=%USB_ROOT%\python-env\windows\python.exe"
SET "DESKTOP=%USB_ROOT%\carry-ai\ui\desktop.py"
SET "BOOTSTRAP=%USB_ROOT%\carry-ai\bootstrap.py"

:: ---- Resolve Python interpreter ---
SET "PY="
IF EXIST "%USB_PYTHON%" (
    SET "PY=%USB_PYTHON%"
    ECHO [carry-ai] USB Python: %USB_PYTHON%
) ELSE (
    WHERE python >nul 2>&1
    IF !ERRORLEVEL! EQU 0 (
        SET "PY=python"
    ) ELSE (
        WHERE python3 >nul 2>&1
        IF !ERRORLEVEL! EQU 0 SET "PY=python3"
    )
)

IF "%PY%"=="" (
    ECHO.
    ECHO  [carry-ai] ERROR: Python not found.
    ECHO  Run setup_usb.py from another machine to bundle Python on this USB,
    ECHO  or install Python 3.10+ from https://python.org
    ECHO.
    PAUSE
    GOTO :EOF
)

:: ---- Launch desktop app (or fall back to web UI) ---
IF EXIST "%DESKTOP%" (
    ECHO [carry-ai] Starting desktop app...
    "%PY%" "%DESKTOP%" %*
) ELSE (
    ECHO [carry-ai] Desktop app not found, starting web UI...
    "%PY%" "%BOOTSTRAP%" %*
)
ENDLOCAL
