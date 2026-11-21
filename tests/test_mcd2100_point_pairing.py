import csv
import io
import pytest
from app.engine.mcd2100_worker import MCD2100Worker, MCD2100StabilizationError
from tests.test_mcd2100_workflow import _ContinuousFakeController, _ContinuousFakeOptical, _safe_snapshot


def test_point_pair_retains_actual_fields_and_ids(tmp_path):
    controller = _ContinuousFakeController([])
    def idle_snapshot():
        snap = _safe_snapshot(controller.target)
        snap.status.backend_details["h_state"] = "IDLE"
        return controller._handle("read", snap)
    controller.read_snapshot_async = idle_snapshot
    optical = _ContinuousFakeOptical()
    optical.acquire = lambda angle, label, stop_event: ([700., 701.], [1., 2.], angle)
    optical.move_to = lambda angle: setattr(optical, "position", angle)
    optical.get_position = lambda: optical.position
    worker = MCD2100Worker(controller, optical, 0, .05, [0, 90], tmp_path,
                           point_pairing=True, point_stable_s=0)
    class Stream(io.StringIO):
        def fileno(self):
            return stream_file.fileno()
    stream_file = (tmp_path / 'sync').open('w')
    try:
        stream = Stream()
        metadata = {'spectra_written': 0}
        worker._point_leg(leg=1, start=0, target=.05, writer=csv.writer(stream),
                          stream=stream, wavelengths=[700., 701.], metadata=metadata)
        rows = list(csv.reader(io.StringIO(stream.getvalue())))
        assert len(rows) == 4
        assert rows[0][16] == rows[1][16] != rows[2][16]
        assert [float(row[17]) for row in rows] == [0, 0, .05, .05]
        assert all(pair['status'] == 'accepted' for pair in metadata['point_pairs'])
    finally:
        stream_file.close()


def test_pairing_rejects_bad_parameters_before_commands(tmp_path):
    controller = _ContinuousFakeController([])
    with pytest.raises(ValueError):
        MCD2100Worker(controller, _ContinuousFakeOptical(), 0, .1, [0, 90], tmp_path,
                      point_pairing=True, point_step_t=0)
    assert not controller.events

def test_ramping_does_not_count_toward_stability(tmp_path):
    controller = _ContinuousFakeController([])
    optical = _ContinuousFakeOptical()
    optical.move_to = lambda angle: setattr(optical, 'position', angle)
    optical.get_position = lambda: optical.position
    optical.acquire = lambda angle, label, stop_event: ([700., 701.], [1., 2.], angle)
    samples = []
    def snapshots():
        snap = _safe_snapshot(controller.target)
        state = 'RAMPING' if len(samples) < 3 else 'IDLE'
        samples.append(state)
        snap.status.backend_details['h_state'] = state
        return controller._handle('read', snap)
    controller.read_snapshot_async = snapshots
    now = [0.0]
    def sleep(seconds): now[0] += seconds
    worker = MCD2100Worker(controller, optical, 0, .05, [0, 90], tmp_path,
                           point_pairing=True, point_stable_s=1, sleep=sleep, clock=lambda: now[0])
    worker._sleep_checked = sleep
    with (tmp_path / 'out').open('w', newline='') as stream:
        worker._point_leg(leg=1, start=0, target=.05, writer=csv.writer(stream),
                          stream=stream, wavelengths=[700., 701.], metadata={'spectra_written': 0})
    assert samples[:3] == ['RAMPING'] * 3
    assert now[0] >= 1.0

def test_full_point_run_writes_paired_rows(tmp_path):
    controller = _ContinuousFakeController([])
    def snapshots():
        snap = _safe_snapshot(controller.target)
        snap.status.backend_details['h_state'] = 'IDLE'
        return controller._handle('read', snap)
    controller.read_snapshot_async = snapshots
    optical = _ContinuousFakeOptical()
    optical.move_to = lambda angle: setattr(optical, 'position', angle)
    optical.get_position = lambda: optical.position
    optical.acquire = lambda angle, label, stop_event: ([700., 701.], [1., 2.], angle)
    worker = MCD2100Worker(controller, optical, 0, .05, [0, 90], tmp_path,
                           point_pairing=True, point_stable_s=0)
    result = worker.run()
    assert result['status'] == 'COMPLETED', result
    with open(result['csv_path'], newline='') as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4
    assert rows[0]['pair_id'] == rows[1]['pair_id']
    assert rows[0]['field_target_t'] == '0.0'


