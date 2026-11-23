"""Session state and device access for the tube-calibration sweep.

Owns its own serial connection because calibration never runs at the same time as a
patient recording — only one program can hold the COM port, so the caller must check
`RecordingService.is_recording` before starting.
"""

from pathlib import Path
from time import sleep

from backend.config import (
    ARDUINO_BAUDRATE,
    ARDUINO_CONNECT_DELAY_SECONDS,
    ARDUINO_ENABLED,
    ARDUINO_PORT,
    ARDUINO_RECONNECT_DELAY_SECONDS,
    ARDUINO_TIMEOUT_SECONDS,
    RAW_MEASUREMENT_DATA_DIR,
)
from backend.device.calibration_recorder import CalibrationPointRecorder
from backend.device.gain_phase_sweep import GainPhaseSweep
from backend.device.serial_device import ArduinoSerialClient, list_serial_ports
from backend.processing.FOTImpedance import (
    EXCITATION_HZ,
    bias_calibration_path_for,
    load_bias_calibration_for,
    load_calibration,
    save_bias_calibration,
    save_calibration,
)
from backend.processing.FOTGainPhaseCalibration import (
    FREQUENCIES_HZ,
    current_calibration,
    save_results as save_gain_phase_results,
)
from backend.processing.FOTCalibrationPresets import (
    PERIODS,
    default_store as default_preset_store,
    period_for,
)
from backend.processing.FOTPhaseCalibration import (
    CALIBRATION_SETTLING_SECONDS,
    active_correction_deg,
    evaluate as evaluate_phase_calibration,
    phase_calibration_path_for,
    usable_blocks_for_calibration,
)
from backend.processing.FOTSystemCheck import (
    CHECK_SECONDS,
    CHECK_SETTLING_BLOCKS,
    append_check_log,
    default_check_log_path,
    score_point,
    verdict,
)
from backend.processing.FOTTubeCalibration import (
    build_payload,
    compute_fit,
    default_sweep_path,
    load_sweep_csv,
    write_sweep_csv,
)


CALIBRATION_DATA_DIR = Path(RAW_MEASUREMENT_DATA_DIR).parent / "calibration"
CALIBRATION_ARCHIVE_DIR = CALIBRATION_DATA_DIR / "archive"


