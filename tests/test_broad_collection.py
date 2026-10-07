import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from ui.broad_calibration_dialog import BroadCalibrationDialog


def context(center):
    return dict(profile='bench', grating='300', center_nm=center,
                output_port='SideExit', detector='camera', geometry=[512, 1],
                spectrometer='sp')


def test_collection_all_centers_and_rejects_duplicate_start(monkeypatch):
    app = QApplication.instance() or QApplication([])
    callbacks = []
    monkeypatch.setattr('ui.broad_calibration_dialog.QTimer.singleShot', lambda ms, cb: callbacks.append(cb))
    dialog = BroadCalibrationDialog()
    requested = []
    dialog.capture_center_requested.connect(requested.append)
    dialog.confirm.setChecked(True)
    dialog.start_collection()
    for center in range(900, 1701, 50):
        assert requested[-1] == center
        dialog.frame_collected({'context': context(center)})
        if callbacks:
            dialog.start_collection()
            dialog.capture()
            assert requested[-1] == center
            callbacks.pop(0)()
    assert requested == list(range(900, 1701, 50))
    assert not dialog.sweep_active
    dialog.close()


def test_stop_invalidates_queued_callback_and_wrong_optics_stop(monkeypatch):
    app = QApplication.instance() or QApplication([])
    callbacks = []
    monkeypatch.setattr('ui.broad_calibration_dialog.QTimer.singleShot', lambda ms, cb: callbacks.append(cb))
    dialog = BroadCalibrationDialog()
    requested = []
    dialog.capture_center_requested.connect(requested.append)
    dialog.confirm.setChecked(True)
    dialog.start_collection()
    dialog.frame_collected({'context': context(900)})
    dialog.stop_collection()
    dialog.start_collection()
    callbacks.pop(0)()
    assert requested == [900, 900]
    dialog.frame_collected({'context': context(950)})
    assert not dialog.sweep_active
    dialog.close()


def test_automatic_save_failure_and_cancel_do_not_publish():
    app = QApplication.instance() or QApplication([])
    dialog = BroadCalibrationDialog()
    finished = []
    dialog.automatic_finished.connect(finished.append)
    dialog.automatic = True
    dialog.analysis_running = True
    dialog.save_callback = lambda record: False
    dialog._analysis_done({'generation': dialog.sweep_generation, 'record': {'segments': [], 'rms_nm': 0}})
    assert finished == [False]
    assert 'save' in dialog.result.text().lower()
    dialog.automatic = True
    dialog.analysis_running = True
    generation = dialog.sweep_generation
    dialog.stop_collection()
    dialog._analysis_done({'generation': generation, 'record': {}})
    assert finished == [False, False]
    dialog.close()


def test_one_click_collects_all_positions_then_analyzes(monkeypatch):
    app = QApplication.instance() or QApplication([])
    callbacks=[]; requested=[]; analyzed=[]
    monkeypatch.setattr('ui.broad_calibration_dialog.QTimer.singleShot',lambda ms,cb:callbacks.append(cb))
    monkeypatch.setattr('app.auto_wavelength_calibration.exposure_retry',lambda *args:None)
    dialog=BroadCalibrationDialog();dialog.confirm.setChecked(True)
    dialog.capture_settings_requested.connect(lambda c,e:requested.append((c,e)))
    monkeypatch.setattr(dialog,'_start_analysis',lambda:analyzed.append(len(dialog.auto_frames)))
    dialog.start_automatic_collection(2000)
    for center in range(900,1701,50):
        assert requested[-1]==(center,2000)
        dialog.frame_collected({'context':context(center),'counts':[0]*512})
        if callbacks:callbacks.pop(0)()
    assert len(requested)==17 and analyzed==[17]
    dialog.close()


def test_all_gratings_saved_separately_and_failed_one_retained(monkeypatch):
    app=QApplication.instance() or QApplication([])
    d=BroadCalibrationDialog();d.confirm.setChecked(True)
    switches=[];saved=[];finished=[]
    d.grating_requested.connect(switches.append)
    d.automatic_finished.connect(finished.append)
    d.save_callback=lambda r:saved.append(r) or True
    monkeypatch.setattr(d,'start_automatic_collection',lambda exposure:setattr(d,'automatic',True))
    d.start_all_gratings(['g1','g2'],1000)
    assert switches==['g1']
    d.grating_ready('wrong');assert not d.automatic
    d.grating_ready('g1');d.analysis_running=True
    d._analysis_done({'generation':d.sweep_generation,'record':{'segments':[],'rms_nm':0,'grating':'g1'}})
    assert switches==['g1','g2'] and len(saved)==1 and not finished
    d.grating_ready('g2');d.analysis_running=True
    d._analysis_done({'generation':d.sweep_generation,'error':'No peaks'})
    assert len(saved)==1 and finished==[True]
    assert 'g1' in d.result.text() and 'g2' in d.result.text() and 'No peaks' in d.result.text()
    d.close()


def test_cancel_batch_never_switches_to_next_grating():
    app=QApplication.instance() or QApplication([])
    d=BroadCalibrationDialog();switches=[]
    d.grating_requested.connect(switches.append)
    d.start_all_gratings(['g1','g2'])
    d.stop_collection()
    d.grating_ready('g1')
    d._next_grating()
    assert switches==['g1'] and not d.sweep_active
    d.close()


def test_embedded_tab_switch_preserves_collection_and_progress():
    from PySide6.QtWidgets import QTabWidget,QWidget
    from PySide6.QtCore import Qt
    from ui.wavelength_calibration_dialog import WavelengthCalibrationDialog
    app=QApplication.instance() or QApplication([])
    tabs=QTabWidget();d=WavelengthCalibrationDialog();d.embedded=True
    d.setWindowFlags(Qt.WindowType.Widget)
    tabs.addTab(d,'Calibration');tabs.addTab(QWidget(),'Spectrum');tabs.show();app.processEvents()
    d.open_broad();app.processEvents();b=d.broad_dialog
    b.batch_active=True;b.sweep_active=True;b.automatic=True;b.sweep_expected=1050.
    b.sweep_centers=[1100.];b.result.setText('Collecting 1050 nm')
    tabs.setCurrentIndex(1);app.processEvents()
    assert b.sweep_active and b.sweep_expected==1050.
    tabs.setCurrentIndex(0);app.processEvents()
    assert b.isVisible() and b.result.text()=='Collecting 1050 nm'
    b.stop_collection();tabs.close()


def test_automatic_frame_does_not_show_manual_assignment_prompt():
    import numpy as np
    from ui.wavelength_calibration_dialog import WavelengthCalibrationDialog
    app=QApplication.instance() or QApplication([])
    d=WavelengthCalibrationDialog();d.open_broad();d.broad_dialog.automatic=True
    d.broad_dialog.sweep_active=True
    d.set_frame({'counts':np.ones(512).tolist(),'context':context(900)})
    assert 'no wavelengths assigned' not in d.result.text()
    assert 'Verify identities' not in d.result.text()
    d.broad_dialog.stop_collection();d.close()
