# app/steps/dual_gate_sweep.py
from __future__ import annotations

import time
import inspect
from typing import Dict, Any, Tuple, Iterable, Optional

import numpy as np

from .registry import register


class StopRequested(Exception):
    """User requested stop; triggers safe ramp-down."""
    pass


# =============================================================================
# STOP helpers
# =============================================================================

def _stop_requested(stop_cb) -> bool:
    try:
        return bool(stop_cb()) if callable(stop_cb) else False
    except Exception:
        return False


def _raise_if_stop(
    stop_cb,
    *,
    iv=None,
    ramp_step: float = 0.1,
    ramp_dt: float = 0.02,
    log=None,
    msg: str = "🛑 STOP requested",
):
    """
    If STOP requested:
      - log
      - IMMEDIATELY ramp outputs to 0V (best-effort)
      - raise StopRequested to unwind
    """
    if not _stop_requested(stop_cb):
        return

    try:
        if callable(log):
            log(f"{msg} — ramping outputs to 0V now...")
    except Exception:
        pass

    try:
        if iv is not None:
            _ramp_all_to_zero(iv, ramp_step=float(ramp_step), ramp_dt=float(ramp_dt), log=log)
    except Exception as e:
        try:
            if callable(log):
                log(f"Ramp-to-zero on STOP failed (ignored): {e}")
        except Exception:
            pass

    raise StopRequested()


def _sleep_with_stop(
    total_s: float,
    stop_cb,
    *,
    iv=None,
    ramp_step: float = 0.1,
    ramp_dt: float = 0.02,
    log=None,
    check_dt: float = 0.05,
):
    """Sleep in small chunks so STOP is responsive during waits."""
    if total_s <= 0:
        return
    t0 = time.time()
    while True:
        _raise_if_stop(
            stop_cb,
            iv=iv,
            ramp_step=ramp_step,
            ramp_dt=ramp_dt,
            log=log,
            msg="🛑 STOP requested during wait",
        )
        dt = time.time() - t0
        if dt >= total_s:
            return
        time.sleep(min(check_dt, total_s - dt))


# =============================================================================
# Compatibility helpers (support slightly different IV adapters)
# =============================================================================

def _filter_kwargs(fn, kwargs: dict) -> dict:
    try:
        sig = inspect.signature(fn)
        return {k: v for k, v in kwargs.items() if k in sig.parameters}
    except Exception:
        return kwargs


def _call_compat(fn, *args, **kwargs):
    if fn is None:
        return None
    return fn(*args, **_filter_kwargs(fn, kwargs))


def _force_x_goto(iv, name: str, value: float) -> bool:
    """
    Last-resort output set: iv.setup.x_goto(name, value, delta=0).
    This bypasses any role_map / has_role checks if those are wrong.
    """
    try:
        setup = getattr(iv, "setup", None)
        if setup is None:
            return False
        x_goto = getattr(setup, "x_goto", None)
        if not callable(x_goto):
            return False
        x_goto(str(name), float(value), delta=0, delay=0.0, print_steps=False)
        return True
    except Exception:
        return False


def _force_get_x(iv, name: str) -> Optional[float]:
    """Best-effort read of x setpoint from iv.setup if available."""
    try:
        setup = getattr(iv, "setup", None)
        if setup is None:
            return None
        fn = getattr(setup, "get_single_x_value", None)
        if callable(fn):
            v = float(fn(str(name)))
            return v if np.isfinite(v) else None
    except Exception:
        pass
    return None


def _iv_has_role(iv, role: str) -> bool:
    """
    Prefer iv.has_role(role). If that says False but the underlying setup
    has an x-channel with that name, treat it as available.
    """
    if iv is None:
        return False

    try:
        fn = getattr(iv, "has_role", None)
        if callable(fn) and bool(fn(role)):
            return True
    except Exception:
        pass

    # Fallback: underlying setup has x channel?
    return _force_get_x(iv, role) is not None


