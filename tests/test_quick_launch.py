"""Exercise both public Quick launch branches in real isolated Python processes."""
import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_frontend_is_quick_with_explicit_widgets_fallback(monkeypatch):
    from main import _parse_args
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert _parse_args().ui == "quick"
    monkeypatch.setattr(sys, "argv", ["main.py", "--ui", "widgets"])
    assert _parse_args().ui == "widgets"


BOOTSTRAP = r'''
import importlib.abc,json,os,runpy,sys,time,traceback
from pathlib import Path
project,results=map(Path,sys.argv[1:3])
flags=sys.argv[3:]
sys.path.insert(0,str(project))
forbidden=('controllers','app.ntfy_notifications','app.process_watchdog','app.runtime_exception_reporting','clr','pythonnet','pyvisa','nidaqmx','pylablib')
attempts=[]
class PreviewOnly(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if any(fullname==name or fullname.startswith(name+'.') for name in forbidden):
   attempts.append(fullname)
   raise AssertionError('Quick preview imported a production service: '+fullname)
sys.meta_path.insert(0,PreviewOnly())
import app.ui_preview as preview
from PySide6.QtCore import QSettings,QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtQuickWidgets import QQuickWidget

if '--preview-child' not in flags:
 original_run=preview.subprocess.run
 def child(command,*,env,cwd):
  assert command[1:]==[str(project/'main.py'),'--preview-child','--ui','quick'],command
  # Windows TEMP can use an 8.3 alias while preparation resolves the root.
  assert Path(cwd).resolve()==Path(env[preview.PREVIEW_ROOT_ENV]).resolve(),(cwd,env[preview.PREVIEW_ROOT_ENV])
  with (results/'supervisor-calls.jsonl').open('a',encoding='utf8') as stream:
   stream.write(json.dumps({'command':command,'cwd':str(cwd),'scratch':env[preview.PREVIEW_ROOT_ENV]})+'\n')
  return original_run([sys.executable,'-I',__file__,str(project),str(results),*command[2:]],env=env,cwd=cwd)
 preview.subprocess.run=child

def exercise():
 try:
  app=QApplication.instance()
  window=next(w for w in QApplication.topLevelWidgets() if hasattr(w,'controller'))
  scratch=Path(os.environ[preview.PREVIEW_ROOT_ENV]).resolve()
  from utils.config import cfg,_CONFIG_FILE
  from app.experiment_metadata import ExperimentHistory
  from ui.spectrum_preview import PreviewSpectrumController
  assert type(window.controller) is PreviewSpectrumController
  assert window.controller.identity['simulated']
  assert app.property('spectralQuick') is True
  assert window.quick_commands.status()==QQuickWidget.Status.Ready,window.quick_commands.errors()
  assert window.quick_history.status()==QQuickWidget.Status.Ready,window.quick_history.errors()
  deadline=time.monotonic()+4
  while window.quick_history.view_model.loading and time.monotonic()<deadline:QTest.qWait(10)
  assert window.quick_history.view_model.total==65
  assert not window.quick_history.qml_warnings
  assert Path(_CONFIG_FILE).resolve()==scratch/'config.json'
  assert Path(os.environ['SPECTRALSWEEP_CONFIG_PATH']).resolve()==scratch/'config.json'
  assert cfg.session.sample_id=='PREVIEW'
  assert Path(cfg.filename.base_out).resolve()==scratch/'data'
  assert ExperimentHistory().path.resolve()==scratch/'history.sqlite3'
  assert QSettings.defaultFormat()==QSettings.Format.IniFormat
  settings=QSettings()
  assert settings.format()==QSettings.Format.IniFormat
  settings_file=Path(settings.fileName()).resolve()
  assert settings_file.is_relative_to(scratch/'settings'),settings_file
  settings.setValue('quick-launch-verification','isolated');settings.sync()
  assert settings.status()==QSettings.Status.NoError and settings_file.is_file()
  before=cfg.lf6.exposure_ms
  cfg.lf6.exposure_ms=321.0;cfg.save()
  assert json.loads((scratch/'config.json').read_text())['lf6']['exposure_ms']==321.0
  before_frame=window.controller._frame
  window.controller.acquire_single()
  window.controller._timer.stop();window.controller._complete()
  assert window.controller._frame==before_frame+1
  assert window.panel._last_data.shape==(1024,)
  assert not attempts,attempts
  assert not any(name=='controllers' or name.startswith('controllers.') for name in sys.modules)
  assert 'ui.main_window' not in sys.modules
  assert not any(name in sys.modules for name in forbidden)
  index=len(list(results.glob('child-*.json')))
  record={'flags':flags,'quick':True,'sample':cfg.session.sample_id,'scratch':str(scratch),
          'config':str(_CONFIG_FILE),'settings_ini':str(settings_file),'exposure_before':before,
          'forbidden_import_attempts':attempts,'simulated_frames':window.controller._frame,
          'qml_warnings':window.quick_history.qml_warnings}
  (results/('child-'+str(index)+'.json')).write_text(json.dumps(record),encoding='utf8')
  if os.environ.get('QUICK_LAUNCH_RESTART_ONCE')=='1' and index==0:
   window.reload_preview()
  else:
   app.exit(0)
 except BaseException:
  traceback.print_exc();QApplication.instance().exit(99)

original_exec=QApplication.exec
def instrumented_exec(self):
 QTimer.singleShot(250,exercise)
 QTimer.singleShot(12000,lambda:self.exit(98))
 return original_exec()
QApplication.exec=instrumented_exec
sys.argv=[str(project/'main.py'),*flags]
runpy.run_path(str(project/'main.py'),run_name='__main__')
'''


