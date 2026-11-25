import csv
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button
import serial


# ----------------------------- SETTINGS ------------------------------------
# None = find the Arduino automatically (Windows, macOS or Linux). To force a port,
# set e.g. "COM3" (Windows), "/dev/cu.usbmodem1101" (macOS) or "/dev/ttyACM0" (Linux).
PORT = None

# Baud rate and sample rate come from the app's own settings, so this script
# always matches the web app's flow calibration. On start it tells the device
# to run at EXCITATION_HZ with the speaker silent (FLOW_CAL=1); the device then
# streams EXCITATION_HZ x 70 rows per second, which is SAMPLE_HZ.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.config import ARDUINO_BAUDRATE as BAUD  # noqa: E402
from backend.processing.FOTImpedance import EXCITATION_HZ, SAMPLE_HZ  # noqa: E402
SYRINGE_VOLUME_L = 3.0
TARGET_STROKES = 10
PLOT_SECONDS = 8
MIN_ACTIVE_DP_CMH2O = 0.015
ACTIVE_MARGIN_S = 0.15


# ----------------------------- SERIAL --------------------------------------
if PORT is None:
    from backend.config import (
        ARDUINO_CLONE_USB_VENDOR_IDS,
        ARDUINO_PORT_HINTS,
        ARDUINO_USB_VENDOR_IDS,
    )
    from backend.device.serial_device import resolve_arduino_port

    PORT = resolve_arduino_port(
        port_hints=ARDUINO_PORT_HINTS,
        vendor_ids=ARDUINO_USB_VENDOR_IDS,
        clone_vendor_ids=ARDUINO_CLONE_USB_VENDOR_IDS,
        baudrate=BAUD,
    )
    print(f"Using Arduino on {PORT}")

ser = serial.Serial(PORT, BAUD, timeout=0)
serial_buffer = ""


def configure_device():
    """Put the device in flow-calibration mode at EXCITATION_HZ and start it.

    Without this the device keeps its current frequency (so the sample rate,
    and with it every gain, would be wrong) and the speaker keeps playing into
    the syringe measurement."""
    ser.reset_input_buffer()
    ser.write(f"CONFIG,EXCITATION_HZ={EXCITATION_HZ:g},FLOW_CAL=1\n".encode("ascii"))
    replies = ""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        replies += ser.read(ser.in_waiting or 1).decode("ascii", errors="ignore")
        upper = replies.upper()
        if "CONFIG_REJECTED" in upper:
            sys.exit(f"The device rejected {EXCITATION_HZ:g} Hz:\n{replies.strip()}")
        if "CONFIG_ACK" in upper:
            break
        time.sleep(0.02)
    upper = replies.upper()
    if "EXCITATION HZ SET TO" not in upper or "FLOW_CAL ON" not in upper:
        sys.exit(
            "The device did not confirm flow-calibration mode at "
            f"{EXCITATION_HZ:g} Hz. Check the cable and that the current firmware "
            f"is flashed. Received:\n{replies.strip() or '(nothing)'}"
        )
    ser.write(b"START\n")
    print(f"Device set to {EXCITATION_HZ:g} Hz, speaker silent, {SAMPLE_HZ:g} samples/s")


configure_device()

window_samples = int(PLOT_SECONDS * SAMPLE_HZ)
plot_dp = deque([0.0] * window_samples, maxlen=window_samples)
baseline_samples = deque(maxlen=int(SAMPLE_HZ))

recording = False
stroke_direction = 0
stroke_samples = []
stroke_baseline = 0.0
accepted_strokes = []


def parse_line(line):
    parts = line.split(",")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None


def begin_stroke(direction):
    global recording, stroke_direction, stroke_samples, stroke_baseline
    if recording:
        set_status("Finish or discard the current stroke first.")
        return
    if len(baseline_samples) < int(0.5 * SAMPLE_HZ):
        set_status("Wait at least one second with no flow before starting.")
        return

    recording = True
    stroke_direction = direction
    stroke_samples = []
    stroke_baseline = median(list(baseline_samples))
    sign = "+" if direction > 0 else "-"
    set_status(
        f"Recording {sign}3 L stroke; pre-stroke zero={stroke_baseline:+.5f} cmH2O."
    )


