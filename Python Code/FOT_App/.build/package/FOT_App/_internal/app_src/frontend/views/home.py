import streamlit as st

from frontend.components.ui import card, card_header, hero
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
    hero(
        "OSCAR Measurement System",
        "Forced oscillation measurements for patients on CPAP. Start a test for a new "
        "or existing patient, or check and calibrate the device.",
    )

    col_left, col_right = st.columns([7, 2], gap="small")

    with col_left:
        with card("new-test", "accent"):
            card_header(
                "Create new test",
                "Add a new patient, or pick an existing one, to start a test.",
            )

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

    with col_right:
        with card("tools"):
            card_header("Device tools", "Check and calibrate the device.")
            if st.button("System Check", width="stretch", type="primary"):
                goto("system_check")
            st.caption("Daily check against the test object.")
            st.divider()
            if st.button("Calibrate Device", width="stretch"):
                goto("device_calibration")
            st.caption("After changing the pneumotach, flow sensor, tubing or speaker.")


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
