from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.devices import esp300_shared
from app.devices.rotation_eps300_adapter import NewportEPS300
from app.devices.rotation_esp300_shared_adapter import SharedESP300Rotation


# Captured read-only from the user's ESP301 USB COM8 after signed-driver
# installation on 2026-10-27. Serial identifiers are normalized. Both recorded
# firmware versions return ZU=0H despite a real stage; version whitelisting
# would suppress a working controller as soon as a different version is used.
ESP301_111_LIVE_RESPONSES = {
    "VE?": "ESP301 Version 1.1.1 16/01/14",
    "ZU": "0H",
    "1QM?": "1",
    "1ID?": "SR50CC, SNTESTESP301",
    "2QM?": "0",
    "2ID?": "Unknown",
    "3QM?": "0",
    "3ID?": "Unknown",
}

ESP300_309_LIVE_RESPONSES = {
    "VE?": "ESP300 Version 3.09 05/04/05",
    "ZU": "0H",
    "1QM?": "1",
    "1ID?": "URS75CC, SNTESTESP300",
    "2QM?": "1",
    "2ID?": "Unknown",
    "3QM?": "3",
    "3ID?": "Unknown",
}


class FakeInstrument:
    def __init__(self, responses):
        self.responses = responses
        self.commands = []
        self.closed = False

    def write(self, command):
        self.commands.append(command)

    def read(self):
        value = self.responses.get(self.commands[-1], RuntimeError("unsupported query"))
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class FakeResourceManager:
    def __init__(self, instrument):
        self.instrument = instrument
        self.opened = []

    def open_resource(self, resource):
        self.opened.append(resource)
        return self.instrument


