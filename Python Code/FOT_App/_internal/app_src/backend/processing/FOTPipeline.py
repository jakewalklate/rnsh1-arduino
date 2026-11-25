import csv
import math

import numpy as np

from backend.database.storage import get_measurement_excitation_hz
from backend.device.sample_csv_writer import get_measurement_csv_path
from backend.processing.FOTAcceptance import (
    FLOOR,
    flag_outlier_breaths,
    qc_breath,
    summarise_take,
)
from backend.processing import FOTBreath, FOTImpedance
from backend.processing.FOTBreath import phase_resolved_breaths
from backend.processing.FOTCalibrationPresets import ensure_auto_applied_calibration
from backend.processing.FOTPhaseCalibration import (
    active_correction_deg,
    phase_calibration_path_for,
)
from backend.processing.FOTImpedance import (
    BIAS_FLOW_CORRECTION_ENABLED,
    BLOCKS_PER_WINDOW,
    EXCITATION_HZ,
    LUT_SIZE,
    analyse_blocks,
    bias_calibration_for,
    bias_flow_adjustments,
    block_samples_for,
    make_block_phasor,
    sample_hz_for,
)


# Which calibration basis a reported R5/X5 sits on. Recorded in the status string
# because the two paths below used to differ, and a result that does not say which
# basis produced it cannot be compared with another one.
TIER_BIAS_CORRECTED = "bias-corrected"

# The SCALE-only basis, reported alongside the bias-corrected one so a measurement can be
# read either way. See FOTImpedance.scale_only for why the two differ.
TIER_SCALE_ONLY = "scale-only (bias-flow correction off)"


def process_measurement_csv(measurement_id, apply_bias_polynomial=True, excitation_hz=None):
    """`apply_bias_polynomial=False` reports the same recording on the SCALE-only basis:
    the multiplicative gain calibration stays, every flow-indexed term is dropped.

    Passed explicitly rather than read from a module flag because both bases are
    computed for the same measurement and stored side by side; a global would make the
    result depend on which call ran last.

    The recording is analysed at the frequency it was RECORDED at (stored with the
    measurement; `excitation_hz` overrides it): its sample rate, its 1 s blocks, and
    that frequency's own phase angle and gain. Previously every recording was analysed
    as 6 Hz, so 5/11/19 Hz recordings got the 6 Hz calibration, the wrong block length
    and a tidal volume scaled by 420/(70*hz)."""
    if excitation_hz is None:
        excitation_hz = get_measurement_excitation_hz(measurement_id)
    hz = float(excitation_hz) if excitation_hz else EXCITATION_HZ

    csv_path = get_measurement_csv_path(measurement_id)

    # With auto-apply on, make sure the preset for the current time of day is the one in
    # force BEFORE reading any calibration below. A clinic that leaves the app open
    # across noon would otherwise analyse an afternoon recording against the morning
    # calibration. No-op when auto-apply is off or the period has not changed.
    ensure_auto_applied_calibration(hz)

    if not csv_path.exists():
        return _empty_results(csv_path, "Missing CSV")

    with bias_calibration_for(hz) as bias_calibration:
        return _process_samples(
            csv_path, hz, apply_bias_polynomial, bias_calibration is not None
        )


