"""Gain & Phase Calibration: one recording of the reference load per frequency gives
a gain and a phase angle, which are saved to the files the engine already reads."""

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend.device.calibration_recorder import CalibrationPointRecorder
from backend.device.gain_phase_sweep import GainPhaseSweep
from backend.processing import FOTGainPhaseCalibration as gp
from backend.processing.FOTImpedance import BLOCK_SAMPLES, LUT_SIZE, make_block_phasor


def load_blocks(measured_r, phase_deg, flow_amplitude=0.03, count=10, noise=0.0, seed=0):
    """Blocks of a resistor that reads `measured_r` with the sensors `phase_deg` apart."""
    rng = np.random.default_rng(seed)
    idx = np.arange(BLOCK_SAMPLES) % LUT_SIZE
    angle = 2 * np.pi * idx / LUT_SIZE
    blocks = []
    for _ in range(count):
        flow = flow_amplitude * np.sin(angle)
        pressure = measured_r * flow_amplitude * np.sin(angle + np.deg2rad(phase_deg))
        pressure = pressure + noise * rng.standard_normal(len(idx))
        blocks.append(make_block_phasor(idx, pressure, flow))
    return blocks


class EvaluateTests(unittest.TestCase):
    def test_gain_and_phase_from_a_clean_recording(self):
        result = gp.evaluate_frequency(load_blocks(12.0, -5.0), 13.62, 6.0)
        self.assertTrue(result["passed"], result["reasons"])
        self.assertAlmostEqual(result["measured_resistance_cmH2O_s_per_L"], 12.0, places=3)
        self.assertAlmostEqual(result["gain"], 13.62 / 12.0, places=3)
        # The saved angle is the one the engine rotates by: rotating the raw reading
        # by it must leave a pure resistance.
        self.assertAlmostEqual(abs(result["correction_angle_deg"]), 5.0, places=2)

    def test_weak_flow_fails(self):
        result = gp.evaluate_frequency(load_blocks(12.0, 0.0, flow_amplitude=0.002), 13.62, 5.0)
        self.assertFalse(result["passed"])
        self.assertIn("flow wave too small", "; ".join(result["reasons"]))

    def test_huge_correction_fails_rather_than_hiding_a_fault(self):
        result = gp.evaluate_frequency(load_blocks(6.0, 0.0), 13.62, 5.0)
        self.assertFalse(result["passed"])
        self.assertIn("too large to trust", "; ".join(result["reasons"]))

    def test_unsteady_reading_fails(self):
        result = gp.evaluate_frequency(load_blocks(12.0, 0.0, noise=1.0), 13.62, 6.0)
        self.assertFalse(result["passed"])
        self.assertIn("not steady", "; ".join(result["reasons"]))

    def test_block_counts_cover_the_same_time_at_every_frequency(self):
        # A block is round(hz) cycles, so one second at every frequency.
        for hz in (5.0, 6.0, 11.0, 19.0):
            self.assertEqual(gp.blocks_for_seconds(25, hz), 25)

    def test_hpa_label_is_converted(self):
        self.assertAlmostEqual(gp.to_cmh2o(13.62, "hPa·s/L"), 13.889, places=3)
        self.assertEqual(gp.to_cmh2o(13.62, "cmH₂O·s/L"), 13.62)


