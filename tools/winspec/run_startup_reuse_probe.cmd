@echo off
setlocal
echo Close only the camera SERVER window first. Leave WinSpec and cooling running.
echo This test uses 500 ms x 2, with five fresh/reuse pairs plus one warmup frame.
echo Measurements using other detectors can continue in SpectralSweep.
echo The test retains all SPE files and restores the original exposure settings.
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
"C:\Python27\python.exe" "%~dp0startup_reuse_probe.py"
if errorlevel 1 (
  echo Probe failed. Review the error and restore_ok before restarting the bridge.
  echo Failed data and all SPE files are retained in C:\WinSpecRemote\startup-probe-*.
  pause
  exit /b 1
)
echo Probe complete and original settings restored.
echo Restart C:\WinSpecRemote\start_camera_server.bat.
echo SpectralSweep and WinSpec do not need to restart.
pause
