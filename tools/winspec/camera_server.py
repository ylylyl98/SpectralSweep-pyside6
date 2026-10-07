from __future__ import print_function

"""WinSpec camera bridge for Windows XP, Python 2.7, and pywin32.

The server intentionally uses ExpSetup/DocFile only.  It never creates a
SpectroObj, so LightField can remain the sole owner of the spectrometer.
"""

import json
import glob
import math
import os
import socket
import struct
import threading
import time
import traceback
from collections import deque

import pythoncom
import SocketServer
import win32com.client
import win32com.client.dynamic
from temperature_guard import (TemperatureGuard, validate_temperature,
                               POLL_SECONDS, VERSION as TEMPERATURE_GUARD_VERSION)
from acquisition_owner import Owner
from stop_owner_guard import confirmed_stop as owned_stop


HOST = "0.0.0.0"
PORT = 5000
SPE_PATH = r"C:\WinSpecRemote\remote_frame.spe"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 256 * 1024 * 1024
MIN_ACQUISITION_WATCHDOG_S = 30.0
MAX_ACQUISITION_WATCHDOG_S = 24.0 * 60.0 * 60.0
PROTOCOL_VERSION = 1
SERVER_BUILD = "2026-10-05-combined-start-acceleration-v14"
ACQUISITION_SETTINGS_VERSION = 2
REQUEST_HEADER = struct.Struct("<4sHI")
RESPONSE_HEADER = struct.Struct("<4sHII")
SPE_HEADER_SIZE = 4100
DT_SPE = 1

CAMERA_LOCK = threading.Lock()
TRANSFER_STATE = threading.local()
TRANSFER_PENDING = threading.Event()
OWNER_PENDING = threading.Event()
CLEANUP_FAILED = threading.Event()
STOP_REQUESTED = threading.Event()
ACQUISITION_ACTIVE = threading.Event()
TEMPERATURE_MONITOR_UNHEALTHY = threading.Event()
STOP_IN_FLIGHT = threading.Event()
NATIVE_START_COMMITTED = threading.Event()
ACQUISITION_STATE_LOCK = threading.Lock()
START_ACCELERATOR = None
LOG_LOCK = threading.Lock()
LOG_ENTRIES = deque(maxlen=1000)
LOG_SEQUENCE = [0]
COM_TUPLE_METHODS_LOGGED = set()


def server_log(level, message):
    with LOG_LOCK:
        LOG_SEQUENCE[0] += 1
        entry = {
            "sequence": LOG_SEQUENCE[0],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": str(level).upper(),
            "message": str(message),
        }
        LOG_ENTRIES.append(entry)
    print("%s [%s] %s" % (entry["timestamp"], entry["level"], entry["message"]))


def get_log_entries(after_sequence):
    try:
        after_sequence = int(after_sequence)
    except Exception:
        after_sequence = 0
    with LOG_LOCK:
        entries = [dict(item) for item in LOG_ENTRIES
                   if item["sequence"] > after_sequence]
        latest = LOG_SEQUENCE[0]
    return entries, latest


class ProtocolError(Exception):
    pass


def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ProtocolError("connection closed during request")
        data.extend(chunk)
    return bytes(data)


def const(name):
    try:
        return getattr(win32com.client.constants, name)
    except Exception:
        return None


def com_return_value(result, method_name):
    """Return a COM method's retval when pywin32 also returns out parameters."""
    if isinstance(result, (tuple, list)):
        if not result:
            raise RuntimeError("WinSpec %s returned an empty result" % method_name)
        if method_name not in COM_TUPLE_METHODS_LOGGED:
            COM_TUPLE_METHODS_LOGGED.add(method_name)
            server_log(
                "INFO",
                "WinSpec %s returns %d values on this installation; "
                "using return value at index 0" % (method_name, len(result)),
            )
        return result[0]
    return result


def get_param(exp, enum_value):
    result = exp.GetParam(enum_value)
    value = com_return_value(result, "GetParam")
    if isinstance(result, (tuple, list)) and len(result) > 1:
        status = result[1]
        if status not in (None, 0, False):
            raise RuntimeError("WinSpec GetParam failed with status %r" % status)
    if value is None:
        raise RuntimeError("WinSpec GetParam returned no value")
    return value


PARAMETERS = {
    "exposure_ms": ("EXP_EXPOSURE", lambda value: float(value) / 1000.0,
                    lambda value: float(value) * 1000.0),
    "accumulations": ("EXP_ACCUMS", int, int),
    "sequential_frames": ("EXP_SEQUENTS", int, int),
    "temperature_setpoint_c": ("EXP_TEMPERATURE", float, float),
    "adc_rate": ("EXP_ADC_RATE", int, int),
    "controller_gain": ("EXP_GAIN", int, int),
    "timing_mode": ("EXP_TIMING_MODE", int, int),
    "shutter_control": ("EXP_SHUTTER_CONTROL", int, int),
}

# These values are documented by the Automation manual, but hardware-reported
# ValidRange.AvailValues takes precedence. They are only compatibility fallbacks
# for old controllers that expose a working setting without a ValidRange object.
DOCUMENTED_ENUM_VALUES = {
    "timing_mode": (1, 3),              # free run, external sync
    "shutter_control": (1, 2, 3),       # normal, closed, open
}

# These controls are documented as settable and have already been verified on
# legacy WinSpec releases that do not marshal ValidRange through Python.
LEGACY_ROUNDTRIP_PARAMETERS = (
    "exposure_ms", "accumulations", "sequential_frames",
    "temperature_setpoint_c", "timing_mode", "shutter_control",
)
ENUM_RANGE_REQUIRED_PARAMETERS = ("adc_rate", "controller_gain")

ROI_KEYS = (
    "roi_enabled", "roi_x_start", "roi_x_end", "roi_y_start", "roi_y_end",
    "roi_x_group", "roi_y_group",
)

READ_ONLY_PARAMETERS = {
    "actual_exposure_ms": ("EXP_ETACTUAL", lambda value: float(value) * 1000.0),
    "actual_temperature_c": ("EXP_ACTUAL_TEMP", float),
    "temperature_locked": ("EXP_TEMP_STATUS", bool),
    "controller_alive": ("EXP_CONTROLLER_ALIVE", bool),
    "winspec_reported_running": ("EXP_RUNNING_EXPERIMENT", bool),
    "controller_running": ("EXP_RUNNING", bool),
    "adc_type": ("EXP_ADC_TYPE", int),
    "analog_gain": ("EXP_ANALOG_GAIN", int),
    "readout_time_s": ("EXP_READOUT_TIME", float),
    "detector_width": ("EXP_XDIMDET", int),
    "detector_height": ("EXP_YDIMDET", int),
    "output_width": ("EXP_XDIM", int),
    "output_height": ("EXP_YDIM", int),
}

STATUS_PARAMETERS = {
    "actual_temperature_c": ("EXP_ACTUAL_TEMP", float),
    "temperature_locked": ("EXP_TEMP_STATUS", bool),
    "controller_alive": ("EXP_CONTROLLER_ALIVE", bool),
    "winspec_reported_running": ("EXP_RUNNING_EXPERIMENT", bool),
    "controller_running": ("EXP_RUNNING", bool),
}


def create_experiment():
    # Generate enum bindings, but use the scripting-friendly dynamic wrapper
    # for the actual object. WinSpec's Automation manual documents Start2 and
    # IsAvail2 specifically for scripting clients.
    generated = win32com.client.gencache.EnsureDispatch("WinX32.ExpSetup")
    # Rewrap this request's interface instead of resolving its ProgID twice.
    # No COM interface is cached or shared between request apartments.
    return win32com.client.dynamic.Dispatch(generated._oleobj_)


