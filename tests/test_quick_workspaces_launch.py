"""Public all-workspace entry points, real children, restart and isolation.

Import guards run before main.py; no real controller or SDK can be imported.
F5 is exercised through the registered QAction, with normal Qt exit/cleanup.
"""
import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]

BOOTSTRAP = r'''
import importlib.abc,json,os,runpy,sys,traceback
from pathlib import Path
project,results=map(Path,sys.argv[1:3])
flags=sys.argv[3:]
sys.path.insert(0,str(project))
forbidden=('controllers','adapters','drivers','clr','pythonnet','pyvisa','serial',
           'nidaqmx','pylablib','app.ntfy_notifications','app.process_watchdog',
           'app.runtime_exception_reporting','ui.main_window')
attempts=[]
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if any(fullname==name or fullname.startswith(name+'.') for name in forbidden):
   attempts.append(fullname)
   raise AssertionError('Forbidden preview import '+fullname)
sys.meta_path.insert(0,Guard())
import app.ui_preview as preview
if '--preview-workspaces-child' not in flags:
 original_run=preview.subprocess.run
 def child(command,*,env,cwd):
  assert command[1:]==[str(project/'main.py'),'--preview-workspaces-child','--ui','quick'],command
  # Windows TEMP can use an 8.3 alias while preparation resolves the root.
  assert Path(cwd).resolve()==Path(env[preview.PREVIEW_ROOT_ENV]).resolve(),(cwd,env[preview.PREVIEW_ROOT_ENV])
  with (results/'supervisor.jsonl').open('a',encoding='utf8') as stream:
   stream.write(json.dumps({'command':command,'scratch':str(cwd)})+'\n')
  return original_run([sys.executable,'-I',__file__,str(project),str(results),*command[2:]],env=env,cwd=cwd)
 preview.subprocess.run=child
from PySide6.QtCore import QSettings,QTimer
from PySide6.QtWidgets import QApplication,QMessageBox,QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QWidget,QAbstractButton,QAbstractItemView
from PySide6.QtTest import QTest
QMessageBox.critical=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('Unexpected construction failure'))
original_exec=QApplication.exec
state={}
def exercise():
 try:
  app=QApplication.instance()
  window=next(w for w in QApplication.topLevelWidgets() if hasattr(w,'workspace_pages'))
  from utils.config import _CONFIG_FILE,cfg
  from app.experiment_metadata import ExperimentHistory
  from ui.quick_scientific_plot import QuickScientificPlot
  scratch=Path(os.environ[preview.PREVIEW_ROOT_ENV]).resolve()
  assert Path(_CONFIG_FILE).resolve()==scratch/'config.json'
  assert Path(cfg.filename.base_out).resolve()==scratch/'data'
  assert ExperimentHistory().path.resolve()==scratch/'history.sqlite3'
  settings=QSettings()
  assert Path(settings.fileName()).resolve().is_relative_to(scratch/'settings')
  settings.setValue('full-workspace-launch','isolated');settings.sync()
  assert settings.status()==QSettings.Status.NoError
  assert app.property('spectralQuick') is True
  assert window._lf6.__class__.__module__=='ui.spectrum_preview'
  assert len(window.workspace_pages)==9
  for key,title,page in window.workspace_pages:
   window.quick_workbench.selectPage(key);QTest.qWait(20)
   assert window._tabs.currentWidget() is page
   visible_native=[(type(control).__name__,control.objectName(),control.placeholderText() if isinstance(control,QLineEdit) else '')
                   for control in page.findChildren(QWidget)
                   if isinstance(control,(QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QAbstractButton,QAbstractItemView)) and control.isVisibleTo(page)]
   assert not visible_native,(key,visible_native)
  assert isinstance(window._calibration.plot,QuickScientificPlot)
  assert len(window.preview_operation_guard.buttons)>20
  assert all(not button.isEnabled() for button in window.preview_operation_guard.buttons)
  actions=[action for action in window.actions() if action.shortcut().toString()=='F5']
  assert len(actions)==1
  assert not attempts
  assert not any(any(name==prefix or name.startswith(prefix+'.') for prefix in forbidden) for name in sys.modules)
  index=len(list(results.glob('child-*.json')))
  record={'flags':flags,'scratch':str(scratch),'config':str(_CONFIG_FILE),
          'settings':settings.fileName(),'pages':len(window.workspace_pages),
          'blocked_operations':len(window.preview_operation_guard.buttons),
          'forbidden_import_attempts':attempts,'exposure_before':cfg.lf6.exposure_ms,'f5':'F5'}
  cfg.lf6.exposure_ms=321.;cfg.save()
  state.update(window=window,record=record,path=results/('child-'+str(index)+'.json'))
  if index==0:
   actions[0].trigger()
  else:
   window.close()
 except BaseException:
  traceback.print_exc();QApplication.instance().exit(99)
def timed_exec(self):
 QTimer.singleShot(150,exercise);QTimer.singleShot(20000,lambda:self.exit(98))
 return original_exec()
QApplication.exec=timed_exec
sys.argv=[str(project/'main.py'),*flags]
try:
 runpy.run_path(sys.argv[0],run_name='__main__')
except SystemExit as result:
 if '--preview-workspaces-child' in flags:
  assert result.code in (0,75),result.code
  assert state['window']._preview_closed
  state['record'].update(exit_code=result.code,shutdown=True)
  state['path'].write_text(json.dumps(state['record']),encoding='utf8')
 raise
'''


