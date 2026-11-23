import os
import re
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
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
DEFAULT_PROBE_TIMEOUT_SECONDS = 2.5

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Serial ports every OS lists that are never the rig. They matter because they break
# the single-port fallback: a Mac always shows "Bluetooth-Incoming-Port" and
# "debug-console", and a Windows PC with Bluetooth shows two "Standard Serial over
# Bluetooth link" COM ports, so the Arduino is never the only port on the list.
_IGNORED_PORT_PATTERNS = (
    "bluetooth",
    "debug-console",
    "wlan-debug",
    "incoming-port",
)
# Linux on-board / SoC UARTs (/dev/ttyS0, Raspberry Pi /dev/ttyAMA0) carry no USB IDs.
_LINUX_BUILTIN_UART = re.compile(r"^/dev/tty(S|AMA|mxc)\d+$")

# Replies that prove the firmware in backend/device/firmware is on the other end.
_PROBE_SIGNATURES = ("# STATUS_", "# READY", "# WAITING FOR START")


class ArduinoSerialError(RuntimeError):
    pass


# The last port that was chosen in the UI or opened successfully. Every part of the
# app (patient recording, calibration, system check) builds its own client, so this
# lets them all reuse one detection instead of each re-scanning or re-probing.
_remembered_port = None


def remember_port(port):
    global _remembered_port
    _remembered_port = port or None


def remembered_port():
    return _remembered_port


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
        if details and details != "n/a" and details != self.device:
            return f"{self.device} ({details})"
        return self.device


@dataclass(frozen=True)
class SensorSample:
    arduino_index: int
    pressure_cmH2O: float
    flow_lps: float
    raw_line: str


@dataclass(frozen=True)
class DetectedPort:
    """One serial port found by scan_for_arduinos()."""

    port: SerialPortInfo
    likely_arduino: bool
    confirmed: bool | None  # True/False after a probe, None if not probed

    @property
    def label(self):
        if self.confirmed:
            status = "FOT device confirmed"
        elif self.likely_arduino:
            status = "Arduino" if self.confirmed is None else "Arduino, no reply"
        else:
            status = "USB serial" if self.confirmed is None else "no reply"
        return f"{self.port.display_name} — {status}"


def _is_ignored_port(port_info):
    if any(pattern in port_info.searchable_text for pattern in _IGNORED_PORT_PATTERNS):
        return True
    if IS_LINUX and port_info.vid is None and _LINUX_BUILTIN_UART.match(port_info.device):
        return True
    return False


def _dedupe_macos_ports(ports):
    """macOS exposes each device twice, /dev/tty.X and /dev/cu.X. Keep the cu. one.

    The tty. node blocks on open until carrier-detect is asserted, which an Arduino
    never does, so opening it can hang. pyserial normally lists only cu. nodes, but
    this keeps a stray tty. entry from showing up as a second "Arduino".
    """
    callout = {port.device for port in ports if port.device.startswith("/dev/cu.")}
    return [
        port
        for port in ports
        if not (
            port.device.startswith("/dev/tty.")
            and port.device.replace("/dev/tty.", "/dev/cu.", 1) in callout
        )
    ]


def list_serial_port_infos(include_ignored=False):
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

    if IS_MACOS:
        ports = _dedupe_macos_ports(ports)

    if not include_ignored:
        ports = [port for port in ports if not _is_ignored_port(port)]

    return sorted(ports, key=lambda port: _natural_sort_key(port.device))


def _natural_sort_key(text):
    # COM10 after COM9, ttyACM10 after ttyACM9.
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


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
    strings, and those depend on WHICH DRIVER the OS happened to attach. When the Uno
    R4 binds to the generic Windows usbser.sys driver, it enumerates as
    "USB Serial Device (COM5)" with manufacturer "Microsoft" -- no "Arduino", "UNO",
    "R4" or "Renesas" anywhere, so every hint misses and the device is declared absent
    even though it is sitting right there. The USB vendor ID is burned into the board
    and does not change with the driver or the OS, so it identifies the hardware
    reliably on Windows, macOS and Linux alike.

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


def _platform_open_hint(error):
    """Add the usual OS-specific fix to a failed open."""
    text = str(error).lower()
    if IS_LINUX and ("permission" in text or "errno 13" in text):
        return (
            " On Linux your user needs serial access: run "
            "'sudo usermod -aG dialout $USER', then log out and back in."
        )
    if "busy" in text or "access is denied" in text or "permission" in text:
        return (
            " The port is in use by another program -- close the Arduino IDE serial "
            "monitor or any other copy of this app."
        )
    return ""


