"""Read-only LightField provenance, independent of pythonnet for offline tests.

SDK names follow the installed LightField AddInSupportServices API. Unsupported
settings remain null with a reason; requested values are never used as readbacks.
"""
from __future__ import annotations

import json
import logging
import math
import sys
import weakref
import uuid
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .lightfield_capture_guard import (
    LightFieldAcquisitionStalled, LightFieldCaptureOwnershipError,
    capture_operation, get_capture_guard,
)

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
    guard = get_capture_guard(setup)

    def checked(getter):
        guard.check_available()
        value = getter()
        guard.check_available()
        return value

    def read(group, name, getter):
        try:
            value = checked(getter)
            if value is None or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError("no valid readback")
        except (LightFieldAcquisitionStalled, LightFieldCaptureOwnershipError):
            raise
        except Exception as exc:
            guard.check_available()
            value = None
            result["unavailable"][f"{group}.{name}"] = f"{type(exc).__name__}: {exc}"
        result.setdefault(group, {})[name] = value

    namespaces = {"camera": camera, "spectrometer": spectrometer, "experiment": experiment}
    for group, name, namespace, member, convert in FIELDS:
        def getter(namespace=namespace, member=member, convert=convert):
            key = getattr(namespaces[namespace], member)
            if not checked(lambda: setup.experiment.Exists(key)):
                raise ValueError("setting unavailable for this device/experiment")
            value = checked(lambda: setup.experiment.GetValue(key))
            if value is None:
                raise ValueError("empty readback")
            return convert(value)
        read(group, name, getter)

    read("provenance", "experiment_name", lambda: str(setup.experiment.Name))
    def assembly_version():
        kind = checked(lambda: setup.application.GetType())
        assembly = checked(lambda: kind.Assembly)
        name = checked(lambda: assembly.GetName())
        return str(checked(lambda: name.Version))
    read("provenance", "application_assembly_version", assembly_version)

    def devices():
        return [{name: str(checked(lambda: getattr(device, member)))
                 for name, member in (("type", "Type"), ("model", "Model"), ("serial_number", "SerialNumber"))}
                for device in checked(lambda: setup.experiment.ExperimentDevices)]
    read("provenance", "devices", devices)

    def regions():
        return [{name: int(checked(lambda: getattr(roi, member))) for name, member in (
                    ("x", "X"), ("y", "Y"), ("width", "Width"), ("height", "Height"),
                    ("x_binning", "XBinning"), ("y_binning", "YBinning"))}
                for roi in checked(lambda: setup.experiment.SelectedRegions)]
    read("detector", "regions", regions)
    # This is the SDK detector calibration, before application alignment/binning.
    # The exported spectrum's wavelength column remains the authoritative axis.
    def calibration():
        axis = [float(v) for v in checked(lambda: setup.experiment.SystemColumnCalibration)]
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
        self._last_completed_id = None
        self._last_frame_observation = None
        self.context = {}
        # All backends append to the run's single journal.  ``path`` remains
        # public for legacy integrations and tests.
        self.path = getattr(run, "event_path",
                            run.path.with_name(run.path.stem.removesuffix(".metadata") + ".lightfield.jsonl"))

    @property
    def active(self):
        run = self._run()
        return run is not None and not run._terminal

    @contextmanager
    def _recording_guard(self, event):
        """Keep storage errors visible even when acquisition callers continue."""
        try:
            yield
        except Exception as exc:
            run = self._run()
            marker = getattr(run, "mark_metadata_failure", None) if run is not None else None
            if callable(marker):
                marker(exc, event=event)
            raise

    def begin(self, snapshot, frames, purpose):
        run = self._run()
        if run is None:
            return None
        with run._lock, self._recording_guard("capture_started"):
            if run._terminal:
                return None
            snapshot = copy.deepcopy(snapshot)
            captured = snapshot.pop("captured_utc")
            # WinSpec exposes the preceding frame's timing/guard diagnostics
            # alongside settings. They change on every frame, and must not
            # invalidate the settings identity or duplicate calibration data.
            previous_frame = None
            if snapshot.get("identity", {}).get("backend") == "winspec_ingaas":
                observed = dict(snapshot.get("observed", {}))
                previous_frame = observed.pop("last_frame", None)
                snapshot["observed"] = observed
            camera = dict(snapshot.get("camera", {}))
            temperature = camera.pop("temperature_c", None)
            temperature_status = camera.pop("temperature_status", None)
            snapshot["camera"] = camera
            if snapshot.get("backend") == "andor_sdk2" or snapshot.get("identity", {}).get("backend") == "andor_sdk2":
                observed = dict(snapshot.get("observed", {}))
                temperature = observed.pop("temperature_c", temperature)
                temperature_status = observed.pop("temperature_status", temperature_status)
                snapshot["observed"] = observed
            key = json.dumps(snapshot, sort_keys=True, allow_nan=False)
            state = run.metadata.setdefault("observed", {}).setdefault("lightfield", {
                "schema_version": 1, "settings_snapshots": [],
                "acquisition_log": self.path.relative_to(run.service.output_root).as_posix(),
                "index_semantics": "1-based capture attempts, including warmups; not CSV row numbers",
            })
            shared_settings_id = None
            if hasattr(run, "register_settings_snapshot"):
                shared_settings_id = run.register_settings_snapshot(
                    snapshot, source="lightfield", observed_utc=captured, persist=False)
                run.metadata.setdefault("settings", {}).setdefault("observed", {})[
                    "lightfield_settings_id"] = shared_settings_id
            if key not in self._settings:
                snapshots = state.setdefault("settings_snapshots", [])
                # Compatibility IDs belong to the run, not to an individual
                # backend recorder. Rebinding can reuse an existing shared link.
                existing = next((item for item in snapshots
                                 if shared_settings_id is not None
                                 and item.get("shared_settings_id") == shared_settings_id), None)
                if existing is not None:
                    settings_id = int(existing["settings_id"])
                else:
                    settings_id = max((int(item.get("settings_id", 0)) for item in snapshots), default=0) + 1
                    snapshots.append({"settings_id": settings_id,
                                      "first_observed_utc": captured,
                                      "shared_settings_id": shared_settings_id})
                    run._files_dirty = True
                self._settings[key] = settings_id
                if hasattr(run, "_register_event_file"):
                    run._register_event_file(kind="lightfield_acquisitions", persist=False)
                else:
                    run.register_file(self.path, role="metadata", kind="lightfield_acquisitions")
            # Save the settings, file association and compatibility IDs together.
            # A failed write keeps the dirty marker so the next attempt can retry.
            if getattr(run, "_files_dirty", False):
                run._write()
            self._index += 1
            record = {"event": "capture_started", "acquisition_id": str(self.context.get("acquisition_id") or uuid.uuid4()),
                      "acquisition_index": self._index,
                      "settings_id": shared_settings_id or self._settings[key], "started_utc": captured,
                      "shared_settings_id": shared_settings_id,
                      "temperature_c": temperature, "capture_frames_requested": int(frames),
                      "temperature_status": temperature_status,
                      "purpose": self.context.get("purpose", purpose),
                      "context": dict(self.context)}
            if previous_frame is not None:
                if self._last_completed_id is not None and previous_frame == self._last_frame_observation:
                    record["previous_acquisition_id"] = self._last_completed_id
                else:
                    record["previous_frame_observation"] = previous_frame
            appended = self._append(record)
            if appended is None:
                marker = getattr(run, "mark_metadata_failure", None)
                if callable(marker):
                    marker(RuntimeError("LightField acquisition event was not durably recorded"), event="capture_started")
            return appended

    def finish(self, record, *, dimensions=None, error=None, frame_observation=None):
        if record is None:
            return
        run = self._run()
        if run is None:
            return
        with run._lock, self._recording_guard("capture_failed" if error else "capture_completed"):
            if not run._terminal:
                appended = self._append({"event": "capture_failed" if error else "capture_completed",
                              "acquisition_index": record["acquisition_index"],
                              "settings_id": record["settings_id"], "finished_utc": timestamp(),
                              "output_dimensions": dimensions, "error": error,
                              **({'frame_observation': frame_observation} if frame_observation is not None else {}),
                              "acquisition_id": record.get("acquisition_id")})
                if appended is not None and not error and frame_observation is not None:
                    self._last_completed_id = record.get("acquisition_id")
                    self._last_frame_observation = copy.deepcopy(frame_observation)

    def _append(self, record):
        run = self._run()
        if run is not None and hasattr(run, "record_event"):
            event = record.get("event", "observation")
            values = dict(record)
            values.pop("event", None)
            acquisition_id = values.pop("acquisition_id", None)
            output_file = values.pop("output_file", None)
            if output_file is None and isinstance(values.get("context"), dict):
                output_file = values["context"].get("output_file")
            output = {"file": output_file} if output_file else None
            result = run.record_event(event, acquisition_id=acquisition_id, output=output,
                                      **values)
            if result is None:
                marker = getattr(run, "mark_metadata_failure", None)
                if callable(marker):
                    marker(RuntimeError(f"LightField event {event!r} was not durably recorded"), event=event)
            return result
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, allow_nan=False) + "\n")
        return record