def valid_range(exp, enum_value):
    """Return WinSpec's ValidRange object when the parameter is available."""
    if enum_value is None:
        return None
    try:
        result = exp.IsAvail2(enum_value)
        candidates = result if isinstance(result, (tuple, list)) else (result,)
        for index, candidate in enumerate(candidates):
            if candidate is None:
                continue
            try:
                getattr(candidate, "CurrentValue")
                getattr(candidate, "ReadOrWrite")
                if len(candidates) > 1 and "IsAvail2" not in COM_TUPLE_METHODS_LOGGED:
                    COM_TUPLE_METHODS_LOGGED.add("IsAvail2")
                    server_log(
                        "INFO",
                        "WinSpec IsAvail2 returned %d values; using ValidRange "
                        "at index %d" % (len(candidates), index),
                    )
                return candidate
            except Exception:
                pass
    except Exception:
        pass
    try:
        # Older type-library wrappers may not marshal the optional ValidRange
        # output, but the Boolean result still establishes availability.
        if com_return_value(exp.IsAvail(enum_value), "IsAvail"):
            return True
    except Exception:
        pass
    return None


def is_readable(exp, enum_value):
    if enum_value is None:
        return False
    # IsAvail alone is not sufficient: some legacy drivers report a parameter
    # available but return None/error status from GetParam in the current mode.
    try:
        com_return_value(exp.IsAvail(enum_value), "IsAvail")
    except Exception:
        pass
    try:
        read_parameter(exp, enum_value)
        return True
    except Exception:
        return False


def reports_available(exp, enum_value):
    if enum_value is None:
        return False
    try:
        return bool(com_return_value(exp.IsAvail(enum_value), "IsAvail"))
    except Exception:
        return False


def range_value(range_object, name, default=None):
    if range_object is None or range_object is True:
        return default
    try:
        value = getattr(range_object, name)
        return value() if callable(value) else value
    except Exception:
        return default


def parameter_write_access(key, parameter_range, readable):
    """Return (writable, source) without confusing readable with writable."""
    access = range_value(parameter_range, "ReadOrWrite")
    try:
        access = int(access)
    except Exception:
        access = None
    if access == 2:  # WINX32Lib.EXP_READ_N_WRITE
        return True, "valid_range_read_write"
    if access in (0, 1, 3):
        return False, "read_only"
    if parameter_range is True:
        # IsAvail succeeded but this old wrapper did not marshal ValidRange.
        return True, "is_avail"
    if key in LEGACY_ROUNDTRIP_PARAMETERS and readable:
        return True, "documented_roundtrip"
    return False, "unavailable"


def read_parameter(exp, enum_value):
    """Read a parameter, falling back to documented ValidRange.CurrentValue."""
    try:
        return get_param(exp, enum_value)
    except Exception as get_error:
        parameter_range = valid_range(exp, enum_value)
        current = range_value(parameter_range, "CurrentValue")
        if current is not None:
            if "ValidRange.CurrentValue" not in COM_TUPLE_METHODS_LOGGED:
                COM_TUPLE_METHODS_LOGGED.add("ValidRange.CurrentValue")
                server_log(
                    "INFO",
                    "Using ValidRange.CurrentValue when GetParam provides no value",
                )
            return current
        raise RuntimeError("parameter read failed: %s" % get_error)


def range_metadata(range_object, from_winx):
    if range_object is None or range_object is True:
        return {}
    result = {}
    for source, target in (
            ("MinValue", "minimum"), ("MaxValue", "maximum"),
            ("DefaultValue", "default"), ("CurrentValue", "current"),
            ("Increment", "increment")):
        value = range_value(range_object, source)
        if value is not None:
            try:
                result[target] = from_winx(value)
            except Exception:
                pass
    values = range_value(range_object, "AvailValues")
    if values is not None:
        try:
            result["values"] = [from_winx(item) for item in list(values)]
        except Exception:
            pass
    return result


def capabilities(exp):
    result = {
        "acquire": True,
        "logs": True,
        "stop": True,
        "calibration": False,
        "roi": False,
    }
    ranges = {}
    sources = {}
    for key, definition in PARAMETERS.items():
        enum_value = const(definition[0])
        parameter_range = valid_range(exp, enum_value)
        readable = is_readable(exp, enum_value)
        writable, source = parameter_write_access(
            key, parameter_range, readable)
        result[key] = writable
        sources[key] = source
        if parameter_range is not None and parameter_range is not True:
            metadata = range_metadata(parameter_range, definition[2])
            if metadata:
                ranges[key] = metadata
        if key not in ranges and key in DOCUMENTED_ENUM_VALUES and writable:
            ranges[key] = {
                "values": list(DOCUMENTED_ENUM_VALUES[key]),
            }
        if key in ENUM_RANGE_REQUIRED_PARAMETERS and writable:
            metadata = ranges.get(key, {})
            legal = metadata.get("values")
            if not legal:
                minimum = metadata.get("minimum")
                maximum = metadata.get("maximum")
                increment = metadata.get("increment", 1)
                try:
                    minimum = int(minimum)
                    maximum = int(maximum)
                    increment = max(1, int(increment))
                    if 0 <= maximum - minimum <= 32:
                        legal = list(range(minimum, maximum + 1, increment))
                        metadata["values"] = legal
                        ranges[key] = metadata
                except Exception:
                    legal = None
            if not legal:
                result[key] = False
                sources[key] = "missing_legal_values"
    result["exposure"] = result.pop("exposure_ms", False)
    result["temperature"] = (
        result.get("temperature_setpoint_c", False) or
        is_readable(exp, const("EXP_ACTUAL_TEMP"))
    )
    roi_parameter = const("EXP_USEROI")
    roi_range = valid_range(exp, roi_parameter)
    roi_readable = is_readable(exp, roi_parameter)
    roi_available, roi_source = parameter_write_access(
        "roi_enabled", roi_range, roi_readable)
    # SetROI is a documented ExpSetup mutator even on builds that do not
    # publish a ValidRange for EXP_USEROI.
    if not roi_available and roi_readable:
        roi_available, roi_source = True, "documented_setroi"
    result["roi"] = roi_available
    sources["roi"] = roi_source
    if roi_available:
        try:
            detector_width = int(read_parameter(exp, const("EXP_XDIMDET")))
            detector_height = int(read_parameter(exp, const("EXP_YDIMDET")))
            ranges.update({
                "roi_x_start": {"minimum": 1, "maximum": detector_width,
                                "increment": 1},
                "roi_x_end": {"minimum": 1, "maximum": detector_width,
                              "increment": 1},
                "roi_y_start": {"minimum": 1, "maximum": detector_height,
                                "increment": 1},
                "roi_y_end": {"minimum": 1, "maximum": detector_height,
                              "increment": 1},
                "roi_x_group": {"minimum": 1, "maximum": detector_width,
                                "increment": 1},
                "roi_y_group": {"minimum": 1, "maximum": detector_height,
                                "increment": 1},
            })
        except Exception:
            pass
    result["setting_ranges"] = ranges
    result["capability_sources"] = sources
    return result


def generated_dispatch_view(obj):
    """Return the makepy wrapper for an existing COM object when possible."""
    try:
        return win32com.client.Dispatch(obj._oleobj_)
    except Exception:
        return obj


def roi_member(roi, *names):
    """Read a ROIRect member across case-sensitive pywin32 wrappers."""
    for name in names:
        try:
            return getattr(roi, name)
        except AttributeError:
            pass
        getter = getattr(roi, "get_" + name, None)
        if callable(getter):
            return getter()
    raise AttributeError("ROIRect has none of: %s" % ", ".join(names))


def roi_geometry(roi):
    """Translate ROIRect's top/left/bottom/right to detector coordinates."""
    return {
        "roi_x_start": int(roi_member(roi, "left", "Left")),
        "roi_x_end": int(roi_member(roi, "right", "Right")),
        "roi_y_start": int(roi_member(roi, "top", "Top")),
        "roi_y_end": int(roi_member(roi, "bottom", "Bottom")),
        "roi_x_group": int(roi_member(roi, "XGroup", "xgroup")),
        "roi_y_group": int(roi_member(roi, "YGroup", "ygroup")),
    }


def roi_objects(result):
    """Yield ROIRect objects from pywin32's direct or tuple return forms."""
    candidates = result if isinstance(result, (tuple, list)) else (result,)
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            roi_geometry(candidate)
            yield candidate
        except Exception:
            pass


