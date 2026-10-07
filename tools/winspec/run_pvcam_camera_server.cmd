@echo off
setlocal
echo Close the XP WinSpec and original camera SERVER windows first.
echo Keep the detector/controller powered and cooling unchanged.
echo This starts the optional PVCAM service on the existing port 5000.
echo Do not run WinSpec while this service owns the camera.
echo Press Ctrl+C to stop the service normally before reopening WinSpec.
if not exist "C:\Python27\python.exe" (
  echo ERROR: C:\Python27\python.exe missing.
  pause
  exit /b 1
)
if not exist "%~dp0backend-preflight.json" (
  echo ERROR: Fresh backend-preflight.json required from the host.
  pause
  exit /b 1
)
pushd "%~dp0"
"C:\Python27\python.exe" -m py_compile "%~dp0pvcam_camera_server.py" "%~dp0pvcam_startup_probe.py" "%~dp0startup_reuse_probe.py" "%~dp0temperature_guard.py"
if errorlevel 1 (
  echo ERROR: XP Python compilation failed. No camera initialized.
  popd
  pause
  exit /b 1
)
"C:\Python27\python.exe" -u "%~dp0pvcam_camera_server.py" --preflight "%~dp0backend-preflight.json"
set "PVCAM_SERVICE_EXIT=%ERRORLEVEL%"
popd
echo Service exited with code %PVCAM_SERVICE_EXIT%.
echo Reopen WinSpec and its original start_camera_server.bat.
echo Recheck exposure, accumulation and temperature before any WinSpec run.
pause
exit /b %PVCAM_SERVICE_EXIT%
