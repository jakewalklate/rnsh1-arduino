"""Records the reference load at each excitation frequency in turn.

One CalibrationPointRecorder per frequency (raw tier, blocks retained), run back to
back on a background thread so Streamlit can poll snapshot(). Has the same start /
snapshot / cancel shape as a recorder, so CalibrationService treats it like any other
capture: other tabs see the device as busy, and cancel or disconnect stop it.
"""

from threading import Event, Lock, Thread
from time import sleep

from backend.device.calibration_recorder import CalibrationPointRecorder
from backend.processing.FOTGainPhaseCalibration import (
    FREQUENCIES_HZ,
    MEASURE_SECONDS,
    SETTLE_SECONDS,
    blocks_for_seconds,
    evaluate_frequency,
    failed_frequency,
)

POLL_SECONDS = 0.2
# Time for the board to stop the previous tone before the next CONFIG.
BETWEEN_FREQUENCIES_SECONDS = 0.3


class GainPhaseSweep:
    def __init__(
        self,
        serial_client,
        reference_resistance,
        frequencies=FREQUENCIES_HZ,
        recorder_factory=CalibrationPointRecorder,
    ):
        self.serial_client = serial_client
        self.reference_resistance = float(reference_resistance)
        self.frequencies = tuple(float(hz) for hz in frequencies)
        self._recorder_factory = recorder_factory

        self.state = "idle"        # idle | collecting | done | error
        self.message = ""
        self.results = []
        self.current_hz = None
        self.last_error = None

        self._recorder = None
        self._lock = Lock()
        self._stop_event = Event()
        self._thread = None

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        with self._lock:
            if self.state == "collecting":
                return
            self.state = "collecting"
            self.results = []
            self.message = "Starting…"
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="gain-phase-sweep", daemon=True)
        self._thread.start()

    def cancel(self):
        self._stop_event.set()
        recorder = self._recorder
        if recorder is not None:
            recorder.cancel()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        with self._lock:
            if self.state == "collecting":
                self.state = "idle"
                self.message = "Calibration cancelled."

    def snapshot(self):
        with self._lock:
            done = len(self.results)
            total = len(self.frequencies)
            recorder = self._recorder
            state = self.state
            message = self.message
            results = list(self.results)
            current_hz = self.current_hz

        current = recorder.snapshot() if recorder is not None and state == "collecting" else {}
        within = current.get("progress", 0.0) if current else 0.0
        progress = 1.0 if state == "done" else (done + within) / max(1, total)
        return {
            "state": state,
            "message": message,
            "progress": min(1.0, max(0.0, progress)),
            "current_hz": current_hz,
            "frequencies_done": done,
            "frequencies_total": total,
            "results": results,
            "error": str(self.last_error) if self.last_error else None,
            "trace_time_s": current.get("trace_time_s", []),
            "trace_pressure": current.get("trace_pressure", []),
            "trace_flow": current.get("trace_flow", []),
            "point": None,
        }

    # ------------------------------------------------------------------- thread

    def _run(self):
        try:
            for position, hz in enumerate(self.frequencies, start=1):
                if self._stop_event.is_set():
                    return
                with self._lock:
                    self.current_hz = hz
                    self.message = (
                        f"Measuring at {hz:g} Hz ({position} of {len(self.frequencies)})"
                    )
                result = self._measure(hz)
                with self._lock:
                    self.results.append(result)
            with self._lock:
                self.state = "done"
                self.current_hz = None
                passed = sum(1 for r in self.results if r["passed"])
                self.message = (
                    f"Finished: {passed} of {len(self.results)} frequencies passed."
                )
        except Exception as error:
            with self._lock:
                self.last_error = error
                self.state = "error"
                self.message = f"Calibration failed: {error}"
        finally:
            self._recorder = None
            self._stop_tone()

    def _stop_tone(self):
        try:
            if self.serial_client.is_connected:
                self.serial_client.stop_streaming()
        except Exception:
            pass

    def _measure(self, hz):
        """Record one frequency and reduce it. Any failure at this frequency (the
        firmware rejecting it, the stream dropping) is recorded against it and the
        sweep moves on, so one bad frequency does not lose the others."""
        self._stop_tone()
        sleep(BETWEEN_FREQUENCIES_SECONDS)
        recorder = self._recorder_factory(
            self.serial_client,
            cpap_value="off",
            repeats=1,
            settle_blocks=blocks_for_seconds(SETTLE_SECONDS, hz),
            window_blocks=blocks_for_seconds(MEASURE_SECONDS, hz),
            tier="raw",
            retain_blocks=True,
            excitation_hz=hz,
        )
        self._recorder = recorder
        try:
            recorder.start()
        except Exception as error:
            return failed_frequency(hz, self.reference_resistance, error)

        while not self._stop_event.is_set():
            status = recorder.snapshot()
            if status["state"] == "done":
                break
            if status["state"] in ("error", "idle"):
                recorder.cancel()
                return failed_frequency(
                    hz,
                    self.reference_resistance,
                    status.get("error") or status.get("message") or "recording stopped",
                )
            sleep(POLL_SECONDS)
        else:
            recorder.cancel()
            return failed_frequency(hz, self.reference_resistance, "cancelled")

        blocks = list(recorder.collected_blocks)
        recorder.cancel()
        try:
            return evaluate_frequency(blocks, self.reference_resistance, hz)
        except Exception as error:
            return failed_frequency(hz, self.reference_resistance, error)
