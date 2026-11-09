import csv
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from ui.mcd_panel import _ContinuousMCDWorker


class Magnet:
    def __init__(self):
        self.field = 0.0
        self.offsets = []
        self.moves = []

    def move_to_field(self, target, **kwargs):
        self.field = target
        self.moves.append(target)

    def pause(self):
        pass

    def read_snapshot(self):
        offset = self.offsets.pop(0) if self.offsets else 0
        return SimpleNamespace(field_t=self.field + offset, sweep_state="paused",
                               status=SimpleNamespace(sweep_active=False))


def setup_worker():
    magnet = Magnet()
    worker = _ContinuousMCDWorker({}, SimpleNamespace(adapter=magnet), None, None, None)
    worker._total_legs = 1
    worker._emit_progress = lambda _: None
    worker._validate_magnet_snapshot = lambda s: s.field_t
    worker._sleep_stop_aware = lambda _: None
    worker._acquire_spectrum = lambda _: (np.array([1., 2.]), np.array([3., 4.]))
    rotator = SimpleNamespace(move_to=Mock(), get_position=Mock(side_effect=[0., 90.] * 20))
    params = dict(start_t=0., move_timeout_s=10, field_poll_s=.01, rotation_settle_s=0,
                  point_step_t=.05, point_tolerance_t=.01, point_hold_s=0,
                  angle_a_deg=0., angle_b_deg=90., vtg_v=0., vbg_v=0., vbias_v=0.)
    return worker, magnet, rotator, params


def run(worker, magnet, rotator, params, tmp_path):
    path = tmp_path / "raw.csv"
    worker._csv_path = path
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        result = worker._run_point_pairs(params, [.1], 1, magnet, rotator, None,
                                         np.array([1., 2.]), writer, handle, 0., 0.)
    return result, list(csv.reader(path.open()))


def test_points_paused_and_paired(tmp_path):
    worker, magnet, rotator, params = setup_worker()
    result, rows = run(worker, magnet, rotator, params, tmp_path)
    assert result == 3
    assert magnet.moves == [0., .05, .1]
    assert (tmp_path / "raw.pairs.jsonl").read_text().count("accepted") == 3
    assert [row[18] for row in rows] == ["A", "B"] * 3
    assert [float(rows[i][16]) for i in (0, 2, 4)] == [0., .05, .1]
    assert [rows[i][17] for i in (0, 2, 4)] == ["1", "2", "3"]


def test_drift_keeps_acquired_raw_row_and_stops(tmp_path):
    worker, magnet, rotator, params = setup_worker()
    magnet.offsets = [0, .011]
    with pytest.raises(RuntimeError, match="raw spectra retained"):
        run(worker, magnet, rotator, params, tmp_path)
    rows = list(csv.reader((tmp_path / "raw.csv").open()))
    assert '"status": "rejected"' in (tmp_path / "raw.pairs.jsonl").read_text()
    assert len(rows) == 1
    assert float(rows[0][6]) == pytest.approx(.011)
    assert magnet.moves == [0.]


def test_round_trip_repeats_turning_point_as_distinct_pairs(tmp_path):
    worker, magnet, rotator, params = setup_worker()
    path = tmp_path / "raw.csv"
    worker._csv_path = path
    with path.open("w", newline="") as handle:
        result = worker._run_point_pairs(params, [.1, 0.], 1, magnet, rotator, None,
                                         np.array([1., 2.]), csv.writer(handle), handle, 0., 0.)
    rows = list(csv.reader(path.open()))
    assert result == 6
    assert magnet.moves == [0., .05, .1, .1, .05, 0.]
    assert rows[4][16:19] == ["0.1", "3", "A"]
    assert rows[6][16:19] == ["0.1", "4", "A"]
    assert rows[4][3] == "increasing" and rows[6][3] == "decreasing"


def test_unconfirmed_pause_prevents_exposure(tmp_path):
    worker, magnet, rotator, params = setup_worker()
    snapshot = magnet.read_snapshot
    def active():
        value = snapshot()
        value.sweep_state = "sweeping up"
        value.status.sweep_active = True
        return value
    magnet.read_snapshot = active
    worker._acquire_spectrum = Mock()
    with pytest.raises(RuntimeError, match="paused holding state"):
        run(worker, magnet, rotator, params, tmp_path)
    worker._acquire_spectrum.assert_not_called()


def test_stop_after_first_spectrum_prevents_next(tmp_path):
    worker, magnet, rotator, params = setup_worker()
    def capture(_):
        worker._stop.set()
        return np.array([1., 2.]), np.array([3., 4.])
    worker._acquire_spectrum = Mock(side_effect=capture)
    with pytest.raises(RuntimeError, match="stop requested"):
        run(worker, magnet, rotator, params, tmp_path)
    assert worker._acquire_spectrum.call_count == 1
    assert len(list(csv.reader((tmp_path / "raw.csv").open()))) == 1
    assert '"status": "incomplete"' in (tmp_path / "raw.pairs.jsonl").read_text()


def test_journal_registered_as_metadata(tmp_path):
    worker, magnet, rotator, params = setup_worker()
    worker._metadata_run = Mock()
    run(worker, magnet, rotator, params, tmp_path)
    worker._metadata_run.register_file.assert_called_once()
    args, kwargs = worker._metadata_run.register_file.call_args
    assert args[0] == tmp_path / "raw.pairs.jsonl"
    assert kwargs["role"] == "metadata"
    assert kwargs["kind"] == "mcd_point_pair_journal"


def test_nonfinite_angle_prevents_exposure(tmp_path):
    worker, magnet, rotator, params = setup_worker()
    rotator.get_position = Mock(return_value=float("nan"))
    worker._acquire_spectrum = Mock()
    with pytest.raises(RuntimeError, match="Rotation readback"):
        run(worker, magnet, rotator, params, tmp_path)
    worker._acquire_spectrum.assert_not_called()
