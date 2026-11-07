"""Per-board status, and managing a target's boards from the GUI (#134).

Four layers, in the order a change travels: what the wire may carry
(``protocol``), what a status adds up to (``boardstatus``), how the registry
file is edited (``registryedit``), and what the runner, the controller and
the page do with all of it.

Every registry here is synthetic: a fixture written into a temporary
directory, with made-up serials. No bench registry is read and none is
written, so nothing in this module can reach real hardware.
"""

import asyncio
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from ubertooth_harness import boardstatus, leases, registryedit
from ubertooth_harness.enumeration import UsbDevice
from ubertooth_harness.gui import WEB_DIR
from ubertooth_harness.protocol import (
    MAX_BOARDS,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_controller_message,
    decode_runner_message,
    encode_controller_message,
    encode_runner_message,
)
from ubertooth_harness.registryedit import BoardEdit, RegistryEditError
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from ubertooth_harness.runner_service import RunnerService

try:
    from test_runner_service import RunnerFixture, StubController, StubEnumerator
except ImportError:  # run as tests.test_board_management
    from tests.test_runner_service import (
        RunnerFixture,
        StubController,
        StubEnumerator,
    )

TIMEOUT_S = 30
PLATFORM = "macos" if sys.platform == "darwin" else "linux"
# Made-up serials of the right shape: 32 hex digits, as a board reports.
FIRST = "aaaa0000aaaa0000aaaa0000aaaa0000"
SECOND = "bbbb1111bbbb1111bbbb1111bbbb1111"
THIRD = "cccc2222cccc2222cccc2222cccc2222"

# A registry with comments, a hub, two boards, and a note at the end: an edit
# must leave every line of it that is not the board's own exactly as it is.
HARDWARE = textwrap.dedent(f"""\
    # The bench this registry describes. Keep these notes.
    resources:
      Host_Local:
        type: Host
        platform: {PLATFORM}
        roles: [test target]
        defaults:
          Ubertooth: UT_Local0

      # The switchable hub the boards hang off.
      Hub_Local0:
        type: USB power controller
        attached to: Host_Local
        identity:
          serial: "0x12345678"
        driver: acroname BrainStem USBHub3+
        capabilities:
          - per-port VBUS switching
          - per-port data switching
          - power readback

      UT_Local0:
        type: Ubertooth
        attached to: Host_Local
        identity:
          serial: "{FIRST}"
        power:
          controller: Hub_Local0
          port: 0

      # The second board sits on no switchable port.
      UT_Local1:
        type: Ubertooth
        attached to: Host_Local
        identity:
          serial: "{SECOND}"

    # A note at the end of the file, which no edit may take away.
    """)


def board(alias="UT_Local0", serial=FIRST, **overrides):
    """One entry of the protocol's board list."""
    entry = {
        "alias": alias, "serial": serial, "present": True, "held": False,
        "holder": "", "firmware_revision": None, "power_controller": None,
        "power_port": None,
    }
    entry.update(overrides)
    return entry


# ----- what the wire may carry ---------------------------------------------