def serial_open_kwargs():
    """Extra serial.Serial() arguments for this OS.

    Windows already gives one process exclusive use of a COM port. POSIX does not:
    two handles on /dev/ttyACM0 would silently split the incoming stream between them.
    exclusive=True takes an flock so a second open fails loudly instead.
    """
    return {} if IS_WINDOWS else {"exclusive": True}


def find_arduino_candidates(ports, port_hints=None, vendor_ids=None, clone_vendor_ids=None):
    """Split ports into (likely Arduinos, possible Arduinos).

    likely: the board's own USB vendor ID, or failing that a name/description hint.
    possible: a generic USB-serial chip (CH340, FTDI, CP210x) as used on clone boards.
              Those chips are also used by plenty of non-Arduino gadgets, so a
              possible port is only accepted after the firmware answers a probe.
    """
    likely = [
        port for port in ports if _port_matches_vendor_ids(port, vendor_ids or ())
    ] or [
        port for port in ports if _port_matches_hints(port, port_hints or [])
    ]
    likely_devices = {port.device for port in likely}
    possible = [
        port
        for port in ports
        if port.device not in likely_devices
        and _port_matches_vendor_ids(port, clone_vendor_ids or ())
    ]
    return likely, possible


def probe_port(
    device,
    baudrate=DEFAULT_BAUDRATE,
    timeout_seconds=DEFAULT_PROBE_TIMEOUT_SECONDS,
):
    """Return True if the FOT firmware answers on this port.

    Sends STATUS (harmless: the firmware only replies "# STATUS_IDLE/RECORDING") and
    repeats it, because boards with a USB-serial bridge (Uno R3, clones) reset when the
    port opens and ignore input until their bootloader hands over (~1.5 s). The Uno R4
    does not reset and usually answers on the first try.
    """
    try:
        handle = serial.Serial(
            device,
            baudrate,
            timeout=0.05,
            write_timeout=0.5,
            **serial_open_kwargs(),
        )
    except (OSError, ValueError, serial.SerialException) as error:
        log_debug(f"[DEBUG] Probe could not open {device}: {error}")
        return False

    received = ""
    try:
        deadline = monotonic() + float(timeout_seconds)
        next_send = 0.0
        while monotonic() < deadline:
            now = monotonic()
            if now >= next_send:
                try:
                    handle.write(b"STATUS\n")
                except (OSError, serial.SerialException):
                    pass
                next_send = now + 0.4
            chunk = handle.read(handle.in_waiting or 1)
            if chunk:
                received += chunk.decode("ascii", errors="replace")
                upper = received.upper()
                if any(signature in upper for signature in _PROBE_SIGNATURES):
                    log_debug(f"[DEBUG] Probe confirmed FOT firmware on {device}")
                    return True
                received = received[-512:]
        log_debug(f"[DEBUG] Probe got no firmware reply on {device}")
        return False
    except (OSError, serial.SerialException) as error:
        log_debug(f"[DEBUG] Probe failed on {device}: {error}")
        return False
    finally:
        try:
            handle.close()
        except (OSError, serial.SerialException):
            pass


def probe_ports(ports, baudrate=DEFAULT_BAUDRATE, timeout_seconds=DEFAULT_PROBE_TIMEOUT_SECONDS):
    """Probe several ports at once; return the ones running the FOT firmware."""
    ports = list(ports)
    if not ports:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(ports))) as pool:
        results = list(
            pool.map(lambda port: probe_port(port.device, baudrate, timeout_seconds), ports)
        )
    return [port for port, ok in zip(ports, results) if ok]


def scan_for_arduinos(
    port_hints=None,
    vendor_ids=None,
    clone_vendor_ids=None,
    probe=True,
    skip_probe_devices=(),
    baudrate=DEFAULT_BAUDRATE,
    timeout_seconds=DEFAULT_PROBE_TIMEOUT_SECONDS,
):
    """List every connected Arduino-like port, best first.

    With probe=True each candidate is opened and asked for STATUS, so the result says
    which port actually has the FOT firmware. Ports in skip_probe_devices (e.g. one
    this app already holds open) are listed without being opened.
    """
    ports = list_serial_port_infos()
    likely, possible = find_arduino_candidates(
        ports, port_hints, vendor_ids, clone_vendor_ids
    )
    skip = {_normalise_port(device) for device in skip_probe_devices or ()}
    to_probe = [
        port
        for port in likely + possible
        if _normalise_port(port.device) not in skip
    ] if probe else []
    confirmed = {port.device for port in probe_ports(to_probe, baudrate, timeout_seconds)}
    probed = {port.device for port in to_probe}

    detected = [
        DetectedPort(
            port=port,
            likely_arduino=port in likely,
            confirmed=(port.device in confirmed) if port.device in probed else None,
        )
        for port in likely + possible
    ]
    # Confirmed first, then likely, then possible; stable within each group.
    return sorted(
        detected,
        key=lambda item: (not item.confirmed, not item.likely_arduino),
    )


