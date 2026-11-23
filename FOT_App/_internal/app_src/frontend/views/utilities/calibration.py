"""Device calibration tab — CPAP bias-flow sweep.

Run when the tubing, flowhead or speaker mounting changes. A known reference load
replaces the patient, CPAP is stepped through the listed pressures, and the fitted
constants are written to the calibration file the measurement engine loads.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backend.config import (
    ARDUINO_AUTO_DETECT,
    ARDUINO_AUTO_DETECT_SINGLE_PORT,
    ARDUINO_BAUDRATE,
    ARDUINO_CLONE_USB_VENDOR_IDS,
    ARDUINO_PROBE_PORTS,
    ARDUINO_USB_VENDOR_IDS,
    ARDUINO_PORT,
    ARDUINO_PORT_HINTS,
)
from backend.device.serial_device import (
    ArduinoSerialError,
    remember_port,
    resolve_arduino_port,
    scan_for_arduinos,
)
from backend.processing.FOTImpedance import EXCITATION_HZ, REFERENCE_RESISTANCE
from backend.processing.FOTCalibrationPresets import period_label
from backend.processing.FOTPhaseCalibration import (
    CALIBRATION_SETTLING_SECONDS,
    REPEAT_CALIBRATION_WARNING_DEG,
    bias_check_reasons,
    failure_reasons,
    seconds_for_phase_calibration,
)
from backend.processing.FOTTubeCalibration import (
    CPAP_STEPS,
    DEFAULT_REPEATS,
    MAX_FIT_ORDER,
    MAX_REPEATS,
    seconds_for_capture,
)
from backend.services.calibration_service import CalibrationService
from frontend.services.recording_service import get_recording_service


@st.cache_resource
def get_calibration_service():
    return CalibrationService()


def release_other_device_owners():
    """Hand the COM port back from the other device-owning tab before taking it.

    Each tab's service is a cached singleton holding its own connection, and only one
    program can hold a port. Without this, running a syringe calibration and then a
    check fails with a bare pyserial PermissionError that looks like a driver fault.
    Imported lazily because the flow tab imports from this module.
    """
    from frontend.views.utilities.flow_calibration import get_flow_calibration_service

    try:
        get_flow_calibration_service().release_device()
    except Exception:
        pass


def _fit_orders():
    return st.session_state.get("cal_r_degree", 1), st.session_state.get("cal_x_degree", 1)


def _render_active_calibration(service):
    active_bias = service.active_calibration()
    active_phase = service.active_phase_calibration()

    with st.expander("Calibration currently in use", expanded=True):
        if not active_bias and not active_phase:
            st.info(
                "No saved calibration file — the system is running on its built-in "
                "default values."
            )
            return

        cols = st.columns(4)
        cols[0].metric(
            "Phase angle",
            "Not saved"
            if not active_phase
            else f"{active_phase.get('correction_angle_deg', float('nan')):+.3f}°",
        )
        cols[1].metric(
            "Scale",
            "Not saved"
            if not active_bias
            else f"{active_bias.get('scale', float('nan')):.4f}",
        )
        cols[2].metric(
            "K plumbing",
            "Not saved"
            if not active_bias
            else f"{active_bias.get('k_plumbing', float('nan')):.4f}",
        )
        cols[3].metric(
            "Fit quality (R²)",
            "Not saved"
            if not active_bias
            else f"{active_bias.get('fit_r_squared', float('nan')):.3f}",
        )

        details = []
        if active_phase:
            details.append(
                f"phase: {service.active_phase_calibration_path().name}"
            )
        if active_bias:
            details.append(
                f"point fit: {service.active_calibration_path().name}, "
                f"R{active_bias.get('r_degree', '?')}/"
                f"X{active_bias.get('x_degree', '?')}, "
                f"{active_bias.get('n_points', '?')} points"
            )
        


def _clear_device_selection():
    st.session_state.pop("device_port", None)
    st.session_state.pop("device_probe_failed", None)
    st.session_state.pop("device_scan", None)
    remember_port(None)


def _render_device_search(service, disabled, key_prefix):
    """Scan every serial port, ask each Arduino-like one for STATUS, let the user pick."""
    if st.button(
        "Search for devices",
        key=f"{key_prefix}_scan_devices",
        disabled=disabled,
        help="Opens each Arduino-like serial port and checks whether the FOT "
        "firmware answers. Takes a few seconds.",
    ):
        with st.spinner("Searching serial ports for the FOT device..."):
            st.session_state["device_scan"] = scan_for_arduinos(
                port_hints=ARDUINO_PORT_HINTS,
                vendor_ids=ARDUINO_USB_VENDOR_IDS,
                clone_vendor_ids=ARDUINO_CLONE_USB_VENDOR_IDS,
                probe=True,
                # Never open a port this app already holds; it would fail or, on
                # macOS/Linux, steal half of the running stream.
                skip_probe_devices=[service.port] if getattr(service, "client", None) else [],
                baudrate=ARDUINO_BAUDRATE,
            )

    results = st.session_state.get("device_scan")
    if results is None:
        return
    if not results:
        st.warning(
            "No Arduino-like USB serial ports were found. Check the USB cable and "
            "that the board is powered, then search again."
        )
        return

    labels = {item.port.device: item.label for item in results}
    choice = st.selectbox(
        "Detected devices",
        options=list(labels),
        format_func=labels.get,
        key=f"{key_prefix}_scan_choice",
        disabled=disabled,
    )
    if st.button("Use this device", key=f"{key_prefix}_use_scanned", disabled=disabled):
        st.session_state["device_port"] = choice
        st.session_state.pop("device_probe_failed", None)
        st.session_state.pop("device_scan", None)
        remember_port(choice)
        st.rerun()


def _render_port_selector(service, disabled, key_prefix="cal", render=True):
    preferred_port = (
        st.session_state.get("device_port")
        or service.port
        or ARDUINO_PORT
    )
    # Probing opens ports and waits for a reply, so do it once per detection attempt
    # rather than on every Streamlit rerun; "Refresh device" re-arms it.
    probe = ARDUINO_PROBE_PORTS and not st.session_state.get("device_probe_failed")
    try:
        port = resolve_arduino_port(
            preferred_port=preferred_port,
            auto_detect=ARDUINO_AUTO_DETECT,
            port_hints=ARDUINO_PORT_HINTS,
            allow_single_port_fallback=ARDUINO_AUTO_DETECT_SINGLE_PORT,
            vendor_ids=ARDUINO_USB_VENDOR_IDS,
            clone_vendor_ids=ARDUINO_CLONE_USB_VENDOR_IDS,
            probe=probe,
            baudrate=ARDUINO_BAUDRATE,
        )
    except ArduinoSerialError as error:
        if probe:
            st.session_state["device_probe_failed"] = True
        if render:
            st.error(str(error))
            if st.button(
                "Refresh device",
                key=f"{key_prefix}_refresh_ports_empty",
                disabled=disabled,
            ):
                _clear_device_selection()
                st.rerun()
            _render_device_search(service, disabled, key_prefix)
        return None

    st.session_state["device_port"] = port
    remember_port(port)
    if not render:
        return port

    port_cols = st.columns([2, 1])
    with port_cols[0]:
        st.success(f"Device detected on {port}")
    with port_cols[1]:
        if st.button(
            "Refresh device",
            width="stretch",
            disabled=disabled,
            key=f"{key_prefix}_refresh_ports",
        ):
            _clear_device_selection()
            st.rerun()

    with st.expander("Use a different device"):
        _render_device_search(service, disabled, key_prefix)

    return port


def _preset_line(entry):
    """One-line summary of a stored preset, for a card or a table cell."""
    if not entry:
        return "—"
    s = entry.get("summary") or {}
    parts = []
    components = []
    if entry.get("phase"):
        components.append("phase")
    if entry.get("bias"):
        components.append("point fit")
    if components:
        parts.append(" + ".join(components))
    if s.get("correction_angle_deg") is not None:
        parts.append(f"{s['correction_angle_deg']:+.3f}°")
    if s.get("scale") is not None:
        parts.append(f"scale {s['scale']:.4f}")
    if s.get("k_plumbing") is not None:
        parts.append(f"k {s['k_plumbing']:.3f}")
    return " · ".join(parts) or "stored"


def _render_preset_history(service, status):
    """Prior presets per period, with restore. Restoring makes an old set of values the
    period's preset again; it does not put it in force until Activate is pressed."""
    with st.expander("Calibration history", expanded=False):
        period = st.selectbox(
            "Period",
            service.preset_periods(),
            format_func=period_label,
            key="cal_hist_period",
        )
        history = status["periods"][period]["history"]
        if not history:
            st.info(
                f"No earlier calibrations stored for {period}. Each time you store a "
                "preset here, the one it replaces is kept in this list."
            )
            return

        st.dataframe(
            pd.DataFrame([
                {
                    "#": i + 1,
                    "Stored (UTC)": e.get("saved_utc", "")[:19].replace("T", " "),
                    "Components": " + ".join(
                        component
                        for component, present in (
                            ("Phase", e.get("phase")),
                            ("Point fit", e.get("bias")),
                        )
                        if present
                    ),
                    "Angle": (
                        None if (e.get("summary") or {}).get("correction_angle_deg") is None
                        else round(e["summary"]["correction_angle_deg"], 3)
                    ),
                    "Scale": (
                        None if (e.get("summary") or {}).get("scale") is None
                        else round(e["summary"]["scale"], 4)
                    ),
                    "K plumbing": (
                        None if (e.get("summary") or {}).get("k_plumbing") is None
                        else round(e["summary"]["k_plumbing"], 3)
                    ),
                    "R²": (e.get("summary") or {}).get("fit_r_squared"),
                    "Points": (e.get("summary") or {}).get("n_points"),
                    "Order": _order_text(e.get("summary") or {}),
                    "Note": e.get("note", ""),
                }
                for i, e in enumerate(history)
            ]),
            width="stretch",
            hide_index=True,
        )

        pick_cols = st.columns([2, 1, 1])
        with pick_cols[0]:
            index = st.selectbox(
                "Entry",
                list(range(len(history))),
                format_func=lambda i: f"{i + 1}. {_preset_line(history[i])}",
                key="cal_hist_entry",
            )
        with pick_cols[1]:
            st.write("")
            if st.button("Restore", width="stretch", key="cal_hist_restore"):
                service.restore_preset(period, index)
                st.session_state["cal_preset_message"] = (
                    f"Restored an earlier calibration as the {period} preset. "
                    f"Press **Activate** on {period} to put it in force."
                )
                st.rerun()
        with pick_cols[2]:
            st.write("")
            if st.button("Delete", width="stretch", key="cal_hist_delete"):
                service.delete_preset_history_entry(period, index)
                st.rerun()


