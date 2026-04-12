@echo off
setlocal
cd /d %~dp0

set "REQ=%~dp0requirements.txt"
set "PY="
set "BOOTSTRAP="
set "PYVER="
set "PYTAG="
set "VENV="

REM Prefer an existing project venv so launch works even without the "py" launcher.
for %%T in (313 312 311) do (
    if exist "%~dp0.venv-pyside6-%%T\Scripts\python.exe" (
        set "PY=%~dp0.venv-pyside6-%%T\Scripts\python.exe"
        set "VENV=%~dp0.venv-pyside6-%%T"
        set "PYTAG=%%T"
        if "%%T"=="313" set "PYVER=3.13"
        if "%%T"=="312" set "PYVER=3.12"
        if "%%T"=="311" set "PYVER=3.11"
        goto :managed_python
    )
)

REM Fall back to the Windows py launcher when available.
py -3.13 --version >nul 2>&1 && set "PYVER=3.13" && set "PYTAG=313"
if "%PYVER%"=="" py -3.12 --version >nul 2>&1 && set "PYVER=3.12" && set "PYTAG=312"
if "%PYVER%"=="" py -3.11 --version >nul 2>&1 && set "PYVER=3.11" && set "PYTAG=311"
if not "%PYVER%"=="" (
    set "VENV=%~dp0.venv-pyside6-%PYTAG%"
    set "PY=%VENV%\Scripts\python.exe"
    set "BOOTSTRAP=py -%PYVER%"
    goto :managed_python
)

REM Final fallback: plain python on PATH.
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Could not find Python 3.11 / 3.12 / 3.13.
    echo Install Python or create a local .venv-pyside6-311/312/313 environment.
    pause
    exit /b 1
)

echo Python found on PATH. Using it directly without creating a managed venv.
echo Installing dependencies from:
echo   %REQ%
python -m pip install --disable-pip-version-check --no-input -r "%REQ%"
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)
echo.
echo Launching SpectralSweep...
set "PYTHONPATH=%~dp0"
python main.py %*
goto :eof

:managed_python
echo Python %PYVER% selected.

if not exist "%PY%" (
    echo Creating virtual environment: %VENV%
    %BOOTSTRAP% -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo Installing dependencies from:
echo   %REQ%
"%PY%" -m pip install --disable-pip-version-check --no-input -r "%REQ%"
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo.
echo Launching SpectralSweep...
set "PYTHONPATH=%~dp0"
"%PY%" main.py %*

endlocal
