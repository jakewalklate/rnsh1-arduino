"""Flow calibration (syringe) tab.

A Hans Rudolph 3 L syringe is pushed and pulled through the pneumotach. Each stroke
moves a known volume, so integrating the differential pressure across it gives the
gain that converts pressure to flow. Ten strokes agreeing within 3% is the
calibration.

Push and pull are reported separately because the mesh is not symmetric, and both
are saved. The result takes effect immediately for every measurement the app makes;
the Arduino is never reflashed.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backend.processing.FlowGainCalibration import (
    CONSISTENCY_TOLERANCE_PCT,
    SYRINGE_VOLUME_L,
    TARGET_STROKES,
)
from backend.services.flow_calibration_service import FlowCalibrationService
from frontend.components.ui import card, card_header, hero, pill, steps
from frontend.services.recording_service import get_recording_service
from frontend.views.utilities.calibration import (
    _render_port_selector,
    get_calibration_service,
)


@st.cache_resource
def get_flow_calibration_service():
    return FlowCalibrationService()


def _port_error_text(error, port):
    """pyserial's PermissionError says only 'Access is denied', which gives the
    operator nothing to act on. Name the things that actually hold a COM port."""
    text = str(error)
    if "Access is denied" in text or "PermissionError" in text:
        return (
            f"{port} is already open in another program. Close the Arduino IDE "
            f"Serial Monitor, the bench PressureSensorReading.py, or any other app "
            f"using the device, then try again.\n\n{text}"
        )
    return f"Could not start on {port}: {text}"


def _render_saved_calibration(service):
    saved = service.saved_calibration()
    push, pull = service.active_gains()
    firmware_push, firmware_pull = service.firmware_gains()

    with card("flow-gains"):
        card_header(
            "Flow gains in use",
            "L/(s·cmH₂O), applied to every flow sample the app reads.",
        )
        cols = st.columns(2)
        cols[0].metric("Push (dp ≥ 0)", f"{push:.6f}")
        cols[1].metric("Pull (dp < 0)", f"{pull:.6f}")

        if not saved:
            st.info(
                "No syringe calibration has been saved yet. The app is currently "
                f"using the firmware defaults ({firmware_push:.4f} push, "
                f"{firmware_pull:.4f} pull)."
            )
            return

        

        # The saved gains are a multiplier ON TOP of whatever the firmware applied.
        # If the .ino constants change, they no longer mean what they meant.
        stale = (
            abs(saved.get("firmware_flow_gain_pos", firmware_push) - firmware_push) > 1e-9
            or abs(saved.get("firmware_flow_gain_neg", firmware_pull) - firmware_pull) > 1e-9
        )
        if stale:
            st.error(
                "This calibration was derived against different firmware gains "
                f"({saved.get('firmware_flow_gain_pos')} / "
                f"{saved.get('firmware_flow_gain_neg')}) than the app now assumes "
                f"({firmware_push} / {firmware_pull}). Flow readings are wrong until "
                "you re-run the syringe calibration."
            )


def _render_trace(snapshot):
    trace = snapshot.get("trace") or []
    if not trace:
        st.caption("Waiting for the first samples…")
        return

    figure = go.Figure(go.Scatter(y=trace, mode="lines", line={"width": 1.4}))
    figure.update_layout(
        height=240,
        margin={"l": 10, "r": 10, "t": 24, "b": 10},
        title="A2 differential pressure (cmH₂O)",
        showlegend=False,
    )
    figure.add_hline(y=0.0, line_width=1, line_color="grey")
    st.plotly_chart(figure, width="stretch", key="flowcal_trace")


@st.fragment(run_every=0.5)
def _render_session(service):
    snapshot = service.snapshot()
    if snapshot is None:
        return

    if snapshot["state"] == "error":
        st.error(snapshot["message"])
        return

    header = st.columns(3)
    header[0].metric("A2 now", f"{snapshot['latest_dp']:+.4f}")
    header[1].metric(
        "Strokes", f"{len(service.strokes)}/{TARGET_STROKES}"
    )
    header[2].metric(
        "State", "RECORDING" if snapshot["recording"] else "READY"
    )

    _render_trace(snapshot)

    if snapshot["recording"]:
        st.info(
            f"{snapshot['message']}  ·  {snapshot['stroke_duration_s']:.2f} s recorded"
        )
        action = st.columns(2)
        if action[0].button("Finish stroke", width="stretch", type="primary",
                            key="flowcal_finish"):
            ok, message = service.finish_stroke()
            (st.success if ok else st.warning)(message)
            # scope="app": an accepted stroke changes the table and the medians, which
            # are rendered OUTSIDE this fragment. A default fragment-scoped rerun
            # would refresh the buttons and leave the results stale.
            st.rerun(scope="app")
        if action[1].button("Discard", width="stretch", key="flowcal_discard"):
            service.discard_stroke()
            st.rerun()
        return

    if not snapshot["baseline_ready"]:
        st.warning(
            "Hold the syringe still — a full second of no-flow baseline is needed "
            "before a stroke can start."
        )

    start = st.columns(2)
    disabled = not snapshot["baseline_ready"]
    if start[0].button(f"Start +{SYRINGE_VOLUME_L:g} L (push)", width="stretch",
                       disabled=disabled, key="flowcal_push"):
        ok, message = service.begin_stroke(+1)
        if not ok:
            st.warning(message)
        st.rerun()
    if start[1].button(f"Start −{SYRINGE_VOLUME_L:g} L (pull)", width="stretch",
                       disabled=disabled, key="flowcal_pull"):
        ok, message = service.begin_stroke(-1)
        if not ok:
            st.warning(message)
        st.rerun()

    st.caption(snapshot["message"])


def _render_strokes(service):
    strokes = service.strokes
    if not strokes:
        return

    summary = service.summary()
    with card("flow-strokes", "accent"):
        card_header(
            f"Accepted strokes: {len(strokes)} of {TARGET_STROKES}",
            "Each stroke gives one gain. They should agree closely before you save.",
        )

        frame = pd.DataFrame([
            {
                "#": s["number"],
                "Direction": s["direction"],
                "Active s": round(s["active_duration_s"], 2),
                "Zero": round(s["baseline_cmH2O"], 4),
                "∫dp": round(s["dp_integral_cmH2O_s"], 4),
                "Gain": round(s["gain_Lps_per_cmH2O"], 6),
                "Dev %": round(deviation, 2),
            }
            for s, deviation in zip(strokes, summary["deviations"])
        ])
        st.dataframe(frame, width="stretch", hide_index=True)

        cols = st.columns(4)
        cols[0].metric("Median gain", f"{summary['median_gain']:.6f}")
        cols[1].metric(
            "Push", "—" if summary["push_gain"] is None else f"{summary['push_gain']:.6f}"
        )
        cols[2].metric(
            "Pull", "—" if summary["pull_gain"] is None else f"{summary['pull_gain']:.6f}"
        )
        cols[3].metric("Worst spread", f"{summary['worst_deviation_pct']:.2f}%")

        if summary["consistency_pass"]:
            st.success(service.status_text())
        elif summary["complete"]:
            st.warning(service.status_text())
        else:
            st.info(service.status_text())

        manage = st.columns([2, 1, 1])
        with manage[0]:
            remove = st.number_input(
                "Remove stroke #", min_value=1, max_value=len(strokes), value=len(strokes),
                step=1, key="flowcal_remove_number",
            )
        with manage[1]:
            st.write("")
            if st.button("Remove", width="stretch", key="flowcal_remove"):
                service.remove_stroke(int(remove))
                st.rerun()
        with manage[2]:
            st.write("")
            if st.button("Clear all", width="stretch", key="flowcal_clear"):
                service.clear_strokes()
                st.rerun()

        notes = st.text_input(
            "Notes (optional)", key="flowcal_notes",
            placeholder="e.g. new flowhead, syringe serial, who ran it",
        )

        one_sided = summary["push_gain"] is None or summary["pull_gain"] is None
        if one_sided:
            st.warning(
                "Record at least one push AND one pull stroke before saving — a "
                "one-directional calibration would leave the other direction on the "
                "firmware default."
            )
        elif not summary["consistency_pass"]:
            st.caption(
                f"You can still save, but the strokes disagree by more than "
                f"{CONSISTENCY_TOLERANCE_PCT:g}% or fewer than {TARGET_STROKES} were "
                f"recorded. Saving applies these gains to every measurement immediately."
            )

        if st.button("Save and apply flow gains", type="primary", disabled=one_sided,
                     key="flowcal_save"):
            try:
                path, payload = service.save(notes=notes)
            except ValueError as error:
                st.error(str(error))
            else:
                st.success(
                    f"Saved to {path.name}. Push {payload['flow_gain_pos']:.6f}, "
                    f"pull {payload['flow_gain_neg']:.6f} — applied from the next "
                    f"sample onward, no restart needed."
                )
                st.rerun()


def flow_calibration_tab():
    service = get_flow_calibration_service()

    recording_service = get_recording_service()
    busy_reason = None
    try:
        if recording_service.is_recording:
            busy_reason = "A patient recording is in progress."
    except Exception:
        pass

    # Resolve the port for the start button, but show the device status section
    # with the other page status information at the bottom.
    port = _render_port_selector(
        service,
        disabled=bool(busy_reason),
        key_prefix="flowcal",
        render=False,
    )

    hero(
        "Flow Calibration",
        f"Push and pull a {SYRINGE_VOLUME_L:g} L syringe through the pneumotach "
        f"{TARGET_STROKES} times. Each stroke moves a known volume, which gives the "
        "pressure-to-flow gain.",
        pills=(
            pill(f"Device on {port}", "pass") if port else pill("No device found", "fail"),
            pill("Session active", "info") if service.is_active
            else pill("Saved calibration", "pass") if service.saved_calibration()
            else pill("Using firmware defaults", "neutral"),
        ),
    )

    left, right = st.columns([5, 4], gap="small")
    with right:
        _render_saved_calibration(service)

    with left:
        if not service.is_active:
            with card("flow-start", "accent"):
                card_header("Start a session")
                steps([
                    "Remove the patient circuit and fit the syringe.",
                    "Press Start. The speaker goes quiet and raw pressure is shown.",
                    f"Do {TARGET_STROKES} full strokes, alternating push and pull.",
                ])
                if busy_reason:
                    st.warning(busy_reason)
                if st.button("Start calibration session", type="primary",
                             width="stretch",
                             disabled=bool(busy_reason) or port is None,
                             key="flowcal_start"):
                    try:
                        # The other calibration tab holds its own connection to the
                        # same port, and only one program can have it open.
                        get_calibration_service().release_device()
                        service.start_session(port)
                    except Exception as error:
                        st.error(_port_error_text(error, port))
                    else:
                        st.rerun()
                if port is None and not busy_reason:
                    st.info("Connect the device to start. See Device connection below.")
        else:
            with card("flow-session", "accent"):
                card_header(
                    "Calibration session",
                    "Speaker silenced, streaming raw A2 differential pressure.",
                )
                _render_session(service)
                if st.button("Finish session and restore normal mode",
                             key="flowcal_end"):
                    service.end_session()
                    st.rerun()

    if service.is_active:
        _render_strokes(service)

    with card("flow-device", "muted"):
        card_header("Device connection")
        _render_port_selector(
            service,
            disabled=bool(busy_reason) or service.is_active,
            key_prefix="flowcal",
        )
