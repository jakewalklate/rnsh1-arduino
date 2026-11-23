import time
from datetime import datetime

import streamlit as st

from backend.database.storage import (
    complete_measurement,
    create_measurement,
    discard_measurement,
    get_test_parameters,
    save_measurement_results,
    save_scale_only_results,
    update_test_status,
)
from backend.processing.FOTPipeline import process_measurement_csv
from frontend.navigation import goto, persist_state_to_url
from frontend.services.recording_service import get_recording_service
from frontend.views.tests.recording_state import (
    MAX_DURATION_OPTIONS,
    MIN_CUSTOM_DURATION_MINUTES,
    MIN_RECORDING_SECONDS,
    discard_empty_test_if_needed,
    discard_invalid_recording_to_patient,
    event_marker_key,
    format_duration,
    max_duration_display,
    prepare_config_dialog_state,
    resume_invalid_stop_recording,
    save_config_dialog_state,
    test_param_key,
    OTHER_TIMER_LABEL,
)


@st.dialog("Configure Test Parameters", dismissible=False)
def configure_test_parameters_dialog():
    test_id = st.session_state.current_test_id

    st.number_input(
        "CPAP Pressure (cmH2O)",
        # 0.0 not 0: Streamlit requires every numeric argument to be the SAME type, and
        # max_value/step are floats. An int here raises StreamlitMixedNumericTypesError,
        # whose message unhelpfully blames `value` (that is just the internal "min"
        # sentinel, since the value comes from session_state via `key`).
        min_value=0.0,
        max_value=20.0,
        step=0.5,
        key=test_param_key(test_id, "config_cpap_pressure_cmH2O")
    )

    st.selectbox(
        "Recording Timer",
        list(MAX_DURATION_OPTIONS.keys()),
        key=test_param_key(test_id, "config_max_duration_label")
    )

    if st.session_state[test_param_key(test_id, "config_max_duration_label")] == OTHER_TIMER_LABEL:
        st.number_input(
            "Custom Timer Duration (minutes)",
            min_value=MIN_CUSTOM_DURATION_MINUTES,
            max_value=60,
            step=1,
            key=test_param_key(test_id, "config_custom_duration_minutes"),
        )


    col1, col2 = st.columns(2)

    with col1:
        if st.button(
            "Save Parameters",
            type="primary",
            width="stretch"
        ):
            save_config_dialog_state(test_id)
            st.session_state.show_config_dialog = False
            st.session_state.show_start_dialog = True
            persist_state_to_url()
            st.rerun()

    with col2:
        if st.button(
            "Back",
            width="stretch"
        ):
            st.session_state.show_config_dialog = False
            st.session_state.show_start_dialog = True
            persist_state_to_url()
            st.rerun()


@st.dialog("Confirm Test Start", dismissible=False)
def confirm_start_dialog():
    parameters = get_test_parameters(st.session_state.current_test_id)

    st.write(
        "Please ensure the mask is fitted properly to the patient before beginning the test."
    )

    st.write(f"**CPAP Pressure:** {parameters['cpap_pressure_cmH2O']:.1f} cmH2O")
    st.caption(
        f"Recordings shorter than {MIN_RECORDING_SECONDS} seconds are invalid. "
        "If stopped early, the measurement must be discarded."
    )

    st.write(f"**Recording Timer:** {max_duration_display(parameters['max_duration_seconds'])}")

    col1, col2 = st.columns(2)

    with col1:
        if st.button(
            "Start Recording",
            type="primary",
            width="stretch"
        ):
            # Utility pages intentionally retain their healthy idle calibration
            # connection so moving between System Check and Device Calibration does
            # not cycle the Uno R4 USB port. Hand that connection back immediately
            # before the patient recorder takes ownership.
            from frontend.views.utilities.device_utilities import release_device_connections

            release_device_connections()
            measurement_id = create_measurement(
                st.session_state.current_test_id,
                cpap_pressure_cmH2O=parameters["cpap_pressure_cmH2O"],
            )

            try:
                get_recording_service().start_measurement(
                    measurement_id,
                    cpap_pressure_cmH2O=parameters["cpap_pressure_cmH2O"],
                )
            except Exception as error:
                discard_measurement(measurement_id)
                st.error(f"Could not start Arduino recording: {error}")
                return

            st.session_state.delete_empty_test_on_cancel = False
            st.session_state[event_marker_key(st.session_state.current_test_id)] = []
            st.session_state.current_measurement_id = measurement_id
            st.session_state.recording = True
            st.session_state.recording_start_time = time.time()
            st.session_state.recording_start_datetime = datetime.now()
            st.session_state.show_start_dialog = False
            st.session_state.show_config_dialog = False
            st.session_state.show_invalid_stop_dialog = False

            update_test_status(
                st.session_state.current_test_id,
                "Recording"
            )

            persist_state_to_url()
            st.rerun()

    with col2:
        if st.button(
            "Configure Test Parameters",
            width="stretch"
        ):
            prepare_config_dialog_state(st.session_state.current_test_id)
            st.session_state.show_start_dialog = False
            st.session_state.show_config_dialog = True
            persist_state_to_url()
            st.rerun()

    if st.button(
        "Cancel",
        width="stretch"
    ):
        discard_empty_test_if_needed(st.session_state.current_test_id)
        st.session_state.delete_empty_test_on_cancel = False
        st.session_state.show_start_dialog = False
        st.session_state.recording = False
        goto("patient_view")


