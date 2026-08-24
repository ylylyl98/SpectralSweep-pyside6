from __future__ import annotations

import unittest
import inspect
from types import SimpleNamespace

from lf6_automation import LF6Setup, SpectrometerSettings
from controllers.lf6_controller import _LF6Worker, LightFieldLifecycleState


class _Experiment:
    def __init__(self, *, writable_after: int | None = 0, frozen_attempts: int = 0,
                 wrapped: bool = False, unrelated: bool = False):
        self.probes = 0
        self.attempts = 0
        self.writes = []
        self.writable_after = writable_after
        self.frozen_attempts = int(frozen_attempts)
        self.wrapped = bool(wrapped)
        self.unrelated = bool(unrelated)

    def Exists(self, _setting):
        return True

    def IsWritable(self, _setting):
        self.probes += 1
        return self.writable_after is not None and self.probes > self.writable_after

    def SetValue(self, _setting, value):
        self.attempts += 1
        if self.attempts <= self.frozen_attempts:
            cause = InvalidOperationException(
                "Cannot modify a frozen setting. (Spectrometer.Grating.CenterWavelength)"
                if not self.unrelated else "other invalid operation"
            )
            if self.wrapped:
                raise TargetInvocationException(cause)
            raise cause
        self.writes.append(float(value))

    def GetValue(self, _setting):
        return self.writes[-1] if self.writes else None


def _setup(experiment: _Experiment, ready=lambda: True) -> LF6Setup:
    setup = object.__new__(LF6Setup)
    setup.application = SimpleNamespace(IsReady=ready)
    setup.experiment = experiment
    setup.change_spectra_center = lambda value: experiment.writes.append(float(value))
    return setup


class InvalidOperationException(RuntimeError):
    def __init__(self, message):
        super().__init__(message)
        self.Message = message


class TargetInvocationException(RuntimeError):
    def __init__(self, inner):
        super().__init__("Target invocation failed")
        self.InnerException = inner


