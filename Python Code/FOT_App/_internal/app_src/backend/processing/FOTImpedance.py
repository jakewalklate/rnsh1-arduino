"""Device-grade single-frequency lock-in and H1 impedance calculations.

The device supplies a phase index from a 70-sample excitation lookup table.
Using that index as the lock-in reference keeps the analysis phase-locked to
the actual generated tone without relying on PC arrival timestamps.

The lock-in itself is frequency-independent (it only sees the 0..69 index). What
depends on the excitation frequency is the sample rate (hz x 70), the block length
(one second = round(hz) whole cycles) and which calibration applies. Use the
*_for(hz) helpers and bias_calibration_for(hz) for those; the module-level
EXCITATION_HZ / SAMPLE_HZ / BLOCK_SAMPLES are only the 6 Hz defaults.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
from pathlib import Path
import threading
from typing import Iterable

import numpy as np


EXCITATION_HZ = 6.0   # default frequency (app start-up, tools); not an analysis limit
LUT_SIZE = 70


def cycles_per_block(hz: float) -> int:
    """Whole excitation cycles in one ~1 s block: 5 at 5 Hz, 6 at 6 Hz, 11 at 11 Hz,
    19 at 19 Hz. WHY one second at every frequency: block-to-block variation is a QC
    gate (MAX_BLOCK_RELATIVE_DEVIATION) and every recorder counts blocks as seconds.
    A fixed cycle count made blocks 0.32 s long at 19 Hz, which roughly doubled the
    measured variation there and failed the gain/phase calibration on noise alone."""
    return max(1, int(round(float(hz))))


def block_samples_for(hz: float) -> int:
    return cycles_per_block(hz) * LUT_SIZE


def sample_hz_for(hz: float) -> float:
    """The device streams LUT_SIZE rows per excitation cycle."""
    return float(hz) * LUT_SIZE


CYCLES_PER_BLOCK = cycles_per_block(EXCITATION_HZ)
SAMPLE_HZ = sample_hz_for(EXCITATION_HZ)
BLOCK_SAMPLES = block_samples_for(EXCITATION_HZ)
BLOCKS_PER_WINDOW = 10
REFERENCE_RESISTANCE = 13.62

MIN_FLOW_AMPLITUDE_LPS = 0.01
PRESSURE_FULL_SCALE_CMH2O = 25.4
FLOW_FULL_SCALE_LPS = min(2.568909, 2.651078) * 5.08
CLIP_FRACTION = 0.95
MAX_BLOCK_RELATIVE_DEVIATION = 0.20
MAX_CALIBRATION_CORRECTION_DEG = 30.0

# Bias-flow + baseline correction. Recovers
#   R_true = SCALE * R_measured - K_PLUMBING * Q_bias,
# which flattens the 13.62 load to ~nominal at any CPAP bias flow. SCALE fixes the
# multiplicative flow-gain/baseline shortfall (R = P/Q, so a gain error scales R);
# K_PLUMBING removes the resistive turbulent bias-flow term (hits R only, not X).
# Tied to the CURRENT flowhead + tubing: re-derive BOTH constants if either changes,
# using FOTTubeCalibration.py (CPAP sweep -> straight-line fit -> these two numbers).
#
# CURRENT (2026-07-22, NEW TUBE, speaker re-tightened after an air leak):
#   FOTTubeCalibration.py sweep, 11 CPAP steps 0..20, logs/tube_calibration_20260722_160400
#   phase correction -3.000 deg, 1 window per point
#   Fit: R_phase = 11.6711 + 7.2712 * Q_bias   (R^2 = 0.9328)
#   -> SCALE = 13.62 / 11.6711, K_PLUMBING = 7.2712 * SCALE
#   CAVEATS accepted by Henry at this prototype stage: R^2 is only 0.933; the CPAP 16
#   point is an outlier (residual -1.70) and dropping it would give SCALE 1.1823 /
#   K 9.1933, i.e. K moves 8.3%. Residuals are positive at both ends and negative in
#   the middle, hinting at mild curvature the linear model does not capture.
#
# K_X is the reactance analogue of K_PLUMBING: on the resistive reference load X
# should be 0, but it drifts with CPAP bias flow (X_phase = c + d*Q_bias), a pure
# rig artifact. K_X = SCALE * d removes it: X_true = SCALE * X_measured - K_X * Q_bias.
# Default 0.0 = no X bias correction (backward compatible); set it via a
# FOTTubeCalibration.py sweep + Save, which writes a per-frequency JSON (below).
#
# PREVIOUS (old tube, NewTake T1 sweep, flow gain ~2.79):
#   Fit: R_measured = 11.375 + 7.287 * Q_bias -> SCALE = 1.1974, K_PLUMBING = 8.726
#   Plan: okay-so-offset-it-tidy-metcalfe.md.
BIAS_FLOW_CORRECTION_ENABLED = True
_DEFAULT_BIAS_CORR_SCALE = 1.1670        # 13.62 / 11.6711 (measured zero-flow intercept)
_DEFAULT_BIAS_CORR_K_PLUMBING = 8.4854   # 7.2712 (measured slope) * SCALE
_DEFAULT_BIAS_CORR_K_X = 0.0             # reactance bias term, off until calibrated
BIAS_CORR_SCALE = _DEFAULT_BIAS_CORR_SCALE
BIAS_CORR_K_PLUMBING = _DEFAULT_BIAS_CORR_K_PLUMBING
BIAS_CORR_K_X = _DEFAULT_BIAS_CORR_K_X

# Polynomial generalisation of the two bias-flow terms. Each list holds OUTPUT-UNIT
# coefficients for Q^1, Q^2, ... with NO constant term, so the correction is 0 at zero
# flow. A single-element list [k] reproduces the old scalar behaviour, so these default
# to the scalars above and a fresh checkout is unchanged. The scalars above are kept in
# sync with poly[0] (the Q^1 term) so modules importing the scalar still work.
# WHY (device-grade): the 13.62 load's R-vs-bias-flow curve is S-shaped, which a
# straight line cannot follow; a higher order tracks it far better. Coefficients live
# in fot_bias_calibration_{hz}hz.json (written by FOTTubeCalibration.py); source stays
# the linear fallback.
_DEFAULT_BIAS_CORR_K_PLUMBING_POLY = [_DEFAULT_BIAS_CORR_K_PLUMBING]
_DEFAULT_BIAS_CORR_K_X_POLY = [_DEFAULT_BIAS_CORR_K_X]
BIAS_CORR_K_PLUMBING_POLY = list(_DEFAULT_BIAS_CORR_K_PLUMBING_POLY)
BIAS_CORR_K_X_POLY = list(_DEFAULT_BIAS_CORR_K_X_POLY)

# Constant offset on X, in output units, subtracted alongside the X polynomial.
# WHY: on the reference load the truth is R = REFERENCE_RESISTANCE at every Q and
# X = 0 at every Q, so EVERY term of the fitted X curve is instrument error, its
# constant included. R's constant is not discarded -- it becomes SCALE (= reference
# / intercept), which anchors R to the load. X had no equivalent anchor: the fit's
# constant was dropped from k_x_poly and nothing replaced it, so the corrected X sat
# at SCALE * x_intercept instead of 0. That intercept is an EXTRAPOLATION to Q=0 from
# a sweep whose lowest point is around Q=0.01, and a high-order fit can put it
# anywhere: the 2026-08-03 14:22 calibration extrapolated to -1.644, parking every
# reading near -2.17 and failing the system check at every CPAP while the underlying
# measurement was fine. Subtracting it makes X the pure fit residual, and because the
# same intercept then appears in both the scaled reading and the subtraction it
# cancels exactly, so the correction no longer depends on that extrapolation at all.
# The value is scale * x_intercept, already written to every calibration file as
# "x_residual_at_zero". 0.0 = uncalibrated, i.e. the historical behaviour.
_DEFAULT_BIAS_CORR_X_OFFSET = 0.0
BIAS_CORR_X_OFFSET = _DEFAULT_BIAS_CORR_X_OFFSET

# High-end clamp on Q_bias before the polynomial is evaluated: a high-order fit
# diverges outside its measured range, so above clamp_q_high the correction is frozen
# at its endpoint. None = no clamp. The low end floors at 0, where the deviation
# polynomial is 0 by construction.
_DEFAULT_BIAS_CORR_Q_CLAMP_HIGH = None
BIAS_CORR_Q_CLAMP_HIGH = _DEFAULT_BIAS_CORR_Q_CLAMP_HIGH

_BASE_DIR = Path(__file__).resolve().parent

# Frequency whose bias calibration is loaded into the BIAS_CORR_* globals.
ACTIVE_BIAS_HZ = EXCITATION_HZ
_BIAS_LOCK = threading.RLock()


def bias_calibration_path_for(hz: float) -> Path:
    """Per-frequency bias-flow calibration file, parallel to the phase-calibration
    naming used by PressureSensorReading.py."""
    return _BASE_DIR / f"fot_bias_calibration_{hz:g}hz.json"


def load_bias_calibration_for(hz: float) -> dict | None:
    """Set the active BIAS_CORR_* module constants from the per-frequency JSON if it
    exists, else reset them to the hardcoded defaults. Returns the loaded dict (or
    None if absent). Called once at import for the default frequency; callers that
    change frequency may call it again. Because bias_flow_corrected() reads these as
    module globals at call time, updating them here takes effect immediately for the
    bench path; the patient path in PressureSensorReading reads them the same way."""
    global BIAS_CORR_SCALE, BIAS_CORR_K_PLUMBING, BIAS_CORR_K_X
    global BIAS_CORR_K_PLUMBING_POLY, BIAS_CORR_K_X_POLY, BIAS_CORR_Q_CLAMP_HIGH
    global BIAS_CORR_X_OFFSET, ACTIVE_BIAS_HZ
    ACTIVE_BIAS_HZ = float(hz)
    data = load_calibration(bias_calibration_path_for(hz))
    if data is None:
        BIAS_CORR_SCALE = _DEFAULT_BIAS_CORR_SCALE
        BIAS_CORR_K_PLUMBING = _DEFAULT_BIAS_CORR_K_PLUMBING
        BIAS_CORR_K_X = _DEFAULT_BIAS_CORR_K_X
        BIAS_CORR_K_PLUMBING_POLY = list(_DEFAULT_BIAS_CORR_K_PLUMBING_POLY)
        BIAS_CORR_K_X_POLY = list(_DEFAULT_BIAS_CORR_K_X_POLY)
        BIAS_CORR_Q_CLAMP_HIGH = _DEFAULT_BIAS_CORR_Q_CLAMP_HIGH
        BIAS_CORR_X_OFFSET = _DEFAULT_BIAS_CORR_X_OFFSET
        return None
    BIAS_CORR_SCALE = float(data.get("scale", _DEFAULT_BIAS_CORR_SCALE))
    # Polynomial arrays (Q^1.. coeffs, output units) if present, else fall back to the
    # scalar linear term so an older scalar-only JSON keeps working unchanged.
    plumbing_poly = data.get("k_plumbing_poly")
    if plumbing_poly:
        BIAS_CORR_K_PLUMBING_POLY = [float(c) for c in plumbing_poly]
    else:
        BIAS_CORR_K_PLUMBING_POLY = [
            float(data.get("k_plumbing", _DEFAULT_BIAS_CORR_K_PLUMBING))
        ]
    x_poly = data.get("k_x_poly")
    if x_poly:
        BIAS_CORR_K_X_POLY = [float(c) for c in x_poly]
    else:
        BIAS_CORR_K_X_POLY = [float(data.get("k_x", _DEFAULT_BIAS_CORR_K_X))]
    # Keep the scalar names in sync with the Q^1 term for modules that import them.
    BIAS_CORR_K_PLUMBING = BIAS_CORR_K_PLUMBING_POLY[0]
    BIAS_CORR_K_X = BIAS_CORR_K_X_POLY[0]
    clamp_high = data.get("clamp_q_high")
    BIAS_CORR_Q_CLAMP_HIGH = float(clamp_high) if clamp_high is not None else None
    # Pre-poly files have no x_residual_at_zero; 0.0 keeps them behaving as before.
    BIAS_CORR_X_OFFSET = float(data.get("x_residual_at_zero", 0.0))
    return data


@contextmanager
def bias_calibration_for(hz: float):
    """Analyse with the bias/gain calibration of `hz` in force, then put back the
    one that was active before. Yields the loaded dict, or None when that frequency
    has never been calibrated (the hardcoded defaults are then in force, and the
    caller should say so rather than present the result as calibrated).

    The BIAS_CORR_* values are module globals read at call time, so a recording made
    at 11 Hz used to be corrected with the 6 Hz gain. Holding a lock for the duration
    stops two analyses at different frequencies swapping the globals under each other."""
    with _BIAS_LOCK:
        previous = ACTIVE_BIAS_HZ
        try:
            yield load_bias_calibration_for(hz)
        finally:
            load_bias_calibration_for(previous)


def save_bias_calibration(hz: float, payload: dict) -> Path:
    """Write the per-frequency bias-flow calibration JSON and return its path. The
    Save button in FOTTubeCalibration.py calls this; the main app picks the values
    up on its next launch (import-time load), no source edit ever needed."""
    path = bias_calibration_path_for(hz)
    save_calibration(path, payload)
    return path


@dataclass(frozen=True)
class BlockPhasor:
    pressure: complex
    flow: complex
    pressure_amplitude: float
    flow_amplitude: float
    peak_pressure: float
    peak_flow: float
    mean_flow: float


@dataclass(frozen=True)
class ImpedanceResult:
    raw: complex
    corrected: complex
    bias_corrected: complex
    pressure_amplitude: float
    flow_amplitude: float
    bias_flow_lps: float
    raw_phase_deg: float
    correction_deg: float
    valid: bool
    reason: str
    block_relative_deviation: float

    @property
    def resistance(self) -> float:
        return float(self.bias_corrected.real)

    @property
    def reactance(self) -> float:
        return float(self.bias_corrected.imag)


def expected_next_index(index: int) -> int:
    return (int(index) + 1) % LUT_SIZE


def validate_index_sequence(
    indices: Iterable[int], expected_samples: int = BLOCK_SAMPLES
) -> tuple[bool, str]:
    idx = np.asarray(list(indices), dtype=int)
    if len(idx) != expected_samples:
        return False, f"expected {expected_samples} samples, received {len(idx)}"
    if np.any((idx < 0) | (idx >= LUT_SIZE)):
        return False, "LUT index outside 0..69"
    expected = (idx[:-1] + 1) % LUT_SIZE
    bad = np.flatnonzero(idx[1:] != expected)
    if len(bad):
        return False, f"LUT discontinuity after sample {int(bad[0])}"
    return True, "OK"


def lock_in_phasor(values: Iterable[float], indices: Iterable[int]) -> complex:
    """Lock-in phasor for one block, Hann-tapered.

    WHY the taper: a block is a hard cut out of a continuous record, so unless the
    signal happens to start and end at the same value there is a STEP at each edge.
    A step is broadband, so energy from everything else in the record -- above all the
    breathing waveform, which on a patient is ~12x the size of the 6 Hz tone -- leaks
    into the 6 Hz bin and is counted as impedance. That is what produced blocks with
    R jumping -0.8 -> 14.2 -> 2.1 in three consecutive seconds on MEAS0007, and the
    physically impossible negative-R blocks with them. A Hann taper fades the block in
    and out so there is no edge step, and the leak goes with it (measured on the three
    real patient records: block variation 124.0% -> 29.1%, 91.9% -> 24.7%, and every
    negative-R block gone).

    Normalisation is coherent gain (2 * sum(x*w*ref) / sum(w)), the same convention as
    FOTBreath.short_window_phasor, so the returned amplitude is in the same units as
    before and the P/Q ratio is unchanged for a clean tone. On a noise-free integer-
    cycle block, tapered and rectangular agree, so the bench/calibration path is
    unaffected; only leak-contaminated blocks move.
    """
    x = np.asarray(list(values), dtype=float)
    idx = np.asarray(list(indices), dtype=float)
    if len(x) != len(idx) or len(x) == 0:
        raise ValueError("values and indices must have equal non-zero length")
    x = x - np.mean(x)
    window = np.hanning(len(x)) if len(x) > 1 else np.ones(1)
    denominator = np.sum(window)
    if denominator <= 0.0:
        raise ValueError("window has zero sum")
    reference = np.exp(-1j * 2.0 * np.pi * idx / LUT_SIZE)
    return complex(2.0 * np.sum(x * window * reference) / denominator)


def make_block_phasor(
    indices: Iterable[int],
    pressure_cm_h2o: Iterable[float],
    flow_lps: Iterable[float],
    expected_samples: int = BLOCK_SAMPLES,
) -> BlockPhasor:
    """`expected_samples` is block_samples_for(hz) of the recording's frequency."""
    idx = np.asarray(list(indices), dtype=int)
    pressure = np.asarray(list(pressure_cm_h2o), dtype=float)
    flow = np.asarray(list(flow_lps), dtype=float)
    if len(pressure) != expected_samples or len(flow) != expected_samples:
        raise ValueError(f"blocks must contain exactly {expected_samples} samples")
    valid, reason = validate_index_sequence(idx, expected_samples)
    if not valid:
        raise ValueError(reason)
    p_phasor = lock_in_phasor(pressure, idx)
    q_phasor = lock_in_phasor(flow, idx)
    return BlockPhasor(
        pressure=p_phasor,
        flow=q_phasor,
        pressure_amplitude=abs(p_phasor),
        flow_amplitude=abs(q_phasor),
        peak_pressure=float(np.max(np.abs(pressure))),
        peak_flow=float(np.max(np.abs(flow))),
        mean_flow=float(np.mean(flow)),
    )


