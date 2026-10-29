"""Run every test module in a fresh process, with deterministic CI shards.

Qt objects, application properties and patched SDK modules must not leak from
one module into the next. A crash/timeout is recorded as a failure, never a skip.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if not 0 <= args.shard_index < args.shard_count or args.timeout <= 0:
        parser.error("Invalid shard or timeout")
    modules = sorted((ROOT / "tests").rglob("test_*.py"))
    if not modules:
        raise RuntimeError("No test modules found")
    selected = modules[args.shard_index::args.shard_count]
    output = ROOT / "tmp/ci-tests" / f"shard-{args.shard_index}"
    output.mkdir(parents=True, exist_ok=True)
    suites = ET.Element("testsuites")
    failures = []
    for index, module in enumerate(selected, 1):
        label = module.relative_to(ROOT).as_posix()
        stem = module.stem
        xml_path = output / f"{stem}.xml"
        xml_path.unlink(missing_ok=True)
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
        # Never inherit a developer's persisted device/configuration choices.
        profile = output / f"{stem}-profile"
        profile.mkdir(exist_ok=True)
        config = profile / "config.json"
        config.write_text("{}", encoding="utf-8")
        env["SPECTRALSWEEP_CONFIG_PATH"] = str(config)
        started = time.monotonic()
        command = [sys.executable, "-m", "pytest", str(module), "-q", "-ra",
                   "--durations=5", f"--junitxml={xml_path}"]
        with (output / f"{stem}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=args.timeout)
                reason = f"pytest exited with {code}"
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, check=False)
                else:
                    process.kill()
                process.wait()
                code, reason = 1, f"Module exceeded {args.timeout}s timeout"
        reported_failure = False
        if xml_path.exists():
            for suite in ET.parse(xml_path).getroot().iter("testsuite"):
                suites.append(suite)
                reported_failure |= bool(int(suite.get("failures", "0")) + int(suite.get("errors", "0")))
        if not xml_path.exists() or (code and not reported_failure):
            suite = ET.SubElement(suites, "testsuite", name=label, tests="1", failures="1")
            case = ET.SubElement(suite, "testcase", name="module_process", classname=stem)
            ET.SubElement(case, "failure", message=reason).text = reason
        if code:
            failures.append(label)
            print((output / f"{stem}.log").read_text(encoding="utf-8")[-16000:], flush=True)
        print(f"[{index}/{len(selected)}] {label}: {'FAIL' if code else 'PASS'} "
              f"({time.monotonic() - started:.1f}s)", flush=True)
    ET.ElementTree(suites).write(output / "test-results.xml", encoding="utf-8", xml_declaration=True)
    summary = {"total_modules": len(modules), "selected_modules": len(selected),
               "shard_index": args.shard_index, "shard_count": args.shard_count,
               "failed_modules": failures}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