class ProtocolTests(unittest.TestCase):

    def register(self, **overrides):
        fields = dict(
            host="Host_Local", hostname="box", os={"system": "Linux"},
            python="3.12.6", ubertooth_serials=[FIRST], enumeration_error=None,
            boards=[board()], heartbeat_interval_s=10.0, active_run_id=None,
        )
        fields.update(overrides)
        return encode_runner_message("register", **fields)

    def test_register_and_heartbeat_carry_the_board_list(self):
        message = decode_runner_message(self.register())
        self.assertEqual([board()], message["boards"])
        beat = decode_runner_message(encode_runner_message(
            "heartbeat", active_run_id=None, elapsed_s=None,
            last_event_sequence=None, last_event_type=None,
            boards=[board(present=False)],
        ))
        self.assertIs(False, beat["boards"][0]["present"])

    def test_the_version_is_the_one_this_change_bumped_it_to(self):
        # There is no negotiation, so the number is part of the contract: a
        # runner of the last deploy and this controller never pair up.
        self.assertEqual(8, PROTOCOL_VERSION)

    def test_a_board_entry_is_strict(self):
        cases = {
            "missing field": {k: v for k, v in board().items() if k != "held"},
            "extra field": {**board(), "temperature": 20},
            "alias is a path": board(alias="../etc"),
            "held is a string": board(held="yes"),
            "present is a string": board(present="yes"),
            "holder is null": board(holder=None),
            "negative port": board(power_port=-1),
            "port is a string": board(power_port="1"),
        }
        for name, entry in cases.items():
            with self.subTest(name):
                with self.assertRaises(ProtocolError):
                    self.register(boards=[entry])

    def test_boards_are_one_per_alias_and_bounded(self):
        with self.assertRaises(ProtocolError):
            self.register(boards=[board(), board()])
        many = [
            board(alias=f"UT_{index}", serial=f"{index:032x}")
            for index in range(MAX_BOARDS + 1)
        ]
        with self.assertRaises(ProtocolError):
            self.register(boards=many)

    def test_present_may_be_null_when_usb_cannot_be_read(self):
        # Unknown is not offline: a target that cannot enumerate must not
        # make every board look unplugged.
        message = decode_runner_message(self.register(
            boards=[board(present=None)], ubertooth_serials=None,
            enumeration_error="USB enumeration is not supported on sunos5",
        ))
        self.assertIsNone(message["boards"][0]["present"])

    def test_list_devices_is_a_request_with_only_an_id(self):
        message = decode_controller_message(
            encode_controller_message("list_devices", request_id="r1")
        )
        self.assertEqual("list_devices", message["type"])
        with self.assertRaises(ProtocolError):
            encode_controller_message("list_devices", request_id="r1", host="x")

    def update(self, **overrides):
        fields = dict(
            request_id="r1", action="add", alias="UT_Local2", serial=THIRD,
            power_controller=None, power_port=None,
        )
        fields.update(overrides)
        return encode_controller_message("update_registry", **fields)

    def test_an_update_the_receiver_could_not_act_on_is_refused(self):
        cases = {
            "add with no serial": dict(action="add", serial=None),
            "add with an empty serial": dict(action="add", serial=""),
            "remove with a serial": dict(action="remove", serial=THIRD),
            "remove with a controller": dict(
                action="remove", serial=None, power_controller="Hub_Local0",
                power_port=0,
            ),
            "a controller with no port": dict(power_controller="Hub_Local0"),
            "a port on no controller": dict(power_port=0),
            "an action of its own": dict(action="replace"),
            "an alias that is a path": dict(alias="../../etc"),
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                with self.assertRaises(ProtocolError):
                    self.update(**overrides)

    def test_a_whole_update_round_trips(self):
        message = decode_controller_message(
            self.update(power_controller="Hub_Local0", power_port=3)
        )
        self.assertEqual(("add", "UT_Local2", THIRD), (
            message["action"], message["alias"], message["serial"]
        ))
        self.assertEqual(3, message["power_port"])
        removal = decode_controller_message(self.update(
            action="remove", serial=None,
        ))
        self.assertEqual("remove", removal["action"])

    def test_the_answers_carry_what_the_page_needs(self):
        answer = decode_runner_message(encode_runner_message(
            "devices", request_id="r1",
            devices=[{"serial": FIRST, "vendor": "1d50", "product": "6002",
                      "path": "3-2.1", "alias": "UT_Local0"},
                     {"serial": "", "vendor": "1d50", "product": "6003",
                      "path": "3-2.2", "alias": None}],
            error=None,
            power_controllers=[{"alias": "Hub_Local0", "driver": "acroname",
                                "ports": [1, 2, 3]}],
            suggested_alias="UT_Local2", boards=[board()],
        ))
        self.assertEqual("UT_Local0", answer["devices"][0]["alias"])
        self.assertIsNone(answer["devices"][1]["alias"])
        updated = decode_runner_message(encode_runner_message(
            "registry_updated", request_id="r1", action="remove",
            alias="UT_Local1", boards=[board()], ubertooth_serials=[FIRST],
        ))
        self.assertEqual("UT_Local1", updated["alias"])

    def test_a_refusal_carries_a_status_a_route_can_answer_with(self):
        for status in (400, 404, 409, 503):
            with self.subTest(status=status):
                refusal = decode_runner_message(encode_runner_message(
                    "registry_refused", request_id="r1", status=status,
                    reason="no",
                ))
                self.assertEqual(status, refusal["status"])
        for status in (200, 500, 302, True):
            with self.subTest(status=status):
                with self.assertRaises(ProtocolError):
                    encode_runner_message(
                        "registry_refused", request_id="r1", status=status,
                        reason="no",
                    )


# ----- what a status adds up to --------------------------------------------


class BoardStatusTests(unittest.TestCase):

    def test_a_free_board_a_run_has_read_is_ready(self):
        status, why = boardstatus.board_status(
            board(firmware_revision="git-abcdef0")
        )
        self.assertEqual(boardstatus.READY, status)
        self.assertIn("git-abcdef0", why)

    def test_a_board_usb_has_not_got_is_offline(self):
        status, why = boardstatus.board_status(board(present=False))
        self.assertEqual(boardstatus.OFFLINE, status)
        self.assertIn("USB", why)

    def test_a_leased_board_is_busy_and_names_its_holder(self):
        status, why = boardstatus.board_status(
            board(held=True, holder="agent-1, run abc",
                  firmware_revision="git-abcdef0")
        )
        self.assertEqual(boardstatus.BUSY, status)
        self.assertIn("agent-1, run abc", why)

    def test_a_leased_board_is_busy_even_when_usb_cannot_be_read(self):
        # An established fact beats an unestablished one: a lease holds it,
        # whatever this target can say about USB.
        status, why = boardstatus.board_status(
            board(present=None, held=True, holder="agent-1")
        )
        self.assertEqual(boardstatus.BUSY, status)
        self.assertIn("agent-1", why)

    def test_a_board_no_run_has_read_is_unknown_not_ready(self):
        status, why = boardstatus.board_status(board())
        self.assertEqual(boardstatus.UNKNOWN, status)
        self.assertIn("not established", why)

    def test_an_offline_board_mentions_the_dfu_bootloader(self):
        # A board sitting in its DFU bootloader reports no serial, so it is
        # absent from the enumerated set; the reason says so rather than
        # leaving "unplugged" as the only explanation.
        _, why = boardstatus.board_status(board(present=False))
        self.assertIn("DFU bootloader", why)

    def test_a_target_that_cannot_enumerate_leaves_its_boards_unknown(self):
        status, why = boardstatus.board_status(
            board(present=None, firmware_revision="git-abcdef0")
        )
        self.assertEqual(boardstatus.UNKNOWN, status)
        self.assertIn("not established", why)
    def test_every_status_has_a_level_the_page_can_draw(self):
        self.assertEqual(set(boardstatus.STATUSES), set(boardstatus.LEVELS))
        self.assertEqual("ready", boardstatus.LEVELS[boardstatus.READY])
        self.assertEqual("offline", boardstatus.LEVELS[boardstatus.OFFLINE])
        # Busy and unknown are one color and two words: the color says
        # "look at this", the word says which of the two it is.
        self.assertEqual("attention", boardstatus.LEVELS[boardstatus.BUSY])
        self.assertEqual("attention", boardstatus.LEVELS[boardstatus.UNKNOWN])

    def test_with_status_keeps_what_the_runner_sent(self):
        [view] = boardstatus.with_status([board(power_controller="Hub_Local0",
                                                power_port=2)])
        self.assertEqual("Hub_Local0", view["power_controller"])
        self.assertEqual(boardstatus.UNKNOWN, view["status"])
        self.assertTrue(view["status_detail"])

    def test_the_summary_counts_by_status(self):
        self.assertEqual(
            "no board registered on this target", boardstatus.summary([])
        )
        text = boardstatus.summary([
            board(firmware_revision="git-a"),
            board(alias="UT_Local1", serial=SECOND, present=False),
        ])
        self.assertEqual("2 boards: 1 ready, 1 offline", text)


# ----- editing the registry file -------------------------------------------


class RegistryEditTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.path = self.root / "hardware.yaml"
        self.path.write_text(HARDWARE, encoding="utf-8")

    def tearDown(self):
        self._dir.cleanup()

    def apply(self, **fields):
        return registryedit.apply_edit(
            self.path, "Host_Local", BoardEdit(**fields)
        )

    def text(self):
        return self.path.read_text(encoding="utf-8")

    def test_a_registered_board_appears_and_nothing_else_moves(self):
        registry = self.apply(
            action="add", alias="UT_Local2", serial=THIRD,
            power_controller="Hub_Local0", power_port=3,
        )
        self.assertEqual(THIRD, registry.get("UT_Local2").identity["serial"])
        self.assertEqual(3, registry.get("UT_Local2").power["port"])
        text = self.text()
        self.assertIn("  UT_Local2:", text)
        self.assertIn("# The bench this registry describes. Keep these notes.",
                      text)
        self.assertIn("# The switchable hub the boards hang off.", text)
        self.assertIn("# A note at the end of the file, which no edit may "
                      "take away.", text)
        # And taking it off again leaves the file exactly as it started: the
        # two edits touched that board's own lines and nothing else, down to
        # the whitespace.
        self.apply(action="remove", alias="UT_Local2")
        self.assertEqual(HARDWARE, self.text())

    def test_a_board_with_no_switchable_port_is_registered_without_one(self):
        registry = self.apply(action="add", alias="UT_Local2", serial=THIRD)
        self.assertEqual({}, dict(registry.get("UT_Local2").power))
        self.assertNotIn("power:\n      controller: Hub_Local0\n      port: 3",
                         self.text())

    def test_an_all_digit_serial_stays_a_string(self):
        registry = self.apply(action="add", alias="UT_Local2",
                              serial="0" * 32)
        self.assertEqual("0" * 32, registry.get("UT_Local2").identity["serial"])

    def test_unregistering_removes_that_board_and_keeps_the_comments(self):
        registry = self.apply(action="remove", alias="UT_Local1")
        self.assertNotIn("UT_Local1", registry.resources)
        self.assertIn("UT_Local0", registry.resources)
        text = self.text()
        self.assertNotIn("UT_Local1", text)
        self.assertNotIn(SECOND, text)
        # The comment above it and the note below it are the file's, not the
        # board's: removing a board does not quietly delete prose.
        self.assertIn("# The second board sits on no switchable port.", text)
        self.assertIn("# A note at the end of the file", text)
        self.assertIn("# The switchable hub the boards hang off.", text)

    def test_the_previous_contents_are_kept_as_a_backup(self):
        self.apply(action="remove", alias="UT_Local1")
        backup = self.root / ("hardware.yaml" + registryedit.BACKUP_SUFFIX)
        self.assertEqual(HARDWARE, backup.read_text(encoding="utf-8"))

    def test_no_temporary_or_candidate_file_is_left_behind(self):
        self.apply(action="add", alias="UT_Local2", serial=THIRD)
        self.assertEqual(
            {"hardware.yaml", "hardware.yaml" + registryedit.BACKUP_SUFFIX},
            {path.name for path in self.root.iterdir()},
        )

    def refused(self, status, pattern, **fields):
        with self.assertRaises(RegistryEditError) as raised:
            self.apply(**fields)
        self.assertEqual(status, raised.exception.status)
        self.assertRegex(raised.exception.detail, pattern)
        # Refused means nothing was written, not written and put back.
        self.assertEqual(HARDWARE, self.text())

    def test_a_change_the_registry_already_contradicts_is_refused(self):
        self.refused(409, "already registered", action="add",
                     alias="UT_Local0", serial=THIRD)
        self.refused(409, "already registered as UT_Local1", action="add",
                     alias="UT_Local2", serial=SECOND)
        self.refused(409, "already registered on Hub_Local0 port 0",
                     action="add", alias="UT_Local2", serial=THIRD,
                     power_controller="Hub_Local0", power_port=0)
        self.refused(404, "not a USB power controller", action="add",
                     alias="UT_Local2", serial=THIRD,
                     power_controller="UT_Local0", power_port=1)

    def test_an_unknown_host_is_refused(self):
        with self.assertRaises(RegistryEditError) as raised:
            registryedit.apply_edit(
                self.path, "Host_Elsewhere",
                BoardEdit(action="add", alias="UT_Local2", serial=THIRD),
            )
        self.assertEqual(404, raised.exception.status)
        self.assertEqual(HARDWARE, self.text())

    def test_a_board_the_registry_has_not_got_cannot_be_unregistered(self):
        self.refused(404, "not registered", action="remove", alias="UT_Local9")

    def test_only_an_ubertooth_is_managed_here(self):
        self.refused(409, "only an Ubertooth is managed here", action="remove",
                     alias="Hub_Local0")

    def test_a_board_another_resource_depends_on_is_refused(self):
        # The host names UT_Local0 as its default Ubertooth, so removing it
        # would leave a registry that does not load. The refusal says which
        # resource to change, rather than leaving the loader to.
        self.refused(409, "names UT_Local0 as a default", action="remove",
                     alias="UT_Local0")

    def test_a_board_a_programmer_is_wired_to_is_refused(self):
        programmer = (
            "  Prog_Local0:\n"
            "    type: Debug programmer\n"
            "    attached to: Host_Local\n"
            "    identity:\n"
            '      serial: "PROG0001"\n'
            "    programs: UT_Local1\n"
        )
        marker = "\n# A note at the end of the file"
        self.path.write_text(
            HARDWARE.replace(marker, "\n" + programmer + marker),
            encoding="utf-8",
        )
        with self.assertRaises(RegistryEditError) as raised:
            self.apply(action="remove", alias="UT_Local1")
        self.assertEqual(409, raised.exception.status)
        self.assertIn("debug programmer", raised.exception.detail)

    def test_a_serial_that_is_not_one_is_refused_before_the_file_is_read(self):
        for serial in ("", "not-hex", "ab", "AAAA0000AAAA0000AAAA0000AAAA0000",
                       "a" * 65):
            with self.subTest(serial=serial):
                with self.assertRaises(RegistryEditError):
                    BoardEdit(action="add", alias="UT_Local2", serial=serial)

    def test_an_alias_that_is_not_one_is_refused(self):
        for alias in ("", "0UT", "UT Local2", "../UT", "UT" * 40):
            with self.subTest(alias=alias):
                with self.assertRaises(RegistryEditError):
                    BoardEdit(action="add", alias=alias, serial=THIRD)

    def test_half_a_power_mapping_is_refused(self):
        with self.assertRaises(RegistryEditError):
            BoardEdit(action="add", alias="UT_Local2", serial=THIRD,
                      power_controller="Hub_Local0")
        with self.assertRaises(RegistryEditError):
            BoardEdit(action="add", alias="UT_Local2", serial=THIRD,
                      power_port=1)

    def test_a_port_the_hub_has_not_got_is_refused_by_the_registry(self):
        # The acroname hub has ports 0-7, so port 9 makes a registry that
        # does not load; the file is left alone.
        with self.assertRaises(RegistryEditError) as raised:
            self.apply(action="add", alias="UT_Local2", serial=THIRD,
                       power_controller="Hub_Local0", power_port=9)
        self.assertEqual(HARDWARE, self.text())
        self.assertIn("port", raised.exception.detail)

    def test_a_file_that_is_not_a_registry_is_not_edited(self):
        self.path.write_text("not: a registry\n", encoding="utf-8")
        with self.assertRaises(RegistryEditError) as raised:
            self.apply(action="add", alias="UT_Local2", serial=THIRD)
        self.assertEqual(503, raised.exception.status)
        self.assertEqual("not: a registry\n", self.text())

    def test_a_registry_that_cannot_be_read_names_no_path(self):
        self.path.unlink()
        with self.assertRaises(RegistryEditError) as raised:
            self.apply(action="add", alias="UT_Local2", serial=THIRD)
        self.assertEqual(503, raised.exception.status)
        self.assertNotIn(str(self.path), raised.exception.detail)
        self.assertNotIn(str(self.root), raised.exception.detail)

    def test_an_edit_whose_result_is_not_what_it_meant_is_dropped(self):
        # The surgical edit is text work on a format with significant
        # indentation, so its result is compared with the mapping the change
        # intended rather than trusted. Here the text step is made to produce
        # something else; nothing is kept and the file does not change.
        with mock.patch.object(
            registryedit, "edited_text",
            return_value="resources:\n  Host_Local:\n    type: Host\n"
                         f"    platform: {PLATFORM}\n",
        ):
            with self.assertRaises(RegistryEditError) as raised:
                self.apply(action="remove", alias="UT_Local1")
        self.assertEqual(503, raised.exception.status)
        self.assertIn("did not match what the change intended",
                      raised.exception.detail)
        self.assertEqual(HARDWARE, self.text())

    def test_a_registry_that_does_not_load_once_in_place_is_rolled_back(self):
        # It loaded from identical text a moment earlier, so this stands for
        # the filesystem or another writer getting in the way. What was there
        # comes back, and the caller is told.
        real = FileResourceRegistry.load
        calls = []

        def load(path):
            calls.append(Path(path))
            if len(calls) > 1:
                raise RegistryError("the registry went missing")
            return real(path)

        with mock.patch.object(FileResourceRegistry, "load", staticmethod(load)):
            with self.assertRaises(RegistryEditError) as raised:
                self.apply(action="remove", alias="UT_Local1")
        self.assertEqual(503, raised.exception.status)
        self.assertIn("previous one was restored", raised.exception.detail)
        self.assertEqual(HARDWARE, self.text())
        registry = FileResourceRegistry.load(self.path)
        self.assertIn("UT_Local1", registry.resources)

    def test_a_registry_with_no_board_yet_gains_one(self):
        self.path.write_text(textwrap.dedent(f"""\
            resources:
              Host_Local:
                type: Host
                platform: {PLATFORM}
                roles: [test target]
            """), encoding="utf-8")
        registry = self.apply(action="add", alias="UT_Local0", serial=FIRST)
        self.assertEqual(FIRST, registry.get("UT_Local0").identity["serial"])

    def test_the_suggested_alias_is_the_next_free_one(self):
        self.assertEqual(
            "UT_Local0", registryedit.suggested_alias("Host_Local", [])
        )
        self.assertEqual(
            "UT_Local2",
            registryedit.suggested_alias(
                "Host_Local", ["UT_Local0", "ut_local1"]
            ),
        )
        # A host alias that says nothing beyond its own name keeps it whole.
        self.assertEqual("UT_lab0", registryedit.suggested_alias("lab", []))

    def test_the_free_ports_of_each_hub_are_offered(self):
        registry = FileResourceRegistry.load(self.path)
        [controller] = registryedit.power_controllers(registry, "Host_Local")
        self.assertEqual("Hub_Local0", controller["alias"])
        # Port 0 has UT_Local0 on it, so it is not offered.
        self.assertEqual([1, 2, 3, 4, 5, 6, 7], controller["ports"])

    def test_a_registry_may_not_grow_past_the_bound(self):
        edit = BoardEdit(action="add", alias="UT_Local2", serial=THIRD)
        raw = {"resources": {
            f"UT_{index}": {"type": "Ubertooth"}
            for index in range(registryedit.MAX_BOARDS)
        }}
        raw["resources"]["Host_Local"] = {"type": "Host"}
        with self.assertRaises(RegistryEditError) as raised:
            registryedit.intended_resources(raw, "Host_Local", edit)
        self.assertEqual(409, raised.exception.status)


class RefusalWithoutAHostTests(unittest.TestCase):
    """A registry whose host is not a Host refuses an add against it."""

    def test_a_host_that_is_not_one_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "hardware.yaml"
            path.write_text(HARDWARE, encoding="utf-8")
            with self.assertRaises(RegistryEditError) as raised:
                registryedit.apply_edit(
                    path, "Hub_Local0",
                    BoardEdit(action="add", alias="UT_Local2", serial=THIRD),
                )
            self.assertEqual(404, raised.exception.status)
            self.assertIn("not a Host", raised.exception.detail)


# ----- the runner --------------------------------------------------------


class RunnerBoardTests(unittest.IsolatedAsyncioTestCase):
    """The runner's own board listing and registry edits, over a real socket."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.fixture = RunnerFixture(self.root)
        self.fixture.hardware.write_text(HARDWARE, encoding="utf-8")
        self.leases = self.root / "leases"
        self.leases.mkdir()
        self._environment = mock.patch.dict(
            os.environ, {leases.LEASE_DIR_ENV: str(self.leases)}
        )
        self._environment.start()

    async def asyncTearDown(self):
        self._environment.stop()
        self._dir.cleanup()

    def service(self, url, **overrides):
        enumerator = overrides.pop("enumerator", StubEnumerator([FIRST]))
        config = self.fixture.config(url, host="Host_Local", **overrides)
        return RunnerService(config, enumerator=enumerator, log=lambda text: None)

    async def registered(self, controller, **overrides):
        service = self.service(controller.url, **overrides)
        task = asyncio.create_task(service.run_forever())
        register = await controller.next("register", skip=("heartbeat",))
        return service, task, register

    async def stop(self, service, task):
        service.request_stop()
        await asyncio.wait_for(task, TIMEOUT_S)

    async def test_registration_carries_each_board_with_its_own_facts(self):
        async with StubController() as controller:
            service, task, register = await self.registered(controller)
            try:
                boards = {entry["alias"]: entry for entry in register["boards"]}
                self.assertEqual({"UT_Local0", "UT_Local1"}, set(boards))
                # FIRST is on USB, SECOND is not.
                self.assertIs(True, boards["UT_Local0"]["present"])
                self.assertIs(False, boards["UT_Local1"]["present"])
                self.assertEqual("Hub_Local0", boards["UT_Local0"]["power_controller"])
                self.assertEqual(0, boards["UT_Local0"]["power_port"])
                self.assertIsNone(boards["UT_Local1"]["power_controller"])
                # No run has read either board yet.
                self.assertIsNone(boards["UT_Local0"]["firmware_revision"])
                self.assertFalse(boards["UT_Local0"]["held"])
            finally:
                await self.stop(service, task)

    async def test_a_leased_board_is_reported_held_with_its_holder(self):
        held = leases.acquire(
            [leases.Board("UT_Local0", FIRST)],
            leases.holder_info(run_id="run-x", owner="agent-1", purpose="test"),
            directory=self.leases,
        )
        try:
            async with StubController() as controller:
                service, task, register = await self.registered(controller)
                try:
                    entry = next(item for item in register["boards"]
                                 if item["alias"] == "UT_Local0")
                    self.assertTrue(entry["held"])
                    self.assertIn("agent-1", entry["holder"])
                    status, why = boardstatus.board_status(entry)
                    self.assertEqual(boardstatus.BUSY, status)
                    self.assertIn("agent-1", why)
                finally:
                    await self.stop(service, task)
        finally:
            held.release()

    async def test_the_heartbeat_carries_the_board_list_too(self):
        async with StubController() as controller:
            service, task, _ = await self.registered(controller)
            try:
                beat = await controller.next("heartbeat", skip=())
                self.assertEqual(
                    {"UT_Local0", "UT_Local1"},
                    {entry["alias"] for entry in beat["boards"]},
                )
            finally:
                await self.stop(service, task)

    async def test_a_slow_scan_does_not_hold_up_the_heartbeat(self):
        # ioreg's own timeout is longer than a heartbeat interval, so a
        # heartbeat that waited for a fresh scan could arrive late enough
        # for the controller to call the target offline mid-run. It carries
        # the last list read and refreshes on its own.
        scanning = asyncio.Event()

        class Slow:
            def serials(self):
                import time as clock
                scanning.set()
                clock.sleep(5)
                return frozenset({FIRST})

            def devices(self):
                return ()

        async with StubController() as controller:
            service, task, register = await self.registered(
                controller, enumerator=Slow()
            )
            try:
                # Registration read it once (slowly, before the connection
                # was serving); every heartbeat after that is prompt.
                self.assertEqual(
                    {"UT_Local0", "UT_Local1"},
                    {entry["alias"] for entry in register["boards"]},
                )
                scanning.clear()
                start = time.monotonic()
                await controller.next("heartbeat", skip=())
                await controller.next("heartbeat", skip=())
                waited = time.monotonic() - start
                self.assertLess(waited, 3.0, f"heartbeats took {waited:.1f}s")
            finally:
                await self.stop(service, task)

    async def test_a_registry_that_will_not_load_costs_the_list_not_the_target(self):
        self.fixture.hardware.write_text("resources: broken\n", encoding="utf-8")
        async with StubController() as controller:
            service, task, register = await self.registered(controller)
            try:
                self.assertEqual([], register["boards"])
                self.assertEqual("Host_Local", register["host"])
            finally:
                await self.stop(service, task)

    async def test_list_devices_marks_each_attached_board_registered_or_new(self):
        attached = [
            UsbDevice(FIRST, "1d50", "6002", "3-2.1"),
            UsbDevice(THIRD, "1d50", "6002", "3-2.2"),
            UsbDevice("", "1d50", "6003", "3-2.3"),
        ]
        async with StubController() as controller:
            service, task, _ = await self.registered(
                controller, enumerator=StubEnumerator([FIRST, THIRD], attached)
            )
            try:
                await controller.send(
                    encode_controller_message("list_devices", request_id="d1")
                )
                answer = await controller.next("devices")
                by_path = {item["path"]: item for item in answer["devices"]}
                self.assertEqual("UT_Local0", by_path["3-2.1"]["alias"])
                self.assertIsNone(by_path["3-2.2"]["alias"])
                # A board in its DFU bootloader reports no serial, so it is
                # listed and cannot be matched to a registered board.
                self.assertEqual("", by_path["3-2.3"]["serial"])
                self.assertEqual("6003", by_path["3-2.3"]["product"])
                self.assertIsNone(answer["error"])
                self.assertEqual(
                    ["Hub_Local0"],
                    [item["alias"] for item in answer["power_controllers"]],
                )
                self.assertEqual("UT_Local2", answer["suggested_alias"])
            finally:
                await self.stop(service, task)

    async def test_list_devices_says_why_it_found_nothing(self):
        class Broken:
            def serials(self):
                raise RuntimeError("sysfs is not mounted")

            def devices(self):
                raise RuntimeError("sysfs is not mounted")

        async with StubController() as controller:
            service, task, _ = await self.registered(
                controller, enumerator=Broken()
            )
            try:
                await controller.send(
                    encode_controller_message("list_devices", request_id="d1")
                )
                answer = await controller.next("devices")
                self.assertEqual([], answer["devices"])
                self.assertIn("sysfs is not mounted", answer["error"])
            finally:
                await self.stop(service, task)

    async def ask(self, controller, *, skip=("heartbeat",), **fields):
        await controller.send(encode_controller_message(
            "update_registry", **{"request_id": "u1", **fields}
        ))
        return await controller.next(skip=skip)

    async def test_a_board_is_registered_and_the_new_list_comes_back(self):
        async with StubController() as controller:
            service, task, _ = await self.registered(
                controller,
                enumerator=StubEnumerator([FIRST, THIRD]),
            )
            try:
                answer = await self.ask(
                    controller, action="add", alias="UT_Local2", serial=THIRD,
                    power_controller="Hub_Local0", power_port=3,
                )
                self.assertEqual("registry_updated", answer["type"])
                self.assertEqual(("add", "UT_Local2"),
                                 (answer["action"], answer["alias"]))
                boards = {entry["alias"]: entry for entry in answer["boards"]}
                self.assertEqual(
                    {"UT_Local0", "UT_Local1", "UT_Local2"}, set(boards)
                )
                self.assertIs(True, boards["UT_Local2"]["present"])
                self.assertEqual(3, boards["UT_Local2"]["power_port"])
                # And the file on the target really says so.
                registry = FileResourceRegistry.load(self.fixture.hardware)
                self.assertEqual(
                    THIRD, registry.get("UT_Local2").identity["serial"]
                )
            finally:
                await self.stop(service, task)

    async def test_a_board_is_unregistered_and_stays_on_usb(self):
        async with StubController() as controller:
            service, task, _ = await self.registered(controller)
            try:
                answer = await self.ask(
                    controller, action="remove", alias="UT_Local1",
                    serial=None, power_controller=None, power_port=None,
                )
                self.assertEqual("registry_updated", answer["type"])
                self.assertEqual(
                    {"UT_Local0"},
                    {entry["alias"] for entry in answer["boards"]},
                )
                # Nothing was sent to the board: the enumerator still has
                # every serial it had, and it was only ever read.
                self.assertEqual([FIRST], answer["ubertooth_serials"])
            finally:
                await self.stop(service, task)

    async def test_a_change_is_refused_while_a_run_is_active(self):
        async with StubController() as controller:
            service, task, _ = await self.registered(controller)
            try:
                await controller.send(encode_controller_message(
                    "run", request_id="r1", ref="HEAD", commit=None,
                    plan="local", suite="fast", tests=[], build=False,
                    flash_build=True, with_firmware=False, with_btbr=False,
                    with_python=False, bootloader="record",
                ))
                await controller.next("run_accepted", skip=("heartbeat", "event"))
                answer = await self.ask(
                    controller, action="remove", alias="UT_Local1",
                    serial=None, power_controller=None, power_port=None,
                    skip=("heartbeat", "event"),
                )
                self.assertEqual("registry_refused", answer["type"])
                self.assertEqual(409, answer["status"])
                self.assertIn("is active on this target", answer["reason"])
                self.assertEqual(HARDWARE, self.fixture.hardware.read_text())
            finally:
                await self.stop(service, task)

    async def test_a_leased_board_is_not_unregistered(self):
        held = leases.acquire(
            [leases.Board("UT_Local1", SECOND)],
            leases.holder_info(run_id="run-x", owner="agent-1", purpose="test"),
            directory=self.leases,
        )
        try:
            async with StubController() as controller:
                service, task, _ = await self.registered(controller)
                try:
                    answer = await self.ask(
                        controller, action="remove", alias="UT_Local1",
                        serial=None, power_controller=None, power_port=None,
                    )
                    self.assertEqual("registry_refused", answer["type"])
                    self.assertEqual(409, answer["status"])
                    self.assertIn("agent-1", answer["reason"])
                    self.assertEqual(
                        HARDWARE, self.fixture.hardware.read_text()
                    )
                finally:
                    await self.stop(service, task)
        finally:
            held.release()

    async def test_a_refusal_names_no_path_on_the_target(self):
        self.fixture.hardware.unlink()
        async with StubController() as controller:
            service, task, _ = await self.registered(controller)
            try:
                answer = await self.ask(
                    controller, action="add", alias="UT_Local2", serial=THIRD,
                    power_controller=None, power_port=None,
                )
                self.assertEqual("registry_refused", answer["type"])
                self.assertNotIn(str(self.root), answer["reason"])
                self.assertNotIn("hardware.yaml", answer["reason"])
            finally:
                await self.stop(service, task)

    async def test_a_change_the_registry_refuses_leaves_the_file_alone(self):
        async with StubController() as controller:
            service, task, _ = await self.registered(controller)
            try:
                answer = await self.ask(
                    controller, action="add", alias="UT_Local0", serial=THIRD,
                    power_controller=None, power_port=None,
                )
                self.assertEqual("registry_refused", answer["type"])
                self.assertEqual(409, answer["status"])
                self.assertEqual(HARDWARE, self.fixture.hardware.read_text())
            finally:
                await self.stop(service, task)

    async def test_an_update_the_protocol_refuses_is_a_protocol_error(self):
        async with StubController() as controller:
            service, task, _ = await self.registered(controller)
            try:
                # Hand-built, because encoding it here would be refused too.
                await controller.send(json.dumps({
                    "version": PROTOCOL_VERSION, "type": "update_registry",
                    "request_id": "u1", "action": "add", "alias": "UT_Local2",
                    "serial": None, "power_controller": None,
                    "power_port": None,
                }))
                answer = await controller.next("protocol_error")
                self.assertIn("serial", answer["reason"])
                self.assertEqual(HARDWARE, self.fixture.hardware.read_text())
            finally:
                await self.stop(service, task)

    def test_a_flash_record_teaches_the_runner_a_board_revision(self):
        service = RunnerService(
            self.fixture.config("ws://127.0.0.1:1/ws/runner", host="Host_Local"),
            enumerator=StubEnumerator([FIRST]), log=lambda text: None,
        )
        service._learn_revision({
            "event_type": "build_flash_board_finished", "status": "passed",
            "details": {"resource": "UT_Local0", "serial": FIRST,
                        "before": "git-0000000", "after": "git-abcdef0"},
        })
        [entry] = [item for item in service._board_states([FIRST])
                   if item["alias"] == "UT_Local0"]
        self.assertEqual("git-abcdef0", entry["firmware_revision"])
        self.assertEqual(boardstatus.READY, boardstatus.board_status(entry)[0])
        # A record of another kind, or one without the fields, teaches
        # nothing rather than raising inside the event stream.
        for record in (
            {"event_type": "test_started", "details": {}},
            {"event_type": "build_flash_board_finished", "details": "text"},
            {"event_type": "build_flash_board_finished",
             "details": {"serial": FIRST, "before": "", "after": ""}},
        ):
            service._learn_revision(record)
        self.assertEqual({FIRST: "git-abcdef0"}, service._revisions)


# ----- the page ------------------------------------------------------------


class PageTests(unittest.TestCase):
    """What the Targets card and the Manage boards panel are made of.

    A JS-free reading of the three assets: the page has no build step and
    loads nothing from anywhere else, so its markup, its wiring and its
    styles can be read as text.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        cls.css = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    def test_the_panel_is_a_labelled_section_with_a_close_button(self):
        self.assertRegex(
            self.html,
            r'<section id="manage" class="panel panel-manage" hidden '
            r'aria-labelledby="manage-heading">',
        )
        # A row of its own across the page, so the scan's table has the width
        # for a 32-digit serial, a bus path and the button that acts on the
        # row without any of them folding or going out of sight.
        self.assertIn('"manage manage"', self.css)
        self.assertIn(".panel-manage { grid-area: manage; }", self.css)
        # Its heading takes focus when it opens and gives it back on close,
        # so the keyboard follows the panel.
        self.assertRegex(self.html, r'id="manage-heading" tabindex="-1"')
        self.assertIn('id="manage-close"', self.html)
        self.assertIn('$("manage-heading").focus();', self.js)
        self.assertIn('$("manage-close").addEventListener("click", closeManage);',
                      self.js)
        self.assertRegex(
            self.js, r'if \(pressed\.key === "Escape"\) closeManage\(\);'
        )

    def test_every_control_of_the_panel_has_a_label(self):
        for control, label in (("add-alias", "Alias"),
                               ("add-power", "Switchable port")):
            with self.subTest(control):
                self.assertIn(f'<label for="{control}">{label}</label>',
                              self.html)
        # The two per-row buttons are named for the board they act on, so a
        # screen reader does not read a column of bare "Add" and
        # "Unregister".
        self.assertIn('"aria-label": `Unregister ${board.alias} from ${manage.host}`',
                      self.js)
        self.assertIn("`Register the board with serial ${serial}`", self.js)
        self.assertIn('"aria-label": `Manage the boards of ${target.host}`',
                      self.js)

    def test_what_the_panel_is_doing_is_announced(self):
        self.assertRegex(
            self.html,
            r'<p id="manage-status" class="hint" role="status" aria-live="polite">',
        )
        self.assertIn('manageStatus("Reading the target\'s USB bus…")', self.js)

    def test_a_board_status_is_text_and_a_symbol_not_color_alone(self):
        # The symbol is decoration (aria-hidden) and the status word is the
        # content, as every other badge on the page is built.
        badge = self.js[self.js.index("function boardBadge(board)"):
                        self.js.index("function timeElement(")]
        self.assertIn('el("span", { "aria-hidden": "true", text: icon + " " })',
                      badge)
        self.assertIn("status)", badge)
        self.assertIn("`status level-${level}`", badge)
        # And the sentence saying why, which the controller derived.
        self.assertIn("String(board.status_detail || \"\")", self.js)

    def test_the_three_levels_are_the_three_colors(self):
        for level, token in (("ready", "--ok-fg"), ("attention", "--run-fg"),
                             ("offline", "--bad-fg")):
            with self.subTest(level):
                rule = re.search(
                    rf"\.level-{level} \{{(.*?)\}}", self.css, re.S
                )
                self.assertIsNotNone(rule, f"no .level-{level} rule")
                self.assertIn(token, rule.group(1))

    def test_the_page_asks_the_two_new_endpoints(self):
        self.assertIn(
            '"GET", `/api/targets/${encodeURIComponent(manage.host)}/devices`',
            self.js,
        )
        self.assertIn(
            '"POST", `/api/targets/${encodeURIComponent(manage.host)}/boards`',
            self.js,
        )
        # add and remove are the only two actions it sends.
        self.assertIn('action: "add",', self.js)
        self.assertIn('{ action: "remove", alias },', self.js)

    def test_a_board_with_no_serial_cannot_be_added_and_the_page_says_why(self):
        detected = self.js[self.js.index("function renderDetected()"):
                           self.js.index("async function detectBoards()")]
        self.assertIn("disabled: manage.busy || !serial,", detected)
        self.assertIn("DFU", detected)

    def test_autodetect_says_it_opens_nothing(self):
        self.assertIn("No device is opened and nothing is sent to one",
                      self.html)

    def test_the_page_still_loads_only_its_own_assets(self):
        self.assertNotRegex(self.html, r"\son[a-z]+\s*=")
        self.assertNotRegex(self.html, r"\sstyle\s*=")
        for text in (self.html, self.js, self.css):
            self.assertNotRegex(text, r"(?i)(https?:)?//[a-z0-9.-]+\.[a-z]{2,}/")


# ----- what the review round found -----------------------------------------


class EditedFileShapeTests(unittest.TestCase):
    """Registries whose shape the surgical edit has to survive."""

    def edit(self, text, action="add", **fields):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "hardware.yaml"
            path.write_text(text, encoding="utf-8")
            registryedit.apply_edit(
                path, "Host_Local",
                BoardEdit(action=action, **({"serial": THIRD} if action == "add"
                                            else {}), **fields),
            )
            return path.read_text(encoding="utf-8")

    def test_a_resources_mapping_that_is_not_the_last_key_still_edits(self):
        # The parser's end mark for the resources mapping is the next
        # top-level key's own line, so a block has to end at the last line
        # indented past `resources:`; otherwise an add lands under that key
        # and a remove deletes it.
        text = HARDWARE + textwrap.dedent("""\
            notes:
              bench: the one by the window
            """)
        added = self.edit(text, alias="UT_Local2")
        self.assertIn("notes:", added)
        self.assertIn("  bench: the one by the window", added)
        registry = None
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "hardware.yaml"
            path.write_text(added, encoding="utf-8")
            registry = FileResourceRegistry.load(path)
        self.assertEqual(THIRD, registry.get("UT_Local2").identity["serial"])
        # And a removal takes only the board.
        removed = self.edit(text, action="remove", alias="UT_Local1")
        self.assertIn("notes:", removed)
        self.assertIn("  bench: the one by the window", removed)
        self.assertNotIn("UT_Local1", removed)

    def test_a_crlf_registry_keeps_its_line_endings(self):
        text = HARDWARE.replace("\n", "\r\n")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "hardware.yaml"
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            registryedit.apply_edit(
                path, "Host_Local",
                BoardEdit(action="add", alias="UT_Local2", serial=THIRD),
            )
            with path.open("r", encoding="utf-8", newline="") as stream:
                added = stream.read()
        self.assertNotIn("\n", added.replace("\r\n", ""))
        self.assertIn('\r\n      serial: "' + THIRD + '"\r\n', added)

    def test_the_scratch_files_are_this_process_own(self):
        # Two runners editing one registry must not find each other's
        # half-written file under the same name.
        path = Path("/tmp/hardware.yaml")
        self.assertIn(str(os.getpid()), registryedit._temporary(path, ".tmp").name)
        self.assertIn(
            str(os.getpid()), registryedit._temporary(path, ".candidate").name
        )

    def test_a_controller_with_an_unknown_driver_costs_only_itself(self):
        another = (
            "  Hub_Local1:\n"
            "    type: USB power controller\n"
            "    attached to: Host_Local\n"
            "    identity:\n"
            '      serial: "0x87654321"\n'
            "    driver: a hub this harness does not switch\n"
        )
        marker = "\n# A note at the end of the file"
        text = HARDWARE.replace(marker, "\n" + another + marker)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "hardware.yaml"
            path.write_text(text, encoding="utf-8")
            registry = FileResourceRegistry.load(path)
        listed = registryedit.power_controllers(registry, "Host_Local")
        self.assertEqual(["Hub_Local0", "Hub_Local1"],
                         [item["alias"] for item in listed])
        # The hub whose driver the harness does not switch offers no port,
        # and the one it does still offers its own.
        self.assertEqual([], listed[1]["ports"])
        self.assertEqual([1, 2, 3, 4, 5, 6, 7], listed[0]["ports"])


class HolderTextTests(unittest.TestCase):
    """What a board's holder line may say.

    It travels to a browser and into a refusal, so it names who and since
    when, and never the command line a lease by hand records as its purpose.
    """

    def test_the_purpose_a_manual_lease_records_is_not_reported(self):
        from ubertooth_harness.runner_service import _holder_line

        holder = leases.holder_info(
            run_id=None, owner="agent-1",
            purpose="lease: /opt/example/build/bin/ubertooth-util -v",
        )
        line = _holder_line(holder, {})
        self.assertIn("agent-1", line)
        self.assertNotIn("/opt/example", line)
        self.assertNotIn("ubertooth-util", line)
        self.assertIn(f"pid {os.getpid()}", line)

    def test_a_bench_lock_is_named_beside_the_lease(self):
        from ubertooth_harness.runner_service import _holder_line

        line = _holder_line(
            leases.holder_info(run_id="run-x", owner="agent-1", purpose=""),
            {"held": True, "holder": "someone else"},
        )
        self.assertIn("run run-x", line)
        self.assertIn("a bench lock (someone else)", line)

    def test_a_free_board_has_no_holder_line(self):
        from ubertooth_harness.runner_service import _holder_line

        self.assertEqual("", _holder_line({}, {}))


class LeaseHeldEditTests(unittest.IsolatedAsyncioTestCase):
    """Unregistering a board holds its lease, so it cannot slip through."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.fixture = RunnerFixture(self.root)
        self.fixture.hardware.write_text(HARDWARE, encoding="utf-8")
        self.leases = self.root / "leases"
        self.leases.mkdir()
        self._environment = mock.patch.dict(
            os.environ, {leases.LEASE_DIR_ENV: str(self.leases)}
        )
        self._environment.start()

    def tearDown(self):
        self._environment.stop()
        self._dir.cleanup()

    def service(self):
        return RunnerService(
            self.fixture.config("ws://127.0.0.1:1/ws/runner", host="Host_Local"),
            enumerator=StubEnumerator([FIRST]), log=lambda text: None,
        )

    def test_the_board_is_leased_while_it_is_unregistered(self):
        service = self.service()
        edit = BoardEdit(action="remove", alias="UT_Local1")
        inside = {}

        real = registryedit.apply_edit

        def watched(path, host, edit):
            # While the edit runs, nobody else can take the board.
            with self.assertRaises(leases.LeaseError):
                leases.acquire(
                    [leases.Board("UT_Local1", SECOND)],
                    leases.holder_info(run_id=None, owner="other", purpose=""),
                    directory=self.leases,
                )
            inside["held"] = True
            return real(path, host, edit)

        with mock.patch.object(registryedit, "apply_edit", watched):
            answer, fields = service._apply_edit(edit)
        self.assertEqual("registry_updated", answer)
        self.assertTrue(inside.get("held"))
        # And it is given back afterwards.
        leases.acquire(
            [leases.Board("UT_Local1", SECOND)],
            leases.holder_info(run_id=None, owner="other", purpose=""),
            directory=self.leases,
        ).release()

    def test_a_board_somebody_holds_is_not_unregistered(self):
        held = leases.acquire(
            [leases.Board("UT_Local1", SECOND)],
            leases.holder_info(run_id="run-x", owner="agent-1", purpose="test"),
            directory=self.leases,
        )
        try:
            service = self.service()
            answer, fields = service._apply_edit(
                BoardEdit(action="remove", alias="UT_Local1")
            )
            self.assertEqual("registry_refused", answer)
            self.assertEqual(409, fields["status"])
            self.assertIn("in use", fields["reason"])
            self.assertEqual(HARDWARE, self.fixture.hardware.read_text())
        finally:
            held.release()

    def test_a_lease_directory_that_cannot_be_read_fails_closed(self):
        service = self.service()
        with mock.patch.object(
            leases, "acquire", side_effect=OSError("permission denied")
        ):
            answer, fields = service._apply_edit(
                BoardEdit(action="remove", alias="UT_Local1")
            )
        self.assertEqual("registry_refused", answer)
        self.assertEqual(503, fields["status"])
        self.assertIn("cannot hold", fields["reason"])
        self.assertEqual(HARDWARE, self.fixture.hardware.read_text())

    def test_registering_a_board_takes_no_lease(self):
        # There is nothing to hold: the alias does not exist yet.
        service = self.service()
        with mock.patch.object(
            leases, "acquire", side_effect=AssertionError("took a lease")
        ):
            answer, _ = service._apply_edit(
                BoardEdit(action="add", alias="UT_Local2", serial=THIRD)
            )
        self.assertEqual("registry_updated", answer)

    def test_one_board_this_protocol_cannot_describe_costs_its_own_line(self):
        service = self.service()
        real = service._registry

        def registry():
            loaded = real()
            # A serial too long for the schema to carry.
            object.__setattr__(
                loaded.resources["UT_Local1"], "identity",
                {"serial": "f" * 5000},
            )
            return loaded

        with mock.patch.object(service, "_registry", registry):
            states = service._board_states([FIRST])
        self.assertEqual(["UT_Local0"], [item["alias"] for item in states])

    def test_the_revision_map_is_bounded(self):
        service = self.service()
        for index in range(300):
            service._learn_revision({
                "event_type": "build_flash_board_finished",
                "details": {"serial": f"{index:032x}", "after": "git-1"},
            })
        self.assertLessEqual(len(service._revisions), 256)
