from __future__ import annotations

from ctypes import (
    byref,
    c_bool,
    c_double,
    c_int,
    c_int16,
    c_uint32,
    create_string_buffer,
)

try:
    from TLPMX import TLPMX, TLPM_DEFAULT_CHANNEL
except ImportError:
    TLPMX = None
    TLPM_DEFAULT_CHANNEL = 1


def _decode_tlpm_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    return str(value)


def _buffer_to_text(buf) -> str:
    return _decode_tlpm_text(getattr(buf, "value", b""))


def _normalize_resource_name(resource_name) -> str:
    text = _decode_tlpm_text(resource_name).replace("\x00", "").strip()
    if text.upper().startswith("USB"):
        text = text.rstrip(":")
    if text.upper().startswith("USB") and "::" in text and not text.upper().endswith("::INSTR"):
        text = f"{text}::INSTR"
    return text


def _is_placeholder_serial(serial: str) -> bool:
    """Return True for non-real serial values that drivers sometimes emit."""
    return serial.lower() in ("n/a", "na", "", "unknown", "none")


def _strip_na_serial(resource_name: str) -> str:
    """
    If resource_name contains a placeholder serial like 'n/a', replace it with
    an empty field so the resource manager can still find the device by VID/PID.
    e.g. USB0::0x1313::0x8078::n/a::INSTR  →  USB0::0x1313::0x8078::::INSTR
    """
    if not resource_name.upper().startswith("USB"):
        return resource_name
    parts = resource_name.split("::")
    # USB parts: USB0, VID, PID, SERIAL, INSTR
    if len(parts) == 5 and _is_placeholder_serial(parts[3]):
        parts[3] = ""
        return "::".join(parts)
    return resource_name


def _repair_usb_resource_name(resource_name: str, serial_number: str) -> str:
    text = _normalize_resource_name(resource_name)
    serial = _decode_tlpm_text(serial_number).replace("\x00", "").strip()
    if not text.upper().startswith("USB"):
        return text
    # Treat placeholder serials the same as empty
    if _is_placeholder_serial(serial):
        serial = ""
    if "::::INSTR" not in text.upper():
        # Resource has a non-empty serial slot — check if it's a placeholder
        return _strip_na_serial(text)

    parts = [part for part in text.split("::") if part]
    if len(parts) < 3:
        return text
    prefix = "::".join(parts[:3])
    if serial:
        return f"{prefix}::{serial}::INSTR"
    return text


def scan_pm100d_resource_entries() -> list[dict[str, object]]:
    if TLPMX is None:
        raise ImportError("TLPMX.py is missing! Please put it in the project root.")

    tlpm = TLPMX()
    count = c_uint32()
    found: list[dict[str, object]] = []
    try:
        try:
            tlpm.setLookForInfoOnSearch(c_int16(1))
        except Exception:
            pass
        tlpm.findRsrc(byref(count))
        if count.value <= 0:
            return []
        name_buf = create_string_buffer(1024)
        model_buf = create_string_buffer(1024)
        serial_buf = create_string_buffer(1024)
        maker_buf = create_string_buffer(1024)
        for idx in range(count.value):
            tlpm.getRsrcName(c_int(idx), name_buf)
            raw_name = _buffer_to_text(name_buf)
            model_name = ""
            serial_number = ""
            manufacturer = ""
            available = True
            try:
                avail_flag = c_int16(0)
                tlpm.getRsrcInfo(
                    c_int(idx),
                    model_buf,
                    serial_buf,
                    maker_buf,
                    byref(avail_flag),
                )
                model_name = _buffer_to_text(model_buf)
                serial_number = _buffer_to_text(serial_buf)
                manufacturer = _buffer_to_text(maker_buf)
                available = bool(avail_flag.value)
            except Exception:
                pass

            normalized_raw = _normalize_resource_name(raw_name)
            repaired_name = _repair_usb_resource_name(normalized_raw, serial_number)
            if repaired_name:
                found.append(
                    {
                        "resource": repaired_name,
                        "raw_resource": normalized_raw,
                        "model": model_name,
                        "serial": serial_number,
                        "manufacturer": manufacturer,
                        "available": available,
                    }
                )
        return found
    finally:
        try:
            tlpm.close()
        except Exception:
            pass


def scan_pm100d_resources() -> list[str]:
    return [str(entry["resource"]) for entry in scan_pm100d_resource_entries() if entry.get("resource")]


