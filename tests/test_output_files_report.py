"""The Output files table in report.md, and the records it is built from (#187)."""

import unittest

from ubertooth_harness.outputfiles import OUTPUT_DIR, OutputFile, saved_output
from ubertooth_harness.report import render_run
STDOUT_FILE = f"{OUTPUT_DIR}/002-command-1.stdout.log"
STDERR_FILE = f"{OUTPUT_DIR}/002-command-1.stderr.log"
CAPTURE_FILE = "captures/002/case/capture.pcapng"
BODY = "".join(f"line {n:04d}\n" for n in range(2000))


def event(sequence, event_type, details, status="passed"):
    return {
        "schema_version": 1, "run_id": "r1", "sequence": sequence,
        "timestamp": "2026-10-30T00:00:00.000",
        "event_type": event_type, "status": status, "details": details,
    }


def file_record(path, stream, body, lines=None):
    saved = OutputFile(
        path=path, stream=stream, bytes=len(body.encode("utf-8")),
        lines=body.count("\n") if lines is None else lines,
        sha256=_sha(body),
    )
    return saved.to_dict()


def _sha(body: str) -> str:
    import hashlib

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def run_events():
    """A small run: one test, one command with two streams, one capture."""
    return [
        event(1, "run_started", {"plan_name": "fast", "suite": "fast"}, "running"),
        event(2, "test_started", {"invocation_id": "002", "test_name": "Host CLI",
                                  "host": "Host_A"}, "running"),
        event(3, "command_finished", {
            "phase": "test", "invocation_id": "002", "case_id": "002-case",
            "command_id": "002-command-1", "returncode": 0,
            "output_files": [file_record(STDOUT_FILE, "stdout", BODY),
                             file_record(STDERR_FILE, "stderr", "warning: busy\n")],
        }),
        event(4, "output_checks", {
            "phase": "test", "invocation_id": "002", "command_id": "002-command-1",
            "checks": [
                {"kind": "expected", "name": "Host CLI", "pattern": "line 0001",
                 "streams": ["stdout", "stderr"],
                 "files": [STDOUT_FILE, STDERR_FILE], "found": True,
                 "matched": "line 0001", "reason": ""},
                {"kind": "forbidden", "name": "Host CLI", "pattern": "error: ",
                 "streams": ["stdout", "stderr"],
                 "files": [STDOUT_FILE, STDERR_FILE], "found": False, "reason": ""},
                {"kind": "case", "name": "Host CLI", "streams": ["stdout"],
                 "files": [STDOUT_FILE], "reason": "two lines were not samples"},
            ],
        }, "failed"),
        event(5, "output_files", {
            "phase": "test", "invocation_id": "002", "case_id": "002-case",
            "files": [file_record(CAPTURE_FILE, "capture", "\xd4\xc3\xb2\xa1", lines=0)],
        }),
        event(6, "test_finished", {"invocation_id": "002", "test_name": "Host CLI",
                                   "reason": ""}),
        event(7, "run_finished", {"failure_reason": ""}),
    ]


class SavedOutputTests(unittest.TestCase):

    def test_each_file_carries_the_checks_that_read_it(self):
        saved = saved_output(run_events())
        self.assertEqual([STDOUT_FILE, STDERR_FILE, CAPTURE_FILE],
                         [item.file.path for item in saved])
        self.assertEqual(["002"] * 3, [item.group for item in saved])
        # The two patterns read both streams; the case check read stdout only.
        self.assertEqual(3, len(saved[0].checks))
        self.assertEqual(2, len(saved[1].checks))
        self.assertEqual((), saved[2].checks)
        self.assertEqual("002-command-1", saved[0].owner["command_id"])

    def test_a_run_with_no_saved_files_lists_none(self):
        self.assertEqual((), saved_output([event(1, "run_started", {}, "running")]))


