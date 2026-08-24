"""One-shot real-hardware commissioning: 0.02 T -> 0.03 T, no optics.

All device access is submitted through AttoDRY2100Controller.  The adapter
subclass only durably traces accepted adapter calls made on the owner QThread.
"""
from __future__ import annotations

import concurrent.futures
import math
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from app.devices.attodry2100_adapter import AttoDRY2100Adapter
from app.engine.attodry2100_commissioning import CommissioningEvidenceRecorder
from controllers.attodry2100_controller import AttoDRY2100Controller
from utils.config import cfg


START_T = 0.02
STOP_T = 0.03
GATE_T = max(0.001, abs(STOP_T) * 2e-4)
POLL_S = 0.2
PREPOSITION_TIMEOUT_S = 120.0
LEG_TIMEOUT_S = 180.0
MAX_TEMPERATURE_K = 7.0
MAX_FIELD_T = 6.0
MATERIAL_DIRECTION_T = 0.001
MATERIAL_ENDPOINT_OVERSHOOT_T = 0.001


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def snapshot_details(snapshot):
    status = snapshot.status
    details = status.backend_details if isinstance(status.backend_details, dict) else {}
    return {
        "field_t": snapshot.field_t,
        "setpoint_t": snapshot.setpoint_t,
        "temperature_k": snapshot.temperature_k,
        "driven_mode": status.driven_mode,
        "persistent_mode": status.persistent_mode,
        "h_state": details.get("h_state", status.field_control_state),
        "field_control": details.get("field_control"),
        "quench": status.quench,
        "heater": details.get("heater", status.heater_on),
        "leads_hot": details.get("leads_hot", status.leads_hot),
        "lead_field_t": details.get("lead_field", snapshot.lead_field_t),
    }


def validate_snapshot(snapshot, *, expected_setpoint=None):
    item = snapshot_details(snapshot)
    field = item["field_t"]
    temperature = item["temperature_k"]
    if not isinstance(field, (int, float)) or not math.isfinite(float(field)):
        raise RuntimeError("finite field telemetry is required")
    if abs(float(field)) > MAX_FIELD_T:
        raise RuntimeError(f"field telemetry exceeds +/-{MAX_FIELD_T:g} T")
    if not isinstance(temperature, (int, float)) or not math.isfinite(float(temperature)):
        raise RuntimeError("finite temperature telemetry is required")
    if float(temperature) > MAX_TEMPERATURE_K:
        raise RuntimeError(f"temperature exceeds {MAX_TEMPERATURE_K:g} K")
    if type(item["driven_mode"]) is not bool or item["driven_mode"] is not True:
        raise RuntimeError("driven_mode is not exactly True")
    if type(item["persistent_mode"]) is not bool or item["persistent_mode"] is not False:
        raise RuntimeError("persistent_mode is not exactly False")
    if item["quench"] is not False:
        raise RuntimeError("quench telemetry is not explicitly safe")
    if type(item["heater"]) is not bool:
        raise RuntimeError("persistent-switch heater telemetry is invalid")
    if type(item["leads_hot"]) is not bool:
        raise RuntimeError("leads-hot telemetry is invalid")
    lead_field = item["lead_field_t"]
    if not isinstance(lead_field, (int, float)) or not math.isfinite(float(lead_field)):
        raise RuntimeError("lead-field telemetry is invalid")
    if abs(float(lead_field)) > MAX_FIELD_T:
        raise RuntimeError("lead-field telemetry exceeds the authorized range")
    if item["field_control"] is not True:
        raise RuntimeError("field-control telemetry is not exactly True")
    if expected_setpoint is not None:
        setpoint = item["setpoint_t"]
        if not isinstance(setpoint, (int, float)) or not math.isfinite(float(setpoint)):
            raise RuntimeError("setpoint telemetry is invalid")
        if abs(float(setpoint) - float(expected_setpoint)) > 1e-6:
            raise RuntimeError("setpoint telemetry does not match the commanded endpoint")
    return item


def resolve(app, handle, timeout_s=30.0):
    result = handle.result(timeout_s)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            handle.wait_drained(0.02)
            break
        except concurrent.futures.TimeoutError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{handle.kind} owner drain timed out")
            app.processEvents()
    app.processEvents()
    return result


