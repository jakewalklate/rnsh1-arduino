# Live Waveforms Component

This Streamlit custom component renders the live pressure and flow waveforms for
the measurement page.

## Current Design

- Python reads the active measurement CSV once per Streamlit fragment update.
- Streamlit sends the current time-window samples to the component.
- The browser stores received samples in memory.
- The cursor is animated with `requestAnimationFrame`, so it can move smoothly
  between Streamlit reruns.
- The renderer currently uses Canvas 2D. This keeps the component build-free and
  easy to test before moving to a TypeScript/WebGL implementation.

## Inputs

- `samples`: recent pressure/flow samples.
- `elapsedSeconds`: current recording time.
- `recording`: whether the measurement is actively recording.
- `timeWindowSeconds`: displayed time segment length.
- `pressureYRange`: pressure graph y-axis range.
- `flowYRange`: flow graph y-axis range.
- `excludedIntervals`: future QC overlay regions.

## Future Upgrade Path

The Python wrapper should stay stable. If Canvas 2D becomes limiting, replace
the renderer inside `static/index.html` with a WebGL renderer while keeping the
same component inputs.
