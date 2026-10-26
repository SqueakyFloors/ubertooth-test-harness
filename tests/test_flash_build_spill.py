"""Long flash and build output: run.jsonl keeps the first and last part,
output/ keeps it all, as for test commands (#112)."""

import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.build import BuildError, Builder
from ubertooth_harness.events import REPORT, RUN_LOG, open_run_publisher
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.gui import failure_summary
from ubertooth_harness.rawlog import OUTPUT_DIR, RECORD_OUTPUT_EVENTS, RECORD_TAIL_EVENTS
from ubertooth_harness.report import load_events, write_report
from ubertooth_harness.flash import FlashReliabilityRunner

from tests.test_build import FakeBuildExecutor, make_plan
from tests import test_flash
from tests.test_flash import FakeClock, FakeExecutor


BLOCKS = 1000


def block(n: int) -> str:
    return f"block {n} of {BLOCKS} written\n"


def mine(events, command_id):
    return [e for e in events if e["details"].get("command_id") == command_id]


class FloodingDfuExecutor(FakeExecutor):
    """ubertooth-dfu printing one progress line per block before it finishes."""

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        if argv[0] == "ubertooth-dfu" and on_output is not None:
            for n in range(BLOCKS):
                on_output("stdout", block(n))
        return super().run(
            argv, timeout_s=timeout_s, cancellation=cancellation, on_output=on_output
        )


class FloodingBuildExecutor(FakeBuildExecutor):
    """host-build printing a compiler line per file, then failing."""

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None, cwd=None):
        step = next((c for c in self.plan.commands if c.argv == tuple(argv)), None)
        if step is not None and step.step_id == "host-build":
            for n in range(BLOCKS):
                on_output("stdout", f"[{n}%] Building C object file{n}.o\n")
            on_output("stderr", "error: last compiler error\n")
            return CommandResult(tuple(argv), 2, "", "", 0.01, False)
        return super().run(
            argv, timeout_s=timeout_s, cancellation=cancellation, on_output=on_output, cwd=cwd
        )


class FlashSpillTests(unittest.TestCase):

    def test_flash_output_is_capped_with_full_output_in_output_dir(self):
        helper = test_flash.FlashRunnerTests()
        with tempfile.TemporaryDirectory() as directory:
            request, _ = helper.prepare_request(directory, flashes=2)
            clock = FakeClock()
            executor = FloodingDfuExecutor(
                clock, {str(a.path): a.expected_revision for a in request.artifacts.values()}
            )
            results = Path(directory) / "results"
            publisher = open_run_publisher(results, "flash-run")
            owner = {"phase": "test", "invocation_id": "003-Flash", "case_id": "flash"}
            summary = FlashReliabilityRunner(executor, clock=clock).run(
                request, publisher, owner=owner, output_dir=results
            )
            write_report(results)

            self.assertTrue(summary.passed, summary.failure_reason)
            events = load_events(results / RUN_LOG)
            first = mine(events, "flash-0001")
            self.assertEqual(
                ["command_started"] + ["command_output"] * RECORD_OUTPUT_EVENTS
                + ["command_output_omitted"] + ["command_output"] * RECORD_TAIL_EVENTS
                + ["command_finished"],
                [e["event_type"] for e in first],
            )
            outputs = [e for e in first if e["event_type"] == "command_output"]
            self.assertEqual(block(0), outputs[0]["details"]["text"])
            # The tail ends with what the tool printed last.
            self.assertEqual("Verified firmware revision\n", outputs[-1]["details"]["text"])
            self.assertEqual(block(BLOCKS - 1), outputs[-2]["details"]["text"])
            # Every record keeps the owner and operation.
            for event in first[1:-1]:
                self.assertEqual("003-Flash", event["details"]["invocation_id"])
                self.assertEqual("firmware_flash", event["details"]["operation"])

            # Flash command IDs repeat per case: the file names the case too.
            name = f"{OUTPUT_DIR}/003-Flash-flash-0001.log"
            done = first[-1]["details"]
            self.assertEqual("passed", first[-1]["status"])
            self.assertEqual(name, done["output_file"])
            # The head holds block lines only; the tail ends with the result line.
            left_out = BLOCKS - RECORD_OUTPUT_EVENTS - (RECORD_TAIL_EVENTS - 1)
            self.assertEqual({"stdout": left_out, "stderr": 0},
                             done["output_omitted"]["lines"])
            log = (results / name).read_text().splitlines()
            self.assertEqual(BLOCKS + 1, len(log))
            self.assertTrue(log[0].endswith(f"[O] {block(0).rstrip()}"))
            self.assertTrue(log[-1].endswith("[O] Verified firmware revision"))
            # The second flash has its own file; short revision reads have none.
            self.assertTrue((results / OUTPUT_DIR / "003-Flash-flash-0002.log").is_file())
            self.assertNotIn("output_file", mine(events, "verify-0001")[-1]["details"])

            report = (results / REPORT).read_text()
            self.assertIn(f"[M] full output in {name}; left out of run.jsonl here: "
                          f"{left_out} stdout lines", report)
            self.assertLess(report.index(f"[M] full output in {name}"),
                            report.index(f"[O] {block(BLOCKS - 1).rstrip()}"))

            # The GUI log tail of a failed flash test shows how the flash ended.
            failed = events + [{
                "event_type": "test_finished", "status": "failed",
                "details": {"invocation_id": "003-Flash", "test_name": "Flash"},
            }]
            log_tail = failure_summary({}, failed)[0]["log"]
            self.assertEqual("Firmware version: git-kgv (API:1.07)", log_tail[-1])


