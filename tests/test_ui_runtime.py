"""Presentation admission uses fake fast executors, never hardware or SDKs."""
import threading
import time

import pytest
from PySide6.QtCore import QThread

from app.ui_runtime import CommandsBridge, RuntimeState
from tests.quick_helpers import quick_application, drain_qt_deletions


@pytest.fixture
def application():
    app = quick_application()
    yield app
    drain_qt_deletions()


@pytest.fixture
def runtime(application):
    state = RuntimeState()
    state.register_page("spectrum", "Spectrum", ("spectrometer",))
    state.register_page("motion", "Motion", ("spectrometer", "stage"))
    state.register_page("history", "History", ())
    return state


def flush(application, predicate=lambda: True):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        time.sleep(.001)
    raise AssertionError("Queued result did not arrive")


def test_resource_admission_is_atomic_even_for_reentrant_signal(runtime):
    nested = []
    page = runtime.workflow("spectrum")
    page.changed.connect(lambda: nested.append(runtime.try_start("motion"))
                         if page.busy else None)
    assert runtime.try_start("spectrum")
    assert nested and not any(nested)
    assert not runtime.try_start("spectrum")
    assert not runtime.workflow("motion").canStart
    assert "Spectrum" in runtime.workflow("motion").blockReason
    assert runtime.try_start("history")


def test_stop_and_metadata_hold_lease_until_explicit_drain(runtime):
    assert runtime.try_start("spectrum")
    assert runtime.request_stop("spectrum")
    assert not runtime.request_stop("spectrum")
    runtime.observe("spectrum", busy=False)
    assert runtime.workflow("spectrum").stopping
    assert not runtime.workflow("spectrum").drained
    assert not runtime.try_start("motion")
    runtime.observe("spectrum", metadata_finalizing=True)
    runtime.mark_drained("spectrum")
    assert not runtime.try_start("motion")
    assert not runtime.drained
    runtime.observe("spectrum", metadata_finalizing=False)
    assert runtime.drained
    assert not runtime.workflow("spectrum").stopping
    assert runtime.try_start("motion")


def test_backend_state_remains_truthful_if_legacy_operations_overlap(runtime):
    runtime.set_busy("spectrum", True)
    runtime.set_busy("motion", True)
    assert runtime.workflow("motion").busy
    runtime.finish("spectrum")
    assert not runtime.workflow("spectrum").canStart
    runtime.finish("motion")
    assert runtime.workflow("spectrum").canStart


def test_legacy_finished_and_backend_block_reason(runtime):
    runtime.observe("spectrum", block_reason="Camera recovery required")
    assert not runtime.try_start("spectrum")
    assert runtime.workflow("spectrum").blockReason == "Camera recovery required"
    runtime.observe("spectrum", block_reason="")
    runtime.set_busy("spectrum", True, "capturing")
    assert runtime.workflow("spectrum").phase == "capturing"
    runtime.set_busy("spectrum", False)
    assert runtime.workflow("spectrum").canStart


def test_state_properties_and_validation(runtime):
    runtime.sampleId = "sample-42"
    runtime.activePage = "history"
    assert runtime.sampleId == "sample-42"
    assert runtime.activePage == "history"
    assert [page.pageId for page in runtime.workflowStates] == ["spectrum", "motion", "history"]
    with pytest.raises(ValueError):
        runtime.selectPage("missing")
    with pytest.raises(ValueError):
        runtime.register_page("history")
    with pytest.raises(TypeError):
        runtime.register_page("bad", resources="camera")


def test_queued_cross_thread_commands_execute_on_qt_owner(runtime, application):
    bridge = CommandsBridge(runtime)
    calls = []
    bridge.register("spectrum", start=lambda: calls.append(QThread.currentThread()))
    caller = threading.Thread(target=lambda: bridge.start("spectrum"))
    caller.start()
    caller.join(2)
    assert not caller.is_alive()
    assert not calls  # request does not run the executor in the caller thread
    flush(application, lambda: bool(calls))
    assert calls == [application.thread()]
    assert runtime.workflow("spectrum").busy


def test_state_mutation_from_wrong_thread_is_rejected(runtime):
    failures = []

    def mutate():
        try:
            runtime.try_start("spectrum")
        except RuntimeError as exc:
            failures.append(str(exc))

    caller = threading.Thread(target=mutate)
    caller.start()
    caller.join(2)
    assert failures and not runtime.workflow("spectrum").busy


