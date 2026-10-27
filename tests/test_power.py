import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ubertooth_harness import cli

from ubertooth_harness.power import (
    ACRONAME_DRIVER,
    SIMULATED_DRIVER,
    AcronameUSBHub3pController,
    PortState,
    PowerControllerError,
    SimulatedPowerController,
    controller_from_resource,
    preserved_power_state,
)
from ubertooth_harness.resources import Resource
from ubertooth_harness.topology import (
    PowerIsolationAction,
    applied_power_isolation,
)


VBUS, USB2, USB3, ATTACHED = 1 << 0, 1 << 1, 1 << 3, 1 << 23
PORT_ON = VBUS | USB2 | USB3 | ATTACHED  # 0x80080b without the speed bit
INDEX_RANGE_ERROR = 13


class FakeResult:
    def __init__(self, value=None, error=0):
        self.error = error
        self.value = value


class FakeSystem:
    def __init__(self, serial, error=0, model=19, version=0x28000000):
        self.serial = serial
        self.error = error
        self.model = model
        self.version = version

    def getSerialNumber(self):
        return FakeResult(self.serial, self.error)

    def getModel(self):
        return FakeResult(self.model, self.error)

    def getVersion(self):
        return FakeResult(self.version, self.error)


class FakeUSB:
    """The BrainStem USBHub3+ `usb` entity: ports addressed by index."""

    def __init__(self, states=None, *, read_error=0, write_error=0,
                 ignore_writes=False):
        self.states = list(states or [PORT_ON] * 8)
        self.current = [25_000] * 8
        self.read_error = read_error
        self.write_error = write_error
        self.ignore_writes = ignore_writes
        self.calls = []

    def _read(self, port, value):
        if not 0 <= port < len(self.states):
            return FakeResult(0, INDEX_RANGE_ERROR)
        return FakeResult(value(port), self.read_error)

    def getPortState(self, port):
        return self._read(port, lambda p: self.states[p])

    def getPortVoltage(self, port):
        return self._read(port, lambda p: 5_000_000 if self.states[p] & VBUS else 8_000)

    def getPortCurrent(self, port):
        return self._read(port, lambda p: self.current[p] if self.states[p] & VBUS else 0)

    def _write(self, name, port, set_bits, clear_bits):
        self.calls.append((name, port))
        if self.write_error or self.ignore_writes:
            return self.write_error
        self.states[port] = (self.states[port] | set_bits) & ~clear_bits
        return 0

    def setPowerEnable(self, port):
        return self._write("setPowerEnable", port, VBUS, 0)

    def setPowerDisable(self, port):
        return self._write("setPowerDisable", port, 0, VBUS | ATTACHED)

    def setDataEnable(self, port):
        return self._write("setDataEnable", port, USB2 | USB3, 0)

    def setDataDisable(self, port):
        return self._write("setDataDisable", port, 0, USB2 | USB3)


class FakeHub:
    def __init__(self, serial, *, connect_error=0, identity_error=0, model=19,
                 usb=None):
        self.serial = serial
        self.connect_error = connect_error
        self.system = FakeSystem(serial, identity_error, model)
        self.usb = usb or FakeUSB()
        self.connected_transport = None
        self.connected_serial = None
        self.disconnected = False

    def discoverAndConnect(self, transport, serial_number):
        self.connected_transport = transport
        self.connected_serial = serial_number
        return self.connect_error

    def disconnect(self):
        self.disconnected = True


def acroname(hub, *, allow_changes=False, serial=0x12345678):
    return AcronameUSBHub3pController(
        serial,
        allow_changes=allow_changes,
        module_factory=lambda: hub,
        no_error=0,
        usb_transport="USB",
        model=19,
    )

