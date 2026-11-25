"""
FOTAcceptance.py - ERS-style breath acceptance for the live patient mode.

Two cleanly separated parts, kept apart on purpose:

  PART A  ACQUISITION stop-decision core (pure arithmetic on counts + time):
          `step()` / `AcceptanceMonitor` / `LiveAcquisition`. Given
          (accepted_count, elapsed_seconds, event) it returns a verdict. No DSP,
          no file I/O, no breath segmentation. Fully unit-testable at the number
          level. Ported from Zaid/Alvi's `accepted_breaths.py` (collect-to-target
          model, Henry's chosen acquisition), adapted only in name and docstring.

  PART B  per-breath clean QC: `qc_breath()` / `BreathQC`. Decides whether ONE
          segmented breath (from FOTBreath.phase_resolved_breaths) is clean enough
          to count, using ERS-grounded criteria. Reads a breath dict, never mutates
          the estimator or the impedance pipeline.

GUARDRAIL: acceptance is a PURE COUNT + QC layer. It never modifies a breath, the
lock-in, the phase calibration, or the bias-flow correction. It only reads the
per-breath results FOTBreath produces and decides count / stop / clean.

Software validation only, not clinical validation. Thresholds here are
acquisition-protocol + QC knobs, tunable on real recordings, NOT clinically frozen.

ERS grounding (King GG et al., Eur Respir J 2020;55(2):1900753): >= 3 accepted
breaths per acquisition; within-breath analysis; coherence dropped in favour of
CoV. Acceptable-breath numbers (insp/exp >= 0.35 s, tidal volume >= 100 mL,
>= 70% valid after outlier removal) from FOT validation practice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np

# --- PART A: acquisition-protocol thresholds (CONFIGURABLE, not clinically derived) ---
FLOOR = 3           # >= FLOOR clean breaths -> the take is usable at all (sufficient)
TARGET = 10         # >= TARGET clean breaths -> stop immediately, goal met
CEILING_S = 120.0   # hard time cap on one acquisition [s]
HEARTBEAT_S = 30.0  # status-snapshot cadence [s] (no state change)

# stop_reason vocabulary (orthogonal to `sufficient` - never collapse the two)
REACHED_TARGET = "reached_target"
OPERATOR_STOPPED = "operator_stopped"
CEILING_REACHED = "ceiling_reached"

# event kinds consumed by the core
EV_BREATH = "breath"
EV_OPERATOR_STOP = "operator_stop"
EV_TICK = "tick"


@dataclass(frozen=True)
class Event:
    """A core input event. `clean` is meaningful only for EV_BREATH (it is the
    per-breath QC verdict, passed through - the core never recomputes quality)."""
    kind: str
    clean: bool = False


def breath(clean: bool) -> Event:
    """A segmented breath arriving, carrying its QC-clean verdict."""
    return Event(EV_BREATH, bool(clean))


def operator_stop() -> Event:
    """Operator pressed stop."""
    return Event(EV_OPERATOR_STOP)


def tick() -> Event:
    """A timer tick. The wrapper fires these at heartbeat cadence and at/after the
    ceiling; the core turns a tick into either a heartbeat snapshot (elapsed <
    CEILING_S) or a ceiling stop (elapsed >= CEILING_S)."""
    return Event(EV_TICK)


@dataclass(frozen=True)
class Verdict:
    """The two output fields are ORTHOGONAL and never collapsed:
      stop_reason : WHAT triggered the stop (reached_target / operator_stopped /
                    ceiling_reached)
      sufficient  : whether the take is usable at all, == accepted_count >= FLOOR
    """
    accepted_count: int
    stop_reason: str
    sufficient: bool
    elapsed_at_stop: float


@dataclass(frozen=True)
class StepOutcome:
    """Result of one core step. Exactly one of {halt, status(heartbeat), plain
    continue} is the salient outcome:
      - halt=True  -> `verdict` is set (session is over)
      - status set -> heartbeat snapshot {accepted, elapsed}, no state change
      - otherwise  -> a plain continue (count may have incremented on a clean breath)
    `accepted_count` is the post-event count (unchanged except on a clean breath)."""
    accepted_count: int
    halt: bool
    verdict: Optional[Verdict] = None
    status: Optional[dict] = None


def _sufficient(accepted_count: int) -> bool:
    return accepted_count >= FLOOR


def step(accepted_count: int, elapsed_s: float, event: Event) -> StepOutcome:
    """Pure decision step: (count, elapsed, event) -> StepOutcome. No mutation of
    any external state - returns the next count and any verdict/snapshot.

    Event precedence (matches the four numbered protocol events):
      1. operator_stop -> stop, operator_stopped, sufficient = count >= FLOOR
      2. clean breath  -> count += 1; if count >= TARGET stop reached_target
                          (HALT IMMEDIATELY, mid-interval - do not wait for a tick).
                          A non-clean breath does NOT count and does not halt.
      3. tick, elapsed >= CEILING_S -> stop, ceiling_reached, sufficient = count>=FLOOR
      4. tick, elapsed <  CEILING_S -> heartbeat snapshot, NO state change

    TARGET-vs-CEILING boundary is by ELAPSED, inclusive at the wall: elapsed ==
    CEILING_S is AT-WALL. A clean breath at elapsed < CEILING_S that hits TARGET ->
    reached_target. Once elapsed >= CEILING_S the ceiling is a HARD WALL: any breath
    then resolves as ceiling_reached with the count AS IT STOOD at the wall, NEVER
    reached_target. Edge: count 9 + clean breath at t=119.9 -> reached_target; at
    t=120.0 or t=121 -> ceiling_reached (count stays 9).
    """
    if event.kind == EV_OPERATOR_STOP:
        return StepOutcome(
            accepted_count, True,
            Verdict(accepted_count, OPERATOR_STOPPED, _sufficient(accepted_count), elapsed_s))

    if event.kind == EV_BREATH:
        # HARD WALL, checked FIRST and by ELAPSED (inclusive): a breath arriving
        # at/after the ceiling cannot count and cannot produce reached_target.
        if elapsed_s >= CEILING_S:
            return StepOutcome(
                accepted_count, True,
                Verdict(accepted_count, CEILING_REACHED, _sufficient(accepted_count), elapsed_s))
        if not event.clean:
            # QC-rejected breath: never counts.
            return StepOutcome(accepted_count, False)
        new_count = accepted_count + 1
        if new_count >= TARGET:
            return StepOutcome(
                new_count, True,
                Verdict(new_count, REACHED_TARGET, _sufficient(new_count), elapsed_s))
        return StepOutcome(new_count, False)

    if event.kind == EV_TICK:
        if elapsed_s >= CEILING_S:
            return StepOutcome(
                accepted_count, True,
                Verdict(accepted_count, CEILING_REACHED, _sufficient(accepted_count), elapsed_s))
        return StepOutcome(accepted_count, False,
                           status={"accepted": accepted_count, "elapsed": elapsed_s})

    raise ValueError(f"unknown event kind: {event.kind!r}")


class AcceptanceMonitor:
    """Stateful driver around the pure `step`: holds the running accepted count and
    the final verdict. Once halted it is inert - further events return the stored
    verdict without changing state (so a late breath/tick after target can't
    corrupt the result)."""

    def __init__(self):
        self.accepted_count = 0
        self.halted = False
        self.verdict: Optional[Verdict] = None
        self.statuses: list[dict] = []

    def feed(self, elapsed_s: float, event: Event) -> StepOutcome:
        if self.halted:
            return StepOutcome(self.accepted_count, True, self.verdict)
        out = step(self.accepted_count, elapsed_s, event)
        self.accepted_count = out.accepted_count
        if out.status is not None:
            self.statuses.append(out.status)
        if out.halt:
            self.halted = True
            self.verdict = out.verdict
        return out

    def on_breath(self, clean: bool, elapsed_s: float) -> StepOutcome:
        return self.feed(elapsed_s, breath(clean))

    def on_operator_stop(self, elapsed_s: float) -> StepOutcome:
        return self.feed(elapsed_s, operator_stop())

    def on_tick(self, elapsed_s: float) -> StepOutcome:
        return self.feed(elapsed_s, tick())


class LiveAcquisition:
    """Object form of the acquisition core for an event-driven UI. Three inputs:
      * on_breath(brth)   - a segmented breath arrived (its QC-clean verdict);
      * on_timer(elapsed) - the independent wall-clock timer (heartbeats + ceiling);
      * operator_stop(t)  - the stop button.
    The timer is what makes the ceiling robust to a SILENT stream (apnea / sensor
    dropout): no breath event arrives, so only the timer can enforce the wall."""

    def __init__(self, is_clean: Callable[[dict], bool],
                 heartbeat_s: float = HEARTBEAT_S, ceiling_s: float = CEILING_S):
        self.monitor = AcceptanceMonitor()
        self.is_clean = is_clean
        self.heartbeat_s = heartbeat_s
        self.ceiling_s = ceiling_s
        self._next_hb = heartbeat_s

    def _fire_due_heartbeats(self, upto: float):
        """Fire scheduled heartbeats at/below `upto` but strictly below the ceiling."""
        while (self._next_hb <= upto and self._next_hb < self.ceiling_s
               and not self.monitor.halted):
            self.monitor.on_tick(self._next_hb)
            self._next_hb += self.heartbeat_s

    def on_breath(self, brth: dict) -> StepOutcome:
        t = float(brth["t1"])
        self._fire_due_heartbeats(t)
        if self.monitor.halted:
            return StepOutcome(self.monitor.accepted_count, True, self.monitor.verdict)
        return self.monitor.on_breath(bool(self.is_clean(brth)), t)

    def on_timer(self, elapsed_s: float) -> StepOutcome:
        """Independent wall-clock timer input. The event loop MUST call this on the
        heartbeat cadence AND at the ceiling, on a clock that runs REGARDLESS of
        breath arrivals. It is the ONLY thing that enforces the CEILING_S wall on a
        SILENT stream. Inert after halt."""
        self._fire_due_heartbeats(elapsed_s)
        if not self.monitor.halted and elapsed_s >= self.ceiling_s:
            return self.monitor.on_tick(elapsed_s)
        return StepOutcome(self.monitor.accepted_count, self.monitor.halted,
                           self.monitor.verdict)

    def operator_stop(self, elapsed_s: float) -> StepOutcome:
        return self.monitor.on_operator_stop(elapsed_s)

    @property
    def verdict(self) -> Optional[Verdict]:
        return self.monitor.verdict


# --- PART B: per-breath clean QC (ERS-grounded, tunable) ------------------------------
# CONFIGURABLE, not clinically frozen. See module docstring for provenance.
MIN_PHASE_TIME_S = 0.35        # each of insp/exp must last >= this (ERS acceptable breath)
MIN_TIDAL_VOLUME_L = 0.100     # integrated |breathing flow| over the breath >= 100 mL
MIN_PHASE_WINDOWS = 3          # need >= this many clean short-windows in each phase
                               # (kept in step with FOTBreath.MIN_PHASE_PTS; see there
                               # for why a median of 1 window is not a median)
OUTLIER_MAD_K = 5.0           # reject a breath whose VT/duration is > K robust-sigmas out
REJECT_NEGATIVE_R5_WINDOW = True
# DISPLAY ONLY -- no longer gates sufficiency anywhere. The ERS 2020 rule for whether
# an acquisition survives artefact removal is a COUNT ("at least three breaths
# remaining in that acquisition"), which is FLOOR; there is no clean-fraction
# requirement in the standard. FOTPipeline now uses the count alone, and the results
# view uses this only to label a take "Good" vs "Usable".
VALID_FRACTION_FLOOR = 0.70    # >= 70% of complete breaths clean reads as a tidy take


@dataclass
class BreathQC:
    """Per-breath acceptance config. Mirrors the ERS-grounded knobs above so a caller
    can override without touching module state."""
    min_phase_time_s: float = MIN_PHASE_TIME_S
    min_tidal_volume_l: float = MIN_TIDAL_VOLUME_L
    min_phase_windows: int = MIN_PHASE_WINDOWS
    reject_negative_r5_window: bool = REJECT_NEGATIVE_R5_WINDOW


def qc_breath(brth: dict, qc: BreathQC | None = None) -> tuple[bool, list[str]]:
    """Return (is_clean, [reasons]) for ONE breath dict from FOTBreath.

    Expects the FOTBreath per-breath contract: t0, t1, n_insp, n_exp, R5_insp,
    R5_exp, X5_insp, X5_exp, and optionally `insp_time_s`, `exp_time_s`,
    `tidal_volume_l`, `r5_windows` (list of per-window R5 inside the breath).
    Missing optional fields skip only their own check (so this works on partial
    dicts during bring-up). Criteria that are always available (window counts,
    finiteness) always run.
    """
    qc = qc or BreathQC()
    reasons: list[str] = []

    if int(brth.get("n_insp", 0)) < qc.min_phase_windows:
        reasons.append(f"too_few_insp_windows:{brth.get('n_insp')}<{qc.min_phase_windows}")
    if int(brth.get("n_exp", 0)) < qc.min_phase_windows:
        reasons.append(f"too_few_exp_windows:{brth.get('n_exp')}<{qc.min_phase_windows}")

    insp_time = brth.get("insp_time_s")
    exp_time = brth.get("exp_time_s")
    if insp_time is not None and insp_time < qc.min_phase_time_s:
        reasons.append(f"insp_too_short:{insp_time:.3f}<{qc.min_phase_time_s}")
    if exp_time is not None and exp_time < qc.min_phase_time_s:
        reasons.append(f"exp_too_short:{exp_time:.3f}<{qc.min_phase_time_s}")

    vt = brth.get("tidal_volume_l")
    if vt is not None and vt < qc.min_tidal_volume_l:
        reasons.append(f"tidal_volume_too_small:{vt:.4f}<{qc.min_tidal_volume_l}")

    if qc.reject_negative_r5_window:
        r5s = brth.get("r5_windows")
        if r5s is not None and any(r < 0 for r in r5s):
            reasons.append("negative_R5_window")

    finite = np.isfinite([
        brth.get("R5_insp", np.nan), brth.get("R5_exp", np.nan),
        brth.get("X5_insp", np.nan), brth.get("X5_exp", np.nan),
    ])
    if not np.all(finite):
        reasons.append("non_finite_impedance")

    return (len(reasons) == 0), reasons


def summarise_take(clean_breaths: Iterable[dict]) -> dict:
    """Ensemble-average one take's clean breaths into a single clinical result.

    Pure arithmetic on breath dicts (the FOTBreath contract: R5_insp, R5_exp,
    X5_insp, X5_exp). dXrs/dRrs are the mean of each breath's OWN insp-exp
    difference (not the difference of the means), matching the per-breath
    within-breath definition used throughout. cov_r5 is the between-breath
    coefficient of variation of each breath's mean R5 (insp/exp averaged),
    the ERS reproducibility statistic - NaN if fewer than 2 breaths or the
    mean is zero. n_clean == 0 returns an all-NaN result with n_clean = 0,
    never raises, so a caller can always safely display it."""
    breaths = list(clean_breaths)
    n = len(breaths)
    if n == 0:
        return {
            "Rrs_insp": float("nan"), "Rrs_exp": float("nan"),
            "Xrs_insp": float("nan"), "Xrs_exp": float("nan"),
            "dXrs": float("nan"), "dRrs": float("nan"),
            "cov_r5": float("nan"), "n_clean": 0,
        }

    ri = np.array([float(b["R5_insp"]) for b in breaths])
    re = np.array([float(b["R5_exp"]) for b in breaths])
    xi = np.array([float(b["X5_insp"]) for b in breaths])
    xe = np.array([float(b["X5_exp"]) for b in breaths])

    r5_per_breath = (ri + re) / 2.0
    mean_r5 = float(np.mean(r5_per_breath))
    if n >= 2 and mean_r5 != 0.0:
        cov_r5 = float(np.std(r5_per_breath, ddof=1) / mean_r5)
    else:
        cov_r5 = float("nan")

    return {
        "Rrs_insp": float(np.mean(ri)), "Rrs_exp": float(np.mean(re)),
        "Xrs_insp": float(np.mean(xi)), "Xrs_exp": float(np.mean(xe)),
        # Both deltas are expiration minus inspiration; see FOTBreath for why dXrs
        # changed direction.
        "dXrs": float(np.mean(xe - xi)), "dRrs": float(np.mean(re - ri)),
        "cov_r5": cov_r5, "n_clean": n,
    }


def flag_outlier_breaths(breaths: Iterable[dict], k: float = OUTLIER_MAD_K) -> list[bool]:
    """Robust (median/MAD) outlier flag per breath on tidal volume and duration.

    A cough is a huge-volume, often short breath; an irregular/partial breath sits
    far from the session's typical breath. Returns a list of bools aligned to the
    input (True = outlier, should be rejected). Adaptive, so no fixed magic
    threshold; needs >= 3 breaths to be meaningful (else all False).
    """
    breaths = list(breaths)
    n = len(breaths)
    flags = [False] * n
    if n < 3:
        return flags

    def _mad_outliers(vals: list[float]) -> list[bool]:
        arr = np.asarray(vals, dtype=float)
        finite = np.isfinite(arr)
        if finite.sum() < 3:
            return [False] * len(arr)
        med = np.median(arr[finite])
        mad = np.median(np.abs(arr[finite] - med))
        robust_sigma = 1.4826 * mad          # MAD -> sigma for a normal distribution
        if robust_sigma <= 0:
            # near-constant bulk (MAD masks a lone outlier): fall back to a std scale
            # so a gross outlier is still caught instead of silently passing.
            robust_sigma = float(np.std(arr[finite]))
            if robust_sigma <= 0:
                return [False] * len(arr)
        return [bool(np.isfinite(v) and abs(v - med) > k * robust_sigma) for v in arr]

    vt = [float(b.get("tidal_volume_l", np.nan)) for b in breaths]
    dur = [float(b.get("t1", np.nan)) - float(b.get("t0", np.nan)) for b in breaths]
    vt_out = _mad_outliers(vt)
    dur_out = _mad_outliers(dur)
    return [a or b for a, b in zip(vt_out, dur_out)]
