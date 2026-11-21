import csv
import json

import pytest

from app.paired_spectrum_files import PairedSpectrumFiles, PairedSpectrumSaveError


def spectrum(axis):
    return {"wavelengths": axis, "counts": list(range(len(axis))), "axis_unit": "nm",
            "settings": {"frames": 3, "center_nm": 800, "exposure_ms": 250}}


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_pair_files_link_points_and_preserve_independent_calibrations(tmp_path):
    writer = PairedSpectrumFiles(tmp_path, "run", "andor_ingaas")
    records = {"lightfield": spectrum([700., 701.]), "andor_ingaas": spectrum([1100., 1101., 1102.])}
    first = writer.write_point("p1", records, scalars={"gate_V": 0})
    second = writer.write_point("p2", records, scalars={"gate_V": .5})
    assert first == second
    for backend, path in first.items():
        rows = read_csv(path)
        assert [row["point_id"] for row in rows] == ["p1", "p2"]
        assert {row["run_id"] for row in rows} == {writer.run_id}
        assert [row["gate_V"] for row in rows] == ["0", "0.5"]
        assert rows[0]["backend"] == backend
    assert "700.0000" in read_csv(first["lightfield"])[0]
    assert "1102.0000" in read_csv(first["andor_ingaas"])[0]

    records["andor_ingaas"] = spectrum([1200., 1201.])
    third = writer.write_point("p3", records, scalars={"gate_V": 1})
    assert third["lightfield"] == first["lightfield"]
    assert third["andor_ingaas"] != first["andor_ingaas"]
    assert len(read_csv(first["andor_ingaas"])) == 2
    assert PairedSpectrumFiles(tmp_path, "run", "andor_si").manifest_path != writer.manifest_path


def test_partial_and_write_errors_keep_good_data_and_mark_incomplete(tmp_path):
    writer = PairedSpectrumFiles(tmp_path, "run", "andor_si")
    records = {"andor_si": spectrum([700., 701.]),
               "lightfield": {"error": "Timed out", "recovery_required": True}}
    records["andor_si"]["readback"] = {"output_port": "side"}
    paths = writer.write_point(1, records)
    assert set(paths) == {"andor_si"}
    records["lightfield"] = {"wavelengths": [1], "counts": []}
    with pytest.raises(PairedSpectrumSaveError) as exc:
        writer.write_point(2, records)
    assert exc.value.paths == paths
    assert [row["point_id"] for row in read_csv(paths["andor_si"])] == ["1", "2"]
    rows = [json.loads(line) for line in writer.manifest_path.read_text().splitlines()]
    assert [row["status"] for row in rows] == ["writing", "incomplete"] * 2
    assert rows[1]["detectors"]["lightfield"]["error"] == "Timed out"
    assert rows[1]["detectors"]["lightfield"]["recovery_required"] is True
    assert rows[1]["detectors"]["andor_si"]["readback"]["output_port"] == "side"
    with pytest.raises(ValueError, match="unique"):
        writer.write_point(2, records)


def test_partial_repeat_averages_keep_both_files_with_incomplete_status(tmp_path):
    writer = PairedSpectrumFiles(tmp_path, "binned", "andor_si")
    records = {backend: spectrum([1, 2]) for backend in writer.backends}
    paths = writer.write_point(1, records, incomplete_reason="Stopped after two of three repeats")
    assert len(paths) == 2
    entry = json.loads(writer.manifest_path.read_text().splitlines()[-1])
    assert entry["status"] == "incomplete" and "two of three" in entry["reason"]


def test_each_detector_file_can_carry_its_own_recipe_in_the_name(tmp_path):
    (tmp_path / "pixis_650nm_pairs.jsonl").touch()
    writer = PairedSpectrumFiles(tmp_path, "pixis_650nm", "andor_ingaas", andor_stem="andor_1100nm")
    assert writer.manifest_path.name == "pixis_650nm_001_pairs.jsonl"
    both = PairedSpectrumFiles(tmp_path, "both", "andor_ingaas", pixis_stem="p", andor_stem="a")
    named = both.write_point("p1", {"lightfield": {"wavelengths": [1.0], "counts": [1.0]},
                                    "andor_ingaas": {"wavelengths": [1.0], "counts": [1.0]}})
    assert (both.manifest_path.name, named["lightfield"].name, named["andor_ingaas"].name) == (
        "both_pairs.jsonl", "p_PIXIS.csv", "a_Andor_InGaAs.csv")
    record = lambda value: {"wavelengths": [1.0, 2.0], "counts": [value, value], "settings": {}}
    paths = writer.write_point("p1", {"lightfield": record(1.0), "andor_ingaas": record(2.0)})
    assert paths["lightfield"].name == "pixis_650nm_001_PIXIS.csv"
    assert paths["andor_ingaas"].name == "andor_1100nm_001_Andor_InGaAs.csv"
