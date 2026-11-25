"""Tube / bias-flow calibration engine (CPAP dependence of R).

Headless port of Henry's FOTTubeCalibration.py bench tool, for use inside the
clinical app. The MATH IS UNCHANGED — only the matplotlib/serial front end is
removed, so the app and the bench tool derive identical constants from identical
sweeps.

WHAT IT DOES
------------
Re-derives the bias-flow correction in FOTImpedance after a tubing or flowhead
change. A known reference load (REFERENCE_RESISTANCE) is fitted in place of the
patient and CPAP is stepped through CPAP_STEPS. At each step the phase-corrected
resistance and the bias flow are measured, giving one point. Fitting those points:

    R_phase = a0 + a1*Q + ... + a_d*Q^d
    SCALE            = REFERENCE_RESISTANCE / a0
    K_PLUMBING_POLY  = [SCALE*a1, ..., SCALE*a_d]     (Q^1..Q^d, output units)

Substituting the fit back into the engine correction proves it self-consistent:
    R_corrected = SCALE*(a0 + a1*Q) - K*Q = SCALE*a0 = REFERENCE_RESISTANCE
i.e. flat at the reference for ANY bias flow, which is the whole point.

WHY R_phase AND NOT R
---------------------
The fitted y-axis MUST be the PHASE-CORRECTED-ONLY resistance, because SCALE and
K_PLUMBING are the very things being solved for. Feeding the fully corrected R back
in would apply the old constants and fit them to themselves. analyse_blocks exposes
all three tiers, so no engine fork is needed:

    result.raw            no correction at all
    result.corrected      phase rotation ONLY          <-- this tool uses this
    result.bias_corrected phase + SCALE + K_PLUMBING   (what the clinical app shows)

bias_flow_lps in the SAME ImpedanceResult is the mean of the very same blocks, so
every (Q_bias, R_phase) pair is internally consistent by construction.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from backend.processing.FOTImpedance import (
    BLOCKS_PER_WINDOW,
    EXCITATION_HZ,
    REFERENCE_RESISTANCE,
)


# CPAP settings offered to the operator. Extra resolution at 4/5/6 because that is
# the low-pressure region the clinical work sits in.
CPAP_STEPS = [0, 4, 5, 6, 8, 10, 12, 14, 16, 18, 20]

# After the CPAP knob moves the blower needs to settle before the flow is steady.
# These blocks are collected and thrown away.
SETTLE_BLOCKS = 5

# Each repeat is a FULL, INDEPENDENT BLOCKS_PER_WINDOW window (no overlap with its
# neighbours), so averaging them is statistically meaningful. Two keeps a point to
# about 25 s while still giving a spread to average.
DEFAULT_REPEATS = 2
MAX_REPEATS = 10
MAX_FIT_ORDER = 10

# Broad mathematical safety limits. These are not clinical acceptance limits; they
# stop a singular or wildly divergent polynomial from becoming the active correction.
FIT_CONDITION_WARNING = 1.0e8
FIT_CONDITION_BLOCK = 1.0e12
FIT_SCALE_WARNING_MIN = 0.5
FIT_SCALE_WARNING_MAX = 2.0
FIT_SCALE_BLOCK_MIN = 0.1
FIT_SCALE_BLOCK_MAX = 10.0
FIT_CORRECTION_WARNING = 100.0
FIT_CORRECTION_BLOCK = 1000.0

SWEEP_CSV_COLUMNS = {
    "captured_utc": "captured_utc",
    "CPAP_cmH2O": "cpap",
    "R_phase_mean": "r_phase",
    "R_phase_sd": "r_phase_sd",
    "X_phase_mean": "x_phase",
    "bias_flow_mean_Lps": "bias_flow",
    "bias_flow_sd_Lps": "bias_flow_sd",
    "flow_5Hz_amplitude_Lps": "flow_amplitude",
    "block_variation_percent": "variation",
    "n_windows": "n_windows",
}


def seconds_for_capture(repeats: int) -> int:
    """Wall-clock seconds one captured point takes: settle + repeats x window.
    One block is one second at the standard operating point."""
    return int(SETTLE_BLOCKS + max(1, int(repeats)) * BLOCKS_PER_WINDOW)


def _polyfit(q: np.ndarray, y: np.ndarray, degree: int) -> tuple[list[float], float]:
    """Least-squares polynomial y = a0 + a1*q + ... + a_degree*q^degree, returned as
    ASCENDING coefficients [a0, a1, ...] plus an R^2. Assumes q has spread and
    len(q) >= degree + 1."""
    coeffs_desc = np.polyfit(q, y, degree)
    predicted = np.polyval(coeffs_desc, q)
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    ascending = [float(c) for c in coeffs_desc[::-1]]
    return ascending, float(r_squared)


def _holdout_rms(
    q: np.ndarray, r: np.ndarray, x: np.ndarray, degree: int, x_degree: int
) -> tuple[float, float]:
    """Leave-one-out error of the CORRECTED outputs, in output units.

    R2 is computed on the same points the curve was fitted to, so it only ever rises
    with order and cannot see over-fitting. This hides each point in turn, re-derives
    SCALE and the K terms from the remaining ones, then applies that calibration to the
    hidden point -- which is the situation a patient at an unmeasured CPAP is in.
    Returns (rms error of R about REFERENCE_RESISTANCE, rms error of X about 0).

    The lowest-Q and highest-Q points are never held out: removing an endpoint forces
    the fit to extrapolate, which the engine never does because it clamps Q to the
    measured range. Including them would penalise high orders for a failure mode that
    cannot occur in use. Returns (nan, nan) when there is not enough data to refit.
    """
    n = len(q)
    order = np.argsort(q)
    interior = order[1:-1]
    if n - 1 < max(degree, x_degree) + 1 or len(interior) == 0:
        return float("nan"), float("nan")
    r_errs: list[float] = []
    x_errs: list[float] = []
    for i in interior:
        keep = np.ones(n, dtype=bool)
        keep[i] = False
        r_asc, _ = _polyfit(q[keep], r[keep], degree)
        if not np.isfinite(r_asc[0]) or abs(r_asc[0]) < 1e-9:
            return float("nan"), float("nan")
        scale = REFERENCE_RESISTANCE / r_asc[0]
        x_asc, _ = _polyfit(q[keep], x[keep], x_degree)
        qi = float(q[i])
        r_corr = scale * r[i] - sum(
            scale * c * qi ** k for k, c in enumerate(r_asc[1:], start=1)
        )
        # Subtract the X constant too, matching bias_flow_adjustments. Leaving it in
        # measured the held-out point against a curve the live path does not use, and
        # inflated x_holdout_rms by the whole offset.
        x_corr = scale * x[i] - scale * x_asc[0] - sum(
            scale * c * qi ** k for k, c in enumerate(x_asc[1:], start=1)
        )
        r_errs.append(float(r_corr) - REFERENCE_RESISTANCE)
        x_errs.append(float(x_corr))
    return (
        float(np.sqrt(np.mean(np.square(r_errs)))),
        float(np.sqrt(np.mean(np.square(x_errs)))),
    )


def compute_fit(
    points: list[dict], degree: int = 1, x_degree: int | None = None
) -> dict | None:
    """Polynomial fits of R_phase and X_phase vs bias flow, at INDEPENDENT orders:
        R_phase = a0 + a1*Q + ... + a_dr*Q^dr      (degree)
        X_phase = c0 + c1*Q + ... + c_dx*Q^dx      (x_degree, defaults to degree)

    R and X are separated because they are not equally well conditioned: on a
    resistive reference X is near zero and dominated by scatter, so it usually
    supports a LOWER order than R before it starts fitting noise. SCALE always comes
    from the R intercept, so a different X order does not change it.

    Degree 1 is the historical straight line. Returns None until there are at least
    degree+1 points spanning a range of bias flows.
    """
    degree = max(1, int(degree))
    x_degree = degree if x_degree is None else max(1, int(x_degree))
    if degree > MAX_FIT_ORDER or x_degree > MAX_FIT_ORDER:
        return None
    usable = [p for p in points
              if np.isfinite(p["r_phase"]) and np.isfinite(p["bias_flow"])
              and np.isfinite(p["x_phase"])]
    if len(usable) < max(degree, x_degree) + 1:
        return None
    q = np.array([p["bias_flow"] for p in usable], dtype=float)
    r = np.array([p["r_phase"] for p in usable], dtype=float)
    x = np.array([p["x_phase"] for p in usable], dtype=float)
    if float(np.ptp(q)) < 1e-6:
        return None  # all points at one bias flow: fit undefined
    if len(np.unique(np.round(q, 9))) < max(degree, x_degree) + 1:
        return None

    r_asc, r_r2 = _polyfit(q, r, degree)
    if not np.isfinite(r_asc[0]) or abs(r_asc[0]) < 1e-9:
        return None
    scale = REFERENCE_RESISTANCE / r_asc[0]
    x_asc, x_r2 = _polyfit(q, x, x_degree)
    k_plumbing_poly = [float(scale * c) for c in r_asc[1:]]
    k_x_poly = [float(scale * c) for c in x_asc[1:]]
    r_holdout, x_holdout = _holdout_rms(q, r, x, degree, x_degree)
    fit = {
        "degree": degree,
        "x_degree": x_degree,
        "r_holdout_rms": r_holdout,
        "x_holdout_rms": x_holdout,
        "r_coeffs_asc": r_asc,
        "x_coeffs_asc": x_asc,
        "intercept": r_asc[0],
        "x_intercept": x_asc[0],
        "r_squared": r_r2,
        "x_r_squared": x_r2,
        "scale": float(scale),
        "k_plumbing_poly": k_plumbing_poly,
        "k_x_poly": k_x_poly,
        "k_plumbing": float(k_plumbing_poly[0]),   # Q^1 term (backward-compat scalar)
        "k_x": float(k_x_poly[0]),                 # Q^1 term (backward-compat scalar)
        "x_residual_at_zero": float(scale * x_asc[0]),
        "q_min": float(np.min(q)),
        "q_max": float(np.max(q)),
        "n_points": len(usable),
    }
    fit["validation"] = validate_fit(usable, fit)
    return fit


def validate_fit(points: list[dict], fit: dict) -> dict:
    """Check whether a proposed polynomial is safe to make active.

    Coefficient size alone is not meaningful because adjacent polynomial terms can
    cancel. Validation therefore checks the curve and correction across the measured
    bias-flow interval, plus the conditioning of the underlying fit matrices.
    """
    errors: list[str] = []
    warnings: list[str] = []

    q = np.asarray([p["bias_flow"] for p in points], dtype=float)
    r = np.asarray([p["r_phase"] for p in points], dtype=float)
    x = np.asarray([p["x_phase"] for p in points], dtype=float)
    unique_q = np.unique(np.round(q, 9))
    highest_order = max(int(fit["degree"]), int(fit["x_degree"]))

    if len(unique_q) < highest_order + 1:
        errors.append(
            f"Order {highest_order} needs at least {highest_order + 1} distinct "
            f"bias-flow values; only {len(unique_q)} are available."
        )

    scale = float(fit["scale"])
    if not np.isfinite(scale) or scale <= 0:
        errors.append("The fitted scale is not a finite positive value.")
    elif scale < FIT_SCALE_BLOCK_MIN or scale > FIT_SCALE_BLOCK_MAX:
        errors.append(
            f"Scale {scale:.3f} is outside the broad safety range "
            f"{FIT_SCALE_BLOCK_MIN:g}-{FIT_SCALE_BLOCK_MAX:g}."
        )
    elif scale < FIT_SCALE_WARNING_MIN or scale > FIT_SCALE_WARNING_MAX:
        warnings.append(
            f"Scale {scale:.3f} is unusually far from 1.0; check the tubing, "
            "reference load, and flow calibration before saving."
        )

    q_span = float(np.ptp(q))
    if q_span <= 0:
        errors.append("The bias-flow values do not span a measurable range.")
        return {"can_save": False, "errors": errors, "warnings": warnings}

    q_normalised = (q - float(np.mean(q))) / (0.5 * q_span)
    conditions = {}
    for label, order in (("R", int(fit["degree"])), ("X", int(fit["x_degree"]))):
        matrix = np.vander(q_normalised, N=order + 1)
        condition = float(np.linalg.cond(matrix))
        conditions[label] = condition
        if not np.isfinite(condition) or condition >= FIT_CONDITION_BLOCK:
            errors.append(
                f"{label} order {order} is numerically singular for these points "
                f"(condition {condition:.2e}). Choose a lower order."
            )
        elif condition >= FIT_CONDITION_WARNING:
            warnings.append(
                f"{label} order {order} is poorly conditioned "
                f"({condition:.2e}); a lower order is likely more stable."
            )

    grid = np.linspace(float(np.min(q)), float(np.max(q)), 400)
    r_curve = np.polyval(np.asarray(fit["r_coeffs_asc"])[::-1], grid)
    x_curve = np.polyval(np.asarray(fit["x_coeffs_asc"])[::-1], grid)
    r_correction = np.zeros_like(grid)
    x_correction = np.zeros_like(grid)
    for power, coefficient in enumerate(fit["k_plumbing_poly"], start=1):
        r_correction += coefficient * grid ** power
    for power, coefficient in enumerate(fit["k_x_poly"], start=1):
        x_correction += coefficient * grid ** power

    arrays = (r_curve, x_curve, r_correction, x_correction)
    if not all(np.all(np.isfinite(values)) for values in arrays):
        errors.append("The fitted curve contains non-finite values.")
    else:
        max_correction = float(
            max(np.max(np.abs(r_correction)), np.max(np.abs(x_correction)))
        )
        if max_correction >= FIT_CORRECTION_BLOCK:
            errors.append(
                f"The proposed correction reaches {max_correction:.1f}, above the "
                f"{FIT_CORRECTION_BLOCK:g} safety limit. Choose a lower order."
            )
        elif max_correction >= FIT_CORRECTION_WARNING:
            warnings.append(
                f"The proposed correction reaches {max_correction:.1f}; inspect the "
                "curves closely before saving."
            )

        for label, curve, observed in (
            ("Resistance", r_curve, r),
            ("Reactance", x_curve, x),
        ):
            observed_span = max(float(np.ptp(observed)), 0.1)
            lower = float(np.min(observed)) - 2.0 * observed_span
            upper = float(np.max(observed)) + 2.0 * observed_span
            if float(np.min(curve)) < lower or float(np.max(curve)) > upper:
                warnings.append(
                    f"{label} oscillates well outside the captured data between "
                    "points; this is a sign of high-order over-fitting."
                )

    if highest_order >= len(unique_q) - 1:
        warnings.append(
            "This order interpolates the captured points with no spare point for "
            "validation. A lower order is safer unless the curve shape is known."
        )

    holdout = fit.get("r_holdout_rms")
    if holdout is None or not np.isfinite(holdout):
        warnings.append(
            "Leave-one-out error cannot be calculated at this order, so performance "
            "between measured pressures is unverified."
        )

    return {
        "can_save": not errors,
        "errors": errors,
        "warnings": warnings,
        "condition_r": conditions.get("R"),
        "condition_x": conditions.get("X"),
    }


def build_payload(
    fit: dict,
    correction_deg: float,
    phase_calibration_file: str,
    source_csv: str,
    loaded_sources: list[str] | None = None,
) -> dict:
    """The JSON the engine loads at startup. Same keys and rounding as the bench
    tool writes, so a calibration saved here is indistinguishable from one saved
    there — and readable by both."""
    payload = {
        "excitation_hz": EXCITATION_HZ,
        "reference_resistance": REFERENCE_RESISTANCE,
        # fit_degree kept = r_degree so older readers still see the R order.
        "fit_degree": fit["degree"],
        "r_degree": fit["degree"],
        "x_degree": fit["x_degree"],
        "scale": round(fit["scale"], 6),
        # Q^1 terms kept for modules importing the scalar; the poly arrays are what
        # the engine applies (Q^1..Q^d, output units, no constant term).
        "k_plumbing": round(fit["k_plumbing"], 6),
        "k_x": round(fit["k_x"], 6),
        "k_plumbing_poly": [round(c, 6) for c in fit["k_plumbing_poly"]],
        "k_x_poly": [round(c, 6) for c in fit["k_x_poly"]],
        # Freeze the correction outside the measured range so a high-order fit
        # cannot diverge (the low end floors at 0 in the engine).
        "clamp_q_low": 0.0,
        "clamp_q_high": round(fit["q_max"], 6),
        "fit_r_intercept": round(fit["intercept"], 6),
        "fit_r_coeffs_ascending": [round(c, 6) for c in fit["r_coeffs_asc"]],
        "fit_r_squared": round(fit["r_squared"], 6),
        "fit_x_intercept": round(fit["x_intercept"], 6),
        "fit_x_coeffs_ascending": [round(c, 6) for c in fit["x_coeffs_asc"]],
        "fit_x_squared": round(fit["x_r_squared"], 6),
        "r_holdout_rms": round(fit["r_holdout_rms"], 6),
        "x_holdout_rms": round(fit["x_holdout_rms"], 6),
        "x_residual_at_zero": round(fit["x_residual_at_zero"], 6),
        "fit_q_min": round(fit["q_min"], 6),
        "fit_q_max": round(fit["q_max"], 6),
        "n_points": fit["n_points"],
        "fit_model": (
            f"R_phase=a+sum r_i Q^i (deg {fit['degree']}); "
            f"X_phase=a_X+sum x_i Q^i (deg {fit['x_degree']}); "
            f"correction: R=SCALE*R_phase - sum(SCALE*r_i * Qc^i), "
            f"X=SCALE*X_phase - sum(SCALE*x_i * Qc^i); "
            f"Qc=clip(Q,0,clamp_q_high); SCALE from the R intercept only"
        ),
        "phase_correction_deg": round(float(correction_deg), 6),
        "phase_calibration_file": phase_calibration_file,
        "source_csv": source_csv,
        "saved_utc": datetime.now(timezone.utc).isoformat(),
        "saved_from": "clinical app calibration tab",
    }
    if loaded_sources:
        payload["loaded_from_csv"] = list(loaded_sources)
    return payload


def write_sweep_csv(path: Path, points: list[dict], correction_deg: float) -> Path:
    """Persist the raw swept points so a sweep can be re-fitted later without
    returning to the rig."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(SWEEP_CSV_COLUMNS.keys()) + ["correction_deg", "valid"])
        for point in sorted(points, key=lambda p: p["cpap"]):
            writer.writerow([
                point.get("captured_utc", ""),
                point["cpap"],
                f"{point['r_phase']:.4f}",
                f"{point.get('r_phase_sd', 0.0):.4f}",
                f"{point['x_phase']:.4f}",
                f"{point['bias_flow']:.5f}",
                f"{point.get('bias_flow_sd', 0.0):.5f}",
                f"{point.get('flow_amplitude', float('nan')):.5f}",
                f"{point.get('variation', float('nan')):.2f}",
                point.get("n_windows", 1),
                f"{float(correction_deg):.3f}",
                "1" if point.get("all_valid", True) else "0",
            ])
    return path


