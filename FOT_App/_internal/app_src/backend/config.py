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
)
# USB vendor IDs that identify the board regardless of which driver Windows attached.
# Checked BEFORE the text hints above, because when the Uno R4 binds to the generic
# usbser.sys driver it reports itself as "USB Serial Device (COMn)" / "Microsoft" and
# none of those hints match. 0x2341 = Arduino SA (the R4's VID), 0x2A03 = Arduino.org.
ARDUINO_USB_VENDOR_IDS = (0x2341, 0x2A03)
ARDUINO_AUTO_DETECT_SINGLE_PORT = True
ARDUINO_BAUDRATE = 115200
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

DEFAULT_EXCITATION_HZ = 6.0
DEFAULT_SAMPLE_HZ = 420
DEFAULT_DAC_AMPLITUDE = None

RAW_MEASUREMENT_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "measurements"
SAVE_RAW_SAMPLES_TO_DATABASE = False
