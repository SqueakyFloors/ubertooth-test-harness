import itertools
import json
import re
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.events import open_run_publisher
from ubertooth_harness.report import load_events, render_run, render_runs, write_report


SERIAL = "aaaa0000"
_moments = itertools.count()


def command_event(event_type: str, status: str, details: dict) -> dict:
    """One record of command `c`, timestamped a millisecond after the last.

    Built here rather than through a publisher: a test of the output cap
    needs thousands of records, and writing them through run.jsonl and the
    console writer takes minutes.
    """
    second, ms = divmod(next(_moments), 1000)
    return {
        "run_id": "run-a", "event_type": event_type, "status": status,
        "timestamp": f"2026-01-01T00:00:{second:02d}.{ms:03d}",
        "details": {"command_id": "c", **details},
    }


def write_run(directory: Path, *, finish=True, host="Host_Mac", source=None):
    publisher = open_run_publisher(directory, directory.name)
    publisher.emit("run_started", "running", {"plan_name": "Plan", "suite": "fast", "host": host})
    publisher.emit("manifest", "passed", {"manifest": {
        "host": host,
        "plan_name": "Plan",
        "suite": "fast",
        "sources": [{"name": "Ubertooth", "requested_ref": "main", "commit": "a" * 40,
                     "expected_revision": "git-aaaaaaa", "repository": "repo",
                     **(source or {})}],
        "artifacts": [],
        "invocations": [{"resource_identities": {"Ubertooth": {"serial": SERIAL}}}],
        "environment": {"os": {"system": "Darwin", "architecture": "arm64"}},
    }})
    publisher.emit("test_started", "running", {
        "invocation_id": "001-Ubertooth identity", "test_name": "Ubertooth identity",
        "host": host, "resources": {"Ubertooth": "UT_0"},
    })
    publisher.emit("command_started", "running", {"command_id": "c1", "argv": ["ubertooth-util", "-U", SERIAL, "-s"]})
    publisher.emit("command_output", "running", {"command_id": "c1", "stream": "stdout", "text": f"Serial No: {SERIAL}\n"})
    publisher.emit("command_finished", "passed", {"command_id": "c1", "returncode": 0, "duration_s": 0.1})
    if finish:
        publisher.emit("test_finished", "passed", {
            "invocation_id": "001-Ubertooth identity", "test_name": "Ubertooth identity",
            "reason": "", "failed_cases": 0, "skipped_cases": 0,
        })
        publisher.emit("run_finished", "passed", {
            "completed_tests": 1, "requested_tests": 1, "completed_preflight_checks": 0,
            "requested_preflight_checks": 0, "skipped_cases": 0, "failure_reason": "",
        })


