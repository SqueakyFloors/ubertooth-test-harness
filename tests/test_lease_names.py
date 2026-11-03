"""One lock file, one device (#288, #290).

Two flat spaces of names decide which file a device is held through: the
lease keys in the harness's own lease directory, and the plain serials the
bench's own lock files are named after. A registry that puts two devices in
one place in either space is refused where it is loaded, and the lease file
that goes with a bench lock name is prefixed, so a serial of 32 hex digits
can no longer be the lease file of an Ubertooth with that serial.

Nothing here touches a device: every registry is a temporary file and every
lock is a file in a temporary directory.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

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
    """Two resources may not take one lease file (#288)."""

    def test_two_dks_whose_keys_collide_and_whose_serials_do_not(self):
        """The lease key alone, with nothing in the serials to go on.

        A J-Link serial is zero-padded by sysfs and not by nrfutil, so the
        key drops the leading zeros: these two are two serials, two bench
        lock names, and one lease file. Nothing but the key check sees it.
        """
        reason = self.refuse(dk("DK_A", "000000"), dk("DK_B", "0000000"))
        self.assertIn("DK_A", reason)
        self.assertIn("DK_B", reason)
        self.assertIn("would both be leased as 'nrf52840dk-0'", reason)
        self.assertIn("one lease file is one device", reason)

    def test_two_peers_registered_with_one_bridge_serial(self):
        """Their other identity fields differ, so only the key is shared."""
        reason = self.refuse(
            peer("ESP_A", BRIDGE), peer("ESP_B", BRIDGE, last="02")
        )
        self.assertIn("ESP_A", reason)
        self.assertIn("ESP_B", reason)

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
    """Two devices may not share the serial a bench lock is named after."""

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
    """What the bench's own lock name takes, and what it no longer takes."""

    def setUp(self):
        super().setUp()
        self.leases = self.root / "leases"
        self.leases.mkdir()
        self.locks = self.root / "bench"
        self.locks.mkdir()
        self.template = str(self.locks / "board-{serial}.lock")

    def bench_board(self, alias: str, serial: str) -> leases.Board:
        """The second lock `scripts/lab/hold` takes for such a device."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lab"))
        self.addCleanup(sys.path.pop, 0)
        import _bench

        registry = self.load(peer(alias, serial))
        return _bench.bench_lock_board(registry.get(alias))

    def test_the_lease_file_is_prefixed_and_the_bench_s_file_is_not(self):
        board = self.bench_board("ESP_A", BRIDGE)
        self.assertEqual(f"bench-{BRIDGE}", board.key)
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
        board = self.bench_board("ESP_A", BRIDGE)
        held = leases.acquire(
            [board],
            leases.holder_info(run_id=None, owner="bench", purpose="a peer session"),
            directory=self.leases,
            external_template=self.template,
        )
        self.addCleanup(held.release)
        self.assertTrue((self.leases / f"bench-{BRIDGE}.lock").exists())
        self.assertTrue((self.locks / f"board-{BRIDGE}.lock").exists())
        states = leases.board_states(
            [leases.Board("UT_Elsewhere", BRIDGE)], directory=self.leases
        )
        self.assertFalse(states[0]["held"])

    def test_the_bench_s_own_file_is_still_the_plain_serial(self):
        """What the second name is for: a script written against the serial.

        It holds `board-<serial>.lock` and must be met on that same file.
        """
        board = self.bench_board("ESP_A", BRIDGE)
        external = self.locks / f"board-{BRIDGE}.lock"
        held = leases.acquire(
            [leases.Board("ESP_A (bench lock)", "bench-elsewhere",
                          external_key=BRIDGE)],
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

    def test_a_board_needs_no_second_name_at_all(self):
        """Its lease key is its serial, so there is nothing to prefix."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lab"))
        self.addCleanup(sys.path.pop, 0)
        import _bench

        registry = self.load(ubertooth("UT_A", BOARD))
        self.assertIsNone(_bench.bench_lock_board(registry.get("UT_A")))
        board = leases.Board("UT_A", BOARD)
        self.assertEqual(BOARD, board.lock_name)


if __name__ == "__main__":
    unittest.main()
