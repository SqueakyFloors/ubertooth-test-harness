"""What a person watching `run-suite` with `tail -F` sees (#342).

The live stream on stderr and ``report.md`` are rendered from the same
records, through the words in ``rawlog``: the banner that opens a test, the
verdict of a check, the plain label of each state step, and the aligned
expected and observed lines. The run then ends with a block that identifies
itself, so a tailed run says what was tested without opening the report.
"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ubertooth_harness import cli, rawlog, report
from ubertooth_harness.build import BuildError
from ubertooth_harness.events import RunEvent, TerminalEventSink
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.flash import FlashCancelled, HarnessError
from ubertooth_harness.rawlog import (
    RESULT_TAG,
    STAGE_BASELINE,
    STAGE_CLEANUP,
    STAGE_STATE,
    STAGE_TRANSITION,
)
from ubertooth_harness.sequencer import NamedTestSequencer
from tests import watchdog_samples
from tests.test_guards import A, B, Bench, hardware, step_plan
from tests.test_transitions import FakeUbertooth, patch_host_api


def setUpModule():
    patch_host_api()


SKIPPED = ("skipped case", "skipped cases")


def event(sequence, kind, status="running", **details):
    return RunEvent(1, "demo-run", sequence, "2026-11-05T09:12:33.004",
                    kind, status, details)


def rendered(*events) -> str:
    stream = io.StringIO()
    sink = TerminalEventSink(stream)
    for item in events:
        sink.emit(item)
    return stream.getvalue()


class SharedWordsTests(unittest.TestCase):
    """The words the live stream and the report both use (rawlog)."""

    def test_the_result_tag_is_the_execution_result_not_a_verdict(self):
        self.assertEqual("[X]", RESULT_TAG)
        # A command the harness expects to exit nonzero is still just a
        # result here, so the tag may not be read as a pass or a failure.
        for code in (0, 1, -9):
            text = rawlog.result_text(
                CommandResult(("ubertooth-util", "-v"), code, "", "", 0.25, False)
            )
            self.assertTrue(text.startswith("[X] ubertooth-util exit "), text)
            self.assertNotIn("PASS", text)
            self.assertNotIn("FAIL", text)

    def test_a_banner_is_three_full_width_lines(self):
        text = rawlog.banner("TEST", "BLE settings")
        first, middle, last, empty = text.split("\n")
        self.assertEqual("#" * rawlog.BANNER_WIDTH, first)
        self.assertEqual("TEST: BLE settings", middle)
        self.assertEqual(first, last)
        self.assertEqual("", empty)
        # A case's banner nests inside a test's: same width, lighter rule.
        case = rawlog.banner("CASE", "Set and read back the access address",
                             rule=rawlog.CASE_RULE)
        self.assertEqual("-" * rawlog.BANNER_WIDTH, case.split("\n")[0])

    def test_every_status_a_run_records_has_a_short_verdict_word(self):
        self.assertEqual("PASS", rawlog.verdict("passed"))
        self.assertEqual("FAIL", rawlog.verdict("failed"))
        self.assertEqual("SKIP", rawlog.verdict("skipped"))
        self.assertEqual("CANCELED", rawlog.verdict("canceled"))
        self.assertEqual("N/A", rawlog.verdict("not_applicable"))
        self.assertEqual("BLOCKED", rawlog.verdict("blocked"))
        # A status this code does not know is still stated, not dropped.
        self.assertEqual("SOMETHING_NEW", rawlog.verdict("something_new"))

    def test_each_state_step_stage_has_a_plain_label(self):
        self.assertEqual("STEP", rawlog.stage_label(STAGE_TRANSITION))
        self.assertEqual("BASELINE", rawlog.stage_label(STAGE_BASELINE))
        self.assertEqual("CLEANUP", rawlog.stage_label(STAGE_CLEANUP))
        self.assertEqual("STATE", rawlog.stage_label(STAGE_STATE))
        # Runs recorded before the two reads were named apart still read as
        # what they were: a check of a board's state.
        self.assertEqual("STATE", rawlog.stage_label("guard"))

    def test_a_count_is_written_with_its_own_plural_and_a_zero_is_left_out(self):
        self.assertEqual("1 skipped case", rawlog.count_text(1, *SKIPPED))
        self.assertEqual("3 skipped cases", rawlog.count_text(3, *SKIPPED))
        for nothing in (0, -1, None, "2", 2.0, True, False):
            # True is not one skipped case, and neither is a count a record
            # carries as something other than a whole number.
            self.assertEqual("", rawlog.count_text(nothing, *SKIPPED), nothing)

    def test_a_value_is_put_on_one_line_whatever_break_it_carries(self):
        # Every character Python itself ends a line on, so nothing a record
        # carries can break the lines this vocabulary is read in.
        breaks = {"\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e",
                  "\x85", "\u2028", "\u2029"}
        for break_characters in breaks:
            self.assertEqual(
                "one two", rawlog.inline(f"one{break_characters}two"),
                repr(break_characters),
            )
        # Checked the same way the harness reads lines, so this list cannot
        # fall behind Python's: every one of them really ends a line.
        self.assertEqual(
            sorted(breaks),
            sorted(c for c in breaks if len(f"a{c}b".splitlines()) == 2),
        )
        # A run of them is one space, and nothing is left to split a line.
        self.assertEqual(["one two"], rawlog.inline("one\r\n\u2028two").splitlines())

    def test_expected_and_observed_are_separate_aligned_lines(self):
        self.assertEqual(
            ["    expected: 2470", "    observed: 2441"],
            rawlog.value_lines("2470", "2441"),
        )
        # One value read and nothing to compare it with.
        self.assertEqual(["    recorded: -75"], rawlog.value_lines(None, "-75"))
        self.assertEqual([], rawlog.value_lines(None, None))
        # The labels are the same width, so the values line up under one
        # another, and a value's own line ending cannot break that.
        lines = rawlog.value_lines("one\ntwo", "three")
        self.assertEqual(2, len(lines))
        self.assertEqual("    expected: one two", lines[0])
        self.assertEqual(
            {line.index(": ") for line in lines + rawlog.value_lines(None, "x")},
            {len("    expected")},
        )


class LiveStreamTests(unittest.TestCase):
    """How the terminal sink renders one run's records."""

    def test_a_test_opens_with_a_banner_and_what_it_runs_on(self):
        text = rendered(event(
            1, "test_started", test_name="BLE settings",
            invocation_id="006-BLE settings", host="Host_Linux",
            resources={"Ubertooth": "UT_0", "guard Ubertooth 1": "UT_1"},
        ))
        self.assertIn("#" * rawlog.BANNER_WIDTH + "\nTEST: BLE settings\n", text)
        self.assertIn(
            "[M] 006-BLE settings on Host_Linux; Ubertooth=UT_0, "
            "guard Ubertooth 1=UT_1\n",
            text,
        )

    def test_each_case_opens_with_its_own_banner(self):
        text = rendered(event(
            1, "check_started", phase="test",
            case_name="Set and read back the access address",
        ))
        self.assertIn(
            "-" * rawlog.BANNER_WIDTH
            + "\nCASE: Set and read back the access address\n",
            text,
        )
        # Preflight is a phase of its own and keeps its short line.
        self.assertIn("[preflight] Host version\n", rendered(event(
            1, "check_started", phase="preflight", check_name="Host version",
        )))

    def test_a_case_states_its_verdict_and_a_failure_says_why(self):
        self.assertIn("[PASS] Set and read back the access address\n", rendered(
            event(1, "check_finished", "passed",
                  case_name="Set and read back the access address"),
        ))
        text = rendered(event(
            2, "check_finished", "failed", case_name="Set the follow target",
            reason="Set the follow target: observed 11:22:33:44:55:66",
        ))
        self.assertIn("[FAIL] Set the follow target: observed "
                      "11:22:33:44:55:66\n", text)

    def test_a_test_states_its_verdict_with_what_it_counted(self):
        self.assertIn("[PASS] TEST BLE settings\n", rendered(event(
            1, "test_finished", "passed", test_name="BLE settings",
            failed_cases=0, failed_guard_checks=0, skipped_cases=0,
        )))
        text = rendered(event(
            2, "test_finished", "failed", test_name="Radio setting transitions",
            failed_cases=2, failed_guard_checks=1, skipped_cases=1,
            reason="Radio setting transitions: channel differs",
        ))
        # The reason opens with the test's own name in the record; the line
        # says it once.
        self.assertIn(
            "[FAIL] TEST Radio setting transitions (2 failed cases, "
            "1 failed state check, 1 skipped case): channel differs\n",
            text,
        )
        # A count of one is not written as a plural.
        self.assertIn("(1 failed case)\n", rendered(event(
            3, "test_finished", "failed", test_name="Raw receive",
            failed_cases=1, reason="",
        )))

    def test_a_state_step_names_its_phase_and_lines_its_values_up(self):
        def step(stage, status, **details):
            return rendered(event(1, "step_finished", status, stage=stage,
                                  step_name="guard Ubertooth 1 channel",
                                  **details))

        self.assertEqual(
            "[BASELINE PASS] guard Ubertooth 1 channel\n"
            "    recorded: 2470\n",
            step(STAGE_BASELINE, "passed", observed="2470"),
        )
        self.assertEqual(
            "[STATE FAIL] guard Ubertooth 1 channel unchanged: observed 2441\n"
            "    expected: 2470\n"
            "    observed: 2441\n",
            rendered(event(
                1, "step_finished", "failed", stage=STAGE_STATE,
                step_name="guard Ubertooth 1 channel unchanged",
                reason="guard Ubertooth 1 channel unchanged: observed 2441",
                expected="2470", observed="2441",
            )),
        )
        self.assertEqual(
            "[CLEANUP PASS] Restore the channel\n"
            "    expected: 2441\n    observed: 2441\n",
            rendered(event(1, "step_finished", "passed", stage=STAGE_CLEANUP,
                           step_name="Restore the channel",
                           expected="2441", observed="2441")),
        )
        # A step the harness ran again says which try this was.
        self.assertIn("(attempt 2)", step(STAGE_TRANSITION, "passed", attempt=2))

    def test_a_name_in_a_reason_is_said_once_and_a_reason_stays_one_line(self):
        # Every reason a case, step, or test records opens with its own name
        # (stateful.py, sequencer.py), which the verdict line has said.
        text = rendered(event(
            1, "step_finished", "failed", stage=STAGE_TRANSITION,
            step_name="Verify channel after set",
            reason="Verify channel after set: expected channel=2426,\nobserved 2441",
            expected="2426", observed="2441",
        ))
        self.assertEqual(
            "[STEP FAIL] Verify channel after set: expected channel=2426, "
            "observed 2441\n"
            "    expected: 2426\n"
            "    observed: 2441\n",
            text,
        )
        # A reason that names something else is kept whole.
        self.assertIn(
            "[FAIL] Guard Ubertooths baseline: guard Ubertooth 1 squelch was "
            "not reported\n",
            rendered(event(1, "check_finished", "failed",
                           case_name="Guard Ubertooths baseline",
                           reason="guard Ubertooth 1 squelch was not reported")),
        )

    def test_a_record_from_another_version_cannot_end_the_run_from_here(self):
        # A live display failure must never interrupt hardware work, and
        # every sink failure reaches the run (CompositeEventSink), so a
        # status or a count this code did not expect is still only text.
        text = rendered(
            event(1, "step_finished", "wedged", stage=7, step_name=None,
                  reason=None, expected=None, observed=0),
            event(2, "test_finished", "failed", test_name="Raw receive",
                  failed_cases=True, skipped_cases="2", reason=None),
        )
        self.assertIn("[7 WEDGED] step\n    recorded: 0\n", text)
        self.assertIn("[FAIL] TEST Raw receive\n", text)

    def test_what_a_host_or_a_bench_cannot_run_states_that_as_a_verdict(self):
        self.assertIn("[N/A] Host CLI surface / follow -h:", rendered(event(
            1, "not_applicable", "running", test_name="Host CLI surface",
            case_name="follow -h", host="Host_Mac", reason="macOS has no BlueZ",
        )))
        self.assertIn("[BLOCKED] Recover AFH map:", rendered(event(
            2, "blocked", "blocked", test_name="Recover AFH map",
            host="Host_Linux", reason="no ESP32 Classic peer is registered",
        )))