def _order_text(summary):
    r, x = summary.get("r_degree"), summary.get("x_degree")
    if r is None and x is None:
        return ""
    return f"R{r}/X{x}"


def _render_presets(service, switched=None):
    """Time-of-day presets: one calibration per period, optionally applied by the clock.

    Phase and point-fit components can be updated independently when required. Storing
    both together remains the default because the sweep was captured under its phase
    angle.
    """
    status = service.preset_status()
    st.markdown("##### Store as a time-of-day preset")

    message = st.session_state.pop("cal_preset_message", None)
    if message:
        st.success(message)
    if switched:
        st.info(f"Auto-applied the **{switched}** preset for the current time of day.")

    auto = st.toggle(
        "Apply automatically by time of day",
        value=status["auto_apply"],
        key="cal_auto_apply",
        help="When on, the preset for the current period is put in force on its own — "
             "including before a recording is analysed. When off, the period you "
             "activate stays in force until you change it.",
    )
    if auto != status["auto_apply"]:
        service.set_auto_apply(auto)
        st.rerun()

    clock = status["clock_period"]
    active = status["active_period"]
    if auto:
        st.caption(
            f"It is currently **{clock}** — that preset is the one being applied. "
            "Turn this off to hold a single calibration all day."
        )
    else:
        st.caption(
            f"Automatic switching is off. It is currently {clock}; "
            f"the calibration in force is "
            + (f"the **{active}** preset." if active else "whatever was last saved.")
        )

    period_cols = st.columns(3, gap="medium")
    for column, period in zip(period_cols, service.preset_periods()):
        entry = status["periods"][period]["current"]
        count = len(status["periods"][period]["history"])
        with column:
            marks = []
            if period == active:
                marks.append("in force")
            if period == clock:
                marks.append("now")
            suffix = f"  ·  {', '.join(marks)}" if marks else ""
            st.markdown(f"**{period_label(period)}**{suffix}")

            if entry is None:
                st.caption("No preset yet. Calibrate, then store it here.")
            else:
                st.caption(_preset_line(entry))
                st.caption(
                    f"Stored {entry.get('saved_utc', '')[:19].replace('T', ' ')} UTC"
                    + (f" · {count} earlier" if count else "")
                )
            # Auto-apply owns the choice while it is on, so a manual Activate there
            # would be undone on the next render.
            if st.button(
                "Activate",
                key=f"cal_activate_{period}",
                width="stretch",
                disabled=auto or entry is None or period == active,
            ):
                service.activate_period(period)
                st.session_state["cal_preset_message"] = (
                    f"The {period} preset is now in force."
                )
                st.rerun()

    component_cols = st.columns(2)
    with component_cols[0]:
        include_phase = st.checkbox(
            "Phase angle",
            value=True,
            key="cal_preset_include_phase",
        )
    with component_cols[1]:
        include_bias = st.checkbox(
            "Point calibration (R/X fit)",
            value=True,
            key="cal_preset_include_bias",
        )

    if include_phase != include_bias:
        st.warning(
            "Only one component will be updated. Any unselected component already "
            "stored in this period will be retained. Confirm that the phase angle "
            "and point fit remain compatible before activating the preset."
        )

    store_cols = st.columns([2, 2, 1])
    with store_cols[0]:
        target = st.selectbox(
            "Store the calibration in force as",
            service.preset_periods(),
            index=list(service.preset_periods()).index(clock),
            format_func=period_label,
            key="cal_preset_target",
        )
    with store_cols[1]:
        note = st.text_input(
            "Note (optional)",
            key="cal_preset_note",
            placeholder="e.g. new flowhead",
        )
    with store_cols[2]:
        st.write("")
        if st.button(
            "Store",
            width="stretch",
            key="cal_preset_store",
            disabled=not include_phase and not include_bias,
        ):
            try:
                service.store_current_as_preset(
                    target,
                    note=note.strip(),
                    include_phase=include_phase,
                    include_bias=include_bias,
                )
            except Exception as error:
                st.error(str(error))
            else:
                st.session_state["cal_preset_message"] = (
                    f"Stored the calibration in force as the {target} preset. "
                    "The preset it replaced is in the history below."
                )
                st.rerun()
    st.caption(
        "The selected components are copied from the calibration currently in force. "
        "Storing both together is recommended."
    )

    _render_preset_history(service, status)


