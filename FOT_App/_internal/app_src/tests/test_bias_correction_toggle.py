"""The results screen reports one recording on two calibration bases.

The contract the UI depends on, and which these tests pin:
  - "on"  = full bench calibration: SCALE plus the flow-indexed polynomial
  - "off" = SCALE only, every flow-indexed term dropped
  - the differences (dRrs, dXrs) and everything decided by QC are IDENTICAL on both,
    because the additive term cancels between the two phases of a breath and breath
    acceptance runs upstream of the correction. The UI states this to the operator, so
    if it ever stops being true the caption becomes a lie.
"""

import math
import unittest

from backend.processing import FOTImpedance
from backend.processing.FOTImpedance import (
    BLOCK_SAMPLES,
    LUT_SIZE,
    analyse_blocks,
    bias_flow_adjustments,
    make_block_phasor,
    scale_only,
)
from backend.processing.FOTPipeline import _bias_correct_breath


def _blocks(resistance, bias_flow, count=10):
    """Synthetic blocks for a pure resistance sitting on a steady bias flow."""
    blocks = []
    for _ in range(count):
        # LUT index, so it wraps every LUT_SIZE samples rather than counting samples.
        idx = [i % LUT_SIZE for i in range(BLOCK_SAMPLES)]
        flow = [
            0.05 * math.cos(2 * math.pi * i / LUT_SIZE) + bias_flow
            for i in range(BLOCK_SAMPLES)
        ]
        pressure = [resistance * (value - bias_flow) for value in flow]
        blocks.append(make_block_phasor(idx, pressure, flow))
    return blocks


class ScaleOnlyBasisTests(unittest.TestCase):
    def test_scale_only_applies_gain_and_nothing_else(self):
        self.assertAlmostEqual(
            scale_only(complex(10.0, 2.0)).real,
            10.0 * FOTImpedance.BIAS_CORR_SCALE,
        )
        self.assertAlmostEqual(
            scale_only(complex(10.0, 2.0)).imag,
            2.0 * FOTImpedance.BIAS_CORR_SCALE,
        )

    def test_scale_only_is_independent_of_bias_flow(self):
        """The whole point: the reported value must not depend on measured mean flow."""
        low = analyse_blocks(_blocks(13.62, 0.02), apply_bias_polynomial=False)
        high = analyse_blocks(_blocks(13.62, 0.90), apply_bias_polynomial=False)
        self.assertAlmostEqual(low.resistance, high.resistance, places=6)

    def test_bias_corrected_basis_does_depend_on_bias_flow(self):
        """Guards against the toggle silently doing nothing if the polynomial were
        ever cleared: the two bases must actually differ where a real bias flow
        exists."""
        adj, _ = bias_flow_adjustments(0.90)
        if abs(adj) < 1e-9:
            self.skipTest("no bias polynomial loaded, bases cannot differ")

        on = analyse_blocks(_blocks(13.62, 0.90), apply_bias_polynomial=True)
        off = analyse_blocks(_blocks(13.62, 0.90), apply_bias_polynomial=False)
        self.assertNotAlmostEqual(on.resistance, off.resistance, places=3)
        self.assertAlmostEqual(on.resistance, off.resistance - adj, places=6)

    def test_master_flag_still_overrides_both_bases(self):
        original = FOTImpedance.BIAS_FLOW_CORRECTION_ENABLED
        FOTImpedance.BIAS_FLOW_CORRECTION_ENABLED = False
        try:
            on = analyse_blocks(_blocks(13.62, 0.90), apply_bias_polynomial=True)
            off = analyse_blocks(_blocks(13.62, 0.90), apply_bias_polynomial=False)
            self.assertAlmostEqual(on.resistance, off.resistance, places=9)
        finally:
            FOTImpedance.BIAS_FLOW_CORRECTION_ENABLED = original


