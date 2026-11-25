"""
FOTBreath.py - breath segmentation + within-breath (phase-resolved) impedance.

The live-patient path that Henry's bench engine lacks. It sits ON TOP of the
existing 6 Hz LUT-locked lock-in in FOTImpedance (reused, not modified) and adds:

  1. segment_breaths(): split the SLOW breathing flow into inhale/exhale phases
     (low-pass to strip the 6 Hz tone, hysteresis dead-band so the label does not
     flicker at the zero crossing). +flow = inhale, -flow = exhale (Henry's wiring).
  2. short_window_phasor(): a lock-in phasor over a SHORT (< 1 s) window with a Hann
     taper, so a window holding a non-integer number of 6 Hz cycles does not leak
     ("click"). The bench 1 s integer-cycle path never needs this; only this path.
  3. phase_resolved_breaths(): slide the short window across the record, assign each
     window to inhale or exhale, DROP windows straddling the boundary, median per
     phase -> per-breath R5/X5, dXrs/dRrs, phase times, tidal volume.

WHY short + Hann: the impedance window must be shorter than a breath phase or
inspiration and expiration blend and dXrs collapses to ~0. Short windows are noisy,
so we median several per phase and across breaths. Non-integer cycles in a short
window cause spectral leakage that lands mostly on X (the clinical signal), which
the Hann taper removes (validated: X error ~6% -> ~0.2% on a synthetic tone).

Software validation only, not clinical validation. Window/hop/hysteresis are knobs,
tunable on real recordings. Ported segmentation logic from Zaid/Alvi's
phase_resolved.segment_breaths (numpy/scipy only), retargeted to 6 Hz / 420 Hz.
"""
from __future__ import annotations

import numpy as np

from backend.processing.FOTImpedance import (
    LUT_SIZE,
    SAMPLE_HZ,
    MIN_FLOW_AMPLITUDE_LPS,
    BIAS_CORR_SCALE,
    rotate_impedance,
)

# ---- tunable parameters (all overridable; see plan for rationale) --------------------
WINDOW_S = 0.5        # short impedance window [s]; 3 cycles at 6 Hz; MUST fit in a phase
HOP_S = 0.1           # slide the window this far between readings [s]
BREATH_LP_HZ = 1.5    # low-pass cutoff [Hz] to isolate breathing from the 6 Hz tone
HYST_FRAC = 0.10      # hysteresis dead-band, as a fraction of breathing amplitude
MIN_PHASE_PTS = 3     # need >= this many clean short-windows in a phase to trust it
# WHY 3 and not 1: the phase value is the MEDIAN of the windows inside that phase, and
# a median of one window is just that window, with none of the robustness the median is
# there to provide. At 0.5 s windows on a 0.1 s hop a normal phase yields ~10 windows,
# so 3 is a floor, not a quota. It only bites once DROP_NEGATIVE_R5_WINDOWS starts
# removing windows: on MEAS0006 that rescued 6 breaths from whole-breath rejection, but
# four of them were left with 1-6 windows total and their noise took CoV(R5) from 2.6%
# to 24.7%. Requiring 3 keeps 8 of 13 breaths at CoV 8.7% (5 would give 7 breaths at
# 2.3%). MEAS0007/0008 are unaffected at any setting -- every breath there has 18+
# windows per phase.
# Drop individual short windows whose R5 is negative, BEFORE the per-phase median.
# ERS 2020 technical standard: "Windows containing negative resistances should be
# excluded as they are physiologically implausible. They may result from poor design
# of hardware or signal processing, or noise artefacts such as cough." The standard
# explicitly allows removing the affected SEGMENTS rather than the whole breath
# ("Laboratories and research studies may exclude individual breaths, only the
# affected segments, or entire recordings"), which is what this does: a breath with
# one bad window keeps its other windows instead of being thrown away entirely.
# Measured on the three real patient records, this alone took CoV(R5) from 54.8% to
# 3.6% and 51.9% to 3.5% without discarding a single breath. A phase left with no
# surviving window falls out anyway via MIN_PHASE_PTS / quality_ok.
DROP_NEGATIVE_R5_WINDOWS = True


def _moving_average(sig, win):
    """Centered (zero-phase) boxcar moving average, edge-padded. Odd window."""
    sig = np.asarray(sig, dtype=float)
    if win <= 1:
        return sig
    if win % 2 == 0:
        win += 1
    pad = win // 2
    kernel = np.ones(win) / win
    padded = np.pad(sig, pad, mode="edge")
    return np.convolve(padded, kernel, mode="valid")  # length preserved, zero-phase