def bind_lightfield_metadata(controller, run):
    """Bind without reading hardware on the UI thread; other backends opt out."""
    setup = getattr(controller, "setup", None)
    existing = getattr(setup, '_metadata_recorder', None)
    if isinstance(existing, LightFieldRecorder) and existing._run() is run:
        return
    if getattr(controller, 'backend', None) == 'winspec_ingaas':
        adapter = getattr(controller, 'adapter', None)
        if adapter is not None:
            adapter.bind_metadata_run(run)
        return
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


def release_capture_dataset(dataset, operation, *, suppress_errors=False):
    """Release a returned SDK dataset on its owner, including after timeout.

    Callers transfer ownership only after a successful return from
    ``capture_with_metadata``. A primary acquisition error must survive a
    secondary release failure. Older offline SDK doubles may omit Dispose.
    """
    if dataset is None:
        return
    operation.stage("release_dataset", check=False)
    try:
        dispose = getattr(dataset, "Dispose", None)
        if callable(dispose):
            dispose()
    except Exception as exc:
        operation.quarantine(f"Dataset release failed: {type(exc).__name__}: {exc}")
        if not suppress_errors:
            raise
        log.warning("LightField dataset release failed after acquisition error", exc_info=True)
    if not suppress_errors:
        operation.check()