class BreathBasisTests(unittest.TestCase):
    # Both deltas are expiration minus inspiration: dRrs = 9.5 - 8.0, dXrs = -0.4 - -1.0.
    BREATH = {
        "R5_insp": 8.0,
        "R5_exp": 9.5,
        "X5_insp": -1.0,
        "X5_exp": -0.4,
        "dRrs": 1.5,
        "dXrs": 0.6,
        "bias_flow_lps": 0.45,
    }

    def test_off_basis_leaves_the_breath_untouched(self):
        self.assertEqual(
            _bias_correct_breath(self.BREATH, apply_bias_polynomial=False),
            self.BREATH,
        )

    def test_differences_are_identical_on_both_bases(self):
        """This is what the UI caption promises the operator."""
        on = _bias_correct_breath(self.BREATH, apply_bias_polynomial=True)
        off = _bias_correct_breath(self.BREATH, apply_bias_polynomial=False)
        self.assertAlmostEqual(on["dRrs"], off["dRrs"], places=9)
        self.assertAlmostEqual(on["dXrs"], off["dXrs"], places=9)

    def test_absolutes_shift_by_exactly_the_adjustment(self):
        k_r, k_x = bias_flow_adjustments(self.BREATH["bias_flow_lps"])
        on = _bias_correct_breath(self.BREATH, apply_bias_polynomial=True)
        self.assertAlmostEqual(on["R5_insp"], self.BREATH["R5_insp"] - k_r, places=9)
        self.assertAlmostEqual(on["R5_exp"], self.BREATH["R5_exp"] - k_r, places=9)
        self.assertAlmostEqual(on["X5_insp"], self.BREATH["X5_insp"] - k_x, places=9)
        self.assertAlmostEqual(on["X5_exp"], self.BREATH["X5_exp"] - k_x, places=9)


class ResultsSwapTests(unittest.TestCase):
    """The view swaps only the basis-dependent fields and passes the rest through."""

    RESULTS = {
        "resistance_R5": 1.46,
        "Rrs_insp": 1.43,
        "Rrs_exp": 1.49,
        "cov_r5": 0.32,
        "dRrs": 0.06,
        "clean_breath_count": 8,
        "breath_count": 8,
        "tidal_volume_l": 0.533,
    }
    SCALE_ONLY = {
        "resistance_R5": 2.55,
        "Rrs_insp": 2.51,
        "Rrs_exp": 2.58,
        "cov_r5": 0.038,
        "status": "ok",
    }

    def _swap(self, on):
        from frontend.views.tests.test_results import results_on_basis
        return results_on_basis(self.RESULTS, on, self.SCALE_ONLY)

    def test_on_returns_stored_results_unchanged(self):
        self.assertIs(self._swap(True), self.RESULTS)

    def test_off_swaps_basis_dependent_fields(self):
        off = self._swap(False)
        self.assertAlmostEqual(off["Rrs_insp"], 2.51)
        self.assertAlmostEqual(off["cov_r5"], 0.038)

    def test_off_preserves_shared_fields(self):
        off = self._swap(False)
        for field in ("dRrs", "clean_breath_count", "breath_count", "tidal_volume_l"):
            self.assertEqual(off[field], self.RESULTS[field], msg=field)

    def test_missing_scale_only_falls_back_to_stored_results(self):
        from frontend.views.tests.test_results import results_on_basis
        self.assertIs(results_on_basis(self.RESULTS, False, {}), self.RESULTS)


class ReproducibilityVerdictTests(unittest.TestCase):
    """A negative CoV must never be reported as good reproducibility. It appears when
    the correction over-subtracts a take into negative mean resistance, which really
    happens: MEAS0012 on the bias-corrected basis reports Rrs -4.78 and CoV -11.6%."""

    def _verdict(self, cov):
        from frontend.views.tests.test_results import reproducibility_verdict
        return reproducibility_verdict(cov)

    def test_negative_cov_is_flagged_not_passed(self):
        text, state = self._verdict(-0.116)
        self.assertEqual(state, "bad")
        self.assertNotIn("Within guideline", text)

    def test_good_cov_passes(self):
        self.assertEqual(self._verdict(0.038)[1], "ok")

    def test_high_cov_warns(self):
        self.assertEqual(self._verdict(0.32)[1], "warn")

    def test_missing_and_non_finite_are_blank(self):
        self.assertEqual(self._verdict(None)[1], None)
        self.assertEqual(self._verdict(float("nan"))[1], None)
        self.assertEqual(self._verdict(float("inf"))[1], None)


if __name__ == "__main__":
    unittest.main()
