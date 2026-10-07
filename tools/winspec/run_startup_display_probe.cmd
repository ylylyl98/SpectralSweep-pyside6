@echo off
setlocal
echo Close only the camera SERVER window first. Leave WinSpec and cooling running.
echo This compares visible and hidden data windows at 500 ms x 2.
echo It acquires 12 frames, usually taking about 30 seconds.
echo Every frame uses a fresh document. SPE archives and counts are retained.
echo Original exposure and display settings are restored at the end.
echo Measurements using other detectors can continue in SpectralSweep.
pause
if not exist "C:\Python27\python.exe" (
  echo ERROR: C:\Python27\python.exe not found.
  pause
  exit /b 1
)
if not exist "C:\WinSpecRemote\camera_server.py" (
  echo ERROR: Installed WinSpec bridge not found.
  pause
  exit /b 1
)
"C:\Python27\python.exe" "%~dp0startup_display_probe.py"
if errorlevel 1 (
  echo Probe failed. Check restore_ok and restore_errors before restarting the bridge.
  echo Recover retained documents and files if required.
) else (
  echo Probe completed and original settings restored.
  echo Restart C:\WinSpecRemote\start_camera_server.bat.
)
echo SpectralSweep and WinSpec do not need to restart.
pause
