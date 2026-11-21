from __future__ import annotations

import os
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app.experiment_metadata import ExperimentMetadataService
from utils.config import cfg

from ui.megasweep_panel import (
    CoordSystem,
    MegaSweepPanel,
    OpticalCondition,
    _MegaSweepWorker,
    _AxisSelectorWidget,
    build_sweep_points,
    _read_point_electrical,
)


class _FakeIV:
    def __init__(self):
        self.gates = (0.0, 0.0)
        self.bias = 0.0
        self.zero_ramps = 0

    def set_gates(self, Vtg, Vbg, **_kwargs):
        self.gates = (float(Vtg), float(Vbg))

    def set_bias(self, Vbias, **_kwargs):
        self.bias = float(Vbias)

    def read_gates(self):
        return self.gates[1], self.gates[0]

    def read_current_bias(self):
        return self.bias

    def read_currents(self):
        return 1e-9, 2e-9, 3e-9

    def ramp_all_to_zero(self, **_kwargs):
        self.gates = (0.0, 0.0)
        self.bias = 0.0
        self.zero_ramps += 1


class _FakeSMUController:
    is_connected = True

    def __init__(self):
        self.device = _FakeIV()


class _FakeSpectrometer:
    def __init__(self):
        self.centers = []
        self.exposures = []
        self.frames = []
        self.acquire_count = 0

    def change_spectra_center(self, value):
        self.centers.append(float(value))

    def change_expose_time(self, value):
        self.exposures.append(float(value))

    def set_accumulations(self, value):
        self.frames.append(int(value))

    def get_wavelength_calibration(self):
        center = self.centers[-1]
        return np.array([center - 1.0, center, center + 1.0])

    def acquire(self):
        self.acquire_count += 1
        return self.get_wavelength_calibration(), np.array([10.0, 20.0, 30.0])


class _FakeLF6Controller:
    is_connected = True

    def __init__(self):
        self.adapter = _FakeSpectrometer()
        self.setup = self.adapter

    def set_center_wavelength_when_ready(self, value):
        self.adapter.change_spectra_center(value)

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        self.adapter.change_spectra_center(center_nm)
        self.adapter.change_expose_time(exposure_ms)
        self.adapter.set_accumulations(frames)


def _point(a: float, b: float) -> dict:
    return {
        "axis_a": a,
        "axis_b": b,
        "axis_values": {"Vtg": a, "Vbg": b, "Doping": a + b, "E-field": a - b},
        "raw": (a, b, 0.0),
    }


def _params(out_path: Path) -> dict:
    points = [_point(0.0, 0.0), _point(0.1, 0.2)]
    return {
        "coord": CoordSystem.RAW,
        "axis_a": "Vtg",
        "axis_b": "Vbg",
        "axis_a_desc": {"start": 0.0, "stop": 0.1, "step": 0.1, "mode": "Step Size", "points": 2},
        "axis_b_desc": {"start": 0.0, "stop": 0.2, "step": 0.2, "mode": "Step Size", "points": 2},
        "axis_a_vals": np.array([0.0, 0.1]),
        "axis_b_vals": np.array([0.0, 0.2]),
        "all_points": points,
        "valid_points": points,
        "fixed": {"Vbias": 0.0},
        "safety": {
            "vtg_min": -1.0,
            "vtg_max": 1.0,
            "vbg_min": -1.0,
            "vbg_max": 1.0,
            "vbias_min": -1.0,
            "vbias_max": 1.0,
        },
        "ratio": 1.0,
        "snake": False,
        "settle": 0.0,
        "extra_overhead_s": 0.0,
        "ramp_step": 0.1,
        "step_delay_s": 0.0,
        "sample": "device",
        "tag": "sequence",
        "laser_nm": "",
        "power_uw": "",
        "vbias_available": True,
        "smu_connected": True,
        "lf6_connected": True,
        "out_path": out_path,
        "center_nm": 720.0,
        "exp_ms": 30.0,
        "frames": 2,
        "base_name": "unused",
        "optical_conditions": [
            {"enabled": True, "name": "red", "center_nm": 720.0, "exposure_ms": 30.0, "frames": 2},
            {"enabled": True, "name": "blue", "center_nm": 750.0, "exposure_ms": 40.0, "frames": 3},
        ],
    }