def _render_phase_bias_advisory(result):
    """Separate verdict on the BIAS calibration, using this run's own capture.

    Kept visible because this is the only point in the workflow that measures the
    reference load with everything applied — dropping it would lose a real check.
    Kept SEPARATE because it says nothing about the angle: see FOTPhaseCalibration.
    """
    reasons = bias_check_reasons(result)
    detail = (
        f"With the saved bias calibration applied, the same capture reads "
        f"**{result['bias_corrected_resistance_cmH2O_s_per_L']:.3f}** / "
        f"**{result['bias_corrected_reactance_cmH2O_s_per_L']:+.3f}** at "
        f"{result['bias_flow_lps']:.3f} L/s bias flow."
    )
    if not reasons:
        st.caption(f"Bias calibration check: OK. {detail}")
        return
    st.warning(
        f"**The bias calibration looks off — this is not a phase problem.** {detail}"
    )
    for reason in reasons:
        st.caption(f"· {reason}")
    st.caption(
        "Re-run the CPAP sweep in the Calibrate Device tab, not this step. The angle "
        "above is unaffected and has been applied."
    )


def _render_phase_result(service):
    """Outcome of the last phase calibration run in this session."""
    result = service.phase_result
    if not result:
        return

    if result["calibration_pass"]:
        st.success(
            f"Phase calibration passed — correction angle "
            f"{result['correction_angle_deg']:+.3f}°."
        )
    else:
        # Applied regardless: the angle comes from this run's own phasor, so it is
        # self-consistent, whereas leaving the old one stranded is not. See
        # FOTPhaseCalibration's docstring.
        st.error(
            f"Phase calibration did not pass its checks — angle "
            f"{result['correction_angle_deg']:+.3f}° has been applied anyway."
        )
        for reason in failure_reasons(result):
            st.caption(f"· {reason}")

    # RAW, matching the live readout and the angle itself. The bias-corrected pair is
    # reported below as its own advisory: rotation nulls X by construction, so a
    # corrected reactance here only ever restates the bias polynomial.
    cols = st.columns(4)
    cols[0].metric("Correction angle", f"{result['correction_angle_deg']:+.3f}°")
    cols[1].metric(
        "Resistance (raw)", f"{result['raw_resistance_cmH2O_s_per_L']:.3f}"
    )
    cols[2].metric(
        "Reactance (raw)", f"{result['raw_reactance_cmH2O_s_per_L']:+.3f}"
    )
    cols[3].metric("Block variation", f"{result['block_variation_percent']:.1f}%")
    st.caption(
        "Unrotated, uncorrected — the phasor the angle was computed from. The "
        "reactance shown is the phase error this run just cancelled."
    )

    _render_phase_bias_advisory(result)

    repeat = result.get("repeat_angle_difference_deg")
    if repeat is not None and not result.get("repeat_ok", True):
        st.warning(
            f"This angle differs from the previous one by {repeat:.3f}° "
            f"(above the {REPEAT_CALIBRATION_WARNING_DEG:g}° repeat warning). "
            "Something in the setup has moved — check the tubing and the load."
        )

    st.caption(
        f"Reference {result['reference_resistance_cmH2O_s_per_L']:g} · bias flow "
        f"{result['bias_flow_lps']:.3f} L/s · "
        f"{result['blocks_used']} s used · saved to "
        f"{service.active_phase_calibration_path().name}"
    )

    if service.phase_stale_sweep:
        st.error(
            "**The captured points below were taken at the old angle.** R and X in a "
            "sweep are already rotated, so re-fitting them across a phase change gives "
            "wrong bias constants. Clear the points and capture the sweep again."
        )

    st.warning(
        "**Restart the application** before recording patients, so the new angle "
        "reaches measurement analysis."
    )
    if st.button("Dismiss", key="cal_phase_dismiss"):
        service.clear_phase_result()
        st.rerun()


