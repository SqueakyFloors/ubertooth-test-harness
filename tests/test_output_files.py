"""Output streamed to files, checked from files, and re-checked offline (#187)."""

import hashlib
import io
import json
import re
from pathlib import Path
import tempfile
import unittest

from ubertooth_harness.cases import CommandCase
from ubertooth_harness.events import RUN_LOG
from ubertooth_harness.executor import (
    MEMORY_WINDOW_CHARS,
    CommandResult,
    SubprocessCommandExecutor,
)
from ubertooth_harness.outputfiles import (
    MAX_BLOCK_CHARS,
    OUTPUT_DIR,
    READ_BLOCK_CHARS,
    SEARCH_OVERLAP_CHARS,
    SKIP_LINE,
    TARGET_SERIAL_STREAM,
    OutsideRun,
    confined,
    CommandOutput,
    LineSource,
    OutputCheck,
    OutputFile,
    find_lines,
    measure,
    search_parts,
    stream_for,
    text_lines,
)
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import Observation, Step, nothing_on_stdout
from ubertooth_harness.validate import validate_command, validate_run

from tests.test_sequencer import run_contract

VERSION = "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
# Four times the in-memory window, in 64 KiB chunks, so the run holds far
# more output than any copy of it in memory.
STREAM_CHARS = 4 * MEMORY_WINDOW_CHARS
CHUNK_CHARS = 64 * 1024
PREFLIGHT = "preflight-001-host-version-command-1"


def block(n: int) -> str:
    """One line of the flood, padded so a chunk is a round number."""
    return f"rx block timestamp {n:08d} * 100 nanoseconds".ljust(63) + "\n"


class StreamExecutor:
    """Answers the version, then floods stderr like a long capture."""

    def __init__(self, chars=STREAM_CHARS, stream=True, last="END OF STREAM\n"):
        self.calls = 0
        self.chars = chars
        self.stream = stream
        self.last = last

    def _chunks(self):
        yield "stdout", VERSION
        text = ""
        written = 0
        number = 0
        while written < self.chars:
            text += block(number)
            written += len(block(number))
            number += 1
            if len(text) >= CHUNK_CHARS:
                yield "stderr", text
                text = ""
        if text:
            yield "stderr", text
        if self.last:
            yield "stdout", self.last

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        self.calls += 1
        chunks = list(self._chunks()) if self.calls == 1 else [("stdout", VERSION)]
        if self.stream and on_output:
            for name, text in chunks:
                on_output(name, text)
        return CommandResult(
            tuple(argv), 0,
            "".join(t for s, t in chunks if s == "stdout"),
            "".join(t for s, t in chunks if s == "stderr"),
            0.01, False,
        )


def run_once(root: Path, executor):
    invocation = TestInvocation("001-Host version", "Host version", "Host_Mac", {}, {})
    manifest, checks = run_contract(root, invocation)
    summary = NamedTestSequencer(executor).run(
        (invocation,), root, run_id="named-run", manifest=manifest,
        preflight_checks=checks,
    )
    events = [json.loads(line) for line in (root / RUN_LOG).read_text().splitlines()]
    return summary, events


def finished(events, command_id):
    return next(e for e in events if e["event_type"] == "command_finished"
                and e["details"]["command_id"] == command_id)["details"]


