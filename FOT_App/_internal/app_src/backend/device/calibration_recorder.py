"""Serial capture for one tube-calibration point.

Mirrors the block state machine in Henry's bench tool (settle -> collect N full
windows -> average), but reads through the app's own ArduinoSerialClient and runs on
a background thread so Streamlit can poll it.

One point = SETTLE_BLOCKS discarded blocks, then `repeats` INDEPENDENT windows of
BLOCKS_PER_WINDOW blocks each. A block is one full second at the standard operating
point, so a 3-repeat point takes about 35 s.

The fitted y-value is result.corrected — phase rotation ONLY, never bias_corrected,
because the bias constants are what the sweep is solving for.
"""

from collections import deque
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from time import monotonic, sleep

import numpy as np

from backend.processing.FOTImpedance import (
    BLOCK_SAMPLES,
    BLOCKS_PER_WINDOW,
    LUT_SIZE,
    SAMPLE_HZ,
    analyse_blocks,
    make_block_phasor,
)
from backend.processing.FOTTubeCalibration import SETTLE_BLOCKS

# Rolling trace shown live while a point is captured, so the operator can see the
# signal is healthy before trusting the point. Display only — never fed to the fit.
TRACE_SECONDS = 6.0
TRACE_SAMPLES = int(TRACE_SECONDS * SAMPLE_HZ)
TRACE_DISPLAY_HZ = 60

# Give up if the device goes quiet for this long, rather than waiting forever.
NO_SAMPLE_TIMEOUT_S = 8.0
SERIAL_RECOVERY_ATTEMPTS = 3
SERIAL_RECOVERY_DELAY_S = 0.5


