"""Tests for the syringe flow calibration.

Run from the src directory:  python -m pytest tests/test_flow_gain_calibration.py
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import FIRMWARE_FLOW_GAIN_NEG, FIRMWARE_FLOW_GAIN_POS
from backend.device.serial_device import parse_sample_line
from backend.processing import FlowGainCalibration as fgc


FS = 420.0


def half_sine_stroke(amplitude, duration_s, baseline, pad_s=1.0):
    """A dp pulse whose integral is known analytically: 2*A*T/pi."""
    n = int(duration_s * FS)
    pad = [baseline] * int(pad_s * FS)
    body = [baseline + amplitude * math.sin(math.pi * i / n) for i in range(n)]
    return pad + body + pad


@pytest.fixture
def temp_gain_file(tmp_path, monkeypatch):
    path = tmp_path / "fot_flow_gain.json"
    monkeypatch.setattr(fgc, "flow_gain_path", lambda: path)
    return path


# ------------------------------------------------------------- stroke scoring

def test_gain_matches_the_analytic_integral():
    amplitude, duration = 0.5, 1.2
    expected = fgc.SYRINGE_VOLUME_L / (2 * amplitude * duration / math.pi)

    stroke, reason = fgc.score_stroke(
        half_sine_stroke(amplitude, duration, baseline=0.0),
        baseline=0.0, direction=+1, sample_hz=FS,
    )

    assert reason is None
    assert stroke["gain_Lps_per_cmH2O"] == pytest.approx(expected, rel=1e-4)


def test_baseline_offset_does_not_change_the_gain():
    """The pre-stroke zero is subtracted, so a drifting sensor offset must not move
    the result. This is why the operator is made to hold still first."""
    clean, _ = fgc.score_stroke(
        half_sine_stroke(0.5, 1.2, baseline=0.0),
        baseline=0.0, direction=+1, sample_hz=FS,
    )
    offset, _ = fgc.score_stroke(
        half_sine_stroke(0.5, 1.2, baseline=0.35),
        baseline=0.35, direction=+1, sample_hz=FS,
    )
    assert offset["gain_Lps_per_cmH2O"] == pytest.approx(
        clean["gain_Lps_per_cmH2O"], rel=1e-9
    )


def test_pull_stroke_gives_a_positive_gain():
    """Direction and integral are both negative on a pull, so the gain stays
    positive — a negative gain would invert flow."""
    stroke, reason = fgc.score_stroke(
        half_sine_stroke(-0.5, 1.2, baseline=0.0),
        baseline=0.0, direction=-1, sample_hz=FS,
    )
    assert reason is None
    assert stroke["gain_Lps_per_cmH2O"] > 0
    assert stroke["direction"] == "pull"


def test_wrong_button_is_rejected():
    _, reason = fgc.score_stroke(
        half_sine_stroke(0.5, 1.2, baseline=0.0),
        baseline=0.0, direction=-1, sample_hz=FS,
    )
    assert "opposite" in reason


def test_flat_trace_is_rejected():
    _, reason = fgc.score_stroke(
        [0.0] * int(2 * FS), baseline=0.0, direction=+1, sample_hz=FS
    )
    assert "no A2 pulse" in reason


def test_too_short_is_rejected():
    _, reason = fgc.score_stroke([0.0] * 10, baseline=0.0, direction=+1, sample_hz=FS)
    assert "0.25 seconds" in reason


# ------------------------------------------------------------------- summary

def _fake_strokes(gains, directions):
    return [
        {
            "number": i + 1,
            "direction": d,
            "baseline_cmH2O": 0.0,
            "dp_integral_cmH2O_s": fgc.SYRINGE_VOLUME_L / g,
            "gain_Lps_per_cmH2O": g,
        }
        for i, (g, d) in enumerate(zip(gains, directions))
    ]


def test_summary_reproduces_the_bench_tool_medians():
    """Numbers lifted from flow_calibration_20260722_133929.csv, the run that set the
    firmware's current constants. The port must agree with the bench tool exactly."""
    gains = [
        2.860315148, 2.842803139, 2.843531948, 2.858674596, 2.826764211,
        2.861955700, 2.841875193, 2.843167543, 2.862315000, 2.820000000,
    ]
    directions = ["push", "pull"] * 5
    summary = fgc.summarise(_fake_strokes(gains, directions))

    assert summary["n_strokes"] == 10
    assert summary["n_push"] == 5
    assert summary["n_pull"] == 5
    assert summary["median_gain"] == pytest.approx(fgc.median(gains))
    assert summary["complete"] is True


