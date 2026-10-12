import unittest
from PySide6.QtCore import QObject
from controllers.smu_controller import SMUController


class SMUReadbackCacheTests(unittest.TestCase):
    def test_partial_updates_copy_and_connection_changes_invalidate_readback(self):
        controller = SMUController.__new__(SMUController)
        QObject.__init__(controller)
        controller._voltage_readback = {}
        controller._applied_limit_addresses = set()
        controller._on_readings_ready({"Vbg_meas": 1.2, "Vtg_meas": -2., "Vbias_meas": .1})
        controller._on_readings_ready({"Vbg_meas": 1.3})
        self.assertEqual(controller.latest_voltage_readback, {"Vbg": 1.3, "Vtg": -2., "Vbias": .1})
        snapshot = controller.latest_voltage_readback
        snapshot["Vbg"] = 10
        self.assertEqual(controller.latest_voltage_readback["Vbg"], 1.3)
        controller._on_readings_ready({"Vbg_meas": float("nan"), "Vtg_meas": None})
        self.assertIsNone(controller.latest_voltage_readback["Vbg"])
        self.assertIsNone(controller.latest_voltage_readback["Vtg"])
        controller.disconnect_instrument()
        self.assertEqual(controller.latest_voltage_readback, {})
        controller._on_readings_ready({"Vbg_meas": 5})
        controller.connect_instrument([], {})
        self.assertEqual(controller.latest_voltage_readback, {})


if __name__ == "__main__":
    unittest.main()
