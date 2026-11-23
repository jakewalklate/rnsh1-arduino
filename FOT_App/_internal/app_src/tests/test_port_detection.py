"""Arduino port auto-detection across Windows, macOS and Linux port listings."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.device import serial_device
from backend.device.serial_device import (
    ArduinoSerialError,
    probe_port,
    remember_port,
    resolve_arduino_port,
    scan_for_arduinos,
)

ARDUINO_VIDS = (0x2341, 0x2A03)
CLONE_VIDS = (0x1A86, 0x0403, 0x10C4)
HINTS = ("Arduino", "UNO", "R4", "RA4M1", "Renesas", "usbmodem", "ttyACM")


def port(device, description="n/a", manufacturer=None, vid=None, pid=None, hwid="n/a"):
    return SimpleNamespace(
        device=device,
        description=description,
        manufacturer=manufacturer,
        hwid=hwid,
        vid=vid,
        pid=pid,
    )


MAC_SYSTEM_PORTS = [
    port("/dev/cu.Bluetooth-Incoming-Port"),
    port("/dev/cu.debug-console"),
]
WIN_BLUETOOTH_PORTS = [
    port("COM3", "Standard Serial over Bluetooth link (COM3)", "Microsoft"),
    port("COM4", "Standard Serial over Bluetooth link (COM4)", "Microsoft"),
]


class PortDetectionTests(unittest.TestCase):
    def setUp(self):
        remember_port(None)
        self.addCleanup(remember_port, None)

    def resolve(self, ports, platform="darwin", probe_ok=(), **kwargs):
        def fake_probe(device, *args, **kw):
            return device in probe_ok

        with (
            patch.object(serial_device.list_ports, "comports", return_value=ports),
            patch.object(serial_device, "IS_MACOS", platform == "darwin"),
            patch.object(serial_device, "IS_LINUX", platform == "linux"),
            patch.object(serial_device, "IS_WINDOWS", platform == "win32"),
            patch.object(serial_device, "probe_port", side_effect=fake_probe) as probe,
        ):
            kwargs.setdefault("port_hints", HINTS)
            kwargs.setdefault("vendor_ids", ARDUINO_VIDS)
            kwargs.setdefault("clone_vendor_ids", CLONE_VIDS)
            self.probe_mock = probe
            return resolve_arduino_port(**kwargs)

    def test_macos_uno_r4_found_past_bluetooth_and_debug_ports(self):
        ports = MAC_SYSTEM_PORTS + [
            port("/dev/cu.usbmodem1101", "UNO R4 Minima", "Arduino", vid=0x2341, pid=0x0069)
        ]
        self.assertEqual(self.resolve(ports), "/dev/cu.usbmodem1101")
        self.probe_mock.assert_not_called()

    def test_macos_usbmodem_name_alone_is_enough(self):
        ports = MAC_SYSTEM_PORTS + [port("/dev/cu.usbmodem14201", "USB Serial")]
        self.assertEqual(self.resolve(ports), "/dev/cu.usbmodem14201")

    def test_macos_tty_duplicate_is_not_a_second_arduino(self):
        ports = [
            port("/dev/tty.usbmodem1101", vid=0x2341),
            port("/dev/cu.usbmodem1101", vid=0x2341),
        ]
        self.assertEqual(self.resolve(ports), "/dev/cu.usbmodem1101")

    def test_linux_ttyacm_found_and_builtin_uart_ignored(self):
        ports = [
            port("/dev/ttyS0", "ttyS0"),
            port("/dev/ttyACM0", "UNO R4 Minima", vid=0x2341, pid=0x0069),
        ]
        self.assertEqual(self.resolve(ports, platform="linux"), "/dev/ttyACM0")

    def test_windows_generic_driver_found_by_vendor_id_past_bluetooth(self):
        ports = WIN_BLUETOOTH_PORTS + [
            port("COM5", "USB Serial Device (COM5)", "Microsoft", vid=0x2341, pid=0x0069)
        ]
        self.assertEqual(self.resolve(ports, platform="win32"), "COM5")

    def test_single_port_fallback_survives_system_ports(self):
        # An unknown board with no USB IDs is still taken when it is the only real port.
        ports = WIN_BLUETOOTH_PORTS + [port("COM7", "Communications Port (COM7)")]
        self.assertEqual(self.resolve(ports, platform="win32"), "COM7")

    def test_macos_bluetooth_gadget_is_not_taken_as_the_arduino(self):
        ports = MAC_SYSTEM_PORTS + [port("/dev/cu.Maker3-71CB")]
        with self.assertRaisesRegex(ArduinoSerialError, "Device not found"):
            self.resolve(ports)

    def test_macos_single_usb_port_fallback(self):
        ports = MAC_SYSTEM_PORTS + [
            port("/dev/cu.Maker3-71CB"),
            port("/dev/cu.SLAB_USBtoUART", vid=0x1234),
        ]
        self.assertEqual(self.resolve(ports), "/dev/cu.SLAB_USBtoUART")

    def test_two_arduinos_probe_picks_the_one_with_fot_firmware(self):
        ports = [
            port("/dev/cu.usbmodem1101", vid=0x2341),
            port("/dev/cu.usbmodem1201", vid=0x2341),
        ]
        chosen = self.resolve(ports, probe_ok={"/dev/cu.usbmodem1201"})
        self.assertEqual(chosen, "/dev/cu.usbmodem1201")
        self.assertEqual(self.probe_mock.call_count, 2)

    def test_two_arduinos_without_a_reply_asks_the_user(self):
        ports = [port("COM5", vid=0x2341), port("COM6", vid=0x2341)]
        with self.assertRaisesRegex(ArduinoSerialError, "Multiple Arduino-like"):
            self.resolve(ports, platform="win32")

    def test_preferred_port_skips_probing(self):
        ports = [port("COM5", vid=0x2341), port("COM6", vid=0x2341)]
        self.assertEqual(self.resolve(ports, platform="win32", preferred_port="COM6"), "COM6")
        self.probe_mock.assert_not_called()

    def test_remembered_port_is_used_when_none_is_passed(self):
        ports = [port("COM5", vid=0x2341), port("COM6", vid=0x2341)]
        remember_port("COM6")
        self.assertEqual(self.resolve(ports, platform="win32"), "COM6")
        self.probe_mock.assert_not_called()

    def test_clone_board_accepted_only_after_probe(self):
        ports = MAC_SYSTEM_PORTS + [
            port("/dev/cu.wchusbserial110", "USB Serial", vid=0x1A86),
            port("/dev/cu.usbserial-A10K", "FT232R", vid=0x0403),
        ]
        self.assertEqual(
            self.resolve(ports, probe_ok={"/dev/cu.wchusbserial110"}),
            "/dev/cu.wchusbserial110",
        )
        with self.assertRaisesRegex(ArduinoSerialError, "Device not found"):
            self.resolve(ports)

    def test_probe_disabled_never_opens_ports(self):
        ports = [port("COM5", vid=0x1A86), port("COM6", vid=0x0403)]
        with self.assertRaises(ArduinoSerialError):
            self.resolve(ports, platform="win32", probe=False)
        self.probe_mock.assert_not_called()


class ScanTests(unittest.TestCase):
    def test_scan_lists_confirmed_first_and_skips_held_port(self):
        ports = MAC_SYSTEM_PORTS + [
            port("/dev/cu.usbmodem1101", vid=0x2341),
            port("/dev/cu.usbmodem1201", vid=0x2341),
            port("/dev/cu.wchusbserial110", vid=0x1A86),
        ]
        probed = []

        def fake_probe(device, *args, **kw):
            probed.append(device)
            return device == "/dev/cu.wchusbserial110"

        with (
            patch.object(serial_device.list_ports, "comports", return_value=ports),
            patch.object(serial_device, "probe_port", side_effect=fake_probe),
        ):
            results = scan_for_arduinos(
                HINTS, ARDUINO_VIDS, CLONE_VIDS,
                skip_probe_devices=["/dev/cu.usbmodem1101"],
            )

        self.assertNotIn("/dev/cu.usbmodem1101", probed)
        self.assertEqual(
            [r.port.device for r in results],
            ["/dev/cu.wchusbserial110", "/dev/cu.usbmodem1101", "/dev/cu.usbmodem1201"],
        )
        self.assertTrue(results[0].confirmed)
        self.assertIsNone(results[1].confirmed)
        self.assertFalse(results[2].confirmed)
        self.assertIn("confirmed", results[0].label)


class FakeSerial:
    def __init__(self, reply_after_writes=1, reply=b"# STATUS_IDLE\r\n"):
        self.writes = 0
        self.reply_after_writes = reply_after_writes
        self.reply = reply
        self.pending = b""
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.pending)

    def write(self, data):
        self.writes += 1
        if self.writes >= self.reply_after_writes:
            self.pending += self.reply

    def read(self, size):
        data, self.pending = self.pending[:size], self.pending[size:]
        return data

    def close(self):
        self.closed = True


class ProbeTests(unittest.TestCase):
    def test_probe_recognises_firmware_reply(self):
        fake = FakeSerial()
        with patch.object(serial_device.serial, "Serial", return_value=fake):
            self.assertTrue(probe_port("COM5", timeout_seconds=1))
        self.assertTrue(fake.closed)

    def test_probe_retries_while_a_resetting_board_boots(self):
        fake = FakeSerial(reply_after_writes=2)
        with patch.object(serial_device.serial, "Serial", return_value=fake):
            self.assertTrue(probe_port("COM5", timeout_seconds=1.5))
        self.assertGreaterEqual(fake.writes, 2)

    def test_probe_rejects_other_devices(self):
        fake = FakeSerial(reply=b"OK\r\n")
        with patch.object(serial_device.serial, "Serial", return_value=fake):
            self.assertFalse(probe_port("COM5", timeout_seconds=0.3))

    def test_probe_handles_busy_port(self):
        error = serial_device.serial.SerialException("Resource busy")
        with patch.object(serial_device.serial, "Serial", side_effect=error):
            self.assertFalse(probe_port("COM5", timeout_seconds=0.3))


if __name__ == "__main__":
    unittest.main()