class ThorlabsPM100D_Wrapper:
    def __init__(self, resource_name_str):
        if TLPMX is None:
            raise ImportError("TLPMX.py is missing! Please put it in the project root.")

        requested = _normalize_resource_name(resource_name_str)
        if not requested:
            raise ValueError("No PM100D resource name was provided.")

        self.inst = None
        self.resource_name = ""

        candidates = self._build_resource_candidates(requested)
        print(f"DEBUG: PM100D connect requested '{requested}'")
        print(f"DEBUG: PM100D connect candidates: {candidates}")

        # Preferred order: IDQuery=True no-reset first (safe), then no-IDQuery,
        # reset=True last (it can briefly disconnect the USB device).
        _OPEN_PARAMS = [(True, False), (False, False), (True, True)]

        attempts: list[str] = []
        connected = False
        for candidate in candidates:
            for id_query, reset in _OPEN_PARAMS:
                # Always use a fresh TLPMX instance — a failed open() leaves the
                # previous instance in an invalid state that rejects all subsequent
                # open() calls on the same object.
                inst = TLPMX()
                try:
                    inst.open(
                        create_string_buffer(candidate.encode("ascii")),
                        c_bool(id_query),
                        c_bool(reset),
                    )
                    self.inst = inst
                    self.resource_name = candidate
                    connected = True
                    print(
                        "DEBUG: PM100D connected via "
                        f"'{candidate}' (IDQuery={id_query}, reset={reset})"
                    )
                    break
                except Exception as exc:
                    attempts.append(
                        f"{candidate} [IDQuery={id_query}, reset={reset}]: "
                        f"{self._format_driver_error(exc)}"
                    )
                    try:
                        inst.close()
                    except Exception:
                        pass
            if connected:
                break

        if not connected:
            detail = "; ".join(attempts) if attempts else "no connection attempts were made"
            raise RuntimeError(f"Connection failed: {detail}")

        try:
            self.inst.setWavelength(c_double(730), TLPM_DEFAULT_CHANNEL)
            self.inst.setPowerAutoRange(c_int16(1), TLPM_DEFAULT_CHANNEL)
            self.inst.setPowerUnit(c_int16(0), TLPM_DEFAULT_CHANNEL)
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

        # Strip placeholder serials before the real scan; try the cleaned form first.
        add(_strip_na_serial(requested))
        add(requested)  # also keep original in case stripping was wrong

        try:
            scanned_entries = scan_pm100d_resource_entries()
        except Exception as exc:
            print(f"DEBUG: PM100D scan fallback unavailable: {self._format_driver_error(exc)}")
            scanned_entries = []

        requested_upper = requested.upper()
        for entry in scanned_entries:
            name = str(entry.get("resource") or "")
            raw_name = str(entry.get("raw_resource") or "")
            serial = str(entry.get("serial") or "")
            name_upper = name.upper()
            raw_upper = raw_name.upper()
            if (
                name_upper == requested_upper
                or requested_upper in name_upper
                or name_upper in requested_upper
                or raw_upper == requested_upper
                or requested_upper in raw_upper
                or raw_upper in requested_upper
                or (serial and serial.upper() in requested_upper)
            ):
                add(name)
                add(raw_name)

        if len(candidates) == 1 and len(scanned_entries) == 1:
            add(str(scanned_entries[0].get("resource") or ""))
            add(str(scanned_entries[0].get("raw_resource") or ""))

        return candidates

    def _format_driver_error(self, exc: Exception) -> str:
        parts = []
        for arg in getattr(exc, "args", ()):
            text = _decode_tlpm_text(arg).strip()
            if text:
                parts.append(text)
        if parts:
            return " | ".join(parts)
        return _decode_tlpm_text(exc).strip()

    def get_power(self):
        if self.inst is None:
            return float("nan")
        val = c_double()
        try:
            self.inst.measPower(byref(val), TLPM_DEFAULT_CHANNEL)
            return val.value
        except Exception:
            return float("nan")

    def configure_wavelength(self, nm):
        try:
            val = c_double(float(nm))
            self.inst.setWavelength(val, TLPM_DEFAULT_CHANNEL)
            print(f"DEBUG: Wavelength set to {nm} nm")
        except Exception as exc:
            print(f"Error setting wavelength: {self._format_driver_error(exc)}")
            raise

    def set_wavelength(self, nm):
        self.configure_wavelength(nm)

    def close(self):
        if self.inst is not None:
            try:
                self.inst.close()
            except Exception:
                pass
            self.inst = None
