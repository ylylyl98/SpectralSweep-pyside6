"""Bounded camera-free verification of the actual x86 helper DLL."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
CASES = (
    "identity-race", "combined", "changed-bits", "missing-permit", "stale-permit",
    "full-first", "mode-zero", "first-pulse", "header-shape", "second-header",
    "sequence", "scope", "thread", "phase", "hold", "revoke-permit",
    "padding", "pulse-shape", "pre-failure", "io-error", "busy-disarm",
)


def main():
    results = []
    commands = [["test_batch"]] + [["replay_test"] + ([] if c == "combined" else [c])
                                   for c in CASES]
    commands += [["lifecycle_test", c] for c in ("reset-only", "events", "idle-hold", "idle-fault")]
    commands += [["lifecycle_test"]]
    for command in commands:
        try:
            run = subprocess.run([str(ROOT / (command[0] + ".exe"))] + command[1:],
                                 cwd=ROOT, capture_output=True, text=True, timeout=15 if command[0] == "lifecycle_test" else 6)
            item = dict(test=command, rc=run.returncode, stdout=run.stdout,
                        stderr=run.stderr)
        except subprocess.TimeoutExpired:
            item = dict(test=command, timeout=True)
        print(json.dumps(item), flush=True)
        results.append(item)
    report = dict(dll_sha256=hashlib.sha256((ROOT / "batch_probe.dll").read_bytes()).hexdigest(),
                  results=results)
    (ROOT / "native-local-checks.json").write_text(json.dumps(report, indent=2) + "\n")
    assert all(row.get("rc") == 0 for row in results), report


if __name__ == "__main__":
    main()