def _iv_set_gates_jump(iv, *, Vbg=None, Vtg=None, delay_s: float = 0.0):
    """
    Jump to gates with maximum API compatibility:
      1) iv.set_gates(Vbg=..., Vtg=..., ramp_step=0)
      2) positional fallback (only if both provided)
      3) force via iv.setup.x_goto
    """
    if iv is None:
        return

    fn = getattr(iv, "set_gates", None)
    if callable(fn):
        kw = {"delay_s": float(delay_s), "ramp_step": 0.0}
        if Vbg is not None:
            kw["Vbg"] = float(Vbg)
        if Vtg is not None:
            kw["Vtg"] = float(Vtg)

        if ("Vbg" in kw) or ("Vtg" in kw):
            try:
                _call_compat(fn, **kw)
                return
            except TypeError:
                pass
            except Exception:
                pass

        if (Vbg is not None) and (Vtg is not None):
            try:
                _call_compat(fn, float(Vbg), float(Vtg), delay_s=float(delay_s), ramp_step=0.0)
                return
            except Exception:
                pass

    # last resort
    if Vbg is not None:
        _force_x_goto(iv, "Vbg", float(Vbg))
    if Vtg is not None:
        _force_x_goto(iv, "Vtg", float(Vtg))


def _iv_set_bias_jump(iv, Vbias: float, *, delay_s: float = 0.0):
    """
    Jump bias with maximum API compatibility:
      1) iv.set_bias(Vbias=..., ramp_step=0)
      2) iv.set_vbias(...)
      3) force via iv.setup.x_goto('Vbias', ...)
    """
    if iv is None:
        return

    fn = getattr(iv, "set_bias", None) or getattr(iv, "set_vbias", None)
    if callable(fn):
        try:
            _call_compat(fn, Vbias=float(Vbias), delay_s=float(delay_s), ramp_step=0.0)
            return
        except TypeError:
            try:
                _call_compat(fn, float(Vbias), delay_s=float(delay_s), ramp_step=0.0)
                return
            except Exception:
                pass
        except Exception:
            pass

    _force_x_goto(iv, "Vbias", float(Vbias))


# =============================================================================
# Best-effort readbacks (measured if available, else setpoint fallback)
# =============================================================================

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
        fn = getattr(iv, "read_current_gates", None)
        if callable(fn):
            bg, tg = fn()
            try:
                bg = float(bg)
                if not np.isfinite(bg):
                    bg = None
            except Exception:
                bg = None
            try:
                tg = float(tg)
                if not np.isfinite(tg):
                    tg = None
            except Exception:
                tg = None
            return bg, tg
    except Exception:
        pass

    return _force_get_x(iv, "Vbg"), _force_get_x(iv, "Vtg")


def _safe_read_bias_voltage(iv) -> Optional[float]:
    """
    Read back actual bias voltage if the IV adapter supports it.
    Returns float or None.
    """
    if iv is None:
        return None

    for name in ("read_current_bias", "read_bias_voltage", "read_bias"):
        fn = getattr(iv, name, None)
        if callable(fn):
            try:
                v = float(fn())
                return v if np.isfinite(v) else None
            except Exception:
                pass

    return _force_get_x(iv, "Vbias")


def _log_measured_state(iv, log, *, tag: str):
    """Log measured gates/bias + currents (if available)."""
    if iv is None or not callable(log):
        return

    bg, tg = _safe_read_current(iv)
    vb = _safe_read_bias_voltage(iv)

    Ibg = Itg = Ib = None
    try:
        fn = getattr(iv, "read_currents", None)
        if callable(fn):
            Ibg, Itg, Ib = fn()
    except Exception:
        pass

    def _fmt(v):
        if v is None:
            return "?"
        try:
            x = float(v)
            return "?" if not np.isfinite(x) else f"{x:g}"
        except Exception:
            return "?"

    def _fmt_i(i_a):
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

    log(
        f"[{tag}] "
        f"Vbg={_fmt(bg)} V, Vtg={_fmt(tg)} V, Vbias={_fmt(vb)} V; "
        f"Ibg={_fmt_i(Ibg)}, Itg={_fmt_i(Itg)}, Ibias={_fmt_i(Ib)}"
    )


