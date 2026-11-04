"""One lock file, one device (#288, #290).

Two flat spaces of names decide which file a device is held through: the
lease keys in the harness's own lease directory, and the plain serials the
bench's own lock files are named after. A registry that puts two devices in
one place in either space is refused where it is loaded, and a lease key
carries its type's prefix for everything but an Ubertooth, so a serial of 32
hex digits can no longer be the lease file of an Ubertooth with that serial.

Every path that leases a device asks for both of its names
(``leases.board_for_resource``), which is what makes a run and a lab's own
``flock`` wrapper exclude each other (#317).

Nothing here touches a device: every registry is a temporary file and every
lock is a file in a temporary directory.
"""

from __future__ import annotations

import fcntl
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from ubertooth_harness import leases
from ubertooth_harness.resources import FileResourceRegistry, RegistryError

PLATFORM = "macos" if sys.platform == "darwin" else "linux"
# Invented serials: no lab device is named here.
BOARD = "aaaa0000aaaa0000aaaa0000aaaa0000"
OTHER = "bbbb1111bbbb1111bbbb1111bbbb1111"
BRIDGE = "cccc2222cccc2222cccc2222cccc2222"

HOST = textwrap.dedent(
    f"""
    resources:
      Host_Linux:
        type: Host
        platform: {PLATFORM}
        roles: [controller, test target]
    """
).lstrip()


def ubertooth(alias: str, serial: str) -> str:
    return (
        f"  {alias}:\n"
        "    type: Ubertooth\n"
        "    attached to: Host_Linux\n"
        "    identity:\n"
        f"      serial: {serial}\n"
    )


def dk(alias: str, jlink: str) -> str:
    return (
        f"  {alias}:\n"
        "    type: nRF52840 DK\n"
        "    attached to: Host_Linux\n"
        "    roles: [advertiser]\n"
        "    identity:\n"
        f'      jlink serial: "{jlink}"\n'
        f'      BLE address: "C0:00:00:00:00:{alias[-1]}1"\n'
    )


def peer(alias: str, bridge: str, last: str = "01") -> str:
    return (
        f"  {alias}:\n"
        "    type: ESP32 Classic peer\n"
        "    attached to: Host_Linux\n"
        "    roles: [acceptor, initiator]\n"
        "    identity:\n"
        f"      bridge serial: {bridge}\n"
        f'      BD_ADDR: "C8:00:00:00:00:{last}"\n'
    )


def programmer(alias: str, serial: str, target: str) -> str:
    return (
        f"  {alias}:\n"
        "    type: Debug programmer\n"
        "    attached to: Host_Linux\n"
        f"    programs: {target}\n"
        "    identity:\n"
        f'      serial: "{serial}"\n'
    )


class NamesTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def load(self, *blocks: str) -> FileResourceRegistry:
        path = self.root / "hardware.yaml"
        path.write_text(HOST + "".join(blocks), encoding="utf-8")
        return FileResourceRegistry.load(path)

    def refuse(self, *blocks: str) -> str:
        with self.assertRaises(RegistryError) as caught:
            self.load(*blocks)
        return str(caught.exception)


