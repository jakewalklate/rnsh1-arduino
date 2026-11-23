"""Phase-angle calibration — headless port of the bench tool's calibrate action.

A known reference resistor replaces the patient. A pure resistor has zero reactance,
so whatever angle the measured phasor sits at IS the system's phase error; the
correction is simply the negative of that angle. Everything downstream — the bias
sweep, the daily check, patient R5/X5 — is rotated by it, so this runs FIRST and a
change here invalidates any bias sweep captured at the old angle.

Rules mirror PressureSensorReading._finish_calibration in Henry's bench tool:

  * 30 s total, of which the first 5 s are discarded as settling (25 usable blocks).
  * PASS/FAIL is judged on what this step actually measures: the correction angle and
    the signal quality behind it. The R/X window checks are still computed and still
    reported, but as a BIAS-CALIBRATION advisory, not as the phase verdict -- see
    below.
  * The result is ALWAYS saved and applied, pass or fail. The angle comes straight
    from this run's own phasor, so it is internally self-consistent — unlike a stale
    file. Withholding a "failed" calibration used to strand the old, possibly worse,
    angle with no way to overwrite it. PASS/FAIL is a diagnostic, never a gate.

WHY the R/X windows are not the phase verdict:

    Rotation nulls X by construction -- the angle is DEFINED as whatever makes the
    corrected reactance zero, and the saved files show it landing at ~1e-17. The
    bias-corrected reactance is therefore always exactly the negative of the bias
    polynomial at whatever flow happened to be present, and carries no information
    about the phase angle at all. Judging the phase step on it reported bias-fit
    problems as "phase calibration failed", which sent the operator to re-run the
    wrong thing. Same for R: the bias-corrected value is the polynomial's output, not
    the instrument's reading.

    So the phase verdict now rests on quality_valid and phase_pass. r_pass/x_pass are
    kept in the record and surfaced separately under bias_check_pass.
"""

from datetime import datetime, timezone
from pathlib import Path

from backend.processing.FOTImpedance import (
    BLOCK_SAMPLES,
    EXCITATION_HZ,
    MAX_CALIBRATION_CORRECTION_DEG,
    REFERENCE_RESISTANCE,
    SAMPLE_HZ,
    analyse_blocks,
    angle_difference_deg,
    calibration_from_blocks,
    load_calibration,
)

CALIBRATION_SECONDS = 30           # total capture, settling included
CALIBRATION_SETTLING_SECONDS = 5   # discarded before the angle is computed
REPEAT_CALIBRATION_WARNING_DEG = 1.0

# Acceptance window on the bias-corrected reading of the reference resistor.
PHASE_R_MIN = 12.94
PHASE_R_MAX = 14.30
# Confirms the resistor sits near X~0 only. Validating the X SCALE needs a load of
# known non-zero reactance, which this does not provide.
PHASE_X_ABS_TOLERANCE = 0.27


def phase_calibration_path_for(hz=EXCITATION_HZ):
    """Per-frequency phase file, beside the bias file in this same directory. Mirrors
    FOTImpedance.bias_calibration_path_for so the pair stay together."""
    return Path(__file__).resolve().parent / f"fot_phase_calibration_{hz:g}hz.json"


def active_correction_deg(hz=EXCITATION_HZ):
    """The rotation angle in force, read from disk at CALL time.

    Every path that measures impedance must go through here. Passing a literal 0.0
    instead silently discards the calibration: rotating by -3.4 deg moves X by roughly
    0.06 * R, which is the whole of a patient's X5 signal, while barely touching R.

    No saved file means no calibration has ever been run, and 0.0 is then the honest
    answer. A file that exists but lacks the angle is corrupt, and is allowed to raise
    rather than degrade to an un-rotated result that looks perfectly normal.
    """
    data = load_calibration(phase_calibration_path_for(hz))
    if data is None:
        return 0.0
    return float(data["correction_angle_deg"])


def usable_blocks_for_calibration():
    return CALIBRATION_SECONDS - CALIBRATION_SETTLING_SECONDS


def seconds_for_phase_calibration():
    return int(CALIBRATION_SECONDS)


