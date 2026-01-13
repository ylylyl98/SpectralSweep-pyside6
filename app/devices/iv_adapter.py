from __future__ import annotations
from typing import Tuple, Optional, Dict
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
        """Return True if this role is mapped to any instrument."""
        return bool(self.role_map.get(name))

    def _safe_x_goto(self, name: str, value: float, delay_s: float = 0.02) -> bool:
        """
        Jump to value (no ramp). Kept for backward-compat.
        """
        if not self.has_role(name):
            return False
        self.setup.x_goto(name, float(value), delta=0, delay=delay_s, print_steps=False)
        return True

    def ramp_to(self, name: str, value: float, step: float, delay_s: float = 0.02) -> bool:
        """
        Safely ramp an axis to 'value' using IVSetup.x_goto with a step size.
        Returns False if that role does not exist.
        """
        if not self.has_role(name):
            return False
        step = abs(step) if step and step > 0 else 0.0  # delta=0 -> jump
        self.setup.x_goto(name, float(value), delta=step, delay=delay_s, print_steps=False)
        return True

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

    def _ramp_gates_together(self, Vbg: float, Vtg: float, ramp_step: float, delay_s: float) -> None:
        """
        Interleaved ramp: at each step, set BOTH Vbg and Vtg (delta=0 jumps),
        then sleep once. This avoids: 'finish Vbg ramp then start Vtg'.
        """
        # start points: prefer measured, fall back to x setpoint
        bg0, tg0 = self.read_current_gates()

        if not math.isfinite(bg0):
            bg0 = self._safe_read_x("Vbg")
        if not math.isfinite(tg0):
            tg0 = self._safe_read_x("Vtg")

        # If we still can't read both starts reliably, fall back to old sequential ramps
        if not (math.isfinite(bg0) and math.isfinite(tg0)):
            # sequential fallback (better than using wrong start values)
            self.ramp_to("Vbg", Vbg, ramp_step, delay_s)
            self.ramp_to("Vtg", Vtg, ramp_step, delay_s)
            return

        bg1 = float(Vbg)
        tg1 = float(Vtg)

        d_bg = bg1 - bg0
        d_tg = tg1 - tg0

        step = float(abs(ramp_step)) if ramp_step and ramp_step > 0 else 0.0
        if step <= 0:
            # jump both
            self.setup.x_goto("Vbg", bg1, delta=0, delay=0.0, print_steps=False)
            self.setup.x_goto("Vtg", tg1, delta=0, delay=0.0, print_steps=False)
            time.sleep(delay_s)
            return

        max_d = max(abs(d_bg), abs(d_tg))
        n = max(1, int(math.ceil(max_d / step)))

        for i in range(1, n + 1):
            f = i / n
            bg_i = bg0 + d_bg * f
            tg_i = tg0 + d_tg * f

            # Do NOT call ramp_to() here (would nest ramps). Use delta=0 jumps.
            self.setup.x_goto("Vbg", float(bg_i), delta=0, delay=0.0, print_steps=False)
            self.setup.x_goto("Vtg", float(tg_i), delta=0, delay=0.0, print_steps=False)

            time.sleep(delay_s)

        # Ensure exact final values
        self.setup.x_goto("Vbg", bg1, delta=0, delay=0.0, print_steps=False)
        self.setup.x_goto("Vtg", tg1, delta=0, delay=0.0, print_steps=False)
        time.sleep(delay_s)

    # ---------------- public API used by UI/steps ----------------

    def set_gates(
        self,
        Vbg: Optional[float] = None,
        Vtg: Optional[float] = None,
        delay_s: float = 0.05,
        ramp_step: Optional[float] = 0.1,
    ):
        """
        Set gate voltages. If ramp_step>0, move in steps; otherwise jump.

        NEW: If BOTH Vbg and Vtg are provided AND both roles exist, ramp together (interleaved).
        """
        # Only do "together ramp" when user requests both and both roles exist
        if (
            Vbg is not None
            and Vtg is not None
            and self.has_role("Vbg")
            and self.has_role("Vtg")
            and ramp_step is not None
            and ramp_step > 0
        ):
            self._ramp_gates_together(float(Vbg), float(Vtg), float(ramp_step), float(delay_s))
            return

        # Original behavior for single gate / missing role / no ramp
        if Vbg is not None:
            if ramp_step and ramp_step > 0:
                self.ramp_to("Vbg", Vbg, ramp_step, delay_s)
            else:
                self._safe_x_goto("Vbg", Vbg, delay_s)

        if Vtg is not None:
            if ramp_step and ramp_step > 0:
                self.ramp_to("Vtg", Vtg, ramp_step, delay_s)
            else:
                self._safe_x_goto("Vtg", Vtg, delay_s)

    def set_bias(
        self,
        Vbias: Optional[float] = None,
        delay_s: float = 0.05,
        ramp_step: Optional[float] = None,
    ):
        """
        Set source-drain bias. If ramp_step>0, move in steps; otherwise jump.
        """
        if Vbias is not None:
            if ramp_step and ramp_step > 0:
                self.ramp_to("Vbias", Vbias, ramp_step, delay_s)
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

    def read_current_bias(self) -> Optional[float]:
        if not self.has_role("Vbias"):
            return None
        try:
            meas_key = "measured_Vbias"
            inst = self.setup.y_channel_collection.get_instrument(meas_key)
            if inst:
                inst.read_y()
            self.setup.y_channel_collection.receive_y(meas_key)
            return float(self.setup.get_single_y_value(meas_key))
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
        if self.has_role("Vbg"): roles_to_check.append(("Vbg", "measured_Vbg"))
        if self.has_role("Vtg"): roles_to_check.append(("Vtg", "measured_Vtg"))

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

                if role == "Vbg": bg_val = float(val)
                if role == "Vtg": tg_val = float(val)

            except Exception:
                pass

        return bg_val, tg_val
