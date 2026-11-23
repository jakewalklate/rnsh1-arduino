import math

import streamlit as st

from backend.database.storage import (
    delete_test,
    get_measurement_events,
    get_measurement_results,
    get_test,
    get_test_measurements,
    save_scale_only_results,
)
from backend.processing.FOTAcceptance import FLOOR, VALID_FRACTION_FLOOR
from backend.processing.FOTImpedance import EXCITATION_HZ
from backend.processing.FOTPipeline import process_measurement_csv
from frontend.navigation import goto

# Fields that genuinely differ between the two calibration bases. Everything not listed
# here is shared: dRrs and dXrs cancel the additive term between the two phases of a
# breath, and breath counts, quality and tidal volume are decided by QC which runs
# upstream of the correction. See backend.database.storage.SCALE_ONLY_RESULT_COLUMNS.
BASIS_DEPENDENT_FIELDS = (
    "resistance_R5",
    "reactance_X5",
    "impedance_Z5",
    "Rrs_insp",
    "Rrs_exp",
    "Xrs_insp",
    "Xrs_exp",
    "cov_r5",
)

# Magnitude of dXrs that flags expiratory flow limitation. Henry's bench value, carried
# over with his caveat intact: DISPLAY-ONLY and UNVERIFIED, so the UI presents it as an
# indicator to look at, never as a diagnosis.
#
# Compared as a MAGNITUDE because dXrs is signed expiration minus inspiration. Under
# flow limitation expiratory reactance falls, so EFL is a large NEGATIVE dXrs.
EFL_DISPLAY_THRESHOLD = 2.8

# Both within-breath differences are EXPIRATION MINUS INSPIRATION. Shown in the metric
# labels rather than left to a tooltip, because a delta whose direction is not on the
# face of the number is the easiest thing on this screen to misread. The hospital's FOT
# device signs dXrs the same way.
DELTA_DIRECTION_LABEL = "exp − insp"

# Between-breath CoV(R5) regarded as reproducible. This is the published ERS/ATS
# guideline figure for adults, NOT a threshold the engine enforces anywhere — shown as
# a reference next to the number rather than used to pass or fail a measurement.
COV_R5_GUIDELINE = 0.10


def bias_toggle_key(measurement_id):
    """Per measurement, so two measurements in the same test can be read on different
    bases side by side. Session-only by design: which basis you are looking at is a view
    preference, not part of the saved record."""
    return f"bias_correction_on_{measurement_id}"


def ensure_scale_only_results(measurement_id, results):
    """Backfill the bias-off copy for a measurement saved before it was stored.

    `status_nobias` is NULL exactly for those rows. Recomputing needs the raw CSV, which
    is retained, so this is a straight reprocess and then a write, done once. If the CSV
    has been removed the backfill fails and the caller falls back to showing the
    bias-corrected numbers with a note rather than a grid of dashes."""
    scale_only = (results or {}).get("scale_only") or {}
    if scale_only.get("status") is not None:
        return scale_only

    try:
        computed = process_measurement_csv(measurement_id, apply_bias_polynomial=False)
    except Exception:
        return {}

    if not computed or computed.get("status") is None:
        return {}

    save_scale_only_results(measurement_id, computed)
    return {field: computed.get(field) for field in BASIS_DEPENDENT_FIELDS} | {
        "status": computed.get("status")
    }


def results_on_basis(results, bias_correction_on, scale_only):
    """Return the results dict with the basis-dependent fields swapped for the selected
    basis. Everything else passes through untouched, which is what keeps ΔRrs, ΔXrs,
    breath counts and quality identical between the two views."""
    if bias_correction_on or not scale_only:
        return results

    swapped = dict(results)
    for field in BASIS_DEPENDENT_FIELDS:
        swapped[field] = scale_only.get(field)
    return swapped


def metric_value(value, suffix=""):
    if value is None:
        return "-"

    if isinstance(value, float):
        return f"{value:.3f}{suffix}"

    return f"{value}{suffix}"


def within_breath_value(value, decimals=2, signed=False):
    """Within-breath numbers read as a pair, so they need a fixed width and an explicit
    sign on the differences — '+1.38' and '-0.98' are the point of dRrs and dXrs."""
    if value is None:
        return "—"
    return f"{value:+.{decimals}f}" if signed else f"{value:.{decimals}f}"


