SpectralSweep - Windows x64 portable release

Extract the entire ZIP to a writable folder, then run SpectralSweep.exe.
Keep SpectralSweep-worker.exe and _internal alongside the main executable.
Python is included; a separate Python installation is not required.

User configuration remains in %APPDATA%\SpectralSweep. This archive does not
contain the developer's local configuration, credentials or experiment data.
The version.json file identifies the exact source commit.

Instrument prerequisites must still be installed/configured on the target PC:
LightField and its .NET SDK; NI/VISA/DAQ and camera/USB/FTDI drivers as applicable;
the selected attoDRY SDK; and any separately provisioned WinUSB helper runtime.
Bundled vendor DLLs do not replace device drivers or licensed vendor software.

CI checks the test suite and an isolated nine-workspace UI/worker smoke test.
It does not validate physical acquisition, temperature control or magnet motion.

For diagnostics, run:
  SpectralSweep.exe --smoke-test C:\Temp\SpectralSweep-smoke
Reports and screenshots are written to that output folder. No devices connect.
