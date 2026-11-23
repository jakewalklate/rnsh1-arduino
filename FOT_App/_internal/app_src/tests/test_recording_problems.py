"""A recording that produces no data must be reported, not shown as an empty graph."""

import unittest
from unittest.mock import patch

from backend.device import measurement_acquisition
from backend.device.measurement_acquisition import MeasurementSerialRecorder


class FakeClient:
    def __init__(self, notices=None):
        self.notices = list(notices or [])

    def read_available(self):
        return []

    def drain_notices(self):
        notices, self.notices = self.notices, []
        return notices


def recorder(client):
    rec = MeasurementSerialRecorder(client, "MEAS_TEST", save_to_csv=False)
    rec.is_recording = True
    rec.started_at = 0.0
    return rec


class RecordingProblemTests(unittest.TestCase):
    def test_no_problem_right_after_start(self):
        rec = recorder(FakeClient())
        with patch.object(measurement_acquisition, "monotonic", return_value=1.0):
            self.assertIsNone(rec.get_problem())

    def test_stalled_stream_is_reported(self):
        rec = recorder(FakeClient())
        with patch.object(measurement_acquisition, "monotonic", return_value=5.0):
            self.assertIn("not sending any data", rec.get_problem())

    def test_firmware_error_notice_is_reported(self):
        client = FakeClient(["# STARTED", "# ERROR: no hardware timer channel available"])
        rec = recorder(client)
        with patch.object(measurement_acquisition, "monotonic", return_value=1.0):
            rec.poll()
            self.assertIn("no hardware timer channel", rec.get_problem())

    def test_connection_failure_is_reported(self):
        rec = recorder(FakeClient())
        rec.last_error = OSError("[Errno 6] Device not configured")
        self.assertIn("Device not configured", rec.get_problem())


if __name__ == "__main__":
    unittest.main()