@contextmanager
def managed_capture_dataset(setup, frames, *, purpose="measurement"):
    """Keep the capture operation active until copied data and dataset cleanup finish."""
    with capture_operation(setup, frames=frames, purpose=purpose) as operation:
        dataset = None
        try:
            dataset = capture_with_metadata(setup, frames, purpose=purpose)
            yield dataset, operation
        finally:
            release_capture_dataset(dataset, operation,
                                    suppress_errors=sys.exc_info()[0] is not None)
        operation.check()


def capture_with_metadata(setup, frames, *, purpose="measurement"):
    """Record capture outcomes and transfer a successful dataset to the caller.

    Capture completion is an SDK observation, not a claim that conversion or
    CSV persistence succeeded. The surrounding operation also covers those
    readout steps and rejects results which return after its deadline.
    """
    recorder = getattr(setup, "_metadata_recorder", None)
    record = None
    dataset = None
    operation = None
    failure_recorded = False

    def record_failure(exc):
        nonlocal failure_recorded
        if recorder is None or failure_recorded:
            return
        failure_recorded = True
        try:
            recorder.finish(record, error=f"{type(exc).__name__}: {exc}")
        except Exception:
            log.warning("LightField capture failure metadata could not be recorded", exc_info=True)

    def discard_owned_dataset(active_operation):
        nonlocal dataset
        # Clear the ownership slot before Dispose: even a failing cleanup must
        # never be retried by a second exception/exit path.
        owned, dataset = dataset, None
        release_capture_dataset(owned, active_operation, suppress_errors=True)

    @contextmanager
    def release_on_failure():
        try:
            yield
        except BaseException as exc:
            record_failure(exc)
            discard_owned_dataset(operation)
            raise

    try:
        with capture_operation(setup, frames=frames, purpose=purpose,
                               on_stalled_exit=discard_owned_dataset) as operation, release_on_failure():
            if recorder is not None and recorder.active:
                operation.stage("metadata_snapshot")
                try:
                    snapshot = setup.read_metadata_snapshot()
                    operation.check()
                    record = recorder.begin(snapshot, frames, purpose)
                    if record is not None:
                        operation.update_context(acquisition_id=record.get("acquisition_id"))
                except LightFieldAcquisitionStalled:
                    raise
                except Exception as exc:
                    run = recorder._run()
                    marker = getattr(run, "mark_metadata_failure", None) if run is not None else None
                    if callable(marker):
                        marker(exc, event="capture_started")
                    log.warning("LightField metadata snapshot could not be recorded", exc_info=True)
            operation.stage("validate_before_capture")
            verify = getattr(setup, 'verify_acquisition_settings', None)
            if callable(verify):
                verify()
            operation.stage("capture")
            dataset = setup.experiment.Capture(frames)
            operation.stage("validate_after_capture")
            if callable(verify):
                verify()
            operation.check()
            if recorder is not None and record is not None:
                operation.stage("frame_dimensions")
                try:
                    frame = dataset.GetFrame(0, 0)
                    operation.check()
                    width, height = setup._frame_dims(frame)
                    operation.check()
                    dimensions = {"width": width, "height": height}
                except LightFieldAcquisitionStalled:
                    raise
                except Exception:
                    dimensions = None
                operation.stage("record_capture_completed")
                try:
                    recorder.finish(record, dimensions=dimensions)
                except Exception:
                    log.warning("LightField capture result metadata could not be recorded", exc_info=True)
            operation.check()
        return dataset
    except BaseException as exc:
        record_failure(exc)
        raise
