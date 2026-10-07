"""Calibrated scan surface over the raw WinSpec detector acquisition."""
import copy
import logging

import numpy as np

from app.devices import winspec_adapter
from app.wavelength_calibration import calibrated_axis
from utils.config import cfg

log = logging.getLogger(__name__)


class WinSpecScanAdapter:
    def __init__(self, setup):
        self.setup = setup
        self._prepared = None

    def _context(self):
        return self.setup._calibration_context(winspec_adapter.read_optics(self.setup.lightfield))

    @staticmethod
    def _resolve(context):
        for record in reversed(cfg.lf6.winspec_wavelength_calibrations):
            try:
                result = calibrated_axis(record, context)
                if result is not None:
                    axis, mask = result
                    return copy.deepcopy(record), np.asarray(axis), np.asarray(mask)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        context = context or {}
        raise RuntimeError(
            f"WinSpec scan requires a matching wavelength calibration: "
            f"center={context.get('center_nm', 'unknown')} nm, "
            f"grating={context.get('grating', 'unknown')}, "
            f"output={context.get('output_port', 'unknown')}. "
            "Save/import a validated WinSpec calibration covering this center, "
            "grating and detector setup in the calibration page. "
            "Missing live optics/detector readbacks must also be resolved."
        )

    def validate_scan_centers(self, centers):
        context = self._context()
        if context is None:
            self._resolve(None)
        for center in dict.fromkeys(float(value) for value in centers):
            self._resolve({**context, 'center_nm': center})

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        self._prepared = None
        self.validate_scan_centers([center_nm])
        readback = self.setup.configure_for_acquisition(
            center_nm=center_nm, exposure_ms=exposure_ms, frames=frames)
        context = self._context()
        if context is None or not np.isclose(context['center_nm'], float(center_nm), rtol=0, atol=1e-6):
            raise RuntimeError('WinSpec scan center wavelength readback differs from request')
        record, axis, mask = self._resolve(context)
        self._prepared = (copy.deepcopy(context), record, axis, mask)
        return {**readback, 'axis_unit': 'nm',
                'calibration_status': record.get('kind', 'fixed_position_calibration'),
                'wavelength_calibration': copy.deepcopy(record)}

    def _check_prepared(self):
        if self._prepared is None:
            raise RuntimeError('Prepare WinSpec scan settings and wavelength calibration before acquisition')
        if self._context() != self._prepared[0]:
            raise RuntimeError('WinSpec scan calibration context changed; apply scan settings again')
        return self._prepared

    def calibration_wavelengths(self, force=False):
        return self._check_prepared()[2].copy()

    get_wavelength_calibration = calibration_wavelengths

    def acquire(self):
        context, record, axis, mask = self._check_prepared()
        recorder = getattr(self.setup, '_metadata_recorder', None)
        capture_record = None
        if recorder is not None and recorder.active:
            try:
                capture_record = recorder.begin(self.read_metadata_snapshot(), 1, 'measurement')
            except Exception as exc:
                self._metadata_failure(recorder, exc)
        try:
            _, counts = self.setup.acquire()
            if self.setup.last_calibration_context != context:
                raise RuntimeError('WinSpec scan calibration context changed during acquisition; frame discarded')
        except Exception as exc:
            if recorder is not None:
                self._finish_metadata(recorder, capture_record, error=str(exc))
            raise
        if recorder is not None:
            self._finish_metadata(recorder, capture_record, dimensions={'width': len(axis), 'height': 1},
                                  frame_observation=copy.deepcopy(self.setup.read_metadata_snapshot()['observed']['last_frame']))
        return axis.copy(), np.asarray(counts)[mask]

    def bind_metadata_run(self, run):
        from app.lightfield_metadata import LightFieldRecorder
        # The common context setter addresses the raw setup.
        self.setup._metadata_recorder = LightFieldRecorder(run)

    @staticmethod
    def _metadata_failure(recorder, exc):
        run = recorder._run()
        if run is not None:
            run.mark_metadata_failure(exc, event='winspec_capture')
        log.warning('WinSpec scan metadata could not be recorded: %s', exc)

    def _finish_metadata(self, recorder, record, **kwargs):
        try:
            recorder.finish(record, **kwargs)
        except Exception as exc:
            self._metadata_failure(recorder, exc)

    def read_metadata_snapshot(self):
        from app.lightfield_metadata import timestamp
        snapshot = self.setup.read_metadata_snapshot()
        snapshot['captured_utc'] = timestamp()
        if self._prepared is not None:
            context, record, axis, _ = self._prepared
            snapshot['identity'] = {**snapshot['identity'], 'axis_unit': 'nm',
                                    'calibration_status': record.get('kind', 'fixed_position_calibration')}
            snapshot['calibration'] = {'axis_unit': 'nm', 'wavelength_axis_nm': axis.tolist(),
                                       'context': copy.deepcopy(context),
                                       'record': copy.deepcopy(record)}
        return snapshot

    def __getattr__(self, name):
        return getattr(self.setup, name)
