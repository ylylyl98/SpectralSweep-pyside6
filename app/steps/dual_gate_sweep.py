from __future__ import annotations
from typing import Dict, Any
import time
import numpy as np
from .registry import register

@register
class DualGateSweep:
    """
    Smart Dual Gate Sweep (Wrapper Compatible).
    - Uses 'iv.has_role()' to detect connections.
    - Uses 'iv.set_gates()' to control hardware safely.
    - Uses 'iv.read_leakages()' for measurements.
    """
    id = "dual_gate_sweep"

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.vbg_start = float(cfg["Vbg_start"])
        self.vbg_stop  = float(cfg["Vbg_stop"])
        self.vtg_start = float(cfg["Vtg_start"])
        self.vtg_stop  = float(cfg["Vtg_stop"])
        self.npts = int(cfg.get("frames", 21))
        self.file_base = cfg.get("file_base", "run")
        
        self.measure_delay = 0.2
        self.ramp_step_size = 0.1
        self.ramp_step_time = 0.1

    def _safe_ramp(self, iv, start_bg, start_tg, stop_bg, stop_tg, log):
        """Safe ramp using the IVDevice wrapper methods."""
        dist_bg = abs(stop_bg - start_bg)
        dist_tg = abs(stop_tg - start_tg)
        max_dist = max(dist_bg, dist_tg)

        if max_dist < 0.001: return

        steps = int(np.ceil(max_dist / self.ramp_step_size))
        if steps < 1: steps = 1
        
        bg_path = np.linspace(start_bg, stop_bg, steps + 1)[1:] 
        tg_path = np.linspace(start_tg, stop_tg, steps + 1)[1:]

        for b, t in zip(bg_path, tg_path):
            # The wrapper 'set_gates' handles checking which roles exist
            if hasattr(iv, "set_gates"):
                iv.set_gates(b, t)
            time.sleep(self.ramp_step_time)

    def run(self, ctx) -> None:
        log  = ctx.progress_cb or (lambda *_: None)
        spec = ctx.devices.get("spectrometer")
        iv   = ctx.devices.get("iv")
        csvw = ctx.csv_writer

        # --- 1. STOP IF NO SPECTROMETER ---
        if spec is None:
            raise RuntimeError("CRITICAL: Spectrometer not connected!")

        # --- 2. IDENTIFY ROLES (Using Wrapper Methods) ---
        # We rely on the wrapper to tell us what is connected
        has_vtg = False
        has_vbg = False
        
        if iv and hasattr(iv, "has_role"):
            has_vtg = iv.has_role("Vtg")
            has_vbg = iv.has_role("Vbg")
        
        # --- 3. VALIDATION: STOP IF SWEEPING DISCONNECTED GATE ---
        # Vbg Check
        if abs(self.vbg_start - self.vbg_stop) > 1e-5:
            if not has_vbg:
                raise RuntimeError(
                    f"Invalid Logic: Attempting to sweep Vbg ({self.vbg_start}->{self.vbg_stop}), "
                    "but 'Vbg' role is not assigned in IV setup."
                )

        # Vtg Check
        if abs(self.vtg_start - self.vtg_stop) > 1e-5:
            if not has_vtg:
                raise RuntimeError(
                    f"Invalid Logic: Attempting to sweep Vtg ({self.vtg_start}->{self.vtg_stop}), "
                    "but 'Vtg' role is not assigned in IV setup."
                )

        # --- 4. Prepare & Run ---
        vbg_list = np.linspace(self.vbg_start, self.vbg_stop, self.npts)
        vtg_list = np.linspace(self.vtg_start, self.vtg_stop, self.npts)

        # Setup Headers
        wl_headers = getattr(csvw, "wavelength_headers", None)
        if not wl_headers and hasattr(spec, "calibration_wavelengths"):
            wl_headers = list(spec.calibration_wavelengths())
            if hasattr(csvw, "set_wavelength_headers"):
                csvw.set_wavelength_headers(wl_headers)

        total = self.npts
        idx = 0
        last_vbg, last_vtg = 0.0, 0.0

        try:
            # --- PHASE A: RAMP TO START ---
            if iv:
                log("Reading hardware state...")
                
                # Get current voltages via wrapper
                curr_bg, curr_tg = 0.0, 0.0
                if hasattr(iv, "read_current_gates"):
                    try:
                        # Expecting a tuple/list (vbg, vtg) or similar
                        vals = iv.read_current_gates()
                        if vals and len(vals) >= 2:
                            curr_bg, curr_tg = vals[0], vals[1]
                    except Exception as e:
                        log(f"Warning reading gates: {e}")

                # Ramp to Start
                if abs(curr_bg - self.vbg_start) > 0.05 or abs(curr_tg - self.vtg_start) > 0.05:
                    log(f"Ramping to start ({self.vbg_start}, {self.vtg_start})...")
                    self._safe_ramp(iv, curr_bg, curr_tg, self.vbg_start, self.vtg_start, log)
                
                # Ensure exact start
                if hasattr(iv, "set_gates"):
                    iv.set_gates(self.vbg_start, self.vtg_start)
                time.sleep(0.5)

            # --- PHASE B: MAIN SWEEP ---
            for vbg, vtg in zip(vbg_list, vtg_list):
                last_vbg, last_vtg = vbg, vtg
                idx += 1
                log(f"[{idx}/{total}] Vbg={vbg:.3f}, Vtg={vtg:.3f}")

                # 1. Set Voltages
                if iv and hasattr(iv, "set_gates"):
                    iv.set_gates(vbg, vtg)
                
                time.sleep(self.measure_delay)

                # 2. Acquire Spectrum
                wl, intens = spec.acquire()
                intens = list(map(float, intens))
                
                # Align Data
                if wl_headers and len(intens) != len(wl_headers):
                     intens = intens[:len(wl_headers)] if len(intens) > len(wl_headers) else intens + [0.0]*(len(wl_headers)-len(intens))

                # 3. Measure Leakages (Smart)
                meas_vtg = np.nan
                meas_vbg = np.nan
                meas_vbias = np.nan

                if iv and hasattr(iv, "read_leakages"):
                    try:
                        # read_leakages likely returns a dict {'Vbg': x, 'Vtg': y, ...}
                        # or a tuple. We handle both just in case.
                        leak = iv.read_leakages()
                        if isinstance(leak, dict):
                            meas_vbg = leak.get("Vbg", np.nan)
                            meas_vtg = leak.get("Vtg", np.nan)
                            meas_vbias = leak.get("Vbias", np.nan)
                        elif isinstance(leak, (list, tuple)) and len(leak) >= 2:
                            meas_vbg, meas_vtg = leak[0], leak[1]
                    except:
                        pass

                # 4. Write Data
                scalars = {
                    "Vbg": float(vbg), "Vtg": float(vtg),
                    "Ibg": meas_vbg, "Itg": meas_vtg, "Ibias": meas_vbias
                }
                
                # Check for Vbias setpoint in context
                if ctx.axes.get("Vbias") is not None: 
                    scalars["Vbias"] = float(ctx.axes["Vbias"])

                if hasattr(csvw, "write_row"): csvw.write_row(scalars, intens)
                else: csvw.add_row(scalars, intens)

        except Exception as e:
            log(f"Error: {e}")
            raise e

        finally:
            if iv:
                log("Ramping down to 0V...")
                self._safe_ramp(iv, last_vbg, last_vtg, 0.0, 0.0, log)
                
                if hasattr(iv, "set_gates"):
                    iv.set_gates(0.0, 0.0)
                
                log("Outputs at 0V.")