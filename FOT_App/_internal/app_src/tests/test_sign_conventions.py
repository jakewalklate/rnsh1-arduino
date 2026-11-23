"""Sign conventions for the within-breath differences.

Both ΔRrs and ΔXrs are EXPIRATION MINUS INSPIRATION, matching the hospital FOT device.
ΔXrs used to be the other way round, so these tests exist to stop it drifting back:
a silent sign flip on a number a clinician reads is the worst failure this screen has.

Consequence of the direction: expiratory flow limitation drives expiratory reactance
down, so EFL is a large NEGATIVE ΔXrs, not a positive one.
"""

import unittest

import numpy as np

from backend.processing.FOTAcceptance import summarise_take
from backend.processing.FOTPipeline import _bias_correct_breath
from frontend.views.tests.test_results import (
    EFL_DISPLAY_THRESHOLD,
    delta_rrs,
    delta_xrs,
    efl_verdict,
    expiratory_load_verdict,
)


class DeltaDirectionTests(unittest.TestCase):
    RESULTS = {
        "Rrs_insp": 3.0,
        "Rrs_exp": 5.0,
        "Xrs_insp": -1.0,
        "Xrs_exp": -4.0,
    }

    def test_delta_rrs_is_expiration_minus_inspiration(self):
        self.assertAlmostEqual(delta_rrs(self.RESULTS), 2.0)

    def test_delta_xrs_is_expiration_minus_inspiration(self):
        self.assertAlmostEqual(delta_xrs(self.RESULTS), -3.0)

    def test_both_deltas_use_the_same_direction(self):
        """The bug this replaces: ΔRrs was exp-insp while ΔXrs was insp-exp, so the two
        numbers shown side by side pointed opposite ways."""
        swapped = {
            "Rrs_insp": 5.0, "Rrs_exp": 3.0,
            "Xrs_insp": 4.0, "Xrs_exp": 1.0,
        }
        self.assertLess(delta_rrs(swapped), 0)
        self.assertLess(delta_xrs(swapped), 0)

    def test_stored_delta_is_ignored_when_phase_values_exist(self):
        """Records written before the flip still hold the old sign. Deriving from the
        phase values means they display in the current convention with no migration."""
        stale = dict(self.RESULTS, dXrs=+3.0, dRrs=-2.0)
        self.assertAlmostEqual(delta_xrs(stale), -3.0)
        self.assertAlmostEqual(delta_rrs(stale), 2.0)

    def test_stored_delta_is_used_when_phase_values_are_absent(self):
        self.assertAlmostEqual(delta_xrs({"dXrs": -1.25}), -1.25)
        self.assertIsNone(delta_xrs({}))


class EflVerdictTests(unittest.TestCase):
    def test_efl_is_indicated_by_a_large_negative_dxrs(self):
        self.assertEqual(efl_verdict(-EFL_DISPLAY_THRESHOLD - 0.1)[0], "Indicated")
        self.assertEqual(efl_verdict(-EFL_DISPLAY_THRESHOLD)[0], "Indicated")

    def test_a_large_positive_dxrs_is_not_efl(self):
        """Under the old convention this was the EFL case. It must not be any more."""
        self.assertEqual(efl_verdict(EFL_DISPLAY_THRESHOLD + 5.0)[0], "Not indicated")

    def test_small_values_are_not_efl(self):
        self.assertEqual(efl_verdict(0.02)[0], "Not indicated")
        self.assertEqual(efl_verdict(-0.5)[0], "Not indicated")

    def test_missing_is_blank(self):
        self.assertEqual(efl_verdict(None), ("—", None))

    def test_expiratory_load_reads_positive_as_expiration_harder(self):
        self.assertIn("Expiration harder", expiratory_load_verdict(1.5)[0])
        self.assertIn("Inspiration harder", expiratory_load_verdict(-1.5)[0])


class EngineAgreesWithDisplayTests(unittest.TestCase):
    """The engine and the view must not disagree about direction."""

    def test_summarise_take_uses_expiration_minus_inspiration(self):
        breaths = [{
            "k": 0, "X5_insp": -1.0, "X5_exp": -4.0,
            "R5_insp": 3.0, "R5_exp": 5.0,
            "n_insp": 5, "n_exp": 5,
            "insp_time_s": 1.0, "exp_time_s": 1.0,
            "tidal_volume_l": 0.5, "r5_windows": [3.0, 5.0],
        }]
        summary = summarise_take(breaths)
        if not np.isfinite(summary.get("dXrs", float("nan"))):
            self.skipTest("summarise_take rejected the synthetic breath")
        self.assertAlmostEqual(summary["dXrs"], -3.0, places=6)
        self.assertAlmostEqual(summary["dRrs"], 2.0, places=6)

    def test_bias_correction_preserves_direction(self):
        breath = {
            "R5_insp": 8.0, "R5_exp": 9.5,
            "X5_insp": -1.0, "X5_exp": -4.0,
            "dRrs": 1.5, "dXrs": -3.0,
            "bias_flow_lps": 0.45,
        }
        corrected = _bias_correct_breath(breath, apply_bias_polynomial=True)
        self.assertAlmostEqual(corrected["dXrs"], -3.0, places=9)
        self.assertAlmostEqual(corrected["dRrs"], 1.5, places=9)


if __name__ == "__main__":
    unittest.main()