class ReportTests(unittest.TestCase):

    def test_report_has_setup_results_and_headed_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run)

            report = (write_report(run)).read_text()

            self.assertIn("| Result | **passed**: 1/1 tests", report)
            self.assertIn(
                "| 001-Ubertooth identity | Host_Mac (Ubertooth=UT_0) |  | passed |", report)
            self.assertIn("=== TEST 001-Ubertooth identity: Ubertooth identity on Host_Mac (Ubertooth=UT_0) ===", report)
            self.assertIn(f"[C] ubertooth-util -U {SERIAL} -s", report)
            self.assertIn(f"[O] Serial No: {SERIAL}", report)
            self.assertIn(SERIAL, report)  # serials are shown by default

    def test_source_shows_the_ref_and_its_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run)
            self.assertIn(
                f"| Ubertooth | `main` = `{'a' * 40}` (git-aaaaaaa) from repo |",
                write_report(run).read_text(),
            )

    def test_source_chosen_by_name_shows_the_name_and_the_commit(self):
        # A controller run: the runner passes the listed commit as the ref
        # and the branch name as ref_name.
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, source={"requested_ref": "a" * 40, "ref_name": "main"})
            self.assertIn(
                f"| Ubertooth | `main` at `{'a' * 40}` (git-aaaaaaa) from repo |",
                write_report(run).read_text(),
            )

    def test_serials_can_be_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run)

            report = render_run(load_events(run / "run.jsonl"), redact_serials=True)

            self.assertNotIn(SERIAL, report)
            self.assertIn("<serial>", report)

    def test_devices_row_names_a_dk_by_its_jlink_serial(self):
        # A BLE run's devices are an Ubertooth and one or two nRF52840 DKs.
        # The DK's registry identity has no `serial`, so the row used to name
        # it with nothing at all.
        from ubertooth_harness.report import _Run, _setup_rows

        records = []
        identities = (
            ("UT_Linux4", {"serial": SERIAL}),
            ("DK_Linux1", {"jlink serial": "001050210121"}),
        )
        for index, (alias, identity) in enumerate(identities):
            check = {"phase": "preflight", "check_id": f"p{index}",
                     "check_name": "Device serial"}
            records.append({"run_id": "r", "event_type": "check_started",
                            "status": "running",
                            "timestamp": "2026-01-01T00:00:00+00:00",
                            "details": {**check, "resource": alias,
                                        "resource_identity": identity}})
            records.append({"run_id": "r", "event_type": "check_finished",
                            "status": "passed",
                            "timestamp": "2026-01-01T00:00:00+00:00",
                            "details": check})

        devices = dict(_setup_rows(_Run(records)))["Devices"]

        self.assertIn(f"UT_Linux4 serial {SERIAL}", devices)
        self.assertIn("DK_Linux1 serial 001050210121", devices)

    def test_redaction_masks_32_hex_tokens_but_not_longer_digests(self):
        from ubertooth_harness.report import _redact

        serial = "0123456789abcdef0123456789ABCDEF"
        git_sha = "0123456789abcdef0123456789abcdef01234567"
        sha256 = "f" * 64
        text = f"extra {serial}; commit {git_sha}; digest {sha256}"

        redacted = _redact(text, ())

        self.assertEqual(
            f"extra <serial>; commit {git_sha}; digest {sha256}", redacted
        )

    def test_redaction_covers_guard_and_preflight_serials(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, finish=False)
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("flash_run_started", "running", {
                "serial": SERIAL, "guard_serials": ["cccc2222"], "requested_flashes": 2,
            })
            publisher.emit("command_started", "running", {"command_id": "g", "argv": ["ubertooth-util", "-U", "cccc2222", "-v"]})
            publisher.emit("command_finished", "passed", {"command_id": "g", "returncode": 0, "duration_s": 0.1})

            report = render_run(load_events(run / "run.jsonl"), redact_serials=True)

            self.assertNotIn("cccc2222", report)
            self.assertNotIn(SERIAL, report)

    def test_report_shows_every_recorded_output_record(self):
        # run.jsonl output is capped when recorded (rawlog.RECORD_*), so the
        # report keeps every record, including how a long command ended.
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("command_started", "running", {"command_id": "c", "argv": ["ubertooth-btle", "-f"]})
            chunk = "x" * 4000 + "\n"
            for _ in range(20):
                publisher.emit("command_output", "running", {"command_id": "c", "stream": "stdout", "text": chunk})
            publisher.emit("command_output", "running", {"command_id": "c", "stream": "stdout", "text": "the end\n"})
            publisher.emit("command_finished", "stopped", {"command_id": "c", "returncode": 124, "duration_s": 4.0, "timed_out": True})

            report = render_run(load_events(run / "run.jsonl"))

            self.assertEqual(20, report.count(chunk.rstrip()))
            self.assertIn("[O] the end", report)
            self.assertNotIn("more characters of output", report)

    def test_output_recorded_before_the_cap_is_capped_by_the_report(self):
        # A run recorded before run.jsonl capped output at write time holds
        # every line, and rendering it printed megabytes. The report keeps
        # the start and the end, and says how much is only in run.jsonl.
        from ubertooth_harness.report import (
            LEGACY_OUTPUT_HEAD_CHARS, LEGACY_OUTPUT_TAIL_CHARS,
        )
        line = "rx block timestamp 283640 * 100 nanoseconds\n"
        records = 5000
        events = [
            command_event("command_started", "running",
                          {"argv": ["ubertooth-dump"]}),
        ]
        events += [
            command_event("command_output", "running",
                          {"stream": "stderr", "text": line})
            for _ in range(records)
        ]
        events.append(command_event("command_output", "running",
                                    {"stream": "stderr", "text": "the end\n"}))
        events.append(command_event("command_finished", "passed",
                                    {"returncode": 0, "duration_s": 9.0}))
        printed = len(line) * records + len("the end\n")

        report = render_run(events)

        kept = report.count(line.rstrip())
        self.assertLess(kept * len(line),
                        LEGACY_OUTPUT_HEAD_CHARS + LEGACY_OUTPUT_TAIL_CHARS)
        self.assertGreater(kept, 0)
        self.assertIn("[E] the end", report)  # how the command ended
        gap = int(re.search(
            r"\[M\] (\d+) more characters of output are in run\.jsonl", report,
        ).group(1))
        # Everything but the start and the end of the output, give or take
        # the line the cap fell inside.
        self.assertAlmostEqual(
            printed - LEGACY_OUTPUT_HEAD_CHARS - LEGACY_OUTPUT_TAIL_CHARS,
            gap, delta=len(line),
        )
        # The end starts on a whole line, as run.jsonl's own tail does. (The
        # start may stop inside one, as run.jsonl's head does: it is bounded
        # by size, not by lines.)
        after = report.split("more characters of output are in run.jsonl\n", 1)[1]
        for text in after.splitlines():
            if "rx block" in text:
                self.assertTrue(text.endswith(line.rstrip()), text)

    def test_output_capped_when_it_was_recorded_is_shown_as_recorded(self):
        # The records a capped command leaves are already the start and the
        # end of its output, with its own [M] note between them: capping
        # them again would cut output the run deliberately kept.
        for named in ({"output_file": "output/c.log"},
                      {"output_files": [{"path": "output/c.stderr.log"}]}):
            with tempfile.TemporaryDirectory() as directory, self.subTest(**named):
                run = Path(directory) / "run-a"
                publisher = open_run_publisher(run, "run-a")
                publisher.emit("command_started", "running", {
                    "command_id": "c", "argv": ["ubertooth-dump"],
                })
                chunk = "x" * 1000 + "\n"
                for _ in range(100):
                    publisher.emit("command_output", "running", {
                        "command_id": "c", "stream": "stderr", "text": chunk,
                    })
                publisher.emit("command_output_omitted", "running", {
                    "command_id": "c", **named,
                    "output_omitted": {"lines": {"stderr": 7}, "chars": {"stderr": 70}},
                })
                publisher.emit("command_output", "running", {
                    "command_id": "c", "stream": "stderr", "text": "the end\n",
                })
                publisher.emit("command_finished", "passed", {
                    "command_id": "c", "returncode": 0, "duration_s": 9.0, **named,
                })

                report = render_run(load_events(run / "run.jsonl"))

                self.assertEqual(100, report.count(chunk.rstrip()))
                self.assertNotIn("more characters of output are in run.jsonl", report)
                self.assertIn("7 stderr lines (70 characters)", report)

    def test_older_runs_with_offset_timestamps_render_elapsed_times(self):
        events = [
            {"run_id": "old", "sequence": 1, "timestamp": "2026-10-23T23:00:00.000000+00:00",
             "event_type": "run_started", "status": "running", "details": {}},
            {"run_id": "old", "sequence": 2, "timestamp": "2026-10-23T23:00:01.500",
             "event_type": "run_finished", "status": "passed", "details": {}},
        ]

        report = render_run(events)

        self.assertIn("00:00:00.000     === RUN old", report)
        self.assertIn("00:00:01.500     === RUN passed ===", report)

    def test_unfinished_run_still_renders(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, finish=False)
            with (run / "run.jsonl").open("a") as stream:
                stream.write('{"truncated": ')  # crash mid-write

            report = render_run(load_events(run / "run.jsonl"))

            self.assertIn("incomplete (no run_finished record)", report)
            self.assertIn("did not finish", report)

    def test_several_runs_in_one_report(self):
        with tempfile.TemporaryDirectory() as directory:
            mac = Path(directory) / "mac-run"
            linux = Path(directory) / "linux-run"
            write_run(mac, host="Host_Mac")
            write_run(linux, host="Host_Linux")

            report = render_runs([mac, linux], title="Release validation")

            self.assertTrue(report.startswith("# Release validation"))
            self.assertIn("| `mac-run` | Host_Mac | passed |", report)
            self.assertIn("| `linux-run` | Host_Linux | passed |", report)
            self.assertNotIn("## Transcript", report)

    def test_records_are_valid_json_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run)

            lines = (run / "run.jsonl").read_text().splitlines()

            self.assertEqual("run_started", json.loads(lines[0])["event_type"])
            self.assertEqual("run_finished", json.loads(lines[-1])["event_type"])


if __name__ == "__main__":
    unittest.main()