# One run's records, as a passing run of one test on one board writes them.
def summary_events() -> list[dict]:
    manifest = {
        "plan_name": "fast",
        "suite": "fast",
        "host": "Host_Bench",
        "environment": {
            "os": {"system": "Linux", "distribution": "24.04",
                   "release": "6.8.0", "architecture": "x86_64"},
        },
        "sources": [
            {"name": "Ubertooth", "requested_ref": "main",
             "commit": "9903b4251f" + "0" * 30, "short_commit": "9903b42",
             "expected_revision": "git-9903b42",
             "repository": "https://example.invalid/ubertooth.git"},
            # `--libbtbb-ref fork-pin` resolves to the commit the Ubertooth
            # commit pins, and records that name beside it.
            {"name": "libbtbb", "requested_ref": "7e1cdf0336" + "1" * 30,
             "ref_name": "fork-pin", "commit": "7e1cdf0336" + "1" * 30,
             "short_commit": "7e1cdf0", "expected_revision": "git-7e1cdf0",
             "repository": "https://example.invalid/libbtbb.git"},
        ],
        "invocations": [{
            "id": "001-BLE settings", "test_name": "BLE settings",
            "resources": {"Ubertooth": "UT_0"},
        }],
        "artifacts": [],
    }
    def record(sequence, kind, outcome, timestamp, **details):
        return {
            "schema_version": 1, "run_id": "20261105T091233Z-abcdef123456",
            "sequence": sequence, "timestamp": timestamp,
            "event_type": kind, "status": outcome, "details": details,
        }

    return [
        record(1, "run_started", "running", "2026-11-05T09:12:33.004",
               plan_name="fast", suite="fast", host="Host_Bench"),
        record(2, "manifest", "recorded", "2026-11-05T09:12:33.100",
               manifest=manifest),
        record(3, "check_started", "running", "2026-11-05T09:12:33.200",
               phase="preflight", check_id="preflight-001-host-version",
               check_name="Host version", host="Host_Bench",
               expected_revision="git-9903b42"),
        record(4, "output_checks", "passed", "2026-11-05T09:12:33.260",
               phase="preflight", check_id="preflight-001-host-version",
               checks=[
                   {"kind": "expected", "name": "Host version", "found": True,
                    "matched": "Host tools version: libubertooth 1.07 (git-9903b42)"},
                   {"kind": "expected", "name": "Host version libbtbb",
                    "found": True,
                    "matched": "Host tools version: libubertooth 1.07 "
                               "(git-9903b42), libbtbb 1.3 (git-7e1cdf0)"},
               ]),
        record(5, "check_finished", "passed", "2026-11-05T09:12:33.300",
               phase="preflight", check_id="preflight-001-host-version",
               check_name="Host version", reason=""),
        record(6, "check_started", "running", "2026-11-05T09:12:33.400",
               phase="preflight", check_id="preflight-002-firmware",
               check_name="Firmware version", host="Host_Bench",
               resource="UT_0", resource_identity={"serial": "a" * 32},
               expected_revision="git-9903b42"),
        record(7, "check_finished", "passed", "2026-11-05T09:12:33.500",
               phase="preflight", check_id="preflight-002-firmware",
               check_name="Firmware version", reason=""),
        record(8, "test_started", "running", "2026-11-05T09:12:33.600",
               invocation_id="001-BLE settings", test_name="BLE settings",
               host="Host_Bench", resources={"Ubertooth": "UT_0"}),
        record(9, "test_finished", "passed", "2026-11-05T09:14:51.900",
               invocation_id="001-BLE settings", test_name="BLE settings",
               reason="", failed_cases=0, skipped_cases=1),
        record(10, "run_finished", "passed", "2026-11-05T09:14:52.000",
               run_id="20261105T091233Z-abcdef123456", status="passed",
               passed=True, requested_preflight_checks=2,
               completed_preflight_checks=2, requested_tests=1,
               completed_tests=1, failure_reason="", duration_s=138.9,
               skipped_cases=1),
    ]


