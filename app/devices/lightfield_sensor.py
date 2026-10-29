"""LightField ROI policy with injected SDK namespaces; importing this is SDK-free."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time


_ADVICE = "Check LightField sensor regions and binning; acquisition blocked."
_REGION_FIELDS = (("x", "X"), ("y", "Y"), ("width", "Width"),
                  ("height", "Height"), ("x_binning", "XBinning"), ("y_binning", "YBinning"))
_FINGERPRINT_FIELDS = ("selection", "regions", "full_regions", "sensor_width", "sensor_height",
                       "provider", "experiment", "row_binning", "acquisition_orientation", "calibration_orientation")


def sensor_fingerprint(snapshot):
    """Stable identity of observed geometry, provider, dimensions and experiment."""
    payload = {key: snapshot.get(key) for key in _FINGERPRINT_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def normalize_regions(regions):
    result = []
    for region in regions:
        item = {}
        for key, member in _REGION_FIELDS:
            value = region[key] if isinstance(region, dict) else getattr(region, member)
            if isinstance(value, bool):
                raise ValueError("ROI coordinates/binning must be integers")
            item[key] = int(value)
        result.append(item)
    return result


def read_sensor_snapshot(setup, camera_settings, *, experiment_settings=None, check_access=lambda: None):
    """Read only; optional SDK fields remain explicitly unknown, never inferred."""
    experiment = setup.experiment

    def read(member, default=None, required=False, namespace=None):
        check_access()
        key = getattr(camera_settings if namespace is None else namespace, member, None)
        try:
            if key is None or not experiment.Exists(key):
                if required:
                    raise RuntimeError(f"{member} unavailable")
                return default
            result = experiment.GetValue(key)
            check_access()
            return result
        except Exception as exc:
            check_access()
            if required:
                raise RuntimeError(f"Cannot read LightField {member}. {_ADVICE}") from exc
            return default

    selection = str(read("ReadoutControlRegionsOfInterestSelection", required=True))
    regions = read("ReadoutControlRegionsOfInterestResult")
    if regions is None:
        check_access()
        try:
            regions = experiment.SelectedRegions
        except Exception as exc:
            raise RuntimeError(f"Cannot read LightField selected regions. {_ADVICE}") from exc
        check_access()
    full_regions = read("ReadoutControlRegionsOfInterestFullSensor", [])
    try:
        snapshot = {
            "selection": selection,
            "regions": normalize_regions(regions),
            "full_regions": normalize_regions(full_regions),
            "sensor_width": _optional_positive_int(read("SensorInformationActiveAreaWidth")),
            "sensor_height": _optional_positive_int(read("SensorInformationActiveAreaHeight")),
            "provider": str(read("ReadoutControlRegionsOfInterestBinningProvider", "Unknown")),
            "experiment": str(getattr(experiment, "Name", "Unknown")),
            "row_binning": _optional_positive_int(read("ReadoutControlRegionsOfInterestLineSensorRowBinning")),
            "acquisition_orientation": str(read("AcquisitionOrientationResult", "Unknown", namespace=experiment_settings))
                if experiment_settings is not None else "Unknown",
            "calibration_orientation": str(read("WavelengthCalibrationOrientation", "Unknown", namespace=experiment_settings))
                if experiment_settings is not None else "Unknown",
        }
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError(f"Invalid LightField sensor-region readback. {_ADVICE}") from exc
    check_access()
    snapshot["fingerprint"] = sensor_fingerprint(snapshot)
    return snapshot


def _optional_positive_int(value):
    if value is None:
        return None
    if isinstance(value, bool) or int(value) <= 0:
        raise ValueError("sensor dimensions/binning must be positive")
    return int(value)


def _single_region(snapshot):
    regions = snapshot.get("regions", [])
    if len(regions) != 1:
        raise RuntimeError(f"One sensor ROI is required; multiple/unknown regions cannot form one spectrum. {_ADVICE}")
    region = regions[0]
    if (any(region.get(key, 0) <= 0 for key in ("width", "height", "x_binning", "y_binning"))
            or region.get("x", -1) < 0 or region.get("y", -1) < 0
            or region["width"] % region["x_binning"] or region["height"] % region["y_binning"]):
        raise RuntimeError(f"Invalid or incompatible sensor ROI geometry. {_ADVICE}")
    return region


def _full_reference(snapshot):
    regions = snapshot.get("full_regions", [])
    width, height = snapshot.get("sensor_width"), snapshot.get("sensor_height")
    if (len(regions) != 1 or not width or not height
            or regions[0].get("width") != width or regions[0].get("height") != height
            or regions[0].get("x_binning") != 1 or regions[0].get("y_binning") != 1):
        raise RuntimeError(f"Physical full-sensor area cannot be verified (layout may be overridden). {_ADVICE}")
    return regions[0]


def verify_sensor_readout(snapshot, intent):
    """Verify observed geometry without any SDK mutation or array flattening."""
    mode = intent.get("mode")
    region = _single_region(snapshot)
    if mode == "custom":
        expected = intent.get("snapshot")
        if not isinstance(expected, dict) or snapshot["fingerprint"] != sensor_fingerprint(expected):
            raise RuntimeError(f"Custom LightField ROI fingerprint changed; review the selected regions. {_ADVICE}")
        if region["height"] // region["y_binning"] != 1:
            raise RuntimeError(f"Custom LightField ROI must output exactly one row for 1D; no silent flattening. {_ADVICE}")
    elif mode in {"bin_all", "full_sensor"}:
        full = _full_reference(snapshot)
        expected_selection = "LineSensor" if mode == "bin_all" else "FullSensor"
        if snapshot.get("selection") != expected_selection:
            raise RuntimeError(f"LightField {mode} requires {expected_selection} readback. {_ADVICE}")
        if any(region[key] != full[key] for key in ("x", "y", "width", "height")):
            raise RuntimeError(f"LightField {mode} does not cover the entire physical active area. {_ADVICE}")
        expected_y_bin = full["height"] if mode == "bin_all" else 1
        if region["x_binning"] != 1 or region["y_binning"] != expected_y_bin:
            raise RuntimeError(f"LightField {mode} binning/output geometry was not verified. {_ADVICE}")
        if mode == "bin_all" and snapshot.get("row_binning") != full["height"]:
            raise RuntimeError(f"LightField Bin All row count is not the full sensor height. {_ADVICE}")
    else:
        raise ValueError("Sensor intent mode must be bin_all, full_sensor or custom")
    bound = intent.get("_verified_fingerprint")
    if bound and snapshot["fingerprint"] != bound:
        raise RuntimeError(f"Prepared LightField sensor settings changed during acquisition. {_ADVICE}")
    return snapshot


def prepare_sensor_readout(setup, intent, camera_settings, selections, *, stop_event=None,
                           apply=True, timeout_s=15., poll_interval_s=.05,
                           clock=time, check_access=lambda: None, integer_type=int,
                           experiment_settings=None):
    """Prepare one explicit acquisition policy, preserving provider and custom ROI."""
    intent = copy.deepcopy(dict(intent))
    mode = intent.get("mode")
    if mode not in {"bin_all", "full_sensor", "custom"}:
        raise ValueError("Sensor intent mode must be bin_all, full_sensor or custom")
    timeout_s, poll_interval_s = float(timeout_s), float(poll_interval_s)
    if not all(math.isfinite(v) and v > 0 for v in (timeout_s, poll_interval_s)):
        raise ValueError("Sensor timeout and polling interval must be finite and positive")
    deadline = clock.monotonic() + timeout_s
    changed = False
    phase = "waiting for the sensor to become idle"
    evidence = "LightField is not ready or is busy (check Preview/Acquire)."

    def check():
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError(f"LightField {mode} preparation cancelled; acquisition blocked.")
        check_access()

    def remaining():
        check()
        value = deadline - clock.monotonic()
        if value <= 0:
            raise TimeoutError(
                f"LightField {mode} preparation timed out after {timeout_s:g}s while {phase}. "
                f"{evidence} {_ADVICE}")
        return value

    def ready():
        # Sensor settings do not depend on spectrometer or online-processing
        # capabilities. Legacy/test adapters retain their existing readiness.
        state = getattr(setup, "sensor_readout_ready", None)
        return bool(setup.is_ready if state is None else state) and not setup.is_busy

    def pause():
        clock.sleep(min(poll_interval_s, remaining()))

    def snapshot():
        remaining()
        result = read_sensor_snapshot(setup, camera_settings, experiment_settings=experiment_settings, check_access=check)
        remaining()
        return result

    while not ready():
        pause()
    phase = "reading the current sensor regions"
    initial = snapshot()
    if mode == "custom" or not apply:
        while not ready():
            pause()
        actual = snapshot()
        verify_sensor_readout(actual, intent)
        return dict(actual, mode=mode, changed=False)
    if mode == "bin_all" and initial["selection"] == "CustomRegions":
        permitted = (intent.get("expected_fingerprint"), intent.get("allow_custom"))
        if initial["fingerprint"] not in permitted:
            raise RuntimeError(f"Custom LightField ROI would be overwritten by Bin All; review and confirm its fingerprint. {_ADVICE}")
    _full_reference(initial)  # Fail before any write when physical height is unknown.

    # A fixed/read-only setting can be unavailable for writing while its
    # verified readout already matches the requested policy. Do not wait for
    # editability when no change is needed.
    try:
        verify_sensor_readout(initial, intent)
    except RuntimeError:
        pass
    else:
        if ready():
            remaining()
            return dict(initial, mode=mode, changed=False)

    def write_verified(member, value, *, collection=False):
        nonlocal changed, phase, evidence
        phase = f"applying {member}"
        key = getattr(camera_settings, member, None)
        if key is None:
            raise RuntimeError(f"LightField {member} is unavailable in this SDK. {_ADVICE}")
        wrote = False
        while True:
            remaining()
            # Exists/GetValue describe readable state. Availability/writability
            # are requirements for mutation, not for accepting equal readback.
            exists = setup.experiment.Exists(key)
            if exists:
                actual = setup.experiment.GetValue(key)
                idle = ready()
                evidence = f"Requested {value}; read back {actual}."
                remaining()
                if actual == value and idle:
                    return
                if not idle:
                    evidence += " LightField is not ready or is busy (check Preview/Acquire)."
                elif wrote:
                    evidence += " The write was sent once but has not taken effect."
                elif actual != value:
                    available = setup.setting_is_available(key)
                    writable = setup.setting_is_writable(key) if available else False
                    if not available:
                        evidence += " The setting is currently unavailable for changes."
                    elif writable is False:
                        evidence += " The setting is read-only."
                    else:
                        if collection:
                            getter = getattr(setup.experiment, "GetCurrentCapabilities", None)
                            supported = getter(key) if callable(getter) else None
                            if supported is not None and value not in list(supported):
                                raise RuntimeError(f"LightField {mode} is unsupported by this experiment. {_ADVICE}")
                        valid = getattr(setup.experiment, "IsValid", None)
                        if callable(valid) and not valid(key, value):
                            raise RuntimeError(f"LightField {mode} value is unsupported for {member}. {_ADVICE}")
                        remaining()  # SDK probes must not start a write after deadline.
                        # Custom selection can appear while readiness was being
                        # awaited; authorization covers only the reviewed image.
                        if mode == "bin_all":
                            current = snapshot()
                            if current["selection"] == "CustomRegions" and current["fingerprint"] not in (
                                    intent.get("expected_fingerprint"), intent.get("allow_custom")):
                                raise RuntimeError(f"Custom LightField ROI changed before Bin All; review again. {_ADVICE}")
                        remaining()
                        try:
                            setup.experiment.SetValue(key, value)
                        except Exception as exc:
                            raise RuntimeError(f"Cannot set LightField {mode} {member}. {_ADVICE}") from exc
                        changed, wrote = True, True
                        continue
            else:
                # No amount of waiting for write availability can create a
                # missing camera capability in an otherwise idle experiment.
                if ready():
                    raise RuntimeError(f"LightField {member} is unavailable for this camera/experiment. {_ADVICE}")
                evidence = f"{member} is unavailable while LightField is not ready."
            pause()

    target = getattr(selections, "LineSensor" if mode == "bin_all" else "FullSensor")
    write_verified("ReadoutControlRegionsOfInterestSelection", target, collection=True)
    if mode == "bin_all":
        write_verified("ReadoutControlRegionsOfInterestLineSensorRowBinning", integer_type(initial["sensor_height"]))
    phase = "verifying the sensor regions and binning"
    while True:
        actual = snapshot()
        if actual["provider"] != initial["provider"]:
            raise RuntimeError(f"LightField binning provider changed during preparation. {_ADVICE}")
        try:
            verify_sensor_readout(actual, intent)
        except RuntimeError as exc:
            evidence = str(exc).replace(_ADVICE, "").strip()
            evidence += (f" Readback: selection={actual['selection']}; "
                         f"sensor={actual['sensor_width']}x{actual['sensor_height']}; "
                         f"rows binned={actual['row_binning']}; regions={actual['regions']}.")
            pause()
        else:
            if ready():
                remaining()
                return dict(actual, mode=mode, changed=changed)
            evidence = "Sensor geometry matches, but LightField is not ready or is busy (check Preview/Acquire)."
            pause()
