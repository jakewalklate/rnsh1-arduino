"""Daily system check tab — start-of-day accuracy verification.

Measures a known reference load at two different CPAP levels and reports whether the
system is ready. This VERIFIES the calibration in force; it never changes it. A
failure sends the operator to the Calibrate Device tab.
"""

import pandas as pd
import streamlit as st

from backend.processing.FOTSystemCheck import (
    CHECK_LOAD_PRESETS,
    CHECK_POINTS_REQUIRED,
    CHECK_R_TOLERANCE_FRACTION,
    CHECK_X_ABS_TOLERANCE,
    seconds_for_check,
)
from frontend.components.ui import card, card_header, hero, pill, steps
from frontend.services.recording_service import get_recording_service
from frontend.views.utilities.calibration import (
    _render_live_trace,
    _render_port_selector,
    get_calibration_service,
    release_other_device_owners,
)


def _render_verdict(service):
    ready, message = service.check_verdict()
    if ready is True:
        st.success(message)
    elif ready is False:
        st.error(message)
    else:
        st.info(message)


def _render_results(service):
    if not service.check_points:
        return

    card_header("Results")
    frame = pd.DataFrame([
        {
            "#": p["measurement"],
            "CPAP": p["cpap_note"] or "-",
            "Reference": p["reference_r"],
            "Resistance": round(p["R"], 3),
            "Error %": round(p["r_error_pct"], 1),
            "Reactance": round(p["X"], 3),
            "Bias flow": round(p["bias_flow_lps"], 3),
            "Result": "PASS" if p["point_pass"] else "FAIL",
        }
        for p in service.check_points
    ])
    st.dataframe(frame, width="stretch", hide_index=True)

    for p in service.check_points:
        if not p["point_pass"]:
            reasons = []
            if not p["r_pass"]:
                reasons.append(
                    f"resistance out by {p['r_error_pct']:+.1f}% "
                    f"(limit ±{CHECK_R_TOLERANCE_FRACTION * 100:g}%)"
                )
            if not p["x_pass"]:
                reasons.append(
                    f"reactance {p['X']:+.2f} outside ±{CHECK_X_ABS_TOLERANCE:g}"
                )
            if not p["qc_valid"]:
                reasons.append(f"signal quality: {p['qc_reason']}")
            st.caption(f"Measurement {p['measurement']} failed — " + "; ".join(reasons))

    if service.check_log_path:
        st.caption(f"Saved to {service.check_log_path.name}")


@st.fragment(run_every=1)
def _render_check_progress(service):
    status = service.capture_status("check")
    if status is None:
        return

    if status["state"] in ("settling", "collecting"):
        _render_live_progress(service, status)
        return
    _render_finished(service, status)


def _render_live_progress(service, status):
    with card("check-progress", "accent"):
        card_header("Measuring…")
        progress_col, trace_col = st.columns([1, 1], gap="medium")
        with progress_col:
            st.progress(status["progress"], text=status["message"])
            live_cols = st.columns(3)
            live_cols[0].metric(
                "Resistance",
                "-" if status["live_r"] is None else f"{status['live_r']:.2f}",
            )
            live_cols[1].metric(
                "Reactance",
                "-" if status["live_x"] is None else f"{status['live_x']:.2f}",
            )
            live_cols[2].metric(
                "Bias flow",
                "-" if status["live_bias_flow"] is None
                else f"{status['live_bias_flow']:.2f}",
            )
            st.caption(
                "Live readout with the current calibration applied — the same basis "
                "the result is scored on."
            )
            if status["dropped_blocks"]:
                st.caption(f"{status['dropped_blocks']} block(s) discarded and re-taken.")
            if st.button("Cancel measurement", key="chk_cancel"):
                service.cancel_capture()
                st.rerun(scope="app")
        with trace_col:
            _render_live_trace(status, key_prefix="chk")


