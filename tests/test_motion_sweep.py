from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLineEdit

from ui.main_window import _SharedSampleIdBinder
from ui.power_sweep_panel import PowerSweepPanel, _PowerSweepWorker
from utils.config import cfg


class _FakeMotionAdapter:
    minimum_position = 0.0
    maximum_position = 50.0
    position_unit = "mm"

    def __init__(self, position: float = 7.0):
        self.position = position
        self.moves = []

    def move_to(self, value: float):
        self.position = float(value)
        self.moves.append(float(value))

    def get_position(self) -> float:
        return self.position


class _FakeStageController(QObject):
    connected = Signal(str)
    disconnected = Signal()

    def __init__(self, connected: bool = True):
        super().__init__()
        self.is_connected = connected
        self.adapter = _FakeMotionAdapter()


class _FakeRotationController(QObject):
    connected = Signal(str, str)
    disconnected = Signal(str)

    def __init__(self):
        super().__init__()
        self.adapters = {
            "rot1": _FakeMotionAdapter(position=12.0),
            "rot2": _FakeMotionAdapter(position=24.0),
        }

    def is_connected(self, slot: str) -> bool:
        return slot in self.adapters

    def adapter(self, slot: str):
        return self.adapters.get(slot)


class _FakeSpectrum:
    def calibration_wavelengths(self, force=False):
        return np.array([700.0, 701.0, 702.0])

    def acquire(self):
        return (
            np.array([700.0, 701.0, 702.0]),
            np.array([1.0, 2.0, 3.0]),
        )


class _FakeLF6Controller(QObject):
    connected = Signal(list)
    disconnected = Signal()

    def __init__(self):
        super().__init__()
        self.is_connected = True
        self.adapter = _FakeSpectrum()
        self.setup = None


def _worker_params(out_path: str, motion_key: str) -> dict:
    return {
        "positions": np.array([1.0, 2.0]),
        "motion_key": motion_key,
        "motion_settle_s": 0.0,
        "return_motion_to_start": True,
        "center_nm": 730.0,
        "exp_ms": 10.0,
        "frames": 1,
        "pm_wl_nm": 730.0,
        "out_path": out_path,
        "base_name": f"motion_{motion_key}",
        "apply_gates": False,
        "return_to_zero": False,
    }


