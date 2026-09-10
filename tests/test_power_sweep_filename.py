import unittest

from ui.power_sweep_panel import PowerSweepPanel


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
        panel._smu.device._gates = (3.0, 4.0)
        panel._smu.device.read_current_gates = lambda: panel._smu.device._gates
        panel._smu.device.read_current_bias = lambda: 0.5
        self.assertEqual(panel._filename_gate_values(), (3.0, 4.0, 0.5))


if __name__ == "__main__":
    unittest.main()