class StreamToFilesTests(unittest.TestCase):

    def test_a_stream_larger_than_memory_is_whole_on_disk_and_bounded_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, events = run_once(root, StreamExecutor())
            self.assertTrue(summary.passed, summary.failure_reason)

            saved = {file["stream"]: file
                     for file in finished(events, PREFLIGHT)["output_files"]}
            self.assertEqual(f"{OUTPUT_DIR}/{PREFLIGHT}.stderr.log",
                             saved["stderr"]["path"])
            # The whole stream reached the file, and nothing of the harness's
            # own went into it.
            text = (root / saved["stderr"]["path"]).read_text()
            self.assertGreaterEqual(len(text), STREAM_CHARS)
            self.assertEqual(block(0), text[:len(block(0))])
            self.assertTrue(text.endswith(block(saved["stderr"]["lines"] - 1)))
            # Its size, line count, and hash are recorded.
            body = (root / saved["stderr"]["path"]).read_bytes()
            self.assertEqual(len(body), saved["stderr"]["bytes"])
            self.assertEqual(hashlib.sha256(body).hexdigest(), saved["stderr"]["sha256"])
            self.assertEqual(text.count("\n"), saved["stderr"]["lines"])
            # run.jsonl stays far smaller than the stream it describes.
            self.assertLess((root / RUN_LOG).stat().st_size, STREAM_CHARS)

    def test_the_in_memory_copy_of_a_long_stream_is_bounded(self):
        window = SubprocessCommandExecutor()
        result = window.run(
            ["python3", "-c",
             "import sys\n"
             f"sys.stdout.write('x' * {4 * MEMORY_WINDOW_CHARS})\n"
             "sys.stdout.write('TAIL')\n"],
            timeout_s=60.0,
        )
        self.assertEqual(0, result.returncode)
        # Head, a line saying what is missing, and the end of the stream.
        self.assertLess(len(result.stdout), 2 * MEMORY_WINDOW_CHARS)
        self.assertIn("characters left out of this copy", result.stdout)
        self.assertTrue(result.stdout.endswith("TAIL"))

    def test_a_check_reads_the_file_so_it_sees_past_the_memory_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _summary, events = run_once(root, StreamExecutor())
            saved = tuple(
                OutputFile.from_dict(file)
                for file in finished(events, PREFLIGHT)["output_files"]
            )
            # The in-memory copy a non-streaming caller would read has a gap;
            # the CommandOutput reads the file and has none.
            output = CommandOutput(run_dir=root, files=saved)
            self.assertIsNotNone(output.search(r"END OF STREAM"))
            self.assertIsNotNone(output.search(r"rx block timestamp 00000000 "))
            middle = STREAM_CHARS // len(block(0)) // 2
            self.assertIsNotNone(
                output.search(rf"rx block timestamp {middle:08d} "))
            self.assertIsNone(output.search(r"no such line"))

    def test_a_short_command_leaves_no_file_and_is_still_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, events = run_once(root, StreamExecutor(chars=0, last=""))
            self.assertTrue(summary.passed, summary.failure_reason)
            self.assertFalse((root / OUTPUT_DIR).exists())
            checks = [e for e in events if e["event_type"] == "output_checks"]
            self.assertTrue(checks)
            for event in checks:
                for check in event["details"]["checks"]:
                    self.assertEqual([], check["files"])

    def test_a_case_s_captures_and_target_traffic_are_recorded(self):
        self.assertEqual("capture", stream_for(Path("a/capture.pcapng")))
        self.assertEqual(TARGET_SERIAL_STREAM, stream_for(Path("a/dk-0121.serial.log")))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "captures" / "dk.serial.log"
            path.parent.mkdir(parents=True)
            path.write_text("utdk: ready\nstatus\nOK\n", encoding="utf-8")
            saved = measure(path, stream_for(path), relative_to=root)
            self.assertEqual("captures/dk.serial.log", saved.path)
            self.assertEqual(TARGET_SERIAL_STREAM, saved.stream)
            self.assertEqual(3, saved.lines)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), saved.sha256)


    def test_a_short_stream_beside_a_flooding_one_is_whole_in_its_own_file(self):
        # stdout printed two lines before stderr flooded. Both files must hold
        # their stream in full, so a check over both reads the same bytes
        # live and offline (no stream is left half in memory).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _summary, events = run_once(root, StreamExecutor())
            saved = {file["stream"]: file
                     for file in finished(events, PREFLIGHT)["output_files"]}
            self.assertEqual(VERSION + "END OF STREAM\n",
                             (root / saved["stdout"]["path"]).read_text())
            checks = next(e for e in events if e["event_type"] == "output_checks"
                          and e["details"]["command_id"] == PREFLIGHT)
            # The check over both streams names both files, in its read order.
            self.assertEqual([saved["stdout"]["path"], saved["stderr"]["path"]],
                             checks["details"]["checks"][0]["files"])
            self.assertEqual((), validate_run(root).differences)

    def test_a_stream_with_no_line_endings_is_read_in_bounded_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.log"
            # One "line" far longer than a block, with no line ending at all.
            path.write_text("x" * (4 * MAX_BLOCK_CHARS) + "needle",
                            encoding="utf-8")
            blocks = list(LineSource((path,)))
            self.assertTrue(all(len(block) <= MAX_BLOCK_CHARS for block in blocks))
            self.assertGreater(len(blocks), 1)
            self.assertIsNotNone(search_parts((path,), r"needle"))


