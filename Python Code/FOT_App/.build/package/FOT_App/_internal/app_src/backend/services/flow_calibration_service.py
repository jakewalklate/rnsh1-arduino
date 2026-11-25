"""Session state and device access for the syringe flow calibration.

Owns its own serial connection for the same reason CalibrationService does: only one
program can hold the COM port, so the caller must check that no recording or other
calibration is running before starting.
"""

from backend.config import (
    ARDUINO_BAUDRATE,
    ARDUINO_CONNECT_DELAY_SECONDS,
    ARDUINO_ENABLED,
    ARDUINO_PORT,
    ARDUINO_TIMEOUT_SECONDS,
    FIRMWARE_FLOW_GAIN_NEG,
    FIRMWARE_FLOW_GAIN_POS,
)
from backend.device.flow_calibration_recorder import FlowCalibrationRecorder
from backend.device.serial_device import ArduinoSerialClient, list_serial_ports
from backend.processing.FlowGainCalibration import (
    active_flow_gains,
    flow_gain_path,
    load_flow_gain,
    save_flow_gain,
    status_message,
    summarise,
)


class FlowCalibrationService:
    def __init__(self, hardware_enabled=ARDUINO_ENABLED):
        self.hardware_enabled = hardware_enabled
        self.client = None
        self.port = ARDUINO_PORT
        self.recorder = None
        self.last_saved_path = None

    # ------------------------------------------------------------------ device

    @staticmethod
    def available_ports():
        return list_serial_ports()

    def _ensure_client(self, port=None):
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

    def disconnect(self):
        self.end_session()
        if self.client is not None:
            try:
                self.client.disconnect()
            finally:
                self.client = None

    # ----------------------------------------------------------------- session

    @property
    def is_active(self):
        return self.recorder is not None

    def start_session(self, port=None):
        """Take the port and put the device into flow-calibration mode."""
        if not self.hardware_enabled:
            raise RuntimeError("Arduino access is disabled in backend/config.py.")
        if self.recorder is not None:
            return self.recorder
        client = self._ensure_client(port)
        recorder = FlowCalibrationRecorder(client)
        try:
            recorder.start()
        except Exception:
            # Never leave the device silenced and streaming dp because setup failed.
            try:
                client.set_flow_calibration_mode(False)
                client.stop_streaming()
            except Exception:
                pass
            raise
        self.recorder = recorder
        return recorder

    def end_session(self):
        """Return the device to normal operation and RELEASE the port.

        Releasing matters: only one program can hold a COM port, and this service is
        a cached singleton that outlives the tab. Holding the port after the session
        ends makes every other tab fail with a bare PermissionError from pyserial,
        which reads like a driver fault rather than "the app is still holding it".
        """
        if self.recorder is None:
            self._close_client()
            return
        try:
            self.recorder.stop()
        finally:
            self.recorder = None
            self._close_client()

    def _close_client(self):
        if self.client is None:
            return
        try:
            self.client.disconnect()
        except Exception:
            pass
        finally:
            self.client = None

    def release_device(self):
        """Free the port for another tab. Safe to call when nothing is open."""
        self.end_session()

    def snapshot(self):
        if self.recorder is None:
            return None
        return self.recorder.snapshot()

    # ----------------------------------------------------------------- strokes

    @property
    def strokes(self):
        return self.recorder.strokes if self.recorder else []

    def begin_stroke(self, direction):
        if self.recorder is None:
            return False, "Start the calibration session first."
        return self.recorder.begin_stroke(direction)

    def finish_stroke(self):
        if self.recorder is None:
            return False, "Start the calibration session first."
        return self.recorder.finish_stroke()

    def discard_stroke(self):
        if self.recorder is None:
            return False, "Start the calibration session first."
        return self.recorder.discard_stroke()

    def remove_stroke(self, number):
        if self.recorder is not None:
            self.recorder.remove_stroke(number)

    def clear_strokes(self):
        if self.recorder is not None:
            self.recorder.clear_strokes()

    # -------------------------------------------------------------- fit / save

    def summary(self):
        return summarise(self.strokes)

    def status_text(self):
        return status_message(self.summary())

    def save(self, notes=""):
        """Write the gains the app applies from here on. Takes effect immediately:
        rescale_flow_lps reads the file at call time, so the next sample parsed uses
        the new numbers with no restart."""
        path, payload = save_flow_gain(self.strokes, notes=notes)
        self.last_saved_path = path
        return path, payload

    # ------------------------------------------------------------------ status

    @staticmethod
    def active_gains():
        """(push, pull) currently applied to every incoming flow sample."""
        return active_flow_gains()

    @staticmethod
    def firmware_gains():
        """(push, pull) compiled into the Arduino, which the app divides back out."""
        return FIRMWARE_FLOW_GAIN_POS, FIRMWARE_FLOW_GAIN_NEG

    @staticmethod
    def saved_calibration():
        return load_flow_gain()

    @staticmethod
    def saved_path():
        return flow_gain_path()
