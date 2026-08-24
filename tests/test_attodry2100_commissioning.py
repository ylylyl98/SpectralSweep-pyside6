import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app.engine.attodry2100_commissioning import AttoDRY2100CommissioningEvidence


def snapshot(field=0.0):
    return SimpleNamespace(
        field_t=field,
        setpoint_t=field + 0.01,
        temperature_k=4.0,
        status=SimpleNamespace(
            field_control_state="IDLE",
            driven_mode=True,
            persistent_mode=False,
            heater_on=False,
            leads_hot=False,
            quench=False,
            backend_details={"field_control": True},
        ),
        lead_field_t=0.0,
    )


class FakeController:
    def __init__(self, *, stop_error=None, verify_error=None, verify_after_reads=1,
                 cleanup_error=False):
        self.stop_error = stop_error
        self.verify_error = verify_error
        self.verify_after_reads = verify_after_reads
        self.cleanup_error = cleanup_error
        self.reads = 0
        self.calls = []

    def read_snapshot(self):
        self.calls.append("read_snapshot")
        self.reads += 1
        if self.reads > self.verify_after_reads and self.verify_error is not None:
            raise self.verify_error
        return snapshot(self.reads)

    def stop_field_control(self):
        self.calls.append("stop_field_control")
        if self.stop_error is not None:
            raise self.stop_error
        return {"vendor_ack": True}

    def disconnect_async(self):
        self.calls.append("disconnect_async")
        if self.cleanup_error:
            raise RuntimeError("disconnect failed")
        return True

    def shutdown(self):
        self.calls.append("shutdown")
        if self.cleanup_error:
            raise RuntimeError("shutdown failed")
        return True


class CommissioningEvidenceTests(unittest.TestCase):
    def test_stop_ack_succeeds_with_idle_and_field_control_true_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FakeController()
            evidence = AttoDRY2100CommissioningEvidence(
                controller, Path(tmp) / "commissioning.json"
            )
            acknowledgement, observation = evidence.stop_and_observe()
            self.assertEqual(acknowledgement, {"vendor_ack": True})
            self.assertEqual(observation.status.field_control_state, "IDLE")
            self.assertIs(observation.status.backend_details["field_control"], True)
            events = json.loads(evidence.recorder.path.read_text())['events']
            self.assertEqual(
                [event["event"] for event in events],
                ["commissioning_started", "stop_requested", "stop_ack",
                 "stop_verification_observation"],
            )

    def test_vendor_stop_error_fails_and_is_checkpointed(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = AttoDRY2100CommissioningEvidence(
                FakeController(stop_error=RuntimeError("vendor stop failed")),
                Path(tmp) / "commissioning.json",
            )
            with self.assertRaisesRegex(RuntimeError, "vendor stop failed"):
                evidence.stop_and_observe()
            events = json.loads(evidence.recorder.path.read_text())['events']
            self.assertEqual([event["event"] for event in events],
                             ["commissioning_started", "stop_requested", "stop_error"])

    def test_prior_evidence_survives_stop_verification_and_cleanup_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "commissioning.json"
            controller = FakeController(
                verify_error=RuntimeError("post-stop read failed"), verify_after_reads=2,
                cleanup_error=True
            )
            evidence = AttoDRY2100CommissioningEvidence(controller, path)
            evidence.snapshot("before_stop")
            evidence.snapshot("stability_sample")
            evidence.stabilization(0.01, stable=True, snapshot=snapshot(0.01))
            with self.assertRaisesRegex(RuntimeError, "post-stop read failed"):
                evidence.stop_and_observe()
            with self.assertRaisesRegex(RuntimeError, "disconnect failed"):
                evidence.cleanup()
            data = json.loads(path.read_text())
            names = [event["event"] for event in data["events"]]
            self.assertEqual(names[:6], [
                "commissioning_started", "snapshot", "snapshot",
                "stabilization_decision", "stop_requested", "stop_ack",
            ])
            self.assertIn("stop_verification_error", names)
            self.assertIn("disconnect_error", names)
            self.assertIn("shutdown_error", names)
            self.assertIn("cleanup_error", names)
            samples = [event for event in data["events"] if event["event"] == "snapshot"]
            self.assertEqual([sample["label"] for sample in samples],
                             ["before_stop", "stability_sample"])
            self.assertEqual([sample["sequence"] for sample in samples], [2, 3])
            timestamps = [datetime.fromisoformat(sample["timestamp_utc"]) for sample in samples]
            self.assertLessEqual(timestamps[0], timestamps[1])
            for sample in samples:
                payload = sample["snapshot"]
                self.assertIn("field_t", payload)
                self.assertIn("setpoint_t", payload)
                self.assertIn("temperature_k", payload)
                self.assertIn("lead_field_t", payload)
                status = payload["status"]
                self.assertEqual(status["field_control_state"], "IDLE")
                self.assertIs(status["driven_mode"], True)
                self.assertIs(status["persistent_mode"], False)
                self.assertIs(status["quench"], False)
                self.assertIs(status["heater_on"], False)
                self.assertIs(status["leads_hot"], False)
                self.assertIs(status["backend_details"]["field_control"], True)


if __name__ == "__main__":
    unittest.main()
