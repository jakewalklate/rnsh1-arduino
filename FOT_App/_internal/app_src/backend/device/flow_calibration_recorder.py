"""Serial capture for the syringe flow calibration.

Mirrors the bench tool's stroke state machine (idle baseline -> record a stroke ->
score it) but reads through the app's ArduinoSerialClient on a background thread so
Streamlit can poll it.

The device is put into flow-calibration mode for the whole session: the speaker is
silenced and the third stream field carries RAW A2 differential pressure. Samples are
therefore read with apply_flow_gain=False — scaling a dp reading by a flow gain would
corrupt the very quantity being calibrated.
"""

from collections import deque
from threading import Event, Lock, Thread
from time import monotonic, sleep

from backend.processing.FlowGainCalibration import (
    MIN_BASELINE_SECONDS,
    score_stroke,
)
from backend.processing.FOTImpedance import SAMPLE_HZ

# Live trace shown while calibrating, so the operator can see the A2 pulse and the
# zero before trusting a stroke. Display only.
TRACE_SECONDS = 8.0
TRACE_SAMPLES = int(TRACE_SECONDS * SAMPLE_HZ)
BASELINE_SAMPLES = int(SAMPLE_HZ)          # one second of pre-stroke zero
MODE_ACK_TIMEOUT_S = 3.0


class FlowCalibrationRecorderError(RuntimeError):
    pass