@pytest.mark.parametrize("motion_axes", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_paired_motion_writes_each_detector_with_the_same_point_coordinates(tmp_path, motion_axes, partial):
    stage = _FakeStageController()
    spec = _FakeLF6Controller()
    spec.configure_for_acquisition = Mock(side_effect=AssertionError("single-detector settings called"))
    spec.validate_station_pair = Mock()
    params = _worker_params(str(tmp_path), "stage")
    recipes = {"lightfield": dict(center_nm=690, exposure_ms=4, frames=1),
               "andor_si": dict(center_nm=750, exposure_ms=32, frames=3)}
    params.update(station_pair_backend="andor_si", station_pair_recipes=recipes)
    if motion_axes:
        params["motion_axes"] = {"stage": {"mode": "sweep", "values": [1, 2]}}

    def acquire(backend, settings, stop, *, owner):
        assert backend == "andor_si" and settings == recipes and owner == "power_sweep"
        assert stage.adapter.get_position() in (1.0, 2.0)
        return {
            "lightfield": dict(settings=settings["lightfield"], wavelengths=[690, 691], counts=[14, 20]),
            "andor_si": (dict(settings=settings["andor_si"], error="Detector failed") if partial else
                         dict(settings=settings["andor_si"], wavelengths=[750, 751, 752], counts=[4, 5, 6])),
        }

    spec.acquire_station_pair = Mock(side_effect=acquire)
    worker = _PowerSweepWorker(params, stage, None, None, spec, None)
    if partial:
        with pytest.raises(RuntimeError, match="Detector failed"):
            worker._run_sweep(worker._p)
    else:
        worker._run_sweep(worker._p)
    spec.configure_for_acquisition.assert_not_called()
    assert spec.acquire_station_pair.call_count == (1 if partial else 2)
    with (tmp_path / "motion_stage_PIXIS.csv").open(newline="", encoding="utf-8") as stream:
        pixis = list(csv.DictReader(stream))
    assert list(pixis[0].keys())[-2:] == ["690.0000", "691.0000"]
    assert pixis[0]["point_id"] == "1"
    paired_file = tmp_path / "motion_stage_Andor_Si.csv"
    if partial:
        assert not paired_file.exists()
    else:
        with paired_file.open(newline="", encoding="utf-8") as stream:
            andor = list(csv.DictReader(stream))
        assert list(andor[0].keys())[-3:] == ["750.0000", "751.0000", "752.0000"]
        assert [row["point_id"] for row in andor] == [row["point_id"] for row in pixis]
        actual_column = "stage_actual" if motion_axes else "stage_pos_actual"
        assert [row[actual_column] for row in andor] == ["1", "2"]
    manifest = (tmp_path / "motion_stage_pairs.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(manifest[-1])["status"] == ("incomplete" if partial else "complete")


class MotionSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_validation_allows_missing_optional_power_meter(self):
        panel = PowerSweepPanel(
            lf6_ctrl=_FakeLF6Controller(),
            stage_ctrl=_FakeStageController(),
            rotation_ctrl=_FakeRotationController(),
            pm_ctrl=None,
            smu_ctrl=None,
        )
        panel._apply_gates_chk.setChecked(False)

        self.assertTrue(panel._validate())
        panel._hardware_grp.setChecked(True)
        self.assertTrue(panel._pm_notice_lbl.isVisibleTo(panel))

    def test_history_refuses_paired_recipe_at_another_station(self):
        panel = PowerSweepPanel(lf6_ctrl=_FakeLF6Controller())
        with unittest.mock.patch.object(cfg.lf6, "station_id", "default"):
            keys = panel.apply_saved_experiment_settings({}, keys_only=True)["supported_keys"]
            self.assertIn("station_pair_backend", keys)
            original = panel._center_spin.value()
            with self.assertRaisesRegex(ValueError, "requires the attoDRY 2100"):
                panel.apply_saved_experiment_settings(
                    {"center_nm": 675, "station_pair_backend": "andor_si", "station_pair_recipes": {}})
            self.assertEqual(panel._center_spin.value(), original)
        panel.close()

    def test_rot1_sweep_without_pm_omits_power_and_restores_position(self):
        stage = _FakeStageController()
        rotation = _FakeRotationController()
        lf6 = _FakeLF6Controller()
        initial = rotation.adapter("rot1").get_position()

        with tempfile.TemporaryDirectory() as tmp:
            worker = _PowerSweepWorker(
                _worker_params(tmp, "rot1"),
                stage,
                rotation,
                None,
                lf6,
                None,
            )
            worker._run_sweep(worker._p)

            output = Path(tmp) / "motion_rot1.csv"
            with output.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))

        self.assertNotIn("Power_uW", rows[0])
        self.assertIn("rot1_deg", rows[0])
        self.assertIn("rot1_deg_actual", rows[0])
        self.assertEqual(rotation.adapter("rot1").moves, [1.0, 2.0, initial])
        self.assertEqual(stage.adapter.moves, [])

    def test_shared_sample_id_binder_uses_latest_edit(self):
        edits = [QLineEdit("old-a"), QLineEdit("old-b"), QLineEdit()]
        binder = _SharedSampleIdBinder(edits, initial="initial")
        self.assertEqual([edit.text() for edit in edits], ["initial"] * 3)

        edits[1].setText("latest-sample")

        self.assertEqual(binder.value, "latest-sample")
        self.assertEqual(
            [edit.text() for edit in edits],
            ["latest-sample"] * 3,
        )

    def test_shared_sample_id_binder_can_defer_until_explicit_commit(self):
        edits = [QLineEdit("old-a"), QLineEdit("old-b")]
        binder = _SharedSampleIdBinder(edits, initial="initial", commit_on_edit=True)
        edits[0].setText("draft")

        self.assertEqual(binder.value, "initial")
        self.assertEqual(edits[1].text(), "initial")
        binder.commit("draft")
        self.assertEqual(binder.value, "draft")
        self.assertEqual([edit.text() for edit in edits], ["draft", "draft"])

    def test_rot2_sweep_only_moves_rot2_and_saves_angles(self):
        rotation = _FakeRotationController()
        with tempfile.TemporaryDirectory() as tmp:
            p = _worker_params(tmp, "rot2")
            worker = _PowerSweepWorker(p, None, rotation, None, _FakeLF6Controller(), None)
            worker._run_sweep(p)
            with (Path(tmp) / "motion_rot2.csv").open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        self.assertEqual(rotation.adapter("rot2").moves, [1.0, 2.0, 24.0])
        self.assertEqual(rotation.adapter("rot1").moves, [])
        self.assertEqual([float(row["rot2_deg_actual"]) for row in rows], [1.0, 2.0])

    def test_rot_axis_ui_hides_stage_power_controls(self):
        panel = PowerSweepPanel()
        for axis in ("rot1", "rot2"):
            panel._motion_buttons[axis].click()
            self.assertEqual(panel._motion_combo.currentData(), axis)
            self.assertTrue(panel._motion_buttons[axis].isChecked())
            self.assertIn("Values are interpreted in deg", panel._pos_input.toolTip())
            self.assertTrue(panel._input_mode.isHidden())
            self.assertTrue(panel._cal_grp.isHidden())
        panel._motion_combo.setCurrentIndex(panel._motion_combo.findData("stage"))
        self.assertTrue(panel._motion_buttons["stage"].isChecked())
        self.assertFalse(panel._input_mode.isHidden())
        panel.close()


if __name__ == "__main__":
    unittest.main()
