"""Gain and phase calibration: one recording of the reference load per frequency.

The reference load is a plain resistor, so at every frequency the true answer is
R = the value printed on it and X = 0. Whatever the device measures instead is its own
error, and two numbers per frequency undo it:

  * phase: the angle the measured impedance sits at. A resistor should sit at 0 deg,
    so the angle is the timing offset between the pressure and flow sensors. Every
    later reading is rotated back by it (FOTImpedance.rotate_impedance).
  * gain: printed value / measured resistance after that rotation. Every later
    reading is multiplied by it (FOTImpedance.scale_only, the SCALE constant).

Measured with CPAP off, so the load reads its printed value and nothing here depends
on bias flow. The bias-flow polynomial the old CPAP sweep fitted is NOT carried
over: it was fitted against the previous gain and angle, and the handover report
(section 3) found it wrong for patient measurements anyway. Saving writes zero
polynomial terms, so the "bias-corrected" and "correction off" bases agree.

Writes the same per-frequency files the measurement engine already reads, so nothing
downstream changes: fot_phase_calibration_{hz}hz.json (the angle) and
fot_bias_calibration_{hz}hz.json (the gain, as "scale"). Files that are replaced are
copied to data/calibration/archive first.
"""

from __future__ import annotations

import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np

from backend.config import EXCITATION_FREQUENCY_OPTIONS_HZ
from backend.processing.FOTImpedance import (
    BLOCK_SAMPLES,
    CYCLES_PER_BLOCK,
    EXCITATION_HZ,
    LUT_SIZE,
    MAX_BLOCK_RELATIVE_DEVIATION,
    MAX_CALIBRATION_CORRECTION_DEG,
    MIN_FLOW_AMPLITUDE_LPS,
    BlockPhasor,
    _block_variation,
    bias_calibration_path_for,
    h1_impedance,
    load_bias_calibration_for,
    load_calibration,
    rotate_impedance,
    save_calibration,
)
from backend.processing.FOTPhaseCalibration import phase_calibration_path_for

FREQUENCIES_HZ = tuple(float(hz) for hz in EXCITATION_FREQUENCY_OPTIONS_HZ)

# Per frequency: let the speaker and the air in the tubes settle, then measure.
SETTLE_SECONDS = 5
MEASURE_SECONDS = 25

HPA_TO_CMH2O = 1.019716
RESISTANCE_UNITS = {
    "hPa·s/L": HPA_TO_CMH2O,
    "cmH₂O·s/L": 1.0,
}

# A gain this far from 1 means the device is reading the load more than ~25% wrong
# before correction. That is a hardware problem (a leak, a blocked tap, the 5 Hz
# anti-resonance), and multiplying it away would hide it rather than fix it.
GAIN_MIN = 0.75
GAIN_MAX = 1.33

METHOD = "gain_phase"


def blocks_for_seconds(seconds: float, hz: float) -> int:
    """A block is CYCLES_PER_BLOCK cycles, so it lasts CYCLES_PER_BLOCK / hz seconds:
    1.2 s at 5 Hz, 0.32 s at 19 Hz. Convert a duration into whole blocks."""
    return max(1, int(math.ceil(float(seconds) * float(hz) / CYCLES_PER_BLOCK)))


def seconds_per_frequency() -> int:
    return SETTLE_SECONDS + MEASURE_SECONDS


def total_seconds(frequencies: Iterable[float] = FREQUENCIES_HZ) -> int:
    return seconds_per_frequency() * len(tuple(frequencies))


def to_cmh2o(value: float, unit: str) -> float:
    return float(value) * RESISTANCE_UNITS[unit]