class SimulatedPowerControllerTests(unittest.TestCase):

    def test_snapshots_switches_and_restores_power_after_failure(self):
        controller = SimulatedPowerController(
            0x12345678,
            {
                0: PortState(0, True, True, 5_000_000, 25_000),
                1: PortState(1, False, True, 0, 0),
            },
        )
        controller.connect()

        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with preserved_power_state(controller, (0, 1)):
                controller.set_power_enabled(0, False)
                controller.set_power_enabled(1, True)
                self.assertFalse(controller.snapshot(0).power_enabled)
                self.assertTrue(controller.snapshot(1).power_enabled)
                raise RuntimeError("test failure")

        self.assertTrue(controller.snapshot(0).power_enabled)
        self.assertFalse(controller.snapshot(1).power_enabled)

    def test_applied_isolation_restores_compiled_actions(self):
        controller = SimulatedPowerController(
            0x12345678,
            {1: PortState(1, True, True, 5_000_000, 25_000)},
        )
        actions = (
            PowerIsolationAction("UT_Linux1", "Hub_Linux0", 1, False),
        )

        with applied_power_isolation(
            actions, lambda alias: controller
        ) as controllers:
            self.assertIs(controller, controllers["Hub_Linux0"])
            self.assertFalse(controller.snapshot(1).power_enabled)

        with self.assertRaisesRegex(PowerControllerError, "not connected"):
            controller.snapshot(1)
        controller.connect()
        self.assertTrue(controller.snapshot(1).power_enabled)

    def test_fails_when_switch_readback_does_not_match(self):
        controller = SimulatedPowerController(
            0x12345678,
            {0: PortState(0, True, True, 5_000_000, 25_000)},
            ignore_writes=True,
        )
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "readback"):
            controller.set_power_enabled(0, False)

    def test_rejects_invalid_port_and_use_before_connect(self):
        controller = SimulatedPowerController(0x12345678)

        with self.assertRaisesRegex(PowerControllerError, "not connected"):
            controller.snapshot(0)
        controller.connect()
        for port in (-1, 8):
            with self.assertRaisesRegex(PowerControllerError, "0-7"):
                controller.snapshot(port)


class AcronamePowerControllerTests(unittest.TestCase):

    def test_connects_by_serial_and_verifies_controller_identity(self):
        hub = FakeHub(0x12345678)
        controller = acroname(hub)

        controller.connect()
        state = controller.snapshot(0)

        self.assertEqual("USB", hub.connected_transport)
        self.assertEqual(0x12345678, hub.connected_serial)
        self.assertEqual("0x28000000", controller.firmware_version)
        self.assertEqual(PortState(0, True, True, 5_000_000, 25_000), state)
        controller.close()
        self.assertTrue(hub.disconnected)
        self.assertIsNone(controller.firmware_version)
        with self.assertRaisesRegex(PowerControllerError, "not connected"):
            controller.snapshot(0)

    def test_no_hub_with_that_serial_fails_closed(self):
        hub = FakeHub(0x12345678, connect_error=3)  # BrainStem NOT_FOUND

        with self.assertRaisesRegex(PowerControllerError, "0x12345678: error 3"):
            acroname(hub).connect()

    def test_rejects_wrong_controller_identity(self):
        hub = FakeHub(0x87654321)

        with self.assertRaisesRegex(PowerControllerError, "identity"):
            acroname(hub).connect()
        self.assertTrue(hub.disconnected)

    def test_rejects_wrong_model_and_identity_read_errors(self):
        for hub, pattern in ((FakeHub(0x12345678, model=4), "not a USBHub3"),
                             (FakeHub(0x12345678, identity_error=6), "error 6")):
            controller = acroname(hub)
            with self.assertRaisesRegex(PowerControllerError, pattern):
                controller.connect()
            self.assertTrue(hub.disconnected)
            with self.assertRaisesRegex(PowerControllerError, "not connected"):
                controller.snapshot(0)

    def test_rejects_unknown_ports_before_calling_the_hub(self):
        hub = FakeHub(0x12345678)
        controller = acroname(hub, allow_changes=True)
        controller.connect()

        for port in (-1, 8, True, "1"):
            with self.assertRaisesRegex(PowerControllerError, "0-7"):
                controller.snapshot(port)
            with self.assertRaisesRegex(PowerControllerError, "0-7"):
                controller.set_power_enabled(port, False)
        self.assertEqual([], hub.usb.calls)

    def test_real_power_changes_are_unarmed_by_default(self):
        hub = FakeHub(0x12345678)
        controller = acroname(hub)
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "not armed"):
            controller.set_power_enabled(0, False)
        with self.assertRaisesRegex(PowerControllerError, "not armed"):
            controller.set_data_enabled(0, False)
        self.assertEqual([], hub.usb.calls)

    def test_armed_power_change_keeps_data_and_reads_back(self):
        hub = FakeHub(0x12345678)
        controller = acroname(hub, allow_changes=True)
        controller.connect()

        controller.set_power_enabled(2, False)

        self.assertEqual(PortState(2, False, True, 8_000, 0), controller.snapshot(2))
        self.assertEqual([("setPowerDisable", 2)], hub.usb.calls)
        controller.set_power_enabled(2, True)
        self.assertTrue(controller.snapshot(2).power_enabled)

    def test_data_is_reported_and_switched_as_the_usb2_usb3_pair(self):
        usb = FakeUSB([VBUS | USB2] + [PORT_ON] * 7)  # USB 3.0 data off
        controller = acroname(FakeHub(0x12345678, usb=usb), allow_changes=True)
        controller.connect()

        self.assertFalse(controller.snapshot(0).data_enabled)
        controller.set_data_enabled(0, True)
        self.assertEqual(VBUS | USB2 | USB3, usb.states[0])
        controller.set_data_enabled(1, False)
        self.assertFalse(controller.snapshot(1).data_enabled)
        self.assertTrue(controller.snapshot(1).power_enabled)

    def test_stale_readback_fails_closed(self):
        usb = FakeUSB(ignore_writes=True)
        controller = acroname(FakeHub(0x12345678, usb=usb), allow_changes=True)
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "power readback mismatch on port 0"):
            controller.set_power_enabled(0, False)
        with self.assertRaisesRegex(PowerControllerError, "data readback mismatch on port 0"):
            controller.set_data_enabled(0, False)

    def test_write_and_read_errors_fail_closed(self):
        controller = acroname(
            FakeHub(0x12345678, usb=FakeUSB(write_error=7)), allow_changes=True
        )
        controller.connect()
        with self.assertRaisesRegex(PowerControllerError, "set power on port 0 failed with error 7"):
            controller.set_power_enabled(0, False)

        controller = acroname(FakeHub(0x12345678, usb=FakeUSB(read_error=5)))
        controller.connect()
        with self.assertRaisesRegex(PowerControllerError, "error 5"):
            controller.snapshot(0)

    def test_preserved_state_restores_power_and_data_after_failure(self):
        usb = FakeUSB([PORT_ON, VBUS] + [PORT_ON] * 6)  # port 1: data off
        hub = FakeHub(0x12345678, usb=usb)
        controller = acroname(hub, allow_changes=True)
        controller.connect()

        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with preserved_power_state(controller, (0, 1)):
                controller.set_power_enabled(0, False)
                controller.set_power_enabled(1, False)
                controller.set_data_enabled(1, True)
                raise RuntimeError("test failure")

        self.assertEqual(VBUS | USB2 | USB3, usb.states[0])
        self.assertEqual(VBUS, usb.states[1])

    def test_controller_from_resource_builds_the_adapter(self):
        resource = Resource(
            alias="Hub_Acroname", type="USB power controller", platform=None,
            roles=(), defaults={}, attached_to="Host_Linux",
            identity={"serial": "0x12345678"}, capabilities=(), power={},
            driver=ACRONAME_DRIVER,
        )
        hub = FakeHub(0x12345678)
        controller = controller_from_resource(
            resource, module_factory=lambda: hub, no_error=0, usb_transport="USB"
        )
        self.assertIsInstance(controller, AcronameUSBHub3pController)
        self.assertFalse(controller.allow_changes)
        controller.connect()
        self.assertEqual(0x12345678, hub.connected_serial)


