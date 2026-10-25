"""Long command output: run.jsonl keeps the first part, output/ keeps it all (#49)."""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.events import REPORT, RUN_LOG
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.rawlog import (
    OUTPUT_DIR,
    RECORD_OUTPUT_EVENTS,
    CommandOutputLog,
    OmittedOutput,
    line_shape,
)
from ubertooth_harness.sequencer import NamedTestSequencer

from tests.test_sequencer import run_contract


VERSION = "libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
BLOCKS = 1000


def block(n: int) -> str:
    return f"rx block timestamp {1000 + n} * 100 nanoseconds\n"


class FloodExecutor:
    """ubertooth-util -H answering with the version, then a flood of
    per-block stderr lines like ubertooth-dump prints (one chunk each)."""

    def __init__(self, flood_calls=(1,), stream=True, blocks=BLOCKS):
        self.calls = 0
        self.blocks = blocks
        self.flood_calls = flood_calls
        self.stream = stream

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        self.calls += 1
        chunks = [("stdout", VERSION)]
        if self.calls in self.flood_calls:
            chunks += [("stderr", block(n)) for n in range(self.blocks)]
            chunks.append(("stdout", "last stdout line\n"))
        if self.stream and on_output:
            for stream, text in chunks:
                on_output(stream, text)
        stdout = "".join(t for s, t in chunks if s == "stdout")
        stderr = "".join(t for s, t in chunks if s == "stderr")
        return CommandResult(tuple(argv), 0, stdout, stderr, 0.01, False)


def run(root: Path, executor) -> tuple:
    invocation = TestInvocation("001-Host version", "Host version", "Host_Mac", {}, {})
    manifest, checks = run_contract(root, invocation)
    summary = NamedTestSequencer(executor).run(
        (invocation,), root, run_id="named-run", manifest=manifest,
        preflight_checks=checks,
    )
    events = [json.loads(line) for line in (root / RUN_LOG).read_text().splitlines()]
    return summary, events


def outputs(events, command_id):
    return [e for e in events if e["event_type"] == "command_output"
            and e["details"]["command_id"] == command_id]


def finished(events, command_id):
    return next(e for e in events if e["event_type"] == "command_finished"
                and e["details"]["command_id"] == command_id)


PREFLIGHT = "preflight-001-host-version-command-1"


class SpillTests(unittest.TestCase):

    def test_flood_is_capped_in_run_jsonl_and_kept_whole_in_output_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, events = run(root, FloodExecutor())

            # Checks still see the full result.
            self.assertTrue(summary.passed, summary.failure_reason)
            recorded = outputs(events, PREFLIGHT)
            self.assertEqual(RECORD_OUTPUT_EVENTS, len(recorded))
            self.assertEqual(VERSION, recorded[0]["details"]["text"])

            done = finished(events, PREFLIGHT)["details"]
            name = f"{OUTPUT_DIR}/{PREFLIGHT}.log"
            self.assertEqual(name, done["output_file"])
            log = (root / name).read_text().splitlines()
            # Every line, in arrival order, stamped and tagged.
            self.assertEqual(BLOCKS + 2, len(log))
            self.assertRegex(log[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} \[O\] libubertooth 1\.1")
            self.assertEqual([f"[E] {block(n).rstrip()}" for n in range(BLOCKS)],
                             [line.split(" ", 1)[1] for line in log[1:-1]])
            self.assertTrue(log[-1].endswith("[O] last stdout line"))

            omitted = done["output_omitted"]
            left_out = BLOCKS - (RECORD_OUTPUT_EVENTS - 1)
            self.assertEqual({"stdout": 1, "stderr": left_out}, omitted["lines"])
            self.assertEqual(
                [{"stream": "stderr", "shape": "rx block timestamp N * N nanoseconds",
                  "count": left_out},
                 {"stream": "stdout", "shape": "last stdout line", "count": 1}],
                omitted["common_shapes"],
            )
            self.assertEqual(2, omitted["distinct_shapes"])

            report = (root / REPORT).read_text()
            self.assertIn(f"[M] full output in {name}; not in run.jsonl: "
                          f"1 stdout lines (17 characters), {left_out} stderr lines", report)
            self.assertIn(f"[M]   {left_out} x [E] rx block timestamp N * N nanoseconds", report)

    def test_short_output_stays_in_run_jsonl_without_an_output_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, events = run(root, FloodExecutor(flood_calls=()))
            self.assertTrue(summary.passed)
            self.assertFalse((root / OUTPUT_DIR).exists())
            self.assertNotIn("output_file", finished(events, PREFLIGHT)["details"])
            self.assertEqual([VERSION], [e["details"]["text"] for e in outputs(events, PREFLIGHT)])

    def test_output_of_a_non_streaming_executor_is_spilled_too(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 4096-character chunks: past the character limit, not the count.
            summary, events = run(root, FloodExecutor(stream=False, blocks=3000))
            self.assertTrue(summary.passed)
            done = finished(events, PREFLIGHT)["details"]
            text = (root / done["output_file"]).read_text()
            self.assertEqual(3000, text.count("[E] rx block timestamp"))
            self.assertIn("[O] last stdout line", text)

    def test_each_flooding_command_gets_its_own_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _summary, events = run(root, FloodExecutor(flood_calls=(1, 2)))
            files = sorted(e["details"]["output_file"] for e in events
                           if e["event_type"] == "command_finished"
                           and "output_file" in e["details"])
            self.assertEqual(2, len(set(files)))
            self.assertEqual(sorted(p.relative_to(root).as_posix()
                                    for p in (root / OUTPUT_DIR).iterdir()), files)


class PartsTests(unittest.TestCase):

    def test_line_shape_replaces_digit_runs(self):
        self.assertEqual("rx block timestamp N * N nanoseconds",
                         line_shape("rx block timestamp 283441 * 100 nanoseconds"))

    def test_omitted_lines_joined_across_chunks_and_trailing_text_counted(self):
        omitted = OmittedOutput()
        for text in ("rx block time", "stamp 1 * 100 nanoseconds\nrx block ",
                     "timestamp 2 * 100 nanoseconds\npartial"):
            omitted.add("stderr", text)
        summary = omitted.summary()
        self.assertEqual(3, summary["lines"]["stderr"])
        self.assertEqual(
            {"stream": "stderr", "shape": "rx block timestamp N * N nanoseconds", "count": 2},
            summary["common_shapes"][0],
        )

    def test_output_log_writes_partial_lines_on_close_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / OUTPUT_DIR / "x.log"
            moment = datetime.datetime(2026, 10, 25, 1, 2, 3, 456000,
                                       tzinfo=datetime.timezone.utc)
            log = CommandOutputLog(path)
            log.write(moment, "stdout", "one\ntw")
            log.write(moment, "stderr", "err\n")
            log.write(moment, "stdout", "o")
            log.close()
            self.assertEqual(
                "2026-10-25T01:02:03.456 [O] one\n"
                "2026-10-25T01:02:03.456 [E] err\n"
                "2026-10-25T01:02:03.456 [O] two\n",
                path.read_text(),
            )
            with self.assertRaises(FileExistsError):
                CommandOutputLog(path)


if __name__ == "__main__":
    unittest.main()
