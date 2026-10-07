# Import the .NET class library
import clr, ctypes
import builtins
import time
from app.lightfield_metadata import LightFieldRecorder, capture_with_metadata, read_snapshot
from app.lightfield_diagnostics import center_context, diagnostic_path, trace_center_write

# Import python sys module
import sys, os

# numpy import
import numpy as np, matplotlib.pyplot as plt

# Import c compatible List and String
from System import *
from System.IO import *
from System.Collections.Generic import List
from System.Runtime.InteropServices import Marshal
from System.Runtime.InteropServices import GCHandle, GCHandleType


# Add needed dll references
sys.path.append(os.environ['LIGHTFIELD_ROOT'])
sys.path.append(os.environ['LIGHTFIELD_ROOT']+"\\AddInViews")
clr.AddReference('PrincetonInstruments.LightFieldViewV5')
clr.AddReference('PrincetonInstruments.LightField.AutomationV5')
clr.AddReference('PrincetonInstruments.LightFieldAddInSupportServices')

# PI imports
from PrincetonInstruments.LightField.Automation import *
from PrincetonInstruments.LightField.AddIns import *
from PrincetonInstruments.LightField.AddIns import SpectrometerSettings
from PrincetonInstruments.LightField.AddIns import ExperimentSettings
from PrincetonInstruments.LightField.AddIns import CameraSettings



# Create the LightField Application (true for visible)
# The 2nd parameter forces LF to load with no experiment

LIGHTFIELD_SETTING_TIMEOUT_S = 15.0
LIGHTFIELD_POLL_INTERVAL_S = 0.05
LIGHTFIELD_ACQUISITION_ABORT_TIMEOUT_S = 5.0
# Acceptance tolerance for SDK setting readback, not wavelength calibration accuracy.
LIGHTFIELD_CENTER_TOLERANCE_NM = 0.01
LIGHTFIELD_OPTICS_STABLE_S = 0.3
LIGHTFIELD_CENTER_MAX_REWRITES = 2


class LightFieldSettingTimeoutError(TimeoutError):
    """A LightField setting could not be written and verified within the bounded wait."""