POWER_REGISTRY = """
resources:
  Host_Linux:
    type: Host
    platform: linux
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: aaaa0000aaaa0000aaaa0000aaaa0000}
    power: {controller: Hub, port: 2}
  Hub:
    type: USB power controller
    attached to: Host_Linux
    identity: {serial: "0x12345678"}
    driver: DRIVER
    capabilities: [per-port VBUS switching, power readback]
"""


class PowerCommandTests(unittest.TestCase):

    def run_power(self, driver, *extra):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.yaml"
            path.write_text(POWER_REGISTRY.replace("DRIVER", driver))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = cli.main(["power", "--hardware", str(path), "Hub", *extra])
        return status, out.getvalue(), err.getvalue()

    def test_reads_every_port_and_names_the_mapped_board(self):
        status, out, _ = self.run_power(SIMULATED_DRIVER)

        self.assertEqual(0, status)
        lines = out.splitlines()
        self.assertEqual(9, len(lines))
        self.assertIn("port 2: power on  data on  VBUS  5.00 V    0.0 mA  UT_Linux0", lines)
        self.assertTrue(lines[1].startswith("port 0: power on "))

        status, out, _ = self.run_power(SIMULATED_DRIVER, "--json")
        ports = json.loads(out)["ports"]
        self.assertEqual(list(range(8)), [row["port"] for row in ports])
        self.assertEqual("UT_Linux0", ports[2]["resource"])

    def test_missing_brainstem_fails_closed(self):
        with mock.patch.dict(sys.modules, {"brainstem": None}):
            status, out, err = self.run_power(ACRONAME_DRIVER)

        self.assertEqual(2, status)
        self.assertEqual("", out)
        self.assertIn("BrainStem Python API is not installed", err)


if __name__ == "__main__":
    unittest.main()
