"""Failed capture files: the cases, and the regressions each one catches."""

import io
import struct
import sys
import time
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import (
    capture_failure_cases, capturefile, dfuimage, refusal, rxdump, rxreplay,
)
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import StateCase
from tests.test_sequencer import CollectSink, run_contract
from tests.test_tool_cases import failed_names

NAME = "Failed capture files"
NOTE = "note: UBERTOOTH_TEST_NO_DEVICES is set, so no board is selected\n"
NO_BOARD = "error: no Ubertooth devices found\n"
LINKTYPE = capturefile.LINKTYPE_BLUETOOTH_BREDR_BB
# The three capture options, and which writer each one is.
WRITERS = {"r": "pcapng", "q": "pcap", "c": "pcap", "d": "dump"}
# The fixture file each replay variant is written as.
VARIANT_OF = {name: variant
              for variant, name in capture_failure_cases.FIXTURES.items()}


# --- the files the writers make ----------------------------------------------


def pcap_bytes(packets):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 0x10000, LINKTYPE)
    # capturefile reads the magic as the native-endian d4c3b2a1 byte string.
    out = b"\xd4\xc3\xb2\xa1" + out[4:]
    for index in range(packets):
        body = bytes([index]) * 8
        out += struct.pack("<IIII", 1700000000, index, len(body), len(body)) + body
    return out


def pcapng_bytes(packets):
    shb = struct.pack("<IIIHHq", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1)
    shb += struct.pack("<I", 28)
    idb = struct.pack("<IIHHI", 0x00000001, 20, LINKTYPE, 0, 0x10000)
    idb += struct.pack("<I", 20)
    out = shb + idb
    for index in range(packets):
        body = bytes([index]) * 8
        block = struct.pack("<IIIIIII", 0x00000006, 32 + len(body), 0, 0, index,
                            len(body), len(body)) + body
        out += block + struct.pack("<I", 32 + len(body))
    return out


def dump_bytes(packets):
    """``packets`` whole records of the shape rxdump.py accepts: a host time
    near the file's own, a basic-rate packet type, and a real channel."""
    now = int(time.time())
    out = b""
    for index in range(packets):
        record = struct.pack(">I", now) + bytes([0, 0, 20 + index % 50])
        out += record + b"\0" * (rxreplay.RECORD - len(record))
    return out