def _process_samples(csv_path, hz, apply_bias_polynomial, gain_calibrated):
    """Body of process_measurement_csv, run with `hz`'s bias calibration loaded."""
    row_count, samples = _read_measurement_samples(csv_path, hz)

    if row_count == 0:
        return _empty_results(csv_path, "CSV contains no samples")

    if not samples:
        return {
            **_empty_results(csv_path, "CSV samples could not be parsed"),
            "sample_count": row_count,
        }

    correction_deg = active_correction_deg(hz)
    blocks = _make_impedance_blocks(samples, hz)
    impedance_result = analyse_blocks(
        blocks,
        correction_deg=correction_deg,
        required_blocks=BLOCKS_PER_WINDOW,
        apply_bias_polynomial=apply_bias_polynomial,
        excitation_hz=hz,
    )

    breath_result = _analyse_breath_qc(
        samples, hz, correction_deg, apply_bias_polynomial=apply_bias_polynomial
    )
    phase_calibrated = phase_calibration_path_for(hz).exists()

    global_resistance = _finite_or_none(impedance_result.resistance)
    global_reactance = _finite_or_none(impedance_result.reactance)
    breath_resistance = _finite_or_none(breath_result.get("resistance_R5"))
    breath_reactance = _finite_or_none(breath_result.get("reactance_X5"))

    # Safe to choose between these only because both are now on the same tier: the
    # per-breath path is bias-corrected in _bias_correct_breath, matching what
    # analyse_blocks reports. Before that they differed, so the basis of a result
    # silently depended on how many clean breaths the take happened to yield.
    if breath_result["sufficient"] and breath_resistance is not None:
        resistance = breath_resistance
        resistance_source = "within-breath"
    else:
        resistance = global_resistance
        resistance_source = "whole-record"

    if breath_result["sufficient"] and breath_reactance is not None:
        reactance = breath_reactance
    else:
        reactance = global_reactance

    return {
        "resistance_R5": resistance,
        "reactance_X5": reactance,
        "impedance_Z5": _hypot_or_none(resistance, reactance),
        "resonant_frequency_hz": None,
        "ax": None,
        "tidal_volume_l": _finite_or_none(breath_result.get("tidal_volume_l")),
        # Within-breath results. Present only when the take yielded clean breaths; the
        # whole-record fallback has no inspiration/expiration split to report, so these
        # stay None rather than being faked from the averaged value.
        "Rrs_insp": breath_result.get("Rrs_insp"),
        "Rrs_exp": breath_result.get("Rrs_exp"),
        "dRrs": breath_result.get("dRrs"),
        "Xrs_insp": breath_result.get("Xrs_insp"),
        "Xrs_exp": breath_result.get("Xrs_exp"),
        "dXrs": breath_result.get("dXrs"),
        "cov_r5": breath_result.get("cov_r5"),
        "clean_breath_count": breath_result.get("clean_breath_count"),
        "breath_count": breath_result.get("breath_count"),
        "sample_count": row_count,
        "raw_csv_path": str(csv_path),
        "status": _processing_status(
            row_count,
            len(samples),
            len(blocks),
            impedance_result,
            breath_result,
            resistance_source,
            apply_bias_polynomial,
            hz,
            phase_calibrated and gain_calibrated,
        ),
        "excitation_hz": hz,
        "calibrated_at_frequency": phase_calibrated and gain_calibrated,
    }


def _read_measurement_samples(csv_path, hz=EXCITATION_HZ):
    samples = []
    row_count = 0

    with csv_path.open("r", newline="", encoding="utf-8") as sample_file:
        reader = csv.DictReader(sample_file)

        for row in reader:
            row_count += 1

            sample_number = _safe_int(_first_present(row, "sample", "sample_number"))
            elapsed_seconds = _safe_float(
                _first_present(row, "time_s", "elapsed_seconds")
            )
            arduino_index = _safe_int(_first_present(row, "idx", "arduino_index"))
            pressure = _safe_float(row.get("pressure_cmH2O"))
            flow = _safe_float(_first_present(row, "flow_Lps", "flow_lps"))

            if elapsed_seconds is None and sample_number is not None:
                elapsed_seconds = sample_number / sample_hz_for(hz)

            if arduino_index is None or pressure is None or flow is None:
                continue

            samples.append({
                "sample": sample_number,
                "time_s": elapsed_seconds,
                "idx": arduino_index,
                "pressure_cmH2O": pressure,
                "flow_Lps": flow,
            })

    return row_count, samples


def _make_impedance_blocks(samples, hz=EXCITATION_HZ):
    block_samples = block_samples_for(hz)
    blocks = []
    block_indices = []
    block_pressure = []
    block_flow = []

    for sample in samples:
        index = sample["idx"]

        if block_indices and index != (block_indices[-1] + 1) % LUT_SIZE:
            block_indices = []
            block_pressure = []
            block_flow = []

        block_indices.append(index)
        block_pressure.append(sample["pressure_cmH2O"])
        block_flow.append(sample["flow_Lps"])

        if len(block_indices) == block_samples:
            try:
                blocks.append(
                    make_block_phasor(
                        block_indices, block_pressure, block_flow, block_samples
                    )
                )
            except ValueError:
                pass

            block_indices = []
            block_pressure = []
            block_flow = []

    return blocks


