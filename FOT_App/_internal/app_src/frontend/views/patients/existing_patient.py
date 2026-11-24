import streamlit as st
from html import escape

import pandas as pd
from backend.database.storage import delete_patient, get_patient, search_patients, get_all_patients, create_test, get_patient_tests
from utils import calculate_age
from frontend.components.ui import card, card_header, pill
from frontend.navigation import goto

# Confirm delete patient dialog
@st.dialog("Delete Patient", dismissible=False)
def confirm_delete_patient_dialog(patient_id, patient_name):
    st.info(
        f"Confirm to delete {patient_name}'s patient record and all associated data.\n"
    )


    col1, col2 = st.columns(2)

    with col1:
        if st.button(
            "Delete Patient",
            type="primary",
            use_container_width=True,
            key=f"danger_confirm_delete_patient_{patient_id}"
        ):
            delete_patient(patient_id)

            st.session_state.show_delete_patient_dialog = False
            st.session_state.selected_patient_id = None
            st.session_state.current_test_id = None
            st.session_state.current_measurement_id = None
            st.session_state.recording = False
            st.session_state.show_start_dialog = False
            st.session_state.show_config_dialog = False
            st.session_state.show_stop_summary = False
            st.session_state.delete_empty_test_on_cancel = False

            goto("home")

    with col2:
        if st.button(
            "Cancel",
            use_container_width=True,
            key=f"cancel_delete_patient_{patient_id}"
        ):
            st.session_state.show_delete_patient_dialog = False
            st.rerun()


PATIENTS_PER_PAGE = 20


def _patient_row(patient):
    patient_id, first_name, last_name, dob, sex = patient[:5]
    try:
        age = f"{calculate_age(dob)} yrs"
    except Exception:
        age = ""
    with st.container(key=f"ui-row-{patient_id}"):
        name_col, dob_col, sex_col, open_col = st.columns(
            PATIENT_COLUMNS, vertical_alignment="center", gap="small"
        )
        name_col.markdown(
            f'<div class="ui-row-name">{escape(f"{first_name} {last_name}")}</div>'
            f'<div class="ui-row-sub">{escape(str(patient_id))}</div>',
            unsafe_allow_html=True,
        )
        dob_col.markdown(
            f'<div class="ui-row-value">{escape(str(dob or "—"))}</div>'
            f'<div class="ui-row-sub">{escape(age)}</div>',
            unsafe_allow_html=True,
        )
        sex_col.markdown(
            f'<div class="ui-row-value">{escape(str(sex or "—"))}</div>',
            unsafe_allow_html=True,
        )
        if open_col.button(
            "Open",
            icon=":material/chevron_right:",
            icon_position="right",
            key=f"open_patient_{patient_id}",
            width="stretch",
        ):
            st.session_state.selected_patient_id = patient_id
            goto("patient_view")
            st.stop()


PATIENT_COLUMNS = [4, 3, 2, 1]


# Existing patient search and list
def existing_patient_tab():

    query = st.text_input(
        "Search for patient by name or ID",
        placeholder="Enter patient name or ID",
        icon=":material/search:",
    )

    results = search_patients(query) if query else get_all_patients()

    if not results:
        if query:
            st.warning("No patients found.")
        else:
            st.info("No patients yet. Add one on the Add New Patient tab.")
        return

    shown_key = "patient_list_shown"
    shown = st.session_state.get(shown_key, PATIENTS_PER_PAGE)

    with st.container(key="ui-rows-patients"):
        with st.container(key="ui-rowhead-patients"):
            header = st.columns(PATIENT_COLUMNS, vertical_alignment="center", gap="small")
            for col, label in zip(header, ("Patient", "DOB", "Sex", "")):
                col.markdown(
                    f'<div class="ui-row-head">{label}</div>', unsafe_allow_html=True
                )
        for patient in results[:shown]:
            _patient_row(patient)

    remaining = len(results) - shown
    st.caption(
        f"Showing {min(shown, len(results))} of {len(results)} "
        f"patient{'s' if len(results) != 1 else ''}."
    )
    if remaining > 0 and st.button(
        f"Show {min(remaining, PATIENTS_PER_PAGE)} more", key="patient_list_more"
    ):
        st.session_state[shown_key] = shown + PATIENTS_PER_PAGE
        st.rerun()


TEST_COLUMNS = [3, 3, 3, 3, 2]