@st.fragment(run_every=1)
def _render_phase_progress(service):
    status = service.capture_status("phase")
    if status is None:
        return

    if status["state"] in ("settling", "collecting"):
        progress_col, trace_col = st.columns([1, 1], gap="medium")
        with progress_col:
            st.progress(status["progress"], text=status["message"])
            live_cols = st.columns(3)
            live_cols[0].metric(
                "Resistance",
                "-" if status["live_r"] is None else f"{status['live_r']:.2f}",
            )
            live_cols[1].metric(
                "Reactance",
                "-" if status["live_x"] is None else f"{status['live_x']:.2f}",
            )
            live_cols[2].metric(
                "Bias flow",
                "-" if status["live_bias_flow"] is None
                else f"{status['live_bias_flow']:.2f}",
            )
            st.caption(
                "Live readout is the RAW, unrotated signal — no phase angle and no "
                "bias correction — which is exactly what the new angle is computed "
                "from when the capture finishes. Expect a reactance offset here; "
                "zeroing it is what this step does."
            )
            if status["dropped_blocks"]:
                st.caption(f"{status['dropped_blocks']} block(s) discarded and re-taken.")
            if st.button("Cancel calibration", key="cal_phase_cancel"):
                service.cancel_capture()
                st.rerun(scope="app")
        with trace_col:
            _render_live_trace(status, key_prefix="phase")
        return

    if status["state"] == "error":
        st.error(status["message"])
        if st.button("Dismiss", key="cal_phase_dismiss_error"):
            service.cancel_capture()
            st.rerun(scope="app")
        return

    if status["state"] == "done":
        service.commit_phase_calibration()
        st.rerun(scope="app")


