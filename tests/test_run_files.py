"""The Output files table, the GUI's file list, and serving one page (#187)."""

import json
from pathlib import Path
import tempfile
import unittest

from ubertooth_harness.events import RUN_LOG
from ubertooth_harness.outputfiles import OUTPUT_DIR, OutputFile, saved_output
from ubertooth_harness.report import render_run
from ubertooth_harness.runfiles import (
    MAX_PAGE_BYTES,
    PAGE_BYTES,
    OutputFileError,
    confined,
    read_output_page,
)

try:  # the controller tests' fixture, by whichever import path works
    from test_controller import ControllerTestCase
except ImportError:  # pragma: no cover - depends on how the suite is run
    from tests.test_controller import ControllerTestCase

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


def write_run(root: Path, events=None) -> Path:
    events = run_events() if events is None else events
    (root / RUN_LOG).write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    (root / OUTPUT_DIR).mkdir(exist_ok=True)
    (root / STDOUT_FILE).write_text(BODY, encoding="utf-8")
    (root / STDERR_FILE).write_text("warning: busy\n", encoding="utf-8")
    (root / CAPTURE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / CAPTURE_FILE).write_text("\xd4\xc3\xb2\xa1", encoding="utf-8")
    return root


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
        self.assertIn("### 002", report)
        self.assertIn(f"| {STDOUT_FILE} | stdout |", report)
        self.assertIn(f"`{_sha(BODY)}`", report)
        # Sizes read as a magnitude and a byte count; captures have no lines.
        self.assertIn(f"{len(BODY.encode()) / 1024:.1f} KiB ({len(BODY.encode())} bytes)",
                      report)
        # A binary capture has no line count to show.
        self.assertIn(f"| {CAPTURE_FILE} | capture | 8 bytes |  | ", report)
        # The checks that read each file, and the one that failed.
        self.assertIn("1 case, 1 expected, 1 forbidden: 1 failed "
                      "(Host CLI: two lines were not samples)", report)
        self.assertIn("1 expected, 1 forbidden: all passed", report)
        self.assertIn("| none |", report)

    def test_a_run_with_no_saved_files_has_no_section(self):
        self.assertNotIn("## Output files",
                         render_run([event(1, "run_started", {}, "running")],
                                    transcript=False))


class PageTests(unittest.TestCase):

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = write_run(Path(self._directory.name))
        self.events = run_events()

    def tearDown(self):
        self._directory.cleanup()

    def page(self, path, offset=0, limit=PAGE_BYTES):
        return read_output_page(self.root, self.events, path, offset, limit)

    def test_a_page_names_where_it_sits_in_the_file(self):
        page = self.page(STDOUT_FILE, 0, 100)
        self.assertEqual((0, 100, False), (page["offset"], page["length"], page["eof"]))
        self.assertEqual(BODY[:100], page["text"])
        self.assertEqual(len(BODY.encode()), page["file_bytes"])
        self.assertEqual(_sha(BODY), page["sha256"])
        # The next page starts where this one ended, and the last one says so.
        last = self.page(STDOUT_FILE, len(BODY.encode()) - 11, 100)
        self.assertEqual(BODY[-11:], last["text"])
        self.assertTrue(last["eof"])

    def test_a_page_never_reads_more_than_the_maximum(self):
        page = self.page(STDOUT_FILE, 0, MAX_PAGE_BYTES * 4)
        self.assertEqual(MAX_PAGE_BYTES, page["page_bytes"])

    def test_an_offset_past_the_end_reads_nothing(self):
        page = self.page(STDOUT_FILE, len(BODY.encode()) * 2)
        self.assertEqual(("", True), (page["text"], page["eof"]))

    def test_a_split_character_is_replaced_rather_than_refused(self):
        page = self.page(CAPTURE_FILE, 1, 2)
        self.assertEqual(2, page["length"])
        self.assertTrue(page["text"])

    def test_a_file_the_run_did_not_record_is_refused(self):
        (self.root / "secret.txt").write_text("secret\n")
        for path in ("secret.txt", RUN_LOG, f"{OUTPUT_DIR}/other.stdout.log"):
            with self.subTest(path=path), self.assertRaises(OutputFileError) as caught:
                self.page(path)
            self.assertEqual(404, caught.exception.status)
            self.assertIn("no saved output file", caught.exception.detail)

    def test_a_bad_offset_or_limit_is_refused(self):
        for offset, limit in ((-1, PAGE_BYTES), (0, 0), (0, -5)):
            with self.subTest(offset=offset, limit=limit):
                with self.assertRaises(OutputFileError) as caught:
                    self.page(STDOUT_FILE, offset, limit)
                self.assertEqual(400, caught.exception.status)