def has_within_breath(results):
    """Whether this measurement was analysed breath by breath. Takes with too few clean
    breaths fall back to a whole-record R5/X5 and have no insp/exp split to show."""
    if not results:
        return False
    return results.get("Rrs_insp") is not None and results.get("Rrs_exp") is not None


def phase_delta(results, exp_key, insp_key, stored_key):
    """Expiration minus inspiration, derived from the two phase values rather than read
    from the stored delta.

    Measurements recorded before dXrs changed direction still hold the old
    inspiration-minus-expiration value in the database. Deriving here means one
    convention is applied to old and new records alike, with no migration and no risk of
    a half-converted table. Falls back to the stored value only when a phase value is
    missing, which is the pre-within-breath case where both are absent anyway."""
    expiration = results.get(exp_key)
    inspiration = results.get(insp_key)
    if expiration is None or inspiration is None:
        return results.get(stored_key)
    return expiration - inspiration


def delta_rrs(results):
    return phase_delta(results, "Rrs_exp", "Rrs_insp", "dRrs")


def delta_xrs(results):
    return phase_delta(results, "Xrs_exp", "Xrs_insp", "dXrs")


def efl_verdict(dxrs):
    """dXrs is expiration minus inspiration. Flow limitation drives expiratory reactance
    down, so the indicator is a large NEGATIVE value."""
    if dxrs is None:
        return "—", None
    if dxrs <= -EFL_DISPLAY_THRESHOLD:
        return "Indicated", "warn"
    return "Not indicated", "ok"


def expiratory_load_verdict(drrs):
    """dRrs is expiration minus inspiration, so a positive value means expiration is
    the harder phase."""
    if drrs is None:
        return "—", None
    if drrs > 0:
        return "Expiration harder than inspiration", "warn"
    if drrs < 0:
        return "Inspiration harder than expiration", None
    return "No difference between phases", "ok"


def quality_verdict(results):
    """Three-way verdict. FLOOR clean breaths is the engine's sufficiency rule (the ERS
    "at least three breaths remaining" count), so below it the take really is
    unusable. VALID_FRACTION_FLOOR is NOT a gate anywhere any more — here it only
    separates "Good" from "Usable", i.e. it tells the operator the acquisition was
    noisy without withholding its result. CoV is reported alongside but does not gate
    the verdict."""
    clean = results.get("clean_breath_count")
    total = results.get("breath_count")
    if clean is None or not total:
        return "—", None

    fraction = clean / total
    if clean >= FLOOR and fraction >= VALID_FRACTION_FLOOR:
        return "Good", "ok"
    if clean >= FLOOR:
        return f"Usable — only {fraction * 100:.0f}% of breaths were clean", "warn"
    return f"Insufficient — {clean} clean breath(s), {FLOOR} needed", "bad"


def format_duration_seconds(seconds):
    """One decimal place. The DB may hold an int, a float or a string, and a duration of
    0 is a real value — `or '-'` would print a dash for it."""
    if seconds is None:
        return "-"
    try:
        return f"{float(seconds):.1f}"
    except (TypeError, ValueError):
        return "-"


def format_event_time(seconds):
    if seconds is None:
        seconds = 0

    total_seconds = int(seconds)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def cpap_display(cpap_pressure):
    """NULL means the measurement was recorded before the pressure was stored per
    measurement, which is not the same as 0 cmH2O and must not render as it."""
    if cpap_pressure is None:
        return "CPAP not recorded"
    return f"CPAP {float(cpap_pressure):.1f} cmH2O"


