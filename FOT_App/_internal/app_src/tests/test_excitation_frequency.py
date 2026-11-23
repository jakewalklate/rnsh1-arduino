"""The excitation frequency chosen for a recording reaches the Arduino, is confirmed by
it, and is stored with the measurement."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.database import storage
from backend.device.arduino_controller import ArduinoController
from backend.device.serial_device import ArduinoSerialClient, ArduinoSerialError


def client_with_notices(*batches):
    client = ArduinoSerialClient()
    batches = iter(batches)
    client.read_available = lambda: []
    client.drain_notices = lambda: next(batches, [])
    return client


class FrequencyAckTests(unittest.TestCase):
    def test_confirmed_frequency_is_returned(self):
        client = client_with_notices(
            [],
            ["# excitation Hz set to: 5.000", "# sample Hz: 350.000"],
        )
        self.assertEqual(client.wait_for_frequency_ack(5, timeout_seconds=1), 5.0)

    def test_rejected_frequency_raises(self):
        client = client_with_notices(
            ["# CONFIG_REJECTED frequency allowed 5/6/11/19, got: 7", "# CONFIG_ACK"]
        )
        with self.assertRaisesRegex(ArduinoSerialError, "rejected"):
            client.wait_for_frequency_ack(7, timeout_seconds=1)

    def test_firmware_that_ignores_the_frequency_raises(self):
        # Older builds could not parse EXCITATION_HZ: they ACK but never set it.
        client = client_with_notices(["# CONFIG_ACK"])
        with self.assertRaisesRegex(ArduinoSerialError, "Upload the current firmware"):
            client.wait_for_frequency_ack(5, timeout_seconds=1)

    def test_no_reply_times_out(self):
        client = client_with_notices()
        with self.assertRaisesRegex(ArduinoSerialError, "did not confirm"):
            client.wait_for_frequency_ack(5, timeout_seconds=0.1)


class FakeClient:
    def __init__(self):
        self.commands = []
        self.is_connected = True

    def connect(self):
        pass

    def stop_streaming(self):
        self.commands.append("STOP")

    def reset_input_buffer(self):
        pass

    def configure(self, **settings):
        self.commands.append(("CONFIG", settings))

    def wait_for_frequency_ack(self, hz):
        return float(hz)


class ControllerTests(unittest.TestCase):
    def test_start_sends_chosen_frequency_and_matching_sample_rate(self):
        controller = ArduinoController(send_device_commands=True)
        controller.client = FakeClient()

        with patch("backend.device.arduino_controller.MeasurementSerialRecorder") as rec:
            controller.start_measurement("MEAS_TEST", cpap_pressure_cmH2O=4, excitation_hz=11)

        config = [c for c in controller.client.commands if isinstance(c, tuple)][0][1]
        self.assertEqual(config["excitation_hz"], 11)
        self.assertEqual(config["sample_hz"], 11 * 70)
        self.assertEqual(controller.excitation_hz, 11.0)
        rec.return_value.start.assert_called_once()

    def test_default_frequency_is_5_hz(self):
        controller = ArduinoController(send_device_commands=True)
        controller.client = FakeClient()

        with patch("backend.device.arduino_controller.MeasurementSerialRecorder"):
            controller.start_measurement("MEAS_TEST")

        self.assertEqual(controller.excitation_hz, 5.0)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(storage, "DB_NAME", Path(self.tmp.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        storage.initialize_database()
        # Foreign keys off: these tests insert tests without creating their patients.
        fk_patch = patch.object(storage, "get_connection", self._connection_without_fk)
        fk_patch.start()
        self.addCleanup(fk_patch.stop)

    def test_test_parameters_default_to_5_hz_and_remember_the_choice(self):
        self.assertEqual(storage.get_test_parameters("TEST9001")["excitation_hz"], 5.0)
        storage.save_test_parameters("TEST9001", 4.0, None, 19.0)
        self.assertEqual(storage.get_test_parameters("TEST9001")["excitation_hz"], 19.0)

    def test_old_parameter_rows_read_as_the_default(self):
        storage.save_test_parameters("TEST9002")
        conn = sqlite3.connect(storage.DB_NAME)
        conn.execute("UPDATE test_parameters SET excitation_hz = NULL")
        conn.commit()
        conn.close()
        self.assertEqual(storage.get_test_parameters("TEST9002")["excitation_hz"], 5.0)

    def test_measurement_keeps_its_frequency(self):
        conn = sqlite3.connect(storage.DB_NAME)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO tests (test_id, patient_id, created_datetime, frequency_hz, "
            "sample_rate_hz, recording_sessions, status) "
            "VALUES ('TEST9003', 'P1', '01/01/2026 00:00:00', 6, 420, 0, 'Waiting')"
        )
        conn.commit()
        conn.close()

        measurement_id = storage.create_measurement("TEST9003", 4.0, excitation_hz=11)
        conn = storage.get_connection()
        conn.execute(
            "UPDATE measurements SET duration_seconds = 60 WHERE measurement_id = ?",
            (measurement_id,),
        )
        conn.commit()
        conn.close()
        rows = storage.get_test_measurements("TEST9003")

        self.assertEqual(rows[0][8], 11.0)

    @staticmethod
    def _connection_without_fk():
        return sqlite3.connect(storage.DB_NAME)


if __name__ == "__main__":
    unittest.main()