def finish_stroke(_event):
    global recording, stroke_samples
    if not recording:
        set_status("No stroke is currently being recorded.")
        return

    recording = False
    if len(stroke_samples) < int(0.25 * SAMPLE_HZ):
        stroke_samples = []
        set_status("Stroke discarded: fewer than 0.25 seconds of samples.")
        return

    corrected_samples = [value - stroke_baseline for value in stroke_samples]
    active_indices = [
        index
        for index, value in enumerate(corrected_samples)
        if abs(value) >= MIN_ACTIVE_DP_CMH2O
    ]
    if not active_indices:
        stroke_samples = []
        set_status(
            f"Stroke discarded: no A2 pulse exceeded {MIN_ACTIVE_DP_CMH2O:.3f} cmH2O."
        )
        return

    margin_samples = int(ACTIVE_MARGIN_S * SAMPLE_HZ)
    active_start = max(0, active_indices[0] - margin_samples)
    active_end = min(len(corrected_samples), active_indices[-1] + margin_samples + 1)
    active_samples = corrected_samples[active_start:active_end]

    pressure_time_integral = sum(active_samples) / SAMPLE_HZ
    if abs(pressure_time_integral) < 1e-6:
        stroke_samples = []
        set_status("Stroke discarded: pressure integral is too close to zero.")
        return
    if stroke_direction * pressure_time_integral <= 0.0:
        stroke_samples = []
        set_status(
            "Stroke discarded: signal sign is opposite the selected button. "
            "Use the other Start button or check the A2 tube direction."
        )
        return

    signed_volume = stroke_direction * SYRINGE_VOLUME_L
    gain = signed_volume / pressure_time_integral
    accepted_strokes.append(
        {
            "number": len(accepted_strokes) + 1,
            "direction": "push" if stroke_direction > 0 else "pull",
            "recorded_samples": len(stroke_samples),
            "recorded_duration_s": len(stroke_samples) / SAMPLE_HZ,
            "active_samples": len(active_samples),
            "active_duration_s": len(active_samples) / SAMPLE_HZ,
            "baseline_cmH2O": stroke_baseline,
            "dp_integral_cmH2O_s": pressure_time_integral,
            "gain_Lps_per_cmH2O": gain,
        }
    )
    stroke_samples = []
    update_results()


def discard_stroke(_event):
    global recording, stroke_samples
    recording = False
    stroke_samples = []
    set_status("Current stroke discarded.")


def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def update_results():
    gains = [stroke["gain_Lps_per_cmH2O"] for stroke in accepted_strokes]
    gain_median = median(gains)
    deviations = [abs(value - gain_median) / abs(gain_median) * 100 for value in gains]
    worst_deviation = max(deviations)
    push_gains = [
        stroke["gain_Lps_per_cmH2O"]
        for stroke in accepted_strokes
        if stroke["direction"] == "push"
    ]
    pull_gains = [
        stroke["gain_Lps_per_cmH2O"]
        for stroke in accepted_strokes
        if stroke["direction"] == "pull"
    ]
    push_text = "n/a" if not push_gains else f"{median(push_gains):.6f}"
    pull_text = "n/a" if not pull_gains else f"{median(pull_gains):.6f}"

    lines = [
        f"Accepted strokes: {len(gains)}/{TARGET_STROKES}",
        f"Median FLOW_GAIN: {gain_median:.6f} L/(s*cmH2O)",
        f"FLOW_GAIN_POS push: {push_text}",
        f"FLOW_GAIN_NEG pull: {pull_text}",
        f"Worst deviation from median: {worst_deviation:.2f}%",
        f"Median pre-stroke zero: "
        f"{median([s['baseline_cmH2O'] for s in accepted_strokes]):+.5f} cmH2O",
        "",
    ]
    for stroke, deviation in zip(accepted_strokes[-6:], deviations[-6:]):
        lines.append(
            f"#{stroke['number']:02d} {stroke['direction']:4s} "
            f"{stroke['active_duration_s']:.2f}s zero={stroke['baseline_cmH2O']:+.4f} "
            f"gain={stroke['gain_Lps_per_cmH2O']:.6f} "
            f"({deviation:.1f}%)"
        )

    result_text.set_text("\n".join(lines))
    if len(gains) < TARGET_STROKES:
        set_status("Stroke accepted. Record alternating full push and pull strokes.")
    elif worst_deviation <= 3.0:
        set_status("Calibration passes the 3% consistency check. Save the results.")
    else:
        set_status("Calibration varies by more than 3%; inspect strokes, leaks, and zero offset.")


