"""A test's status after "Discard and Re-record".

Discarding a measurement deletes it (row and recording file); the test is then
"Complete" if it still has saved measurements, "Waiting" if not. The stop-summary
dialog used to force "Waiting" afterwards, which left TEST0020 showing "Waiting"
with two saved measurements."""

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.database import storage


class DiscardStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (
            ("DB_NAME", Path(self.tmp.name) / "test.db"),
            ("RAW_MEASUREMENT_DATA_DIR", Path(self.tmp.name) / "measurements"),
        ):
            if hasattr(storage, name):
                patcher = patch.object(storage, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        storage.initialize_database()
        conn = sqlite3.connect(storage.DB_NAME)
        conn.execute(
            "INSERT INTO tests (test_id, patient_id, created_datetime, frequency_hz, "
            "sample_rate_hz, recording_sessions, status) "
            "VALUES ('TEST9100', 'P1', '01/01/2026 00:00:00', 6, 420, 0, 'Waiting')"
        )
        conn.commit()
        conn.close()
        no_fk = patch.object(
            storage, "get_connection", lambda: sqlite3.connect(storage.DB_NAME)
        )
        no_fk.start()
        self.addCleanup(no_fk.stop)

    def status(self):
        conn = sqlite3.connect(storage.DB_NAME)
        row = conn.execute(
            "SELECT status, recording_sessions FROM tests WHERE test_id = 'TEST9100'"
        ).fetchone()
        conn.close()
        return row

    def saved_measurement(self):
        measurement_id = storage.create_measurement("TEST9100", 5.0, excitation_hz=6)
        storage.complete_measurement(measurement_id, 60.0)
        return measurement_id

    def test_discarding_keeps_a_test_with_saved_measurements_complete(self):
        self.saved_measurement()
        self.saved_measurement()
        extra = storage.create_measurement("TEST9100", 5.0, excitation_hz=6)
        storage.discard_measurement(extra)
        self.assertEqual(self.status(), ("Complete", 2))

    def test_discarding_the_only_measurement_leaves_the_test_waiting(self):
        only = storage.create_measurement("TEST9100", 5.0, excitation_hz=6)
        storage.discard_measurement(only)
        self.assertEqual(self.status()[0], "Waiting")


class DialogTests(unittest.TestCase):
    def test_discard_and_rerecord_does_not_force_waiting(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend" / "views" / "tests" / "recording_dialogs.py"
        ).read_text()
        start = source.index('"Discard and Re-record"')
        block = source[start:source.index("st.rerun()", start)]
        self.assertIsNone(re.search(r'update_test_status\(', block))


if __name__ == "__main__":
    unittest.main()
