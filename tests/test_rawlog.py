import datetime
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ubertooth_harness.executor import CommandResult
from ubertooth_harness.rawlog import CommandCapture, format_command_record

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} ")
BASE = datetime.datetime(2026, 10, 23, 20, 4, 10, tzinfo=datetime.timezone.utc)


class Ticker:
    """Deterministic UTC clock that advances 10 ms per reading."""

    def __init__(self):
        self.readings = 0

    def __call__(self):
        moment = BASE + datetime.timedelta(milliseconds=10 * self.readings)
        self.readings += 1
        return moment


def stamp_of(index):
    moment = BASE + datetime.timedelta(milliseconds=10 * index)
    return moment.replace(tzinfo=None).isoformat(timespec="milliseconds")


def result(argv=("tool", "-x"), returncode=0, stdout="", stderr="",
           duration_s=0.25, timed_out=False, cancelled=False):
    return CommandResult(tuple(argv), returncode, stdout, stderr, duration_s,
                         timed_out, cancelled)


def capture_of(*chunks):
    """A capture whose clock reads: start, one per chunk, then finish."""
    capture = CommandCapture(Ticker())
    for stream, text in chunks:
        capture.on_output(stream, text)
    capture.finish()
    return capture


def lines_of(record):
    return record.rstrip("\n").split("\n")


class RawLogFormatTests(unittest.TestCase):
    def test_simple_command_is_two_timestamped_lines(self):
        stdout = "Firmware version: git-x (API:1.07)\n"
        record = format_command_record(
            result(("ubertooth-util", "-U", "abc", "-v"), 0, stdout, "", 0.059),
            capture_of(("stdout", stdout)),
        )
        self.assertEqual(
            [
                f"{stamp_of(0)} [C] ubertooth-util -U abc -v",
                f"{stamp_of(2)} [R] ubertooth-util exit 0 in 0.059s; [O] Firmware version: git-x (API:1.07)",
            ],
            lines_of(record),
        )

    def test_no_output_is_stated_on_the_second_line(self):
        record = format_command_record(result(), capture_of())
        self.assertEqual(
            [f"{stamp_of(0)} [C] tool -x", f"{stamp_of(1)} [R] tool exit 0 in 0.250s; no output"],
            lines_of(record),
        )

    def test_multi_line_output_is_stamped_per_line_in_arrival_order(self):
        record = format_command_record(
            result(("tool",), 0, "a\nc\n", "b\n"),
            capture_of(("stdout", "a\n"), ("stderr", "b\n"), ("stdout", "c\n")),
        )
        self.assertEqual(
            [
                f"{stamp_of(0)} [C] tool",
                f"{stamp_of(1)} [O] a",
                f"{stamp_of(2)} [E] b",
                f"{stamp_of(3)} [O] c",
                f"{stamp_of(4)} [R] tool exit 0 in 0.250s",
            ],
            lines_of(record),
        )

    def test_every_line_starts_with_an_iso_utc_timestamp(self):
        record = format_command_record(
            result(("tool",), 3, "x\ny\n", "z\n"),
            capture_of(("stdout", "x\ny\n"), ("stderr", "z\n")),
        )
        for line in lines_of(record):
            self.assertRegex(line, STAMP)
            datetime.datetime.fromisoformat(line.split(" ", 1)[0])

    def test_timestamp_format_matches_event_timestamps(self):
        from ubertooth_harness.events import EventPublisher

        class Sink:
            def __init__(self):
                self.events = []

            def emit(self, event):
                self.events.append(event)

        sink = Sink()
        publisher = EventPublisher("run", sink, utc_now=Ticker())
        publisher.emit("run_started", "running", {})
        line = lines_of(format_command_record(result(), capture_of()))[0]
        self.assertEqual(
            len(sink.events[0].timestamp), len(line.split(" ", 1)[0])
        )
        self.assertEqual(sink.events[0].timestamp, line.split(" ", 1)[0])

    def test_line_split_across_chunks_is_joined_and_trailing_text_is_kept(self):
        record = format_command_record(
            result(("tool",)),
            capture_of(("stdout", "par"), ("stdout", "tial\nnext"), ("stderr", "e")),
        )
        self.assertEqual(
            [
                f"{stamp_of(0)} [C] tool",
                f"{stamp_of(2)} [O] partial",
                f"{stamp_of(4)} [O] next",
                f"{stamp_of(4)} [E] e",
                f"{stamp_of(4)} [R] tool exit 0 in 0.250s",
            ],
            lines_of(record),
        )

    def test_command_is_shell_quoted_so_it_can_be_pasted(self):
        record = format_command_record(result(("tool", "a b", "it's")), capture_of())
        self.assertIn("[C] tool 'a b' 'it'\"'\"'s'", record)

    def test_failure_timeout_and_cancellation_are_on_the_result_line(self):
        record = format_command_record(
            result(returncode=-9, timed_out=True, cancelled=True), capture_of()
        )
        self.assertIn("[R] tool exit -9 in 0.250s, TIMED OUT, CANCELLED", record)
        record = format_command_record(result(returncode=7), capture_of())
        self.assertIn("[R] tool exit 7 in 0.250s", record)
        self.assertNotIn("TIMED OUT", record)
        record = format_command_record(result(timed_out=True), capture_of(), stopped=True)
        self.assertIn("[R] tool exit 0 in 0.250s, stopped at its planned deadline", record)

    def test_falls_back_to_collected_output_when_the_executor_did_not_stream(self):
        record = format_command_record(
            result(stdout="from result\n"), capture_of()
        )
        self.assertIn("[O] from result", record)

    def test_carriage_returns_are_not_kept(self):
        record = format_command_record(
            result(), capture_of(("stdout", "windows line\r\n"))
        )
        self.assertIn("[O] windows line\n", record)

    def test_records_concatenate_into_a_readable_log(self):
        log = format_command_record(result(("a",)), capture_of()) + (
            format_command_record(result(("b",)), capture_of())
        )
        self.assertLess(log.index("[C] a"), log.index("[C] b"))
        for line in lines_of(log):
            self.assertRegex(line, STAMP)


if __name__ == "__main__":
    unittest.main()
