"""Helpers for keeping spectrometer axes and intensity samples aligned."""

from __future__ import annotations

from typing import Any

import numpy as np


def align_wavelengths_to_intensities(
    wavelengths: Any,
    intensities: Any,
) -> tuple[np.ndarray, np.ndarray]:
    """Return one wavelength label for every acquired intensity sample.

    LightField can report trailing calibration entries beyond the active
    acquisition buffer. The active spectrum width is defined by the captured
    intensity array, so surplus calibration values are removed from its tail.
    A short calibration is rejected because its missing labels cannot be
    reconstructed safely.
    """
    wl = np.asarray(wavelengths, dtype=float).ravel()
    counts = np.asarray(intensities, dtype=float).ravel()

    if counts.size == 0:
        raise ValueError("Spectrometer returned no intensity samples.")
    if wl.size == 0:
        raise ValueError("Spectrometer returned no wavelength calibration values.")
    if wl.size < counts.size:
        raise ValueError(
            "Wavelength calibration has "
            f"{wl.size} values, but the acquired spectrum has {counts.size} samples."
        )

    return wl[:counts.size], counts


def align_wavelengths_to_image(
    wavelengths: Any,
    image: Any,
) -> tuple[np.ndarray, np.ndarray]:
    """Return a ``(y, wavelength)`` image with one label per column.

    LightField normally returns full-sensor data as ``(y, x)``, but some
    driver paths expose the transpose.  Calibration arrays may also contain
    unused trailing detector columns.  Select the image dimension closest to
    (without exceeding) the calibration width, transpose when needed, and
    trim only the surplus calibration tail.
    """
    wl = np.asarray(wavelengths, dtype=float).ravel()
    frame = np.asarray(image, dtype=float)

    if frame.ndim != 2 or 0 in frame.shape:
        raise ValueError(
            f"Full-sensor acquisition must be a non-empty 2D array; received shape {frame.shape}."
        )
    if wl.size == 0:
        raise ValueError("Spectrometer returned no wavelength calibration values.")

    candidates = [axis for axis, size in enumerate(frame.shape) if size <= wl.size]
    if not candidates:
        raise ValueError(
            "Wavelength calibration has "
            f"{wl.size} values, but full-sensor image shape is {frame.shape}."
        )

    # The spectral dimension is normally the dimension nearest the detector's
    # calibration width.  Prefer columns in the truly ambiguous square case.
    spectral_axis = min(candidates, key=lambda axis: (wl.size - frame.shape[axis], axis != 1))
    if spectral_axis == 0:
        frame = frame.T

    return wl[: frame.shape[1]], frame
