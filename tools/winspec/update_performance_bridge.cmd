@echo off
setlocal
echo Make sure WinSpec is idle, then close only the camera SERVER window.
echo Leave WinSpec and detector cooling running.
echo Measurements using other detectors can continue in SpectralSweep.
echo This installs display-performance-v12. Exposure and averaging settings are unchanged.
pause
if not exist "%~dp0camera_server.py" exit /b 1
if not exist "%~dp0temperature_guard.py" exit /b 1
if not exist "C:\WinSpecRemote\camera_server.py" (
  echo ERROR: C:\WinSpecRemote\camera_server.py not found.
  pause
  exit /b 1
)
if not exist "C:\WinSpecRemote\camera_server.before-display-v12.py" (
  copy /y "C:\WinSpecRemote\camera_server.py" "C:\WinSpecRemote\camera_server.before-display-v12.py" >nul
  if errorlevel 1 exit /b 1
)
if not exist "C:\WinSpecRemote\temperature_guard.before-display-v12.py" (
  copy /y "C:\WinSpecRemote\temperature_guard.py" "C:\WinSpecRemote\temperature_guard.before-display-v12.py" >nul
  if errorlevel 1 exit /b 1
)
copy /y "%~dp0temperature_guard.py" "C:\WinSpecRemote\temperature_guard.py" >nul
if errorlevel 1 exit /b 1
copy /y "%~dp0camera_server.py" "C:\WinSpecRemote\camera_server.py" >nul
if errorlevel 1 exit /b 1
echo Updated. Restart C:\WinSpecRemote\start_camera_server.bat.
echo The server banner must show 2026-10-01-display-performance-v12.
echo This bridge-only update does not require restarting SpectralSweep.
echo Load any pending desktop code changes only after the current measurement ends.
pause
