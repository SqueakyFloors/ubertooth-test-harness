"""`Board state between runs` against a model of a board left in a mode.

The fake runs the real checkers (rxdump.py, specandump.py, killedcapture.py
verify) on the files it writes, so what the cases require is checked by the
code that will judge the hardware run. ``bugs`` switches on one misbehavior at
a time, and each one has to fail the case that is there to catch it.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ubertooth_harness import boardstate_cases, killedcapture, rxdump
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.stateful import StateCase
import tests.test_transitions as tt
from tests.test_transitions import SERIAL
from tests.test_tool_cases import ToolUbertooth, failed_names
from tests.test_receive_cases import full_record

TEST = boardstate_cases.TEST_NAME
DUMP_CASE = "A symbol dump after a killed spectrum sweep"
# What the killed tool had written when it was killed, in the fake.
KILLED_BYTES = 8192


class BoardStateUbertooth(ToolUbertooth):
    """ToolUbertooth plus ubertooth-dump, the killedcapture step, and the
    warning libubertooth prints when a capture starts on a board an earlier
    run left in a mode (fork e657807).

    ``bugs``: ``no-warning`` (the capture starts without saying what it
    found), ``warning-on-stdout`` (the warning goes to stdout, where samples
    and records belong), ``mode-did-not-stop`` (the wait for the board to
    leave the mode ran out), ``foreign-records`` (the new capture opens with a
    record of the mode before it), ``stale-records`` (a basic-rate record from
    before the stream started, #318), ``modulation-lost-at-mode-start`` (the
    mode start threw away the modulation the capture stated for it),
    ``evidence-overwritten`` (the next run writes over the file the killed one
    left), and ``modulation-read-fails`` (the board stops answering a read
    afterwards).
    """

    MODES = {"ubertooth-specan": boardstate_cases.MODE_SPECAN}

    def __init__(self, *args, bugs=(), records=200, **kwargs):
        super().__init__(*args, **kwargs)
        # MDMCTRL, which keeps whatever the last mode programmed.
        self.modulation = "0029"
        self.bugs = set(bugs)
        self.records = records
        # The mode a killed capture left the board running, by number; None
        # once a capture has reported it, and on a board left idle, which is
        # what a tool that exits normally leaves.
        self.left_in_mode = None
        self.killed_capture = None

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if command[1:3] == (boardstate_cases._KILLER, "kill"):
            self.commands.append(command)
            return self._kill(command, on_output)
        if command[1:3] == (boardstate_cases._KILLER, "verify"):
            self.commands.append(command)
            return self._checker(command, killedcapture.main, on_output)
        if command[1:2] == (boardstate_cases._DUMP_CHECKER,):
            return self._checker(command, rxdump.main, on_output)
        if command[0] == "ubertooth-dump":
            self.commands.append(command)
            return self._dump(command, on_output)
        if command[0] == "ubertooth-debug":
            self.commands.append(command)
            if "modulation-read-fails" in self.bugs:
                return self._result(command, 1, "",
                                    "error: register read failed\n", on_output)
            return self._result(command, 0,
                                f"%MDMCTRL = 0x{self.modulation.upper()}\n", "",
                                on_output)
        return super().run(argv, timeout_s=timeout_s, cancellation=cancellation,
                           on_output=on_output)

    def _checker(self, command, main, on_output):
        """One of the real checkers, on the files this fake wrote."""
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(command[2:]))
        return self._result(command, code, out.getvalue(), "", on_output)

    # killedcapture.py kill ----------------------------------------------
    def _kill(self, command, on_output):
        args = list(command[3:])
        capture = Path(args[args.index("--capture") + 1])
        record = Path(args[args.index("--record") + 1])
        tool_argv = args[args.index("--") + 1:]
        tool = Path(tool_argv[0]).name
        capture.write_bytes(os.urandom(KILLED_BYTES))
        size, checksum = killedcapture.digest(capture)
        record.write_text(json.dumps({
            "capture": capture.name, "size": size, "sha256": checksum,
            "argv": tool_argv, "signal": 9,
        }) + "\n", encoding="utf-8")
        self.killed_capture = capture
        # No STOP was sent, so the board keeps running the mode the tool
        # started and the next capture start is the one that has to stop it.
        self.mode = tool
        self.left_in_mode = self.MODES[tool]
        return self._result(command, 0, (
            f"Killed capture: {tool} pid 31337 streamed {KILLED_BYTES} bytes "
            "in 1.2 s, SIGKILL reaped as signal 9, nothing left in its "
            "process group\n"
        ), "", on_output)

    def _previous_mode(self):
        """(stdout, stderr) of libubertooth's ubertooth_bulk_init(): the mode
        an earlier run left is read, stopped, and waited for."""
        mode, self.left_in_mode = self.left_in_mode, None
        if mode is None or "no-warning" in self.bugs:
            return "", ""
        lines = [f"warning: the board is still in mode {mode} from an earlier "
                 "run; stopping it first (ubertooth-util -w names the modes)"]
        if "mode-did-not-stop" in self.bugs:
            lines.append("warning: it has not stopped after 1000 ms; this "
                         "capture may open with a packet or two of the "
                         "earlier run's")
        text = "".join(line + "\n" for line in lines)
        if "warning-on-stdout" in self.bugs:
            return text, ""
        return "", text

    def _overwrite_evidence(self):
        if "evidence-overwritten" in self.bugs and self.killed_capture is not None:
            with self.killed_capture.open("ab") as handle:
                handle.write(b"later")

    # ubertooth-dump ------------------------------------------------------
    def _dump(self, command, on_output):
        opts = list(command[3:])
        # The mode start resets the modulation to basic rate; a capture that
        # states LE for it gets LE, unless this bug is on (fork d4aca18).
        if "-l" in opts and "modulation-lost-at-mode-start" not in self.bugs:
            self.modulation = boardstate_cases.MDMCTRL_LE
        else:
            self.modulation = "0029"
        stdout, stderr = self._previous_mode()
        data = bytearray()
        if "foreign-records" in self.bugs:
            # A record of the spectrum sweep before it: the firmware's own
            # packet type says which mode wrote it.
            data += full_record(pkt_type=4)
        if "stale-records" in self.bugs:
            # A basic-rate record from before this stream started: the clock
            # restarts at mode entry, so its clock is ahead of the first.
            data += full_record(clock=2_000_000)
        for index in range(self.records):
            data += full_record(clock=index)
        Path(opts[opts.index("-d") + 1]).write_bytes(bytes(data))
        self._overwrite_evidence()
        self.mode = "dump"
        self._leave(True)
        return self._result(command, 124, stdout, stderr + "HARD TIMEOUT\n",
                            on_output, timed_out=True)

    # ubertooth-specan ----------------------------------------------------
    def _specan(self, command, on_output, cwd=Path.cwd()):
        opts = list(command[3:])
        low = int(opts[opts.index("-l") + 1])
        high = int(opts[opts.index("-u") + 1])
        stdout, stderr = self._previous_mode()
        data = bytearray()
        if "foreign-records" in self.bugs:
            # A record of the dump before it, in the stream the samples are
            # read from: it shifts every sample after it.
            data += bytes(10)
        for _ in range(3):
            for frequency in range(low, high + 1):
                data += bytes([frequency >> 8, frequency & 0xFF, 0xD3])
        Path(opts[opts.index("-d") + 1]).write_bytes(bytes(data))
        self._overwrite_evidence()
        self.mode = "specan"
        self._leave(True)
        return self._result(command, 124, stdout, stderr + "HARD TIMEOUT\n",
                            on_output, timed_out=True)


def cases_for(duration="2s"):
    definition = builtin_catalog().get(TEST)
    arguments = definition.normalize_arguments(
        {"hosts": ["Host_Linux"], "Ubertooth": "default", "duration": duration})
    return arguments, definition.command_cases(
        arguments, {"Ubertooth": "UT_Linux0"}, {"Ubertooth": {"serial": SERIAL}})


def run(device, duration="2s"):
    original = tt.cases_for

    def with_duration(name, coverage="smoke"):
        if name == TEST:
            return cases_for(duration)
        return original(name, coverage)

    with mock.patch.object(tt, "cases_for", with_duration):
        return tt.run_test(TEST, device)


def steps_of(case):
    return [step.step_id for step in case.steps]


class CaseGenerationTests(unittest.TestCase):

    def setUp(self):
        _arguments, self.cases = cases_for()
        (self.dump,) = self.cases

    def test_one_case_the_dump_after_a_killed_sweep(self):
        self.assertEqual(["killed-specan-then-dump"],
                         [case.case_id for case in self.cases])
        for case in self.cases:
            self.assertIsInstance(case, StateCase)
            self.assertTrue(case.capture_dir)

    def test_the_board_is_reset_and_its_boot_defaults_are_read(self):
        for case in self.cases:
            ids = steps_of(case)
            self.assertLess(ids.index("reset"), ids.index("kill-capture"))
            for key in ("channel", "squelch", "leds", "pa-level"):
                self.assertIn(f"verify-{key}-at-boot", ids)

    def test_the_kill_runs_the_tool_with_nothing_in_between(self):
        context = {"capture dir": "/run/captures"}
        for case in self.cases:
            step = next(s for s in case.steps if s.step_id == "kill-capture")
            argv = list(step.resolve_argv(context))
            self.assertEqual([sys.executable, boardstate_cases._KILLER, "kill"],
                             argv[:3])
            # No shell, no timeout wrapper: the tool follows "--" and is what
            # the signal reaches.
            self.assertIn("--", argv)
            tool = argv[argv.index("--") + 1]
            self.assertIn(tool, ("ubertooth-specan", "ubertooth-dump"))
            self.assertIn("nothing left in its process group",
                          " ".join(step.output_patterns))

    def test_the_second_capture_is_the_other_tool_and_names_the_mode_left(self):
        context = {"capture dir": "/run/captures"}
        for case in self.cases:
            tool, mode = "ubertooth-dump", boardstate_cases.MODE_SPECAN
            step = next(s for s in case.steps if s.step_id == "second-capture")
            argv = step.resolve_argv(context)
            self.assertEqual(tool, argv[0])
            # The modulation is stated before the mode request, which is the
            # setting the case requires to survive the mode start.
            self.assertIn("-l", argv)
            self.assertTrue(step.continuous)
            self.assertIn(f"still in mode {mode} from an earlier run",
                          " ".join(step.output_patterns))
            self.assertIn("has not stopped after", " ".join(step.forbidden_patterns))

    def test_the_second_capture_requires_the_warning_on_stderr(self):
        step = next(s for s in self.dump.steps if s.step_id == "second-capture")
        warning = (f"warning: the board is still in mode "
                   f"{boardstate_cases.MODE_SPECAN} from an earlier run\n")
        # Both captures write to a file, so stdout carries nothing at all:
        # the warning there is a failure, and with the pattern above requiring
        # it in either stream, that is how the case says it was on stderr.
        self.assertIn("progress goes to stderr", step.output_check([warning]))
        self.assertEqual("", step.output_check([]))

    def test_the_dump_check_allows_no_stale_and_no_foreign_record(self):
        step = next(s for s in self.dump.steps if s.step_id == "verify-second-capture")
        argv = list(step.resolve_argv({"capture dir": "/run/captures"}))
        self.assertEqual(["--max-stale", "0"], argv[-2:])
        self.assertIn("br,keep-alive", argv)
        # 2 s at the 50 records a second floor.
        self.assertEqual(["--min-records", "100"],
                         argv[argv.index("--min-records"):][:2])

    def test_the_stated_modulation_has_to_survive_the_mode_start(self):
        step = next(s for s in self.dump.steps
                    if s.step_id == "verify-modulation-survived")
        self.assertEqual(boardstate_cases.MDMCTRL_LE, step.observe.expected)

    def test_every_read_the_issue_asks_for_runs_after_the_kill(self):
        for case in self.cases:
            ids = steps_of(case)
            for step_id in ("verify-killed-capture-kept", "read-firmware-after-kill",
                            "read-serial-after-kill", "read-channel-after-kill",
                            "read-pa-level-after-kill", "read-squelch-after-kill",
                            "read-leds-after-kill",
                            # The modulation read, which also checks its value.
                            "verify-modulation-survived"):
                self.assertIn(step_id, ids)

    def test_the_cleanup_stops_resets_checks_defaults_and_restores(self):
        for case in self.cases:
            ids = [step.step_id for step in case.cleanup]
            self.assertEqual(["cleanup-stop", "cleanup-reset"], ids[:2])
            for key in ("channel", "squelch", "leds", "pa-level"):
                self.assertIn(f"cleanup-verify-{key}", ids)
                self.assertIn(f"restore-{key}", ids)
                self.assertIn(f"verify-restored-{key}", ids)
                # The defaults are checked before the originals go back.
                self.assertLess(ids.index(f"cleanup-verify-{key}"),
                                ids.index(f"restore-{key}"))
            self.assertEqual("restored-device-identity", ids[-1])


class CaseOutcomeTests(unittest.TestCase):

    def run_with(self, **kwargs):
        summary, events = run(BoardStateUbertooth(**kwargs))
        return summary, failed_names(events)

    def test_a_clean_run_passes_both_cases(self):
        summary, failed = self.run_with()
        self.assertEqual([], failed)
        self.assertEqual("passed", summary.status)

    def test_a_capture_that_says_nothing_about_the_mode_it_found_fails(self):
        _summary, failed = self.run_with(bugs=("no-warning",))
        self.assertEqual([DUMP_CASE], failed)

    def test_the_warning_on_stdout_fails(self):
        _summary, failed = self.run_with(bugs=("warning-on-stdout",))
        self.assertEqual([DUMP_CASE], failed)

    def test_a_board_that_did_not_leave_the_mode_fails(self):
        _summary, failed = self.run_with(bugs=("mode-did-not-stop",))
        self.assertEqual([DUMP_CASE], failed)

    def test_a_record_of_the_previous_mode_fails(self):
        _summary, failed = self.run_with(bugs=("foreign-records",))
        self.assertEqual([DUMP_CASE], failed)

    def test_records_from_before_the_stream_started_fail(self):
        _summary, failed = self.run_with(bugs=("stale-records",))
        self.assertEqual([DUMP_CASE], failed)

    def test_a_modulation_lost_at_mode_start_fails(self):
        _summary, failed = self.run_with(bugs=("modulation-lost-at-mode-start",))
        self.assertEqual([DUMP_CASE], failed)

    def test_overwriting_the_killed_capture_fails(self):
        _summary, failed = self.run_with(bugs=("evidence-overwritten",))
        self.assertEqual([DUMP_CASE], failed)

    def test_too_few_records_fails(self):
        _summary, failed = self.run_with(records=10)
        self.assertEqual([DUMP_CASE], failed)

    def test_a_board_that_stops_answering_a_read_fails(self):
        # The reads after the kill are the point of the case: a board that
        # does not answer one is a board the killed run left unusable.
        _summary, failed = self.run_with(bugs=("modulation-read-fails",))
        self.assertEqual([DUMP_CASE], failed)


class DumpCheckerTests(unittest.TestCase):
    """--max-stale, which this test added to rxdump.py."""

    def check(self, data, *options):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dump.bin"
            path.write_bytes(data)
            out = io.StringIO()
            with redirect_stdout(out):
                code = rxdump.main([str(path), "full", *options])
            return code, out.getvalue()

    def test_a_clean_dump_reports_no_stale_records(self):
        data = b"".join(full_record(clock=index) for index in range(5))
        code, output = self.check(data, "--max-stale", "0")
        self.assertEqual(0, code, output)
        self.assertIn("Receive dump: 5 full records, 0 stale (0 LE)", output)

    def test_a_stale_record_is_counted_and_refused(self):
        data = full_record(clock=2_000_000) + b"".join(
            full_record(clock=index) for index in range(5))
        code, output = self.check(data, "--max-stale", "0")
        self.assertEqual(1, code)
        self.assertIn("1 stale (0 LE)", output)
        self.assertIn("more than 0", output)

    def test_without_the_option_the_line_is_unchanged(self):
        data = full_record(clock=2_000_000) + full_record(clock=0)
        code, output = self.check(data)
        self.assertEqual(0, code)
        self.assertEqual("Receive dump: 2 full records\n", output)

    def test_the_option_needs_whole_records(self):
        code, output = self.check(b"\x00" * 10, "--max-stale", "0")
        self.assertEqual(1, code)
        self.assertIn("trailing bytes", output)


if __name__ == "__main__":
    unittest.main()
