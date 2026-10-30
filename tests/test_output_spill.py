"""Long command output: run.jsonl keeps the first part, output/ keeps it all (#49)."""

import datetime
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.events import REPORT, RUN_LOG, RunEvent, TerminalEventSink
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.gui import failure_summary
from ubertooth_harness.outputfiles import OUTPUT_DIR
from ubertooth_harness.rawlog import (
    RECORD_OUTPUT_BYTES,
    RECORD_OUTPUT_EVENTS,
    RECORD_TAIL_EVENTS,
    CappedOutput,
    CommandOutputFiles,
    OmittedOutput,
    line_shape,
    record_bytes,
)
from ubertooth_harness.sequencer import NamedTestSequencer

from tests.test_sequencer import run_contract


VERSION = "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
BLOCKS = 1000


def block(n: int) -> str:
    return f"rx block timestamp {1000 + n} * 100 nanoseconds\n"


class FloodExecutor:
    """ubertooth-util -V answering with the version, then a flood of
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

    def test_flood_keeps_head_and_tail_in_run_jsonl_and_all_in_output_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, events = run(root, FloodExecutor())

            # Checks still see the full result.
            self.assertTrue(summary.passed, summary.failure_reason)
            mine = [e for e in events if e["details"].get("command_id") == PREFLIGHT]
            kinds = [e["event_type"] for e in mine]
            # Head, one note, tail, then the result.
            self.assertEqual(
                ["command_started"] + ["command_output"] * RECORD_OUTPUT_EVENTS
                + ["command_output_omitted"] + ["command_output"] * RECORD_TAIL_EVENTS
                + ["command_finished", "output_checks"],
                kinds,
            )
            recorded = outputs(events, PREFLIGHT)
            self.assertEqual(VERSION, recorded[0]["details"]["text"])
            tail = recorded[RECORD_OUTPUT_EVENTS:]
            self.assertEqual(
                [block(n) for n in range(BLOCKS - RECORD_TAIL_EVENTS + 1, BLOCKS)]
                + ["last stdout line\n"],
                [e["details"]["text"] for e in tail],
            )
            # Tail records carry when the chunk arrived.
            self.assertTrue(all("received" in e["details"] for e in tail))
            self.assertTrue(all("received" not in e["details"]
                                for e in recorded[:RECORD_OUTPUT_EVENTS]))

            done = finished(events, PREFLIGHT)["details"]
            out_name = f"{OUTPUT_DIR}/{PREFLIGHT}.stdout.log"
            err_name = f"{OUTPUT_DIR}/{PREFLIGHT}.stderr.log"
            saved = {file["stream"]: file for file in done["output_files"]}
            self.assertEqual([out_name, err_name],
                             [saved["stdout"]["path"], saved["stderr"]["path"]])
            # Each file holds its own stream exactly as the command wrote it.
            self.assertEqual(VERSION + "last stdout line\n",
                             (root / out_name).read_text())
            self.assertEqual("".join(block(n) for n in range(BLOCKS)),
                             (root / err_name).read_text())
            for file in done["output_files"]:
                body = (root / file["path"]).read_bytes()
                self.assertEqual(len(body), file["bytes"])
                self.assertEqual(hashlib.sha256(body).hexdigest(), file["sha256"])
            self.assertEqual(2, saved["stdout"]["lines"])
            self.assertEqual(BLOCKS, saved["stderr"]["lines"])

            omitted = done["output_omitted"]
            left_out = BLOCKS - (RECORD_OUTPUT_EVENTS - 1) - (RECORD_TAIL_EVENTS - 1)
            self.assertEqual({"stdout": 0, "stderr": left_out}, omitted["lines"])
            self.assertEqual(
                [{"stream": "stderr", "shape": "rx block timestamp N * N nanoseconds",
                  "count": left_out}],
                omitted["common_shapes"],
            )
            note = next(e for e in mine if e["event_type"] == "command_output_omitted")
            self.assertEqual(done["output_files"], note["details"]["output_files"])
            self.assertEqual(omitted, note["details"]["output_omitted"])

            report = (root / REPORT).read_text()
            self.assertIn(f"[M] full output in {out_name}, {err_name}; "
                          f"left out of run.jsonl here: {left_out} stderr lines",
                          report)
            self.assertIn(f"[M]   {left_out} x [E] rx block timestamp N * N nanoseconds", report)
            # The note sits between the head and the tail, and the tail ends the record.
            head_line = f"[E] {block(RECORD_OUTPUT_EVENTS - 2).rstrip()}"
            tail_line = f"[E] {block(BLOCKS - 1).rstrip()}"
            self.assertLess(report.index(head_line), report.index("[M] full output in"))
            self.assertLess(report.index("[M] full output in"), report.index(tail_line))
            self.assertLess(report.index(tail_line), report.index("[O] last stdout line"))
            self.assertNotIn(block(RECORD_OUTPUT_EVENTS).rstrip(), report)

    def test_failure_summary_shows_the_end_of_a_spilled_command(self):
        events = [{
            "event_type": "command_started", "status": "running",
            "details": {"invocation_id": "001", "command_id": "c"},
        }]
        for n in range(RECORD_OUTPUT_EVENTS):
            events.append({"event_type": "command_output", "status": "running",
                           "details": {"invocation_id": "001", "command_id": "c",
                                       "stream": "stderr", "text": block(n)}})
        events.append({"event_type": "command_output_omitted", "status": "running",
                       "details": {"invocation_id": "001", "command_id": "c",
                                   "output_files": [{"path": "output/c.stderr.log",
                                                     "stream": "stderr"}],
                                   "output_omitted": {"lines": {"stderr": 7}}}})
        events.append({"event_type": "command_output", "status": "running",
                       "details": {"invocation_id": "001", "command_id": "c",
                                   "stream": "stderr", "text": "final error\n"}})
        events.append({"event_type": "test_finished", "status": "failed",
                       "details": {"invocation_id": "001", "test_name": "T",
                                   "reason": "boom"}})
        failure = failure_summary({"status": "failed"}, events)[0]
        self.assertEqual(
            ["[... 7 output lines left out; full output in output/c.stderr.log ...]",
             "final error"],
            failure["log"][-2:],
        )

    def test_executor_sink_error_marker_reaches_run_jsonl_after_a_spill(self):
        class MarkerExecutor(FloodExecutor):
            def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
                result = super().run(argv, timeout_s=timeout_s,
                                     cancellation=cancellation, on_output=on_output)
                if self.calls == 1:
                    on_output("stderr", "\nOUTPUT SINK ERROR: disk full\n")
                return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _summary, events = run(root, MarkerExecutor())
            texts = [e["details"]["text"] for e in outputs(events, PREFLIGHT)]
            self.assertEqual("\nOUTPUT SINK ERROR: disk full\n", texts[-1])
            self.assertIn("[E] OUTPUT SINK ERROR: disk full", (root / REPORT).read_text())

    def test_cap_counts_bytes_in_run_jsonl_not_characters(self):
        # One 4-byte character is 12 bytes in run.jsonl (an escaped surrogate
        # pair); 1000 of them per record passes 64 KiB within a few records.
        line = "\U0001F600" * 999 + "\n"

        class WideExecutor(FloodExecutor):
            def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
                self.calls += 1
                if self.calls == 1:
                    for _ in range(20):
                        on_output("stdout", line)
                else:
                    on_output("stdout", VERSION)
                text = VERSION if self.calls > 1 else line * 20
                return CommandResult(tuple(argv), 0, text, "", 0.01, False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _summary, events = run(root, WideExecutor())
            head = []
            for event in events:
                if event["event_type"] == "command_output_omitted":
                    break
                if event["event_type"] == "command_output" \
                        and event["details"]["command_id"] == PREFLIGHT:
                    head.append(event)
            self.assertEqual(RECORD_OUTPUT_BYTES // record_bytes(line), len(head))
            run_bytes = sum(record_bytes(e["details"]["text"]) for e in head)
            self.assertLessEqual(run_bytes, RECORD_OUTPUT_BYTES)
            on_disk = (root / RUN_LOG).read_bytes()
            self.assertGreater(len(on_disk), run_bytes)
            done = finished(events, PREFLIGHT)["details"]
            saved = done["output_files"][0]["path"]
            self.assertEqual(20, (root / saved).read_text().count("\U0001F600" * 999))

    def test_short_output_stays_in_run_jsonl_without_an_output_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, events = run(root, FloodExecutor(flood_calls=()))
            self.assertTrue(summary.passed)
            self.assertFalse((root / OUTPUT_DIR).exists())
            self.assertNotIn("output_files", finished(events, PREFLIGHT)["details"])
            self.assertEqual([VERSION], [e["details"]["text"] for e in outputs(events, PREFLIGHT)])

    def test_output_of_a_non_streaming_executor_is_spilled_too(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 4096-character chunks: past the character limit, not the count.
            summary, events = run(root, FloodExecutor(stream=False, blocks=3000))
            self.assertTrue(summary.passed)
            done = finished(events, PREFLIGHT)["details"]
            saved = {file["stream"]: file["path"] for file in done["output_files"]}
            self.assertEqual(
                3000,
                (root / saved["stderr"]).read_text().count("rx block timestamp"))
            self.assertIn("last stdout line", (root / saved["stdout"]).read_text())

    def test_each_flooding_command_gets_its_own_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _summary, events = run(root, FloodExecutor(flood_calls=(1, 2)))
            files = sorted(
                file["path"] for e in events
                if e["event_type"] == "command_finished"
                for file in e["details"].get("output_files", [])
            )
            # Two commands flooded, each leaving a stdout and a stderr file.
            self.assertEqual(4, len(set(files)))
            self.assertEqual(sorted(p.relative_to(root).as_posix()
                                    for p in (root / OUTPUT_DIR).iterdir()), files)


class PartsTests(unittest.TestCase):

    def test_live_views_mark_the_left_out_output(self):
        stream = io.StringIO()
        sink = TerminalEventSink(stream)
        for sequence, (kind, details) in enumerate((
            ("command_started", {"argv": ["ubertooth-dump"]}),
            ("command_output", {"stream": "stderr", "text": "rx block"}),
            ("command_output_omitted",
             {"output_files": [{"path": "output/x.stdout.log", "stream": "stdout"}]}),
            ("command_output", {"stream": "stderr", "text": "last\n"}),
        ), 1):
            sink.emit(RunEvent(1, "run", sequence, "2026-10-25T00:00:00.000",
                               kind, "running", details))
        self.assertIn("[E] rx block\n[M] output left out; full output in "
                      "output/x.stdout.log\n[E] last\n", stream.getvalue())
        # The browser live view writes the same gap line.
        js = (Path(__file__).resolve().parents[1] / "ubertooth_harness" / "web"
              / "app.js").read_text(encoding="utf-8")
        self.assertIn('kind === "command_output_omitted"', js)
        self.assertIn("output lines left out; full output in", js)

    def test_record_bytes_counts_json_escapes(self):
        self.assertEqual(3, record_bytes("abc"))
        self.assertEqual(12, record_bytes("\U0001F600"))
        self.assertEqual(6, record_bytes("\x00"))
        self.assertEqual(2, record_bytes("\n"))

    def test_tail_starts_at_a_line_boundary_after_a_gap(self):
        records, notes = [], []
        moment = datetime.datetime(2026, 10, 25, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            capped = CappedOutput(
                lambda stream, text, received: records.append((stream, text, received)),
                lambda: CommandOutputFiles(Path(directory), "x"),
            )
            for _ in range(RECORD_OUTPUT_EVENTS):
                capped.add(moment, "stdout", "head\n")
            # A long line split across chunks; the tail keeps its end only.
            capped.add(moment, "stderr", "start of a long line ")
            for _ in range(RECORD_TAIL_EVENTS):
                capped.add(moment, "stderr", "middle ")
            capped.add(moment, "stderr", "end\nlast line\n")
            capped.close()
            details = capped.finish(notes.append)
        tail = records[RECORD_OUTPUT_EVENTS:]
        # Chunks inside the cut line are dropped; the tail starts at "last line".
        self.assertEqual([("stderr", "last line\n", "2026-10-25T00:00:00.000")], tail)
        self.assertEqual(1, details["output_omitted"]["lines"]["stderr"])
        self.assertEqual([f"{OUTPUT_DIR}/x.stdout.log", f"{OUTPUT_DIR}/x.stderr.log"],
                         [file["path"] for file in details["output_files"]])
        # The note gives when the left-out output ended.
        self.assertEqual([dict(details, received="2026-10-25T00:00:00.000")], notes)

    def test_large_chunks_are_split_into_bounded_records(self):
        records = []
        capped = CappedOutput(lambda s, t, r: records.append(t), lambda: None)
        capped.add(datetime.datetime(2026, 10, 25), "stdout", "\x00" * 5000)
        self.assertEqual(5, len(records))
        self.assertTrue(all(record_bytes(t) <= 12 * 1024 for t in records))

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

    def test_each_stream_goes_to_its_own_file_with_its_size_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = CommandOutputFiles(root, "x")
            files.write("stdout", "one\ntw")
            files.write("stderr", "err\n")
            files.write("stdout", "o")
            saved = files.close()
            self.assertEqual(
                [f"{OUTPUT_DIR}/x.stdout.log", f"{OUTPUT_DIR}/x.stderr.log"],
                [file.path for file in saved],
            )
            self.assertEqual("one\ntwo", (root / saved[0].path).read_text())
            self.assertEqual("err\n", (root / saved[1].path).read_text())
            # A last line with no line ending still counts.
            self.assertEqual((7, 2), (saved[0].bytes, saved[0].lines))
            self.assertEqual(hashlib.sha256(b"one\ntwo").hexdigest(),
                             saved[0].sha256)
            # A second command of the same name takes the next free name.
            again = CommandOutputFiles(root, "x")
            again.write("stdout", "two\n")
            self.assertEqual(f"{OUTPUT_DIR}/x-2.stdout.log", again.close()[0].path)


if __name__ == "__main__":
    unittest.main()
