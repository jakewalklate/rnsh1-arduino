from collections import deque
from dataclasses import dataclass
from threading import RLock
from time import monotonic, sleep

import serial
from serial.tools import list_ports

from backend.config import log_debug
from backend.processing.FlowGainCalibration import rescale_flow_lps


DEFAULT_BAUDRATE = 115200
DEFAULT_TIMEOUT_SECONDS = 0.05
DEFAULT_WRITE_TIMEOUT_SECONDS = 1.0
DEFAULT_COMMAND_ACK_TIMEOUT_SECONDS = 3.0


class ArduinoSerialError(RuntimeError):
    pass


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    description: str
    manufacturer: str
    hwid: str
    vid: int | None
    pid: int | None

    @property
    def searchable_text(self):
        return " ".join(
            value
            for value in (
                self.device,
                self.description,
                self.manufacturer,
                self.hwid,
            )
            if value
        ).lower()

    @property
    def display_name(self):
        details = self.description or self.manufacturer or self.hwid
        return f"{self.device} ({details})" if details else self.device


@dataclass(frozen=True)
class SensorSample:
    arduino_index: int
    pressure_cmH2O: float
    flow_lps: float
    raw_line: str


def list_serial_port_infos():
    ports = []

    for port in list_ports.comports():
        ports.append(
            SerialPortInfo(
                device=port.device,
                description=port.description or "",
                manufacturer=port.manufacturer or "",
                hwid=port.hwid or "",
                vid=port.vid,
                pid=port.pid,
            )
        )

    return ports


def list_serial_ports():
    return [port.device for port in list_serial_port_infos()]


def _normalise_port(port):
    return str(port).strip().lower() if port else None


def _port_matches_hints(port_info, hints):
    searchable_text = port_info.searchable_text

    return any(str(hint).lower() in searchable_text for hint in hints or [])


def _port_matches_vendor_ids(port_info, vendor_ids):
    """Match a port by its USB vendor ID.

    WHY this exists alongside the text hints: the hints match description/manufacturer
    strings, and those depend on WHICH DRIVER Windows happened to attach. When the Uno
    R4 binds to the generic usbser.sys driver, it enumerates as
    "USB Serial Device (COM5)" with manufacturer "Microsoft" -- no "Arduino", "UNO",
    "R4" or "Renesas" anywhere, so every hint misses and the device is declared absent
    even though it is sitting right there. The USB vendor ID is burned into the board
    and does not change with the driver, so it identifies the hardware reliably.

    This was a real failure: the rig worked one day and reported "Device not found on
    the serial port" the next, because the single-port fallback below had been silently
    carrying the detection. Two Bluetooth COM ports appeared, the port count went from
    1 to 3, the fallback stopped firing and nothing was left to find COM5.
    """
    if port_info.vid is None:
        return False

    return any(int(port_info.vid) == int(vendor_id) for vendor_id in vendor_ids or ())


def _available_ports_message(ports):
    if not ports:
        return "No serial ports were detected."

    return "Available ports: " + ", ".join(port.display_name for port in ports)


def resolve_arduino_port(
    preferred_port=None,
    auto_detect=True,
    port_hints=None,
    allow_single_port_fallback=True,
    vendor_ids=None,
):
    ports = list_serial_port_infos()
    preferred = _normalise_port(preferred_port)

    if not auto_detect:
        if preferred_port:
            return preferred_port
        raise ArduinoSerialError(
            "No Arduino serial port is configured and auto-detect is disabled. "
            f"{_available_ports_message(ports)}"
        )

    # Vendor ID first: it identifies the board itself, so it beats the description text,
    # which reflects only the driver Windows attached. Text hints stay as the fallback
    # for boards/clones whose VID is not in the list but which do name themselves.
    hinted_ports = [
        port for port in ports if _port_matches_vendor_ids(port, vendor_ids or ())
    ] or [
        port for port in ports if _port_matches_hints(port, port_hints or [])
    ]

    if len(hinted_ports) == 1:
        return hinted_ports[0].device

    if preferred:
        for port in hinted_ports:
            if _normalise_port(port.device) == preferred:
                return port.device

        for port in ports:
            if _normalise_port(port.device) == preferred:
                return port.device

    if len(hinted_ports) > 1:
        raise ArduinoSerialError(
            "Multiple Arduino-like serial ports were detected. "
            "Set ARDUINO_PORT in backend/config.py to choose one. "
            f"{_available_ports_message(hinted_ports)}"
        )

    if allow_single_port_fallback and len(ports) == 1:
        return ports[0].device

    # Name what WAS seen. The bare "Device not found" gave the operator nothing to act
    # on, and hid the fact that the port was present and merely unrecognised.
    raise ArduinoSerialError(
        "Device not found on the serial port. "
        f"{_available_ports_message(ports)}"
    )


