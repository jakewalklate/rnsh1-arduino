"""Recordings are analysed at the frequency they were recorded at.

Before this, every recording was analysed as 6 Hz: 6-cycle blocks (0.32 s at 19 Hz),
a 420 Hz sample rate for the breath analysis and tidal volume, and the 6 Hz phase
angle and gain whatever the recording's frequency."""

import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend.device.calibration_recorder import CalibrationPointRecorder
from backend.processing import FOTBreath, FOTImpedance, FOTPhaseCalibration, FOTPipeline
from backend.processing.FOTGainPhaseCalibration import analysed_at_recorded_frequency
from backend.processing.FOTImpedance import (
    LUT_SIZE,
    block_samples_for,
    cycles_per_block,
    sample_hz_for,
)


class BlockSizeTests(unittest.TestCase):
    def test_a_block_is_one_second_at_every_frequency(self):
        for hz in (5.0, 6.0, 11.0, 19.0):
            self.assertEqual(cycles_per_block(hz), int(hz))
            self.assertAlmostEqual(block_samples_for(hz) / sample_hz_for(hz), 1.0)

    def test_six_hz_is_unchanged(self):
        self.assertEqual(FOTImpedance.BLOCK_SAMPLES, 420)
        self.assertEqual(FOTImpedance.SAMPLE_HZ, 420.0)

    def test_calibration_recorder_uses_its_own_frequency(self):
        recorder = CalibrationPointRecorder(object(), "off", excitation_hz=19.0)
        self.assertEqual(recorder.block_samples, 19 * LUT_SIZE)
        self.assertEqual(recorder.sample_hz, 19.0 * LUT_SIZE)


class BreathLowpassTests(unittest.TestCase):
    def test_tone_is_removed_at_every_frequency(self):
        for hz in (5.0, 6.0, 11.0, 19.0):
            fs = sample_hz_for(hz)
            t = np.arange(int(20 * fs)) / fs
            breathing = 0.3 * np.sin(2 * np.pi * 0.25 * t)
            tone = 0.05 * np.sin(2 * np.pi * hz * t)
            out = FOTBreath._lowpass(breathing + tone, fs, FOTBreath.BREATH_LP_HZ)
            mid = slice(int(5 * fs), int(15 * fs))      # away from the padded edges
            # What is left of the tone after filtering the tone alone.
            leak = FOTBreath._lowpass(tone, fs, FOTBreath.BREATH_LP_HZ)[mid]
            self.assertLess(np.max(np.abs(leak)), 0.002, f"tone leaks at {hz} Hz")
            self.assertTrue(np.all(np.isfinite(out)))


def _write_recording(path, hz, z, seconds=30.0, flow_amp=0.1):
    """A recording at `hz` whose true impedance is `z`, with slow breathing."""
    fs = sample_hz_for(hz)
    n = int(seconds * fs)
    idx = np.arange(n) % LUT_SIZE
    theta = 2 * np.pi * idx / LUT_SIZE
    t = np.arange(n) / fs
    breathing = 0.3 * np.sin(2 * np.pi * 0.25 * t)
    flow = breathing + flow_amp * np.cos(theta)
    pressure = abs(z) * flow_amp * np.cos(theta + np.angle(z))
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["sample", "time_s", "idx", "pressure_cmH2O", "flow_Lps"])
        for i in range(n):
            writer.writerow([i, i / fs, int(idx[i]), pressure[i], flow[i]])


class PipelineFrequencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.csv_path = self.tmp / "MEAS.csv"

        def phase_path(hz=FOTImpedance.EXCITATION_HZ):
            return self.tmp / f"fot_phase_calibration_{hz:g}hz.json"

        patches = [
            patch.object(FOTImpedance, "_BASE_DIR", self.tmp),
            patch.object(FOTPhaseCalibration, "phase_calibration_path_for", phase_path),
            patch.object(FOTPipeline, "phase_calibration_path_for", phase_path),
            patch.object(FOTPipeline, "get_measurement_csv_path", lambda _id: self.csv_path),
            patch.object(FOTPipeline, "ensure_auto_applied_calibration", lambda hz=None: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(FOTImpedance.load_bias_calibration_for, FOTImpedance.EXCITATION_HZ)
        FOTImpedance.load_bias_calibration_for(FOTImpedance.EXCITATION_HZ)

    def _calibrate(self, hz, angle_deg, scale):
        (self.tmp / f"fot_phase_calibration_{hz:g}hz.json").write_text(
            json.dumps({"correction_angle_deg": angle_deg})
        )
        (self.tmp / f"fot_bias_calibration_{hz:g}hz.json").write_text(
            json.dumps({"scale": scale, "k_plumbing_poly": [0.0], "k_x_poly": [0.0]})
        )

    def test_recording_uses_its_own_frequency_calibration(self):
        # The device reads 10 cmH2O.s/L at a 20 deg phase error, 11 Hz gain 1.25.
        raw = 10.0 * complex(math.cos(math.radians(20)), math.sin(math.radians(20)))
        _write_recording(self.csv_path, 11.0, raw)
        self._calibrate(11.0, 20.0, 1.25)
        self._calibrate(6.0, -5.0, 0.80)        # must NOT be used

        result = FOTPipeline.process_measurement_csv("M1", excitation_hz=11.0)

        self.assertIn("blocks at 11 Hz", result["status"])
        self.assertNotIn("NOT CALIBRATED", result["status"])
        self.assertTrue(result["calibrated_at_frequency"])
        self.assertAlmostEqual(result["resistance_R5"], 12.5, delta=0.25)
        self.assertAlmostEqual(result["reactance_X5"], 0.0, delta=0.25)
        self.assertTrue(analysed_at_recorded_frequency(result["status"], 11.0))

    def test_frequency_is_read_from_the_measurement(self):
        _write_recording(self.csv_path, 19.0, complex(8.0, 0.0))
        self._calibrate(19.0, 0.0, 1.0)
        with patch.object(FOTPipeline, "get_measurement_excitation_hz", lambda _id: 19.0):
            result = FOTPipeline.process_measurement_csv("M2")
        self.assertIn("blocks at 19 Hz", result["status"])
        self.assertAlmostEqual(result["resistance_R5"], 8.0, delta=0.2)

    def test_uncalibrated_frequency_is_reported(self):
        _write_recording(self.csv_path, 19.0, complex(8.0, 0.0))
        self._calibrate(6.0, 0.0, 1.0)
        result = FOTPipeline.process_measurement_csv("M3", excitation_hz=19.0)
        self.assertIn("NOT CALIBRATED at 19 Hz", result["status"])
        self.assertFalse(result["calibrated_at_frequency"])

    def test_active_calibration_is_restored_after_analysis(self):
        self._calibrate(6.0, 0.0, 0.9)
        self._calibrate(11.0, 0.0, 1.3)
        FOTImpedance.load_bias_calibration_for(6.0)
        _write_recording(self.csv_path, 11.0, complex(10.0, 0.0), seconds=15.0)
        FOTPipeline.process_measurement_csv("M4", excitation_hz=11.0)
        self.assertEqual(FOTImpedance.ACTIVE_BIAS_HZ, 6.0)
        self.assertAlmostEqual(FOTImpedance.BIAS_CORR_SCALE, 0.9)

    def test_tidal_volume_does_not_depend_on_frequency(self):
        volumes = {}
        for hz in (6.0, 19.0):
            _write_recording(self.csv_path, hz, complex(8.0, 0.0))
            self._calibrate(hz, 0.0, 1.0)
            result = FOTPipeline.process_measurement_csv("M5", excitation_hz=hz)
            volumes[hz] = result["tidal_volume_l"]
        self.assertIsNotNone(volumes[6.0])
        self.assertAlmostEqual(volumes[19.0], volumes[6.0], delta=0.05 * volumes[6.0])


class StaleResultTests(unittest.TestCase):
    def test_old_six_hz_only_result_is_detected(self):
        old = "Processed 9000 rows (9000 parsed); 21 blocks at 6 Hz; impedance OK"
        self.assertFalse(analysed_at_recorded_frequency(old, 5.0))
        self.assertTrue(analysed_at_recorded_frequency(old, 6.0))
        self.assertFalse(analysed_at_recorded_frequency(None, 5.0))


if __name__ == "__main__":
    unittest.main()