class ReadingFilesTests(unittest.TestCase):

    def test_lines_can_be_read_more_than_once_and_drop_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.log"
            path.write_text("one\r\ntwo\nthree", encoding="utf-8", newline="")
            source = LineSource((path,))
            self.assertEqual(["one", "two", "three"], list(source))
            self.assertEqual(["one", "two", "three"], list(source))

    def test_a_pattern_is_found_across_a_read_block_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.log"
            filler = "filler line that is long enough to fill blocks\n"
            # A line that lands wherever the blocks happen to break.
            path.write_text(filler * 40000 + "the needle is here\n" + filler * 40000,
                            encoding="utf-8")
            self.assertIsNotNone(search_parts((path,), r"the needle is here"))
            self.assertIsNone(search_parts((path,), r"no needle at all"))

    def test_a_pattern_over_several_lines_is_found_once_each(self):
        lines = ["USR LED status: 1", "RX LED status : 0", "TX LED status : 1"]
        pattern = (r"USR LED status: (?P<usr>[01])\s+RX LED status : (?P<rx>[01])\s+"
                   r"TX LED status : (?P<tx>[01])")
        matches = list(find_lines(lines * 3, pattern))
        self.assertEqual(3, len(matches))
        self.assertEqual([("1", "0", "1")] * 3,
                         [m.group("usr", "rx", "tx") for m in matches])

    def test_an_observed_setting_that_is_not_the_expected_one_is_recorded_failed(self):
        step = Step("read", "Read the channel",
                    ("ubertooth-util", "-c"),
                    observe=Observation("channel", r"Channel: (?P<value>\d+)",
                                        expected="2426"))
        output = CommandOutput(stdout="Channel: 2402\n")
        result = CommandResult(("ubertooth-util", "-c"), 0, "Channel: 2402\n",
                               "", 0.1, False, output=output)
        outcome = step.evaluate(result, {})
        self.assertIn("expected channel=2426, observed 2402", outcome.failure)
        # The record carries the reason, so the report and validate do not
        # read a matched pattern as a check that came out for the run.
        recorded = output.checks[-1]
        self.assertEqual(("observed", True, "2402"),
                         (recorded.kind, recorded.found, recorded.matched))
        self.assertIn("expected channel=2426", recorded.reason)

    def test_an_anchored_pattern_does_not_match_at_a_block_boundary(self):
        # What is carried from one block to the next starts at a line start,
        # so '^' cannot match in the middle of a line that a block split.
        line = "x" * 99 + "\n"
        head = line * ((READ_BLOCK_CHARS + SEARCH_OVERLAP_CHARS) // 100 + 100)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.log"
            spliced = head + "pad SKIP: boom and more\n" + line * 2000
            path.write_text(spliced, encoding="utf-8")
            self.assertIsNone(re.search(SKIP_LINE, spliced))
            self.assertIsNone(search_parts((path,), SKIP_LINE))
            # A real line start far into the stream is still found.
            real = head + "SKIP: no traffic\n" + line * 2000
            path.write_text(real, encoding="utf-8")
            match = search_parts((path,), SKIP_LINE)
            self.assertIsNotNone(match)
            self.assertEqual("no traffic", match[1])

    def test_a_string_and_a_file_of_the_same_bytes_give_the_same_lines(self):
        text = "a\rb\nc\x0cd\ne\r\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.log"
            path.write_text(text, encoding="utf-8", newline="")
            self.assertEqual(list(LineSource((path,))), list(text_lines(text)))
            self.assertEqual(["a\rb", "c\x0cd", "e"], list(text_lines(text)))

    def test_a_check_reads_a_string_as_well_as_a_file(self):
        self.assertEqual("", nothing_on_stdout("\n  \n"))
        self.assertIn("stdout is not empty", nothing_on_stdout("note: busy\n"))
        self.assertEqual(
            "2402",
            Observation("channel", r"Channel: (?P<value>\d+)").extract(
                "Channel: 2402\n"),
        )

    def test_a_check_records_the_pattern_it_read_and_the_files_it_read_it_from(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = root / OUTPUT_DIR / "c.stdout.log"
            saved.parent.mkdir()
            saved.write_text("Firmware version: git-1234abc (API:1.07)\n")
            output = CommandOutput(
                run_dir=root,
                files=(OutputFile(f"{OUTPUT_DIR}/c.stdout.log", "stdout", 41, 1, "ab"),),
            )
            case = CommandCase("c", "Firmware", ("ubertooth-util", "-v"),
                               output_patterns=(r"Firmware version: (?P<value>\S+)",))
            # The in-memory copy is empty: the check reads the file.
            result = CommandResult(("ubertooth-util", "-v"), 0, "", "", 0.1, False,
                                   output=output)
            self.assertEqual("", case.result_failure(result))
            self.assertEqual(1, len(output.checks))
            recorded = output.checks[0]
            self.assertEqual(("expected", True, "git-1234abc"),
                             (recorded.kind, recorded.found, recorded.matched))
            self.assertEqual((f"{OUTPUT_DIR}/c.stdout.log",), recorded.files)


    def test_a_recorded_path_that_leaves_the_run_is_refused_before_it_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "run"
            (root / OUTPUT_DIR).mkdir(parents=True)
            (Path(directory) / "elsewhere.txt").write_text("not yours\n")
            (root / OUTPUT_DIR / "link.stdout.log").symlink_to(
                Path(directory) / "elsewhere.txt")
            # A link that stays inside the run directory is refused too: a
            # record would otherwise name one file and be served another.
            (root / "run.jsonl").write_text("{}\n")
            (root / OUTPUT_DIR / "inside.stdout.log").symlink_to(root / "run.jsonl")
            (root / "away").symlink_to(Path(directory), target_is_directory=True)
            for path in ("../elsewhere.txt", "/etc/passwd", "output/../run.jsonl",
                         f"{OUTPUT_DIR}/link.stdout.log",
                         f"{OUTPUT_DIR}/inside.stdout.log", "away/elsewhere.txt"):
                with self.subTest(path=path), self.assertRaises(OutsideRun):
                    confined(root, path)
            self.assertEqual((root / OUTPUT_DIR).resolve(),
                             confined(root, OUTPUT_DIR))


class ValidateTests(unittest.TestCase):

    def validated(self, root: Path) -> str:
        out, errors = io.StringIO(), io.StringIO()
        code = validate_command(root, out, errors)
        return code, out.getvalue() + errors.getvalue()

    def test_a_finished_run_revalidates_with_no_differences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, _events = run_once(root, StreamExecutor())
            self.assertTrue(summary.passed, summary.failure_reason)
            validation = validate_run(root)
            self.assertEqual((), validation.differences)
            self.assertGreater(validation.rerun, 0)
            code, text = self.validated(root)
            self.assertEqual(0, code, text)
            self.assertIn("differences: 0", text)

    def test_a_check_that_now_reaches_another_verdict_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_once(root, StreamExecutor())
            # A check's pattern no longer matches, as if the output had been
            # taken from a run where the tool answered differently.
            log = root / RUN_LOG
            lines = []
            for line in log.read_text().splitlines():
                event = json.loads(line)
                if event["event_type"] == "output_checks":
                    for check in event["details"]["checks"]:
                        check["pattern"] = r"a pattern nothing matches"
                lines.append(json.dumps(event))
            log.write_text("\n".join(lines) + "\n")
            code, text = self.validated(root)
            self.assertEqual(1, code)
            self.assertIn("matched then, does not match now", text)

    def test_a_changed_or_missing_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _summary, events = run_once(root, StreamExecutor())
            saved = finished(events, PREFLIGHT)["output_files"][0]["path"]
            (root / saved).write_text("something else\n")
            code, text = self.validated(root)
            self.assertEqual(1, code)
            self.assertIn(f"error: {saved}:", text)
            self.assertIn("bytes now", text)

            (root / saved).unlink()
            code, text = self.validated(root)
            self.assertEqual(1, code)
            self.assertIn("missing from the run directory", text)

    def test_a_run_with_no_recorded_checks_has_nothing_to_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / RUN_LOG).write_text(json.dumps({
                "schema_version": 1, "run_id": "old", "sequence": 1,
                "timestamp": "2026-10-25T00:00:00.000",
                "event_type": "run_finished", "status": "passed", "details": {},
            }) + "\n")
            code, text = self.validated(root)
            self.assertEqual(0, code)
            self.assertIn("checks re-run: 0", text)
            self.assertIn("recorded no output checks", text)

    def test_a_check_written_as_code_is_reported_as_not_re_runnable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            step = Step("s", "Stop", ("ubertooth-util", "-S"),
                        output_check=nothing_on_stdout)
            output = CommandOutput(stdout="")
            step.evaluate(
                CommandResult(("ubertooth-util", "-S"), 0, "", "", 0.1, False,
                              output=output),
                {},
            )
            (root / RUN_LOG).write_text("\n".join(json.dumps({
                "schema_version": 1, "run_id": "r", "sequence": 1,
                "timestamp": "2026-10-25T00:00:00.000",
                "event_type": "output_checks", "status": "passed",
                "details": {"command_id": "c",
                            "checks": [check.to_dict() for check in output.checks]},
            }) for _ in (1,)) + "\n")
            code, text = self.validated(root)
            self.assertEqual(0, code)
            self.assertIn("not re-runnable: 1", text)
            self.assertIn("a check written as code", text)

    def test_a_directory_that_is_not_a_run_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            code, text = self.validated(Path(directory))
            self.assertEqual(1, code)
            self.assertIn("is this a run directory?", text)

    def test_a_recorded_path_outside_the_run_directory_is_refused(self):
        check = OutputFile("../elsewhere/x.log", "stdout", 1, 1, "ab")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / RUN_LOG).write_text(json.dumps({
                "schema_version": 1, "run_id": "r", "sequence": 1,
                "timestamp": "2026-10-25T00:00:00.000",
                "event_type": "command_finished", "status": "passed",
                "details": {"command_id": "c", "output_files": [check.to_dict()]},
            }) + "\n")
            code, text = self.validated(root)
            self.assertEqual(1, code)
            self.assertIn("is not a path inside the run directory", text)

    def test_the_record_round_trips(self):
        check = OutputCheck("expected", "Firmware", r"git-\S+", ("stdout",),
                            ("output/c.stdout.log",), True, "git-1", "git-1", "")
        self.assertEqual(check, OutputCheck.from_dict(check.to_dict()))
        saved = OutputFile("output/c.stdout.log", "stdout", 12, 2, "abc")
        self.assertEqual(saved, OutputFile.from_dict(saved.to_dict()))


if __name__ == "__main__":
    unittest.main()
