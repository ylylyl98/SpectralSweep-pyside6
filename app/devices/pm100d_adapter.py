"""PyVISA/SCPI adapter for a Thorlabs PM100D power meter."""

from __future__ import annotations

import math

try:
    import pyvisa
except ImportError:  # pragma: no cover - exercised only in incomplete installs
    pyvisa = None


PM100D_VENDOR_ID = "0X1313"
PM100D_PRODUCT_ID = "0X8078"
PM100D_DEFAULT_WAVELENGTH_NM = 730.0
PM100D_DEFAULT_AVERAGING_COUNT = 1000
PM100D_TIMEOUT_MS = 5000

# PyVISA ResourceManager sessions are process-wide and their destructor closes
# the shared session. Keep one manager alive for the process lifetime rather
# than allowing repeated discovery calls to create retained wrappers.
_PM100D_RESOURCE_MANAGER = None


def _decode_tlpm_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    return str(value)


def _normalize_resource_name(resource_name) -> str:
    text = _decode_tlpm_text(resource_name).replace("\x00", "").strip()
    if text.upper().startswith("USB"):
        text = text.rstrip(":")
    if text.upper().startswith("USB") and "::" in text and not text.upper().endswith("::INSTR"):
        text = f"{text}::INSTR"
    return text


def _is_placeholder_serial(serial: str) -> bool:
    return serial.lower() in ("n/a", "na", "", "unknown", "none")


def _strip_na_serial(resource_name: str) -> str:
    if not resource_name.upper().startswith("USB"):
        return resource_name
    parts = resource_name.split("::")
    if len(parts) == 5 and _is_placeholder_serial(parts[3]):
        parts[3] = ""
        return "::".join(parts)
    return resource_name


def _repair_usb_resource_name(resource_name: str, serial_number: str) -> str:
    """Normalize a USB VISA name and replace a missing serial when available."""
    text = _normalize_resource_name(resource_name)
    serial = _decode_tlpm_text(serial_number).replace("\x00", "").strip()
    if not text.upper().startswith("USB"):
        return text
    if _is_placeholder_serial(serial):
        serial = ""
    parts = text.split("::")
    if len(parts) != 5:
        return text
    if _is_placeholder_serial(parts[3]) and serial:
        parts[3] = serial
    return "::".join(parts)


def _is_pm100d_resource(resource_name: str) -> bool:
    """Return whether a VISA resource has the PM100D's USB VID/PID."""
    parts = _normalize_resource_name(resource_name).split("::")
    return (
        len(parts) >= 5
        and parts[0].upper().startswith("USB")
        and parts[1].upper() == PM100D_VENDOR_ID
        and parts[2].upper() == PM100D_PRODUCT_ID
    )


def _usb_vid_pid(resource_name: str) -> tuple[str, str] | None:
    parts = _normalize_resource_name(resource_name).split("::")
    if len(parts) >= 3 and parts[0].upper().startswith("USB"):
        return parts[1].upper(), parts[2].upper()
    return None


def _require_pyvisa():
    if pyvisa is None:
        raise ImportError("pyvisa is required to connect to the PM100D.")
    return pyvisa


def _get_resource_manager():
    global _PM100D_RESOURCE_MANAGER
    _require_pyvisa()
    manager = _PM100D_RESOURCE_MANAGER
    if manager is not None:
        try:
            session = getattr(manager, "session", True)
        except Exception:
            session = None
        if session is None:
            manager = None
    if manager is None:
        manager = pyvisa.ResourceManager()
        _PM100D_RESOURCE_MANAGER = manager
    return manager


def _is_invalid_session_error(exc: Exception) -> bool:
    invalid_session = getattr(getattr(pyvisa, "errors", None), "InvalidSession", None)
    if isinstance(invalid_session, type) and isinstance(exc, invalid_session):
        return True
    text = _decode_tlpm_text(exc).lower()
    return "invalid session" in text or "vi_error_inv_object" in text


def _identity_metadata_from_name(resource_name: str) -> tuple[str, str, str, bool]:
    parts = resource_name.split("::")
    serial = parts[3] if len(parts) >= 5 and not _is_placeholder_serial(parts[3]) else ""
    return "PM100D", serial, "Thorlabs", True


def _identity_metadata(instrument, resource_name: str) -> tuple[str, str, str, bool]:
    model, serial, manufacturer, available = _identity_metadata_from_name(resource_name)
    try:
        idn = _decode_tlpm_text(instrument.query("*IDN?")).strip()
        idn_parts = [part.strip() for part in idn.split(",")]
        if len(idn_parts) > 0 and idn_parts[0]:
            manufacturer = idn_parts[0]
        if len(idn_parts) > 1 and idn_parts[1]:
            model = idn_parts[1]
        if len(idn_parts) > 2 and idn_parts[2]:
            serial = idn_parts[2]
    except Exception:
        available = False
    return model, serial, manufacturer, available


def scan_pm100d_resource_entries() -> list[dict[str, object]]:
    """Discover PM100D USB resources through VISA, retaining identity metadata."""
    _require_pyvisa()
    manager = _get_resource_manager()
    found: list[dict[str, object]] = []
    for raw_resource in manager.list_resources():
        resource = _normalize_resource_name(raw_resource)
        if not _is_pm100d_resource(resource):
            continue

        instrument = None
        model, serial, manufacturer, available = _identity_metadata_from_name(resource)
        try:
            instrument = manager.open_resource(resource)
            model, serial, manufacturer, available = _identity_metadata(instrument, resource)
        except Exception:
            available = False
        finally:
            if instrument is not None:
                try:
                    instrument.close()
                except Exception:
                    pass

        repaired_resource = _repair_usb_resource_name(resource, serial)
        found.append(
            {
                "resource": repaired_resource,
                "raw_resource": resource,
                "model": model,
                "serial": serial,
                "manufacturer": manufacturer,
                "available": available,
            }
        )
    return found