class ConsoleSummaryTests(unittest.TestCase):
    """The block a tailed run ends with identifies the run on its own."""

    def summary(self, events=None) -> str:
        return report.console_summary(events or summary_events(),
                                      report="runs/demo/report.md")

    def test_it_names_when_the_run_started_in_utc_and_how_long_it_took(self):
        text = self.summary()
        self.assertIn("started (UTC)      2026-11-05T09:12:33.004 (ran 139.0s)",
                      text)

    def test_it_names_each_source_by_ref_and_exact_commit(self):
        text = self.summary()
        self.assertIn(f"Ubertooth          main = 9903b4251f{'0' * 30} "
                      "(git-9903b42)", text)
        # A ref chosen elsewhere (the libbtbb a commit pins) is named as that.
        self.assertIn(f"libbtbb            fork-pin at 7e1cdf0336{'1' * 30} "
                      "(git-7e1cdf0)", text)

    def test_the_version_line_is_one_line_however_it_was_recorded(self):
        # A build keeps the whole answer `-V` gave, which is more than one
        # line, and every row of this block is read by its alignment.
        answer = (
            "Host tools version: libubertooth 1.07 (git-9903b42), "
            "libbtbb 1.3 (git-7e1cdf0)\n"
            "libbtbb loaded from: /usr/local/lib/libbtbb.so.1\n"
        )
        events = summary_events()
        # No preflight output of its own: the build's record is the source.
        events = [item for item in events
                  if item["event_type"] != "output_checks"]
        events[1]["details"]["manifest"]["build"] = {"host_version": answer}
        rows = [line for line in self.summary(events).splitlines()
                if line.startswith("  ")]
        self.assertIn(
            "  host tools         Host tools version: libubertooth 1.07 "
            "(git-9903b42), libbtbb 1.3 (git-7e1cdf0)",
            rows,
        )
        self.assertFalse([line for line in rows if "loaded from" in line])
        # A build that recorded something else still says what it said, on
        # one line.
        events[1]["details"]["manifest"]["build"] = {"host_version": "odd\nanswer"}
        self.assertIn("  host tools         odd answer",
                      self.summary(events).splitlines())

    def test_a_reason_keeps_the_characters_it_was_recorded_with(self):
        events = summary_events()
        events[-1]["status"] = "failed"
        events[-1]["details"].update(
            status="failed", passed=False,
            failure_reason="Failed capture files: no file matched "
                           "'captures/**/*.pcap' or `-q -`",
        )
        self.assertIn(
            "  reason             Failed capture files: no file matched "
            "'captures/**/*.pcap' or `-q -`",
            self.summary(events).splitlines(),
        )

    def test_it_carries_the_version_line_the_host_tools_printed(self):
        # Both halves: the libubertooth and libbtbb versions and commits the
        # tools themselves reported, which is the only thing that says which
        # library they load (#294).
        self.assertIn(
            "host tools         Host tools version: libubertooth 1.07 "
            "(git-9903b42), libbtbb 1.3 (git-7e1cdf0)",
            self.summary(),
        )

    def test_it_names_each_device_by_alias_serial_and_firmware(self):
        self.assertIn(
            f"device UT_0        Ubertooth, serial {'a' * 32}: "
            "firmware git-9903b42 (verified)",
            self.summary(),
        )

    def test_it_ends_with_the_totals_and_the_verdict(self):
        text = self.summary()
        self.assertIn("preflight          2/2 checks passed", text)
        self.assertIn("tests              1/1 passed", text)
        self.assertIn("also               1 skipped case", text)
        self.assertIn("report             runs/demo/report.md", text)
        self.assertTrue(text.rstrip().endswith("=" * rawlog.BANNER_WIDTH), text)
        self.assertIn("VERDICT: PASS", text)

    def test_a_failed_run_says_so_and_gives_the_reason_as_plain_text(self):
        events = summary_events()
        events[-1]["status"] = "failed"
        events[-1]["details"].update(
            status="failed", passed=False, completed_tests=0,
            failure_reason="BLE settings: the access address read back 8e89bed6",
            blocked_tests=1,
        )
        text = self.summary(events)
        self.assertIn("VERDICT: FAIL", text)
        self.assertIn("tests              0/1 passed", text)
        self.assertIn("also               1 skipped case, 1 test blocked", text)
        self.assertIn("reason             BLE settings: the access address "
                      "read back 8e89bed6", text)

    def test_a_run_that_failed_before_its_manifest_still_identifies_itself(self):
        events = summary_events()[:1]
        events[0]["details"]["sources"] = [{
            "name": "Ubertooth", "requested_ref": "HEAD", "commit": "c" * 40,
            "short_commit": "ccccccc", "expected_revision": "git-ccccccc",
            "repository": "https://example.invalid/ubertooth.git",
        }]
        text = report.console_summary(events)
        self.assertIn(f"Ubertooth          HEAD = {'c' * 40}", text)
        self.assertIn("VERDICT: INCOMPLETE (NO RUN_FINISHED RECORD)", text)

    def test_a_long_alias_does_not_cost_the_block_its_alignment(self):
        events = summary_events()
        manifest = events[1]["details"]["manifest"]
        manifest["invocations"][0]["resources"] = {"Ubertooth": "UT_Bench_Spare0"}
        for item in events:
            if item["details"].get("resource") == "UT_0":
                item["details"]["resource"] = "UT_Bench_Spare0"
        rows = [
            line for line in self.summary(events).splitlines()
            if line.startswith("  ")
        ]
        self.assertTrue(any("device UT_Bench_Spare0" in line for line in rows))
        # Every row's value starts in the same column: one past the longest
        # label this run has.
        column = len("  device UT_Bench_Spare0 ")
        for line in rows:
            self.assertEqual(" ", line[column - 1], line)
            self.assertNotEqual(" ", line[column], line)

    def test_a_reason_that_carries_a_newline_stays_on_its_own_row(self):
        events = summary_events()
        events[-1]["status"] = "failed"
        events[-1]["details"].update(
            status="failed", passed=False,
            failure_reason="BLE settings: output was\n  Access address: 8e89bed6",
        )
        text = self.summary(events)
        self.assertIn("  reason             BLE settings: output was   "
                      "Access address: 8e89bed6\n", text)

    def test_a_record_with_no_plan_or_host_leaves_those_rows_out(self):
        # A failed `build` command records a run of its own, with no plan and
        # no host: a row saying " / " would say nothing.
        events = summary_events()[:1]
        events[0]["details"] = {"sources": []}
        text = self.summary(events)
        self.assertNotIn("plan / suite", text)
        self.assertNotIn("host", text)
        self.assertIn("VERDICT:", text)

    def test_writing_it_never_raises_and_never_touches_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stream = io.StringIO()
            # No run.jsonl at all: nothing to say, and no error either.
            report.write_console_summary(root, stream)
            self.assertEqual("", stream.getvalue())
            (root / "run.jsonl").write_text("not json\n")
            report.write_console_summary(root, stream)
            self.assertEqual("", stream.getvalue())
            (root / "run.jsonl").write_text(
                "\n".join(json.dumps(item) for item in summary_events()) + "\n"
            )
            report.write_console_summary(root, stream)
            self.assertIn("VERDICT: PASS", stream.getvalue())
            # No report.md was written, so the block does not name one: a
            # row pointing at a file that is not there would mislead.
            self.assertNotIn("report ", stream.getvalue())
            (root / "report.md").write_text("# report\n")
            stream = io.StringIO()
            report.write_console_summary(root, stream)
            self.assertIn(str(root / "report.md"), stream.getvalue())