class BuildSpillTests(unittest.TestCase):

    def test_build_output_is_capped_and_a_failed_build_shows_its_last_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            plan = make_plan(run_dir / "build")
            publisher = open_run_publisher(run_dir, "build-run")
            with self.assertRaises(BuildError):
                Builder(FloodingBuildExecutor(plan), publisher, run_dir=run_dir).run(plan)
            write_report(run_dir)

            events = load_events(run_dir / RUN_LOG)
            build = mine(events, "build-host-build")
            self.assertEqual(
                ["command_started"] + ["command_output"] * RECORD_OUTPUT_EVENTS
                + ["command_output_omitted"] + ["command_output"] * RECORD_TAIL_EVENTS
                + ["command_finished"],
                [e["event_type"] for e in build],
            )
            outputs = [e for e in build if e["event_type"] == "command_output"]
            self.assertEqual("[0%] Building C object file0.o\n", outputs[0]["details"]["text"])
            self.assertEqual("error: last compiler error\n", outputs[-1]["details"]["text"])

            name = f"{OUTPUT_DIR}/build-host-build.log"
            done = build[-1]
            self.assertEqual("failed", done["status"])
            self.assertEqual(name, done["details"]["output_file"])
            log = (run_dir / name).read_text().splitlines()
            self.assertEqual(BLOCKS + 1, len(log))
            self.assertTrue(log[-1].endswith("[E] error: last compiler error"))
            # Short build commands stay entirely in run.jsonl.
            self.assertNotIn("output_file", mine(events, "build-host-configure")[-1]["details"])

            # report.md shows the build's last lines, not only its first ones.
            report = (run_dir / REPORT).read_text()
            self.assertIn(f"[M] full output in {name}", report)
            self.assertIn("[E] error: last compiler error", report)
            self.assertIn(f"[O] [{BLOCKS - 1}%] Building C object file{BLOCKS - 1}.o", report)

            # So does the GUI failure summary of the failed build.
            build_failure = failure_summary({}, events)[0]
            self.assertEqual("build", build_failure["step"])
            self.assertEqual(
                [f"[{n}%] Building C object file{n}.o" for n in range(BLOCKS - 4, BLOCKS)]
                + ["error: last compiler error"],
                build_failure["log"],
            )

    def test_failure_summary_marks_left_out_build_output(self):
        details = {"phase": "build", "step_id": "host-build", "command_id": "build-host-build"}
        events = [
            {"event_type": "command_output", "status": "running",
             "details": {**details, "stream": "stdout", "text": "first line\npartial"}},
            {"event_type": "command_output_omitted", "status": "running",
             "details": {**details, "output_file": "output/build-host-build.log",
                         "output_omitted": {"lines": {"stdout": 7, "stderr": 2}}}},
            {"event_type": "command_output", "status": "running",
             "details": {**details, "stream": "stderr", "text": "error: last"}},
            {"event_type": "build_finished", "status": "failed",
             "details": {"reason": "Build host tools exited 2"}},
        ]

        self.assertEqual(
            [{"step": "build", "status": "failed", "reason": "Build host tools exited 2",
              "log": ["first line", "partial",
                      "[... 9 output lines left out; full output in "
                      "output/build-host-build.log ...]",
                      "error: last"]}],
            failure_summary({"status": "running"}, events),
        )


if __name__ == "__main__":
    unittest.main()
