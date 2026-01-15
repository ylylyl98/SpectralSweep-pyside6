# app/steps/dual_gate_sweep.py
from __future__ import annotations

import time
from typing import Dict, Any, Tuple, Iterable, Optional

import numpy as np

from .registry import register


def _as_float_list(arr: Iterable[float]) -> list[float]:
    return [float(x) for x in arr]


def _safe_read_current(iv) -> Tuple[Optional[float], Optional[float]]:
    """
    Read back actual gate voltages from the IV adapter if supported.
    Returns (Vbg_meas, Vtg_meas) or (None, None) if unavailable.
    """
    if iv is None:
        return None, None
    try:
        if hasattr(iv, "read_current_gates"):
            bg, tg = iv.read_current_gates()
            # normalize to float or None
            try:
                bg = float(bg)
            except Exception:
                bg = None
            try:
                tg = float(tg)
            except Exception:
                tg = None
            return bg, tg
    except Exception:
        pass
    return None, None


@register
class DualGateSweep:
    """
    Dual-gate sweep that:
      • ramps to the initial point,
      • jumps (no ramp) between sweep points,
      • ramps back to 0 V at the end,
      • reads back actual Vbg/Vtg after each set and writes those to CSV (columns still 'Vbg' / 'Vtg').
    """

    id = "dual_gate_sweep"

    def __init__(self, cfg: Dict[str, Any]) -> None:
        # Sweep endpoints
        self.vbg_start: float = float(cfg["Vbg_start"])
        self.vbg_stop: float = float(cfg["Vbg_stop"])
        self.vtg_start: float = float(cfg["Vtg_start"])
        self.vtg_stop: float = float(cfg["Vtg_stop"])

        # Number of points
        self.npts: int = int(cfg.get("frames", 21))

        # Optional file-base (not used here, naming handled by caller)
        self.file_base: str = cfg.get("file_base", "run")

        # Timing / ramping knobs
        # These apply to the **ramp-in** and **ramp-out** only.
        self.measure_delay: float = float(cfg.get("measure_delay", 0.20))
        self.ramp_step_size: float = float(cfg.get("ramp_step_size", 0.10))
        self.ramp_step_time: float = float(cfg.get("ramp_step_time", 0.03))

        # Optional tiny settle after a jump (during the sweep)
        self.jump_settle_s: float = float(cfg.get("jump_settle_s", 0.0))

    # ------------------------------ main ------------------------------

    def run(self, ctx) -> None:
        log = ctx.progress_cb or (lambda *_: None)
        spec = ctx.devices.get("spectrometer", None)
        iv = ctx.devices.get("iv", None)
        csvw = ctx.csv_writer

        if spec is None:
            raise RuntimeError("Spectrometer not connected.")

        # Role presence (adapter decides if a role exists)
        has_vbg = bool(getattr(iv, "has_role", lambda *_: False)("Vbg")) if iv else False
        has_vtg = bool(getattr(iv, "has_role", lambda *_: False)("Vtg")) if iv else False

        if abs(self.vbg_start - self.vbg_stop) > 1e-12 and not has_vbg:
            raise RuntimeError("Sweep includes Vbg but no 'Vbg' role configured.")
        if abs(self.vtg_start - self.vtg_stop) > 1e-12 and not has_vtg:
            raise RuntimeError("Sweep includes Vtg but no 'Vtg' role configured.")

        # Build setpoint lists
        vbg_list = _as_float_list(np.linspace(self.vbg_start, self.vbg_stop, int(self.npts), dtype=float))
        vtg_list = _as_float_list(np.linspace(self.vtg_start, self.vtg_stop, int(self.npts), dtype=float))

        # log("Vbg sweep (setpoints): " + ", ".join(f"{x:g}" for x in vbg_list))
        # log("Vtg sweep (setpoints): " + ", ".join(f"{x:g}" for x in vtg_list))

        total = len(vbg_list)

        # Wavelength headers setup (if CSVWriter doesn't have them yet)
        wl_headers = None  # ✅ don't trust pre-passed headers; we'll lock to first acquired wl

        if (not wl_headers) and hasattr(spec, "calibration_wavelengths"):
            try:
                wl_headers = list(spec.calibration_wavelengths())
                if hasattr(csvw, "set_wavelength_headers"):
                    csvw.set_wavelength_headers(wl_headers)
            except Exception:
                pass

        try:
            # --------- Ramp to start (ramped) ----------
            if iv:
                # log("Reading hardware state...")
                start_bg, start_tg = vbg_list[0], vtg_list[0]
                # Use your adapter's ramp facility for a smooth move-in
                try:
                    
                    log(f"Ramping to start: Vbg={start_bg:g} V, Vtg={start_tg:g} V ")
                    iv.set_gates(
                        Vbg=start_bg if has_vbg else None,
                        Vtg=start_tg if has_vtg else None,
                        delay_s=self.ramp_step_time,
                        ramp_step=self.ramp_step_size,
                    )
                except Exception as e:
                    log(f"Ramp to start failed: {e}")

                # Snap precisely to start (no ramp) to remove any rounding residue
                try:
                    iv.set_gates(
                        Vbg=start_bg if has_vbg else None,
                        Vtg=start_tg if has_vtg else None,
                        delay_s=0.0,
                        ramp_step=0.0,
                    )
                except Exception:
                    pass

                # Read back measured start
                bg_meas, tg_meas = _safe_read_current(iv)
                if bg_meas is not None or tg_meas is not None:
                    log(f"At start: Vbg={bg_meas if bg_meas is not None else start_bg:g} V, "
                        f"Vtg={tg_meas if tg_meas is not None else start_tg:g} V")
                # store in context axes too
                ctx.axes["Vbg"] = float(bg_meas if bg_meas is not None else start_bg)
                ctx.axes["Vtg"] = float(tg_meas if tg_meas is not None else start_tg)

            # --------- Main sweep (jump between points) ----------
            for i in range(total):
                vbg_set = vbg_list[i]
                vtg_set = vtg_list[i]

                # Jump (no ramp) to each setpoint during the sweep
                if iv:
                    try:
                        iv.set_gates(
                            Vbg=vbg_set if has_vbg else None,
                            Vtg=vtg_set if has_vtg else None,
                            delay_s=0.0,
                            ramp_step=0.0,  # <- explicit "jump"
                        )
                    except Exception as e:
                        log(f"Set gates failed: {e}")

                # Optional ultra-short settle after jump
                if self.jump_settle_s > 0:
                    time.sleep(self.jump_settle_s)

                # Read back actual values for logging & CSV
                bg_meas, tg_meas = _safe_read_current(iv)

                nan = float("nan")
                vbg_use = float(bg_meas) if (has_vbg and bg_meas is not None) else (float(vbg_set) if has_vbg else nan)
                vtg_use = float(tg_meas) if (has_vtg and tg_meas is not None) else (float(vtg_set) if has_vtg else nan)

                log(
                    f"[{i+1}/{total}] "
                    f"Vbg_set={vbg_set:g} V, Vbg={vbg_use:g} V; "
                    f"Vtg_set={vtg_set:g} V, Vtg={vtg_use:g} V"
                )



                # Wait before spectral acquisition (integration settled etc.)
                time.sleep(self.measure_delay)

                # Acquire spectrum
                wl, intens = spec.acquire()
                wl = list(map(float, wl))                 # ✅ make wl a float list
                intens = list(map(float, intens))
                # ✅ ADD THIS: on the first row, force CSV wavelength header = actual wl from acquire()
                if getattr(csvw, "_data_rows_written", 0) == 0:
                    wl_headers = wl
                    if hasattr(csvw, "set_wavelength_headers"):
                        csvw.set_wavelength_headers(wl_headers)
                else:
                    # keep using existing headers
                    wl_headers = wl_headers or getattr(csvw, "wavelength_headers", None)
                # Align to headers length if needed
                if wl_headers and len(intens) != len(wl_headers):
                    if len(intens) > len(wl_headers):
                        intens = intens[: len(wl_headers)]
                    else:
                        intens = intens + [0.0] * (len(wl_headers) - len(intens))

                # Optionally read “leakages” (currents) if your adapter provides it
                meas_vbg, meas_vtg, meas_vbias = np.nan, np.nan, np.nan
                if iv and hasattr(iv, "read_leakages"):
                    try:
                        leak = iv.read_leakages()
                        if isinstance(leak, dict):
                            meas_vbg = leak.get("Vbg", np.nan)
                            meas_vtg = leak.get("Vtg", np.nan)
                            meas_vbias = leak.get("Vbias", np.nan)
                        elif isinstance(leak, (list, tuple)) and len(leak) >= 2:
                            meas_vbg, meas_vtg = float(leak[0]), float(leak[1])
                    except Exception:
                        pass

                # Save scalars (columns remain Vbg/Vtg, but values are the **measured** ones)
                scalars = {
                    "Vbg": vbg_use,
                    "Vtg": vtg_use,
                    "Ibg": meas_vbg,
                    "Itg": meas_vtg,
                    "Ibias": meas_vbias,
                }
                if getattr(ctx, "axes", None) and ctx.axes.get("Vbias") is not None:
                    scalars["Vbias"] = float(ctx.axes["Vbias"])

                if hasattr(csvw, "write_row"):
                    csvw.write_row(scalars, intens)
                else:
                    csvw.add_row(scalars, intens)

                # Keep context axes up to date with what we actually used
                ctx.axes["Vbg"] = vbg_use
                ctx.axes["Vtg"] = vtg_use

        except Exception as e:
            log(f"Error: {e}")
            raise

        finally:
            # --------- Ramp down to 0 V (ramped) ----------
            if iv:
                log("Ramping down to 0V...")
                try:
                    iv.set_gates(
                        Vbg=0.0 if has_vbg else None,
                        Vtg=0.0 if has_vtg else None,
                        delay_s=self.ramp_step_time,
                        ramp_step=self.ramp_step_size,
                    )
                except Exception as e:
                    log(f"Ramp down failed: {e}")
                log("Outputs at 0V.")
