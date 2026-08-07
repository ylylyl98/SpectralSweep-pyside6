"""Continuous two-angle MCD measurement panel."""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from utils.config import cfg
from utils.filename_builder import (
    FilenameContext,
    build_base_filename,
    format_compact_number,
    make_unique_stem,
    sanitize_token,
)


class _MCDStopRequested(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", str(value).strip())
    return cleaned.strip(" .") or "MCD"


def _spectrum_1d(wavelengths, counts) -> tuple[np.ndarray, np.ndarray]:
    wl = np.asarray(wavelengths, dtype=float).ravel()
    data = np.asarray(counts, dtype=float)
    if data.ndim == 2:
        if data.shape[-1] == wl.size:
            data = np.nanmean(data, axis=0)
        elif data.shape[0] == wl.size:
            data = np.nanmean(data, axis=1)
        else:
            data = data.ravel()
    else:
        data = data.ravel()
    n = min(wl.size, data.size)
    if n < 2:
        raise RuntimeError("LightField returned an empty spectrum")
    return wl[:n], data[:n]


MCD_SCALAR_FIELDS = [
    "Bfield_T",
    "rotation_angle_deg",
    "Vtg_V",
    "Vbg_V",
    "Vbias_V",
    "Doping_V",
    "Efield_V",
]


def _mcd_coordinates(vtg_v: float, vbg_v: float, ratio: float) -> tuple[float, float]:
    doping = float(vtg_v) + float(ratio) * float(vbg_v)
    efield = float(vtg_v) - float(ratio) * float(vbg_v)
    return doping, efield


def _vtg_vbg_from_doping_efield(
    doping_v: float, efield_v: float, ratio: float
) -> tuple[float, float]:
    if abs(float(ratio)) <= 1e-12:
        raise ValueError(
            "Gate ratio r must be non-zero to compute Vtg/Vbg from Doping/E-field"
        )
    vtg = (float(doping_v) + float(efield_v)) / 2.0
    vbg = (float(doping_v) - float(efield_v)) / (2.0 * float(ratio))
    return vtg, vbg


def build_mcd_filename_base(params: dict) -> str:
    """Build a Dual-Gate-style base name plus fixed MCD context tokens."""
    style = str(params.get("decimal_style", "dot")).strip().lower()
    context = FilenameContext(
        device_id=params.get("sample_id", ""),
        point=params.get("point", ""),
        tag="",
        temperature=params.get("temperature", ""),
        mode=params.get("measurement_mode", "Ref"),
        laser_nm=params.get("laser_nm", ""),
        nominal_power_uw=params.get("power_uw", ""),
        center_nm=params.get("center_nm"),
        exposure_ms=params.get("exposure_ms"),
        accumulations=params.get("frames"),
        condition_label=params.get("condition_label", ""),
        power_coefficient=float(params.get("power_coefficient", 1.0)),
        decimal_style=style,
    )
    enabled_parts = [
        part
        for part in (params.get("filename_parts") or [])
        if part in {"temp_mode", "laser_power", "center", "exposure", "condition"}
    ]
    standard = build_base_filename(context, enabled_parts)

    def number(value, *, signed=False, decimals=4):
        return format_compact_number(
            value,
            keep_sign=signed,
            decimals=decimals,
            decimal_style=style,
        )

    doping, efield = _mcd_coordinates(
        params["vtg_v"], params["vbg_v"], params["gate_ratio"]
    )
    rotator_index = "1" if str(params["rotator"]).lower() == "rot1" else "2"
    suffix = [
        "MCD",
        f"B{number(params['start_t'], signed=True)}to{number(params['stop_t'], signed=True)}T",
        (
            f"Rot{rotator_index}-{number(params['angle_a_deg'], signed=True)}"
            f"to{number(params['angle_b_deg'], signed=True)}deg"
        ),
        f"D{number(doping)}V",
        f"E{number(efield)}V",
        f"Vtg{number(params['vtg_v'], signed=True)}",
        f"Vbg{number(params['vbg_v'], signed=True)}",
        f"Vb{number(params['vbias_v'], signed=True)}",
        f"r{number(params['gate_ratio'])}",
    ]
    return "_".join(
        [sanitize_token(standard)] + [sanitize_token(token) for token in suffix]
    )


class _ContinuousMCDWorker(QObject):
    log = Signal(str)
    progress = Signal(float, float, int, int)  # field, percentage, condition_index, count
    spectrum = Signal(object, object, str, float)
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, params: dict, magnet_ctrl, lf6_ctrl, rotation_ctrl, smu_ctrl):
        super().__init__()
        self._p = params
        self._magnet_ctrl = magnet_ctrl
        self._lf6_ctrl = lf6_ctrl
        self._rotation_ctrl = rotation_ctrl
        self._smu_ctrl = smu_ctrl
        self._stop = threading.Event()
        self._csv_path: Optional[Path] = None
        self._metadata_path: Optional[Path] = None
        self._event_path: Optional[Path] = None
        self._metadata: dict = {}
        self._spectra_written = 0
        self._total_legs = 1
        self._leg_index = 0
        self._leg_start_t = 0.0
        self._leg_target_t = 0.0
        self._condition_index = 1
        self._condition_count = 1
        self._total_spectra = 0

    def request_stop(self) -> None:
        self._stop.set()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise _MCDStopRequested("MCD stop requested")

    def _emit_log(self, text: str) -> None:
        self.log.emit(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    @Slot()
    def run(self) -> None:
        result: dict = {}
        try:
            result = self._run()
        except _MCDStopRequested:
            self._emit_log("Measurement stopped by user.")
            result["stopped"] = True
        except Exception as exc:
            self.error.emit(str(exc))
            result["error"] = str(exc)
        finally:
            adapter = self._magnet_ctrl.adapter
            if adapter is not None:
                try:
                    adapter.pause()
                    self._emit_log("APS100 sweep paused and confirmed.")
                except Exception as exc:
                    self._emit_log(f"WARNING: could not confirm APS100 pause: {exc}")
            if result.get("stopped"):
                self._set_metadata_status("stopped")
            elif result.get("error"):
                self._set_metadata_status("failed", error=result["error"])
            self.finished.emit(result)

    def _run(self) -> dict:
        p = self._p
        magnet = self._magnet_ctrl.adapter
        sweep_mode = str(p.get("sweep_mode", "one_way")).strip().lower()
        if sweep_mode == "round_trip":
            legs = [p["stop_t"], p["start_t"]]
        else:
            legs = [p["stop_t"]]
        if magnet is None or not getattr(magnet, "connected", False):
            raise RuntimeError("APS100 is not connected")
        if not self._lf6_ctrl.is_connected or self._lf6_ctrl.adapter is None:
            raise RuntimeError("LightField is not connected")
        rotator = self._rotation_ctrl.adapter(p["rotator"])
        if rotator is None:
            raise RuntimeError(f"{p['rotator'].upper()} is not connected")

        initial = magnet.read_snapshot()
        if initial.status.faulted:
            raise RuntimeError("APS100 reports a quench or power-module failure")
        if not initial.heater_on:
            raise RuntimeError("APS100 is not in driven mode (persistent heater is OFF)")
        if "paused" not in initial.sweep_state:
            raise RuntimeError("APS100 must be paused before starting MCD")

        conditions = self._resolve_conditions(p)
        total = len(conditions)
        self._condition_count = total
        self._total_legs = len(legs) * total
        magnet.take_remote()
        summary: list = []
        result: dict = {"csv_paths": [], "conditions": []}
        pending_error: Optional[BaseException] = None
        self._total_spectra = 0
        total_cycles = 0
        for index, condition in enumerate(conditions, start=1):
            if self._stop.is_set():
                break
            self._condition_index = index
            self._emit_log(
                f"Condition {index}/{total}: Vtg={condition.get('vtg_v', 0.0):.4g} V, "
                f"Vbg={condition.get('vbg_v', 0.0):.4g} V, "
                f"Vbias={condition.get('vbias_v', 0.0):.4g} V."
            )
            try:
                paths = self._run_condition(condition, index, total, legs)
            except _MCDStopRequested:
                summary.append({"condition": index, "status": "stopped"})
                result["stopped"] = True
                break
            except Exception as exc:
                summary.append(
                    {"condition": index, "status": "failed", "error": str(exc)}
                )
                pending_error = exc
                break
            summary.append(
                {
                    "condition": index,
                    "status": "completed",
                    "data_file": paths["csv_path"],
                }
            )
            result["csv_paths"].append(paths["csv_path"])
            self._total_spectra += paths["spectra_written"]
            total_cycles += paths["cycles"]
        for index in range(len(summary) + 1, total + 1):
            summary.append({"condition": index, "status": "not_started"})
        result["conditions"] = list(summary)
        self._finalize_run_summary(result, summary, total, sweep_mode)
        result["cycles"] = total_cycles
        result["spectra_written"] = self._total_spectra
        if result["csv_paths"]:
            result["csv_path"] = result["csv_paths"][-1]
        if pending_error is not None:
            raise pending_error
        return result

    def _resolve_conditions(self, p: dict) -> list[dict]:
        conditions = [
            dict(condition)
            for condition in p.get("conditions", [])
            if bool(condition.get("enabled", True))
        ]
        if not conditions:
            conditions = [
                {
                    "enabled": True,
                    "vtg_v": float(p.get("vtg_v", 0.0)),
                    "vbg_v": float(p.get("vbg_v", 0.0)),
                    "vbias_v": float(p.get("vbias_v", 0.0)),
                }
            ]
        return conditions

    def _finalize_run_summary(
        self, result: dict, summary: list, total: int, sweep_mode: str
    ) -> None:
        paths = result.get("csv_paths") or []
        payload = {
            "created_utc": _utc_now(),
            "sweep_mode": sweep_mode,
            "condition_count": total,
            "conditions": summary,
            "csv_paths": paths,
        }
        result["summary_path"] = None
        if not paths:
            return
        first = Path(paths[0])
        summary_path = first.with_name(f"{first.stem}_summary.json")
        summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        result["summary_path"] = str(summary_path)

    def _run_condition(
        self, condition: dict, index: int, total: int, legs: list
    ) -> dict:
        p = dict(self._p)
        p.update(
            {
                "vtg_v": float(condition.get("vtg_v", 0.0)),
                "vbg_v": float(condition.get("vbg_v", 0.0)),
                "vbias_v": float(condition.get("vbias_v", 0.0)),
                "condition_index": index,
                "condition_count": total,
            }
        )
        magnet = self._magnet_ctrl.adapter
        rotator = self._rotation_ctrl.adapter(p["rotator"])
        if rotator is None:
            raise RuntimeError(f"{p['rotator'].upper()} is not connected")
        self._csv_path, self._metadata_path, self._event_path = self._create_output_paths(p)
        self._event_path.write_text("", encoding="utf-8")
        self._event(
            "preflight",
            f"condition={index}/{total} field={magnet.get_field_t():.9g} T",
        )
        self._event("remote", "REMOTE")

        identity = getattr(magnet, "identity", None)
        doping, efield = _mcd_coordinates(
            p["vtg_v"], p["vbg_v"], p["gate_ratio"]
        )
        self._metadata = {
            "created_utc": _utc_now(),
            "status": "running",
            "mode": "continuous_two_angle_mcd",
            "parameters": p,
            "condition_index": index,
            "condition_count": total,
            "condition": {
                "vtg_v": p["vtg_v"],
                "vbg_v": p["vbg_v"],
                "vbias_v": p["vbias_v"],
                "doping_v": doping,
                "efield_v": efield,
            },
            "data_file": self._csv_path.name,
            "csv_scalar_columns": MCD_SCALAR_FIELDS,
            "sweep_mode": str(p.get("sweep_mode", "one_way")).strip().lower(),
            "legs": [],
            "field_definition": "mean of APS100 field readings before and after acquisition",
            "doping_definition": "Doping_V = Vtg_V + gate_ratio * Vbg_V",
            "efield_definition": "Efield_V = Vtg_V - gate_ratio * Vbg_V",
            "aps100_identity": asdict(identity) if identity is not None else {},
            "initial_snapshot": self._snapshot_dict(magnet.read_snapshot()),
        }
        self._write_metadata()

        self._configure_lightfield(p)
        bias_info = self._apply_voltages(p)
        self._metadata["applied_bias"] = bias_info["applied_bias"]
        self._metadata["bias_skipped_reason"] = bias_info["bias_skipped_reason"]
        self._metadata["post_ramp_readback"] = bias_info.get("readback", {})
        self._write_metadata()
        self._check_stop()

        low_t = min(p["start_t"], p["stop_t"])
        high_t = max(p["start_t"], p["stop_t"])
        rate_profile = [
            {
                "index": rate_index,
                "upper_range_a": upper_range_a,
                "rate_t_per_min": rate_t_per_min,
            }
            for rate_index, (upper_range_a, rate_t_per_min) in magnet.get_rates().items()
        ]
        self._metadata["aps100_slow_rate_profile"] = rate_profile
        self._write_metadata()
        self._event("aps100_slow_rate_profile", json.dumps(rate_profile))

        self._emit_log(
            f"Moving to start field {p['start_t']:.4f} T using the APS100 "
            "stored SLOW rate profile."
        )
        magnet.move_to_field(
            p["start_t"],
            tolerance_t=p["field_tolerance_t"],
            timeout_s=p["move_timeout_s"],
            stop_event=self._stop,
            progress=lambda field: self._emit_progress(field),
        )
        actual_limits = magnet.set_limits_t(low_t, high_t)
        self._event(
            "limits",
            f"LLIM={actual_limits[0]:.9g} T ULIM={actual_limits[1]:.9g} T",
        )
        if p["start_settle_s"] > 0:
            self._sleep_stop_aware(p["start_settle_s"])

        spec = self._lf6_ctrl.adapter
        wavelengths = np.asarray(
            spec.calibration_wavelengths(force=True), dtype=float
        ).ravel()
        if wavelengths.size < 2:
            raise RuntimeError("Could not obtain LightField wavelength calibration")

        wl_headers = [f"{value:.6f}" for value in wavelengths]
        cycle = 0
        self._spectra_written = 0
        assert self._csv_path is not None
        with self._csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(MCD_SCALAR_FIELDS + wl_headers)
            handle.flush()
            try:
                leg_log: list = []
                for leg_index, target in enumerate(legs, start=1):
                    self._check_stop()
                    leg_dir = magnet.start_sweep_to(target)
                    leg_direction = 1.0 if leg_dir == "up" else -1.0
                    leg_log.append(
                        {
                            "leg": leg_index,
                            "target": float(target),
                            "direction": leg_dir,
                        }
                    )
                    self._metadata["legs"] = list(leg_log)
                    self._event(
                        "leg_start",
                        f"leg={leg_index}/{len(legs)} target={target:.9g} T "
                        f"direction={leg_dir}",
                    )
                    self._emit_log(
                        f"Sweep leg {leg_index}/{len(legs)}: {leg_dir} "
                        f"toward {target:.4f} T."
                    )
                    self._leg_index = (index - 1) * len(legs) + (leg_index - 1)
                    self._leg_start_t = magnet.get_field_t()
                    self._leg_target_t = float(target)
                    while True:
                        self._check_stop()
                        current = magnet.get_field_t()
                        self._emit_progress(current)
                        if leg_direction * (current - target) >= 0:
                            break
                        order = ("A", "B") if cycle % 2 == 0 else ("B", "A")
                        for label in order:
                            self._check_stop()
                            angle = p["angle_a_deg"] if label == "A" else p["angle_b_deg"]
                            rotator.move_to(angle)
                            self._sleep_stop_aware(p["rotation_settle_s"])
                            measured_angle = float(rotator.get_position())
                            field_before = magnet.get_field_t()
                            wl, counts = _spectrum_1d(*spec.acquire())
                            field_after = magnet.get_field_t()
                            if wl.size != wavelengths.size or not np.allclose(
                                wl, wavelengths, rtol=0.0, atol=1e-6
                            ):
                                counts = np.interp(wavelengths, wl, counts)
                                wl = wavelengths
                            field_mean = (field_before + field_after) / 2.0
                            writer.writerow(
                                [
                                    field_mean,
                                    measured_angle,
                                    p["vtg_v"],
                                    p["vbg_v"],
                                    p["vbias_v"],
                                    doping,
                                    efield,
                                ]
                                + counts.tolist()
                            )
                            handle.flush()
                            self._spectra_written += 1
                            self.spectrum.emit(wl, counts, label, field_mean)
                            self._emit_progress(field_after)
                        cycle += 1
            finally:
                magnet.pause()
                self._event("sweep_pause", f"field={magnet.get_field_t():.9g} T")

        final_snapshot = magnet.read_snapshot()
        self._metadata.update(
            {
                "status": "completed",
                "completed_utc": _utc_now(),
                "final_snapshot": self._snapshot_dict(final_snapshot),
                "cycles": cycle,
                "spectra_written": self._spectra_written,
            }
        )
        self._write_metadata()
        self._emit_log(f"MCD spectra saved to {self._csv_path}")
        return {
            "csv_path": str(self._csv_path),
            "cycles": cycle,
            "spectra_written": self._spectra_written,
        }

    def _configure_lightfield(self, p: dict) -> None:
        setup = self._lf6_ctrl.setup
        if setup is None:
            return
        setup.change_spectra_center(f"{p['center_nm']:.0f}")
        setup.change_expose_time(float(p["exposure_ms"]))
        for name in ("set_accumulations", "set_frames", "change_frame_to_combine"):
            if hasattr(setup, name):
                getattr(setup, name)(int(p["frames"]))
                break
        self._emit_log("LightField settings applied.")

    def _apply_voltages(self, p: dict) -> dict:
        """Apply per-condition gates and optional bias; return applied-bias info."""
        info = {"applied_bias": False, "bias_skipped_reason": None, "readback": {}}
        if not p["apply_voltages"]:
            return info
        if self._smu_ctrl is None or not self._smu_ctrl.is_connected:
            raise RuntimeError("Sample voltages requested, but SMUs are not connected")
        device = self._smu_ctrl.device
        if device is None:
            raise RuntimeError("SMU device is unavailable")

        def _fmt(value) -> str:
            try:
                x = float(value)
                if math.isfinite(x):
                    return f"{x:.4g}"
            except (TypeError, ValueError):
                pass
            return "n/a"

        # 1) Pre-ramp voltage readback (mirrors the Dual Gate tab ramp flow):
        #    read the current voltages, then ramp to the set values.
        pre_vbg = pre_vtg = pre_vbias = None
        if hasattr(device, "read_current_gates"):
            try:
                pre_vbg, pre_vtg = device.read_current_gates()
            except Exception:
                pass
        if hasattr(device, "read_current_bias"):
            try:
                pre_vbias = device.read_current_bias()
            except Exception:
                pass
        self._emit_log(
            "Pre-ramp readback: "
            f"Vbg={_fmt(pre_vbg)} V, Vtg={_fmt(pre_vtg)} V"
            + (
                f", Vbias={_fmt(pre_vbias)} V"
                if pre_vbias is not None
                else ", Vbias=skipped"
            )
        )
        self._event(
            "voltage_pre_ramp",
            f"Vbg={_fmt(pre_vbg)} V Vtg={_fmt(pre_vtg)} V "
            f"Vbias={_fmt(pre_vbias)} V",
        )

        # 2) Stop-aware stepped ramp to the set voltages.
        device.set_gates(
            Vbg=p["vbg_v"],
            Vtg=p["vtg_v"],
            ramp_step=p["voltage_ramp_step_v"],
            delay_s=p["voltage_step_delay_s"],
            stop_cb=self._stop.is_set,
            stop_exc=_MCDStopRequested,
        )
        vbias = float(p.get("vbias_v", 0.0))
        bias_reason: Optional[str] = None
        if abs(vbias) <= 1e-9:
            bias_reason = "Vbias is 0 V"
        elif not hasattr(device, "set_bias"):
            bias_reason = "device has no set_bias"
        else:
            try:
                has_bias_channel = bool(self._smu_ctrl.has_vbias())
            except (AttributeError, TypeError):
                has_bias_channel = True
            if not has_bias_channel:
                bias_reason = "no connected Vbias channel"
        if bias_reason is None:
            device.set_bias(
                Vbias=vbias,
                ramp_step=p.get("voltage_vbias_step_v", p["voltage_ramp_step_v"]),
                delay_s=p["voltage_step_delay_s"],
                stop_cb=self._stop.is_set,
                stop_exc=_MCDStopRequested,
            )
            info["applied_bias"] = True
        else:
            info["bias_skipped_reason"] = bias_reason
            self._emit_log(f"Vbias skipped: {bias_reason}.")

        # 3) Settle, then read the resulting currents once (not during the
        #    magnetic sweep / spectrum acquisition, which stays fast).
        self._sleep_stop_aware(p["voltage_settle_s"])
        readback: dict = {}
        if hasattr(device, "read_currents"):
            try:
                ibg, itg, ib = device.read_currents()
            except Exception:
                ibg = itg = ib = None
            readback = {
                "Ibg_A": ibg,
                "Itg_A": itg,
                "Ibias_A": ib,
            }
            self._emit_log(
                "Post-ramp current: "
                f"Ibg={_fmt(ibg)} A, Itg={_fmt(itg)} A, Ibias={_fmt(ib)} A"
            )
            self._event(
                "voltage_settled",
                f"Vbg={_fmt(p['vbg_v'])} V Vtg={_fmt(p['vtg_v'])} V "
                f"Vbias={_fmt(p['vbias_v'])} V "
                f"Ibg={_fmt(ibg)} A Itg={_fmt(itg)} A Ibias={_fmt(ib)} A",
            )
        info["readback"] = readback
        self._emit_log("Sample voltages applied.")
        return info

    def _sleep_stop_aware(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            self._check_stop()
            time.sleep(min(0.05, deadline - time.monotonic()))

    def _emit_progress(self, field_t: float) -> None:
        if self._total_legs <= 1:
            start = self._p["start_t"]
            stop = self._p["stop_t"]
            fraction = (field_t - start) / (stop - start)
        else:
            span = self._leg_target_t - self._leg_start_t
            within = (
                (field_t - self._leg_start_t) / span
                if abs(span) > 1e-12
                else 0.0
            )
            fraction = (self._leg_index + within) / self._total_legs
        self.progress.emit(
            field_t,
            min(100.0, max(0.0, fraction * 100.0)),
            self._condition_index,
            self._condition_count,
        )

    def _create_output_paths(self, p: dict) -> tuple[Path, Path, Path]:
        sample = _safe_name(p["sample_id"])
        subfolder = _safe_name(p.get("subfolder", "MCD Data"))
        out_dir = Path(p["base_output_dir"]) / sample / subfolder
        base_stem = build_mcd_filename_base(p)
        stem = make_unique_stem(out_dir, base_stem)
        suffix = 1
        while any(
            (out_dir / f"{stem}{extension}").exists()
            for extension in (".csv", ".meta.json", ".log")
        ):
            stem = f"{base_stem}_{suffix:03d}"
            suffix += 1
        return (
            out_dir / f"{stem}.csv",
            out_dir / f"{stem}.meta.json",
            out_dir / f"{stem}.log",
        )

    def _event(self, event: str, detail: str) -> None:
        if self._event_path is None:
            return
        with self._event_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{_utc_now()}\t{event}\t{detail}\n")

    def _write_metadata(self) -> None:
        if self._metadata_path is not None:
            self._metadata_path.write_text(
                json.dumps(self._metadata, indent=2), encoding="utf-8"
            )

    def _set_metadata_status(self, status: str, *, error: Optional[str] = None) -> None:
        if not self._metadata:
            return
        if self._metadata.get("status") != "running":
            return
        self._metadata["status"] = str(status)
        self._metadata["ended_utc"] = _utc_now()
        self._metadata["spectra_written"] = self._spectra_written
        if error:
            self._metadata["error"] = str(error)
        try:
            self._write_metadata()
        except Exception:
            pass

    @staticmethod
    def _snapshot_dict(snapshot) -> dict:
        result = asdict(snapshot)
        result["status"] = asdict(snapshot.status)
        return result

class MCDPanel(QWidget):
    run_state_changed = Signal(bool)

    def __init__(self, magnet_ctrl, lf6_ctrl, rotation_ctrl, smu_ctrl, parent=None):
        super().__init__(parent)
        self._magnet = magnet_ctrl
        self._lf6 = lf6_ctrl
        self._rotation = rotation_ctrl
        self._smu = smu_ctrl
        self._thread: Optional[QThread] = None
        self._worker: Optional[_ContinuousMCDWorker] = None
        self._last_snapshot = None
        self._updating_table = False
        self._voltage_limit = abs(float(cfg.smu.volt_compliance_V))
        self._build_ui()
        self._wire_signals()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self._splitter)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setMinimumWidth(560)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        controls_layout.setContentsMargins(8, 6, 8, 6)
        controls_layout.setSpacing(6)
        self._scroll.setWidget(controls)
        self._splitter.addWidget(self._scroll)

        # APS100 connection and always-available pause.
        connection = QGroupBox("APS100 — attoDRY1000")
        conn_form = QFormLayout(connection)
        conn_form.setContentsMargins(8, 6, 8, 6)
        conn_form.setVerticalSpacing(4)
        conn_form.setHorizontalSpacing(8)
        connection.setToolTip(
            "Magnet power supply (Attocube APS100) connection, live status, "
            "and the always-available pause button."
        )
        self._resource = QLineEdit(cfg.magnet.visa_resource)
        self._resource.setToolTip(
            "VISA/serial address of the APS100 (e.g. ASRL5::INSTR) - "
            "where the magnet controller is connected."
        )
        self._mock = QCheckBox("Use mock APS100")
        self._mock.setToolTip(
            "Talk to a simulated APS100 instead of real hardware. "
            "Useful for testing the UI safely without an instrument."
        )
        resource_row = QHBoxLayout()
        resource_row.setSpacing(8)
        resource_row.addWidget(self._resource, stretch=1)
        resource_row.addWidget(self._mock)
        conn_form.addRow("VISA resource", resource_row)
        conn_buttons = QGridLayout()
        conn_buttons.setHorizontalSpacing(6)
        conn_buttons.setVerticalSpacing(4)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setToolTip(
            "Open the link and identify the APS100. Does not change its "
            "operating mode - use Take Remote to take control."
        )
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setToolTip(
            "Close the link. Returns the APS100 to local/front-panel mode "
            "if it was in remote mode."
        )
        self._remote_btn = QPushButton("Take Remote")
        self._remote_btn.setToolTip(
            "Hand control from the front panel to the computer (sends REMOTE). "
            "Required before ramping, sweeping, or heater transitions."
        )
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setToolTip(
            "Re-read field, heater, sweep state, and faults from the APS100."
        )
        for column, button in enumerate(
            (self._connect_btn, self._disconnect_btn, self._remote_btn)
        ):
            conn_buttons.addWidget(button, 0, column)
            conn_buttons.setColumnStretch(column, 1)
        conn_buttons.addWidget(self._refresh_btn, 1, 0)
        self._identity = QLabel("Disconnected")
        self._identity.setToolTip("Device name reported by the APS100 (*IDN?).")
        self._field = QLabel("—")
        self._field.setToolTip("Field measured in the magnet coil (persistent side).")
        self._output = QLabel("—")
        self._output.setToolTip(
            "Field from the power-supply leads, with the drive current in amperes."
        )
        self._heater = QLabel("—")
        self._heater.setToolTip(
            "Persistent heater ON = driven mode; OFF = persistent mode."
        )
        self._sweep = QLabel("—")
        self._sweep.setToolTip("APS100 sweep state (e.g. paused, sweeping).")
        self._fault = QLabel("—")
        self._fault.setToolTip(
            "Active faults: quench, power-module failure, or front-panel menu lock."
        )
        self._pause_btn = QPushButton("PAUSE MAGNET")
        self._pause_btn.setToolTip(
            "Immediately pause the sweep / hold the field. Also stops an active MCD run."
        )
        self._pause_btn.setStyleSheet(
            "QPushButton { background:#a82020; color:white; font-weight:700; min-height:34px; }"
        )
        conn_buttons.addWidget(self._pause_btn, 1, 1, 1, 2)
        conn_form.addRow(conn_buttons)

        def caption(text: str) -> QLabel:
            label = QLabel(text)
            label.setStyleSheet("color:#5b6b7f;")
            return label

        status_widget = QWidget()
        status_grid = QGridLayout(status_widget)
        status_grid.setContentsMargins(0, 0, 0, 0)
        status_grid.setHorizontalSpacing(10)
        status_grid.setVerticalSpacing(2)
        status_grid.addWidget(caption("Identity"), 0, 0)
        status_grid.addWidget(self._identity, 0, 1)
        status_grid.addWidget(caption("Magnet field"), 0, 2)
        status_grid.addWidget(self._field, 0, 3)
        status_grid.addWidget(caption("Lead/output"), 1, 0)
        status_grid.addWidget(self._output, 1, 1)
        status_grid.addWidget(caption("Heater / mode"), 1, 2)
        status_grid.addWidget(self._heater, 1, 3)
        status_grid.addWidget(caption("Sweep"), 2, 0)
        status_grid.addWidget(self._sweep, 2, 1)
        status_grid.addWidget(caption("Faults"), 2, 2)
        status_grid.addWidget(self._fault, 2, 3)
        status_grid.setColumnStretch(1, 1)
        status_grid.setColumnStretch(3, 1)
        conn_form.addRow(status_widget)
        self._advanced_toggle = QToolButton()
        self._advanced_toggle.setCheckable(True)
        self._advanced_toggle.setText("Magnet mode transition (advanced)")
        self._advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._advanced_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._advanced_toggle.setToolTip(
            "Expand or collapse the driven/persistent magnet mode "
            "transition controls."
        )
        self._transition_widget = QWidget()
        transition_form = QFormLayout(self._transition_widget)
        transition_form.setContentsMargins(8, 6, 8, 6)
        transition_form.setVerticalSpacing(3)
        transition_form.setHorizontalSpacing(8)
        self._driven_btn = QPushButton("Enter Driven Mode")
        self._driven_btn.setToolTip(
            "Match the lead current to the magnet current, switch the persistent "
            "heater ON, and wait for warm-up. Required before sweeping the field."
        )
        self._persistent_btn = QPushButton("Enter Persistent Mode")
        self._persistent_btn.setToolTip(
            "Pause, switch the heater OFF, and wait for cooling so current is "
            "trapped in the superconducting coil."
        )
        self._zero_leads = QCheckBox("Zero leads after cooling (persistent)")
        self._zero_leads.setToolTip(
            "Only used when entering persistent mode: after the heater cools "
            "and the field is trapped in the magnet, ramp the power-supply "
            "leads to 0 A. Leave unchecked to keep the matched lead current "
            "for a quick return to driven mode."
        )
        self._zero_leads.setChecked(True)
        self._transition_status = QLabel("Idle")
        self._transition_status.setToolTip(
            "Live progress of the heater warm-up / cool-down transition."
        )
        transition_form.addRow(self._driven_btn, self._persistent_btn)
        transition_form.addRow(self._zero_leads)
        transition_form.addRow("Transition", self._transition_status)
        self._transition_widget.setVisible(False)
        conn_form.addRow(self._advanced_toggle)
        conn_form.addRow(self._transition_widget)
        controls_layout.addWidget(connection)

        magnet_group = QGroupBox("Continuous Field Sweep")
        magnet_group.setToolTip(
            "Field range for the continuous two-angle MCD measurement. The "
            "APS100 sweeps between these fields while spectra are acquired."
        )
        magnet_form = QFormLayout(magnet_group)
        magnet_form.setContentsMargins(8, 6, 8, 6)
        magnet_form.setVerticalSpacing(3)
        magnet_form.setHorizontalSpacing(8)
        self._start_t = self._double_spin(-cfg.magnet.mcd_max_field_t, cfg.magnet.mcd_max_field_t, cfg.mcd.start_field_t, 4, " T")
        self._start_t.setToolTip(
            "Field where the MCD run begins. The app moves here first and settles."
        )
        self._stop_t = self._double_spin(-cfg.magnet.mcd_max_field_t, cfg.magnet.mcd_max_field_t, cfg.mcd.stop_field_t, 4, " T")
        self._stop_t.setToolTip("End target of the continuous sweep.")
        self._start_settle = self._double_spin(0.0, 120.0, 3.0, 1, " s")
        self._start_settle.setToolTip(
            "Extra wait after reaching the start field before the first spectrum."
        )
        magnet_form.addRow("Start field", self._start_t)
        magnet_form.addRow("Stop field", self._stop_t)
        sweep_rate_label = QLabel("APS100 stored SLOW rate profile")
        sweep_rate_label.setWordWrap(True)
        sweep_rate_label.setToolTip(
            "The sweep rate is fixed to the SLOW rate profile stored in the "
            "APS100 and is not settable here."
        )
        magnet_form.addRow("Sweep rate", sweep_rate_label)
        magnet_form.addRow("Start settle", self._start_settle)
        self._sweep_mode = QComboBox()
        self._sweep_mode.addItem("One-way", "one_way")
        self._sweep_mode.addItem("Round trip", "round_trip")
        self._sweep_mode.setCurrentIndex(
            1 if str(cfg.mcd.sweep_mode).strip().lower() == "round_trip" else 0
        )
        self._sweep_mode.setToolTip(
            "One-way: single sweep from the start field to the stop field. "
            "Round trip: sweep start -> stop, then reverse back to start once."
        )
        magnet_form.addRow("Sweep mode", self._sweep_mode)

        optics = QGroupBox("Rotation and LightField")
        optics.setToolTip(
            "Sample rotation and spectrometer settings used at every "
            "measurement point."
        )
        optics_form = QFormLayout(optics)
        optics_form.setContentsMargins(8, 6, 8, 6)
        optics_form.setVerticalSpacing(3)
        optics_form.setHorizontalSpacing(8)
        self._rotator = QComboBox()
        self._rotator.setToolTip(
            "Which rotation motor (rot1/rot2) rotates the sample."
        )
        self._rotator.addItems(["rot1", "rot2"])
        self._rotator.setCurrentText(cfg.mcd.rotator)
        self._angle_a = self._double_spin(-360.0, 360.0, cfg.mcd.angle_a_deg, 3, "°")
        self._angle_a.setToolTip(
            "Sample angle for spectrum A, taken once per sweep cycle."
        )
        self._angle_b = self._double_spin(-360.0, 360.0, cfg.mcd.angle_b_deg, 3, "°")
        self._angle_b.setToolTip(
            "Sample angle for spectrum B, taken once per sweep cycle."
        )
        self._rotation_settle = self._double_spin(0.0, 30.0, cfg.mcd.rotation_settle_s, 2, " s")
        self._rotation_settle.setToolTip(
            "Wait after the rotator reaches an angle before acquiring."
        )
        self._exposure = self._double_spin(1.0, 600_000.0, cfg.lf6.exposure_ms, 1, " ms")
        self._exposure.setToolTip(
            "Spectrometer exposure time in ms per spectrum."
        )
        self._center = self._double_spin(1.0, 3000.0, cfg.lf6.center_nm, 2, " nm")
        self._center.setToolTip("Spectrometer center wavelength in nm.")
        self._frames = QSpinBox()
        self._frames.setToolTip(
            "LightField frames/accumulations averaged into each spectrum."
        )
        self._frames.setRange(1, 1000)
        self._frames.setValue(cfg.lf6.accumulations)
        optics_form.addRow("Rotator", self._rotator)
        optics_form.addRow("Angle A", self._angle_a)
        optics_form.addRow("Angle B", self._angle_b)
        optics_form.addRow("Rotation settle", self._rotation_settle)
        optics_form.addRow("Exposure", self._exposure)
        optics_form.addRow("Center", self._center)
        optics_form.addRow("Frames/accums", self._frames)

        voltages = QGroupBox("Sample Voltages")
        voltages.setToolTip(
            "Gate and bias voltages applied through the SMUs before the MCD "
            "measurement starts. Ramping follows the Dual Gate tab settings "
            "(step, delay, settle)."
        )
        voltage_form = QFormLayout(voltages)
        voltage_form.setContentsMargins(8, 6, 8, 6)
        voltage_form.setVerticalSpacing(3)
        voltage_form.setHorizontalSpacing(8)
        self._apply_voltages = QCheckBox("Ramp voltages before MCD")
        self._apply_voltages.setToolTip(
            "Apply Vtg/Vbg/Vbias through the SMUs before each condition sweep. "
            "Ramping follows the Dual Gate tab settings (step/delay/settle). "
            "Requires connected SMUs."
        )
        self._apply_voltages.setChecked(cfg.mcd.apply_sample_voltages)
        self._gate_ratio = self._double_spin(
            -1000.0, 1000.0, cfg.mcd.gate_ratio, 4, ""
        )
        self._gate_ratio.setToolTip(
            "Ratio defining the derived coordinates: "
            "Doping = Vtg + r*Vbg and E-field = Vtg - r*Vbg. "
            "Must be non-zero to enter Doping/E-field directly; "
            "otherwise they are computed from Vtg/Vbg."
        )
        self._condition_table = QTableWidget(1, 6)
        self._condition_table.setHorizontalHeaderLabels(
            ["Use", "Vtg", "Vbg", "Vbias", "Doping", "E-field"]
        )
        self._condition_table.setMinimumWidth(0)
        self._condition_table.verticalHeader().setVisible(False)
        self._condition_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._condition_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._condition_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._condition_table.setFixedHeight(150)
        self._condition_table.setToolTip(
            "One row per voltage condition; the MCD sweep runs once per enabled row. "
            "Edit Vtg/Vbg to update Doping/E-field, or edit Doping/E-field to "
            "back-compute Vtg/Vbg (requires non-zero r). Vbias is skipped "
            "automatically when no Vbias channel is connected."
        )
        self._add_condition_btn = QPushButton("Add row")
        self._add_condition_btn.setToolTip(
            "Append a new voltage condition row."
        )
        self._remove_condition_btn = QPushButton("Remove row")
        self._remove_condition_btn.setToolTip(
            "Remove the selected voltage condition row."
        )
        condition_buttons = QHBoxLayout()
        condition_buttons.setSpacing(6)
        condition_buttons.addWidget(self._add_condition_btn)
        condition_buttons.addWidget(self._remove_condition_btn)
        voltage_form.addRow(self._apply_voltages)
        voltage_form.addRow("Gate ratio r", self._gate_ratio)
        voltage_form.addRow(self._condition_table)
        voltage_form.addRow(condition_buttons)
        self._seed_condition_table(cfg.mcd.conditions or [])
        self._update_condition_editable()

        self._row_a = QHBoxLayout()
        self._row_a.setContentsMargins(0, 0, 0, 0)
        self._row_a.setSpacing(6)
        self._row_a.addWidget(magnet_group, 1)
        self._row_a.addWidget(optics, 1)
        row_a_widget = QWidget()
        row_a_widget.setLayout(self._row_a)
        controls_layout.addWidget(row_a_widget)

        controls_layout.addWidget(voltages)

        output = QGroupBox("File Naming and Run")
        output.setToolTip(
            "Filenames, output folder, and the controls that arm and start "
            "the continuous MCD measurement."
        )
        output_form = QFormLayout(output)
        output_form.setContentsMargins(8, 6, 8, 6)
        output_form.setVerticalSpacing(3)
        output_form.setHorizontalSpacing(8)
        self._sample_id = QLineEdit(cfg.mcd.sample_id)
        self._sample_id.setToolTip(
            "Sample ID (required) - embedded in every filename and folder path."
        )
        self._sample_id.setPlaceholderText("Sample ID (required)")
        self._point = QLineEdit(cfg.mcd.point)
        self._point.setToolTip(
            "Measurement location (e.g. p1, center, edge) - embedded in filenames."
        )
        self._point.setPlaceholderText("p1, center, edge")
        self._condition = QLineEdit(cfg.mcd.condition_label)
        self._condition.setToolTip(
            "Optional experiment condition label included in filenames."
        )
        self._condition.setPlaceholderText("Optional condition")
        self._temperature = QLineEdit(cfg.mcd.temperature)
        self._temperature.setToolTip(
            "Temperature token used in filenames, e.g. 6 or 1.8."
        )
        self._mode = QComboBox()
        self._mode.setToolTip(
            "Measurement mode token (Ref or PL) used in filenames."
        )
        self._mode.addItems(["Ref", "PL"])
        self._mode.setCurrentText(cfg.mcd.measurement_mode or "Ref")
        self._laser = QLineEdit(cfg.mcd.laser_nm)
        self._laser.setToolTip(
            "Excitation laser wavelength in nm - recorded in filenames."
        )
        self._laser.setPlaceholderText("Laser nm")
        self._power = QLineEdit(cfg.mcd.power_uw)
        self._power.setToolTip(
            "Nominal excitation power in uW - recorded in filenames."
        )
        self._power.setPlaceholderText("Power uW")
        self._power_coefficient = self._double_spin(
            -1_000_000.0, 1_000_000.0, cfg.mcd.power_coefficient, 6, ""
        )
        self._power_coefficient.setMinimumWidth(140)
        self._power_coefficient.setToolTip(
            "Multiplier applied to the nominal power token in filenames."
        )
        sample_row = QHBoxLayout()
        sample_row.setSpacing(6)
        sample_row.addWidget(self._sample_id, 1)
        sample_row.addWidget(self._point, 1)
        sample_row.addWidget(self._condition, 1)
        output_form.addRow("Sample/point/cond", sample_row)

        naming_row = QHBoxLayout()
        naming_row.setSpacing(6)
        for widget in (
            self._temperature,
            self._mode,
            self._laser,
            self._power,
            self._power_coefficient,
        ):
            naming_row.addWidget(widget, 1)
        output_form.addRow("Run tokens", naming_row)

        filename_part_tips = {
            "temp_mode": "Include temperature and mode (Ref/PL) tokens in filenames.",
            "laser_power": "Include laser wavelength and power tokens in filenames.",
            "center": "Include the center wavelength token in filenames.",
            "exposure": "Include the exposure time token in filenames.",
            "condition": "Include the condition label token in filenames.",
        }
        self._filename_part_checks = {}
        part_widgets = []
        for key, label in (
            ("temp_mode", "Temp+mode"),
            ("laser_power", "Laser+power"),
            ("center", "Center"),
            ("exposure", "Exposure"),
            ("condition", "Condition"),
        ):
            checkbox = QCheckBox(label)
            checkbox.setToolTip(filename_part_tips[key])
            checkbox.setChecked(key in (cfg.mcd.filename_parts or []))
            self._filename_part_checks[key] = checkbox
            part_widgets.append(checkbox)
        parts_widget = QWidget()
        parts_grid = QGridLayout(parts_widget)
        parts_grid.setContentsMargins(0, 0, 0, 0)
        parts_grid.setHorizontalSpacing(6)
        parts_grid.setVerticalSpacing(2)
        for index, checkbox in enumerate(part_widgets):
            row, column = divmod(index, 2)
            parts_grid.addWidget(checkbox, row, column)
            parts_grid.setColumnStretch(column, 1)
        output_form.addRow(parts_widget)

        self._filename_preview = QLabel()
        self._filename_preview.setToolTip(
            "Live preview of the generated CSV filename."
        )
        self._filename_preview.setWordWrap(True)
        self._filename_preview.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._path_preview = QLabel()
        self._path_preview.setToolTip(
            "Live preview of the full output folder path."
        )
        self._path_preview.setWordWrap(True)
        self._path_preview.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._run_btn = QPushButton("Start Continuous MCD")
        self._run_btn.setToolTip(
            "Run preflight checks, then start the continuous two-angle sweep "
            "with the settings above."
        )
        self._stop_btn = QPushButton("Stop MCD")
        self._stop_btn.setToolTip(
            "Request a clean stop and pause the APS100."
        )
        self._stop_btn.setEnabled(False)
        self._progress = QProgressBar()
        self._progress.setToolTip(
            "Sweep progress from start field to stop field."
        )
        self._progress.setRange(0, 1000)
        self._run_status = QLabel("Ready")
        self._run_status.setToolTip(
            "Current run status: moves, spectra acquired, or errors."
        )
        self._mode_notice = QLabel("Connect the APS100 — MCD requires driven mode.")
        self._mode_notice.setWordWrap(True)
        self._mode_notice.setStyleSheet("color:#8b96a5;")
        self._mode_notice.setToolTip(
            "MCD sweep requires driven mode (persistent heater ON). "
            "Use the advanced transition controls if the heater is OFF."
        )
        output_form.addRow(self._mode_notice)
        output_form.addRow(self._filename_preview)
        output_form.addRow(self._path_preview)
        output_form.addRow(self._run_btn)
        output_form.addRow(self._stop_btn)
        output_form.addRow(self._progress)
        output_form.addRow("Status", self._run_status)
        controls_layout.addWidget(output)
        controls_layout.addStretch()

        display = QWidget()
        display_layout = QVBoxLayout(display)
        display_layout.setContentsMargins(8, 6, 8, 6)
        display_layout.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("Live spectrum")
        title.setStyleSheet("font-weight:700;")
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        self._show_spectrum_chk = QCheckBox("Show spectrum")
        self._show_spectrum_chk.setChecked(True)
        self._show_spectrum_chk.setToolTip(
            "Hide or show the live spectrum. Hide it to give the log more room."
        )
        self._clear_log_btn = QPushButton("Clear log")
        self._clear_log_btn.setToolTip("Clear the run/event log text.")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._show_spectrum_chk)
        header.addWidget(self._clear_log_btn)
        display_layout.addLayout(header)
        self._plot = pg.PlotWidget()
        self._plot.setToolTip(
            "Real-time spectra: A (blue) and B (orange), updated as each is acquired."
        )
        self._plot.setMinimumHeight(180)
        self._plot.setLabel("bottom", "Wavelength", units="nm")
        self._plot.setLabel("left", "Intensity", units="counts")
        self._curve_a = self._plot.plot(pen=pg.mkPen("#2374c6", width=1.5), name="A")
        self._curve_a.setToolTip("Angle A spectrum (blue).")
        self._curve_b = self._plot.plot(pen=pg.mkPen("#c06020", width=1.5), name="B")
        self._curve_b.setToolTip("Angle B spectrum (orange).")
        self._plot.addLegend()
        self._log = QPlainTextEdit()
        self._log.setToolTip(
            "Chronological run/event/error log. Also saved alongside the CSV."
        )
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._plot_log_splitter = QSplitter(Qt.Orientation.Vertical)
        self._plot_log_splitter.addWidget(self._plot)
        self._plot_log_splitter.addWidget(self._log)
        self._plot_log_splitter.setStretchFactor(0, 1)
        self._plot_log_splitter.setStretchFactor(1, 1)
        self._plot_log_splitter.setSizes([260, 220])
        display_layout.addWidget(self._plot_log_splitter, stretch=1)
        self._splitter.addWidget(display)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([760, 440])
        self._update_coordinates_and_filename()
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        stacked = self._scroll.viewport().width() < 700
        self._row_a.setDirection(
            QBoxLayout.Direction.TopToBottom
            if stacked
            else QBoxLayout.Direction.LeftToRight
        )
        self._row_a.setStretch(0, 0 if stacked else 1)
        self._row_a.setStretch(1, 0 if stacked else 1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    @staticmethod
    def _double_spin(low, high, value, decimals, suffix):
        spin = QDoubleSpinBox()
        spin.setRange(float(low), float(high))
        spin.setDecimals(int(decimals))
        spin.setValue(float(value))
        spin.setSuffix(suffix)
        spin.setKeyboardTracking(False)
        return spin

    def _wire_signals(self) -> None:
        self._connect_btn.clicked.connect(self._connect_magnet)
        self._disconnect_btn.clicked.connect(self._magnet.disconnect_instrument)
        self._remote_btn.clicked.connect(self._magnet.take_remote)
        self._refresh_btn.clicked.connect(self._magnet.refresh_snapshot)
        self._pause_btn.clicked.connect(self._pause_magnet)
        self._driven_btn.clicked.connect(self._enter_driven)
        self._persistent_btn.clicked.connect(self._enter_persistent)
        self._run_btn.clicked.connect(self._start_run)
        self._stop_btn.clicked.connect(self._stop_run)
        self._show_spectrum_chk.toggled.connect(self._set_spectrum_visible)
        self._clear_log_btn.clicked.connect(self._log.clear)
        self._sweep_mode.currentIndexChanged.connect(
            self._update_coordinates_and_filename
        )
        self._condition_table.itemChanged.connect(self._on_condition_item_changed)
        self._add_condition_btn.clicked.connect(self._add_condition_row)
        self._remove_condition_btn.clicked.connect(self._remove_condition_row)
        self._gate_ratio.valueChanged.connect(self._on_gate_ratio_changed)
        self._advanced_toggle.toggled.connect(self._on_advanced_toggled)
        self._magnet.connected.connect(self._on_magnet_connected)
        self._magnet.disconnected.connect(self._on_magnet_disconnected)
        self._magnet.snapshot_updated.connect(self._on_snapshot)
        self._magnet.transition_progress.connect(self._on_transition)
        self._magnet.operation_finished.connect(self._on_magnet_operation)
        self._magnet.error.connect(self._on_magnet_error)
        self._magnet.fault.connect(self._on_magnet_error)
        for edit in (
            self._sample_id,
            self._point,
            self._condition,
            self._temperature,
            self._laser,
            self._power,
        ):
            edit.textChanged.connect(self._update_coordinates_and_filename)
        for spin in (
            self._start_t,
            self._stop_t,
            self._angle_a,
            self._angle_b,
            self._exposure,
            self._center,
            self._frames,
            self._power_coefficient,
        ):
            spin.valueChanged.connect(self._update_coordinates_and_filename)
        for combo in (self._rotator, self._mode):
            combo.currentIndexChanged.connect(self._update_coordinates_and_filename)
        for checkbox in self._filename_part_checks.values():
            checkbox.toggled.connect(self._update_coordinates_and_filename)

    @Slot()
    def _connect_magnet(self) -> None:
        cfg.magnet.visa_resource = self._resource.text().strip() or "ASRL5::INSTR"
        try:
            cfg.save()
        except Exception:
            pass
        self._identity.setText("Connecting…")
        self._magnet.connect_instrument(
            cfg.magnet.visa_resource,
            use_mock=self._mock.isChecked(),
        )

    @Slot(object)
    def _on_magnet_connected(self, identity) -> None:
        self._identity.setText(identity.display_name)
        self._append_log(f"Connected: {identity.display_name}")

    @Slot()
    def _on_magnet_disconnected(self) -> None:
        self._identity.setText("Disconnected")
        self._field.setText("—")
        self._output.setText("—")
        self._heater.setText("—")
        self._sweep.setText("—")
        self._fault.setText("—")
        self._mode_notice.setText(
            "Connect the APS100 — MCD requires driven mode."
        )
        self._mode_notice.setStyleSheet("color:#8b96a5;")

    @Slot(object)
    def _on_snapshot(self, snapshot) -> None:
        self._last_snapshot = snapshot
        self._field.setText(f"{snapshot.field_t:+.6f} T")
        self._output.setText(
            f"{snapshot.output_field_t:+.6f} T ({snapshot.output_current_a:+.5f} A)"
        )
        self._heater.setText("ON — driven" if snapshot.heater_on else "OFF — persistent")
        self._sweep.setText(snapshot.sweep_state)
        if snapshot.heater_on:
            self._mode_notice.setText(
                "MCD ready — magnet in driven mode (heater ON)."
            )
            self._mode_notice.setStyleSheet("color:#1d7a3e;")
        else:
            self._mode_notice.setText(
                "MCD sweep requires driven mode — persistent heater is OFF. "
                "Use \"Enter Driven Mode\" first."
            )
            self._mode_notice.setStyleSheet("color:#a82020; font-weight:600;")
        faults = []
        if snapshot.status.quench:
            faults.append("QUENCH")
        if snapshot.status.power_module_failure:
            faults.append("MODULE FAILURE")
        if snapshot.status.menu_locked:
            faults.append("MENU LOCK")
        self._fault.setText(", ".join(faults) if faults else "None")

    @Slot(str, float)
    def _on_transition(self, label: str, remaining: float) -> None:
        self._transition_status.setText(f"{label}: {remaining:.1f} s remaining")

    @Slot(str)
    def _on_magnet_operation(self, operation: str) -> None:
        self._transition_status.setText(f"Completed: {operation}")
        self._append_log(f"APS100 operation completed: {operation}")

    @Slot(str)
    def _on_magnet_error(self, message: str) -> None:
        self._append_log(f"ERROR: {message}")
        self._run_status.setText("Magnet error")

    @Slot(bool)
    def _set_spectrum_visible(self, visible: bool) -> None:
        self._plot.setVisible(bool(visible))

    @Slot(float)
    def _on_gate_ratio_changed(self, _value: float) -> None:
        self._update_condition_editable()
        self._update_coordinates_and_filename()

    @Slot(bool)
    def _on_advanced_toggled(self, checked: bool) -> None:
        self._transition_widget.setVisible(checked)
        self._advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )

    def _condition_rows(self) -> list[dict]:
        rows = []
        for row in range(self._condition_table.rowCount()):
            check = self._condition_table.item(row, 0)
            enabled = bool(
                check is not None
                and check.checkState() == Qt.CheckState.Checked
            )
            rows.append(
                {
                    "enabled": enabled,
                    "vtg_v": self._row_value(row, 1),
                    "vbg_v": self._row_value(row, 2),
                    "vbias_v": self._row_value(row, 3),
                    "doping_v": self._row_value(row, 4),
                    "efield_v": self._row_value(row, 5),
                }
            )
        return rows

    def _row_value(self, row: int, column: int) -> float:
        item = self._condition_table.item(row, column)
        if item is None:
            return 0.0
        try:
            return float(item.text())
        except (TypeError, ValueError):
            return 0.0

    def _set_row_value(self, row: int, column: int, value: float) -> None:
        item = self._condition_table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self._condition_table.setItem(row, column, item)
        item.setText(f"{value:.6g}")

    def _seed_condition_table(self, conditions: list) -> None:
        self._updating_table = True
        try:
            self._condition_table.setRowCount(max(1, len(conditions)))
            for row, condition in enumerate(conditions):
                check = QTableWidgetItem()
                check.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                check.setCheckState(
                    Qt.CheckState.Checked
                    if bool(condition.get("enabled", True))
                    else Qt.CheckState.Unchecked
                )
                self._condition_table.setItem(row, 0, check)
                for column, key in (
                    (1, "vtg_v"),
                    (2, "vbg_v"),
                    (3, "vbias_v"),
                    (4, "doping_v"),
                    (5, "efield_v"),
                ):
                    self._set_row_value(row, column, float(condition.get(key, 0.0)))
            for row in range(self._condition_table.rowCount()):
                if self._condition_table.item(row, 0) is None:
                    check = QTableWidgetItem()
                    check.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    check.setCheckState(Qt.CheckState.Checked)
                    self._condition_table.setItem(row, 0, check)
                for column in range(1, 6):
                    if self._condition_table.item(row, column) is None:
                        self._set_row_value(row, column, 0.0)
        finally:
            self._updating_table = False

    def _update_condition_editable(self) -> None:
        editable = abs(self._gate_ratio.value()) > 1e-9
        self._updating_table = True
        try:
            for row in range(self._condition_table.rowCount()):
                doping, efield = _mcd_coordinates(
                    self._row_value(row, 1),
                    self._row_value(row, 2),
                    self._gate_ratio.value(),
                )
                self._set_row_value(row, 4, doping)
                self._set_row_value(row, 5, efield)
                for column in (4, 5):
                    item = self._condition_table.item(row, column)
                    if item is None:
                        continue
                    flags = item.flags()
                    if editable:
                        item.setFlags(flags | Qt.ItemFlag.ItemIsEditable)
                    else:
                        item.setFlags(flags & ~Qt.ItemFlag.ItemIsEditable)
        finally:
            self._updating_table = False

    @Slot(object)
    def _on_condition_item_changed(self, item) -> None:
        if self._updating_table or item is None:
            return
        row = item.row()
        column = item.column()
        if column in (1, 2):
            doping, efield = _mcd_coordinates(
                self._row_value(row, 1),
                self._row_value(row, 2),
                self._gate_ratio.value(),
            )
            self._updating_table = True
            try:
                self._set_row_value(row, 4, doping)
                self._set_row_value(row, 5, efield)
            finally:
                self._updating_table = False
        elif column in (4, 5) and abs(self._gate_ratio.value()) > 1e-9:
            try:
                vtg, vbg = _vtg_vbg_from_doping_efield(
                    self._row_value(row, 4),
                    self._row_value(row, 5),
                    self._gate_ratio.value(),
                )
            except ValueError:
                return
            self._updating_table = True
            try:
                self._set_row_value(row, 1, vtg)
                self._set_row_value(row, 2, vbg)
            finally:
                self._updating_table = False
        self._update_coordinates_and_filename()

    def _add_condition_row(self) -> None:
        self._updating_table = True
        try:
            row = self._condition_table.rowCount()
            self._condition_table.insertRow(row)
            source = self._condition_rows()[-1] if row > 0 else {}
            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            check.setCheckState(Qt.CheckState.Checked)
            self._condition_table.setItem(row, 0, check)
            self._set_row_value(row, 1, float(source.get("vtg_v", 0.0)))
            self._set_row_value(row, 2, float(source.get("vbg_v", 0.0)))
            self._set_row_value(row, 3, float(source.get("vbias_v", 0.0)))
            doping, efield = _mcd_coordinates(
                float(source.get("vtg_v", 0.0)),
                float(source.get("vbg_v", 0.0)),
                self._gate_ratio.value(),
            )
            self._set_row_value(row, 4, doping)
            self._set_row_value(row, 5, efield)
            self._condition_table.selectRow(row)
        finally:
            self._updating_table = False
        self._update_coordinates_and_filename()

    def _remove_condition_row(self) -> None:
        if self._condition_table.rowCount() <= 1:
            return
        row = self._condition_table.currentRow()
        if row < 0:
            row = self._condition_table.rowCount() - 1
        self._condition_table.removeRow(row)
        self._update_coordinates_and_filename()

    @Slot()
    def _pause_magnet(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
        self._magnet.pause()
        self._append_log("Pause requested.")

    @Slot()
    def _enter_driven(self) -> None:
        answer = QMessageBox.question(
            self,
            "Enter driven mode",
            "The APS100 will match the lead current, turn the persistent heater ON, "
            "and hold the matched current for the configured warm-up time. Continue?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._magnet.take_remote()
            self._magnet.enter_driven_mode()

    @Slot()
    def _enter_persistent(self) -> None:
        answer = QMessageBox.question(
            self,
            "Enter persistent mode",
            "The APS100 will pause, turn the heater OFF, hold current for the "
            "configured cooling time, then optionally zero the leads. Continue?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._magnet.take_remote()
            self._magnet.enter_persistent_mode(
                zero_leads=self._zero_leads.isChecked()
            )

    @Slot()
    def _update_coordinates_and_filename(self, *_args) -> None:
        try:
            params = self._collect_params(require_sample=False)
            preview_params = dict(params)
            preview_params["sample_id"] = params["sample_id"] or "SampleID"
            stem = build_mcd_filename_base(preview_params)
            folder = (
                Path(params["base_output_dir"])
                / _safe_name(preview_params["sample_id"])
                / _safe_name(params["subfolder"])
            )
            conditions = params.get("conditions") or []
            enabled = [c for c in conditions if c.get("enabled", True)]
            suffix = (
                f"  ·  {len(enabled)} conditions"
                if len(enabled) > 1
                else ""
            )
            self._filename_preview.setText(f"{stem}.csv{suffix}")
            self._path_preview.setText(str(folder))
        except Exception as exc:
            self._filename_preview.setText(f"Invalid filename settings: {exc}")
            self._path_preview.setText("—")

    def _collect_params(self, *, require_sample: bool = True) -> dict:
        start = self._start_t.value()
        stop = self._stop_t.value()
        if math.isclose(start, stop, abs_tol=1e-12):
            raise ValueError("Start and stop fields must be different")
        if abs(start) > cfg.magnet.mcd_max_field_t or abs(stop) > cfg.magnet.mcd_max_field_t:
            raise ValueError(
                f"MCD fields are limited to ±{cfg.magnet.mcd_max_field_t:g} T"
            )
        sample_id = self._sample_id.text().strip()
        if require_sample and not sample_id:
            raise ValueError("Sample ID is required")
        base_output = str(cfg.base_out)
        subfolder = cfg.mcd.subfolder or "MCD Data"
        conditions = self._condition_rows()
        enabled = [c for c in conditions if c["enabled"]]
        if not enabled:
            raise ValueError("At least one enabled voltage condition is required")
        limit = self._voltage_limit
        for condition in enabled:
            for key in ("vtg_v", "vbg_v", "vbias_v"):
                if abs(condition[key]) > limit + 1e-9:
                    raise ValueError(
                        f"{key} is outside the ±{limit:g} V compliance limit"
                    )
        first = enabled[0]
        params = {
            "start_t": start,
            "stop_t": stop,
            "field_tolerance_t": cfg.magnet.field_tolerance_t,
            "move_timeout_s": 1800.0,
            "start_settle_s": self._start_settle.value(),
            "rotator": self._rotator.currentText(),
            "angle_a_deg": self._angle_a.value(),
            "angle_b_deg": self._angle_b.value(),
            "rotation_settle_s": self._rotation_settle.value(),
            "exposure_ms": self._exposure.value(),
            "center_nm": self._center.value(),
            "frames": self._frames.value(),
            "sweep_mode": self._sweep_mode.currentData() or "one_way",
            "apply_voltages": self._apply_voltages.isChecked(),
            "conditions": conditions,
            "vbg_v": first["vbg_v"],
            "vtg_v": first["vtg_v"],
            "vbias_v": first["vbias_v"],
            "gate_ratio": self._gate_ratio.value(),
            "voltage_ramp_step_v": cfg.ramp.step_V,
            "voltage_vbias_step_v": cfg.ramp.vbias_step_V,
            "voltage_step_delay_s": cfg.ramp.delay_s,
            "voltage_settle_s": cfg.ramp.settle_s,
            "sample_id": sample_id,
            "point": self._point.text().strip(),
            "condition_label": self._condition.text().strip(),
            "temperature": self._temperature.text().strip(),
            "measurement_mode": self._mode.currentText(),
            "laser_nm": self._laser.text().strip(),
            "power_uw": self._power.text().strip(),
            "power_coefficient": self._power_coefficient.value(),
            "decimal_style": "dot",
            "filename_parts": [
                key
                for key, checkbox in self._filename_part_checks.items()
                if checkbox.isChecked()
            ],
            "base_output_dir": base_output,
            "subfolder": subfolder,
        }
        build_mcd_filename_base(
            {**params, "sample_id": sample_id or "SampleID"}
        )
        return params

    @Slot()
    def _start_run(self) -> None:
        if self._worker is not None:
            return
        try:
            params = self._collect_params()
        except Exception as exc:
            QMessageBox.warning(self, "Invalid MCD settings", str(exc))
            return
        if not self._magnet.is_connected:
            QMessageBox.warning(self, "APS100", "Connect the APS100 first.")
            return
        if self._last_snapshot is None or not self._last_snapshot.heater_on:
            QMessageBox.warning(self, "APS100", "The magnet must be in driven mode.")
            return
        if not self._magnet.acquire_exclusive("mcd"):
            QMessageBox.warning(
                self,
                "APS100 busy",
                f"The magnet is already owned by {self._magnet.exclusive_owner or 'another operation'}.",
            )
            return
        snapshot = self._last_snapshot
        mode_line = (
            "Magnet mode: DRIVEN (heater ON) — required for MCD sweep.\n"
            if snapshot is not None and snapshot.heater_on
            else "Magnet mode: PERSISTENT (heater OFF) — MCD sweep requires "
            "driven mode.\n"
        )
        if params["sweep_mode"] == "round_trip":
            sweep_line = (
                f"Round-trip sweep: {params['start_t']:+.3f} T → "
                f"{params['stop_t']:+.3f} T → {params['start_t']:+.3f} T\n"
            )
        else:
            sweep_line = (
                f"One-way sweep: {params['start_t']:+.3f} T → "
                f"{params['stop_t']:+.3f} T\n"
            )
        conditions = params.get("conditions") or []
        enabled = [c for c in conditions if c.get("enabled", True)]
        first = enabled[0] if enabled else {}
        cond_line = (
            f"{len(enabled)} condition(s); first: "
            f"Vtg={first.get('vtg_v', 0.0):+.4g} V, "
            f"Vbg={first.get('vbg_v', 0.0):+.4g} V, "
            f"Vbias={first.get('vbias_v', 0.0):+.4g} V\n"
        )
        summary = (
            sweep_line
            + cond_line
            + mode_line
            + "Rate: APS100 stored SLOW rate profile\n"
            f"{params['rotator'].upper()}: {params['angle_a_deg']:.3f}° / "
            f"{params['angle_b_deg']:.3f}°\n"
            f"Output: {build_mcd_filename_base(params)}.csv\n"
            "The heater will remain ON. The APS100 will be paused on Stop or completion."
        )
        if params.get("apply_voltages"):
            try:
                has_bias = bool(self._smu.has_vbias())
            except (AttributeError, TypeError):
                has_bias = True
            if not has_bias:
                summary += "\nVbias not connected — bias rows will be skipped."
        if QMessageBox.question(self, "Arm continuous MCD", summary) != QMessageBox.StandardButton.Yes:
            self._magnet.release_exclusive("mcd")
            return

        self._save_config_from_ui()
        self._worker = _ContinuousMCDWorker(
            params, self._magnet, self._lf6, self._rotation, self._smu
        )
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._append_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.spectrum.connect(self._on_spectrum)
        self._worker.error.connect(self._on_run_error)
        self._worker.finished.connect(self._on_run_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._on_run_thread_finished)
        self._thread.finished.connect(self._thread.deleteLater)
        self._set_running(True)
        self._log.clear()
        self._thread.start()

    @Slot()
    def _stop_run(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
        self._magnet.pause()
        self._stop_btn.setEnabled(False)
        self._run_status.setText("Stopping and pausing APS100…")

    @Slot(float, float, int, int)
    def _on_progress(
        self,
        field_t: float,
        percent: float,
        condition_index: int,
        condition_count: int,
    ) -> None:
        self._progress.setValue(round(percent * 10.0))
        if condition_count > 1:
            self._run_status.setText(
                f"Cond {condition_index}/{condition_count} — "
                f"{field_t:+.6f} T — {percent:.1f}%"
            )
        else:
            self._run_status.setText(f"{field_t:+.6f} T — {percent:.1f}%")

    @Slot(object, object, str, float)
    def _on_spectrum(self, wl, counts, label: str, field_t: float) -> None:
        curve = self._curve_a if label == "A" else self._curve_b
        curve.setData(np.asarray(wl), np.asarray(counts))
        self._run_status.setText(f"Angle {label} acquired at {field_t:+.6f} T")

    @Slot(str)
    def _on_run_error(self, message: str) -> None:
        self._append_log(f"ERROR: {message}")
        self._run_status.setText("Error — APS100 pause requested")

    @Slot(object)
    def _on_run_finished(self, result: dict) -> None:
        if result.get("error"):
            self._run_status.setText("Error — check log and APS100")
        elif result.get("stopped"):
            self._run_status.setText("Stopped — APS100 paused")
        elif result.get("csv_path"):
            self._run_status.setText(f"Completed: {result['csv_path']}")
        else:
            self._run_status.setText("Finished")
        self._magnet.release_exclusive("mcd")
        self._magnet.refresh_snapshot()

    @Slot()
    def _on_run_thread_finished(self) -> None:
        self._worker = None
        self._thread = None
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        self._run_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._disconnect_btn.setEnabled(not running)
        self._driven_btn.setEnabled(not running)
        self._persistent_btn.setEnabled(not running)
        self.run_state_changed.emit(bool(running))

    def _append_log(self, message: str) -> None:
        self._log.appendPlainText(str(message))

    def _save_config_from_ui(self) -> None:
        cfg.mcd.start_field_t = self._start_t.value()
        cfg.mcd.stop_field_t = self._stop_t.value()
        cfg.mcd.sample_id = self._sample_id.text().strip()
        cfg.mcd.point = self._point.text().strip()
        cfg.mcd.condition_label = self._condition.text().strip()
        cfg.mcd.temperature = self._temperature.text().strip()
        cfg.mcd.measurement_mode = self._mode.currentText()
        cfg.mcd.laser_nm = self._laser.text().strip()
        cfg.mcd.power_uw = self._power.text().strip()
        cfg.mcd.power_coefficient = self._power_coefficient.value()
        cfg.mcd.decimal_style = "dot"
        cfg.mcd.filename_parts = [
            key
            for key, checkbox in self._filename_part_checks.items()
            if checkbox.isChecked()
        ]
        cfg.mcd.rotator = self._rotator.currentText()
        cfg.mcd.angle_a_deg = self._angle_a.value()
        cfg.mcd.angle_b_deg = self._angle_b.value()
        cfg.mcd.rotation_settle_s = self._rotation_settle.value()
        cfg.mcd.sweep_mode = self._sweep_mode.currentData() or "one_way"
        cfg.mcd.apply_sample_voltages = self._apply_voltages.isChecked()
        conditions = self._condition_rows()
        cfg.mcd.conditions = conditions
        first = next(
            (c for c in conditions if c["enabled"]),
            conditions[0] if conditions else {},
        )
        cfg.mcd.vbg_v = float(first.get("vbg_v", 0.0))
        cfg.mcd.vtg_v = float(first.get("vtg_v", 0.0))
        cfg.mcd.vbias_v = float(first.get("vbias_v", 0.0))
        cfg.mcd.gate_ratio = self._gate_ratio.value()
        cfg.lf6.exposure_ms = self._exposure.value()
        cfg.lf6.center_nm = self._center.value()
        cfg.lf6.accumulations = self._frames.value()
        try:
            cfg.save()
        except Exception as exc:
            self._append_log(f"Configuration save warning: {exc}")

    def capture_session_state(self) -> dict:
        return {
            "start_settle_s": self._start_settle.value(),
            "splitter_sizes": [int(v) for v in self._splitter.sizes()],
            "plot_log_sizes": [int(v) for v in self._plot_log_splitter.sizes()],
            "spectrum_visible": bool(self._show_spectrum_chk.isChecked()),
            "conditions": self._condition_rows(),
        }

    def restore_session_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        if isinstance(state.get("spectrum_visible"), bool):
            self._show_spectrum_chk.setChecked(state["spectrum_visible"])
        if isinstance(state.get("conditions"), list):
            self._seed_condition_table(state["conditions"])
            self._update_condition_editable()
            self._update_coordinates_and_filename()
        try:
            self._start_settle.setValue(float(state.get("start_settle_s", self._start_settle.value())))
        except (TypeError, ValueError):
            pass
        sizes = state.get("splitter_sizes")
        if isinstance(sizes, (list, tuple)) and len(sizes) == 2:
            try:
                self._splitter.setSizes([int(sizes[0]), int(sizes[1])])
            except (TypeError, ValueError):
                pass
        plot_log_sizes = state.get("plot_log_sizes")
        if isinstance(plot_log_sizes, (list, tuple)) and len(plot_log_sizes) == 2:
            try:
                self._plot_log_splitter.setSizes(
                    [int(plot_log_sizes[0]), int(plot_log_sizes[1])]
                )
            except (TypeError, ValueError):
                pass

    def shutdown(self, timeout_ms: int = 30_000) -> bool:
        """Stop an active run and wait until its worker no longer owns hardware."""
        if self._worker is not None:
            self._worker.request_stop()
            self._magnet.pause()
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.quit()
            return bool(thread.wait(max(0, int(timeout_ms))))
        return True
