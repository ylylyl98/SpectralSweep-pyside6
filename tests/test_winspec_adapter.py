import json
import socket
import struct
import threading

import numpy as np
import pytest

from app.devices.winspec_adapter import WinSpecClient, WinSpecSetup


def exchange(reply, payload=b'', magic=b'WXRS', version=1, receipts=None):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    requests = []
    def serve():
        with listener, listener.accept()[0] as conn:
            header = b''
            while len(header) < 10:
                header += conn.recv(10-len(header))
            m, v, size = struct.unpack('<4sHI', header)
            body = b''
            while len(body) < size:
                body += conn.recv(size-len(body))
            requests.append((m, v, json.loads(body)))
            body = json.dumps(reply).encode()
            wire = struct.pack('<4sHII', magic, version, len(body), len(payload)) + body + payload
            for i in range(0, len(wire), 3):
                try:
                    conn.sendall(wire[i:i+3])
                except OSError:
                    break
            if receipts is not None:
                conn.settimeout(2)
                receipt=conn.recv(4)
                receipts.append(receipt)
                if receipt==b'WXAK':conn.sendall(b'WXCL')
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return WinSpecClient('127.0.0.1', port, timeout_s=2), requests, thread


@pytest.mark.parametrize('valid',[True,False])
def test_complete_valid_frame_receipt_precedes_temporary_file_cleanup(valid):
    receipts=[]
    payload=np.arange(512,dtype='<u2').tobytes() if valid else b'bad'
    client,_,thread=exchange(dict(ok=True,receipt_required=True,width=512,height=1,
                                  frame_count=1,winspec_datatype=3),payload,receipts=receipts)
    if valid:
        metadata,_=client.request('ACQUIRE_GUARDED')
        assert metadata.get('temporary_spe_cleanup')=='complete'
        assert 0 <= metadata['client_timing_s']['cleanup_wait'] <= metadata['client_timing_s']['request_total']
    else:
        with pytest.raises(RuntimeError):client.request('ACQUIRE_GUARDED')
    thread.join(3)
    assert receipts==([b'WXAK'] if valid else [b''])


def test_fragmented_wire_and_read_only_request():
    client, requests, thread = exchange({'ok': True, 'settings': {'detector_width': 512}})
    result, payload = client.request('GET_SETTINGS')
    thread.join(2)
    assert result['settings']['detector_width'] == 512 and payload == b''
    assert result['client_timing_s']['cleanup_wait'] == 0
    assert requests[0] == (b'WXRQ', 1, {'command': 'GET_SETTINGS', 'parameters': {}})


@pytest.mark.parametrize('reply,magic', [({'ok': True}, b'NOPE'), ({'ok': False, 'error': 'camera offline'}, b'WXRS')])
def test_invalid_or_failed_response_rejected(reply, magic):
    client, _, thread = exchange(reply, magic=magic)
    with pytest.raises(RuntimeError):
        client.request('GET_STATUS')
    thread.join(2)


class Camera:
    def __init__(self):
        self.calls = []
        self.settings = dict(detector_width=512, detector_height=1, output_width=512,
                             output_height=1, roi_enabled=False, roi_x_group=1,
                             exposure_ms=500., accumulations=1, sequential_frames=1,
                             timing_mode=1, running=False, actual_temperature_c=-100., temperature_locked=True)
    def request(self, command, parameters=None, **kwargs):
        self.calls.append((command, parameters))
        if command == 'SET_SETTINGS': self.settings.update(parameters)
        if command == 'ACQUIRE_GUARDED':
            return dict(ok=True, width=512, height=1, frame_count=1, winspec_datatype=3,
                        temperature_guard=dict(version=4, monitoring_mode='before_after', policy='cold_or_locked', passed=True, limit_c=-100., sample_count=2,
                                               minimum_c=-100., maximum_c=-100., max_gap_s=0.5),
                        settings=dict(self.settings)), np.arange(512, dtype='<u2').tobytes()
        return {'ok': True, 'temperature_guard_version': 4, 'settings': dict(self.settings)}, b''


class Optics:
    is_ready = True
    is_busy = False
    def __init__(self): self.calls = []; self.closed = False
    def set_center_wavelength_when_ready(self, nm, **kwargs): self.calls.append(nm)
    def get_saved_experiments(self): return ['experiment']
    def close(self): self.closed = True


