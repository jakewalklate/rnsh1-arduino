import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.device.calibration_recorder import CalibrationPointRecorder
from backend.device.serial_device import ArduinoSerialClient
from backend.processing.FOTCalibrationPresets import PresetStore
from backend.processing.FOTPhaseCalibration import (
    bias_check_reasons,
    failure_reasons,
)
from backend.processing.FOTTubeCalibration import compute_fit
from backend.services.calibration_service import CalibrationService


def calibration_points(count=11):
    points = []
    for index in range(count):
        flow = index / 10
        points.append(
            {
                "bias_flow": flow,
                "r_phase": 11.5 + 2.0 * flow + 0.1 * flow ** 2,
                "x_phase": 0.1 + 0.2 * flow,
            }
        )
    return points


class CalibrationWorkflowTests(unittest.TestCase):
    def test_order_ten_is_available_with_eleven_distinct_points(self):
        fit = compute_fit(calibration_points(), degree=10, x_degree=10)

        self.assertIsNotNone(fit)
        self.assertEqual(fit["degree"], 10)
        self.assertTrue(fit["validation"]["can_save"])
        self.assertTrue(fit["validation"]["warnings"])

    def test_high_order_rejects_insufficient_distinct_flow_values(self):
        points = calibration_points()
        for index, point in enumerate(points):
            point["bias_flow"] = float(index % 2)

        self.assertIsNone(compute_fit(points, degree=5, x_degree=5))

    def test_partial_preset_update_retains_unselected_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PresetStore(
                root / "presets.json",
                phase_path=root / "phase.json",
                bias_path=root / "bias.json",
            )
            store.record(
                "morning",
                {"correction_angle_deg": -3.0},
                {"scale": 1.1, "k_plumbing": 8.0},
            )

            class TestCalibrationService(CalibrationService):
                @property
                def presets(self):
                    return store

            service = TestCalibrationService(hardware_enabled=False)
            service.active_phase_calibration = lambda: {
                "correction_angle_deg": -4.0
            }
            service.active_calibration = lambda: {
                "scale": 1.2,
                "k_plumbing": 9.0,
            }

            service.store_current_as_preset(
                "morning",
                include_phase=True,
                include_bias=False,
            )
            current = store.load()["periods"]["morning"]["current"]

            self.assertEqual(current["phase"]["correction_angle_deg"], -4.0)
            self.assertEqual(current["bias"]["scale"], 1.1)

    def test_consecutive_captures_stop_streaming_and_reuse_healthy_port(self):
        class FakeClient:
            instances = []

            def __init__(self, **_kwargs):
                self.is_connected = False
                self.stop_calls = 0
                self.disconnect_calls = 0
                self.__class__.instances.append(self)

            def connect(self):
                self.is_connected = True

            def stop_streaming(self):
                self.stop_calls += 1

            def disconnect(self):
                self.disconnect_calls += 1
                self.is_connected = False

        class FakeRecorder:
            def __init__(self, _client, cpap_value, **_kwargs):
                self.cpap_value = cpap_value
                self.cancelled = False

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

            def snapshot(self):
                return {
                    "state": "done",
                    "point": {
                        "cpap": self.cpap_value,
                        "bias_flow": float(self.cpap_value),
                        "r_phase": 10.0,
                        "x_phase": 0.0,
                    },
                }

        with (
            patch(
                "backend.services.calibration_service.ArduinoSerialClient",
                FakeClient,
            ),
            patch(
                "backend.services.calibration_service.CalibrationPointRecorder",
                FakeRecorder,
            ),
        ):
            service = CalibrationService(hardware_enabled=True)
            service.port = "COM6"
            service.persist_sweep = lambda: None

            service.start_capture(4, 1)
            service.commit_captured_point()
            service.start_capture(5, 1)

        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(FakeClient.instances[0].stop_calls, 1)
        self.assertEqual(FakeClient.instances[0].disconnect_calls, 0)
        self.assertTrue(FakeClient.instances[0].is_connected)

    def test_failed_start_write_reconnects_automatically(self):
        class FakeClient:
            instances = []

            def __init__(self, **_kwargs):
                self.is_connected = False
                self.disconnect_calls = 0
                self.__class__.instances.append(self)

            def connect(self):
                self.is_connected = True

            def stop_streaming(self):
                pass

            def disconnect(self):
                self.disconnect_calls += 1
                self.is_connected = False

        class FakeRecorder:
            starts = 0

            def __init__(self, _client, **_kwargs):
                pass

            def start(self):
                self.__class__.starts += 1
                if self.starts == 1:
                    raise PermissionError("stale Windows serial handle")

            def cancel(self):
                pass

            def snapshot(self):
                return {"state": "settling"}

        with (
            patch(
                "backend.services.calibration_service.ArduinoSerialClient",
                FakeClient,
            ),
            patch(
                "backend.services.calibration_service.CalibrationPointRecorder",
                FakeRecorder,
            ),
            patch("backend.services.calibration_service.sleep"),
        ):
            service = CalibrationService(hardware_enabled=True)
            service.port = "COM6"
            recorder = service.start_capture(4, 1)

        self.assertIsNotNone(recorder)
        self.assertEqual(FakeRecorder.starts, 2)
        self.assertEqual(len(FakeClient.instances), 2)
        self.assertEqual(FakeClient.instances[0].disconnect_calls, 1)
        self.assertTrue(FakeClient.instances[1].is_connected)

    def test_capture_read_failure_reconnects_and_restarts_settling(self):
        class FakeClient:
            def __init__(self):
                self.disconnect_calls = 0
                self.connect_calls = 0
                self.reset_calls = 0
                self.start_calls = 0

            def disconnect(self):
                self.disconnect_calls += 1

            def connect(self):
                self.connect_calls += 1

            def reset_input_buffer(self):
                self.reset_calls += 1

            def configure(self, **_kwargs):
                pass

            def wait_for_frequency_ack(self, hz):
                return hz

            def start_streaming(self, **_kwargs):
                self.start_calls += 1

        client = FakeClient()
        recorder = CalibrationPointRecorder(client, cpap_value=6, repeats=1)
        recorder.state = "collecting"
        recorder._settle_seen = 3
        recorder._window_blocks = [object()]

        with patch("backend.device.calibration_recorder.sleep"):
            recovered = recorder._recover_serial(
                PermissionError("ClearCommError failed")
            )

        self.assertTrue(recovered)
        self.assertEqual(recorder.state, "settling")
        self.assertEqual(recorder._settle_seen, 0)
        self.assertEqual(recorder._window_blocks, [])
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(client.connect_calls, 1)
        self.assertEqual(client.reset_calls, 1)
        self.assertEqual(client.start_calls, 1)

    def test_start_acknowledgement_is_detected_before_capture(self):
        client = ArduinoSerialClient()
        notices = iter([[], ["# STARTED"]])
        client.read_available = lambda: []
        client.drain_notices = lambda: next(notices)

        client.wait_for_notice("# STARTED", timeout_seconds=1)

    def test_phase_verdict_ignores_the_bias_windows(self):
        """Rotation nulls X by construction, so the bias-corrected reactance restates
        the bias polynomial and says nothing about the angle. A bad bias fit must not
        read as a failed phase calibration."""
        result = {
            "quality_valid": True,
            "phase_pass": True,
            "correction_angle_deg": -0.332,
            "r_pass": True,
            "x_pass": False,
            "bias_corrected_resistance_cmH2O_s_per_L": 13.181,
            "bias_corrected_reactance_cmH2O_s_per_L": -1.418,
        }

        self.assertEqual(failure_reasons(result), [])
        self.assertEqual(len(bias_check_reasons(result)), 1)
        self.assertIn("reactance", bias_check_reasons(result)[0])

    def test_phase_verdict_still_fails_on_a_phase_problem(self):
        result = {
            "quality_valid": False,
            "quality_reason": "flow amplitude too low",
            "phase_pass": False,
            "correction_angle_deg": -42.0,
            "r_pass": True,
            "x_pass": True,
        }

        reasons = failure_reasons(result)
        self.assertEqual(len(reasons), 2)
        self.assertEqual(bias_check_reasons(result), [])

    def test_phase_capture_reports_the_raw_tier(self):
        """The phase step solves for the rotation angle, so its readout must be the
        unrotated phasor the angle is derived from -- not one already rotated by the
        angle being replaced."""
        recorder = CalibrationPointRecorder.__new__(CalibrationPointRecorder)
        result = SimpleNamespace(
            raw=complex(12.5, -0.8),
            corrected=complex(12.6, 0.0),
            bias_corrected=complex(13.6, 0.7),
        )

        for tier, expected in (
            ("raw", result.raw),
            ("corrected", result.corrected),
            ("bias_corrected", result.bias_corrected),
        ):
            recorder.tier = tier
            self.assertEqual(recorder._tier_impedance(result), expected)


if __name__ == "__main__":
    unittest.main()
