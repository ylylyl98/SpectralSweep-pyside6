"""Read-only LightField provenance, independent of pythonnet for offline tests.

SDK names follow the installed LightField AddInSupportServices API. Unsupported
settings remain null with a reason; requested values are never used as readbacks.
"""
from __future__ import annotations

import json
import logging
import math
import weakref
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# (group, output field, SDK settings class, SDK member, conversion)
FIELDS = (
    ("acquisition", "exposure_ms", "camera", "ShutterTimingExposureTime", float),
    ("acquisition", "exposures_per_frame", "experiment", "OnlineProcessingFrameCombinationFramesCombined", int),
    ("acquisition", "combination_method", "experiment", "OnlineProcessingFrameCombinationMethod", str),
    ("acquisition", "frames_to_store", "experiment", "AcquisitionFramesToStore", int),
    ("camera", "temperature_c", "camera", "SensorTemperatureReading", float),
    ("camera", "temperature_setpoint_c", "camera", "SensorTemperatureSetPoint", float),
    ("camera", "temperature_status", "camera", "SensorTemperatureStatus", str),
    ("camera", "readout_mode", "camera", "ReadoutControlMode", str),
    ("camera", "adc_speed_mhz", "camera", "AdcSpeed", float),
    ("camera", "adc_quality", "camera", "AdcQuality", str),
    ("camera", "analog_gain", "camera", "AdcAnalogGain", str),
    ("camera", "em_gain", "camera", "AdcEMGain", float),
    ("camera", "adc_bit_depth", "camera", "AdcBitDepth", int),
    ("detector", "roi_selection", "camera", "ReadoutControlRegionsOfInterestSelection", str),
    ("spectrometer", "center_wavelength_nm", "spectrometer", "GratingCenterWavelength", float),
    ("spectrometer", "grating", "spectrometer", "GratingSelected", str),
    ("spectrometer", "entrance_port", "spectrometer", "OpticalPortEntranceSelected", str),
    ("spectrometer", "exit_port", "spectrometer", "OpticalPortExitSelected", str),
    *(("spectrometer", f"{port}_slit_width_um", "spectrometer", member, float)
      for port, member in (
          ("entrance_front", "OpticalPortEntranceFrontWidth"),
          ("entrance_side", "OpticalPortEntranceSideWidth"),
          ("entrance_back", "OpticalPortEntranceBackWidth"),
          ("exit_front", "OpticalPortExitFrontWidth"),
          ("exit_side", "OpticalPortExitSideWidth"))),
    *(("processing", field, "experiment", member, bool) for field, member in (
        ("background_subtraction_enabled", "OnlineCorrectionsBackgroundCorrectionEnabled"),
        ("flatfield_correction_enabled", "OnlineCorrectionsFlatfieldCorrectionEnabled"),
        ("blemish_correction_enabled", "OnlineCorrectionsBlemishCorrectionEnabled"),
        ("cosmic_ray_correction_enabled", "OnlineCorrectionsCosmicRayCorrectionEnabled"),
        ("orientation_correction_enabled", "OnlineCorrectionsOrientationCorrectionEnabled"),
        ("cross_section_enabled", "OnlineProcessingCrossSectionEnabled"),
        ("formulas_enabled", "OnlineProcessingFormulasEnabled"))),
)


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_snapshot(setup, *, camera, spectrometer, experiment):
    """Run on the acquisition execution path, immediately before Capture."""
    result = {"schema_version": 1, "captured_utc": timestamp(), "unavailable": {}}

    def read(group, name, getter):
        try:
            value = getter()
            if value is None or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError("no valid readback")
        except Exception as exc:
            value = None
            result["unavailable"][f"{group}.{name}"] = f"{type(exc).__name__}: {exc}"
        result.setdefault(group, {})[name] = value

    namespaces = {"camera": camera, "spectrometer": spectrometer, "experiment": experiment}
    for group, name, namespace, member, convert in FIELDS:
        def getter(namespace=namespace, member=member, convert=convert):
            key = getattr(namespaces[namespace], member)
            if not setup.experiment.Exists(key):
                raise ValueError("setting unavailable for this device/experiment")
            value = setup.experiment.GetValue(key)
            if value is None:
                raise ValueError("empty readback")
            return convert(value)
        read(group, name, getter)

    read("provenance", "experiment_name", lambda: str(setup.experiment.Name))
    read("provenance", "application_assembly_version",
         lambda: str(setup.application.GetType().Assembly.GetName().Version))
    read("provenance", "devices", lambda: [
        {"type": str(d.Type), "model": str(d.Model), "serial_number": str(d.SerialNumber)}
        for d in setup.experiment.ExperimentDevices])
    read("detector", "regions", lambda: [
        {name: int(getattr(roi, member)) for name, member in (
            ("x", "X"), ("y", "Y"), ("width", "Width"), ("height", "Height"),
            ("x_binning", "XBinning"), ("y_binning", "YBinning"))}
        for roi in setup.experiment.SelectedRegions])
    # This is the SDK detector calibration, before application alignment/binning.
    # The exported spectrum's wavelength column remains the authoritative axis.
    def calibration():
        axis = [float(v) for v in setup.experiment.SystemColumnCalibration]
        if not axis or not all(math.isfinite(v) for v in axis):
            raise ValueError("empty or non-finite wavelength calibration")
        return axis
    read("calibration", "detector_wavelength_nm", calibration)
    return result


