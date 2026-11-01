"""Interrupting a capture: the helper against a stub tool, and the cases."""

import io
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ubertooth_harness import captureinterrupt, interrupt_cases, rxdump
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import StateCase
from tests.test_sequencer import CollectSink, run_contract
from tests.test_tool_cases import failed_names

NAME = "Interrupting a capture"
SERIAL = "1d00010c8088cb95ad9aad61c22000f5"

# A stand-in for a capture tool: it streams 68-byte records into the file -d
# names until a SIGINT, then stops and exits 0, which is what the fork's
# tools do. Each flag switches on one misbehavior.
STUB = textwrap.dedent('''
    import argparse, os, signal, sys, time
    p = argparse.ArgumentParser()
    p.add_argument("-d", dest="dump", required=True)
    p.add_argument("--ignore-sigint", action="store_true")
    p.add_argument("--slow-exit", type=float, default=0.0)
    p.add_argument("--keep-writing", action="store_true")
    p.add_argument("--note-on-stdout", action="store_true")
    p.add_argument("--exit-status", type=int, default=0)
    p.add_argument("--no-file", action="store_true")
    p.add_argument("--quiet-stderr", action="store_true")
    p.add_argument("--buffered", action="store_true")
    p.add_argument("--warning-on-stdout", action="store_true")
    a = p.parse_args()
    stop = False
    def handler(_s, _f):
        global stop
        stop = True
    if not a.ignore_sigint:
        signal.signal(signal.SIGINT, handler)
    if a.warning_on_stdout:
        print("warning: a progress line in the wrong place", flush=True)
    print("note: starting" if a.note_on_stdout else "packet", flush=True)
    if not a.quiet_stderr:
        print("note: entering the mode", file=sys.stderr, flush=True)
    if a.no_file:
        while not stop:
            time.sleep(0.005)
    else:
        with open(a.dump, "wb", buffering=(-1 if a.buffered else 0)) as handle:
            while not stop:
                handle.write(bytes(68))
                time.sleep(0.005)
        if a.keep_writing and os.fork() == 0:
            with open(a.dump, "ab", buffering=0) as handle:
                for _ in range(200):
                    handle.write(bytes(68))
                    time.sleep(0.005)
            os._exit(0)
    time.sleep(a.slow_exit)
    if not a.quiet_stderr:
        print("Stopping ubertooth device", file=sys.stderr, flush=True)
    sys.exit(a.exit_status)
''')


