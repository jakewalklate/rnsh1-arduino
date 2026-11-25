"""Per-period calibration presets, with history.

A rig does not behave identically all day: the blower warms, the room changes, the
tubing settles. This keeps one calibration preset per time-of-day period and can apply
whichever matches the clock, so a morning calibration is not still in force at 6pm.

A preset can contain a phase angle, bias constants, or both. The normal and safest
workflow stores both together: a bias sweep's R/X are already rotated by the angle in
force when it was captured. Partial updates are supported for deliberate re-use of one
component, and the UI warns that independently captured components need review.

The engine itself resolves one file per frequency and knows nothing about periods, so
"activating" a preset writes its two payloads into those canonical files. FOTImpedance
stays byte-identical to Henry's validated engine; nothing here forks it.

Pure data + clock logic. No Streamlit, no serial.
"""

from copy import deepcopy
from datetime import datetime, time, timezone
from pathlib import Path

from backend.processing.FOTImpedance import (
    EXCITATION_HZ,
    bias_calibration_path_for,
    load_bias_calibration_for,
    load_calibration,
    save_bias_calibration,
    save_calibration,
)
from backend.processing.FOTPhaseCalibration import phase_calibration_path_for

PERIODS = ("morning", "afternoon", "evening")

# Local-clock windows. Evening deliberately wraps past midnight so every hour of the
# day maps to exactly one period and `period_for` can never return None.
PERIOD_WINDOWS = {
    "morning": (time(5, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), time(5, 0)),
}

# Entries kept per period before the oldest is dropped. Enough to see a week of
# three-a-day calibrations without the file growing without bound.
MAX_HISTORY_PER_PERIOD = 20


def period_label(period):
    start, end = PERIOD_WINDOWS[period]
    return f"{period.title()} ({start:%H:%M}–{end:%H:%M})"


def period_for(now=None):
    """Which period a moment falls in, by LOCAL clock time."""
    now = now or datetime.now()
    current = now.time()
    for period, (start, end) in PERIOD_WINDOWS.items():
        if start <= end:
            if start <= current < end:
                return period
        elif current >= start or current < end:   # the wrapping window
            return period
    # Unreachable while the windows tile the day; kept so a bad edit fails loudly
    # rather than returning None into the UI.
    raise ValueError(f"no period covers {current}")


def summarise(phase, bias):
    """The few numbers the history table shows, pulled out once so the UI never has to
    reach into the stored payloads."""
    summary = {}
    if phase:
        summary["correction_angle_deg"] = phase.get("correction_angle_deg")
        summary["phase_pass"] = phase.get("calibration_pass")
    if bias:
        summary["scale"] = bias.get("scale")
        summary["k_plumbing"] = bias.get("k_plumbing")
        summary["fit_r_squared"] = bias.get("fit_r_squared")
        summary["n_points"] = bias.get("n_points")
        summary["r_degree"] = bias.get("r_degree")
        summary["x_degree"] = bias.get("x_degree")
    return summary


def make_entry(phase, bias, note=""):
    return {
        "saved_utc": datetime.now(timezone.utc).isoformat(),
        "note": note,
        "phase": deepcopy(phase) if phase else None,
        "bias": deepcopy(bias) if bias else None,
        "summary": summarise(phase, bias),
    }


def _empty_store(hz):
    return {
        "excitation_hz": float(hz),
        "auto_apply": False,
        "active_period": None,
        "periods": {p: {"current": None, "history": []} for p in PERIODS},
    }