def launch(tmp_path, flags):
    results = tmp_path / "results"
    results.mkdir()
    script = tmp_path / "workspaces-bootstrap.py"
    script.write_text(BOOTSTRAP, encoding="utf-8")
    preview = tmp_path / "preview"
    (preview / "data").mkdir(parents=True)
    (preview / "config.json").write_text(json.dumps({
        "filename": {"base_out": str(preview / "data")},
        "session": {"sample_id": "PREVIEW"},
        "lf6": {"exposure_ms": 250.},
    }), encoding="utf-8")
    production = tmp_path / "production-config.json"
    original = b'{"production":true,"lf6":{"exposure_ms":9999}}'
    production.write_bytes(original)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
               SPECTRALSWEEP_PREVIEW_ROOT=str(preview),
               SPECTRALSWEEP_CONFIG_PATH=str(production),
               SPECTRALSWEEP_HISTORY_PATH=str(tmp_path / "production-history.sqlite"))
    result = subprocess.run([sys.executable, "-I", str(script), str(PROJECT_ROOT), str(results), *flags],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert production.read_bytes() == original
    assert not (tmp_path / "production-history.sqlite").exists()
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(results.glob("child-*.json"))]
    return result, records, results


def test_real_main_workspaces_child_isolated_pages_f5_and_shutdown(tmp_path):
    process, records, _ = launch(tmp_path, ["--preview-workspaces-child"])
    assert process.returncode == 75, process.stdout + process.stderr
    assert len(records) == 1
    record = records[0]
    assert record["pages"] == 9 and record["blocked_operations"] > 20
    assert record["exit_code"] == 75 and record["shutdown"]
    assert record["forbidden_import_attempts"] == []
    assert Path(record["settings"]).is_relative_to(tmp_path / "preview/settings")


def test_real_main_workspaces_supervisor_restarts_same_scratch_then_cleans(tmp_path):
    process, records, results = launch(tmp_path, ["--preview-workspaces"])
    assert process.returncode == 0, process.stdout + process.stderr
    assert len(records) == 2
    assert [record["exit_code"] for record in records] == [75, 0]
    assert [record["exposure_before"] for record in records] == [250., 321.]
    assert all(record["shutdown"] and not record["forbidden_import_attempts"] for record in records)
    calls = [json.loads(line) for line in (results / "supervisor.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert records[0]["scratch"] == records[1]["scratch"]
    assert not Path(records[0]["scratch"]).exists()
