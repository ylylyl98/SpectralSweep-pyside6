"""Isolated, device-free motion preview timings; each size uses a fresh process.

Example: python tools/benchmark_motion_sequence.py --sizes 1000 5000 100000
         --label model --output tmp/motion-sequence-model.json
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def resident_bytes() -> int:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize
    if sys.platform.startswith("linux"):
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    raise RuntimeError("RSS measurement supports Windows and Linux")


def measure(size: int) -> dict:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import PySide6
    from PySide6.QtWidgets import QApplication
    from ui.motion_sequence_preview import MotionSequencePreviewDialog

    app = QApplication.instance() or QApplication([])
    rows = [dict(sequence=i + 1, condition_index=i % 10 + 1, label=f"Condition {i % 10}",
                 D=1.0, F=2.0, Vtg=1.5, Vbg=-0.5, Vbias=0.1,
                 rot1=float(i % 360), rot2=25.0, repeat=2, point_count=8,
                 context={"source": i, "values": [1.0, 2.0]}) for i in range(size)]
    before = resident_bytes()
    started = time.perf_counter()
    dialog = MotionSequencePreviewDialog(rows, point_count=8)
    constructed = time.perf_counter()
    after_constructor = resident_bytes()
    dialog.show()
    app.processEvents()
    visible = time.perf_counter()
    result = {"rows": size, "constructor_seconds": constructed - started,
              "first_layout_seconds": visible - started,
              "constructor_rss_delta_bytes": after_constructor - before,
              "first_layout_rss_delta_bytes": resident_bytes() - before,
              "python": sys.version.split()[0], "pyside6": PySide6.__version__}
    model = getattr(dialog, "sequence_model", None)
    if model is not None:
        started = time.perf_counter()
        model.set_checked(size // 2, False)
        result["checkbox_seconds"] = time.perf_counter() - started
        started = time.perf_counter()
        model.move_entry(size - 1, 0)
        result["move_last_to_first_seconds"] = time.perf_counter() - started
        result["selected_count"] = model.selectedCount
    dialog.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 5000, 100000])
    parser.add_argument("--label", default="motion-preview")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker is not None:
        print(json.dumps(measure(args.worker)))
        return
    results = []
    with tempfile.TemporaryDirectory(prefix="motion-sequence-benchmark-") as isolated:
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                           SPECTRALSWEEP_HISTORY_PATH=str(Path(isolated) / "history.sqlite"))
        for size in args.sizes:
            if size < 1 or size > 100000:
                parser.error("sizes must be between 1 and 100000")
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                        "--worker", str(size)], env=environment,
                                       capture_output=True, text=True)
            if completed.returncode:
                raise RuntimeError(completed.stderr)
            results.append(json.loads(completed.stdout))
    artifact = {"label": args.label, "surface": "offscreen QWidget dialog",
                "memory": "process RSS delta after input rows are allocated; fresh process per size",
                "results": results}
    text = json.dumps(artifact, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
