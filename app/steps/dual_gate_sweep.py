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

    Expects an IV wrapper providing:
      - has_role(name) -> bool
      - set_gates(Vbg=?, Vtg=?, delay_s=?, ramp_step=?)
      - read_leakages() -> dict or (bg, tg)
      - read_current_gates() -> (curr_bg, curr_tg)
    """

    id = "dual_gate_sweep"

    # ---------- hardcoded ramp-to-zero profile (per request) ----------
    _R2Z_STEP_V = 0.1     # 0.1 V per step
    _R2Z_DELAY_S = 0.05   # 50 ms per step

    def __init__(self, cfg: Dict[str, Any]) -> None:
        # Sweep endpoints
        self.vbg_start: float = float(cfg["Vbg_start"])
        self.vbg_stop: float = float(cfg["Vbg_stop"])
        self.vtg_start: float = float(cfg["Vtg_start"])
        self.vtg_stop: float = float(cfg["Vtg_stop"])

        # Number of points
        self.npts: int = int(cfg.get("frames", 21))

        # File naming base (the engine uses this; we don't touch it here)
        self.file_base: str = cfg.get("file_base", "run")

        # Timing / ramping knobs for pre-sweep ramp *only* (not exposed in UI)
        self.measure_delay: float = float(cfg.get("measure_delay", 0.2))
        self.ramp_step_size: float = float(cfg.get("ramp_step_size", 0.1))
        self.ramp_step_time: float = float(cfg.get("ramp_step_time", 0.03))

    # ------------------------------ helpers ------------------------------

    def _ramp_linear(
        self,
        iv,
        start_bg: float,
        start_tg: float,
        stop_bg: float,
        stop_tg: float,
        step_v: float,
        delay_s: float,
        log,
    ) -> None:
        """
        Linear ramp both gates from (start_bg, start_tg) to (stop_bg, stop_tg)
        using fixed voltage step (step_v) and per-step delay (delay_s).

        We generate our own path (no rounding), then call iv.set_gates for each
        waypoint with ramp_step=0 (jump), since *our* loop is the ramp.
        """
        if iv is None:
            return

        dist_bg = abs(stop_bg - start_bg)
        dist_tg = abs(stop_tg - start_tg)
        max_dist = max(dist_bg, dist_tg)
        if max_dist <= 1e-12:
            return

        step_v = max(float(step_v), 1e-9)
        steps = int(np.ceil(max_dist / step_v))
        steps = max(1, steps)

        path_bg = _as_float_list(np.linspace(start_bg, stop_bg, steps + 1)[1:])
        path_tg = _as_float_list(np.linspace(start_tg, stop_tg, steps + 1)[1:])

        for b, t in zip(path_bg, path_tg):
            try:
                iv.set_gates(Vbg=b, Vtg=t, delay_s=0.0, ramp_step=0.0)  # instantaneous set to each waypoint
            except Exception as e:
                log(f"Ramp step failed: {e}")
            time.sleep(delay_s)

    def _read_current(self, iv) -> Tuple[float, float]:
        """Best-effort read of current gate outputs."""
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
        """
        ctx.devices: dict with keys like "spectrometer", "iv"
        ctx.csv_writer: writer with write_row(scalars, intens) or add_row(...).
        ctx.progress_cb: callable(str) for UI log.
        ctx.axes: optional dict with static axis values (e.g., Vbias).
        """
        log = ctx.progress_cb or (lambda *_: None)
        spec = ctx.devices.get("spectrometer", None)
        iv = ctx.devices.get("iv", None)
        csvw = ctx.csv_writer

        if spec is None:
            raise RuntimeError("Spectrometer not connected.")

        # Role awareness
        has_vbg = bool(getattr(iv, "has_role", lambda *_: False)("Vbg")) if iv else False
        has_vtg = bool(getattr(iv, "has_role", lambda *_: False)("Vtg")) if iv else False

        # Validate we're not trying to sweep a disconnected role
        if abs(self.vbg_start - self.vbg_stop) > 1e-9 and not has_vbg:
            raise RuntimeError(
                f"Attempting to sweep Vbg ({self.vbg_start}→{self.vbg_stop}) "
                "but no 'Vbg' role is configured."
            )
        if abs(self.vtg_start - self.vtg_stop) > 1e-9 and not has_vtg:
            raise RuntimeError(
                f"Attempting to sweep Vtg ({self.vtg_start}→{self.vtg_stop}) "
                "but no 'Vtg' role is configured."
            )

        # Build sweep lists as raw floats (no rounding sent to hardware)
        vbg_list = _as_float_list(np.linspace(self.vbg_start, self.vbg_stop, int(self.npts), dtype=float))
        vtg_list = _as_float_list(np.linspace(self.vtg_start, self.vtg_stop, int(self.npts), dtype=float))

        # Cosmetic: print the planned sweeps
        log("Vbg sweep: " + ", ".join(f"{x:g}" for x in vbg_list))
        log("Vtg sweep: " + ", ".join(f"{x:g}" for x in vtg_list))

        total = len(vbg_list)
        last_vbg, last_vtg = 0.0, 0.0

        # Ensure wavelength headers exist once (if your writer supports it)
        wl_headers = getattr(csvw, "wavelength_headers", None)
        if not wl_headers and hasattr(spec, "calibration_wavelengths"):
            try:
                wl_headers = list(spec.calibration_wavelengths())
                if hasattr(csvw, "set_wavelength_headers"):
                    csvw.set_wavelength_headers(wl_headers)
            except Exception:
                wl_headers = None

        try:
            # --------- Ramp to start (uses pre-sweep profile) ----------
            if iv:
                log("Reading hardware state...")
                curr_bg, curr_tg = self._read_current(iv)

                start_bg, start_tg = vbg_list[0], vtg_list[0]
                if abs(curr_bg - start_bg) > 0.05 or abs(curr_tg - start_tg) > 0.05:
                    log(f"Ramping to start ({start_bg:g}, {start_tg:g})...")
                    self._ramp_linear(
                        iv,
                        curr_bg,
                        curr_tg,
                        start_bg,
                        start_tg,
                        step_v=self.ramp_step_size,
                        delay_s=self.ramp_step_time,
                        log=log,
                    )

                # Snap exactly to start
                try:
                    iv.set_gates(
                        Vbg=start_bg if has_vbg else None,
                        Vtg=start_tg if has_vtg else None,
                        delay_s=0.0,
                        ramp_step=0.0,
                    )
                except Exception as e:
                    log(f"Set start failed: {e}")
                time.sleep(0.2)

            # --------- Main sweep ----------
            for i in range(total):
                vbg = vbg_list[i]
                vtg = vtg_list[i]
                last_vbg, last_vtg = vbg, vtg

                log(f"[{i+1}/{total}] Vbg={vbg:g}, Vtg={vtg:g}")

                if iv:
                    try:
                        # instantaneous setpoint per step (no internal ramping)
                        iv.set_gates(
                            Vbg=vbg if has_vbg else None,
                            Vtg=vtg if has_vtg else None,
                            delay_s=0.0,
                            ramp_step=0.0,
                        )
                    except Exception as e:
                        log(f"Set gates failed: {e}")

                time.sleep(self.measure_delay)

                # Acquire spectrum
                wl, intens = spec.acquire()
                intens = list(map(float, intens))

                # Align intensity length to headers if needed
                if wl_headers and len(intens) != len(wl_headers):
                    if len(intens) > len(wl_headers):
                        intens = intens[: len(wl_headers)]
                    else:
                        intens = intens + [0.0] * (len(wl_headers) - len(intens))

                # Optional leakage / measured values
                meas_vbg = np.nan
                meas_vtg = np.nan
                meas_vbias = np.nan
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

                scalars = {
                    "Vbg": vbg,
                    "Vtg": vtg,
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

        except Exception as e:
            log(f"Error: {e}")
            raise

        finally:
            # --------- Forced ramp-down to 0 V with fixed profile ----------
            if iv:
                log("Ramping down to 0V...")
                try:
                    self._ramp_linear(
                        iv,
                        last_vbg,
                        last_vtg,
                        0.0,
                        0.0,
                        step_v=self._R2Z_STEP_V,   # << fixed 0.1 V
                        delay_s=self._R2Z_DELAY_S, # << fixed 0.05 s
                        log=log,
                    )
                    iv.set_gates(
                        Vbg=0.0 if has_vbg else None,
                        Vtg=0.0 if has_vtg else None,
                        delay_s=0.0,
                        ramp_step=0.0,
                    )
                except Exception as e:
                    log(f"Ramp down failed: {e}")
                log("Outputs at 0V.")
