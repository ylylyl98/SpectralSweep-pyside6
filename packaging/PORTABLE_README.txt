SpectralSweep - Windows x64 release

Setup.exe installs for the current Windows user without requiring administrator
rights. It adds a Start menu shortcut and can optionally add a desktop shortcut.
Use Windows Installed apps to uninstall; user settings and acquired data remain.
To upgrade, stop acquisition and close SpectralSweep and its background worker
normally before running the newer Setup.exe. Setup never stops instrument
processes, starts the app automatically, or requests a system restart.

For the portable ZIP, extract the entire ZIP to a writable folder, then run SpectralSweep.exe.
Keep SpectralSweep-worker.exe and _internal alongside the main executable.
Python is included; a separate Python installation is not required.

User configuration remains in %APPDATA%\SpectralSweep. This archive does not
contain the developer's local configuration, credentials or experiment data.
The version.json file identifies the source commit, build number, channel and UTC build time.

Instrument prerequisites must still be installed/configured on the target PC:
LightField and its .NET SDK; NI/VISA/DAQ and camera/USB/FTDI drivers as applicable;
the selected attoDRY SDK; and any separately provisioned WinUSB helper runtime.
Bundled vendor DLLs do not replace device drivers or licensed vendor software.

CI checks the test suite, an isolated nine-workspace UI/worker smoke test,
and temporary installation, upgrade, installed-app smoke and uninstallation.
It does not validate physical acquisition, temperature control or magnet motion.

For diagnostics, run:
  SpectralSweep.exe --smoke-test C:\Temp\SpectralSweep-smoke
Reports and screenshots are written to that output folder. No devices connect.
