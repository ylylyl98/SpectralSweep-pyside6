import sys; sys.path.insert(0,'.')
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
from ui.instrument_panel import InstrumentPanel, _VisaScanWorker, _SMUSection
from controllers.lf6_controller import LF6Controller
from controllers.smu_controller import SMUController
from controllers.rotation_controller import RotationController
lf6 = LF6Controller(); smu = SMUController(); rot = RotationController()
panel = InstrumentPanel(lf6_ctrl=lf6, smu_ctrl=smu, rotation_ctrl=rot)
print('Panel OK')
smu_secs = panel.findChildren(_SMUSection)
if smu_secs:
    s = smu_secs[0]
    print('ASRL checkbox:', s._asrl_chk.text())
    print('Refresh btn enabled:', s._refresh_btn.isEnabled())
import importlib, ui.instrument_panel as m; importlib.reload(m); print('Reload OK')
print('ALL PASS')
