"""How usable a saved measurement is, and so how usable a test is.

One rule, used by the results page (the "Measurement quality" line) and by the
patient page's Previous tests list (the quality tag):

  * good:         at least FLOOR clean breaths, and at least VALID_FRACTION_FLOOR of
                  the breaths clean.
  * usable:       at least FLOOR clean breaths, but many noisy ones.
  * insufficient: fewer than FLOOR clean breaths. No reliable within-breath result.

FLOOR (3) is the analysis engine's own sufficiency rule (FOTAcceptance), so
"insufficient" here means the same as the engine reporting "insufficient clean
breaths".

A test is as good as its best measurement: one good measurement is enough.

A test's stored status ("Waiting" / "Complete") is NOT changed by any of this. It
records whether a measurement has been saved, and other code relies on that.
"""

from __future__ import annotations

import re
from typing import Iterable

from backend.processing.FOTAcceptance import FLOOR, VALID_FRACTION_FLOOR

GOOD = "good"
USABLE = "usable"
INSUFFICIENT = "insufficient"

_RANK = {GOOD: 3, USABLE: 2, INSUFFICIENT: 1}

# The analysis note of measurements saved before clean_breath_count had its own
# column still carries the count: "...; QC 6/13 clean breaths; ..."
_QC_COUNTS = re.compile(r"QC (\d+)/(\d+) clean breaths")


def breath_counts(results: dict | None):
    """(clean, total) breaths for one measurement's results, or (None, None)."""
    if not results:
        return None, None
    clean, total = results.get("clean_breath_count"), results.get("breath_count")
    if clean is not None and total:
        return int(clean), int(total)
    match = _QC_COUNTS.search(results.get("status") or "")
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def measurement_quality(results: dict | None):
    """GOOD, USABLE, INSUFFICIENT, or None when the measurement was not analysed."""
    clean, total = breath_counts(results)
    if clean is None or not total:
        return None
    if clean < FLOOR:
        return INSUFFICIENT
    if clean / total >= VALID_FRACTION_FLOOR:
        return GOOD
    return USABLE


def best_quality(all_results: Iterable[dict | None]):
    """The best quality among a test's measurements, or None if none was analysed."""
    found = [q for q in (measurement_quality(r) for r in all_results) if q]
    return max(found, key=_RANK.get) if found else None


def sensors_near_full_scale(results: dict | None) -> bool:
    """True when the analysis flagged the pressure or flow sensor as near full scale,
    i.e. the signal was probably clipped."""
    return bool(results) and "near full scale" in (results.get("status") or "")