def _render_phase_section(service, busy_reason, port, phase_status):
    st.markdown("#### 1. Phase angle")
    st.caption(
        "Zeroes the instrument's phase error against the reference load. Run this "
        "FIRST — the bias-flow sweep below is captured at whatever angle is in force, "
        "so changing the angle afterwards invalidates it."
    )

    active = service.active_phase_calibration()
    info_cols = st.columns([1, 1])
    if active:
        info_cols[0].metric(
            "Angle in use", f"{active['correction_angle_deg']:+.3f}°"
        )
        info_cols[1].metric(
            "Last result",
            "Pass" if active.get("calibration_pass") else "Check",
        )
    
    else:
        st.info(
            "No phase calibration saved — the system is applying no rotation at all "
            "(0°). Run this before anything else."
        )

    run = st.button(
        "Run phase calibration",
        type="primary",
        disabled=bool(busy_reason) or not port,
        key="cal_phase_run",
        width="stretch",
    )
    st.caption(
        f"Takes {seconds_for_phase_calibration()} seconds with the reference load "
        f"fitted in place of the patient — the first "
        f"{CALIBRATION_SETTLING_SECONDS} are discarded as settling."
    )

    if run:
        try:
            release_other_device_owners()
            service.start_phase_calibration(port=port)
        except Exception as error:
            st.error(f"Could not start phase calibration on {port}: {error}")
        else:
            st.rerun()

    if phase_status is not None:
        _render_phase_progress(service)

    _render_phase_result(service)


def _render_capture_controls(service, busy_reason, port):
    st.markdown("#### 2. Capture a point at each pressure")
    st.caption(
        "Set the CPAP machine to the pressure below, then capture. Each point is "
        "recorded at a steady pressure — wait for the blower to settle before starting."
    )

    captured = {p["cpap"] for p in service.points}

    control_cols = st.columns([2, 1, 1])
    with control_cols[0]:
        cpap_value = st.selectbox(
            "CPAP pressure (cmH2O)",
            CPAP_STEPS,
            format_func=lambda v: f"{v} cmH2O" + ("   captured" if v in captured else ""),
            key="cal_cpap_value",
        )
    with control_cols[1]:
        repeats = st.number_input(
            "Repeats",
            min_value=1,
            max_value=MAX_REPEATS,
            value=DEFAULT_REPEATS,
            step=1,
            key="cal_repeats",
            help="Independent windows averaged into one point. More repeats, less scatter.",
        )
    with control_cols[2]:
        st.space("small")
        start = st.button(
            "Capture point",
            type="primary",
            width="stretch",
            disabled=bool(busy_reason) or not port,
        )

    st.caption(f"One point takes about {seconds_for_capture(repeats)} seconds.")

    if busy_reason:
        st.warning(busy_reason)

    if start:
        try:
            release_other_device_owners()
            service.start_capture(cpap_value, int(repeats), port=port)
        except Exception as error:
            st.error(f"Could not start capture on {port}: {error}")
        else:
            st.rerun()


