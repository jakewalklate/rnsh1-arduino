"""Hans Rudolph 3 L syringe flow calibration — headless port of the bench tool.

A syringe of KNOWN volume is pushed and pulled through the pneumotach. Flow is not
measured directly: the sensor reports a differential pressure across the mesh, and
over one full stroke

    integral(flow) dt = syringe volume

so with flow = gain * dp,

    gain = signed_volume / integral(dp) dt

Every accepted stroke gives one independent estimate of that gain, and the median of
ten is the calibration. Push and pull are kept separate because the mesh is not
perfectly symmetric, which is why the firmware carries two constants.

Rules and constants are UNCHANGED from FlowSyringeCalibration.py; only the matplotlib
front end and the module-level serial port are removed so the app can drive it.

WHERE THE RESULT TAKES EFFECT: the firmware's FLOW_GAIN_POS/NEG are compile-time
constants and are deliberately NOT rewritten. The device streams flow already scaled
by them, so this module divides them back out and re-applies the saved gains in
software (see rescale_flow_lps). One saved file therefore changes every reading the
app makes, with no reflash.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from backend.config import FIRMWARE_FLOW_GAIN_NEG, FIRMWARE_FLOW_GAIN_POS


SYRINGE_VOLUME_L = 3.0
TARGET_STROKES = 10
# A2 pulse below this is indistinguishable from baseline drift, so it is not counted
# as part of the stroke.
MIN_ACTIVE_DP_CMH2O = 0.015
# Kept either side of the active region so the slow tails of the pulse, which sit
# under the threshold but still carry volume, are inside the integral.
ACTIVE_MARGIN_S = 0.15
# Spread across the ten strokes that counts as a consistent calibration.
CONSISTENCY_TOLERANCE_PCT = 3.0

MIN_BASELINE_SECONDS = 0.5
MIN_STROKE_SECONDS = 0.25

_BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------- saved gains

def flow_gain_path() -> Path:
    """The saved syringe calibration. Not per-frequency: the pneumotach's gain is a
    property of the mesh and tubing, not of the excitation tone."""
    return _BASE_DIR / "fot_flow_gain.json"


def load_flow_gain() -> dict | None:
    path = flow_gain_path()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def active_flow_gains() -> tuple[float, float]:
    """(push, pull) gains in force, read from disk at CALL time.

    With no saved file the firmware's own constants are returned, which makes
    rescale_flow_lps the identity — a fresh checkout behaves exactly as before any
    syringe calibration was ever run.
    """
    data = load_flow_gain()
    if not data:
        return FIRMWARE_FLOW_GAIN_POS, FIRMWARE_FLOW_GAIN_NEG
    return (
        float(data.get("flow_gain_pos", FIRMWARE_FLOW_GAIN_POS)),
        float(data.get("flow_gain_neg", FIRMWARE_FLOW_GAIN_NEG)),
    )


def rescale_flow_lps(flow_lps: float) -> float:
    """Re-scale one streamed flow sample from the firmware's gain to the saved one.

    The firmware sent flow_fw = G_fw * dp, so dp = flow_fw / G_fw and the wanted
    reading is G_new * dp = flow_fw * G_new / G_fw. Both gains are positive, so the
    sign of flow is the sign of dp and picks the same branch the firmware used —
    the raw dp is recovered exactly, nothing is lost by working from flow.
    """
    if flow_lps >= 0.0:
        new_gain, firmware_gain = active_flow_gains()[0], FIRMWARE_FLOW_GAIN_POS
    else:
        new_gain, firmware_gain = active_flow_gains()[1], FIRMWARE_FLOW_GAIN_NEG
    if firmware_gain == 0.0:
        return flow_lps
    return flow_lps * (new_gain / firmware_gain)


def save_flow_gain(strokes, notes="") -> tuple[Path, dict]:
    """Write the calibration the app applies from now on. Raises if the strokes do
    not yield a gain in each direction, because a one-sided calibration would leave
    the other direction silently on the firmware default."""
    summary = summarise(strokes)
    if summary["push_gain"] is None or summary["pull_gain"] is None:
        raise ValueError(
            "Need at least one accepted push AND one accepted pull stroke; a "
            "one-directional calibration would leave the other direction uncorrected."
        )

    payload = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "flow_gain_pos": round(summary["push_gain"], 9),
        "flow_gain_neg": round(summary["pull_gain"], 9),
        "flow_gain_median": round(summary["median_gain"], 9),
        "syringe_volume_L": SYRINGE_VOLUME_L,
        "n_strokes": summary["n_strokes"],
        "n_push": summary["n_push"],
        "n_pull": summary["n_pull"],
        "worst_deviation_percent": round(summary["worst_deviation_pct"], 4),
        "consistency_pass": summary["consistency_pass"],
        "median_baseline_cmH2O": round(summary["median_baseline"], 6),
        # Recorded so a later reader can tell what the stream was scaled by when
        # these gains were derived. If the firmware constants ever change, the saved
        # gains are no longer the right multiplier and must be re-derived.
        "firmware_flow_gain_pos": FIRMWARE_FLOW_GAIN_POS,
        "firmware_flow_gain_neg": FIRMWARE_FLOW_GAIN_NEG,
        "notes": notes,
        "strokes": list(strokes),
    }

    path = flow_gain_path()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return path, payload


# ------------------------------------------------------------ stroke scoring

def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def score_stroke(samples, baseline, direction, sample_hz, stroke_number=1):
    """Turn one recorded stroke into a gain estimate.

    Returns (stroke_dict, None) when accepted, or (None, reason) when rejected. The
    rejection paths are the bench tool's, in the same order.
    """
    samples = list(samples)
    if len(samples) < int(MIN_STROKE_SECONDS * sample_hz):
        return None, (
            f"Stroke discarded: fewer than {MIN_STROKE_SECONDS:g} seconds of samples."
        )

    corrected = [value - baseline for value in samples]
    active_indices = [
        index for index, value in enumerate(corrected)
        if abs(value) >= MIN_ACTIVE_DP_CMH2O
    ]
    if not active_indices:
        return None, (
            f"Stroke discarded: no A2 pulse exceeded {MIN_ACTIVE_DP_CMH2O:.3f} cmH2O."
        )

    margin = int(ACTIVE_MARGIN_S * sample_hz)
    start = max(0, active_indices[0] - margin)
    end = min(len(corrected), active_indices[-1] + margin + 1)
    active = corrected[start:end]

    integral = sum(active) / sample_hz
    if abs(integral) < 1e-6:
        return None, "Stroke discarded: pressure integral is too close to zero."
    if direction * integral <= 0.0:
        return None, (
            "Stroke discarded: signal sign is opposite the selected button. Use the "
            "other Start button or check the A2 tube direction."
        )

    return {
        "number": stroke_number,
        "direction": "push" if direction > 0 else "pull",
        "recorded_samples": len(samples),
        "recorded_duration_s": len(samples) / sample_hz,
        "active_samples": len(active),
        "active_duration_s": len(active) / sample_hz,
        "baseline_cmH2O": baseline,
        "dp_integral_cmH2O_s": integral,
        "gain_Lps_per_cmH2O": (direction * SYRINGE_VOLUME_L) / integral,
    }, None


def summarise(strokes) -> dict:
    """Medians and the consistency check over the accepted strokes."""
    strokes = list(strokes)
    if not strokes:
        return {
            "n_strokes": 0, "n_push": 0, "n_pull": 0,
            "median_gain": None, "push_gain": None, "pull_gain": None,
            "worst_deviation_pct": float("nan"), "deviations": [],
            "consistency_pass": False, "median_baseline": float("nan"),
            "complete": False,
        }

    gains = [s["gain_Lps_per_cmH2O"] for s in strokes]
    median_gain = median(gains)
    deviations = [abs(g - median_gain) / abs(median_gain) * 100.0 for g in gains]
    worst = max(deviations)

    push = [s["gain_Lps_per_cmH2O"] for s in strokes if s["direction"] == "push"]
    pull = [s["gain_Lps_per_cmH2O"] for s in strokes if s["direction"] == "pull"]
    complete = len(strokes) >= TARGET_STROKES

    return {
        "n_strokes": len(strokes),
        "n_push": len(push),
        "n_pull": len(pull),
        "median_gain": median_gain,
        "push_gain": median(push) if push else None,
        "pull_gain": median(pull) if pull else None,
        "worst_deviation_pct": worst,
        "deviations": deviations,
        # The bench tool only calls the 3% check once the ten strokes are in; a tight
        # spread over three strokes is not evidence of a repeatable calibration.
        "consistency_pass": bool(complete and worst <= CONSISTENCY_TOLERANCE_PCT),
        "median_baseline": median([s["baseline_cmH2O"] for s in strokes]),
        "complete": complete,
    }


def status_message(summary) -> str:
    """The operator-facing line, matching the bench tool's wording."""
    if summary["n_strokes"] == 0:
        return "No accepted strokes yet. Record alternating full push and pull strokes."
    if not summary["complete"]:
        remaining = TARGET_STROKES - summary["n_strokes"]
        return (
            f"Stroke accepted. {remaining} more to go — keep alternating full push "
            f"and pull strokes."
        )
    if summary["consistency_pass"]:
        return (
            f"Calibration passes the {CONSISTENCY_TOLERANCE_PCT:g}% consistency "
            f"check. Save the results."
        )
    return (
        f"Calibration varies by more than {CONSISTENCY_TOLERANCE_PCT:g}% "
        f"({summary['worst_deviation_pct']:.2f}%); inspect strokes, leaks, and zero "
        f"offset before saving."
    )