def get_roi_candidates(exp):
    """Read every plausible first ROI across legacy WinSpec index conventions."""
    candidates = []
    seen = set()
    generated = generated_dispatch_view(exp)
    apis = (generated, exp) if generated is not exp else (exp,)
    # The manual text says indices are one-based, but its own working example
    # calls GetROI(0).  WinSpec 2.x installations exist with either behavior.
    for api in apis:
        for index in (0, 1):
            try:
                result = api.GetROI(index)
            except Exception:
                continue
            for roi in roi_objects(result):
                geometry = roi_geometry(roi)
                signature = tuple(geometry[key] for key in ROI_KEYS[1:])
                if signature in seen:
                    continue
                seen.add(signature)
                candidates.append((index, roi, geometry))
    return candidates


def get_first_roi(exp, expected=None):
    candidates = get_roi_candidates(exp)
    if expected is not None:
        for index, roi, geometry in candidates:
            if all(geometry.get(key) == value
                   for key, value in expected.items()):
                return roi
    return candidates[0][1] if candidates else None


def read_roi(exp, expected=None):
    useroi = const("EXP_USEROI")
    if not is_readable(exp, useroi):
        return {}
    result = {"roi_enabled": bool(read_parameter(exp, useroi))}
    roi_count = const("EXP_ROICOUNT")
    if roi_count is not None and is_readable(exp, roi_count):
        try:
            result["roi_count"] = int(read_parameter(exp, roi_count))
        except Exception:
            pass
    roi = get_first_roi(exp, expected)
    if roi is not None:
        try:
            # ROIRect follows screen geometry: left/right are detector X and
            # top/bottom are detector Y. Generated wrappers use lower-case
            # edge names even though the VB manual prints them capitalized.
            result.update(roi_geometry(roi))
        except Exception:
            pass
    if "roi_x_start" not in result:
        try:
            result.update({
                "roi_x_start": 1,
                "roi_x_end": int(read_parameter(exp, const("EXP_XDIMDET"))),
                "roi_y_start": 1,
                "roi_y_end": int(read_parameter(exp, const("EXP_YDIMDET"))),
                "roi_x_group": 1,
                "roi_y_group": 1,
            })
        except Exception:
            pass
    return result


def apply_roi(exp, requested):
    current = read_roi(exp)
    values = dict(current)
    values.update(requested)
    enabled = bool(values.get("roi_enabled", False))
    geometry_requested = any(key in requested for key in ROI_KEYS[1:])

    if geometry_requested or enabled:
        required = ROI_KEYS[1:]
        missing = [key for key in required if key not in values]
        if missing:
            raise ValueError("missing ROI values: %s" % ", ".join(missing))
        x_start = int(values["roi_x_start"])
        x_end = int(values["roi_x_end"])
        y_start = int(values["roi_y_start"])
        y_end = int(values["roi_y_end"])
        x_group = int(values["roi_x_group"])
        y_group = int(values["roi_y_group"])
        detector_width = int(read_parameter(exp, const("EXP_XDIMDET")))
        detector_height = int(read_parameter(exp, const("EXP_YDIMDET")))
        if not (1 <= x_start <= x_end <= detector_width):
            raise ValueError("ROI X must satisfy 1 <= start <= end <= %d" %
                             detector_width)
        if not (1 <= y_start <= y_end <= detector_height):
            raise ValueError("ROI Y must satisfy 1 <= start <= end <= %d" %
                             detector_height)
        if not (1 <= x_group <= (x_end - x_start + 1)):
            raise ValueError("ROI X group is larger than the selected width")
        if not (1 <= y_group <= (y_end - y_start + 1)):
            raise ValueError("ROI Y group is larger than the selected height")
        if (x_end - x_start + 1) % x_group:
            raise ValueError(
                "ROI X width %d is not divisible by X group %d" %
                (x_end - x_start + 1, x_group))
        if (y_end - y_start + 1) % y_group:
            raise ValueError(
                "ROI Y height %d is not divisible by Y group %d; "
                "choose a group that divides the height evenly" %
                (y_end - y_start + 1, y_group))

        try:
            roi = win32com.client.gencache.EnsureDispatch("WinX32.ROIRect")
        except Exception:
            roi = win32com.client.dynamic.Dispatch("WinX32.ROIRect")
        # IROIRect.Set(top, left, bottom, right, x_group, y_group).
        roi.Set(y_start, x_start, y_end, x_end, x_group, y_group)
        rectangle = roi_geometry(roi)
        expected_rectangle = {
            "roi_x_start": x_start, "roi_x_end": x_end,
            "roi_y_start": y_start, "roi_y_end": y_end,
            "roi_x_group": x_group, "roi_y_group": y_group,
        }
        if rectangle != expected_rectangle:
            raise RuntimeError(
                "WinSpec ROIRect.Set produced unexpected geometry: %r" %
                rectangle)

        # Passing a ROIRect through the generated wrapper preserves its COM
        # interface type.  Some WinSpec 2.x builds silently ignore the same
        # object when SetROI is invoked through a late-bound wrapper.
        roi_api = generated_dispatch_view(exp)
        roi_api.ClearROIs()
        server_log("INFO", "WinSpec ClearROIs completed (void method)")
        roi_api.SetROI(roi)
        server_log("INFO", "WinSpec SetROI completed (void method)")

    roi_api = generated_dispatch_view(exp)
    enable_result = roi_api.SetParam(
        const("EXP_USEROI"), 1 if enabled else 0)
    enable_status = com_return_value(enable_result, "SetParam")
    if enable_status not in (0, False):
        raise RuntimeError(
            "WinSpec rejected EXP_USEROI with status %r" % enable_status)

    expected = None
    if geometry_requested or enabled:
        expected = {
            "roi_x_start": x_start, "roi_x_end": x_end,
            "roi_y_start": y_start, "roi_y_end": y_end,
            "roi_x_group": x_group, "roi_y_group": y_group,
        }

    # WinSpec may update its stored ROI after SetROI returns.  Pump COM messages
    # and accept a match from either documented index convention.
    deadline = time.time() + 2.0
    while True:
        actual = read_roi(exp, expected)
        geometry_matches = (expected is None or all(
            actual.get(key) == value for key, value in expected.items()))
        enabled_matches = (
            bool(actual.get("roi_enabled", False)) == enabled)
        if geometry_matches and enabled_matches:
            break
        if time.time() >= deadline:
            break
        pythoncom.PumpWaitingMessages()
        time.sleep(0.1)

    if bool(actual.get("roi_enabled", False)) != enabled:
        raise RuntimeError("WinSpec ROI enable read-back mismatch")
    if geometry_requested or enabled:
        mismatches = [
            "%s requested %r, actual %r" %
            (key, value, actual.get(key))
            for key, value in expected.items()
            if actual.get(key) != value
        ]
        if mismatches:
            candidates = [
                "index %d: %r" % (index, geometry)
                for index, roi, geometry in get_roi_candidates(exp)
            ]
            if candidates:
                server_log("WARNING", "WinSpec ROI candidates: %s" %
                           "; ".join(candidates))
            else:
                server_log("WARNING", "WinSpec GetROI returned no ROI object")
            raise RuntimeError("WinSpec ROI read-back mismatch: %s" %
                               "; ".join(mismatches))
    server_log(
        "INFO",
        "Camera ROI %s: X %s-%s / %s, Y %s-%s / %s" % (
            "enabled" if enabled else "disabled",
            actual.get("roi_x_start", "?"), actual.get("roi_x_end", "?"),
            actual.get("roi_x_group", "?"), actual.get("roi_y_start", "?"),
            actual.get("roi_y_end", "?"), actual.get("roi_y_group", "?")),
    )
    return actual


