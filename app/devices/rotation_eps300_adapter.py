# ---------------------------
# Axis auto-detection
# ---------------------------
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

    Features used by the UI:
      - open via VISA resource, optional baud for ASRL (serial)
      - axis selection (default 1)
      - automatic motor_on() after connect (UI does this)
      - move_to(angle_deg)
      - get_position() -> float degrees
      - motor_on() / motor_off()
      - get_axes()  -> list[int] (auto-detect)
      - close()

    Notes
    -----
    * Command style is "AXISCMD", e.g. "1MO" (axis 1 motor on), "2TP" (axis 2 tell position).
    * Most ESP/ESP300 controllers end lines with <CR> and expect <CR>.
    * Position units depend on stage config (often degrees for rotation).
    * If your controller requires a different termination/baud, adjust below.
    """

    #: Default VISA timeouts (ms)
    DEFAULT_TIMEOUT_MS = 3000
    #: Default serial baud for EPS/ESP-300 (adjust if needed)
    DEFAULT_BAUD = 19200
    #: Default write/read terminations (CR)
    DEFAULT_TERM = "\r"

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
        """
        Parameters
        ----------
        resource : str
            VISA resource string, e.g. "ASRL6::INSTR" (serial) or "GPIB0::5::INSTR".
        rm : pyvisa.ResourceManager, optional
            If None, a new ResourceManager() is created.
        baud : int, optional
            Serial baud for ASRL resources. If None, DEFAULT_BAUD is used.
        axis : int, optional
            Current working axis (1..8 typical). Can be changed later via set_axis().
        connect_only : bool
            If True, opens the resource (for probing) and leaves it open; caller can close().
        timeout_ms : int
            VISA timeout in milliseconds.
        """
        if pyvisa is None:  # pragma: no cover
            raise RuntimeError("pyvisa not installed")

        self.resource_name = resource
        self.rm = rm or pyvisa.ResourceManager()
        self._inst = None
        self._timeout_ms = int(timeout_ms)
        self._axis = int(axis) if axis is not None else 1

        self._open(baud=baud)

        # Some controllers like a brief pause after opening serial
        time.sleep(0.05)

        # Optionally verify communication (non-fatal)
        # self._maybe_verify()

        # Probe mode is handled by the caller (UI may open/scan/close)
        # Nothing special required here for connect_only.

    # ---------------------------
    # Basic VISA open/close
    # ---------------------------
    def _open(self, *, baud: Optional[int]):
        """Open VISA resource and set serial/GPIB attributes."""
        inst = self.rm.open_resource(self.resource_name)

        # Common settings
        inst.timeout = self._timeout_ms

        # Serial tuning
        if self.resource_name.upper().startswith("ASRL"):
            # Serial: set baud/terminations
            try:
                inst.baud_rate = int(baud or self.DEFAULT_BAUD)
            except Exception:
                # Not all VISA layers expose baud_rate; ignore if unavailable
                pass

            # Many Newport controllers use CR as EOL
            try:
                inst.read_termination = self.DEFAULT_TERM
                inst.write_termination = self.DEFAULT_TERM
            except Exception:
                pass

            # Optional: tweak parity/bytesize if needed for your device
            # inst.parity = pyvisa.constants.Parity.none
            # inst.data_bits = 8
            # inst.stop_bits = pyvisa.constants.StopBits.one

        else:
            # GPIB: keep default EOL, but setting read/write terminations is harmless
            try:
                inst.read_termination = self.DEFAULT_TERM
                inst.write_termination = self.DEFAULT_TERM
            except Exception:
                pass

        self._inst = inst

    def close(self):
        """Close VISA resource."""
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
        # Avoid accidental newlines; VISA handles terminations
        return self._inst.write(s)

    def _read(self) -> str:
        if self._inst is None:
            raise RuntimeError("Controller not open")
        return self._inst.read()

    def _query(self, s: str) -> str:
        """Do a safe query (write + read). Some controllers dislike 'query'; we emulate it."""
        self._write(s)
        # Small delay can help some serial stacks
        time.sleep(0.01)
        return self._read()

    # ---------------------------
    # Public helpers
    # ---------------------------
    @property
    def axis(self) -> int:
        return self._axis

    def set_axis(self, axis: int):
        """Change the current working axis (1..8 typical)."""
        self._axis = int(axis)

    # ---------------------------
    # Axis / Motor power
    # ---------------------------
    def motor_on(self, axis: Optional[int] = None):
        """
        Turn motor on for given axis (or current axis).
        Command: '<axis>MO'
        """
        ax = int(self._axis if axis is None else axis)
        self._write(f"{ax}MO")

    def motor_off(self, axis: Optional[int] = None):
        """
        Turn motor off for given axis (or current axis).
        Command: '<axis>MF'
        """
        ax = int(self._axis if axis is None else axis)
        self._write(f"{ax}MF")

    # ---------------------------
    # Motion
    # ---------------------------
    def move_to(self, position_deg: Union[int, float], *, axis: Optional[int] = None, wait: bool = False, poll_s: float = 0.1):
        """
        Absolute move to 'position_deg' for the given axis (or current axis).
        Command: '<axis>PA<position>'
        """
        ax = int(self._axis if axis is None else axis)
        self._write(f"{ax}PA{float(position_deg)}")

        if wait:
            # Many controllers support motion status, but a simple settle is often ok.
            # If your controller exposes motion-done query, add it here.
            time.sleep(poll_s)
            # naive wait loop can be added if needed

    def get_position(self, *, axis: Optional[int] = None) -> float:
        """
        Read position in degrees for the given axis (or current axis).
        Command: '<axis>TP'
        Returns: float
        """
        ax = int(self._axis if axis is None else axis)
        resp = self._query(f"{ax}TP")
        # Response is often like "12.345" (string). Strip & parse float.
        try:
            return float(str(resp).strip())
        except Exception as e:
            # Some firmwares return like "1TP+012.345" or include axis echoes;
            # try to parse the last token that looks like a float.
            txt = str(resp).strip()
            for token in txt.replace(",", " ").split():
                try:
                    return float(token)
                except Exception:
                    continue
            raise RuntimeError(f"Unexpected TP response for axis {ax}: {txt}") from e


    def _safe_query_float(self, cmd: str) -> Optional[float]:
        """Send a query and try to parse a float from the reply."""
        try:
            resp = self._query(cmd)
            return float(str(resp).strip())
        except Exception:
            # attempt to extract a float token if the response has extra text
            try:
                txt = str(resp).strip()
                for tok in txt.replace(",", " ").split():
                    try:
                        return float(tok)
                    except Exception:
                        pass
            except Exception:
                pass
        return None


    def get_axes(self, conservative: bool = True):
        """
        Return a list of available axes on ESP300.
        Conservative mode:
        - Only probe 1..3 (ESP300 max)
        - Validate with axis-specific query that must return a numeric position.
        """
        # Typical ESP300 supports up to 3 axes
        probe_range = range(1, 4)

        found = []
        for ax in probe_range:
            ok = False
            # Try a couple of axis-specific reads; accept if we get a numeric value.
            # Order: TP? (common Newport pos query). Fallback to PR? (position read) if TP? not supported on your firmware.
            for cmd in (f"{ax}TP?", f"{ax}PR?"):
                try:
                    resp = self.query(cmd, timeout=self.timeout)
                    if resp is None:
                        continue
                    s = str(resp).strip()
                    # Some firmwares return like "  +12.345" or "12.345"
                    # Accept presence only if this parses as a float
                    float(s)
                    ok = True
                    break
                except Exception:
                    # any parse/timeout/visa error → try next command
                    pass
            if ok:
                found.append(ax)

        # Fallback: if *nothing* validated but controller is alive, assume axis 1 only
        if not found:
            try:
                # controller-level check; if we at least respond, default to [1]
                _ = self.query("VE?", timeout=self.timeout)  # version
                found = [1]
            except Exception:
                found = []

        return found


    def motor_on_all(self, axes=None):
        """
        Turn motors ON for all provided axes; if axes is None, scans conservatively.
        """
        axes = axes or self.get_axes(conservative=True)
        for a in axes:
            try:
                # prefer motor_on(axis=...), fall back to axisless
                if hasattr(self, "motor_on"):
                    if "axis" in self.motor_on.__code__.co_varnames:
                        self.motor_on(axis=a)
                    else:
                        self.motor_on()
            except Exception:
                pass

    # ---------------------------
    # Optional / diagnostics
    # ---------------------------
    def idn(self) -> str:
        """Attempt *IDN? or 'ID?' on current axis, for diagnostics."""
        try:
            return self._query("*IDN?")
        except Exception:
            try:
                return self._query(f"{self._axis}ID?")
            except Exception:
                return "UNKNOWN"

    def clear_errors(self):
        """Attempt to clear controller errors if supported."""
        try:
            self._write("CL")  # many firmwares use 'CL' for clear
        except Exception:
            pass

    def __del__(self):  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass

