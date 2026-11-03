"""A debug programmer and the device it programs, held as one pair (#286).

A programmer wired to a board's debug or programming header is not a device
of its own as far as using it goes: programming the board while another
command owns it, or using the board while it is being programmed, corrupts a
test or leaves the board needing hands on it. So the registry records the
pair, and every command that takes one member takes the other.

Nothing here touches a device: the registries are temporary files and the
locks are files in a temporary directory.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from ubertooth_harness import cli, leases
from ubertooth_harness.cli import main
from ubertooth_harness.report import _Run, _setup_rows, render_run
from ubertooth_harness.resources import (
    DEBUG_PROGRAMMER,
    FileResourceRegistry,
    RegistryError,
    Resource,
)
from ubertooth_harness.topology import PowerIsolationAction

PLATFORM = "macos" if sys.platform == "darwin" else "linux"
# Invented serials: no lab device is named here.
FREE = "aaaa0000aaaa0000aaaa0000aaaa0000"
TARGET = "99887766554433221100aabbccddeeff"
MAC = "cccc2222cccc2222cccc2222cccc2222"
PROGRAMMER_SERIAL = "A000AA00"
TARGET_KEY = TARGET
PROGRAMMER_KEY = f"programmer-{PROGRAMMER_SERIAL}"

BENCH = textwrap.dedent(
    f"""
    resources:
      Host_Linux:
        type: Host
        platform: {PLATFORM}
        roles: [controller, test target]
        defaults:
          Ubertooth: UT_Free
      Host_Other:
        type: Host
        platform: macos
        roles: [test target]
      UT_Free:
        type: Ubertooth
        attached to: Host_Linux
        identity:
          serial: {FREE}
      UT_Debug:
        type: Ubertooth
        attached to: Host_Linux
        identity:
          serial: {TARGET}
        power:
          controller: Hub0
          port: 2
      UT_Other:
        type: Ubertooth
        attached to: Host_Other
        identity:
          serial: {MAC}
      Hub0:
        type: USB power controller
        attached to: Host_Linux
        identity:
          serial: "0x1234ABCD"
        driver: acroname BrainStem USBHub3+
        capabilities:
          - per-port VBUS switching
          - per-port data switching
          - power readback
          - VBUS voltage measurement
          - VBUS current measurement
    """
).lstrip()

# The pair itself, appended to the bench above, at the indentation the
# resources mapping needs. Tests change these lines to describe a pairing no
# bench could have.
PROGRAMMER = (
    "  Prog0:\n"
    f"    type: {DEBUG_PROGRAMMER}\n"
    "    attached to: Host_Linux\n"
    "    programs: UT_Debug\n"
    "    identity:\n"
    f'      serial: "{PROGRAMMER_SERIAL}"\n'
)


class PairTestCase(unittest.TestCase):
    """A temporary registry and lease directory."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.leases = self.root / "leases"
        self.leases.mkdir()

    def write(self, *blocks: str) -> Path:
        """A registry file holding the bench and these extra resources."""
        path = self.root / "hardware.yaml"
        path.write_text(BENCH + "".join(blocks), encoding="utf-8")
        return path

    def load(self, *blocks: str) -> FileResourceRegistry:
        return FileResourceRegistry.load(self.write(*blocks))

    def refuse(self, block: str, fragment: str) -> str:
        """Load a registry that must be refused, and return the reason."""
        with self.assertRaises(RegistryError) as caught:
            self.load(block)
        self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    def invocation(self, **fields):
        """One test of a run, as the planner hands it to the leases."""
        values = dict(
            test_name="Radio setting transitions", host="Host_Linux",
            resources={"Ubertooth": "UT_Free"}, power_isolation=(),
            exclusive_ubertooth=False,
        )
        values.update(fields)
        return SimpleNamespace(**values)