def _render_live_trace(status, key_prefix="cal"):
    """Live pressure and flow while the point is captured, so the operator can see
    the signal is healthy rather than trusting a progress bar alone."""
    times = status.get("trace_time_s") or []
    if not times:
        st.caption("Waiting for the first samples…")
        return

    for title, key, unit in (
        ("Pressure", "trace_pressure", "cmH2O"),
        ("Flow", "trace_flow", "L/s"),
    ):
        values = status.get(key) or []
        if not values:
            continue
        figure = go.Figure(go.Scatter(
            x=times, y=values, mode="lines", line={"width": 1.4},
        ))
        figure.update_layout(
            height=150,
            margin={"l": 8, "r": 8, "t": 22, "b": 24},
            title={"text": f"{title} ({unit})", "font": {"size": 13}},
            xaxis_title=None,
            showlegend=False,
            xaxis={"range": [times[0], 0]},
        )
        st.plotly_chart(
            figure,
            width="stretch",
            config={"displayModeBar": False},
            key=f"{key_prefix}_trace_{key}",
        )


@st.fragment(run_every=1)
def _render_capture_progress(service):
    """Polls the background capture. Runs as a fragment so the rest of the page is
    not re-run every second."""
    status = service.capture_status("sweep")
    if status is None:
        return

    if status["state"] in ("settling", "collecting"):
        progress_col, trace_col = st.columns([1, 1], gap="medium")

        with progress_col:
            st.progress(status["progress"], text=status["message"])
            live_cols = st.columns(3)
            live_cols[0].metric(
                "Resistance",
                "-" if status["live_r"] is None else f"{status['live_r']:.2f}",
            )
            live_cols[1].metric(
                "Reactance",
                "-" if status["live_x"] is None else f"{status['live_x']:.2f}",
            )
            live_cols[2].metric(
                "Bias flow",
                "-" if status["live_bias_flow"] is None
                else f"{status['live_bias_flow']:.2f}",
            )
            st.caption("Live readout — the saved point is the average of the windows.")
            if status["dropped_blocks"]:
                st.caption(
                    f"{status['dropped_blocks']} block(s) discarded and re-taken."
                )
            if st.button("Cancel capture", key="cal_cancel"):
                service.cancel_capture()
                st.rerun(scope="app")

        with trace_col:
            _render_live_trace(status, key_prefix="cal")
        return

    if status["state"] == "error":
        st.error(status["message"])
        if st.button("Dismiss", key="cal_dismiss_error"):
            service.cancel_capture()
            st.rerun(scope="app")
        return

    if status["state"] == "done":
        service.commit_captured_point()
        st.rerun(scope="app")


def _render_saved_sweeps(service):
    sweeps = service.available_sweeps()
    message = st.session_state.pop("cal_sweep_message", None)
    if message:
        st.success(message)
    for warning in st.session_state.pop("cal_sweep_warnings", []):
        st.warning(warning)

    with st.expander("Open a past calibration run", expanded=False):
        if not sweeps:
            st.info("No saved calibration sweeps were found.")
            return

        selected = st.selectbox(
            "Saved sweep",
            sweeps,
            format_func=lambda path: path.stem.replace("tube_calibration_", ""),
            key="cal_saved_sweep",
            help="Loads the captured points as a new working copy. The original CSV "
                 "is not changed when you choose another fit order.",
        )
        if st.button("Load selected sweep", key="cal_load_sweep"):
            try:
                warnings = service.load_sweep(selected, replace=True)
            except Exception as error:
                st.error(f"Could not load {selected.name}: {error}")
            else:
                st.session_state["cal_sweep_message"] = (
                    f"Loaded {selected.name}. Choose new R/X orders in Step 4, "
                    "then save it as a new calibration in Step 5."
                )
                st.session_state["cal_sweep_warnings"] = warnings
                st.rerun()


def _render_points_table(service):
    st.markdown("#### 3. Calibration points")
    _render_saved_sweeps(service)

    if not service.points:
        st.info("No points captured yet. Capture at least two at different pressures.")
        return

    frame = pd.DataFrame([
        {
            "CPAP (cmH2O)": p["cpap"],
            "Resistance": round(p["r_phase"], 3),
            "Reactance": round(p["x_phase"], 3),
            "Bias flow (L/s)": round(p["bias_flow"], 4),
            "Windows": p["n_windows"],
            "Quality": "OK" if p.get("all_valid", True) else "Check",
        }
        for p in service.points
    ])
    st.dataframe(frame, width="stretch", hide_index=True)

    remove_cols = st.columns([2, 1])
    with remove_cols[0]:
        to_remove = st.selectbox(
            "Remove a point",
            [p["cpap"] for p in service.points],
            format_func=lambda v: f"{v} cmH2O",
            key="cal_remove_value",
        )
    with remove_cols[1]:
        st.write("")
        if st.button("Remove", width="stretch", key="cal_remove"):
            service.remove_point(to_remove)
            st.rerun()

    if service.sweep_path:
        st.caption(f"Points saved to {service.sweep_path.name}")
    elif service.loaded_sources:
        st.caption(
            "Working from saved data: " + ", ".join(service.loaded_sources)
        )


