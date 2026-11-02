"""Reset into invalid application (#141): the image, the power cycle, the locks.

Everything here runs against fakes: a simulated USB power controller, a USB
enumeration that follows that controller and the board's flash, and the
ubertooth-dfu fake the DFU images tests use. No board is touched, and the
hardware part of the issue is still to be run.

The checks that matter most are the ones about the port: every way this
command can end -- a failed check, a controller error, a bus that cannot be
read, a signal -- must leave the board's port powered, and must leave the
run's leases and host lock exactly as it found them.
"""

import io
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ubertooth_harness import dfuimage, invalidapp, invalidapp_cases, leases
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.cli import _check_power_cycles_armed
from ubertooth_harness.enumeration import (
    UNIDENTIFIED_PREFIX,
    EnumerationError,
)
from ubertooth_harness.flash import HarnessError
from ubertooth_harness.manifest import FileIdentity, SourceRevision, build_run_manifest
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler, TestInvocation
from ubertooth_harness.power import PortState, PowerControllerError, SimulatedPowerController
from ubertooth_harness.preflight import PreflightCheck
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import REGISTRY_PATH_KEY
from ubertooth_harness.topology import plan_power_cycle

from tests.test_dfu_cases import FakeDfuBoard, signed
from tests.test_run_isolation import HARDWARE, SERIALS
from tests.test_sequencer import CollectSink

BOARD = "UT_Linux0"
CONTROLLER = "Hub_StarTech"
PORT = 1
SERIAL = SERIALS[BOARD]
GUARDS = (SERIALS["UT_Linux1"], SERIALS["UT_Linux2"])
REVISION = "git-test"
DFU_ENTRY = f"{UNIDENTIFIED_PREFIX}3-2.4.1:6003"
# Waits short enough for a unit test; the production values are measured
# enumeration timings (enumeration.py).
FAST = {
    "removal_deadline_s": 0.3,
    "return_deadline_s": 0.3,
    "settle_s": 0.0,
    "poll_interval_s": 0.01,
}

# An image whose vector words are the ones a linked firmware carries: the
# stack at the top of the 16 KiB the images in the fork are linked for, and a
# Thumb entry just above the bootloader.
STACK_POINTER = 0x10003FF8
RESET_VECTOR = 0x00004101