def scan_pm100d_resources() -> list[str]:
    return [str(entry["resource"]) for entry in scan_pm100d_resource_entries() if entry.get("resource")]


class ThorlabsPM100D_Wrapper:
    """Compatibility wrapper exposing the methods used by the UI/controller."""

    def __init__(self, resource_name_str):
        _require_pyvisa()
        requested = _normalize_resource_name(resource_name_str)
        if not requested:
            raise ValueError("No PM100D resource name was provided.")

        self.inst = None
        self.resource_name = ""
        self._wavelength_nm = PM100D_DEFAULT_WAVELENGTH_NM
        candidates = self._build_resource_candidates(requested)
        self.rm = _get_resource_manager()
        print(f"DEBUG: PM100D connect requested '{requested}'")
        print(f"DEBUG: PM100D connect candidates: {candidates}")

        attempts: list[str] = []
        for candidate in candidates:
            try:
                self.inst = self.rm.open_resource(candidate)
                self.resource_name = candidate
                print(f"DEBUG: PM100D connected via '{candidate}'")
                break
            except Exception as exc:
                attempts.append(f"{candidate}: {self._format_driver_error(exc)}")

        if self.inst is None:
            detail = "; ".join(attempts) if attempts else "no connection attempts were made"
            raise RuntimeError(f"Connection failed: {detail}")

        try:
            self._configure_instrument()
        except Exception as exc:
            print(
                "Warning: PM100D config failed "
                f"({self._format_driver_error(exc)}), but connection is active."
            )

    def _build_resource_candidates(self, requested: str) -> list[str]:
        seen: set[str] = set()
        candidates: list[str] = []

        def add(name: str) -> None:
            normalized = _normalize_resource_name(name)
            key = normalized.lower()
            if normalized and key not in seen:
                seen.add(key)
                candidates.append(normalized)

        requested_vid_pid = _usb_vid_pid(requested)
        requested_parts = requested.split("::")
        placeholder_serial = (
            requested_vid_pid == (PM100D_VENDOR_ID, PM100D_PRODUCT_ID)
            and len(requested_parts) >= 5
            and _is_placeholder_serial(requested_parts[3])
        )
        try:
            scanned_entries = scan_pm100d_resource_entries()
        except Exception as exc:
            print(f"DEBUG: PM100D scan fallback unavailable: {self._format_driver_error(exc)}")
            scanned_entries = []

        if not placeholder_serial:
            # An explicit serial is authoritative; never silently connect to
            # another PM100D discovered on the same VID/PID.
            add(requested)
            return candidates

        scanned_matches: list[str] = []
        for entry in scanned_entries:
            name = str(entry.get("resource") or "")
            if requested_vid_pid is None or _usb_vid_pid(name) == requested_vid_pid:
                normalized = _normalize_resource_name(name)
                if normalized and normalized.lower() not in {item.lower() for item in scanned_matches}:
                    scanned_matches.append(normalized)

        if len(scanned_matches) > 1:
            raise RuntimeError(
                "Ambiguous PM100D resource: multiple devices match the "
                "requested placeholder serial. Select a device with its serial."
            )
        if scanned_matches:
            add(scanned_matches[0])
        add(requested)

        if not candidates:
            add(_strip_na_serial(requested))
        return candidates

    def _format_driver_error(self, exc: Exception) -> str:
        parts = []
        for arg in getattr(exc, "args", ()):
            text = _decode_tlpm_text(arg).strip()
            if text:
                parts.append(text)
        return " | ".join(parts) if parts else _decode_tlpm_text(exc).strip()

    def _configure_instrument(self):
        self.inst.timeout = PM100D_TIMEOUT_MS
        self.inst.write(f"SENS:CORR:WAV {self._wavelength_nm:g}")
        self.inst.write("SENS:POW:RANG:AUTO ON")
        self.inst.write("SENS:POW:UNIT W")
        self.inst.write("SENS:AVER ON")
        self.inst.write(f"SENS:AVER:COUN {PM100D_DEFAULT_AVERAGING_COUNT}")

    def _recover_connection(self) -> bool:
        previous_instrument = self.inst
        instrument = None
        try:
            manager = _get_resource_manager()
            instrument = manager.open_resource(self.resource_name)
            self.inst = instrument
            self._configure_instrument()
            self.rm = manager
        except Exception:
            self.inst = None
            if instrument is not None:
                try:
                    instrument.close()
                except Exception:
                    pass
            return False

        if previous_instrument is not instrument:
            try:
                previous_instrument.close()
            except Exception:
                pass
        return True

    def get_power(self):
        if self.inst is None:
            return float("nan")
        try:
            return float(self.inst.query("MEAS:POW?").strip())
        except Exception as exc:
            if not _is_invalid_session_error(exc) or not self._recover_connection():
                return float("nan")
            try:
                return float(self.inst.query("MEAS:POW?").strip())
            except Exception:
                return float("nan")

    def configure_wavelength(self, nm):
        if self.inst is None:
            raise RuntimeError("PM100D is not connected.")
        try:
            value = float(nm)
            if not math.isfinite(value):
                raise ValueError("wavelength must be finite")
            self.inst.write(f"SENS:CORR:WAV {value:g}")
            self._wavelength_nm = value
            print(f"DEBUG: Wavelength set to {nm} nm")
        except Exception as exc:
            print(f"Error setting wavelength: {self._format_driver_error(exc)}")
            raise

    def set_wavelength(self, nm):
        self.configure_wavelength(nm)

    def close(self):
        instrument = self.inst
        self.inst = None
        if instrument is not None:
            try:
                instrument.close()
            except Exception:
                pass