class FlowCalibrationRecorder:
    """Owns the device for a syringe calibration session. Call start(), then
    begin_stroke/finish_stroke per stroke, then stop() to hand the device back."""

    def __init__(self, serial_client, poll_interval_seconds=0.005):
        self.serial_client = serial_client
        self.poll_interval_seconds = poll_interval_seconds
        self.sample_hz = float(SAMPLE_HZ)

        self.state = "idle"          # idle | ready | recording | error
        self.message = ""
        self.last_error = None
        self.strokes = []

        self._recording = False
        self._direction = 0
        self._stroke_samples = []
        self._stroke_baseline = 0.0
        self._baseline = deque(maxlen=BASELINE_SAMPLES)
        self._trace = deque(maxlen=TRACE_SAMPLES)
        self._latest_dp = 0.0

        self._lock = Lock()
        self._stop_event = Event()
        self._thread = None

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        """Enter flow-calibration mode and begin streaming.

        Raises if the firmware does not acknowledge FLOW_CAL. An older build ignores
        the command and keeps streaming FLOW, which looks perfectly normal but would
        be integrated as though it were pressure — a wrong gain with no visible
        symptom. Failing loudly here is the only protection against that.
        """
        with self._lock:
            if self.state in ("ready", "recording"):
                return

        self.serial_client.reset_input_buffer()
        self.serial_client.set_flow_calibration_mode(True)

        # The firmware handles serial commands whether or not it is streaming, so the
        # reply arrives without starting the stream first. Draining is what surfaces
        # it: read_available is the only thing emptying the port.
        deadline = monotonic() + MODE_ACK_TIMEOUT_S
        acknowledged = False
        while monotonic() < deadline and not acknowledged:
            try:
                self.serial_client.read_available(apply_flow_gain=False)
            except Exception:
                pass
            for notice in self.serial_client.drain_notices():
                upper = notice.upper()
                if "FLOW_CAL ON" in upper:
                    acknowledged = True
                    break
                if "UNKNOWN_COMMAND" in upper:
                    raise FlowCalibrationRecorderError(
                        "This Arduino firmware does not support CONFIG,FLOW_CAL. "
                        "Flash the current arduino_connection.ino once, then retry — "
                        "without it the speaker stays on and the device streams flow "
                        "instead of differential pressure."
                    )
            if not acknowledged:
                sleep(0.05)

        if not acknowledged:
            raise FlowCalibrationRecorderError(
                "The device did not acknowledge flow-calibration mode within "
                f"{MODE_ACK_TIMEOUT_S:g} s. Check the cable, and that the firmware "
                "flashed is the current arduino_connection.ino."
            )

        # Only once the mode is confirmed, so no sample can arrive still meaning flow.
        self.serial_client.start_streaming()

        with self._lock:
            self.state = "ready"
            self.message = (
                "Flow-calibration mode on, speaker silent. With no flow, confirm A2 "
                "reads near zero, then record a full stroke."
            )

        self._stop_event.clear()
        self._thread = Thread(
            target=self._run, name="flow-calibration", daemon=True
        )
        self._thread.start()

    def stop(self):
        """Leave flow-calibration mode and return the device to normal operation.
        Always call this, including on error, or the speaker stays silent and the
        stream keeps carrying dp instead of flow."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self.serial_client.set_flow_calibration_mode(False)
            self.serial_client.stop_streaming()
        finally:
            with self._lock:
                self._recording = False
                self.state = "idle"

    # ------------------------------------------------------------------ strokes

    def begin_stroke(self, direction):
        with self._lock:
            if self.state == "error":
                return False, self.message
            if self._recording:
                return False, "Finish or discard the current stroke first."
            if len(self._baseline) < int(MIN_BASELINE_SECONDS * self.sample_hz):
                return False, "Wait at least one second with no flow before starting."

            ordered = sorted(self._baseline)
            middle = len(ordered) // 2
            baseline = (
                ordered[middle] if len(ordered) % 2
                else 0.5 * (ordered[middle - 1] + ordered[middle])
            )

            self._recording = True
            self._direction = direction
            self._stroke_samples = []
            self._stroke_baseline = baseline
            self.state = "recording"
            sign = "+" if direction > 0 else "-"
            self.message = (
                f"Recording {sign}3 L stroke; pre-stroke zero={baseline:+.5f} cmH2O."
            )
            return True, self.message

    def finish_stroke(self):
        with self._lock:
            if not self._recording:
                return False, "No stroke is currently being recorded."
            self._recording = False
            self.state = "ready"
            samples = self._stroke_samples
            baseline = self._stroke_baseline
            direction = self._direction
            self._stroke_samples = []

        stroke, reason = score_stroke(
            samples,
            baseline=baseline,
            direction=direction,
            sample_hz=self.sample_hz,
            stroke_number=len(self.strokes) + 1,
        )
        if stroke is None:
            with self._lock:
                self.message = reason
            return False, reason

        self.strokes.append(stroke)
        with self._lock:
            self.message = (
                f"Stroke {stroke['number']} accepted "
                f"({stroke['direction']}, gain {stroke['gain_Lps_per_cmH2O']:.6f})."
            )
            return True, self.message

    def discard_stroke(self):
        with self._lock:
            self._recording = False
            self._stroke_samples = []
            self.state = "ready"
            self.message = "Current stroke discarded."
            return True, self.message

    def remove_stroke(self, number):
        """Drop one accepted stroke and renumber, so a bad one can be retaken
        without restarting the whole session."""
        self.strokes = [s for s in self.strokes if s["number"] != number]
        for index, stroke in enumerate(self.strokes, start=1):
            stroke["number"] = index
        return self.strokes

    def clear_strokes(self):
        self.strokes = []

    # ----------------------------------------------------------------- snapshot

    def snapshot(self):
        with self._lock:
            trace = list(self._trace)
            return {
                "state": self.state,
                "message": self.message,
                "recording": self._recording,
                "latest_dp": self._latest_dp,
                "baseline_ready": (
                    len(self._baseline)
                    >= int(MIN_BASELINE_SECONDS * self.sample_hz)
                ),
                "stroke_samples": len(self._stroke_samples),
                "stroke_duration_s": len(self._stroke_samples) / self.sample_hz,
                "trace": trace,
                "last_error": self.last_error,
            }

    # --------------------------------------------------------------------- loop

    def _run(self):
        try:
            while not self._stop_event.is_set():
                samples = self.serial_client.read_available(apply_flow_gain=False)
                if not samples:
                    sleep(self.poll_interval_seconds)
                    continue

                with self._lock:
                    for sample in samples:
                        # In flow-calibration mode this field is raw A2 dp in cmH2O,
                        # despite the name on the dataclass.
                        dp = sample.flow_lps
                        self._latest_dp = dp
                        self._trace.append(dp)
                        if self._recording:
                            self._stroke_samples.append(dp)
                        else:
                            self._baseline.append(dp)
        except Exception as error:      # noqa: BLE001 - surfaced to the operator
            with self._lock:
                self.state = "error"
                self.last_error = str(error)
                self.message = f"Device error: {error}"