class PresetStore:
    """The preset file for one excitation frequency. Read/modify/write on each call —
    the store is small and several Streamlit sessions may hold the same service."""

    def __init__(self, path, hz=EXCITATION_HZ, phase_path=None, bias_path=None):
        """`phase_path`/`bias_path` are the files activate() writes into, defaulting to
        the canonical pair the engine reads. They are overridable so a test can exercise
        activation without touching the live calibration — activate() has real side
        effects on the running system, which is exactly what makes it worth testing."""
        self.path = Path(path)
        self.hz = float(hz)
        self.phase_path = Path(phase_path) if phase_path else phase_calibration_path_for(hz)
        self.bias_path = Path(bias_path) if bias_path else bias_calibration_path_for(hz)

    # -------------------------------------------------------------------- storage

    def load(self):
        data = load_calibration(self.path)
        if not data:
            return _empty_store(self.hz)
        # Tolerate a file written by an older version: fill in anything missing rather
        # than KeyError-ing in the middle of a clinic.
        store = _empty_store(self.hz)
        store["auto_apply"] = bool(data.get("auto_apply", False))
        store["active_period"] = data.get("active_period")
        for period in PERIODS:
            saved = (data.get("periods") or {}).get(period) or {}
            store["periods"][period] = {
                "current": saved.get("current"),
                "history": list(saved.get("history") or []),
            }
        return store

    def save(self, store):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        save_calibration(self.path, store)
        return self.path

    # ------------------------------------------------------------------- mutation

    def set_auto_apply(self, enabled):
        store = self.load()
        store["auto_apply"] = bool(enabled)
        self.save(store)
        return store["auto_apply"]

    def record(self, period, phase, bias, note=""):
        """Make a freshly measured calibration this period's current preset. The
        preset it replaces moves into history, so nothing is ever overwritten silently.
        """
        if period not in PERIODS:
            raise ValueError(f"unknown period {period!r}")
        store = self.load()
        slot = store["periods"][period]
        if slot["current"] is not None:
            slot["history"].insert(0, slot["current"])
            del slot["history"][MAX_HISTORY_PER_PERIOD:]
        slot["current"] = make_entry(phase, bias, note)
        self.save(store)
        return slot["current"]

    def restore(self, period, index):
        """Promote a history entry back to current. The preset being displaced goes
        into history itself, so restoring is undoable."""
        store = self.load()
        slot = store["periods"][period]
        if not 0 <= index < len(slot["history"]):
            raise IndexError(f"no history entry {index} for {period}")
        entry = slot["history"].pop(index)
        if slot["current"] is not None:
            slot["history"].insert(0, slot["current"])
            del slot["history"][MAX_HISTORY_PER_PERIOD:]
        slot["current"] = entry
        self.save(store)
        return entry

    def delete_history_entry(self, period, index):
        store = self.load()
        slot = store["periods"][period]
        if not 0 <= index < len(slot["history"]):
            raise IndexError(f"no history entry {index} for {period}")
        removed = slot["history"].pop(index)
        self.save(store)
        return removed

    # ----------------------------------------------------------------- activation

    def activate(self, period):
        """Write this period's preset into the canonical files the engine reads, and
        refresh the in-process bias constants so it takes effect without a restart.

        Returns the entry applied, or None when the period has no preset yet — in which
        case the files are left exactly as they are. Applying "nothing" by blanking the
        calibration would be far worse than leaving the previous one in force.
        """
        store = self.load()
        entry = store["periods"][period]["current"]
        if entry is None:
            return None

        if entry.get("phase"):
            self.phase_path.parent.mkdir(parents=True, exist_ok=True)
            save_calibration(self.phase_path, entry["phase"])
        if entry.get("bias"):
            self.bias_path.parent.mkdir(parents=True, exist_ok=True)
            save_calibration(self.bias_path, entry["bias"])
            # bias_flow_corrected() reads these as module globals at call time, so this
            # is what makes activation live rather than pending a restart. It reloads
            # from the canonical file, so it is only meaningful when that is the file we
            # just wrote — a redirected bias_path is a test, and has nothing to refresh.
            if self.bias_path == bias_calibration_path_for(self.hz):
                load_bias_calibration_for(self.hz)

        store["active_period"] = period
        self.save(store)
        return entry

    def ensure_auto_applied(self, now=None):
        """Apply the preset for the current clock period, if auto-apply is on and it is
        not already the active one. Cheap and idempotent — safe to call on every render
        and before every analysis. Returns the period switched TO, else None.
        """
        store = self.load()
        if not store["auto_apply"]:
            return None
        wanted = period_for(now)
        if store["active_period"] == wanted:
            return None
        if store["periods"][wanted]["current"] is None:
            # Nothing measured for this period yet. Leave the previous calibration in
            # force; the UI says which period is missing.
            return None
        self.activate(wanted)
        return wanted

    # -------------------------------------------------------------------- reading

    def status(self, now=None):
        """Everything the UI needs in one read, so a render never loads the file twice."""
        store = self.load()
        return {
            "auto_apply": store["auto_apply"],
            "active_period": store["active_period"],
            "clock_period": period_for(now),
            "periods": store["periods"],
        }


def canonical_paths(hz=EXCITATION_HZ):
    """The two files a preset is written into, for display."""
    return phase_calibration_path_for(hz), bias_calibration_path_for(hz)


def default_store_path(hz=EXCITATION_HZ):
    """Beside the sweep CSVs and check logs — this is operator data, not source."""
    from backend.config import RAW_MEASUREMENT_DATA_DIR

    directory = Path(RAW_MEASUREMENT_DATA_DIR).parent / "calibration"
    return directory / f"calibration_presets_{hz:g}hz.json"


def default_store(hz=EXCITATION_HZ):
    return PresetStore(default_store_path(hz), hz)


def ensure_auto_applied_calibration(hz=EXCITATION_HZ, now=None):
    """Module-level entry point for callers that have no service handle — notably the
    measurement pipeline. Never allowed to break an analysis: a missing or malformed
    preset file means the calibration already in force simply stays in force."""
    try:
        return default_store(hz).ensure_auto_applied(now)
    except Exception:
        return None