def test_pixel_spectrum_routes_camera_and_preserves_optics_ownership(monkeypatch):
    camera, optics = Camera(), Optics()
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda lf, route, **kw: {'output_port': 'SideExit'})
    setup = WinSpecSetup(optics, client=camera)
    assert all(c[0] == 'GET_SETTINGS' for c in camera.calls)
    setup.configure_for_acquisition(center_nm=1050, exposure_ms=20, frames=2)
    x, y = setup.acquire()
    np.testing.assert_array_equal(x, np.arange(1, 513))
    np.testing.assert_array_equal(y, np.arange(512)/2)
    processing = setup.read_metadata_snapshot()['observed']['last_frame']['intensity_processing']
    assert processing['accumulations'] == 2
    assert processing['output'] == 'mean_counts_per_exposure'
    np.testing.assert_array_equal(setup.read_metadata_snapshot()['observed']['last_frame']['raw_accumulated_counts'], np.arange(512))
    assert optics.calls == [1050]
    assert setup.identity['axis_unit'] == 'pixel'
    setup.abort_acquisition()
    assert camera.calls[-1][0] == 'STOP'
    setup.close()
    assert not optics.closed


def test_wrong_detector_geometry_fails_before_writes():
    camera = Camera(); camera.settings['detector_height'] = 100
    with pytest.raises(RuntimeError, match='512'):
        WinSpecSetup(Optics(), client=camera)
    assert len(camera.calls) == 1


@pytest.mark.parametrize('n', [1, 3, 10])
def test_average_uses_each_frames_readback_once(monkeypatch, n):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    camera = Camera(); camera.settings['accumulations'] = n
    setup = WinSpecSetup(Optics(), client=camera)
    for _ in range(2):
        _, counts = setup.acquire()
        np.testing.assert_array_equal(counts, np.arange(512)/n)


@pytest.mark.parametrize('n', [None, 0, -1, True, 1.5, float('nan'), 2])
def test_invalid_or_changed_accumulations_reject_frame(monkeypatch, n):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    camera = Camera(); original = camera.request
    def request(command, *a, **kw):
        metadata, payload = original(command, *a, **kw)
        if command == 'ACQUIRE_GUARDED':
            metadata['settings']['accumulations'] = n
        return metadata, payload
    camera.request = request
    setup = WinSpecSetup(Optics(), client=camera)
    with pytest.raises(RuntimeError, match='accumulation readback'):
        setup.acquire()


def test_bad_payload_cannot_be_plotted():
    meta = dict(width=512, height=1, frame_count=1, winspec_datatype=3)
    with pytest.raises(RuntimeError, match='payload'):
        WinSpecSetup.decode_frame(meta, b'broken')


def test_no_side_port_fails_without_writing(monkeypatch):
    camera, optics = Camera(), Optics()
    setup = WinSpecSetup(optics, client=camera)
    def missing(lf, route, **kw):
        raise RuntimeError('No side output')
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', missing)
    with pytest.raises(RuntimeError, match='side'):
        setup.configure_for_acquisition(center_nm=1050, exposure_ms=20, frames=1)
    assert optics.calls == [] and not any(c[0] == 'SET_SETTINGS' for c in camera.calls)


def test_abort_during_preflight_never_launches_camera(monkeypatch):
    camera = Camera()
    setup = WinSpecSetup(Optics(), client=camera)
    def optics(_, route, **kw):
        setup._abort.set()
        return {'output_port': 'SideExit'}
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', optics)
    with pytest.raises(RuntimeError, match='stopped'):
        setup.acquire()
    assert not any(command.startswith('ACQUIRE') for command, _ in camera.calls)


def test_abort_before_queued_acquisition_is_not_cleared():
    camera = Camera()
    setup = WinSpecSetup(Optics(), client=camera)
    setup.abort_acquisition()
    with pytest.raises(RuntimeError, match='stopped'):
        setup.acquire()
    assert not any(command.startswith('ACQUIRE') for command, _ in camera.calls)


@pytest.mark.parametrize('value', [None, True, '-100', float('nan')])
def test_temperature_interlock_blocks_before_any_write(value):
    camera, optics = Camera(), Optics()
    setup = WinSpecSetup(optics, client=camera)
    camera.settings['actual_temperature_c'] = value
    with pytest.raises(RuntimeError, match='temperature interlock'):
        setup.configure_for_acquisition(center_nm=950, exposure_ms=10, frames=1)
    assert not optics.calls
    assert not any(cmd in ('SET_SETTINGS', 'ACQUIRE', 'ACQUIRE_GUARDED') for cmd, _ in camera.calls)