class ConfinementTests(unittest.TestCase):
    """Nothing outside the run directory can be read, whatever the path says."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name) / "run"
        self.root.mkdir()
        write_run(self.root)
        (Path(self._directory.name) / "elsewhere.txt").write_text("not yours\n")

    def tearDown(self):
        self._directory.cleanup()

    def test_a_path_that_climbs_out_is_refused(self):
        for path in ("../elsewhere.txt", "output/../../elsewhere.txt",
                     "..", "output/.."):
            with self.subTest(path=path), self.assertRaises(OutputFileError) as caught:
                confined(self.root, path)
            self.assertEqual(400, caught.exception.status)

    def test_an_absolute_path_is_refused(self):
        with self.assertRaises(OutputFileError) as caught:
            confined(self.root, str(Path(self._directory.name) / "elsewhere.txt"))
        self.assertEqual(400, caught.exception.status)
        self.assertIn("inside the run directory", caught.exception.detail)

    def test_a_symlink_out_of_the_run_directory_is_not_followed(self):
        link = self.root / OUTPUT_DIR / "link.stdout.log"
        link.symlink_to(Path(self._directory.name) / "elsewhere.txt")
        with self.assertRaises(OutputFileError) as caught:
            confined(self.root, f"{OUTPUT_DIR}/link.stdout.log")
        self.assertEqual(400, caught.exception.status)
        self.assertIn("leaves the run directory", caught.exception.detail)

    def test_a_symlink_to_a_directory_out_of_the_run_is_not_followed(self):
        outside = Path(self._directory.name) / "outside"
        outside.mkdir()
        (outside / "x.log").write_text("not yours\n")
        (self.root / "away").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OutputFileError) as caught:
            confined(self.root, "away/x.log")
        self.assertEqual(400, caught.exception.status)

    def test_a_directory_and_a_missing_file_are_refused(self):
        for path in (OUTPUT_DIR, f"{OUTPUT_DIR}/absent.log"):
            with self.subTest(path=path), self.assertRaises(OutputFileError) as caught:
                confined(self.root, path)
            self.assertEqual(404, caught.exception.status)

    def test_a_recorded_file_is_read(self):
        self.assertEqual(self.root.resolve() / STDOUT_FILE,
                         confined(self.root, STDOUT_FILE))


class WebAssetTests(unittest.TestCase):
    """The viewer the maintainer asked for, pinned where it is easy to break."""

    def setUp(self):
        web = Path(__file__).resolve().parents[1] / "ubertooth_harness" / "web"
        self.html = (web / "index.html").read_text(encoding="utf-8")
        self.css = (web / "app.css").read_text(encoding="utf-8")
        self.js = (web / "app.js").read_text(encoding="utf-8")

    def test_the_panel_sits_beside_the_log_tail(self):
        self.assertIn('id="live-file"', self.html)
        self.assertIn('id="file-view"', self.html)
        self.assertIn('"log file"', self.css)

    def test_a_saved_file_is_black_on_white_in_a_fixed_width_font(self):
        view = self.css.split(".file-view {", 1)[1].split("}", 1)[0]
        self.assertIn("background: #ffffff", view)
        self.assertIn("color: #000000", view)
        self.assertIn("monospace", view)

    def test_a_large_file_is_read_in_pages_with_its_name_size_and_hash(self):
        self.assertIn('id="file-previous"', self.html)
        self.assertIn('id="file-next"', self.html)
        self.assertIn("offset=", self.js)
        self.assertIn("SHA-256", self.js)
        self.assertIn("fileSize(page.file_bytes)", self.js)

    def test_each_saved_file_is_a_link_in_the_run_s_results(self):
        self.assertIn("check-files", self.js)
        self.assertIn("openOutputFile", self.js)
        self.assertIn("/files`", self.js)


class RouteTests(ControllerTestCase):
    """The two routes, on the controller's own loopback server."""

    async def stored_run(self, run_id="run-files"):
        directory = self.results / run_id
        directory.mkdir(parents=True)
        write_run(directory)
        (directory / "run.json").write_text(json.dumps({
            "run_id": run_id, "target": "Host_A", "status": "passed",
            "ref": "main", "commit": "a" * 40, "plan": "fast", "suite": "fast",
            "created_at": "2026-10-30T00:00:00+00:00",
            "finished_at": "2026-10-30T00:01:00+00:00",
        }) + "\n")
        # The controller reads the runs it already has when it starts.
        await self.server.stop()
        await self.start_controller()
        return run_id

    async def test_the_list_names_every_file_and_its_checks(self):
        run_id = await self.stored_run()
        status, payload, _ = await self.http("GET", f"/api/runs/{run_id}/files")
        self.assertEqual(200, status, payload)
        paths = [file["path"] for file in payload["files"]]
        self.assertEqual([STDOUT_FILE, STDERR_FILE, CAPTURE_FILE], paths)
        self.assertEqual(3, len(payload["files"][0]["checks"]))
        self.assertEqual("002", payload["files"][0]["owner"]["invocation_id"])

    async def test_one_page_of_a_file_is_served(self):
        run_id = await self.stored_run()
        status, payload, _ = await self.http(
            "GET", f"/api/runs/{run_id}/file?path={STDOUT_FILE}&offset=0&limit=50")
        self.assertEqual(200, status, payload)
        self.assertEqual(BODY[:50], payload["text"])
        self.assertEqual(_sha(BODY), payload["sha256"])

    async def test_a_path_outside_the_run_directory_is_refused(self):
        run_id = await self.stored_run()
        for path in ("../../etc/passwd", "/etc/passwd", "run.jsonl",
                     "..%2frun.json", "output/../../elsewhere.txt"):
            with self.subTest(path=path):
                status, payload, _ = await self.http(
                    "GET", f"/api/runs/{run_id}/file?path={path}")
                self.assertIn(status, (400, 404), payload)
                self.assertNotIn("root:", json.dumps(payload))

    async def test_a_run_that_is_not_there_is_refused(self):
        status, _payload, _ = await self.http("GET", "/api/runs/absent/files")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
