from collections import deque
from threading import Event, Lock, Thread, current_thread
from time import monotonic, sleep

from backend.config import SAVE_RAW_SAMPLES_TO_DATABASE, log_debug
from backend.database.storage import save_measurement_samples
from backend.device.sample_csv_writer import MeasurementCsvWriter


class MeasurementSerialRecorder:
    def __init__(
        self,
        serial_client,
        measurement_id,
        save_to_database=SAVE_RAW_SAMPLES_TO_DATABASE,
        save_to_csv=True,
        send_device_commands=False,
        poll_interval_seconds=0.01,
        recent_sample_limit=4200,
        database_batch_size=100,
        database_flush_interval_seconds=0.25,
        csv_flush_interval_seconds=0.5,
    ):
        self.serial_client = serial_client
        self.measurement_id = measurement_id
        self.save_to_database = save_to_database
        self.save_to_csv = save_to_csv
        self.send_device_commands = send_device_commands
        self.poll_interval_seconds = poll_interval_seconds
        self.database_batch_size = database_batch_size
        self.database_flush_interval_seconds = database_flush_interval_seconds
        self.csv_flush_interval_seconds = csv_flush_interval_seconds
        self.recent_samples = deque(maxlen=recent_sample_limit)
        self.started_at = None
        self.sample_number = 0
        self.is_recording = False
        self.last_error = None
        self._pending_database_rows = []
        self._last_database_flush = monotonic()
        self._csv_writer = MeasurementCsvWriter(measurement_id) if save_to_csv else None
        self._last_csv_flush = monotonic()
        self._lock = Lock()
        self._stop_event = Event()
        self._thread = None

    def start(self):
        if self.is_recording:
            return

        if self._csv_writer is not None:
            self._csv_writer.open()

        if self.send_device_commands:
            self.serial_client.start_streaming(require_ack=True)

        with self._lock:
            self.started_at = monotonic()
            self.sample_number = 0
            self.last_error = None
            self.recent_samples.clear()
            self._pending_database_rows = []
            self._last_database_flush = monotonic()
            self._last_csv_flush = monotonic()
            self.is_recording = True

        self._stop_event.clear()
        self._thread = Thread(
            target=self._recording_loop,
            name=f"measurement-recorder-{self.measurement_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        if not self.is_recording and self._thread is None:
            return

        self._stop_event.set()

        stop_error = None

        if self.send_device_commands:
            try:
                self.serial_client.stop_streaming()
            except Exception as error:
                stop_error = error

        if (
            self._thread is not None
            and self._thread.is_alive()
            and current_thread() is not self._thread
        ):
            self._thread.join(timeout=2.0)

        with self._lock:
            self.is_recording = False
            self._thread = None

        self._flush_pending_database_rows()
        self._close_csv_writer()

        if stop_error is not None:
            raise stop_error

    def poll(self):
        with self._lock:
            if not self.is_recording or self.started_at is None:
                return []

        samples = self.serial_client.read_available()
        rows = []

        with self._lock:
            for sample in samples:
                self.sample_number += 1
                rows.append({
                    "sample_number": self.sample_number,
                    "elapsed_seconds": monotonic() - self.started_at,
                    "arduino_index": sample.arduino_index,
                    "pressure_cmH2O": sample.pressure_cmH2O,
                    "flow_lps": sample.flow_lps,
                    "raw_line": sample.raw_line,
                })

            self.recent_samples.extend(rows)
            if self.sample_number > 0 and self.sample_number % 200 < len(rows):
                latest = rows[-1]
                log_debug(f"[DEBUG LIVE] Sample #{self.sample_number:05d} | P={latest['pressure_cmH2O']:+.2f} cmH2O | Flow={latest['flow_lps']:+.2f} L/s")

        if self.save_to_database:
            self._queue_database_rows(rows)

        if self._csv_writer is not None:
            self._write_csv_rows(rows)

        return rows

    def get_recent_samples(self):
        with self._lock:
            return list(self.recent_samples)

    def _recording_loop(self):
        log_debug(f"[DEBUG] Recording thread started for measurement {self.measurement_id}")
        while not self._stop_event.is_set():
            try:
                self.poll()
            except Exception as error:
                import traceback
                log_debug(f"[DEBUG ERROR in recording loop] {error}")
                traceback.print_exc()
                with self._lock:
                    self.last_error = error
                    self.is_recording = False
                self._stop_event.set()
                break

            sleep(self.poll_interval_seconds)

        with self._lock:
            self.is_recording = False

        log_debug(f"[DEBUG] Recording thread ended. Total samples collected: {self.sample_number}")
        self._flush_pending_database_rows()
        self._close_csv_writer()

    def _queue_database_rows(self, rows):
        if not rows:
            return

        rows_to_save = []

        with self._lock:
            self._pending_database_rows.extend(rows)
            elapsed_since_flush = monotonic() - self._last_database_flush

            if (
                len(self._pending_database_rows) >= self.database_batch_size
                or elapsed_since_flush >= self.database_flush_interval_seconds
            ):
                rows_to_save = self._pending_database_rows
                self._pending_database_rows = []
                self._last_database_flush = monotonic()

        if rows_to_save:
            save_measurement_samples(self.measurement_id, rows_to_save)

    def _flush_pending_database_rows(self):
        rows_to_save = []

        with self._lock:
            if self._pending_database_rows:
                rows_to_save = self._pending_database_rows
                self._pending_database_rows = []
                self._last_database_flush = monotonic()

        if rows_to_save:
            save_measurement_samples(self.measurement_id, rows_to_save)

    def _write_csv_rows(self, rows):
        if not rows:
            return

        self._csv_writer.write_rows(rows)

        if monotonic() - self._last_csv_flush >= self.csv_flush_interval_seconds:
            self._csv_writer.flush()
            self._last_csv_flush = monotonic()

    def _close_csv_writer(self):
        if self._csv_writer is not None:
            self._csv_writer.close()