def test_duplicate_and_reentrant_command_do_not_start_second_executor(runtime, application):
    bridge = CommandsBridge(runtime)
    calls, rejected = [], []

    def start():
        calls.append("start")
        bridge.start("spectrum")

    bridge.register("spectrum", start=start)
    bridge.rejected.connect(lambda *args: rejected.append(args))
    bridge.start("spectrum")
    bridge.start("spectrum")
    flush(application, lambda: len(rejected) == 2)
    assert calls == ["start"]
    assert all(item[1] == "start" for item in rejected)


def test_declined_start_releases_reservation(runtime, application):
    bridge = CommandsBridge(runtime)
    bridge.register("spectrum", start=lambda: False)
    bridge.start("spectrum")
    flush(application)
    assert runtime.workflow("spectrum").canStart
    assert runtime.try_start("motion")


def test_callback_error_retains_reservation_until_backend_ack(runtime, application):
    bridge = CommandsBridge(runtime)

    def fail():
        raise RuntimeError("Native command acknowledgement lost")

    bridge.register("spectrum", start=fail)
    bridge.start("spectrum")
    flush(application)
    page = runtime.workflow("spectrum")
    assert page.busy and not page.drained
    assert "acknowledgement lost" in page.error
    assert not runtime.try_start("motion")
    runtime.finish("spectrum", error=page.error, phase="failed")
    assert runtime.try_start("motion")


def test_duplicate_stop_keeps_lock_and_calls_executor_once(runtime, application):
    bridge = CommandsBridge(runtime)
    calls = []
    bridge.register("spectrum", start=lambda: None, stop=lambda: calls.append("stop"))
    bridge.start("spectrum")
    flush(application)
    bridge.stop("spectrum")
    bridge.stop("spectrum")
    flush(application, lambda: bool(calls))
    assert calls == ["stop"]
    assert runtime.workflow("spectrum").stopping
    assert not runtime.try_start("motion")
    runtime.finish("spectrum")
    assert runtime.drained


def test_shutdown_gates_pending_commands_and_waits_for_real_drain(runtime, application):
    bridge = CommandsBridge(runtime)
    calls = []
    bridge.register("spectrum", start=lambda: calls.append("start"),
                    stop=lambda: calls.append("stop"))
    runtime.set_busy("spectrum", True)
    bridge.start("spectrum")
    assert not runtime.shutdown()
    assert not runtime.shutdown()
    assert runtime.closing
    flush(application, lambda: bool(calls))
    assert calls == ["stop"]
    assert not runtime.workflow("history").canStart
    runtime.mark_drained("spectrum")
    assert runtime.shutdown()


def test_detached_bridge_discards_already_queued_calls(runtime, application):
    bridge = CommandsBridge(runtime)
    calls = []
    bridge.register("spectrum", start=lambda: calls.append("start"))
    bridge.start("spectrum")
    bridge.close()
    flush(application)
    assert not calls
    assert runtime.drained


def test_delayed_stop_cannot_cancel_a_new_generation(runtime, application):
    bridge = CommandsBridge(runtime)
    calls, rejected = [], []
    bridge.register("spectrum", stop=lambda: calls.append("stop"))
    bridge.rejected.connect(lambda *args: rejected.append(args))
    assert runtime.try_start("spectrum")
    old_generation = runtime.workflow("spectrum").generation
    bridge.stop("spectrum")
    runtime.finish("spectrum")
    assert runtime.try_start("spectrum")
    assert runtime.workflow("spectrum").generation > old_generation
    flush(application, lambda: bool(rejected))
    assert not calls
    assert not runtime.workflow("spectrum").stopping
    assert rejected[0][2] == "Operation already changed"


def test_collecting_runtime_keeps_application_thread_and_nested_event_loop_alive():
    # A native binding lifetime regression must fail in its own process instead
    # of corrupting the thread used by all subsequent GUI tests.
    import os
    from pathlib import Path
    import subprocess
    import sys

    source = r'''
import gc, weakref
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid
from app.ui_runtime import RuntimeState
app = QApplication([])
owner = app.thread()
for _ in range(20):
    state = RuntimeState()
    state.register_page('spectrum')
    state.try_start('spectrum')
    state.finish('spectrum')
    released = weakref.ref(state)
    del state
    gc.collect()
    assert released() is None, 'The lifetime check must actually collect the runtime'
    assert isValid(owner), 'Collecting the runtime invalidated the Qt main thread'
    loop = QEventLoop()
    QTimer.singleShot(0, loop.quit)
    assert loop.exec() == 0
'''
    result = subprocess.run([sys.executable, "-c", source],
                            cwd=Path(__file__).resolve().parents[1],
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