def read_settings(exp, temperature_status=None):
    settings = {}
    for key, definition in PARAMETERS.items():
        enum_value = const(definition[0])
        if enum_value is None:
            continue
        try:
            settings[key] = definition[2](read_parameter(exp, enum_value))
        except Exception:
            pass
    for key, definition in READ_ONLY_PARAMETERS.items():
        if temperature_status is not None and key in ('actual_temperature_c', 'temperature_locked'):
            settings[key] = temperature_status[key]
            continue
        enum_value = const(definition[0])
        if enum_value is None:
            continue
        try:
            settings[key] = definition[1](read_parameter(exp, enum_value))
        except Exception:
            pass
    try:
        settings.update(read_roi(exp))
    except Exception:
        pass
    # Some WinSpec releases leave EXP_RUNNING_EXPERIMENT asserted while the
    # application is idle. Only the server-owned acquisition flag is reliable
    # for deciding whether SpectralSweep must lock camera settings.
    settings["running"] = ACQUISITION_ACTIVE.is_set()
    return settings


def read_status(exp):
    status = {}
    for key, definition in STATUS_PARAMETERS.items():
        enum_value = const(definition[0])
        if not is_readable(exp, enum_value):
            continue
        try:
            status[key] = definition[1](read_parameter(exp, enum_value))
        except Exception:
            pass
    status["running"] = ACQUISITION_ACTIVE.is_set()
    return status


def read_acquisition_settings(exp, temperature_status=None):
    """Read live frame-critical values, without optional/legacy ROI discovery.

    Full detector/no-ROI acquisition only needs the ROI enable flag and actual
    input/output dimensions. Missing safety values fail the request closed.
    """
    settings = {}
    for key, enum_name, convert in (
        ('exposure_ms', 'EXP_EXPOSURE', lambda value: float(value) * 1000.),
        ('accumulations', 'EXP_ACCUMS', int),
        ('sequential_frames', 'EXP_SEQUENTS', int),
        ('timing_mode', 'EXP_TIMING_MODE', int),
        ('detector_width', 'EXP_XDIMDET', int),
        ('detector_height', 'EXP_YDIMDET', int),
        ('output_width', 'EXP_XDIM', int),
        ('output_height', 'EXP_YDIM', int),
        ('roi_enabled', 'EXP_USEROI', bool),
        ('actual_temperature_c', 'EXP_ACTUAL_TEMP', float),
        ('temperature_locked', 'EXP_TEMP_STATUS', bool),
    ):
        if temperature_status is not None and key in ('actual_temperature_c', 'temperature_locked'):
            settings[key] = temperature_status[key]
            continue
        enum_value = const(enum_name)
        if enum_value is None:
            raise RuntimeError('Required acquisition setting unavailable: ' + key)
        settings[key] = convert(read_parameter(exp, enum_value))
    for key, enum_name, convert in (
        ('readout_time_s', 'EXP_READOUT_TIME', float),
        ('adc_rate', 'EXP_ADC_RATE', int),
        ('controller_gain', 'EXP_GAIN', int),
    ):
        enum_value = const(enum_name)
        if enum_value is not None:
            try:
                settings[key] = convert(read_parameter(exp, enum_value))
            except Exception:
                pass
    settings['running'] = ACQUISITION_ACTIVE.is_set()
    return settings


def read_temperature_status(exp):
    # Do not query unrelated controller state for temperature display/checks.
    return dict(actual_temperature_c=float(read_parameter(exp, const('EXP_ACTUAL_TEMP'))),
                temperature_locked=bool(read_parameter(exp, const('EXP_TEMP_STATUS'))))


def validate_acquisition_request(expected):
    """Validate the already-applied managed recipe without controller queries."""
    if not isinstance(expected, dict):
        raise RuntimeError('Managed acquisition requires verified expected settings')
    if (expected.get('detector_width'), expected.get('detector_height'),
            expected.get('output_width'), expected.get('output_height')) != (512, 1, 512, 1) or expected.get('roi_enabled') is not False:
        raise RuntimeError('WinSpec acquisition geometry requires full 512 x 1 without ROI')
    if expected.get('timing_mode') != 1 or expected.get('sequential_frames') != 1:
        raise RuntimeError('WinSpec acquisition requires Free Run and one sequential frame')
    for key in ('exposure_ms', 'accumulations'):
        value = expected.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or math.isnan(value) or math.isinf(value) or value <= 0
                or (key == 'accumulations' and int(value) != value)):
            raise RuntimeError('Invalid managed acquisition setting: ' + key)
    return dict(expected)


def settings_from_spe(expected, hardware, width, height, frames, temperature_status):
    """Use the acquired file's observations, rather than rereading camera COM.

    SPE 2.x layout: WinSpec/32 manual Appendix C (exp_sec=10,
    lavgexp=668, NumExpAccums=1422). No offsets are inferred from live state.
    """
    if (width, height, frames) != (512, 1, 1):
        raise RuntimeError('Acquired SPE geometry differs from managed request')
    for key in ('exposure_ms', 'accumulations'):
        actual = hardware.get(key)
        if (isinstance(actual, bool) or not isinstance(actual, (int, float))
                or math.isnan(actual) or math.isinf(actual) or actual <= 0
                or (key == 'accumulations' and (int(actual) != actual or actual != expected[key]))
                or (key == 'exposure_ms' and
                    abs(float(actual) - float(expected[key])) > max(1e-6, abs(float(expected[key])) * 1e-6))):
            raise RuntimeError('Acquired SPE %s differs from managed request' % key)
    for key in ('adc_rate', 'controller_gain'):
        if key in expected and hardware.get(key) != expected[key]:
            raise RuntimeError('Acquired SPE %s differs from managed request' % key)
    settings = dict(expected)
    settings.update(hardware)
    settings.update(temperature_status)
    settings.update(output_width=width, output_height=height, sequential_frames=frames, running=False)
    return settings


def apply_settings(exp, requested):
    if not isinstance(requested, dict):
        raise ValueError("settings must be a JSON object")
    roi_requested = {key: value for key, value in requested.items()
                     if key in ROI_KEYS}
    normal_requested = {key: value for key, value in requested.items()
                        if key not in ROI_KEYS}
    for key, value in normal_requested.items():
        if key not in PARAMETERS:
            raise ValueError("unknown or read-only camera setting: %s" % key)
        enum_name, to_winx, from_winx = PARAMETERS[key]
        enum_value = const(enum_name)
        parameter_range = valid_range(exp, enum_value)
        readable = is_readable(exp, enum_value)
        writable, access_source = parameter_write_access(
            key, parameter_range, readable)
        if not writable:
            raise ValueError("camera does not support %s" % key)
        converted = to_winx(value)
        if isinstance(converted, float) and (math.isnan(converted) or
                                             math.isinf(converted)):
            raise ValueError("%s must be a finite number" % key)

        minimum = range_value(parameter_range, "MinValue")
        maximum = range_value(parameter_range, "MaxValue")
        available_values = range_value(parameter_range, "AvailValues")
        documented_values = DOCUMENTED_ENUM_VALUES.get(key)
        legal_values = []
        if available_values is not None:
            try:
                legal_values = list(available_values)
            except Exception:
                legal_values = []
        if legal_values:
            if converted not in legal_values:
                raise ValueError("%s is not one of WinSpec's legal values %r" %
                                 (key, [from_winx(item) for item in legal_values]))
        elif documented_values is not None:
            if converted not in documented_values:
                raise ValueError("%s must be one of %r" %
                                 (key, list(documented_values)))
        else:
            if minimum is not None and converted < minimum:
                raise ValueError("%s is below WinSpec minimum %r" %
                                 (key, from_winx(minimum)))
            if maximum is not None and converted > maximum:
                raise ValueError("%s is above WinSpec maximum %r" %
                                 (key, from_winx(maximum)))
        result = exp.SetParam(enum_value, converted)
        result_value = com_return_value(result, "SetParam")
        if result_value not in (0, False):
            raise RuntimeError(
                "%s SetParam failed with status %r" % (key, result_value))
        try:
            actual = from_winx(read_parameter(exp, enum_value))
        except Exception as exc:
            raise RuntimeError("%s read-back failed: %s" % (key, exc))
        requested_value = from_winx(converted)
        if isinstance(actual, float) or isinstance(requested_value, float):
            increment = range_value(parameter_range, "Increment", 0.0)
            try:
                increment_in_app_units = abs(
                    float(from_winx(increment)) - float(from_winx(0.0)))
            except Exception:
                increment_in_app_units = 0.0
            tolerance = max(1e-9, increment_in_app_units * 0.51,
                            abs(float(requested_value)) * 1e-6)
            if abs(float(actual) - float(requested_value)) > tolerance:
                raise RuntimeError("%s read-back mismatch: requested %r, actual %r" %
                                   (key, requested_value, actual))
        elif actual != requested_value:
            raise RuntimeError("%s read-back mismatch: requested %r, actual %r" %
                               (key, requested_value, actual))
        server_log(
            "INFO",
            "Camera setting %s applied: %r (access=%s, SetParam=%r)" %
            (key, actual, access_source, result),
        )
    if roi_requested:
        roi_parameter = const("EXP_USEROI")
        if not is_readable(exp, roi_parameter):
            raise ValueError("camera does not support ROI settings")
        apply_roi(exp, roi_requested)
    settings = read_settings(exp)
    return settings


