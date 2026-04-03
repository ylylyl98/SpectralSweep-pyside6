@echo off
setlocal
cd /d %~dp0

REM ── Pick Python (prefer 3.13, fall back to 3.12 / 3.11) ──────────────────
set "PYVER="
py -3.13 --version >nul 2>&1 && set "PYVER=3.13" && set "PYTAG=313"
if "%PYVER%"=="" py -3.12 --version >nul 2>&1 && set "PYVER=3.12" && set "PYTAG=312"
if "%PYVER%"=="" py -3.11 --version >nul 2>&1 && set "PYVER=3.11" && set "PYTAG=311"

if "%PYVER%"=="" (
    echo [ERROR] Python 3.11 / 3.12 / 3.13 not found.
    echo Install from https://python.org and ensure "py" launcher is available.
    pause
    exit /b 1
)
echo Python %PYVER% selected.

REM ── Virtual environment ───────────────────────────────────────────────────
set "VENV=%~dp0.venv-pyside6-%PYTAG%"
set "PY=%VENV%\Scripts\python.exe"
set "REQ=%~dp0requirements\requirements-pyside6.txt"

if not exist "%PY%" (
    echo Creating virtual environment: %VENV%
    py -%PYVER% -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM ── Install / sync dependencies ───────────────────────────────────────────
echo Installing dependencies from:
echo   %REQ%
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r "%REQ%"
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

REM ── Launch ────────────────────────────────────────────────────────────────
echo.
echo Launching SpectralSweep...
set "PYTHONPATH=%~dp0"
"%PY%" main.py %*

endlocal