class LightFieldReadinessTests(unittest.TestCase):
    def test_frozen_startup_is_retried_until_ready(self):
        calls = {"n": 0}

        def ready():
            calls["n"] += 1
            return calls["n"] >= 3

        experiment = _Experiment(writable_after=0)
        setup = _setup(experiment, ready)
        setup.set_center_wavelength_when_ready(730.0, timeout_s=0.2, poll_interval_s=0.001)
        self.assertEqual(experiment.writes, [730.0])
        self.assertGreaterEqual(calls["n"], 3)

    def test_configuration_proceeds_once_setting_is_writeable(self):
        experiment = _Experiment(writable_after=2)
        setup = _setup(experiment)
        setup.set_center_wavelength_when_ready(810.0, timeout_s=0.2, poll_interval_s=0.001)
        self.assertEqual(experiment.writes, [810.0])
        self.assertGreaterEqual(experiment.probes, 3)

    def test_permanent_frozen_setting_has_bounded_clear_error(self):
        experiment = _Experiment(writable_after=0, frozen_attempts=10_000, wrapped=True)
        setup = _setup(experiment)
        with self.assertRaises(TimeoutError) as raised:
            setup.set_center_wavelength_when_ready(810.0, timeout_s=0.02, poll_interval_s=0.001)
        self.assertIn("GratingCenterWavelength", str(raised.exception))
        self.assertIn("frozen", str(raised.exception))
        self.assertGreater(experiment.attempts, 1)
        self.assertEqual(setup.center_wavelength_write_stats["result"], "timeout")

    def test_actual_setvalue_frozen_then_success_retries_inner_exception(self):
        experiment = _Experiment(writable_after=0, frozen_attempts=2, wrapped=True)
        setup = _setup(experiment)
        setup.set_center_wavelength_when_ready(825.0, timeout_s=0.2, poll_interval_s=0.001)
        self.assertEqual(experiment.attempts, 3)
        self.assertEqual(experiment.writes, [825.0])

    def test_unrelated_invalid_operation_propagates_without_retry(self):
        experiment = _Experiment(writable_after=0, frozen_attempts=1, unrelated=True)
        setup = _setup(experiment)
        with self.assertRaises(InvalidOperationException):
            setup.set_center_wavelength_when_ready(825.0, timeout_s=0.2, poll_interval_s=0.001)
        self.assertEqual(experiment.attempts, 1)

    def test_successful_setvalue_is_authoritative_and_readback_is_recorded(self):
        experiment = _Experiment(writable_after=0)
        setup = _setup(experiment)
        setup.set_center_wavelength_when_ready(910.0, timeout_s=0.2, poll_interval_s=0.001)
        stats = setup.center_wavelength_write_stats
        self.assertEqual(stats["result"], "succeeded")
        self.assertEqual(stats["attempts"], 1)
        self.assertEqual(stats["readback"], 910.0)

    def test_state_is_reported_when_setting_never_available(self):
        experiment = _Experiment(writable_after=0)
        experiment.Exists = lambda _setting: False
        setup = _setup(experiment)
        with self.assertRaises(TimeoutError) as raised:
            setup.set_center_wavelength_when_ready(700.0, timeout_s=0.01, poll_interval_s=0.001)
        message = str(raised.exception)
        self.assertIn("SetValue attempts=0", message)
        self.assertIn("available", message)

    def test_busy_startup_state_is_waited_without_setvalue_attempt(self):
        experiment = _Experiment(writable_after=0)
        setup = _setup(experiment)
        polls = {"n": 0}

        def busy():
            polls["n"] += 1
            return polls["n"] < 3

        setup.application.IsBusy = busy
        setup.set_center_wavelength_when_ready(735.0, timeout_s=0.2, poll_interval_s=0.001)
        self.assertEqual(experiment.attempts, 1)
        self.assertEqual(experiment.writes, [735.0])

    def test_shared_controller_lifecycle_has_explicit_ordered_states(self):
        worker = _LF6Worker()
        observed = []
        worker.state_changed.connect(observed.append)
        for state in (
            LightFieldLifecycleState.STARTING,
            LightFieldLifecycleState.INITIALIZING,
            LightFieldLifecycleState.READY,
            LightFieldLifecycleState.DISCONNECTED,
        ):
            worker._transition(state)
        self.assertEqual(observed, [
            LightFieldLifecycleState.STARTING,
            LightFieldLifecycleState.INITIALIZING,
            LightFieldLifecycleState.READY,
            LightFieldLifecycleState.DISCONNECTED,
        ])

    def test_external_center_write_is_blocked_until_shared_controller_ready(self):
        experiment = _Experiment(writable_after=0)
        setup = _setup(experiment)
        worker = _LF6Worker()
        worker._setup = setup
        worker._state = LightFieldLifecycleState.INITIALIZING
        with self.assertRaises(RuntimeError):
            worker.set_center_wavelength_when_ready(700.0)
        self.assertEqual(experiment.attempts, 0)
        worker._state = LightFieldLifecycleState.READY
        worker.set_center_wavelength_when_ready(700.0)
        self.assertEqual(experiment.attempts, 1)

    def test_ready_gate_does_not_create_or_replace_a_lightfield_instance(self):
        worker = _LF6Worker()
        setup = object()
        adapter = object()
        worker._setup, worker._adapter = setup, adapter
        worker._state = LightFieldLifecycleState.INITIALIZING
        self.assertIs(worker._setup, setup)
        self.assertIs(worker._adapter, adapter)

    def test_startup_connect_path_contains_no_mutable_setting_writes(self):
        source = inspect.getsource(_LF6Worker.connect_instrument)
        self.assertNotIn("change_expose_time", source)
        self.assertNotIn("change_frame_to_combine", source)
        self.assertNotIn("set_center_wavelength_when_ready", source)

    def test_acquisition_configuration_is_deferred_to_explicit_preflight_surface(self):
        connect_source = inspect.getsource(_LF6Worker.connect_instrument)
        preflight_source = inspect.getsource(LF6Setup.configure_for_acquisition)
        self.assertIn("wait_until_ready", connect_source)
        self.assertIn("set_center_wavelength_when_ready", preflight_source)
        self.assertIn("change_expose_time", preflight_source)
        self.assertIn("change_frame_to_combine", preflight_source)

    def test_missing_explicit_ready_uses_capability_handshake(self):
        setup = object.__new__(LF6Setup)
        setup.application = SimpleNamespace()
        setup.experiment = _Experiment(writable_after=0)
        self.assertIsNone(setup.readiness_evidence)
        self.assertTrue(setup.is_ready)
        self.assertIn("capability handshake", setup.readiness_snapshot["reason"])

    def test_explicit_not_ready_still_blocks_capability_handshake(self):
        setup = object.__new__(LF6Setup)
        setup.application = SimpleNamespace(IsReady=False)
        setup.experiment = _Experiment(writable_after=0)
        self.assertFalse(setup.is_ready)
        self.assertIn("explicit", setup.readiness_snapshot["reason"])

    def test_worker_requires_stable_capability_readiness(self):
        snapshots = iter([
            {"ready": True, "busy": False},
            {"ready": False, "busy": True},
            {"ready": True, "busy": False},
            {"ready": True, "busy": False},
            {"ready": True, "busy": False},
        ])

        class Setup:
            @property
            def readiness_snapshot(self):
                return next(snapshots)

        worker = _LF6Worker()
        worker._setup = Setup()
        worker.wait_until_ready(timeout_s=0.2, poll_interval_s=0.001)


if __name__ == "__main__":
    unittest.main()
