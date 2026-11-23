import streamlit as st

from backend.database.storage import save_measurement_event
from frontend.views.tests.waveform_panel import (
    render_waveform_options_controls,
)
from frontend.views.tests.recording_state import (
    event_marker_key,
    format_duration,
    request_stop_recording,
)


def render_recording_page_styles():
    st.markdown(
        """
        <style>
            .control-row-spacer {
                height: 0.35rem;
            }

            div[data-testid="stHorizontalBlock"] div[data-testid="column"] button {
                min-height: 2.7rem;
            }
        </style>
        """,
        unsafe_allow_html=True
    )


def render_test_header(test_id, patient_name, test_status, elapsed):
    header_left, header_right = st.columns([1.1, 1.9], vertical_alignment="center")

    with header_left:
        st.title(test_id)
        st.caption(patient_name)
        excitation_hz = st.session_state.get("recording_excitation_hz")
        if st.session_state.recording and excitation_hz is not None:
            st.caption(f"Arduino excitation: **{excitation_hz:g} Hz**")

    with header_right:
        marker_key = event_marker_key(test_id)
        options_col, event_col, stop_col = st.columns(
            [1.15, 1.0, 0.9],
            gap="medium"
        )

        with options_col:
                    options_popover = st.popover(
                        "Options",
                        disabled=not st.session_state.recording,
                        width="stretch",
                    )
        
                    with options_popover:
                        render_waveform_options_controls(test_id)

        with event_col:
            event_popover = st.popover(
                "Add Event",
                disabled=not st.session_state.recording,
                width="stretch",
            )

            with event_popover:
                with st.form(
                    key=f"event_note_form_{test_id}",
                    clear_on_submit=True,
                ):
                    note = st.text_area(
                        "Event Note",
                        placeholder=(
                            "CPAP pressure change, cough, sneeze, mask adjustment, "
                            "position change..."
                        ),
                        height=120,
                    )
                    submitted = st.form_submit_button(
                        "Save Event",
                        type="primary",
                        width="stretch",
                    )

                if submitted:
                    cleaned_note = note.strip()
                    if not cleaned_note:
                        st.warning("Enter a note before saving the event.")
                    else:
                        measurement_id = st.session_state.get("current_measurement_id")

                        if measurement_id is None:
                            st.warning("No active measurement is available for this event.")
                        else:
                            save_measurement_event(
                                measurement_id,
                                elapsed,
                                cleaned_note,
                            )
                            st.session_state[marker_key].append(
                                {
                                    "time": format_duration(elapsed),
                                    "annotation": cleaned_note,
                                }
                            )
                            st.success("Event saved.")

        with stop_col:
            if st.session_state.recording:
                if st.button(
                    "Stop Recording",
                    type="primary",
                    width="stretch",
                ):
                    request_stop_recording()
            else:
                st.button(
                    test_status,
                    disabled=True,
                    width="stretch"
                )

        
    if st.session_state[marker_key]:
        latest_marker = st.session_state[marker_key][-1]
        st.caption(
            f"Last event: {latest_marker['time']} - {latest_marker['annotation']}"
        )