class OneLeaseKeyOneDevice(NamesTestCase):
    """Two resources may not take one lease file (#288).

    For every type the harness leases today, the key and the bench lock
    name come from one identity field, so a registry that puts two devices
    on one key almost always puts them on one bench name as well and is
    refused for that, which names the serial a person wrote. What is left
    for the key check itself is below: the key check is what makes the rule
    hold for a key built from something else.
    """

    def test_two_dks_whose_keys_collide_and_whose_serials_do_not(self):
        """The lease key alone, with nothing in the serials to go on.

        A J-Link serial is zero-padded by sysfs and not by nrfutil, so the
        key drops the leading zeros. `000000` and `0000000` are two serials
        with no bench name in common (the stripped form is empty and is not
        a name at all) and one lease file.
        """
        reason = self.refuse(dk("DK_A", "000000"), dk("DK_B", "0000000"))
        self.assertIn("DK_A", reason)
        self.assertIn("DK_B", reason)
        self.assertIn("would both be leased as 'nrf52840dk-0'", reason)
        self.assertIn("one lease file is one device", reason)

    def test_the_key_is_read_from_the_function_that_builds_it(self):
        """So a type whose key stops coming from a serial is still covered."""
        registry = self.load(dk("DK_A", "000123456789"))
        self.assertEqual(
            "nrf52840dk-123456789", leases.resource_key(registry.get("DK_A"))
        )

    def test_distinct_serials_load_and_keep_their_own_keys(self):
        """The refusals above must not be refusing an ordinary bench."""
        registry = self.load(
            ubertooth("UT_A", BOARD),
            ubertooth("UT_B", OTHER),
            dk("DK_A", "000123456789"),
            peer("ESP_A", BRIDGE),
            programmer("Prog_A", "A000AA00", "UT_A"),
        )
        self.assertEqual(
            [BOARD, OTHER, "nrf52840dk-123456789", f"esp32-{BRIDGE}",
             "programmer-A000AA00"],
            [leases.resource_key(registry.get(alias))
             for alias in ("UT_A", "UT_B", "DK_A", "ESP_A", "Prog_A")],
        )

    def test_a_device_with_no_key_yet_does_not_fail_the_registry(self):
        """A board recorded with a short serial is not leasable, not invalid.

        Leasing it is refused where it is asked for, with the field to fill
        in; the registry still loads, as it did before keys were compared.
        """
        registry = self.load(ubertooth("UT_Short", "aaaa0000"))
        with self.assertRaises(leases.LeaseError) as caught:
            leases.resource_key(registry.get("UT_Short"))
        self.assertIn("needs a full 32-hex-digit serial", str(caught.exception))


