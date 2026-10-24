import datetime
import io
import json
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.events import (
    CompositeEventSink,
    EventPublisher,
    EventSinkError,
    JsonlEventSink,
    RunEvent,
    TerminalEventSink,
    create_run_directory,
    generate_run_id,
    validate_run_id,
)


class CollectSink:
    def __init__(self, name="sink", calls=None):
        self.events = []
        self.name = name
        self.calls = calls

    def emit(self, event):
        self.events.append(event)
        if self.calls is not None:
            self.calls.append((self.name, event.sequence))


class FailingSink:
    def emit(self, event):
        raise RuntimeError(f"cannot store {event.event_type}")


class EventTests(unittest.TestCase):

    def test_generated_run_id_is_safe_and_contains_time(self):
        now = datetime.datetime(2026, 10, 22, 20, 30, 45, tzinfo=datetime.timezone.utc)

        run_id = generate_run_id(now=now, random_suffix="a1b2c3d4e5f6")

        self.assertEqual("20261022T203045Z-a1b2c3d4e5f6", run_id)
        self.assertEqual(run_id, validate_run_id(run_id))

    def test_rejects_unsafe_or_ambiguous_run_ids(self):
        for run_id in ("../escape", "a/b", ".", "", " space", "x" * 65):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    validate_run_id(run_id)

    def test_create_run_directory_stays_under_root_and_rejects_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            run_dir = create_run_directory(root, "run-1")

            self.assertEqual(root.resolve() / "run-1", run_dir)
            self.assertTrue(run_dir.is_dir())
            with self.assertRaises(FileExistsError):
                create_run_directory(root, "run-1")

    def test_publisher_assigns_version_run_id_sequence_and_timestamp(self):
        sink = CollectSink()
        times = iter(
            (
                datetime.datetime(2026, 10, 22, 1, tzinfo=datetime.timezone.utc),
                datetime.datetime(2026, 10, 22, 2, tzinfo=datetime.timezone.utc),
            )
        )
        publisher = EventPublisher("run-1", sink, utc_now=lambda: next(times))

        first = publisher.emit("run_started", "running", {"platform": "macos"})
        second = publisher.emit("run_finished", "passed", {"verified": 100})

        self.assertEqual([1, 2], [event.sequence for event in sink.events])
        self.assertEqual(1, first.schema_version)
        self.assertEqual("run-1", second.run_id)
        self.assertEqual("2026-10-22T02:00:00.000", second.timestamp)
        self.assertEqual({"verified": 100}, second.details)

    def test_jsonl_sink_appends_complete_versioned_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sink = JsonlEventSink(path)
            events = (
                RunEvent(
                    1, "run", 1, "2026-10-22T01:00:00+00:00",
                    "run_started", "running", {}
                ),
                RunEvent(
                    1, "run", 2, "2026-10-22T01:01:00+00:00",
                    "run_finished", "passed", {"count": 1}
                ),
            )

            for event in events:
                sink.emit(event)

            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([1, 2], [row["sequence"] for row in rows])
            self.assertEqual(
                ["run_started", "run_finished"],
                [row["event_type"] for row in rows],
            )
            self.assertTrue(all(row["schema_version"] == 1 for row in rows))

    def test_terminal_sink_tags_whole_lines_and_names_timeouts(self):
        stream = io.StringIO()
        sink = TerminalEventSink(stream)
        events = [
            ("command_started", "running", {"argv": ["/usr/bin/ubertooth-util", "-v"]}),
            ("command_output", "running", {"stream": "stdout", "text": "Firmware ver"}),
            ("command_output", "running", {"stream": "stdout", "text": "sion: git-x\npart"}),
            ("command_finished", "failed", {"returncode": -9, "duration_s": 25.0, "timed_out": True}),
        ]
        for sequence, (kind, status, details) in enumerate(events, 1):
            sink.emit(RunEvent(1, "run", sequence, "2026-10-22T01:00:00.000", kind, status, details))

        rendered = stream.getvalue()
        self.assertIn("[O] Firmware version: git-x\n[O] part\n", rendered)
        self.assertIn("[R] ubertooth-util exit -9 in 25.000s, TIMED OUT", rendered)

    def test_terminal_sink_renders_command_live_output_and_result(self):
        stream = io.StringIO()
        sink = TerminalEventSink(stream)
        events = (
            RunEvent(
                1, "run", 1, "2026-10-22T01:00:00+00:00",
                "command_started", "running",
                {"command_id": "flash-1", "argv": ["tool", "--arg", "two words"]},
            ),
            RunEvent(
                1, "run", 2, "2026-10-22T01:00:01+00:00",
                "command_output", "running",
                {"command_id": "flash-1", "stream": "stdout", "text": "hello"},
            ),
            RunEvent(
                1, "run", 3, "2026-10-22T01:00:02+00:00",
                "command_output", "running",
                {"command_id": "flash-1", "stream": "stderr", "text": "warning\n"},
            ),
            RunEvent(
                1, "run", 4, "2026-10-22T01:00:03+00:00",
                "command_finished", "passed",
                {"command_id": "flash-1", "returncode": 0, "duration_s": 1.2},
            ),
        )

        for event in events:
            sink.emit(event)
        sink.emit(
            RunEvent(
                1, "run", 5, "2026-10-22T01:00:04+00:00",
                "check_started", "running",
                {"phase": "preflight", "check_name": "Firmware version"},
            )
        )
        sink.emit(
            RunEvent(
                1, "run", 6, "2026-10-22T01:00:05+00:00",
                "check_finished", "passed",
                {"phase": "preflight", "check_name": "Firmware version"},
            )
        )

        rendered = stream.getvalue()
        self.assertIn("[C] tool --arg 'two words'", rendered)
        self.assertIn("[O] hello", rendered)
        self.assertIn("[E] warning", rendered)
        self.assertIn("[R] tool exit 0 in 1.200s", rendered)
        self.assertNotIn("[O] hello[E]", rendered)
        self.assertIn("[preflight] Firmware version", rendered)
        self.assertIn("[passed] Firmware version", rendered)

    def test_composite_sink_preserves_order_for_every_sink(self):
        calls = []
        first = CollectSink("first", calls)
        second = CollectSink("second", calls)
        publisher = EventPublisher("run", CompositeEventSink((first, second)))

        publisher.emit("run_started", "running", {})
        publisher.emit("run_finished", "passed", {})

        self.assertEqual(
            [("first", 1), ("second", 1), ("first", 2), ("second", 2)],
            calls,
        )

    def test_composite_sink_surfaces_sink_failure(self):
        publisher = EventPublisher(
            "run", CompositeEventSink((CollectSink(), FailingSink()))
        )

        with self.assertRaisesRegex(EventSinkError, "cannot store run_started"):
            publisher.emit("run_started", "running", {})


if __name__ == "__main__":
    unittest.main()