def test_two_detectors_record_each_polarization_before_advancing(tmp_path):
    from types import SimpleNamespace
    from app.engine.mcd_paired_spectra import MCDPairedSpectra
    controller = _ContinuousFakeController([])
    def snapshot():
        snap = _safe_snapshot(controller.target)
        snap.status.backend_details["h_state"] = "IDLE"
        return controller._handle("read", snap)
    controller.read_snapshot_async = snapshot
    optical = _ContinuousFakeOptical()
    optical.move_to = lambda angle: setattr(optical, "position", angle)
    optical.get_position = lambda: optical.position
    captures = []
    def capture(backend, recipes, stop, *, owner):
        assert owner == "mcd" and backend == "andor_ingaas"
        captures.append((controller.target, optical.position))
        return {"lightfield": {"wavelengths": [700, 701], "counts": [1, 2]},
                backend: {"wavelengths": [1100, 1101, 1102], "counts": [3, 4, 5]}}
    optical.station_pair = MCDPairedSpectra(SimpleNamespace(acquire_station_pair=capture),
                                             "andor_ingaas", {}, tmp_path, "dual")
    worker = MCD2100Worker(controller, optical, 0, .05, [0, 90], tmp_path,
                           point_pairing=True, point_stable_s=0)
    result = worker.run()
    assert result["status"] == "COMPLETED", result
    assert captures == [(0, 0), (0, 90), (.05, 0), (.05, 90)]
    with open(result["csv_path"], newline="") as f:
        ids = [row["point_id"] for row in csv.DictReader(f)]
    assert len(set(ids)) == 4
    for detector in ("PIXIS", "Andor_InGaAs"):
        with (tmp_path / f"dual_{detector}.csv").open(newline="") as f:
            assert [row["point_id"] for row in csv.DictReader(f)] == ids

def test_drift_rejects_pair_but_keeps_acquired_spectrum(tmp_path):
    controller = _ContinuousFakeController([])
    optical = _ContinuousFakeOptical()
    optical.move_to = lambda angle: setattr(optical, 'position', angle)
    optical.get_position = lambda: optical.position
    captured = [False]
    def acquire(angle, label, stop_event):
        captured[0] = True
        return [700., 701.], [9., 11.], angle
    optical.acquire = acquire
    def snapshots():
        snap = _safe_snapshot(controller.target + (.02 if captured[0] else 0))
        snap.setpoint_t = controller.target
        snap.status.backend_details['h_state'] = 'IDLE'
        return controller._handle('read', snap)
    controller.read_snapshot_async = snapshots
    worker = MCD2100Worker(controller, optical, 0, .05, [0, 90], tmp_path,
                           point_pairing=True, point_stable_s=0)
    result = worker.run()
    assert result['status'] == 'FAILED'
    with open(result['csv_path'], newline='') as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and float(rows[0]['B1_T']) == .02
    import json
    metadata = json.loads((tmp_path / 'mcd2100_continuous.meta.json').read_text())
    assert metadata['point_pairs'][0]['status'] == 'rejected'


def test_cancel_after_capture_preserves_partial_pair(tmp_path):
    controller = _ContinuousFakeController([])
    def snapshots():
        snap = _safe_snapshot(controller.target)
        snap.status.backend_details['h_state'] = 'IDLE'
        return controller._handle('read', snap)
    controller.read_snapshot_async = snapshots
    optical = _ContinuousFakeOptical()
    optical.move_to = lambda angle: setattr(optical, 'position', angle)
    optical.get_position = lambda: optical.position
    def acquire(angle, label, stop_event):
        stop_event.set()
        return [700., 701.], [9., 11.], angle
    optical.acquire = acquire
    worker = MCD2100Worker(controller, optical, 0, .05, [0, 90], tmp_path,
                           point_pairing=True, point_stable_s=0)
    result = worker.run()
    assert result['status'] == 'CANCELLED'
    with open(result['csv_path'], newline='') as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    import json
    metadata = json.loads((tmp_path / 'mcd2100_continuous.meta.json').read_text())
    assert metadata['point_pairs'][0]['status'] != 'accepted'
