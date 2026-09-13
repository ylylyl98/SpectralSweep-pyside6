import csv
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.power_reading import power_correction_factor, read_power
from utils.config import AppConfig, cfg
from utils.filename_builder import FilenameContext, resolve_power_uw


class PowerCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_raw_and_corrected_reading_and_filename_are_not_double_scaled(self):
        adapter = Mock()
        adapter.get_power.return_value = 12.5e-6
        with patch.object(cfg.pm100d, "correction_factor", 2.0):
            reading = read_power(adapter)
        adapter.get_power.assert_called_once_with()
        self.assertAlmostEqual(reading.raw_w, 12.5e-6)
        self.assertAlmostEqual(reading.corrected_w, 25e-6)
        ctx = FilenameContext(device_id="sample", tag="", temperature="", mode="PL",
                              laser_nm="532", nominal_power_uw="10", center_nm="700",
                              exposure_ms="10", accumulations="1", measured_power_uw=25,
                              measure_power=True, power_coefficient=2)
        self.assertEqual(resolve_power_uw(ctx), (25.0, "measured"))

    def test_invalid_factor_rejected_before_hardware_read(self):
        for value in (0, -1, float("nan"), float("inf")):
            adapter = Mock()
            with self.assertRaises(ValueError):
                read_power(adapter, factor=value)
            adapter.get_power.assert_not_called()

    def test_config_migration_and_new_setting_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"filename": {"power_coefficient": 2.5}}))
            config = AppConfig()
            config.load(path)
            self.assertEqual(config.pm100d.correction_factor, 2.5)
            config.pm100d.correction_factor = 0.25
            config.save(path)
            restored = AppConfig()
            restored.load(path)
            self.assertEqual(restored.pm100d.correction_factor, 0.25)

    def test_motion_export_freezes_factor_and_saves_raw_and_corrected(self):
        from tests.test_motion_sweep import (
            _FakeStageController, _FakeRotationController, _FakeLF6Controller,
            _worker_params,
        )
        from ui.power_sweep_panel import _PowerSweepWorker
        adapter = Mock()
        # Changing the global setting during a read must not affect this run.
        def raw_read():
            cfg.pm100d.correction_factor = 10.0
            return 3e-6
        adapter.get_power.side_effect = raw_read
        with tempfile.TemporaryDirectory() as folder, patch.object(cfg.pm100d, "correction_factor", 2.0):
            worker = _PowerSweepWorker(
                _worker_params(folder, "rot1"), _FakeStageController(),
                _FakeRotationController(), SimpleNamespace(is_connected=True, adapter=adapter),
                _FakeLF6Controller(), None)
            worker._run_sweep(worker._p)
            with (Path(folder) / "motion_rot1.csv").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(float(row["Power_raw_uW"]), 3.0)
                self.assertEqual(float(row["Power_uW"]), 6.0)
                self.assertEqual(float(row["Power_correction_factor"]), 2.0)

    def test_controller_and_sidebar_worker_use_the_same_correction(self):
        from controllers.pm100d_controller import _PM100DWorker
        from ui.instrument_panel import _PMReadWorker
        adapter = Mock()
        adapter.get_power.return_value = 4e-6
        controller_worker = _PM100DWorker()
        controller_worker._adapter = adapter
        sidebar_worker = _PMReadWorker(SimpleNamespace(adapter=adapter))
        readings, details = [], []
        controller_worker.power_ready.connect(readings.append)
        sidebar_worker.reading.connect(readings.append)
        sidebar_worker.details.connect(details.append)
        with patch.object(cfg.pm100d, "correction_factor", 3.0):
            controller_worker.read_power()
            sidebar_worker.run()
        self.assertEqual(len(readings), 2)
        for reading in readings:
            self.assertAlmostEqual(reading, 12e-6)
        self.assertEqual(details[0].raw_w, 4e-6)

    def test_sidebar_factor_is_shared_and_readout_exposes_raw_value(self):
        from PySide6.QtWidgets import QApplication
        from tests.test_pm100d_adapter import _FakePMController
        from ui.instrument_panel import _PM100DSection
        app = QApplication.instance() or QApplication([])
        with patch.object(cfg.pm100d, "correction_factor", 1.0):
            section = _PM100DSection(_FakePMController())
            section._factor_spn.setValue(4.0)
            self.assertEqual(power_correction_factor(), 4.0)
            adapter = Mock()
            adapter.get_power.return_value = 2e-6
            reading = read_power(adapter)
            section._on_reading(reading.corrected_w)
            section._on_reading_details(reading)
            self.assertIn("8", section._pwr_lbl.text())
            self.assertIn("Raw: 2", section._pwr_lbl.toolTip())
            self.assertIn("factor: 4", section._pwr_lbl.toolTip())
            section.close()

    def test_sidebar_factor_is_observed_autosaved_and_loaded_as_canonical_config(self):
        from tests.test_pm100d_adapter import _FakePMController
        from ui.instrument_panel import InstrumentPanel
        from ui.main_window import MainWindow
        import utils.config as config_module

        old_factor = cfg.pm100d.correction_factor
        old_session = copy.deepcopy(cfg.session)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "config.json"
                panel = InstrumentPanel(pm_ctrl=_FakePMController())
                self.addCleanup(panel.close)
                section = panel._sections["pm100d"]
                main = MainWindow.__new__(MainWindow)
                main._active_tab_id = lambda: "instrument"
                main._session_panels = {"instrument": panel}
                main._last_observed_session = MainWindow._capture_session(main)
                main._session_save_timer = Mock()

                section._factor_spn.setValue(2.75)
                MainWindow._poll_session_changes(main)
                self.assertTrue(main._session_save_timer.start.called)
                self.assertEqual(
                    main._last_observed_session["panels"]["instrument"]["pm100d"]["correction_factor"],
                    2.75,
                )

                with patch.object(config_module, "_CONFIG_FILE", path):
                    MainWindow._persist_session(main)
                restored_config = AppConfig()
                restored_config.load(path)
                self.assertEqual(restored_config.pm100d.correction_factor, 2.75)
                from ui.instrument_panel import _PM100DSection
                with patch.object(cfg.pm100d, "correction_factor", restored_config.pm100d.correction_factor):
                    fresh_sidebar = _PM100DSection(_FakePMController())
                    self.assertEqual(fresh_sidebar._factor_spn.value(), 2.75)
                    fresh_sidebar.close()

                # A stale session duplicate is observation data only; startup
                # and the sidebar continue to use the canonical cfg value.
                stale = panel.capture_session_state()
                stale["pm100d"]["correction_factor"] = 0.25
                cfg.pm100d.correction_factor = 2.75
                panel.restore_session_state(stale)
                self.assertEqual(section._factor_spn.value(), 2.75)
        finally:
            cfg.pm100d.correction_factor = old_factor
            cfg.session = old_session

    def test_presets_export_and_filename_share_one_frozen_correction(self):
        import threading
        import pandas as pd
        from tests.test_smu_resilience import SMUResilienceTests, _HealthyRunDevice, _FakeLFController
        from ui.presets_panel import _RunWorker
        adapter = Mock()
        def raw_read():
            cfg.pm100d.correction_factor = 9.0
            return 4e-6
        adapter.get_power.side_effect = raw_read
        sequence = [{"Center Wavelength (nm)": 700.0,
                     "Exposure Time (ms)": 1.0, "Accumulations (EPF)": 1}]
        batch = pd.DataFrame([SMUResilienceTests._batch_row(MeasurePower=True, repeat=2)])
        with tempfile.TemporaryDirectory() as folder, patch.object(cfg.pm100d, "correction_factor", 2.0):
            meta = SMUResilienceTests._run_meta()
            meta.update(initial_voltage_settle_s=0.0, voltage_settle_s=0.0)
            worker = _RunWorker(sequence, batch, lf6_ctrl=_FakeLFController(),
                                smu_ctrl=SimpleNamespace(is_connected=True, device=_HealthyRunDevice()),
                                pm_ctrl=SimpleNamespace(is_connected=True, adapter=adapter),
                                out_dir=Path(folder), run_meta=meta, filename_parts=["laser_power"],
                                stop_event=threading.Event())
            finished = []
            worker.finished.connect(lambda success, message: finished.append((success, message)))
            with patch.object(cfg.ramp, "delay_s", 0.0), patch.object(cfg.ramp, "settle_s", 0.0):
                worker.run()
            self.assertTrue(finished and finished[0][0], finished)
            paths = list((Path(folder) / "PL").glob("*.csv"))
            self.assertEqual(len(paths), 2)
            for path in paths:
                self.assertIn("633nm8.000uW", path.name)
                with path.open(newline="", encoding="utf-8-sig") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(float(rows[0]["Power_raw_uW"]), 4.0)
                self.assertEqual(float(rows[0]["Power_uW"]), 8.0)
                self.assertEqual(float(rows[0]["Power_correction_factor"]), 2.0)


if __name__ == "__main__":
    unittest.main()