def h1_impedance(blocks: Iterable[BlockPhasor]) -> complex:
    blocks = list(blocks)
    if not blocks:
        raise ValueError("at least one block is required")
    numerator = sum(block.pressure * np.conj(block.flow) for block in blocks)
    denominator = sum(abs(block.flow) ** 2 for block in blocks)
    if denominator <= 0.0:
        raise ValueError("flow energy is zero")
    return complex(numerator / denominator)


def rotate_impedance(raw: complex, correction_deg: float) -> complex:
    return complex(raw * np.exp(-1j * np.deg2rad(correction_deg)))


def _bias_poly(coeffs: list[float], q: float) -> float:
    """Evaluate sum_i coeffs[i] * q**(i+1): powers start at Q^1, so the result is 0 at
    q=0 (no constant term). coeffs are already in output units."""
    total = 0.0
    power = q
    for c in coeffs:
        total += c * power
        power *= q
    return total


def _clamp_bias_flow(bias_flow_lps: float) -> float:
    """Floor Q_bias at 0 and, when a calibration sets one, cap it at
    BIAS_CORR_Q_CLAMP_HIGH so a high-order fit cannot diverge outside its measured
    range. Shared by every path that evaluates the bias-flow polynomials."""
    q = float(bias_flow_lps)
    if q < 0.0:
        q = 0.0
    if BIAS_CORR_Q_CLAMP_HIGH is not None and q > BIAS_CORR_Q_CLAMP_HIGH:
        q = BIAS_CORR_Q_CLAMP_HIGH
    return q


def bias_flow_adjustments(bias_flow_lps: float) -> tuple[float, float]:
    """Additive bias-flow corrections for a given bias flow, evaluated from the SAME
    clamped polynomials the bench path uses. Returns (k_plumbing_adj, k_x_adj) in
    output units: k_plumbing_adj is subtracted off R, k_x_adj off X. Each is the full
    polynomial sum, so a single-coefficient list reproduces the old linear scalar and a
    higher-order fit applies its full curve. SCALE is NOT included here; callers that
    already report SCALE-applied R5/X5 (the patient/medical path) subtract these
    directly. This is the single source of truth so the bench and patient paths cannot
    disagree above degree 1: pick linear at calibration and the whole system is linear;
    pick a polynomial and the whole system uses that polynomial.

    The X term carries BIAS_CORR_X_OFFSET on top of its polynomial, so the correction
    removes the WHOLE fitted X error curve rather than all of it except the constant.
    See the constant's definition for why. R needs no counterpart: its constant is
    already folded into SCALE."""
    q = _clamp_bias_flow(bias_flow_lps)
    return (
        _bias_poly(BIAS_CORR_K_PLUMBING_POLY, q),
        _bias_poly(BIAS_CORR_K_X_POLY, q) + BIAS_CORR_X_OFFSET,
    )