class HelperTests(unittest.TestCase):
    """captureinterrupt.py against a real child process."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.stub = self.root / "stub.py"
        self.stub.write_text(STUB)
        self.dump = self.root / "dump.bin"
        self.addCleanup(self.directory.cleanup)

    def run_helper(self, *flags, extra=()):
        # Each run writes its own capture, as a case does in its own
        # directory: a file left by the run before would satisfy the growth
        # wait before this tool had written anything.
        for stale in (self.dump, Path(f"{self.dump}.stdout.log"),
                      Path(f"{self.dump}.stderr.log")):
            if stale.exists():
                stale.unlink()
        argv = ["interrupt", "--grow", str(self.dump),
                "--min-bytes", str(rxdump.FULL_RECORD * 5), "--settle", "0.2",
                "--ready-deadline", "10", "--exit-deadline", "2",
                *extra, "--",
                sys.executable, str(self.stub), "-d", str(self.dump), *flags]
        out = io.StringIO()
        with redirect_stdout(out):
            code = captureinterrupt.main(argv)
        return code, out.getvalue()

    def test_one_sigint_ends_a_running_capture(self):
        code, report = self.run_helper()
        self.assertEqual(0, code, report)
        self.assertIn("Interrupt: one SIGINT to pid ", report)
        self.assertIn("Interrupt done: one SIGINT ended the run in ", report)
        self.assertGreaterEqual(self.dump.stat().st_size, rxdump.FULL_RECORD * 5)
        # The tool's own streams are kept beside the capture.
        self.assertIn("note: entering the mode",
                      (self.root / "dump.bin.stderr.log").read_text())

    def test_a_tool_killed_by_the_signal_fails(self):
        code, report = self.run_helper("--ignore-sigint")
        self.assertEqual(1, code)
        self.assertIn("killed by signal 2 instead of handling the SIGINT", report)

    def test_a_tool_that_needs_longer_than_the_deadline_fails(self):
        code, report = self.run_helper("--slow-exit", "4")
        self.assertEqual(1, code)
        self.assertIn("one Ctrl-C did not end it", report)

    def test_a_process_left_behind_and_a_file_still_growing_fail(self):
        code, report = self.run_helper("--keep-writing")
        self.assertEqual(1, code)
        self.assertIn("left behind in the group it ran in", report)
        self.assertIn("after the tool exited, so the capture was still running",
                      report)

    def test_a_diagnostic_line_on_standard_output_fails(self):
        code, report = self.run_helper("--warning-on-stdout")
        self.assertEqual(1, code)
        self.assertIn("is on standard output; progress and diagnostics go to "
                      "standard error", report)

    def test_another_exit_status_fails(self):
        code, report = self.run_helper("--exit-status", "2")
        self.assertEqual(1, code)
        self.assertIn("it exited 2, not 0", report)

    def test_a_capture_that_never_got_going_fails(self):
        # These streams do not depend on the air, so a capture that never got
        # going is the tool's fault and not the bench's: it must not skip.
        code, report = self.run_helper("--no-file", extra=("--ready-deadline", "1"))
        self.assertEqual(1, code)
        self.assertNotIn("SKIP", report)
        self.assertIn("so no capture was running to interrupt", report)

    def test_a_teardown_line_is_required_only_where_it_is_asked_for(self):
        # ubertooth-btle and ubertooth-rx print nothing on a clean run, so a
        # silent stderr must pass unless the case names a line to look for.
        code, report = self.run_helper("--quiet-stderr")
        self.assertEqual(0, code, report)
        code, report = self.run_helper(
            "--quiet-stderr", extra=("--teardown-says", "Stopping ubertooth"))
        self.assertEqual(1, code)
        self.assertIn("nothing matching 'Stopping ubertooth' is on standard "
                      "error", report)
        code, report = self.run_helper(
            extra=("--teardown-says", "Stopping ubertooth"))
        self.assertEqual(0, code, report)

    def test_a_capture_file_already_there_fails(self):
        self.dump.parent.mkdir(parents=True, exist_ok=True)
        self.dump.write_bytes(bytes(rxdump.FULL_RECORD * 50))
        argv = ["interrupt", "--grow", str(self.dump), "--min-bytes", "1", "--",
                sys.executable, str(self.stub), "-d", str(self.dump)]
        out = io.StringIO()
        with redirect_stdout(out):
            code = captureinterrupt.main(argv)
        self.assertEqual(1, code)
        self.assertIn("is already there", out.getvalue())

    def test_a_note_on_standard_output_is_allowed(self):
        # ubertooth-btle prints its undecoded-packet note on the report
        # stream, which is standard output for a capture written to a file,
        # and the fork puts it there deliberately.
        code, report = self.run_helper("--note-on-stdout")
        self.assertEqual(0, code, report)

    def test_a_block_buffered_writer_still_reaches_the_floor(self):
        # ubertooth-specan does not flush per sample, so its file grows a
        # stdio buffer at a time; the floor has to be reachable that way.
        code, report = self.run_helper("--buffered")
        self.assertEqual(0, code, report)

    def test_nothing_is_retried(self):
        # One run, one signal, one verdict: there is no retry to hide a
        # failure behind.
        code, report = self.run_helper("--exit-status", "2")
        self.assertEqual(1, code)
        self.assertNotIn("Interrupt retry", report)
        self.assertNotIn("Interrupt attempt", report)
        self.assertTrue(report.rstrip().endswith(
            "Interrupt problem: it exited 2, not 0"), report)

    def test_it_needs_a_tool_to_run(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            captureinterrupt.main(["interrupt", "--grow", str(self.dump), "--"])


def cases(coverage):
    definition = builtin_catalog().get(NAME)
    arguments = definition.normalize_arguments(
        {"hosts": ["Host_Linux"], "coverage": coverage})
    return arguments, definition.command_cases(
        arguments, {"Ubertooth": "UT_Linux1"},
        {"Ubertooth": {"serial": SERIAL}})


class FakeTools:
    """Answers the board queries, and stands in for the interrupt helper.

    ``helper`` is what the helper step reports: "done" for a run one SIGINT
    ended, "problem" for one it did not, "never-started" for a capture that
    never got going, which is a failure and not a skip.
    """

    def __init__(self, *, helper="done"):
        self.helper = helper
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-V"):
            return self._result(command, 0, "Host tools version: libubertooth 1.1 "
                                            "(git-test), libbtbb 1.0\n")
        if command[0] == sys.executable:
            return self._helper(command)
        assert command[0].startswith("ubertooth-"), command
        return self._util(command)

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    def _helper(self, command):
        name = Path(command[1]).name
        if name != "captureinterrupt.py":
            # The file checkers run for real against what the fake wrote.
            return self._checker(command)
        grow = Path(command[command.index("--grow") + 1])
        if self.helper == "never-started":
            return self._result(command, 1, "Interrupt problem: dump.bin held 0 "
                                            "bytes of the 1360 wanted after 20s, "
                                            "so no capture was running to "
                                            "interrupt\n")
        if self.helper == "problem":
            return self._result(command, 1, "Interrupt problem: it was still "
                                            "running 5s after the SIGINT, so one "
                                            "Ctrl-C did not end it\n")
        # A run one SIGINT ended: write the file the following steps read.
        grow.parent.mkdir(parents=True, exist_ok=True)
        if grow.suffix == ".pcapng":
            grow.write_bytes(_PCAPNG)
            grow.with_suffix(".pcap").write_bytes(_PCAP)
        elif grow.name == "specan.bin":
            sweep = b"".join(
                bytes([f >> 8, f & 0xFF, 0xC0])
                for f in range(interrupt_cases.SPECAN_LOW,
                               interrupt_cases.SPECAN_HIGH + 1))
            grow.write_bytes(sweep * 13)
        elif grow.suffix == ".bin":
            grow.write_bytes(_dump_records(25))
        return self._result(command, 0, "Interrupt: one SIGINT to pid 1 0.400s in, "
                            "with 1700 byte(s) captured\nInterrupt done: one SIGINT "
                            "ended the run in 0.05s with status 0, nothing left "
                            "behind, 1700 byte(s) captured and no more\n")

    def _checker(self, command):
        import importlib
        module = importlib.import_module(
            "ubertooth_harness." + Path(command[1]).stem)
        out = io.StringIO()
        with redirect_stdout(out):
            code = module.main(list(command[2:]))
        return self._result(command, code, out.getvalue())

    def _util(self, command):
        option = command[-1]
        answers = {
            "-v": "Firmware version: bluetooth_rxtx git-test (API:1.57)\n",
            "-C": "Current frequency: 2441 MHz\n",
            "-z": "Squelch set to -120\n",
            "-d": ("USR LED status: 0\nRX LED status : 0\n"
                   "TX LED status : 0\n"),
            "-s": f"Serial No: {SERIAL}\n",
        }
        if option in answers:
            return self._result(command, 0, answers[option])
        # A setter or a stop prints nothing on stdout.
        return self._result(command, 0, "", f"Stopping ubertooth device {SERIAL}\n")


def _dump_records(count):
    import struct
    import time as clock
    now = int(clock.time())
    record = struct.pack(">I", now) + bytes(
        [0, 0, 20, 0] + [0] * 4 + [0] * 4 + [0xC0, 0xA6, 0xB2, 0] + [0] * 50)
    return record[:rxdump.FULL_RECORD] * count


def _pcap_bytes():
    import struct
    head = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHiIII", 2, 4, 0, 0, 0x10000, 256)
    return head


def _pcapng_bytes():
    import struct
    shb = struct.pack("<IIIHHq", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1)
    shb += struct.pack("<I", 28)
    idb = struct.pack("<IIHHI", 1, 20, 256, 0, 0x10000) + struct.pack("<I", 20)
    return shb + idb


_PCAP = _pcap_bytes()
_PCAPNG = _pcapng_bytes()


def run(executor, coverage="comprehensive"):
    arguments, _cases = cases(coverage)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = TestInvocation(f"001-{NAME}", NAME, "Host_Linux", arguments,
                                    {"Ubertooth": "UT_Linux1"},
                                    {"Ubertooth": {"serial": SERIAL}})
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(executor, event_sink=sink).run(
            (invocation,), root, run_id="named-run", manifest=manifest,
            preflight_checks=checks,
        )
        return summary, sink.events


class CaseTests(unittest.TestCase):

    def test_the_case_set_binds_one_board_and_no_peer(self):
        definition = builtin_catalog().get(NAME)
        self.assertTrue(definition.implemented)
        self.assertEqual({"Ubertooth": "Ubertooth"},
                         dict(definition.hardware_roles))
        self.assertTrue(definition.guards_other_ubertooths)
        self.assertFalse(definition.writes_firmware)
        # No placement requirement: no case needs traffic, so an attenuated
        # board is as good as any.
        self.assertEqual({}, dict(definition.rf_defaults or {}))
        _a, smoke = cases("smoke")
        self.assertEqual(2, len(smoke))
        _a, full = cases("comprehensive")
        self.assertEqual(3, len(full))
        self.assertEqual(len(full), len({case.case_id for case in full}))
        context = {"capture dir": "/run/c"}
        for case in full:
            self.assertIsInstance(case, StateCase)
            self.assertTrue(case.capture_dir)
            self.assertEqual("establish-idle", case.steps[0].step_id)
            interrupt = next(s for s in case.steps if s.step_id == "interrupt")
            # No case here can pass by skipping.
            self.assertIsNone(interrupt.skip_returncode)
            self.assertTrue(interrupt.mutates)
            argv = interrupt.resolve_argv(context)
            # One SIGINT, to the tool itself: no shell, no timeout wrapper.
            self.assertEqual(sys.executable, argv[0])
            self.assertEqual("captureinterrupt.py", Path(argv[1]).name)
            tool = argv[argv.index("--") + 1]
            self.assertTrue(tool.startswith("ubertooth-"), tool)
            self.assertNotIn("timeout", argv)
            self.assertIn("-U", argv)
            self.assertEqual(SERIAL, argv[argv.index("-U") + 1])
            self.assertEqual("answers", case.steps[-1].step_id)
            # The channel and the squelch are read before the cleanup's own
            # stop, so a board still carrying the mode's settings is seen.
            read_back = [s.step_id for s in case.steps
                         if s.step_id.startswith("mode-stopped-")]
            self.assertEqual(["mode-stopped-channel", "mode-stopped-squelch"],
                             read_back)
            # Cleanup always restores and then reads the identity back.
            self.assertEqual("cleanup-stop", case.cleanup[0].step_id)
            self.assertEqual("restored-device-identity", case.cleanup[-1].step_id)

    def test_a_run_one_sigint_ended_passes(self):
        for coverage in ("smoke", "comprehensive"):
            with self.subTest(coverage=coverage):
                summary, events = run(FakeTools(), coverage)
                self.assertTrue(summary.passed, failed_names(events))

    def test_a_capture_one_sigint_did_not_end_fails(self):
        summary, events = run(FakeTools(helper="problem"), "smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(2, len(failed_names(events)), failed_names(events))

    def test_a_capture_that_never_got_going_fails_and_still_cleans_up(self):
        executor = FakeTools(helper="never-started")
        summary, events = run(executor, "smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(2, len(failed_names(events)), failed_names(events))
        self.assertEqual(0, summary.skipped_cases)
        # The cleanup still ran in full: the board is stopped, restored, and
        # its identity read.
        self.assertTrue(any(c[-1] == "-S" for c in executor.commands))
        self.assertTrue(any(c[-1] == "-s" for c in executor.commands))


if __name__ == "__main__":
    unittest.main()