def load_sweep_csv(path: Path) -> tuple[list[dict], list[float], list[str]]:
    """Parse a tube_calibration_*.csv sweep back into the point dicts a live capture
    produces, so an earlier sweep can be re-fitted without redoing it on the rig.

    Points are deduplicated by CPAP keeping the LAST occurrence, mirroring live
    capture where a recaptured step replaces its predecessor. Unparseable rows are
    reported in warnings rather than silently dropped. The correction angles seen let
    the caller check the sweep was taken under the phase calibration now loaded:
    R_phase/X_phase in the file are ALREADY phase-rotated, so re-fitting them under a
    different angle produces a wrong SCALE and K terms.
    """
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"{path.name} has no data rows.")
    missing = [c for c in SWEEP_CSV_COLUMNS if c not in rows[0]]
    if missing:
        raise ValueError(
            f"{path.name} is not a tube-calibration sweep "
            f"(missing column(s): {', '.join(missing)})."
        )

    points: list[dict] = []
    warnings: list[str] = []
    angles: list[float] = []

    for line_no, row in enumerate(rows, start=2):
        point: dict = {}
        try:
            for column, key in SWEEP_CSV_COLUMNS.items():
                raw = (row.get(column) or "").strip()
                if key == "captured_utc":
                    point[key] = raw
                elif key == "cpap":
                    point[key] = int(float(raw))
                elif key == "n_windows":
                    point[key] = int(float(raw)) if raw else 1
                else:
                    point[key] = float(raw) if raw else float("nan")
        except (TypeError, ValueError) as error:
            warnings.append(f"line {line_no} skipped ({error})")
            continue

        raw_valid = (row.get("valid") or "").strip()
        point["all_valid"] = raw_valid in ("", "1", "True", "true")
        points.append(point)

        raw_angle = (row.get("correction_deg") or "").strip()
        if raw_angle:
            try:
                angles.append(float(raw_angle))
            except ValueError:
                warnings.append(f"line {line_no}: unreadable correction_deg")

    # Later rows win on a shared CPAP step, matching live recapture semantics.
    kept: dict[int, dict] = {}
    for point in points:
        kept[point["cpap"]] = point
    ordered = sorted(kept.values(), key=lambda p: p["cpap"])

    n_invalid = sum(1 for p in ordered if not p["all_valid"])
    if n_invalid:
        warnings.append(f"{n_invalid} of {len(ordered)} loaded point(s) logged INVALID")

    return ordered, angles, warnings


def default_sweep_path(data_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(data_dir) / f"tube_calibration_{stamp}.csv"
