# app/steps/dual_gate_sweep.py
from __future__ import annotations

import time
import inspect
from typing import Dict, Any, Tuple, Iterable, Optional

import numpy as np

from app.devices.spectrum_alignment import align_wavelengths_to_intensities
from .registry import register


class StopRequested(Exception):
    """User requested stop; triggers safe ramp-down in finally."""
    pass


# -------------------- STOP + IV compat helpers --------------------

def _stop_requested(stop_cb) -> bool:
    try:
        return bool(stop_cb()) if callable(stop_cb) else False
    except Exception:
        return False


def _raise_if_stop(stop_cb, *, log=None, msg="🛑 STOP requested"):
    if _stop_requested(stop_cb):
        try:
            if callable(log):
                log(msg)
        except Exception:
            pass
        raise StopRequested()


def _sleep_with_stop(total_s: float, stop_cb, *, log=None, check_dt: float = 0.05):
    """Sleep in small chunks so STOP is responsive during measure_delay."""
    if total_s <= 0:
        return
    t0 = time.time()
    while True:
        _raise_if_stop(stop_cb, log=log, msg="🛑 STOP requested during wait — aborting (will ramp to 0V).")
        dt = time.time() - t0
        if dt >= total_s:
            return
        time.sleep(min(check_dt, total_s - dt))


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


def _iv_has_role(iv, role: str) -> bool:
    try:
        return bool(getattr(iv, "has_role")(role))
    except Exception:
        return False


def _iv_set_gates_jump(iv, *, Vbg=None, Vtg=None, delay_s: float = 0.0):
    """Jump to gates with maximum API compatibility (keyword first, then positional)."""
    if iv is None or not hasattr(iv, "set_gates"):
        return
    fn = getattr(iv, "set_gates")

    # keyword attempt (only include provided channels)
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

    # positional fallback only safe if BOTH provided
    if (Vbg is not None) and (Vtg is not None):
        try:
            _call_compat(fn, float(Vbg), float(Vtg), delay_s=float(delay_s), ramp_step=0.0)
            return
        except Exception:
            # last resort
            fn(float(Vbg), float(Vtg))


def _iv_set_bias_jump(iv, Vbias: float, *, delay_s: float = 0.0):
    """Jump bias with compatibility (keyword Vbias, else positional)."""
    if iv is None:
        return
    fn = getattr(iv, "set_bias", None) or getattr(iv, "set_vbias", None)
    if fn is None:
        return
    try:
        _call_compat(fn, Vbias=float(Vbias), delay_s=float(delay_s), ramp_step=0.0)
    except TypeError:
        _call_compat(fn, float(Vbias), delay_s=float(delay_s), ramp_step=0.0)


def _iv_write_x_only(iv, values: dict, *, delay_s: float = 0.0) -> bool:
    """
    Write-only setpoints WITHOUT per-step READ?.
    Uses iv.setup.x_channel_collection if available.
    Returns True if succeeded, else False.
    """
    if iv is None:
        return False

    setup = getattr(iv, "setup", None)
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

    for inst in insts:
        try:
            inst.write_x()
        except Exception:
            pass

    if delay_s and float(delay_s) > 0:
        time.sleep(float(delay_s))

    return True


def _iv_set_gates_rampstep(iv, *, Vbg=None, Vtg=None):
    """
    Used ONLY inside ramp-to-start and ramp-down-to-0 loops.
    Writes setpoints without per-step READ?.
    No sleeping here; the caller controls dt so we get exactly dt per step.
    """
    ok = _iv_write_x_only(iv, {"Vbg": Vbg, "Vtg": Vtg}, delay_s=0.0)
    if not ok:
        _iv_set_gates_jump(iv, Vbg=Vbg, Vtg=Vtg, delay_s=0.0)


def _iv_set_bias_rampstep(iv, Vbias: float):
    """
    Writes bias setpoints without per-step READ?.
    No sleeping here; the caller controls dt so we get exactly dt per step.
    """
    ok = _iv_write_x_only(iv, {"Vbias": Vbias}, delay_s=0.0)
    if not ok:
        _iv_set_bias_jump(iv, float(Vbias), delay_s=0.0)


