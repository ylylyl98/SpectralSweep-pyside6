"""WinSpec XP detector with a borrowed LightField spectrograph session.

No WinSpec SpectroObj is used. No LightField camera is captured. Connection
and disconnect are read-only with respect to detector and optics settings.
"""
from __future__ import annotations

import json
import hashlib
import logging
import math
import socket
import struct
import threading
import time
import uuid

import numpy as np
from tools.winspec.temperature_guard import validate_temperature

from .lightfield_optics import ensure_output_route, read_optics as _read_optics

log = logging.getLogger(__name__)


def read_optics(setup):
    return _read_optics(setup, include_capabilities=False)


class WinSpecClient:
    MAX_PAYLOAD = 256 * 1024 * 1024

    def __init__(self, host, port=5000, timeout_s=15):
        self.host, self.port = str(host).strip(), int(port)
        self.timeout_s = float(timeout_s)
        if not self.host or not 1 <= self.port <= 65535 or not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError('Invalid WinSpec host, port or timeout')

    @staticmethod
    def _read(sock, size, deadline):
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('WinSpec response timed out')
            sock.settimeout(remaining)
            chunk = sock.recv(min(size-len(result), 1024*1024))
            if not chunk:
                raise RuntimeError('WinSpec connection closed during response')
            result.extend(chunk)
        return bytes(result)

    def request(self, command, parameters=None, *, timeout_s=None):
        started = time.monotonic()
        started_unix = time.time()
        timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('WinSpec timeout must be finite and positive')
        body = json.dumps({'command': command, 'parameters': parameters or {}}, allow_nan=False).encode('utf-8')
        with socket.create_connection((self.host, self.port), timeout=min(5, timeout)) as sock:
            sock.settimeout(min(5, timeout))
            sock.sendall(struct.pack('<4sHI', b'WXRQ', 1, len(body)) + body)
            deadline = time.monotonic() + timeout
            magic, version, count, payload_size = struct.unpack('<4sHII', self._read(sock, 14, deadline))
            if magic != b'WXRS' or version != 1 or not 2 <= count <= 1024*1024 or payload_size > self.MAX_PAYLOAD:
                raise RuntimeError('Invalid WinSpec response header')
            metadata = json.loads(self._read(sock, count, deadline))
            if not isinstance(metadata, dict) or metadata.get('ok') is not True:
                raise RuntimeError('WinSpec: ' + str(metadata.get('error', 'request failed') if isinstance(metadata, dict) else 'invalid metadata'))
            if command not in ('ACQUIRE', 'ACQUIRE_GUARDED') and payload_size:
                raise RuntimeError('Unexpected WinSpec payload for status request')
            payload = self._read(sock, payload_size, deadline)
            cleanup_wait = 0.
            if metadata.get('receipt_required') is True:
                if command not in ('ACQUIRE', 'ACQUIRE_GUARDED'):
                    raise RuntimeError('Unexpected WinSpec transfer receipt request')
                # Acknowledge only a complete, decodable frame in host memory.
                # The XP server retains its document/SPE on any receive failure.
                WinSpecSetup.decode_frame(metadata, payload)
                sock.sendall(b'WXAK')
                cleanup_started = time.monotonic()
                # Avoid starting the next exposure while XP closes this document.
                # Cleanup transport errors must not discard the received spectrum.
                try:
                    receipt = self._read(sock, 4, time.monotonic()+5.)
                    metadata['temporary_spe_cleanup'] = 'complete' if receipt == b'WXCL' else 'recovery_required'
                except (OSError, RuntimeError, TimeoutError):
                    metadata['temporary_spe_cleanup'] = 'unconfirmed'
                cleanup_wait = time.monotonic() - cleanup_started
            finished = time.monotonic()
            finished_unix = time.time()
            metadata['client_timing_s'] = {'request_total': finished - started,
                                           'cleanup_wait': cleanup_wait,
                                           'request_started_unix': started_unix,
                                           'request_finished_unix': finished_unix,
                                           'request_elapsed_wall_s': finished_unix-started_unix}
            return metadata, payload


