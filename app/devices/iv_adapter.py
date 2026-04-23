from __future__ import annotations

from typing import Tuple, Optional, Dict, Callable, Type
import math
import time
import iv_automation


class IVDevice:
    """
    Thin adapter over iv_automation.IVSetup with role awareness.
    Roles may be missing: Vbg, Vtg, Vbias. We only operate on roles that exist.
    """

    def __init__(
        self,
        setup: iv_automation.IVSetup,
        role_map: Optional[Dict[str, Optional[str]]] = None,  # e.g. {"Vbg": "...", "Vtg": "...", "Vbias": "..."}
    ):
        self.setup = setup
        self.role_map = role_map or {"Vbg": None, "Vtg": None, "Vbias": None}

    # ---------------- internal helpers ----------------

    def has_role(self, name: str) -> bool:
        """
        Return True if this role is mapped OR can be auto-detected from the setup.
        This avoids "no Vbg role configured" when the channel exists but role_map wasn't passed.
        """
        if bool(self.role_map.get(name)):
            return True

        # Auto-detect if the setup can read this x channel name.
        try:
            fn = getattr(self.setup, "get_single_x_value", None)
            if callable(fn):
                _ = fn(name)  # raises if missing
                return True
        except Exception:
            pass

        return False

    def _write_x_only(self, values: Dict[str, float]) -> bool:
        """
        Fast path: update X setpoints and call each owning instrument.write_x()
        WITHOUT creating sweeps or reading Y channels.
        """
        setup = getattr(self, "setup", None)
        xcc = getattr(setup, "x_channel_collection", None) if setup else None
        if xcc is None:
            return False
        if not (hasattr(xcc, "send_x") and hasattr(xcc, "get_instrument")):
            return False

        insts = []
        seen = set()

        for name, val in (values or {}).items():
            if val is None:
                continue
            try:
                xcc.send_x(str(name), float(val))  # updates cache + instrument.receive_x()
                inst = xcc.get_instrument(str(name))
                if inst is not None:
                    k = id(inst)
                    if k not in seen:
                        seen.add(k)
                        insts.append(inst)
            except Exception:
                pass

        if not insts:
            return False

        for inst in insts:
            try:
                inst.write_x()
            except Exception:
                pass

        return True

    def _set_x_fast(self, name: str, value: float) -> bool:
        """
        Fast set (no ramp, no readback). Falls back to setup.x_goto if needed.
        """
        if not self.has_role(name):
            return False

        ok = self._write_x_only({name: float(value)})
        if ok:
            return True

        try:
            self.setup.x_goto(name, float(value), delta=0, delay=0.0, print_steps=False)
            return True
        except Exception:
            return False

    def _safe_x_goto(self, name: str, value: float, delay_s: float = 0.05) -> bool:
        """
        Jump to value (no ramp). Uses fast write_x_only when possible.
        """
        ok = self._set_x_fast(name, float(value))
        if not ok:
            return False
        if delay_s and float(delay_s) > 0:
            time.sleep(float(delay_s))
        return True

    def _check_stop(
        self,
        stop_cb: Optional[Callable[[], bool]],
        stop_exc: Optional[Type[BaseException]],
    ) -> None:
        """
        If stop_cb requests stop, raise stop_exc() (or RuntimeError) to unwind safely.
        This is designed to be called frequently inside ramp loops.
        """
        if not callable(stop_cb):
            return
        try:
            requested = bool(stop_cb())
        except Exception:
            requested = False

        if requested:
            if stop_exc is not None:
                raise stop_exc()
            raise RuntimeError("StopRequested")

    def _safe_read_x(self, name: str) -> float:
        """Best-effort read of current x (setpoint)."""
        nan = float("nan")
        try:
            fn = getattr(self.setup, "get_single_x_value", None)
            if callable(fn):
                return float(fn(name))
        except Exception:
            pass
        return nan

    def _update_x_cache(self, name: str, value: Optional[float]) -> None:
        """Keep cached setpoints aligned with a confirmed live hardware read."""
        if value is None:
            return
        try:
            value_f = float(value)
        except Exception:
            return
        if not math.isfinite(value_f):
            return
        try:
            xcc = getattr(self.setup, "x_channel_collection", None)
            if xcc is not None and hasattr(xcc, "send_x"):
                xcc.send_x(str(name), value_f)
        except Exception:
            pass

    def _safe_read_live_x(self, name: str) -> float:
        """Best-effort live hardware read for one axis, falling back to nan."""
        nan = float("nan")
        try:
            if name == "Vbg":
                value, _ = self.read_current_gates()
                return float(value)
            if name == "Vtg":
                _, value = self.read_current_gates()
                return float(value)
            if name == "Vbias":
                value = self.read_current_bias()
                return float(value) if value is not None else nan
        except Exception:
            pass
        return nan

    def _ramp_axis_stopaware(
        self,
        name: str,
        target: float,
        step: float,
        delay_s: float = 0.05,
        *,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ) -> bool:
        """
        Manual stepped ramp so we can poll stop_cb each step.
        Fast per-step write (no sweep construction / no per-step READ?).
        """
        if not self.has_role(name):
            return False

        target = float(target)
        step = float(abs(step)) if step and step > 0 else 0.0

        # jump mode
        if step <= 0:
            self._check_stop(stop_cb, stop_exc)
            self._set_x_fast(name, target)
            if delay_s and float(delay_s) > 0:
                time.sleep(float(delay_s))
            return True

        x0 = self._safe_read_live_x(name)
        if not math.isfinite(x0):
            x0 = self._safe_read_x(name)
        if not math.isfinite(x0):
            x0 = 0.0

        dx = target - float(x0)
        n = max(1, int(math.ceil(abs(dx) / max(step, 1e-12))))

        for i in range(1, n + 1):
            self._check_stop(stop_cb, stop_exc)
            f = i / n
            xi = float(x0) + dx * f
            self._set_x_fast(name, float(xi))
            if delay_s and float(delay_s) > 0:
                time.sleep(float(delay_s))

        # exact final snap (should be equal already, but keep it explicit)
        self._check_stop(stop_cb, stop_exc)
        self._set_x_fast(name, target)
        if delay_s and float(delay_s) > 0:
            time.sleep(float(delay_s))
        return True

    def ramp_to(
        self,
        name: str,
        value: float,
        step: float,
        delay_s: float = 0.05,
        *,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ) -> bool:
        """
        Safely ramp an axis to 'value' using manual stepped ramp (stop-aware).
        Returns False if that role does not exist.
        """
        if not self.has_role(name):
            return False
        step = abs(step) if step and step > 0 else 0.0
        return self._ramp_axis_stopaware(
            name,
            float(value),
            float(step),
            float(delay_s),
            stop_cb=stop_cb,
            stop_exc=stop_exc,
        )

    def _ramp_gates_together(
        self,
        Vbg: float,
        Vtg: float,
        ramp_step: float,
        delay_s: float,
        *,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ) -> None:
        """
        Interleaved ramp: at each step, set BOTH Vbg and Vtg, then sleep once.
        Fast per-step write (no sweep construction / no per-step READ?).
        """
        # start points: prefer measured, fall back to x setpoint
        bg0, tg0 = self.read_current_gates()

        if not math.isfinite(bg0):
            bg0 = self._safe_read_x("Vbg")
        if not math.isfinite(tg0):
            tg0 = self._safe_read_x("Vtg")

        # If we still can't read both starts reliably, fall back to per-axis ramps
        if not (math.isfinite(bg0) and math.isfinite(tg0)):
            self.ramp_to("Vbg", Vbg, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            self.ramp_to("Vtg", Vtg, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            return

        bg1 = float(Vbg)
        tg1 = float(Vtg)

        d_bg = bg1 - bg0
        d_tg = tg1 - tg0

        step = float(abs(ramp_step)) if ramp_step and ramp_step > 0 else 0.0
        if step <= 0:
            self._check_stop(stop_cb, stop_exc)
            self._write_x_only({"Vbg": bg1, "Vtg": tg1})
            if delay_s and float(delay_s) > 0:
                time.sleep(float(delay_s))
            return

        max_d = max(abs(d_bg), abs(d_tg))
        n = max(1, int(math.ceil(max_d / step)))

        for i in range(1, n + 1):
            self._check_stop(stop_cb, stop_exc)
            f = i / n
            bg_i = bg0 + d_bg * f
            tg_i = tg0 + d_tg * f
            self._write_x_only({"Vbg": float(bg_i), "Vtg": float(tg_i)})
            if delay_s and float(delay_s) > 0:
                time.sleep(float(delay_s))

        self._check_stop(stop_cb, stop_exc)
        self._write_x_only({"Vbg": bg1, "Vtg": tg1})
        if delay_s and float(delay_s) > 0:
            time.sleep(float(delay_s))

    # ---------------- public API used by UI/steps ----------------

    def set_gates(
        self,
        Vbg: Optional[float] = None,
        Vtg: Optional[float] = None,
        delay_s: float = 0.05,
        ramp_step: Optional[float] = 0.1,
        *,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ):
        """
        Set gate voltages. If ramp_step>0, move in steps; otherwise jump.

        If BOTH Vbg and Vtg are provided AND both roles exist AND ramp_step>0,
        ramp together (interleaved). Stop-aware via stop_cb.
        """
        if (
            Vbg is not None
            and Vtg is not None
            and self.has_role("Vbg")
            and self.has_role("Vtg")
            and ramp_step is not None
            and ramp_step > 0
        ):
            self._ramp_gates_together(
                float(Vbg),
                float(Vtg),
                float(ramp_step),
                float(delay_s),
                stop_cb=stop_cb,
                stop_exc=stop_exc,
            )
            return

        # Single gate / missing role / no ramp
        if Vbg is not None:
            if ramp_step and ramp_step > 0:
                self.ramp_to("Vbg", Vbg, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            else:
                self._safe_x_goto("Vbg", Vbg, delay_s)

        if Vtg is not None:
            if ramp_step and ramp_step > 0:
                self.ramp_to("Vtg", Vtg, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            else:
                self._safe_x_goto("Vtg", Vtg, delay_s)

    def set_bias(
        self,
        Vbias: Optional[float] = None,
        delay_s: float = 0.05,
        ramp_step: Optional[float] = 0.1,
        *,
        stop_cb: Optional[Callable[[], bool]] = None,
        stop_exc: Optional[Type[BaseException]] = None,
    ):
        """
        Set source-drain bias. If ramp_step>0, move in steps; otherwise jump.
        Stop-aware via stop_cb.
        """
        if Vbias is not None:
            if ramp_step and ramp_step > 0:
                self.ramp_to("Vbias", Vbias, ramp_step, delay_s, stop_cb=stop_cb, stop_exc=stop_exc)
            else:
                self._safe_x_goto("Vbias", Vbias, delay_s)

    def read_leakages(self) -> Tuple[float, float]:
        """Return (Vbg_leakage, Vtg_leakage); if missing, return 0.0."""
        def _get(name: str) -> float:
            try:
                return float(self.setup.get_single_y_value(name))
            except Exception:
                return 0.0
        return _get("Vbg_leakage"), _get("Vtg_leakage")

    def _safe_read_y(self, key: str) -> Optional[float]:
        """
        Best-effort: force a hardware read for the instrument that owns this y-channel,
        then return the latest y value for that key.
        """
        try:
            ycc = getattr(self.setup, "y_channel_collection", None)
            if ycc is None:
                return None

            try:
                inst = ycc.get_instrument(key)
            except Exception:
                return None

            if inst is not None and hasattr(inst, "read_y"):
                try:
                    inst.read_y()
                except Exception:
                    pass

            try:
                ycc.receive_y(key)
            except Exception:
                pass

            try:
                return float(self.setup.get_single_y_value(key))
            except Exception:
                return None
        except Exception:
            return None

    def read_currents(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Return (Ibg, Itg, Ibias) in Amps.
        Keys come directly from iv_automation.py: <x_name>_leakage.
        """
        Ibg = self._safe_read_y("Vbg_leakage") if self.has_role("Vbg") else None
        Itg = self._safe_read_y("Vtg_leakage") if self.has_role("Vtg") else None
        Ib  = self._safe_read_y("Vbias_leakage") if self.has_role("Vbias") else None
        return Ibg, Itg, Ib

    def read_current_bias(self) -> Optional[float]:
        if not self.has_role("Vbias"):
            return None
        try:
            meas_key = "measured_Vbias"
            inst = self.setup.y_channel_collection.get_instrument(meas_key)
            if inst:
                inst.read_y()
            self.setup.y_channel_collection.receive_y(meas_key)
            value = float(self.setup.get_single_y_value(meas_key))
            self._update_x_cache("Vbias", value)
            return value
        except Exception:
            return None

    def read_current_gates(self):
        """
        Forces a hardware read of 'measured_Vbg' and 'measured_Vtg'.
        Returns nan for any role that is not mapped / not readable.
        """
        nan = float("nan")
        bg_val = nan
        tg_val = nan

        roles_to_check = []
        if self.has_role("Vbg"):
            roles_to_check.append(("Vbg", "measured_Vbg"))
        if self.has_role("Vtg"):
            roles_to_check.append(("Vtg", "measured_Vtg"))

        for role, meas_key in roles_to_check:
            try:
                inst = None
                try:
                    inst = self.setup.y_channel_collection.get_instrument(meas_key)
                except KeyError:
                    continue

                if inst:
                    inst.read_y()

                self.setup.y_channel_collection.receive_y(meas_key)
                val = self.setup.get_single_y_value(meas_key)

                if role == "Vbg":
                    bg_val = float(val)
                    self._update_x_cache("Vbg", bg_val)
                if role == "Vtg":
                    tg_val = float(val)
                    self._update_x_cache("Vtg", tg_val)

            except Exception:
                pass

        return bg_val, tg_val

    def ramp_all_to_zero(self, ramp_step: float = 0.1, delay_s: float = 0.05):
        """Ramp Vbias, Vbg, Vtg -> 0V if roles exist."""
        try:
            if self.has_role("Vbias"):
                self.set_bias(Vbias=0.0, delay_s=delay_s, ramp_step=ramp_step)
                self.set_bias(Vbias=0.0, delay_s=0.0, ramp_step=0.0)
        except Exception:
            pass

        try:
            self.set_gates(
                Vbg=0.0 if self.has_role("Vbg") else None,
                Vtg=0.0 if self.has_role("Vtg") else None,
                delay_s=delay_s,
                ramp_step=ramp_step,
            )
            self.set_gates(
                Vbg=0.0 if self.has_role("Vbg") else None,
                Vtg=0.0 if self.has_role("Vtg") else None,
                delay_s=0.0,
                ramp_step=0.0,
            )
        except Exception:
            pass