def bias_flow_corrected(phase_corrected: complex, bias_flow_lps: float) -> complex:
    """Scale for the flow-gain/baseline shortfall, then remove the bias-flow terms: a
    plumbing polynomial off the resistance and an X polynomial off the reactance. Both
    polynomials have no constant term (0 at zero flow) and default to a single linear
    coefficient, so with no bias calibration this is the historical R-only linear
    behaviour. Q_bias is floored at 0 and, when a calibration sets a clamp, capped at
    BIAS_CORR_Q_CLAMP_HIGH so a high-order fit cannot diverge outside its measured
    range. Pure math; the caller decides whether to apply it. The additive terms come
    from bias_flow_adjustments() so the patient path uses the exact same correction."""
    scaled = phase_corrected * BIAS_CORR_SCALE
    k_plumbing_adj, k_x_adj = bias_flow_adjustments(bias_flow_lps)
    return complex(
        scaled.real - k_plumbing_adj,
        scaled.imag - k_x_adj,
    )


def scale_only(phase_corrected: complex) -> complex:
    """Apply the MULTIPLICATIVE half of the bench correction and nothing else.

    This is the "Bias-flow correction: Off" basis. SCALE is a plain gain calibration
    measured against the reference load and is not in dispute, so it stays. What is
    dropped is every flow-indexed term: both polynomials and BIAS_CORR_X_OFFSET.

    Why the distinction exists: the polynomials were fitted against a STEADY CPAP bias
    flow on the bench. A patient breathing through a mask produces no steady flow, so
    `mean(flow)` there is sensor drift rather than a physical bias flow, and feeding it
    to the polynomial subtracts a large, meaningless, breath-varying amount. See the
    2026-08-05 handover for the measurements behind this."""
    return complex(phase_corrected * BIAS_CORR_SCALE)


