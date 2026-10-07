@echo off
:: ============================================================
:: carry-ai — Windows Launcher
:: ============================================================
:: Boots carry-ai through launcher.py (session setup, eject watcher,
:: wipe on exit). The native desktop app is used when tkinter is
:: available; otherwise the web UI opens in an isolated browser window.
:: ============================================================
SETLOCAL EnableDelayedExpansion

SET "USB_ROOT=%~dp0"
SET "USB_ROOT=%USB_ROOT:~0,-1%"
SET "USB_PYTHON=%USB_ROOT%\python-env\windows\python.exe"
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

:: ---- Launch (desktop app, falling back to the web UI) ----
IF EXIST "%BOOTSTRAP%" (
    "%PY%" "%BOOTSTRAP%" --ui desktop %*
) ELSE IF EXIST "%LAUNCHER%" (
    "%PY%" "%LAUNCHER%" --ui desktop %*
) ELSE (
    ECHO [carry-ai] ERROR: Cannot find launcher.py
    ECHO  Expected at: %LAUNCHER%
    PAUSE
)
ENDLOCAL