def firmware(revision: str = REVISION, size: int = 3000) -> bytes:
    """A startable test image carrying the firmware info block."""
    body = bytearray(bytes(range(256)) * (size // 256))
    struct.pack_into("<II", body, 0, STACK_POINTER, RESET_VECTOR)
    block = dfuimage.fw_info_block(revision=revision)
    body[dfuimage.FW_INFO_OFFSET:dfuimage.FW_INFO_OFFSET + len(block)] = block
    return signed(bytes(body))


def registry_file(directory: Path) -> Path:
    path = directory / "hardware.yaml"
    path.write_text(HARDWARE, encoding="utf-8")
    return path


class FakeBus:
    """USB enumeration that follows the hub's VBUS and the board's flash.

    The board under test has no serial while it sits in its bootloader, so it
    is enumerated as a serial-less device; its guards are always there. What
    a cold boot brings back is the bootloader's decision, taken from the
    flash the fake board holds, which is what the test is about.
    """

    def __init__(self, controller, board, *, port=PORT, serial=SERIAL,
                 guards=GUARDS):
        self.controller = controller
        self.board = board
        self.port = port
        self.serial = serial
        self.guards = set(guards)
        self.extra = set()  # another board on the bus
        self.scans = 0
        self.raise_after = None  # scan number that fails, and with what
        # What the board does on the cold boot, which is the outcome under
        # test, so both only apply once its port has been off: it faults and
        # reaches no bus at all, or it starts the image it should refuse.
        self.faulted = False
        self.cold_boot_runs_image = False
        self.was_off = False
        self.bootloaders = 1  # how many serial-less devices the board shows as

    def serials(self):
        self.scans += 1
        if self.raise_after is not None and self.scans >= self.raise_after[0]:
            raise self.raise_after[1]
        seen = set(self.guards) | set(self.extra)
        if not self.controller.physical_power(self.port):
            self.was_off = True
            return frozenset(seen)
        if self.was_off and self.faulted:
            return frozenset(seen)
        if self.board.mode == "app" or (self.was_off and self.cold_boot_runs_image):
            seen.add(self.serial)
        else:
            seen.update(
                f"{UNIDENTIFIED_PREFIX}3-2.4.{index + 1}:6003"
                for index in range(self.bootloaders)
            )
        return frozenset(seen)


def hub(port=PORT, powered=True):
    return SimulatedPowerController(
        0x1, {port: PortState(port, powered, True, 5_000_000, 0)}
    )


def app_board(image=None, **flags):
    """The fake ubertooth-dfu board, in its application, with this bench's serials."""
    return FakeDfuBoard(image or firmware(), serial=SERIAL, guards=GUARDS,
                        checks_vectors=True, **flags)


def dfu_board(image=None, **flags):
    """A board where the write of the unstartable image left it: in DFU."""
    board = app_board(image, **flags)
    board._program(dfuimage.make_unstartable(invalidapp.UNSTARTABLE_KIND,
                                             image or firmware()))
    board.mode = "dfu"
    return board


def cycle(controller, bus, *, state=None, log=None, **timing):
    target = invalidapp.PowerCycleTarget(BOARD, CONTROLLER, PORT)
    lines = [] if log is None else log
    options = dict(FAST)
    options.update(timing)
    code = invalidapp.power_cycle(
        target, controller, bus,
        state or invalidapp.BoardState(SERIAL, frozenset(GUARDS), 1),
        log=lines.append, **options,
    )
    return code, "\n".join(lines)


class UnstartableImageTests(unittest.TestCase):
    """What the bootloader reads, and what the harness builds for it."""

    def test_a_linked_image_is_startable(self):
        image = firmware()
        self.assertEqual([], dfuimage.suffix_problems(image))
        self.assertEqual([], dfuimage.startable_problems(image))
        self.assertEqual((STACK_POINTER, RESET_VECTOR), dfuimage.app_vectors(image))

    def test_every_unstartable_kind_is_a_valid_dfu_file_the_board_must_refuse(self):
        image = firmware()
        for kind in dfuimage.UNSTARTABLE_KINDS:
            with self.subTest(kind):
                bad = dfuimage.make_unstartable(kind, image)
                # The host has no reason to refuse it: valid suffix, and the
                # firmware info block -d reads is still there.
                self.assertEqual([], dfuimage.suffix_problems(bad))
                self.assertEqual(REVISION, dfuimage.fw_info(bad).revision)
                self.assertNotEqual([], dfuimage.startable_problems(bad))
                # Only the two vector words differ, so nothing else about
                # the image can explain what the board does with it; the
                # suffix's CRC covers them, so it is written again.
                self.assertEqual(image[8:-4], bad[8:-4])
                self.assertEqual(len(image), len(bad))

    def test_each_kind_fails_the_check_it_is_meant_to(self):
        image = firmware()

        def why(kind):
            return "; ".join(
                dfuimage.startable_problems(dfuimage.make_unstartable(kind, image))
            )

        self.assertIn("stack pointer 0xffffffff is outside RAM", why("erased-vectors"))
        self.assertIn("is not word-aligned", why("misaligned-stack"))
        self.assertIn("stack pointer 0x40000000 is outside RAM", why("stack-outside-ram"))
        self.assertIn("is not a Thumb address", why("arm-reset-vector"))
        self.assertIn("outside the application flash", why("reset-vector-in-bootloader"))

    def test_the_bounds_are_the_bootloaders_own(self):
        def startable(stack_pointer, reset_vector, flash_top=dfuimage.FLASH_TOP):
            image = struct.pack("<II", stack_pointer, reset_vector) + bytes(64)
            return dfuimage.startable_problems(image, flash_top) == []

        self.assertTrue(startable(dfuimage.APP_RAM_LIMIT, RESET_VECTOR))
        self.assertFalse(startable(dfuimage.APP_RAM_BASE, RESET_VECTOR))
        self.assertFalse(startable(dfuimage.APP_RAM_LIMIT + 4, RESET_VECTOR))
        # The AHB banks a linker script outside the fork could use.
        self.assertTrue(startable(dfuimage.APP_AHB_RAM_BASE + 4, RESET_VECTOR))
        # The entry may sit at the first application address, not below it,
        # and must be inside the flash the caller allows.
        self.assertTrue(startable(STACK_POINTER, dfuimage.APPLICATION_OFFSET | 1))
        self.assertFalse(startable(STACK_POINTER, (dfuimage.APPLICATION_OFFSET - 2) | 1))
        self.assertFalse(startable(STACK_POINTER, (dfuimage.FLASH_TOP) | 1))
        self.assertTrue(startable(STACK_POINTER, 0x70001 | 1, flash_top=0x80000))

    def test_an_unknown_kind_and_a_short_image_are_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown unstartable image kind"):
            dfuimage.make_unstartable("no-such-kind", firmware())
        with self.assertRaisesRegex(ValueError, "shorter than the two vector words"):
            dfuimage.app_vectors(b"\x00\x01")


class MakeCommandTests(unittest.TestCase):

    def run_make(self, source_bytes):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "test_build.dfu"
            source.write_bytes(source_bytes)
            output = root / "unstartable.dfu"
            out = io.StringIO()
            with redirect_stdout(out):
                code = invalidapp.main(
                    ["make", "--image", str(source), "--output", str(output)]
                )
            data = output.read_bytes() if output.exists() else b""
            return code, out.getvalue(), data

    def test_it_writes_the_image_and_says_both_halves(self):
        code, output, data = self.run_make(firmware())
        self.assertEqual(0, code, output)
        self.assertIn("DFU suffix valid", output)
        self.assertIn(f"firmware info harness {REVISION}", output)
        self.assertIn("Unstartable: stack pointer 0xffffffff is outside RAM", output)
        self.assertEqual([], dfuimage.suffix_problems(data))
        self.assertNotEqual([], dfuimage.startable_problems(data))

    def test_it_refuses_a_source_that_is_not_a_valid_image(self):
        code, output, data = self.run_make(b"not firmware at all")
        self.assertEqual(1, code)
        self.assertIn("Source image is not a valid DFU file", output)
        self.assertEqual(b"", data)

    def test_it_refuses_a_source_the_board_could_not_run_either(self):
        code, output, _data = self.run_make(
            dfuimage.make_unstartable("erased-vectors", firmware())
        )
        self.assertEqual(1, code)
        self.assertIn("Source image would not start either", output)


class PowerCycleTests(unittest.TestCase):
    """The cycle itself: what it requires, and what it leaves behind."""

    def setUp(self):
        self.controller = hub()
        self.board = dfu_board()
        self.bus = FakeBus(self.controller, self.board)

    def assert_powered(self):
        self.assertTrue(self.controller.physical_power(PORT),
                        "the port was left without power")

    def test_a_bootloader_that_refuses_the_image_passes(self):
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(0, code, output)
        self.assertIn("Board off the bus after", output)
        self.assertIn("Port 1 power restored", output)
        self.assertIn("back on the bus", output)
        self.assertIn("Power cycle: the bootloader refused", output)
        self.assert_powered()

    def test_a_board_that_starts_the_image_fails_and_says_so(self):
        self.bus.cold_boot_runs_image = True
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("started the image the bootloader should have refused",
                      output)
        self.assertIn("write a good one back", output)
        self.assert_powered()

    def test_a_board_that_does_not_come_back_names_the_entry_pin(self):
        self.bus.faulted = True
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("not on the bus at all", output)
        self.assertIn("entry pin bridged by hand", output)
        self.assert_powered()

    def test_nothing_is_switched_when_the_board_is_in_its_application(self):
        self.board.mode = "app"
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("not sitting in its DFU bootloader", output)
        self.assertIn("enumerated by its serial", output)
        self.assert_powered()

    def test_nothing_is_switched_when_a_guard_is_missing(self):
        self.bus.guards.discard(GUARDS[0])
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("guard board(s) gone", output)
        self.assert_powered()

    def test_nothing_is_switched_when_another_board_could_be_the_one(self):
        self.bus.bootloaders = 2
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("2 DFU bootloader device(s) enumerated, expected 1", output)
        self.assert_powered()

    def test_an_unregistered_board_refuses_before_any_switch(self):
        self.bus.extra.add("dddd3333dddd3333dddd3333dddd3333")
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("unregistered Ubertooth enumerated", output)
        self.assert_powered()

    def test_a_port_already_off_is_refused_and_switched_back_on(self):
        controller = hub(powered=False)
        code, output = cycle(controller, FakeBus(controller, self.board))
        self.assertEqual(1, code)
        self.assertIn("is already off", output)
        self.assertTrue(controller.physical_power(PORT))

    def test_a_bus_that_cannot_be_read_while_the_port_is_off_restores_power(self):
        # The scan after the port goes off fails: the port is on again, and
        # the reason is the bus, not the check.
        self.bus.raise_after = (2, EnumerationError("cannot read /sys/bus/usb"))
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("power cycle failed: cannot read /sys/bus/usb", output)
        self.assert_powered()

    def test_a_signal_while_the_port_is_off_restores_power(self):
        self.bus.raise_after = (2, invalidapp.PowerCycleInterrupted("SIGTERM"))
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("power cycle interrupted: SIGTERM", output)
        self.assert_powered()

    def test_a_keyboard_interrupt_while_the_port_is_off_restores_power(self):
        self.bus.raise_after = (2, KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            cycle(self.controller, self.bus)
        # Not caught, so the command still ends; the port is on either way.
        self.assert_powered()

    def test_a_controller_that_cannot_switch_off_leaves_the_port_on(self):
        def refuse(port, enabled):
            if not enabled:
                raise PowerControllerError("set power on port 1 rejected")
            return original(port, enabled)

        original = self.controller.set_power_enabled
        self.controller.set_power_enabled = refuse
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("rejected", output)
        self.assert_powered()

    def test_a_restoration_that_fails_once_still_powers_the_port(self):
        failures = []
        original = self.controller.set_power_enabled

        def flaky(port, enabled):
            if enabled and not failures:
                failures.append(port)
                raise PowerControllerError("controller I/O failed")
            return original(port, enabled)

        self.controller.set_power_enabled = flaky
        code, output = cycle(self.controller, self.bus)
        # restore_port_states() retries a port once, reconnecting first, so a
        # transient controller error costs the cycle nothing.
        self.assertEqual([PORT], failures)
        self.assertEqual(0, code, output)
        self.assert_powered()

    def test_a_port_that_cannot_be_restored_is_the_failure_reported(self):
        original = self.controller.set_power_enabled

        def refuse(port, enabled):
            if enabled:
                raise PowerControllerError("controller is gone")
            return original(port, enabled)

        self.controller.set_power_enabled = refuse
        code, output = cycle(self.controller, self.bus)
        self.assertEqual(1, code)
        self.assertIn("PORT 1 STILL OFF", output)
        self.assertIn("someone switches it on", output)
        self.assertFalse(self.controller.physical_power(PORT))

    def test_every_exit_path_restores_the_port(self):
        """One table of the ways the command can end, each starting fresh.

        A port left without power is the one outcome that needs someone at
        the bench, so every path is checked rather than the interesting ones.
        """
        def application(bus):
            bus.board.mode = "app"

        def interrupted(bus):
            bus.raise_after = (2, invalidapp.PowerCycleInterrupted("SIGTERM"))

        def unreadable(bus):
            bus.raise_after = (1, EnumerationError("cannot read the bus"))

        def missing_guard(bus):
            bus.guards.clear()

        def ran_the_image(bus):
            bus.cold_boot_runs_image = True

        def faulted(bus):
            bus.faulted = True

        def two_bootloaders(bus):
            bus.bootloaders = 2

        for name, arrange in (
            ("passes", lambda bus: None),
            ("board in its application", application),
            ("interrupted", interrupted),
            ("bus unreadable", unreadable),
            ("guard missing", missing_guard),
            ("board ran the image", ran_the_image),
            ("board faulted", faulted),
            ("two bootloaders", two_bootloaders),
        ):
            with self.subTest(name):
                controller = hub()
                bus = FakeBus(controller, dfu_board())
                arrange(bus)
                cycle(controller, bus)
                self.assertTrue(
                    controller.physical_power(PORT),
                    f"{name} left the port without power",
                )


class PortCommandTests(unittest.TestCase):
    """The cleanup step: the port is powered, whatever the case did."""

    def run_port(self, controller, *, allow=True):
        with tempfile.TemporaryDirectory() as directory:
            hardware = registry_file(Path(directory))
            argv = ["port", "--hardware", str(hardware), "--board", BOARD,
                    "--controller", CONTROLLER]
            if allow:
                argv.append("--allow-power-changes")
            out = io.StringIO()
            with redirect_stdout(out):
                code = invalidapp.main(
                    argv,
                    controller_factory=lambda resource, **kwargs: controller,
                )
            return code, out.getvalue()

    def test_a_powered_port_is_left_alone(self):
        controller = hub()
        code, output = self.run_port(controller)
        self.assertEqual(0, code, output)
        self.assertIn("Port 1 power: on", output)
        self.assertIn("port 1 is powered", output)

    def test_a_port_left_off_is_switched_back_on(self):
        controller = hub(powered=False)
        code, output = self.run_port(controller)
        self.assertEqual(0, code, output)
        self.assertIn("switching it back on", output)
        self.assertTrue(controller.physical_power(PORT))

    def test_a_port_that_cannot_be_switched_on_fails_loudly(self):
        controller = hub(powered=False)
        controller.set_power_enabled = mock.Mock(
            side_effect=PowerControllerError("controller is gone")
        )
        code, output = self.run_port(controller)
        self.assertEqual(1, code)
        self.assertIn("PORT 1 STILL OFF", output)

    def test_it_does_nothing_without_the_opt_in(self):
        controller = hub()
        built = []
        with tempfile.TemporaryDirectory() as directory:
            hardware = registry_file(Path(directory))
            out = io.StringIO()
            with redirect_stdout(out):
                code = invalidapp.main(
                    ["port", "--hardware", str(hardware), "--board", BOARD,
                     "--controller", CONTROLLER],
                    controller_factory=lambda resource, **kwargs: built.append(1),
                )
        self.assertEqual(1, code)
        self.assertIn("needs --allow-power-changes", out.getvalue())
        self.assertEqual([], built)
        self.assertTrue(controller.physical_power(PORT))


class RegistryTargetTests(unittest.TestCase):
    """The port comes from the registry, and only from there."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.registry = FileResourceRegistry.load(registry_file(Path(self._dir.name)))

    def tearDown(self):
        self._dir.cleanup()

    def test_the_boards_own_mapping_decides_the_port(self):
        target = plan_power_cycle(self.registry, "Host_Linux", BOARD, CONTROLLER)
        self.assertEqual((BOARD, CONTROLLER, PORT),
                         (target.resource_alias, target.controller_alias, target.port))
        # The plan may leave the controller to the registry.
        self.assertEqual(target, plan_power_cycle(self.registry, "Host_Linux", BOARD))

    def test_a_plan_that_names_another_controller_is_refused(self):
        with self.assertRaisesRegex(RegistryError, "the plan names the wrong controller"):
            plan_power_cycle(self.registry, "Host_Linux", BOARD, "Hub_Acroname")

    def test_a_board_with_no_power_mapping_cannot_be_cycled(self):
        text = HARDWARE.replace("    power: {controller: Hub_StarTech, port: 1}\n", "", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.yaml"
            path.write_text(text, encoding="utf-8")
            registry = FileResourceRegistry.load(path)
        with self.assertRaisesRegex(RegistryError, "has no power mapping"):
            plan_power_cycle(registry, "Host_Linux", BOARD)

    def test_a_controller_that_cannot_switch_vbus_per_port_cannot_be_used(self):
        text = HARDWARE.replace(
            "    capabilities: [per-port VBUS switching]",
            "    capabilities: [power readback]", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.yaml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(RegistryError):
                # The registry itself refuses a StarTech hub without it; a
                # driver that validates leaves plan_power_cycle to refuse.
                plan_power_cycle(FileResourceRegistry.load(path),
                                 "Host_Linux", BOARD)

    def test_the_cycle_command_reads_the_registry_for_the_port(self):
        controller = hub()
        board = dfu_board()
        bus = FakeBus(controller, board)
        with tempfile.TemporaryDirectory() as directory:
            hardware = registry_file(Path(directory))
            out = io.StringIO()
            with redirect_stdout(out):
                code = invalidapp.main(
                    ["cycle", "--hardware", str(hardware), "--board", BOARD,
                     "--controller", CONTROLLER, "--allow-power-changes",
                     "--serial", SERIAL, "--guard", GUARDS[0],
                     "--guard", GUARDS[1]],
                    enumerator_factory=lambda: bus,
                    controller_factory=lambda resource, **kwargs: controller,
                    install_handlers=False,
                    timing=FAST,
                )
        self.assertEqual(0, code, out.getvalue())
        self.assertIn("Port 1 power restored", out.getvalue())
        self.assertTrue(controller.physical_power(PORT))


class RecoverTests(unittest.TestCase):
    """Writing the run's image back, with and without a selector."""

    def run_recover(self, board, image_bytes, guards=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "test_build.dfu"
            image.write_bytes(image_bytes)
            argv = ["recover", "--serial", SERIAL, "--image", str(image),
                    "--revision", REVISION,
                    "--readback", str(root / "readback.bin")]
            for guard in guards:
                argv += ["--guard", guard]
            out = io.StringIO()
            with redirect_stdout(out):
                code = invalidapp.main(argv, board_factory=board._board)
            return code, out.getvalue()

    def test_a_board_in_its_bootloader_is_written_without_a_selector(self):
        board = dfu_board()
        code, output = self.run_recover(board, firmware())
        self.assertEqual(0, code, output)
        self.assertIn("looking for its DFU bootloader", output)
        self.assertIn("Recovered: application flash holds the test image", output)
        self.assertEqual("app", board.mode)
        self.assertIn(("ubertooth-dfu", "-d", mock.ANY, "-r"),
                      [tuple(c[:2]) + (mock.ANY, c[3]) if len(c) == 4 else c
                       for c in board.commands])

    def test_a_board_already_running_the_test_image_is_left_alone(self):
        image = firmware()
        board = app_board(image)
        code, output = self.run_recover(board, image)
        self.assertEqual(0, code, output)
        self.assertIn("already runs the test image", output)
        self.assertNotIn("-d", [part for command in board.commands for part in command])

    def test_a_board_running_another_image_is_rewritten_by_serial(self):
        board = app_board(firmware(revision="git-other"))
        board.revision = "git-other"
        code, output = self.run_recover(board, firmware())
        self.assertEqual(0, code, output)
        self.assertIn("writing it", output)
        self.assertIn("Recovered: application flash holds the test image", output)
        self.assertTrue(any("-U" in command for command in board.commands))

    def test_two_dfu_devices_stop_the_recovery_rather_than_guess(self):
        board = dfu_board(dfu_count_extra=1)
        code, output = self.run_recover(board, firmware())
        self.assertEqual(1, code)
        self.assertIn("2 DFU devices attached; recover the board by hand", output)

    def test_a_guard_that_does_not_answer_stops_the_recovery(self):
        board = dfu_board()
        code, output = self.run_recover(board, firmware(), guards=("ffff",))
        self.assertEqual(1, code)
        self.assertIn("does not answer, so the DFU device may be it", output)


class CaseTests(unittest.TestCase):
    """The case the catalog builds, before anything runs."""

    def setUp(self):
        self.definition = builtin_catalog().get(invalidapp_cases.TEST_NAME)
        arguments = self.definition.normalize_arguments({
            "hosts": ["Host_Linux"], "platforms": ["linux"],
            "Ubertooth": BOARD, "USB power controller": CONTROLLER,
        })
        self.cases = self.definition.command_cases(
            arguments,
            {"Ubertooth": BOARD, "USB power controller": CONTROLLER},
            {"Ubertooth": {"serial": SERIAL},
             "guard Ubertooth 1": {"serial": GUARDS[0]}},
        )
        self.case = self.cases[0]
        self.context = {
            REGISTRY_PATH_KEY: "/bench/hardware.yaml",
            "capture dir": "/run/captures/001",
            f"artifact {invalidapp_cases.TEST_IMAGE}": "/run/build/test.dfu",
            "expected firmware revision": REVISION,
        }

    def argv(self, step_id):
        for step in self.case.steps + self.case.cleanup:
            if step.step_id == step_id:
                return step.resolve_argv(self.context)
        raise AssertionError(f"no step {step_id}")

    def test_it_is_one_case_with_the_steps_in_order(self):
        self.assertEqual(1, len(self.cases))
        self.assertEqual(
            ["read-original-revision", "make", "write", "cycle", "recover"],
            [step.step_id for step in self.case.steps],
        )
        self.assertEqual(
            ["restore-port-power", "restore-image", "verify-restored-revision"],
            [step.step_id for step in self.case.cleanup],
        )

    def test_the_cleanup_restores_the_port_before_it_writes_the_board(self):
        # A board whose port is off cannot be written, so the order matters.
        self.assertEqual("restore-port-power", self.case.cleanup[0].step_id)
        self.assertIn("port", self.argv("restore-port-power"))
        self.assertIn("recover", self.argv("restore-image"))

    def test_the_power_steps_read_the_bench_from_the_registry(self):
        for step_id in ("cycle", "restore-port-power"):
            with self.subTest(step_id):
                argv = self.argv(step_id)
                self.assertIn("--hardware", argv)
                self.assertEqual("/bench/hardware.yaml",
                                 argv[argv.index("--hardware") + 1])
                self.assertEqual(BOARD, argv[argv.index("--board") + 1])
                self.assertEqual(CONTROLLER, argv[argv.index("--controller") + 1])
                self.assertIn("--allow-power-changes", argv)
                self.assertNotIn("--port", argv)  # never from a command line

    def test_the_cycle_step_names_the_board_and_its_guards(self):
        argv = self.argv("cycle")
        self.assertEqual(SERIAL, argv[argv.index("--serial") + 1])
        self.assertEqual([GUARDS[0]],
                         [argv[index + 1] for index, part in enumerate(argv)
                          if part == "--guard"])

    def test_the_write_step_must_not_report_a_verified_revision(self):
        step = next(s for s in self.case.steps if s.step_id == "write")
        self.assertIn("Verified firmware revision", step.forbidden_patterns[0])
        self.assertIn("firmware programming completed", step.output_patterns[0])
        self.assertEqual((0, 1), step.expected_returncodes)
        self.assertTrue(step.mutates)

    def test_every_step_that_changes_the_board_says_so(self):
        changing = {step.step_id for step in self.case.steps + self.case.cleanup
                    if step.mutates}
        self.assertEqual(
            {"write", "cycle", "recover", "restore-port-power", "restore-image"},
            changing,
        )

    def test_every_step_has_a_bounded_timeout(self):
        for step in self.case.steps + self.case.cleanup:
            with self.subTest(step.step_id):
                self.assertGreater(step.timeout_s, 0)
                self.assertLess(step.timeout_s, 300)

    def test_a_run_without_the_registry_path_fails_the_step_by_name(self):
        step = next(s for s in self.case.steps if s.step_id == "cycle")
        del self.context[REGISTRY_PATH_KEY]
        with self.assertRaises(LookupError) as caught:
            step.resolve_argv(self.context)
        self.assertEqual(REGISTRY_PATH_KEY, caught.exception.args[0])

    def test_the_catalog_declares_what_the_run_has_to_arrange(self):
        self.assertTrue(self.definition.switches_power)
        self.assertTrue(self.definition.writes_firmware)
        self.assertTrue(self.definition.establishes_firmware)
        self.assertTrue(self.definition.build_required.firmware_build)
        self.assertTrue(self.definition.guards_other_ubertooths)
        self.assertEqual(
            {"Ubertooth": "Ubertooth", "USB power controller": "USB power controller"},
            dict(self.definition.hardware_roles),
        )


class RunRequirementTests(unittest.TestCase):
    """What run-suite must arrange before the test may run."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.registry = FileResourceRegistry.load(registry_file(root))
        plan = root / "power.yaml"
        plan.write_text(
            Path("testplans/power.yaml").read_text(encoding="utf-8")
            .replace("Ubertooth: default", f"Ubertooth: {BOARD}")
            .replace("Hub_Linux0", CONTROLLER),
            encoding="utf-8",
        )
        self.catalog = builtin_catalog()
        self.invocations = PlanCompiler(self.catalog, self.registry).compile(
            load_plan(plan, self.catalog), "power"
        )

    def tearDown(self):
        self._dir.cleanup()

    def test_planning_resolves_the_port_from_the_registry(self):
        invocation, = self.invocations
        self.assertEqual(CONTROLLER, invocation.power_cycle.controller_alias)
        self.assertEqual(PORT, invocation.power_cycle.port)
        self.assertEqual(BOARD, invocation.power_cycle.resource_alias)

    def test_the_run_holds_the_host_lock(self):
        self.assertTrue(leases.needs_host_lock(self.invocations, self.catalog))

    def test_the_run_leases_the_board_and_every_guard(self):
        boards = leases.boards_for_invocations(self.invocations, self.registry)
        self.assertEqual(
            {BOARD, "UT_Linux1", "UT_Linux2"},
            {board.alias for board in boards},
        )

    def test_a_run_without_the_power_opt_in_is_refused(self):
        with self.assertRaisesRegex(HarnessError, "power-cycles the board"):
            _check_power_cycles_armed(
                SimpleNamespace(allow_power_changes=False), self.invocations
            )
        _check_power_cycles_armed(
            SimpleNamespace(allow_power_changes=True), self.invocations
        )

    def test_the_refusal_names_the_board_and_port(self):
        with self.assertRaises(HarnessError) as caught:
            _check_power_cycles_armed(
                SimpleNamespace(allow_power_changes=False), self.invocations
            )
        self.assertIn(f"{BOARD} on {CONTROLLER} port {PORT}", str(caught.exception))


class LeaseTests(unittest.TestCase):
    """The commands never touch the run's leases or the host lock."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def held(self):
        """What the lease directory holds, file by file."""
        return {
            str(path.relative_to(self.dir)): path.read_bytes()
            for path in sorted(self.dir.rglob("*")) if path.is_file()
        }

    def run_under_leases(self, action):
        board = leases.Board(BOARD, SERIAL)
        with leases.acquire(
            [board],
            leases.holder_info(run_id="run-141", owner="test",
                               purpose="run-suite power / power"),
            directory=self.dir, host_lock=True,
        ) as held:
            self.assertTrue(held.holds_host_lock())
            before = self.held()
            result = action()
            self.assertEqual(before, self.held(),
                             "the command changed the lease directory")
            self.assertTrue(held.holds_host_lock(),
                            "the host lock was not held afterwards")
            self.assertEqual(
                [BOARD], [lease.board.alias for lease in held.leases
                          if lease.board != leases.HOST_LOCK],
            )
            return result

    def test_the_cycle_keeps_the_leases_and_the_host_lock(self):
        controller = hub()
        bus = FakeBus(controller, dfu_board())
        code, _output = self.run_under_leases(
            lambda: cycle(controller, bus)
        )
        self.assertEqual(0, code)
        self.assertTrue(controller.physical_power(PORT))

    def test_a_failed_cycle_keeps_them_too(self):
        controller = hub()
        bus = FakeBus(controller, dfu_board())
        bus.cold_boot_runs_image = True
        code, _output = self.run_under_leases(lambda: cycle(controller, bus))
        self.assertEqual(1, code)

    def test_the_port_command_keeps_them(self):
        controller = hub(powered=False)

        def run_port():
            hardware = registry_file(self.dir.parent)
            out = io.StringIO()
            with redirect_stdout(out):
                return invalidapp.main(
                    ["port", "--hardware", str(hardware), "--board", BOARD,
                     "--controller", CONTROLLER, "--allow-power-changes"],
                    controller_factory=lambda resource, **kwargs: controller,
                )

        self.assertEqual(0, self.run_under_leases(run_port))
        self.assertTrue(controller.physical_power(PORT))


class FullCaseTests(unittest.TestCase):
    """The case run end to end by the sequencer, against the fakes."""

    def run_case(self, *, arrange=None, image=None):
        image_bytes = image or firmware()
        definition = builtin_catalog().get(invalidapp_cases.TEST_NAME)
        arguments = definition.normalize_arguments({
            "hosts": ["Host_Linux"], "platforms": ["linux"],
            "Ubertooth": BOARD, "USB power controller": CONTROLLER,
        })
        board = app_board(image_bytes)
        controller = hub()
        bus = FakeBus(controller, board, guards=())
        if arrange is not None:
            arrange(board, bus, controller)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hardware = registry_file(root)
            image_path = root / "test_build.dfu"
            image_path.write_bytes(image_bytes)
            (root / "plan.yaml").write_text("plan\n")
            invocation = TestInvocation(
                "001-invalid", invalidapp_cases.TEST_NAME, "Host_Linux", arguments,
                {"Ubertooth": BOARD, "USB power controller": CONTROLLER},
                {"Ubertooth": {"serial": SERIAL}},
            )
            manifest = build_run_manifest(
                run_id="run-141", plan_name="power", suite="power",
                host="Host_Linux", plan_path=root / "plan.yaml",
                hardware_path=hardware,
                # The expected revision the manifest derives from this is
                # "git-" plus the last field, which is REVISION.
                sources=(SourceRevision("Ubertooth", "/source",
                                        "https://example.invalid/u.git",
                                        "test-ref", "a" * 40, "test"),),
                invocations=(invocation,),
                artifacts=(FileIdentity.from_path("test build", image_path),),
                created_at="2026-11-02T00:00:00+00:00",
            )
            check = PreflightCheck(
                "preflight-001-host-version", "Host version", "Host_Linux", None,
                {}, ("ubertooth-util", "-V"),
                (r"libubertooth\s+\S+\s+\(git\-test\)",), REVISION)
            executor = HelperExecutor(board, bus, controller)
            sink = CollectSink()
            summary = NamedTestSequencer(
                executor, event_sink=sink, registry_path=hardware,
            ).run((invocation,), root / "run", run_id="run-141",
                  manifest=manifest, preflight_checks=(check,))
        return SimpleNamespace(summary=summary, events=sink.events, board=board,
                               controller=controller, bus=bus)

    def steps(self, events):
        return {
            event.details["step_id"]: event.status
            for event in events if event.event_type == "step_finished"
        }

    def case_status(self, events):
        return [event.status for event in events
                if event.event_type == "check_finished"
                and event.details.get("phase") == "test"]

    def test_a_board_whose_bootloader_refuses_the_image_passes(self):
        run = self.run_case()
        self.assertEqual(["passed"], self.case_status(run.events))
        self.assertEqual("passed", self.steps(run.events)["cycle"])
        self.assertEqual("passed", self.steps(run.events)["restore-port-power"])
        self.assertEqual("app", run.board.mode)
        self.assertEqual(REVISION, run.board.revision)
        self.assertTrue(run.controller.physical_power(PORT))

    def test_a_board_that_starts_the_image_fails_and_is_recovered(self):
        def arrange(_board, bus, _controller):
            bus.cold_boot_runs_image = True

        run = self.run_case(arrange=arrange)
        self.assertEqual(["failed"], self.case_status(run.events))
        self.assertEqual("failed", self.steps(run.events)["cycle"])
        # The cleanup still put the board back and left the port powered.
        self.assertEqual("passed", self.steps(run.events)["restore-image"])
        self.assertEqual("app", run.board.mode)
        self.assertEqual(REVISION, run.board.revision)
        self.assertTrue(run.controller.physical_power(PORT))

    def test_a_port_left_off_by_the_cycle_is_restored_by_the_cleanup(self):
        def arrange(_board, bus, controller):
            # The cycle's own restoration fails, so the cleanup is the only
            # thing left to power the port; it uses a controller that works.
            original = controller.set_power_enabled
            calls = []

            def flaky(port, enabled):
                calls.append(enabled)
                if enabled and calls.count(True) <= 2:
                    raise PowerControllerError("controller I/O failed")
                return original(port, enabled)

            controller.set_power_enabled = flaky

        run = self.run_case(arrange=arrange)
        self.assertEqual(["failed"], self.case_status(run.events))
        self.assertEqual("failed", self.steps(run.events)["cycle"])
        self.assertEqual("passed", self.steps(run.events)["restore-port-power"])
        self.assertTrue(run.controller.physical_power(PORT))

    def test_the_run_record_names_the_commands_and_the_image(self):
        run = self.run_case()
        argv = [
            event.details["argv"] for event in run.events
            if event.event_type == "command_started"
        ]
        written = [parts for parts in argv
                   if parts[0] == "ubertooth-dfu" and "-d" in parts]
        self.assertTrue(written)
        self.assertTrue(any("unstartable.dfu" in " ".join(parts)
                            for parts in written))


class HelperExecutor:
    """The fake executor: harness helpers in process, host tools on the fake.

    The helper scripts are run by calling their ``main`` with the fakes
    injected, rather than as subprocesses, so no real controller or bus is
    reachable from a unit test.
    """

    def __init__(self, board, bus, controller):
        self.board = board
        self.bus = bus
        self.controller = controller

    def run(self, argv, **kwargs):
        command = tuple(argv)
        if command[:2] == (sys.executable, invalidapp_cases._HELPER):
            out = io.StringIO()
            with redirect_stdout(out):
                code = invalidapp.main(
                    list(command[2:]),
                    board_factory=self.board._board,
                    enumerator_factory=lambda: self.bus,
                    controller_factory=lambda resource, **_kwargs: self.controller,
                    install_handlers=False,
                    timing=FAST,
                )
            return self.board._result(command, code, out.getvalue())
        return self.board.run(argv, **kwargs)


if __name__ == "__main__":
    unittest.main()
