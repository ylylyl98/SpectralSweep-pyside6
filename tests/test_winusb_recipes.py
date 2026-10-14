"""Variable recipes must preserve every subframe or publish nothing."""
import hashlib
import threading
import uuid

import numpy as np
import pytest

from app.devices import winusb_ingaas as target
from tests.test_winusb_ingaas import Optics


class RecipeBridge:
    is_busy = False
    is_ready = True
    recipe_capabilities = {'exposure_min_ms': 10, 'exposure_max_ms': 1000,
                           'max_accumulations': 100, 'integer_exposure': True}

    def __init__(self):
        self.sequence = 0
        self.session = str(uuid.uuid4())
        self.damage = None

    def preflight(self): return {'runtime_verified': True}
    def close(self): self.is_ready = False

    def capture(self, cancelled, *, request_id, context, exposure_ms):
        self.sequence += 1
        # Large values catch uint16 sum overflow. Distinct values catch repeats.
        value = 60000 if self.sequence % 2 else 40000
        payload = np.full(512, value, dtype='<u2').tobytes()
        native = {'acquisition_backend':'winusb_local', 'connection_mode':'persistent',
            'bridge_session_id':self.session, 'request_sequence':self.sequence,
            'request_id':request_id, 'request_context':context, 'exposure_ms':exposure_ms,
            'payload_sha256':hashlib.sha256(payload).hexdigest(), 'frame_cleanup_verified':True}
        if self.damage == 'exposure': native['exposure_ms'] = 100
        if self.damage == 'sequence': native['request_sequence'] = 1
        if self.damage == 'session' and self.sequence == 2: native['bridge_session_id'] = str(uuid.uuid4())
        if self.damage == 'context': native['request_context'] = {}
        if self.damage == 'cancel' and self.sequence == 2: cancelled.set()
        return native, payload


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(target,'ensure_output_route',lambda *a,**k:{'output_port':'SideExit'})
    monkeypatch.setattr(target,'read_optics',lambda *a:{'output_port':'SideExit'})
    return target.LocalWinUSBSetup(Optics(),bridge=RecipeBridge())


def test_accumulation_preserves_raw_members_and_averages_without_uint16_overflow(setup):
    setup.configure_for_acquisition(center_nm=1300,exposure_ms=300,frames=2)
    x,y=setup.acquire()
    np.testing.assert_array_equal(y,np.full(512,50000))
    snap=setup.read_metadata_snapshot()['observed']['last_frame']
    assert snap['raw_accumulated_counts']==[100000]*512
    assert snap['intensity_processing']['divisor']==2
    assert snap['intensity_processing']['exposure_ms']==300
    batch=snap['native_capture']
    assert batch['accumulations']==2 and batch['exposure_ms']==300
    assert [f['request_sequence'] for f in batch['frames']]==[1,2]
    assert batch['raw_frames']==[[60000]*512,[40000]*512]
    assert len({f['request_id'] for f in batch['frames']})==2
    with pytest.raises(RuntimeError,match='Apply'): setup.acquire()


@pytest.mark.parametrize('damage',['exposure','sequence','session','context','cancel'])
def test_invalid_or_cancelled_accumulation_never_publishes_a_partial_mean(setup,damage):
    setup.bridge.damage=damage
    setup.configure_for_acquisition(center_nm=1300,exposure_ms=300,frames=5)
    with pytest.raises(RuntimeError): setup.acquire()
    assert setup.read_metadata_snapshot()['observed']['last_frame']=={}
    assert setup.bridge.sequence<=2
    assert not setup.is_busy


@pytest.mark.parametrize('exposure,frames',[(9,1),(1001,1),(10.5,1),(float('nan'),1),
    (True,1),(100,True),(100,0),(100,101),(100,1.5)])
def test_bad_recipe_is_rejected_before_optics_or_capture(setup,exposure,frames):
    with pytest.raises(ValueError):
        setup.configure_for_acquisition(center_nm=1300,exposure_ms=exposure,frames=frames)
    assert setup.lightfield.centers==[] and setup.bridge.sequence==0


def test_new_recipe_capability_keeps_scans_disabled(setup):
    assert setup.identity['exposure_min_ms']==10
    assert setup.identity['exposure_max_ms']==1000
    assert setup.identity['max_accumulations']==100
    assert setup.identity['supports_scans'] is False


def test_manual_ui_allows_bounded_recipes_and_preserves_batch_in_real_export(setup,tmp_path,monkeypatch):
    import json
    from PySide6.QtWidgets import QApplication
    from controllers.lf6_controller import _LF6Worker
    from tests.test_spectrum_panel_controls import _FakeSpectrumController
    from ui.spectrum_panel import SpectrumPanel
    monkeypatch.setenv('SPECTRALSWEEP_HISTORY_PATH',str(tmp_path/'history.sqlite'))
    app=QApplication.instance() or QApplication([])
    controller=_FakeSpectrumController();controller.identity=setup.identity
    panel=SpectrumPanel(controller)
    worker=_LF6Worker();worker.connect_instrument(True,'lightfield')
    worker._setup=setup;worker._adapter=setup;worker._backend='winspec_ingaas';worker._identity=setup.identity
    worker.acquisition_settings_readback.connect(panel._on_acquisition_readback)
    worker.spectrum_ready.connect(panel._on_spectrum_ready)
    errors=[];worker.error.connect(errors.append)
    try:
        controller.connected.emit([])
        assert panel._exposure.isEnabled() and panel._accumulations.isEnabled()
        assert panel._exposure.minimum()==10 and panel._exposure.maximum()==1000
        assert panel._accumulations.maximum()==100
        assert not panel._run_1d_btn.isEnabled()
        worker.apply_settings(300,1300,2);worker.acquire_single()
        assert not errors
        snap=panel._last_acquisition_snapshot
        assert snap['native_capture']['raw_frames']==[[60000]*512,[40000]*512]
        assert snap['raw_accumulated_counts']==[100000]*512
        panel.save_current(tmp_path/'batch.csv')
        sidecar=json.loads((tmp_path/'batch.csv.experiment.metadata.json').read_text())
        saved=sidecar['settings']['requested']['acquisition_time_snapshot']
        assert saved['native_capture']['raw_frames']==[[60000]*512,[40000]*512]
        assert saved['raw_accumulated_counts']==[100000]*512
    finally:panel.close()
