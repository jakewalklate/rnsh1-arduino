import streamlit as st

from backend.database.storage import initialize_database
from frontend.navigation import persist_state_to_url, restore_state_from_url
from frontend.views.home import render_app


st.set_page_config(
    page_title="Oscillometry Measurement System",
    layout="wide"
)

st.markdown("""
<style>
    [data-testid='stHeaderActionElements'] {
        display: none;
    }

    .block-container {
        padding-top: 1rem;
        padding-left: 2rem;
        padding-right: 2rem;
        max-width: 80%;
    }
</style>
""", unsafe_allow_html=True)

initialize_database()

DEFAULT_SESSION_STATE = {
    "form_version": 0,
    "page": "home",
    "home_tab": "new_patient",
    "selected_patient_id": None,
    "current_test_id": None,
    "current_measurement_id": None,
    "recording": False,
    "recording_start_time": None,
    "recording_start_datetime": None,
    "show_start_dialog": False,
    "show_config_dialog": False,
    "show_stop_summary": False,
    "show_invalid_stop_dialog": False,
    "show_delete_test_dialog": False,
    "show_delete_patient_dialog": False,
    "delete_empty_test_on_cancel": False,
    "recording_end_time": None,
    "recording_duration": None,
}

restore_state_from_url(DEFAULT_SESSION_STATE)
persist_state_to_url()
render_app()