def angle_difference_deg(first: float, second: float) -> float:
    return abs((float(first) - float(second) + 180.0) % 360.0 - 180.0)


def _block_variation(blocks: list[BlockPhasor], combined: complex) -> float:
    if abs(combined) <= 1e-12:
        return math.inf
    individual = [
        block.pressure / block.flow
        for block in blocks
        if abs(block.flow) >= MIN_FLOW_AMPLITUDE_LPS
    ]
    if len(individual) < 2:
        return math.inf
    deviations = [abs(value - combined) / abs(combined) for value in individual]
    return float(np.sqrt(np.mean(np.square(deviations))))


def analyse_blocks(
    blocks: Iterable[BlockPhasor],
    correction_deg: float = 0.0,
    required_blocks: int = BLOCKS_PER_WINDOW,
    apply_bias_polynomial: bool = True,
    excitation_hz: float = EXCITATION_HZ,
) -> ImpedanceResult:
    """`apply_bias_polynomial=False` selects the SCALE-only basis (see scale_only).
    It is an explicit argument rather than a module flag because the app reports both
    bases for the same recording and a global would make the answer depend on call
    order. BIAS_FLOW_CORRECTION_ENABLED remains the master switch above it: with that
    off, neither basis applies anything and this argument has no effect."""
    blocks = list(blocks)
    if len(blocks) < required_blocks:
        return ImpedanceResult(
            raw=complex(np.nan, np.nan),
            corrected=complex(np.nan, np.nan),
            bias_corrected=complex(np.nan, np.nan),
            pressure_amplitude=0.0,
            flow_amplitude=0.0,
            bias_flow_lps=0.0,
            raw_phase_deg=0.0,
            correction_deg=correction_deg,
            valid=False,
            reason=f"warming up: {len(blocks)}/{required_blocks} blocks",
            block_relative_deviation=math.inf,
        )

    blocks = blocks[-required_blocks:]
    pressure_amplitude = float(np.mean([b.pressure_amplitude for b in blocks]))
    flow_amplitude = float(np.mean([b.flow_amplitude for b in blocks]))
    peak_pressure = max(b.peak_pressure for b in blocks)
    peak_flow = max(b.peak_flow for b in blocks)

    reasons: list[str] = []
    if flow_amplitude < MIN_FLOW_AMPLITUDE_LPS:
        reasons.append(
            f"{float(excitation_hz):g} Hz flow amplitude {flow_amplitude:.4f} L/s below "
            f"{MIN_FLOW_AMPLITUDE_LPS:.4f} L/s"
        )
    if peak_pressure >= CLIP_FRACTION * PRESSURE_FULL_SCALE_CMH2O:
        reasons.append("pressure channel near full scale")
    if peak_flow >= CLIP_FRACTION * FLOW_FULL_SCALE_LPS:
        reasons.append("flow channel near full scale")

    try:
        raw = h1_impedance(blocks)
    except ValueError as error:
        raw = complex(np.nan, np.nan)
        reasons.append(str(error))

    corrected = rotate_impedance(raw, correction_deg)
    bias_flow_lps = float(np.mean([b.mean_flow for b in blocks]))
    if not BIAS_FLOW_CORRECTION_ENABLED:
        bias_corrected = corrected
    elif apply_bias_polynomial:
        bias_corrected = bias_flow_corrected(corrected, bias_flow_lps)
    else:
        bias_corrected = scale_only(corrected)
    variation = _block_variation(blocks, raw)
    if not np.isfinite(variation) or variation > MAX_BLOCK_RELATIVE_DEVIATION:
        reasons.append(
            f"block variation {variation * 100:.1f}% exceeds "
            f"{MAX_BLOCK_RELATIVE_DEVIATION * 100:.0f}%"
        )

    return ImpedanceResult(
        raw=raw,
        corrected=corrected,
        bias_corrected=bias_corrected,
        pressure_amplitude=pressure_amplitude,
        flow_amplitude=flow_amplitude,
        bias_flow_lps=bias_flow_lps,
        raw_phase_deg=float(np.rad2deg(np.angle(raw))),
        correction_deg=float(correction_deg),
        valid=not reasons,
        reason="OK" if not reasons else "; ".join(reasons),
        block_relative_deviation=variation,
    )


