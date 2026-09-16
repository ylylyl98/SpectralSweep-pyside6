# MCD2100 preparation timeout and progress fix

## Cause and scope

The shared preparation coordinator reused the 300 s mode-readiness timeout
for reaching Start. The position budget includes a fresh preflight, target
commands, movement, slow owner reads and five safe in-tolerance observations.
A nominal 284 s movement therefore leaves too little time for confirmation.
The observed later arrival at -2 T does not prove which read or confirmation
was active when the previous software timeout occurred.

Both Prepare magnet and measurement Start now pass
`attodry2100.position_timeout_s`, default 1800 s. The 300 s mode-readiness
budget and individual SDK/owner request budgets are unchanged. The new value
is a configurable software wait limit, not a measured speed, rate command,
or estimate derived from an unverified ramp table. Existing config files
inherit the default; configuration values must be finite and positive before
preparation submits owner work. Legacy coordinator callers that omit the new
argument retain their explicit `timeout_s` positioning behavior.

## Time boundaries and evidence

Position time starts after mode preparation has returned and drained, before
the fresh preflight and field commands. Expiry is checked before subsequent
submissions and after an owner result drains. A fifth in-tolerance result
arriving at or after the deadline cannot turn the operation into success.
Existing owner drain, cancellation, five-sample confirmation, safety checks
and failure Stop cleanup remain in place. No new safety condition or Stop
trigger was added; no ramp-rate write, unit/mapping or scan sampling changed.

Position timeout logs and errors include the last successful snapshot's
field, target, signed error, sample age, elapsed/remaining position budget
and prior accepted stable count. This is recorded before existing cleanup.
A late result is retained as evidence but is not counted as confirmation.

## Display and logging

A separate preparation progress callback carries stage, target/current field,
snapshot timestamp, budget boundaries and stable count. It is wired through
both entry points. The existing UI activity timer renders sample age and
remaining time without polling the instrument. Mode preparation reuses
`snapshot_updated`; its accepted timestamp must belong to the active stage
and be newer than the last displayed sample. Missing samples show N/A, and
sample age continues growing while owner queries are pending.
Mode readiness shows observed Driven/Persistent/heater/leads state and lead
field, with explicit unknown values; the five-sample counter is shown only
for position progress. The status label wraps within its existing layout.

Position progress logs are limited to 15 s or stage/confirmation-state changes.
Mode wait logs use the UI timer on the same 15 s cadence. Progress does not
reset the phase timer per sample. Runner identity and a closed-preparation
flag reject old-run and post-terminal progress; older worker callback
signatures keep phase and spectrum-event delivery.

## Verification

Tests use virtual clocks, fake drainable handles and offscreen Qt. Coverage
includes 284 s movement plus slow reads and five confirmations, independent
budgets, late fifth-sample rejection, expired preflight without field writes,
cancel-during-read drain/Stop order, configuration validation, both entry
points, unchanged owner request count, local aging, log cadence and stale
runner rejection. No real application or instrument connection was started.

Focused command (bundled environment has unittest, not pytest):

```powershell
.venv-pyside6-313/Scripts/python.exe -m unittest tests.test_magnet_preparation tests.test_mcd2100_workflow tests.test_mcd2100_panel tests.test_mcd2100_batch tests.test_config_persistence -q
```

Result: 146 tests passed in 13.894 s, process exit 0. `git diff --check`
also passed; Git emitted only existing LF-to-CRLF conversion notices.

Hardware timing remains to be verified during the next user-controlled run;
the software tests do not assert real ramp speed or firmware behavior.

## Final independent Astra review

PASS. Implementing Astra reported 146 affected tests passing (13.894 s, exit 0).
Root Astra reviewed the change against the pre-fix file snapshot and independently
ran the complete `tests.test_magnet_preparation` module plus three panel regressions
for local aging/stale runner rejection, mode snapshot/cadence, and wrapped geometry:
16 tests passed, exit 0. Counts describe separate runs and are not additive coverage.

Review confirmed independent budgets for both entry points, actual owner drain before
deadline decisions, no success from a late fifth confirmation, and detailed timeout
evidence before existing cleanup. Mode progress originally displayed the position
five-sample counter; this was corrected to show observed mode fields with unknown
values. Long status text now wraps and passed offscreen geometry verification.

Root comparison found `_leg` AST unchanged, and controller/adapter files byte-identical
to the pre-fix snapshot. No remaining blocking findings in this repair scope. The five
ramp-table SDK errors remain a separate unresolved compatibility investigation.

## User-operated acceptance

At a suitable time, restart the updated App and use the next planned Prepare or Start.
No extra magnet motion is needed purely for this verification. In mode readiness,
observe changing elapsed time and sample age plus available mode states. In position
progress, observe actual/target field, error and stable confirmation count. The default
position budget is 1800 s (30 minutes); reaching the target and completing confirmation
finishes immediately, without waiting out this limit. Configure `attodry2100.position_timeout_s`
independently of `mode_prepare_timeout_s` if a different bounded wait is required.

Record stage/progress logs through `Magnet ready` or any final failure. If a timeout
occurs, retain the detailed pre-cleanup field/sample-age/count message and subsequent
cleanup outcome. Do not intentionally cause a timeout or change sweep rates for testing.
