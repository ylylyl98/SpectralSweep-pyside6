from __future__ import annotations
from typing import Tuple, Optional, Dict
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

    def _safe_x_goto(self, name: str, value: float, delay_s: float) -> bool:
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

    # ---------------- public API used by UI/steps ----------------

    def set_gates(
        self,
        Vbg: Optional[float] = None,
        Vtg: Optional[float] = None,
        delay_s: float = 0.05,
        ramp_step: Optional[float] = None,
    ):
        """
        Set gate voltages. If ramp_step>0, move in steps; otherwise jump.
        """
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

    def read_current_gates(self) -> Tuple[float, float]:
        """
        Forces a hardware read of 'measured_Vbg' and 'measured_Vtg'.
        Fixes the issue where update_ys() didn't trigger a real measurement.
        """
        bg_val, tg_val = 0.0, 0.0

        roles_to_check = []
        if self.has_role("Vbg"): roles_to_check.append(("Vbg", "measured_Vbg"))
        if self.has_role("Vtg"): roles_to_check.append(("Vtg", "measured_Vtg"))

        for role, meas_key in roles_to_check:
            try:
                inst = None
                try:
                    inst = self.setup.y_channel_collection.get_instrument(meas_key)
                except KeyError:
                    print(f"Warning: Key '{meas_key}' not found. Check initialization name.")
                    continue

                if inst:
                    inst.read_y()

                self.setup.y_channel_collection.receive_y(meas_key)
                val = self.setup.get_single_y_value(meas_key)

                if role == "Vbg": bg_val = val
                if role == "Vtg": tg_val = val

            except Exception as e:
                print(f"Warning: Failed reading {role}: {e}")

        return float(bg_val), float(tg_val)
