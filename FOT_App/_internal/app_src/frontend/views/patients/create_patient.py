import streamlit as st
from frontend.navigation import goto
from backend.database.storage import create_patient, create_test
from utils import validate_patient, calculate_age

def create_patient_tab():
    
    form_key = st.session_state.form_version

    st.write("Enter patient details below.")

    colA, colB = st.columns(2, gap="small")
    with colA:
        first_name = st.text_input(
            "First Name",
            key=f"first_name_{form_key}"
        )

        last_name = st.text_input(
            "Last Name",
            key=f"last_name_{form_key}"
        )

        col1, col2, col3 = st.columns(3)

        with col1:
            day = st.number_input(
                "Day",
                min_value=1,
                max_value=31,
                step=1,
                value=1,
                key=f"day_{form_key}"
            )

        with col2:
            month = st.number_input(
                "Month",
                min_value=1,
                max_value=12,
                step=1,
                value=1,
                key=f"month_{form_key}"
            )

        with col3:
            year = st.number_input(
                "Year",
                min_value=1900,
                max_value=2100,
                step=1,
                value=2000,
                key=f"year_{form_key}"
            )

        dob = f"{int(day):02d}/{int(month):02d}/{int(year)}"

        sex = st.selectbox(
            "Sex",
            ["Female", "Male"],
            key=f"sex_{form_key}"
        )
    
    with colB: 

        height = st.number_input(
            "Height (cm)",
            min_value=0,
            max_value=250,
            step=1,
            value=0,
            key=f"height_{form_key}"
        )

        weight = st.number_input(
            "Weight (kg)",
            min_value=0,
            max_value=500,
            step=1,
            value=0,
            key=f"weight_{form_key}"
        )

        smoking_status = st.selectbox(
            "Smoking Status",
            ["Smoker", "Non-Smoker"],
            key=f"smoking_status_{form_key}"
        )

        pack_years = st.number_input(
            "Pack Years",
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            value=0.0,
            key=f"pack_years_{form_key}",
            disabled=(smoking_status != "Smoker")
        )

    if st.button(
        "Create Patient",
        type="primary",
        width="stretch",
        key=f"create_patient_{form_key}"
    ):

        errors = validate_patient(
            first_name,
            last_name,
            dob,
            sex,
            height,
            weight,
            smoking_status,
            pack_years
        )

        if errors:
            for error in errors:
                st.error(error)

        else:
            patient_id = create_patient(
                first_name,
                last_name,
                dob,
                sex,
                height,
                weight,
                smoking_status,
                pack_years
            )

            test_id = create_test(patient_id)

            st.session_state.selected_patient_id = patient_id
            st.session_state.current_test_id = test_id
            st.session_state.show_start_dialog = True
            st.session_state.show_config_dialog = False
            st.session_state.show_stop_summary = False
            st.session_state.delete_empty_test_on_cancel = True
            st.session_state.recording = False
            st.session_state.current_measurement_id = None

            goto("start_test")