def evaluate_frequency(
    blocks: Iterable[BlockPhasor],
    reference_resistance: float,
    hz: float,
) -> dict:
    """Gain, phase and the quality checks behind them, for one frequency.

    `reference_resistance` is in cmH2O·s/L. `passed` is False when the recording is
    too weak or unsteady to trust, or when the correction it asks for is so large
    that something physical is wrong; `reasons` says which, in plain words."""
    blocks = list(blocks)
    result = {
        "excitation_hz": float(hz),
        "reference_resistance_cmH2O_s_per_L": float(reference_resistance),
        "blocks_used": len(blocks),
        "passed": False,
        "reasons": [],
    }
    if len(blocks) < 2:
        result["reasons"].append("not enough clean data was recorded")
        return result

    raw = h1_impedance(blocks)
    angle = float(np.rad2deg(np.angle(raw)))
    corrected = rotate_impedance(raw, angle)
    measured_r = float(corrected.real)
    gain = reference_resistance / measured_r if measured_r > 0 else math.nan
    pressure_amplitude = float(np.mean([b.pressure_amplitude for b in blocks]))
    flow_amplitude = float(np.mean([b.flow_amplitude for b in blocks]))
    variation = _block_variation(blocks, raw)

    result.update({
        "correction_angle_deg": angle,
        "gain": float(gain),
        "raw_resistance_cmH2O_s_per_L": float(raw.real),
        "raw_reactance_cmH2O_s_per_L": float(raw.imag),
        "measured_resistance_cmH2O_s_per_L": measured_r,
        "pressure_amplitude_cmH2O": pressure_amplitude,
        "flow_amplitude_lps": flow_amplitude,
        "mean_flow_lps": float(np.mean([b.mean_flow for b in blocks])),
        "block_variation_percent": float(variation * 100.0),
    })

    reasons = result["reasons"]
    if flow_amplitude < MIN_FLOW_AMPLITUDE_LPS:
        reasons.append(
            f"flow wave too small ({flow_amplitude:.4f} L/s, needs at least "
            f"{MIN_FLOW_AMPLITUDE_LPS:g})"
        )
    if not np.isfinite(variation) or variation > MAX_BLOCK_RELATIVE_DEVIATION:
        reasons.append(
            f"reading not steady ({variation * 100:.0f}% variation, limit "
            f"{MAX_BLOCK_RELATIVE_DEVIATION * 100:.0f}%)"
        )
    if abs(angle) > MAX_CALIBRATION_CORRECTION_DEG:
        reasons.append(
            f"phase offset {angle:+.1f}° is larger than ±"
            f"{MAX_CALIBRATION_CORRECTION_DEG:g}°"
        )
    if not (np.isfinite(gain) and GAIN_MIN <= gain <= GAIN_MAX):
        reasons.append(
            f"measured {measured_r:.2f} against {reference_resistance:.2f}: "
            f"a correction of ×{gain:.2f} is too large to trust"
        )
    result["passed"] = not reasons
    return result


def failed_frequency(hz: float, reference_resistance: float, error) -> dict:
    """Result for a frequency that could not be recorded at all."""
    return {
        "excitation_hz": float(hz),
        "reference_resistance_cmH2O_s_per_L": float(reference_resistance),
        "blocks_used": 0,
        "passed": False,
        "reasons": [str(error)],
    }


def phase_record(result: dict, saved_utc: str) -> dict:
    """fot_phase_calibration_{hz}hz.json, keeping the keys the engine and the old
    phase tab read (correction_angle_deg above all)."""
    angle = result["correction_angle_deg"]
    return {
        "method": METHOD,
        "reference_resistance_cmH2O_s_per_L": result["reference_resistance_cmH2O_s_per_L"],
        "correction_angle_deg": angle,
        "raw_resistance_cmH2O_s_per_L": result["raw_resistance_cmH2O_s_per_L"],
        "raw_reactance_cmH2O_s_per_L": result["raw_reactance_cmH2O_s_per_L"],
        "corrected_resistance_cmH2O_s_per_L": result["measured_resistance_cmH2O_s_per_L"],
        "corrected_reactance_cmH2O_s_per_L": 0.0,
        "resistance_error_percent": (
            (result["measured_resistance_cmH2O_s_per_L"]
             - result["reference_resistance_cmH2O_s_per_L"])
            / result["reference_resistance_cmH2O_s_per_L"] * 100.0
        ),
        "blocks_used": result["blocks_used"],
        "created_utc": saved_utc,
        "excitation_hz": result["excitation_hz"],
        "sample_hz": result["excitation_hz"] * LUT_SIZE,
        "block_samples": BLOCK_SAMPLES,
        "quality_valid": True,
        "quality_reason": "OK",
        "block_variation_percent": result["block_variation_percent"],
        "phase_pass": True,
        "calibration_pass": True,
    }