def read_spe_frames(path):
    handle = open(path, "rb")
    try:
        header = handle.read(SPE_HEADER_SIZE)
        if len(header) != SPE_HEADER_SIZE:
            raise RuntimeError("truncated SPE header")
        width = struct.unpack_from("<H", header, 42)[0]
        height = struct.unpack_from("<H", header, 656)[0]
        datatype = struct.unpack_from("<h", header, 108)[0]
        frames = struct.unpack_from("<l", header, 1446)[0]
        sizes = {0: 4, 1: 4, 2: 2, 3: 2, 4: 1, 5: 8, 6: 1, 7: 1}
        if width < 1 or height < 1 or frames < 1 or datatype not in sizes:
            raise RuntimeError("invalid SPE frame header")
        payload_bytes = width * height * frames * sizes[datatype]
        if payload_bytes > MAX_FRAME_BYTES:
            raise RuntimeError("SPE payload exceeds configured size limit")
        raw = handle.read(payload_bytes)
        if len(raw) != payload_bytes:
            raise RuntimeError("truncated SPE data")
        hardware = {
            'exposure_ms': struct.unpack_from('<f', header, 10)[0] * 1000.,
            'accumulations': (struct.unpack_from('<I', header, 1422)[0]
                              or struct.unpack_from('<l', header, 668)[0]),
            'spe_timing_mode': struct.unpack_from('<h', header, 8)[0],
            "adc_rate": struct.unpack_from("<H", header, 190)[0],
            "adc_type": struct.unpack_from("<H", header, 192)[0],
            "controller_gain": struct.unpack_from("<H", header, 198)[0],
            "controller_type": struct.unpack_from("<h", header, 704)[0],
            "analog_gain": struct.unpack_from("<h", header, 4092)[0],
            "readout_time_s": struct.unpack_from("<f", header, 672)[0],
        }
        return width, height, frames, datatype, raw, hardware
    finally:
        handle.close()


def unique_spe_path():
    base, extension = os.path.splitext(SPE_PATH)
    return "%s_%d_%s%s" % (
        base,
        int(time.time() * 1000),
        threading.current_thread().ident,
        extension,
    )


def remove_if_unlocked(path, attempts=1):
    last_error = None
    for attempt in range(max(1, attempts)):
        try:
            if os.path.exists(path):
                os.remove(path)
                server_log("INFO", "Removed temporary SPE file %s" % path)
            return True
        except OSError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.1)
    server_log("WARNING", "Temporary SPE remains locked: %s (%s)" %
               (path, last_error))
    return False


def disable_acquisition_autosave(exp):
    key = const('EXP_AUTOSAVE')
    if com_return_value(exp.SetParam(key, False), 'SetParam') != 0:
        raise RuntimeError('Cannot disable WinSpec automatic file saving')
    if bool(read_parameter(exp, key)):
        raise RuntimeError('WinSpec automatic file saving remains enabled')


def read_acquisition_display(exp):
    """Read only the optional UI flag, without capability/ValidRange queries."""
    key = const('EXP_BSHOWWINDOW')
    if key is None:
        return None
    try:
        value = get_param(exp, key)
        if value not in (-1, 0, 1):
            raise RuntimeError('Unsupported display Boolean %r' % value)
        return key, value
    except Exception as exc:
        server_log('INFO', 'WinSpec display optimization unavailable: %s' % exc)
        return None


def set_acquisition_display(exp, key, value):
    status = com_return_value(exp.SetParam(key, value), 'SetParam')
    if status not in (0, False):
        raise RuntimeError('WinSpec display SetParam failed with status %r' % status)
    actual = get_param(exp, key)
    if actual not in (-1, 0, 1) or bool(actual) != bool(value):
        raise RuntimeError('WinSpec display setting readback failed')


def create_document():
    # A new, empty DocFile is explicitly supplied to Start, as documented in
    # WinX32 Automation p16. Never use GetDocument/the active user document.
    return win32com.client.DispatchEx("WinX32.DocFile")


def owner_recovery_required():
    return (TEMPERATURE_MONITOR_UNHEALTHY.is_set() or CLEANUP_FAILED.is_set() or
            (START_ACCELERATOR is not None and START_ACCELERATOR.failed))


def require_idle_owner():
    if owner_recovery_required():raise RuntimeError('WinSpec recovery required; retain owner and restart bridge after recovery')
    if TRANSFER_PENDING.is_set():raise RuntimeError('Previous SPE transfer pending; wait for receipt and cleanup')
    if OWNER_PENDING.is_set():raise RuntimeError('Previous request still owns acquisition cleanup')
    if STOP_IN_FLIGHT.is_set():raise RuntimeError('WinSpec Stop is still pending')


def confirmed_stop(exp):
    STOP_IN_FLIGHT.set()
    try:return owned_stop(exp,TEMPERATURE_MONITOR_UNHEALTHY,com_return_value)
    finally:
        if not TEMPERATURE_MONITOR_UNHEALTHY.is_set():STOP_IN_FLIGHT.clear()


def wait_for_stop_completion():
    clock=getattr(time,'monotonic',None) or time.clock
    deadline=clock()+3.
    while STOP_IN_FLIGHT.is_set() and not TEMPERATURE_MONITOR_UNHEALTHY.is_set() and clock()<deadline:
        time.sleep(.01)
    if STOP_IN_FLIGHT.is_set() or TEMPERATURE_MONITOR_UNHEALTHY.is_set():
        TEMPERATURE_MONITOR_UNHEALTHY.set()
        raise RuntimeError('WinSpec Stop completion uncertain; owner retained')


def rejected_capture_stop(exp):
    # A user Stop may have run in the tiny interval before COM Start entered.
    # Reconfirm after Start returns, without overlapping an earlier Stop.
    wait_for_stop_completion()
    result=confirmed_stop(exp)
    mark_native_complete()
    return result


def mark_native_start():
    with ACQUISITION_STATE_LOCK:
        if STOP_REQUESTED.is_set():raise RuntimeError('WinSpec acquisition stopped before Start')
        NATIVE_START_COMMITTED.set()


def mark_native_complete():
    with ACQUISITION_STATE_LOCK:NATIVE_START_COMMITTED.clear()
    wait_for_stop_completion()


def get_accelerator():
    global START_ACCELERATOR
    if START_ACCELERATOR is None:
        from start_acceleration import Manager
        START_ACCELERATOR=Manager(os.path.join(os.path.dirname(SPE_PATH),'start-acceleration'))
    return START_ACCELERATOR


def start_new_document(exp, timings=None):
    clock = (getattr(time, 'monotonic', None) or time.clock) if timings is not None else None
    phase_started = clock() if clock is not None else None
    doc = create_document()
    owner=getattr(exp,'acquisition_owner',None)
    if owner is not None:owner.retain(doc)
    if timings is not None:
        timings['create_document'] = clock() - phase_started
        phase_started = clock()
    result = exp.Start(doc)
    if timings is not None:
        timings['start_experiment'] = clock() - phase_started
    if not com_return_value(result, "Start"):
        raise RuntimeError('WinSpec could not start acquisition into a new document')
    # Dynamic pywin32 returns the updated by-reference DocFile as an output.
    if isinstance(result, (tuple, list)) and len(result) > 1:
        return start2_document(result[1:])
    return doc


