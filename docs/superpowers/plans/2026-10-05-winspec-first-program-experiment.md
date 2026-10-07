# WinSpec first-program omission experiment implementation plan

> **For agentic workers:** Use executing-plans in this session; retain the existing independent reviewer. The user has authorized executing the diagnostic and conditional experiment. Do not ask again for routine implementation choices.

**Goal:** Determine whether omitting the first programming body, while retaining its handshake and the second complete program, reduces Start time for one fixed 500 ms × 2 acquisition.

**Architecture:** Keep all production files unchanged. A separate XP diagnostic learns a baseline within the same owned WinSpec process, validates a strictly bounded state match, then optionally redirects one suspended instruction pointer from `contrman+C6708` to `contrman+C6D89`. Natural success bookkeeping and RET4 execute normally. Any mismatch executes the full original body. Record whether an actual omission occurred; a fallback is not an experiment success.

**Tech Stack:** Python 2.7 x86 on XP; existing guarded COM acquisition; Windows execution breakpoints; Python 3.13 pytest and frozen PE analysis on host.

**Spec:** `docs/winspec-side-port.md`, sections on repeated preparation, actual output sequences, and input handshake. Frozen evidence is in `tmp/winspec-performance-20261001/communication-dependency-20261005` and `input-dependency-20261005`.

## Constraints

- Preserve real `D6266` handshake, its original failure branch, and status-0x100 handling before C6708.
- Preserve every Stop/mode/count/ROI operation outside the omitted body and the complete second program.
- C9D30 and the first body contain additional checks; this is an isolated causal experiment, not a claim of retaining every check or production readiness.
- No DLL/code-memory patches, stale-object restoration, manual flag writes, temperature-policy changes, or takeover of existing COM owners.
- Preserve debugger ownership, foreign exceptions, trap-flag restoration, uncertain-Stop retention, durable SPE archival and recipe/flag restoration.
- ROI heads +7190/+7194 are rebuilt lists, not interchangeable cached objects. Read current nodes; compare contents; never restore old pointers.
- Fixed exposure 500 ms, accumulations 2, sequential frames 1, current ROI/ADC/gain and existing cold_or_locked temperature policy, max gap 3 s.
- Save all experiment code under `tmp/winspec-performance-20261001/first-program-experiment-20261005/`. Do not commit unrelated workspace changes or enable production behavior.

## Task 1: Validate an instruction-pointer transfer without hardware

Files: `redirect_step.py`, a private copy of `winspec_debug_trace.py`, `test_redirect_step.py`, `redirect_selftest.py` in the experiment directory.

- [x] Write tests that reject a foreign TF, unmatched source address, wrong destination bytes, incompatible stack, non-unit success local, and a destination that is another active breakpoint.
- [x] Observe failures before implementing the redirect helper.
- [x] The helper returns the expected post-step EIP and ESP; it changes only EIP before the existing controlled instruction step. In the debugger, compare the actual post-step state with those expectations, preserving the destination instruction's real flags.
- [x] Execute a native synthetic function in XP with a success local at EBP-14, an observable body-side counter, a natural bookkeeping tail and RET4. Compare baseline and redirected return/counter/stack behavior, and execute again after detach.
- [x] Exercise two threads, rejected guards, foreign exception handling, and detach while a redirected function is still active. Confirm the debugger does not leave TF or debug registers behind.

Required behavioral assertions:

```python
assert baseline_return == redirected_return == 1
assert baseline_body_counter == 1 and redirected_body_counter == 0
assert baseline_tail_counter == redirected_tail_counter == 1
assert trace['detached'] and not trace['debugger_attached_at_return']
assert post_detach_return == 1
```

## Task 2: Add a fail-closed, same-process experiment gate

Files: `experiment_state.py`, `experiment_trace.py`, `test_experiment_state.py`, `targets.json`.

- [x] Capture program entry, body entry C6708, and program exit; capture Output entries as an independent programming-work count.
- [x] At C6708 verify frame provenance, EBP=entry ESP-4, ESP=EBP-64, local EBP-14=1, original DE32 return and controller argument. Validate frozen source and destination bytes.
- [x] Read exact native7530/settings4A8/mirror3C allocations, active PIPP/PIDC dispatch, and bounded current ROI lists (node size20 hex). Require stable, valid links and semantic contents equal to the baseline, allowing only replacement node addresses.
- [x] Require a complete baseline with two successful programs; both bodies must have equal semantic configuration. Confirm baseline post-state changes are confined to the known natural-tail fields. Do not expand an allowed-difference list after seeing unexpected changes.
- [x] In the trial, allow one transfer only at program1/body1 on the same controller/thread with matching state. Program2 always runs in full. A missing baseline, extra call, changed setting/object/ROI or read error disables the transfer.
- [x] Test all rejection paths, second-call non-omission, bounded/cyclic/invalid lists and missing baseline.

Required behavioral assertions:

```python
assert decision(matching_first_body)['redirect'] is True
assert decision(second_body)['redirect'] is False
assert decision(changed_roi)['redirect'] is False
assert decision(incomplete_baseline)['redirect'] is False
```

## Task 3: Review, stage, and conditionally run one guarded trial

Files: `internal_probe.py`, `control.py`, `stage.py`, `analyze.py`, frozen source/evidence and analysis manifests in the experiment directory; outcome in `docs/winspec-side-port.md`.

- [x] Retain the known guard/ownership/archive code. Use four before frames, one baseline traced frame, one trial traced frame and four after frames. Only one trial may redirect.
- [x] Run host tests, XP compile and all camera-free selftests; independently review frozen code and target bytes before starting hardware.
- [x] If any precondition fails, preserve evidence and report the exact unresolved condition; do not improvise a broader skip on hardware.
- [x] After the run, verify trace coverage, exactly one or zero actual transfers, second complete program, natural return/status, ten distinct valid SPE files, temperature reports, restoration, detach, Quit and absence of remaining owners.
- [x] Compare baseline and trial Start durations with surrounding controls; label measurements as instrumented. A single successful fixed-recipe trial does not enable production reuse or cover changed exposure/ROI/reconnect.
- [x] Preserve failures, hash source/evidence, recompute host analysis and update the diagnostic documentation.

## Verified outcome

Version 2 completed the conditional 10-frame run with one actual first-body omission. Instrumented Start was 0.652363 s baseline and 0.361668 s trial; 155 first-body output entries were removed. All preserved streams, ten SPE files, temperature, restoration and exit were independently verified. Version 1 failed before trial on a report filename collision; its evidence remains intact. Production acquisition was not changed. See `tmp/winspec-performance-20261001/first-program-experiment-20261005/analysis-v2-final.json` and the diagnostic section in `docs/winspec-side-port.md`.