def calibration_from_blocks(
    blocks: Iterable[BlockPhasor],
    reference_resistance: float = REFERENCE_RESISTANCE,
) -> dict:
    blocks = list(blocks)
    raw = h1_impedance(blocks)
    correction_deg = float(np.rad2deg(np.angle(raw)))
    corrected = rotate_impedance(raw, correction_deg)
    resistance_error_percent = (
        (corrected.real - reference_resistance) / reference_resistance * 100.0
    )
    return {
        "reference_resistance_cmH2O_s_per_L": float(reference_resistance),
        "correction_angle_deg": correction_deg,
        "raw_resistance_cmH2O_s_per_L": float(raw.real),
        "raw_reactance_cmH2O_s_per_L": float(raw.imag),
        "corrected_resistance_cmH2O_s_per_L": float(corrected.real),
        "corrected_reactance_cmH2O_s_per_L": float(corrected.imag),
        "resistance_error_percent": float(resistance_error_percent),
        "blocks_used": len(blocks),
    }


def filter_calibration_from_blocks(
    blocks: Iterable[BlockPhasor],
    correction_deg: float = 0.0,
    apply_scale: bool = BIAS_FLOW_CORRECTION_ENABLED,
) -> dict:
    """Reduce blocks captured with a breathing filter open to atmosphere into the
    filter's own impedance, on the SAME basis the patient path reports R5/X5:
    phase-corrected, then multiplied by the flow-gain SCALE when apply_scale is set
    (the multiplicative half of the bench correction). The additive K_PLUMBING*Q_bias
    term is deliberately NOT applied here, matching phase_resolved_breaths, and is
    irrelevant anyway because an open-to-air filter carries ~0 bias flow. The stored
    R/X can therefore be subtracted directly from the live patient R5/X5."""
    blocks = list(blocks)
    raw = h1_impedance(blocks)
    corrected = rotate_impedance(raw, correction_deg)
    filter_z = corrected * BIAS_CORR_SCALE if apply_scale else corrected
    return {
        "filter_R_cmH2O_s_per_L": float(filter_z.real),
        "filter_X_cmH2O_s_per_L": float(filter_z.imag),
        "raw_R_cmH2O_s_per_L": float(raw.real),
        "raw_X_cmH2O_s_per_L": float(raw.imag),
        "correction_angle_deg": float(correction_deg),
        "scale_applied": bool(apply_scale),
        "blocks_used": len(blocks),
    }


def subtract_filter(z: complex, filter_r: float, filter_x: float = 0.0) -> complex:
    """Series subtraction of a filter's impedance from a measured impedance.
    Defaults to resistance-only (filter_x=0), which leaves the reactance untouched."""
    return complex(z.real - float(filter_r), z.imag - float(filter_x))


def load_calibration(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_calibration(path: Path, calibration: dict) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(calibration, file, indent=2)
        file.write("\n")


# Load any saved per-frequency bias calibration at import, AFTER load_calibration is
# defined. This runs before other modules resolve `from FOTImpedance import
# BIAS_CORR_*`, so they all pick up saved values with no source edit. Absent file =
# hardcoded defaults, so a fresh checkout behaves exactly as before.
load_bias_calibration_for(EXCITATION_HZ)