class PairRegistry(PairTestCase):
    """What a registry may and may not say about a wired pair."""

    def test_a_programmer_and_its_target_load(self):
        registry = self.load(PROGRAMMER)
        programmer = registry.get("Prog0")
        self.assertEqual(DEBUG_PROGRAMMER, programmer.type)
        self.assertEqual("UT_Debug", programmer.programs)
        self.assertEqual(PROGRAMMER_SERIAL, programmer.identity["serial"])

    def test_a_stable_serial_device_path_may_be_recorded(self):
        registry = self.load(
            PROGRAMMER
            + "      serial device: /dev/serial/by-id/usb-Vendor_Adapter-if00-port0\n"
        )
        self.assertIn("serial device", registry.get("Prog0").identity)

    def test_a_programmer_wired_to_nothing_is_refused(self):
        self.refuse(
            PROGRAMMER.replace("    programs: UT_Debug\n", ""),
            "requires 'programs: <alias>'",
        )

    def test_a_programmer_cannot_program_itself(self):
        self.refuse(
            PROGRAMMER.replace("programs: UT_Debug", "programs: Prog0"),
            "cannot program itself",
        )

    def test_a_target_that_is_not_registered_is_refused(self):
        self.refuse(
            PROGRAMMER.replace("programs: UT_Debug", "programs: UT_Nope"),
            "programs unknown device 'UT_Nope'",
        )

    def test_a_target_the_harness_cannot_lease_is_refused(self):
        """There would be no lock to take for it, so every command naming
        either member would fail, naming a resource nobody asked for."""
        for target, what in (("Host_Linux", "Host"),
                             ("Hub0", "USB power controller")):
            with self.subTest(target=target):
                reason = self.refuse(
                    PROGRAMMER.replace("programs: UT_Debug", f"programs: {target}"),
                    f"which is a {what}",
                )
                self.assertIn("a pair is two devices the harness leases", reason)

    def test_every_leasable_type_may_be_a_target(self):
        """The refusal above is about the lock, not about the board.

        Each type the harness leases is wired to in turn, so the membership
        check cannot be passing only for the type the bench happens to use.
        """
        self.assertEqual(
            ("Ubertooth", "nRF52840 DK", "ESP32 Classic peer", DEBUG_PROGRAMMER),
            leases.LEASED_TYPES,
        )
        extra = (
            "  DK_Pair:\n"
            "    type: nRF52840 DK\n"
            "    attached to: Host_Linux\n"
            "    roles: [advertiser]\n"
            "    identity:\n"
            '      jlink serial: "000123456789"\n'
            '      BLE address: "C0:00:00:00:00:01"\n'
            "  ESP_Pair:\n"
            "    type: ESP32 Classic peer\n"
            "    attached to: Host_Linux\n"
            "    roles: [acceptor, initiator]\n"
            "    identity:\n"
            "      bridge serial: a1b2c3d400000000a1b2c3d400000000\n"
            '      BD_ADDR: "C8:00:00:00:00:01"\n'
        )
        for target in ("UT_Debug", "UT_Free", "DK_Pair", "ESP_Pair", "Prog1"):
            with self.subTest(target=target):
                second = (
                    PROGRAMMER.replace("Prog0", "Prog1")
                    .replace("programs: UT_Debug", "programs: UT_Free")
                    .replace(PROGRAMMER_SERIAL, "B111BB11")
                )
                registry = self.load(
                    PROGRAMMER.replace("programs: UT_Debug", f"programs: {target}")
                    + (second if target != "UT_Free" else "")
                    + extra
                )
                self.assertIn("Prog0", registry.pair_members(target))

    def test_a_programmer_may_not_take_a_name_that_is_already_something_s(self):
        """The bench's own lock file is named after a device's plain serial,
        so that name is in one space with every other device's serial and
        with the files the lease directory keeps for itself."""
        others = (
            "  DK_Pair:\n"
            "    type: nRF52840 DK\n"
            "    attached to: Host_Linux\n"
            "    roles: [advertiser]\n"
            "    identity:\n"
            '      jlink serial: "000123456789"\n'
            '      BLE address: "C0:00:00:00:00:01"\n'
            "  ESP_Pair:\n"
            "    type: ESP32 Classic peer\n"
            "    attached to: Host_Linux\n"
            "    roles: [acceptor, initiator]\n"
            "    identity:\n"
            "      bridge serial: a1b2c3d400000000a1b2c3d400000000\n"
            '      BD_ADDR: "C8:00:00:00:00:01"\n'
        )
        for serial, fragment in (
            (TARGET, "UT_Debug (Ubertooth)"),
            ("000123456789", "DK_Pair (nRF52840 DK)"),
            # nrfutil prints a J-Link serial without its leading zeros, and
            # a bench script may name the lock either way.
            ("123456789", "DK_Pair (nRF52840 DK)"),
            ("a1b2c3d400000000a1b2c3d400000000", "ESP_Pair (ESP32 Classic peer)"),
            ("host-power", "this host's power/firmware lock"),
            ("takeover", "the lease directory's own takeover lock"),
        ):
            with self.subTest(serial=serial):
                self.refuse(
                    PROGRAMMER.replace(f'"{PROGRAMMER_SERIAL}"', f'"{serial}"')
                    + others,
                    fragment,
                )

    def test_a_serial_nothing_else_uses_is_accepted(self):
        """The refusal above must not be refusing every serial."""
        registry = self.load(
            PROGRAMMER.replace(f'"{PROGRAMMER_SERIAL}"', '"host-power-adapter"')
        )
        self.assertEqual(
            "programmer-host-power-adapter",
            leases.resource_key(registry.get("Prog0")),
        )

    def test_a_programs_line_with_nothing_after_it_is_refused(self):
        """Present and empty is a pairing left half written, not no pairing.

        On any other resource type it is the type that is refused, which is
        the mistake there: nothing but a programmer is wired to a device.
        """
        self.refuse(
            PROGRAMMER.replace("programs: UT_Debug", "programs:"),
            "must name the one device it programs by alias",
        )
        self.refuse(
            PROGRAMMER
            + "  UT_Null:\n"
              "    type: Ubertooth\n"
              "    attached to: Host_Linux\n"
              "    programs:\n"
              "    identity:\n"
              "      serial: bbbb1111bbbb1111bbbb1111bbbb1111\n",
            "only a Debug programmer is wired to one",
        )

    def test_a_pair_must_be_on_one_host(self):
        """Leases are host-local, so a pair across two hosts holds nothing."""
        self.refuse(
            PROGRAMMER.replace("programs: UT_Debug", "programs: UT_Other"),
            "are not attached to one host",
        )

    def test_two_programmers_cannot_share_one_serial(self):
        """One serial is one adapter, and the lease key is the serial alone.

        The identity check compares whole identity mappings, so an optional
        `serial device` on one of them would otherwise let two resources
        name one device, each holding it for a target of its own.
        """
        for second_serial, label in (
            (PROGRAMMER_SERIAL, "the same"),
            (PROGRAMMER_SERIAL.lower(), "the same in another case"),
        ):
            with self.subTest(label=label):
                second = (
                    PROGRAMMER.replace("Prog0", "Prog1")
                    .replace("programs: UT_Debug", "programs: UT_Free")
                    .replace(PROGRAMMER_SERIAL, second_serial)
                    + "      serial device: /dev/serial/by-id/usb-Vendor_A-if00-port0\n"
                )
                self.refuse(PROGRAMMER + second, "the same serial")

    def test_one_target_has_one_programmer(self):
        second = PROGRAMMER.replace("Prog0", "Prog1").replace(
            PROGRAMMER_SERIAL, "B111BB11"
        )
        self.refuse(PROGRAMMER + second, "both program UT_Debug")

    def test_more_than_one_target_is_refused(self):
        self.refuse(
            PROGRAMMER.replace("programs: UT_Debug", "programs: [UT_Debug, UT_Free]"),
            "must name the one device it programs by alias",
        )

    def test_only_a_programmer_names_the_device_it_programs(self):
        """An Ubertooth with `programs:` is a registry mistake, not a pair."""
        self.refuse(
            PROGRAMMER
            + "  UT_Confused:\n"
              "    type: Ubertooth\n"
              "    attached to: Host_Linux\n"
              "    programs: UT_Free\n"
              "    identity:\n"
              "      serial: bbbb1111bbbb1111bbbb1111bbbb1111\n",
            "only a Debug programmer is wired to one",
        )

    def test_a_ring_of_programmers_is_refused(self):
        """Each would hold the other, for a bench that has no such wiring."""
        first = PROGRAMMER.replace("programs: UT_Debug", "programs: Prog1")
        second = (
            PROGRAMMER.replace("Prog0", "Prog1")
            .replace("programs: UT_Debug", "programs: Prog0")
            .replace(PROGRAMMER_SERIAL, "B111BB11")
        )
        self.refuse(first + second, "loops back on itself")

    def test_a_longer_ring_is_refused_too(self):
        def programmer(name, serial, target):
            return (
                PROGRAMMER.replace("Prog0", name)
                .replace("programs: UT_Debug", f"programs: {target}")
                .replace(PROGRAMMER_SERIAL, serial)
            )

        self.refuse(
            programmer("Prog0", "A000AA00", "Prog1")
            + programmer("Prog1", "B111BB11", "Prog2")
            + programmer("Prog2", "C222CC22", "Prog0"),
            "loops back on itself",
        )

    def test_a_programmer_needs_a_serial_of_its_own(self):
        self.refuse(
            PROGRAMMER.replace(f'serial: "{PROGRAMMER_SERIAL}"', 'serial: "two words"'),
            "requires a 'serial' identity",
        )

    def test_an_unknown_identity_key_is_refused(self):
        self.refuse(
            PROGRAMMER + '      BD_ADDR: "C0:00:00:00:00:01"\n',
            "identity has unknown key(s) BD_ADDR",
        )

    def test_an_unstable_device_path_is_refused(self):
        self.refuse(
            PROGRAMMER + "      serial device: /dev/ttyUSB0\n",
            "/dev/serial/by-id/ path",
        )

    def test_a_programmer_has_no_rf_placement(self):
        """It carries no radio, so no test can select it by where it sits."""
        self.refuse(
            PROGRAMMER + "    rf:\n      antenna: true\n",
            "cannot have an rf placement",
        )


