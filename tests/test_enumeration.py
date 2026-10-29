import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ubertooth_harness.enumeration import (
    EnumerationError,
    EnumerationExpectation,
    IOREG_TIMEOUT_S,
    IoregUbertoothEnumerator,
    SysfsUbertoothEnumerator,
    local_enumerator,
    wait_for_enumeration,
)
from ubertooth_harness.power import (
    PortState,
    PowerControllerError,
    SimulatedPowerController,
)
from ubertooth_harness.topology import (
    IsolationVerification,
    PowerIsolationAction,
    applied_power_isolation,
)

SELECTED = "1cc001038088cb956c43ad61c52000f5"
EXTRA = "05f00014e380df95f4436562c32000f5"


def write_device(root, name, vendor, product, serial=None):
    device = Path(root) / name
    device.mkdir()
    (device / "idVendor").write_text(vendor + "\n")
    (device / "idProduct").write_text(product + "\n")
    if serial is not None:
        (device / "serial").write_text(serial + "\n")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PortEnumerator:
    """Boards are enumerated exactly when their controller port has power."""

    def __init__(self, controller, boards, unpowered=(), lag_polls=0):
        self.controller = controller
        self.boards = boards  # port -> serial
        self.unpowered = frozenset(unpowered)  # always-enumerated extras
        self.lag_polls = lag_polls  # polls a board lingers after losing power
        self._lingering = {}

    def serials(self):
        seen = set(self.unpowered)
        for port, serial in self.boards.items():
            # Physical port state, independent of the control connection.
            if self.controller.physical_power(port):
                self._lingering.pop(port, None)
                seen.add(serial)
            else:
                left = self._lingering.setdefault(port, self.lag_polls)
                if left > 0:
                    self._lingering[port] = left - 1
                    seen.add(serial)
        return frozenset(seen)


class CountingProvider:
    """Controller provider that records whether any hardware was touched."""

    def __init__(self, controller):
        self.controller = controller
        self.calls = 0

    def __call__(self, alias):
        self.calls += 1
        return self.controller


def simulated(port_power):
    return SimulatedPowerController(
        0x12345678,
        {
            port: PortState(port, on, True, 5_000_000 if on else 0, 0)
            for port, on in port_power.items()
        },
    )


ACTIONS = (PowerIsolationAction("UT_Linux1", "Hub_Linux0", 3, False),)


class SysfsEnumeratorTests(unittest.TestCase):

    def test_lists_ubertooths_and_reports_serial_less_dfu_as_unidentified(self):
        with tempfile.TemporaryDirectory() as root:
            write_device(root, "3-2.1", "1d50", "6002", SELECTED.upper())
            write_device(root, "3-2.2", "1d50", "6002", EXTRA)
            # DFU bootloader has no serial number (iSerialNumber = 0).
            write_device(root, "3-2.3", "1d50", "6003")
            write_device(root, "3-2.4", "0403", "6001", "B001DBLH")  # FTDI: ignored
            (Path(root) / "3-2.1:1.0").mkdir()  # interface: no idVendor

            self.assertEqual(
                frozenset({SELECTED, EXTRA, "unidentified:3-2.3:6003"}),
                SysfsUbertoothEnumerator(root).serials(),
            )

    def test_entries_without_device_attributes_are_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            write_device(root, "3-2.1", "1d50", "6002", SELECTED)
            (Path(root) / "3-2.9").mkdir()  # no idVendor (interface-like)
            self.assertEqual(
                frozenset({SELECTED}), SysfsUbertoothEnumerator(root).serials()
            )

    def test_device_removed_while_reading_serial_is_skipped(self):
        import shutil
        with tempfile.TemporaryDirectory() as root:
            write_device(root, "3-2.1", "1d50", "6002", SELECTED)
            write_device(root, "3-2.3", "1d50", "6002", EXTRA)
            original = Path.read_text

            def vanish(path, *args, **kwargs):
                if path.parent.name == "3-2.3" and path.name == "serial":
                    shutil.rmtree(path.parent)  # unplugged mid-scan
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", vanish):
                serials = SysfsUbertoothEnumerator(root).serials()
            self.assertEqual(frozenset({SELECTED}), serials)

    def test_undecodable_attributes_are_handled(self):
        with tempfile.TemporaryDirectory() as root:
            write_device(root, "3-2.1", "1d50", "6002", SELECTED)
            write_device(root, "3-2.2", "1d50", "6002", "x")
            (Path(root) / "3-2.2" / "serial").write_bytes(b"\xff\xfe")
            write_device(root, "3-2.3", "1d50", "6002", EXTRA)
            (Path(root) / "3-2.3" / "idVendor").write_bytes(b"\xff")
            self.assertEqual(
                frozenset({SELECTED, "unidentified:3-2.2:6002"}),
                SysfsUbertoothEnumerator(root).serials(),
            )

    def test_empty_serial_is_unidentified(self):
        with tempfile.TemporaryDirectory() as root:
            write_device(root, "3-2.2", "1d50", "6002", "")
            self.assertEqual(
                frozenset({"unidentified:3-2.2:6002"}),
                SysfsUbertoothEnumerator(root).serials(),
            )

    def test_unreadable_root_fails_closed(self):
        with self.assertRaisesRegex(EnumerationError, "cannot read USB devices"):
            SysfsUbertoothEnumerator("/nonexistent/usb").serials()