class OneBenchLockNameOneDevice(NamesTestCase):
    """Two devices may not share the serial a bench lock is named after.

    That name is also a lease key for an Ubertooth, so these cases cover
    both spaces at once; the message names the serial, which is the field
    a person wrote.
    """

    def test_two_peers_registered_with_one_bridge_serial(self):
        """Their other identity fields differ, so the registry loaded before.

        The duplicate-identity check compares whole identity mappings, and
        one of these two would have been held as the other.
        """
        reason = self.refuse(
            peer("ESP_A", BRIDGE), peer("ESP_B", BRIDGE, last="02")
        )
        self.assertIn("ESP_A", reason)
        self.assertIn("ESP_B", reason)
        self.assertIn("same serial", reason)

    def test_one_serial_written_two_ways_is_still_one_device(self):
        """Taken verbatim, a peer's two spellings would name two files.

        That is the mistake: one device would be held twice over, so the
        comparison ignores case even though the two could not collide.
        """
        reason = self.refuse(
            peer("ESP_A", BRIDGE.upper()), peer("ESP_B", BRIDGE, last="02")
        )
        self.assertIn("same serial", reason)

    def test_two_ubertooths_registered_with_one_serial_in_two_cases(self):
        reason = self.refuse(
            ubertooth("UT_A", BOARD.upper()), ubertooth("UT_B", BOARD)
        )
        self.assertIn("UT_A", reason)
        self.assertIn("UT_B", reason)

    def test_an_all_digit_serial_reserves_no_second_name(self):
        """Only a DK has a second spelling, so only a DK reserves one.

        A peer's bridge serial is taken verbatim, so `0123` is one name;
        another device registered `123` collides with nothing.
        """
        registry = self.load(peer("ESP_A", "0123456789"),
                             programmer("Prog_A", "123456789", "ESP_A"))
        self.assertEqual(
            "esp32-0123456789", leases.resource_key(registry.get("ESP_A"))
        )
        # A DK's stripped form is a real name, so that one still collides.
        reason = self.refuse(dk("DK_A", "000123456789"),
                             peer("ESP_B", "123456789", last="02"))
        self.assertIn("DK_A (nRF52840 DK)", reason)
        self.assertIn("ESP_B (ESP32 Classic peer)", reason)
        # And the name quoted is the one that collides, which here is
        # neither device's spelling of it in the registry.
        self.assertIn("'123456789' is the serial of both", reason)

    def test_a_peer_may_not_take_a_registered_board_s_serial(self):
        """A bridge serial is 32 hex digits, like a board's (#290).

        `scripts/lab/hold` names the bench's own lock file after the plain
        serial, so these two would be held through one file.
        """
        reason = self.refuse(ubertooth("UT_A", BOARD), peer("ESP_A", BOARD))
        self.assertIn("UT_A (Ubertooth)", reason)
        self.assertIn("ESP_A (ESP32 Classic peer)", reason)
        self.assertIn("lock of one would be read as a lock of the other", reason)

    def test_a_dk_may_not_take_a_peer_s_serial(self):
        """Every type is in that one space, not only the two with a pair."""
        reason = self.refuse(dk("DK_A", "123456789"), peer("ESP_A", "123456789"))
        self.assertIn("DK_A (nRF52840 DK)", reason)
        self.assertIn("ESP_A (ESP32 Classic peer)", reason)

    def test_a_device_the_harness_does_not_lease_takes_no_name(self):
        """Only a leased device has a lease name, so only it claims one.

        The key function answers for anything it is handed, falling back to
        a board's serial, so a hub whose own serial happened to look like a
        board's would otherwise be refused for sharing a file no command
        ever locks for it.
        """
        registry = self.load(
            ubertooth("UT_A", BOARD),
            f"  Hub0:\n"
            "    type: USB power controller\n"
            "    attached to: Host_Linux\n"
            "    identity:\n"
            f'      serial: "{BOARD}"\n'
            "    driver: acroname BrainStem USBHub3+\n"
            "    capabilities:\n"
            "      - per-port VBUS switching\n"
            "      - power readback\n",
        )
        self.assertEqual(BOARD, leases.resource_key(registry.get("UT_A")))

    def test_a_serial_that_spells_another_device_s_lease_name_is_refused(self):
        """One device's serial may not be another device's lease name.

        A programmer's serial is chosen by whoever labels the adapter and
        may be any name a file can carry, so it may be the very name another
        device's lease uses. No file is shared by that pair today, because
        each device's external lock is named after its own plain serial; it
        is refused because a registry that depends on which of the two names
        a command happens to take reads as a mistake to the next editor.
        """
        reason = self.refuse(
            peer("ESP_A", BRIDGE),
            programmer("Prog_A", f"esp32-{BRIDGE}", "ESP_A"),
        )
        self.assertIn("Prog_A (Debug programmer)", reason)
        self.assertIn("ESP_A", reason)
        self.assertIn("is the name", reason)
        self.assertIn("is leased under", reason)
        # The lease files themselves do not collide, which is why this needs
        # a check of its own.
        self.assertNotEqual(f"esp32-{BRIDGE}", f"programmer-esp32-{BRIDGE}")

    def test_a_board_may_not_take_a_name_the_lease_directory_keeps(self):
        """`host-power` and `takeover` are the lease directory's own files."""
        for serial, what in (
            ("host-power", "this host's power/firmware lock"),
            ("takeover", "the lease directory's own takeover lock"),
        ):
            with self.subTest(serial=serial):
                reason = self.refuse(
                    ubertooth("UT_A", BOARD), programmer("Prog_A", serial, "UT_A")
                )
                self.assertIn(what, reason)

    def test_a_dk_keeps_both_spellings_of_its_own_serial(self):
        """Its own two names are not a collision with itself."""
        registry = self.load(dk("DK_A", "000123456789"))
        self.assertEqual("nrf52840dk-123456789",
                         leases.resource_key(registry.get("DK_A")))

    def test_two_dks_whose_serials_differ_only_in_leading_zeros(self):
        """sysfs zero-pads a J-Link serial and nrfutil does not.

        So a bench note may name either spelling, both are in that one
        space, and these two would also take one lease file.
        """
        reason = self.refuse(dk("DK_A", "000123456789"), dk("DK_B", "123456789"))
        self.assertIn("DK_A", reason)
        self.assertIn("DK_B", reason)
        self.assertIn("one serial is one device", reason)
        registry = self.load(dk("DK_A", "000123456789"))
        self.assertEqual(
            "nrf52840dk-123456789", leases.resource_key(registry.get("DK_A"))
        )