#: The Ubertooth file that derives a build's version string, as a commit whose
#: build names a release has it (`ubertooth_harness.release`).
UBERTOOTH_RELEASE_FILE = """\
set(LATEST_RELEASE "2020-12-R1")
set(RELEASE_TAG_MATCH
\t--match "[0-9][0-9][0-9][0-9]-[0-9][0-9]-R[0-9]*"
\t--match "[0-9][0-9][0-9][0-9]-[0-9][0-9]-RC[0-9]*"
\t--match "[0-9][0-9][0-9][0-9]-[0-9][0-9]-DEV[0-9]*")
set(RELEASE_TAG_FORM "^[0-9][0-9][0-9][0-9]-[0-9][0-9]-(R|RC|DEV)[0-9]+$")
"""
#: libbtbb's own, as the commit the fork pins has it: it looks again at the tag
#: name git handed back, and names no floor.
LIBBTBB_RELEASE_FILE = """\
set(BTBB_RELEASE_TAG_GLOBS
\t"[0-9][0-9][0-9][0-9]-[0-9][0-9]-R[0-9]*"
\t"[0-9][0-9][0-9][0-9]-[0-9][0-9]-RC[0-9]*"
\t"[0-9][0-9][0-9][0-9]-[0-9][0-9]-DEV[0-9]*")
function(btbb_release_from_describe out describe)
\tif(NOT text MATCHES "^[0-9][0-9][0-9][0-9]-[0-9][0-9]-(R|RC|DEV)[0-9]+$")
\t\tset(${out} "" PARENT_SCOPE)
\tendif()
endfunction()
"""
#: The release the sample source is tagged at, so a build of it reports a
#: release name rather than a commit hash.
SAMPLE_RELEASE = "2026-11-DEV1"