def gain_record(result: dict, saved_utc: str) -> dict:
    """fot_bias_calibration_{hz}hz.json holding only the gain. Every bias-flow term
    is zero, so applying this file is exactly "multiply by the gain"."""
    hz = result["excitation_hz"]
    return {
        "method": METHOD,
        "excitation_hz": hz,
        "reference_resistance": result["reference_resistance_cmH2O_s_per_L"],
        "scale": result["gain"],
        "k_plumbing": 0.0,
        "k_x": 0.0,
        "k_plumbing_poly": [0.0],
        "k_x_poly": [0.0],
        "x_residual_at_zero": 0.0,
        "clamp_q_high": None,
        "measured_resistance_cmH2O_s_per_L": result["measured_resistance_cmH2O_s_per_L"],
        "pressure_amplitude_cmH2O": result["pressure_amplitude_cmH2O"],
        "flow_amplitude_lps": result["flow_amplitude_lps"],
        "block_variation_percent": result["block_variation_percent"],
        "phase_correction_deg": result["correction_angle_deg"],
        "phase_calibration_file": phase_calibration_path_for(hz).name,
        "saved_utc": saved_utc,
        "saved_from": "Gain & Phase Calibration",
    }


def _archive(path: Path, archive_dir: Path, stamp: str) -> None:
    if path.exists():
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, archive_dir / f"{path.stem}_{stamp}{path.suffix}")


def save_results(results: Iterable[dict], archive_dir: Path) -> list[float]:
    """Write every passed frequency's phase and gain files and put them in force.
    Failed frequencies keep whatever calibration they had. Returns the frequencies
    saved."""
    # Imported here: the presets module imports FOTPhaseCalibration, which this
    # module also imports, and keeping it local avoids an import cycle at startup.
    from backend.processing.FOTCalibrationPresets import default_store

    now = datetime.now(timezone.utc)
    saved_utc = now.isoformat()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    saved = []
    for result in results:
        if not result.get("passed"):
            continue
        hz = result["excitation_hz"]
        phase_path = phase_calibration_path_for(hz)
        gain_path = bias_calibration_path_for(hz)
        _archive(phase_path, Path(archive_dir), stamp)
        _archive(gain_path, Path(archive_dir), stamp)
        save_calibration(phase_path, phase_record(result, saved_utc))
        save_calibration(gain_path, gain_record(result, saved_utc))
        # A time-of-day preset with auto-apply on would copy its old files back over
        # these the next time a recording is processed.
        store = default_store(hz)
        if store.path.exists() and store.load()["auto_apply"]:
            store.set_auto_apply(False)
        saved.append(hz)

    if EXCITATION_HZ in saved:
        load_bias_calibration_for(EXCITATION_HZ)
    return saved


def current_calibration(hz: float) -> dict:
    """What is saved for one frequency, for display."""
    phase = load_calibration(phase_calibration_path_for(hz)) or {}
    gain = load_calibration(bias_calibration_path_for(hz)) or {}
    return {
        "excitation_hz": float(hz),
        "gain": gain.get("scale"),
        "phase_deg": phase.get("correction_angle_deg"),
        "saved_utc": gain.get("saved_utc") or phase.get("created_utc"),
        "method": gain.get("method") or ("cpap_sweep" if gain else None),
        "has_bias_polynomial": any(
            abs(float(c)) > 0 for c in (gain.get("k_plumbing_poly") or [])
        ),
    }
