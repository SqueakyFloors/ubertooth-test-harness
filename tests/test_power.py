import unittest

from ubertooth_harness.power import (
    AcronameUSBHub3pController,
    PortState,
    PowerControllerError,
    SimulatedPowerController,
    preserved_power_state,
)
from ubertooth_harness.topology import (
    PowerIsolationAction,
    applied_power_isolation,
)


class FakeResult:
    def __init__(self, value=None, error=0):
        self.error = error
        self.value = value


class FakeSystem:
    def __init__(self, serial, error=0):
        self.serial = serial
        self.error = error

    def getSerialNumber(self):
        return FakeResult(self.serial, self.error)


class FakePort:
    def __init__(
        self,
        *,
        power=True,
        data=True,
        voltage=5_000_000,
        current=25_000,
        read_error=0,
        write_error=0,
        ignore_writes=False,
    ):
        self.power = power
        self.data = data
        self.voltage = voltage
        self.current = current
        self.read_error = read_error
        self.write_error = write_error
        self.ignore_writes = ignore_writes

    def getPowerEnabled(self):
        return FakeResult(self.power, self.read_error)

    def getDataEnabled(self):
        return FakeResult(self.data, self.read_error)

    def getVbusVoltage(self):
        return FakeResult(self.voltage, self.read_error)

    def getVbusCurrent(self):
        return FakeResult(self.current, self.read_error)

    def setPowerEnabled(self, enabled):
        if not self.ignore_writes and self.write_error == 0:
            self.power = bool(enabled)
            self.voltage = 5_000_000 if enabled else 0
            self.current = 25_000 if enabled else 0
        return self.write_error

    def setDataEnabled(self, enabled):
        if not self.ignore_writes and self.write_error == 0:
            self.data = bool(enabled)
        return self.write_error


class FakeHub:
    def __init__(self, serial, *, connect_error=0, identity_error=0, ports=None):
        self.serial = serial
        self.connect_error = connect_error
        self.system = FakeSystem(serial, identity_error)
        self.port = ports or [FakePort() for _ in range(8)]
        self.connected_transport = None
        self.connected_serial = None
        self.disconnected = False

    def discoverAndConnect(self, transport, serial_number):
        self.connected_transport = transport
        self.connected_serial = serial_number
        return self.connect_error

    def disconnect(self):
        self.disconnected = True


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
        controller = AcronameUSBHub3pController(
            0x12345678,
            module_factory=lambda: hub,
            no_error=0,
            usb_transport="USB",
        )

        controller.connect()
        state = controller.snapshot(0)

        self.assertEqual("USB", hub.connected_transport)
        self.assertEqual(0x12345678, hub.connected_serial)
        self.assertEqual(
            PortState(0, True, True, 5_000_000, 25_000), state
        )
        controller.close()
        self.assertTrue(hub.disconnected)

    def test_rejects_wrong_controller_identity(self):
        hub = FakeHub(0x87654321)
        controller = AcronameUSBHub3pController(
            0x12345678,
            module_factory=lambda: hub,
            no_error=0,
            usb_transport="USB",
        )

        with self.assertRaisesRegex(PowerControllerError, "identity"):
            controller.connect()
        self.assertTrue(hub.disconnected)

    def test_real_power_changes_are_unarmed_by_default(self):
        hub = FakeHub(0x12345678)
        controller = AcronameUSBHub3pController(
            0x12345678,
            module_factory=lambda: hub,
            no_error=0,
            usb_transport="USB",
        )
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "not armed"):
            controller.set_power_enabled(0, False)
        self.assertTrue(hub.port[0].power)

    def test_armed_change_checks_error_and_readback(self):
        hub = FakeHub(0x12345678)
        controller = AcronameUSBHub3pController(
            0x12345678,
            allow_changes=True,
            module_factory=lambda: hub,
            no_error=0,
            usb_transport="USB",
        )
        controller.connect()

        controller.set_power_enabled(0, False)

        self.assertFalse(controller.snapshot(0).power_enabled)
        self.assertEqual(0, controller.snapshot(0).vbus_microvolts)

    def test_result_errors_fail_closed(self):
        hub = FakeHub(0x12345678, ports=[FakePort(read_error=5)] * 8)
        controller = AcronameUSBHub3pController(
            0x12345678,
            allow_changes=True,
            module_factory=lambda: hub,
            no_error=0,
            usb_transport="USB",
        )
        controller.connect()

        with self.assertRaisesRegex(PowerControllerError, "error 5"):
            controller.snapshot(0)


if __name__ == "__main__":
    unittest.main()
