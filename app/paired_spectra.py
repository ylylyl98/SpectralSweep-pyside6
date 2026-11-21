"""Coordinate a paired spectrum without transferring either SDK's ownership."""
from datetime import datetime, timezone
import math
import threading

import numpy as np

from app.lightfield_capture_guard import LightFieldAcquisitionStalled


PAIRED_STATION_ID = "attodry2100"
ANDOR_BACKENDS = ("andor_si", "andor_ingaas")


def validate_pair_recipes(andor_backend, recipes):
    if andor_backend not in ANDOR_BACKENDS:
        raise ValueError("Choose Andor Si or Andor InGaAs for paired spectra.")
    normalized = {}
    for backend in ("lightfield", andor_backend):
        try:
            settings = recipes[backend]
            center = float(settings["center_nm"])
            exposure = float(settings["exposure_ms"])
            frames = float(settings["frames"])
            if (not math.isfinite(center) or center < 0
                    or not math.isfinite(exposure) or exposure <= 0
                    or not frames.is_integer() or frames < 1
                    or any(isinstance(settings[name], bool) for name in ("center_nm", "exposure_ms", "frames"))):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{backend} needs a finite, non-negative center wavelength, "
                "positive exposure and a positive whole accumulation count."
            ) from exc
        normalized[backend] = dict(center_nm=center, exposure_ms=exposure, frames=int(frames))
    return normalized


def validate_pair_centers(andor_backend, centers):
    if andor_backend not in ANDOR_BACKENDS:
        raise ValueError("Choose Andor Si or Andor InGaAs for paired spectra.")
    normalized = {}
    for backend in ("lightfield", andor_backend):
        try:
            value = centers[backend]
            center = float(value)
            if isinstance(value, (bool, np.bool_)) or not math.isfinite(center) or center < 0:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{backend} needs a finite, non-negative center wavelength.") from exc
        normalized[backend] = center
    return normalized


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _check_stop(stop_event):
    if stop_event.is_set():
        raise RuntimeError("Paired spectrum acquisition stopped.")


def _check_ready(setup):
    ready = getattr(setup, "is_ready", False)
    busy = getattr(setup, "is_busy", False)
    if not (ready() if callable(ready) else ready) or (busy() if callable(busy) else busy):
        raise RuntimeError("Spectrometer is connected but not ready for acquisition.")


def _wait_for_peer(event, stop_event):
    while not event.wait(0.05):
        _check_stop(stop_event)
    _check_stop(stop_event)


def _run_pair(andor_name, lightfield, andor, stop_event, results, operate, finish):
    lightfield_name = "lightfield"
    ready = {backend: threading.Event() for backend in results}
    available = {}

    def run_channel(backend, other, session):
        try:
            try:
                _check_stop(stop_event)
                _check_ready(session[0])
                available[backend] = True
            finally:
                ready[backend].set()
            _wait_for_peer(ready[other], stop_event)
            if not available.get(other):
                raise RuntimeError(f"Paired operation requires {other} to be ready.")
            operate(backend, other, session)
        except Exception as exc:
            results[backend]["error"] = str(exc) or type(exc).__name__
            if finish is not None and isinstance(exc, LightFieldAcquisitionStalled):
                results[backend]["recovery_required"] = True
        finally:
            if finish is not None:
                finish(backend)

    def run_andor():
        try:
            setup, _ = andor
            owner = getattr(setup, "run_paired_operation", None)

            def operation():
                run_channel(andor_name, lightfield_name, andor)

            if owner is None:
                operation()
            else:
                owner(operation)
        except Exception as exc:
            results[andor_name]["error"] = str(exc) or type(exc).__name__
            ready[andor_name].set()
            if finish is not None:
                finish(andor_name)

    andor_thread = threading.Thread(target=run_andor, name="Paired spectrometer Andor dispatch")
    andor_thread.start()
    try:
        run_channel(lightfield_name, andor_name, lightfield)
    finally:
        # Retain ownership while either SDK is returning from its current call.
        andor_thread.join()
    return results


def capture_pair(recipes, lightfield, andor, prepare_lightfield_sensor, stop_event):
    """Run on the LF6 worker. Andor calls stay on its existing SDK owner."""
    _, andor_name = recipes
    prepared = {backend: threading.Event() for backend in recipes}
    results = {
        backend: dict(settings=dict(settings), started_at=None, finished_at=None)
        for backend, settings in recipes.items()
    }

    def capture(backend, other, session):
        setup, adapter = session
        record = results[backend]
        readback = adapter.configure_for_acquisition(**recipes[backend])
        if backend == "lightfield":
            _check_stop(stop_event)
            sensor = prepare_lightfield_sensor()
            readback = dict(readback or {}, lightfield_sensor_mode=sensor)
        record["readback"] = readback or {}
        prepared[backend].set()
        # A failed preparation also releases its peer for a partial capture.
        _wait_for_peer(prepared[other], stop_event)
        record["started_at"] = _timestamp()
        acquire = getattr(adapter, "acquire_for_pair", None)
        wavelengths, counts = adapter.acquire() if acquire is None else acquire(stop_event)
        wavelengths = np.array(wavelengths, dtype=float, copy=True)
        counts = np.array(counts, dtype=float, copy=True)
        if (wavelengths.ndim != 1 or counts.shape != wavelengths.shape or not wavelengths.size
                or not np.isfinite(wavelengths).all() or not np.isfinite(counts).all()):
            raise ValueError("Spectrometer returned invalid wavelength/count data.")
        axis_unit = "pixel" if backend == andor_name and recipes[backend]["center_nm"] == 0 else "nm"
        if axis_unit == "pixel" and not hasattr(setup, "run_paired_operation"):
            wavelengths = np.arange(wavelengths.size, dtype=float)
        record.update(wavelengths=wavelengths, counts=counts, axis_unit=axis_unit)

    def finish(backend):
        prepared[backend].set()
        results[backend]["finished_at"] = _timestamp()

    return _run_pair(andor_name, lightfield, andor, stop_event, results, capture, finish)


def apply_pair_centers(centers, lightfield, andor, stop_event):
    """Apply only the centers, keeping each spectrometer on its permanent owner."""
    _, andor_name = centers
    results = {backend: {} for backend in centers}

    def apply(backend, _other, session):
        setup, adapter = session
        adapter.invalidate_wavelengths()
        value = float(setup.apply_center_wavelength(centers[backend], stop_event=stop_event))
        if not math.isfinite(value):
            raise RuntimeError("Spectrometer did not return a finite center wavelength.")
        results[backend]["center_nm"] = value

    return _run_pair(andor_name, lightfield, andor, stop_event, results, apply, None)