def render_within_breath(results, bias_correction_on=True, cpap_pressure=None):
    """Henry's within-breath layout: the insp/exp pair and its difference on one row for
    resistance, the same for reactance, then reproducibility and the interpretation.

    Layout is identical on both calibration bases by design, so that toggling swaps the
    numbers in place and nothing moves under the reader's eye."""
    clean = results.get("clean_breath_count")
    total = results.get("breath_count")

    st.markdown("#### Within-breath analysis")
    accepted = (
        f"Averaged over {clean} accepted breath{'s' if clean != 1 else ''}"
        if clean is not None else "Averaged over the accepted breaths"
    )
    basis = "bias-corrected" if bias_correction_on else "bias-flow correction OFF"
    st.caption(
        f"{accepted} · {cpap_display(cpap_pressure)} · {basis} · {EXCITATION_HZ:g} Hz"
    )

    r_cols = st.columns(3)
    r_cols[0].metric("Rrs inspiration", within_breath_value(results.get("Rrs_insp")))
    r_cols[1].metric("Rrs expiration", within_breath_value(results.get("Rrs_exp")))
    r_cols[2].metric(
        f"ΔRrs ({DELTA_DIRECTION_LABEL})",
        within_breath_value(delta_rrs(results), signed=True),
        help="Expiration minus inspiration. Positive means expiration is the harder "
             "phase.",
    )

    x_cols = st.columns(3)
    x_cols[0].metric("Xrs inspiration", within_breath_value(results.get("Xrs_insp")))
    x_cols[1].metric("Xrs expiration", within_breath_value(results.get("Xrs_exp")))
    x_cols[2].metric(
        f"ΔXrs ({DELTA_DIRECTION_LABEL})",
        within_breath_value(delta_xrs(results), signed=True),
        help="Expiration minus inspiration, the same direction as ΔRrs and the same "
             "sign convention as the hospital FOT device. Flow limitation drives "
             "expiratory reactance down, so a large NEGATIVE value is the indicator.",
    )

    summary_cols = st.columns(3)
    cov = results.get("cov_r5")
    summary_cols[0].metric(
        "Breath-to-breath variation",
        "—" if cov is None else f"{cov * 100:.1f}%",
        help=f"CoV of R5 between breaths. Guideline for good reproducibility is "
             f"under {COV_R5_GUIDELINE * 100:g}%.",
    )
    summary_cols[1].metric(
        "Accepted breaths",
        "—" if clean is None else (f"{clean} of {total}" if total else str(clean)),
    )
    summary_cols[2].metric(
        r"$V_T$",
        metric_value(results.get("tidal_volume_l"), " L"),
    )

    st.divider()
    render_interpretation(results, bias_correction_on)


def reproducibility_verdict(cov):
    """CoV against the published guideline. This is the one interpretation line that
    genuinely moves with the calibration basis, because the bias polynomial is
    re-evaluated on each breath's own mean flow and injects its own spread.

    A NEGATIVE CoV is rejected rather than compared. It arises when the mean R5 across
    breaths is itself negative (the correction can over-subtract a take into negative
    resistance), which makes sd/mean negative and would otherwise sail under the
    guideline and be reported as excellent reproducibility. A negative resistance is
    not a reproducible measurement, it is an invalid one."""
    if cov is None or not math.isfinite(cov):
        return "—", None
    if cov < 0:
        return "Not meaningful — mean resistance is negative", "bad"
    if cov <= COV_R5_GUIDELINE:
        return f"Within guideline ({cov * 100:.1f}% ≤ {COV_R5_GUIDELINE * 100:g}%)", "ok"
    return f"Above guideline ({cov * 100:.1f}% > {COV_R5_GUIDELINE * 100:g}%)", "warn"


def render_interpretation(results, bias_correction_on=True):
    """Plain-language reading of the numbers above. Deliberately worded as indicators,
    not diagnoses."""
    icons = {"ok": ":green[●]", "warn": ":orange[●]", "bad": ":red[●]", None: ":gray[●]"}

    efl_text, efl_state = efl_verdict(delta_xrs(results))
    load_text, load_state = expiratory_load_verdict(delta_rrs(results))
    quality_text, quality_state = quality_verdict(results)
    repro_text, repro_state = reproducibility_verdict(results.get("cov_r5"))

    for label, text, state in (
        ("Expiratory flow limitation", efl_text, efl_state),
        ("Expiratory load", load_text, load_state),
        ("Reproducibility", repro_text, repro_state),
        ("Measurement quality", quality_text, quality_state),
    ):
        row = st.columns([1, 2])
        row[0].markdown(f"{icons[state]} **{label}**")
        row[1].markdown(text)

    st.caption(
        f"Both differences are expiration minus inspiration. Flow-limitation indicator "
        f"is ΔXrs at or below −{EFL_DISPLAY_THRESHOLD:g}. It is a display-only "
        "threshold and has not been clinically validated on this device — treat it as "
        "a prompt to look at the trace, not as a finding."
    )

    # Said explicitly because it looks like a bug otherwise: toggling changes the
    # absolutes but leaves these four alone, and a reader who spots that without an
    # explanation will reasonably assume the toggle is broken.
    st.caption(
        "ΔRrs, ΔXrs, accepted breaths and measurement quality are identical on both "
        "settings. The two phases of a breath share the same bias flow so the "
        "correction cancels in the differences, and breath acceptance is decided "
        "before the correction is applied."
    )

    if not bias_correction_on:
        st.caption(
            ":orange[●] Bias-flow correction is OFF. Gain calibration (SCALE) is still "
            "applied; the flow-indexed polynomial is not. This basis is appropriate "
            "when there is no steady bias flow — a sealed mask or a closed circuit — "
            "where the measured mean flow is drift rather than real flow."
        )