# =============================================================================
# STOP-aware ramps (adapter-independent; uses jump setters)
# =============================================================================

def _ramp_linear_2ch(
    *,
    iv,
    Vbg0=None, Vtg0=None,
    Vbg1=None, Vtg1=None,
    step: float = 0.1,
    dt: float = 0.02,
    stop_cb=None,
    log=None,
):
    """
    STOP-aware interleaved ramp for (Vbg, Vtg) using jump updates.
    Works even if iv.set_gates doesn't support ramp_step/stop_cb.
    """
    if iv is None:
        return

    if (Vbg1 is None) and (Vtg1 is None):
        return

    # Start values
    if Vbg0 is None or Vtg0 is None:
        bgm, tgm = _safe_read_current(iv)
        if Vbg0 is None:
            Vbg0 = bgm if (bgm is not None and np.isfinite(bgm)) else 0.0
        if Vtg0 is None:
            Vtg0 = tgm if (tgm is not None and np.isfinite(tgm)) else 0.0

    dbg = 0.0 if Vbg1 is None else float(Vbg1) - float(Vbg0)
    dtg = 0.0 if Vtg1 is None else float(Vtg1) - float(Vtg0)

    maxd = max(abs(dbg), abs(dtg))
    if maxd < 1e-15:
        _iv_set_gates_jump(iv, Vbg=Vbg1, Vtg=Vtg1, delay_s=0.0)
        return

    step = max(float(step), 1e-6)
    n = max(1, int(np.ceil(maxd / step)))

    for k in range(1, n + 1):
        _raise_if_stop(stop_cb, iv=iv, ramp_step=step, ramp_dt=dt, log=log, msg="🛑 STOP requested during gate ramp")

        frac = k / n
        bg = None if Vbg1 is None else (float(Vbg0) + dbg * frac)
        tg = None if Vtg1 is None else (float(Vtg0) + dtg * frac)

        _iv_set_gates_jump(iv, Vbg=bg, Vtg=tg, delay_s=0.0)

        if dt > 0:
            _sleep_with_stop(dt, stop_cb, iv=iv, ramp_step=step, ramp_dt=dt, log=log, check_dt=0.05)


def _ramp_bias_1ch(
    *,
    iv,
    V0: Optional[float],
    V1: float,
    step: float = 0.1,
    dt: float = 0.02,
    stop_cb=None,
    log=None,
):
    if iv is None:
        return

    if V0 is None:
        vm = _safe_read_bias_voltage(iv)
        V0 = vm if (vm is not None and np.isfinite(vm)) else 0.0

    d = float(V1) - float(V0)
    if abs(d) < 1e-15:
        _iv_set_bias_jump(iv, float(V1), delay_s=0.0)
        return

    step = max(float(step), 1e-6)
    n = max(1, int(np.ceil(abs(d) / step)))

    for k in range(1, n + 1):
        _raise_if_stop(stop_cb, iv=iv, ramp_step=step, ramp_dt=dt, log=log, msg="🛑 STOP requested during bias ramp")

        v = float(V0) + d * (k / n)
        _iv_set_bias_jump(iv, v, delay_s=0.0)

        if dt > 0:
            _sleep_with_stop(dt, stop_cb, iv=iv, ramp_step=step, ramp_dt=dt, log=log, check_dt=0.05)