def test_direct_acquire_cannot_bypass_temperature_guard():
    camera = Camera()
    setup = WinSpecSetup(Optics(), client=camera)
    camera.settings['temperature_locked'] = False
    camera.settings['actual_temperature_c'] = -99.
    with pytest.raises(RuntimeError, match='temperature interlock'):
        setup.acquire()
    assert not any(cmd.startswith('ACQUIRE') for cmd, _ in camera.calls)


def test_old_bridge_blocks_without_launching(monkeypatch):
    camera = Camera()
    original = camera.request
    def old(*args, **kwargs):
        reply, payload = original(*args, **kwargs)
        reply.pop('temperature_guard_version', None)
        return reply, payload
    camera.request = old
    setup = WinSpecSetup(Optics(), client=camera)
    with pytest.raises(RuntimeError, match='bridge'):
        setup.acquire()
    assert not any(cmd.startswith('ACQUIRE') for cmd, _ in camera.calls)


def test_missing_guard_report_invalidates_frame_and_latches(monkeypatch):
    camera = Camera()
    original = camera.request
    def incomplete(*args, **kwargs):
        reply, payload = original(*args, **kwargs)
        reply.pop('temperature_guard', None)
        return reply, payload
    camera.request = incomplete
    setup = WinSpecSetup(Optics(), client=camera)
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    with pytest.raises(RuntimeError, match='temperature interlock'):
        setup.acquire()
    with pytest.raises(RuntimeError, match='stopped'):
        setup.acquire()


def test_locked_above_old_limit_can_configure(monkeypatch):
    monkeypatch.setattr("app.devices.winspec_adapter.ensure_output_route", lambda *a, **kw: {})
    camera=Camera();camera.settings['actual_temperature_c']=-99.5
    setup=WinSpecSetup(Optics(),client=camera)
    setup.configure_for_acquisition(center_nm=950,exposure_ms=10,frames=1)
    assert any(cmd=='SET_SETTINGS' for cmd, _ in camera.calls)


def test_idle_temperature_is_queried_at_most_every_ten_seconds(monkeypatch):
    import app.devices.winspec_adapter as module
    now=[0.];monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    camera=Camera();setup=WinSpecSetup(Optics(),client=camera)
    assert setup.get_temperature_snapshot() is not None
    now[0]=2.;assert setup.get_temperature_snapshot() is None
    now[0]=10.;assert setup.get_temperature_snapshot() is not None
    assert len([c for c in camera.calls if c[0]=='GET_STATUS'])==2
    setup._busy.set();now[0]=20.
    assert setup.get_temperature_snapshot() is None


def performance_camera():
    camera = Camera()
    original = camera.request
    def request(command, *args, **kwargs):
        reply, payload = original(command, *args, **kwargs)
        reply['acquisition_settings_version'] = 2
        return reply, payload
    camera.request = request
    return camera


@pytest.mark.parametrize('phase', ['before', 'during'])
@pytest.mark.parametrize('field,value', [('exposure_ms', 800.), ('accumulations', 4)])
def test_legacy_bridge_rejects_drift_from_configured_recipe(monkeypatch, phase, field, value):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    camera = Camera()
    setup = WinSpecSetup(Optics(), client=camera)
    setup.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    original = camera.request
    if phase == 'before':
        camera.settings[field] = value
    else:
        def request(command, *args, **kwargs):
            if command == 'ACQUIRE_GUARDED':
                camera.settings[field] = value
            return original(command, *args, **kwargs)
        camera.request = request
    camera.calls.clear()
    with pytest.raises(RuntimeError, match='exposure|accumulation'):
        setup.acquire()
    if phase == 'before':
        assert not any(cmd == 'ACQUIRE_GUARDED' for cmd, _ in camera.calls)
    assert not setup.read_metadata_snapshot()['observed']['last_frame']
    camera.request = original
    setup.configure_for_acquisition(center_nm=1050, exposure_ms=250, frames=3)
    _, counts = setup.acquire()
    np.testing.assert_array_equal(counts, np.arange(512) / 3)


