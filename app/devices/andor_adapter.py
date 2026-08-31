"""Andor SDK2 camera + Shamrock spectrograph backend.

The pylablib camera and spectrograph objects are created and used on one
dedicated Python thread.  UI and sweep workers interact with this module via
synchronous proxy methods, so an SDK object is never shared directly between
the application's several worker threads.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import queue
import threading
from typing import Any, Callable, Optional

import numpy as np

from .spectrum_alignment import align_wavelengths_to_intensities


_SHAMROCK_DISCOVERY_LOCK = threading.Lock()


@dataclass(frozen=True)
class AndorConnectionOptions:
    camera_role: str = "ingaas"
    camera_index: int = 0
    camera_serial: str = ""
    spectrograph_index: int = 0
    sdk2_dll_dir: str = ""
    shamrock_dll_dir: str = ""
    temperature_c: float = -75.0
    cooler_on_connect: bool = False
    fan_mode: str = "full"
    output_port: str = "unchanged"
    shutter_mode: str = "auto"
    grating: int = 1
    slit_width_um: float = 1000.0
    invert_wavelength_axis: bool = True
    discard_first: bool = False
    timeout_margin_s: float = 2.0
    operation_timeout_s: float = 120.0


class _OwnerThread:
    """Small synchronous executor whose jobs all run on one owner thread."""

    def __init__(self, name: str = "AndorSDKOwner") -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def ident(self) -> Optional[int]:
        return self._thread.ident

    def call(self, fn: Callable[[], Any], timeout_s: float) -> Any:
        if threading.get_ident() == self._thread.ident:
            return fn()
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._jobs.put((fn, reply))
        try:
            ok, value = reply.get(timeout=max(0.1, float(timeout_s)))
        except queue.Empty as exc:
            raise TimeoutError(
                f"Andor SDK operation exceeded {float(timeout_s):g} s"
            ) from exc
        if ok:
            return value
        raise value

    def close(self, timeout_s: float = 5.0) -> None:
        if not self._thread.is_alive():
            return
        self._jobs.put(None)
        self._thread.join(timeout=max(0.1, float(timeout_s)))

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, reply = job
            try:
                reply.put((True, fn()))
            except BaseException as exc:
                reply.put((False, exc))


class AndorSDK2Setup:
    """LF6Setup-compatible facade for an Andor SDK2/Shamrock pair."""

    def __init__(
        self,
        options: AndorConnectionOptions,
        *,
        andor_module: Any = None,
    ) -> None:
        self.options = options
        self._owner = _OwnerThread()
        self._andor_module = andor_module
        self._camera = None
        self._spectrograph = None
        self._closed = False
        self._busy = threading.Event()
        self._abort = threading.Event()
        self._exposure_ms = 1000.0
        self._accumulations = 1
        self._center_nm = 860.0
        self._roi_mode = "LineSensor"
        self._image_roi_settings: Optional[dict[str, int]] = None
        self._output_port = str(options.output_port or "unchanged")
        self._identity: dict[str, Any] = {}
        self._stored_calibration_cache: dict[tuple[Any, ...], np.ndarray] = {}
        self._last_control_snapshot: dict[str, Any] = {}
        try:
            self._call(self._connect_on_owner)
        except BaseException:
            self._owner.close()
            raise

    def _call(self, fn: Callable[[], Any], timeout_s: Optional[float] = None) -> Any:
        if self._closed:
            raise RuntimeError("Andor spectrometer is disconnected")
        return self._owner.call(
            fn,
            self.options.operation_timeout_s if timeout_s is None else timeout_s,
        )

    def _load_andor_module(self):
        if self._andor_module is not None:
            return self._andor_module
        import pylablib as pll

        if self.options.sdk2_dll_dir:
            pll.par["devices/dlls/andor_sdk2"] = self.options.sdk2_dll_dir
        if self.options.shamrock_dll_dir:
            pll.par["devices/dlls/andor_shamrock"] = self.options.shamrock_dll_dir
        from pylablib.devices import Andor

        self._andor_module = Andor
        return Andor

    @staticmethod
    def _serial_from_info(info: Any) -> str:
        if isinstance(info, dict):
            return str(info.get("serial_number", info.get("serial", "")))
        if isinstance(info, (tuple, list)) and len(info) >= 3:
            return str(info[2])
        return str(getattr(info, "serial_number", ""))

    def _select_camera_index(self, module: Any) -> tuple[int, list[dict[str, Any]]]:
        count = int(module.get_cameras_number_SDK2())
        if count < 1:
            raise RuntimeError("No Andor SDK2 cameras were detected")
        discovered: list[dict[str, Any]] = []
        selected = None
        wanted_serial = self.options.camera_serial.strip()
        for index in range(count):
            camera = module.AndorSDK2Camera(
                idx=index, temperature="off", fan_mode="off"
            )
            try:
                info = camera.get_device_info()
                serial = self._serial_from_info(info)
                discovered.append({"index": index, "serial": serial, "info": info})
                if wanted_serial and serial == wanted_serial:
                    selected = index
            finally:
                camera.close()
        if wanted_serial and selected is None:
            available = ", ".join(
                f"idx={item['index']} serial={item['serial'] or '?'}"
                for item in discovered
            )
            raise RuntimeError(
                f"Configured Andor {self.options.camera_role} serial "
                f"{wanted_serial!r} was not found; detected: {available}"
            )
        if selected is None:
            selected = int(self.options.camera_index)
        if selected < 0 or selected >= count:
            raise RuntimeError(
                f"Configured Andor camera index {selected} is outside 0..{count - 1}"
            )
        return selected, discovered

    @staticmethod
    def _open_shamrock(module: Any, index: int) -> tuple[Any, Optional[str]]:
        """Open a Shamrock while tolerating the SDK's corrupt extra serial slots.

        The supplied SR500i notebook records one valid serial followed by six
        non-text entries.  pylablib determines the device count by decoding
        every serial, so one undecodable extra entry prevents even index 0 from
        opening.  Retry using the vendor-reported count only for that specific
        decoding failure; all other SDK and hardware errors still propagate.
        """
        try:
            return module.ShamrockSpectrograph(idx=index), None
        except UnicodeDecodeError as decode_error:
            implementation = importlib.import_module(
                module.ShamrockSpectrograph.__module__
            )
            original_count = implementation.get_spectrographs_number

            def raw_device_count() -> int:
                with implementation.libctl.temp_open():
                    return int(implementation.lib.ShamrockGetNumberDevices())

            with _SHAMROCK_DISCOVERY_LOCK:
                implementation.get_spectrographs_number = raw_device_count
                try:
                    spectrograph = module.ShamrockSpectrograph(idx=index)
                finally:
                    implementation.get_spectrographs_number = original_count
            warning = (
                "Shamrock serial enumeration returned corrupt extra entries; "
                f"opened configured spectrograph index {index} directly "
                f"({decode_error})"
            )
            return spectrograph, warning

    def _connect_on_owner(self) -> None:
        module = self._load_andor_module()
        index, discovered = self._select_camera_index(module)
        connection_warnings: list[str] = []
        initial_temperature = (
            float(self.options.temperature_c)
            if self.options.cooler_on_connect else "off"
        )
        camera = module.AndorSDK2Camera(
            idx=index,
            temperature=initial_temperature,
            fan_mode=self.options.fan_mode,
        )
        try:
            spectrograph, discovery_warning = self._open_shamrock(
                module, int(self.options.spectrograph_index)
            )
            if discovery_warning:
                connection_warnings.append(discovery_warning)
        except BaseException:
            camera.close()
            raise
        self._camera = camera
        self._spectrograph = spectrograph
        try:
            if hasattr(camera, "set_temperature"):
                camera.set_temperature(
                    float(self.options.temperature_c),
                    enable_cooler=bool(self.options.cooler_on_connect),
                )
            if self.options.fan_mode and hasattr(camera, "set_fan_mode"):
                try:
                    camera.set_fan_mode(self.options.fan_mode)
                except Exception as exc:
                    connection_warnings.append(f"fan mode was not applied: {exc}")
            if self.options.shutter_mode and hasattr(camera, "setup_shutter"):
                try:
                    camera.setup_shutter(self.options.shutter_mode)
                except Exception as exc:
                    connection_warnings.append(f"shutter mode was not applied: {exc}")
            if hasattr(camera, "set_cooler"):
                try:
                    camera.set_cooler(on=bool(self.options.cooler_on_connect))
                except Exception as exc:
                    if self.options.cooler_on_connect:
                        raise
                    connection_warnings.append(f"cooler-off was not confirmed: {exc}")
            self._configure_optics_on_owner()
            info = camera.get_device_info()
            self._identity = {
                "backend": "andor_sdk2",
                "camera_role": self.options.camera_role,
                "camera_index": index,
                "camera_serial": self._serial_from_info(info),
                "device_info": info,
                "detector_size": camera.get_detector_size(),
                "spectrograph_index": int(self.options.spectrograph_index),
                "discovered_cameras": discovered,
                "owner_thread_id": threading.get_ident(),
                "connection_warnings": connection_warnings,
            }
            try:
                spec_info = spectrograph.get_device_info()
                self._identity["spectrograph_serial"] = str(
                    getattr(spec_info, "serial_number", spec_info[0])
                )
            except Exception as exc:
                connection_warnings.append(
                    f"Shamrock serial readback was unavailable: {exc}"
                )
        except BaseException:
            try:
                spectrograph.close()
            except Exception:
                pass
            camera.close()
            self._camera = None
            self._spectrograph = None
            raise

    def _configure_optics_on_owner(self) -> None:
        spec = self._require_spectrograph()
        if hasattr(spec, "set_grating"):
            spec.set_grating(int(self.options.grating))
        if hasattr(spec, "set_slit_width"):
            spec.set_slit_width(
                "input_side", float(self.options.slit_width_um) * 1e-6
            )
        self._stored_calibration_cache.clear()

    @staticmethod
    def _plain_value(value: Any) -> Any:
        """Convert pylablib named tuples and numpy scalars to UI-safe values."""
        if hasattr(value, "_asdict"):
            return {
                str(key): AndorSDK2Setup._plain_value(item)
                for key, item in value._asdict().items()
            }
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, tuple):
            return tuple(AndorSDK2Setup._plain_value(item) for item in value)
        if isinstance(value, list):
            return [AndorSDK2Setup._plain_value(item) for item in value]
        return value

    def _stored_calibration_key_on_owner(self) -> tuple[Any, ...]:
        camera = self._require_camera()
        spec = self._require_spectrograph()
        detector_size = tuple(int(v) for v in camera.get_detector_size())
        pixel_size = tuple(round(float(v), 15) for v in camera.get_pixel_size())
        read_mode = str(camera.get_read_mode())
        try:
            roi = tuple(camera.get_roi())
        except Exception:
            roi = ()
        return (
            self.options.camera_role,
            self._identity.get("camera_serial", ""),
            detector_size,
            pixel_size,
            str(self._roi_mode),
            read_mode,
            roi,
            int(spec.get_grating()),
            round(float(spec.get_wavelength()) * 1e9, 6),
        )

    def _read_stored_calibration_on_owner(self, force: bool = False) -> np.ndarray:
        """Read the wavelength axis derived from Shamrock's stored coefficients.

        This only supplies the active detector geometry and reads the factory/user
        coefficients already stored by the Shamrock controller.  It never fits,
        writes, resets, or otherwise changes calibration coefficients.
        """
        camera = self._require_camera()
        spec = self._require_spectrograph()
        spec.setup_pixels_from_camera(camera)
        key = self._stored_calibration_key_on_owner()
        if force or key not in self._stored_calibration_cache:
            wavelengths = (
                np.asarray(spec.get_calibration(), dtype=float).ravel() * 1e9
            )
            if self.options.invert_wavelength_axis:
                wavelengths = wavelengths[::-1]
            self._stored_calibration_cache[key] = wavelengths.copy()
        return self._stored_calibration_cache[key].copy()

    def _control_snapshot_on_owner(self, include_calibration: bool = True) -> dict[str, Any]:
        camera = self._require_camera()
        spec = self._require_spectrograph()
        snapshot: dict[str, Any] = {
            "backend": "andor_sdk2",
            "camera_role": self.options.camera_role,
            "camera_index": self._identity.get("camera_index"),
            "camera_serial": self._identity.get("camera_serial", ""),
            "spectrograph_index": int(self.options.spectrograph_index),
            "spectrograph_serial": self._identity.get("spectrograph_serial", ""),
            "calibration_source": "Shamrock stored coefficients",
            "readback_errors": {},
        }

        def read(name: str, fn: Callable[[], Any], transform=None) -> None:
            try:
                value = fn()
                if transform is not None:
                    value = transform(value)
                snapshot[name] = self._plain_value(value)
            except Exception as exc:
                snapshot["readback_errors"][name] = str(exc)

        read("camera_device_info", camera.get_device_info)
        read("detector_size", camera.get_detector_size)
        read("pixel_size_um", camera.get_pixel_size, lambda value: tuple(float(v) * 1e6 for v in value))
        read("read_mode", camera.get_read_mode)
        read("roi", camera.get_roi)
        read("exposure_ms", camera.get_exposure, lambda value: float(value) * 1e3)
        read("temperature_c", camera.get_temperature)
        read("temperature_status", camera.get_temperature_status)
        read("temperature_setpoint_c", camera.get_temperature_setpoint)
        read("temperature_range_c", camera.get_temperature_range)
        read("cooler_on", camera.is_cooler_on)
        read("fan_mode", camera.get_fan_mode)

        read("spectrograph_device_info", spec.get_device_info)
        read("optical_parameters", spec.get_optical_parameters)
        read("wavelength_nm", spec.get_wavelength, lambda value: float(value) * 1e9)
        read("grating", spec.get_grating)
        read("gratings_number", spec.get_gratings_number)
        if "gratings_number" in snapshot:
            infos = []
            for grating in range(1, int(snapshot["gratings_number"]) + 1):
                try:
                    info = self._plain_value(spec.get_grating_info(grating))
                    infos.append({"index": grating, "info": info})
                except Exception as exc:
                    snapshot["readback_errors"][f"grating_info_{grating}"] = str(exc)
            snapshot["grating_infos"] = infos
        current_grating = snapshot.get("grating")
        if current_grating is not None:
            read(
                "wavelength_limits_nm",
                lambda: spec.get_wavelength_limits(int(current_grating)),
                lambda value: tuple(float(v) * 1e9 for v in value),
            )
        read("input_slit_present", lambda: spec.is_slit_present("input_side"))
        if snapshot.get("input_slit_present"):
            read(
                "input_slit_width_um",
                lambda: spec.get_slit_width("input_side"),
                lambda value: float(value) * 1e6,
            )
        read("shutter_present", spec.is_shutter_present)
        if snapshot.get("shutter_present"):
            read("shutter_mode", spec.get_shutter)
        read("output_flipper_present", lambda: spec.is_flipper_present("output"))
        if snapshot.get("output_flipper_present"):
            read("output_port", lambda: spec.get_flipper_port("output"))
        if include_calibration:
            read("stored_calibration_nm", self._read_stored_calibration_on_owner)
            calibration = snapshot.get("stored_calibration_nm")
            if isinstance(calibration, np.ndarray):
                calibration = calibration.tolist()
                snapshot["stored_calibration_nm"] = calibration
            if isinstance(calibration, list) and calibration:
                snapshot["calibration_pixel_count"] = len(calibration)
                snapshot["calibration_range_nm"] = (
                    float(calibration[0]),
                    float(calibration[-1]),
                )
        self._last_control_snapshot = dict(snapshot)
        return snapshot

    def get_control_snapshot(self, include_calibration: bool = True) -> dict[str, Any]:
        return self._call(
            lambda: self._control_snapshot_on_owner(include_calibration),
            timeout_s=self.options.operation_timeout_s,
        )

    def apply_controls(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Apply requested Andor controls and return verified hardware readbacks."""
        requested = dict(settings or {})

        def apply() -> dict[str, Any]:
            camera = self._require_camera()
            spec = self._require_spectrograph()
            optics_changed = False
            if "grating" in requested:
                expected = int(requested["grating"])
                result = spec.set_grating(expected)
                actual = int(result if result is not None else spec.get_grating())
                if actual != expected:
                    raise RuntimeError(f"Grating readback {actual} != requested {expected}")
                optics_changed = True
            if "wavelength_nm" in requested:
                expected = float(requested["wavelength_nm"])
                result = spec.set_wavelength(expected * 1e-9)
                actual = float(
                    result if result is not None else spec.get_wavelength()
                ) * 1e9
                if abs(actual - expected) > 0.1:
                    raise RuntimeError(
                        f"Wavelength readback {actual:.3f} nm != requested {expected:.3f} nm"
                    )
                self._center_nm = actual
                optics_changed = True
            if "input_slit_width_um" in requested:
                expected = float(requested["input_slit_width_um"])
                if not spec.is_slit_present("input_side"):
                    raise RuntimeError("The Shamrock input-side motorized slit is not present")
                result = spec.set_slit_width("input_side", expected * 1e-6)
                actual = float(
                    result
                    if result is not None
                    else spec.get_slit_width("input_side")
                ) * 1e6
                if abs(actual - expected) > max(0.5, abs(expected) * 0.01):
                    raise RuntimeError(
                        f"Slit readback {actual:.1f} µm != requested {expected:.1f} µm"
                    )
                optics_changed = True
            shutter_mode = str(requested.get("shutter_mode", "unchanged"))
            if shutter_mode != "unchanged":
                if not spec.is_shutter_present():
                    raise RuntimeError("The Shamrock shutter is not present")
                actual = str(spec.set_shutter(shutter_mode))
                if actual != shutter_mode:
                    raise RuntimeError(
                        f"Shutter readback {actual!r} != requested {shutter_mode!r}"
                    )
            if "output_port" in requested:
                output_port = str(requested["output_port"])
                if output_port not in {"unchanged", "direct", "side"}:
                    raise ValueError(f"Unsupported Shamrock output port: {output_port}")
                self._output_port = output_port
                if output_port != "unchanged":
                    self._ensure_output_port_on_owner(output_port)
            cooler_requested = requested.get("cooler_on")
            if "temperature_setpoint_c" in requested:
                enable = (
                    bool(cooler_requested)
                    if cooler_requested is not None
                    else bool(camera.is_cooler_on())
                )
                camera.set_temperature(
                    float(requested["temperature_setpoint_c"]),
                    enable_cooler=enable,
                )
            elif cooler_requested is not None:
                camera.set_cooler(on=bool(cooler_requested))
            if "fan_mode" in requested:
                camera.set_fan_mode(str(requested["fan_mode"]))
            if "read_mode" in requested:
                read_mode = str(requested["read_mode"])
                if read_mode not in {"fvb", "image"}:
                    raise ValueError(f"Unsupported Andor read mode: {read_mode}")
                if read_mode == "image" and self.options.camera_role == "ingaas":
                    raise RuntimeError(
                        "The InGaAs detector is a one-dimensional array and "
                        "does not support 2D image mode"
                    )
                camera.set_read_mode(read_mode)
                self._roi_mode = "FullSensor" if read_mode == "image" else "LineSensor"
                if read_mode == "image":
                    hbin = max(1, int(requested.get("horizontal_binning", 1)))
                    vbin = max(1, int(requested.get("vertical_binning", 1)))
                    detector_width, detector_height = camera.get_detector_size()
                    hstart = max(0, int(requested.get("roi_hstart", 0)))
                    hend_value = int(requested.get("roi_hend", 0))
                    hend = detector_width if hend_value <= 0 else hend_value
                    vstart = max(0, int(requested.get("roi_vstart", 0)))
                    vend_value = int(requested.get("roi_vend", 0))
                    vend = detector_height if vend_value <= 0 else vend_value
                    if not (hstart < hend <= detector_width):
                        raise ValueError(
                            "Horizontal ROI must satisfy "
                            f"0 <= start < end <= {detector_width}"
                        )
                    if not (vstart < vend <= detector_height):
                        raise ValueError(
                            "Vertical ROI must satisfy "
                            f"0 <= start < end <= {detector_height}"
                        )
                    roi_settings = {
                        "hstart": hstart,
                        "hend": hend,
                        "vstart": vstart,
                        "vend": vend,
                        "hbin": hbin,
                        "vbin": vbin,
                    }
                    camera.set_roi(
                        **roi_settings,
                    )
                    self._image_roi_settings = roi_settings
                optics_changed = True
            if optics_changed:
                self._stored_calibration_cache.clear()
            return self._control_snapshot_on_owner(include_calibration=True)

        return self._call(apply, timeout_s=self.options.operation_timeout_s)

    def _ensure_output_port_on_owner(self, output_port: str) -> str:
        spec = self._require_spectrograph()
        if not spec.is_flipper_present("output"):
            raise RuntimeError("The Shamrock output flipper mirror is not present")
        actual = str(spec.get_flipper_port("output"))
        if actual != output_port:
            result = spec.set_flipper_port("output", output_port)
            actual = str(
                result
                if result is not None
                else spec.get_flipper_port("output")
            )
        if actual != output_port:
            raise RuntimeError(
                f"Output-port readback {actual!r} != requested {output_port!r}"
            )
        return actual

    def _require_camera(self):
        if self._camera is None:
            raise RuntimeError("Andor camera is unavailable")
        return self._camera

    def _require_spectrograph(self):
        if self._spectrograph is None:
            raise RuntimeError("Andor Shamrock spectrograph is unavailable")
        return self._spectrograph

    @property
    def identity(self) -> dict[str, Any]:
        return dict(self._identity)

    @property
    def is_ready(self) -> bool:
        return (
            not self._closed
            and self._camera is not None
            and self._spectrograph is not None
            and not self._busy.is_set()
        )

    @property
    def is_busy(self) -> bool:
        return self._busy.is_set()

    @property
    def readiness_snapshot(self) -> dict[str, Any]:
        return {
            "ready": self.is_ready,
            "busy": self.is_busy,
            "backend": "andor_sdk2",
            "camera_role": self.options.camera_role,
        }

    def get_saved_experiments(self) -> list[str]:
        return []

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        center_nm = float(center_nm)
        exposure_ms = float(exposure_ms)
        frames = int(frames)
        if exposure_ms <= 0 or frames < 1:
            raise ValueError("Andor exposure and accumulation count must be positive")

        def configure():
            camera = self._require_camera()
            spec = self._require_spectrograph()
            output_port = None
            if self._output_port != "unchanged":
                output_port = self._ensure_output_port_on_owner(self._output_port)
            center_result = spec.set_wavelength(center_nm * 1e-9)
            actual_center_nm = (
                float(center_result) * 1e9
                if center_result is not None
                else center_nm
            )
            set_read_mode = getattr(camera, "set_read_mode", None)
            if callable(set_read_mode):
                set_read_mode("fvb")
                self._roi_mode = "LineSensor"
            exposure_result = camera.set_exposure(exposure_ms / 1000.0)
            actual_exposure_ms = (
                float(exposure_result) * 1e3
                if exposure_result is not None
                else exposure_ms
            )
            self._center_nm = actual_center_nm
            self._exposure_ms = actual_exposure_ms
            self._accumulations = frames
            spec.setup_pixels_from_camera(camera)
            self._stored_calibration_cache.clear()
            calibration = self._read_stored_calibration_on_owner()
            detector_width = int(camera.get_detector_size()[0])
            if calibration.size != detector_width:
                raise RuntimeError(
                    "Stored Shamrock calibration length "
                    f"{calibration.size} does not match detector width {detector_width}"
                )
            if abs(actual_center_nm - center_nm) > 0.1:
                raise RuntimeError(
                    f"Wavelength readback {actual_center_nm:.3f} nm does not match "
                    f"requested {center_nm:.3f} nm"
                )
            current_grating = int(spec.get_grating())
            slit_width_um = None
            if spec.is_slit_present("input_side"):
                slit_width_um = float(spec.get_slit_width("input_side")) * 1e6
            return {
                "backend": "andor_sdk2",
                "center_wavelength": {
                    "result": "succeeded",
                    "requested_value": center_nm,
                    "readback": actual_center_nm,
                },
                "exposure_ms": actual_exposure_ms,
                "frames": frames,
                "grating": current_grating,
                "input_slit_width_um": slit_width_um,
                "output_port": output_port,
                "calibration_source": "Shamrock stored coefficients",
                "calibration_pixel_count": int(calibration.size),
            }

        return self._call(configure)

    def set_center_wavelength_when_ready(self, center_nm, **_kwargs) -> None:
        self.configure_for_acquisition(
            center_nm=float(center_nm),
            exposure_ms=self._exposure_ms,
            frames=self._accumulations,
        )

    def change_spectra_center(self, center_nm) -> None:
        self.set_center_wavelength_when_ready(center_nm)

    def change_center_wavelength(self, center_nm) -> None:
        self.set_center_wavelength_when_ready(center_nm)

    def change_expose_time(self, exposure_ms) -> None:
        self.configure_for_acquisition(
            center_nm=self._center_nm,
            exposure_ms=float(exposure_ms),
            frames=self._accumulations,
        )

    def change_frame_to_combine(self, frames: int) -> None:
        self.configure_for_acquisition(
            center_nm=self._center_nm,
            exposure_ms=self._exposure_ms,
            frames=int(frames),
        )

    def get_wavelength_calibration(self, force: bool = False) -> np.ndarray:
        return self._call(lambda: self._read_stored_calibration_on_owner(force=force))

    def _cleanup_acquisition_on_owner(self) -> None:
        camera = self._require_camera()
        for name in ("stop_acquisition", "clear_acquisition"):
            method = getattr(camera, name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass

    def _capture_frames_on_owner(self) -> np.ndarray:
        camera = self._require_camera()
        self._cleanup_acquisition_on_owner()
        camera.set_exposure(self._exposure_ms / 1000.0)
        timeout = max(
            self._exposure_ms / 1000.0 + self.options.timeout_margin_s,
            self.options.timeout_margin_s,
        )
        count = self._accumulations + (1 if self.options.discard_first else 0)
        frames = []
        try:
            for _ in range(count):
                if self._abort.is_set():
                    raise RuntimeError("Andor acquisition was cancelled")
                frame = np.asarray(camera.snap(timeout=timeout), dtype=np.float32)
                # pylablib commonly returns one requested frame with a leading
                # acquisition dimension: (1, x) or (1, y, x).
                if frame.ndim >= 2 and frame.shape[0] == 1:
                    frame = frame[0]
                frames.append(frame)
        finally:
            self._cleanup_acquisition_on_owner()
        if self.options.discard_first:
            frames = frames[1:]
        if not frames:
            raise RuntimeError("Andor camera returned no frames")
        shapes = {frame.shape for frame in frames}
        if len(shapes) != 1:
            raise RuntimeError(f"Andor frame shape changed during acquisition: {shapes}")
        return np.mean(np.stack(frames, axis=0), axis=0, dtype=np.float64)

    def acquire_2d(self) -> np.ndarray:
        self._abort.clear()
        self._busy.set()
        timeout = (
            self.options.operation_timeout_s
            + (self._exposure_ms / 1000.0 + self.options.timeout_margin_s)
            * self._accumulations
        )
        try:
            frame = np.asarray(
                self._call(self._capture_frames_on_owner, timeout_s=timeout),
                dtype=float,
            )
        finally:
            self._busy.clear()
        return frame

    def acquire(self) -> np.ndarray:
        frame = self.acquire_2d()
        wavelengths = self.get_wavelength_calibration()
        if frame.ndim == 1:
            return frame
        if frame.ndim != 2:
            raise RuntimeError(f"Unsupported Andor frame shape {frame.shape}")
        if frame.shape[-1] == wavelengths.size:
            return np.nanmean(frame, axis=0)
        if frame.shape[0] == wavelengths.size:
            return np.nanmean(frame, axis=1)
        raise RuntimeError(
            f"Andor frame shape {frame.shape} does not match "
            f"{wavelengths.size} calibration pixels"
        )

    def abort_acquisition(self) -> bool:
        self._abort.set()
        return self._busy.is_set()

    def change_roi_FullSensor(self) -> None:
        def change():
            camera = self._require_camera()
            method = getattr(camera, "set_read_mode", None)
            if callable(method):
                method("image")
            set_roi = getattr(camera, "set_roi", None)
            # Do not erase a custom image ROI/binning that was applied from
            # the Spectrum controls drawer.  The no-argument call is only the
            # transition default when entering image mode from line mode.
            if callable(set_roi):
                if self._image_roi_settings:
                    set_roi(**self._image_roi_settings)
                elif self._roi_mode != "FullSensor":
                    set_roi()
            self._roi_mode = "FullSensor"
            self._stored_calibration_cache.clear()

        self._call(change)

    def change_roi_LineSensor(self) -> None:
        def change():
            camera = self._require_camera()
            method = getattr(camera, "set_read_mode", None)
            if callable(method):
                method("fvb")
            self._roi_mode = "LineSensor"
            self._stored_calibration_cache.clear()

        self._call(change)

    def get_temperature(self) -> Any:
        return self._call(lambda: self._require_camera().get_temperature())

    def get_disconnect_safety_snapshot(self) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            camera = self._require_camera()
            return {
                "temperature_c": float(camera.get_temperature()),
                "temperature_status": str(camera.get_temperature_status()),
                "cooler_on": bool(camera.is_cooler_on()),
                "camera_role": self.options.camera_role,
            }

        return self._call(read, timeout_s=10.0)

    def set_cooler(self, on: bool) -> None:
        self._call(lambda: self._require_camera().set_cooler(on=bool(on)))

    def close(self) -> None:
        if self._closed:
            return

        def close_hardware():
            self._cleanup_acquisition_on_owner()
            if self._spectrograph is not None:
                try:
                    self._spectrograph.close()
                finally:
                    self._spectrograph = None
            if self._camera is not None:
                try:
                    self._camera.close()
                finally:
                    self._camera = None

        try:
            self._owner.call(close_hardware, self.options.operation_timeout_s)
        finally:
            self._closed = True
            self._owner.close()


class SpectrometerAndor:
    """Common spectrometer adapter consumed by all existing acquisition tabs."""

    def __init__(self, setup: AndorSDK2Setup) -> None:
        self.setup = setup
        self._wavelengths: Optional[np.ndarray] = None

    def invalidate_wavelengths(self) -> None:
        self._wavelengths = None

    def calibration_wavelengths(self, force: bool = False) -> np.ndarray:
        if force or self._wavelengths is None:
            self._wavelengths = self.setup.get_wavelength_calibration()
        return np.asarray(self._wavelengths, dtype=float).copy()

    def acquire(self):
        counts = np.asarray(self.setup.acquire(), dtype=float).ravel()
        wavelengths = self.calibration_wavelengths(force=False)
        return align_wavelengths_to_intensities(wavelengths, counts)

    def acquire_2d(self) -> np.ndarray:
        return self.setup.acquire_2d()

    def abort_acquisition(self) -> bool:
        return self.setup.abort_acquisition()

    def configure_for_acquisition(self, *, center_nm, exposure_ms, frames):
        result = self.setup.configure_for_acquisition(
            center_nm=center_nm, exposure_ms=exposure_ms, frames=frames
        )
        self.invalidate_wavelengths()
        return result

    def set_center_wavelength_when_ready(self, center_nm, **kwargs) -> None:
        self.setup.set_center_wavelength_when_ready(center_nm, **kwargs)
        self.invalidate_wavelengths()

    def change_spectra_center(self, center_nm) -> None:
        self.set_center_wavelength_when_ready(center_nm)

    def set_accumulations(self, frames: int) -> None:
        self.setup.change_frame_to_combine(frames)

    set_frames = set_accumulations

    def __getattr__(self, name):
        return getattr(self.setup, name)
