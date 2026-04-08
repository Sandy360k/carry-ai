@echo off
:: ============================================================
:: carry-ai — Windows Launcher
:: ============================================================
:: Priority order:
::   1. USB-local portable Python  (python-env\windows\python.exe)
::   2. System Python              (python / python3)
::
:: To build the USB-local env, run once:
::   python carry-ai\setup_usb.py
:: ============================================================
SETLOCAL EnableDelayedExpansion

SET "USB_ROOT=%~dp0"
SET "USB_ROOT=%USB_ROOT:~0,-1%"
SET "USB_PYTHON=%USB_ROOT%\python-env\windows\python.exe"
SET "BOOTSTRAP=%USB_ROOT%\carry-ai\bootstrap.py"

:: ---- Use USB-local Python if present -------------------------
IF EXIST "%USB_PYTHON%" (
    ECHO [carry-ai] USB Python: %USB_PYTHON%
    "%USB_PYTHON%" "%BOOTSTRAP%" %*
    GOTO :EOF
)

:: ---- Fall back to system Python ------------------------------
WHERE python >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    ECHO [carry-ai] System Python detected.
    ECHO            For a fully self-contained USB run: python carry-ai\setup_usb.py
    python "%BOOTSTRAP%" %*
    GOTO :EOF
)

WHERE python3 >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    python3 "%BOOTSTRAP%" %*
    GOTO :EOF
)

:: ---- No Python found -----------------------------------------
ECHO.
ECHO  [carry-ai] ERROR: Python not found.
ECHO.
ECHO  Options:
ECHO    1. Run setup_usb.py from another machine to bundle Python on this USB:
ECHO         python carry-ai\setup_usb.py
ECHO    2. Install Python 3.10+ from https://python.org and re-run.
ECHO.
PAUSE
ENDLOCAL
