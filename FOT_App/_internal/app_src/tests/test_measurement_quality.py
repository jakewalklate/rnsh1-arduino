"""Measurement and test quality: the rule behind the results page's "Measurement
quality" line and the patient page's quality tags."""

import unittest
from unittest.mock import patch

from backend.processing import MeasurementQuality as mq
from backend.processing.FOTAcceptance import FLOOR


def results(clean=None, total=None, status=""):
    return {"clean_breath_count": clean, "breath_count": total, "status": status}


class MeasurementQualityTests(unittest.TestCase):
    def test_good_usable_and_insufficient(self):
        self.assertEqual(mq.measurement_quality(results(10, 10)), mq.GOOD)
        self.assertEqual(mq.measurement_quality(results(9, 16)), mq.USABLE)
        self.assertEqual(mq.measurement_quality(results(FLOOR - 1, 5)), mq.INSUFFICIENT)
        self.assertEqual(mq.measurement_quality(results(0, 40)), mq.INSUFFICIENT)

    def test_older_measurements_read_the_count_from_the_analysis_note(self):
        # MEAS0001-style row: no count columns, the count is only in the note.
        old = results(status="Processed 38771 rows; QC 0/12 clean breaths; insufficient")
        self.assertEqual(mq.breath_counts(old), (0, 12))
        self.assertEqual(mq.measurement_quality(old), mq.INSUFFICIENT)

    def test_unanalysed_measurement_has_no_quality(self):
        self.assertIsNone(mq.measurement_quality(None))
        self.assertIsNone(mq.measurement_quality(results(status="Recording")))

    def test_a_test_is_as_good_as_its_best_measurement(self):
        self.assertEqual(
            mq.best_quality([results(0, 22), results(9, 9), results(6, 13)]), mq.GOOD
        )
        self.assertEqual(mq.best_quality([results(0, 12)]), mq.INSUFFICIENT)
        self.assertIsNone(mq.best_quality([None]))

    def test_clipped_sensors_are_flagged(self):
        clipped = results(11, 11, "pressure channel near full scale; QC 11/11 clean breaths")
        self.assertTrue(mq.sensors_near_full_scale(clipped))
        self.assertFalse(mq.sensors_near_full_scale(results(11, 11, "impedance OK")))


class PatientPageTagTests(unittest.TestCase):
    """test_quality() on the patient page, with the database calls stubbed."""

    def tag(self, measurements, results_by_id):
        from frontend.views.patients import existing_patient as page

        with patch.object(page, "get_test_measurements", return_value=measurements), \
                patch.object(page, "get_measurement_results",
                             side_effect=lambda mid: results_by_id.get(mid)):
            return page.test_quality("TEST9000")

    @staticmethod
    def row(measurement_id, status="Complete"):
        return (measurement_id, "TEST9000", 1, "", "", 60.0, status, None, 6.0)

    def test_complete_test_with_no_usable_measurement_says_not_usable(self):
        tag = self.tag([self.row("M1")], {"M1": results(0, 25)})
        self.assertEqual(tag, ("Not usable", "fail", False))

    def test_one_good_measurement_makes_the_test_good(self):
        tag = self.tag(
            [self.row("M1"), self.row("M2")],
            {"M1": results(0, 22), "M2": results(8, 9)},
        )
        self.assertEqual(tag[:2], ("Good", "pass"))

    def test_unsaved_measurements_are_ignored(self):
        tag = self.tag([self.row("M1", status="Recording")], {"M1": results(9, 9)})
        self.assertEqual(tag[:2], ("No data", "neutral"))

    def test_clipping_is_reported_alongside_the_tag(self):
        tag = self.tag(
            [self.row("M1")],
            {"M1": results(11, 11, "flow channel near full scale")},
        )
        self.assertEqual(tag, ("Good", "pass", True))


if __name__ == "__main__":
    unittest.main()
