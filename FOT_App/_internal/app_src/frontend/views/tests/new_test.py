import streamlit as st

from backend.database.storage import get_patient, get_test
from frontend.components.ui import card
from frontend.navigation import goto
from frontend.views.tests.recording_dialogs import (
    configure_test_parameters_dialog,
    confirm_start_dialog,
    invalid_stop_dialog,
    stop_summary_dialog,
)
from frontend.views.tests.recording_layout import (
    render_recording_page_styles,
    render_test_header,
)
from frontend.views.tests.recording_state import (
    can_save_recording,
    current_elapsed_seconds,
    format_duration,
    initialise_test_page_state,
    remaining_minimum_seconds,
    stop_recording,
    test_param_key,
)
from frontend.views.tests.waveform_panel import render_waveform_panel
from frontend.views.tests.waveform_panel import get_waveform_view_settings


@st.fragment(run_every=1)
def render_recording_runtime(test_id, test_status, max_duration_seconds):
    elapsed = current_elapsed_seconds()

    if (
        st.session_state.recording
        and max_duration_seconds is not None
        and elapsed >= max_duration_seconds
    ):
        stop_recording()

    if st.session_state.recording:
        if can_save_recording(elapsed):
            timing_message = "This recording is valid and can be stopped when ready."
        else:
            timing_message = (
                "Minimum valid recording time remaining: "
                f"{format_duration(remaining_minimum_seconds(elapsed))}."
            )

        st.info(
            f"Recording in progress. Elapsed time: {format_duration(elapsed)}. "
            f"{timing_message}"
        )
    else:
        st.info("Waveforms will update when a recording is active.")

    waveform_settings = get_waveform_view_settings(test_id)

    render_waveform_panel(
        st.session_state.current_measurement_id,
        elapsed,
        time_window_seconds=waveform_settings["time_window_seconds"],
        pressure_y_range=waveform_settings["pressure_y_range"],
        flow_y_range=waveform_settings["flow_y_range"],
    )


def render_active_dialogs():
    if st.session_state.show_start_dialog:
        confirm_start_dialog()

    if st.session_state.show_config_dialog:
        configure_test_parameters_dialog()

    if st.session_state.show_stop_summary:
        stop_summary_dialog()

    if st.session_state.get("show_invalid_stop_dialog"):
        invalid_stop_dialog()


def render_missing_state(message, back_page):
    st.error(message)
    if st.button("Back"):
        goto(back_page)


def new_test_page():
    patient = get_patient(st.session_state.selected_patient_id)
    test = get_test(st.session_state.current_test_id)

    if patient is None:
        render_missing_state("No patient selected.", "home")
        return

    if test is None:
        render_missing_state("No test selected.", "patient_view")
        return

    patient_name = f"{patient[1]} {patient[2]}"
    test_id = test[0]
    test_status = test[8]

    initialise_test_page_state(test_id)

    max_duration_seconds = st.session_state[test_param_key(test_id, "max_duration_seconds")]

    render_active_dialogs()
    render_recording_page_styles()
    with card("recording-header", "accent"):
        render_test_header(
            test_id,
            patient_name,
            test_status,
            current_elapsed_seconds(),
        )

    with card("waveforms"):
        if st.session_state.show_start_dialog or st.session_state.show_config_dialog:
            st.info("Waveforms will update when a recording is active.")
        else:
            render_recording_runtime(test_id, test_status, max_duration_seconds)

    if st.session_state.recording or st.session_state.show_stop_summary:
        st.caption("Stop and save or discard the measurement before leaving this test.")
    elif st.button("Back to Patient", width="stretch"):
        goto("patient_view")