def _render_finished(service, status):
    if status["state"] == "error":
        st.error(status["message"])
        if st.button("Dismiss", key="chk_dismiss"):
            service.cancel_capture()
            st.rerun(scope="app")
        return

    if status["state"] == "done":
        service.commit_check_measurement()
        st.rerun(scope="app")


def _verdict_pill(service):
    ready, _ = service.check_verdict()
    if ready is True:
        return pill("Passed today", "pass")
    if ready is False:
        return pill("Failed", "fail")
    return pill("Not run yet", "neutral")


def system_check_tab():
    service = get_calibration_service()

    if not service.hardware_enabled:
        st.warning("Device access is switched off in the configuration file.")
        return

    busy_reason = None
    try:
        if get_recording_service().is_recording:
            busy_reason = "A patient recording is in progress. Stop it before checking."
    except Exception:
        pass

    status = service.capture_status("check")
    capturing = bool(status and status["state"] in ("settling", "collecting"))
    if capturing:
        busy_reason = "A measurement is already running."

    # Resolve the port here so the measurement controls can be enabled or
    # disabled, but render the device status section at the bottom of the page.
    port = _render_port_selector(
        service, disabled=capturing, key_prefix="chk", render=False
    )

    hero(
        "Daily System Check",
        "Run this once at the start of each day. It measures the test object at two "
        "different CPAP pressures and checks the device reads it correctly.",
        pills=(
            pill(f"Device on {port}", "pass") if port else pill("No device found", "fail"),
            _verdict_pill(service),
        ),
    )

    left, right = st.columns([5, 4], gap="small")
    with right:
        with card("check-howto"):
            card_header("How to run it")
            steps([
                "Fit the test object in place of the patient.",
                "Set a low CPAP pressure and let it settle.",
                f"Enter the values and press Run measurement (about "
                f"{seconds_for_check()} seconds).",
                "Change to a clearly different CPAP pressure and run it again.",
                "If it fails, calibrate the device, then repeat the check.",
            ])

    with left:
        with card("check-measure", "accent"):
            card_header(
                "Measurement",
                "Two measurements at clearly different pressures. Passes if resistance "
                "is within ±10% of the test object and reactance within ±1.",
            )
            setup_cols = st.columns(2)
            with setup_cols[0]:
                reference_r = st.number_input(
                    "Reference load",
                    min_value=0.1,
                    max_value=100.0,
                    value=float(CHECK_LOAD_PRESETS[0]),
                    step=0.05,
                    key="chk_reference_r",
                    help="Enter the known resistance printed on the reference load, "
                         "in cmH2O*s/L.",
                )
            with setup_cols[1]:
                cpap_note = st.text_input(
                    "CPAP pressure",
                    key="chk_cpap_note",
                    placeholder="e.g. 6",
                    help="Recorded with the result. The two measurements must use "
                         "different pressures.",
                )

            if busy_reason:
                st.warning(busy_reason)
            elif port is None:
                st.info("Connect the device to start. See Device connection below.")

            action_cols = st.columns(2)
            with action_cols[0]:
                run = st.button(
                    "Run measurement",
                    type="primary",
                    width="stretch",
                    disabled=bool(busy_reason) or not port,
                )
            with action_cols[1]:
                if st.button("Reset check", width="stretch", disabled=capturing):
                    service.reset_check()
                    st.rerun()

            if run:
                try:
                    release_other_device_owners()
                    service.start_check_measurement(
                        reference_r=reference_r,
                        cpap_note=cpap_note.strip(),
                        port=port,
                    )
                except Exception as error:
                    st.error(f"Could not start measurement on {port}: {error}")
                else:
                    st.rerun()

    # See the note in calibration.py: mount the 1 Hz fragment only while this tab
    # owns a running capture.
    if status is not None:
        _render_check_progress(service)

    with card("check-results", "accent" if service.check_points else ""):
        if service.check_points:
            _render_results(service)
        else:
            card_header("Results")
        _render_verdict(service)

    with card("check-device", "muted"):
        card_header("Device connection")
        _render_port_selector(service, disabled=capturing, key_prefix="chk")
