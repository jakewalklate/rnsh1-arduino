import streamlit as st

from backend.services.recording_service import RecordingService


@st.cache_resource
def get_recording_service():
    return RecordingService()