class LF6Setup:
    def bind_metadata_run(self, run):
        self._metadata_recorder = LightFieldRecorder(run)

    def read_metadata_snapshot(self):
        return read_snapshot(self, camera=CameraSettings,
                             spectrometer=SpectrometerSettings, experiment=ExperimentSettings)

    def __init__(self):
        self.auto = Automation(True, List[String]())
        try:
            self.application = self.auto.LightFieldApplication
            self.experiment = self.application.Experiment
            self.exp_settings = ExperimentSettings
            self.spectrometer_settings = SpectrometerSettings
            self._center_wavelength_write_stats = None
        except Exception:
            self.close()
            raise

    def close(self):
        """Dispose only this setup's owned Automation instance, once."""
        auto = getattr(self, 'auto', None)
        if auto is not None:
            auto.Dispose()
            self.auto = None

    def print_saved_experiments(self):
        # Print a list (of type string) of saved experiments
        print("My Saved Experiments:")
        for saved_experiment in self.experiment.GetSavedExperiments():
            print("\t" + saved_experiment)

    def load_experiment(self, exp_name: str):
        load_success = self.experiment.Load(exp_name)
        if load_success:
            print('loading experiment successful')
        else:
            print('loading experiment failed')

    def acquire(self, *, purpose="measurement"):
        frames = 1
        dataset = capture_with_metadata(self, frames, purpose=purpose)
        image_data = dataset.GetFrame(0, frames - 1).GetData()
        image_frame = dataset.GetFrame(0, frames - 1)
        array = self.convert_buffer(image_data, image_frame.Format)
        return array

    def abort_acquisition(self) -> bool:
        """Request cancellation of an active LightField acquisition.

        LightField versions expose this operation on different automation
        objects. Try the supported-looking surfaces without assuming one
        particular SDK version; the caller still enforces its own timeout.
        """
        for owner in (self.experiment, self.application):
            for name in ("Stop", "Abort", "Cancel"):
                method = getattr(owner, name, None)
                if not callable(method):
                    continue
                try:
                    method()
                    return True
                except Exception:
                    continue
        return False

    def _frame_dims(self, frame):
        """Try common LightField frame width/height attributes (property or method)."""
        def _get(names):
            for name in names:
                if hasattr(frame, name):
                    v = getattr(frame, name)
                    try:
                        v = v() if callable(v) else v
                        v = int(v)
                        if v > 0:
                            return v
                    except Exception:
                        pass
            return None

        w = _get(["Width", "GetWidth", "SizeX", "GetSizeX", "XSize", "GetXSize"])
        h = _get(["Height", "GetHeight", "SizeY", "GetSizeY", "YSize", "GetYSize"])
        return w, h

    def acquire_2d(self, *, purpose="measurement"):
        """
        Capture one frame and return a 2D array (H, W) if frame dimensions are available.
        If dimension detection fails, returns the raw 1D array (same as acquire()).
        """
        frames = 1
        dataset = capture_with_metadata(self, frames, purpose=purpose)

        # for frames=1, index is always 0
        frame = dataset.GetFrame(0, 0)
        image_data = frame.GetData()

        arr = self.convert_buffer(image_data, frame.Format)

        w, h = self._frame_dims(frame)
        if w and h and arr.ndim == 1 and arr.size == w * h:
            arr = arr.reshape(h, w)  # (H, W)

        return arr

    def change_exp_setting(self, setting, value):
        # Check for existence before setting
        # gain, adc rate, or adc quality
        if self.exp_settings.Exists(setting):
            self.exp_settings.SetValue(setting, value)

    def change_spec_setting(self, setting, value):
        # Check for existence before setting
        # gain, adc rate, or adc quality
        if self.spectrometer_settings.Exists(setting):
            self.spectrometer_settings.SetValue(setting, value)

    # Creates a numpy array from our acquired buffer
    def convert_buffer(self, net_array, image_format):
        src_hndl = GCHandle.Alloc(net_array, GCHandleType.Pinned)
        try:
            src_ptr = src_hndl.AddrOfPinnedObject().ToInt64()

            # Possible data types returned from acquisition
            if (image_format == ImageDataFormat.MonochromeUnsigned16):
                buf_type = ctypes.c_ushort * len(net_array)
            elif (image_format == ImageDataFormat.MonochromeUnsigned32):
                buf_type = ctypes.c_uint * len(net_array)
            elif (image_format == ImageDataFormat.MonochromeFloating32):
                buf_type = ctypes.c_float * len(net_array)

            cbuf = buf_type.from_address(src_ptr)
            resultArray = np.frombuffer(cbuf, dtype=cbuf._type_)

        # Free the handle
        finally:
            if src_hndl.IsAllocated: src_hndl.Free()

        # Make a copy of the buffer
        return np.copy(resultArray)

    def change_center_wavelength(self, wavelength):
        """Legacy alias; all center writes use the guarded shared setter."""
        return self.set_center_wavelength_when_ready(wavelength)

    def get_wavelength_calibration(self):
        net_array = self.experiment.SystemColumnCalibration
        src_hndl = GCHandle.Alloc(net_array, GCHandleType.Pinned)
        try:
            src_ptr = src_hndl.AddrOfPinnedObject().ToInt64()
            buf_type = ctypes.c_double * len(net_array)
            cbuf = buf_type.from_address(src_ptr)
            resultArray = np.frombuffer(cbuf, dtype=cbuf._type_)
        # Free the handle
        finally:
            if src_hndl.IsAllocated: src_hndl.Free()
        return np.copy(resultArray)

    def take_one_look(self):
        plt.plot(self.get_wavelength_calibration(), self.acquire())

    def create_spectra_sweep(self, sample_name, exp_name):
        return SpectraSweep(sample_name, exp_name, self)
    # added by Lei
    def change_expose_time(self, value):
        value = float(value)
        resume = self._begin_recipe_change('exposure_ms', value)
        setting = CameraSettings.ShutterTimingExposureTime
        if not self.experiment.Exists(setting):
            raise RuntimeError('LightField exposure setting is unavailable')
        self.experiment.SetValue(setting, value)
        self._finish_recipe_change(resume)

    def change_spectra_center(self, value):
        """Legacy center setter routed through the guarded shared path."""
        return self.set_center_wavelength_when_ready(value)

    def _set_center_wavelength_raw(self, value, *, deadline=None, before_write=None):
        """Perform exactly one authoritative CenterWavelength SetValue attempt."""
        setting = SpectrometerSettings.GratingCenterWavelength
        if not bool(self.experiment.Exists(setting)):
            raise RuntimeError("LightField setting GratingCenterWavelength is unavailable")
        # Exists is itself a synchronous SDK query. Check its completion before
        # counting or dispatching a motor command.
        if deadline is not None and time.monotonic() >= deadline:
            raise LightFieldSettingTimeoutError('LightField center write deadline expired; acquisition blocked')
        if before_write is not None and before_write() is False:
            return self._read_center_wavelength(setting)
        self.experiment.SetValue(setting, value)
        return self._read_center_wavelength(setting)

    def _read_center_wavelength(self, setting):
        getter = getattr(self.experiment, "GetValue", None)
        if not callable(getter):
            return None
        try:
            return getter(setting)
        except BaseException:
            return None

    @property
    def center_wavelength_write_stats(self):
        """Last guarded write outcome, including actual SetValue attempt count."""
        return dict(self._center_wavelength_write_stats or {})

    @staticmethod
    def _exception_chain(exc):
        current = exc
        seen = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            yield current
            inner = None
            for name in ("InnerException", "inner_exception", "inner"):
                try:
                    inner = getattr(current, name)
                except BaseException:
                    inner = None
                if inner is not None:
                    break
            current = inner

    @classmethod
    def _frozen_exception(cls, exc):
        """Return the exact InvalidOperationException frozen-setting cause, if present."""
        for item in cls._exception_chain(exc):
            try:
                get_type = getattr(item, "GetType", None)
                type_name = str(get_type().FullName if callable(get_type) else type(item).__name__)
            except BaseException:
                type_name = type(item).__name__
            type_name = type_name.rsplit(".", 1)[-1]
            try:
                message = getattr(item, "Message")
            except BaseException:
                message = str(item)
            normalized = str(message).strip().rstrip(".")
            frozen_messages = {
                "Cannot modify a frozen setting",
                "Cannot modify a frozen setting (Spectrometer.Grating.CenterWavelength)",
                "Cannot modify a frozen setting. (Spectrometer.Grating.CenterWavelength)",
            }
            if type_name == "InvalidOperationException" and normalized in frozen_messages:
                return item
        return None

    @staticmethod
    def _exception_description(exc) -> str:
        if exc is None:
            return ""
        try:
            get_type = getattr(exc, "GetType", None)
            type_name = str(get_type().FullName if callable(get_type) else type(exc).__name__)
        except BaseException:
            type_name = type(exc).__name__
        try:
            message = getattr(exc, "Message")
        except BaseException:
            message = str(exc)
        return f"{type_name}: {message}"

    @staticmethod
    def _flag(obj, names, *args):
        """Read an optional LightField readiness flag without assuming one API."""
        for name in names:
            try:
                value = getattr(obj, name)
                value = value(*args) if callable(value) else value
                if isinstance(value, (bool, np.bool_)):
                    return bool(value)
            except BaseException:
                continue
        return None

    @property
    def is_ready(self):
        """Return readiness from explicit state or a usable experiment handshake."""
        return bool(self.readiness_snapshot["ready"])

    @property
    def readiness_evidence(self):
        """Return True/False when LightField exposes explicit readiness evidence."""
        values = [
            self._flag(self.application, ("IsReady", "Ready", "IsInitialized", "Initialized")),
            self._flag(self.experiment, ("IsReady", "Ready", "IsLoaded", "Loaded")),
        ]
        explicit = [value for value in values if value is not None]
        if not explicit:
            return None
        return False if any(value is False for value in explicit) else True

    @property
    def readiness_snapshot(self):
        """Describe the strongest readiness evidence exposed by this LF version."""
        explicit = self.readiness_evidence
        application_present = getattr(self, "application", None) is not None
        experiment = getattr(self, "experiment", None)
        experiment_present = experiment is not None
        busy = self.is_busy if experiment_present else False
        required = {
            "center_wavelength": SpectrometerSettings.GratingCenterWavelength,
            "exposure": CameraSettings.ShutterTimingExposureTime,
            "frame_combination": ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined,
        }
        settings = {}
        if experiment_present:
            for label, setting in required.items():
                try:
                    settings[label] = bool(experiment.Exists(setting))
                except BaseException:
                    settings[label] = False
        else:
            settings = {label: False for label in required}

        query_ok = experiment_present
        saved_experiments = getattr(experiment, "GetSavedExperiments", None) if experiment_present else None
        if callable(saved_experiments):
            try:
                saved_experiments()
            except BaseException:
                query_ok = False

        capability_ready = (
            application_present
            and experiment_present
            and query_ok
            and all(settings.values())
        )
        if explicit is False:
            ready = False
            reason = "explicit LightField readiness is false"
        elif busy:
            ready = False
            reason = "LightField is busy/loading/acquiring"
        elif explicit is True:
            ready = capability_ready
            reason = "ready" if ready else "required experiment capabilities are unavailable"
        else:
            ready = capability_ready
            reason = "ready via experiment capability handshake" if ready else "readiness capability handshake incomplete"
        return {
            "ready": ready,
            "reason": reason,
            "explicit_ready": explicit,
            "busy": busy,
            "application_present": application_present,
            "experiment_present": experiment_present,
            "query_ok": query_ok,
            "settings": settings,
        }

    @property
    def is_busy(self):
        """Read SDK acquisition/update state as well as legacy wrapper flags."""
        for obj in (self.application, self.experiment):
            for name in ("IsRunning", "IsUpdating", "IsBusy", "Busy",
                         "IsAcquiring", "Acquiring", "IsLoading", "Loading"):
                # These flags describe independent states. A false IsRunning
                # must not hide IsUpdating during an exit/grating transition.
                if self._flag(obj, (name,)) is True:
                    return True
        return False

    def setting_is_available(self, setting) -> bool:
        """Return whether a setting exists and any explicit availability API allows it."""
        try:
            if not bool(self.experiment.Exists(setting)):
                return False
        except BaseException:
            return False
        value = self._flag(
            self.experiment,
            ("IsAvailable", "Available", "SettingAvailable"),
            setting,
        )
        return True if value is None else value

    def setting_is_writable(self, setting):
        """Return explicit writability, or ``None`` when LightField has no such API."""
        for name in ("IsWritable", "Writable", "CanSetValue", "CanWrite"):
            try:
                value = getattr(self.experiment, name)
                value = value(setting) if callable(value) else value
                if isinstance(value, (bool, np.bool_)):
                    return bool(value)
            except BaseException:
                continue
        value = self._flag(self.experiment, ("IsReadOnly", "ReadOnly"), setting)
        return None if value is None else not value

    def wait_until_setting_writable(
        self,
        setting,
        *,
        timeout_s: float = LIGHTFIELD_SETTING_TIMEOUT_S,
        poll_interval_s: float = LIGHTFIELD_POLL_INTERVAL_S,
    ) -> None:
        """Wait for readiness/availability before attempting a setting write."""
        timeout_s = float(timeout_s)
        poll_interval_s = float(poll_interval_s)
        if timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("LightField readiness timings must be positive")
        deadline = time.monotonic() + timeout_s
        reason = "not ready"
        label = "GratingCenterWavelength" if "CenterWavelength" in str(setting) else str(setting)
        while True:
            if not self.is_ready:
                reason = "LightField is still starting or loading"
            elif self.is_busy:
                reason = "LightField experiment is busy"
            elif not self.setting_is_available(setting):
                reason = "setting is unavailable"
            elif self.setting_is_writable(setting) is False:
                reason = "setting is read-only/frozen"
            else:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LightFieldSettingTimeoutError(
                    f"LightField setting {label} remained {reason} for {timeout_s:g}s"
                )
            time.sleep(min(poll_interval_s, remaining))

    def _read_optical_state(self, *, include_center=True):
        """Read optical identity and, optionally, the finite SDK center."""
        result = {}
        fields = [('output_port', 'OpticalPortExitSelected'), ('grating', 'GratingSelected')]
        if include_center:
            fields.append(('wavelength_nm', 'GratingCenterWavelength'))
        for field, name in fields:
            key = getattr(SpectrometerSettings, name, None)
            if key is None or not self.experiment.Exists(key):
                continue
            value = self.experiment.GetValue(key)
            if value is None:
                raise RuntimeError(f'LightField {field} readback unavailable')
            if field == 'wavelength_nm':
                if isinstance(value, (bool, np.bool_)) or not np.isfinite(float(value)):
                    raise RuntimeError('LightField center readback is not finite')
                result[field] = float(value)
            else:
                result[field] = str(value)
        if include_center and 'wavelength_nm' not in result:
            raise RuntimeError('LightField center readback unavailable')
        return result

    def wait_until_optics_stable(self, *, expected=None, reject_changes=False,
                                timeout_s=LIGHTFIELD_SETTING_TIMEOUT_S,
                                poll_interval_s=LIGHTFIELD_POLL_INTERVAL_S):
        """Require 300 ms of unchanged optical readbacks while idle and writable."""
        timeout_s, poll_interval_s = float(timeout_s), float(poll_interval_s)
        if (not np.isfinite(timeout_s) or timeout_s <= 0
                or not np.isfinite(poll_interval_s) or poll_interval_s <= 0):
            raise ValueError('LightField optics timings must be finite and positive')
        started = time.monotonic()
        deadline = started + timeout_s
        stable_since = None
        baseline = None
        last_error = None
        last_snapshot = None
        expected = expected or {}
        setting = SpectrometerSettings.GratingCenterWavelength
        while True:
            try:
                snapshot = self._read_optical_state()
                last_snapshot = snapshot
                last_error = None
            except builtins.Exception as exc:
                snapshot = None
                last_error = str(exc)
            state = self._center_wavelength_state(setting)
            matches = snapshot is not None and all(
                snapshot.get(key) == str(value) for key, value in expected.items())
            if snapshot is not None and reject_changes and not matches:
                raise RuntimeError('LightField optical configuration changed; acquisition blocked')
            idle = (state['ready'] and not state['busy'] and state['available']
                    and state['writable'] is not False)
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                raise LightFieldSettingTimeoutError(
                    f'LightField optical readbacks did not settle within {timeout_s:g}s; '
                    f'readback={last_snapshot}; state={state}; last error={last_error}')
            if idle and matches:
                unchanged = (baseline is not None
                             and snapshot.keys() == baseline.keys()
                             and all(snapshot[key] == baseline[key]
                                     for key in snapshot if key != 'wavelength_nm')
                             and self._numeric_readback_matches(
                                 snapshot['wavelength_nm'], baseline['wavelength_nm'],
                                 LIGHTFIELD_CENTER_TOLERANCE_NM))
                if not unchanged:
                    stable_since, baseline = now, snapshot
                elif now - stable_since + 1e-12 >= LIGHTFIELD_OPTICS_STABLE_S:
                    self._optics_stability_stats = {
                        'elapsed_s': now - started, 'stable_s': now - stable_since,
                        'readback': snapshot,
                    }
                    return snapshot
            else:
                stable_since = baseline = None
            time.sleep(min(poll_interval_s, remaining))

    @trace_center_write
    def set_center_wavelength_when_ready(
        self,
        value,
        *,
        timeout_s: float = LIGHTFIELD_SETTING_TIMEOUT_S,
        poll_interval_s: float = LIGHTFIELD_POLL_INTERVAL_S,
        update_acquisition_recipe: bool = True,
        _deadline=None,
    ) -> None:
        """Verify a write, with bounded recovery only after a matching read reverts."""
        value = float(value)
        setting = SpectrometerSettings.GratingCenterWavelength
        timeout_s = float(timeout_s)
        poll_interval_s = float(poll_interval_s)
        if (not np.isfinite(value) or not np.isfinite(timeout_s) or timeout_s <= 0
                or not np.isfinite(poll_interval_s) or poll_interval_s <= 0):
            raise ValueError("LightField center must be finite and readiness timings must be positive")
        # WinSpec borrows this spectrograph but must not replace PIXIS's recipe.
        resume = self._begin_recipe_change('center_nm', value) if update_acquisition_recipe else False
        started = time.monotonic()
        deadline = started + timeout_s
        if _deadline is not None:
            deadline = min(deadline, float(_deadline))
            started = min(started, deadline - timeout_s)
        last_error = None
        attempts = 0
        optical_identity = None
        stats = {
            "setting": "GratingCenterWavelength",
            "requested_value": value,
            "attempts": 0,
            "result": "pending",
            "elapsed_s": 0.0,
            "last_exception": None,
            "readback": None,
            "state": {},
            "readback_changes": [],
            "write_attempts": [],
            "rewrites": [],
        }
        self._center_wavelength_write_stats = stats
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stats.update({
                    "result": "timeout",
                    "elapsed_s": time.monotonic() - started,
                    "last_exception": self._exception_description(last_error) if last_error is not None else None,
                    "state": self._center_wavelength_state(setting),
                })
                detail = (
                    f"; last exception: {self._exception_description(last_error)}"
                    if last_error is not None else ""
                )
                raise LightFieldSettingTimeoutError(
                    f"LightField setting GratingCenterWavelength requested={value!r} "
                    f"remained frozen/unavailable for {stats['elapsed_s']:.3f}s; "
                    f"SetValue attempts={attempts}; state={stats['state']}{detail}"
                ) from last_error
            try:
                self.wait_until_setting_writable(
                    setting, timeout_s=remaining, poll_interval_s=poll_interval_s
                )
                if attempts == 0:
                    try:
                        optical_identity = self._read_optical_state(include_center=False)
                        if set(optical_identity) != {'output_port', 'grating'}:
                            optical_identity = None
                    except builtins.Exception:
                        # Missing identity evidence disables automatic recovery.
                        optical_identity = None
                context = None
                try:
                    if diagnostic_path(self) is not None:
                        context = center_context(self, value)
                        stats.setdefault('before_write', context)
                except builtins.Exception as exc:
                    stats['diagnostic_error'] = str(exc)
                def record_write():
                    nonlocal attempts
                    if stats['rewrites']:
                        current = self._read_optical_state()
                        identity = {key: item for key, item in current.items() if key != 'wavelength_nm'}
                        if identity != optical_identity:
                            raise RuntimeError('LightField optical configuration changed; acquisition blocked')
                        if self._numeric_readback_matches(
                                current['wavelength_nm'], value, LIGHTFIELD_CENTER_TOLERANCE_NM):
                            return False  # The center recovered before dispatch.
                    if time.monotonic() >= deadline:
                        raise LightFieldSettingTimeoutError('LightField center write deadline expired; acquisition blocked')
                    if context is not None and len(stats['write_attempts']) < 32:
                        stats['write_attempts'].append({
                            'attempt': attempts + 1, 'elapsed_s': time.monotonic() - started,
                            'before': context,
                        })
                    attempts += 1
                    stats['attempts'] = attempts
                    return True

                readback = self._set_center_wavelength_raw(
                    value, deadline=deadline, before_write=record_write)
                stable = 0
                seen_matching = False
                while True:
                    state = self._center_wavelength_state(setting)
                    stats.update(readback=readback, state=state,
                                 elapsed_s=time.monotonic() - started)
                    changes = stats['readback_changes']
                    if len(changes) < 32 and (not changes or str(changes[-1]['value']) != str(readback)):
                        changes.append({'elapsed_s': stats['elapsed_s'], 'value': readback})
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LightFieldSettingTimeoutError(
                            f"Center wavelength readback did not settle: requested={value:g} nm, "
                            f"readback={readback!r} nm (tolerance={LIGHTFIELD_CENTER_TOLERANCE_NM:g} nm). "
                            "Acquisition blocked; check LightField and apply settings again."
                        )
                    matching = self._numeric_readback_matches(readback, value, LIGHTFIELD_CENTER_TOLERANCE_NM)
                    seen_matching = seen_matching or matching
                    if state['ready'] and not state['busy'] and matching:
                        stable += 1
                        if stable >= 3:
                            self._finish_recipe_change(resume)
                            if time.monotonic() >= deadline:
                                raise LightFieldSettingTimeoutError('LightField verification deadline expired; acquisition blocked')
                            stats['result'] = 'succeeded'
                            return
                    else:
                        stable = 0
                    try:
                        finite = (readback is not None and not isinstance(readback, (bool, np.bool_))
                                  and np.isfinite(float(readback)))
                    except (TypeError, ValueError, OverflowError):
                        finite = False
                    if (seen_matching and not matching and finite and optical_identity is not None
                            and len(stats['rewrites']) < LIGHTFIELD_CENTER_MAX_REWRITES):
                        settled = self.wait_until_optics_stable(
                            expected=optical_identity, reject_changes=True,
                            timeout_s=remaining, poll_interval_s=poll_interval_s)
                        if self._numeric_readback_matches(
                                settled['wavelength_nm'], value, LIGHTFIELD_CENTER_TOLERANCE_NM):
                            readback = settled['wavelength_nm']
                            continue  # A late successful read needs no new motor command.
                        stats['rewrites'].append({
                            'reason': 'matching_center_reverted', 'readback_nm': float(readback),
                            'settled_nm': settled['wavelength_nm'], 'next_attempt': attempts + 1,
                            'elapsed_s': time.monotonic() - started,
                            'stability': dict(self._optics_stability_stats),
                        })
                        break
                    time.sleep(min(poll_interval_s, remaining))
                    readback = self._read_center_wavelength(setting)
            except LightFieldSettingTimeoutError as exc:
                # Preserve the bounded state/attempt diagnostics from the wait.
                if update_acquisition_recipe:
                    self._acquisition_prepared = False
                stats.update({
                    "result": "timeout",
                    "elapsed_s": time.monotonic() - started,
                    "last_exception": self._exception_description(exc),
                    "state": self._center_wavelength_state(setting),
                })
                raise LightFieldSettingTimeoutError(
                    f"LightField setting GratingCenterWavelength requested={value!r} "
                    f"could not be verified within {stats['elapsed_s']:.3f}s; "
                    f"readback={stats['readback']!r}; "
                    f"SetValue attempts={attempts}; state={stats['state']}; "
                    f"last exception: {self._exception_description(exc)}"
                ) from exc
            except BaseException as exc:
                frozen = self._frozen_exception(exc)
                if frozen is None:
                    stats.update(result='failed', elapsed_s=time.monotonic() - started,
                                 last_exception=self._exception_description(exc))
                    raise
                last_error = frozen
                stats["last_exception"] = self._exception_description(frozen)
                time.sleep(min(float(poll_interval_s), max(0.0, deadline - time.monotonic())))

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames,
                                  timeout_s=LIGHTFIELD_SETTING_TIMEOUT_S):
        """Apply the complete mutable run recipe immediately before acquisition."""
        self._acquisition_prepared = False
        timeout_s = float(timeout_s)
        if not np.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('LightField optics timeout must be finite and positive')
        deadline = time.monotonic() + timeout_s
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise LightFieldSettingTimeoutError('LightField optical preparation timed out; acquisition blocked')
            return value

        self._acquisition_recipe = dict(center_nm=float(center_nm), exposure_ms=float(exposure_ms), frames=int(frames))
        self._configuring_acquisition = True
        try:
            self._ensure_detector_output(timeout_s=remaining())
            grating = getattr(SpectrometerSettings, 'GratingSelected', None)
            if grating is not None and self.experiment.Exists(grating):
                value = self.experiment.GetValue(grating)
                if value is None:
                    raise RuntimeError('LightField grating readback unavailable; acquisition blocked.')
                self._acquisition_recipe['grating'] = str(value)
            self.set_center_wavelength_when_ready(float(center_nm), timeout_s=remaining())
            remaining()
            self.change_expose_time(float(exposure_ms))
            remaining()
            self.change_frame_to_combine(int(frames))
            remaining()
            actual = self._verify_acquisition_recipe()
            remaining()
            self._acquisition_prepared = True
        finally:
            self._configuring_acquisition = False
        return {
            "center_wavelength": self.center_wavelength_write_stats,
            "exposure_ms": actual['exposure_ms'],
            "frames": int(actual['frames']),
            "output_route": getattr(self, 'last_output_route', {}),
        }

    @staticmethod
    def _numeric_readback_matches(actual, expected, tolerance):
        try:
            return (actual is not None and not isinstance(actual, (bool, np.bool_))
                    and np.isfinite(float(actual)) and abs(float(actual) - expected) <= tolerance)
        except (TypeError, ValueError, OverflowError):
            return False

    def _begin_recipe_change(self, name, value):
        """Track explicit PIXIS setters as well as complete acquisition recipes."""
        resume = not getattr(self, '_configuring_acquisition', False)
        self._acquisition_prepared = False
        recipe = dict(getattr(self, '_acquisition_recipe', {}))
        recipe[name] = value
        self._acquisition_recipe = recipe
        return resume

    def _finish_recipe_change(self, resume):
        if resume:
            self._verify_acquisition_recipe()
            self._acquisition_prepared = True

    def _verify_acquisition_recipe(self):
        actual = {}
        for name, setting, tolerance in (
            ('center_nm', SpectrometerSettings.GratingCenterWavelength, LIGHTFIELD_CENTER_TOLERANCE_NM),
            ('exposure_ms', CameraSettings.ShutterTimingExposureTime, max(.001, abs(self._acquisition_recipe.get('exposure_ms', 0.)) * 1e-6)),
            ('frames', ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined, 0.),
        ):
            if name not in self._acquisition_recipe:
                continue
            expected = self._acquisition_recipe[name]
            try:
                value = self.experiment.GetValue(setting)
            except builtins.Exception as exc:
                raise RuntimeError(f'LightField {name} readback unavailable; requested={expected:g}. Acquisition blocked.') from exc
            if not self._numeric_readback_matches(value, expected, tolerance):
                raise RuntimeError(
                    f'LightField {name} readback mismatch: requested={expected:g}, readback={value}. '
                    'Acquisition blocked; apply settings again.'
                )
            actual[name] = float(value)
        if 'grating' in self._acquisition_recipe:
            expected = self._acquisition_recipe['grating']
            try:
                value = self.experiment.GetValue(SpectrometerSettings.GratingSelected)
            except builtins.Exception as exc:
                raise RuntimeError('LightField grating readback unavailable; acquisition blocked.') from exc
            if value is None or str(value) != expected:
                raise RuntimeError(
                    f'LightField grating readback mismatch: prepared={expected}, readback={value}. '
                    'Acquisition blocked; apply settings again.'
                )
        return actual

    def verify_acquisition_settings(self):
        """Read-only checks before/after Capture; never move optics mid-measurement."""
        try:
            if not getattr(self, '_acquisition_prepared', True):
                raise RuntimeError('LightField acquisition setup was not verified; apply settings again.')
            self._ensure_detector_output(apply=False)
            if getattr(self, '_acquisition_recipe', None) is not None:
                self._verify_acquisition_recipe()
        except builtins.Exception:
            self._acquisition_prepared = False
            raise

    def _ensure_detector_output(self, *, apply=True, timeout_s=LIGHTFIELD_SETTING_TIMEOUT_S):
        route = getattr(self, 'detector_output_route', None)
        if route is not None:
            from app.devices.lightfield_optics import ensure_output_route
            self.last_output_route = ensure_output_route(self, route, apply=apply, timeout_s=timeout_s)

    def _center_wavelength_state(self, setting) -> dict:
        return {
            "ready": self.is_ready,
            "busy": self.is_busy,
            "IsRunning": self._flag(self.experiment, ("IsRunning",)),
            "IsUpdating": self._flag(self.experiment, ("IsUpdating",)),
            # Acquisition readiness is diagnostic only: WinSpec borrows the
            # spectrograph without acquiring through LightField's camera.
            "IsReadyToRun": self._flag(self.experiment, ("IsReadyToRun",)),
            "available": self.setting_is_available(setting),
            "writable": self.setting_is_writable(setting),
        }

    def change_roi_FullSensor(self):
        if self.experiment.Exists(CameraSettings.ReadoutControlRegionsOfInterestSelection):
            self.experiment.SetValue(CameraSettings.ReadoutControlRegionsOfInterestSelection,
                                     RegionsOfInterestSelection.FullSensor)
            print('roi sets to FullSensor')

    def change_roi_LineSensor(self):
        if self.experiment.Exists(CameraSettings.ReadoutControlRegionsOfInterestSelection):
            self.experiment.SetValue(CameraSettings.ReadoutControlRegionsOfInterestSelection,
                                     RegionsOfInterestSelection.LineSensor)
            print('roi sets to LineSensor')

    def change_to_side_exit_port(self):
        # 4 front exit 5 Side exit
        if self.experiment.Exists(SpectrometerSettings.OpticalPortExitSelected):
            self.experiment.SetValue(SpectrometerSettings.OpticalPortExitSelected,
                                     OpticalPortLocation.SideExit)
            print('exit port : Side')
    def change_to_front_exit_port(self):
        # 4 front exit 5 Side exit
        if self.experiment.Exists(SpectrometerSettings.OpticalPortExitSelected):
            self.experiment.SetValue(SpectrometerSettings.OpticalPortExitSelected,
                                     OpticalPortLocation.FrontExit)
            print('exit port : Front')
            
    def change_frame_to_combine(self, frames: int):
        """
        Sets Online Processes -> Exposures per Frame.
        Crucial: Must use .NET Int64 (Long) for LightField integer settings.
        """
        frames = int(frames)
        resume = self._begin_recipe_change('frames', frames)
        setting = ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined
        if not self.experiment.Exists(setting):
            raise RuntimeError('LightField exposures-per-frame setting is unavailable')
        self.experiment.SetValue(setting, Int64(frames))
        self._finish_recipe_change(resume)

    def readback_online_process(self) -> dict:
        def _get(key):
            try:
                if self.experiment.Exists(key):
                    return self.experiment.GetValue(key)
            except Exception:
                pass
            return None

        # Safe attribute lookup
        combine_mode_key = getattr(ExperimentSettings, "OnlineProcessingFrameCombinationMethod", None)
        
        return {
            "exposures_per_frame": _get(ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined),
            "combine_mode":        _get(combine_mode_key) if combine_mode_key else "Unknown"
        }


    def set_frames_to_save(self,frames):
        if self.experiment.Exists(ExperimentSettings.AcquisitionFramesToStore):
            self.experiment.SetValue(
                                ExperimentSettings.AcquisitionFramesToStore,frames)
            print('Frame:', String.Format(str(frames)))

    def multi_frame_acquire(self,image_mode:bool=False , x_pixels_num = 512 ,y_pixels_num = 512):
        if self.experiment.Exists(ExperimentSettings.AcquisitionFramesToStore):
            frames = self.experiment.GetValue(
                                    ExperimentSettings.AcquisitionFramesToStore)
            print('Frame:', String.Format(str(frames)))
            if image_mode:
                
                dataset = capture_with_metadata(self, frames)
                bufferdata = np.zeros((frames,x_pixels_num*y_pixels_num))
                for i in range(frames):
                    image_data = dataset.GetFrame(0, i).GetData()
                    image_frame = dataset.GetFrame(0, i)
                    array = self.convert_buffer(image_data, image_frame.Format)
                    bufferdata[i,:] = array
                return bufferdata
            else:
                dataset = capture_with_metadata(self, frames)
                bufferdata = np.zeros((frames,x_pixels_num))
                for i in range(frames):
                    image_data = dataset.GetFrame(0, i).GetData()
                    image_frame = dataset.GetFrame(0, i)
                    array = self.convert_buffer(image_data, image_frame.Format)
                    bufferdata[i,:] = array
                return bufferdata
        else:
            return    