class ReportTests(unittest.TestCase):

    def test_the_report_lists_every_file_with_its_hash_and_checks(self):
        report = render_run(run_events(), transcript=False)
        self.assertIn("## Output files", report)
        # The heading names the test, not only its invocation ID.
        self.assertIn("### 002: Host CLI", report)
        self.assertIn(f"| {STDOUT_FILE} | stdout |", report)
        self.assertIn(f"`{_sha(BODY)}`", report)
        # Sizes read as a magnitude and a byte count; captures have no lines.
        self.assertIn(f"{len(BODY.encode()) / 1024:.1f} KiB ({len(BODY.encode())} bytes)",
                      report)
        # A binary capture has no line count to show.
        self.assertIn(f"| {CAPTURE_FILE} | capture | 8 bytes |  | ", report)
        # The checks that read each file, and the one that failed.
        self.assertIn("1 case, 1 expected, 1 forbidden: 1 against the run "
                      "(Host CLI: two lines were not samples)", report)
        self.assertIn("1 expected, 1 forbidden: all passed", report)
        self.assertIn("| none |", report)

    def test_a_check_that_found_nothing_counts_against_the_run(self):
        # An observed setting that was not reported, and a command that
        # exited with its skip status without a SKIP line, record no reason
        # of their own: the step record carries it. The table must not read
        # them as passes.
        events = [e for e in run_events() if e["event_type"] != "output_checks"]
        events.insert(4, event(4, "output_checks", {
            "phase": "test", "invocation_id": "002", "command_id": "002-command-1",
            "checks": [
                {"kind": "observed", "name": "Host CLI: channel",
                 "pattern": "Channel: (?P<value>\\d+)", "streams": ["stdout"],
                 "files": [STDOUT_FILE], "found": False, "reason": ""},
                {"kind": "skip-line", "name": "Host CLI", "pattern": "^SKIP: (.+)$",
                 "streams": ["stdout"], "files": [STDOUT_FILE], "found": False,
                 "reason": ""},
            ],
        }, "failed"))
        report = render_run(events, transcript=False)
        self.assertIn("1 observed, 1 skip-line: 2 against the run", report)
        self.assertIn("was not found", report)
        self.assertNotIn("all passed", report)

    def test_a_forbidden_pattern_that_matched_counts_against_the_run(self):
        events = [e for e in run_events() if e["event_type"] != "output_checks"]
        events.insert(4, event(4, "output_checks", {
            "phase": "test", "invocation_id": "002", "command_id": "002-command-1",
            "checks": [
                {"kind": "forbidden", "name": "Host CLI", "pattern": "error: ",
                 "streams": ["stdout"], "files": [STDOUT_FILE], "found": True,
                 "reason": ""},
            ],
        }, "failed"))
        report = render_run(events, transcript=False)
        self.assertIn("1 forbidden: 1 against the run", report)
        self.assertIn("was found", report)

    def test_a_file_from_the_build_is_grouped_under_its_phase(self):
        events = [
            event(1, "run_started", {"plan_name": "fast"}, "running"),
            event(2, "command_finished", {
                "phase": "build", "step_id": "host-build",
                "command_id": "build-host-build", "returncode": 2,
                "output_files": [file_record(f"{OUTPUT_DIR}/build-host-build.stderr.log",
                                             "stderr", "error: no\n")],
            }, "failed"),
            event(3, "run_finished", {"failure_reason": "build failed"}, "failed"),
        ]
        report = render_run(events, transcript=False)
        self.assertIn("### build", report)
        self.assertIn("build-host-build.stderr.log", report)

    def test_a_record_whose_hash_is_not_one_cannot_break_the_table(self):
        events = [e for e in run_events() if e["event_type"] != "output_checks"]
        for e in events:
            for file in (e["details"].get("output_files")
                         or e["details"].get("files") or []):
                file["sha256"] = "ab|cd"
        report = render_run(events, transcript=False)
        for line in report.splitlines():
            if "stdout.log" in line and line.startswith("|"):
                # The pipe is escaped, so the row still has six cells.
                self.assertIn(r"ab\|cd", line)
                self.assertEqual(7, line.replace(r"\|", "").count("|"), line)

    def test_two_records_that_describe_one_path_differently_are_both_listed(self):
        events = run_events()
        events.insert(5, event(5, "output_files", {
            "phase": "test", "invocation_id": "002",
            "files": [file_record(CAPTURE_FILE, "capture", "\xd4\xc3", lines=0)],
        }))
        report = render_run(events, transcript=False)
        self.assertEqual(2, report.count(f"| {CAPTURE_FILE} | capture |"))

    def test_a_run_with_no_saved_files_has_no_section(self):
        self.assertNotIn("## Output files",
                         render_run([event(1, "run_started", {}, "running")],
                                    transcript=False))


if __name__ == "__main__":
    unittest.main()
