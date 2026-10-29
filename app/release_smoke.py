"""Exercise the packaged resources and internal worker in a disposable profile."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import traceback


def run(output: Path) -> int:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        from app.subprocess_command import module_command
        resource_root = Path(__file__).resolve().parents[1]
        worker_cwd = output if getattr(sys, "frozen", False) else resource_root
        manifests = []
        for filename in ("winusb_ingaas_runtime.json", "winusb_persistent_runtime.json", "winusb_portable_runtime.json"):
            data = json.loads((resource_root / "app/devices" / filename).read_text(encoding="utf-8"))
            if not data.get("runtime_files") or not data.get("release"):
                raise RuntimeError(f"Invalid packaged WinUSB manifest: {filename}")
            manifests.append(filename)
        # A protocol error response proves pipes/entry routing work in the console
        # helper without depending on Python being installed on the target PC.
        worker = subprocess.run(
            module_command("app.history_reader"), input='{"operation":"smoke-probe"}',
            text=True, encoding="utf-8", capture_output=True, timeout=45,
            cwd=worker_cwd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if worker.returncode or json.loads(worker.stdout).get("error") != "Unknown history operation":
            raise RuntimeError(f"Worker protocol probe failed: {worker.returncode}: {worker.stderr}")
        watchdog = subprocess.run(module_command("app.process_watchdog", "--help"),
                                  capture_output=True, text=True, encoding="utf-8", timeout=30,
                                  cwd=worker_cwd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if watchdog.returncode or "--state" not in watchdog.stdout:
            raise RuntimeError(f"Packaged watchdog entry failed: {watchdog.stderr}")
        from tools.verify_quick_workspaces import audit
        report = audit(output, sizes=((1400, 900), (1024, 768)), backend="software")
        # UI refresh compiles source instead of importing from the PYZ archive.
        from ui.page_reload import PAGE_RELOAD_REGISTRY, _fresh_page_modules
        reloaded = []
        for page, spec in PAGE_RELOAD_REGISTRY.items():
            with _fresh_page_modules(spec.modules, spec.class_name) as page_class:
                if not callable(page_class):
                    raise RuntimeError(f"Packaged reload did not produce a page class: {page}")
            reloaded.append(page)
        passed = (report["pages"] == 9 and not report["qml_warnings"]
                  and not report["python_callback_errors"]
                  and not report["hardware_import_attempts"]
                  and report["all_pages_switched_and_closed"]
                  and report["requested_sizes_honored"] and report["all_layouts_stable"]
                  and report["all_visible_bounds_clear"]
                  and report["all_horizontal_ranges_zero"] and report["all_tab_headers_unpaged"])
        (output / "smoke-result.json").write_text(json.dumps({
            "passed": passed, "worker_protocol": True, "watchdog_entry": True,
            "pages": report["pages"], "reload_modules": reloaded, "winusb_manifests": manifests,
            "hardware_validated": False,
        }, indent=2), encoding="utf-8")
        return 0 if passed else 1
    except Exception:
        (output / "smoke-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return 1