def _safe_read_setup_x(iv, role: str) -> Optional[float]:
    """
    Try reading a channel value via iv.setup.get_single_x_value(role) if available.
    This is often present even when read_current_* methods aren't.
    """
    if iv is None:
        return None
    try:
        setup = getattr(iv, "setup", None)
        getx = getattr(setup, "get_single_x_value", None) if setup else None
        if callable(getx):
            v = float(getx(str(role)))
            return v if np.isfinite(v) else None
    except Exception:
        pass
    return None


def _safe_read_current(iv) -> Tuple[Optional[float], Optional[float]]:
    """
    Read back actual gate voltages from the IV adapter if supported.
    Returns (Vbg_meas, Vtg_meas) or (None, None) if unavailable.
    """
    if iv is None:
        return None, None

    # preferred explicit method
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
            return (
                bg if (bg is not None and np.isfinite(bg)) else None,
                tg if (tg is not None and np.isfinite(tg)) else None,
            )
    except Exception:
        pass

    # fallback: setup channel read (setpoint cache)
    bg = _safe_read_setup_x(iv, "Vbg")
    tg = _safe_read_setup_x(iv, "Vtg")
    return bg, tg


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

    # fallback: setup channel read
    return _safe_read_setup_x(iv, "Vbias")


def _sync_ctx_gates(
    ctx,
    iv,
    *,
    has_vbg: bool,
    has_vtg: bool,
    fallback_vbg: Optional[float] = None,
    fallback_vtg: Optional[float] = None,
    log=None,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Best-effort: read back current gates and store them into ctx.axes.
    If readback is unavailable, fall back to provided fallbacks (or keep existing ctx.axes).
    Returns (Vbg_now, Vtg_now) from ctx.axes (may be None).
    """
    if not getattr(ctx, "axes", None):
        ctx.axes = {}

    bg_meas, tg_meas = _safe_read_current(iv)

    if has_vbg:
        if bg_meas is not None:
            ctx.axes["Vbg"] = float(bg_meas)
        elif fallback_vbg is not None:
            ctx.axes["Vbg"] = float(fallback_vbg)

    if has_vtg:
        if tg_meas is not None:
            ctx.axes["Vtg"] = float(tg_meas)
        elif fallback_vtg is not None:
            ctx.axes["Vtg"] = float(fallback_vtg)

    try:
        if callable(log):
            log(f"Synced gates: Vbg={ctx.axes.get('Vbg', None)}, Vtg={ctx.axes.get('Vtg', None)}")
    except Exception:
        pass

    return ctx.axes.get("Vbg", None), ctx.axes.get("Vtg", None)


def _sync_ctx_bias(
    ctx,
    iv,
    *,
    has_vbias: bool,
    fallback_vbias: Optional[float] = None,
    log=None,
) -> Optional[float]:
    """
    Best-effort: read back bias and store into ctx.axes.
    Returns ctx.axes["Vbias"] (may be None).
    """
    if not getattr(ctx, "axes", None):
        ctx.axes = {}

    if not has_vbias:
        return None

    vb_meas = _safe_read_bias_voltage(iv)
    if vb_meas is not None:
        ctx.axes["Vbias"] = float(vb_meas)
    elif fallback_vbias is not None:
        ctx.axes["Vbias"] = float(fallback_vbias)

    try:
        if callable(log):
            log(f"Synced bias: Vbias={ctx.axes.get('Vbias', None)}")
    except Exception:
        pass

    return ctx.axes.get("Vbias", None)


def _as_float_list(arr: Iterable[float]) -> list[float]:
    return [float(x) for x in arr]


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


def _ramp_linear_2ch(
    *,
    iv,
    Vbg0=None, Vtg0=None,
    Vbg1=None, Vtg1=None,
    step: float = 0.1,
    dt: float = 0.05,
    stop_cb=None,
    log=None,
):
    """
    STOP-aware ramp (works even if IV adapter doesn't support ramp_step/stop_cb kwargs).
    Ramps both channels together in N steps where max(|Δ|)/N <= step.

    IMPORTANT:
      We do NOT sleep inside _iv_set_gates_rampstep. We sleep here with _sleep_with_stop(dt),
      so you get exactly "one 0.1V step per dt seconds" (e.g. dt=0.05).
    """
    if iv is None:
        return

    if (Vbg1 is None) and (Vtg1 is None):
        return

    if Vbg0 is None or Vtg0 is None:
        bgm, tgm = _safe_read_current(iv)
        if Vbg0 is None:
            Vbg0 = bgm
        if Vtg0 is None:
            Vtg0 = tgm

    if Vbg0 is None:
        Vbg0 = 0.0
    if Vtg0 is None:
        Vtg0 = 0.0

    dbg = 0.0 if Vbg1 is None else float(Vbg1) - float(Vbg0)
    dtg = 0.0 if Vtg1 is None else float(Vtg1) - float(Vtg0)

    maxd = max(abs(dbg), abs(dtg))
    if maxd < 1e-15:
        _iv_set_gates_rampstep(iv, Vbg=Vbg1, Vtg=Vtg1)
        return

    step = max(float(step), 1e-6)
    n = int(np.ceil(maxd / step))
    n = max(n, 1)

    for k in range(1, n + 1):
        _raise_if_stop(stop_cb, log=log, msg="🛑 STOP requested during ramp — aborting (will ramp to 0V).")

        frac = k / n
        bg = None if Vbg1 is None else (float(Vbg0) + dbg * frac)
        tg = None if Vtg1 is None else (float(Vtg0) + dtg * frac)

        _iv_set_gates_rampstep(iv, Vbg=bg, Vtg=tg)

        if dt > 0:
            _sleep_with_stop(float(dt), stop_cb, log=log, check_dt=min(0.05, float(dt)))


def _ramp_bias_1ch(
    *,
    iv,
    V0: Optional[float],
    V1: float,
    step: float = 0.1,
    dt: float = 0.05,
    stop_cb=None,
    log=None,
):
    """
    STOP-aware bias ramp. Same timing rule: one step per dt seconds.
    """
    if iv is None:
        return

    if V0 is None:
        vm = _safe_read_bias_voltage(iv)
        V0 = vm

    if V0 is None:
        V0 = 0.0

    d = float(V1) - float(V0)
    if abs(d) < 1e-15:
        _iv_set_bias_rampstep(iv, float(V1))
        return

    step = max(float(step), 1e-6)
    n = int(np.ceil(abs(d) / step))
    n = max(n, 1)

    for k in range(1, n + 1):
        _raise_if_stop(stop_cb, log=log, msg="🛑 STOP requested during bias ramp — aborting (will ramp to 0V).")
        v = float(V0) + d * (k / n)
        _iv_set_bias_rampstep(iv, v)
        if dt > 0:
            _sleep_with_stop(float(dt), stop_cb, log=log, check_dt=min(0.05, float(dt)))


def _ramp_all_to_zero(
    iv,
    *,
    has_vbg: bool,
    has_vtg: bool,
    has_vbias: bool,
    ramp_step: float,
    ramp_dt: float,
    log=None,
    Vbg0: Optional[float] = None,
    Vtg0: Optional[float] = None,
    Vbias0: Optional[float] = None,
):
    """
    Best-effort ramp-down that does NOT depend on adapter kwargs.

    IMPORTANT:
      No "snap-to-0V" at the end (you said never jump 1V->0V in one step).
      The ramp itself reaches 0 in steps.
    """
    if iv is None:
        return

    # Bias first
    try:
        if has_vbias:
            _ramp_bias_1ch(
                iv=iv,
                V0=Vbias0,
                V1=0.0,
                step=ramp_step,
                dt=ramp_dt,
                stop_cb=None,  # ALWAYS ramp down
                log=log,
            )
    except Exception as e:
        if callable(log):
            log(f"Bias ramp-down failed (ignored): {e}")

    # Gates
    try:
        _ramp_linear_2ch(
            iv=iv,
            Vbg0=Vbg0,
            Vtg0=Vtg0,
            Vbg1=(0.0 if has_vbg else None),
            Vtg1=(0.0 if has_vtg else None),
            step=ramp_step,
            dt=ramp_dt,
            stop_cb=None,  # ALWAYS ramp down
            log=log,
        )
    except Exception as e:
        if callable(log):
            log(f"Gate ramp-down failed (ignored): {e}")


# ------------------------------ Step ------------------------------

@register
class DualGateSweep:
    """
    Dual-gate (+ optional bias) sweep that:
      • ramps to the initial point,
      • jumps (no ramp) between sweep points (measurement frames),
      • ramps gates back to 0 V at the end (STOP-safe),
      • reads back actual Vbg/Vtg/Vbias if supported and writes those to CSV,
      • on STOP: still ramps outputs to 0V even if readback methods are missing.
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
        self.ramp_step_time: float = float(cfg.get("ramp_step_time", 0.15))  # default 0.05s

        # Optional tiny settle after a jump (during the sweep)
        self.jump_settle_s: float = float(cfg.get("jump_settle_s", 0.0))

        # Optional: per-frame progress callback injected by UI layer (Streamlit)
        # Signature: cb(frame_i: int, frame_total: int) where frame_i is 1-based
        self.frame_progress_cb = cfg.get("frame_progress_cb", None)

        # STOP callback (callable -> bool)
        self.stop_cb = cfg.get("stop_cb", None)

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

        # Helper formatters (log only)
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

        # Wavelength headers setup (lock to first acquired wl)
        wl_headers = None
        if (not wl_headers) and hasattr(spec, "calibration_wavelengths"):
            try:
                wl_headers = list(spec.calibration_wavelengths())
                if hasattr(csvw, "set_wavelength_headers"):
                    csvw.set_wavelength_headers(wl_headers)
            except Exception:
                pass

        # -------- pre-validate roles ----------
        if abs(self.vbg_start - self.vbg_stop) > 1e-12 and not has_vbg:
            raise RuntimeError("Sweep includes Vbg but no 'Vbg' role configured.")
        if abs(self.vtg_start - self.vtg_stop) > 1e-12 and not has_vtg:
            raise RuntimeError("Sweep includes Vtg but no 'Vtg' role configured.")

        # Build setpoint lists
        vbg_list = _as_float_list(np.linspace(self.vbg_start, self.vbg_stop, int(self.npts), dtype=float))
        vtg_list = _as_float_list(np.linspace(self.vtg_start, self.vtg_stop, int(self.npts), dtype=float))

        vbias_list: Optional[list[float]] = None
        if (self.vbias_start is not None) or (self.vbias_stop is not None):
            if not iv or (not has_vbias) or (not (hasattr(iv, "set_bias") or hasattr(iv, "set_vbias"))):
                raise RuntimeError("Sweep includes Vbias but no 'Vbias' role or iv.set_bias()/set_vbias() is available.")

            vb0 = self.vbias_start if self.vbias_start is not None else self.vbias_stop
            vb1 = self.vbias_stop if self.vbias_stop is not None else self.vbias_start
            vbias_list = _as_float_list(np.linspace(float(vb0), float(vb1), int(self.npts), dtype=float))

        total = len(vbg_list)

        try:
            # --------- Ramp to start (STOP-aware) ----------
            if iv:
                start_bg, start_tg = vbg_list[0], vtg_list[0]

                # Always read current gates before any ramp
                _sync_ctx_gates(
                    ctx,
                    iv,
                    has_vbg=has_vbg,
                    has_vtg=has_vtg,
                    fallback_vbg=ctx.axes.get("Vbg", None),
                    fallback_vtg=ctx.axes.get("Vtg", None),
                    log=None,
                )

                log(f"Ramping to start: Vbg={start_bg:g} V, Vtg={start_tg:g} V")
                _ramp_linear_2ch(
                    iv=iv,
                    Vbg0=ctx.axes.get("Vbg", None),
                    Vtg0=ctx.axes.get("Vtg", None),
                    Vbg1=(start_bg if has_vbg else None),
                    Vtg1=(start_tg if has_vtg else None),
                    step=self.ramp_step_size,
                    dt=self.ramp_step_time,
                    stop_cb=self.stop_cb,
                    log=log,
                )

                # After ramp: read current gates and store, then continue
                _sync_ctx_gates(
                    ctx,
                    iv,
                    has_vbg=has_vbg,
                    has_vtg=has_vtg,
                    fallback_vbg=start_bg if has_vbg else None,
                    fallback_vtg=start_tg if has_vtg else None,
                    log=None,
                )

                # Bias ramp to start (STOP-aware)
                if vbias_list is not None:
                    vb_start = float(vbias_list[0])
                    log(f"Ramping bias to start: Vbias={vb_start:g} V")

                    _sync_ctx_bias(
                        ctx,
                        iv,
                        has_vbias=has_vbias,
                        fallback_vbias=ctx.axes.get("Vbias", None),
                        log=None,
                    )

                    _ramp_bias_1ch(
                        iv=iv,
                        V0=ctx.axes.get("Vbias", None),
                        V1=vb_start,
                        step=self.ramp_step_size,
                        dt=self.ramp_step_time,
                        stop_cb=self.stop_cb,
                        log=log,
                    )

                    _sync_ctx_bias(ctx, iv, has_vbias=has_vbias, fallback_vbias=vb_start, log=None)

            # --------- Main sweep (jump between points) ----------
            for i in range(total):
                _raise_if_stop(self.stop_cb, log=log, msg="🛑 STOP requested — aborting sweep (will ramp outputs to 0V).")

                vbg_set = vbg_list[i]
                vtg_set = vtg_list[i]

                # Jump gates (no ramp) — measurement part stays "manual jump by frames"
                if iv:
                    try:
                        _iv_set_gates_jump(
                            iv,
                            Vbg=(vbg_set if has_vbg else None),
                            Vtg=(vtg_set if has_vtg else None),
                            delay_s=0.05,
                        )
                    except Exception as e:
                        log(f"Set gates failed: {e}")

                # Immediately read current gates after any change (your rule)
                bg_meas, tg_meas = _sync_ctx_gates(
                    ctx,
                    iv,
                    has_vbg=has_vbg,
                    has_vtg=has_vtg,
                    fallback_vbg=(vbg_set if has_vbg else None),
                    fallback_vtg=(vtg_set if has_vtg else None),
                    log=None,
                )

                # Jump bias (no ramp) if requested
                vbias_set = None
                vbias_meas = None
                if iv and vbias_list is not None:
                    try:
                        vbias_set = float(vbias_list[i])
                        _iv_set_bias_jump(iv, vbias_set, delay_s=0.05)
                    except Exception as e:
                        log(f"Set bias failed: {e}")
                        vbias_set = None

                    vbias_meas = _sync_ctx_bias(
                        ctx,
                        iv,
                        has_vbias=has_vbias,
                        fallback_vbias=(vbias_set if vbias_set is not None else None),
                        log=None,
                    )

                # Optional settle (STOP-aware)
                if self.jump_settle_s > 0:
                    _sleep_with_stop(self.jump_settle_s, self.stop_cb, log=log, check_dt=0.02)

                nan = float("nan")
                vbg_use = float(bg_meas) if (has_vbg and bg_meas is not None and np.isfinite(bg_meas)) else (float(vbg_set) if has_vbg else nan)
                vtg_use = float(tg_meas) if (has_vtg and tg_meas is not None and np.isfinite(tg_meas)) else (float(vtg_set) if has_vtg else nan)

                # LOG ONLY: currents + bias
                Ibg = Itg = Ibias = None
                if iv and hasattr(iv, "read_currents"):
                    try:
                        Ibg, Itg, Ibias = iv.read_currents()
                    except Exception:
                        pass

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

                # Keep context axes updated
                if has_vbg:
                    ctx.axes["Vbg"] = float(vbg_use) if np.isfinite(vbg_use) else float(vbg_set)
                if has_vtg:
                    ctx.axes["Vtg"] = float(vtg_use) if np.isfinite(vtg_use) else float(vtg_set)
                if vbias_set is not None:
                    ctx.axes["Vbias"] = float(vbias_meas) if (vbias_meas is not None and np.isfinite(vbias_meas)) else float(vbias_set)

                # Wait before spectral acquisition (STOP-aware)
                _sleep_with_stop(self.measure_delay, self.stop_cb, log=log, check_dt=0.01)

                # Acquire spectrum (can't interrupt a blocking acquire unless driver supports abort)
                _raise_if_stop(self.stop_cb, log=log, msg="🛑 STOP requested before acquire — aborting (will ramp to 0V).")

                wl, intens = spec.acquire()
                wl, intens = align_wavelengths_to_intensities(wl, intens)
                wl = wl.tolist()
                intens = intens.tolist()

                # On first row, force CSV wavelength header = actual wl from acquire()
                if getattr(csvw, "_data_rows_written", 0) == 0:
                    wl_headers = wl
                    if hasattr(csvw, "set_wavelength_headers"):
                        csvw.set_wavelength_headers(wl_headers)
                else:
                    wl_headers = wl_headers or getattr(csvw, "wavelength_headers", None)

                if wl_headers and len(intens) != len(wl_headers):
                    raise RuntimeError(
                        "Spectrometer output length changed during the sweep: "
                        f"expected {len(wl_headers)} samples, received {len(intens)}."
                    )

                # Measured bias voltage (if bias is active)
                vbias_meas_v = float(vbias_meas) if (vbias_set is not None and vbias_meas is not None and np.isfinite(vbias_meas)) else None

                # Currents (A). Prefer read_currents()
                Ibg_a = Itg_a = Ibias_a = None
                if iv and hasattr(iv, "read_currents"):
                    try:
                        Ibg_a, Itg_a, Ibias_a = iv.read_currents()
                    except Exception:
                        Ibg_a = Itg_a = Ibias_a = None

                scalars = {
                    "Vbg_set":   float(vbg_set) if has_vbg else None,
                    "Vtg_set":   float(vtg_set) if has_vtg else None,
                    "Vbias_set": float(vbias_set) if vbias_set is not None else None,

                    "Vbg":   float(vbg_use) if has_vbg else None,
                    "Vtg":   float(vtg_use) if has_vtg else None,
                    "Vbias": float(vbias_meas_v) if vbias_meas_v is not None else (float(vbias_set) if vbias_set is not None else None),

                    "Ibg":   None if Ibg_a is None else float(Ibg_a),
                    "Itg":   None if Itg_a is None else float(Itg_a),
                    "Ibias": None if Ibias_a is None else float(Ibias_a),
                }

                if hasattr(csvw, "write_row"):
                    csvw.write_row(scalars, intens)
                else:
                    csvw.add_row(scalars, intens)

                cb = getattr(self, "frame_progress_cb", None)
                if callable(cb):
                    try:
                        cb(i + 1, total)
                    except StopRequested:
                        raise
                    except Exception:
                        pass

        except StopRequested:
            log("🛑 StopRequested — exiting sweep (will ramp outputs to 0V).")
            raise

        except Exception as e:
            log(f"Error: {e}")
            raise

        finally:
            # --------- ALWAYS ramp to 0V (STOP-immune) ----------
            if iv:
                log("Ramping outputs down to 0V...")

                def _finite_or_none(x):
                    try:
                        x = float(x)
                        return x if np.isfinite(x) else None
                    except Exception:
                        return None

                bg0 = _finite_or_none(ctx.axes.get("Vbg", None))
                tg0 = _finite_or_none(ctx.axes.get("Vtg", None))
                vb0 = _finite_or_none(ctx.axes.get("Vbias", None))

                _ramp_all_to_zero(
                    iv,
                    has_vbg=has_vbg,
                    has_vtg=has_vtg,
                    has_vbias=has_vbias,
                    ramp_step=self.ramp_step_size,
                    ramp_dt=self.ramp_step_time,
                    log=log,
                    Vbg0=bg0,
                    Vtg0=tg0,
                    Vbias0=vb0,
                )

                # Measure after ramp (best effort) and log
                bgm, tgm = _safe_read_current(iv)
                vbm = _safe_read_bias_voltage(iv) if has_vbias else None

                Ibg = Itg = Ibias = None
                if hasattr(iv, "read_currents"):
                    try:
                        Ibg, Itg, Ibias = iv.read_currents()
                    except Exception:
                        pass

                log(
                    "Ramp-down done. "
                    f"Vbg={_fmt_v(bgm)} V, Vtg={_fmt_v(tgm)} V"
                    + (f", Vbias={_fmt_v(vbm)} V" if has_vbias else "")
                    + f" | Ibg={_fmt_i(Ibg)}, Itg={_fmt_i(Itg)}"
                    + (f", Ibias={_fmt_i(Ibias)}" if has_vbias else "")
                )

                # Update ctx.axes to reflect safe state (0V)
                try:
                    if has_vbg:
                        ctx.axes["Vbg"] = 0.0
                    if has_vtg:
                        ctx.axes["Vtg"] = 0.0
                    if has_vbias:
                        ctx.axes["Vbias"] = 0.0
                except Exception:
                    pass

                log("Outputs at 0V.")