def _ramp_all_to_zero(iv, *, ramp_step: float, ramp_dt: float, log=None):
    """
    Best-effort ramp-down to 0V:
      - logs measured Vbg/Vtg/Vbias (+ currents) before/after
      - does not depend on role_map being correct
    """
    if iv is None:
        return

    _log_measured_state(iv, log, tag="before ramp-to-0V")

    # Bias -> 0
    try:
        _ramp_bias_1ch(iv=iv, V0=None, V1=0.0, step=ramp_step, dt=ramp_dt, stop_cb=None, log=log)
        _iv_set_bias_jump(iv, 0.0, delay_s=0.0)
    except Exception as e:
        if callable(log):
            log(f"Bias ramp-down failed (ignored): {e}")

    _log_measured_state(iv, log, tag="after bias->0V")

    # Gates -> 0
    try:
        _ramp_linear_2ch(
            iv=iv,
            Vbg0=None, Vtg0=None,
            Vbg1=0.0,
            Vtg1=0.0,
            step=ramp_step,
            dt=ramp_dt,
            stop_cb=None,
            log=log,
        )
        _iv_set_gates_jump(iv, Vbg=0.0, Vtg=0.0, delay_s=0.0)
    except Exception as e:
        if callable(log):
            log(f"Gate ramp-down failed (ignored): {e}")

    _log_measured_state(iv, log, tag="after gates->0V")


# =============================================================================
# Step implementation
# =============================================================================