def _bias_correct_breath(breath, apply_bias_polynomial=True):
    """Put ONE breath's R5/X5 on the bias-corrected tier, the same basis as the global
    path and the System Check.

    phase_resolved_breaths(apply_scale=True) applies only the MULTIPLICATIVE half of
    the bench correction (BIAS_CORR_SCALE). The additive bias-flow polynomials are
    left to the caller -- FOTBreath says so in its own docstring -- and nothing used
    to add them, so a patient's R was reported roughly 3.4 too high at a typical
    0.45 L/s CPAP bias flow while the global fallback for the same recording was fully
    corrected. Which of the two you got depended only on the clean-breath count.

    bias_flow_adjustments() is the shared source of truth: its coefficients already
    have SCALE folded in and it applies the Q clamp, so with SCALE applied upstream
    the remaining step is a plain subtraction. Each breath uses its OWN bias_flow_lps,
    which FOTBreath returns for exactly this purpose.

    CALLED AFTER QC, NOT BEFORE, on purpose. The only QC gates that read impedance are
    finiteness (which a finite subtraction cannot change) and the negative-R5 test,
    which reads `r5_windows` -- left on the phase-only basis here. Keeping QC upstream
    of the correction means a re-run calibration changes the reported numbers but never
    which breaths were considered usable, and the accepted-breath count stays stable.

    dXrs and dRrs are differences BETWEEN the two phases of one breath, and both phases
    share that breath's bias flow, so the additive term cancels exactly and they are
    unchanged. They are recomputed here anyway so the dict stays self-consistent.

    `apply_bias_polynomial=False` returns the breath untouched, which is the SCALE-only
    basis: phase_resolved_breaths(apply_scale=True) has already applied the
    multiplicative half upstream, so skipping this function drops exactly the
    flow-indexed terms and nothing else. A direct consequence of the cancellation noted
    above is that dRrs and dXrs are IDENTICAL on both bases; only the per-phase
    absolutes and anything derived from their spread (cov_r5) move.
    """
    if not BIAS_FLOW_CORRECTION_ENABLED or not apply_bias_polynomial:
        return breath

    k_plumbing_adj, k_x_adj = bias_flow_adjustments(
        _finite_or_none(breath.get("bias_flow_lps")) or 0.0
    )

    corrected = dict(breath)
    for key, adjustment in (
        ("R5_insp", k_plumbing_adj),
        ("R5_exp", k_plumbing_adj),
        ("X5_insp", k_x_adj),
        ("X5_exp", k_x_adj),
    ):
        value = _finite_or_none(breath.get(key))
        if value is not None:
            corrected[key] = value - adjustment

    insp_x = _finite_or_none(corrected.get("X5_insp"))
    exp_x = _finite_or_none(corrected.get("X5_exp"))
    insp_r = _finite_or_none(corrected.get("R5_insp"))
    exp_r = _finite_or_none(corrected.get("R5_exp"))
    if insp_x is not None and exp_x is not None:
        # Expiration minus inspiration, matching dRrs below. See FOTBreath.
        corrected["dXrs"] = exp_x - insp_x
    if insp_r is not None and exp_r is not None:
        corrected["dRrs"] = exp_r - insp_r

    return corrected


