@echo off
setlocal
echo Close the XP camera SERVER and WinSpec windows first.
echo Keep the detector/controller powered. Do not change cooling settings.
echo This uses the EXISTING PVCAM driver; it does not install or replace anything.
echo Six sequences are measured, each containing two raw 500 ms frames.
echo Start is measured separately from one-time configuration.
echo Other-detector measurements in SpectralSweep can continue.
pause
if not exist "C:\Python27\python.exe" exit /b 1
if not exist "%~dp0preflight.json" (
  echo ERROR: preflight.json missing. Do not run without the original settings.
  pause
  exit /b 1
)
:choose_log
set "PVCAM_PROBE_LOG=%~dp0pvcam-console-%RANDOM%-%RANDOM%.log"
if exist "%PVCAM_PROBE_LOG%" goto choose_log
echo Diagnostic log: %PVCAM_PROBE_LOG%
"C:\Python27\python.exe" -u "%~dp0pvcam_startup_probe.py" >"%PVCAM_PROBE_LOG%" 2>&1
set "PVCAM_PROBE_EXIT=%ERRORLEVEL%"
type "%PVCAM_PROBE_LOG%"
if not "%PVCAM_PROBE_EXIT%"=="0" (
  echo Probe failed. Check the shared report for error and native_restore_ok.
  echo If recovery_required is true, preserve the probe process and seek recovery.
) else (
  echo Probe completed. Native exposure and available configuration were restored.
)
echo Reopen WinSpec and restart C:\WinSpecRemote\start_camera_server.bat.
echo The original WinSpec recipe and cooling must be rechecked before resuming WinSpec measurement.
pause
