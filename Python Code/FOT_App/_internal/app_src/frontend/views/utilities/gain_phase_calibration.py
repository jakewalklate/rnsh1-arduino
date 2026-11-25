"""Gain & Phase Calibration tab: enter the test object's value, press one button.

The device records the reference load at every excitation frequency and works out
two corrections for each (see backend/processing/FOTGainPhaseCalibration.py). The
older CPAP-sweep tools live on the Advanced tab.
"""

from datetime import datetime
from html import escape

import pandas as pd
import streamlit as st

from backend.processing.FOTGainPhaseCalibration import (
    FREQUENCIES_HZ,
    RESISTANCE_UNITS,
    to_cmh2o,
    total_seconds,
)
from backend.processing.FOTImpedance import REFERENCE_RESISTANCE
from frontend.components.ui import card, card_header, hero, pill, steps, tile, tiles
from frontend.services.recording_service import get_recording_service
from frontend.views.utilities.calibration import (
    _render_live_trace,
    _render_port_selector,
    get_calibration_service,
    release_other_device_owners,
)

KEY = "gp"


def _format_saved(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d %b %Y %H:%M")
    except ValueError:
        return value


def _source_label(entry):
    if entry["gain"] is None and entry["phase_deg"] is None:
        return "Not calibrated"
    if entry["method"] == "gain_phase":
        return "Gain & Phase"
    return "Old CPAP sweep"


def _render_in_use(entries):
    card_header(
        "Calibration in use",
        "Each recording is analysed with the values for the frequency it was recorded at.",
    )
    items = []
    for entry in entries:
        calibrated = entry["gain"] is not None or entry["phase_deg"] is not None
        used = calibrated
        items.append(tile(
            f"{entry['excitation_hz']:g} Hz",
            [
                ("Gain", "—" if entry["gain"] is None else f"×{entry['gain']:.3f}"),
                ("Phase", "—" if entry["phase_deg"] is None else f"{entry['phase_deg']:+.2f}°"),
            ],
            tone="pass" if calibrated else "neutral",
            badge='<span class="ui-tag">Results</span>' if used else "",
            note=escape(f"{_source_label(entry)} · {_format_saved(entry['saved_utc'])}")
            if calibrated else "Not calibrated",
            highlight=used,
        ))
    # Two per row: 5 and 6 Hz, then 11 and 19 Hz.
    tiles(items, columns=2)


def _render_inputs(disabled):
    cols = st.columns([3, 2])
    with cols[0]:
        value = st.number_input(
            "Resistance printed on the test object",
            min_value=0.0,
            value=float(REFERENCE_RESISTANCE),
            step=0.01,
            format="%.2f",
            key=f"{KEY}_reference_value",
            disabled=disabled,
        )
    with cols[1]:
        unit = st.selectbox(
            "Unit on the label",
            options=list(RESISTANCE_UNITS),
            key=f"{KEY}_reference_unit",
            disabled=disabled,
        )
    reference = to_cmh2o(value, unit)
    if unit != "cmH₂O·s/L":
        st.caption(f"= {reference:.2f} cmH₂O·s/L, the unit the app reports in.")
    return reference


@st.fragment(run_every=1)
def _render_progress(service):
    status = service.capture_status("gain_phase")
    if status is None:
        return
    if status["state"] in ("done", "error"):
        service.commit_gain_phase()
        st.rerun(scope="app")
        return

    with card("progress", "accent"):
        card_header("Calibrating…", status["message"])
        st.progress(status["progress"])
        done = {r["excitation_hz"]: r["passed"] for r in status["results"]}
        pills = []
        for hz in FREQUENCIES_HZ:
            if hz in done:
                pills.append(pill(f"{hz:g} Hz", "pass" if done[hz] else "fail"))
            elif hz == status.get("current_hz"):
                pills.append(pill(f"{hz:g} Hz measuring", "info"))
            else:
                pills.append(pill(f"{hz:g} Hz", "neutral"))
        st.markdown(" ".join(pills), unsafe_allow_html=True)
        _render_live_trace(status, key_prefix=KEY)
        if st.button("Cancel calibration", key=f"{KEY}_cancel"):
            service.cancel_capture()
            st.rerun(scope="app")


def _fmt(value, pattern):
    return "—" if value is None else pattern.format(value)


def _results_table(results):
    return pd.DataFrame([
        {
            "Frequency": f"{r['excitation_hz']:g} Hz",
            "Result": "Pass" if r["passed"] else "Fail",
            "Gain": _fmt(r.get("gain"), "×{:.3f}"),
            "Phase": _fmt(r.get("correction_angle_deg"), "{:+.2f}°"),
            "Measured before correction": _fmt(
                r.get("measured_resistance_cmH2O_s_per_L"), "{:.2f}"
            ),
            "Pressure wave (cmH₂O)": _fmt(r.get("pressure_amplitude_cmH2O"), "{:.3f}"),
            "Flow wave (L/s)": _fmt(r.get("flow_amplitude_lps"), "{:.4f}"),
            "Variation": _fmt(r.get("block_variation_percent"), "{:.1f}%"),
        }
        for r in results
    ])


def _render_results(service):
    results = service.gain_phase_results
    if not results:
        return

    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]
    reference = results[0]["reference_resistance_cmH2O_s_per_L"]

    with card("results", "accent"):
        card_header(
            f"Results: {len(passed)} of {len(results)} frequencies passed",
            f"Test object {reference:.2f} cmH₂O·s/L. Gain brings the measured value "
            "onto the printed one; phase is the timing correction between the "
            "pressure and flow sensors.",
        )
        tiles([
            tile(
                f"{r['excitation_hz']:g} Hz",
                [
                    ("Gain", _fmt(r.get("gain"), "×{:.3f}")),
                    ("Phase", _fmt(r.get("correction_angle_deg"), "{:+.2f}°")),
                    ("Measured", _fmt(r.get("measured_resistance_cmH2O_s_per_L"), "{:.2f}")),
                ],
                tone="pass" if r["passed"] else "fail",
                badge=pill("Pass", "pass") if r["passed"] else pill("Fail", "fail"),
                reason="" if r["passed"] else "; ".join(r["reasons"]),
            )
            for r in results
        ])
        with st.expander("Full details"):
            st.dataframe(_results_table(results), hide_index=True, width="stretch")

        if passed and failed:
            st.caption(
                "Saving keeps the previous calibration for the frequencies that failed."
            )
        cols = st.columns([1, 1, 2])
        with cols[0]:
            if st.button(
                "Save calibration",
                type="primary",
                width="stretch",
                disabled=not passed,
                key=f"{KEY}_save",
            ):
                try:
                    saved = service.save_gain_phase()
                except Exception as error:
                    st.error(str(error))
                else:
                    st.session_state[f"{KEY}_saved"] = saved
                    st.rerun()
        with cols[1]:
            if st.button("Discard", width="stretch", key=f"{KEY}_discard"):
                service.discard_gain_phase()
                st.rerun()