class FakeTools:
    """``ubertooth-rx``, ``-btle`` and ``-dump`` as at fork 166b4f91.

    The command line is accepted, so the run goes on to either the board
    lookup (the no-devices switch: no board, exit 1, and the capture files
    were never created, because they are created only once a board is open)
    or, with ``-i``, a replay, which creates them and then decodes. A replay
    that read no whole record takes them back; one that read some keeps them.
    A file the writer cannot create is reported with that call's own reason.

    ``bugs`` switches on one misbehavior:

    - ``create-before-board``: the capture files are created while the
      command line is read, so a run stopped at the lookup leaves them.
    - ``keep-empty``: a replay that read no whole record leaves the files it
      created, holding their headers.
    - ``discard-evidence``: a replay that did read records takes its files
      back anyway.
    - ``discard-skipped-record``: a replay keeps its files only once a packet
      has been decoded, so one whose record the decoder skipped loses them.
    - ``remove-any-path``: the discard removes a symbolic link or /dev/null
      as readily as a regular file.
    - ``stale-reason``: the create failure carries the errno of an earlier
      call (fork #1020), here always "No such file or directory".
    - ``status-from-library``: a failed capture exits with a libusb return
      value instead of 1 (fork #565).
    - ``note-on-stdout``: the lookup's note and error go to standard output,
      where a capture written to ``-`` would be.
    - ``truncate-destination``: a refused run truncates a destination that
      already exists.
    """

    def __init__(self, *, bugs=()):
        self.bugs = set(bugs)
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-V"):
            return self._result(command, 0, "Host tools version: libubertooth 1.1 "
                                            "(git-test), libbtbb 1.0\n")
        if command[0] == sys.executable:
            return self._helper(command)
        if command[0] in ("mkdir", "chmod", "ln"):
            return self._shell(command)
        assert command[:2] == ("env", "UBERTOOTH_TEST_NO_DEVICES=1"), command
        return self._tool(command, command[2], list(command[3:]))

    def _shell(self, command):
        """The three directory commands the cases set their stage with."""
        if command[0] == "mkdir":
            Path(command[1]).mkdir()
        elif command[0] == "chmod":
            Path(command[2]).chmod(int(command[1], 8))
        else:
            target, link = command[2], Path(command[3])
            link.symlink_to(target)
        return self._result(command, 0, "")

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    def _helper(self, command):
        helper = Path(command[1]).name
        module = {"refusal.py": refusal, "rxreplay.py": rxreplay,
                  "dfuimage.py": dfuimage, "capturefile.py": capturefile,
                  "rxdump.py": rxdump}[helper]
        if helper == "refusal.py" and command[2] == "probe":
            return self._result(command, 0,
                                "No-device switch: yes (/p/lib/libubertooth.so.1)\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = module.main(list(command[2:]))
        return self._result(command, code, out.getvalue())

    # --- the tools ----------------------------------------------------------

    def _tool(self, command, tool, words):
        files, source, mode = {}, None, []
        index = 0
        while index < len(words):
            word = words[index]
            if word in ("-r", "-q", "-c", "-d"):
                files[word[1]] = words[index + 1]
                index += 2
                continue
            if word == "-i":
                source = words[index + 1]
                index += 2
                continue
            mode.append(word)
            index += 1
        if "create-before-board" in self.bugs or "truncate-destination" in self.bugs:
            for option, name in files.items():
                # A lone "-" is standard output, not a file to create.
                if name != "-" and not self._create_refusal(name):
                    self._create(option, name, 0)
        if source is None:
            return self._lookup(command, files)
        return self._replay(command, source, files)

    def _lookup(self, command, files):
        """No board: the run stops here, and said nothing about the files."""
        status = 247 if "status-from-library" in self.bugs else 1
        if "note-on-stdout" in self.bugs:
            return self._result(command, status, NOTE + NO_BOARD)
        return self._result(command, status, "", NOTE + NO_BOARD)

    def _replay(self, command, source, files):
        path = Path(source)
        if path.is_dir():
            read, failure = 0, f"error: reading {source} failed: Is a directory\n"
            packets = 0
        else:
            data = path.read_bytes()
            read, over = divmod(len(data), rxreplay.RECORD)
            failure = ("" if not over else
                       f"error: {source} ends in a partial record: {over} of "
                       f"{rxreplay.RECORD} bytes, ignored\n")
            # The decoder's answer is the fixture's, so a record it skips
            # counts as read and makes no packet.
            packets = len(rxreplay.expected_packets(VARIANT_OF[path.name]))
        created = []
        for option, name in sorted(files.items()):
            reason = self._create_refusal(name)
            if reason:
                for made in created:
                    self._discard(*made)
                status = 249 if "status-from-library" in self.bugs else 1
                if "stale-reason" in self.bugs:
                    reason = "No such file or directory"
                return self._result(command, status, "",
                                    f"error: {name}: {reason}\n")
            self._create(option, name, packets)
            created.append((option, name))
        if not failure:
            return self._result(command, 0, "")
        kept = read and "discard-evidence" not in self.bugs
        if read and packets == 0 and "discard-skipped-record" in self.bugs:
            # The regression: the files are kept only from the first packet.
            kept = False
        if kept or "keep-empty" in self.bugs:
            return self._result(command, 1, "", failure)
        warnings = "".join(self._discard(*made) for made in created)
        return self._result(command, 1, "", failure + warnings)

    @staticmethod
    def _create_refusal(name):
        path = Path(name)
        if path.is_dir():
            return "Is a directory"
        if not path.parent.is_dir():
            return "No such file or directory"
        try:
            path.touch()
        except PermissionError:
            return "Permission denied"
        return ""

    @staticmethod
    def _create(option, name, packets):
        body = {"pcap": pcap_bytes, "pcapng": pcapng_bytes,
                "dump": dump_bytes}[WRITERS[option]](packets)
        Path(name).write_bytes(body)

    def _discard(self, _option, name):
        """opt_discard_capture_file(): only a regular file at that name."""
        path = Path(name)
        if not path.is_symlink() and path.is_file():
            path.unlink()
            return ""
        if "remove-any-path" in self.bugs:
            if path.is_symlink():
                path.unlink()
                return ""
            return f"warning: cannot remove {name}: Permission denied\n"
        return ""


# --- running the cases --------------------------------------------------------


def cases(coverage):
    definition = builtin_catalog().get(NAME)
    arguments = definition.normalize_arguments(
        {"hosts": ["Host_Mac"], "coverage": coverage})
    return arguments, definition.command_cases(arguments, {}, {})


def run(executor, coverage="comprehensive"):
    arguments, _cases = cases(coverage)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = TestInvocation(f"001-{NAME}", NAME, "Host_Mac", arguments, {}, {})
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(executor, event_sink=sink).run(
            (invocation,), root, run_id="named-run", manifest=manifest,
            preflight_checks=checks,
        )
        return summary, sink.events


class FixtureTests(unittest.TestCase):

    def test_the_partial_variant_holds_no_whole_record(self):
        data = rxreplay.fixture_bytes(1700000000, "partial")
        self.assertEqual(rxreplay.RECORD - rxreplay.TRUNCATED_BYTES, len(data))
        self.assertEqual(0, len(data) // rxreplay.RECORD)
        self.assertEqual([], rxreplay.expected_packets("partial"))
        # The truncated variant does read records before it fails, which is
        # what makes the two cases different.
        self.assertTrue(rxreplay.expected_packets("truncated"))

    def test_the_fixture_command_writes_and_recognizes_the_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.bin"
            out = io.StringIO()
            with redirect_stdout(out):
                code = rxreplay.main(["fixture", str(path), "--variant", "partial",
                                      "--base-time", "1700000000"])
            self.assertEqual(0, code)
            self.assertIn("Replay fixture: partial, 0 whole records, 58 bytes",
                          out.getvalue())
            self.assertEqual("", rxreplay.only_fixture(
                Path(directory), "x.bin", 1700000000, "partial"))

    def test_the_base_time_is_the_fixture_age_before_now(self):
        self.assertEqual(1700000000 - rxreplay.FIXTURE_AGE_S,
                         rxreplay.fixture_base_time(1700000000))


class CaseTests(unittest.TestCase):

    def test_the_case_set_needs_no_board_and_stays_in_its_directory(self):
        definition = builtin_catalog().get(NAME)
        self.assertTrue(definition.implemented)
        self.assertEqual({}, dict(definition.hardware_roles or {}))
        self.assertIsNone(definition.estimates.get("smoke").per_count)
        _a, smoke = cases("smoke")
        self.assertEqual(6, len(smoke))
        _a, full = cases("comprehensive")
        self.assertEqual(15, len(full))
        self.assertEqual(len(full), len({case.case_id for case in full}))
        self.assertEqual({case.case_id for case in smoke} & {c.case_id for c in full},
                         {case.case_id for case in smoke})
        context = {"capture dir": "/run/captures/x", "directory before": "{}"}
        for case in full:
            self.assertIsInstance(case, StateCase)
            self.assertTrue(case.capture_dir)
            self.assertEqual("probe", case.steps[0].step_id)
            self.assertEqual("list-files", case.cleanup[-1].step_id)
            for step in case.steps + case.cleanup:
                argv = step.resolve_argv(context)
                self.assertFalse(step.mutates or step.continuous)
                if argv[0] not in (sys.executable, "mkdir", "chmod", "ln"):
                    # Every tool runs with the switch, so no case can open a
                    # board even on a host that has one attached.
                    self.assertEqual(("env", "UBERTOOTH_TEST_NO_DEVICES=1"), argv[:2])
                for path in (a for a in argv if a.startswith("/run")):
                    self.assertTrue(path == "/run/captures/x"
                                    or path.startswith("/run/captures/x/"), path)

    def test_fork_tools_pass_every_case(self):
        for coverage in ("smoke", "comprehensive"):
            with self.subTest(coverage=coverage):
                summary, events = run(FakeTools(), coverage)
                self.assertTrue(summary.passed, failed_names(events))

    def _fails(self, bug, expected):
        summary, events = run(FakeTools(bugs=(bug,)))
        self.assertFalse(summary.passed, bug)
        failed = failed_names(events)
        for case_id in expected:
            self.assertTrue(any(case_id in name for name in failed),
                            f"{bug}: {case_id} passed; failed: {failed}")

    def test_a_run_that_creates_its_files_before_the_board_fails(self):
        # Fork #996 and #1000: the files come after everything that can fail.
        self._fails("create-before-board",
                    ("no board", "with no board"))

    def test_an_empty_capture_left_behind_fails(self):
        # Fork #1009: the file a run read no whole record for goes back.
        self._fails("keep-empty", ("ubertooth-rx -i <no whole record> -r -q -d",))

    def test_taking_back_the_files_of_a_record_the_decoder_skipped_fails(self):
        # The line is the first whole record, not the first packet: a run
        # that kept its files only once a packet was decoded takes these.
        self._fails("discard-skipped-record",
                    ("<one record the decoder skips, then a cut-short one>",))

    def test_taking_back_a_capture_that_holds_packets_fails(self):
        self._fails("discard-evidence",
                    ("ubertooth-rx -i <whole records then a cut-short one>",))

    def test_removing_a_path_the_user_arranged_fails(self):
        self._fails("remove-any-path",
                    ("<a symbolic link>", "/dev/null"))

    def test_a_create_failure_reported_with_another_call_s_reason_fails(self):
        # Fork #1020: errno is cleared before each create and read straight
        # after, so the reason belongs to that call.
        self._fails("stale-reason", ("<a directory>", "read-only directory"))

    def test_a_libusb_return_value_as_the_exit_status_fails(self):
        # Fork #565: a capture that cannot start exits 1.
        self._fails("status-from-library", ("with no board",))

    def test_the_lookup_note_on_standard_output_fails(self):
        self._fails("note-on-stdout", ("ubertooth-rx -q - with no board",))

    def test_truncating_a_destination_that_already_exists_fails(self):
        self._fails("truncate-destination", ("with no board",))


if __name__ == "__main__":
    unittest.main()
