@echo off
:: ============================================================
:: carry-ai — Windows Launcher
:: ============================================================
:: Tries the desktop chat app first. If tkinter is missing
:: (common with portable Python), falls back to the web UI
:: which auto-opens in your browser.
:: ============================================================
SETLOCAL EnableDelayedExpansion

SET "USB_ROOT=%~dp0"
SET "USB_ROOT=%USB_ROOT:~0,-1%"
SET "USB_PYTHON=%USB_ROOT%\python-env\windows\python.exe"
SET "DESKTOP=%USB_ROOT%\carry-ai\ui\desktop.py"
SET "BOOTSTRAP=%USB_ROOT%\carry-ai\bootstrap.py"
SET "LAUNCHER=%USB_ROOT%\carry-ai\launcher.py"

:: ---- Resolve Python interpreter ----
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
    ECHO  Run setup_usb.py to bundle Python, or install from https://python.org
    ECHO.
    PAUSE
    GOTO :EOF
)

:: ---- Try desktop app first ----
IF EXIST "%DESKTOP%" (
    ECHO [carry-ai] Trying desktop app...
    "%PY%" "%DESKTOP%" 2>nul
    IF !ERRORLEVEL! EQU 0 GOTO :EOF
    ECHO [carry-ai] Desktop app failed (tkinter not available in portable Python).
    ECHO [carry-ai] Launching web UI instead — your browser will open automatically.
    ECHO.
)

:: ---- Fall back to web UI (auto-opens browser) ----
IF EXIST "%BOOTSTRAP%" (
    "%PY%" "%BOOTSTRAP%" %*
) ELSE IF EXIST "%LAUNCHER%" (
    "%PY%" "%LAUNCHER%" %*
) ELSE (
    ECHO [carry-ai] ERROR: Cannot find launcher.py
    ECHO  Expected at: %LAUNCHER%
    PAUSE
)
ENDLOCAL
