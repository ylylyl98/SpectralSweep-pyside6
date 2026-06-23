from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.devices.spectrum_alignment import align_wavelengths_to_intensities
from app.engine.csv_writer import CSVWriter


class SpectrumAlignmentTests(unittest.TestCase):
    def test_trims_trailing_calibration_to_spectrum_width(self) -> None:
        wavelengths, counts = align_wavelengths_to_intensities(
            np.arange(1030, dtype=float), np.arange(1024, dtype=float)
        )

        self.assertEqual(wavelengths.size, 1024)
        self.assertEqual(counts.size, 1024)
        self.assertEqual(wavelengths[-1], 1023.0)

    def test_preserves_arbitrary_matching_spectrum_widths(self) -> None:
        for width in (512, 1024, 2048):
            wavelengths, counts = align_wavelengths_to_intensities(
                np.arange(width, dtype=float), np.arange(width, dtype=float)
            )

            self.assertEqual(wavelengths.size, width)
            self.assertEqual(counts.size, width)

    def test_rejects_calibration_shorter_than_spectrum(self) -> None:
        with self.assertRaisesRegex(ValueError, "calibration has 1023 values"):
            align_wavelengths_to_intensities(
                np.arange(1023, dtype=float), np.arange(1024, dtype=float)
            )


class CSVWriterTests(unittest.TestCase):
    def test_trims_excess_calibration_before_dual_gate_export(self) -> None:
        wavelengths, counts = align_wavelengths_to_intensities(
            np.arange(1030, dtype=float), np.arange(1024, dtype=float)
        )
        scalar_fields = [
            "Vbg_set", "Vbg_meas", "Vtg_set", "Vtg_meas", "Vbias_set",
            "Vbias_meas", "Ibg", "Itg", "Ibias",
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            writer = CSVWriter(
                out_dir=temp_dir,
                file_base="dual_gate",
                wavelength_headers=wavelengths.tolist(),
                scalar_fields_order=scalar_fields,
            )
            writer.write_row({field: 0.0 for field in scalar_fields}, counts.tolist())
            writer.close()

            with (Path(temp_dir) / "dual_gate.csv").open(newline="", encoding="utf-8") as handle:
                header, row = list(csv.reader(handle))

        self.assertEqual(len(header), 1033)
        self.assertEqual(len(row), 1033)

    def test_writes_matching_header_and_data_widths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = CSVWriter(
                out_dir=temp_dir,
                file_base="spectrum",
                wavelength_headers=[1.0] * 512,
                scalar_fields_order=["Vbg_set"],
            )
            writer.write_row({"Vbg_set": 0.25}, list(range(512)))
            writer.close()

            with (Path(temp_dir) / "spectrum.csv").open(newline="", encoding="utf-8") as handle:
                header, row = list(csv.reader(handle))

        self.assertEqual(len(header), 513)
        self.assertEqual(len(row), 513)

    def test_rejects_mismatched_spectrum_width(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = CSVWriter(
                out_dir=temp_dir,
                file_base="spectrum",
                wavelength_headers=[1.0] * 4,
                scalar_fields_order=["Vbg_set"],
            )

            with self.assertRaisesRegex(ValueError, "expected 4, received 3"):
                writer.write_row({"Vbg_set": 0.25}, [1.0, 2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