def _lowpass(sig, fs, cutoff):
    """Numpy-only zero-phase breathing low-pass (no scipy, matching the engine's
    dependency-light style). A boxcar of length fs/cutoff nulls that frequency and its
    multiples; with cutoff=1.5 Hz at 420 Hz the window is ~280 samples, whose nulls
    include the 6 Hz tone (1.5 x 4), so the oscillation is removed exactly. Applied
    twice for a smoother roll-off, leaving the slow breathing waveform.

    The window is rounded to WHOLE excitation cycles (fs / LUT_SIZE is the tone
    frequency), so its nulls always include the tone, at any frequency: 3 cycles at
    5 Hz, 4 at 6 Hz (280 samples, unchanged), 7 at 11 Hz, 13 at 19 Hz. A plain
    fs / cutoff only lands on the tone at 6 Hz and leaks it into the breathing trace
    (and so into breath segmentation and tidal volume) everywhere else."""
    tone_hz = fs / LUT_SIZE
    cycles = max(1, int(round(tone_hz / cutoff)))
    win = cycles * LUT_SIZE
    y = _moving_average(sig, win)
    y = _moving_average(y, win)
    return y


def segment_breaths(flow, fs=SAMPLE_HZ, breath_lp_hz=BREATH_LP_HZ, hyst_frac=HYST_FRAC):
    """Label every sample 'insp' or 'exp' from the SLOW breathing flow.

    breathing = low-pass(flow) strips the 6 Hz oscillation + noise, leaving the slow
    in/out airflow. We segment on THIS, never on the tone, because the oscillation
    carries no breath phase. +flow = inhale (Henry's sensor wiring). Hysteresis (a
    dead-band around 0) stops the label flickering near a zero crossing.

    The bias-flow baseline is the MEAN, never the median (see the comment at the
    subtraction for why the median destroys the I:E ratio).

    Returns (breathing, phase, starts): the low-passed bias-removed flow, a per-sample
    object array of 'insp'/'exp', and the sample indices where each breath starts
    (exp -> insp flips).
    """
    flow = np.asarray(flow, dtype=float)
    breathing = _lowpass(flow, fs, breath_lp_hz)
    # Remove bias flow / DC offset. MUST be the mean, not the median: whatever volume
    # is inhaled is exhaled, so the breathing component integrates to zero and the mean
    # of the record IS the bias flow. The median is the 50th percentile, so subtracting
    # it forces exactly half the samples either side of zero and pins the measured I:E
    # ratio at 1:1 no matter what the subject did. That silently erases prolonged
    # expiration, which is the signature of the obstruction this device exists to find
    # (measured on a real subject: true I:E 41/59, median baseline reported 50/50).
    breathing = breathing - np.mean(breathing)
    amp = np.percentile(np.abs(breathing), 90)       # robust amplitude (ignores spikes)
    thr = hyst_frac * amp

    phase = np.empty(len(breathing), dtype=object)
    state = "exp"
    for i, v in enumerate(breathing):
        if state == "exp" and v > thr:
            state = "insp"
        elif state == "insp" and v < -thr:
            state = "exp"
        phase[i] = state
    starts = [i for i in range(1, len(phase))
              if phase[i] == "insp" and phase[i - 1] == "exp"]
    return breathing, phase, starts


def short_window_phasor(values, indices, window=None) -> complex:
    """Lock-in phasor over one window, optionally Hann-tapered.

    Same reference-index math as FOTImpedance.lock_in_phasor (uses the Arduino LUT
    index, not PC time), but on an arbitrary-length window with a window weight and
    coherent-gain normalisation, so it also works for short, non-integer-cycle
    windows. With window=None and an integer number of cycles it reduces exactly to
    the 2/N rectangular lock-in.
    """
    x = np.asarray(values, dtype=float)
    idx = np.asarray(indices, dtype=float)
    if len(x) != len(idx) or len(x) == 0:
        raise ValueError("values and indices must have equal non-zero length")
    x = x - np.mean(x)
    ref = np.exp(-1j * 2.0 * np.pi * idx / LUT_SIZE)
    if window is None:
        return complex((2.0 / len(x)) * np.sum(x * ref))
    w = np.asarray(window, dtype=float)
    denom = np.sum(w)
    if denom == 0:
        raise ValueError("window has zero sum")
    return complex(2.0 * np.sum(x * w * ref) / denom)


def _window_impedance(pressure, flow, indices, window, correction_deg):
    """Phase-calibrated impedance for one short window. Returns (Z, |P|, |Q|)."""
    p = short_window_phasor(pressure, indices, window)
    q = short_window_phasor(flow, indices, window)
    if abs(q) <= 0.0:
        return complex(np.nan, np.nan), abs(p), abs(q)
    raw = p / q
    return rotate_impedance(raw, correction_deg), abs(p), abs(q)