def _test_row(test):
    """One test in a patient's Previous tests list. Columns of the tests table:
    0 test_id, 2 created_datetime ("dd/mm/yyyy hh:mm:ss"), 7 recording_sessions
    (saved measurements), 8 status."""
    test_id, created, measurements, status = test[0], test[2], test[7], test[8]
    date, _, time_of_day = str(created or "").partition(" ")
    with st.container(key=f"ui-row-test-{test_id}"):
        id_col, date_col, count_col, status_col, open_col = st.columns(
            TEST_COLUMNS, vertical_alignment="center", gap="small"
        )
        id_col.markdown(
            f'<div class="ui-row-name">{escape(str(test_id))}</div>',
            unsafe_allow_html=True,
        )
        date_col.markdown(
            f'<div class="ui-row-value">{escape(date or "—")}</div>'
            f'<div class="ui-row-sub">{escape(time_of_day[:5])}</div>',
            unsafe_allow_html=True,
        )
        count = int(measurements or 0)
        count_col.markdown(
            f'<div class="ui-row-value">{count}</div>', unsafe_allow_html=True
        )
        status_col.markdown(
            pill(str(status or "—"), "pass" if status == "Complete" else "neutral"),
            unsafe_allow_html=True,
        )
        if open_col.button(
            "Open",
            icon=":material/chevron_right:",
            icon_position="right",
            key=f"open_test_{test_id}",
            width="stretch",
        ):
            st.session_state.current_test_id = test_id
            goto("test_view")


# Existing patient details and test history view
def existing_patient_view():

    if "show_delete_patient_dialog" not in st.session_state:
        st.session_state.show_delete_patient_dialog = False

    patient = get_patient(st.session_state.selected_patient_id)

    patient_id = patient[0]
    first_name = patient[1]
    last_name = patient[2]
    dob = patient[3]
    sex = patient[4]
    height = patient[5]
    weight = patient[6]
    smoking_status = patient[7]
    pack_years = patient[8]

    age = calculate_age(dob)

    # Navigation row: back on the left, the main action on the right.
    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button(
            "Return to List",
            icon=":material/arrow_back:",
            key="return_to_list",
        ):
            st.session_state.home_tab = "existing_patients"
            goto("home")

        st.space("stretch")

        if st.button("Start New Test", type="primary", key="start_new_test"):
            test_id = create_test(st.session_state.selected_patient_id)
            st.session_state.current_test_id = test_id

            st.session_state.show_start_dialog = True
            st.session_state.show_config_dialog = False
            st.session_state.show_stop_summary = False
            st.session_state.delete_empty_test_on_cancel = True
            st.session_state.recording = False
            st.session_state.current_measurement_id = None

            goto("start_test")

    st.title(f"{first_name} {last_name}")
    st.caption(f"Patient {patient_id} · {sex} · {age} years")

    col1, col2 = st.columns([1, 2], gap="medium")

    with col1, card("patient-details"):
        card_header("Patient details")

        detail_rows = [
            {"Detail": "Patient ID", "Value": str(patient_id)},
            {"Detail": "Date of Birth", "Value": str(dob)},
            {"Detail": "Age", "Value": str(age)},
            {"Detail": "Sex", "Value": str(sex)},
            {"Detail": "Height", "Value": f"{height} cm"},
            {"Detail": "Weight", "Value": f"{weight} kg"},
            {"Detail": "Smoking", "Value": str(smoking_status)},
        ]

        if smoking_status == "Smoker":
            detail_rows.append({
                "Detail": "Pack Years",
                "Value": str(pack_years) if pack_years else "-"
            })

        st.dataframe(
            pd.DataFrame(detail_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Detail": st.column_config.Column(width="stretch"),
                "Value": st.column_config.Column(width="stretch"),
            }
        )

        edit_col, delete_col = st.columns(2)

        with edit_col:
            if st.button("Edit Patient Details", use_container_width=True):
                goto("edit_patient")

        with delete_col:
            if st.button(
                "Delete Patient",
                type="primary",
                use_container_width=True,
                key="danger_delete_patient"
            ):
                st.session_state.show_delete_patient_dialog = True
                st.rerun()

        if st.session_state.show_delete_patient_dialog:
            confirm_delete_patient_dialog(
                patient_id,
                f"{first_name} {last_name}"
            )

    with col2, card("previous-tests", "accent"):
        card_header("Previous tests", "Open a test to see its results.")

        tests = get_patient_tests(patient_id)

        if tests:
            with st.container(key="ui-rows-tests"):
                with st.container(key="ui-rowhead-tests"):
                    header = st.columns(TEST_COLUMNS, vertical_alignment="center", gap="small")
                    for col, label in zip(header, ("Test", "Date", "Measurements", "Status", "")):
                        col.markdown(
                            f'<div class="ui-row-head">{label}</div>', unsafe_allow_html=True
                        )
                for test in tests:
                    _test_row(test)
        else:
            st.info("No previous tests have been recorded.")
