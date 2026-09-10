import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ui.spectrum_panel import SpectrumPanel


class SpectrumMetadataSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_real_apply_then_acquire_freezes_request_before_later_edit(self):
        class Controller(QObject):
            connected = Signal()
            disconnected = Signal()
            spectrum_ready = Signal(object, object)
            frame_ready = Signal(object)
            settings_applied = Signal()
            error = Signal(str)

            def __init__(self):
                super().__init__()
                self.calls = []

            is_connected = True

            def apply_settings(self, **values):
                self.calls.append(values)

            def acquire_single(self):
                self.calls.append("acquire")

            def acquire_2d(self):
                self.calls.append("acquire2d")

            def abort_acquisition(self):
                return False

        ctrl = Controller()
        panel = SpectrumPanel(ctrl)
        panel._connected = True
        panel._center.setValue(700.0)
        panel._exposure.setValue(10.0)
        panel._accumulations.setValue(1)
        panel._apply_settings_then("1d")
        panel._center.setValue(900.0)
        panel._exposure.setValue(999.0)
        ctrl.settings_applied.emit()
        ctrl.spectrum_ready.emit(np.array([700.0, 701.0]), np.array([1.0, 2.0]))
        self.assertEqual(ctrl.calls[0], {
            "exposure_ms": 10.0, "center_nm": 700.0, "accumulations": 1,
        })
        self.assertEqual(panel._last_acquisition_snapshot["exposure_ms"], 10.0)
        self.assertEqual(panel._last_acquisition_snapshot["center_nm"], 700.0)

    def test_save_uses_frame_snapshot_when_later_settings_are_applied(self):
        panel = SpectrumPanel.__new__(SpectrumPanel)
        panel._last_data_kind = "spectrum_1d"
        panel._last_wavelength = np.array([700.0, 701.0])
        panel._last_data = np.array([1.0, 2.0])
        panel._last_acquisition_snapshot = {"exposure_ms": 10.0, "center_nm": 700.0}
        panel._acquisition_settings_snapshot = {"exposure_ms": 999.0, "center_nm": 900.0}
        panel._ctrl = SimpleNamespace(identity={})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "spectrum.csv"
            panel.save_current(output)
            sidecar = next(Path(directory).glob("*.experiment.metadata.json"))
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(metadata["settings"]["requested"]["acquisition_time_snapshot"]["exposure_ms"], 10.0)


if __name__ == "__main__":
    unittest.main()