def _analyse_breath_qc(samples, hz, correction_deg, apply_bias_polynomial=True):
    indices = np.asarray([sample["idx"] for sample in samples], dtype=int)
    pressure = np.asarray([sample["pressure_cmH2O"] for sample in samples], dtype=float)
    flow = np.asarray([sample["flow_Lps"] for sample in samples], dtype=float)

    # FOTBreath does `from FOTImpedance import BIAS_CORR_SCALE`, which binds the value
    # at IMPORT. Reloading a calibration updates FOTImpedance's global but not that
    # copy, so without this line a calibration saved or switched during the session is
    # ignored here and only a restart would pick it up. Re-pointing the name is done
    # from this side deliberately: FOTBreath stays byte-identical to Henry's engine.
    FOTBreath.BIAS_CORR_SCALE = FOTImpedance.BIAS_CORR_SCALE

    try:
        breath_output = phase_resolved_breaths(
            pressure,
            flow,
            indices,
            fs=sample_hz_for(hz),
            correction_deg=correction_deg,
            apply_scale=BIAS_FLOW_CORRECTION_ENABLED,
        )
    except Exception as error:
        return {
            "breath_count": 0,
            "clean_breath_count": 0,
            "sufficient": False,
            "reason": f"breath QC unavailable: {error}",
            "resistance_R5": None,
            "reactance_X5": None,
            "tidal_volume_l": None,
        }

    breaths = breath_output["breaths"]
    prelim_clean = []

    for breath in breaths:
        clean, _reasons = qc_breath(breath)
        if clean and breath.get("quality_ok", False):
            prelim_clean.append(breath)

    outliers = flag_outlier_breaths(prelim_clean)
    clean_breaths = [
        breath
        for breath, is_outlier in zip(prelim_clean, outliers)
        if not is_outlier
    ]
    # Onto the same tier the global path and the System Check use. Deliberately AFTER
    # QC and outlier flagging, never before: see _bias_correct_breath.
    summary = summarise_take(
        [_bias_correct_breath(b, apply_bias_polynomial) for b in clean_breaths]
    )

    breath_count = len(breaths)
    clean_breath_count = len(clean_breaths)
    clean_fraction = clean_breath_count / breath_count if breath_count else 0.0
    # Count only, no fraction. The 70% clean-fraction floor was our own invention and
    # is not in any standard; the ERS 2020 rule is a count: "Modern analysis tools allow
    # removal of entire breaths affected by artefact, so that an acquisition may remain
    # technically acceptable, as long as there are at least three breaths remaining in
    # that acquisition." The fraction penalised long recordings for having more chances
    # to catch an artefact, and it declared MEAS0006 insufficient at 46% clean while it
    # still held 6 usable breaths -- dumping a good acquisition onto the whole-record
    # fallback, which is the weaker estimate. clean_fraction is still reported, as
    # operator information about how noisy the acquisition was.
    sufficient = clean_breath_count >= FLOOR

    return {
        "breath_count": breath_count,
        "clean_breath_count": clean_breath_count,
        "clean_fraction": clean_fraction,
        "sufficient": sufficient,
        "reason": "OK" if sufficient else "insufficient clean breaths",
        "resistance_R5": _mean_finite(
            summary.get("Rrs_insp"),
            summary.get("Rrs_exp"),
        ),
        "reactance_X5": _mean_finite(
            summary.get("Xrs_insp"),
            summary.get("Xrs_exp"),
        ),
        "tidal_volume_l": _mean_finite(
            *[breath.get("tidal_volume_l") for breath in clean_breaths]
        ),
        # The within-breath quantities are the clinical read-out: averaging insp and exp
        # into one R5 throws away dRrs and dXrs, which is the whole point of phase-
        # resolved analysis. summarise_take already computes them, so carry them through
        # rather than recomputing anywhere else.
        "Rrs_insp": _finite_or_none(summary.get("Rrs_insp")),
        "Rrs_exp": _finite_or_none(summary.get("Rrs_exp")),
        "dRrs": _finite_or_none(summary.get("dRrs")),
        "Xrs_insp": _finite_or_none(summary.get("Xrs_insp")),
        "Xrs_exp": _finite_or_none(summary.get("Xrs_exp")),
        "dXrs": _finite_or_none(summary.get("dXrs")),
        "cov_r5": _finite_or_none(summary.get("cov_r5")),
    }


def _processing_status(
    row_count,
    parsed_count,
    block_count,
    impedance_result,
    breath_result,
    resistance_source="whole-record",
    apply_bias_polynomial=True,
    hz=EXCITATION_HZ,
    calibrated=True,
):
    impedance_status = "impedance OK" if impedance_result.valid else impedance_result.reason
    breath_status = (
        f"QC {breath_result['clean_breath_count']}/"
        f"{breath_result['breath_count']} clean breaths"
    )

    if breath_result["sufficient"]:
        breath_status = f"{breath_status}; sufficient"
    else:
        breath_status = f"{breath_status}; {breath_result['reason']}"

    if not BIAS_FLOW_CORRECTION_ENABLED:
        basis = "phase-only"
    elif apply_bias_polynomial:
        basis = TIER_BIAS_CORRECTED
    else:
        basis = TIER_SCALE_ONLY

    calibration_note = (
        "" if calibrated
        else f"; NOT CALIBRATED at {hz:g} Hz - run the gain/phase calibration"
    )
    return (
        f"Processed {row_count} rows ({parsed_count} parsed); "
        f"{block_count} blocks at {hz:g} Hz; "
        f"{impedance_status}; {breath_status}; "
        f"reported from {resistance_source}, {basis}{calibration_note}"
    )


def _empty_results(csv_path, status):
    return {
        "resistance_R5": None,
        "reactance_X5": None,
        "impedance_Z5": None,
        "resonant_frequency_hz": None,
        "ax": None,
        "tidal_volume_l": None,
        "sample_count": 0,
        "raw_csv_path": str(csv_path),
        "status": status,
    }


def _first_present(row, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _safe_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None

    return parsed if math.isfinite(parsed) else None


def _finite_or_none(value):
    if value is None:
        return None

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None

    return parsed if math.isfinite(parsed) else None


def _mean_finite(*values):
    finite_values = []

    for value in values:
        parsed = _finite_or_none(value)
        if parsed is not None:
            finite_values.append(parsed)

    if not finite_values:
        return None

    return float(np.mean(finite_values))


def _hypot_or_none(first, second):
    if first is None or second is None:
        return None

    return float(math.hypot(first, second))
