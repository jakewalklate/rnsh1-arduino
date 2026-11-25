from pathlib import Path

import streamlit.components.v1 as components


_COMPONENT_DIR = Path(__file__).parent / "static"
_live_waveforms = components.declare_component(
    "live_waveforms",
    path=_COMPONENT_DIR,
)


def live_waveforms(
    samples,
    elapsed_seconds,
    recording,
    time_window_seconds=30,
    playback_delay_seconds= 0.2,
    pressure_y_range=(-2.0, 2.0),
    flow_y_range=(-2.0, 2.0),
    excluded_intervals=None,
    key=None,
):
    return _live_waveforms(
        samples=samples,
        elapsedSeconds=elapsed_seconds or 0,
        recording=recording,
        timeWindowSeconds=time_window_seconds,
        playbackDelaySeconds=playback_delay_seconds,
        pressureYRange=list(pressure_y_range),
        flowYRange=list(flow_y_range),
        excludedIntervals=excluded_intervals or [],
        default=None,
        key=key,
    )