def start2_document(result):
    """Extract DocFile from pywin32's direct or multi-out Start2 result."""
    candidates = result if isinstance(result, (tuple, list)) else (result,)
    for index, candidate in enumerate(candidates):
        if candidate is None:
            continue
        try:
            save_as = getattr(candidate, "SaveAs")
            close = getattr(candidate, "Close")
            if callable(save_as) and callable(close):
                if len(candidates) > 1:
                    server_log(
                        "INFO",
                        "WinSpec Start2 returned %d values; using DocFile at index %d" %
                        (len(candidates), index),
                    )
                return candidate
        except Exception:
            pass
    result_types = [type(item).__name__ for item in candidates]
    raise RuntimeError(
        "WinSpec Start2 returned no DocFile (types=%s)" % result_types)


def acquisition_watchdog_timeout(exp, settings=None):
    """Calculate a generous finite limit for one complete experiment."""
    def value(name, default):
        try:
            if settings is not None:
                keys = {'EXP_EXPOSURE': 'exposure_ms', 'EXP_ACCUMS': 'accumulations',
                        'EXP_SEQUENTS': 'sequential_frames', 'EXP_READOUT_TIME': 'readout_time_s',
                        'EXP_TIMING_MODE': 'timing_mode'}
                result = float(settings.get(keys[name], default))
                return result / 1000. if name == 'EXP_EXPOSURE' else result
            return float(read_parameter(exp, const(name)))
        except Exception:
            return float(default)

    exposure_s = max(0.0, value("EXP_EXPOSURE", 0.0))
    accumulations = max(1.0, value("EXP_ACCUMS", 1.0))
    frames = max(1.0, value("EXP_SEQUENTS", 1.0))
    readout_s = max(0.0, value("EXP_READOUT_TIME", 0.0))
    timing_mode = int(value("EXP_TIMING_MODE", 1.0))
    expected_s = frames * (exposure_s * accumulations + readout_s)
    timeout_s = max(MIN_ACQUISITION_WATCHDOG_S,
                    expected_s * 1.5 + 15.0)
    # External-sync experiments may legitimately wait a long time for a trigger.
    if timing_mode == 3:
        timeout_s = MAX_ACQUISITION_WATCHDOG_S
    return min(timeout_s, MAX_ACQUISITION_WATCHDOG_S)


def acquisition_watchdog(timeout_s, finished, timed_out, temperature_result):
    clock = getattr(time, 'monotonic', None) or time.clock
    started = clock()
    while not finished.wait(0.1):
        if clock() - started > timeout_s:
            break
    else:
        return
    timed_out.set()
    pythoncom.CoInitialize()
    watchdog_exp = None
    try:
        watchdog_exp = create_experiment()
        result = confirmed_stop(watchdog_exp)
        server_log(
            "ERROR",
            "Acquisition watchdog stopped WinSpec after %.1f s (result=%r)" %
            (timeout_s, result),
        )
    except Exception as exc:
        temperature_result['error']=str(exc)
        server_log("ERROR", "Acquisition watchdog could not stop WinSpec: %s" % exc)
    finally:
        # Keep this watchdog's COM proxy in its own apartment after uncertain Stop.
        while TEMPERATURE_MONITOR_UNHEALTHY.is_set():time.sleep(1)
        watchdog_exp = None
        pythoncom.CoUninitialize()


def wait_for_guarded_frame(exp, doc, guard, timed_out, result, expected_frames):
    """Wait without any temperature/status/document polling during exposure."""
    if STOP_REQUESTED.is_set():
        raise RuntimeError('WinSpec acquisition was stopped by request')
    completed = com_return_value(exp.WaitForExperiment(), 'WaitForExperiment')
    if STOP_REQUESTED.is_set() or timed_out.is_set():
        raise RuntimeError('WinSpec acquisition was stopped or timed out')
    if not completed:
        raise RuntimeError('WinSpec acquisition did not complete')


def acquire_active(exp, compact_settings=False, expected_settings=None):
    if TRANSFER_PENDING.is_set():
        raise RuntimeError('Previous SPE transfer is still pending; wait for cleanup')
    if CLEANUP_FAILED.is_set():
        raise RuntimeError('Previous SPE transfer/cleanup failed; recover retained data and restart bridge')
    started = time.time()
    clock = getattr(time, 'monotonic', None) or time.clock
    timing_started = clock()
    timings = {}
    phase_started = clock()
    server_log("INFO", "Starting WinSpec camera acquisition")
    frame_path = unique_spe_path()
    timeout_s = (acquisition_watchdog_timeout(exp, settings=expected_settings)
                 if compact_settings else acquisition_watchdog_timeout(exp))
    watchdog_finished = threading.Event()
    watchdog_timed_out = threading.Event()
    temperature_result = {}
    # A dedicated new document avoids prompting to replace an old Untitled.
    doc = None
    result_doc = None
    watchdog = threading.Thread(
        target=acquisition_watchdog,
        args=(timeout_s, watchdog_finished, watchdog_timed_out, temperature_result),
    )
    watchdog.daemon = True
    watchdog.start()
    guard = TemperatureGuard(lambda: read_temperature_status(exp), lambda: confirmed_stop(exp), boundary_only=True)
    temperature_result['guard'] = guard
    old_autosave = None
    old_display = None
    data_window_hidden = False
    try:
        old_autosave = bool(read_parameter(exp, const('EXP_AUTOSAVE')))
        disable_acquisition_autosave(exp)
        guard.check()
        if compact_settings:
            preflight = validate_acquisition_request(expected_settings)
            expected_frames = preflight['sequential_frames']
        else:
            expected_frames = int(read_parameter(exp, const('EXP_SEQUENTS')))
        if expected_frames != 1:
            raise RuntimeError('Guarded WinSpec acquisition requires one sequential frame')
        if compact_settings:
            display_started = clock()
            old_display = read_acquisition_display(exp)
            if old_display is not None:
                # Save the original before writing, including a failed/partial
                # write, so the finally block can restore it on rejection.
                if bool(old_display[1]):
                    set_acquisition_display(exp, old_display[0], 0)
                data_window_hidden = True
            timings['display_prepare'] = clock() - display_started
        timings['prepare'] = clock() - phase_started
        phase_started = clock()
        doc = start_new_document(exp, timings=timings)
        timings['start_document'] = clock() - phase_started
        phase_started = clock()
        wait_for_guarded_frame(exp, doc, guard, watchdog_timed_out,
                               temperature_result, expected_frames)
        mark_native_complete()
        timings['exposure_wait'] = clock() - phase_started
        phase_started = clock()
        guard.check()  # Exposure is complete; reject data if the final check fails.
        temperature_report = guard.report()
        # Total acquisition timeout remains active through the final read.
        watchdog_finished.set()
        watchdog.join(3.0)
        if watchdog.is_alive():
            TEMPERATURE_MONITOR_UNHEALTHY.set()
            raise RuntimeError('WinSpec temperature interlock: Stop still pending; restart bridge')
        if temperature_result.get('error'):
            raise RuntimeError('WinSpec temperature interlock: ' + temperature_result['error'])
        if watchdog_timed_out.is_set():
            raise RuntimeError(
                "WinSpec acquisition exceeded %.1f second watchdog" % timeout_s)
        if STOP_REQUESTED.is_set():
            raise RuntimeError("WinSpec acquisition was stopped by request")
        timings['after_exposure'] = clock() - phase_started
        phase_started = clock()
        if not com_return_value(doc.SaveAs(frame_path, DT_SPE), "SaveAs"):
            raise RuntimeError("WinSpec could not save the temporary SPE file")
        timings['save_spe'] = clock() - phase_started
        phase_started = clock()
        width, height, frames, datatype, raw, hardware = read_spe_frames(frame_path)
        timings['read_spe'] = clock() - phase_started
        if compact_settings:
            phase_started = clock()
            managed_readback = settings_from_spe(preflight, hardware, width, height, frames, guard.last_settings)
            timings['settings_after'] = clock() - phase_started
        result_doc = doc
    except Exception:
        try:
            rejected_capture_stop(exp)
        except Exception as exc:
            TEMPERATURE_MONITOR_UNHEALTHY.set()
            server_log('ERROR', 'Failed to stop rejected acquisition: %s' % exc)
        raise
    finally:
        watchdog_finished.set()
        watchdog.join(3.0)
        if watchdog.is_alive():
            TEMPERATURE_MONITOR_UNHEALTHY.set()
        # COM reference is retained until this request receives a complete-data
        # acknowledgement. Failed documents/files remain available for recovery.
        if doc is not None:
            server_log('INFO', 'Acquisition document retained pending transfer receipt')
        doc = None
        restoration_errors = []
        display_started = clock() if compact_settings else None
        try:wait_for_stop_completion()
        except Exception as exc:restoration_errors.append(str(exc))
        if TEMPERATURE_MONITOR_UNHEALTHY.is_set():
            # Do not send more COM configuration writes behind a stalled Stop.
            restoration_errors.append('Stop is stalled; configuration restoration skipped until recovery')
        else:
            try:
                if old_display is not None and bool(old_display[1]):
                    set_acquisition_display(exp, old_display[0], old_display[1])
            except Exception as exc:
                restoration_errors.append('display restoration failed: %s' % exc)
            finally:
                if compact_settings:
                    timings['display_restore'] = clock() - display_started
            # Restore auto-save even if the independent display restoration failed.
            if old_autosave is not None:
                try:
                    status = com_return_value(exp.SetParam(const('EXP_AUTOSAVE'), old_autosave), 'SetParam')
                    if status != 0 or bool(read_parameter(exp, const('EXP_AUTOSAVE'))) != old_autosave:
                        raise RuntimeError('Auto-save restoration readback failed')
                except Exception as exc:
                    restoration_errors.append('auto-save restoration failed: %s' % exc)
        if restoration_errors:
            TEMPERATURE_MONITOR_UNHEALTHY.set()
            raise RuntimeError('WinSpec acquisition configuration: ' + '; '.join(restoration_errors))
    elapsed = time.time() - started
    server_log(
        "INFO",
        "Acquisition completed: %d frame(s), %dx%d, datatype %d, %.3f s; "
        "ADC rate=%r, type=%r, gain=%r, controller=%r" %
        (frames, width, height, datatype, elapsed,
         hardware.get("adc_rate"), hardware.get("adc_type"),
         hardware.get("controller_gain"), hardware.get("controller_type")),
    )
    # Reuse this acquisition's successful post-exposure temperature check.
    if compact_settings:
        settings = managed_readback
    else:
        phase_started = clock()
        settings = read_settings(exp, temperature_status=guard.last_settings)
        timings['settings_after'] = clock() - phase_started
    for key in ("adc_rate", "controller_gain"):
        if key in settings and hardware.get(key) != settings[key]:
            raise RuntimeError(
                "acquired SPE %s mismatch: setup %r, hardware %r" %
                (key, settings[key], hardware.get(key)))
    timings['total_before_transfer'] = clock() - timing_started
    server_log('INFO', 'Acquisition timing (s): %s' % json.dumps(timings, sort_keys=True))
    TRANSFER_STATE.pending = (result_doc, frame_path)
    TRANSFER_PENDING.set()
    return {
        "receipt_required": True,
        "temperature_guard": temperature_report,
        "width": width,
        "height": height,
        "frame_count": frames,
        "winspec_datatype": datatype,
        "elapsed_s": elapsed,
        "settings": settings,
        "hardware_settings": hardware,
        "watchdog_timeout_s": timeout_s,
        "bridge_timing_s": timings,
        "data_window_hidden": data_window_hidden,
        "settings_scope": 'configured_plus_spe' if compact_settings else 'full',
        "settings_provenance": ({'configuration': ['detector_width', 'detector_height', 'roi_enabled', 'timing_mode'],
                                  'spe': ['exposure_ms', 'accumulations', 'output_width', 'output_height',
                                          'sequential_frames', 'adc_rate', 'controller_gain', 'readout_time_s'],
                                  'temperature': 'fresh_boundary_checks'} if compact_settings else {'configuration': 'live_com'}),
    }, raw