@pytest.mark.parametrize("partial", [False, True])
def test_station_pair_map_writes_both_sides_of_each_held_point(tmp_path, partial):
    app = QApplication.instance() or QApplication([])
    smu = _FakeSMUController()
    calls = []
    set_gates = smu.device.set_gates

    def tracked_gates(Vtg, Vbg, **kwargs):
        calls.append(("gates", Vtg, Vbg))
        set_gates(Vtg, Vbg, **kwargs)

    smu.device.set_gates = tracked_gates
    settings = {
        "lightfield": {"center_nm": 770, "exposure_ms": 45, "frames": 2},
        "andor_ingaas": {"center_nm": 1200, "exposure_ms": 120, "frames": 3},
    }

    class PairController(_FakeLF6Controller):
        def validate_station_pair(self, backend, recipes):
            assert backend == "andor_ingaas" and recipes == settings
            calls.append(("validate",))

        def acquire_station_pair(self, backend, recipes, stop_event, *, owner):
            assert owner == "megasweep" and backend == "andor_ingaas" and recipes == settings
            calls.append(("pair", *smu.device.gates))
            return {name: ({"error": "Andor stopped unexpectedly"} if partial and name == backend else
                           {"wavelengths": np.arange(3) + recipe["center_nm"],
                            "counts": np.linspace(10, 30, 3), "settings": recipe})
                    for name, recipe in recipes.items()}

    params = _params(tmp_path)
    params.update(pair_andor_backend="andor_ingaas", pair_recipes=settings)
    controller = PairController()
    run = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite").begin(
        "gate_map_2d", "device", output_dir=tmp_path, settings=params)
    worker = _MegaSweepWorker(params, smu, controller, run)
    failures = []
    worker.error.connect(failures.append)
    worker.run()
    assert controller.adapter.acquire_count == 0
    assert calls[:3] == [("validate",), ("gates", 0., 0.), ("pair", 0., 0.)], failures
    if partial:
        assert len(calls) == 3
        assert "Andor stopped unexpectedly" in failures[0]
        assert smu.device.zero_ramps == 0
    else:
        assert not failures
        assert calls[3:] == [("gates", 0.1, 0.2), ("pair", 0.1, 0.2)]
        assert smu.device.zero_ramps == 1
    manifest, = tmp_path.glob("*_pairs.jsonl")
    entries = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    assert [entry["status"] for entry in entries if entry["status"] != "writing"] == (
        ["incomplete"] if partial else ["complete", "complete"])
    spectra = list(tmp_path.glob("*.csv"))
    assert len(spectra) == (1 if partial else 2)
    for path in spectra:
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        assert [row["point_id"] for row in rows] == (["1"] if partial else ["1", "2"])
    panel = MegaSweepPanel()
    try:
        panel._worker = worker
        panel._run_failed = bool(failures)
        panel._experiment_run = run
        panel._on_finished()
        metadata = ExperimentMetadataService.load_metadata(run.path)
        assert metadata["status"] == ("failed" if partial else "completed")
        assert {manifest.name, *(path.name for path in spectra)} <= {
            Path(record["path"]).name for record in metadata["files"]}
    finally:
        panel.close()


def test_station_pair_control_defaults_to_single_and_ingaas_when_selected(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg.lf6, "station_id", "attodry2100")
    widget = MegaSweepPanel()
    try:
        widget._refresh_preview()
        assert "pair_andor_backend" not in widget._collect_params()
        widget._pair_controls.mode.setCurrentIndex(1)
        params = widget._collect_params()
        assert params["pair_andor_backend"] == "andor_ingaas"
        assert set(params["pair_recipes"]) == {"lightfield", "andor_ingaas"}
        state = widget.capture_session_state()
        widget._pair_controls.set_station("default")
        monkeypatch.setattr(cfg.lf6, "station_id", "default")
        assert "pair_andor_backend" not in widget._collect_params()
        with pytest.raises(ValueError, match="2100 station"):
            widget.restore_session_state(state)
        with pytest.raises(ValueError, match="2100 station"):
            widget.apply_saved_experiment_settings(params)
        monkeypatch.setattr(cfg.lf6, "station_id", "attodry2100")
        widget._pair_controls.set_station("attodry2100")
        widget.apply_saved_experiment_settings(params)
        assert widget._collect_params()["pair_recipes"] == params["pair_recipes"]
    finally:
        widget.close()


class MegaSweepSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_sequence_state_round_trip_and_legacy_restore(self):
        panel = MegaSweepPanel()
        self.addCleanup(panel.close)
        panel._optical_widget.set_conditions([
            OpticalCondition(True, "first", 721.5, 31.0, 4),
            OpticalCondition(False, "second", 755.0, 80.0, 9),
        ])

        state = panel.capture_session_state()
        restored = MegaSweepPanel()
        self.addCleanup(restored.close)
        restored.restore_session_state(state)
        self.assertEqual(
            restored._optical_widget.conditions(),
            panel._optical_widget.conditions(),
        )

        legacy = MegaSweepPanel()
        self.addCleanup(legacy.close)
        legacy.restore_session_state({
            "optical": {"center_nm": 812.0, "exposure_ms": 55.0, "frames": 7}
        })
        self.assertEqual(
            legacy._optical_widget.conditions(),
            [OpticalCondition(True, "C1", 812.0, 55.0, 7)],
        )

    def test_run_metadata_accepts_raw_and_physical_coordinate_settings(self):
        panel = MegaSweepPanel()
        self.addCleanup(panel.close)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ExperimentMetadataService(root, root / "history.sqlite")
            for coord, radio in [(CoordSystem.RAW, panel._coord_widget._raw),
                                 (CoordSystem.PHYSICAL, panel._coord_widget._physical)]:
                with self.subTest(coord=coord):
                    radio.setChecked(True)
                    panel._refresh_preview()
                    params = panel._collect_params()
                    params["out_path"] = temp_dir
                    run = service.begin("gate_map_2d", "test", output_dir=root, settings=params)
                    run.complete()
                    metadata = json.loads(run.path.read_text(encoding="utf-8"))
                    self.assertEqual(metadata["settings"]["requested"]["coord"], coord.value)

    def test_bias_can_swap_from_fast_to_slow_axis(self):
        selector = _AxisSelectorWidget()
        self.addCleanup(selector.close)
        selector.set_available_axes(["Vtg", "Vbg", "Vbias"])
        selector._inner.setCurrentText("Vbias")
        selector._outer.setCurrentText("Vbias")
        self.assertEqual((selector.outer(), selector.inner()), ("Vbias", "Vtg"))
        selector._inner.setCurrentText("Vbias")
        self.assertEqual((selector.outer(), selector.inner()), ("Vtg", "Vbias"))
        selector.set_available_axes(["Vtg", "Vbg"])
        self.assertEqual((selector.outer(), selector.inner()), ("Vtg", "Vbg"))
        self.assertEqual(selector._outer.findText("Vbias"), -1)

    def test_map_eta_uses_measured_speed_and_resets_between_setups(self):
        panel = MegaSweepPanel()
        self.addCleanup(panel.close)
        panel._on_map_started(1, 2, "PIXIS")
        with patch("ui.megasweep_panel.time.monotonic", side_effect=[0, 20]):
            panel._on_progress(1, 200)
            panel._on_progress(11, 200)
        self.assertIn("2 min 58 sec", panel._status_lbl.text())
        panel._on_map_started(2, 2, "WinSpec")
        with patch("ui.megasweep_panel.time.monotonic", side_effect=[30, 110]):
            panel._on_progress(101, 200)
            self.assertNotIn("remaining", panel._status_lbl.text())
            panel._on_progress(111, 200)
        self.assertIn("11 min 52 sec", panel._status_lbl.text())

    def test_bias_slow_axis_holds_bias_for_each_snake_row(self):
        safety = {"vtg_min": -5, "vtg_max": 5, "vbg_min": -5,
                  "vbg_max": 5, "vbias_min": -1, "vbias_max": 1}
        points, valid = build_sweep_points(
            CoordSystem.RAW, "Vbias", np.array([0.01, 0.02]),
            "Vtg", np.array([0.0, 0.1, 0.2]), {"Vbg": 0.5}, 1, safety, True,
        )
        self.assertEqual([p["raw"] for p in valid], [
            (0.0, 0.5, 0.01), (0.1, 0.5, 0.01), (0.2, 0.5, 0.01),
            (0.2, 0.5, 0.02), (0.1, 0.5, 0.02), (0.0, 0.5, 0.02),
        ])
        self.assertEqual(len(points), 6)

    def test_physical_mode_bias_slow_axis_with_doping_or_efield(self):
        panel = MegaSweepPanel()
        self.addCleanup(panel.close)
        panel._smu = _FakeSMUController()
        panel._coord_widget._physical.setChecked(True)
        selector = panel._axis_selector
        selector._inner.setCurrentText("Vbias")
        selector._outer.setCurrentText("Vbias")
        self.assertEqual((selector.outer(), selector.inner()), ("Vbias", "Doping"))
        safety = {"vtg_min": -5, "vtg_max": 5, "vbg_min": -5,
                  "vbg_max": 5, "vbias_min": -1, "vbias_max": 1}
        for fast, fixed, expected in [
            ("Doping", {"E-field": 0.2}, [(0.3, 0.05), (0.5, 0.15)]),
            ("E-field", {"Doping": 0.2}, [(0.3, -0.05), (0.5, -0.15)]),
        ]:
            with self.subTest(fast=fast):
                selector._inner.setCurrentText(fast)
                self.assertEqual(selector.outer(), "Vbias")
                self.assertEqual(set(panel._fixed_widget.get_values()), set(fixed))
                _, points = build_sweep_points(
                    CoordSystem.PHYSICAL, selector.outer(), np.array([0.01, 0.02]),
                    selector.inner(), np.array([0.4, 0.8]), fixed, 2, safety, True,
                )
                np.testing.assert_allclose([p["raw"] for p in points], [
                    (*expected[0], 0.01), (*expected[1], 0.01),
                    (*expected[1], 0.02), (*expected[0], 0.02),
                ])
                state = panel.capture_session_state()
                panel.restore_session_state(state)
                self.assertEqual((selector.outer(), selector.inner()), ("Vbias", fast))

    def test_worker_creates_one_complete_file_per_condition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            smu = _FakeSMUController()
            lf6 = _FakeLF6Controller()
            worker = _MegaSweepWorker(_params(Path(temp_dir)), smu, lf6)
            progress = []
            maps = []
            worker.progress.connect(lambda done, total: progress.append((done, total)))
            worker.map_started.connect(
                lambda index, count, description: maps.append((index, count, description))
            )

            with patch(
                "ui.megasweep_panel._get_wavelengths",
                side_effect=lambda _spec, _lf6, center, tol_nm: np.array(
                    [center - 1.0, center, center + 1.0]
                ),
            ):
                worker._run_sweep(worker._p)

            csv_files = sorted(Path(temp_dir).glob("*.csv"))
            meta_files = sorted(Path(temp_dir).glob("*.meta.txt"))
            self.assertEqual(len(csv_files), 2)
            self.assertEqual(len(meta_files), 2)
            self.assertIn("C01_red", csv_files[0].name)
            self.assertIn("C02_blue", csv_files[1].name)
            self.assertTrue(all(len(path.read_text().splitlines()) == 3 for path in csv_files))
            self.assertTrue(all("# Status: Complete" in path.read_text() for path in meta_files))
            self.assertTrue(all("# CompletedPoints: 2" in path.read_text() for path in meta_files))
            self.assertEqual(lf6.adapter.centers, [720.0, 750.0])
            self.assertEqual(lf6.adapter.exposures, [30.0, 40.0])
            self.assertEqual(lf6.adapter.frames, [2, 3])
            self.assertEqual(smu.device.zero_ramps, 2)
            self.assertEqual(progress[-1], (4, 4))
            self.assertEqual([item[:2] for item in maps], [(1, 2), (2, 2)])

    def test_worker_saves_voltage_and_current_from_one_read_per_role(self):
        with tempfile.TemporaryDirectory() as folder:
            smu = _FakeSMUController()
            reads = []
            def read_role(role):
                reads.append(role)
                return {"Vbg": (0.21, 1e-9), "Vtg": (0.12, 2e-9),
                        "Vbias": (0.03, 3e-9)}[role]
            smu.device.read_role_snapshot = read_role
            smu.device.read_currents = lambda: self.fail("duplicate current acquisition")
            smu.device.read_current_bias = lambda: self.fail("duplicate bias acquisition")
            worker = _MegaSweepWorker(_params(Path(folder)), smu, _FakeLF6Controller())
            worker._run_sweep(worker._p)
            self.assertEqual(reads, ["Vbg", "Vtg", "Vbias"] * 4)
            for path in Path(folder).glob("*.csv"):
                rows = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
                np.testing.assert_allclose(rows[:, 7:13], [
                    [0.21, 0.12, 0.03, 1e-9, 2e-9, 3e-9],
                    [0.21, 0.12, 0.03, 1e-9, 2e-9, 3e-9],
                ], atol=0)

    def test_paired_readback_failure_does_not_retry_or_lose_other_channels(self):
        class Device:
            def read_role_snapshot(self, role):
                if role == "Vbg":
                    raise RuntimeError("read failed")
                if role == "Vtg":
                    return 0.2, 2e-9
                return None, None
        values = _read_point_electrical(Device())
        self.assertEqual((values[1], values[4]), (0.2, 2e-9))
        self.assertTrue(all(np.isnan(values[i]) for i in (0, 2, 3, 5)))

    def test_stop_preserves_partial_map_and_does_not_start_next_map(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            smu = _FakeSMUController()
            lf6 = _FakeLF6Controller()
            worker = _MegaSweepWorker(_params(Path(temp_dir)), smu, lf6)
            worker.progress.connect(
                lambda done, _total: worker.request_stop() if done == 1 else None
            )

            with patch(
                "ui.megasweep_panel._get_wavelengths",
                return_value=np.array([719.0, 720.0, 721.0]),
            ):
                worker._run_sweep(worker._p)

            csv_files = list(Path(temp_dir).glob("*.csv"))
            meta_files = list(Path(temp_dir).glob("*.meta.txt"))
            self.assertEqual(len(csv_files), 1)
            self.assertEqual(len(csv_files[0].read_text().splitlines()), 2)
            self.assertEqual(len(meta_files), 1)
            metadata = meta_files[0].read_text()
            self.assertIn("# Status: Stopped", metadata)
            self.assertIn("# CompletedPoints: 1", metadata)
            self.assertEqual(lf6.adapter.centers, [720.0])
            self.assertEqual(smu.device.zero_ramps, 1)


@pytest.mark.parametrize('failure', ['axis', 'validation', 'shape'])
def test_map_stops_before_writing_invalid_frame_and_retains_verified_rows(tmp_path, failure):
    app = QApplication.instance() or QApplication([])
    smu, lf6 = _FakeSMUController(), _FakeLF6Controller()
    original = lf6.adapter.acquire

    def acquire():
        axis, counts = original()
        if lf6.adapter.acquire_count == 2:
            if failure == 'validation':
                raise RuntimeError('LightField grating readback mismatch')
            if failure == 'axis':
                axis = np.array([718., 720., 722.])
            if failure == 'shape':
                counts = np.array([10., 20.])
        return axis, counts

    lf6.adapter.acquire = acquire
    worker = _MegaSweepWorker(_params(tmp_path), smu, lf6)
    with patch('ui.megasweep_panel._get_wavelengths', return_value=np.array([719., 720., 721.])):
        with pytest.raises(RuntimeError, match='grating|[Ww]avelength|shape'):
            worker._run_sweep(worker._p)
    files = list(tmp_path.glob('*.csv'))
    assert len(files) == 1
    assert len(files[0].read_text().splitlines()) == 2  # Header + verified first row.
    metadata = next(tmp_path.glob('*.meta.txt')).read_text()
    assert '# Status: Failed' in metadata
    assert '# CompletedPoints: 1' in metadata


if __name__ == "__main__":
    unittest.main()


def test_map_filename_is_compact_and_names_each_paired_detector_by_its_own_recipe():
    from ui import megasweep_panel as mega
    params = dict(sample="S1", tag="Tag", axis_a="Vbias", axis_b="Doping", coord=mega.CoordSystem.PHYSICAL,
                  ratio=1.0, fixed={"E-field": 0.0}, laser_nm="730", power_uw="1",
                  axis_a_desc=dict(start=-0.7, stop=-2.2, points=31), axis_b_desc=dict(start=-5, stop=5, points=201),
                  condition_index=1, condition_count=1, condition_name="PIXIS + Andor InGaAs",
                  center_nm=650, exp_ms=50, frames=20, pair_andor_backend="andor_ingaas",
                  pair_recipes={"lightfield": dict(center_nm=650, exposure_ms=50, frames=20),
                                "andor_ingaas": dict(center_nm=1100, exposure_ms=500, frames=4)})
    assert mega.build_megasweep_filename(params, detector="lightfield") == (
        "S1~Vb-0.7to-2.2x31_D-5to+5x201~E0~730nm1uW~650nm_0.05sx20~Tag")
    assert "~1100nm_0.5sx4~" in mega.build_megasweep_filename(params, detector="andor_ingaas")
    several = dict(params, pair_andor_backend="", backend="winspec_ingaas", condition_count=2, ratio=1.1)
    assert mega.build_megasweep_filename(several).endswith("x201_r1.1~E0~730nm1uW~C01~650nm_0.05sx20~Tag~WinSpec")