class WinSpecSetup:
    def __init__(self, lightfield, *, host='192.168.170.128', port=5000, client=None, pixel_pitch_um=50.0, output_route='side', acquisition_backend='winspec', start_acceleration=False):
        if acquisition_backend not in {'winspec', 'pvcam'}:
            raise ValueError('Unknown InGaAs acquisition backend')
        self.acquisition_backend = acquisition_backend
        if type(start_acceleration) is not bool or (start_acceleration and acquisition_backend!='winspec'):
            raise ValueError('Start acceleration is available only for the WinSpec backend')
        self.start_acceleration=start_acceleration
        self._start_session=uuid.uuid4().hex
        if output_route not in {'front', 'side', 'fixed_front', 'fixed_side'}:
            raise RuntimeError('WinSpec detector is disabled in this optical setup profile')
        self.lightfield = lightfield
        self.output_route = output_route
        self.client = client or WinSpecClient(host, port)
        self._closed = False
        self._temperature_checked_monotonic = None
        self._busy = threading.Event()
        self._abort = threading.Event()
        self._settings = self._read_settings()
        self._validate_geometry(self._settings)
        self._expected_settings = dict(self._settings)
        self._acquisition_prepared = True
        self._identity = {'backend': 'winspec_ingaas', 'acquisition_backend': acquisition_backend, 'camera_role': 'ingaas',
                          'start_acceleration_requested': start_acceleration,
                          'camera_model': 'OMA V 512 (provisional)', 'axis_unit': 'pixel',
                          'calibration_status': 'uncalibrated', 'pixel_pitch_um': float(pixel_pitch_um),
                          'pixel_pitch_verified': False, 'detector_width': 512,
                          'detector_height': 1, 'host': host, 'port': int(port),
                          'spectrograph_control': 'lightfield', 'required_output_port': output_route}
        self._last_frame = {}
        self.last_calibration_context = None

    def _calibration_context(self, optics):
        from utils.config import cfg
        try:
            if any(key in optics.get('readback_errors', {}) for key in ('grating','wavelength_nm','output_port')):
                return None
            devices = [{'type': str(d.Type), 'model': str(d.Model), 'serial': str(d.SerialNumber)}
                       for d in self.lightfield.experiment.ExperimentDevices if 'spectrom' in str(d.Type).lower()]
            if not devices or any(not d['serial'] or d['serial'] == 'None' for d in devices): return None
            return {'profile': cfg.lf6.optical_profile, 'grating': optics['grating'],
                    'center_nm': float(optics['wavelength_nm']),
                    'output_port': optics.get('output_port') or self.output_route,
                    'spectrometer': devices,
                    'detector': {'host': self._identity['host'], 'port': self._identity['port'],
                                 'model': self._identity['camera_model'], 'serial_verified': False},
                    'geometry': [512,1]}
        except Exception:
            return None

    @property
    def identity(self): return dict(self._identity)

    @property
    def is_ready(self): return not self._closed and bool(self.lightfield.is_ready)

    @property
    def is_busy(self): return self._busy.is_set() or bool(self.lightfield.is_busy)

    @property
    def readiness_snapshot(self): return {'ready': self.is_ready, 'busy': self.is_busy}

    def _read_settings(self):
        reply, _ = self.client.request('GET_SETTINGS')
        self._check_backend(reply)
        self._temperature_guard_version = reply.get('temperature_guard_version')
        self._acquisition_settings_version = reply.get('acquisition_settings_version')
        self._start_acceleration_version=reply.get('start_acceleration_version')
        if self.start_acceleration and (self._start_acceleration_version!=1 or self._acquisition_settings_version!=2):
            raise RuntimeError('WinSpec Start acceleration: upgrade the XP bridge before enabling this mode')
        if reply.get('camera_busy'):
            raise RuntimeError('WinSpec is busy; finish its acquisition first')
        settings = reply.get('settings')
        if not isinstance(settings, dict):
            raise RuntimeError('WinSpec did not return settings')
        return settings

    def _check_backend(self, reply):
        actual = reply.get('acquisition_backend', 'winspec')
        if actual != self.acquisition_backend:
            raise RuntimeError(f'InGaAs acquisition backend mismatch: selected {self.acquisition_backend}, service {actual}')
        if actual == 'pvcam' and (reply.get('acquisition_settings_version') != 2 or reply.get('temperature_guard_version') != 4):
            raise RuntimeError('PVCAM backend requires the guarded managed acquisition service')

    def _check_temperature(self, settings):
        try:
            validate_temperature(settings)
            if self._temperature_guard_version != 4:
                raise RuntimeError('WinSpec temperature interlock: upgrade the XP bridge for guarded acquisition')
        except Exception:
            self._abort.set()
            raise

    def get_temperature_snapshot(self):
        if self._busy.is_set():
            return None  # During exposure the XP guard owns temperature polling.
        now = time.monotonic()
        if self._temperature_checked_monotonic is not None and now - self._temperature_checked_monotonic < 10.:
            return None
        self._temperature_checked_monotonic = now
        reply, _ = self.client.request('GET_STATUS')
        self._check_backend(reply)
        if reply.get('camera_busy'):
            return None
        settings = reply.get('settings', {})
        value = settings.get('actual_temperature_c')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RuntimeError('WinSpec temperature readback unavailable')
        return {'temperature_c': value,
                'temperature_status': 'Locked' if settings.get('temperature_locked') is True else 'Not locked',
                'temperature_limit_c': -100., 'temperature_policy': 'cold_or_locked',
                'temperature_setpoint_c': settings.get('temperature_setpoint_c'),
                'checked_at_unix': time.time()}

    @staticmethod
    def _validate_geometry(settings):
        if (settings.get('detector_width'), settings.get('detector_height')) != (512, 1):
            raise RuntimeError('Expected the 512 x 1 WinSpec InGaAs detector')
        if settings.get('roi_enabled') or (settings.get('output_width'), settings.get('output_height')) != (512, 1):
            raise RuntimeError('Select full 512 x 1 detector without ROI/binning in WinSpec')

    def get_saved_experiments(self): return []

    def close(self):
        if not self._closed and self.start_acceleration:
            self.client.request('RELEASE_START_ACCELERATION',{'session':self._start_session},timeout_s=25.)
        self._closed = True  # Borrowed LightField and XP service stay alive; no cooling writes.

    def invalidate_wavelengths(self): pass

    def calibration_wavelengths(self, force=False):
        # Legacy method name; identity.axis_unit explicitly marks these as pixels.
        return np.arange(1, 513, dtype=float)

    get_wavelength_calibration = calibration_wavelengths

    def set_center_wavelength_when_ready(self, center_nm, **kwargs):
        self.lightfield.set_center_wavelength_when_ready(
            float(center_nm), update_acquisition_recipe=False, **kwargs)

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames, timeout_s=15.0):
        self._acquisition_prepared = False
        timeout_s = float(timeout_s)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('WinSpec optics timeout must be finite and positive')
        deadline = time.monotonic() + timeout_s

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError('WinSpec optical preparation timed out; acquisition blocked')
            return value

        if not self.is_ready or self.is_busy:
            raise RuntimeError('WinSpec/LightField is not ready or is busy')
        if not math.isfinite(float(center_nm)) or float(center_nm) <= 0 or not math.isfinite(float(exposure_ms)) or float(exposure_ms) <= 0 or int(frames) != frames or int(frames) < 1:
            raise ValueError('Center, exposure and accumulations must be positive')
        current = self._read_settings()
        self._validate_geometry(current)
        self._check_temperature(current)
        if current.get('running') or current.get('winspec_reported_running'):
            raise RuntimeError('Stop the current WinSpec acquisition before applying settings')
        if current.get('timing_mode') != 1:
            raise RuntimeError('Select Free Run timing in WinSpec for Spectrum acquisition')
        optics = ensure_output_route(self.lightfield, self.output_route, timeout_s=remaining())
        self.set_center_wavelength_when_ready(center_nm, timeout_s=remaining())
        request = {'exposure_ms': float(exposure_ms), 'accumulations': int(frames), 'sequential_frames': 1}
        reply, _ = self.client.request('SET_SETTINGS', request, timeout_s=remaining())
        self._check_backend(reply)
        self._settings = reply.get('settings', {})
        for key, value in request.items():
            if key not in self._settings or not math.isclose(float(self._settings[key]), value, rel_tol=1e-6, abs_tol=1e-6):
                raise RuntimeError(f'WinSpec {key} readback differs from request')
        remaining()
        self._expected_settings = {**self._settings, **request}
        self._acquisition_prepared = True
        self._abort.clear()  # Only a successfully verified Apply arms the next run.
        return {'center_nm': float(center_nm), 'exposure_ms': self._settings['exposure_ms'],
                'frames': self._settings['accumulations'], 'axis_unit': 'pixel',
                'calibration_status': 'uncalibrated', 'winspec': dict(self._settings), 'optics': optics}

    def _verify_acquisition_recipe(self, actual):
        """Compare observations to Apply/connection, never to the previous frame."""
        for key in ('exposure_ms', 'accumulations', 'sequential_frames', 'timing_mode'):
            value = actual.get(key)
            expected = self._expected_settings.get(key)
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and isinstance(expected, (int, float))
                     and not isinstance(expected, bool) and math.isfinite(expected))
            matches = valid and (math.isclose(value, expected, rel_tol=1e-6, abs_tol=1e-6)
                                 if key == 'exposure_ms' else value == expected)
            if not matches:
                label = 'accumulation readback' if key == 'accumulations' else key
                raise RuntimeError(
                    f'WinSpec {label} mismatch: expected={expected}, readback={value}; '
                    'acquisition rejected; apply settings again')

    @staticmethod
    def decode_frame(metadata, payload):
        if metadata.get('acquisition_backend') == 'pvcam':
            n = metadata.get('settings', {}).get('accumulations')
            if (isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 64
                    or metadata.get('raw_frame_count') != n
                    or metadata.get('accumulation_method') != 'software_sum_of_raw_frames'
                    or metadata.get('winspec_datatype') != 5
                    or metadata.get('settings_scope') != 'native_setup_plus_raw'):
                raise RuntimeError('Incompatible PVCAM summed frame metadata')
            if hashlib.sha256(payload).hexdigest() != metadata.get('payload_sha256'):
                raise RuntimeError('PVCAM payload checksum mismatch')
        dtypes = {0: '<f4', 1: '<i4', 2: '<i2', 3: '<u2', 5: '<f8', 6: 'u1'}
        try:
            width, height, frames = (int(metadata[key]) for key in ('width', 'height', 'frame_count'))
            dtype = np.dtype(dtypes[metadata['winspec_datatype']])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError('Unsupported WinSpec frame metadata') from exc
        if (width, height, frames) != (512, 1, 1):
            raise RuntimeError('Expected one 512 x 1 WinSpec frame')
        if len(payload) != width*height*frames*dtype.itemsize:
            raise RuntimeError('WinSpec payload length does not match frame geometry')
        result = np.frombuffer(payload, dtype=dtype).astype(float)
        if not np.all(np.isfinite(result)):
            raise RuntimeError('WinSpec frame contains non-finite values')
        return result

    def acquire(self):
        if not self.is_ready or self.is_busy:
            raise RuntimeError('WinSpec/LightField is not ready or is busy')
        if not self._acquisition_prepared:
            raise RuntimeError('WinSpec acquisition setup was not verified; apply settings again')
        if self._abort.is_set():
            raise RuntimeError('WinSpec acquisition stopped; apply settings to start a new run')
        self._busy.set()
        started = time.monotonic()
        timings = {}
        try:
            phase_started = time.monotonic()
            compact = self._acquisition_settings_version == 2
            # Apply/connection readbacks define the requested configuration.
            # The bridge checks the acquired SPE's exposure/count/geometry.
            current = dict(self._settings) if compact else self._read_settings()
            self._validate_geometry(current)
            self._check_temperature(current)
            if current.get('timing_mode') != 1 or current.get('sequential_frames') != 1:
                raise RuntimeError('WinSpec requires Free Run and one sequential frame')
            self._verify_acquisition_recipe(current)
            timings['settings'] = time.monotonic() - phase_started
            phase_started = time.monotonic()
            optics = ensure_output_route(self.lightfield, self.output_route, apply=False)
            before = self._calibration_context(optics)
            timings['optics_before'] = time.monotonic() - phase_started
            timeout = max(30., (float(current['exposure_ms'])/1000 + float(current.get('readout_time_s', 0))) * int(current['accumulations'])*1.5+30.)
            if self._abort.is_set():
                raise RuntimeError('WinSpec acquisition stopped')
            phase_started = time.monotonic()
            if compact:
                keys = ('exposure_ms', 'accumulations', 'sequential_frames', 'timing_mode',
                        'detector_width', 'detector_height', 'output_width', 'output_height',
                        'roi_enabled', 'adc_rate', 'controller_gain', 'readout_time_s', 'native_speed_index')
                expected = {key: self._expected_settings[key] for key in keys if key in self._expected_settings}
                parameters={'settings_mode':'managed','expected_settings':expected}
                if self._start_acceleration_version==1:
                    parameters['start_acceleration']={'enabled':self.start_acceleration,'session':self._start_session}
                metadata, payload = self.client.request('ACQUIRE_GUARDED',parameters,timeout_s=timeout+25. if self.start_acceleration else timeout)
            else:
                metadata, payload = self.client.request('ACQUIRE_GUARDED', timeout_s=timeout)
            timings['acquire_request'] = time.monotonic() - phase_started
            self._check_backend(metadata)
            if self.start_acceleration:
                acceleration=metadata.get('start_acceleration',{})
                if (acceleration.get('version')!=1 or acceleration.get('requested') is not True
                        or acceleration.get('session')!=self._start_session
                        or acceleration.get('mode') not in ('baseline','optimized','fallback')
                        or acceleration.get('optimized') is not (acceleration.get('mode')=='optimized')):
                    raise RuntimeError('WinSpec Start acceleration report is missing or inconsistent')
            phase_started = time.monotonic()
            if self._abort.is_set():
                raise RuntimeError('WinSpec acquisition stopped')
            guard = metadata.get('temperature_guard', {})
            if (guard.get('version') != 4 or guard.get('monitoring_mode') != 'before_after' or guard.get('passed') is not True
                    or guard.get('policy') != 'cold_or_locked' or guard.get('limit_c') != -100. or guard.get('sample_count', 0) < 2
                    or not isinstance(guard.get('max_gap_s'), (int, float))
                    or not math.isfinite(guard['max_gap_s']) or not 0 <= guard['max_gap_s'] <= 3.):
                raise RuntimeError('WinSpec temperature interlock: missing or invalid exposure monitoring report')
            validate_temperature({'actual_temperature_c': guard.get('maximum_c'), 'temperature_locked': True})
            self._check_temperature(metadata.get('settings', {}))
            returned = metadata.get('settings', {})
            self._validate_geometry(returned)
            self._verify_acquisition_recipe(returned)
            counts = self.normalize_frame(metadata, payload, self._expected_settings['accumulations'])
            if (metadata.get('temporary_spe_cleanup') not in (None,'complete')
                    or self.start_acceleration and metadata.get('temporary_spe_cleanup') != 'complete'):
                self._last_frame=metadata  # Keep received raw counts for recovery, but stop publishing/continuing.
                raise RuntimeError('WinSpec data received, but document cleanup is unconfirmed; recover the retained frame')
            timings['processing'] = time.monotonic() - phase_started
            phase_started = time.monotonic()
            after = self._calibration_context(read_optics(self.lightfield))
            self.last_calibration_context = before if before == after else None
            timings['optics_after'] = time.monotonic() - phase_started
            timings['total'] = time.monotonic() - started
            metadata['host_timing_s'] = timings
            log.info('WinSpec acquisition timing (s): %s', timings)
            self._last_frame = metadata
            self._settings = metadata.get('settings', current)
            return self.calibration_wavelengths(), counts
        except Exception:
            self._abort.set()  # Failed frames never enter the plot/save/sweep pipeline.
            self.last_calibration_context = None
            raise
        finally:
            self._busy.clear()

    @staticmethod
    def normalize_frame(metadata, payload, expected_accumulations):
        """Shared production/validation normalization; mutate provenance once."""
        counts = WinSpecSetup.decode_frame(metadata, payload)
        n = metadata.get('settings', {}).get('accumulations')
        if (isinstance(n, bool) or not isinstance(n, (int, float))
                or not math.isfinite(n) or n < 1 or int(n) != n
                or n != expected_accumulations):
            raise RuntimeError('WinSpec accumulation readback missing, invalid or changed during acquisition')
        metadata['raw_accumulated_counts'] = counts.tolist()
        metadata['intensity_processing'] = {
            'input': 'summed_counts', 'output': 'mean_counts_per_exposure',
            'accumulations': int(n), 'divisor': int(n),
            'exposure_ms': metadata.get('settings', {}).get('exposure_ms'),
            'method': 'sum_divided_by_accumulations', 'version': 1}
        return counts / float(n)

    def abort_acquisition(self):
        self._abort.set()
        deadline = time.monotonic() + 5
        while True:
            reply, _ = self.client.request('STOP', {'reason': 'SpectralSweep user stop'},
                                           timeout_s=max(0.1, deadline-time.monotonic()))
            if reply.get('stop_requested'):
                return True
            if not self._busy.is_set():
                return False
            if time.monotonic() >= deadline:
                raise RuntimeError('WinSpec stop was not acknowledged; check XP WinSpec')
            # STOP can reach XP before a just-sent ACQUIRE becomes active.
            time.sleep(0.1)

    def read_metadata_snapshot(self):
        return {'identity': self.identity, 'observed': {'winspec': dict(self._settings),
                'last_frame': dict(self._last_frame)}, 'calibration': {'axis_unit': 'pixel', 'status': 'uncalibrated'}}

    def acquire_2d(self):
        raise RuntimeError('This InGaAs detector is one-dimensional; use Acquire 1D')
