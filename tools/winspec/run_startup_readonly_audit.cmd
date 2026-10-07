@echo off
setlocal
echo Close only the camera SERVER window first. Leave WinSpec and cooling running.
echo This audit only reads settings. It does not acquire or change any settings.
echo Measurements using other detectors can continue in SpectralSweep.
pause
if not exist "C:\Python27\python.exe" (
  echo ERROR: C:\Python27\python.exe not found.
  pause
  exit /b 1
)
if not exist "C:\WinSpecRemote\camera_server.py" (
  echo ERROR: The installed WinSpec bridge was not found.
  pause
  exit /b 1
)
"C:\Python27\python.exe" "%~dp0startup_readonly_audit.py"
if errorlevel 1 (
  echo Audit failed. No acquisition or setting writes were requested.
) else (
  echo Read-only audit completed. Reports are in this folder and C:\WinSpecRemote.
)
echo Restart C:\WinSpecRemote\start_camera_server.bat.
echo SpectralSweep and WinSpec do not need to restart.
pause
