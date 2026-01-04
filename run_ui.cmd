@echo off
setlocal enabledelayedexpansion

REM ---- Settings ----
set "PORT=8502"
set "ROOT=%~dp0"
set "APP=%ROOT%app\ui_streamlit\main_ui.py"

REM ---- Pick Python (prefer 3.13, then 3.12, then 3.11) ----
set "PYVER="
for %%V in (3.13 3.12 3.11) do (
  py -%%V --version >nul 2>&1 && (set "PYVER=%%V" & goto :found_py)
)
:found_py
if "%PYVER%"=="" (
  echo [ERROR] No Python 3.11/3.12/3.13 found via "py".
  pause
  exit /b 1
)

REM "3.13" -> "313"
set "PYTAG=%PYVER:.=%"
set "VENV=%ROOT%.venv-%PYTAG%"
set "LOCK=%ROOT%requirements\requirements-%PYTAG%.lock.txt"
set "REQ=%ROOT%requirements\requirements.txt"

if not exist "%APP%" (
  echo [ERROR] App entry not found:
  echo   %APP%
  pause
  exit /b 1
)

REM ---- Choose requirement file: lock if exists, else requirements.txt ----
set "REQFILE="
if exist "%LOCK%" (
  set "REQFILE=%LOCK%"
) else if exist "%REQ%" (
  echo [WARN] No lock file for Python %PYVER% found:
  echo   %LOCK%
  echo Using requirements.txt instead (less reproducible).
  set "REQFILE=%REQ%"
) else (
  echo [ERROR] Neither lock file nor requirements.txt found.
  echo   %LOCK%
  echo   %REQ%
  pause
  exit /b 1
)

REM ---- Create venv if missing ----
if not exist "%VENV%\Scripts\python.exe" (
  echo Creating venv: %VENV% (Python %PYVER%)
  py -%PYVER% -m venv "%VENV%"
  if errorlevel 1 (
    echo [ERROR] Failed to create venv.
    pause
    exit /b 1
  )
)

REM ---- Install/update deps ----
echo Sync deps from:
echo   %REQFILE%
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip >nul
"%VENV%\Scripts\python.exe" -m pip install -r "%REQFILE%"
if errorlevel 1 (
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)

REM ---- Run Streamlit + open browser ----
pushd "%ROOT%"

start "LabRunner Streamlit" /B "%VENV%\Scripts\python.exe" -m streamlit run "%APP%" ^
  --server.address localhost ^
  --server.port %PORT% ^
  --server.headless false

timeout /t 2 >nul
start "" "http://localhost:%PORT%"

popd
endlocal
