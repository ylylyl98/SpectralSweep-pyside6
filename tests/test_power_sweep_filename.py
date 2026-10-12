import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ui.power_sweep_panel import PowerSweepPanel, _PowerSweepWorker, _build_gate_token


class _Check:
    def __init__(self, checked):
        self.checked = checked

    def isChecked(self):
        return self.checked


class _Spin:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _SMU:
    is_connected = True

    class _Device:
        def read_current_gates(self):
            return (0.0, 0.0)

        def read_current_bias(self):
            return 0.0

    device = _Device()


class PowerSweepFilenameTests(unittest.TestCase):
    def _panel(self, apply):
        panel = PowerSweepPanel.__new__(PowerSweepPanel)
        panel._apply_gates_chk = _Check(apply)
        panel._vbg_spin = _Spin(1.25)
        panel._vtg_spin = _Spin(-2.5)
        panel._vbias_spin = _Spin(0.15)
        panel._smu = _SMU()
        return panel

    def test_preview_source_uses_pending_gate_targets(self):
        panel = self._panel(True)
        self.assertEqual(panel._filename_gate_values(), (1.25, -2.5, 0.15))

    def test_external_gate_mode_uses_readback(self):
        panel = self._panel(False)
        panel._smu.latest_voltage_readback = {"Vbg": 3., "Vtg": 4., "Vbias": .5}
        def no_io():
            raise AssertionError("Filename preview contacted hardware")
        panel._smu.device.read_current_gates = no_io
        panel._smu.device.read_current_bias = no_io
        self.assertEqual(panel._filename_gate_values(), (3.0, 4.0, 0.5))

    def test_unknown_external_readback_is_not_reported_as_zero(self):
        panel = self._panel(False)
        self.assertEqual(panel._filename_gate_values(), (None, None, None))

    def test_optional_bias_or_unknown_roles_are_omitted_from_gate_token(self):
        self.assertEqual(_build_gate_token(3., 4., None), _build_gate_token(3., 4., float("nan")))
        self.assertEqual(_build_gate_token(None, None, None), "")

    def test_run_filename_uses_fresh_external_readback_in_worker(self):
        params = {"base_name": "old_cached_name", "external_gate_filename_base": "actual",
                  "apply_gates": False, "motion_axes": []}
        worker = _PowerSweepWorker(params, None, None, None, None, None)
        worker._smu = SimpleNamespace(is_connected=True, device=object())
        with patch("ui.power_sweep_panel._read_gates", return_value=(3., 4.)) as gates, patch("ui.power_sweep_panel._read_bias", return_value=.5), patch.object(worker, "_run_motion_axes"):
            worker._run_sweep(params)
        self.assertEqual(params["base_name"], "actual_" + _build_gate_token(3., 4., .5))
        self.assertEqual(gates.call_count, 1)


if __name__ == "__main__":
    unittest.main()
