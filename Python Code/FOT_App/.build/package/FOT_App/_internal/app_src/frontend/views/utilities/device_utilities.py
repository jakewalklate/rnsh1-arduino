import streamlit as st

from frontend.navigation import goto
from frontend.views.utilities.calibration import (
    calibration_tab,
    get_calibration_service,
)
from frontend.views.utilities.gain_phase_calibration import gain_phase_calibration_tab
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
    # A wrapping row rather than fixed columns, so the button keeps its size and
    # drops under the title on a narrow window instead of being squashed.
    with st.container(horizontal=True, vertical_alignment="center"):
        st.title(title, width="content")
        st.space("stretch")
        if st.button("Return Home", key=f"{title}_return_home"):
            _leave_utility_page()
            goto("home")


def device_calibration_page():
    _render_page_header("Device Calibration")

    gain_phase_view, flow_view = st.tabs(
        ["Gain & Phase Calibration", "Flow Calibration"]
    )
    with gain_phase_view:
        gain_phase_calibration_tab()
    with flow_view:
        flow_calibration_tab()


def system_check_page():
    _render_page_header("System Check")
    system_check_tab()
