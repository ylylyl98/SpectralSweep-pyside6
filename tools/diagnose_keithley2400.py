"""
Standalone Keithley 2400 / 2401 connection diagnostic (no Qt / UI).

Purpose
-------
Reproduce the exact initialization sequence used by the application one
operation at a time, with timestamped, per-operation logging, so the precise
WRITE / QUERY / READ that stalls can be identified without any UI involved.

The RSYN A/B experiment for a specific unit is:

  Scenario A (RSYN sent):    --rsyn on
  Scenario B (RSYN skipped): --rsyn off   (automatic default skips only the
                                           Keithley MODEL 2400)

Scenarios to compare (see the project hardening brief):

  A. Fresh power cycle  : power-cycle the instrument, run immediately.
  B. Close / reconnect   : run once, close, wait, run again.
  C. Aborted experiment  : stop/interrupt a run, then run this script.
  D. Changed trigger     : set a non-immediate trigger (e.g. :TRIG:SOUR TLIN)
                           if safe, close, then run again to verify recovery.

Usage
-----
  python tools/diagnose_keithley2400.py --address GPIB0::24::INSTR
  python tools/diagnose_keithley2400.py --address GPIB0::24::INSTR --with-read
  python tools/diagnose_keithley2400.py --address GPIB0::24::INSTR --rsyn on
  python tools/diagnose_keithley2400.py --address GPIB0::24::INSTR --rsyn off
  python tools/diagnose_keithley2400.py --address GPIB0::24::INSTR --no-recover

The script exits 0 when every stage passes and 1 when any stage fails.  The
primary failure is always printed with its last successful operation.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standalone Keithley 2400/2401 connection diagnostic."
    )
    parser.add_argument("--address", required=True, help="VISA address, e.g. GPIB0::24::INSTR")
    parser.add_argument("--timeout-ms", type=int, default=5000, help="Per-I/O timeout (default 5000)")
    parser.add_argument("--no-recover", action="store_true", help="Skip VISA clear + *CLS + :ABOR on open")
    parser.add_argument("--with-read", action="store_true", help="Also perform a live READ? at the end")
    parser.add_argument("--output-on", action="store_true", help="Enable :OUTP ON during source configuration")
    parser.add_argument("--check-errors", action="store_true", help="Drain :SYST:ERR? after each stage")
    parser.add_argument("--curr-compliance", type=float, default=6e-7, help="Current compliance in A")
    parser.add_argument("--volt-range", type=float, default=20.0, help="Voltage source range in V")
    parser.add_argument(
        "--rsyn",
        choices=("auto", "on", "off"),
        default="auto",
        help=":SENS:CURR:PROT:RSYN policy. auto sends it for every model "
        "except the Keithley MODEL 2400 (whose old firmware wedges its GPIB "
        "interface for ~10 s when sent this command); on/off force it.",
    )
    parser.add_argument(
        "--scenario",
        choices=("fresh", "stale", "reconnect", "trigger-changed"),
        default="stale",
        help="Informational label describing the hardware state being tested.",
    )
    args = parser.parse_args()

    log(f"scenario: {args.scenario}")
    log(f"target: {args.address}  timeout_ms={args.timeout_ms}")

    try:
        import pyvisa
        from iv_automation import KeithControl
    except Exception as exc:  # pragma: no cover - import failure path
        log(f"DIAGNOSTIC FAILED (import): {type(exc).__name__}: {exc}")
        return 1

    log(f"pyvisa version: {getattr(pyvisa, '__version__', '?')}")
    rm = None
    kc = None
    try:
        rm = pyvisa.ResourceManager()
        log(f"resource manager: {rm}")
        try:
            resources = rm.list_resources()
            log(f"VISA resources visible: {resources}")
        except Exception as exc:
            log(f"list_resources failed: {type(exc).__name__}: {exc}")

        log(f"opening {args.address} ...")
        kc = KeithControl(
            args.address,
            "DIAG_SMU",
            "Vbg",
            rm,
            curr_compliance=args.curr_compliance,
            volt_compliance=args.volt_compliance,
            timeout_ms=args.timeout_ms,
            configure_on_connect=False,
            recover_on_open=not args.no_recover,
            rsyn_enabled={"auto": None, "on": True, "off": False}[args.rsyn],
            trace_io=True,
        )
        log("--- I/O performed during open/recovery/identify ---")
        for entry in kc.recent_io(50):
            status = entry.get("status")
            if status == "ok":
                detail = f"OK {entry.get('elapsed_ms', 0):.0f} ms"
            else:
                detail = (
                    f"{entry.get('classification', 'FAILED')} after "
                    f"{entry.get('elapsed_ms', 0):.0f} ms: {entry.get('error')}"
                )
            log(
                f"  {entry.get('timestamp')} {entry.get('address')} "
                f"{entry.get('op')} {entry.get('command')} -> {detail}"
            )

        log(f"full IDN: {kc.identity_raw or '?'}")
        log(
            f"normalized model: {kc.identity.get('model') or kc.model or '?'}  "
            f"serial: {kc.identity.get('serial') or '?'}  "
            f"firmware: {kc.firmware or '?'}"
        )
        log(f"session params: {kc.session_params}")

        log(f"stage CAPABILITY: :SENS:CURR:PROT:RSYN policy={args.rsyn} ...")
        kc.ensure_trigger_immediate(verify=False)
        settings = kc.apply_compliance_settings(
            args.curr_compliance, None, args.volt_range
        )
        log(f"compliance settings: {settings}")
        log(
            "Detected model: "
            f"{kc.identity.get('model') or kc.model or '?'}"
        )
        log(f"Resolved RSYN policy: {kc.rsyn_policy}")
        log(
            "RSYN sent/skipped: "
            f"{'sent' if kc._rsyn_supported else 'skipped'} "
            f"(supported={kc._rsyn_supported}, "
            f"system errors: {kc._last_system_errors})"
        )

        log("stage SOURCE: configuring fixed-voltage source ...")
        kc.set_volt_step(
            curr_compliance=args.curr_compliance,
            volt_compliance=args.volt_range,
            output_on=args.output_on,
        )
        log("stage READBACK: reading compliance settings back ...")
        settings = kc.read_compliance_settings()
        log(f"readback settings: {settings}")

        log("stage TRIGGER: verifying :TRIG:SOUR ...")
        source = kc.ensure_trigger_immediate(verify=True)
        log(f":TRIG:SOUR? -> {source}")

        log("stage DIAGNOSTIC: *ESR? and :OUTP? ...")
        esr = kc.read_esr()
        log(f"*ESR? -> {esr} (power-on bit set: {kc.esr_power_on(esr)})")
        out = kc.output_state()
        log(f":OUTP? -> {out}")

        if args.check_errors:
            errors = kc.drain_system_errors(max_errors=8)
            log(f":SYST:ERR? drain -> {errors}")

        if args.with_read:
            log("stage MEASUREMENT: performing READ? ...")
            volt, curr = kc.read_float()
            log(f"READ? -> {volt} V, {curr} A")

        log("DIAGNOSTIC PASSED")
        return 0
    except Exception as exc:
        log(f"DIAGNOSTIC FAILED: {type(exc).__name__}: {exc}")
        if kc is not None:
            log("--- last I/O entries ---")
            for entry in kc.recent_io(10):
                status = entry.get("status")
                if status == "ok":
                    detail = f"OK {entry.get('elapsed_ms', 0):.0f} ms"
                else:
                    detail = (
                        f"{entry.get('classification', 'FAILED')} after "
                        f"{entry.get('elapsed_ms', 0):.0f} ms: {entry.get('error')}"
                    )
                log(
                    f"  {entry.get('timestamp')} {entry.get('op')} "
                    f"{entry.get('command')} -> {detail}"
                )
            log("--- stages ---")
            for stage in kc.stage_log:
                log(f"  {stage}")
        traceback.print_exc()
        return 1
    finally:
        if kc is not None:
            try:
                kc.close()
            except Exception:
                pass
        if rm is not None:
            try:
                rm.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
