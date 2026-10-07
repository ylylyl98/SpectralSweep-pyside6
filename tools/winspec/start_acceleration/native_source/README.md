# WinSpec Start native helper â€” maintenance

This source supports the optional WinSpec Start accelerator on the validated
32-bit Windows XP driver build. Python owns the process, debugger, document and
archive lifecycle. The native helper replaces two `contrman.dll` import-table
entries; it leaves the original program pointer at `controller + 0x618` intact.

The helper checks the current program's original stack frame and current pulse
values. A generation-bound permission from the debugger allows five checked
75-byte writes during the second program. It does not replay a saved first
program. Failed or uncertain I/O retains the owner and buffers and blocks further
output; a false return from the original program is not a sufficient abort.

## Source and build provenance

The reviewed development source is in
`tmp/winspec-performance-20261001/combined-start-formal-20261005/`. Its `build.py`
uses the existing x86 TCC 0.9.27 compiler at
`tmp/winspec-performance-20261001/driver-command-trace-build/tcc/tcc.exe`.
The compiler SHA-256 recorded in the current audit is
`11b86934bb2833f57fa0453a605ca342aee9207e193faea9b973baa2b2b4c35b`.

The DLL build is equivalent to:

```text
tcc.exe -shared -nostdlib -o batch_probe.dll batch_probe.c -lkernel32 -lmsvcrt
```

`build.py` also builds `test_batch.exe`, `replay_test.exe`, `debug_fixture.exe` and
`lifecycle_test.exe`, then writes `binary-audit.json`. The audit records source,
compiler and DLL hashes, export addresses, image size, the fixed control-entry
prefix, validated driver hashes and relocation-aware driver code excerpts. The
DLL is x86 (`Machine = 0x14c`) with subsystem major version 4. The control prefix
must contain no relocation entries; supported driver excerpts use the audited
HIGHLOW fixups when loaded at nonpreferred addresses.

The current build script resolves its compiler, `pefile` and driver evidence
relative to that development directory. Copying it into a permanent source
directory does not make those input paths portable. Preserve the frozen recipe
and input hashes, or explicitly parameterize those paths before relocating the
build. Do not infer runtime driver offsets from a different driver installation.
The SDK/driver binaries are audit inputs, not helper runtime dependencies to add
to this source directory.

## Evidence and resource lifetime

- The 30-DWORD shared Gate identity remains stable while asynchronous readers
  inspect it. Arm publishes `armed` only after initializing the new generation.
- Unarmed callbacks preserve original output behavior and activity/fault
  ownership, while leaving the bounded record buffer unchanged.
- Each WinSpec process retains one loaded helper and one write event. Python
  reuses one CONTROL buffer only after the preceding remote call is confirmed.
- Control operation 6 accepts the exact archived generation, frame, row address,
  row size and count only after hook restoration and a healthy idle state. It
  clears committed records, preserving generation, final state and I/O resources.
  Durable archival and hash verification are Python responsibilities.
- Neither reset nor reinstallation clears a fault, an unknown completion or a
  held debugger event. DLL, event, OVERLAPPED, packet and remote allocations stay
  retained until the owned process exits when completion is uncertain.

## Tests without a camera

The following fixtures create synthetic controller memory and named pipes. They
do not load the camera SDK or open a physical instrument.

| File | Coverage |
| --- | --- |
| `test_batch.c` | The same checked WriteFile core: complete synchronous/pending writes, errors, short writes, cancellation and unknown-completion retention. |
| `replay_test.c` | The actual DLL and x86 calling conventions, original program pointer, fresh permissions, current pulse values, packet bytes and failure parking. |
| `lifecycle_test.c` | More than one record-buffer capacity across archive/reset cycles, rejected archive descriptors, busy/held/faulted states, stale nonces and event/handle reuse. |
| `debug_fixture.c` | A synthetic x86 program with the real entry/body/redirect/exit instruction shapes and original stack frame, controlled by the external Python debugger. |
| `binding_lifecycle_selftest.py` | Real NativeProbe construction, explicit install, remote CONTROL calls, readback and repeated retirement/reinstallation; only SDK module/controller identity uses a fixture. |
| `combined_debug_selftest.py` and `combined_debug_fixture.py` | Actual XP suspension, bounded helper-memory writes, redirect and TF completion, actual DLL packets, and retained ownership after an uncertain permission write. SDK snapshots/caller identity are explicit fixtures. |

`run_native_checks.py` runs the native executables on the development host.
`native_selftest.py` is the packaged XP entry point and adds Python ownership,
binding and debugger integration checks. Keep each run's source/binary hashes
with its output. Use a fresh staging/evidence directory because several outputs
are deliberately created exclusively.

These tests verify the native protocol, resource lifecycle and debugger wiring.
They do not establish real optical equivalence or unlimited hardware uptime.
After a native behavior change, complete the XP checks and the applicable short
hardware/data comparison before replacing the accepted DLL/audit pair. Do not
mix a rebuilt DLL with an older audit or label an untested rebuild as accepted.

## Permanent Python tests

The prepared test copies under `production-tests/` import
`tools.winspec.start_acceleration` directly. After the implementation is promoted,
copy the three `test_winspec_*.py` files into `tests/` and run those alongside the
existing WinSpec ownership, transport and acceleration regression tests. Their
test bodies retain the candidate tests' assertions; temporary package bootstraps
and cross-test dynamic imports have been removed.