def _busy_reason(service, running):
    try:
        if get_recording_service().is_recording:
            return "A patient recording is in progress. Stop it before calibrating."
    except Exception:
        pass
    any_status = service.capture_status()
    if (
        not running
        and any_status is not None
        and any_status["state"] in ("settling", "collecting")
    ):
        return "Another calibration capture is running."
    return None


def _render_run_card(service, running, port):
    with card("run", "accent"):
        card_header(
            "Run a calibration",
            "Takes about "
            f"{total_seconds() / 60:.0f} minutes: 30 seconds at each of "
            + ", ".join(f"{hz:g}" for hz in FREQUENCIES_HZ)
            + " Hz.",
        )
        steps([
            "Take the mask off and fit the test object in its place.",
            "Turn the CPAP machine off.",
            "Enter the value printed on the test object and press Start.",
        ])
        reference = _render_inputs(disabled=running)
        busy_reason = _busy_reason(service, running)
        if running:
            st.caption("Keep the test object still and the circuit sealed.")
            return
        if st.button(
            "Start calibration",
            type="primary",
            width="stretch",
            disabled=bool(busy_reason) or port is None or reference <= 0,
            key=f"{KEY}_start",
        ):
            release_other_device_owners()
            try:
                service.start_gain_phase(reference, port=port)
            except Exception as error:
                st.error(str(error))
            else:
                st.rerun()
        if busy_reason:
            st.info(busy_reason)
        elif port is None:
            st.info("Connect the device to start. See Device connection below.")


def gain_phase_calibration_tab():
    service = get_calibration_service()

    if not service.hardware_enabled:
        st.warning("Device access is switched off in the configuration file.")
        return

    status = service.capture_status("gain_phase")
    running = status is not None and status["state"] == "collecting"
    port = _render_port_selector(service, disabled=running, key_prefix=KEY, render=False)
    entries = service.gain_phase_in_use()
    missing = [
        e["excitation_hz"] for e in entries
        if e["gain"] is None or e["phase_deg"] is None
    ]

    hero(
        "Gain & Phase Calibration",
        "Corrects the device's readings against a test object of known resistance. "
        "Run it after changing the pneumotach, flow sensor, tubing or speaker. If the "
        "flow sensor changed, run Flow Calibration first.",
        pills=(
            pill(f"Device on {port}", "pass") if port else pill("No device found", "fail"),
            pill(f"All {len(entries)} frequencies calibrated", "pass") if not missing
            else pill(
                "Not calibrated: " + ", ".join(f"{hz:g} Hz" for hz in missing), "fail"
            ),
        ),
    )

    saved = st.session_state.pop(f"{KEY}_saved", None)
    if saved:
        st.success(
            "Calibration saved for "
            + ", ".join(f"{hz:g} Hz" for hz in saved)
            + ". Previous files were copied to data/calibration/archive.",
            icon="✅",
        )

    left, right = st.columns([5, 4], gap="small")
    with left:
        _render_run_card(service, running, port)
    with right:
        with card("in-use"):
            _render_in_use(entries)

    if status is not None:
        _render_progress(service)

    _render_results(service)

    with card("device", "muted"):
        card_header("Device connection")
        _render_port_selector(service, disabled=running, key_prefix=KEY)
