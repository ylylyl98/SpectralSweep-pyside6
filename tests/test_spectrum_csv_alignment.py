from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.devices.spectrum_alignment import align_wavelengths_to_image, align_wavelengths_to_intensities
from app.engine.csv_writer import CSVWriter
from utils.dual_gate_preview import load_last_dual_gate_acquisition


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

    def test_aligns_full_sensor_image_and_trims_trailing_calibration(self) -> None:
        image = np.arange(3 * 1024, dtype=float).reshape(3, 1024)
        wavelengths, aligned = align_wavelengths_to_image(np.arange(1030, dtype=float), image)

        self.assertEqual(wavelengths.size, 1024)
        np.testing.assert_array_equal(aligned, image)

    def test_transposes_full_sensor_image_to_y_by_wavelength(self) -> None:
        image = np.arange(3 * 1024, dtype=float).reshape(1024, 3)
        wavelengths, aligned = align_wavelengths_to_image(np.arange(1030, dtype=float), image)

        self.assertEqual(wavelengths.size, 1024)
        self.assertEqual(aligned.shape, (3, 1024))
        np.testing.assert_array_equal(aligned, image.T)

    def test_presets_failure_shape_aligns_as_256_by_1024(self) -> None:
        image = np.arange(256 * 1024, dtype=float).reshape(256, 1024)
        wavelengths, aligned = align_wavelengths_to_image(np.arange(1024, dtype=float), image)

        self.assertEqual(wavelengths.size, 1024)
        self.assertEqual(aligned.shape, (256, 1024))


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

    def test_writes_and_reconstructs_full_sensor_sweep_points(self) -> None:
        first = np.arange(12, dtype=float).reshape(3, 4)
        second = first + 100.0

        with tempfile.TemporaryDirectory() as temp_dir:
            writer = CSVWriter(
                out_dir=temp_dir,
                file_base="full_sensor",
                wavelength_headers=[700.0, 700.1, 700.2, 700.3],
                scalar_fields_order=["Vbg_set", "Vtg_set"],
            )
            writer.write_matrix({"Vbg_set": -1.0, "Vtg_set": 0.5}, first, point_index=0)
            writer.write_matrix({"Vbg_set": -1.0, "Vtg_set": 1.0}, second, point_index=1)
            writer.close()

            with (Path(temp_dir) / "full_sensor.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))

        self.assertEqual(rows[0][:4], ["point_index", "y_pixel", "Vbg_set", "Vtg_set"])
        self.assertEqual(len(rows), 7)
        reconstructed_first = np.asarray([[float(v) for v in row[4:]] for row in rows[1:4]])
        reconstructed_second = np.asarray([[float(v) for v in row[4:]] for row in rows[4:7]])
        np.testing.assert_array_equal(reconstructed_first, first)
        np.testing.assert_array_equal(reconstructed_second, second)

    def test_viewer_loads_last_1d_acquisition_from_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = CSVWriter(
                out_dir=temp_dir,
                file_base="spectrum",
                wavelength_headers=[700.0, 701.0, 702.0],
                scalar_fields_order=["Vbg_set"],
            )
            writer.write_row({"Vbg_set": 0.0}, [1.0, 2.0, 3.0])
            writer.write_row({"Vbg_set": 1.0}, [4.0, 5.0, 6.0])
            result = load_last_dual_gate_acquisition(writer.path)
            writer.close()

        self.assertEqual(result["mode"], "spectrum")
        np.testing.assert_array_equal(result["data"], [4.0, 5.0, 6.0])

    def test_viewer_loads_last_full_sensor_point_from_csv(self) -> None:
        first = np.arange(12, dtype=float).reshape(3, 4)
        second = first + 100.0
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = CSVWriter(
                out_dir=temp_dir,
                file_base="full_sensor",
                wavelength_headers=[700.0, 701.0, 702.0, 703.0],
                scalar_fields_order=["Vbg_set"],
            )
            writer.write_matrix({"Vbg_set": 0.0}, first, point_index=0)
            writer.write_matrix({"Vbg_set": 1.0}, second, point_index=1)
            result = load_last_dual_gate_acquisition(writer.path)
            writer.close()

        self.assertEqual(result["mode"], "full_sensor")
        self.assertEqual(result["point_index"], 1)
        np.testing.assert_array_equal(result["data"], second)


if __name__ == "__main__":
    unittest.main()
