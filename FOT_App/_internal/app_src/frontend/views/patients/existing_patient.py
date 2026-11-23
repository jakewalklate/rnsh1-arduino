import streamlit as st
import pandas as pd
from backend.database.storage import delete_patient, get_patient, search_patients, get_all_patients, create_test, get_patient_tests
from utils import calculate_age
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
            key=f"confirm_delete_patient_{patient_id}"
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


# Existing patient search and view
def existing_patient_tab():

    query = st.text_input("Search for patient by name or ID", placeholder="Enter patient name or ID")

    if query:
        results = search_patients(query)
    else:
        results = get_all_patients()

    if results:

        # Convert to DataFrame
        table_data = pd.DataFrame([
            {
                "Select": False,
                "Patient ID": p[0],
                "Name": f"{p[1]} {p[2]}"
            }
            for p in results
        ])

        edited_df = st.data_editor(
            table_data,
            width='stretch',
            hide_index=True,
            column_config={
                "Select": st.column_config.CheckboxColumn("Select patient")
            },
            disabled=["Patient ID", "Name"]
        )

        # Safety conversion
        if not isinstance(edited_df, pd.DataFrame):
            edited_df = pd.DataFrame(edited_df)

        selected_rows = edited_df[edited_df["Select"].fillna(False)]

        if not selected_rows.empty:
            selected_id = selected_rows.iloc[0]["Patient ID"]

            st.session_state.selected_patient_id = selected_id
            goto("patient_view")
            st.stop()

    elif query:
        st.warning("No patients found.")

    else:
        st.info("Start typing to search patients or view all records.")

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

    st.space("medium")

    left, right = st.columns([3, 2])

    with left:
        st.title(f"{first_name} {last_name}")
        
    with right:
        st.space("small")
        with st.container(horizontal=True, horizontal_alignment="right"):
            if st.button("Start New Test", type="primary"):
                test_id = create_test(st.session_state.selected_patient_id)
                st.session_state.current_test_id = test_id

                st.session_state.show_start_dialog = True
                st.session_state.show_config_dialog = False
                st.session_state.show_stop_summary = False
                st.session_state.delete_empty_test_on_cancel = True
                st.session_state.recording = False
                st.session_state.current_measurement_id = None

                goto("start_test")
            
            if st.button("Return to List"):
                st.session_state.home_tab = "existing_patients"
                goto("home")




    col1, col2 = st.columns([1, 2])

    with col1:

        st.subheader("Patient Details")
        st.caption("Patient information provided for reference.")

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
                use_container_width=True
            ):
                st.session_state.show_delete_patient_dialog = True
                st.rerun()

        if st.session_state.show_delete_patient_dialog:
            confirm_delete_patient_dialog(
                patient_id,
                f"{first_name} {last_name}"
            )

    with col2: 
        st.subheader("Previous Tests")
        st.caption("Select a test to view detailed results.")

        tests = get_patient_tests(patient_id)

        if tests:
            table_data = [
                {
                    "Select": False,
                    "Test ID": t[0],
                    "Date": t[2],
                    "Measurements": t[7],
                }
                for t in tests
            ]

            column_config = {
                col: st.column_config.Column(width="stretch")
                for col in ["Test ID", "Date", "Measurements"]
            }
            column_config["Select"] = st.column_config.CheckboxColumn("View", width="small")

            edited_df = st.data_editor(
                table_data,
                width="stretch",
                hide_index=True,
                column_config={
                    "Select": st.column_config.CheckboxColumn("View", width="small"),
                    "Test ID": st.column_config.Column(width="stretch", alignment="left"),
                    "Date": st.column_config.Column(width="stretch", alignment="left"),
                    "Measurements": st.column_config.Column(width="stretch", alignment="left"),
                },
                disabled=["Test ID", "Date", "Measurements"]
            )


            if not isinstance(edited_df, pd.DataFrame):
                edited_df = pd.DataFrame(edited_df)

            selected_rows = edited_df[edited_df["Select"].fillna(False)]

            if not selected_rows.empty:
                st.session_state.current_test_id = selected_rows.iloc[0]["Test ID"]
                goto("test_view")

        else:
            st.info("No previous tests have been recorded.")