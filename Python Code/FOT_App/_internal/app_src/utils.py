import streamlit as st
import re
from datetime import datetime

# Validation
def validate_patient(first_name, last_name, dob, sex, height, weight, smoking_status, pack_years):
    errors = []

    name_pattern = r"[A-Za-z\s'-]{1,25}"

    if not first_name.strip():
        errors.append("First name is required.")
    elif not re.fullmatch(name_pattern, first_name.strip()):
        errors.append("First name contains invalid characters.")

    if not last_name.strip():
        errors.append("Last name is required.")
    elif not re.fullmatch(name_pattern, last_name.strip()):
        errors.append("Last name contains invalid characters.")

    try:
        dob_date = datetime.strptime(dob, "%d/%m/%Y")

        today = datetime.today()

        if dob_date > today:
            errors.append("Date of birth cannot be in the future.")

        age = (
            today.year
            - dob_date.year
            - ((today.month, today.day) < (dob_date.month, dob_date.day))
        )

        if age > 120:
            errors.append("Please enter a valid date of birth.")

    except ValueError:
        errors.append("Please enter a valid date of birth.")

    if sex not in ["Male", "Female"]:
        errors.append("Please select a valid sex.")

    if height <= 0 or height > 250:
        errors.append("Height must be between 1 and 250 cm.")

    if weight <= 0 or weight > 500:
        errors.append("Weight must be between 1 and 500 kg.")

    if smoking_status not in ["Non-Smoker", "Smoker"]:
        errors.append("Please select a valid smoking status.")

    if pack_years is not None and (pack_years < 0 or pack_years > 100):
        errors.append("Pack years must be between 0 and 100.")

    return errors

# Calulate age
def calculate_age(dob):
    dob = datetime.strptime(dob, "%d/%m/%Y")
    today = datetime.today()

    return (
        today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    )
