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
