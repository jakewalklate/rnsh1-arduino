from dataclasses import dataclass
from time import sleep

from backend.config import ARDUINO_ENABLED, ARDUINO_RECONNECT_DELAY_SECONDS
from backend.device.arduino_controller import ArduinoController


@dataclass(frozen=True)
class RecordingStartResult:
    hardware_enabled: bool
    message: str


class RecordingService:
    def __init__(self, hardware_enabled=ARDUINO_ENABLED):
        self.hardware_enabled = hardware_enabled
        self.controller = ArduinoController() if hardware_enabled else None

    @property
    def is_recording(self):
        return self.controller is not None and self.controller.is_recording

    def start_measurement(self, measurement_id, cpap_pressure_cmH2O=None):
        if not self.hardware_enabled:
            return RecordingStartResult(
                hardware_enabled=False,
                message="Arduino recording is disabled in backend/config.py.",
            )

        self.disconnect()
        self.controller = ArduinoController()

        try:
            self.controller.start_measurement(
                measurement_id,
                cpap_pressure_cmH2O=cpap_pressure_cmH2O,
            )
        except Exception as first_error:
            self.disconnect()
            sleep(ARDUINO_RECONNECT_DELAY_SECONDS)
            self.controller = ArduinoController()
            try:
                self.controller.start_measurement(
                    measurement_id,
                    cpap_pressure_cmH2O=cpap_pressure_cmH2O,
                )
            except Exception as second_error:
                self.disconnect()
                raise RuntimeError(
                    "Could not start Arduino recording after reconnecting. "
                    "Close Arduino Serial Monitor or any other app using the port, "
                    "then try again. "
                    f"Last error: {second_error}"
                ) from first_error

        return RecordingStartResult(
            hardware_enabled=True,
            message="Arduino recording started.",
        )

    def stop_measurement(self):
        if self.controller is not None:
            controller = self.controller
            try:
                controller.stop_measurement()
            finally:
                self.disconnect()

    def disconnect(self):
        if self.controller is None:
            return

        controller = self.controller
        self.controller = None

        try:
            controller.disconnect()
        except Exception:
            pass

    def reset_device_connection(self):
        if not self.hardware_enabled:
            return "Arduino recording is disabled in backend/config.py."

        if self.controller is None:
            self.controller = ArduinoController()

        try:
            if self.controller.is_recording:
                self.controller.stop_measurement()
            else:
                self.controller.connect()
                if self.controller.send_device_commands:
                    self.controller.client.stop_streaming()
        finally:
            self.disconnect()
            self.controller = ArduinoController()

        return "Arduino connection reset."

    def get_recording_problem(self):
        """Operator-facing message when the live recording has stalled or failed."""
        if self.controller is None:
            return None
        return self.controller.get_recording_problem()

    def get_recent_samples(self):
        if self.controller is None:
            return []

        return self.controller.get_recent_samples()