def source_repo(root: Path) -> tuple[Path, str]:
    """A real checkout at a real commit, as every resolved source is.

    It carries the version files the fork derives a release name in, and sits
    on a release tag, so the run resolves the release the host tools report
    rather than being refused as a commit from before the fork named releases.
    Returns the checkout and that release name.
    """
    source = root / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    (source / "README.md").write_text("source\n")
    for name, text in (
        ("host/cmake/set_release.cmake", UBERTOOTH_RELEASE_FILE),
        ("cmake/set_release.cmake", LIBBTBB_RELEASE_FILE),
    ):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Test",
         "-c", "user.email=test@example.invalid", "commit", "-qm", "source"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "tag", SAMPLE_RELEASE], check=True)
    return source, SAMPLE_RELEASE


class BenchTools:
    """Answer the host version and watchdog reads; forward everything else.

    The fake boards model the option parsing of ``ubertooth-util`` and
    ``ubertooth-btle``; the host tools' own version line and the firmware's
    watchdog record are the run's, not a board's, so they are answered here.
    """

    def __init__(self, bench, revision):
        self.bench = bench
        self.line = f"Host tools version: {revision} (libbtbb {revision})\n"
        self.answered = False

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        output = ""
        if not self.answered and command == ("ubertooth-util", "-V"):
            self.answered = True
            output = self.line
        elif command[0] == "ubertooth-util" and command[-1] == "-w":
            output = watchdog_samples.report()
        if output:
            if on_output:
                on_output("stdout", output)
            return CommandResult(command, 0, output, "", 0.02, False)
        return self.bench.run(argv, timeout_s=timeout_s,
                              cancellation=cancellation, on_output=on_output)