@st.dialog("Delete Test", dismissible=False)
def confirm_delete_test_dialog(test_id, patient_id):
    st.warning(
        f"Deleting Test {test_id} will permanently remove the test, "
        "all associated measurements, and all saved raw sample data."
    )

    col1, col2 = st.columns(2)

    with col1:
        if st.button(
            "Delete Test",
            type="primary",
            key=f"confirm_delete_{test_id}",
            width="stretch",
        ):
            deleted_patient_id = delete_test(test_id)

            st.session_state.show_delete_test_dialog = False
            st.session_state.current_test_id = None
            st.session_state.current_measurement_id = None
            st.session_state.recording = False
            st.session_state.show_start_dialog = False
            st.session_state.show_config_dialog = False
            st.session_state.show_stop_summary = False
            st.session_state.selected_patient_id = deleted_patient_id or patient_id

            goto("patient_view")

    with col2:
        if st.button(
            "Cancel",
            key=f"cancel_delete_{test_id}",
            width="stretch",
        ):
            st.session_state.show_delete_test_dialog = False
            st.rerun()


def test_view():
    if "show_delete_test_dialog" not in st.session_state:
        st.session_state.show_delete_test_dialog = False

    test = get_test(st.session_state.current_test_id)

    if test is None:
        st.error("Test not found.")
        if st.button("Back to Patient"):
            goto("patient_view")
        return

    test_id = test[0]
    patient_id = test[1]
    created_datetime = test[2]
    completed_datetime = test[3]
    frequency_hz = test[4]
    sample_rate_hz = test[5]
    duration_seconds = test[6]
    recording_sessions = test[7]
    status = test[8]

    st.space("small")
    with st.container(horizontal=True):
        with st.container():
            st.title(test_id)
            st.caption(patient_id)
        
        st.space("stretch")
        
        col1, col2 = st.columns(2)

        with col1:
            st.space("small")

            if st.button("New Measurement", type="primary", width="stretch"):
                st.session_state.current_test_id = test_id
                st.session_state.selected_patient_id = patient_id
                st.session_state.show_start_dialog = True
                st.session_state.show_config_dialog = False
                st.session_state.show_stop_summary = False
                st.session_state.delete_empty_test_on_cancel = False
                st.session_state.recording = False
                st.session_state.current_measurement_id = None
                goto("start_test")

        with col2:
            st.space("small")

            if st.button("Return to Patient", width="stretch"):
                st.session_state.selected_patient_id = patient_id
                goto("patient_view")


    st.subheader("Results")
    measurements = get_test_measurements(test_id)

    if measurements:
        tab_labels = [f"Measurement {m[2]}" for m in measurements]
        selected_measurement_id = st.session_state.get("selected_results_measurement_id")
        default_tab = None

        for label, measurement in zip(tab_labels, measurements):
            if measurement[0] == selected_measurement_id:
                default_tab = label
                break

        tabs = st.tabs(
            tab_labels,
            default=default_tab,
            key=f"results_tabs_{test_id}_{selected_measurement_id or 'default'}",
        )

        for tab, measurement in zip(tabs, measurements):
            measurement_id = measurement[0]
            # Index 7 is cpap_pressure_cmH2O, NULL for measurements recorded before it
            # was stored per measurement.
            cpap_pressure = measurement[7] if len(measurement) > 7 else None
            # measurement_number = measurement[2]
            # started_datetime = measurement[3]
            # ended_datetime = measurement[4]
            # measurement_duration = measurement[5]
            # measurement_status = measurement[6]

            with tab:
                results = get_measurement_results(measurement_id)

                # Toggle sits on the right of the measurement header, on the same row as
                # the measurement id. Streamlit cannot place a widget inside the tab
                # strip itself, so this is the closest position to what it labels.
                header = st.container(horizontal=True)
                with header:
                    st.caption(f"{measurement_id} · {cpap_display(cpap_pressure)}")
                    st.space("stretch")

                toggle_key = bias_toggle_key(measurement_id)
                if toggle_key not in st.session_state:
                    st.session_state[toggle_key] = True

                scale_only = {}
                if results:
                    scale_only = ensure_scale_only_results(measurement_id, results)

                with header:
                    bias_correction_on = st.toggle(
                        "Bias-flow correction",
                        key=toggle_key,
                        disabled=not scale_only,
                        help="On applies the full bench calibration (advance "
                             "calibration). Off keeps the gain calibration but drops "
                             "the flow-indexed polynomial.",
                    )

                if results and not scale_only:
                    st.caption(
                        ":gray[●] Bias-flow-correction-off values are unavailable for "
                        "this measurement — its raw recording could not be reprocessed."
                    )

                results = results_on_basis(results, bias_correction_on, scale_only)

                if not results:
                    st.info(
                        "No processing result has been saved for this measurement yet."
                    )
                elif has_within_breath(results):
                    render_within_breath(results, bias_correction_on, cpap_pressure)
                else:
                    # Too few clean breaths to split the breath into its two phases, so
                    # the engine reported a whole-record average instead. Show that, and
                    # say why the within-breath panel is missing rather than showing
                    # a grid of dashes.
                    st.warning(
                        "Not enough clean breaths for a within-breath analysis. "
                        "Showing the whole-record average instead."
                    )
                    st.caption(
                        "bias-corrected" if bias_correction_on
                        else "bias-flow correction OFF"
                    )
                    fallback_cols = st.columns(3)
                    fallback_cols[0].metric(
                        r"$R_5$", metric_value(results["resistance_R5"])
                    )
                    fallback_cols[1].metric(
                        r"$X_5$", metric_value(results["reactance_X5"])
                    )
                    fallback_cols[2].metric(
                        r"$V_T$", metric_value(results["tidal_volume_l"], " L")
                    )

                events = get_measurement_events(measurement_id)

                if events:
                    st.markdown("#### Events")
                    st.dataframe(
                        [
                            {
                                "Time": format_event_time(event["elapsed_seconds"]),
                                "Note": event["note"],
                            }
                            for event in events
                        ],
                        hide_index=True,
                        width="stretch",
                    )
    else:
        st.info("No saved measurements recorded for this test.")

    with st.expander("Test Details", expanded=False):
        st.write(f"**Created:** {created_datetime}")
        st.write(f"**Completed:** {completed_datetime or '-'}")
        st.write(f"**Frequency:** {frequency_hz} Hz")
        st.write(f"**Sample Rate:** {sample_rate_hz} Hz")
        st.write(f"**Valid Measurements:** {recording_sessions}")
        st.write(f"**Total Duration:** {format_duration_seconds(duration_seconds)} seconds")

        # Per measurement rather than one test-level figure. The test parameter row is
        # overwritten on every reconfigure, so it describes the last setting used and
        # not the pressure any given measurement was taken at.
        st.write("**CPAP Pressure:**")
        if measurements:
            for measurement in measurements:
                pressure = measurement[7] if len(measurement) > 7 else None
                st.write(
                    f"&nbsp;&nbsp;&nbsp;&nbsp;Measurement {measurement[2]} "
                    f"({measurement[0]}) — {cpap_display(pressure)}",
                    unsafe_allow_html=True,
                )
            if all(
                (m[7] if len(m) > 7 else None) is None for m in measurements
            ):
                st.caption(
                    "Recorded before CPAP pressure was stored per measurement. The "
                    "test-level setting is not shown here because it holds only the "
                    "most recent value and can disagree with what a given measurement "
                    "was actually taken at."
                )
        else:
            st.write("&nbsp;&nbsp;&nbsp;&nbsp;-", unsafe_allow_html=True)
        
    st.divider()

    if st.button(
        "Delete Test",
        type="primary",
        key=f"open_delete_{test_id}"
    ):
        st.session_state.show_delete_test_dialog = True
        st.rerun()

    if st.session_state.show_delete_test_dialog:
        confirm_delete_test_dialog(test_id, patient_id)
    
    