class SpectraSweep:

    def __init__(self, sample_name: str, exp_name: str,  lf6_setup: LF6Setup):
        self.sample_name = sample_name
        self.exp_name = exp_name
        self.frames = None
        self.lf6_setup = lf6_setup
        self.wavelengths = None
        self.calibrate_wavelength()
        self.total_triggers = None
        self.current_trigger = None
        self.plot = True
        self.data_plot=None

    def set_sweep(self, frames: int, plot=True):
        if plot:
            self.data_plot = plt.subplot(1, 1, 1)
        self.plot = plot
        self.calibrate_wavelength()
        self.total_triggers = frames
        self.current_trigger = 0
        '''self.storage = data_collection.OneDSweepData(self.sample_name, self.exp_name, frames, self.wavelengths, None,
                                                     None, False, True)'''

    def calibrate_wavelength(self):
        self.wavelengths = list(self.lf6_setup.get_wavelength_calibration())
        return self.wavelengths

    def trigger(self):
        if self.current_trigger >= self.total_triggers:
            return

        if self.current_trigger == 0:
            for i in range(2):
                self.lf6_setup.acquire()

        spectrum_data = self.lf6_setup.acquire()

        if self.plot:
            plt.cla()
            self.data_plot.plot(self.wavelengths, spectrum_data)
            plt.pause(0.001)

        self.current_trigger += 1
        return spectrum_data

