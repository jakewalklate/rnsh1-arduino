"""Daily system check — start-of-day accuracy verification against a KNOWN load.

Headless port of the system-check mode Henry added to PressureSensorReading.py. The
rules and tolerances are UNCHANGED; only the matplotlib front end is removed.

In the spirit of the hospital FOT devices: measure a known reference load at two
different CPAP levels and declare the system ready only if both land inside
tolerance.

This VERIFIES; it never writes a calibration. A failure sends the operator to the
Calibrate Device tab for the full 0..20 CPAP sweep.

Unlike the calibration sweep, the check scores the BIAS-CORRECTED impedance — it is
testing the calibration currently in force, so that correction must be applied.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


CHECK_SECONDS = 25                   # counted blocks per measurement (auto-stop)
CHECK_SETTLING_BLOCKS = 5            # discarded first, matching the calibration path
CHECK_R_TOLERANCE_FRACTION = 0.10    # +/-10% on R, against the entered reference
CHECK_X_ABS_TOLERANCE = 1.0          # +/-1 absolute on X (resistive load, target 0)
CHECK_LOAD_PRESETS = (13.65, 15.2)
CHECK_POINTS_REQUIRED = 2
# The two points must be at genuinely different bias flows, otherwise the operator
# forgot to change CPAP and the check has only ever tested one operating point.
CHECK_MIN_BIAS_FLOW_DELTA_LPS = 0.15

CHECK_CSV_HEADER = [
    "check",
    "measurement",
    "finished_time_utc",
    "excitation_hz",
    "reference_R_cmH2O_s_per_L",
    "cpap_note",
    "measured_R_cmH2O_s_per_L",
    "measured_X_cmH2O_s_per_L",
    "R_error_percent",
    "bias_flow_Lps",
    "block_variation_percent",
    "pressure_amplitude",
    "flow_amplitude",
    "qc_valid",
    "R_pass",
    "X_pass",
    "point_pass",
    "verdict",
]


def seconds_for_check() -> int:
    """Wall-clock seconds one check measurement takes."""
    return CHECK_SETTLING_BLOCKS + CHECK_SECONDS


def score_point(captured, reference_r, cpap_note="", measurement_number=1) -> dict:
    """Score one completed measurement against the entered reference.

    Pass needs all three: the block QC gate, R within +/-10% of the reference, and
    |X| within the absolute band (the reference is resistive, so its true X is ~0).

    `captured` is the point dict from CalibrationPointRecorder run with
    tier="bias_corrected".
    """
    reference = float(reference_r)
    measured_r = float(captured["r"])
    measured_x = float(captured["x"])
    r_error_pct = (
        (measured_r - reference) / reference * 100.0 if reference else float("nan")
    )
    r_pass = bool(
        np.isfinite(r_error_pct)
        and abs(r_error_pct) <= CHECK_R_TOLERANCE_FRACTION * 100.0
    )
    x_pass = bool(np.isfinite(measured_x) and abs(measured_x) <= CHECK_X_ABS_TOLERANCE)
    qc_valid = bool(captured.get("all_valid", True))

    return {
        "measurement": measurement_number,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "reference_r": reference,
        "cpap_note": cpap_note,
        "R": measured_r,
        "X": measured_x,
        "r_error_pct": r_error_pct,
        "bias_flow_lps": float(captured["bias_flow"]),
        "variation_pct": float(captured.get("variation", float("nan"))),
        "pressure_amplitude": float(captured.get("pressure_amplitude", float("nan"))),
        "flow_amplitude": float(captured.get("flow_amplitude", float("nan"))),
        "qc_valid": qc_valid,
        "qc_reason": captured.get("reason", ""),
        "r_pass": r_pass,
        "x_pass": x_pass,
        "point_pass": bool(qc_valid and r_pass and x_pass),
    }


def verdict(points) -> tuple[bool | None, str]:
    """(ready, message) over the most recent CHECK_POINTS_REQUIRED measurements.
    ready is None while the check is still incomplete."""
    n = len(points)
    if n < CHECK_POINTS_REQUIRED:
        remaining = CHECK_POINTS_REQUIRED - n
        return None, (
            f"System check: {n}/{CHECK_POINTS_REQUIRED} measurements. "
            f"Change CPAP, then run {remaining} more."
        )

    recent = points[-CHECK_POINTS_REQUIRED:]

    references = {round(p["reference_r"], 6) for p in recent}
    if len(references) > 1:
        return False, (
            "NOT READY - the two measurements used different reference loads "
            f"({', '.join(f'{r:g}' for r in sorted(references))}). Redo on one load."
        )

    flows = [p["bias_flow_lps"] for p in recent]
    spread = max(flows) - min(flows)
    if spread < CHECK_MIN_BIAS_FLOW_DELTA_LPS:
        return False, (
            f"NOT READY - both measurements at the same bias flow (spread "
            f"{spread:.3f} < {CHECK_MIN_BIAS_FLOW_DELTA_LPS:g} L/s). Change CPAP "
            f"between measurements and redo."
        )

    if all(p["point_pass"] for p in recent):
        return True, "SYSTEM READY - both points within tolerance."

    return False, (
        "NOT READY - recalibration required. Run the full 0-20 CPAP sweep in the "
        "Calibrate Device tab, then re-check."
    )


def append_check_log(path: Path, check_number, point, verdict_text, excitation_hz):
    """One row per measurement, matching the bench tool's fot_check_*.csv columns."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(CHECK_CSV_HEADER)
        writer.writerow([
            check_number,
            point["measurement"],
            point["finished_utc"],
            excitation_hz,
            point["reference_r"],
            point["cpap_note"],
            f"{point['R']:.4f}",
            f"{point['X']:.4f}",
            f"{point['r_error_pct']:.3f}",
            f"{point['bias_flow_lps']:.5f}",
            f"{point['variation_pct']:.2f}",
            f"{point['pressure_amplitude']:.5f}",
            f"{point['flow_amplitude']:.5f}",
            point["qc_valid"],
            point["r_pass"],
            point["x_pass"],
            point["point_pass"],
            verdict_text,
        ])
    return path


def default_check_log_path(data_dir) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(data_dir) / f"fot_check_{stamp}.csv"