class CalibrationService:
    def __init__(self, hardware_enabled=ARDUINO_ENABLED):
        self.hardware_enabled = hardware_enabled
        self.client = None
        self.port = ARDUINO_PORT      # starting suggestion; the UI can override it
        self.recorder = None
        self.points = []
        self.loaded_sources = []
        self.load_warning = None
        self.sweep_path = None
        # Daily system-check session state (separate from the calibration sweep).
        self.check_points = []
        self.check_number = 0
        self.check_log_path = None
        self._check_reference_r = 0.0
        self._check_cpap_note = ""
        # Phase-angle calibration state (runs before the bias sweep).
        self.phase_result = None
        self.phase_stale_sweep = False
        # Gain & phase calibration: one result per frequency, kept until saved or
        # discarded.
        self.gain_phase_results = None
        # Which tab owns the running capture: "sweep", "check", "phase" or
        # "gain_phase". Every tab
        # renders on every script run and they share this one service, so without it
        # whichever renders first would commit another tab's finished capture.
        self.capture_purpose = None

    # ------------------------------------------------------------------ device

    @staticmethod
    def available_ports():
        return list_serial_ports()

    def _ensure_client(self, port=None):
        """Open the chosen port, reconnecting if the selection changed."""
        if port and port != self.port:
            self.disconnect()
            self.port = port
        if not self.port:
            raise RuntimeError("No serial port selected.")
        if self.client is None:
            self.client = ArduinoSerialClient(
                port=self.port,
                baudrate=ARDUINO_BAUDRATE,
                timeout=ARDUINO_TIMEOUT_SECONDS,
                connect_delay_seconds=ARDUINO_CONNECT_DELAY_SECONDS,
            )
        if not self.client.is_connected:
            self.client.connect()
        return self.client

    def _close_client(self, send_stop=True):
        """Stop streaming and close the current Windows serial handle."""
        client = self.client
        self.client = None
        if client is None:
            return
        try:
            if send_stop and client.is_connected:
                try:
                    client.stop_streaming()
                except Exception:
                    # A stale handle may reject STOP, but it must still be closed.
                    pass
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    def _stop_streaming(self):
        """Park the speaker between points without reopening the USB connection."""
        if self.client is None or not self.client.is_connected:
            return
        try:
            self.client.stop_streaming()
        except Exception:
            # A failed STOP means the handle cannot be trusted or reused.
            self._close_client(send_stop=False)

    def disconnect(self):
        recorder = self.recorder
        self.recorder = None
        self.capture_purpose = None
        if recorder is not None:
            recorder.cancel()
        self._close_client()

    def release_device(self):
        """Free the COM port for another tab. Only one program can hold a port, and
        this service is a cached singleton that outlives the tab, so a tab that has
        finished must hand the port back or the next one fails with a bare
        PermissionError from pyserial."""
        self.disconnect()

    @property
    def correction_deg(self):
        """Phase-rotation angle in force. The sweep's R/X are rotated by this, so it
        is recorded with the calibration: re-fitting under a different angle would
        produce wrong constants. Same loader the patient path uses — one definition,
        so the tabs and the recordings can never disagree about the angle."""
        return active_correction_deg()

    # ----------------------------------------------------------------- capture

    def _start_recorder(self, purpose, recorder_factory, port=None):
        """Start a capture, retrying once with a new handle after an I/O failure."""
        if self.recorder is not None:
            state = self.recorder.snapshot()["state"]
            if state in ("settling", "collecting"):
                raise RuntimeError("A calibration capture is already running.")
            self.cancel_capture()
        first_error = None

        for attempt in range(2):
            recorder = None
            try:
                client = self._ensure_client(port)
                recorder = recorder_factory(client)
                self.recorder = recorder
                self.capture_purpose = purpose
                recorder.start()
                return recorder
            except Exception as error:
                if first_error is None:
                    first_error = error
                if recorder is not None:
                    recorder.cancel()
                self.recorder = None
                self.capture_purpose = None
                self._close_client()
                if attempt == 0:
                    sleep(ARDUINO_RECONNECT_DELAY_SECONDS)
                    continue
                raise RuntimeError(
                    "The Arduino connection failed after an automatic reconnect. "
                    f"Last error: {error}"
                ) from first_error

    def start_capture(self, cpap_value, repeats, port=None):
        if not self.hardware_enabled:
            raise RuntimeError("Arduino access is disabled in backend/config.py.")
        return self._start_recorder(
            "sweep",
            lambda client: CalibrationPointRecorder(
                client,
                cpap_value=cpap_value,
                repeats=repeats,
                correction_deg=self.correction_deg,
            ),
            port=port,
        )

    def cancel_capture(self):
        recorder = self.recorder
        self.recorder = None
        self.capture_purpose = None
        if recorder is not None:
            recorder.cancel()
        self._stop_streaming()

    def capture_status(self, purpose=None):
        """Snapshot of the running capture. Pass a purpose to get it only when this
        tab owns it, so the other tab neither polls nor commits it."""
        if self.recorder is None:
            return None
        if purpose is not None and self.capture_purpose != purpose:
            return None
        snapshot = self.recorder.snapshot()
        if snapshot["state"] == "error":
            self._close_client()
        return snapshot

    def commit_captured_point(self):
        """Move a finished capture into the sweep. One CPAP value keeps only its most
        recent point, so a recapture replaces its predecessor."""
        if self.recorder is None or self.capture_purpose != "sweep":
            return None
        snapshot = self.recorder.snapshot()
        if snapshot["state"] != "done" or not snapshot["point"]:
            return None
        point = snapshot["point"]
        self.points = [p for p in self.points if p["cpap"] != point["cpap"]]
        self.points.append(point)
        self.points.sort(key=lambda p: p["cpap"])
        recorder = self.recorder
        self.recorder = None
        self.capture_purpose = None
        recorder.cancel()
        self._stop_streaming()
        self.persist_sweep()
        return point

    def remove_point(self, cpap_value):
        self.points = [p for p in self.points if p["cpap"] != cpap_value]
        if self.points:
            self.persist_sweep()

    def clear(self):
        self.cancel_capture()
        self.points = []
        self.loaded_sources = []
        self.load_warning = None
        self.sweep_path = None
        self.phase_stale_sweep = False

    # -------------------------------------------------------------- fit / save

    def fit(self, degree=1, x_degree=None):
        return compute_fit(self.points, degree=degree, x_degree=x_degree)

    def persist_sweep(self):
        """Write the raw points to CSV after every capture, so a sweep is never lost
        if the fit is not saved."""
        if not self.points:
            return None
        if self.sweep_path is None:
            self.sweep_path = default_sweep_path(CALIBRATION_DATA_DIR)
        return write_sweep_csv(self.sweep_path, self.points, self.correction_deg)

    @staticmethod
    def available_sweeps():
        CALIBRATION_DATA_DIR.mkdir(parents=True, exist_ok=True)
        return sorted(
            CALIBRATION_DATA_DIR.glob("tube_calibration_*.csv"),
            key=lambda path: path.name,
            reverse=True,
        )

    def load_sweep(self, path, replace=False):
        path = Path(path)
        points, angles, warnings = load_sweep_csv(path)
        if replace:
            self.points = []
            self.loaded_sources = []
            self.load_warning = None
            self.sweep_path = None
            self.phase_stale_sweep = False

        merged = {p["cpap"]: p for p in self.points}
        for point in points:
            merged[point["cpap"]] = point  # file wins, matching live recapture
        self.points = sorted(merged.values(), key=lambda p: p["cpap"])
        if path.name not in self.loaded_sources:
            self.loaded_sources.append(path.name)

        # R/X in the file are ALREADY phase-rotated, so a different angle invalidates
        # the re-fit.
        unique = sorted({round(a, 3) for a in angles})
        live = round(self.correction_deg, 3)
        if unique and any(abs(a - live) > 0.01 for a in unique):
            self.load_warning = (
                f"{path.name} was captured at {unique[0]:+.3f} deg but the phase "
                f"calibration now loaded is {live:+.3f} deg. Re-fitting across a "
                f"phase change gives wrong constants."
            )
            self.phase_stale_sweep = True
            warnings.append(self.load_warning)
        return warnings

    def save_calibration(self, degree=1, x_degree=None, allow_phase_override=False):
        """Write the fitted constants to the per-frequency JSON the engine loads, then
        apply them in this process so the change takes effect without a restart."""
        fit = self.fit(degree=degree, x_degree=x_degree)
        if fit is None:
            raise ValueError(
                "Not enough points spanning a range of bias flows for this fit order."
            )
        if self.phase_stale_sweep and not allow_phase_override:
            raise ValueError(
                "These points were captured using an older phase angle. Clear or "
                "reload a matching sweep before saving."
            )
        validation = fit.get("validation") or {}
        if not validation.get("can_save", True):
            raise ValueError(" ".join(validation.get("errors") or ["Fit is invalid."]))
        sweep_path = self.persist_sweep()
        payload = build_payload(
            fit,
            correction_deg=self.correction_deg,
            phase_calibration_file=phase_calibration_path_for().name,
            source_csv=sweep_path.name if sweep_path else "",
            loaded_sources=self.loaded_sources or None,
        )
        if self.load_warning:
            payload["load_warning"] = self.load_warning
        if self.phase_stale_sweep:
            payload["phase_warning_overridden"] = True
            payload["phase_warning"] = (
                "Saved with an operator-confirmed older phase-angle override."
            )
        path = save_bias_calibration(EXCITATION_HZ, payload)
        # Takes effect immediately everywhere that reads the module globals at call
        # time (bias_flow_corrected -> the Check and Calibrate tabs). NOT everywhere:
        # FOTBreath binds BIAS_CORR_SCALE by value at import, and the patient-analysis
        # path (FOTPipeline -> phase_resolved_breaths, apply_scale=True) uses that
        # snapshot. That is Henry's engine as written and is deliberately not forked
        # here, so the app must be restarted for a new calibration to reach patient
        # analysis. The caller surfaces this.
        load_bias_calibration_for(EXCITATION_HZ)
        return path, fit

    # -------------------------------------------------- phase-angle calibration

    def start_phase_calibration(self, port=None):
        """Capture the reference resistor for CALIBRATION_SECONDS. The settling
        blocks are discarded by the recorder, leaving exactly the blocks Henry's
        bench tool keeps. Raw blocks are retained because the angle comes from the
        UNROTATED phasor."""
        if not self.hardware_enabled:
            raise RuntimeError("Arduino access is disabled in backend/config.py.")
        return self._start_recorder(
            "phase",
            lambda client: CalibrationPointRecorder(
                client,
                cpap_value="-",
                repeats=1,
                # Carried for the ImpedanceResult only; the saved angle and the RAW
                # tier below both ignore it.
                correction_deg=self.correction_deg,
                settle_blocks=CALIBRATION_SETTLING_SECONDS,
                window_blocks=usable_blocks_for_calibration(),
                # RAW: this capture solves for the rotation angle, so the operator
                # must watch the same unrotated signal the angle is derived from.
                # Under a corrected tier the readout moved with the OLD angle and the
                # old bias constants, neither of which this step is measuring.
                tier="raw",
                retain_blocks=True,
            ),
            port=port,
        )

    def commit_phase_calibration(self):
        """Score the capture and write the phase file, pass or fail — see the note in
        FOTPhaseCalibration. Returns the result dict."""
        if self.recorder is None or self.capture_purpose != "phase":
            return None
        snapshot = self.recorder.snapshot()
        if snapshot["state"] != "done":
            return None
        blocks = self.recorder.collected_blocks
        if not blocks:
            return None

        previous = load_calibration(phase_calibration_path_for())
        result = evaluate_phase_calibration(
            blocks,
            previous=previous,
            excitation_hz=EXCITATION_HZ,
            settling_blocks=CALIBRATION_SETTLING_SECONDS,
        )
        save_calibration(phase_calibration_path_for(), result)
        if not result["calibration_pass"]:
            # Duplicate copy for the record, as the bench tool keeps.
            save_calibration(
                phase_calibration_path_for().with_name(
                    f"fot_phase_calibration_{EXCITATION_HZ:g}hz_failed.json"
                ),
                result,
            )

        # Any sweep points already captured were rotated by the OLD angle, so
        # re-fitting across the change would produce wrong bias constants.
        if self.points and previous is not None:
            moved = abs(
                result["correction_angle_deg"]
                - float(previous["correction_angle_deg"])
            )
            if moved > 0.01:
                self.phase_stale_sweep = True

        self.phase_result = result
        recorder = self.recorder
        self.recorder = None
        self.capture_purpose = None
        recorder.cancel()
        self._stop_streaming()
        return result

    def clear_phase_result(self):
        self.phase_result = None

    # ------------------------------------------------ gain & phase calibration

    def start_gain_phase(self, reference_resistance, port=None, frequencies=FREQUENCIES_HZ):
        """Record the reference load at every frequency in turn. `reference_resistance`
        is in cmH2O·s/L."""
        if not self.hardware_enabled:
            raise RuntimeError("Arduino access is disabled in backend/config.py.")
        if float(reference_resistance) <= 0:
            raise ValueError("Enter the resistance printed on the test object.")
        self.gain_phase_results = None
        return self._start_recorder(
            "gain_phase",
            lambda client: GainPhaseSweep(
                client,
                reference_resistance=reference_resistance,
                frequencies=frequencies,
            ),
            port=port,
        )

    def commit_gain_phase(self):
        """Move a finished sweep's results onto the service and free the capture."""
        if self.recorder is None or self.capture_purpose != "gain_phase":
            return None
        snapshot = self.recorder.snapshot()
        if snapshot["state"] not in ("done", "error"):
            return None
        self.gain_phase_results = snapshot["results"]
        recorder = self.recorder
        self.recorder = None
        self.capture_purpose = None
        recorder.cancel()
        self._stop_streaming()
        return self.gain_phase_results

    def save_gain_phase(self):
        """Save every frequency that passed. Returns the frequencies saved."""
        if not self.gain_phase_results:
            raise ValueError("There is no calibration to save.")
        saved = save_gain_phase_results(self.gain_phase_results, CALIBRATION_ARCHIVE_DIR)
        if not saved:
            raise ValueError("No frequency passed, so nothing was saved.")
        self.gain_phase_results = None
        return saved

    def discard_gain_phase(self):
        self.gain_phase_results = None

    @staticmethod
    def gain_phase_in_use(frequencies=FREQUENCIES_HZ):
        return [current_calibration(hz) for hz in frequencies]

    # ------------------------------------------------------- daily system check

    def start_check_measurement(self, reference_r, cpap_note="", port=None):
        """One check measurement: settle, then a single CHECK_SECONDS window scored
        against the reference load. Uses the BIAS-CORRECTED tier, because the check
        verifies the calibration currently in force."""
        if not self.hardware_enabled:
            raise RuntimeError("Arduino access is disabled in backend/config.py.")
        self._check_reference_r = float(reference_r)
        self._check_cpap_note = cpap_note
        return self._start_recorder(
            "check",
            lambda client: CalibrationPointRecorder(
                client,
                cpap_value=cpap_note or "-",
                repeats=1,
                correction_deg=self.correction_deg,
                settle_blocks=CHECK_SETTLING_BLOCKS,
                window_blocks=CHECK_SECONDS,
                tier="bias_corrected",
            ),
            port=port,
        )

    def commit_check_measurement(self):
        """Score a finished check capture and append it to this session's results."""
        if self.recorder is None or self.capture_purpose != "check":
            return None
        snapshot = self.recorder.snapshot()
        if snapshot["state"] != "done" or not snapshot["point"]:
            return None

        point = score_point(
            snapshot["point"],
            reference_r=getattr(self, "_check_reference_r", 0.0),
            cpap_note=getattr(self, "_check_cpap_note", ""),
            measurement_number=len(self.check_points) + 1,
        )
        self.check_points.append(point)
        recorder = self.recorder
        self.recorder = None
        self.capture_purpose = None
        recorder.cancel()
        self._stop_streaming()

        _, verdict_text = self.check_verdict()
        if self.check_log_path is None:
            self.check_log_path = default_check_log_path(CALIBRATION_DATA_DIR)
        append_check_log(
            self.check_log_path,
            self.check_number,
            point,
            verdict_text,
            EXCITATION_HZ,
        )
        self.check_number += 1
        return point

    def check_verdict(self):
        return verdict(self.check_points)

    def reset_check(self):
        self.cancel_capture()
        self.check_points = []

    # ------------------------------------------------------------------ status

    @staticmethod
    def active_calibration():
        """What the engine is using right now, for display."""
        return load_calibration(bias_calibration_path_for(EXCITATION_HZ))

    @staticmethod
    def active_calibration_path():
        return bias_calibration_path_for(EXCITATION_HZ)

    @staticmethod
    def active_phase_calibration():
        return load_calibration(phase_calibration_path_for())

    @staticmethod
    def active_phase_calibration_path():
        return phase_calibration_path_for()

    # ----------------------------------------------------- time-of-day presets

    @property
    def presets(self):
        return default_preset_store(EXCITATION_HZ)

    def preset_status(self):
        """One read covering the toggle, the active period and every period's preset
        plus history — the whole presets panel renders off this."""
        return self.presets.status()

    def set_auto_apply(self, enabled):
        return self.presets.set_auto_apply(enabled)

    def activate_period(self, period):
        """Put a period's preset in force. Live for the calibration tabs, the check and
        the measurement pipeline; see PresetStore.activate."""
        return self.presets.activate(period)

    def restore_preset(self, period, index):
        """Promote a history entry to be the period's preset. Does NOT put it in force —
        activation stays a separate, deliberate step."""
        return self.presets.restore(period, index)

    def delete_preset_history_entry(self, period, index):
        return self.presets.delete_history_entry(period, index)

    def apply_due_preset(self):
        """Called on render: honours the auto-apply toggle. Returns the period switched
        to, or None if nothing changed."""
        return self.presets.ensure_auto_applied()

    def store_current_as_preset(
        self,
        period,
        note="",
        include_phase=True,
        include_bias=True,
    ):
        """Snapshot the calibration CURRENTLY in force into a period slot.

        Deliberately reads the two canonical files rather than this session's in-memory
        fit: what gets stored is then exactly what the engine is using, with no chance
        of banking a fit that was never saved.
        """
        if not include_phase and not include_bias:
            raise ValueError("Select phase angle, point calibration, or both.")

        active_phase = self.active_phase_calibration()
        active_bias = self.active_calibration()
        store = self.presets.load()
        current = store["periods"][period]["current"] or {}

        phase = active_phase if include_phase else current.get("phase")
        bias = active_bias if include_bias else current.get("bias")

        if include_phase and active_phase is None:
            raise ValueError("There is no saved phase angle to store.")
        if include_bias and active_bias is None:
            raise ValueError("There is no saved point calibration to store.")
        if phase is None and bias is None:
            raise ValueError(
                "There is no saved calibration to store. Run a phase calibration or a "
                "bias sweep first."
            )
        entry = self.presets.record(period, phase, bias, note=note)
        # Storing the active calibration makes this period the active one by definition;
        # recording that keeps auto-apply from immediately re-applying over the top.
        store = self.presets.load()
        store["active_period"] = period
        self.presets.save(store)
        return entry

    @staticmethod
    def current_clock_period():
        return period_for()

    @staticmethod
    def preset_periods():
        return PERIODS
