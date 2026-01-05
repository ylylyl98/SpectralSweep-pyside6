@echo off
setlocal

REM ---- Settings ----
set "PORT=8502"
set "ROOT=%~dp0"
set "APP=%ROOT%streamlit_entry.py"

REM ---- Pick Python: 3.13 -> 3.12 -> 3.11 ----
set "PYVER="
py -3.13 --version >nul 2>&1 && set "PYVER=3.13"
if "%PYVER%"=="" py -3.12 --version >nul 2>&1 && set "PYVER=3.12"
if "%PYVER%"=="" py -3.11 --version >nul 2>&1 && set "PYVER=3.11"

if "%PYVER%"=="" (
  echo [ERROR] No Python 3.11/3.12/3.13 found via "py".
  pause
  exit /b 1
)

REM ---- Tag used in folder names ----
set "PYTAG="
if "%PYVER%"=="3.13" set "PYTAG=313"
if "%PYVER%"=="3.12" set "PYTAG=312"
if "%PYVER%"=="3.11" set "PYTAG=311"

set "VENV=%ROOT%.venv-%PYTAG%"
set "LOCK=%ROOT%requirements\requirements-%PYTAG%.lock.txt"
set "REQ=%ROOT%requirements\requirements.txt"

if not exist "%APP%" (
  echo [ERROR] App entry not found:
  echo   %APP%
  pause
  exit /b 1
)

REM ---- Choose requirement file ----
set "REQFILE=%LOCK%"
if not exist "%REQFILE%" set "REQFILE=%REQ%"

if not exist "%REQFILE%" (
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
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install -r "%REQFILE%"
if errorlevel 1 (
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)


REM ---- Run Streamlit (no line continuations) ----
cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%"
"%VENV%\Scripts\python.exe" -m streamlit run "%APP%" --server.address localhost --server.port %PORT% --server.headless false

timeout /t 2 >nul
REM timeout /t 2 >nul
REM start "" "http://localhost:%PORT%"

endlocal