@st.dialog("Invalid Measurement", dismissible=False)
def invalid_stop_dialog():
    st.warning(
        f"Stopping the recording before {MIN_RECORDING_SECONDS} seconds will create "
        "an invalid measurement. "
        "You can resume the recording, or exit and discard this measurement."
    )

    elapsed = st.session_state.recording_duration
    if elapsed is None:
        elapsed = time.time() - st.session_state.recording_start_time

    st.write(f"**Elapsed Time:** {format_duration(elapsed)}")

    col1, col2 = st.columns(2)

    with col1:
        if st.button(
            "Resume Recording",
            type="primary",
            width="stretch",
        ):
            resume_invalid_stop_recording()

    with col2:
        if st.button(
            "Exit and Discard",
            width="stretch",
        ):
            discard_invalid_recording_to_patient()


@st.dialog("Recording Summary", dismissible=False)
def stop_summary_dialog():
    start_time = st.session_state.recording_start_datetime
    end_time = st.session_state.recording_end_time
    duration = st.session_state.recording_duration

    st.write("Measurement recording has stopped.")

    st.write(f"**Start Time:** {start_time.strftime('%H:%M:%S | %d/%m/%Y ')}")
    st.write(f"**End Time:** {end_time.strftime('%H:%M:%S | %d/%m/%Y ')}")
    st.write(f"**Duration:** {format_duration(duration)}")

    col1, col2 = st.columns(2)

    with col1:
        if st.button(
            "Save Recording",
            type="primary",
            width="stretch"
        ):
            saved_measurement_id = st.session_state.current_measurement_id

            complete_measurement(
                saved_measurement_id,
                duration,
                end_time
            )

            # Processed twice on purpose, once per calibration basis, so the results
            # screen can switch between them without recomputing against whatever
            # calibration happens to be loaded at viewing time. Roughly doubles the
            # save-time processing, which is a second or two for a normal take.
            results = process_measurement_csv(
                saved_measurement_id
            )
            save_measurement_results(
                saved_measurement_id,
                results
            )
            save_scale_only_results(
                saved_measurement_id,
                process_measurement_csv(
                    saved_measurement_id,
                    apply_bias_polynomial=False,
                ),
            )

            st.session_state.show_stop_summary = False
            st.session_state[event_marker_key(st.session_state.current_test_id)] = []
            st.session_state.selected_results_measurement_id = saved_measurement_id
            st.session_state.current_measurement_id = None
            st.session_state.recording_start_time = None
            st.session_state.recording_start_datetime = None
            st.session_state.recording_end_time = None
            st.session_state.recording_duration = None
            st.session_state.show_invalid_stop_dialog = False

            goto("test_view")

    with col2:
        if st.button(
            "Discard and Re-record",
            width="stretch"
        ):
            discard_measurement(
                st.session_state.current_measurement_id
            )

            st.session_state.show_stop_summary = False
            st.session_state.current_measurement_id = None
            st.session_state.recording_start_time = None
            st.session_state.recording_start_datetime = None
            st.session_state.recording_end_time = None
            st.session_state.recording_duration = None
            st.session_state.show_start_dialog = True
            st.session_state.show_invalid_stop_dialog = False

            update_test_status(
                st.session_state.current_test_id,
                "Waiting"
            )

            persist_state_to_url()
            st.rerun()