class PairGraph(PairTestCase):
    """Which devices come with which, and how that is explained."""

    def test_either_member_names_the_other(self):
        registry = self.load(PROGRAMMER)
        self.assertEqual(("Prog0",), registry.pair_members("UT_Debug"))
        self.assertEqual(("UT_Debug",), registry.pair_members("Prog0"))

    def test_a_device_in_no_pair_comes_alone(self):
        registry = self.load(PROGRAMMER)
        self.assertEqual((), registry.pair_members("UT_Free"))
        self.assertEqual((), registry.pair_members("Hub0"))

    def test_a_chain_of_pairs_is_one_set(self):
        """A programmer whose own header is wired to another programmer."""
        second = (
            PROGRAMMER.replace("Prog0", "Prog1")
            .replace("programs: UT_Debug", "programs: Prog0")
            .replace(PROGRAMMER_SERIAL, "B111BB11")
        )
        registry = self.load(PROGRAMMER + second)
        self.assertEqual(("Prog0", "Prog1"), registry.pair_members("UT_Debug"))
        self.assertEqual(("Prog0", "UT_Debug"), registry.pair_members("Prog1"))

    def test_a_pairing_loop_cannot_make_the_walk_spin(self):
        """Validation refuses a loop; a registry built in code need not have
        passed it, and expansion must still answer."""
        resources = {}
        for alias, other in (("Prog0", "Prog1"), ("Prog1", "Prog0")):
            resources[alias] = Resource(
                alias=alias, type=DEBUG_PROGRAMMER, platform=None, roles=(),
                defaults=MappingProxyType({}), attached_to="Host_Linux",
                identity=MappingProxyType({"serial": alias}), capabilities=(),
                power=MappingProxyType({}), driver=None, programs=other,
            )
        looped = FileResourceRegistry.__new__(FileResourceRegistry)
        looped.resources = MappingProxyType(resources)
        self.assertEqual(("Prog1",), looped.pair_members("Prog0"))
        self.assertEqual(
            (("Prog0", "Prog1"), ("Prog1 is the programmer wired to Prog0",)),
            looped.expand_pairs(["Prog0"]),
        )

    def test_expansion_adds_the_other_member_and_says_why(self):
        registry = self.load(PROGRAMMER)
        aliases, why = registry.expand_pairs(["UT_Debug"])
        self.assertEqual(("UT_Debug", "Prog0"), aliases)
        self.assertEqual(("Prog0 is the programmer wired to UT_Debug",), why)
        aliases, why = registry.expand_pairs(["Prog0"])
        self.assertEqual(("Prog0", "UT_Debug"), aliases)
        self.assertEqual(("UT_Debug is the device Prog0 programs",), why)

    def test_naming_both_members_is_harmless(self):
        """Each device once, and nothing to explain: both were asked for."""
        registry = self.load(PROGRAMMER)
        for named in (["UT_Debug", "Prog0"], ["Prog0", "UT_Debug"]):
            with self.subTest(named=named):
                aliases, why = registry.expand_pairs(named)
                self.assertEqual(tuple(named), aliases)
                self.assertEqual((), why)

    def test_naming_one_member_twice_holds_it_once(self):
        registry = self.load(PROGRAMMER)
        aliases, why = registry.expand_pairs(["Prog0", "Prog0"])
        self.assertEqual(("Prog0", "UT_Debug"), aliases)
        self.assertEqual(("UT_Debug is the device Prog0 programs",), why)

    def test_the_expansion_is_deterministic(self):
        """Two commands asking for the same devices ask for the same set."""
        registry = self.load(PROGRAMMER)
        first, _ = registry.expand_pairs(["UT_Free", "UT_Debug"])
        second, _ = registry.expand_pairs(["UT_Free", "UT_Debug"])
        self.assertEqual(("UT_Free", "UT_Debug", "Prog0"), first)
        self.assertEqual(first, second)

    def test_a_registry_that_records_no_pair_changes_nothing(self):
        registry = self.load()
        self.assertEqual((("UT_Debug",), ()), registry.expand_pairs(["UT_Debug"]))

    def test_a_registry_with_no_pairing_at_all_is_accepted(self):
        """leases.expand_pairs is given registries from elsewhere too."""
        self.assertEqual(
            (("UT_Debug",), ()), leases.expand_pairs(object(), ["UT_Debug"])
        )
        self.assertEqual((), leases.pair_members(object(), "UT_Debug"))