@pytest.mark.parametrize('camera_factory', [Camera, performance_camera])
def test_exposure_tolerance_does_not_allow_cumulative_drift(monkeypatch, camera_factory):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    camera = camera_factory()
    setup = WinSpecSetup(Optics(), client=camera)
    setup.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    camera.settings['exposure_ms'] = 500.0004  # Within 1 ppm of the request.
    setup.acquire()
    camera.settings['exposure_ms'] = 500.0008  # Outside 1 ppm of the request.
    with pytest.raises(RuntimeError, match='exposure'):
        setup.acquire()


def test_failed_winspec_reconfiguration_requires_successful_apply(monkeypatch):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    camera = Camera()
    setup = WinSpecSetup(Optics(), client=camera)
    setup.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    original = camera.request
    def ignored_exposure(command, parameters=None, **kwargs):
        if command == 'SET_SETTINGS':
            parameters = {**parameters, 'exposure_ms': 500.}
        return original(command, parameters, **kwargs)
    camera.request = ignored_exposure
    with pytest.raises(RuntimeError, match='readback'):
        setup.configure_for_acquisition(center_nm=1050, exposure_ms=800, frames=4)
    camera.calls.clear()
    with pytest.raises(RuntimeError, match='apply|Apply|verified'):
        setup.acquire()
    assert not any(cmd == 'ACQUIRE_GUARDED' for cmd, _ in camera.calls)


def test_compact_bridge_avoids_full_settings_reads_for_repeated_frames(monkeypatch):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    camera = performance_camera()
    setup = WinSpecSetup(Optics(), client=camera)
    setup.configure_for_acquisition(center_nm=1050, exposure_ms=500, frames=2)
    camera.calls.clear()
    for _ in range(2):
        _, counts = setup.acquire()
        np.testing.assert_array_equal(counts, np.arange(512)/2)
    assert [cmd for cmd, _ in camera.calls] == ['ACQUIRE_GUARDED'] * 2
    request = camera.calls[0][1]
    assert request['settings_mode'] == 'managed'
    assert request['expected_settings']['exposure_ms'] == 500.
    assert request['expected_settings']['accumulations'] == 2
    frame = setup.read_metadata_snapshot()['observed']['last_frame']
    timing = frame['host_timing_s']
    assert {'settings', 'optics_before', 'acquire_request', 'processing', 'optics_after', 'total'} <= timing.keys()
    assert all(value >= 0 for value in timing.values())
    assert timing['total'] >= sum(value for key, value in timing.items() if key != 'total')


@pytest.mark.parametrize('changes', [
    {'detector_width': 256}, {'output_height': 2}, {'roi_enabled': True},
    {'actual_temperature_c': -90., 'temperature_locked': False},
    {'timing_mode': 3}, {'sequential_frames': 2},
])
def test_compact_configured_geometry_and_timing_are_validated_locally(monkeypatch, changes):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    camera = performance_camera()
    setup = WinSpecSetup(Optics(), client=camera)
    setup._settings.update(changes)
    with pytest.raises(RuntimeError):
        setup.acquire()
    assert not any(cmd.startswith('ACQUIRE') for cmd, _ in camera.calls)


@pytest.mark.parametrize('version', [None, 1])
def test_old_bridge_keeps_original_request_protocol(monkeypatch, version):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    camera = Camera()
    if version is not None:
        original = camera.request
        def request(*args, **kwargs):
            reply, payload = original(*args, **kwargs)
            reply['acquisition_settings_version'] = version
            return reply, payload
        camera.request = request
    setup = WinSpecSetup(Optics(), client=camera)
    camera.calls.clear()
    setup.acquire()
    assert camera.calls == [('GET_SETTINGS', None), ('ACQUIRE_GUARDED', None)]


@pytest.mark.parametrize('changes', [{'roi_enabled': True}, {'exposure_ms': 800.}, {'timing_mode': 3}])
def test_compact_frame_rejects_settings_changed_during_exposure(monkeypatch, changes):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route', lambda *a, **kw: {})
    camera = performance_camera()
    original = camera.request
    def request(command, *args, **kwargs):
        reply, payload = original(command, *args, **kwargs)
        if command == 'ACQUIRE_GUARDED':
            reply['settings'].update(changes)
        return reply, payload
    camera.request = request
    setup = WinSpecSetup(Optics(), client=camera)
    with pytest.raises(RuntimeError):
        setup.acquire()
