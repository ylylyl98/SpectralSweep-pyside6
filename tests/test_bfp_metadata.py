from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from unittest.mock import patch

import ui.bfp_panel_integrated as bfp_ui
from ui.bfp_panel_integrated import BFPPanel, _BRCWidget, _FRCWidget
from utils.bfp_io import save_binned_csv, save_full_image_csv


class BFPMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "DeviceA" / "inputs"
        self.root.mkdir(parents=True)
        wl = np.linspace(700, 704, 5)
        save_binned_csv(self.root / "sample.csv", wl, np.arange(5, dtype=float) + 2)
        save_binned_csv(self.root / "background.csv", wl, np.arange(5, dtype=float) + 1)
        # The production full-image loader requires at least four monotonic
        # samples on each interpolation axis.
        y = np.arange(4, dtype=float)
        image = np.arange(20, dtype=float).reshape(4, 5) + 1
        save_full_image_csv(self.root / "sample_full.csv", wl, y, image)
        save_full_image_csv(self.root / "background_full.csv", wl, y, image / 2)

    def tearDown(self):
        self.tmp.cleanup()

    def _sidecar(self, kind):
        paths = sorted(self.root.glob("*.experiment.metadata.json"))
        matching = []
        for path in paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["experiment_type"] == kind:
                matching.append((path, data))
        return matching

    def test_bfp_settings_are_scrollable_and_do_not_force_tall_main_window(self):
        panel = BFPPanel()
        self.addCleanup(panel.deleteLater)
        self.assertTrue(panel._settings_scroll.widgetResizable())
        self.assertEqual(
            panel._settings_scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertLess(panel.minimumSizeHint().height(), 950)

    def test_binned_png_only_gets_sidecar_and_csv_png_share_one_id(self):
        widget = _BRCWidget()
        widget._sample_edit.setText(str(self.root / "sample.csv"))
        widget._bg_edit.setText(str(self.root / "background.csv"))
        widget.compute()
        self.assertEqual(self._sidecar("bfp_binned_rc"), [])
        widget.save_png()
        records = self._sidecar("bfp_binned_rc")
        self.assertEqual(len(records), 1)
        png_only = records[0][1]
        self.assertEqual([item["role"] for item in png_only["files"] if item["role"] != "metadata"], ["figure"])
        widget.save_csv()
        records = self._sidecar("bfp_binned_rc")
        self.assertEqual(len(records), 1)
        data = records[0][1]
        self.assertEqual({item["role"] for item in data["files"]}, {"metadata", "figure", "processed"})
        widget.close()

    def test_recompute_invalidates_processing_context(self):
        widget = _BRCWidget()
        widget._sample_edit.setText(str(self.root / "sample.csv"))
        widget._bg_edit.setText(str(self.root / "background.csv"))
        widget.compute(); widget.save_png()
        first = self._sidecar("bfp_binned_rc")[0][1]["experiment_id"]
        widget.compute(); widget.save_png()
        ids = {data["experiment_id"] for _, data in self._sidecar("bfp_binned_rc")}
        self.assertEqual(len(ids), 2)
        self.assertIn(first, ids)
        widget.close()

    def test_failed_first_export_retains_failed_sidecar(self):
        widget = _BRCWidget()
        widget._sample_edit.setText(str(self.root / "sample.csv"))
        widget._bg_edit.setText(str(self.root / "background.csv"))
        widget.compute()
        with patch.object(bfp_ui, "_save_figure_atomic", side_effect=OSError("disk full")):
            widget.save_png()
        records = self._sidecar("bfp_binned_rc")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1]["status"], "failed")
        self.assertEqual(records[0][1]["result"]["error"]["message"], "disk full")
        widget.close()

    def test_full_sensor_csv_and_png_share_one_id(self):
        widget = _FRCWidget()
        widget._sample_edit.setText(str(self.root / "sample_full.csv"))
        widget._bg_edit.setText(str(self.root / "background_full.csv"))
        widget.compute(); widget.save_csv(); widget.save_png()
        records = self._sidecar("bfp_full_sensor_rc")
        self.assertEqual(len(records), 1)
        data = records[0][1]
        self.assertEqual({item["role"] for item in data["files"]}, {"metadata", "processed", "figure"})
        widget.close()


if __name__ == "__main__":
    unittest.main()