class CalibrationPointRecorder:
    """Captures a single CPAP point. Create one per capture, call start(), poll
    snapshot() until state is 'done' or 'error', then read .point."""

    def __init__(
        self,
        serial_client,
        cpap_value,
        repeats=3,
        correction_deg=0.0,
        poll_interval_seconds=0.01,
        settle_blocks=SETTLE_BLOCKS,
        window_blocks=BLOCKS_PER_WINDOW,
        tier="corrected",
        retain_blocks=False,
    ):
        """`tier` selects which impedance the point reports:
          "raw"            nothing applied — for the phase calibration, which SOLVES
                           for the rotation angle. Showing a rotated or corrected
                           number there asks the operator to judge the capture on a
                           basis the saved angle is not derived from.
          "corrected"      phase rotation only  — for a calibration sweep, which is
                           solving for the bias constants and must not apply them.
          "bias_corrected" phase + bias correction — for the daily system check,
                           which VERIFIES the calibration currently in force.

        `retain_blocks` keeps the settled BlockPhasors on `.collected_blocks`. The
        phase calibration needs them: it derives the correction angle from the raw
        phasor, so no already-rotated summary will do.
        """
        self.serial_client = serial_client
        self.cpap_value = cpap_value
        self.repeats = max(1, int(repeats))
        self.correction_deg = float(correction_deg)
        self.poll_interval_seconds = poll_interval_seconds
        self.settle_blocks = int(settle_blocks)
        self.window_blocks = int(window_blocks)
        self.tier = tier
        self.retain_blocks = bool(retain_blocks)

        self.state = "idle"          # idle | settling | collecting | done | error
        self.message = ""
        self.point = None
        self.collected_blocks = []
        self.last_error = None

        self._settle_seen = 0
        self._window_blocks = []
        self._repeat_results = []
        self._block_idx = []
        self._block_pressure = []
        self._block_flow = []
        self._live_blocks = deque(maxlen=BLOCKS_PER_WINDOW)
        self._dropped_blocks = 0
        self._trace_pressure = deque(maxlen=TRACE_SAMPLES)
        self._trace_flow = deque(maxlen=TRACE_SAMPLES)
        self._live_result = None

        self._lock = Lock()
        self._stop_event = Event()
        self._thread = None

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        with self._lock:
            if self.state in ("settling", "collecting"):
                return
            self.state = "settling"
            self.message = (
                f"Settling at CPAP {self.cpap_value} "
                f"({self.settle_blocks} s), then {self.repeats} x {self.window_blocks} s."
            )
            self._reset_progress_locked()

        # Ask the device to stream rather than assuming it already is. It only starts
        # on START and stays stopped until then, so a capture that follows anything
        # which sent STOP (a finished recording, a syringe calibration session) would
        # otherwise wait forever for samples that are never coming. Sending START to
        # a device already streaming just resets the LUT index, which is harmless.
        # The buffer purge drops bytes queued before this capture, so the first block
        # is not half of someone else's.
        self.serial_client.reset_input_buffer()
        # Do not start the capture clock until the firmware confirms it handled
        # START. A successful Windows write alone does not prove the command reached
        # the board.
        self.serial_client.start_streaming(require_ack=True)

        self._stop_event.clear()
        self._thread = Thread(
            target=self._run,
            name=f"calibration-point-{self.cpap_value}",
            daemon=True,
        )
        self._thread.start()

    def cancel(self):
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            if self.state in ("settling", "collecting"):
                self.state = "idle"
                self.message = "Capture cancelled."

    def snapshot(self):
        """Thread-safe view for the UI."""
        with self._lock:
            total_windows = self.repeats
            done_windows = len(self._repeat_results)
            if self.state == "settling":
                progress = self._settle_seen / max(1, self.settle_blocks)
                progress *= self.settle_blocks / (
                    self.settle_blocks + total_windows * self.window_blocks
                )
            elif self.state == "collecting":
                blocks_done = done_windows * self.window_blocks + len(self._window_blocks)
                total_blocks = self.settle_blocks + total_windows * self.window_blocks
                progress = (self.settle_blocks + blocks_done) / total_blocks
            elif self.state == "done":
                progress = 1.0
            else:
                progress = 0.0

            times, pressure, flow = self._trace_copy()
            live = self._live_result
            return {
                "state": self.state,
                "message": self.message,
                "cpap": self.cpap_value,
                "windows_done": done_windows,
                "windows_total": total_windows,
                "settle_seen": self._settle_seen,
                "settle_total": self.settle_blocks,
                "progress": min(1.0, max(0.0, progress)),
                "dropped_blocks": self._dropped_blocks,
                "point": self.point,
                "error": str(self.last_error) if self.last_error else None,
                "trace_time_s": times,
                "trace_pressure": pressure,
                "trace_flow": flow,
                "live_r": live[0] if live else None,
                "live_x": live[1] if live else None,
                "live_bias_flow": live[2] if live else None,
            }

    def _trace_copy(self):
        """Downsampled COPY of the rolling trace, for plotting only. Called with the
        lock held. Captured data is never downsampled — this is display only."""
        pressure = list(self._trace_pressure)
        flow = list(self._trace_flow)
        n = min(len(pressure), len(flow))
        if n == 0:
            return [], [], []
        pressure = pressure[-n:]
        flow = flow[-n:]
        stride = max(1, int(round(SAMPLE_HZ / TRACE_DISPLAY_HZ)))
        idx = list(range(0, n, stride))
        if idx[-1] != n - 1:
            idx.append(n - 1)
        # x axis ends at 0 = now, so the trace scrolls right-to-left.
        times = [(i - (n - 1)) / SAMPLE_HZ for i in idx]
        return times, [pressure[i] for i in idx], [flow[i] for i in idx]

    # ------------------------------------------------------------------- thread

    def _run(self):
        try:
            last_sample_at = monotonic()
            while not self._stop_event.is_set():
                try:
                    samples = self.serial_client.read_available()
                except Exception as error:
                    if self._recover_serial(error):
                        last_sample_at = monotonic()
                        continue
                    return
                # A silent port is not a slow one: at 420 Hz samples arrive
                # continuously, so a gap this long means the device stopped, was
                # unplugged, or never started. Without this the capture sits on
                # "Waiting for the first samples..." forever and the only way out is
                # restarting the server.
                if samples:
                    last_sample_at = monotonic()
                elif monotonic() - last_sample_at > NO_SAMPLE_TIMEOUT_S:
                    silence_error = RuntimeError(
                        f"No samples for {NO_SAMPLE_TIMEOUT_S:g} s."
                    )
                    if self._recover_serial(silence_error):
                        last_sample_at = monotonic()
                        continue
                    return
                for sample in samples:
                    self._accept_sample(
                        sample.arduino_index,
                        sample.pressure_cmH2O,
                        sample.flow_lps,
                    )
                    with self._lock:
                        finished = self.state == "done"
                    if finished:
                        return
                sleep(self.poll_interval_seconds)
        except Exception as error:  # serial dropped, device unplugged, parse failure
            with self._lock:
                self.last_error = error
                self.state = "error"
                self.message = f"Capture failed: {error}"

    def _reset_progress_locked(self):
        """Discard partial work so a capture always contains one continuous stream."""
        self._settle_seen = 0
        self._window_blocks = []
        self._repeat_results = []
        self._block_idx = []
        self._block_pressure = []
        self._block_flow = []
        self._dropped_blocks = 0
        self._trace_pressure.clear()
        self._trace_flow.clear()
        self._live_blocks.clear()
        self._live_result = None
        self.point = None
        self.collected_blocks = []
        self.last_error = None

    def _recover_serial(self, original_error):
        """Recover a Windows USB serial failure without accepting discontinuous data."""
        last_error = original_error
        for attempt in range(1, SERIAL_RECOVERY_ATTEMPTS + 1):
            if self._stop_event.is_set():
                return False
            try:
                self.serial_client.disconnect()
                sleep(SERIAL_RECOVERY_DELAY_S)
                self.serial_client.connect()
                self.serial_client.reset_input_buffer()
                self.serial_client.start_streaming(require_ack=True)
            except Exception as error:
                last_error = error
                continue

            with self._lock:
                self.state = "settling"
                self._reset_progress_locked()
                self.message = (
                    "Arduino connection recovered automatically. Restarting the "
                    f"{self.settle_blocks} s settling period."
                )
            return True

        with self._lock:
            self.last_error = last_error
            self.state = "error"
            self.message = (
                "Arduino connection was lost and automatic recovery failed after "
                f"{SERIAL_RECOVERY_ATTEMPTS} attempts. Last error: {last_error}"
            )
        return False

    # -------------------------------------------------------------- block build

    def _drop_partial_block(self, reason):
        self._block_idx.clear()
        self._block_pressure.clear()
        self._block_flow.clear()
        self._dropped_blocks += 1
        self.message = f"Discarded a block ({reason}); still capturing."

    def _accept_sample(self, index, pressure, flow):
        with self._lock:
            if self.state not in ("settling", "collecting"):
                return
            # A missing serial sample breaks the phase lock, so the partial block
            # cannot be used: the LUT index must advance exactly one step per sample.
            if self._block_idx and index != (self._block_idx[-1] + 1) % LUT_SIZE:
                self._drop_partial_block(
                    f"LUT jump {self._block_idx[-1]}->{index}"
                )
            self._block_idx.append(index)
            self._block_pressure.append(pressure)
            self._block_flow.append(flow)
            self._trace_pressure.append(pressure)
            self._trace_flow.append(flow)
            if len(self._block_idx) == BLOCK_SAMPLES:
                self._complete_block()

    def _tier_impedance(self, result):
        """The impedance this recorder reports, at its configured tier.

        Calibration takes `corrected` (phase rotation only) — using bias_corrected
        there would fit the old constants to themselves. The system check takes
        `bias_corrected`, because it is verifying those constants. Phase calibration
        takes `raw`, the same unrotated phasor the new angle is computed from. The
        live readout and the committed windows both go through here so they always
        agree.
        """
        if self.tier == "bias_corrected":
            return result.bias_corrected
        if self.tier == "raw":
            return result.raw
        return result.corrected

    def _complete_block(self):
        """Called with the lock held."""
        try:
            block = make_block_phasor(
                self._block_idx, self._block_pressure, self._block_flow
            )
        except ValueError as error:
            self._drop_partial_block(str(error))
            return

        self._block_idx.clear()
        self._block_pressure.clear()
        self._block_flow.clear()
        self._live_blocks.append(block)

        # Rolling readout so the operator can watch the value settle. Same tier as the
        # committed windows: showing phase-only here while the system check scores
        # bias-corrected made the operator judge a different number from the one that
        # decides PASS/FAIL.
        if len(self._live_blocks) >= BLOCKS_PER_WINDOW:
            live = analyse_blocks(
                self._live_blocks, correction_deg=self.correction_deg
            )
            if live.valid:
                z = self._tier_impedance(live)
                self._live_result = (
                    float(z.real),
                    float(z.imag),
                    float(live.bias_flow_lps),
                )

        if self.state == "settling":
            self._settle_seen += 1
            self.message = (
                f"Settling at CPAP {self.cpap_value}: "
                f"{self._settle_seen}/{self.settle_blocks} s"
            )
            if self._settle_seen >= self.settle_blocks:
                self.state = "collecting"
                self._window_blocks = []
                self.message = f"Collecting window 1/{self.repeats}"
            return

        if self.state == "collecting":
            self._window_blocks.append(block)
            done = len(self._repeat_results)
            self.message = (
                f"CPAP {self.cpap_value}: window {done + 1}/{self.repeats}, "
                f"{len(self._window_blocks)}/{self.window_blocks} s"
            )
            if len(self._window_blocks) >= self.window_blocks:
                result = analyse_blocks(
                    self._window_blocks,
                    correction_deg=self.correction_deg,
                    required_blocks=self.window_blocks,
                )
                z = self._tier_impedance(result)
                self._repeat_results.append((
                    float(z.real),
                    float(z.imag),
                    float(result.bias_flow_lps),
                    float(result.flow_amplitude),
                    float(result.block_relative_deviation),
                    bool(result.valid),
                    result.reason,
                    float(result.pressure_amplitude),
                ))
                if self.retain_blocks:
                    self.collected_blocks.extend(self._window_blocks)
                self._window_blocks = []
                if len(self._repeat_results) >= self.repeats:
                    self._finalise_point()

    def _finalise_point(self):
        """Called with the lock held."""
        r_vals = np.array([r[0] for r in self._repeat_results], dtype=float)
        x_vals = np.array([r[1] for r in self._repeat_results], dtype=float)
        q_vals = np.array([r[2] for r in self._repeat_results], dtype=float)
        a_vals = np.array([r[3] for r in self._repeat_results], dtype=float)
        v_vals = np.array([r[4] for r in self._repeat_results], dtype=float)
        all_valid = all(r[5] for r in self._repeat_results)
        reasons = sorted({r[6] for r in self._repeat_results if not r[5]})

        self.point = {
            "captured_utc": datetime.now(timezone.utc).isoformat(),
            "cpap": self.cpap_value,
            # Tier-neutral names. r_phase/x_phase below keep the calibration-sweep
            # naming; they hold the SAME numbers, at whichever tier was requested.
            "tier": self.tier,
            "r": float(np.mean(r_vals)),
            "x": float(np.mean(x_vals)),
            "pressure_amplitude": float(np.mean([r[7] for r in self._repeat_results])),
            "r_phase": float(np.mean(r_vals)),
            "r_phase_sd": float(np.std(r_vals, ddof=1)) if len(r_vals) > 1 else 0.0,
            "x_phase": float(np.mean(x_vals)),
            "bias_flow": float(np.mean(q_vals)),
            "bias_flow_sd": float(np.std(q_vals, ddof=1)) if len(q_vals) > 1 else 0.0,
            "flow_amplitude": float(np.mean(a_vals)),
            "variation": float(np.mean(v_vals)) * 100.0,
            "n_windows": len(r_vals),
            "all_valid": all_valid,
            "reason": "; ".join(reasons) if reasons else "OK",
        }
        self.state = "done"
        self.message = (
            f"CPAP {self.cpap_value} captured: R={self.point['r_phase']:.3f}, "
            f"bias flow={self.point['bias_flow']:.3f} L/s"
        )
        if not all_valid:
            self.message += "  (quality warning)"