def acquire(exp, compact_settings=False, expected_settings=None, acceleration=None):
    require_idle_owner()
    with ACQUISITION_STATE_LOCK:
        if STOP_IN_FLIGHT.is_set():raise RuntimeError('WinSpec Stop is still pending')
        STOP_REQUESTED.clear();NATIVE_START_COMMITTED.clear();ACQUISITION_ACTIVE.set();OWNER_PENDING.set()
    owner=Owner(exp,STOP_REQUESTED.is_set,mark_native_start,com_return_value)
    TRANSFER_STATE.owner=owner
    try:
        capture=lambda value:acquire_active(value,compact_settings=compact_settings,expected_settings=expected_settings)
        if acceleration is not None and (not isinstance(acceleration,dict) or type(acceleration.get('enabled')) is not bool):
            raise ValueError('Invalid Start acceleration option')
        if acceleration and acceleration['enabled']:
            if not compact_settings:raise RuntimeError('Start acceleration requires managed acquisition')
            result=get_accelerator().capture(owner,expected_settings,acceleration.get('session'),capture)
        else:
            if START_ACCELERATOR is not None:START_ACCELERATOR.invalidate('ordinary acquisition requested')
            result=capture(owner)
            result[0]['start_acceleration']=dict(version=1,requested=False,mode='disabled',optimized=False)
        if STOP_REQUESTED.is_set():raise RuntimeError('WinSpec acquisition stopped during completion')
        return result
    finally:
        with ACQUISITION_STATE_LOCK:ACQUISITION_ACTIVE.clear()