def run_sample(
    root: Path, test_name="Radio setting transitions", live=None,
    resolve_libbtbb=False,
) -> tuple[int, str, str]:
    """One whole `run-suite` of ``test_name`` against fake boards.

    Returns its exit status, everything it wrote to stderr, which is what a
    person tailing the run sees, and its stdout. ``live`` collects the
    stderr of a run that is expected to raise, and ``resolve_libbtbb`` names
    a libbtbb source, which is what makes the run record its own start
    before the sequencer takes over.
    """
    source, revision = source_repo(root)
    (root / "hardware.yaml").write_text(hardware((A, B)))
    (root / "plan.yaml").write_text(
        step_plan(test_name, extra="        coverage: smoke\n")
    )
    bench = Bench(
        FakeUbertooth(serial=A, firmware=revision),
        FakeUbertooth(serial=B, firmware=revision),
    )
    live, out = live if live is not None else io.StringIO(), io.StringIO()
    with (
        mock.patch.object(cli, "_detected_platform", return_value="linux"),
        mock.patch("ubertooth_harness.cli.SubprocessCommandExecutor",
                   return_value=BenchTools(bench, revision)),
        redirect_stderr(live),
        redirect_stdout(out),
    ):
        code = cli.main([
            "run-suite", "--plan", str(root / "plan.yaml"), "--suite", "fast",
            "--hardware", str(root / "hardware.yaml"), "--host", "Host_Linux",
            "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
            *(["--libbtbb-source", str(source), "--libbtbb-ref", "HEAD"]
              if resolve_libbtbb else []),
            "--run-id", "console-demo", "--output", str(root / "runs"),
        ])
    return code, live.getvalue(), out.getvalue()


