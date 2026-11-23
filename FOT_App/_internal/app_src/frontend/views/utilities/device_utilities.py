import streamlit as st

from frontend.navigation import goto
from frontend.views.utilities.calibration import (
    calibration_tab,
    get_calibration_service,
)
from frontend.views.utilities.flow_calibration import (
    flow_calibration_tab,
    get_flow_calibration_service,
)
from frontend.views.utilities.system_check import system_check_tab


def release_device_connections():
    """Release every utility-owned handle before patient recording takes the port."""
    try:
        get_flow_calibration_service().release_device()
    except Exception:
        pass

    try:
        get_calibration_service().release_device()
    except Exception:
        pass


def _leave_utility_page():
    """Return home without cycling a healthy idle calibration connection.

    System Check and Device Calibration share the same service. Keeping its stopped,
    idle handle avoids an unnecessary close/reopen of the Uno R4 USB device. Flow
    calibration cannot be left alive because it changes the stream units, and an
    active calibration capture must also be cancelled before leaving.
    """
    try:
        get_flow_calibration_service().release_device()
    except Exception:
        pass

    try:
        service = get_calibration_service()
        if service.capture_status() is not None:
            service.release_device()
    except Exception:
        pass


def _render_page_header(title):
    title_col, back_col = st.columns([6, 1])
    with title_col:
        st.title(title)
    with back_col:
        st.space("small")
        if st.button("Return Home", width="stretch", key=f"{title}_return_home"):
            _leave_utility_page()
            goto("home")


def device_calibration_page():
    _render_page_header("Device Calibration")

    calibration_view, flow_view = st.tabs(
        ["Device Calibration", "Flow Calibration"]
    )
    with calibration_view:
        calibration_tab()
    with flow_view:
        flow_calibration_tab()


def system_check_page():
    _render_page_header("System Check")
    system_check_tab()