@register
class DualGateSweep:
    """
    Dual-gate (+ optional bias) sweep that:
      • ramps to the initial point (STOP-aware),
      • jumps between sweep points,
      • STOP immediately ramps all outputs to 0V (even mid-wait / mid-ramp),
      • end always ramps to 0V and logs measured state.
    """

    id = "dual_gate_sweep"

    def __init__(self, cfg: Dict[str, Any]) -> None:
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

        self.vbias_start: Optional[float] = _opt_float("Vbias_start")
        self.vbias_stop: Optional[float] = _opt_float("Vbias_stop")

        self.npts: int = int(cfg.get("frames", 21))
        self.file_base: str = cfg.get("file_base", "run")

        self.measure_delay: float = float(cfg.get("measure_delay", 0.20))
        self.ramp_step_size: float = float(cfg.get("ramp_step_size", 0.10))
        self.ramp_step_time: float = float(cfg.get("ramp_step_time", 0.02))

        self.jump_settle_s: float = float(cfg.get("jump_settle_s", 0.0))

        self.frame_progress_cb = cfg.get("frame_progress_cb", None)
        self.stop_cb = cfg.get("stop_cb", None)

    def run(self, ctx) -> None:
        log = ctx.progress_cb or (lambda *_: None)
        spec = ctx.devices.get("spectrometer", None)
        iv = ctx.devices.get("iv", None)
        csvw = ctx.csv_writer

        if spec is None:
            raise RuntimeError("Spectrometer not connected.")

        if not getattr(ctx, "axes", None):
            ctx.axes = {}

        has_vbg = _iv_has_role(iv, "Vbg")
        has_vtg = _iv_has_role(iv, "Vtg")
        has_vbias = _iv_has_role(iv, "Vbias")

        log(f"IV roles: Vbg={has_vbg}, Vtg={has_vtg}, Vbias={has_vbias}")

        # Wavelength headers (optional)
        wl_headers = None
        if hasattr(spec, "calibration_wavelengths"):
            try:
                wl_headers = list(spec.calibration_wavelengths())
                if hasattr(csvw, "set_wavelength_headers"):
                    csvw.set_wavelength_headers(wl_headers)
            except Exception:
                pass

        try:
            # config sanity
            if abs(self.vbg_start - self.vbg_stop) > 1e-12 and not has_vbg:
                raise RuntimeError("Sweep includes Vbg but no 'Vbg' role configured.")
            if abs(self.vtg_start - self.vtg_stop) > 1e-12 and not has_vtg:
                raise RuntimeError("Sweep includes Vtg but no 'Vtg' role configured.")
            if ((self.vbias_start is not None) or (self.vbias_stop is not None)) and not has_vbias:
                raise RuntimeError("Sweep includes Vbias but no 'Vbias' role configured.")

            # setpoint lists
            vbg_list = _as_float_list(np.linspace(self.vbg_start, self.vbg_stop, self.npts, dtype=float)) if has_vbg else [float(self.vbg_start)] * self.npts
            vtg_list = _as_float_list(np.linspace(self.vtg_start, self.vtg_stop, self.npts, dtype=float)) if has_vtg else [float(self.vtg_start)] * self.npts

            vbias_list: Optional[list[float]] = None
            if (self.vbias_start is not None) or (self.vbias_stop is not None):
                vb0 = self.vbias_start if self.vbias_start is not None else self.vbias_stop
                vb1 = self.vbias_stop if self.vbias_stop is not None else self.vbias_start
                vbias_list = _as_float_list(np.linspace(float(vb0), float(vb1), self.npts, dtype=float))

            total = len(vbg_list)

            # -------- ramp to start (STOP-aware, immediate ramp-to-0 on STOP) --------
            _raise_if_stop(self.stop_cb, iv=iv, ramp_step=self.ramp_step_size, ramp_dt=self.ramp_step_time, log=log, msg="🛑 STOP before ramp-to-start")

            if iv is not None:
                start_bg, start_tg = vbg_list[0], vtg_list[0]
                log(f"Ramping to start: Vbg={start_bg:g} V, Vtg={start_tg:g} V")
                _ramp_linear_2ch(
                    iv=iv,
                    Vbg0=None, Vtg0=None,
                    Vbg1=(start_bg if has_vbg else None),
                    Vtg1=(start_tg if has_vtg else None),
                    step=self.ramp_step_size,
                    dt=self.ramp_step_time,
                    stop_cb=self.stop_cb,
                    log=log,
                )
                _iv_set_gates_jump(iv, Vbg=(start_bg if has_vbg else None), Vtg=(start_tg if has_vtg else None), delay_s=0.0)

                if vbias_list is not None:
                    vb_start = float(vbias_list[0])
                    log(f"Ramping bias to start: Vbias={vb_start:g} V")
                    _ramp_bias_1ch(
                        iv=iv,
                        V0=None,
                        V1=vb_start,
                        step=self.ramp_step_size,
                        dt=self.ramp_step_time,
                        stop_cb=self.stop_cb,
                        log=log,
                    )
                    _iv_set_bias_jump(iv, vb_start, delay_s=0.0)
                    ctx.axes["Vbias"] = vb_start

                _log_measured_state(iv, log, tag="at start")

            # -------- main sweep --------
            for i in range(total):
                _raise_if_stop(self.stop_cb, iv=iv, ramp_step=self.ramp_step_size, ramp_dt=self.ramp_step_time, log=log, msg="🛑 STOP during sweep loop")

                vbg_set = vbg_list[i]
                vtg_set = vtg_list[i]

                if iv is not None:
                    _iv_set_gates_jump(iv, Vbg=(vbg_set if has_vbg else None), Vtg=(vtg_set if has_vtg else None), delay_s=0.0)

                vbias_set = None
                if iv is not None and vbias_list is not None:
                    vbias_set = float(vbias_list[i])
                    _iv_set_bias_jump(iv, vbias_set, delay_s=0.0)
                    ctx.axes["Vbias"] = vbias_set

                if self.jump_settle_s > 0:
                    _sleep_with_stop(self.jump_settle_s, self.stop_cb, iv=iv, ramp_step=self.ramp_step_size, ramp_dt=self.ramp_step_time, log=log, check_dt=0.01)

                # readback for csv/log
                bg_meas, tg_meas = _safe_read_current(iv)
                vb_meas = _safe_read_bias_voltage(iv) if vbias_set is not None else None

                # quick log
                log(
                    f"[{i+1}/{total}] "
                    f"Vbg_set={vbg_set:g}, Vbg={bg_meas if bg_meas is not None else vbg_set:g}; "
                    f"Vtg_set={vtg_set:g}, Vtg={tg_meas if tg_meas is not None else vtg_set:g}; "
                    f"Vbias_set={vbias_set if vbias_set is not None else '—'}, Vbias={vb_meas if vb_meas is not None else (vbias_set if vbias_set is not None else '—')}"
                )

                _sleep_with_stop(self.measure_delay, self.stop_cb, iv=iv, ramp_step=self.ramp_step_size, ramp_dt=self.ramp_step_time, log=log, check_dt=0.01)

                # NOTE: can't interrupt a blocking acquire unless your spectrometer driver supports abort.
                _raise_if_stop(self.stop_cb, iv=iv, ramp_step=self.ramp_step_size, ramp_dt=self.ramp_step_time, log=log, msg="🛑 STOP before acquire")

                wl, intens = spec.acquire()
                wl = list(map(float, wl))
                intens = list(map(float, intens))

                if getattr(csvw, "_data_rows_written", 0) == 0:
                    wl_headers = wl
                    if hasattr(csvw, "set_wavelength_headers"):
                        csvw.set_wavelength_headers(wl_headers)
                else:
                    wl_headers = wl_headers or getattr(csvw, "wavelength_headers", None)

                if wl_headers and len(intens) != len(wl_headers):
                    if len(intens) > len(wl_headers):
                        intens = intens[: len(wl_headers)]
                    else:
                        intens = intens + [0.0] * (len(wl_headers) - len(intens))

                # currents
                Ibg = Itg = Ib = None
                try:
                    if iv is not None and hasattr(iv, "read_currents"):
                        Ibg, Itg, Ib = iv.read_currents()
                except Exception:
                    pass

                scalars = {
                    "Vbg_set": float(vbg_set) if has_vbg else None,
                    "Vtg_set": float(vtg_set) if has_vtg else None,
                    "Vbias_set": float(vbias_set) if vbias_set is not None else None,

                    "Vbg": float(bg_meas) if (bg_meas is not None and np.isfinite(bg_meas)) else (float(vbg_set) if has_vbg else None),
                    "Vtg": float(tg_meas) if (tg_meas is not None and np.isfinite(tg_meas)) else (float(vtg_set) if has_vtg else None),
                    "Vbias": float(vb_meas) if (vb_meas is not None and np.isfinite(vb_meas)) else (float(vbias_set) if vbias_set is not None else None),

                    "Ibg": None if Ibg is None else float(Ibg),
                    "Itg": None if Itg is None else float(Itg),
                    "Ibias": None if Ib is None else float(Ib),
                }

                if hasattr(csvw, "write_row"):
                    csvw.write_row(scalars, intens)
                else:
                    csvw.add_row(scalars, intens)

                cb = getattr(self, "frame_progress_cb", None)
                if callable(cb):
                    try:
                        cb(i + 1, total)
                    except Exception:
                        pass

                ctx.axes["Vbg"] = scalars["Vbg"]
                ctx.axes["Vtg"] = scalars["Vtg"]
                if vbias_set is not None:
                    ctx.axes["Vbias"] = scalars["Vbias"]

        except StopRequested:
            log("🛑 StopRequested — stopped by user.")
            raise

        except Exception as e:
            log(f"Error: {e}")
            raise

        finally:
            # ALWAYS try to ramp outputs to 0V (even after errors / stop)
            if iv is not None:
                log("Ramping outputs to 0V (finalizer)...")
                _ramp_all_to_zero(iv, ramp_step=self.ramp_step_size, ramp_dt=self.ramp_step_time, log=log)
                try:
                    ctx.axes["Vbg"] = 0.0
                    ctx.axes["Vtg"] = 0.0
                    ctx.axes["Vbias"] = 0.0
                except Exception:
                    pass
                log("Outputs at 0V.")
