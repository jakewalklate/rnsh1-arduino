import time
from datetime import datetime

import streamlit as st

from backend.database.storage import (
    delete_test,
    delete_test_if_no_complete_measurements,
    discard_measurement,
    get_test_measurements,
    get_test_parameters,
    save_test_parameters,
    update_test_status,
)
from frontend.navigation import goto, persist_state_to_url
from frontend.services.recording_service import get_recording_service


MIN_RECORDING_SECONDS = 60
MIN_CUSTOM_DURATION_MINUTES = max(1, (MIN_RECORDING_SECONDS + 59) // 60)
MANUAL_STOP_LABEL = "Manual stop"
OTHER_TIMER_LABEL = "Other"

MAX_DURATION_OPTIONS = {
    MANUAL_STOP_LABEL: None,
    "3 min": 180,
    "5 min": 300,
    "10 min": 600,
    OTHER_TIMER_LABEL: None,
}


def test_param_key(test_id, name):
    return f"{name}_{test_id}"


def event_marker_key(test_id):
    return f"event_markers_{test_id}"


def max_duration_label(max_duration_seconds):
    max_duration_seconds = normalise_max_duration_seconds(max_duration_seconds)

    if max_duration_seconds is None:
        return MANUAL_STOP_LABEL

    for label, value in MAX_DURATION_OPTIONS.items():
        if label != OTHER_TIMER_LABEL and value == max_duration_seconds:
            return label

    return OTHER_TIMER_LABEL


def max_duration_display(max_duration_seconds):
    label = max_duration_label(max_duration_seconds)

    if label == OTHER_TIMER_LABEL:
        return f"Other ({format_duration(max_duration_seconds)})"

    return label


def normalise_max_duration_seconds(max_duration_seconds):
    if max_duration_seconds is None:
        return None

    if max_duration_seconds < MIN_RECORDING_SECONDS:
        return None

    return int(max_duration_seconds)


def format_duration(seconds):
    if seconds is None:
        seconds = 0

    total_seconds = int(seconds)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def current_elapsed_seconds():
    if not st.session_state.recording or st.session_state.recording_start_time is None:
        return st.session_state.recording_duration or 0

    return time.time() - st.session_state.recording_start_time


def can_save_recording(elapsed_seconds):
    return elapsed_seconds >= MIN_RECORDING_SECONDS


def remaining_minimum_seconds(elapsed_seconds):
    return max(0, MIN_RECORDING_SECONDS - int(elapsed_seconds))


def initialise_test_page_state(test_id):
    parameters = get_test_parameters(test_id)
    max_duration_seconds = normalise_max_duration_seconds(
        parameters["max_duration_seconds"]
    )
    custom_duration_minutes = (
        max(MIN_CUSTOM_DURATION_MINUTES, int(max_duration_seconds / 60))
        if max_duration_seconds is not None
        else MIN_CUSTOM_DURATION_MINUTES
    )

    defaults = {
        test_param_key(test_id, "cpap_pressure_cmH2O"): parameters["cpap_pressure_cmH2O"],
        test_param_key(test_id, "max_duration_seconds"): max_duration_seconds,
        test_param_key(test_id, "config_cpap_pressure_cmH2O"): parameters["cpap_pressure_cmH2O"],
        test_param_key(test_id, "config_max_duration_label"): max_duration_label(
            max_duration_seconds
        ),
        test_param_key(test_id, "config_custom_duration_minutes"): custom_duration_minutes,
        test_param_key(test_id, "excitation_hz"): parameters["excitation_hz"],
        test_param_key(test_id, "config_excitation_hz"): parameters["excitation_hz"],
        event_marker_key(test_id): [],
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def prepare_config_dialog_state(test_id):
    parameters = get_test_parameters(test_id)
    max_duration_seconds = normalise_max_duration_seconds(
        parameters["max_duration_seconds"]
    )

    st.session_state[test_param_key(test_id, "config_cpap_pressure_cmH2O")] = parameters[
        "cpap_pressure_cmH2O"
    ]
    st.session_state[test_param_key(test_id, "config_max_duration_label")] = max_duration_label(
        max_duration_seconds
    )
    st.session_state[test_param_key(test_id, "config_custom_duration_minutes")] = (
        max(MIN_CUSTOM_DURATION_MINUTES, int(max_duration_seconds / 60))
        if max_duration_seconds is not None
        else MIN_CUSTOM_DURATION_MINUTES
    )
    st.session_state[test_param_key(test_id, "config_excitation_hz")] = parameters[
        "excitation_hz"
    ]


def save_config_dialog_state(test_id):
    cpap_pressure = st.session_state[test_param_key(test_id, "config_cpap_pressure_cmH2O")]
    max_duration_label_value = st.session_state[
        test_param_key(test_id, "config_max_duration_label")
    ]

    if max_duration_label_value == OTHER_TIMER_LABEL:
        custom_duration_minutes = st.session_state[
            test_param_key(test_id, "config_custom_duration_minutes")
        ]
        max_duration_seconds = int(custom_duration_minutes) * 60
    else:
        max_duration_seconds = MAX_DURATION_OPTIONS[max_duration_label_value]

    excitation_hz = float(
        st.session_state[test_param_key(test_id, "config_excitation_hz")]
    )

    save_test_parameters(
        test_id,
        cpap_pressure,
        max_duration_seconds,
        excitation_hz,
    )

    st.session_state[test_param_key(test_id, "cpap_pressure_cmH2O")] = cpap_pressure
    st.session_state[test_param_key(test_id, "max_duration_seconds")] = max_duration_seconds
    st.session_state[test_param_key(test_id, "excitation_hz")] = excitation_hz


def should_delete_test_on_start_cancel(test_id):
    if not test_id:
        return False

    if st.session_state.get("delete_empty_test_on_cancel"):
        return True

    return len(get_test_measurements(test_id)) == 0


def discard_empty_test_if_needed(test_id):
    if not should_delete_test_on_start_cancel(test_id):
        return

    deleted_patient_id = delete_test(test_id)
    st.session_state.selected_patient_id = (
        deleted_patient_id or st.session_state.selected_patient_id
    )
    st.session_state.current_test_id = None
    st.session_state.current_measurement_id = None


def clear_recording_session_state():
    st.session_state.recording = False
    st.session_state.current_measurement_id = None
    st.session_state.recording_excitation_hz = None
    st.session_state.recording_start_time = None
    st.session_state.recording_start_datetime = None
    st.session_state.recording_end_time = None
    st.session_state.recording_duration = None
    st.session_state.show_start_dialog = False
    st.session_state.show_config_dialog = False
    st.session_state.show_stop_summary = False
    st.session_state.show_invalid_stop_dialog = False


def request_stop_recording():
    elapsed_seconds = current_elapsed_seconds()

    if can_save_recording(elapsed_seconds):
        stop_recording()
        return

    st.session_state.show_invalid_stop_dialog = True
    persist_state_to_url()
    st.rerun()


def resume_invalid_stop_recording():
    st.session_state.show_invalid_stop_dialog = False
    persist_state_to_url()
    st.rerun()


def discard_invalid_recording_to_patient():
    test_id = st.session_state.get("current_test_id")
    measurement_id = st.session_state.get("current_measurement_id")

    try:
        get_recording_service().stop_measurement()
    except Exception as error:
        st.warning(f"Arduino recording stop failed: {error}")

    if measurement_id is not None:
        discard_measurement(measurement_id)

    if test_id is not None:
        deleted_patient_id = delete_test_if_no_complete_measurements(test_id)
        if deleted_patient_id is not None:
            st.session_state.selected_patient_id = deleted_patient_id
            st.session_state.current_test_id = None

    clear_recording_session_state()
    goto("patient_view")


def stop_recording():
    try:
        get_recording_service().stop_measurement()
    except Exception as error:
        st.warning(f"Arduino recording stop failed: {error}")

    st.session_state.recording = False
    st.session_state.recording_end_time = datetime.now()
    st.session_state.recording_duration = (
        time.time() - st.session_state.recording_start_time
    )
    st.session_state.show_stop_summary = True
    st.session_state.show_invalid_stop_dialog = False

    update_test_status(
        st.session_state.current_test_id,
        "Stopped"
    )

    persist_state_to_url()
    st.rerun()