class NewportDiscoveryTests(unittest.TestCase):
    def make_controller(self, responses, **kwargs):
        instrument = FakeInstrument(responses)
        manager = FakeResourceManager(instrument)
        with patch("app.devices.rotation_eps300_adapter.time.sleep"):
            controller = NewportEPS300("ASRL42::INSTR", rm=manager, **kwargs)
        return controller, instrument

    def test_model_serial_defaults_and_explicit_rs232_override(self):
        for options, expected in (({}, 19200), ({"model": "esp301"}, 921600),
                                  ({"model": "esp301", "baud": 19200}, 19200)):
            with self.subTest(options=options):
                _, instrument = self.make_controller({}, **options)
                self.assertEqual(instrument.baud_rate, expected)
                self.assertEqual(instrument.write_termination, "\r")
                self.assertEqual(instrument.read_termination, "\r\n")
                self.assertEqual(instrument.commands, [])

    def test_invalid_settings_fail_before_open(self):
        instrument = FakeInstrument({})
        manager = FakeResourceManager(instrument)
        for options in ({"model": "other"}, {"baud": 0}, {"baud": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                NewportEPS300("ASRL42::INSTR", rm=manager, **options)
        self.assertEqual(manager.opened, [])

    def test_failed_baud_configuration_closes_resource_and_is_reported(self):
        class UnsupportedBaud(FakeInstrument):
            @property
            def baud_rate(self):
                return 19200

            @baud_rate.setter
            def baud_rate(self, value):
                raise ValueError("baud not supported")

        instrument = UnsupportedBaud({})
        with self.assertRaisesRegex(ValueError, "baud not supported"):
            NewportEPS300("ASRL42::INSTR", rm=FakeResourceManager(instrument), model="esp301")
        self.assertTrue(instrument.closed)
        self.assertEqual(instrument.commands, [])

    def test_native_identity_command(self):
        controller, instrument = self.make_controller({"VE?": "VE?\r\nESP301 Version 3.0\r\n"})
        self.assertEqual(controller.idn(), "ESP301 Version 3.0")
        self.assertEqual(instrument.commands, ["VE?"])

    def test_zu_detects_actual_stage_and_driver_intersection(self):
        for reply, expected in (("101H", [1]), ("707H", [1, 2, 3]),
                                ("ZU\r\n505H", [1, 3]), ("7H", []),
                                ("700H", []), ("0H", []), ("0x202", [2])):
            with self.subTest(reply=reply):
                controller, instrument = self.make_controller({"ZU": reply}, axis=3)
                self.assertEqual(controller.get_axes(), expected)
                self.assertEqual(instrument.commands, ["ZU", "VE?"] if reply == "0H" else ["ZU"])

    def test_scan_bounds_axis_range_and_can_inspect_driver_slots(self):
        controller, instrument = self.make_controller({"ZU": "3F3FH"})
        self.assertEqual(controller.get_axes(max_axes=99), [1, 2, 3])
        self.assertEqual(controller.get_axes(max_axes=1), [1])
        self.assertEqual(controller.get_axes(conservative=False, max_axes=2), [1, 2])
        with self.assertRaises(ValueError):
            controller.get_axes(max_axes=0)
        self.assertEqual(instrument.commands, ["ZU", "ZU", "ZU"])

    def test_captured_esp301_111_zero_bitmap_discovers_real_axis(self):
        controller, instrument = self.make_controller(ESP301_111_LIVE_RESPONSES, model="esp301")
        # No caller identity query is needed (including the rotation constructor).
        self.assertEqual(controller.get_axes(), [1])
        self.assertEqual(instrument.commands, ["ZU", "VE?", "1QM?", "1ID?", "2QM?", "3QM?"])
        self.assertEqual(controller.get_axes(conservative=False), [1])
        self.assertEqual(instrument.commands.count("VE?"), 1)
        self.assertTrue(all(command in ESP301_111_LIVE_RESPONSES for command in instrument.commands))

    def test_captured_esp300_309_zero_bitmap_requires_both_motor_and_stage(self):
        controller, instrument = self.make_controller(ESP300_309_LIVE_RESPONSES, model="esp300")
        self.assertEqual(controller.get_axes(), [1])
        self.assertEqual(instrument.commands, [
            "ZU", "VE?", "1QM?", "1ID?", "2QM?", "2ID?", "3QM?", "3ID?",
        ])
        self.assertEqual(controller.get_axes(conservative=False), [1])
        self.assertEqual(instrument.commands.count("VE?"), 1)
        self.assertTrue(all(command in ESP300_309_LIVE_RESPONSES for command in instrument.commands))

    def test_verified_model_identity_does_not_depend_on_version_whitelist(self):
        for model, identity in (
            ("esp301", "ESP301 Version 1.1.0 16/01/14"),
            ("esp301", "ESP301 Version 1.1.10 16/01/14"),
            ("esp301", "ESP301 Version 1.2.0 16/01/14"),
            ("esp301", "ESP301 Version 3.0"),
            ("esp300", "ESP300 Version 3.10 05/04/05"),
        ):
            with self.subTest(model=model, identity=identity):
                responses = dict(ESP301_111_LIVE_RESPONSES, **{"VE?": identity})
                controller, _ = self.make_controller(responses, model=model)
                self.assertEqual(controller.get_axes(), [1])

    def test_explicit_identity_is_cached_for_legacy_zero_bitmap(self):
        controller, instrument = self.make_controller(ESP301_111_LIVE_RESPONSES, model="esp301")
        self.assertEqual(controller.idn(), ESP301_111_LIVE_RESPONSES["VE?"])
        self.assertEqual(controller.get_axes(), [1])
        self.assertEqual(instrument.commands.count("VE?"), 1)

    def test_verified_native_empty_controller_stays_empty(self):
        for model, fixture in (("esp300", ESP300_309_LIVE_RESPONSES),
                               ("esp301", ESP301_111_LIVE_RESPONSES)):
            with self.subTest(model=model):
                responses = dict(fixture, **{"1QM?": "0", "2QM?": "0", "3QM?": "0"})
                controller, instrument = self.make_controller(responses, model=model)
                self.assertEqual(controller.get_axes(), [])
                self.assertEqual(instrument.commands, ["ZU", "VE?", "1QM?", "2QM?", "3QM?"])

    def test_zero_bitmap_requires_native_identity_matching_configured_model(self):
        for identity in (
            "UNKNOWN", "", "ESP300 Version 1.1.1 16/01/14",
            "ESP301 Version 1.1.1junk", "ESP301", "ESP301 Version UNKNOWN",
            "other controller", "ERROR ESP301 Version 1.1.1 16/01/14",
            TimeoutError("identity unavailable"),
        ):
            with self.subTest(identity=identity):
                responses = dict(ESP301_111_LIVE_RESPONSES, **{"VE?": identity})
                controller, instrument = self.make_controller(responses, model="esp301")
                self.assertEqual(controller.get_axes(), [])
                self.assertEqual(instrument.commands, ["ZU", "VE?"])

        controller, instrument = self.make_controller(ESP301_111_LIVE_RESPONSES, model="esp300")
        self.assertEqual(controller.get_axes(), [])
        self.assertEqual(instrument.commands, ["ZU", "VE?"])

    def test_nonzero_mask_does_not_query_identity_or_invoke_legacy_scan(self):
        responses = dict(ESP301_111_LIVE_RESPONSES, ZU="202H")
        controller, instrument = self.make_controller(responses, model="esp301")
        self.assertEqual(controller.get_axes(), [2])
        self.assertEqual(instrument.commands, ["ZU"])

    def test_legacy_zero_bitmap_still_rejects_unidentified_stage(self):
        for stage in ("Unknown", "NO STAGE", "1ID?", "0", "ERROR", "nan"):
            with self.subTest(stage=stage):
                responses = dict(ESP301_111_LIVE_RESPONSES, **{"1ID?": stage})
                controller, _ = self.make_controller(responses, model="esp301")
                self.assertEqual(controller.get_axes(), [])

    def test_legacy_scan_uses_only_read_queries_and_preserves_stage_names(self):
        controller, instrument = self.make_controller({
            "ZU": TimeoutError("ZU unavailable"),
            "1QM?": "1QM?1", "1ID?": "1ID?PR50CC, SN123",
            "2QM?": "0", "3QM?": "3", "3ID?": "URM100, SN456",
        })
        self.assertEqual(controller.get_axes(), [1, 3])
        self.assertEqual(instrument.commands, ["ZU", "1QM?", "1ID?", "2QM?", "3QM?", "3ID?"])

    def test_legacy_rejects_absence_error_numeric_or_echo_ids(self):
        for reply in ("", "UNKNOWN", "Unconfigured", "NO STAGE", "NO MOTOR",
                      "NONE", "NULL", "N/A", "108, MOTOR / STAGE NOT CONNECTED",
                      "9", "0", "?", "1ID?", "ERROR", "2ID?URM100", "nan", "inf",
                      "AXIS NUMBER OUT OF RANGE", "AXIS NOT AVAILABLE", "UNDEFINED"):
            with self.subTest(reply=reply):
                controller, instrument = self.make_controller({
                    "ZU": "invalid", "1QM?": "1", "1ID?": reply,
                    "2QM?": "0", "3QM?": "0",
                }, axis=1)
                self.assertEqual(controller.get_axes(), [])
                self.assertLessEqual(len(instrument.commands), 7)

    def test_legacy_rejects_invalid_motor_enum(self):
        for reply in ("nan", "inf", "-inf", "1.5", "1.0", "9", "-1", "0", "1QM?"):
            with self.subTest(reply=reply):
                controller, instrument = self.make_controller({
                    "ZU": "bad response", "1QM?": reply,
                    "1ID?": "URM100", "2QM?": "0", "3QM?": "0",
                })
                self.assertEqual(controller.get_axes(), [])
                self.assertNotIn("1ID?", instrument.commands)

    def test_failed_all_queries_never_invents_configured_axis(self):
        controller, instrument = self.make_controller({}, axis=3)
        self.assertEqual(controller.get_axes(), [])
        self.assertEqual(instrument.commands, ["ZU", "1QM?", "2QM?", "3QM?"])


class RecordingController:
    connection_settings = NewportEPS300.connection_settings
    instances = []

    def __init__(self, resource, *, axis, model, baud):
        self.options = (resource, axis, model, baud)
        self.closed = False
        self.axes = [1]
        self.calls = []
        self.instances.append(self)

    def get_axes(self, conservative=True):
        self.calls.append("scan")
        if isinstance(self.axes, Exception):
            raise self.axes
        return self.axes

    def apply_fast_safe_motion_profile(self, **kwargs):
        self.calls.append(("profile", kwargs["axis"]))
        return SimpleNamespace(axis=kwargs["axis"])

    def motor_on(self, *, axis):
        self.calls.append(("motor_on", axis))

    def close(self):
        self.closed = True


class NewportSharedSessionTests(unittest.TestCase):
    def setUp(self):
        self.entries = patch.object(esp300_shared, "_ENTRIES", {})
        self.factory = patch.object(esp300_shared, "NewportEPS300", RecordingController)
        self.entries.start()
        self.factory.start()
        RecordingController.instances = []

    def tearDown(self):
        self.factory.stop()
        self.entries.stop()

    def test_temporary_scan_closes_and_empty_scan_stays_empty(self):
        with patch.object(RecordingController, "get_axes", return_value=[]):
            self.assertEqual(esp300_shared.scan_shared_esp300_axes(
                "ASRL42::INSTR", model="esp301", default_axis=3), [])
        controller = RecordingController.instances[0]
        self.assertEqual(controller.options, ("ASRL42::INSTR", 1, "esp301", 921600))
        self.assertTrue(controller.closed)
        self.assertEqual(esp300_shared._ENTRIES, {})

    def test_scan_releases_only_its_own_reference(self):
        controller = esp300_shared.acquire_shared_esp300("ASRL42::INSTR", model="esp301")
        self.assertEqual(esp300_shared.scan_shared_esp300_axes("ASRL42::INSTR", model="esp301"), [1])
        self.assertFalse(controller.closed)
        self.assertEqual(esp300_shared._ENTRIES["ASRL42::INSTR"].refcount, 1)
        self.assertEqual(controller.calls, ["scan"])
        esp300_shared.release_shared_esp300("ASRL42::INSTR")
        self.assertTrue(controller.closed)

    def test_failed_scan_keeps_an_active_session(self):
        controller = esp300_shared.acquire_shared_esp300("ASRL42::INSTR")
        controller.axes = RuntimeError("scan failed")
        with self.assertRaisesRegex(RuntimeError, "scan failed"):
            esp300_shared.scan_shared_esp300_axes("ASRL42::INSTR")
        self.assertFalse(controller.closed)
        self.assertEqual(esp300_shared._ENTRIES["ASRL42::INSTR"].refcount, 1)

    def test_failed_temporary_scan_closes_its_session(self):
        with patch.object(RecordingController, "get_axes", side_effect=RuntimeError("scan failed")):
            with self.assertRaisesRegex(RuntimeError, "scan failed"):
                esp300_shared.scan_shared_esp300_axes("ASRL42::INSTR")
        self.assertTrue(RecordingController.instances[0].closed)
        self.assertEqual(esp300_shared._ENTRIES, {})

    def test_conflicting_serial_baud_does_not_increment_reference(self):
        controller = esp300_shared.acquire_shared_esp300("ASRL42::INSTR", model="esp301")
        with self.assertRaisesRegex(ValueError, "already open"):
            esp300_shared.acquire_shared_esp300("ASRL42::INSTR")
        self.assertEqual(len(RecordingController.instances), 1)
        self.assertEqual(esp300_shared._ENTRIES["ASRL42::INSTR"].refcount, 1)
        self.assertFalse(controller.closed)

    def test_explicit_rs232_baud_can_share_with_legacy_stage(self):
        first = esp300_shared.acquire_shared_esp300("ASRL42::INSTR", model="esp301", baud=19200)
        self.assertIs(first, esp300_shared.acquire_shared_esp300("ASRL42::INSTR"))
        self.assertEqual(esp300_shared._ENTRIES["ASRL42::INSTR"].refcount, 2)

    def test_adapter_validates_then_applies_profile_and_enables_selected_axis(self):
        adapter = SharedESP300Rotation("ASRL42::INSTR", model="esp301")
        controller = RecordingController.instances[0]
        self.assertEqual(adapter.backend_key, "esp301")
        self.assertEqual(adapter.display_name, "Newport ESP301")
        self.assertEqual(controller.calls, ["scan", ("profile", 1), ("motor_on", 1)])
        # Another role's reference survives closing this adapter twice.
        esp300_shared.acquire_shared_esp300("ASRL42::INSTR", model="esp301")
        adapter.close()
        adapter.close()
        self.assertFalse(controller.closed)
        self.assertEqual(esp300_shared._ENTRIES["ASRL42::INSTR"].refcount, 1)

    def test_missing_axis_rejected_before_any_profile_or_enable(self):
        with self.assertRaisesRegex(ValueError, "axis 3 was not detected"):
            SharedESP300Rotation("ASRL42::INSTR", axis=3, model="esp301")
        controller = RecordingController.instances[0]
        self.assertEqual(controller.calls, ["scan"])
        self.assertTrue(controller.closed)
        self.assertEqual(esp300_shared._ENTRIES, {})


if __name__ == "__main__":
    unittest.main()
