import csv

import streamlit as st

from backend.device.sample_csv_writer import get_measurement_csv_path
from frontend.components.live_waveforms import live_waveforms


DEFAULT_TIME_WINDOW_SECONDS = 30
DEFAULT_WAVEFORM_PLAYBACK_DELAY_SECONDS = 1.0
DEFAULT_PRESSURE_Y_RANGE = (-2.0, 8.0)
DEFAULT_FLOW_Y_RANGE = (-2.0, 2.0)

PLOTLY_CONFIG = {
    "displayModeBar": False,
    "responsive": True,
}


def waveform_view_key(test_id, name):
    return f"waveform_{name}_{test_id}"


def initialise_waveform_view_state(test_id):
    defaults = {
        waveform_view_key(test_id, "time_window_seconds"): DEFAULT_TIME_WINDOW_SECONDS,
        waveform_view_key(test_id, "pressure_y_range"): DEFAULT_PRESSURE_Y_RANGE,
        waveform_view_key(test_id, "flow_y_range"): DEFAULT_FLOW_Y_RANGE,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def get_waveform_view_settings(test_id):
    initialise_waveform_view_state(test_id)

    return {
        "time_window_seconds": st.session_state[
            waveform_view_key(test_id, "time_window_seconds")
        ],
        "pressure_y_range": st.session_state[
            waveform_view_key(test_id, "pressure_y_range")
        ],
        "flow_y_range": st.session_state[
            waveform_view_key(test_id, "flow_y_range")
        ],
    }


def reset_waveform_view_state(test_id):
    st.session_state[waveform_view_key(test_id, "time_window_seconds")] = (
        DEFAULT_TIME_WINDOW_SECONDS
    )
    st.session_state[waveform_view_key(test_id, "pressure_y_range")] = (
        DEFAULT_PRESSURE_Y_RANGE
    )
    st.session_state[waveform_view_key(test_id, "flow_y_range")] = (
        DEFAULT_FLOW_Y_RANGE
    )


def render_waveform_options_controls(test_id):
    initialise_waveform_view_state(test_id)
    st.number_input(
        "Time Window (seconds)",
        min_value=5,
        max_value=120,
        step=5,
        key=waveform_view_key(test_id, "time_window_seconds"),
    )

    st.slider(
        "Pressure Y Range (cmH2O)",
        min_value=-20.0,
        max_value=20.0,
        step=0.5,
        key=waveform_view_key(test_id, "pressure_y_range"),
    )

    st.slider(
        "Flow Y Range (L/s)",
        min_value=-10.0,
        max_value=10.0,
        step=0.5,
        key=waveform_view_key(test_id, "flow_y_range"),
    )

    if st.button(
        "Reset",
        width="stretch",
        on_click=reset_waveform_view_state,
        args=(test_id,),
    ):
        pass


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(row, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _segment_bounds(elapsed_seconds, window_seconds):
    if elapsed_seconds is None or elapsed_seconds < 0:
        elapsed_seconds = 0

    segment_start = int(elapsed_seconds // window_seconds) * window_seconds
    return segment_start, segment_start + window_seconds


def _read_measurement_segment(measurement_id, segment_start, segment_end):
    if not measurement_id:
        return [], [], []

    csv_path = get_measurement_csv_path(measurement_id)

    if not csv_path.exists():
        return [], [], []

    times = []
    pressure_values = []
    flow_values = []

    try:
        with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)

            for row in reader:
                elapsed = _safe_float(
                    _first_present(row, "time_s", "elapsed_seconds")
                )

                if elapsed is None:
                    continue

                if elapsed < segment_start:
                    continue

                if elapsed > segment_end:
                    break

                times.append(elapsed)
                pressure_values.append(_safe_float(row.get("pressure_cmH2O")))
                flow_values.append(
                    _safe_float(_first_present(row, "flow_Lps", "flow_lps"))
                )
    except OSError:
        return [], [], []

    return times, pressure_values, flow_values


def _render_plotly_graph(
    go,
    title,
    unit,
    times,
    values,
    color,
    y_range,
    segment_start,
    segment_end,
    cursor_time,
    chart_key,
    excluded_intervals=None,
):
    excluded_intervals = excluded_intervals or []
    cursor_time = max(segment_start, min(cursor_time or 0, segment_end))

    fig = go.Figure()

    fig.add_trace(
        go.Scattergl(
            x=times,
            y=values,
            mode="lines",
            name=title,
            line={
                "color": color,
                "width": 1.6,
            },
            connectgaps=False,
        )
    )

    for interval in excluded_intervals:
        start = interval.get("start")
        end = interval.get("end")

        if start is None or end is None:
            continue

        if end < segment_start or start > segment_end:
            continue

        fig.add_vrect(
            x0=max(start, segment_start),
            x1=min(end, segment_end),
            fillcolor="#fca5a5",
            opacity=0.18,
            line_width=0,
            layer="below",
        )

    fig.add_hline(
        y=0,
        line={
            "color": "#9ca3af",
            "width": 1,
        },
    )

    fig.add_vline(
        x=cursor_time,
        line={
            "color": "#111827",
            "width": 2,
        },
    )

    fig.update_layout(
        height=265,
        margin={
            "l": 54,
            "r": 20,
            "t": 34,
            "b": 40,
        },
        title={
            "text": f"{title} ({unit})",
            "font": {
                "size": 15,
            },
        },
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={
            "color": "#1f2937",
        },
        showlegend=False,
        hovermode="x unified",
        uirevision=chart_key,
        transition={
            "duration": 0,
        },
        xaxis={
            "title": "Time (s)",
            "range": [segment_start, segment_end],
            "dtick": max(5, int((segment_end - segment_start) / 6)),
            "gridcolor": "rgba(148,163,184,0.28)",
            "zeroline": False,
        },
        yaxis={
            "title": unit,
            "range": list(y_range),
            "gridcolor": "rgba(148,163,184,0.28)",
            "zeroline": False,
        },
    )

    st.plotly_chart(
        fig,
        width="stretch",
        config=PLOTLY_CONFIG,
        key=chart_key,
    )


def _render_custom_waveforms(
    times,
    pressure_values,
    flow_values,
    elapsed_seconds,
    time_window_seconds,
    pressure_y_range,
    flow_y_range,
    excluded_intervals,
):
    samples = []

    for elapsed, pressure, flow in zip(times, pressure_values, flow_values):
        samples.append({
            "elapsed_seconds": elapsed,
            "pressure_cmH2O": pressure,
            "flow_lps": flow,
        })

    live_waveforms(
        samples=samples,
        elapsed_seconds=elapsed_seconds,
        recording=st.session_state.recording,
        time_window_seconds=time_window_seconds,
        playback_delay_seconds=DEFAULT_WAVEFORM_PLAYBACK_DELAY_SECONDS,
        pressure_y_range=pressure_y_range,
        flow_y_range=flow_y_range,
        excluded_intervals=excluded_intervals,
        key="live_waveforms",
    )


def _render_plotly_waveforms(
    measurement_id,
    elapsed_seconds,
    time_window_seconds=DEFAULT_TIME_WINDOW_SECONDS,
    pressure_y_range=DEFAULT_PRESSURE_Y_RANGE,
    flow_y_range=DEFAULT_FLOW_Y_RANGE,
    excluded_intervals=None,
):
    try:
        import plotly.graph_objects as go
    except ModuleNotFoundError:
        st.warning(
            "Install Plotly to show live waveform graphs: "
            "`pip install -r requirements.txt`."
        )
        return

    segment_start, segment_end = _segment_bounds(
        elapsed_seconds,
        time_window_seconds,
    )
    times, pressure_values, flow_values = _read_measurement_segment(
        measurement_id,
        segment_start,
        segment_end,
    )

    st.markdown("#### Live Waveforms")

    _render_plotly_graph(
        go,
        "Pressure",
        "cmH2O",
        times,
        pressure_values,
        "#2563eb",
        pressure_y_range,
        segment_start,
        segment_end,
        elapsed_seconds,
        "pressure_waveform",
        excluded_intervals=excluded_intervals,
    )

    _render_plotly_graph(
        go,
        "Flow",
        "L/s",
        times,
        flow_values,
        "#059669",
        flow_y_range,
        segment_start,
        segment_end,
        elapsed_seconds,
        "flow_waveform",
        excluded_intervals=excluded_intervals,
    )


def render_waveform_panel(
    measurement_id,
    elapsed_seconds,
    time_window_seconds=DEFAULT_TIME_WINDOW_SECONDS,
    pressure_y_range=DEFAULT_PRESSURE_Y_RANGE,
    flow_y_range=DEFAULT_FLOW_Y_RANGE,
    excluded_intervals=None,
):
    display_elapsed_seconds = max(
        0,
        elapsed_seconds - DEFAULT_WAVEFORM_PLAYBACK_DELAY_SECONDS,
    )
    segment_start, segment_end = _segment_bounds(
        display_elapsed_seconds,
        time_window_seconds,
    )
    times, pressure_values, flow_values = _read_measurement_segment(
        measurement_id,
        segment_start,
        segment_end,
    )

    try:
        _render_custom_waveforms(
            times,
            pressure_values,
            flow_values,
            elapsed_seconds,
            time_window_seconds,
            pressure_y_range,
            flow_y_range,
            excluded_intervals or [],
        )
    except Exception as error:
        st.caption(f"Custom waveform panel unavailable: {error}")
        _render_plotly_waveforms(
            measurement_id,
            elapsed_seconds,
            time_window_seconds,
            pressure_y_range,
            flow_y_range,
            excluded_intervals,
        )
