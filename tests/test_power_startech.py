import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from ubertooth_harness.power import (
    STARTECH_DRIVER,
    _decode_startech_mask,
    _encode_startech_mask,
    _open_serial,
    PowerControllerError,
    StarTechManagedHubController,
    controller_from_resource,
    preserved_power_state,
)
from ubertooth_harness.resources import FileResourceRegistry, Resource
from ubertooth_harness.topology import (
    PowerIsolationAction,
    applied_power_isolation,
)

DEVICE = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_TEST-if00-port0"
FIRMWARE = "CENTOS000104v02"
# Masks as real hardware reports them: bits 4-31 set, low nibble = ports 1-4.
ALL_OFF = 0xFFFFFFF0


def ports_on(*ports):
    return ALL_OFF | sum(1 << (p - 1) for p in ports)


def wire(mask):
    """Encode a port mask the way the controller sends it (LSB first)."""
    return mask.to_bytes(4, "little").hex().upper()


class FakeHubSerial:
    """Models the StarTech management controller's serial protocol."""

    def __init__(self, *, firmware=FIRMWARE, mask=0, password="pass",
                 ignore_writes=False, silent=False, reply_override=None,
                 fail_after_writes=None, gp_reply=None, fail_once_at=None):
        self.firmware = firmware
        self.mask = mask
        self.password = password.ljust(8)
        self.ignore_writes = ignore_writes
        self.silent = silent
        self.reply_override = reply_override
        self.fail_after_writes = fail_after_writes
        self.gp_reply = gp_reply
        self.fail_once_at = fail_once_at
        self.stale = b""
        self.sent = []
        self.closed = False
        self._reply = b""

    def reset_input_buffer(self):
        self.stale = b""

    def write(self, data):
        command = data.decode("ascii")
        assert command.endswith("\r")
        command = command[:-1]
        self.sent.append(command)
        if self.fail_after_writes is not None and len(self.sent) > self.fail_after_writes:
            raise OSError("device disconnected")
        if self.fail_once_at is not None and len(self.sent) == self.fail_once_at:
            self.fail_once_at = None
            raise OSError("transient I/O error")
        if self.silent:
            self._reply = b""
            return
        if command == "?Q":
            reply = self.firmware
        elif command == "GP":
            reply = self.gp_reply if self.gp_reply is not None else wire(self.mask)
        elif command.startswith("SP"):
            password, value = command[2:10], command[10:]
            if password != self.password:
                reply = "E"
            else:
                if not self.ignore_writes:
                    self.mask = int.from_bytes(bytes.fromhex(value), "little")
                reply = "G" + wire(self.mask)
            if self.reply_override is not None:
                reply = self.reply_override
        else:
            reply = "E"
        self._reply = reply.encode("ascii") + b"\r\n"

    def read_until(self):
        if self.stale:  # a late reply from an earlier request arrives first
            reply, self.stale = self.stale, b""
            return reply
        reply, self._reply = self._reply, b""
        return reply

    def close(self):
        self.closed = True


def controller_for(fake, **kwargs):
    fake.opens = 0

    def factory(path):
        fake.opens += 1
        fake.closed = False
        return fake

    return StarTechManagedHubController(DEVICE, serial_factory=factory, **kwargs)


