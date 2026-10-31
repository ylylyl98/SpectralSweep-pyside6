# Build on Windows with the same Python/Qt versions used by CI.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

root = Path(SPECPATH).parent
datas = [
    (str(root / "ui/qml"), "ui/qml"),
    (str(root / "ui/styles"), "ui/styles"),
    (str(root / "assets/icons"), "assets/icons"),
    (str(root / "calibrations/ingaas-2026-09-26/LightField-SourceSpectra.xml"),
     "calibrations/ingaas-2026-09-26"),
    # Loaded from an explicitly selected SDK directory using a private package name.
    (str(root / "CRYO2100"), "CRYO2100"),
]
datas += collect_data_files("pylablib", includes=["**/*.json", "**/*.cfg"])
# NI packages obtain their version through importlib.metadata at import time.
# Include dependency metadata too: nidaqmx imports nitypes before its own check.
datas += copy_metadata("nidaqmx", recursive=True)
datas += [(str(path), "app/devices") for path in (root / "app/devices").glob("winusb_*_runtime.json")]
# Workspace reload compiles its UI module source directly (ui/page_reload.py).
datas += [(str(path), str(path.parent.relative_to(root)))
          for path in (root / "ui").rglob("*.py") if "__pycache__" not in path.parts]
binaries = [(str(root / "TLPMX_64.dll"), ".")]
binaries += [(str(path), str(path.parent.relative_to(root)))
             for path in (root / "andor dll").rglob("*.dll")]
hidden = ["app.history_reader", "app.process_watchdog", "app.release_smoke",
          "tools.verify_quick_workspaces",
          "pylablib.devices.Andor", "pylablib.devices.Thorlabs", "pyvisa_py",
          "clr", "pythonnet"]
# Page reloads and instrument selection use dynamic imports.
hidden += collect_submodules("ui") + collect_submodules("app")
hidden += collect_submodules("controllers")
a = Analysis(
    [str(root / "main.py"), str(root / "packaging/worker_entry.py")],
    pathex=[str(root)], binaries=binaries, datas=datas, hiddenimports=hidden,
    hookspath=[], runtime_hooks=[], excludes=["tkinter", "PyQt5", "PyQt6", "PySide2"],
    hooksconfig={"matplotlib": {"backends": ["Agg", "QtAgg"]}},
    noarchive=False,
)
pyz = PYZ(a.pure)
# Analysis adds runtime hook scripts before the entry scripts. Both EXEs need them.
gui_scripts = [item for item in a.scripts if item[0] != "worker_entry"]
worker_scripts = [item for item in a.scripts if item[0] != "main"]
gui = EXE(pyz, gui_scripts, [], exclude_binaries=True, name="SpectralSweep",
          console=False, debug=False, strip=False, upx=False,
          icon=str(root / "assets/icons/spectralsweep.ico"))
worker = EXE(pyz, worker_scripts, [], exclude_binaries=True,
             name="SpectralSweep-worker", console=True, debug=False, strip=False, upx=False)
bundle = COLLECT(gui, worker, a.binaries, a.datas, strip=False, upx=False, name="SpectralSweep")