class PairLocks(PairTestCase):
    """The locks a pair takes, and what another command finds."""

    def boards(self, registry, named):
        aliases, _why = leases.expand_pairs(registry, named)
        return [
            leases.Board(alias, leases.resource_key(registry.get(alias)))
            for alias in aliases
        ]

    def test_the_programmer_has_a_lease_key_of_its_own(self):
        registry = self.load(PROGRAMMER)
        self.assertEqual(PROGRAMMER_KEY, leases.resource_key(registry.get("Prog0")))
        self.assertIn(DEBUG_PROGRAMMER, leases.LEASED_TYPES)

    def test_a_serial_no_lock_can_be_named_after_is_refused(self):
        registry = FileResourceRegistry.__new__(FileResourceRegistry)
        registry.resources = MappingProxyType({
            "Prog0": Resource(
                alias="Prog0", type=DEBUG_PROGRAMMER, platform=None, roles=(),
                defaults=MappingProxyType({}), attached_to="Host_Linux",
                identity=MappingProxyType({"serial": "../escape"}), capabilities=(),
                power=MappingProxyType({}), driver=None, programs="UT_Debug",
            )
        })
        with self.assertRaises(leases.LeaseError) as caught:
            leases.resource_key(registry.get("Prog0"))
        self.assertIn("needs a 'serial'", str(caught.exception))

    def test_holding_either_member_locks_both(self):
        registry = self.load(PROGRAMMER)
        for named in (["UT_Debug"], ["Prog0"]):
            with self.subTest(named=named):
                held = leases.acquire(
                    self.boards(registry, named),
                    leases.holder_info(run_id=None, owner="agent", purpose="test"),
                    directory=self.leases,
                )
                try:
                    self.assertEqual(
                        sorted([PROGRAMMER_KEY, TARGET_KEY]),
                        sorted(lease.board.key for lease in held.leases),
                    )
                finally:
                    held.release()

    def test_the_locks_are_taken_in_the_harness_order(self):
        registry = self.load(PROGRAMMER)
        # Named the programmer, whose key sorts after the board's, so the
        # expansion order and the lock order really differ here.
        ordered = self.boards(registry, ["Prog0"])
        self.assertEqual([PROGRAMMER_KEY, TARGET_KEY], [b.key for b in ordered])
        held = leases.acquire(
            ordered,
            leases.holder_info(run_id=None, owner="agent", purpose="test"),
            directory=self.leases, host_lock=True,
        )
        self.addCleanup(held.release)
        keys = [lease.board.key for lease in held.leases]
        # The host lock first, then the pair's leases in key order, which is
        # the harness's own order and not the order the expansion produced:
        # naming the programmer puts its key first in the expansion and
        # second here.
        self.assertEqual(
            [leases.HOST_LOCK.key] + sorted([PROGRAMMER_KEY, TARGET_KEY]), keys
        )

    def test_a_held_programmer_refuses_a_command_that_named_the_target(self):
        registry = self.load(PROGRAMMER)
        first = leases.acquire(
            self.boards(registry, ["Prog0"]),
            leases.holder_info(run_id=None, owner="bob", purpose="programming it"),
            directory=self.leases,
        )
        self.addCleanup(first.release)
        with self.assertRaises(leases.LeaseError) as caught:
            leases.acquire(
                self.boards(registry, ["UT_Debug"]),
                leases.holder_info(run_id=None, owner="alice", purpose="a capture"),
                directory=self.leases,
            )
        message = str(caught.exception)
        self.assertIn("bob", message)
        self.assertIn("programming it", message)

    def test_a_held_target_refuses_a_command_that_named_the_programmer(self):
        registry = self.load(PROGRAMMER)
        first = leases.acquire(
            self.boards(registry, ["UT_Debug"]),
            leases.holder_info(run_id=None, owner="bob", purpose="a capture"),
            directory=self.leases,
        )
        self.addCleanup(first.release)
        with self.assertRaises(leases.LeaseError) as caught:
            leases.acquire(
                self.boards(registry, ["Prog0"]),
                leases.holder_info(run_id=None, owner="alice", purpose="programming it"),
                directory=self.leases,
            )
        self.assertIn("bob", str(caught.exception))

    def test_a_failed_attempt_leaves_neither_member_held(self):
        """All or nothing: the pair is taken in one attempt or not at all."""
        registry = self.load(PROGRAMMER)
        first = leases.acquire(
            self.boards(registry, ["Prog0"]),
            leases.holder_info(run_id=None, owner="bob", purpose="programming it"),
            directory=self.leases,
        )
        with self.assertRaises(leases.LeaseError):
            leases.acquire(
                self.boards(registry, ["UT_Debug"]),
                leases.holder_info(run_id=None, owner="alice", purpose="a capture"),
                directory=self.leases,
            )
        first.release()
        again = leases.acquire(
            self.boards(registry, ["UT_Debug"]),
            leases.holder_info(run_id=None, owner="alice", purpose="a capture"),
            directory=self.leases,
        )
        again.release()

    def dead_holder(self) -> dict:
        """A holder record of a session that is gone, as #215's lease was."""
        child = subprocess.Popen([sys.executable, "-c", ""])
        child.wait()  # reaped: nothing holds this pid now
        return leases.holder_info(
            run_id="r", owner="gone-agent", purpose="programming it",
        ) | {"pid": child.pid, "pgid": child.pid}

    def hold_raw(self, key: str, record: dict) -> Path:
        """Lock a lease file here, with the holder record this test wants.

        flock belongs to an open file rather than to a process, so a second
        descriptor on the same file conflicts with this one: the lease is
        really held, and says whatever the test needs about who took it.
        """
        path = self.leases / f"{key}.lock"
        handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, handle)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.pwrite(handle, (json.dumps(record) + "\n").encode(), 0)
        return path

    def test_a_pair_left_by_a_dead_process_is_taken_over(self):
        """Both members, so a killed session cannot keep a pair for good."""
        registry = self.load(PROGRAMMER)
        record = self.dead_holder()
        paths = {key: self.hold_raw(key, record) for key in (TARGET_KEY, PROGRAMMER_KEY)}
        notes: list[str] = []
        taken = leases.acquire(
            self.boards(registry, ["Prog0"]),
            leases.holder_info(run_id=None, owner="alice", purpose="a capture"),
            directory=self.leases, note=notes.append,
        )
        self.addCleanup(taken.release)
        self.assertEqual(2, len(taken.leases))
        self.assertEqual(
            {"Prog0", "UT_Debug"},
            {item["resource"] for item in taken.takeovers},
        )
        for key, path in paths.items():
            with self.subTest(key=key):
                self.assertEqual("alice", leases.read_holder(path)["owner"])
        self.assertEqual(2, len(notes), notes)
        self.assertTrue(all("no longer holds it" in note for note in notes), notes)


