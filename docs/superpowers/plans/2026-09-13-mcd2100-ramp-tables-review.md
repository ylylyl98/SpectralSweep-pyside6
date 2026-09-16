# Astra ramp-table review

This stage follows the completed/reviewed magnet-preparation change. Review uses the pre-ramp baseline saved at `C:/Users/commo/AppData/Local/Temp/codex-mcd2100-preparation-baseline-le1q9ja8`. No real instrument reads or writes are part of implementation or review.

## Initial review: changes required

Astra confirmed correct current/default getter argument order, raw data preservation and bounded enumeration, with no diagnostic-path SDK writes. Three P2 findings were sent to Luna for correction:

1. Ramp-read admission must atomically require eligible IDLE/ACTIVE lifecycle and no other pending owner request. A blocked SETPOINT allowed ramp reading to queue afterward.
2. Existing manual/chained telemetry and applied-temperature monitor callbacks must not enqueue display reads while the ramp operation is queued, running or draining.
3. A completed report containing table/row errors must display partial/error completion rather than unconditional success, preserving successful rows.

The correction also requires a real panel client-timeout/late-partial-drain regression, including lock retention and error visibility.

## Final targeted review: passed

All three findings are resolved. Astra verified atomic no-pending admission, report errors reflected in status/logs, and timeout/drain partial results. Applied-temperature monitoring now skips submissions during ramp ownership while preserving its timer and resumes afterward.

An additional fake ACTIVE-owner timeout test preserved ACTIVE through drain, allowed a fresh explicit table read afterward, and confirmed a later explicit Stop still works. The ramp diagnostic itself does not issue Stop.

Verification:

- Root ran 219 combined tests successfully before the final one-guard monitor correction, then independently reran all 42 panel tests successfully after that correction.
- Astra ran eight distinct targeted ramp tests across review rounds plus fake owner-timeout/drain probes.
- Compile checks and git diff --check passed.
- No hardware reads/writes, commits, new connections or telemetry optimization performed in this stage.

Final verdict: no remaining actionable findings within the ramp-table scope. Proceed sequentially to the existing telemetry-refresh plan.