class StarTechControllerTests(unittest.TestCase):

    def test_production_mask_codec_matches_hardware_strings(self):
        # Observed on hardware: all ports off reads F0FFFFFF; switching port 3
        # off from all-on sends FBFFFFFF.
        self.assertEqual("F0FFFFFF", _encode_startech_mask(0xFFFFFFF0))
        self.assertEqual(0xFFFFFFF0, _decode_startech_mask("F0FFFFFF"))
        self.assertEqual("FBFFFFFF", _encode_startech_mask(0xFFFFFFFB))
        for bad in ("F0FFFF", "F0FFFFFFFF", "ZZFFFFFF"):
            with self.assertRaisesRegex(PowerControllerError, "malformed"):
                _decode_startech_mask(bad)

    def test_connect_verifies_firmware_and_reads_ports(self):
        fake = FakeHubSerial(mask=ports_on(1, 3))
        controller = controller_for(fake)

        controller.connect()

        self.assertEqual(FIRMWARE, controller.firmware_version)
        self.assertEqual(["?Q", "GP"], fake.sent)
        state = controller.snapshot(1)
        self.assertTrue(state.power_enabled)
        self.assertTrue(state.data_enabled)
        self.assertIsNone(state.vbus_microvolts)
        self.assertIsNone(state.vbus_microamps)
        self.assertFalse(controller.snapshot(2).power_enabled)

    def test_unvalidated_firmware_fails_closed_and_closes_link(self):
        fake = FakeHubSerial(firmware="CENTOS999999v99")
        controller = controller_for(fake)

        with self.assertRaisesRegex(PowerControllerError, "unvalidated"):
            controller.connect()
        self.assertTrue(fake.closed)
        with self.assertRaisesRegex(PowerControllerError, "not connected"):
            controller.snapshot(1)

    def test_silent_controller_fails_closed(self):
        controller = controller_for(FakeHubSerial(silent=True))

        with self.assertRaisesRegex(PowerControllerError, "did not answer"):
            controller.connect()

    def test_changes_are_unarmed_by_default(self):
        fake = FakeHubSerial(mask=ports_on(1, 2, 3))
        controller = controller_for(fake)
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "not armed"):
            controller.set_power_enabled(1, False)
        self.assertFalse(any(cmd.startswith("SP") for cmd in fake.sent))

    def test_switches_only_the_requested_port(self):
        fake = FakeHubSerial(mask=ports_on(1, 3))
        controller = controller_for(fake, allow_changes=True)
        controller.connect()

        controller.set_power_enabled(3, False)

        self.assertEqual(0xFFFFFFF1, fake.mask)
        self.assertIn("SPpass    F1FFFFFF", fake.sent)
        controller.set_power_enabled(2, True)
        self.assertEqual(0xFFFFFFF3, fake.mask)

    def test_port_4_and_reserved_bits_are_preserved(self):
        fake = FakeHubSerial(mask=0xFFFFFFFF)
        controller = controller_for(fake, allow_changes=True)
        controller.connect()

        controller.set_power_enabled(3, False)

        self.assertIn("SPpass    FBFFFFFF", fake.sent)
        self.assertEqual(0xFFFFFFFB, fake.mask)

    def test_no_write_when_already_in_requested_state(self):
        fake = FakeHubSerial(mask=ports_on(1))
        controller = controller_for(fake, allow_changes=True)
        controller.connect()

        controller.set_power_enabled(1, True)

        self.assertFalse(any(cmd.startswith("SP") for cmd in fake.sent))

    def test_ignored_write_fails_readback(self):
        fake = FakeHubSerial(mask=ports_on(1), ignore_writes=True)
        controller = controller_for(fake, allow_changes=True)
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "readback"):
            controller.set_power_enabled(1, False)

    def test_rejected_write_fails(self):
        fake = FakeHubSerial(mask=ports_on(1), password="secret")
        controller = controller_for(fake, allow_changes=True)
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "rejected"):
            controller.set_power_enabled(1, False)

    def test_management_port_and_out_of_range_ports_are_refused(self):
        controller = controller_for(FakeHubSerial(), allow_changes=True)
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "management controller"):
            controller.set_power_enabled(4, False)
        for port in (0, 5, True):
            with self.assertRaisesRegex(PowerControllerError, "1-3"):
                controller.snapshot(port)

    def test_data_cannot_be_disabled(self):
        controller = controller_for(FakeHubSerial())
        controller.connect()

        controller.set_data_enabled(1, True)
        with self.assertRaisesRegex(PowerControllerError, "data"):
            controller.set_data_enabled(1, False)

    def test_restores_original_states_after_failure(self):
        fake = FakeHubSerial(mask=ports_on(1, 3))
        controller = controller_for(fake, allow_changes=True)
        controller.connect()

        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with preserved_power_state(controller, (1, 3)):
                controller.set_power_enabled(1, False)
                controller.set_power_enabled(3, False)
                raise RuntimeError("test failure")

        self.assertEqual(ports_on(1, 3), fake.mask)

    def test_io_failure_during_restore_still_attempts_every_port(self):
        fake = FakeHubSerial(mask=ports_on(1, 3))
        controller = controller_for(fake, allow_changes=True)
        controller.connect()
        with self.assertRaises(PowerControllerError) as caught:
            with preserved_power_state(controller, (1, 3)):
                controller.set_power_enabled(1, False)
                controller.set_power_enabled(3, False)
                fake.fail_after_writes = len(fake.sent)  # unplug before restore
        message = str(caught.exception)
        # Both ports were attempted and both failures are reported, not just the first.
        self.assertIn("restoration failed", message)
        self.assertIn("port 1:", message)
        self.assertIn("port 3:", message)
        self.assertTrue(fake.closed)

    def test_transient_failure_during_restore_reconnects_and_restores_every_port(self):
        fake = FakeHubSerial(mask=ports_on(1, 3))
        controller = controller_for(fake, allow_changes=True)
        controller.connect()
        with preserved_power_state(controller, (1, 3)):
            controller.set_power_enabled(1, False)
            controller.set_power_enabled(3, False)
            fake.fail_once_at = len(fake.sent) + 1  # first restore request
        # One transient error: the link reopens, the port is retried, and
        # every port is back in its original state with no error raised.
        self.assertGreaterEqual(fake.opens, 2)
        self.assertEqual(ports_on(1, 3), fake.mask)

    def test_sp_reply_mismatch_fails_and_closes_link(self):
        fake = FakeHubSerial(mask=ports_on(1), reply_override="G" + wire(ALL_OFF | 0b10))
        controller = controller_for(fake, allow_changes=True)
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "readback mismatch"):
            controller.set_power_enabled(1, False)
        self.assertEqual(1, sum(cmd.startswith("SP") for cmd in fake.sent))
        self.assertTrue(fake.closed)

    def test_late_reply_is_flushed_before_next_request(self):
        fake = FakeHubSerial(mask=ports_on(1))
        controller = controller_for(fake)
        controller.connect()
        fake.stale = b"GFFFFFFFF\r\n"  # late answer to an earlier SP

        self.assertFalse(controller.snapshot(3).power_enabled)

    def test_connect_twice_does_not_reopen(self):
        fake = FakeHubSerial()
        controller = controller_for(fake)
        controller.connect()
        sent = list(fake.sent)

        controller.connect()

        self.assertEqual(sent, fake.sent)
        self.assertEqual(1, fake.opens)
        controller.close()
        self.assertIsNone(controller.firmware_version)

    def test_open_uses_exclusive_access_and_timeouts(self):
        calls = []

        class SerialException(Exception):
            pass

        def Serial(path, **kwargs):
            calls.append((path, kwargs))
            if path.endswith("busy"):
                raise SerialException("could not exclusively lock port")
            return object()

        fake_module = types.SimpleNamespace(Serial=Serial, SerialException=SerialException)
        with mock.patch.dict(sys.modules, {"serial": fake_module}):
            _open_serial(DEVICE)
            with self.assertRaisesRegex(PowerControllerError, "unable to open"):
                _open_serial(DEVICE + "busy")
        self.assertEqual(
            {"baudrate": 9600, "timeout": 1, "write_timeout": 1, "exclusive": True},
            calls[0][1],
        )
        with mock.patch.dict(sys.modules, {"serial": None}):
            with self.assertRaisesRegex(PowerControllerError, "pyserial is not installed"):
                _open_serial(DEVICE)

    def test_example_hardware_config_loads(self):
        example = Path(__file__).resolve().parents[1] / "hardware.example.yaml"
        registry = FileResourceRegistry.load(example)
        self.assertTrue(any(
            r.driver == STARTECH_DRIVER for r in registry.resources.values()
        ))

    def test_malformed_reply_closes_the_link(self):
        fake = FakeHubSerial(mask=ALL_OFF)
        controller = controller_for(fake)
        controller.connect()
        fake.gp_reply = "F0FF"

        with self.assertRaisesRegex(PowerControllerError, "malformed"):
            controller.snapshot(1)
        self.assertTrue(fake.closed)
        with self.assertRaisesRegex(PowerControllerError, "not connected"):
            controller.snapshot(1)

    def test_identity_must_be_stable_by_id_path(self):
        for path in ("/dev/ttyUSB0", "", "/dev/serial/by-id/", "/dev/serial/by-id/../ttyUSB0"):
            with self.assertRaisesRegex(PowerControllerError, "by-id"):
                StarTechManagedHubController(path)
        with self.assertRaisesRegex(PowerControllerError, "ASCII"):
            StarTechManagedHubController(DEVICE, password="pässwd")

    def test_applied_isolation_through_registry_driver(self):
        fake = FakeHubSerial(mask=ports_on(1, 3))
        resource = Resource(
            alias="Hub_Linux0",
            type="USB power controller",
            platform=None,
            roles=(),
            defaults={},
            attached_to="Host_Linux",
            identity={"serial device": DEVICE},
            capabilities=("per-port VBUS switching",),
            power={},
            driver=STARTECH_DRIVER,
        )
        controller = controller_from_resource(
            resource, allow_real_changes=True, serial_factory=lambda path: fake
        )
        actions = (PowerIsolationAction("UT_Linux1", "Hub_Linux0", 3, False),)

        with applied_power_isolation(actions, lambda alias: controller):
            self.assertEqual(ports_on(1), fake.mask)

        self.assertEqual(ports_on(1, 3), fake.mask)
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