class WholeRunTests(unittest.TestCase):
    """A whole run through the CLI, as a reader of the terminal sees it."""

    def test_a_tailed_run_reads_as_banners_verdicts_and_a_closing_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, live, _out = run_sample(root)

            self.assertEqual(0, code, live)
            # The test it is on, and each case inside it, open with a banner.
            self.assertIn(
                "#" * rawlog.BANNER_WIDTH + "\nTEST: Radio setting transitions\n",
                live,
            )
            self.assertIn("-" * rawlog.BANNER_WIDTH + "\nCASE: ", live)
            # Verdicts are stated, and the result tag is never one of them.
            self.assertIn("[PASS] TEST Radio setting transitions", live)
            self.assertIn(f"{RESULT_TAG} ubertooth-util exit 0", live)
            self.assertNotIn("[R] ", live)
            # The three state phases are named in plain words, each read
            # followed by its own value lines.
            self.assertIn("[BASELINE PASS] guard Ubertooth 1 firmware\n"
                          "    recorded: ", live)
            self.assertIn("[STATE PASS] guard Ubertooth 1 firmware unchanged\n"
                          "    expected: ", live)
            self.assertIn("[CLEANUP PASS] ", live)
            # And the run closes with the block that identifies it, once.
            self.assertIn("VERDICT: PASS", live)
            self.assertEqual(1, live.count("RUN SUMMARY"))
            self.assertIn(f"device UT_0        Ubertooth, serial {A}: "
                          f"firmware {SAMPLE_RELEASE}", live)
            self.assertIn("host tools         Host tools version: "
                          f"{SAMPLE_RELEASE} (libbtbb ", live)
            self.assertIn(str(root / "runs" / "console-demo" / "report.md"), live)

    def test_the_summary_is_on_stderr_so_stdout_stays_the_summary_json(self):
        with tempfile.TemporaryDirectory() as directory:
            code, live, out = run_sample(Path(directory))
            self.assertEqual(0, code, live)
            # stdout is the machine-readable summary and nothing else.
            self.assertEqual("passed", json.loads(out)["status"])
            self.assertNotIn("VERDICT", out)
            self.assertIn("VERDICT: PASS", live)

    def test_a_build_records_a_run_so_it_closes_with_the_block_too(self):
        # `build` is a run of its own in the record, and a failed one already
        # printed the block through _finish_failed_run; a build that passes
        # has to as well, or the block depends on how the run went.
        built = SimpleNamespace(to_manifest=lambda: {}, artifacts=())
        for outcome, patched in (
            ("passed", mock.patch.object(cli, "_build", return_value=built)),
            ("failed", mock.patch.object(cli, "_build",
                                         side_effect=BuildError("no compiler"))),
        ):
            with self.subTest(outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, _short = source_repo(root)
                live, out = io.StringIO(), io.StringIO()
                with patched, redirect_stderr(live), redirect_stdout(out):
                    try:
                        cli.main([
                            "build", "--ubertooth-source", str(source),
                            "--ubertooth-ref", "HEAD",
                            "--libbtbb-source", str(source), "--libbtbb-ref", "HEAD",
                            "--output", str(root / "build"),
                        ])
                    except BuildError:
                        pass  # the command's own failure, reported by main()
                printed = live.getvalue()
                self.assertEqual(1, printed.count("RUN SUMMARY"), printed)
                self.assertIn(f"VERDICT: {'PASS' if outcome == 'passed' else 'FAIL'}",
                              printed)
                self.assertIn("Ubertooth          HEAD = ", printed)
                self.assertIn("plan / suite       build / build", printed)
                # A build names no host, so that row is left out rather
                # than printed empty.
                self.assertNotIn("  host ", printed)

    def test_the_report_keeps_the_same_records_and_every_command_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, live, _out = run_sample(root)
            self.assertEqual(0, code, live)
            run_dir = root / "runs" / "console-demo"
            events = [json.loads(line)
                      for line in (run_dir / "run.jsonl").read_text().splitlines()]
            # The recorded stage of every state step is the plain word the
            # label comes from, so the record says which phase it was.
            stages = {
                item["details"]["stage"] for item in events
                if item["event_type"] == "step_finished"
            }
            self.assertTrue(stages <= {STAGE_TRANSITION, STAGE_CLEANUP,
                                       STAGE_BASELINE, STAGE_STATE}, stages)
            self.assertIn(STAGE_BASELINE, stages)
            self.assertIn(STAGE_STATE, stages)
            # Every response a command gave is still in the record and in the
            # transcript, under the new result tag.
            text = (run_dir / "report.md").read_text()
            outputs = [
                item["details"]["text"] for item in events
                if item["event_type"] == "command_output"
            ]
            self.assertTrue(outputs)
            for line in {line for text_block in outputs
                         for line in text_block.splitlines() if line.strip()}:
                self.assertIn(line, text)
            self.assertIn(f"{RESULT_TAG} ubertooth-util exit 0", text)
            self.assertNotIn("[R] ", text)
            # The transcript says the same phase label and verdict the live
            # stream said, so the two read as one vocabulary.
            self.assertIn("[S] BASELINE PASS: guard Ubertooth 1 firmware", text)
            self.assertIn("[S] STATE PASS: guard Ubertooth 1 firmware unchanged",
                          text)
            self.assertIn("[S] CLEANUP PASS: ", text)
            self.assertIn("[S] STEP PASS: ", text)
            self.assertNotIn("[S] transition passed", text)

    def test_a_run_that_stops_before_its_tests_still_closes_with_the_block(self):
        # A build, flash, or bootloader failure ends the run before the
        # sequencer: the block still has to say which commits it was about.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _short = source_repo(root)
            (root / "hardware.yaml").write_text(hardware((A, B)))
            (root / "plan.yaml").write_text(
                step_plan("Radio setting transitions",
                          extra="        coverage: smoke\n")
            )
            live, out = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(cli, "_detected_platform", return_value="linux"),
                mock.patch.object(cli, "_prepare_run",
                                  side_effect=HarnessError("the build failed")),
                redirect_stderr(live),
                redirect_stdout(out),
            ):
                code = cli.main([
                    "run-suite", "--plan", str(root / "plan.yaml"),
                    "--suite", "fast", "--hardware", str(root / "hardware.yaml"),
                    "--host", "Host_Linux", "--ubertooth-source", str(source),
                    "--ubertooth-ref", "HEAD", "--libbtbb-source", str(source),
                    "--libbtbb-ref", "HEAD", "--build", "--no-flash-build",
                    "--run-id", "failed-build", "--output", str(root / "runs"),
                ])

            printed = live.getvalue()
            self.assertEqual(2, code)
            self.assertIn("VERDICT: FAIL", printed)
            self.assertIn("reason             the build failed", printed)
            self.assertIn("Ubertooth          HEAD = ", printed)
            self.assertIn(str(root / "runs" / "failed-build" / "report.md"),
                          printed)
            # One block, not two: the run did not reach the sequencer.
            self.assertEqual(1, printed.count("RUN SUMMARY"))
            # It never reached preflight, so it claims no counts: `0/0
            # checks passed` would read as a run that asked for none.
            self.assertNotIn("preflight ", printed)
            self.assertNotIn("tests ", printed)

    def test_a_canceled_stage_closes_with_the_block_and_says_canceled(self):
        # A cancellation during the flash or bootloader stage ends the run
        # canceled rather than failed (FlashCancelled), and the block is how
        # a tailed run says which it was.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _short = source_repo(root)
            (root / "hardware.yaml").write_text(hardware((A, B)))
            (root / "plan.yaml").write_text(
                step_plan("Radio setting transitions",
                          extra="        coverage: smoke\n")
            )
            live, out = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(cli, "_detected_platform", return_value="linux"),
                mock.patch.object(
                    cli, "_prepare_run",
                    side_effect=FlashCancelled("the flash stage was canceled"),
                ),
                redirect_stderr(live),
                redirect_stdout(out),
            ):
                code = cli.main([
                    "run-suite", "--plan", str(root / "plan.yaml"),
                    "--suite", "fast", "--hardware", str(root / "hardware.yaml"),
                    "--host", "Host_Linux", "--ubertooth-source", str(source),
                    "--ubertooth-ref", "HEAD", "--libbtbb-source", str(source),
                    "--libbtbb-ref", "HEAD", "--build", "--no-flash-build",
                    "--run-id", "canceled-stage", "--output", str(root / "runs"),
                ])

            printed = live.getvalue()
            self.assertEqual(2, code)
            self.assertEqual(1, printed.count("RUN SUMMARY"), printed)
            self.assertIn("VERDICT: CANCELED", printed)
            self.assertIn("reason             the flash stage was canceled",
                          printed)

    def test_a_sequencer_that_raises_still_closes_with_the_block(self):
        # Nothing in the sequencer is meant to raise, but a case builder or
        # an internal error can; the run's records are on disk either way.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = io.StringIO()
            with (
                mock.patch.object(NamedTestSequencer, "run",
                                  side_effect=RuntimeError("a case would not build")),
                self.assertRaises(RuntimeError),
            ):
                # A libbtbb source, so the run has a record of its own
                # before the sequencer takes over.
                run_sample(root, live=live, resolve_libbtbb=True)

            printed = live.getvalue()
            self.assertEqual(1, printed.count("RUN SUMMARY"))
            # No run_finished record was written, and the block says so
            # rather than claiming an outcome.
            self.assertIn("VERDICT: INCOMPLETE (NO RUN_FINISHED RECORD)", printed)
            self.assertIn("Ubertooth          HEAD = ", printed)

    def test_an_installed_tools_run_that_raises_closes_with_the_block_too(self):
        # A run that builds nothing has no record of its own until the
        # sequencer opens one, so this is the path where the block depends on
        # what the sequencer had written when it raised. An error out of the
        # executor is the realistic one: by then the run has its start and
        # its manifest.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, revision = source_repo(root)
            (root / "hardware.yaml").write_text(hardware((A, B)))
            (root / "plan.yaml").write_text(
                step_plan("Radio setting transitions",
                          extra="        coverage: smoke\n")
            )
            tools = BenchTools(
                Bench(FakeUbertooth(serial=A, firmware=revision),
                      FakeUbertooth(serial=B, firmware=revision)),
                revision,
            )
            calls = []

            def fails_once_preflight_has_begun(argv, **named):
                calls.append(tuple(argv))
                if len(calls) > 1:
                    raise RuntimeError("the executor broke")
                return tools.run(argv, **named)

            live, out = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(cli, "_detected_platform", return_value="linux"),
                mock.patch("ubertooth_harness.cli.SubprocessCommandExecutor",
                           return_value=SimpleNamespace(
                               run=fails_once_preflight_has_begun)),
                redirect_stderr(live),
                redirect_stdout(out),
                self.assertRaises(RuntimeError),
            ):
                cli.main([
                    "run-suite", "--plan", str(root / "plan.yaml"),
                    "--suite", "fast", "--hardware", str(root / "hardware.yaml"),
                    "--host", "Host_Linux", "--ubertooth-source", str(source),
                    "--ubertooth-ref", "HEAD", "--run-id", "broken-executor",
                    "--output", str(root / "runs"),
                ])

            printed = live.getvalue()
            self.assertEqual(1, printed.count("RUN SUMMARY"), printed)
            self.assertIn("VERDICT: INCOMPLETE (NO RUN_FINISHED RECORD)", printed)
            self.assertIn("Ubertooth          HEAD = ", printed)
            # The records the block was compiled from are the run's own.
            run_log = root / "runs" / "broken-executor" / "run.jsonl"
            kinds = [json.loads(line)["event_type"]
                     for line in run_log.read_text().splitlines()]
            self.assertEqual("run_started", kinds[0])
            self.assertIn("manifest", kinds)
            self.assertNotIn("run_finished", kinds)


if __name__ == "__main__":  # pragma: no cover - renders the sample by hand
    # `python -m tests.test_console_output [test name]` prints what a person
    # tailing that run would see, against the fake boards.
    patch_host_api()
    with tempfile.TemporaryDirectory() as directory:
        _name = sys.argv[1] if len(sys.argv) > 1 else "Radio setting transitions"
        _code, _live, _out = run_sample(Path(directory), _name)
        sys.stdout.write(_live)
        sys.stdout.write(f"\n$ echo $?\n{_code}\n")