def run_launch(tmp_path, flags, *, restart=False):
    results = tmp_path / "results"
    results.mkdir()
    bootstrap = tmp_path / "quick-launch-bootstrap.py"
    bootstrap.write_text(BOOTSTRAP, encoding="utf-8")
    production = tmp_path / "production-config.json"
    production_bytes = b'{"production":true,"lf6":{"exposure_ms":9999}}'
    production.write_bytes(production_bytes)
    scratch = tmp_path / "preview"
    (scratch / "data").mkdir(parents=True)
    (scratch / "config.json").write_text(json.dumps({
        "filename": {"base_out": str(scratch / "data")},
        "session": {"sample_id": "PREVIEW"},
        "lf6": {"center_nm": 800, "exposure_ms": 250, "accumulations": 1},
    }), encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_QUICK_BACKEND"] = "software"
    env["SPECTRALSWEEP_PREVIEW_ROOT"] = str(scratch)
    # Deliberately inherit production paths: the preview must override them.
    env["SPECTRALSWEEP_CONFIG_PATH"] = str(production)
    env["SPECTRALSWEEP_HISTORY_PATH"] = str(tmp_path / "production-history.sqlite")
    env["QUICK_LAUNCH_RESTART_ONCE"] = "1" if restart else "0"
    process = subprocess.run([sys.executable, "-I", str(bootstrap), str(PROJECT_ROOT),
                              str(results), *flags], cwd=tmp_path, env=env,
                             capture_output=True, text=True, timeout=45)
    assert process.returncode == 0, process.stdout + process.stderr
    assert production.read_bytes() == production_bytes
    assert not (tmp_path / "production-history.sqlite").exists()
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(results.glob("child-*.json"))]
    return records, results


def test_main_preview_child_quick_enters_real_isolated_simulator(tmp_path):
    records, _ = run_launch(tmp_path, ["--preview-child", "--ui", "quick"])
    assert len(records) == 1
    record = records[0]
    assert record["quick"] and record["sample"] == "PREVIEW"
    assert record["flags"] == ["--preview-child", "--ui", "quick"]
    assert record["forbidden_import_attempts"] == [] and record["qml_warnings"] == []
    assert record["simulated_frames"] == 1
    assert Path(record["settings_ini"]).is_relative_to(tmp_path / "preview/settings")


def test_main_preview_quick_supervisor_preserves_quick_across_real_restart(tmp_path):
    records, results = run_launch(tmp_path, ["--preview-quick"], restart=True)
    assert len(records) == 2
    assert all(record["flags"] == ["--preview-child", "--ui", "quick"] for record in records)
    assert records[0]["scratch"] == records[1]["scratch"]
    assert records[0]["exposure_before"] == 250
    assert records[1]["exposure_before"] == 321
    assert all(not record["forbidden_import_attempts"] and not record["qml_warnings"] for record in records)
    calls = [json.loads(line) for line in (results / "supervisor-calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0]["command"][-3:] == ["--preview-child", "--ui", "quick"]
    # The supervisor owns and removes scratch storage after the final child exits.
    assert not Path(records[0]["scratch"]).exists()