class LightFieldRecorder:
    """Deduplicate configuration; append capture timing/temperature to JSONL.

    A weak reference and terminal check prevent late captures modifying a
    completed run. The acquisition journal avoids rewriting a growing JSON
    sidecar on every point of a long sweep.
    """
    def __init__(self, run):
        self._run = weakref.ref(run)
        self._settings = {}
        self._index = 0
        self.context = {}
        self.path = run.path.with_name(run.path.stem.removesuffix(".metadata") + ".lightfield.jsonl")

    @property
    def active(self):
        run = self._run()
        return run is not None and not run._terminal

    def begin(self, snapshot, frames, purpose):
        run = self._run()
        if run is None:
            return None
        with run._lock:
            if run._terminal:
                return None
            snapshot = dict(snapshot)
            captured = snapshot.pop("captured_utc")
            camera = dict(snapshot.get("camera", {}))
            temperature = camera.pop("temperature_c", None)
            snapshot["camera"] = camera
            key = json.dumps(snapshot, sort_keys=True, allow_nan=False)
            state = run.metadata.setdefault("observed", {}).setdefault("lightfield", {
                "schema_version": 1, "settings_snapshots": [],
                "acquisition_log": self.path.relative_to(run.service.output_root).as_posix(),
                "index_semantics": "1-based capture attempts, including warmups; not CSV row numbers",
            })
            if key not in self._settings:
                settings_id = len(self._settings) + 1
                self._settings[key] = settings_id
                state["settings_snapshots"].append({
                    "settings_id": settings_id, "first_observed_utc": captured, **snapshot})
                run.register_file(self.path, role="metadata", kind="lightfield_acquisitions")
            self._index += 1
            record = {"event": "capture_started", "acquisition_index": self._index,
                      "settings_id": self._settings[key], "started_utc": captured,
                      "temperature_c": temperature, "capture_frames_requested": int(frames),
                      "purpose": self.context.get("purpose", purpose),
                      "context": dict(self.context)}
            self._append(record)
            return record

    def finish(self, record, *, dimensions=None, error=None):
        if record is None:
            return
        run = self._run()
        if run is None:
            return
        with run._lock:
            if not run._terminal:
                self._append({"event": "capture_failed" if error else "capture_completed",
                              "acquisition_index": record["acquisition_index"],
                              "settings_id": record["settings_id"], "finished_utc": timestamp(),
                              "output_dimensions": dimensions, "error": error})

    def _append(self, record):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, allow_nan=False) + "\n")


def bind_lightfield_metadata(controller, run):
    """Bind without reading hardware on the UI thread; other backends opt out."""
    setup = getattr(controller, "setup", None)
    binder = getattr(setup, "bind_metadata_run", None)
    if callable(binder):
        binder(run)


def set_lightfield_context(controller, **context):
    """Associate the next capture with a sweep point without changing hardware."""
    setup = getattr(controller, "setup", None)
    recorder = getattr(setup, "_metadata_recorder", None)
    if isinstance(recorder, LightFieldRecorder) and recorder.active:
        output = context.get("output_file")
        if output is not None:
            run = recorder._run()
            try:
                context["output_file"] = Path(output).resolve().relative_to(
                    run.service.output_root.resolve()).as_posix()
            except ValueError:
                context["output_file"] = Path(output).name
        recorder.context = context


def capture_with_metadata(setup, frames, *, purpose="measurement"):
    """Optional readback/log failures never hide a capture result or its error."""
    recorder = getattr(setup, "_metadata_recorder", None)
    record = None
    if recorder is not None and recorder.active:
        try:
            record = recorder.begin(setup.read_metadata_snapshot(), frames, purpose)
        except Exception:
            log.warning("LightField metadata snapshot could not be recorded", exc_info=True)
    try:
        dataset = setup.experiment.Capture(frames)
    except Exception as exc:
        if recorder is not None:
            try:
                recorder.finish(record, error=f"{type(exc).__name__}: {exc}")
            except Exception:
                log.warning("LightField capture failure metadata could not be recorded", exc_info=True)
        raise
    if recorder is not None and record is not None:
        try:
            try:
                width, height = setup._frame_dims(dataset.GetFrame(0, 0))
                dimensions = {"width": width, "height": height}
            except Exception:
                dimensions = None
            recorder.finish(record, dimensions=dimensions)
        except Exception:
            log.warning("LightField capture result metadata could not be recorded", exc_info=True)
    return dataset
