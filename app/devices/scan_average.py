"""Software averaging of independently acquired, calibrated scan frames."""
import numpy as np


class AveragedScanAdapter:
    def __init__(self, adapter, frames, stop_event):
        self.adapter = adapter
        self.frames = int(frames)
        if self.frames < 1 or self.frames != frames:
            raise ValueError('Average frame count must be a positive integer')
        self.stop_event = stop_event
        self._validated_axis = None

    @staticmethod
    def _spectrum(wavelengths, counts):
        axis, counts = np.asarray(wavelengths, dtype=float), np.asarray(counts, dtype=float)
        if axis.ndim != 1 or counts.ndim != 1 or axis.shape != counts.shape or axis.size <= 2:
            raise RuntimeError('2D Sweep requires a one-dimensional spectrum; set PIXIS to spectral/vertical-binned readout, not a full-sensor image')
        if not np.all(np.isfinite(axis)) or not np.all(np.isfinite(counts)):
            raise RuntimeError('Non-finite wavelength or counts in scan spectrum')
        return axis, counts

    def validate_spectrum(self):
        """One explicitly logged validation exposure, outside the gate grid."""
        if self.stop_event.is_set():
            raise RuntimeError('Averaged acquisition stopped')
        axis, _ = self._spectrum(*self.adapter.acquire())
        self._validated_axis = axis.copy()
        return axis.copy()

    def set_validated_axis(self, axis):
        self._validated_axis = np.asarray(axis, dtype=float).copy()

    def calibration_wavelengths(self, force=False):
        if self._validated_axis is not None:
            return self._validated_axis.copy()
        return self.adapter.calibration_wavelengths(force=force)

    def acquire(self):
        axis = total = None
        for index in range(self.frames):
            if self.stop_event.is_set():
                raise RuntimeError('Averaged acquisition stopped')
            wavelengths, counts = self.adapter.acquire()
            wavelengths = np.asarray(wavelengths, dtype=float)
            counts = np.asarray(counts, dtype=float)
            if self.stop_event.is_set():
                raise RuntimeError('Averaged acquisition stopped')
            if not np.all(np.isfinite(counts)):
                raise RuntimeError('Non-finite counts in averaged acquisition')
            if self._validated_axis is not None:
                self._spectrum(wavelengths, counts)
                if wavelengths.shape != self._validated_axis.shape or not np.allclose(wavelengths, self._validated_axis, rtol=0, atol=0.00005):
                    raise RuntimeError('Wavelength axis changed since scan validation')
            if index == 0:
                axis, total = wavelengths.copy(), counts.copy()
            else:
                if axis.shape != wavelengths.shape or not np.allclose(axis, wavelengths, rtol=0, atol=0.00005):
                    raise RuntimeError('Wavelength axis changed between averaged frames')
                if total.shape != counts.shape:
                    raise RuntimeError('Detector shape changed between averaged frames')
                total += counts
        return axis, total / self.frames

    def __getattr__(self, name):
        return getattr(self.adapter, name)
