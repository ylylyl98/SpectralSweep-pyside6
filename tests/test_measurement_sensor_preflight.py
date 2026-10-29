"""Measurement boundaries stop before metadata or instrument work on cancellation."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QVBoxLayout, QWidget

from ui import bfp_panel_integrated as bfp
from ui import mcd2100_panel as mcd2100
from ui import mcd_panel as mcd
from ui import megasweep_panel as mega
from ui import power_sweep_panel as motion
from ui import presets_panel as dual


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("entry", [
    dual.PresetsPanel._on_run, mega.MegaSweepPanel._on_run,
    motion.PowerSweepPanel._on_run, mcd.MCDPanel._start_run,
    mcd2100.MCD2100Panel.start, bfp.BFPPanel._on_acquire,
])
def test_pending_preflight_rejects_reentrant_start(entry):
    # No other state is needed: a nested modal event loop must not launch a
    # second request or start work against the first request's changing state.
    entry(SimpleNamespace(_sensor_preflight_pending=True))


_STOP_ENTRIES = [dual.PresetsPanel._on_stop, mega.MegaSweepPanel._on_stop,
                 motion.PowerSweepPanel._on_stop, mcd.MCDPanel._stop_run,
                 mcd2100.MCD2100Panel.stop]


@pytest.mark.parametrize("entry", _STOP_ENTRIES)
@pytest.mark.parametrize("native_pending", [True, False])
def test_stop_during_preflight_cancels_only_sensor_request(entry, native_pending):
    abort = Mock()
    ctrl = SimpleNamespace(sensor_preflight_pending=native_pending, abort_acquisition=abort)
    page = SimpleNamespace(_sensor_preflight_pending=True, _lf6=ctrl)
    entry(page)
    assert page._sensor_preflight_cancelled
    assert abort.call_count == int(native_pending)


@pytest.mark.parametrize("entry", _STOP_ENTRIES)
def test_stop_during_responsive_sensor_wait_prevents_measurement_start(app, entry):
    from tests.test_sensor_readout_preflight import Controller, snapshot
    from ui.sensor_readout_preflight import ensure_sensor_readout
    page = QWidget()
    QVBoxLayout(page)
    ctrl = Controller(snapshot("LineSensor"))
    ctrl.hold = True
    ctrl.sensor_preflight_pending = True
    page._lf6 = ctrl
    ctrl.abort_acquisition = Mock(side_effect=lambda: [request["cancel_event"].set() for request in ctrl.requests])
    start_measurement = Mock()
    QTimer.singleShot(5, lambda: entry(page))
    try:
        if ensure_sensor_readout(page, ctrl):
            start_measurement()
        start_measurement.assert_not_called()
        ctrl.abort_acquisition.assert_called_once_with()
        assert [request["action"] for request in ctrl.requests] == ["read"]
        assert ctrl.requests[0]["cancel_event"].is_set()
    finally:
        page.close()
        page.deleteLater()


def test_dual_gate_uses_compact_applied_setup_requirements(monkeypatch):
    class UnscannableSchedule:
        def __iter__(self):
            raise AssertionError("Run must not rescan an expanded schedule for sensor choices")

    schedule = UnscannableSchedule()
    controller = SimpleNamespace()
    ensure = Mock(return_value=False)
    metadata = Mock()
    widget = SimpleNamespace(
        _run_thread=None, _plan_apply_pending=False, _final_seq=[{}],
        _lf6=controller, _acquisition_schedule=schedule,
        _plan_readiness_cache=(schedule, (), (), {"andor_si", "lightfield"}),
        _plan_wavelength_contexts_cache=(schedule, ({"Center Wavelength (nm)": 700.},)),
        _validate_before_run=lambda **_: ({"spectrometer_defaults": {}}, None),
        _current_output_dir=lambda _: Path("unused"), _log=Mock(),
    )
    monkeypatch.setattr(dual, "ensure_sensor_readout", ensure)
    monkeypatch.setattr(dual, "ExperimentMetadataService", metadata)
    monkeypatch.setattr("ui.detector_wavelength_advice.confirm_sequence_wavelength", lambda *_a, **_k: True)
    dual.PresetsPanel._on_run(widget)
    ensure.assert_called_once_with(widget, controller, mode="1d", backends=["andor_si", "lightfield"])
    metadata.assert_not_called()


def test_megasweep_uses_only_enabled_unique_setups_and_stops_before_metadata(monkeypatch):
    controller = SimpleNamespace(backend="andor_si")
    params = {"optical_conditions": [
        {"backend": "lightfield"}, {"backend": "lightfield"},
        {"backend": "", "enabled": True}, {"backend": "winspec_ingaas", "enabled": False},
    ]}
    widget = SimpleNamespace(_lf6=controller, _refresh_preview=lambda: None,
                             _collect_params=lambda: params, _validate=lambda _: True)
    ensure = Mock(return_value=False)
    metadata = Mock()
    monkeypatch.setattr(mega, "ensure_sensor_readout", ensure)
    monkeypatch.setattr(mega, "ExperimentMetadataService", metadata)
    mega.MegaSweepPanel._on_run(widget)
    ensure.assert_called_once_with(widget, controller, mode="1d", backends=["andor_si", "lightfield"])
    metadata.assert_not_called()


def test_megasweep_metadata_freezes_sensor_intent(monkeypatch, tmp_path):
    intent = {"lightfield": {"mode": "center_rows", "rows": 10}}
    controller = SimpleNamespace(backend="lightfield", sensor_readout_intents=intent)
    params = {"optical_conditions": [{}], "out_path": tmp_path}
    widget = SimpleNamespace(_lf6=controller, _smu=None, _refresh_preview=lambda: None,
                             _collect_params=lambda: params, _validate=lambda _: True,
                             _on_error=Mock())
    captured = {}

    class Service:
        def __init__(self, *_):
            pass

        def begin(self, *args, **kwargs):
            captured.update(kwargs["settings"])
            raise RuntimeError("stop after observing metadata")

    monkeypatch.setattr(mega, "ensure_sensor_readout", lambda *_a, **_k: True)
    monkeypatch.setattr(mega, "ExperimentMetadataService", Service)
    mega.MegaSweepPanel._on_run(widget)
    intent["lightfield"]["rows"] = 20
    assert captured["sensor_readout_intents"]["lightfield"]["rows"] == 10


@pytest.mark.parametrize("roi, mode", [("Bin All", "1d"), ("Full Sensor", "2d")])
def test_bfp_cancellation_precedes_metadata_even_without_auto_apply(app, monkeypatch, roi, mode):
    widget = bfp.BFPPanel()
    controller = SimpleNamespace()
    widget._ctrl = controller
    widget._dev_edit.setText("sensor-test")
    # BFP uses the full/binned display labels, not the controller enums.
    widget._roi_combo.setCurrentIndex(0 if roi.startswith("Full") else 1)
    widget._auto_apply_chk.setChecked(False)
    ensure = Mock(return_value=False)
    metadata = Mock()
    monkeypatch.setattr(widget, "_persist_ui_config", lambda: None)
    monkeypatch.setattr(bfp, "ensure_sensor_readout", ensure)
    monkeypatch.setattr(bfp, "ExperimentMetadataService", metadata)
    try:
        widget._on_acquire()
        ensure.assert_called_once_with(widget, controller, mode=mode)
        metadata.assert_not_called()
        assert widget._worker is None
    finally:
        widget.close()


@pytest.mark.parametrize("auto_apply", [False, True])
@pytest.mark.parametrize("roi, mode", [("Bin All", "1d"), ("FullSensor", "2d")])
def test_bfp_revalidates_once_before_warmup_and_repeats_without_rewriting_roi(auto_apply, roi, mode):
    calls = []
    setup = SimpleNamespace(change_roi_LineSensor=Mock(side_effect=AssertionError("would replace Bin All")),
                            change_roi_FullSensor=Mock(side_effect=AssertionError("bypasses shared preflight")))
    spectrum = SimpleNamespace(setup=setup, calibration_wavelengths=lambda: [700., 701., 702.])
    ctrl = SimpleNamespace(is_connected=True, backend="lightfield", adapter=spectrum,
                           configure_for_acquisition=lambda **_: calls.append("configure"),
                           prepare_sensor_for_acquisition=lambda **settings: calls.append(("sensor", settings["mode"])))
    worker = bfp._AcquireWorker(ctrl, roi, 700., 10., 1, 2, auto_apply, True)
    errors = []
    worker.error.connect(errors.append)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bfp, "_do_raw_acquire", lambda *_: calls.append("acquire") or np.ones(3))
        worker.run()
    assert not errors
    assert calls == (["configure"] if auto_apply else []) + [("sensor", mode), "acquire", "acquire", "acquire"]


def test_motion_legacy_configuration_failure_stops_before_moves(tmp_path):
    from tests.test_motion_sweep import _FakeStageController, _worker_params
    stage = _FakeStageController()
    ctrl = SimpleNamespace(adapter=object(), setup=object(),
                           configure_for_acquisition=Mock(side_effect=RuntimeError("sensor mismatch")))
    worker = motion._PowerSweepWorker(_worker_params(str(tmp_path), "stage"),
                                      stage_ctrl=stage, rotation_ctrl=None, pm_ctrl=None,
                                      lf6_ctrl=ctrl, smu_ctrl=None)
    with pytest.raises(RuntimeError, match="sensor mismatch"):
        worker._run_sweep(worker._p)
    assert stage.adapter.moves == []
    assert list(tmp_path.iterdir()) == []


def test_motion_cancellation_precedes_busy_state_and_metadata(app, monkeypatch):
    widget = motion.PowerSweepPanel()
    controller = SimpleNamespace()
    widget._lf6 = controller
    widget._devid_edit.setText("sensor-test")
    widget._conditions_grp.setChecked(False)
    widget._apply_gates_chk.setChecked(False)
    ensure, metadata = Mock(return_value=False), Mock()
    monkeypatch.setattr(widget, "_validate", lambda: True)
    monkeypatch.setattr(widget, "_motion_axes_from_ui", lambda: {})
    monkeypatch.setattr(motion, "ensure_sensor_readout", ensure)
    monkeypatch.setattr(motion, "ExperimentMetadataService", metadata)
    try:
        widget._on_run()
        ensure.assert_called_once_with(widget, controller, mode="1d")
        metadata.assert_not_called()
        assert not widget._sweep_busy
        assert widget._worker is None
    finally:
        widget._parse_timer.stop()
        widget.close()


def test_mcd1000_cancellation_releases_magnet_before_metadata(monkeypatch):
    params = {"sweep_mode": "one_way", "start_t": 0., "stop_t": .01,
              "conditions": [], "rotator": "rot1", "angle_a_deg": 0., "angle_b_deg": 90.}
    magnet = SimpleNamespace(is_connected=True, acquire_exclusive=Mock(return_value=True), release_exclusive=Mock())
    controller = SimpleNamespace(is_connected=True, is_ready=True, is_busy=False)
    widget = SimpleNamespace(_worker=None, _safe_magnet_operation_active=False, _externally_busy=False,
                             _collect_params=lambda: params, _magnet=magnet, _lf6=controller,
                             _last_snapshot=SimpleNamespace(heater_on=True, field_t=0.))
    ensure, metadata = Mock(return_value=False), Mock()
    monkeypatch.setattr(mcd.QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(mcd, "build_mcd_filename_base", lambda _: "test")
    monkeypatch.setattr(mcd, "ensure_sensor_readout", ensure)
    monkeypatch.setattr(mcd, "ExperimentMetadataService", metadata)
    mcd.MCDPanel._start_run(widget)
    ensure.assert_called_once_with(widget, controller, mode="1d")
    magnet.release_exclusive.assert_called_once_with("mcd")
    metadata.assert_not_called()


def test_mcd2100_cancellation_precedes_metadata_and_workflow_handoff(app, monkeypatch, tmp_path):
    from tests.test_mcd2100_panel import FakeController
    controller = SimpleNamespace(is_connected=True)
    factory = Mock()
    widget = mcd2100.MCD2100Panel(
        FakeController(True), lf6_ctrl=controller,
        rotation_ctrl=SimpleNamespace(is_connected=lambda _: True), worker_factory=factory,
    )
    widget.output.setText(str(tmp_path))
    widget.apply_voltages.setChecked(False)
    widget._apply_requested = False
    ensure, metadata = Mock(return_value=False), Mock()
    monkeypatch.setattr(mcd2100, "ensure_sensor_readout", ensure)
    monkeypatch.setattr(mcd2100, "ExperimentMetadataService", metadata)
    save = Mock()
    monkeypatch.setattr(widget, "_save_config_from_ui", save)
    try:
        widget.start()
        ensure.assert_called_once_with(widget, controller, mode="1d")
        metadata.assert_not_called()
        save.assert_not_called()
        factory.assert_not_called()
        assert widget._workflow_intent is None
    finally:
        widget.shutdown(2000)
        widget.close()
