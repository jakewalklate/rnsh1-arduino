import streamlit as st
from backend.database.storage import get_patient, update_patient
from utils import validate_patient
from datetime import datetime
from frontend.components.ui import card
from frontend.navigation import goto

# Edit existing patient form
def edit_patient():
    # Edit Patient
    patient = get_patient(st.session_state.selected_patient_id)

    patient_id = patient[0]

    st.title("Edit Patient Details")
    st.caption("Make edits to patient details below. Ensure all fields are filled correctly before saving changes.")

    form_card = card("edit-patient", "accent")
    colA, colB = form_card.columns(2, gap="medium")

    with colA:
        first_name = st.text_input(
            "First Name",
            value=patient[1]
        )

        last_name = st.text_input(
            "Last Name",
            value=patient[2]
        )

        dob_date = datetime.strptime(patient[3], "%d/%m/%Y")

        col1, col2, col3 = st.columns(3)

        with col1:
            day = st.number_input(
                "Day",
                min_value=1,
                max_value=31,
                step=1,
                value=dob_date.day
            )

        with col2:
            month = st.number_input(
                "Month",
                min_value=1,
                max_value=12,
                step=1,
                value=dob_date.month
            )

        with col3:
            year = st.number_input(
                "Year",
                min_value=1900,
                max_value=2100,
                step=1,
                value=dob_date.year
            )

        dob = f"{int(day):02d}/{int(month):02d}/{int(year)}"

        sex = st.selectbox(
            "Sex",
            ["Female", "Male"],
            index=0 if patient[4] == "Female" else 1
        )

    with colB:
        height = st.number_input(
            "Height (cm)",
            min_value=0,
            max_value=250,
            step=1,
            value=int(patient[5])
        )

        weight = st.number_input(
            "Weight (kg)",
            min_value=0,
            max_value=500,
            step=1,
            value=int(patient[6])
        )

        smoking_options = ["Smoker", "Non-Smoker"]
        smoking_status = st.selectbox(
            "Smoking Status",
            smoking_options,
            index=smoking_options.index(patient[7])
        )

        pack_years = st.number_input(
            "Pack Years",
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            value=float(patient[8] or 0),
            disabled=(smoking_status != "Smoker")
        )

    col1, col2 = form_card.columns(2, gap="medium")

    with col1:

        if st.button(
                    "Cancel",
                    use_container_width=True
                ):
                    goto("patient_view")

    with col2:
         
         if st.button(
                     "Save Changes",
                     type="primary",
                     use_container_width=True
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
         
                         update_patient(
                             patient_id,
                             first_name,
                             last_name,
                             dob,
                             sex,
                             height,
                             weight,
                             smoking_status,
                             pack_years
                         )
         
                         st.success("Patient updated successfully!")
         
                         goto("patient_view")

        
