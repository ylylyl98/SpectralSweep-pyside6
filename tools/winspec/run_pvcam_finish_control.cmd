@echo off
setlocal
echo Completed-readout FINISH diagnostic only. Keep WinSpec and original bridge closed.
echo Keep the detector powered, cooling and optics unchanged.
echo No explicit per-point wait is added. Stop normally with Ctrl+C.
if not exist "C:\Python27\python.exe" goto missing
if not exist "%~dp0backend-preflight.json" goto missing
pushd "%~dp0"
"C:\Python27\python.exe" -m py_compile "%~dp0pvcam_camera_server.py" "%~dp0pvcam_startup_probe.py" "%~dp0startup_reuse_probe.py" "%~dp0temperature_guard.py"
if errorlevel 1 goto compile_failed
"C:\Python27\python.exe" -u "%~dp0pvcam_camera_server.py" --preflight "%~dp0backend-preflight.json" --diagnostic-finish-each
set "PVCAM_DIAGNOSTIC_EXIT=%ERRORLEVEL%"
popd
echo Diagnostic service exited with code %PVCAM_DIAGNOSTIC_EXIT%.
pause
exit /b %PVCAM_DIAGNOSTIC_EXIT%
:compile_failed
popd
echo XP compilation failed. No camera initialized.
pause
exit /b 1
:missing
echo XP Python or fresh restoration preflight missing. No camera initialized.
pause
exit /b 1
