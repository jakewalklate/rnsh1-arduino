"""The breath and whole-record paths must report on the same calibration tier."""

import numpy as np
import pytest

from backend.processing import FOTBreath, FOTImpedance
from backend.processing.FOTAcceptance import flag_outlier_breaths, qc_breath
from backend.processing.FOTBreath import phase_resolved_breaths
from backend.processing.FOTImpedance import (
    BIAS_FLOW_CORRECTION_ENABLED,
    BLOCKS_PER_WINDOW,
    BLOCK_SAMPLES,
    LUT_SIZE,
    SAMPLE_HZ,
    analyse_blocks,
    bias_flow_adjustments,
    make_block_phasor,
)
from backend.processing.FOTPipeline import _bias_correct_breath


REFERENCE_R = 13.62
REFERENCE_X = -2.00
BIAS_FLOW = 0.45
OSC_AMPLITUDE = 0.06


def _synthetic_recording(seconds=40.0, breath_hz=0.25, bias_flow=BIAS_FLOW):
    """Generate a known load with breathing flow on the Arduino LUT index."""
    n = int(seconds * SAMPLE_HZ)
    idx = np.arange(n) % LUT_SIZE
    theta = 2.0 * np.pi * idx / LUT_SIZE
    t = np.arange(n) / SAMPLE_HZ

    breathing = 0.35 * np.sin(2.0 * np.pi * breath_hz * t)
    flow = OSC_AMPLITUDE * np.cos(theta) + breathing + bias_flow
    pressure = (
        OSC_AMPLITUDE
        * (REFERENCE_R * np.cos(theta) - REFERENCE_X * np.sin(theta))
        + REFERENCE_R * breathing
    )
    return pressure, flow, idx


def _breaths(pressure, flow, idx):
    FOTBreath.BIAS_CORR_SCALE = FOTImpedance.BIAS_CORR_SCALE
    output = phase_resolved_breaths(
        pressure,
        flow,
        idx,
        fs=SAMPLE_HZ,
        correction_deg=0.0,
        apply_scale=BIAS_FLOW_CORRECTION_ENABLED,
    )
    return output["breaths"]


def _global_impedance(pressure, flow, idx):
    blocks = []
    for start in range(0, len(idx) - BLOCK_SAMPLES + 1, BLOCK_SAMPLES):
        sample_slice = slice(start, start + BLOCK_SAMPLES)
        if idx[sample_slice][0] != 0:
            continue
        blocks.append(
            make_block_phasor(
                idx[sample_slice],
                pressure[sample_slice],
                flow[sample_slice],
            )
        )
    assert len(blocks) >= BLOCKS_PER_WINDOW, "synthetic record too short"
    return analyse_blocks(
        blocks,
        correction_deg=0.0,
        required_blocks=len(blocks),
    )


def test_breath_path_matches_whole_record_path():
    pressure, flow, idx = _synthetic_recording()
    global_result = _global_impedance(pressure, flow, idx)

    breaths = [
        _bias_correct_breath(breath)
        for breath in _breaths(pressure, flow, idx)
        if breath["quality_ok"]
    ]
    assert breaths, "no usable breaths in the synthetic recording"

    breath_r = float(
        np.median(
            [breath["R5_insp"] for breath in breaths]
            + [breath["R5_exp"] for breath in breaths]
        )
    )
    breath_x = float(
        np.median(
            [breath["X5_insp"] for breath in breaths]
            + [breath["X5_exp"] for breath in breaths]
        )
    )

    assert breath_r == pytest.approx(global_result.resistance, abs=0.05)
    assert breath_x == pytest.approx(global_result.reactance, abs=0.35)


def test_uncorrected_breath_path_would_disagree():
    pressure, flow, idx = _synthetic_recording()
    global_result = _global_impedance(pressure, flow, idx)

    raw = [
        breath
        for breath in _breaths(pressure, flow, idx)
        if breath["quality_ok"]
    ]
    raw_r = float(
        np.median(
            [breath["R5_insp"] for breath in raw]
            + [breath["R5_exp"] for breath in raw]
        )
    )

    bias = float(np.median([breath["bias_flow_lps"] for breath in raw]))
    expected_gap, _ = bias_flow_adjustments(bias)
    assert expected_gap > 1.0
    assert abs(raw_r - global_result.resistance) == pytest.approx(
        expected_gap,
        abs=0.05,
    )


def test_correction_leaves_dxrs_and_drrs_unchanged():
    pressure, flow, idx = _synthetic_recording()
    for breath in _breaths(pressure, flow, idx):
        if not breath["quality_ok"]:
            continue
        corrected = _bias_correct_breath(breath)
        assert corrected["dXrs"] == pytest.approx(breath["dXrs"], abs=1e-9)
        assert corrected["dRrs"] == pytest.approx(breath["dRrs"], abs=1e-9)


def test_qc_verdicts_are_unchanged_by_the_correction():
    pressure, flow, idx = _synthetic_recording()
    breaths = _breaths(pressure, flow, idx)

    before = [
        qc_breath(breath)[0] and breath.get("quality_ok", False)
        for breath in breaths
    ]
    after = [
        qc_breath(_bias_correct_breath(breath))[0]
        and breath.get("quality_ok", False)
        for breath in breaths
    ]
    assert before == after

    clean_before = [
        breath for breath, accepted in zip(breaths, before) if accepted
    ]
    clean_after = [_bias_correct_breath(breath) for breath in clean_before]
    assert flag_outlier_breaths(clean_before) == flag_outlier_breaths(clean_after)


def test_correction_uses_each_breaths_own_bias_flow():
    breath = {
        "R5_insp": 20.0,
        "R5_exp": 20.0,
        "X5_insp": -2.0,
        "X5_exp": -2.0,
        "dXrs": 0.0,
        "dRrs": 0.0,
        "bias_flow_lps": 0.9,
    }
    expected_r, expected_x = bias_flow_adjustments(0.9)
    corrected = _bias_correct_breath(breath)
    assert corrected["R5_insp"] == pytest.approx(20.0 - expected_r)
    assert corrected["X5_insp"] == pytest.approx(-2.0 - expected_x)

    other = dict(breath, bias_flow_lps=0.2)
    assert _bias_correct_breath(other)["R5_insp"] != pytest.approx(
        corrected["R5_insp"]
    )


def test_non_finite_and_missing_fields_survive():
    corrected = _bias_correct_breath({"bias_flow_lps": 0.4})
    assert "R5_insp" not in corrected

    corrected = _bias_correct_breath(
        {
            "R5_insp": float("nan"),
            "X5_exp": None,
            "bias_flow_lps": None,
        }
    )
    assert np.isnan(corrected["R5_insp"])
    assert corrected["X5_exp"] is None