def resolve_arduino_port(
    preferred_port=None,
    auto_detect=True,
    port_hints=None,
    allow_single_port_fallback=True,
    vendor_ids=None,
    clone_vendor_ids=None,
    probe=True,
    baudrate=DEFAULT_BAUDRATE,
    probe_timeout_seconds=DEFAULT_PROBE_TIMEOUT_SECONDS,
):
    ports = list_serial_port_infos()
    preferred = _normalise_port(preferred_port or _remembered_port)

    if not auto_detect:
        if preferred_port:
            return preferred_port
        raise ArduinoSerialError(
            "No Arduino serial port is configured and auto-detect is disabled. "
            f"{_available_ports_message(ports)}"
        )

    # Vendor ID first: it identifies the board itself, so it beats the description text,
    # which reflects only the driver the OS attached. Text hints stay as the fallback
    # for boards whose VID is not in the list but which do name themselves.
    likely, possible = find_arduino_candidates(
        ports, port_hints, vendor_ids, clone_vendor_ids
    )

    if len(likely) == 1:
        return likely[0].device

    # A port the operator (or an earlier detection) already chose wins without
    # re-probing. This also keeps the probe away from a port this app holds open.
    if preferred:
        for port in likely:
            if _normalise_port(port.device) == preferred:
                return port.device

        for port in ports:
            if _normalise_port(port.device) == preferred:
                return port.device

    if len(likely) > 1:
        if probe:
            confirmed = probe_ports(likely, baudrate, probe_timeout_seconds)
            if len(confirmed) == 1:
                return confirmed[0].device
            if confirmed:
                likely = confirmed
        raise ArduinoSerialError(
            "Multiple Arduino-like serial ports were detected. "
            "Choose one in Device Calibration, or set ARDUINO_PORT in "
            "backend/config.py. "
            f"{_available_ports_message(likely)}"
        )

    # On macOS/Linux a USB board always reports USB IDs; ports without them are
    # Bluetooth gadgets named after the device (e.g. /dev/cu.Maker3-71CB) or on-board
    # UARTs, so they must not win the fallback. Windows keeps the old behaviour.
    fallback_ports = ports if IS_WINDOWS else [port for port in ports if port.vid is not None]
    if allow_single_port_fallback and len(fallback_ports) == 1:
        return fallback_ports[0].device

    # No board identified itself: ask the clone-chip ports whether they run our firmware.
    if probe and possible:
        confirmed = probe_ports(possible, baudrate, probe_timeout_seconds)
        if len(confirmed) == 1:
            return confirmed[0].device

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
        clone_vendor_ids=None,
        probe=True,
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
        self.clone_vendor_ids = clone_vendor_ids or ()
        self.probe = probe
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
                clone_vendor_ids=self.clone_vendor_ids,
                probe=self.probe,
                baudrate=self.baudrate,
            )

            try:
                self._serial = serial.Serial(
                    self.port,
                    self.baudrate,
                    timeout=self.timeout,
                    write_timeout=self.write_timeout,
                    **serial_open_kwargs(),
                )
                self._rx_buffer = ""

                if self.connect_delay_seconds:
                    sleep(self.connect_delay_seconds)

                if self._serial.in_waiting:
                    self._serial.reset_input_buffer()
                remember_port(self.port)
                log_debug(f"[DEBUG] Connected to Arduino on port {self.port} at {self.baudrate} baud")
            except (OSError, ValueError, serial.SerialException) as error:
                self._invalidate_serial()
                raise ArduinoSerialError(
                    f"Could not open Arduino serial port {self.port}: {error}"
                    f"{_platform_open_hint(error)}"
                ) from error

    def _invalidate_serial(self):
        """Close and forget a handle after the OS reports a device I/O failure."""
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