def execute(command, parameters):
    if command == "GET_LOGS":
        entries, latest = get_log_entries(parameters.get("after_sequence", 0))
        return {"entries": entries, "latest_sequence": latest}, ""
    if owner_recovery_required():raise RuntimeError('WinSpec recovery required; acquisition owner retained')
    if command == "STOP":
        reason = str(parameters.get("reason", "unspecified"))[:80]
        with ACQUISITION_STATE_LOCK:
            active=ACQUISITION_ACTIVE.is_set()
            if START_ACCELERATOR is not None:START_ACCELERATOR.request_invalidate(reason)
            if not active:return {'stop_requested':False,'was_running':False,'reason':reason},''
            STOP_REQUESTED.set()  # Cancellation precedes any possibly blocking COM call.
            native=NATIVE_START_COMMITTED.is_set()
            if native:STOP_IN_FLIGHT.set()
        if not native:return {'stop_requested':True,'was_running':False,'cancelled_preparation':True},''
        try:
            exp=create_experiment();TRANSFER_STATE.stop_owner=exp
            result=confirmed_stop(exp)
            TRANSFER_STATE.stop_owner=None
            return {'stop_requested':True,'was_running':True,'stop_result':bool(result),
                    'completion_pending':True,'reason':reason},''
        except BaseException:
            TEMPERATURE_MONITOR_UNHEALTHY.set();raise
        finally:
            if not TEMPERATURE_MONITOR_UNHEALTHY.is_set():STOP_IN_FLIGHT.clear()

    if command=='RELEASE_START_ACCELERATION':
        if not CAMERA_LOCK.acquire(False):raise RuntimeError('Capture owns Start acceleration')
        try:
            require_idle_owner()
            if START_ACCELERATOR is not None:START_ACCELERATOR.release(parameters.get('session'))
            return {'released':True},''
        finally:CAMERA_LOCK.release()

    # Read/write status requests must never queue behind a long exposure.
    # Returning busy also avoids touching WinSpec COM from two request threads.
    if command in ("HELLO", "GET_SETTINGS", "GET_ACQUISITION_SETTINGS", "GET_STATUS", "SET_SETTINGS"):
        if not CAMERA_LOCK.acquire(False):
            if command == "SET_SETTINGS":
                raise RuntimeError(
                    "camera acquisition owns the server; wait for it to finish "
                    "before changing settings"
                )
            return {
                "camera_busy": True,
                "settings": {"running": True},
            }, ""
        try:
            if command=='SET_SETTINGS':
                require_idle_owner()
                if START_ACCELERATOR is not None:START_ACCELERATOR.invalidate('camera settings changed')
            exp = create_experiment()
            if command == "HELLO":
                caps = capabilities(exp)
                server_log("INFO", "SpectralSweep connected; WinSpec HELLO succeeded")
                sources = caps.get("capability_sources", {})
                server_log(
                    "INFO",
                    "Camera capability sources: %s" % ", ".join(
                        "%s=%s" % (key, sources.get(key, "unavailable"))
                        for key in (
                            "exposure_ms", "accumulations", "sequential_frames",
                            "adc_rate", "controller_gain", "timing_mode",
                            "shutter_control", "roi")),
                )
                reported_ranges = caps.get("setting_ranges", {})
                server_log(
                    "INFO",
                    "WinSpec readout choices: ADC rate=%r; Controller gain=%r" %
                    (reported_ranges.get("adc_rate", {}).get("values"),
                     reported_ranges.get("controller_gain", {}).get("values")),
                )
                return {
                    "temperature_guard_version": TEMPERATURE_GUARD_VERSION,
                    "acquisition_settings_version": ACQUISITION_SETTINGS_VERSION,
                    "server": "winspec-camera",
                    "server_build": SERVER_BUILD,
                    "start_acceleration_version": 1,
                    "protocol_version": PROTOCOL_VERSION,
                    "winspec_connected": True,
                    "capabilities": caps,
                    "settings": read_settings(exp),
                }, ""
            if command == "GET_SETTINGS":
                return {"settings": read_settings(exp), "temperature_guard_version": TEMPERATURE_GUARD_VERSION,
                        "acquisition_settings_version": ACQUISITION_SETTINGS_VERSION,"start_acceleration_version":1}, ""
            if command == "GET_ACQUISITION_SETTINGS":
                return {"settings": read_acquisition_settings(exp), "temperature_guard_version": TEMPERATURE_GUARD_VERSION,
                        "acquisition_settings_version": ACQUISITION_SETTINGS_VERSION,"start_acceleration_version":1}, ""
            if command == "GET_STATUS":
                return {"settings": read_temperature_status(exp), "temperature_guard_version": TEMPERATURE_GUARD_VERSION}, ""
            server_log("INFO", "Applying camera settings")
            return {"settings": apply_settings(exp, parameters)}, ""
        finally:
            CAMERA_LOCK.release()

    with CAMERA_LOCK:
        require_idle_owner()
        exp = create_experiment()
        if command in ("ACQUIRE", "ACQUIRE_GUARDED"):
            if parameters.get('settings_mode') == 'managed':
                return acquire(exp, compact_settings=True, expected_settings=parameters.get('expected_settings'),acceleration=parameters.get('start_acceleration'))
            return acquire(exp)
        raise ProtocolError("unknown command: %s" % command)


def finish_transfer(sock, sent):
    pending = getattr(TRANSFER_STATE, 'pending', None)
    # An error response has never offered a frame for delivery, so there can be
    # no data receipt. Preserve rejected data locally after confirmed shutdown.
    undelivered=getattr(TRANSFER_STATE,'delivery_ready',None) is False
    if pending is None or undelivered:
        owner=getattr(TRANSFER_STATE,'owner',None)
        if owner is not None and not owner_recovery_required():
            try:
                with CAMERA_LOCK:
                    wait_for_stop_completion()
                    paths=owner.cleanup_rejected(lambda i:unique_spe_path())
                    for path in paths:server_log('WARNING','Rejected acquisition retained at '+path)
                    TRANSFER_STATE.owner=None;TRANSFER_STATE.pending=None
                    OWNER_PENDING.clear();TRANSFER_PENDING.clear()
            except BaseException:
                CLEANUP_FAILED.set();raise
        return
    doc, path = pending
    try:
        if not sent:
            raise RuntimeError('response was not fully sent')
        sock.settimeout(5.0)
        if recv_exact(sock, 4) != b'WXAK':
            raise RuntimeError('complete-data receipt missing')
        # Stay on the owning COM apartment and serialize against all acquisition.
        with CAMERA_LOCK:
            if owner_recovery_required():raise RuntimeError('Owner recovery pending; retain received data')
            if not com_return_value(doc.Save(), 'Save'):
                raise RuntimeError('could not save document state before closing')
            if not com_return_value(doc.Close(), 'Close'):
                raise RuntimeError('acquisition document did not close')
            if not remove_if_unlocked(path, attempts=3):
                raise RuntimeError('temporary SPE remains locked')
            owner=getattr(TRANSFER_STATE,'owner',None)
            if owner is not None:
                owner.mark_closed(doc)
                owner.cleanup_rejected(lambda i:unique_spe_path())
        server_log('INFO', 'Received frame acknowledged; document closed and temporary SPE removed')
        TRANSFER_STATE.pending=None;TRANSFER_STATE.owner=None;OWNER_PENDING.clear()
        return True
    except Exception as exc:
        CLEANUP_FAILED.set()
        if START_ACCELERATOR is not None:START_ACCELERATOR.request_invalidate('frame receipt or cleanup failed')
        server_log('ERROR', 'SPE recovery required; preserving %s: %s' % (path, exc))
        return False
    finally:
        TRANSFER_PENDING.clear()


class CameraHandler(SocketServer.BaseRequestHandler):
    def handle(self):
        pythoncom.CoInitialize()
        TRANSFER_STATE.pending = None
        TRANSFER_STATE.owner=None;TRANSFER_STATE.stop_owner=None
        TRANSFER_STATE.delivery_ready=False
        try:
            header = recv_exact(self.request, REQUEST_HEADER.size)
            magic, version, size = REQUEST_HEADER.unpack(header)
            if magic != "WXRQ":
                raise ProtocolError("invalid request magic")
            if version != PROTOCOL_VERSION:
                raise ProtocolError("unsupported protocol version %s" % version)
            if size < 2 or size > MAX_REQUEST_BYTES:
                raise ProtocolError("invalid request size")
            request = json.loads(recv_exact(self.request, size).decode("utf-8"))
            command = str(request.get("command", "")).upper()
            if command not in ("GET_LOGS", "GET_SETTINGS", "GET_ACQUISITION_SETTINGS", "GET_STATUS"):
                server_log("INFO", "Request %s from %s" %
                           (command, self.client_address[0]))
            metadata, payload = execute(command, request.get("parameters", {}))
            metadata["ok"] = True
            TRANSFER_STATE.delivery_ready=bool(metadata.get('receipt_required') and payload)
        except Exception as exc:
            traceback.print_exc()
            server_log("ERROR", "%s: %s" % (exc.__class__.__name__, str(exc)))
            metadata = {"ok": False, "error": "%s: %s" %
                        (exc.__class__.__name__, str(exc))}
            payload = ""
        finally:
            sent = False
            try:
                encoded = json.dumps(metadata, separators=(",", ":"))
                self.request.sendall(RESPONSE_HEADER.pack(
                    "WXRS", PROTOCOL_VERSION, len(encoded), len(payload)))
                self.request.sendall(encoded)
                if payload:
                    self.request.sendall(payload)
                sent = True
            finally:
                try:
                    cleaned = finish_transfer(self.request, sent)
                    if sent and cleaned is not None:
                        self.request.sendall(b'WXCL' if cleaned else b'WXER')
                finally:
                    # These references never move to another request apartment.
                    while owner_recovery_required() and (getattr(TRANSFER_STATE,'owner',None) is not None or getattr(TRANSFER_STATE,'stop_owner',None) is not None):
                        time.sleep(1)
                    pythoncom.CoUninitialize()


class CameraServer(SocketServer.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = False  # An uncertain owner must survive a main-loop shutdown.


if __name__ == "__main__":
    parent = os.path.dirname(SPE_PATH)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    # Do not delete old files: they may be the only copy of a failed transfer.
    server_log(
        "INFO",
        "WinSpec camera server %s protocol %d listening on %s:%d" %
        (SERVER_BUILD, PROTOCOL_VERSION, HOST, PORT),
    )
    server_log("INFO", "Spectrometer automation is intentionally disabled")
    CameraServer((HOST, PORT), CameraHandler).serve_forever()