def save_results(_event):
    if not accepted_strokes:
        set_status("Nothing to save yet.")
        return

    output = Path(__file__).with_name(
        f"flow_calibration_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    gains = [stroke["gain_Lps_per_cmH2O"] for stroke in accepted_strokes]
    gain_median = median(gains)
    push_gains = [
        stroke["gain_Lps_per_cmH2O"]
        for stroke in accepted_strokes
        if stroke["direction"] == "push"
    ]
    pull_gains = [
        stroke["gain_Lps_per_cmH2O"]
        for stroke in accepted_strokes
        if stroke["direction"] == "pull"
    ]

    with output.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["# FLOW_GAIN_Lps_per_cmH2O", f"{gain_median:.9f}"])
        if push_gains:
            writer.writerow(["# FLOW_GAIN_POS_push_Lps_per_cmH2O", f"{median(push_gains):.9f}"])
        if pull_gains:
            writer.writerow(["# FLOW_GAIN_NEG_pull_Lps_per_cmH2O", f"{median(pull_gains):.9f}"])
        writer.writerow(["# syringe_volume_L", SYRINGE_VOLUME_L])
        writer.writerow(["# sample_rate_Hz", SAMPLE_HZ])
        writer.writerow(
            [
                "stroke",
                "direction",
                "recorded_samples",
                "recorded_duration_s",
                "active_samples",
                "active_duration_s",
                "baseline_cmH2O",
                "dp_integral_cmH2O_s",
                "gain_Lps_per_cmH2O",
            ]
        )
        for stroke in accepted_strokes:
            writer.writerow(stroke.values())

    set_status(f"Saved: {output.name}")


def set_status(message):
    status_text.set_text(message)


def update(_frame):
    global serial_buffer
    data = ser.read(ser.in_waiting or 1).decode("ascii", errors="ignore")
    serial_buffer += data

    while "\n" in serial_buffer:
        raw, serial_buffer = serial_buffer.split("\n", 1)
        raw = raw.strip()
        if not raw or raw.startswith("#") or raw.startswith("idx"):
            continue

        parsed = parse_line(raw)
        if parsed is None:
            continue

        _idx, _pressure, dp_cm_h2o = parsed
        plot_dp.append(dp_cm_h2o)
        if recording:
            stroke_samples.append(dp_cm_h2o)
        else:
            baseline_samples.append(dp_cm_h2o)

    dp_line.set_ydata(plot_dp)
    recording_text.set_text(
        f"{'RECORDING' if recording else 'READY'}"
        f" | A2 = {plot_dp[-1]:+.4f} cmH2O"
    )
    return dp_line, recording_text


def close_serial(_event):
    if ser.is_open:
        # Back to normal: speaker on again and streaming stopped.
        try:
            ser.write(b"STOP\nCONFIG,FLOW_CAL=0\n")
            ser.flush()
        except serial.SerialException:
            pass
        ser.close()


# ----------------------------- DISPLAY -------------------------------------
fig, ax = plt.subplots(figsize=(11, 7))
fig.subplots_adjust(bottom=0.30, right=0.70)

(dp_line,) = ax.plot(range(window_samples), list(plot_dp), color="tab:blue")
ax.axhline(0.0, color="black", linewidth=0.8)
ax.set_ylim(-1.0, 1.0)
ax.set_xlabel("Recent samples")
ax.set_ylabel("A2 differential pressure (cmH2O)")
ax.set_title("Hans Rudolph 3 L syringe flow calibration")
ax.grid(True)

recording_text = ax.text(0.01, 0.97, "READY", transform=ax.transAxes, va="top")
status_text = fig.text(
    0.05,
    0.19,
    "With no flow, confirm A2 is near zero. Then record a full stroke.",
    fontsize=10,
)
result_text = fig.text(0.72, 0.88, "No accepted strokes.", va="top", family="monospace")

push_ax = fig.add_axes([0.05, 0.07, 0.13, 0.07])
pull_ax = fig.add_axes([0.20, 0.07, 0.13, 0.07])
finish_ax = fig.add_axes([0.35, 0.07, 0.13, 0.07])
discard_ax = fig.add_axes([0.50, 0.07, 0.13, 0.07])
save_ax = fig.add_axes([0.65, 0.07, 0.13, 0.07])

push_button = Button(push_ax, "Start +3 L")
pull_button = Button(pull_ax, "Start -3 L")
finish_button = Button(finish_ax, "Finish stroke")
discard_button = Button(discard_ax, "Discard")
save_button = Button(save_ax, "Save results")

push_button.on_clicked(lambda event: begin_stroke(+1))
pull_button.on_clicked(lambda event: begin_stroke(-1))
finish_button.on_clicked(finish_stroke)
discard_button.on_clicked(discard_stroke)
save_button.on_clicked(save_results)
fig.canvas.mpl_connect("close_event", close_serial)

animation = FuncAnimation(fig, update, interval=25, blit=False, cache_frame_data=False)
plt.show()
