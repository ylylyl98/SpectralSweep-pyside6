"""Manual local InGaAs acquisition using the existing LightField optics."""
import copy
import hashlib
import math
import threading
import time
import uuid

import numpy as np

from app.devices.lightfield_optics import ensure_output_route, read_optics
from app.devices.winusb_persistent import PersistentCaptureBridge
from app.devices.winusb_recipe import recipe, saved_members

LOCAL_TEMPERATURE_NOTICE = 'Local WinUSB experimental capture: temperature unverified; cooling is not controlled.'


class LocalWinUSBSetup:
    def __init__(self, lightfield, *, output_route='side', pixel_pitch_um=50.0, bridge=None):
        if output_route not in {'side', 'front', 'fixed_side', 'fixed_front'}:
            raise ValueError('Choose a valid InGaAs output route')
        self.lightfield = lightfield
        self.output_route = output_route
        self.bridge = bridge or PersistentCaptureBridge()
        self.bridge.preflight()  # Read-only PnP and file checks; no camera initialization.
        self._closed = False
        self._busy = threading.Event()
        self._abort = threading.Event()
        self._prepared = False
        self._requested_center_nm = None
        self._last_frame = {}
        self._settings = {'exposure_ms': 100.0, 'accumulations': 1, 'sequential_frames': 1,
                          'timing_mode': 1, 'detector_width': 512, 'detector_height': 1,
                          'output_width': 512, 'output_height': 1, 'roi_enabled': False,
                          'verification': 'fixed_reviewed_native_recipe'}
        self._identity = {'backend': 'winspec_ingaas', 'acquisition_backend': 'winusb_local',
                          'camera_role': 'ingaas', 'camera_model': 'ST133 / OMA V 512 (experimental)',
                          'axis_unit': 'pixel', 'calibration_status': 'uncalibrated',
                          'detector_width': 512, 'detector_height': 1, 'pixel_pitch_um': float(pixel_pitch_um),
                          'pixel_pitch_verified': False, 'spectrograph_control': 'lightfield',
                          'required_output_port': output_route, 'supports_scans': False,
                          'fixed_exposure_ms': 100.0, 'fixed_accumulations': 1,
                          'connection_warnings': [LOCAL_TEMPERATURE_NOTICE,
                              '100 ms single frames only; wavelength calibration and automated scans are unavailable.']}
        self.last_calibration_context = None
        self._recipe_capabilities = getattr(self.bridge, 'recipe_capabilities', None)
        if self._recipe_capabilities:
            self._identity.pop('fixed_exposure_ms', None)
            self._identity.pop('fixed_accumulations', None)
            self._identity.update(self._recipe_capabilities)
            self._identity['connection_warnings'] = [LOCAL_TEMPERATURE_NOTICE,
                'Manual software accumulation; pixel axis. Continuous acquisition and scans are unavailable.']

    @property
    def identity(self): return copy.deepcopy(self._identity)
    @property
    def is_ready(self): return not self._closed and bool(self.lightfield.is_ready) and getattr(self.bridge, 'is_ready', True)
    @property
    def is_busy(self): return self._busy.is_set() or self.bridge.is_busy or bool(self.lightfield.is_busy)
    @property
    def readiness_snapshot(self): return {'ready': self.is_ready, 'busy': self.is_busy}

    def get_saved_experiments(self): return []
    def invalidate_wavelengths(self): pass
    def _calibration_context(self, optics): return None
    def calibration_wavelengths(self, force=False): return np.arange(1, 513, dtype=float)
    get_wavelength_calibration = calibration_wavelengths

    def set_center_wavelength_when_ready(self, center_nm, **kwargs):
        return self.lightfield.set_center_wavelength_when_ready(
            float(center_nm), update_acquisition_recipe=False, **kwargs)

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames, timeout_s=15.0):
        self._prepared = False
        if self._recipe_capabilities:
            exposure_ms, frames = recipe(exposure_ms, frames)
        elif (isinstance(exposure_ms, bool) or not math.isfinite(float(exposure_ms))
                or float(exposure_ms) != 100.0 or isinstance(frames, bool) or frames != 1):
            raise ValueError('Local WinUSB supports only 100 ms and one frame in this release')
        if not math.isfinite(float(center_nm)) or float(center_nm) < 0:
            raise ValueError('Center wavelength must be finite and nonnegative')
        if not math.isfinite(float(timeout_s)) or timeout_s <= 0:
            raise ValueError('Optics timeout must be finite and positive')
        if not self.is_ready or self.is_busy:
            raise RuntimeError('Local InGaAs/LightField is not ready or is busy')
        deadline = time.monotonic() + timeout_s
        self.bridge.preflight()
        def remaining():
            duration = deadline-time.monotonic()
            if duration <= 0: raise TimeoutError('Local InGaAs optical preparation timed out')
            return duration
        optics = ensure_output_route(self.lightfield, self.output_route, timeout_s=remaining())
        self.set_center_wavelength_when_ready(center_nm, timeout_s=remaining())
        remaining()
        self._abort.clear()
        self._requested_center_nm = float(center_nm)
        self._settings.update(exposure_ms=float(exposure_ms), accumulations=int(frames),
                              verification='bounded_native_recipe' if self._recipe_capabilities else 'fixed_reviewed_native_recipe')
        self._prepared = True
        return {'center_nm': float(center_nm), 'exposure_ms': float(exposure_ms), 'frames': int(frames),
                'axis_unit': 'pixel', 'calibration_status': 'uncalibrated',
                'winspec': dict(self._settings), 'optics': optics}

    def acquire(self):
        if not self._prepared or self._abort.is_set():
            raise RuntimeError('Apply acquisition settings before each local capture')
        if not self.is_ready or self.is_busy:
            raise RuntimeError('Local InGaAs/LightField is not ready or is busy')
        self._prepared = False  # Never allow a continuous consumer to repeat Start.
        self._busy.set()
        self._last_frame = {}
        started = time.monotonic()
        try:
            before = ensure_output_route(self.lightfield, self.output_route, apply=False)
            exposure, n = self._settings['exposure_ms'], self._settings['accumulations']
            batch_id = str(uuid.uuid4()); members, raw_frames = [], []
            total = np.zeros(512, dtype=np.uint64)
            for index in range(1,n+1):
                if self._abort.is_set(): raise RuntimeError('Local acquisition cancelled')
                request_id = str(uuid.uuid4())
                context = {'requested_center_nm': self._requested_center_nm,
                    'settings': dict(self._settings), 'optics_before': copy.deepcopy(before),
                    'batch_id':batch_id, 'accumulation_index':index, 'accumulations':n}
                kwargs = {'exposure_ms':exposure} if self._recipe_capabilities else {}
                native, payload = self.bridge.capture(self._abort, request_id=request_id, context=context, **kwargs)
                if self._abort.is_set(): raise RuntimeError('Local acquisition cancelled')
                if native.get('request_id') != request_id:
                    raise RuntimeError('Local frame belongs to a different acquisition request')
                if len(payload) != 1024: raise RuntimeError('Expected one 512 x 1 uint16 frame')
                if native.get('payload_sha256') != hashlib.sha256(payload).hexdigest():
                    raise RuntimeError('Local frame payload differs from the validated request result')
                if self._recipe_capabilities and (native.get('exposure_ms') != exposure or native.get('request_context') != context
                        or type(native.get('request_sequence')) is not int
                        or (members and (native['bridge_session_id'] != members[0]['bridge_session_id']
                            or native['request_sequence'] != members[-1]['request_sequence']+1))):
                    raise RuntimeError('Local subframe exposure, sequence, session or batch mismatch')
                raw = np.frombuffer(payload, dtype='<u2').astype(np.uint64)
                total += raw; raw_frames.append(raw.tolist()); members.append(native)
            counts = total.astype(float)/n
            if n > 1:
                native = {'acquisition_backend':'winusb_local', 'connection_mode':'persistent_accumulation',
                    'batch_protocol':1,'batch_id':batch_id,'exposure_ms':exposure,'accumulations':n,
                    'frames':members,'raw_frames':raw_frames}
                saved_members({'native_capture':native,'raw_accumulated_counts':total.tolist()})
            after = read_optics(self.lightfield)
            if self._abort.is_set(): raise RuntimeError('Local acquisition cancelled')
            self._last_frame = {'width': 512, 'height': 1, 'frame_count': 1, 'winspec_datatype': 3,
                'acquisition_backend': 'winusb_local', 'settings': dict(self._settings),
                'raw_accumulated_counts': total.tolist(), 'native_capture': native,
                'temperature_verification': {'verified': False, 'policy': 'experimental_unverified',
                                             'cooling_controlled': False},
                'intensity_processing': {'input': 'raw_counts', 'output': 'mean_counts_per_exposure',
                    'accumulations': n, 'divisor': n, 'exposure_ms': exposure,
                    'method': 'host_sum_then_mean' if n > 1 else 'single_raw_frame', 'version': 1},
                'optics_before': before, 'optics_after': after,
                'host_timing_s': {'total': time.monotonic()-started}}
            return self.calibration_wavelengths(), counts
        finally:
            self._busy.clear()

    def abort_acquisition(self):
        self._abort.set()
        self._prepared = False
        return True  # Cancels publication; the frozen helper performs its own STOP/drain.

    def close(self):
        if self.is_busy:
            raise RuntimeError('Local native acquisition ownership is still active; wait for cleanup')
        self.bridge.close()
        self._closed = True

    def get_temperature_snapshot(self, **kwargs):
        return {'temperature_c': None, 'temperature_status': 'Unverified',
                'temperature_verified': False, 'acquisition_backend': 'winusb_local',
                'policy': 'experimental_unverified'}

    def read_metadata_snapshot(self):
        return {'identity': self.identity, 'observed': {'winspec': dict(self._settings),
                'last_frame': copy.deepcopy(self._last_frame)},
                'calibration': {'axis_unit': 'pixel', 'status': 'uncalibrated'}}

    def acquire_2d(self):
        raise RuntimeError('Local InGaAs supports one-dimensional single-frame capture only')
