from __future__ import annotations

import time
from typing import Dict, Any, Tuple, Iterable

import numpy as np

from .registry import register


def _as_float_list(arr: Iterable[float]) -> list[float]:
    return [float(x) for x in arr]


@register
class DualGateSweep:
    """
    Smart Dual Gate Sweep (wrapper-compatible).
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

        # File naming base
        self.file_base: str = cfg.get("file_base", "run")

        # Timing / ramping knobs
        self.measure_delay: float = float(cfg.get("measure_delay", 0.2))
        # Defaults to fast/custom config, used for both START and END ramps
        self.ramp_step_size: float = float(cfg.get("ramp_step_size", 0.1))
        self.ramp_step_time: float = float(cfg.get("ramp_step_time", 0.03))

    # ------------------------------ helpers ------------------------------

    def _read_current(self, iv) -> Tuple[float, float]:
        bg, tg = 0.0, 0.0
        if iv is None:
            return bg, tg
        if hasattr(iv, "read_current_gates"):
            try:
                vals = iv.read_current_gates()
                if vals and len(vals) >= 2:
                    bg, tg = float(vals[0]), float(vals[1])
            except Exception:
                pass
        return bg, tg

    # ------------------------------ main ------------------------------

    def run(self, ctx) -> None:
        log = ctx.progress_cb or (lambda *_: None)
        spec = ctx.devices.get("spectrometer", None)
        iv = ctx.devices.get("iv", None)
        csvw = ctx.csv_writer

        if spec is None:
            raise RuntimeError("Spectrometer not connected.")

        has_vbg = bool(getattr(iv, "has_role", lambda *_: False)("Vbg")) if iv else False
        has_vtg = bool(getattr(iv, "has_role", lambda *_: False)("Vtg")) if iv else False

        if abs(self.vbg_start - self.vbg_stop) > 1e-9 and not has_vbg:
            raise RuntimeError("Sweep includes Vbg but no 'Vbg' role configured.")
        if abs(self.vtg_start - self.vtg_stop) > 1e-9 and not has_vtg:
            raise RuntimeError("Sweep includes Vtg but no 'Vtg' role configured.")

        # Build sweep lists
        vbg_list = _as_float_list(np.linspace(self.vbg_start, self.vbg_stop, int(self.npts), dtype=float))
        vtg_list = _as_float_list(np.linspace(self.vtg_start, self.vtg_stop, int(self.npts), dtype=float))

        log("Vbg sweep: " + ", ".join(f"{x:g}" for x in vbg_list))
        log("Vtg sweep: " + ", ".join(f"{x:g}" for x in vtg_list))

        total = len(vbg_list)
        last_vbg, last_vtg = 0.0, 0.0

        # Wavelength headers setup
        wl_headers = getattr(csvw, "wavelength_headers", None)
        if not wl_headers and hasattr(spec, "calibration_wavelengths"):
            try:
                wl_headers = list(spec.calibration_wavelengths())
                if hasattr(csvw, "set_wavelength_headers"):
                    csvw.set_wavelength_headers(wl_headers)
            except Exception:
                wl_headers = None

        try:
            # --------- Ramp to start ----------
            if iv:
                log("Reading hardware state...")
                curr_bg, curr_tg = self._read_current(iv)

                start_bg, start_tg = vbg_list[0], vtg_list[0]
                
                # We simply call set_gates with the configured ramp speed.
                # The driver handles the stepping internally.
                if abs(curr_bg - start_bg) > 0.05 or abs(curr_tg - start_tg) > 0.05:
                    log(f"Ramping to start ({start_bg:g}, {start_tg:g})...")
                    try:
                        iv.set_gates(
                            Vbg=start_bg if has_vbg else None,
                            Vtg=start_tg if has_vtg else None,
                            delay_s=self.ramp_step_time,  # SAFE: Uses configured speed
                            ramp_step=self.ramp_step_size # SAFE: Uses configured step
                        )
                    except Exception as e:
                        log(f"Ramp to start failed: {e}")
                
                # Final snap to ensure precision
                time.sleep(0.2)
                try:
                    iv.set_gates(
                        Vbg=start_bg if has_vbg else None,
                        Vtg=start_tg if has_vtg else None,
                        delay_s=0.0,
                        ramp_step=0.0,
                    )
                except Exception:
                    pass

            # --------- Main sweep ----------
            for i in range(total):
                vbg = vbg_list[i]
                vtg = vtg_list[i]
                last_vbg, last_vtg = vbg, vtg

                log(f"[{i+1}/{total}] Vbg={vbg:g}, Vtg={vtg:g}")

                if iv:
                    try:
                        # Sweep steps are usually small enough to be "instant"
                        iv.set_gates(
                            Vbg=vbg if has_vbg else None,
                            Vtg=vtg if has_vtg else None,
                            delay_s=0.0,
                            ramp_step=0.0,
                        )
                    except Exception as e:
                        log(f"Set gates failed: {e}")

                time.sleep(self.measure_delay)

                # Acquire Data
                wl, intens = spec.acquire()
                intens = list(map(float, intens))

                # Align Data
                if wl_headers and len(intens) != len(wl_headers):
                    if len(intens) > len(wl_headers):
                        intens = intens[: len(wl_headers)]
                    else:
                        intens = intens + [0.0] * (len(wl_headers) - len(intens))

                # Read Leakages
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

                # Save Data
                scalars = {
                    "Vbg": vbg, "Vtg": vtg,
                    "Ibg": meas_vbg, "Itg": meas_vtg, "Ibias": meas_vbias,
                }
                if getattr(ctx, "axes", None) and ctx.axes.get("Vbias") is not None:
                    scalars["Vbias"] = float(ctx.axes["Vbias"])

                if hasattr(csvw, "write_row"):
                    csvw.write_row(scalars, intens)
                else:
                    csvw.add_row(scalars, intens)

        except Exception as e:
            log(f"Error: {e}")
            raise

        finally:
            # --------- Zero Gates (FAST but SAFE) ----------
            if iv:
                log("Ramping down to 0V...")
                try:
                    # UPDATED: We use set_gates with your CONFIG settings.
                    # This enables the driver's internal ramp (Safe) but uses your fast timing (Fast).
                    iv.set_gates(
                        Vbg=0.0 if has_vbg else None,
                        Vtg=0.0 if has_vtg else None,
                        delay_s=self.ramp_step_time,  # e.g., 0.03 or 0.0
                        ramp_step=self.ramp_step_size # e.g., 0.1
                    )
                except Exception as e:
                    log(f"Ramp down failed: {e}")
                log("Outputs at 0V.")