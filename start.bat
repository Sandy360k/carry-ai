@echo off
:: ============================================================
:: carry-ai — Windows Launcher
:: ============================================================
:: Tries the native desktop app first.  If tkinter is unavailable
:: (common with portable Python), falls back to the Flask web UI
:: which auto-opens in your default browser.
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
    ECHO [carry-ai] Using bundled Python: %USB_PYTHON%
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
    ECHO  Run setup_usb.py to bundle Python onto the USB,
    ECHO  or install Python from https://python.org
    ECHO.
    PAUSE
    GOTO :EOF
)

:: ---- Try desktop app (requires tkinter) ----
IF EXIST "%DESKTOP%" (
    ECHO [carry-ai] Starting desktop app...
    "%PY%" "%DESKTOP%" %*
    SET "_ERR=!ERRORLEVEL!"
    :: Exit code 0 = normal close, -1 = user closed window (also normal)
    IF !_ERR! EQU 0 GOTO :EOF
    IF !_ERR! EQU 255 GOTO :EOF
    :: Any other non-zero = startup failure (missing tkinter, import error, etc.)
    ECHO [carry-ai] Desktop app unavailable (exit code !_ERR!).
    ECHO [carry-ai] Launching web UI instead — your browser will open automatically.
    ECHO.
)

:: ---- Fall back to web UI (launcher auto-opens browser) ----
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