def _render_fit(service):
    if len(service.points) < 2:
        return

    st.markdown("#### 4. Review the fit")

    order_cols = st.columns(2)
    default_fit_order = min(7, MAX_FIT_ORDER)
    st.session_state.setdefault("cal_r_degree", default_fit_order)
    st.session_state.setdefault("cal_x_degree", default_fit_order)
    with order_cols[0]:
        r_degree = st.selectbox(
            "Resistance fit order",
            list(range(1, MAX_FIT_ORDER + 1)),
            index=default_fit_order - 1,
            key="cal_r_degree",
            help="1 is a straight line. A higher order can follow a curved response, "
                 "but needs more points and can over-fit.",
        )
    with order_cols[1]:
        x_degree = st.selectbox(
            "Reactance fit order",
            list(range(1, MAX_FIT_ORDER + 1)),
            index=default_fit_order - 1,
            key="cal_x_degree",
            help="Reactance is noisier than resistance and usually supports a lower order.",
        )

    fit = service.fit(degree=r_degree, x_degree=x_degree)
    if fit is None:
        need = max(r_degree, x_degree) + 1
        st.warning(
            f"Need at least {need} points at different pressures for this fit order."
        )
        return

    metric_cols = st.columns(4)
    metric_cols[0].metric("Scale", f"{fit['scale']:.4f}")
    metric_cols[1].metric("K plumbing", f"{fit['k_plumbing']:.4f}")
    metric_cols[2].metric("Fit quality (R²)", f"{fit['r_squared']:.3f}")
    metric_cols[3].metric("Points", fit["n_points"])

    validation = fit.get("validation") or {}
    for error in validation.get("errors", []):
        st.error(error)
    for warning in validation.get("warnings", []):
        st.warning(warning)

    if fit["r_squared"] < 0.9:
        st.warning(
            f"The points do not sit close to the fitted line (R² = {fit['r_squared']:.3f}). "
            "Capture more pressures, or re-check the circuit for leaks, before saving."
        )

    # Leave-one-out error: R² always improves with order and cannot see over-fitting,
    # so this is the number that tells you a higher order is genuinely better.
    holdout = fit.get("r_holdout_rms")
    if holdout is not None and holdout == holdout:  # not NaN
        st.caption(
            f"Predicted error at an unmeasured pressure: ±{holdout:.3f} "
            f"(reactance ±{fit['x_holdout_rms']:.3f}). Lower is better — if raising "
            f"the fit order does not reduce this, the extra order is fitting noise."
        )

    _render_fit_chart(service, fit)

    st.caption(
        f"Reference load {REFERENCE_RESISTANCE} · excitation {EXCITATION_HZ:g} Hz · "
        f"phase correction {service.correction_deg:+.3f}°"
    )
    return fit


def _render_fit_chart(service, fit):
    """Both fitted quantities. Reactance is fitted and saved alongside resistance, so
    showing only resistance hides half of what the Save button writes."""
    import numpy as np

    q = [p["bias_flow"] for p in service.points]
    grid = np.linspace(min(q), max(q), 60)

    panels = (
        ("Resistance", "r_phase", "r_coeffs_asc", "r_squared"),
        ("Reactance", "x_phase", "x_coeffs_asc", "x_r_squared"),
    )
    chart_cols = st.columns(2, gap="medium")

    for column, (label, point_key, coeff_key, r2_key) in zip(chart_cols, panels):
        values = [p[point_key] for p in service.points]
        curve = np.polyval(fit[coeff_key][::-1], grid)

        figure = go.Figure()
        figure.add_trace(go.Scatter(
            x=q, y=values, mode="markers", name="Captured",
            marker={"size": 9},
        ))
        figure.add_trace(go.Scatter(
            x=grid, y=curve, mode="lines", name="Fit",
            line={"width": 2},
        ))
        figure.update_layout(
            height=280,
            margin={"l": 10, "r": 10, "t": 30, "b": 40},
            title={"text": f"{label}  (R² = {fit[r2_key]:.3f})", "font": {"size": 13}},
            xaxis_title="Bias flow (L/s)",
            yaxis_title=label,
            showlegend=True,
            legend={"orientation": "h", "y": -0.25},
        )
        with column:
            st.plotly_chart(
                figure,
                width="stretch",
                config={"displayModeBar": False},
                key=f"cal_fit_{point_key}",
            )