class BenchLockFileNames(NamesTestCase):
    """A device's two lock names: its lease file, and the lab's own file."""

    def setUp(self):
        super().setUp()
        self.leases = self.root / "leases"
        self.leases.mkdir()
        self.locks = self.root / "bench"
        self.locks.mkdir()
        self.template = str(self.locks / "board-{serial}.lock")

    def peer_board(self, alias: str, serial: str) -> leases.Board:
        """The device every path that leases a peer asks for."""
        registry = self.load(peer(alias, serial))
        return leases.board_for_resource(registry.get(alias))

    def test_the_lease_file_is_prefixed_and_the_bench_s_file_is_not(self):
        board = self.peer_board("ESP_A", BRIDGE)
        self.assertEqual(f"esp32-{BRIDGE}", board.key)
        self.assertEqual(BRIDGE, board.lock_name)
        self.assertEqual(
            self.locks / f"board-{BRIDGE}.lock",
            leases.external_lock_path(self.template, board),
        )

    def test_a_board_of_that_serial_is_not_held_by_the_bench_name(self):
        """The whole of #290: the two files are no longer one file.

        A bench may load more than one registry against one lease
        directory, so a serial that collides cannot always be refused where
        the registry is read; the prefix settles it either way.
        """
        board = self.peer_board("ESP_A", BRIDGE)
        held = leases.acquire(
            [board],
            leases.holder_info(run_id=None, owner="bench", purpose="a peer session"),
            directory=self.leases,
            external_template=self.template,
        )
        self.addCleanup(held.release)
        self.assertTrue((self.leases / f"esp32-{BRIDGE}.lock").exists())
        self.assertTrue((self.locks / f"board-{BRIDGE}.lock").exists())
        states = leases.board_states(
            [leases.Board("UT_Elsewhere", BRIDGE)], directory=self.leases
        )
        self.assertFalse(states[0]["held"])

    def test_the_bench_s_own_file_is_still_the_plain_serial(self):
        """What the second name is for: a script written against the serial.

        It holds `board-<serial>.lock` and must be met on that same file.
        That is harness #317: a run that asked only for its lease key left
        such a script free to open the peer's bridge, which resets the chip.
        """
        board = self.peer_board("ESP_A", BRIDGE)
        external = self.locks / f"board-{BRIDGE}.lock"
        held = leases.acquire(
            [leases.Board("a lab script", "bench-elsewhere", external_key=BRIDGE)],
            leases.holder_info(run_id=None, owner="bench", purpose="a peer session"),
            directory=self.leases,
            external_template=self.template,
        )
        self.addCleanup(held.release)
        self.assertEqual(
            external, leases.external_lock_path(self.template, board)
        )
        states = leases.board_states(
            [board], directory=self.leases, external_template=self.template
        )
        self.assertTrue(states[0]["held"])

    def test_a_run_and_a_lab_script_meet_on_the_lab_s_own_file(self):
        """#317, the other way round: the run holds it, the script waits.

        The script's `flock` is all a lab wrapper takes, so a run that does
        not hold that file does not exclude it, and two sessions can drive
        one peer. The run holds it now.
        """
        board = self.peer_board("ESP_A", BRIDGE)
        held = leases.acquire(
            [board],
            leases.holder_info(run_id=None, owner="a run", purpose="a pair session"),
            directory=self.leases,
            external_template=self.template,
        )
        self.addCleanup(held.release)
        external = self.locks / f"board-{BRIDGE}.lock"
        with open(external, "rb") as handle:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_run_asks_for_the_lab_s_own_file_for_every_device_it_takes(self):
        """The path that matters: what a run holds while a case drives a peer.

        This is where #317 was: a run asked for its lease key alone, so the
        file a lab wrapper locks was free and a second process could open
        the peer's bridge and reset the chip under a capture.
        """
        registry = self.load(
            ubertooth("UT_A", BOARD), peer("ESP_A", BRIDGE), dk("DK_A", "000123456789")
        )
        invocation = SimpleNamespace(
            test_name="a pair case", host="Host_Linux",
            resources={"Ubertooth": "UT_A", "initiator": "ESP_A", "advertiser": "DK_A"},
            power_isolation=(), exclusive_ubertooth=False,
        )
        boards = leases.boards_for_invocations([invocation], registry)
        self.assertEqual(
            {BOARD: BOARD, f"esp32-{BRIDGE}": BRIDGE,
             "nrf52840dk-123456789": "000123456789"},
            {board.key: board.lock_name for board in boards},
        )

    def test_a_template_that_points_at_the_lease_directory_is_refused(self):
        """Either of a device's own names: one process cannot flock one file
        twice, and the file it would take is a device's lease."""
        board = self.peer_board("ESP_A", BRIDGE)
        for template in (
            str(self.leases / "{serial}.lock"),         # the lab's own name
            str(self.leases / "esp32-{serial}.lock"),   # the lease key
        ):
            with self.subTest(template=template):
                with self.assertRaises(leases.LeaseError) as caught:
                    leases.acquire(
                        [board],
                        leases.holder_info(
                            run_id=None, owner="bench", purpose="a peer session"
                        ),
                        directory=self.leases,
                        external_template=template,
                    )
                self.assertIn("the harness's own lease file", str(caught.exception))

    def test_a_ticket_records_the_lease_name_a_reader_of_it_uses(self):
        """A ticket is read for the lease files of its command's set.

        A lab's own lock file is never read from one, so the name the
        bench uses for a device is not in it; the queue's own file is.
        """
        board = self.peer_board("ESP_A", BRIDGE)
        queue = leases.TicketQueue(
            self.leases, [board],
            leases.holder_info(run_id=None, owner="bench", purpose="a peer session"),
            wait_s=60.0,
        )
        self.assertEqual(
            [{"key": f"esp32-{BRIDGE}", "resource": "ESP_A"}], queue.waiting_for
        )
        read_back = leases.waiting_resources(
            {"waiting_for": queue.waiting_for}, board
        )
        self.assertEqual([f"esp32-{BRIDGE}"], [item.key for item in read_back])

    def test_a_serial_too_long_for_a_lock_file_is_refused(self):
        """The lease file built from it has to be one a file can carry.

        A key carries its type's prefix, so a serial that is a file name on
        its own may still build one that is not. Such a name would be dropped
        where a ticket is read back, so the set that ticket's command waits
        for would read short.
        """
        for block, field, limit in (
            (programmer("Prog_A", "p" * 54, "UT_A"), "serial", 53),
            (peer("ESP_A", "e" * 59), "bridge serial", 58),
        ):
            with self.subTest(field=field):
                reason = self.refuse(ubertooth("UT_A", BOARD), block)
                self.assertIn(f"'{field}' may be at most {limit} characters", reason)
        # The longest that fits loads and is leased as itself.
        registry = self.load(ubertooth("UT_A", BOARD),
                             programmer("Prog_A", "p" * 53, "UT_A"))
        self.assertEqual(
            f"programmer-{'p' * 53}", leases.resource_key(registry.get("Prog_A"))
        )
        # And a registry made in code, which never passed the check above,
        # is refused where the key is asked for.
        made = SimpleNamespace(
            alias="Prog_B", type="Debug programmer",
            identity={"serial": "p" * 60},
        )
        with self.assertRaises(leases.LeaseError) as caught:
            leases.resource_key(made)
        self.assertIn("'serial' of at most 53 characters", str(caught.exception))

    def test_a_board_needs_no_second_name_at_all(self):
        """Its lease key is its serial, so there is nothing to prefix."""
        registry = self.load(ubertooth("UT_A", BOARD))
        board = leases.board_for_resource(registry.get("UT_A"))
        self.assertIsNone(board.external_key)
        self.assertEqual(BOARD, board.key)
        self.assertEqual(BOARD, board.lock_name)

    def test_a_serial_keeps_the_spelling_it_was_registered_with(self):
        """One spelling is one device, in both of its names.

        The registry is what names a peer's bridge on USB and what a lab
        script was written against, so a serial lowercased for the lock file
        would name a file that script never locks, on a filesystem that tells
        the two apart. A bridge serial may be registered in capitals.
        """
        upper = BRIDGE.upper()
        registry = self.load(peer("ESP_A", upper))
        board = leases.board_for_resource(registry.get("ESP_A"))
        self.assertEqual(f"esp32-{upper}", board.key)
        self.assertEqual(upper, board.lock_name)
        self.assertEqual(
            self.locks / f"board-{upper}.lock",
            leases.external_lock_path(self.template, board),
        )

    def test_every_leased_type_s_two_names(self):
        """One rule for all four, so no path can name a file no other does."""
        registry = self.load(
            ubertooth("UT_A", BOARD), peer("ESP_A", BRIDGE),
            dk("DK_A", "000123456789"), programmer("Prog_A", "Z9999ZZ0", "UT_A"),
        )
        self.assertEqual(
            {
                "UT_A": (BOARD, BOARD),
                "ESP_A": (f"esp32-{BRIDGE}", BRIDGE),
                "DK_A": ("nrf52840dk-123456789", "000123456789"),
                "Prog_A": ("programmer-Z9999ZZ0", "Z9999ZZ0"),
            },
            {
                alias: (board.key, board.lock_name)
                for alias in ("UT_A", "ESP_A", "DK_A", "Prog_A")
                for board in (leases.board_for_resource(registry.get(alias)),)
            },
        )


if __name__ == "__main__":
    unittest.main()
