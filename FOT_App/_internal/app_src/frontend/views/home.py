import streamlit as st

from frontend.navigation import goto
from frontend.views.utilities.device_utilities import (
    device_calibration_page,
    system_check_page,
)
from frontend.views.tests.new_test import new_test_page
from frontend.views.patients.create_patient import create_patient_tab
from frontend.views.patients.edit_patient import edit_patient
from frontend.views.patients.existing_patient import existing_patient_tab, existing_patient_view
from frontend.views.tests.test_results import test_view


HOME_TABS = {
    "new_patient": "Add New Patient",
    "existing_patients": "Use Existing Patient",
}


def render_home_page():
    st.title("OSCAR Measurement System")

    col_left, col_right = st.columns([6, 1], gap="medium")

    # Create new test section
    with col_left:
        st.subheader("Create New Test")
        st.caption("Create a new test for a patient by selecting an existing patient or adding a new one.")

        default_home_tab = HOME_TABS.get(
            st.session_state.get("home_tab"),
            HOME_TABS["new_patient"]
        )

        tab_new, tab_existing = st.tabs([
            HOME_TABS["new_patient"],
            HOME_TABS["existing_patients"],
        ], default=default_home_tab)

        with tab_new:
            create_patient_tab()

        with tab_existing:
            existing_patient_tab()

    # Device utilities section
    with col_right:
        st.subheader("Utilities")
        st.caption("Verify & Calibrate")
        utility_tab = st.tabs(["Tools:"], default="")[0]
        with utility_tab:
            if st.button("System Check", width="stretch"):
                goto("system_check")
            if st.button("Calibrate Device", width="stretch"):
                goto("device_calibration")

def render_app():
    page = st.session_state.page

    if page == "home":
        render_home_page()
    elif page == "patient_view":
        existing_patient_view()
    elif page == "edit_patient":
        edit_patient()
    elif page == "start_test":
        new_test_page()
    elif page == "test_view":
        test_view()
    elif page == "system_check":
        system_check_page()
    elif page == "device_calibration":
        device_calibration_page()
    else:
        st.session_state.page = "home"
        render_home_page()