def _render_save(service, fit, switched=None):
    st.markdown("#### 5. Save")
    st.caption(
        "Save the reviewed point fit, then optionally store the phase angle, point "
        "calibration, or both as a time-of-day preset."
    )

    saved_message = st.session_state.pop("cal_save_message", None)
    if saved_message:
        st.success(saved_message)
        st.warning(
            "Restart the application before recording patients so every analysis "
            "module reloads the new calibration."
        )

    if service.load_warning:
        st.error(service.load_warning)
    if service.phase_stale_sweep:
        st.error(
            "These points were captured under an older phase angle. Load a sweep "
            "captured with the active phase calibration, or clear and recapture them."
        )

    if fit:
        validation = fit.get("validation") or {}
        override_phase_warning = False
        if service.phase_stale_sweep:
            override_phase_warning = st.checkbox(
                "I understand these points use an older phase angle and want to "
                "save this fit anyway",
                key="cal_override_phase_warning",
            )

        can_save_fit = (
            validation.get("can_save", True)
            and (not service.phase_stale_sweep or override_phase_warning)
            and not service.load_warning
        )

        # Clear the tick before creating the widget. Streamlit does not permit
        # modifying a widget key after the widget is instantiated.
        if st.session_state.pop("cal_save_done", False):
            st.session_state.pop("cal_confirm_save", None)

        confirm = st.checkbox(
            "I have checked the fit above and want to use it",
            key="cal_confirm_save",
            disabled=not can_save_fit,
        )
        if st.button(
            "Save point calibration",
            type="primary",
            disabled=not confirm or not can_save_fit,
        ):
            r_degree, x_degree = _fit_orders()
            try:
                path, saved = service.save_calibration(
                    degree=r_degree,
                    x_degree=x_degree,
                    allow_phase_override=override_phase_warning,
                )
            except Exception as error:
                st.error(f"Could not save: {error}")
            else:
                st.session_state["cal_save_message"] = (
                    f"Point calibration saved — scale {saved['scale']:.4f}, "
                    f"K plumbing {saved['k_plumbing']:.4f} (file {path.name})."
                )
                st.session_state["cal_save_done"] = True
                st.rerun()
    else:
        st.info(
            "A point calibration can be saved after Step 4 produces a valid fit. "
            "A saved phase angle can still be stored as a preset below."
        )

    st.divider()
    _render_presets(service, switched=switched)


def calibration_tab():
    service = get_calibration_service()

    st.write(
        "Calibrate the device against a known reference load. Run this when the "
        "tubing, flowhead or speaker has been changed — not for routine patient testing."
    )

    if not service.hardware_enabled:
        st.warning("Device access is switched off in the configuration file.")
        return

    # Apply a due preset before rendering the active values so the status panel and
    # the preset section always describe the same calibration.
    switched = service.apply_due_preset()
    _render_active_calibration(service)

    with st.expander("Instructions", expanded=False):
        st.caption(
            "**1. Fit the reference load**  \n"
            "Attach the reference load and run the **phase-angle calibration** first."
        )
        st.caption(
            "**2. Capture CPAP points**  \n"
            "Record each pressure point, waiting for the blower to fully settle "
            "before capturing."
        )
        st.caption(
            "**3. Review captured data**  \n"
            "Inspect the newly captured points or open a previous sweep if needed."
        )
        st.caption(
            "**4. Select R/X fit orders**  \n"
            "Choose the resistance and reactance fit orders and review the fit quality."
        )
        st.caption(
            "**5. Save calibration**  \n"
            "Save the point fit, and store calibration presets when required."
        )

    busy_reason = None
    try:
        if get_recording_service().is_recording:
            busy_reason = "A patient recording is in progress. Stop it before calibrating."
    except Exception:
        pass

    # Purpose-tagged statuses drive the fragments; the untagged one answers "is the
    # device busy at all", so a phase run blocks a sweep capture and vice versa.
    status = service.capture_status("sweep")
    phase_status = service.capture_status("phase")
    any_status = service.capture_status()
    capturing = bool(any_status and any_status["state"] in ("settling", "collecting"))
    if capturing:
        busy_reason = "A capture is already running."

    # Resolve the port for the calibration controls, but show the device status
    # with the other page status information at the bottom.
    port = _render_port_selector(service, disabled=capturing, render=False)

    st.divider()
    _render_phase_section(service, busy_reason, port, phase_status)

    st.divider()
    _render_capture_controls(service, busy_reason, port)
    # Only mount the auto-refreshing fragment while THIS tab has a capture. Both tabs
    # render on every run, so mounting it unconditionally left two 1 Hz timers
    # running forever and orphaned them on each full rerun.
    if status is not None:
        _render_capture_progress(service)

    st.divider()
    _render_points_table(service)

    fit = None
    if service.points:
        st.divider()
        fit = _render_fit(service)

    st.divider()
    _render_save(service, fit, switched=switched)

    if service.points:
        st.divider()
        if st.button("Clear all captured points"):
            service.clear()
            st.rerun()

    st.divider()
    st.markdown("**Device Connection Status**")
    _render_port_selector(service, disabled=capturing)
