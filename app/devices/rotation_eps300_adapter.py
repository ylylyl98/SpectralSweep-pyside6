# app/devices/rotation_eps300_adapter.py
from __future__ import annotations

from typing import Iterable, List, Optional, Union
import time

try:
    import pyvisa
except Exception as e:  # pragma: no cover
    pyvisa = None


class NewportEPS300:
    """
    Minimal Newport ESP/EPS-300 style controller adapter (VISA GPIB/RS-232).
    """

    #: Default VISA timeouts (ms)
    DEFAULT_TIMEOUT_MS = 3000
    #: Default serial baud for EPS/ESP-300
    DEFAULT_BAUD = 19200
    
    def __init__(
        self,
        resource: str,
        *,
        rm: "pyvisa.ResourceManager" = None,
        baud: Optional[int] = None,
        axis: Optional[int] = 1,
        connect_only: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        if pyvisa is None:  # pragma: no cover
            raise RuntimeError("pyvisa not installed")

        self.resource_name = resource
        self.rm = rm or pyvisa.ResourceManager()
        self._inst = None
        self._timeout_ms = int(timeout_ms)
        self._axis = int(axis) if axis is not None else 1

        self._open(baud=baud)
        time.sleep(0.05)

    # ---------------------------
    # Property Fix for main_ui.py
    # ---------------------------
    @property
    def active_axis(self) -> int:
        """Alias for the internal _axis, required by main_ui.py logic."""
        return self._axis

    @active_axis.setter
    def active_axis(self, val: int):
        """Allows main_ui.py to update the axis (e.g. from 1 to 2)."""
        self._axis = int(val)

    # ---------------------------
    # Basic VISA open/close
    # ---------------------------
    def _open(self, *, baud: Optional[int]):
        inst = self.rm.open_resource(self.resource_name)
        inst.timeout = self._timeout_ms

        if self.resource_name.upper().startswith("ASRL"):
            # Serial (RS-232): uses \r
            try:
                inst.baud_rate = int(baud or self.DEFAULT_BAUD)
            except Exception:
                pass
            term = "\r"
            try:
                inst.read_termination = term
                inst.write_termination = term
            except Exception:
                pass
        else:
            # GPIB: uses \r\n
            term = "\r\n"
            try:
                inst.read_termination = term
                inst.write_termination = term
            except Exception:
                pass

        self._inst = inst

    def close(self):
        if self._inst is not None:
            try:
                self._inst.close()
            except Exception:
                pass
            self._inst = None

    # ---------------------------
    # Low-level I/O helpers
    # ---------------------------
    def _write(self, s: str):
        if self._inst is None:
            raise RuntimeError("Controller not open")
        return self._inst.write(s)

    def _read(self) -> str:
        if self._inst is None:
            raise RuntimeError("Controller not open")
        return self._inst.read()

    def _query(self, s: str) -> str:
        self._write(s)
        time.sleep(0.02)
        return self._read()

    # ---------------------------
    # Public helpers
    # ---------------------------
    @property
    def axis(self) -> int:
        return self._axis

    def set_axis(self, axis: int):
        self._axis = int(axis)

    # ---------------------------
    # Axis / Motor power
    # ---------------------------
    def motor_on(self, axis: Optional[int] = None):
        ax = int(self._axis if axis is None else axis)
        self._write(f"{ax}MO")

    def motor_off(self, axis: Optional[int] = None):
        ax = int(self._axis if axis is None else axis)
        self._write(f"{ax}MF")

    # ---------------------------
    # Motion
    # ---------------------------
    def is_motion_done(self, axis: Optional[int] = None) -> bool:
        """Checks 'nMD?' -> returns True if done (1), False if moving (0)."""
        ax = int(self._axis if axis is None else axis)
        try:
            # 'MD?' returns 1 if motion is done, 0 if moving
            resp = self._query(f"{ax}MD?")
            s = str(resp).strip()
            # Handle possible echo like "1MD?1" or just "1"
            if "MD?" in s:
                 s = s.split("?")[-1].strip()
            return (s == "1")
        except Exception:
            return False

    def move_to(self, position_deg: Union[int, float], *, axis: Optional[int] = None, wait: bool = True, timeout_s: float = 60.0):
        """
        Move to position. 
        If wait=True (default), blocks execution until the controller confirms 
        motion is complete (MD=1).
        """
        ax = int(self._axis if axis is None else axis)
        self._write(f"{ax}PA{float(position_deg)}")
        
        if wait:
            start_t = time.time()
            while True:
                # Poll device status
                if self.is_motion_done(axis=ax):
                    break
                
                # Timeout safety
                if (time.time() - start_t) > timeout_s:
                    # You could raise an error here if strict safety is needed
                    print(f"WARNING: Move on axis {ax} timed out after {timeout_s}s")
                    break
                    
                time.sleep(0.1)

    def get_position(self, *, axis: Optional[int] = None) -> float:
        ax = int(self._axis if axis is None else axis)
        resp = self._query(f"{ax}TP")
        try:
            return float(str(resp).strip())
        except Exception as e:
            txt = str(resp).strip()
            for token in txt.replace(",", " ").split():
                try:
                    return float(token)
                except Exception:
                    continue
            raise RuntimeError(f"Unexpected TP response for axis {ax}: {txt}") from e

    # ---------------------------
    # Discovery Helpers
    # ---------------------------
    def _clean_resp(self, s: str) -> str:
        """Trim, strip echoes (like '1TP0.000') and controller prompts."""
        if s is None:
            return ""
        s = str(s).strip()
        # Drop obvious echoes of the command 
        for tag in ("TP", "PR", "ID", "TS", "MD", "QM", "?"):
            # keep numeric payload to the right of the last tag, if any
            if tag in s:
                s = s.split(tag)[-1].strip()
        return s

    def _try_query_float(self, cmd: str) -> tuple[bool, float]:
        """Query and parse a float; tolerate junk around the number."""
        try:
            raw = self._query(cmd)
            s = self._clean_resp(raw).replace(",", " ")
            # pick first token that parses as float
            for tok in s.split():
                try:
                    return True, float(tok)
                except Exception:
                    continue
            return False, 0.0
        except Exception:
            return False, 0.0

    def _try_query_text(self, cmd: str) -> tuple[bool, str]:
        """Query that should return a non-empty, non-echo text."""
        try:
            raw = self._query(cmd)
            s = self._clean_resp(raw)
            if not s:
                return False, ""
            # Filter obvious error/echo markers or single digits
            if s in ("0", "1", "?") or "ERR" in s.upper():
                return False, ""
            return True, s
        except Exception:
            return False, ""

    # ---------------------------
    # Public Discovery
    # ---------------------------
    def get_axes(self, conservative: bool = True, *, max_axes: int = 3) -> list[int]:
        """
        Discover connected axes.
        Checks Motor Type (QM) AND Stage ID (ID).
        """
        found: list[int] = []
        for ax in range(1, max_axes + 1):
            # 1. Check Motor Type (QM?)
            # Returns: 0 = No Motor, 1 = DC, 2 = Stepper.
            # If the controller defaults to "2" for everything, this check passes.
            ok_qm, type_code = self._try_query_float(f"{ax}QM?")
            
            if not ok_qm or int(type_code) == 0:
                continue

            # 2. Check Stage ID (ID?) - The Stricter Check
            # Real stages usually return a name like "URM100".
            # Unplugged axes often return "Unconfigured", "Unknown", or echoes.
            ok_id, id_str = self._try_query_text(f"{ax}ID?")
            
            # If ID failed or explicitly says unconfigured, skip it
            # (Adjust "UNCONFIGURED" if your specific controller uses a different word)
            if ok_id and "UNCONFIGURED" not in id_str.upper() and "UNKNOWN" not in id_str.upper():
                found.append(ax)
            elif not ok_id and int(type_code) > 0:
                 # Fallback: If ID? isn't supported but QM says there is a motor,
                 # we might hesitantly accept it, but for your case, let's be strict.
                 # Change to True if you have 'dumb' motors without ID chips.
                 pass

        # Fallback: If scan found nothing, assume the manually configured axis exists
        if not found:
            return [int(self._axis)]

        return found

    def motor_on_all(self, axes=None):
        axes = axes or self.get_axes(conservative=True)
        for a in axes:
            try:
                self.motor_on(axis=a)
            except Exception:
                pass

    def idn(self) -> str:
        try:
            return self._query("*IDN?")
        except Exception:
            return "UNKNOWN"