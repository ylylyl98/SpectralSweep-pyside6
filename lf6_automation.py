# Import the .NET class library
import clr, ctypes
import time

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


class LightFieldSettingTimeoutError(TimeoutError):
    """A LightField setting never became writable within the bounded wait."""


class LF6Setup:
    def __init__(self):
        self.auto = Automation(True, List[String]())
        self.application = self.auto.LightFieldApplication
        self.experiment = self.application.Experiment
        self.exp_settings = ExperimentSettings
        self.spectrometer_settings = SpectrometerSettings
        self._center_wavelength_write_stats = None

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

    def acquire(self):
        frames = 1
        dataset = self.experiment.Capture(frames)
        image_data = dataset.GetFrame(0, frames - 1).GetData()
        image_frame = dataset.GetFrame(0, frames - 1)
        array = self.convert_buffer(image_data, image_frame.Format)
        return array

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

    def acquire_2d(self):
        """
        Capture one frame and return a 2D array (H, W) if frame dimensions are available.
        If dimension detection fails, returns the raw 1D array (same as acquire()).
        """
        frames = 1
        dataset = self.experiment.Capture(frames)

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
        if self.experiment.Exists(CameraSettings.ShutterTimingExposureTime):
            self.experiment.SetValue(CameraSettings.ShutterTimingExposureTime, value)
            print(String.Format("{0} {1}", "Exposetime(ms):",
                                str(self.experiment.GetValue(
                                    CameraSettings.ShutterTimingExposureTime))))

    def change_spectra_center(self, value):
        """Legacy center setter routed through the guarded shared path."""
        return self.set_center_wavelength_when_ready(value)

    def _set_center_wavelength_raw(self, value):
        """Perform exactly one authoritative CenterWavelength SetValue attempt."""
        setting = SpectrometerSettings.GratingCenterWavelength
        if not bool(self.experiment.Exists(setting)):
            raise RuntimeError("LightField setting GratingCenterWavelength is unavailable")
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
        """Best-effort acquisition/load state; absent APIs are treated as idle."""
        values = []
        for obj in (self.application, self.experiment):
            value = self._flag(
                obj,
                ("IsBusy", "Busy", "IsAcquiring", "Acquiring", "IsLoading", "Loading"),
            )
            if value is not None:
                values.append(value)
        return any(values)

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

    def set_center_wavelength_when_ready(
        self,
        value,
        *,
        timeout_s: float = LIGHTFIELD_SETTING_TIMEOUT_S,
        poll_interval_s: float = LIGHTFIELD_POLL_INTERVAL_S,
    ) -> None:
        """Write center wavelength after readiness, retrying transient frozen states."""
        setting = SpectrometerSettings.GratingCenterWavelength
        timeout_s = float(timeout_s)
        if timeout_s <= 0:
            raise ValueError("LightField readiness timeout must be positive")
        started = time.monotonic()
        deadline = started + timeout_s
        last_error = None
        attempts = 0
        stats = {
            "setting": "GratingCenterWavelength",
            "requested_value": value,
            "attempts": 0,
            "result": "pending",
            "elapsed_s": 0.0,
            "last_exception": None,
            "state": {},
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
                attempts += 1
                stats["attempts"] = attempts
                readback = self._set_center_wavelength_raw(value)
                stats.update({
                    "result": "succeeded",
                    "elapsed_s": time.monotonic() - started,
                    "readback": readback,
                    "state": self._center_wavelength_state(setting),
                })
                return
            except LightFieldSettingTimeoutError as exc:
                # Preserve the bounded state/attempt diagnostics from the wait.
                stats.update({
                    "result": "timeout",
                    "elapsed_s": time.monotonic() - started,
                    "last_exception": self._exception_description(exc),
                    "state": self._center_wavelength_state(setting),
                })
                raise LightFieldSettingTimeoutError(
                    f"LightField setting GratingCenterWavelength requested={value!r} "
                    f"remained frozen/unavailable for {stats['elapsed_s']:.3f}s; "
                    f"SetValue attempts={attempts}; state={stats['state']}; "
                    f"last exception: {self._exception_description(exc)}"
                ) from exc
            except BaseException as exc:
                frozen = self._frozen_exception(exc)
                if frozen is None:
                    raise
                last_error = frozen
                stats["last_exception"] = self._exception_description(frozen)
                time.sleep(min(float(poll_interval_s), max(0.0, deadline - time.monotonic())))

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        """Apply the complete mutable run recipe immediately before acquisition."""
        self.set_center_wavelength_when_ready(float(center_nm))
        self.change_expose_time(float(exposure_ms))
        self.change_frame_to_combine(int(frames))
        return {
            "center_wavelength": self.center_wavelength_write_stats,
            "exposure_ms": float(exposure_ms),
            "frames": int(frames),
        }

    def _center_wavelength_state(self, setting) -> dict:
        return {
            "ready": self.is_ready,
            "busy": self.is_busy,
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
        try:
            # FIX: Use 'Int64' directly because you used 'from System import *'
            val = Int64(int(frames))

            if self.experiment.Exists(ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined):
                self.experiment.SetValue(
                    ExperimentSettings.OnlineProcessingFrameCombinationFramesCombined,
                    val
                )
                print(f"Frame_to_combine sets to: {frames}")
            else:
                print("Setting OnlineProcessingFrameCombinationFramesCombined not found.")
        except Exception as e:
            print(f"change_frame_to_combine failed: {e}")

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
                
                dataset = self.experiment.Capture(frames)
                bufferdata = np.zeros((frames,x_pixels_num*y_pixels_num))
                for i in range(frames):
                    image_data = dataset.GetFrame(0, i).GetData()
                    image_frame = dataset.GetFrame(0, i)
                    array = self.convert_buffer(image_data, image_frame.Format)
                    bufferdata[i,:] = array
                return bufferdata
            else:
                dataset = self.experiment.Capture(frames)
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