class FakeStore:
    def __init__(self, path):
        self.path = path
        self.auto_apply = True

    def load(self):
        return {"auto_apply": self.auto_apply}

    def set_auto_apply(self, enabled):
        self.auto_apply = enabled


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        store_file = self.tmp / "presets.json"
        store_file.write_text("{}")
        self.store = FakeStore(store_file)
        for target, value in (
            ("phase_calibration_path_for", lambda hz: self.tmp / f"phase_{hz:g}.json"),
            ("bias_calibration_path_for", lambda hz: self.tmp / f"bias_{hz:g}.json"),
            ("load_bias_calibration_for", lambda hz: None),
        ):
            patcher = patch.object(gp, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "backend.processing.FOTCalibrationPresets.default_store",
            lambda hz: self.store,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_passed_frequencies_are_saved_and_old_files_archived(self):
        (self.tmp / "bias_6.json").write_text('{"scale": 0.97, "k_plumbing_poly": [5.0]}')
        good = gp.evaluate_frequency(load_blocks(12.0, -5.0), 13.62, 6.0)
        bad = gp.evaluate_frequency(load_blocks(6.0, 0.0), 13.62, 5.0)

        saved = gp.save_results([bad, good], self.tmp / "archive")

        self.assertEqual(saved, [6.0])
        self.assertFalse((self.tmp / "phase_5.json").exists())
        bias = json.loads((self.tmp / "bias_6.json").read_text(encoding="utf-8"))
        phase = json.loads((self.tmp / "phase_6.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(bias["scale"], 13.62 / 12.0, places=3)
        self.assertEqual(bias["k_plumbing_poly"], [0.0])
        self.assertEqual(bias["k_x_poly"], [0.0])
        self.assertAlmostEqual(phase["correction_angle_deg"], good["correction_angle_deg"])
        self.assertEqual(len(list((self.tmp / "archive").glob("bias_6_*.json"))), 1)
        # Auto-apply would copy an old preset back over the new files.
        self.assertFalse(self.store.auto_apply)

    def test_current_calibration_reads_back_what_was_saved(self):
        good = gp.evaluate_frequency(load_blocks(12.0, -5.0), 13.62, 6.0)
        gp.save_results([good], self.tmp / "archive")
        current = gp.current_calibration(6.0)
        self.assertEqual(current["method"], "gain_phase")
        self.assertFalse(current["has_bias_polynomial"])
        self.assertAlmostEqual(current["gain"], good["gain"])


class FakeRecorder:
    """Stands in for CalibrationPointRecorder: finishes at once with given blocks."""

    blocks_by_hz = {}
    fail_on_start = set()
    created = []

    def __init__(self, client, excitation_hz, **kwargs):
        self.hz = excitation_hz
        self.kwargs = kwargs
        self.collected_blocks = []
        FakeRecorder.created.append(self)

    def start(self):
        if self.hz in self.fail_on_start:
            raise RuntimeError("CONFIG_REJECTED")
        self.collected_blocks = self.blocks_by_hz[self.hz]

    def snapshot(self):
        return {"state": "done", "progress": 1.0}

    def cancel(self):
        pass


class FakeClient:
    is_connected = True

    def __init__(self):
        self.commands = []

    def stop_streaming(self):
        self.commands.append("STOP")

    def configure(self, **settings):
        self.commands.append(("CONFIG", settings))

    def wait_for_frequency_ack(self, hz):
        return float(hz)


class SweepTests(unittest.TestCase):
    def test_every_frequency_is_measured_and_one_failure_does_not_lose_the_rest(self):
        FakeRecorder.blocks_by_hz = {5.0: load_blocks(12.0, -4.0), 6.0: load_blocks(12.5, -5.0)}
        FakeRecorder.fail_on_start = {11.0}
        FakeRecorder.created = []
        client = FakeClient()
        sweep = GainPhaseSweep(
            client, 13.62, frequencies=(5.0, 6.0, 11.0), recorder_factory=FakeRecorder
        )
        with patch("backend.device.gain_phase_sweep.BETWEEN_FREQUENCIES_SECONDS", 0):
            sweep.start()
            sweep._thread.join(timeout=5)

        snapshot = sweep.snapshot()
        self.assertEqual(snapshot["state"], "done")
        self.assertEqual([r["excitation_hz"] for r in snapshot["results"]], [5.0, 6.0, 11.0])
        self.assertEqual([r["passed"] for r in snapshot["results"]], [True, True, False])
        self.assertIn("CONFIG_REJECTED", snapshot["results"][2]["reasons"][0])
        # Each frequency records the same 25 s, as 25 one-second blocks.
        self.assertEqual(
            [r.kwargs["window_blocks"] for r in FakeRecorder.created], [25, 25, 25]
        )
        self.assertEqual(client.commands[-1], "STOP")

    def test_recorder_sets_the_board_to_its_own_frequency(self):
        client = FakeClient()
        recorder = CalibrationPointRecorder(client, cpap_value="off", excitation_hz=19.0)
        recorder._set_analysis_frequency()
        self.assertEqual(
            client.commands[0], ("CONFIG", {"excitation_hz": 19.0, "sample_hz": 19.0 * 70})
        )


if __name__ == "__main__":
    unittest.main()
