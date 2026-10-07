@echo off
setlocal
echo Close the existing WinSpec camera SERVER window first. Leave WinSpec open.
echo This updates only the bridge files. It does not change camera settings.
echo WinSpec Start acceleration remains disabled unless selected in SpectralSweep.
pause
for %%F in (camera_server.py temperature_guard.py acquisition_owner.py stop_owner_guard.py) do (
  if not exist "%~dp0%%F" exit /b 1
)
for %%F in (__init__.py manager.py transport.py native_identity.py winspec_debug_trace.py redirect_step.py experiment_state.py input_state.py communication_state.py experiment_trace.py experiment_analysis.py timing_analysis.py resident_protocol.py resident_trace.py targets.json combined_gate.py combined_trace.py native_probe.py native_runtime.py native_session.py relocation.py batch_probe.dll binary-audit.json) do (
  if not exist "%~dp0start_acceleration\%%F" exit /b 1
)
if not exist "C:\WinSpecRemote\camera_server.py" (
  echo ERROR: C:\WinSpecRemote\camera_server.py not found.
  pause
  exit /b 1
)
if not exist "C:\WinSpecRemote\camera_server.before-temperature.py" (
  copy /y "C:\WinSpecRemote\camera_server.py" "C:\WinSpecRemote\camera_server.before-temperature.py" >nul
  if errorlevel 1 exit /b 1
)
copy /y "%~dp0temperature_guard.py" "C:\WinSpecRemote\temperature_guard.py" >nul
if errorlevel 1 exit /b 1
for %%F in (acquisition_owner.py stop_owner_guard.py) do (
  if exist "C:\WinSpecRemote\%%F" if not exist "C:\WinSpecRemote\%%F.before-start-acceleration" copy "C:\WinSpecRemote\%%F" "C:\WinSpecRemote\%%F.before-start-acceleration" >nul
  if errorlevel 1 exit /b 1
  copy /y "%~dp0%%F" "C:\WinSpecRemote\%%F" >nul
  if errorlevel 1 exit /b 1
)
if exist "C:\WinSpecRemote\start_acceleration" if not exist "C:\WinSpecRemote\start_acceleration.before-update" (
  xcopy "C:\WinSpecRemote\start_acceleration" "C:\WinSpecRemote\start_acceleration.before-update\" /e /i /y >nul
  if errorlevel 1 exit /b 1
)
if not exist "C:\WinSpecRemote\start_acceleration" mkdir "C:\WinSpecRemote\start_acceleration"
for %%F in (__init__.py manager.py transport.py native_identity.py winspec_debug_trace.py redirect_step.py experiment_state.py input_state.py communication_state.py experiment_trace.py experiment_analysis.py timing_analysis.py resident_protocol.py resident_trace.py targets.json combined_gate.py combined_trace.py native_probe.py native_runtime.py native_session.py relocation.py batch_probe.dll binary-audit.json) do (
  copy /y "%~dp0start_acceleration\%%F" "C:\WinSpecRemote\start_acceleration\%%F" >nul
  if errorlevel 1 exit /b 1
)
copy /y "%~dp0camera_server.py" "C:\WinSpecRemote\camera_server.py" >nul
if errorlevel 1 exit /b 1
rem Compile exactly the installed dependencies to replace stale Python 2 bytecode.
"C:\Python27\python.exe" -c "import glob,os,py_compile; root=r'C:\WinSpecRemote'; files=glob.glob(os.path.join(root,'start_acceleration','*.py'))+[os.path.join(root,n) for n in ('camera_server.py','temperature_guard.py','acquisition_owner.py','stop_owner_guard.py')]; [py_compile.compile(p,doraise=True) for p in files]"
if errorlevel 1 exit /b 1
echo Updated. Restart C:\WinSpecRemote\start_camera_server.bat and SpectralSweep.
pause