def main() -> int:
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    evidence_path = Path("commissioning_evidence") / (
        f"attodry2100_continuous_002_to_003_{utc_stamp()}.json"
    )
    recorder = CommissioningEvidenceRecorder(
        evidence_path, run_id="first-real-continuous-no-optics"
    )
    recorder.record(
        "commissioning_started",
        start_t=START_T,
        stop_t=STOP_T,
        gate_t=GATE_T,
        lightfield_acquisition=False,
        reverse_leg=False,
        ramp_rate_changed=False,
    )

    class TracingAdapter(AttoDRY2100Adapter):
        def set_h_setpoint(self, target, stop_event=None):
            recorder.record("magnet_mutating_rpc", rpc="setHSetPoint", channel=self.channel, target_t=target)
            return super().set_h_setpoint(target, stop_event=stop_event)

        def start_field_control(self, expected_target=None, stop_event=None):
            recorder.record("magnet_mutating_rpc", rpc="startFieldControl", channel=self.channel, target_t=expected_target)
            return super().start_field_control(expected_target, stop_event=stop_event)

        def stop_field_control(self, stop_event=None):
            recorder.record("magnet_mutating_rpc", rpc="stopFieldControl", channel=self.channel)
            return super().stop_field_control(stop_event=stop_event)

        def verify_continuous_completion(self, target, gate_t):
            recorder.record("completion_verification", target_t=target, gate_t=gate_t)
            return super().verify_continuous_completion(target, gate_t)

        def close(self):
            recorder.record("transport_close_requested")
            return super().close()

    def factory(settings):
        return TracingAdapter(
            settings.sdk_directory,
            settings.host,
            settings.channel,
            settings.timeout_s,
            maximum_field_t=settings.maximum_field_t,
            minimum_temperature_k=settings.minimum_temperature_k,
            maximum_temperature_k=settings.maximum_temperature_k,
        )

    controller = AttoDRY2100Controller(config=cfg.attodry2100, adapter_factory=factory)
    connected = False
    preflight_passed = False
    mutation_attempted = False
    detached = False
    max_temperature = float("-inf")
    trajectory_started = None

    try:
        identity = resolve(app, controller.connect_async())
        connected = True
        recorder.record("connect_result", identity=identity, owner_thread_running=controller._thread.isRunning())

        initial = resolve(app, controller.read_snapshot_async())
        initial_values = validate_snapshot(initial)
        max_temperature = max(max_temperature, float(initial_values["temperature_k"]))
        if controller.has_pending_work:
            raise RuntimeError("controller has unresolved owner work after preflight read")
        preflight_passed = True
        recorder.record("preflight_passed", snapshot=initial, values=initial_values, pending_work=False)

        start_field = float(initial_values["field_t"])
        if abs(start_field - START_T) <= max(0.001, abs(START_T) * 2e-4):
            recorder.record("preposition_skipped", reason="live field already inside start gate", field_t=start_field)
        else:
            mutation_attempted = True
            recorder.record("preposition_setpoint_requested", target_t=START_T)
            set_result = resolve(app, controller.set_h_setpoint_async(START_T))
            recorder.record("preposition_setpoint_result", result=set_result)
            start_result = resolve(app, controller.start_field_control_async())
            recorder.record("preposition_start_result", result=start_result)
            deadline = time.monotonic() + PREPOSITION_TIMEOUT_S
            consecutive = 0
            previous = start_field
            direction = 1.0 if START_T > start_field else -1.0
            while consecutive < 5:
                if time.monotonic() > deadline:
                    raise RuntimeError("preposition start gate timed out")
                snapshot = resolve(app, controller.read_snapshot_async())
                values = validate_snapshot(snapshot, expected_setpoint=START_T)
                field = float(values["field_t"])
                max_temperature = max(max_temperature, float(values["temperature_k"]))
                recorder.record("preposition_sample", snapshot=snapshot, values=values)
                if direction * (field - previous) < -MATERIAL_DIRECTION_T:
                    raise RuntimeError("materially unexpected preposition field direction")
                if field > STOP_T + MATERIAL_ENDPOINT_OVERSHOOT_T:
                    raise RuntimeError("field materially exceeded the authorized test endpoint")
                consecutive = consecutive + 1 if abs(field - START_T) <= max(0.001, abs(START_T) * 2e-4) else 0
                previous = field
                if consecutive < 5:
                    time.sleep(POLL_S)
            recorder.record("preposition_complete", consecutive_in_gate=consecutive, snapshot=snapshot)
            time.sleep(0.5)

        pre_leg = resolve(app, controller.read_snapshot_async())
        pre_leg_values = validate_snapshot(pre_leg)
        max_temperature = max(max_temperature, float(pre_leg_values["temperature_k"]))
        recorder.record("pre_leg_snapshot", snapshot=pre_leg, values=pre_leg_values)

        mutation_attempted = True
        recorder.record("continuous_setpoint_requested", target_t=STOP_T)
        set_result = resolve(app, controller.set_h_setpoint_async(STOP_T))
        recorder.record("continuous_setpoint_result", result=set_result)
        start_result = resolve(app, controller.start_field_control_async())
        recorder.record("continuous_start_result", result=start_result)

        trajectory_started = time.monotonic()
        deadline = trajectory_started + LEG_TIMEOUT_S
        previous = float(pre_leg_values["field_t"])
        sample_index = 0
        endpoint_snapshot = None
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError("continuous endpoint gate timed out")
            snapshot = resolve(app, controller.read_snapshot_async())
            values = validate_snapshot(snapshot, expected_setpoint=STOP_T)
            field = float(values["field_t"])
            max_temperature = max(max_temperature, float(values["temperature_k"]))
            sample_index += 1
            elapsed = time.monotonic() - trajectory_started
            recorder.record(
                "continuous_trajectory_sample",
                sample_index=sample_index,
                elapsed_s=elapsed,
                snapshot=snapshot,
                values=values,
            )
            if field < previous - MATERIAL_DIRECTION_T:
                raise RuntimeError("field moved materially opposite the authorized increasing sweep")
            if field > STOP_T + MATERIAL_ENDPOINT_OVERSHOOT_T:
                raise RuntimeError("field materially exceeded the authorized test endpoint")
            if field >= STOP_T - GATE_T:
                endpoint_snapshot = snapshot
                recorder.record(
                    "endpoint_gate_reached",
                    elapsed_s=elapsed,
                    field_t=field,
                    condition=f"B >= {STOP_T - GATE_T:g} T",
                )
                break
            previous = field
            time.sleep(POLL_S)

        final_snapshot = resolve(app, controller.read_snapshot_async())
        final_values = validate_snapshot(final_snapshot, expected_setpoint=STOP_T)
        max_temperature = max(max_temperature, float(final_values["temperature_k"]))
        recorder.record("final_live_snapshot", snapshot=final_snapshot, values=final_values)
        recorder.record(
            "normal_leg_completed",
            elapsed_s=time.monotonic() - trajectory_started,
            maximum_temperature_k=max_temperature,
            stop_requested=False,
            pause_requested=False,
        )

        detach_handle = controller.detach_completed_run_async(STOP_T, GATE_T)
        recorder.record("successful_detach_requested", target_t=STOP_T, gate_t=GATE_T)
        detach_result = resolve(app, detach_handle, timeout_s=30.0)
        detached = True
        recorder.record(
            "successful_detach_result",
            result=detach_result,
            owner_thread_running=controller._thread.isRunning(),
            controller_state=controller.state,
        )
        mutating = [
            event for event in recorder.state["events"]
            if event.get("event") == "magnet_mutating_rpc"
        ]
        recorder.record(
            "commissioning_completed",
            status="PASSED",
            maximum_temperature_k=max_temperature,
            mutating_rpc_list=[event.get("rpc") for event in mutating],
            stop_rpc_seen=any(event.get("rpc") == "stopFieldControl" for event in mutating),
            ramp_rate_setter_seen=False,
            lightfield_acquisition=False,
        )
        print(str(evidence_path.resolve()))
        return 0
    except BaseException as exc:
        text = str(exc)
        recorder.record(
            "commissioning_failed",
            error_type=type(exc).__name__,
            error=text,
            tryagain="TRYAGAIN" in text.upper(),
            traceback=traceback.format_exc(),
            maximum_temperature_k=None if max_temperature == float("-inf") else max_temperature,
        )
        if connected and preflight_passed and mutation_attempted and not detached:
            try:
                recorder.record("abnormal_stop_requested")
                stop_result = resolve(app, controller.request_stop(), timeout_s=30.0)
                recorder.record("abnormal_stop_result", result=stop_result)
            except BaseException as stop_exc:
                recorder.record("abnormal_stop_error", error_type=type(stop_exc).__name__, error=str(stop_exc))
        if connected and not detached:
            try:
                disconnect_result = resolve(app, controller.disconnect_async(), timeout_s=30.0)
                recorder.record("disconnect_result", result=disconnect_result)
            except BaseException as disconnect_exc:
                recorder.record("disconnect_error", error_type=type(disconnect_exc).__name__, error=str(disconnect_exc))
        try:
            recorder.record("shutdown_result", result=controller.shutdown(30.0))
        except BaseException as shutdown_exc:
            recorder.record("shutdown_error", error_type=type(shutdown_exc).__name__, error=str(shutdown_exc))
        print(str(evidence_path.resolve()))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
