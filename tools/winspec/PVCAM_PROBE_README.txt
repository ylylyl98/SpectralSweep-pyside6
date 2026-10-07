Independent PVCAM startup feasibility probe (throwaway; production v12 unchanged)

In XP:
1. Make sure WinSpec is idle. Close its camera-server window AND WinSpec itself.
   Keep detector/controller power on. Other-detector SpectralSweep runs can continue.
2. Run run_pvcam_startup_probe.cmd in this shared folder.
3. After ordinary completion, reopen WinSpec and start the existing
   C:\WinSpecRemote\start_camera_server.bat. Tell Codex when this is done.

The script first reads WinSpec COM registration to locate its installed
Pvcam32.dll, then checks the legacy/system paths. If absent there, it can load
the existing local_bundle\LicensedBackup\WinSpec\Pvcam32.dll directly from
XP_TRANSFER. The chosen DLL path and source are logged and included in reports.
The version gate accepts legacy PVCAM 2.7.x and the registered XP DLL's observed
2.8.11 build (0x028b). Other versions still abort before opening the camera.
Before loading PVCAM, the probe sets its own working directory and DLL search
directory to the selected WinSpec DLL folder. This lets legacy dynamic loads
find controller libraries such as contrman.dll. It records dependency file
presence and both directories. These changes affect only the probe process;
the original directories are restored after safe cleanup. No DLL is installed.
It installs no DLL, changes no production bridge and never creates LightField,
SMU or spectrometer objects. It reserves bridge port5000 while testing.
The preflight snapshot must be less than one hour old; refresh it if it expires.

Scope: one setup of two raw uint16 512x1 frames at500ms, then six starts
(one warmup plus five measured). Exact2048-byte completion is required.
Native Start, setup and local capture timings are recorded separately.
This is not WinSpec hardware accumulation or a full MegaSweep benchmark.
Raw frame pairs and counts are saved/fsynced before buffer reuse and all archives
are checked again at the end. Reports are saved locally and in this shared folder
as pvcam-result-<timestamp>-<pid>.json.
The launcher also saves pvcam-console-<random>-<random>.log in the shared folder,
including startup failures that occur before a measurement report is created.

Native temperature must be <=-100 C because native lock status is not assumed.
Temperatures are read only before/after exposure, as required by the PVCAM manual.
No temperature setpoint, gain, speed, cleans or shutter setters are called.
The cached WinSpec exposure is restored by a successful native timed setup.
Available scalar settings are compared before/after.
Parameter availability and explicit read access are checked before type/value
queries. Optional parameters reported as existence-only or write-only are listed
as unsupported for readback; required parameters still prohibit acquisition.
All synchronous parameter preflight errors and reported types are recorded in
one pass before any setup/exposure. An uncertain native wait stops immediately.
One observed compatibility case is accepted: the registered PVCAM 2.8.11 DLL
reports clear_cycles as INT16 despite its standard UNS16 parameter ID. Only
that build/parameter is read using the reported INT16 type; negative values
are rejected. Other type mismatches still prohibit acquisition.
PARAM_EXP_TIME is NOT used
as a timed-mode readback (the manufacturer defines it for variable timed mode).
The WinSpec recipe still requires rechecking after WinSpec restarts.

On an uncertain native call/abort/cleanup, the script prints RECOVERY REQUIRED
and retains its process, DLL, camera handle, pinned buffer and port lease.
Do not start WinSpec or the camera server while that probe process is retained.
Notify Codex to inspect the shared failure report before recovery.