def phase_resolved_breaths(
    pressure,
    flow,
    indices,
    fs=SAMPLE_HZ,
    correction_deg=0.0,
    window_s=WINDOW_S,
    hop_s=HOP_S,
    min_phase_pts=MIN_PHASE_PTS,
    apply_scale=False,
    drop_negative_r5_windows=DROP_NEGATIVE_R5_WINDOWS,
):
    """Per-breath inhale/exhale R5/X5 over a recording (or a live buffer).

    Slides a short Hann window at `hop_s`, computes phase-calibrated R5/X5 per window,
    assigns each window fully inside a single phase to that phase, DROPS straddlers,
    and medians per phase per breath. Always reuses the phase calibration angle via
    rotate_impedance.

    `apply_scale`: multiply each phase R5/X5 by the flow-gain SCALE (BIAS_CORR_SCALE).
    This is the multiplicative part of the bench correction; it scales X, so it sets
    the dXrs magnitude. The additive K_PLUMBING*Q_bias part is NOT applied here: it hits
    R only and is bias-flow dependent, so it is left to the caller to add/toggle using
    the per-breath `bias_flow_lps` returned below (see plan; the term is a mix of
    equipment and real flow-dependent airway resistance, so it must be A/B-checked on
    real breaths, not baked in).

    Returns {"breaths": [dict...], "phase", "breathing", "starts"}. Each breath dict
    matches the acceptance contract: k, t0, t1, X5_insp, X5_exp, dXrs, R5_insp,
    R5_exp, dRrs, n_insp, n_exp, insp_time_s, exp_time_s, tidal_volume_l, r5_windows,
    quality_ok.
    """
    P = np.asarray(pressure, dtype=float)
    F = np.asarray(flow, dtype=float)
    idx = np.asarray(indices)
    n = len(P)
    if not (len(F) == n == len(idx)):
        raise ValueError("pressure, flow, indices must have equal length")

    breathing, phase, starts = segment_breaths(F, fs)

    win = int(round(window_s * fs))
    hop = max(1, int(round(hop_s * fs)))
    hann = np.hanning(win)

    # continuous short-window trace: (start_sample, end_sample, R5, X5, locked)
    trace = []
    for s in range(0, n - win + 1, hop):
        sl = slice(s, s + win)
        Z, _pa, qa = _window_impedance(P[sl], F[sl], idx[sl], hann, correction_deg)
        locked = bool(qa >= MIN_FLOW_AMPLITUDE_LPS and np.isfinite(Z.real))
        # ERS: a window whose resistance is negative is physiologically impossible, so
        # it is excluded here rather than being allowed to drag the phase median (or
        # condemn the whole breath). See DROP_NEGATIVE_R5_WINDOWS.
        if locked and drop_negative_r5_windows and Z.real < 0.0:
            locked = False
        trace.append((s, s + win, float(Z.real), float(Z.imag), locked))

    breaths = []
    for k in range(len(starts) - 1):
        b0, b1 = starts[k], starts[k + 1]
        insp_r, insp_x, exp_r, exp_x, r5_all = [], [], [], [], []
        for (a, b, R5, X5, locked) in trace:
            if a < b0 or b > b1 or not locked:       # outside this breath, or unlocked
                continue
            seg = phase[a:b]
            if np.all(seg == "insp"):
                insp_r.append(R5); insp_x.append(X5); r5_all.append(R5)
            elif np.all(seg == "exp"):
                exp_r.append(R5); exp_x.append(X5); r5_all.append(R5)
            # mixed -> straddles the phase boundary -> dropped

        seg_breath = phase[b0:b1]
        insp_mask = (seg_breath == "insp")
        insp_time = float(np.sum(insp_mask) / fs)
        exp_time = float(np.sum(seg_breath == "exp") / fs)
        # tidal volume = inhaled volume = integral of positive breathing flow over insp
        tidal_volume_l = float(np.sum(np.clip(breathing[b0:b1][insp_mask], 0.0, None)) / fs)

        ok = len(insp_x) >= min_phase_pts and len(exp_x) >= min_phase_pts
        xi = float(np.median(insp_x)) if insp_x else np.nan
        xe = float(np.median(exp_x)) if exp_x else np.nan
        ri = float(np.median(insp_r)) if insp_r else np.nan
        re = float(np.median(exp_r)) if exp_r else np.nan
        if apply_scale:
            xi *= BIAS_CORR_SCALE; xe *= BIAS_CORR_SCALE
            ri *= BIAS_CORR_SCALE; re *= BIAS_CORR_SCALE
        # net bias/leak flow over the breath (tidal in/out cancels; leaves CPAP bias).
        # This is the Q_bias the optional K_PLUMBING term would multiply.
        bias_flow_lps = float(np.mean(F[b0:b1]))
        # ASYMMETRIC convention (do NOT reverse): both go positive under obstruction.
        breaths.append({
            "k": k, "t0": b0 / fs, "t1": b1 / fs,
            # BOTH deltas are EXPIRATION MINUS INSPIRATION. dXrs used to be the other
            # way round (insp - exp, the Dellacca EFL sign), which meant the two numbers
            # shown side by side on the results screen pointed in opposite directions
            # and disagreed in sign with the hospital's FOT device. Under expiratory
            # flow limitation expiratory reactance falls, so EFL is now a large NEGATIVE
            # dXrs and the threshold test is signed accordingly.
            "X5_insp": xi, "X5_exp": xe, "dXrs": xe - xi,
            "R5_insp": ri, "R5_exp": re, "dRrs": re - ri,
            "n_insp": len(insp_x), "n_exp": len(exp_x),
            "insp_time_s": insp_time, "exp_time_s": exp_time,
            "tidal_volume_l": tidal_volume_l, "bias_flow_lps": bias_flow_lps,
            "r5_windows": r5_all, "quality_ok": ok,
        })

    return {"breaths": breaths, "phase": phase, "breathing": breathing, "starts": starts}
