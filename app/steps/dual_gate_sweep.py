# app/steps/dual_gate_sweep.py
from __future__ import annotations

import time
from typing import Dict, Any, Tuple, Iterable, Optional

import numpy as np

from .registry import register

class StopRequested(Exception):
    """User requested stop; triggers safe ramp-down in finally."""
    pass

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

def _safe_read_bias_voltage(iv) -> Optional[float]:
    """
    Read back actual bias voltage if the IV adapter supports it.
    Returns float or None.
    """
    if iv is None:
        return None

    # try common method names
    for name in ("read_current_bias", "read_bias_voltage", "read_bias"):
        if hasattr(iv, name):
            try:
                v = float(getattr(iv, name)())
                return v if np.isfinite(v) else None
            except Exception:
                pass
    return None


def _csv_num(v) -> str:
    """
    High-precision numeric text for CSV (avoids 3-decimal rounding downstream).
    If your CSV writer already preserves precision, this is still safe.
    """
    try:
        x = float(v)
        if not np.isfinite(x):
            return ""
        return format(x, ".15g")   # ~float64 precision
    except Exception:
        return ""



@register
class DualGateSweep:
    """
    Dual-gate (+ optional bias) sweep that:
      • ramps to the initial point,
      • jumps (no ramp) between sweep points,
      • ramps gates back to 0 V at the end,
      • reads back actual Vbg/Vtg after each set and writes those to CSV
        (columns still 'Vbg' / 'Vtg'; Vbias is added only if used).
    """

    id = "dual_gate_sweep"

    def __init__(self, cfg: Dict[str, Any]) -> None:
        # Sweep endpoints
        self.vbg_start: float = float(cfg["Vbg_start"])
        self.vbg_stop: float = float(cfg["Vbg_stop"])
        self.vtg_start: float = float(cfg["Vtg_start"])
        self.vtg_stop: float = float(cfg["Vtg_stop"])

        def _opt_float(key: str) -> Optional[float]:
            v = cfg.get(key, None)
            if v is None:
                return None
            s = str(v).strip()
            if s == "" or s.lower() in ("none", "nan"):
                return None
            return float(s)

        # Optional bias sweep endpoints (if either is provided, we sweep bias too)
        self.vbias_start: Optional[float] = _opt_float("Vbias_start")
        self.vbias_stop: Optional[float] = _opt_float("Vbias_stop")

        # Number of points
        self.npts: int = int(cfg.get("frames", 21))

        # Optional file-base (not used here, naming handled by caller)
        self.file_base: str = cfg.get("file_base", "run")

        # Timing / ramping knobs (ramp-in + ramp-out only)
        self.measure_delay: float = float(cfg.get("measure_delay", 0.20))
        self.ramp_step_size: float = float(cfg.get("ramp_step_size", 0.10))
        self.ramp_step_time: float = float(cfg.get("ramp_step_time", 0.02))

        # Optional tiny settle after a jump (during the sweep)
        self.jump_settle_s: float = float(cfg.get("jump_settle_s", 0.0))
        # Optional: per-frame progress callback injected by UI layer (Streamlit)
        # Signature: cb(frame_i: int, frame_total: int) where frame_i is 1-based
        self.frame_progress_cb = cfg.get("frame_progress_cb", None)

        self.stop_cb = cfg.get("stop_cb", None)  # callable -> bool

    # ------------------------------ main ------------------------------

    def run(self, ctx) -> None:
        log = ctx.progress_cb or (lambda *_: None)
        spec = ctx.devices.get("spectrometer", None)
        iv = ctx.devices.get("iv", None)
        csvw = ctx.csv_writer

        if spec is None:
            raise RuntimeError("Spectrometer not connected.")

        # Ensure ctx.axes exists
        if not getattr(ctx, "axes", None):
            ctx.axes = {}

        # Role presence (adapter decides if a role exists)
        has_vbg = bool(getattr(iv, "has_role", lambda *_: False)("Vbg")) if iv else False
        has_vtg = bool(getattr(iv, "has_role", lambda *_: False)("Vtg")) if iv else False
        has_vbias = bool(getattr(iv, "has_role", lambda *_: False)("Vbias")) if iv else False

        if abs(self.vbg_start - self.vbg_stop) > 1e-12 and not has_vbg:
            raise RuntimeError("Sweep includes Vbg but no 'Vbg' role configured.")
        if abs(self.vtg_start - self.vtg_stop) > 1e-12 and not has_vtg:
            raise RuntimeError("Sweep includes Vtg but no 'Vtg' role configured.")

        # Build setpoint lists
        vbg_list = _as_float_list(np.linspace(self.vbg_start, self.vbg_stop, int(self.npts), dtype=float))
        vtg_list = _as_float_list(np.linspace(self.vtg_start, self.vtg_stop, int(self.npts), dtype=float))

        vbias_list: Optional[list[float]] = None
        if (self.vbias_start is not None) or (self.vbias_stop is not None):
            if not iv or (not has_vbias) or (not hasattr(iv, "set_bias")):
                raise RuntimeError("Sweep includes Vbias but no 'Vbias' role or iv.set_bias() is available.")

            vb0 = self.vbias_start if self.vbias_start is not None else self.vbias_stop
            vb1 = self.vbias_stop if self.vbias_stop is not None else self.vbias_start
            vbias_list = _as_float_list(np.linspace(float(vb0), float(vb1), int(self.npts), dtype=float))

        total = len(vbg_list)

        # Wavelength headers setup (lock to first acquired wl)
        wl_headers = None

        # ---- log-only formatters ----
        def _fmt_v(v) -> str:
            try:
                x = float(v)
                return "?" if not np.isfinite(x) else f"{x:g}"
            except Exception:
                return "?"

        def _fmt_i(i_a) -> str:
            """Pretty current units."""
            try:
                x = float(i_a)
                if not np.isfinite(x):
                    return "?"
            except Exception:
                return "?"
            ax = abs(x)
            if ax >= 1e-3:
                return f"{x*1e3:.3g} mA"
            if ax >= 1e-6:
                return f"{x*1e6:.3g} µA"
            if ax >= 1e-9:
                return f"{x*1e9:.3g} nA"
            if ax >= 1e-12:
                return f"{x*1e12:.3g} pA"
            return f"{x:.3g} A"

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
                start_bg, start_tg = vbg_list[0], vtg_list[0]

                # Ramp gates to start (STOP-aware)
                try:
                    log(f"Ramping to start: Vbg={start_bg:g} V, Vtg={start_tg:g} V")
                    iv.set_gates(
                        Vbg=start_bg if has_vbg else None,
                        Vtg=start_tg if has_vtg else None,
                        delay_s=self.ramp_step_time,
                        ramp_step=self.ramp_step_size,
                        stop_cb=self.stop_cb,
                        stop_exc=StopRequested,
                    )
                except StopRequested:
                    raise
                except Exception as e:
                    log(f"Ramp to start (gates) failed: {e}")

                # Snap gates precisely to start (no ramp)
                try:
                    iv.set_gates(
                        Vbg=start_bg if has_vbg else None,
                        Vtg=start_tg if has_vtg else None,
                        delay_s=0.0,
                        ramp_step=0.0,
                    )
                except Exception:
                    pass

                # Ramp/snap bias to start (STOP-aware) if bias sweep requested
                if vbias_list is not None:
                    try:
                        vb_start = float(vbias_list[0])
                        log(f"Ramping bias to start: Vbias={vb_start:g} V")
                        iv.set_bias(
                            Vbias=vb_start,
                            delay_s=self.ramp_step_time,
                            ramp_step=self.ramp_step_size,
                            stop_cb=self.stop_cb,
                            stop_exc=StopRequested,
                        )
                        iv.set_bias(Vbias=vb_start, delay_s=0.0, ramp_step=0.0)
                        ctx.axes["Vbias"] = vb_start
                    except StopRequested:
                        raise
                    except Exception as e:
                        log(f"Ramp bias to start failed: {e}")

                # Read back measured start gates
                bg_meas, tg_meas = _safe_read_current(iv)
                if bg_meas is not None or tg_meas is not None:
                    log(
                        f"At start: Vbg={bg_meas if bg_meas is not None else start_bg:g} V, "
                        f"Vtg={tg_meas if tg_meas is not None else start_tg:g} V"
                    )

                ctx.axes["Vbg"] = float(bg_meas if bg_meas is not None else start_bg)
                ctx.axes["Vtg"] = float(tg_meas if tg_meas is not None else start_tg)

            # --------- Main sweep (jump between points) ----------
            for i in range(total):
                # ---- STOP requested? -> abort -> finally ramps to 0V ----
                if callable(getattr(self, "stop_cb", None)):
                    try:
                        if bool(self.stop_cb()):
                            log("🛑 STOP requested — aborting sweep (will ramp outputs to 0V).")
                            raise StopRequested()
                    except StopRequested:
                        raise
                    except Exception:
                        # never let stop checking crash the run
                        pass

                vbg_set = vbg_list[i]
                vtg_set = vtg_list[i]

                # Jump gates (no ramp)
                if iv:
                    try:
                        iv.set_gates(
                            Vbg=vbg_set if has_vbg else None,
                            Vtg=vtg_set if has_vtg else None,
                            delay_s=0.0,
                            ramp_step=0.0,
                        )
                    except Exception as e:
                        log(f"Set gates failed: {e}")

                # Jump bias (no ramp) if requested
                vbias_set = None
                if iv and vbias_list is not None:
                    try:
                        vbias_set = float(vbias_list[i])
                        iv.set_bias(Vbias=vbias_set, delay_s=0.0, ramp_step=0.0)
                        ctx.axes["Vbias"] = vbias_set
                    except Exception as e:
                        log(f"Set bias failed: {e}")
                        vbias_set = None

                # Optional settle
                if self.jump_settle_s > 0:
                    time.sleep(self.jump_settle_s)

                # Read back actual gates for logging & CSV
                bg_meas, tg_meas = _safe_read_current(iv)

                nan = float("nan")
                vbg_use = float(bg_meas) if (has_vbg and bg_meas is not None) else (float(vbg_set) if has_vbg else nan)
                vtg_use = float(tg_meas) if (has_vtg and tg_meas is not None) else (float(vtg_set) if has_vtg else nan)

                # ---- LOG ONLY: currents + bias ----
                Ibg = Itg = Ibias = None
                if iv and hasattr(iv, "read_currents"):
                    try:
                        Ibg, Itg, Ibias = iv.read_currents()
                    except Exception:
                        pass

                vbias_meas = None
                if vbias_set is not None:
                    vbias_meas = _safe_read_bias_voltage(iv)


                msg = (
                    f"[{i+1}/{total}] "
                    f"Vbg_set={vbg_set:g} V, Vbg={vbg_use:g} V, Ibg={_fmt_i(Ibg)}; "
                    f"Vtg_set={vtg_set:g} V, Vtg={vtg_use:g} V, Itg={_fmt_i(Itg)}"
                )
                if vbias_set is not None:
                    msg += (
                        f"; Vbias_set={float(vbias_set):g} V, "
                        f"Vbias={_fmt_v(vbias_meas)} V, Ibias={_fmt_i(Ibias)}"
                    )
                log(msg)

                # Wait before spectral acquisition
                time.sleep(self.measure_delay)

                # Acquire spectrum
                wl, intens = spec.acquire()
                wl = list(map(float, wl))
                intens = list(map(float, intens))

                # On first row, force CSV wavelength header = actual wl from acquire()
                if getattr(csvw, "_data_rows_written", 0) == 0:
                    wl_headers = wl
                    if hasattr(csvw, "set_wavelength_headers"):
                        csvw.set_wavelength_headers(wl_headers)
                else:
                    wl_headers = wl_headers or getattr(csvw, "wavelength_headers", None)

                # Align lengths if needed
                if wl_headers and len(intens) != len(wl_headers):
                    if len(intens) > len(wl_headers):
                        intens = intens[: len(wl_headers)]
                    else:
                        intens = intens + [0.0] * (len(wl_headers) - len(intens))

                # ---- Measured bias voltage (if bias is active) ----
                vbias_meas_v = _safe_read_bias_voltage(iv) if (vbias_set is not None) else None

                # ---- Currents (A). Prefer read_currents(); fallback to read_leakages if needed ----
                Ibg_a = Itg_a = Ibias_a = None

                if iv and hasattr(iv, "read_currents"):
                    try:
                        Ibg_a, Itg_a, Ibias_a = iv.read_currents()
                    except Exception:
                        Ibg_a = Itg_a = Ibias_a = None


                # ---- Save scalars (add setpoints + measured values) ----
                # NOTE: _csv_num() gives ~float64 precision text (helps if your CSV writer uses str()).
                scalars = {
                    # commanded setpoints
                    "Vbg_set":   float(vbg_set) if has_vbg else None,
                    "Vtg_set":   float(vtg_set) if has_vtg else None,
                    "Vbias_set": float(vbias_set) if vbias_set is not None else None,

                    # measured (readback) values
                    "Vbg":   float(vbg_use) if has_vbg else None,
                    "Vtg":   float(vtg_use) if has_vtg else None,
                    "Vbias": float(vbias_meas_v) if vbias_meas_v is not None else (float(vbias_set) if vbias_set is not None else None),

                    # measured currents (A)
                    "Ibg":   None if Ibg_a is None else float(Ibg_a),
                    "Itg":   None if Itg_a is None else float(Itg_a),
                    "Ibias": None if Ibias_a is None else float(Ibias_a),
                }

                if hasattr(csvw, "write_row"):
                    csvw.write_row(scalars, intens)
                else:
                    csvw.add_row(scalars, intens)

                # NEW: per-frame progress callback (safe)
                cb = getattr(self, "frame_progress_cb", None)
                if callable(cb):
                    try:
                        cb(i + 1, total)
                    except StopRequested:
                        raise
                    except Exception:
                        pass


                # Keep context axes updated
                ctx.axes["Vbg"] = vbg_use
                ctx.axes["Vtg"] = vtg_use
                if vbias_set is not None:
                    ctx.axes["Vbias"] = float(vbias_set)

        except Exception as e:
            log(f"Error: {e}")
            raise

        finally:
            if iv:
                # NOTE: bias ramp-down intentionally disabled (keep your previous behavior).
                # If you ever want it: change `if False` to `if True`.
                # Ramp bias to 0V if bias was requested OR currently present in ctx.axes
                bias_requested = (vbias_list is not None)
                bias_present = False
                try:
                    bias_present = (getattr(ctx, "axes", None) is not None and ctx.axes.get("Vbias") is not None)
                except Exception:
                    bias_present = False

                if has_vbias and hasattr(iv, "set_bias") and (bias_requested or bias_present):
                    log("Ramping bias down to 0V...")
                    try:
                        iv.set_bias(Vbias=0.0, delay_s=self.ramp_step_time, ramp_step=self.ramp_step_size)
                        # snap to exact 0V
                        iv.set_bias(Vbias=0.0, delay_s=0.0, ramp_step=0.0)
                        ctx.axes["Vbias"] = 0.0

                    except Exception as e:
                        log(f"Bias ramp down failed: {e}")


                # Ramp gates down to 0 V (ramped)
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