def evaluate(
    blocks,
    previous=None,
    reference_resistance=REFERENCE_RESISTANCE,
    excitation_hz=EXCITATION_HZ,
    settling_blocks=CALIBRATION_SETTLING_SECONDS,
):
    """Reduce settled blocks into the calibration record written to
    fot_phase_calibration_{hz}hz.json. `blocks` must already have the settling
    blocks removed. `previous` is the calibration being replaced, if any."""
    blocks = list(blocks)
    result = calibration_from_blocks(blocks, reference_resistance)

    quality = analyse_blocks(
        blocks,
        correction_deg=result["correction_angle_deg"],
        required_blocks=len(blocks),
    )
    result["created_utc"] = datetime.now(timezone.utc).isoformat()
    result["excitation_hz"] = float(excitation_hz)
    result["sample_hz"] = float(SAMPLE_HZ)
    result["block_samples"] = BLOCK_SAMPLES
    result["settling_blocks_discarded"] = int(settling_blocks)
    result["quality_valid"] = bool(quality.valid)
    result["quality_reason"] = quality.reason
    result["block_variation_percent"] = quality.block_relative_deviation * 100.0

    if previous is not None and "correction_angle_deg" in previous:
        result["previous_correction_angle_deg"] = float(
            previous["correction_angle_deg"]
        )
        result["repeat_angle_difference_deg"] = angle_difference_deg(
            result["correction_angle_deg"],
            result["previous_correction_angle_deg"],
        )

    # Kept for the bias advisory below, and for the record. NOT the phase verdict.
    corrected_r = float(quality.resistance)
    corrected_x = float(quality.reactance)
    result["bias_corrected_resistance_cmH2O_s_per_L"] = corrected_r
    result["bias_corrected_reactance_cmH2O_s_per_L"] = corrected_x
    result["bias_flow_lps"] = float(quality.bias_flow_lps)

    r_pass = PHASE_R_MIN <= corrected_r <= PHASE_R_MAX
    x_pass = abs(corrected_x) <= PHASE_X_ABS_TOLERANCE
    phase_pass = abs(result["correction_angle_deg"]) <= MAX_CALIBRATION_CORRECTION_DEG
    repeat = result.get("repeat_angle_difference_deg")
    repeat_ok = repeat is None or repeat <= REPEAT_CALIBRATION_WARNING_DEG

    result["r_pass"] = r_pass
    result["x_pass"] = x_pass
    result["phase_pass"] = phase_pass
    result["repeat_ok"] = repeat_ok
    # These two say whether the BIAS calibration is healthy at this bias flow. They
    # are reported alongside the phase result, never as the phase result.
    result["bias_check_pass"] = bool(r_pass and x_pass)
    result["calibration_pass"] = bool(quality.valid and phase_pass)
    return result


def failure_reasons(result):
    """Plain-language reasons the PHASE calibration did not pass, for the operator.

    Only reasons that are about the phase step. Bias-fit problems go through
    bias_check_reasons() so the operator is never sent to re-run the wrong thing.
    """
    reasons = []
    if not result.get("quality_valid", True):
        reasons.append(f"signal quality: {result.get('quality_reason', 'unknown')}")
    if not result.get("phase_pass", True):
        reasons.append(
            f"correction angle {result['correction_angle_deg']:+.2f}° exceeds "
            f"±{MAX_CALIBRATION_CORRECTION_DEG:g}°"
        )
    return reasons


def bias_check_reasons(result):
    """Why the bias calibration looks unhealthy at this run's bias flow.

    Advisory only: these are properties of the tube-calibration polynomials, not of
    the angle this step just measured.
    """
    reasons = []
    if not result.get("r_pass", True):
        reasons.append(
            f"corrected resistance {result['bias_corrected_resistance_cmH2O_s_per_L']:.2f} "
            f"outside {PHASE_R_MIN}-{PHASE_R_MAX}"
        )
    if not result.get("x_pass", True):
        reasons.append(
            f"corrected reactance {result['bias_corrected_reactance_cmH2O_s_per_L']:+.2f} "
            f"outside ±{PHASE_X_ABS_TOLERANCE}"
        )
    return reasons