def parse_sample_line(line, apply_flow_gain=True):
    raw_line = line.strip()

    if not raw_line:
        return None

    lowered = raw_line.lower()
    if raw_line.startswith("#") or lowered.startswith("idx"):
        return None

    parts = [part.strip() for part in raw_line.split(",")]

    if len(parts) != 3:
        return None

    try:
        flow = float(parts[2])
        if apply_flow_gain:
            flow = rescale_flow_lps(flow)
        return SensorSample(
            arduino_index=int(parts[0]),
            pressure_cmH2O=float(parts[1]),
            flow_lps=flow,
            raw_line=raw_line
        )
    except ValueError:
        return None


class ArduinoSerialClient:
    """Serial client for the Arduino pressure/flow stream.

    The firmware stays idle until the GUI sends START, and it streams CSV rows
    until the GUI sends STOP.
    """

    def __init__(
        self,
        port=None,
        baudrate=DEFAULT_BAUDRATE,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        write_timeout=DEFAULT_WRITE_TIMEOUT_SECONDS,
        connect_delay_seconds=0,
        auto_detect=True,
        port_hints=None,
        allow_single_port_fallback=True,
        vendor_ids=None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.connect_delay_seconds = connect_delay_seconds
        self.auto_detect = auto_detect
        self.port_hints = port_hints or ()
        self.allow_single_port_fallback = allow_single_port_fallback
        self.vendor_ids = vendor_ids or ()
        self._serial = None
        self._rx_buffer = ""
        self._notices = deque(maxlen=200)
        self._io_lock = RLock()

    @property
    def is_connected(self):
        return self._serial is not None and self._serial.is_open

    def connect(self, port=None):
        with self._io_lock:
            if port is not None:
                self.port = port

            if self.is_connected:
                return

            self.port = resolve_arduino_port(
                preferred_port=self.port,
                auto_detect=self.auto_detect,
                port_hints=self.port_hints,
                allow_single_port_fallback=self.allow_single_port_fallback,
                vendor_ids=self.vendor_ids,
            )

            try:
                self._serial = serial.Serial(
                    self.port,
                    self.baudrate,
                    timeout=self.timeout,
                    write_timeout=self.write_timeout,
                )
                self._rx_buffer = ""

                if self.connect_delay_seconds:
                    sleep(self.connect_delay_seconds)

                if self._serial.in_waiting:
                    self._serial.reset_input_buffer()
                log_debug(f"[DEBUG] Connected to Arduino on port {self.port} at {self.baudrate} baud")
            except (OSError, serial.SerialException) as error:
                self._invalidate_serial()
                raise ArduinoSerialError(
                    f"Could not open Arduino serial port {self.port}: {error}"
                ) from error

    def _invalidate_serial(self):
        """Close and forget a handle after Windows reports a device I/O failure."""
        serial_port = self._serial
        self._serial = None
        self._rx_buffer = ""
        if serial_port is None:
            return
        try:
            serial_port.close()
        except (OSError, serial.SerialException):
            pass

    def disconnect(self):
        with self._io_lock:
            self._invalidate_serial()

    def reset_input_buffer(self):
        with self._io_lock:
            if not self.is_connected:
                return
            try:
                self._serial.reset_input_buffer()
                self._rx_buffer = ""
                self._notices.clear()
            except (OSError, serial.SerialException) as error:
                self._invalidate_serial()
                raise ArduinoSerialError(
                    f"Could not clear Arduino input on {self.port}: {error}"
                ) from error

    def send_command(self, command):
        cleaned_command = command.strip()
        if not cleaned_command:
            return

        with self._io_lock:
            if not self.is_connected:
                raise ArduinoSerialError("Cannot send command before connecting.")
            try:
                log_debug(f"[DEBUG ARDUINO TX] -> {cleaned_command}")
                self._serial.write(f"{cleaned_command}\n".encode("ascii"))
            except (OSError, serial.SerialException) as error:
                self._invalidate_serial()
                raise ArduinoSerialError(
                    f"Could not send {cleaned_command!r} to {self.port}: {error}"
                ) from error

    def wait_for_notice(self, expected_text, timeout_seconds):
        """Drain the port until a firmware notice confirms a command was handled."""
        expected = str(expected_text).upper()
        deadline = monotonic() + float(timeout_seconds)
        observed = []

        while monotonic() < deadline:
            self.read_available()
            notices = self.drain_notices()
            observed.extend(notices)
            if any(expected in notice.upper() for notice in notices):
                return
            sleep(0.02)

        detail = f" Notices received: {'; '.join(observed[-5:])}" if observed else ""
        raise ArduinoSerialError(
            f"Arduino did not acknowledge {expected_text!r} within "
            f"{float(timeout_seconds):g} s.{detail}"
        )

    def start_streaming(
        self,
        require_ack=False,
        ack_timeout_seconds=DEFAULT_COMMAND_ACK_TIMEOUT_SECONDS,
    ):
        self.send_command("START")
        if require_ack:
            self.wait_for_notice("# STARTED", ack_timeout_seconds)

    def stop_streaming(self):
        self.send_command("STOP")

    def zero(self):
        self.send_command("ZERO")

    def quick_calibrate(self):
        self.send_command("CALIBRATE")

    def set_flow_calibration_mode(self, enabled):
        self.send_command(f"CONFIG,FLOW_CAL={1 if enabled else 0}")

    def configure(
        self,
        excitation_hz=None,
        sample_hz=None,
        dac_amplitude=None,
        cpap_pressure_cmH2O=None
    ):
        settings = []

        if excitation_hz is not None:
            settings.append(f"EXCITATION_HZ={float(excitation_hz):g}")

        if sample_hz is not None:
            settings.append(f"SAMPLE_HZ={float(sample_hz):g}")

        if dac_amplitude is not None:
            settings.append(f"DAC_AMPL={int(dac_amplitude)}")

        if cpap_pressure_cmH2O is not None:
            settings.append(f"CPAP_CM_H2O={float(cpap_pressure_cmH2O):g}")

        if settings:
            self.send_command(f"CONFIG,{','.join(settings)}")

    def read_available(self, max_bytes=8192, apply_flow_gain=True):
        with self._io_lock:
            if not self.is_connected:
                raise ArduinoSerialError("Cannot read samples before connecting.")

            try:
                waiting = self._serial.in_waiting
                if waiting <= 0:
                    return []

                payload = self._serial.read(min(waiting, max_bytes))
                if not payload:
                    return []
            except (OSError, serial.SerialException) as error:
                self._invalidate_serial()
                raise ArduinoSerialError(
                    f"Arduino serial read failed on {self.port}: {error}"
                ) from error

            self._rx_buffer += payload.decode("ascii", errors="replace")
            lines = self._rx_buffer.splitlines()

            if self._rx_buffer.endswith(("\n", "\r")):
                self._rx_buffer = ""
            elif lines:
                self._rx_buffer = lines.pop()

            samples = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#"):
                    log_debug(f"[DEBUG ARDUINO RX] <- {stripped}")
                    self._notices.append(stripped)
                    continue

                sample = parse_sample_line(line, apply_flow_gain=apply_flow_gain)
                if sample is not None:
                    samples.append(sample)

            return samples

    def drain_notices(self):
        with self._io_lock:
            notices = list(self._notices)
            self._notices.clear()
            return notices

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.disconnect()
