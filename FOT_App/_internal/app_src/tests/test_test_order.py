"""A patient's tests are listed newest first by their real date, not by the text of
"dd/mm/yyyy hh:mm:ss", which sorts by day of the month first."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.database import storage


class TestOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(storage, "DB_NAME", Path(self.tmp.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        storage.initialize_database()
        conn = sqlite3.connect(storage.DB_NAME)
        for test_id, created in (
            ("TEST_JUL30", "30/07/2026 16:52:50"),
            ("TEST_NOV24", "24/11/2026 11:37:12"),
            ("TEST_AUG06", "06/08/2026 13:03:51"),
            ("TEST_NOV23", "23/11/2026 15:29:57"),
            ("TEST_BROKEN", "not a date"),
        ):
            conn.execute(
                "INSERT INTO tests (test_id, patient_id, created_datetime, frequency_hz, "
                "sample_rate_hz, recording_sessions, status) "
                "VALUES (?, 'P1', ?, 6, 420, 0, 'Waiting')",
                (test_id, created),
            )
        conn.commit()
        conn.close()
        no_fk = patch.object(
            storage, "get_connection", lambda: sqlite3.connect(storage.DB_NAME)
        )
        no_fk.start()
        self.addCleanup(no_fk.stop)

    def test_newest_first_by_real_date(self):
        order = [test[0] for test in storage.get_patient_tests("P1")]
        self.assertEqual(
            order,
            ["TEST_NOV24", "TEST_NOV23", "TEST_AUG06", "TEST_JUL30", "TEST_BROKEN"],
        )


if __name__ == "__main__":
    unittest.main()
