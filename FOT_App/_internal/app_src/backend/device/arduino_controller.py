from backend.config import (
    ARDUINO_AUTO_DETECT,
    ARDUINO_AUTO_DETECT_SINGLE_PORT,
    ARDUINO_USB_VENDOR_IDS,
    ARDUINO_CLONE_USB_VENDOR_IDS,
    ARDUINO_PROBE_PORTS,
    ARDUINO_BAUDRATE,
    ARDUINO_CONNECT_DELAY_SECONDS,
    ARDUINO_PORT,
    ARDUINO_PORT_HINTS,
    ARDUINO_SEND_COMMANDS,
    ARDUINO_TIMEOUT_SECONDS,
    ARDUINO_WRITE_TIMEOUT_SECONDS,
    DEFAULT_DAC_AMPLITUDE,
    DEFAULT_EXCITATION_HZ,
    DEFAULT_SAMPLE_HZ,
)
from backend.device.measurement_acquisition import MeasurementSerialRecorder
from backend.device.serial_device import ArduinoSerialClient


class ArduinoController:
    def __init__(
        self,
        port=ARDUINO_PORT,
        baudrate=ARDUINO_BAUDRATE,
        timeout=ARDUINO_TIMEOUT_SECONDS,
        write_timeout=ARDUINO_WRITE_TIMEOUT_SECONDS,
        connect_delay_seconds=ARDUINO_CONNECT_DELAY_SECONDS,
        auto_detect=ARDUINO_AUTO_DETECT,
        port_hints=ARDUINO_PORT_HINTS,
        allow_single_port_fallback=ARDUINO_AUTO_DETECT_SINGLE_PORT,
        vendor_ids=ARDUINO_USB_VENDOR_IDS,
        clone_vendor_ids=ARDUINO_CLONE_USB_VENDOR_IDS,
        probe=ARDUINO_PROBE_PORTS,
        send_device_commands=ARDUINO_SEND_COMMANDS,
    ):
        self.client = ArduinoSerialClient(
            port=port,
            baudrate=baudrate,
            timeout=timeout,
            write_timeout=write_timeout,
            connect_delay_seconds=connect_delay_seconds,
            auto_detect=auto_detect,
            port_hints=port_hints,
            allow_single_port_fallback=allow_single_port_fallback,
            vendor_ids=vendor_ids,
            clone_vendor_ids=clone_vendor_ids,
            probe=probe,
        )
        self.send_device_commands = send_device_commands
        self.recorder = None

    @property
    def is_connected(self):
        return self.client.is_connected

    @property
    def is_recording(self):
        return self.recorder is not None and self.recorder.is_recording

    def connect(self):
        self.client.connect()

    def disconnect(self):
        if self.is_recording:
            self.stop_measurement()
        self.client.disconnect()

    def configure(
        self,
        excitation_hz=DEFAULT_EXCITATION_HZ,
        sample_hz=DEFAULT_SAMPLE_HZ,
        dac_amplitude=DEFAULT_DAC_AMPLITUDE,
        cpap_pressure_cmH2O=None,
    ):
        if not self.is_connected:
            self.connect()

        if self.send_device_commands:
            self.client.configure(
                excitation_hz=excitation_hz,
                sample_hz=sample_hz,
                dac_amplitude=dac_amplitude,
                cpap_pressure_cmH2O=cpap_pressure_cmH2O,
            )

    def prepare_for_measurement_start(self):
        if self.is_recording:
            self.stop_measurement()

        if not self.is_connected:
            self.connect()

        if self.send_device_commands:
            self.client.stop_streaming()

        self.client.reset_input_buffer()

    def start_measurement(self, measurement_id, cpap_pressure_cmH2O=None):
        self.prepare_for_measurement_start()
        self.configure(cpap_pressure_cmH2O=cpap_pressure_cmH2O)
        self.recorder = MeasurementSerialRecorder(
            self.client,
            measurement_id,
            send_device_commands=self.send_device_commands,
        )
        self.recorder.start()

    def poll_samples(self):
        if not self.is_recording:
            return []
        return self.recorder.poll()

    def get_recent_samples(self):
        if self.recorder is None:
            return []

        return self.recorder.get_recent_samples()

    def get_recording_error(self):
        if self.recorder is None:
            return None
        return self.recorder.last_error

    def get_recording_problem(self):
        if self.recorder is None:
            return None
        return self.recorder.get_problem()

    def stop_measurement(self):
        if self.recorder is None:
            return

        try:
            self.recorder.stop()
        finally:
            self.recorder = None

    def zero(self):
        if not self.is_connected:
            self.connect()
        if self.send_device_commands:
            self.client.zero()

    def quick_calibrate(self):
        if not self.is_connected:
            self.connect()
        if self.send_device_commands:
            self.client.quick_calibrate()
