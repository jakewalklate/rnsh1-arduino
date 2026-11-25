from datetime import datetime
from pathlib import Path


DEBUG_LOG_FILE = Path(__file__).resolve().parents[2] / "debug.log"


def log_debug(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    formatted = f"[{timestamp}] {msg}"
    print(formatted, flush=True)
    try:
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass


ARDUINO_ENABLED = True
ARDUINO_AUTO_DETECT = True
ARDUINO_PORT = None
ARDUINO_PORT_HINTS = (
    "Arduino",
    "UNO",
    "R4",
    "RA4M1",
    "Renesas",
    # STM32 Nucleo: the on-board ST-LINK's virtual COM port is named
    # "STMicroelectronics STLink Virtual COM Port (COMn)" on Windows and
    # "STM32 STLink" on macOS/Linux.
    "STLink",
    "STMicroelectronics",
    # Native-USB boards (Uno R4, Leonardo, Due) get these device names on macOS
    # (/dev/cu.usbmodem1101) and Linux (/dev/ttyACM0) whatever the description says.
    "usbmodem",
    "ttyACM",
)
# USB vendor IDs that identify the board regardless of OS or of which driver it attached.
# Checked BEFORE the text hints above, because when the Uno R4 binds to the generic
# usbser.sys driver it reports itself as "USB Serial Device (COMn)" / "Microsoft" and
# none of those hints match. 0x2341 = Arduino SA (the R4's VID), 0x2A03 = Arduino.org,
# 0x0483 = STMicroelectronics (the ST-LINK on the STM32 Nucleo-G071RB). If more than
# one of these is plugged in, each is sent STATUS and the one running the FOT
# firmware is used.
ARDUINO_USB_VENDOR_IDS = (0x2341, 0x2A03, 0x0483)
# Generic USB-serial chips used on clone boards: 0x1A86 = WCH CH340, 0x0403 = FTDI,
# 0x10C4 = Silicon Labs CP210x. Other gadgets use these too, so a port with one of
# these IDs is only accepted after the firmware answers a STATUS probe.
ARDUINO_CLONE_USB_VENDOR_IDS = (0x1A86, 0x0403, 0x10C4)
# When several candidate ports are present, open each one and send STATUS to find
# the one running the FOT firmware (backend/device/firmware).
ARDUINO_PROBE_PORTS = True
ARDUINO_AUTO_DETECT_SINGLE_PORT = True
# 460800: the STM32 Nucleo sends through the ST-LINK UART, where 115200 baud is too slow
# for 11/19 Hz (1330 rows/s, ~28 KB/s). Must match USART2's baud rate in CubeMX.
# The Uno R4's native USB ignores this value, so it still works too.
ARDUINO_BAUDRATE = 460800
ARDUINO_TIMEOUT_SECONDS = 0.05
ARDUINO_WRITE_TIMEOUT_SECONDS = 1.0
ARDUINO_CONNECT_DELAY_SECONDS = 1.5
ARDUINO_RECONNECT_DELAY_SECONDS = 0.5
ARDUINO_SEND_COMMANDS = True

# These values must match FLOW_GAIN_POS and FLOW_GAIN_NEG in the firmware.
# Incoming flow is converted back to differential pressure before the saved
# syringe calibration is applied.
FIRMWARE_FLOW_GAIN_POS = 2.86
FIRMWARE_FLOW_GAIN_NEG = 2.8428

# Excitation frequencies the firmware accepts (allowedFrequency in the .ino), offered
# in the recording's "Configure Test Parameters" dialog. The firmware outputs one
# 70-entry sine table per cycle, so the sample rate is always frequency x 70.
EXCITATION_FREQUENCY_OPTIONS_HZ = (5.0, 6.0, 11.0, 19.0)
SAMPLES_PER_EXCITATION_CYCLE = 70
DEFAULT_EXCITATION_HZ = 5.0
DEFAULT_SAMPLE_HZ = DEFAULT_EXCITATION_HZ * SAMPLES_PER_EXCITATION_CYCLE
# Every recording is analysed at the frequency it was recorded at, with that
# frequency's own gain/phase calibration (FOTPipeline.process_measurement_csv), so any
# frequency above can be used once it has been calibrated.
DEFAULT_DAC_AMPLITUDE = None

RAW_MEASUREMENT_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "measurements"
SAVE_RAW_SAMPLES_TO_DATABASE = False