def test_consistency_needs_the_full_ten_strokes():
    """Three tightly agreeing strokes are not evidence of a repeatable calibration,
    so the pass flag must stay down until the target is reached."""
    tight = fgc.summarise(_fake_strokes([2.86, 2.861, 2.859], ["push", "pull", "push"]))
    assert tight["worst_deviation_pct"] < 1.0
    assert tight["consistency_pass"] is False


def test_a_wild_stroke_fails_the_consistency_check():
    gains = [2.86] * 9 + [3.5]
    summary = fgc.summarise(_fake_strokes(gains, ["push", "pull"] * 5))
    assert summary["complete"] is True
    assert summary["worst_deviation_pct"] > fgc.CONSISTENCY_TOLERANCE_PCT
    assert summary["consistency_pass"] is False


# ---------------------------------------------------------------- save / apply

def test_no_saved_file_leaves_flow_untouched(temp_gain_file):
    """A fresh checkout must behave exactly as it did before this feature existed."""
    assert fgc.active_flow_gains() == (FIRMWARE_FLOW_GAIN_POS, FIRMWARE_FLOW_GAIN_NEG)
    assert fgc.rescale_flow_lps(0.734) == pytest.approx(0.734)
    assert fgc.rescale_flow_lps(-0.734) == pytest.approx(-0.734)


def test_saved_gains_rescale_each_direction_independently(temp_gain_file):
    strokes = _fake_strokes([5.72, 1.4214], ["push", "pull"])
    fgc.save_flow_gain(strokes)

    # Push doubled, pull halved, relative to the firmware constants.
    assert fgc.rescale_flow_lps(1.0) == pytest.approx(2.0, rel=1e-9)
    assert fgc.rescale_flow_lps(-1.0) == pytest.approx(-0.5, rel=1e-9)


def test_rescale_recovers_the_true_flow(temp_gain_file):
    """The whole point: the device scaled dp by the OLD gain, and the app must end up
    with dp times the NEW one."""
    true_gain_push = 3.05
    fgc.save_flow_gain(_fake_strokes([true_gain_push, 2.95], ["push", "pull"]))

    dp = 0.42
    streamed = FIRMWARE_FLOW_GAIN_POS * dp          # what the Arduino sends
    assert fgc.rescale_flow_lps(streamed) == pytest.approx(true_gain_push * dp)


def test_one_sided_calibration_is_refused(temp_gain_file):
    with pytest.raises(ValueError, match="push AND one accepted pull"):
        fgc.save_flow_gain(_fake_strokes([2.86, 2.87], ["push", "push"]))


def test_saved_file_records_the_firmware_gains_it_assumed(temp_gain_file):
    _, payload = fgc.save_flow_gain(_fake_strokes([2.9, 2.8], ["push", "pull"]))
    assert payload["firmware_flow_gain_pos"] == FIRMWARE_FLOW_GAIN_POS
    assert payload["firmware_flow_gain_neg"] == FIRMWARE_FLOW_GAIN_NEG


# ------------------------------------------------------------- serial plumbing

def test_parser_applies_the_gain_by_default(temp_gain_file):
    fgc.save_flow_gain(_fake_strokes([5.72, 2.8428], ["push", "pull"]))
    sample = parse_sample_line("12,3.9,1.0")
    assert sample.flow_lps == pytest.approx(2.0, rel=1e-9)


def test_parser_leaves_flow_alone_in_calibration_mode(temp_gain_file):
    """In flow-calibration mode field 3 is raw dp, not flow. Scaling it would corrupt
    the measurement the calibration is built from."""
    fgc.save_flow_gain(_fake_strokes([5.72, 2.8428], ["push", "pull"]))
    sample = parse_sample_line("12,3.9,1.0", apply_flow_gain=False)
    assert sample.flow_lps == pytest.approx(1.0)
