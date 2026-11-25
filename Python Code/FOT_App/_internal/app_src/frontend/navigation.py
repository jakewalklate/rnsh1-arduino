import streamlit as st
from datetime import datetime


STATE_KEYS = {
    "page": str,
    "home_tab": str,
    "selected_patient_id": str,
    "current_test_id": str,
    "current_measurement_id": str,
    "recording": bool,
    "recording_start_time": float,
    "recording_start_datetime": datetime,
    "show_start_dialog": bool,
    "show_config_dialog": bool,
    "show_stop_summary": bool,
    "show_invalid_stop_dialog": bool,
    "delete_empty_test_on_cancel": bool,
    "recording_end_time": datetime,
    "recording_duration": float,
}


def _bool_from_query(value):
    return str(value).lower() in {"1", "true", "yes"}


def _query_value(key):
    value = st.query_params.get(key)

    if isinstance(value, list):
        return value[0] if value else None

    return value


def restore_state_from_url(defaults):
    for key, default in defaults.items():
        value = _query_value(key)

        if value is None:
            if key not in st.session_state:
                st.session_state[key] = default
            continue

        value_type = STATE_KEYS.get(key, str)

        try:
            if value_type is bool:
                st.session_state[key] = _bool_from_query(value)
            elif value_type is float:
                st.session_state[key] = float(value)
            elif value_type is datetime:
                st.session_state[key] = datetime.fromisoformat(value)
            else:
                st.session_state[key] = value
        except (TypeError, ValueError):
            st.session_state[key] = default


def persist_state_to_url():
    params = {}

    for key in STATE_KEYS:
        value = st.session_state.get(key)

        if value is None:
            continue

        if isinstance(value, bool):
            params[key] = "1" if value else "0"
        elif isinstance(value, datetime):
            params[key] = value.isoformat()
        else:
            params[key] = str(value)

    st.query_params.clear()
    st.query_params.update(params)


def goto(page):
    st.session_state.page = page
    persist_state_to_url()
    st.rerun()