# tests/data/ioreg-iousb.plist was derived from a real
# `ioreg -p IOUSB -l -w0 -a` run on a Mac: the tree shape, property names and
# value types are that machine's, and the Ubertooth vendor and product ids are
# the real ones, since that is what the enumerator matches. Everything that
# identifies a machine or a device is made up: the device and vendor names,
# the other devices' ids, the bus locations and the serials, because this
# repository is public and no board was attached to record. These are the two
# Ubertooths in it.
IOREG_FIXTURE = Path(__file__).resolve().parent / "data" / "ioreg-iousb.plist"
FIRST = "a1b2c3d4e5f600112233445566778899"
SECOND = "99887766554433221100f6e5d4c3b2a1"


def ioreg_runner(output=b"", returncode=0, stderr=b"", error=None, calls=None):
    """Stand-in for subprocess.run that returns recorded ioreg output."""
    def run(argv, **kwargs):
        if calls is not None:
            calls.append((tuple(argv), kwargs))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(argv, returncode, output, stderr)
    return run


class IoregEnumeratorTests(unittest.TestCase):

    def enumerator(self, **kwargs):
        return IoregUbertoothEnumerator(run=ioreg_runner(**kwargs))

    def test_lists_ubertooths_from_recorded_ioreg_output(self):
        serials = self.enumerator(output=IOREG_FIXTURE.read_bytes()).serials()
        self.assertEqual(
            frozenset({
                FIRST,              # straight on a root controller
                SECOND,             # behind a hub
                # DFU bootloader: no serial string, named by its bus location.
                "unidentified:02220000:6003",
            }),
            serials,
        )

    def test_reads_the_registry_without_opening_a_device(self):
        calls = []
        self.enumerator(output=IOREG_FIXTURE.read_bytes(), calls=calls).serials()
        (argv, kwargs), = calls
        self.assertEqual(("ioreg", "-p", "IOUSB", "-l", "-w0", "-a"), argv)
        self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
        self.assertEqual(IOREG_TIMEOUT_S, kwargs["timeout"])
        self.assertFalse(kwargs["check"])

    def plist(self, children):
        return plistlib.dumps({"IORegistryEntryChildren": children})

    def device(self, vendor, product, serial=None, location="02100000", **extra):
        node = {
            "IOObjectClass": "IOUSBHostDevice",
            "IORegistryEntryName": "Device",
            "IORegistryEntryLocation": location,
            "idVendor": vendor,
            "idProduct": product,
            **extra,
        }
        if serial is not None:
            node["USB Serial Number"] = serial
            node["kUSBSerialNumberString"] = serial
        return node

    def test_other_vendors_and_products_are_ignored(self):
        output = self.plist([
            self.device(0x0403, 0x6001, "B001DBLH"),       # FTDI
            self.device(0x1d50, 0x6018, "ignored"),        # another GSG board
            self.device(0x1d50, 0x6002, FIRST.upper()),
        ])
        self.assertEqual(
            frozenset({FIRST}), self.enumerator(output=output).serials()
        )

    def test_every_ubertooth_product_counts(self):
        output = self.plist([
            self.device(0x1d50, product, f"{product:04x}00000000000000000000000000", location=f"0{n}")
            for n, product in enumerate((0x6000, 0x6001, 0x6002, 0x6003))
        ])
        self.assertEqual(4, len(self.enumerator(output=output).serials()))

    def test_an_interface_entry_does_not_become_a_second_board(self):
        # An IOUSBHostInterface repeats its device's ids and serial, so a
        # parser that counted every node would see a board twice. A serial-less
        # board would be counted twice under two names, which no amount of
        # deduplicating by serial would catch, so both the interface number
        # and the object class are checked.
        for extra in ({"bInterfaceNumber": 0}, {}):
            with self.subTest(extra=sorted(extra)):
                board = self.device(0x1d50, 0x6003)
                board["IORegistryEntryChildren"] = [{
                    "IOObjectClass": "IOUSBHostInterface",
                    "idVendor": 0x1d50,
                    "idProduct": 0x6003,
                    "IORegistryEntryLocation": "0",
                    **extra,
                }]
                self.assertEqual(
                    frozenset({"unidentified:02100000:6003"}),
                    self.enumerator(output=self.plist([board])).serials(),
                )

    def test_a_serial_less_board_is_named_by_whatever_locates_it(self):
        node = self.device(0x1d50, 0x6003)
        del node["IORegistryEntryLocation"]
        node["locationID"] = 0x02220000
        self.assertEqual(
            frozenset({"unidentified:02220000:6003"}),
            self.enumerator(output=self.plist([node])).serials(),
        )
        bare = self.device(0x1d50, 0x6003)
        del bare["IORegistryEntryLocation"]
        bare["IORegistryEntryName"] = "Ubertooth One DFU"
        self.assertEqual(
            frozenset({"unidentified:Ubertooth One DFU:6003"}),
            self.enumerator(output=self.plist([bare])).serials(),
        )

    def test_an_empty_or_odd_serial_is_unidentified(self):
        for serial in ("", "   "):
            output = self.plist([self.device(0x1d50, 0x6002, serial)])
            self.assertEqual(
                frozenset({"unidentified:02100000:6002"}),
                self.enumerator(output=output).serials(),
                serial,
            )

    def test_unexpected_property_types_are_skipped(self):
        output = self.plist([
            self.device("1d50", "6002", FIRST),            # ids as strings
            self.device(0x1d50, 0x6002, 12345, location="02110000"),  # serial not text
            {"IORegistryEntryChildren": "not a list"},
            "not a node",
        ])
        self.assertEqual(
            frozenset({"unidentified:02110000:6002"}),
            self.enumerator(output=output).serials(),
        )

    def test_no_devices_is_an_empty_set_not_an_error(self):
        self.assertEqual(
            frozenset(), self.enumerator(output=plistlib.dumps({})).serials()
        )

    def test_unparsable_output_fails_closed(self):
        with self.assertRaisesRegex(EnumerationError, "cannot parse ioreg output"):
            self.enumerator(output=b"not a plist").serials()

    def test_a_failed_command_fails_closed(self):
        with self.assertRaisesRegex(EnumerationError, r"ioreg exited 1: ioreg: bad plane"):
            self.enumerator(returncode=1, stderr=b"ioreg: bad plane\nmore\n").serials()
        with self.assertRaisesRegex(EnumerationError, "ioreg exited 1$"):
            self.enumerator(returncode=1).serials()

    def test_a_missing_or_hung_command_fails_closed(self):
        for error in (
            FileNotFoundError(2, "No such file or directory"),
            subprocess.TimeoutExpired(["ioreg"], 15.0),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaisesRegex(
                    EnumerationError, "cannot read USB devices from ioreg"
                ):
                    self.enumerator(error=error).serials()


class LocalEnumeratorTests(unittest.TestCase):

    def test_each_platform_gets_its_read_only_enumerator(self):
        self.assertIsInstance(local_enumerator("linux"), SysfsUbertoothEnumerator)
        self.assertIsInstance(local_enumerator("darwin"), IoregUbertoothEnumerator)
        with self.assertRaisesRegex(EnumerationError, "not supported on win32"):
            local_enumerator("win32")

    def test_this_machine_has_one(self):
        self.assertTrue(hasattr(local_enumerator(), "serials"))


class WaitTests(unittest.TestCase):

    def test_state_must_hold_for_settle_time(self):
        clock = FakeClock()
        sequence = [frozenset()] + [frozenset({EXTRA})] + [frozenset()] * 50

        class Flapping:
            def serials(self):
                return sequence.pop(0)

        waited = wait_for_enumeration(
            Flapping(),
            EnumerationExpectation.of(absent=(EXTRA,)),
            10.0,
            settle_s=1.0,
            clock=clock,
            sleep=clock.sleep,
        )
        # The first match is discarded when the board reappears; the clock
        # must run a full settle window after the second match.
        self.assertGreaterEqual(waited, 1.2)

    def test_times_out_with_details(self):
        clock = FakeClock()

        class Stuck:
            def serials(self):
                return frozenset({EXTRA})

        with self.assertRaisesRegex(
            EnumerationError, f"still enumerated: {EXTRA}.*not enumerated: {SELECTED}"
        ):
            wait_for_enumeration(
                Stuck(),
                EnumerationExpectation.of(absent=(EXTRA,), present=(SELECTED,)),
                2.0,
                clock=clock,
                sleep=clock.sleep,
            )
        self.assertGreaterEqual(clock.now, 2.0)

    def test_unsettled_enumeration_times_out(self):
        from ubertooth_harness.enumeration import stable_serials
        clock = FakeClock()
        flip = [frozenset({SELECTED}), frozenset()]

        class Flapping:
            def serials(self):
                flip.reverse()
                return flip[0]

        with self.assertRaisesRegex(EnumerationError, "did not settle"):
            stable_serials(Flapping(), 1.0, 3.0, clock=clock, sleep=clock.sleep)
        for settle, deadline in ((-1, 1), (0, 0)):
            with self.assertRaisesRegex(EnumerationError, "must be positive"):
                stable_serials(Flapping(), settle, deadline, clock=clock, sleep=clock.sleep)

    def test_rejects_contradictory_expectation(self):
        with self.assertRaises(EnumerationError):
            EnumerationExpectation.of(absent=(SELECTED,), present=(SELECTED.upper(),))


class VerifiedIsolationTests(unittest.TestCase):

    def verification(self, enumerator, clock, **kwargs):
        kwargs.setdefault("isolated_serials", {"UT_Linux1": EXTRA})
        return IsolationVerification(
            enumerator=enumerator,
            selected_serial=SELECTED,
            clock=clock,
            sleep=clock.sleep,
            **kwargs,
        )

    def assert_restored(self, controller):
        self.assertTrue(controller.physical_power(3))

    def test_confirms_delayed_removal_and_return(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        # The isolated board lingers on USB for 3 polls after power-off.
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA}, lag_polls=3)
        verification = self.verification(enumerator, clock)

        with applied_power_isolation(ACTIONS, lambda alias: controller, verification):
            self.assertEqual(frozenset({SELECTED}), enumerator.serials())
            # Baseline (1 s) + lingering polls (0.3 s) + settle (1 s).
            self.assertGreaterEqual(clock.now, 2.3)

        self.assert_restored(controller)

    def test_isolated_board_flapping_once_still_passes_after_settle(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})
        real = enumerator.serials
        state = {"switched": False, "flapped": False}

        def flapping():
            seen = real()
            if not controller.physical_power(3) and not state["flapped"]:
                if state["switched"]:
                    state["flapped"] = True
                    state["flap_at"] = clock.now
                    return seen | {EXTRA}  # one spurious reappearance
                state["switched"] = True
            return seen

        enumerator.serials = flapping
        with applied_power_isolation(
            ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
        ):
            body_at = clock.now
        self.assertTrue(state["flapped"])
        # The reappearance restarted the settle window.
        self.assertGreaterEqual(body_at - state["flap_at"], 1.0)

    def test_isolated_board_in_dfu_at_baseline_passes(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(
            controller, {1: SELECTED, 3: "unidentified:3-2.3:6003"}
        )

        with applied_power_isolation(
            ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
        ):
            self.assertEqual(frozenset({SELECTED}), enumerator.serials())

    def test_more_unidentified_devices_than_missing_boards_rejected_before_switching(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(
            controller, {1: SELECTED, 3: EXTRA}, unpowered=("unidentified:3-6:6003",)
        )
        provider = CountingProvider(controller)

        with self.assertRaisesRegex(EnumerationError, "unidentified Ubertooth"):
            with applied_power_isolation(
                ACTIONS, provider, self.verification(enumerator, clock)
            ):
                pass
        self.assertEqual(0, provider.calls)

    def test_board_still_enumerated_fails_before_body_and_restores(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        # Port reads off but the board stays on USB (power not really cut).
        enumerator = PortEnumerator(controller, {1: SELECTED}, unpowered=(EXTRA,))
        ran = []

        with self.assertRaisesRegex(EnumerationError, "still enumerated"):
            with applied_power_isolation(
                ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
            ):
                ran.append(True)

        self.assertEqual([], ran)
        self.assert_restored(controller)
        self.assertGreaterEqual(clock.now, 5.0)  # waited the removal deadline

    def test_unregistered_ubertooth_rejected_before_switching(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        stranger = "deadbeef" * 4
        enumerator = PortEnumerator(
            controller, {1: SELECTED, 3: EXTRA}, unpowered=(stranger,)
        )
        provider = CountingProvider(controller)

        with self.assertRaisesRegex(EnumerationError, f"unregistered Ubertooth.*{stranger}"):
            with applied_power_isolation(
                ACTIONS, provider, self.verification(enumerator, clock)
            ):
                pass
        self.assertEqual(0, provider.calls)

    def test_wrong_registry_serial_rejected_before_switching(self):
        # Registry says EXTRA is on port 3, but a different board is there.
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        actual = "ab" * 16
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: actual})
        provider = CountingProvider(controller)

        with self.assertRaisesRegex(EnumerationError, f"unregistered Ubertooth.*{actual}"):
            with applied_power_isolation(
                ACTIONS, provider, self.verification(enumerator, clock)
            ):
                pass
        self.assertEqual(0, provider.calls)

    def test_unidentified_device_after_power_off_fails_exclusivity(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        # An isolated board missing at baseline allows one unidentified device,
        # but it stays enumerated after power-off (on an unswitched port).
        enumerator = PortEnumerator(
            controller, {1: SELECTED}, unpowered=("unidentified:3-6:6003",)
        )

        with self.assertRaisesRegex(EnumerationError, "unidentified:3-6:6003"):
            with applied_power_isolation(
                ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
            ):
                pass
        self.assert_restored(controller)

    def test_selected_board_missing_fails_before_switching(self):
        controller = simulated({1: False, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})

        provider = CountingProvider(controller)

        with self.assertRaisesRegex(EnumerationError, f"selected Ubertooth {SELECTED}"):
            with applied_power_isolation(
                ACTIONS, provider, self.verification(enumerator, clock)
            ):
                pass
        self.assertEqual(0, provider.calls)

    def test_body_error_wins_and_return_failure_is_noted(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})

        with self.assertRaises(ValueError) as caught:
            with applied_power_isolation(
                ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
            ):
                enumerator.boards = {1: SELECTED}  # board died while isolated
                raise ValueError("test body failed")
        self.assertIn("after restoration", " ".join(caught.exception.__notes__))

    def test_enumerator_crash_after_restore_does_not_hide_body_error(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})
        real = enumerator.serials
        state = {"broken": False}

        def maybe_broken():
            if state["broken"]:
                raise OSError("sysfs unavailable")
            return real()

        enumerator.serials = maybe_broken
        with self.assertRaises(ValueError) as caught:
            with applied_power_isolation(
                ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
            ):
                state["broken"] = True
                raise ValueError("test body failed")
        self.assertIn("sysfs unavailable", " ".join(caught.exception.__notes__))

    def test_interrupt_skips_return_wait(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})

        with self.assertRaises(KeyboardInterrupt):
            with applied_power_isolation(
                ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
            ):
                enumerator.boards = {1: SELECTED}  # board will not return
                body_end = clock.now
                raise KeyboardInterrupt
        self.assertLess(clock.now - body_end, 1.0)
        self.assert_restored(controller)

    def test_isolation_failure_and_return_failure_noted(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})
        real = enumerator.serials
        state = {"returning": False}

        def stuck_then_gone():
            # Port reads off but the board stays (isolation fails); after
            # restoration it never comes back (return fails).
            if controller.physical_power(3):
                return real() if not state["returning"] else real() - {EXTRA}
            state["returning"] = True
            return real() | {EXTRA}

        enumerator.serials = stuck_then_gone
        with self.assertRaisesRegex(EnumerationError, "still enumerated") as caught:
            with applied_power_isolation(
                ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
            ):
                pass
        self.assertIn("after restoration", " ".join(caught.exception.__notes__))

    def test_switching_failure_skips_verification_and_restores(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})
        original = controller.set_power_enabled
        calls = []

        def fail_first_off(port, enabled):
            calls.append((port, enabled))
            if len(calls) == 1:
                raise PowerControllerError("switch failed")
            original(port, enabled)

        controller.set_power_enabled = fail_first_off
        with self.assertRaisesRegex(PowerControllerError, "switch failed"):
            with applied_power_isolation(
                ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
            ):
                self.fail("body must not run")
        self.assert_restored(controller)

    def test_body_error_and_restore_failure_keeps_restore_error(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})

        with self.assertRaisesRegex(PowerControllerError, "restoration failed") as caught:
            with applied_power_isolation(
                ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
            ):
                controller._ignore_writes = True
                body_end = clock.now
                raise ValueError("test body failed")
        self.assertIsInstance(caught.exception.__context__, ValueError)
        self.assertLess(clock.now - body_end, 1.0)

    def test_restore_failure_wins_and_skips_return_wait(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})
        verification = self.verification(enumerator, clock)

        with self.assertRaisesRegex(PowerControllerError, "restoration failed"):
            with applied_power_isolation(ACTIONS, lambda alias: controller, verification):
                controller._ignore_writes = True  # restore writes now fail readback
                body_end = clock.now
        # No 10 s return wait was spent once restoration had failed.
        self.assertLess(clock.now - body_end, 1.0)

    def test_board_not_returning_after_restore_fails_even_if_body_succeeded(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})

        with self.assertRaisesRegex(EnumerationError, f"not enumerated: {EXTRA}"):
            with applied_power_isolation(
                ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
            ):
                enumerator.boards = {1: SELECTED}  # board died while isolated

    def test_board_absent_beforehand_is_not_expected_back(self):
        controller = simulated({1: True, 3: False})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})

        with applied_power_isolation(
            ACTIONS, lambda alias: controller, self.verification(enumerator, clock)
        ):
            pass

    def test_isolated_serials_must_match_actions(self):
        controller = simulated({1: True, 3: True})
        clock = FakeClock()
        enumerator = PortEnumerator(controller, {1: SELECTED, 3: EXTRA})
        provider = CountingProvider(controller)
        on_action = (PowerIsolationAction("UT_Linux1", "Hub_Linux0", 3, True),)
        duplicate = ACTIONS + ACTIONS
        for actions, message in (
            (on_action, "power-off actions"),
            (duplicate, "duplicate isolation action"),
        ):
            with self.assertRaisesRegex(EnumerationError, message):
                with applied_power_isolation(
                    actions, provider, self.verification(enumerator, clock)
                ):
                    pass
        for serials in ({}, {"UT_Linux1": EXTRA, "UT_Other": "ab" * 16}):
            verification = self.verification(
                enumerator, clock, isolated_serials=serials
            )
            with self.assertRaisesRegex(EnumerationError, "match the isolation actions"):
                with applied_power_isolation(ACTIONS, provider, verification):
                    pass
        self.assertEqual(0, provider.calls)

    def test_invalid_verification_parameters_rejected(self):
        clock = FakeClock()
        bad = (
            {"isolated_serials": {"UT_Linux1": SELECTED}},
            {"isolated_serials": {"A": EXTRA, "B": EXTRA.upper()}},
            {"removal_deadline_s": 0},
            {"settle_s": -1},
            {"settle_s": 5.0},  # equals the removal deadline: can never pass
            {"settle_s": 10.0, "removal_deadline_s": 20},  # equals return deadline
            {"isolated_serials": {"UT_Linux1": 42}},
            {"isolated_serials": {"UT_Linux1": "unidentified:x"}},
            {"isolated_serials": [EXTRA]},
        )
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(EnumerationError):
                    self.verification(object(), clock, **kwargs)


if __name__ == "__main__":
    unittest.main()
