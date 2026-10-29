"""Validate a relocated Quick preview source bundle without installing anything.

This checks resource inclusion and application import isolation against the
current interpreter's dependencies. It is not a standalone executable builder
or an installer for instrument SDKs, drivers, or vendor DLLs.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGES = ("app", "ui", "utils")
MINIMUM_QT = (6, 9)
REQUIRED_RESOURCES = (
    "main.py", "requirements.txt", "tools/verify_quick_ui.py", "app/quick_runtime.py",
    "ui/quick_controls.py", "ui/quick_history.py", "ui/quick_preview.py",
    "ui/qml/CommandBar.qml", "ui/qml/HistoryPage.qml", "ui/qml/Navigation.qml",
    "ui/qml/MotionSequence.qml", "ui/qml/DualGateSequence.qml", "ui/styles/application.qss",
    "app/dual_gate_sequence_model.py", "ui/quick_dual_gate_sequence.py", "ui/dual_gate_sequence_demo.py",
    "tools/verify_quick_workspaces.py", "ui/quick_workspaces_preview.py", "ui/quick_workspace_layouts.py",
    "ui/quick_settings_layout.py", "ui/quick_calibration_layout.py", "ui/quick_tab_layout.py",
    "ui/quick_gate_sections.py", "ui/quick_acquisition_sections.py",
    "ui/quick_dialogs.py", "ui/quick_editing.py", "ui/plot_factory.py",
    "ui/qml/Theme.qml", "ui/qml/AppPane.qml", "ui/qml/AppToolTip.qml", "ui/qml/ParameterForm.qml",
    "ui/qml/TableEditor.qml", "ui/qml/ScientificPlot.qml", "ui/styles/quick_application.qss",
    "assets/icons/spectralsweep.ico",
    "calibrations/ingaas-2026-09-26/LightField-SourceSpectra.xml",
)


def check_runtime(version_string=None):
    version_string = version_string or metadata.version("PySide6")
    parts = re.match(r"^(\d+)\.(\d+)", version_string)
    if parts is None or tuple(map(int, parts.groups())) < MINIMUM_QT:
        raise RuntimeError(f"Qt Quick requires PySide6 {'.'.join(map(str, MINIMUM_QT))} or newer; found {version_string}")
    return version_string


def resource_manifest(source_root):
    """Only tracked-source-shaped paths; no config, outputs, SDKs or environments."""
    source_root = Path(source_root).resolve()
    missing = [name for name in REQUIRED_RESOURCES if not (source_root / name).is_file()]
    if missing:
        raise FileNotFoundError("Quick bundle resources missing: " + ", ".join(missing))
    paths = {source_root / name for name in REQUIRED_RESOURCES}
    for package in SOURCE_PACKAGES:
        paths.update((source_root / package).rglob("*.py"))
    paths.update((source_root / "ui" / "qml").rglob("*.qml"))
    paths.update((source_root / "ui" / "styles").rglob("*.qss"))
    for optional in ("requirements-quick.txt", "ui/qml/qmldir"):
        if (source_root / optional).is_file():
            paths.add(source_root / optional)
    manifest = []
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(source_root):
            raise ValueError(f"Bundle source must stay inside the project: {path}")
        relative = path.relative_to(source_root).as_posix()
        # __pycache__ can contain generated Python sources from local tools.
        if "__pycache__" in path.parts:
            continue
        manifest.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return manifest


def copy_source_bundle(source_root, destination, manifest=None):
    source_root, destination = Path(source_root).resolve(), Path(destination).resolve()
    if destination == source_root or destination.is_relative_to(source_root):
        raise ValueError("Relocation check requires a destination outside the source project")
    manifest = manifest if manifest is not None else resource_manifest(source_root)
    destination.mkdir(parents=True, exist_ok=True)
    for item in manifest:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe manifest path")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError(f"Source changed while assembling the bundle: {relative}")
    return manifest


# Run with -I: no caller cwd, PYTHONPATH or user site-package application paths.
# The original interpreter's third-party site-packages remain intentional.
PROBE_SOURCE = r'''
import importlib.abc,json,os,runpy,sys
from pathlib import Path
bundle,output=map(Path,sys.argv[1:3])
native=sys.argv[3]=='1'
workspaces=len(sys.argv)>4 and sys.argv[4]=='workspaces'
sys.path.insert(0,str(bundle))
denied=('controllers','adapters','drivers','CRYO2100','clr','pythonnet','pyvisa','serial','nidaqmx','pylablib','app.ntfy_notifications','app.process_watchdog')
attempts=[]
class NoHardware(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if any(fullname==prefix or fullname.startswith(prefix+'.') for prefix in denied):
   attempts.append(fullname)
   raise RuntimeError('Hardware/service import forbidden in relocated preview: '+fullname)
guard=NoHardware();sys.meta_path.insert(0,guard)
from app.quick_runtime import require_quick_runtime
require_quick_runtime()
sys.argv=[str(bundle/('tools/verify_quick_workspaces.py' if workspaces else 'tools/verify_quick_ui.py')),'--output',str(output)]+(['--native'] if native else [])
try:
 runpy.run_path(sys.argv[0],run_name='__main__')
except SystemExit as exc:
 if exc.code not in (None,0):raise
modules={}
for name,module in tuple(sys.modules.items()):
 if name.split('.')[0] in ('app','ui','utils') and getattr(module,'__file__',None):
  path=Path(module.__file__).resolve()
  assert path.is_relative_to(bundle),f'Application import escaped bundle: {name}: {path}'
  modules[name]=str(path)
assert 'ui.quick_controls' in modules
assert ('ui.quick_workbench' if workspaces else 'ui.quick_history') in modules
assert not attempts,attempts
from utils import config
scratch=Path(os.environ['SPECTRALSWEEP_PREVIEW_ROOT']).resolve()
config_path=Path(os.environ['SPECTRALSWEEP_CONFIG_PATH']).resolve()
history_path=Path(os.environ['SPECTRALSWEEP_HISTORY_PATH']).resolve()
output_path=Path(config.cfg.filename.base_out).resolve()
assert config_path.is_relative_to(scratch) and history_path.is_relative_to(scratch) and output_path.is_relative_to(scratch)
assert Path(config._CONFIG_FILE).resolve()==config_path
assert os.environ.get('SPECTRAL_MOCK_LF6')=='1'
from PySide6.QtCore import qVersion
report=json.loads((output/'report.json').read_text(encoding='utf8'))
assert not report['qml_warnings'] and not report['hardware_controllers_loaded']
report.update({'qt':qVersion(),'bundle_root':str(bundle),'application_module_paths':modules,
 'application_source_is_relocated':True,'hardware_import_attempts':attempts,
 'isolated_preview_config':str(config_path),'isolated_preview_history':str(history_path),
 'isolated_preview_output':str(output_path),'preview_configuration_isolated':True,
 'interpreter_dependencies_reused':True})
(output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
'''


def validate_bundle(source_root, output, *, native=False, timeout=90):
    source_root, output = Path(source_root).resolve(), Path(output).resolve()
    version = check_runtime()
    manifest = resource_manifest(source_root)
    output.mkdir(parents=True, exist_ok=True)
    # Never accept a successful report left by an earlier run of this tool.
    (output / "report.json").unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="spectralsweep-quick-bundle-") as folder:
        temporary = Path(folder).resolve()
        bundle, external_cwd = temporary / "source", temporary / "external-cwd"
        copy_source_bundle(source_root, bundle, manifest)
        external_cwd.mkdir()
        probe = temporary / "probe.py"
        probe.write_text(PROBE_SOURCE, encoding="utf-8")
        env = os.environ.copy()
        for name in ("PYTHONPATH", "PYTHONHOME", "SPECTRALSWEEP_CONFIG_PATH",
                     "SPECTRALSWEEP_HISTORY_PATH", "SPECTRALSWEEP_PREVIEW_ROOT"):
            env.pop(name, None)
        env["QT_QUICK_BACKEND"] = "software"
        result = subprocess.run([sys.executable, "-I", str(probe), str(bundle), str(output),
                                 "1" if native else "0"], cwd=external_cwd, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        (output / "stdout.txt").write_text(result.stdout, encoding="utf-8")
        (output / "stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"Relocated Quick preview failed ({result.returncode}):\n{result.stderr[-10000:]}")
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        if not report.get("application_source_is_relocated") or not report.get("preview_configuration_isolated"):
            raise RuntimeError("Relocated preview did not supply its isolation contract")
        if Path(report.get("bundle_root", "")).resolve() != bundle:
            raise RuntimeError("Relocated preview reported a different source bundle")
        report.update({"validated_pyside6": version, "resource_manifest": manifest,
                       "resource_count": len(manifest), "validation_kind": "relocated source preview",
                       "standalone_installer_validated": False})
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        workspace_output = output / "workspaces"
        workspace_output.mkdir(exist_ok=True)
        (workspace_output / "report.json").unlink(missing_ok=True)
        result = subprocess.run([sys.executable, "-I", str(probe), str(bundle), str(workspace_output),
                                 "1" if native else "0", "workspaces"], cwd=external_cwd, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        (workspace_output / "stdout.txt").write_text(result.stdout, encoding="utf-8")
        (workspace_output / "stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"Relocated Quick workspaces failed ({result.returncode}):\n{result.stderr[-10000:]}")
        workspace_report = json.loads((workspace_output / "report.json").read_text(encoding="utf-8"))
        if not workspace_report.get("application_source_is_relocated") or not workspace_report.get("preview_configuration_isolated"):
            raise RuntimeError("Relocated workspace preview did not supply its isolation contract")
        report["workspaces"] = workspace_report
        report["validation_kind"] = "relocated source previews: Spectrum and all workspaces"
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "tmp" / "quick-bundle-validation")
    parser.add_argument("--native", action="store_true", help="Use Windows Qt platform without showing a desktop window")
    args = parser.parse_args(argv)
    report = validate_bundle(args.source_root, args.output, native=args.native)
    print(json.dumps({key: report[key] for key in ("validation_kind", "pyside6", "qt", "resource_count",
          "application_source_is_relocated", "preview_configuration_isolated")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