class PairRuns(PairTestCase):
    """A run holds the programmer of every device it uses or power-cycles."""

    def test_a_run_that_uses_the_target_holds_the_programmer(self):
        registry = self.load(PROGRAMMER)
        boards = leases.boards_for_invocations(
            [self.invocation(resources={"Ubertooth": "UT_Debug"})], registry
        )
        self.assertEqual(
            sorted([("Prog0", PROGRAMMER_KEY), ("UT_Debug", TARGET_KEY)],
                   key=lambda item: item[1]),
            [(board.alias, board.key) for board in boards],
        )

    def test_a_run_that_cuts_the_target_s_power_holds_the_programmer(self):
        registry = self.load(PROGRAMMER)
        boards = leases.boards_for_invocations(
            [self.invocation(
                power_isolation=(PowerIsolationAction("UT_Debug", "Hub0", 2),)
            )],
            registry,
        )
        self.assertIn(PROGRAMMER_KEY, [board.key for board in boards])

    def test_a_run_that_touches_neither_holds_no_programmer(self):
        registry = self.load(PROGRAMMER)
        boards = leases.boards_for_invocations([self.invocation()], registry)
        self.assertEqual([FREE], [board.key for board in boards])


class PairRunRecords(PairTestCase):
    """A run says why it held a device it never named (#289).

    The leases list a debug programmer like any other device, so without
    this a reader of the report sees a programmer leased by a run that
    asked for a board. The by-hand commands say it on standard error; a run
    carries it in its record, its report, its transcript and the live view.
    """

    def using_the_target(self):
        return [self.invocation(resources={"Ubertooth": "UT_Debug"})]

    def test_the_record_names_the_device_the_run_asked_for_and_why(self):
        registry = self.load(PROGRAMMER)
        pairings = leases.pairings_for_invocations(self.using_the_target(), registry)
        self.assertEqual(
            [{"resource": "Prog0",
              "reason": "Prog0 is the programmer wired to UT_Debug"}],
            [dict(record) for record in pairings],
        )
        # The registry's own wording, so a run says what `hold` and `boards`
        # say about the same pair, and the clause names both devices, so no
        # second field of the record can name another one.
        self.assertEqual(
            pairings[0]["reason"], registry.describe_pairing("Prog0", "UT_Debug")
        )

    def test_a_chain_of_pairs_is_recorded_in_the_registry_s_own_words(self):
        """A programmer wired to another programmer's header: one set.

        The run names the board at one end and holds every member, so each
        record has to say how that member comes to be there.
        """
        chain = (
            PROGRAMMER.replace("Prog0", "Prog1")
            .replace("programs: UT_Debug", "programs: Prog0")
            .replace(PROGRAMMER_SERIAL, "B111BB11")
        )
        registry = self.load(PROGRAMMER + chain)
        records = leases.pairings_for_invocations(self.using_the_target(), registry)
        self.assertEqual(
            [("Prog0", "Prog0 is the programmer wired to UT_Debug"),
             ("Prog1", "Prog1 is wired to UT_Debug through a programmer pair")],
            [(record["resource"], record["reason"]) for record in records],
        )
        self.assertEqual(
            {"Prog0", "Prog1", "UT_Debug"},
            {board.alias for board
             in leases.boards_for_invocations(self.using_the_target(), registry)},
        )

    def test_a_run_that_named_both_members_added_nothing(self):
        """Both are the run's own devices, so neither needs explaining."""
        registry = self.load(PROGRAMMER)
        invocations = [
            self.invocation(resources={"Ubertooth": "UT_Debug"}),
            self.invocation(
                resources={"Ubertooth": "UT_Free"},
                power_isolation=(PowerIsolationAction("Prog0", "Hub0", 2),),
            ),
        ]
        self.assertIn(
            PROGRAMMER_KEY,
            [board.key for board in leases.boards_for_invocations(invocations, registry)],
        )
        self.assertEqual((), leases.pairings_for_invocations(invocations, registry))

    def test_a_run_that_touches_neither_member_records_nothing(self):
        registry = self.load(PROGRAMMER)
        self.assertEqual(
            (), leases.pairings_for_invocations([self.invocation()], registry)
        )

    def test_a_registry_with_no_pairing_records_nothing(self):
        registry = self.load()
        self.assertEqual(
            (), leases.pairings_for_invocations(self.using_the_target(), registry)
        )

    def test_the_run_record_the_report_and_the_live_view_carry_it(self):
        registry = self.load(PROGRAMMER)
        pairings = leases.pairings_for_invocations(self.using_the_target(), registry)
        events = []

        class Publisher:
            def emit(self, kind, status, details, **_kwargs):
                events.append((kind, status, details))

        cli._record_lease_pairings(Publisher(), pairings)
        self.assertEqual(1, len(events))
        kind, status, details = events[0]
        self.assertEqual(("lease_pairing", "passed"), (kind, status))
        self.assertEqual("Prog0", details["resource"])
        self.assertEqual(
            "Prog0 is the programmer wired to UT_Debug, so the run held it too",
            details["message"],
        )
        # The report's setup table, beside the leases themselves.
        rows = dict(_setup_rows(_Run([{
            "run_id": "r", "event_type": "manifest",
            "timestamp": "2026-01-01T00:00:00.000",
            "details": {"manifest": {"lease_pairings": [dict(pairings[0])]}},
        }])))
        self.assertEqual(
            "Prog0 is the programmer wired to UT_Debug, so the run held it too",
            rows["Leases from a wired pair"],
        )
        # A run on a bench with no pairing says nothing, and so does a
        # record written before any of this existed.
        self.assertNotIn("Leases from a wired pair", dict(_setup_rows(_Run([{
            "run_id": "r", "event_type": "manifest",
            "timestamp": "2026-01-01T00:00:00.000", "details": {"manifest": {}},
        }]))))
        transcript = render_run([{
            "schema_version": 1, "run_id": "r", "sequence": 1,
            "timestamp": "2026-01-01T00:00:00.000", "event_type": "lease_pairing",
            "status": "passed", "details": details,
        }])
        self.assertIn(
            "[M] Prog0 is the programmer wired to UT_Debug, so the run held it too",
            transcript,
        )
        # And the browser's live view shows the same line, in a row of its own.
        web = Path(__file__).resolve().parents[1] / "ubertooth_harness" / "web"
        js = (web / "app.js").read_text(encoding="utf-8")
        self.assertIn('kind === "lease_pairing"', js)
        self.assertIn('$("live-pairings").hidden = live.pairings.length === 0;', js)
        self.assertIn(
            "Leases from a wired pair",
            (web / "index.html").read_text(encoding="utf-8"),
        )

    def test_a_manifest_carries_the_pairing_the_report_reads(self):
        """The run record's own key, and the path a run without --build takes.

        Such a run has no publisher until the sequencer starts, which emits
        the records from the manifest; a report then reads them back out of
        it. So the field has to survive being frozen into a manifest and
        read as the report's row names it.
        """
        from ubertooth_harness.manifest import (
            SourceRevision,
            build_run_manifest,
            json_value,
        )
        from ubertooth_harness.planner import TestInvocation
        from ubertooth_harness.report import _pairing_rows

        registry = self.load(PROGRAMMER)
        record = leases.pairings_for_invocations(self.using_the_target(), registry)[0]
        plan = self.root / "plan.yaml"
        plan.write_text("plan\n", encoding="utf-8")
        manifest = build_run_manifest(
            run_id="paired-run", plan_name="named", suite="fast", host="Host_Linux",
            plan_path=plan, hardware_path=self.write(PROGRAMMER),
            sources=(SourceRevision("Ubertooth", "/source", "https://host.invalid/u.git",
                                    "ref", "a" * 40, "test"),),
            invocations=(TestInvocation(
                "invocation-001", "Host CLI surface", "Host_Linux", {}, {}, {}),),
            lease_pairings=(record,),
        )
        carried = manifest.to_dict()["lease_pairings"]
        self.assertEqual(["Prog0"], [item["resource"] for item in carried])
        # What the sequencer emits for such a run, from the frozen record.
        self.assertEqual(
            "Prog0 is the programmer wired to UT_Debug, so the run held it too",
            leases.describe_pairing_lease(json_value(record)),
        )
        rows = dict(_pairing_rows(manifest.to_dict()))
        self.assertIn("Prog0 is the programmer wired to UT_Debug",
                      rows["Leases from a wired pair"])

    def test_a_real_run_says_it_on_standard_error_and_records_it(self):
        """The whole path, from the registry to run.jsonl.

        `Flash reliability` binds the host's default Ubertooth, which this
        registry wires a programmer to, so the run holds the programmer. The
        note is printed before the locks are taken, as `hold` prints it, and
        the record is written before the build, which is where a run of this
        kind may fail.
        """
        from tests.test_cli import make_source
        from ubertooth_harness.manifest import ManifestError

        source, _short = make_source(self.root)
        kgv = self.root / "kgv.dfu"
        kgv.write_bytes(b"kgv")
        image = self.root / "test.dfu"
        image.write_bytes(b"test")
        plan = self.root / "paired.yaml"
        plan.write_text(textwrap.dedent(f"""\
            plan: paired run
            suites:
              slow:
                - Flash reliability:
                    hosts: each
                    platforms: [{PLATFORM}]
                    Ubertooth: default
                    flashes: 2
                    image sequence: [test build, KGV]
                    KGV sha256: {hashlib.sha256(b"kgv").hexdigest()}
                    KGV revision: 2020-12-R1
            """), encoding="utf-8")
        output = self.root / "runs"
        hardware = self.write(PROGRAMMER.replace("programs: UT_Debug", "programs: UT_Free"))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_prepare_run",
                               side_effect=ManifestError("artifact vanished")), \
                redirect_stdout(out), redirect_stderr(err):
            code = main([
                "run-suite", "--plan", str(plan), "--suite", "slow",
                "--hardware", str(hardware), "--host", "Host_Linux",
                "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
                "--libbtbb-source", str(source), "--libbtbb-ref", "HEAD",
                "--artifact", f"test build={image}", "--artifact", f"KGV={kgv}",
                "--run-id", "paired", "--output", str(output),
                "--lease-dir", str(self.leases),
            ])
        self.assertEqual(2, code)
        self.assertIn(
            "note: Prog0 is the programmer wired to UT_Free, so it is held too",
            err.getvalue(),
        )
        events = [
            json.loads(line) for line
            in (output / "paired" / "run.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        kinds = [event["event_type"] for event in events]
        self.assertNotIn("manifest", kinds)  # the run never got that far
        self.assertEqual(1, kinds.count("lease_pairing"), kinds)
        details = events[kinds.index("lease_pairing")]["details"]
        self.assertEqual("Prog0", details["resource"])
        self.assertEqual(
            "Prog0 is the programmer wired to UT_Free, so the run held it too",
            details["message"],
        )

    def test_a_record_with_no_clause_in_it_still_reads_as_a_sentence(self):
        """A record from elsewhere, or an older one, is only being shown."""
        self.assertEqual(
            "Prog0 is wired to UT_Debug, so the run held it too",
            leases.describe_pairing_lease({"resource": "Prog0", "reason": "Prog0 is wired to UT_Debug"}),
        )
        self.assertEqual(
            "Prog0 is wired to a device this run named, so the run held it too",
            leases.describe_pairing_lease({"resource": "Prog0"}),
        )

    def test_the_record_carries_the_clause_the_registry_gave(self):
        """Not a second wording of its own: the run says what `hold` says.

        `expand_pairs` returns the aliases it added after the ones it was
        given, and one clause for each of those in the same order; the
        records are built from that pairing, so no run can word a pairing
        differently from the commands a person runs by hand.
        """
        registry = self.load(PROGRAMMER)
        named = leases._named_aliases(self.using_the_target(), registry)
        held, clauses = registry.expand_pairs(list(named))
        self.assertEqual(("Prog0",), held[len(named):])
        self.assertEqual(
            list(clauses),
            [record["reason"] for record
             in leases.pairings_for_invocations(self.using_the_target(), registry)],
        )

    def test_a_dry_run_says_which_device_a_pairing_would_add(self):
        """It lists the devices each test binds, so it says this one too."""
        plan = self.root / "dry.yaml"
        plan.write_text(textwrap.dedent(f"""\
            plan: dry pair
            suites:
              fast:
                - Radio setting transitions:
                    hosts: [Host_Linux]
                    platforms: [{PLATFORM}]
                    Ubertooth: UT_Debug
                    coverage: smoke
            """), encoding="utf-8")
        from tests.test_cli import make_source

        source, _short = make_source(self.root)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([
                "run-suite", "--plan", str(plan), "--suite", "fast",
                "--hardware", str(self.write(PROGRAMMER)), "--host", "Host_Linux",
                "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
                "--output", str(self.root / "dry-runs"), "--dry-run",
            ])
        self.assertEqual(0, code, err.getvalue())
        self.assertIn(
            "pair: Prog0 is the programmer wired to UT_Debug, so the run would "
            "hold it too",
            out.getvalue(),
        )


class PairCommands(PairTestCase):
    """What `lease` and `boards` say about a pair."""

    def harness(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        return status, out.getvalue(), err.getvalue()

    def busy_script(self, path: Path, result: Path) -> str:
        """A command that records whether it can take that lock file."""
        return (
            "import fcntl, pathlib\n"
            f"handle = open({str(path)!r}, 'a+')\n"
            "busy = False\n"
            "try:\n"
            "    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except OSError:\n"
            "    busy = True\n"
            f"pathlib.Path({str(result)!r}).write_text('busy' if busy else 'free')\n"
        )

    def test_lease_on_the_target_holds_the_programmer_and_says_why(self):
        registry_path = self.write(PROGRAMMER)
        result = self.root / "programmer.txt"
        status, _out, err = self.harness([
            "lease", "--hardware", str(registry_path),
            "--lease-dir", str(self.leases), "UT_Debug", "--",
            sys.executable, "-c",
            self.busy_script(self.leases / f"{PROGRAMMER_KEY}.lock", result),
        ])
        self.assertEqual(0, status, err)
        self.assertEqual("busy", result.read_text())
        self.assertIn("Prog0 is the programmer wired to UT_Debug", err)
        self.assertIn("so it is held too", err)

    def test_lease_on_the_programmer_holds_the_target(self):
        registry_path = self.write(PROGRAMMER)
        result = self.root / "target.txt"
        status, _out, err = self.harness([
            "lease", "--hardware", str(registry_path),
            "--lease-dir", str(self.leases), "Prog0", "--",
            sys.executable, "-c",
            self.busy_script(self.leases / f"{TARGET_KEY}.lock", result),
        ])
        self.assertEqual(0, status, err)
        self.assertEqual("busy", result.read_text())
        self.assertIn("UT_Debug is the device Prog0 programs", err)

    def test_lease_naming_both_members_runs_the_command_once(self):
        registry_path = self.write(PROGRAMMER)
        status, _out, err = self.harness([
            "lease", "--hardware", str(registry_path),
            "--lease-dir", str(self.leases), "UT_Debug", "Prog0", "--",
            sys.executable, "-c", "pass",
        ])
        self.assertEqual(0, status, err)
        self.assertNotIn("note:", err)

    def test_lease_refuses_the_target_while_the_programmer_is_held(self):
        registry_path = self.write(PROGRAMMER)
        registry = FileResourceRegistry.load(registry_path)
        held = leases.acquire(
            [leases.Board("Prog0", leases.resource_key(registry.get("Prog0")))],
            leases.holder_info(run_id=None, owner="bob", purpose="programming it"),
            directory=self.leases,
        )
        self.addCleanup(held.release)
        marker = self.root / "ran"
        status, _out, err = self.harness([
            "lease", "--hardware", str(registry_path),
            "--lease-dir", str(self.leases), "--wait-for-boards", "0",
            "UT_Debug", "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')",
        ])
        self.assertEqual(2, status)
        self.assertFalse(marker.exists())
        self.assertIn("bob", err)
        self.assertIn("Prog0 is the programmer wired to UT_Debug", err)

    def test_boards_lists_the_programmer_and_what_it_comes_with(self):
        registry_path = self.write(PROGRAMMER)
        status, out, err = self.harness([
            "boards", "--hardware", str(registry_path),
            "--lease-dir", str(self.leases), "--host", "Host_Linux",
        ])
        self.assertEqual(0, status, err)
        self.assertIn("Prog0", out)
        self.assertIn("Prog0 is the programmer wired to UT_Debug, so the two are "
                      "held together", out)
        self.assertIn("UT_Debug is the device Prog0 programs", out)

    def test_boards_json_carries_the_pair(self):
        registry_path = self.write(PROGRAMMER)
        status, out, err = self.harness([
            "boards", "--hardware", str(registry_path), "--json",
            "--lease-dir", str(self.leases), "--host", "Host_Linux",
        ])
        self.assertEqual(0, status, err)
        states = {state["resource"]: state for state in json.loads(out)}
        self.assertEqual(
            [{"resource": "Prog0",
              "why": "Prog0 is the programmer wired to UT_Debug"}],
            states["UT_Debug"]["pair"],
        )
        self.assertNotIn("pair", states["UT_Free"])

    def test_the_programmer_is_held_as_the_command_runs(self):
        """The lock is the command's for as long as it runs, as for a board."""
        registry_path = self.write(PROGRAMMER)
        status, _out, err = self.harness([
            "lease", "--hardware", str(registry_path),
            "--lease-dir", str(self.leases), "UT_Debug", "--",
            sys.executable, "-c", "raise SystemExit(7)",
        ])
        self.assertEqual(7, status, err)
        # Free again afterwards, with nothing left holding either member.
        registry = FileResourceRegistry.load(registry_path)
        states = leases.board_states(
            [leases.Board(alias, leases.resource_key(registry.get(alias)))
             for alias in ("Prog0", "UT_Debug")],
            directory=self.leases,
        )
        self.assertEqual([False, False], [state["held"] for state in states])


if __name__ == "__main__":
    unittest.main()
