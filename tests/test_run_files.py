"""Serving one page of a run's saved output file, and the GUI viewer (#187)."""

import json
from pathlib import Path
import tempfile
import unittest

from ubertooth_harness.events import RUN_LOG
from ubertooth_harness.outputfiles import OUTPUT_DIR, OutputFile, saved_output
from ubertooth_harness.runfiles import (
    MAX_PAGE_BYTES,
    PAGE_BYTES,
    OutputFileError,
    confined,
    listing,
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


class PageTests(unittest.TestCase):

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = write_run(Path(self._directory.name))

    def tearDown(self):
        self._directory.cleanup()

    def page(self, path, offset=0, limit=PAGE_BYTES):
        return read_output_page(self.root, path, offset, limit)

    def test_a_path_the_filesystem_refuses_is_answered_not_raised(self):
        for path in ("output/\x00.log", "output/" + "x" * 400 + ".log"):
            with self.subTest(path=path), self.assertRaises(OutputFileError) as caught:
                confined(self.root, path)
            self.assertEqual(400, caught.exception.status)
            self.assertNotIn(str(self.root), caught.exception.detail)

    def test_a_file_removed_after_it_was_recorded_is_answered_not_raised(self):
        page = self.page(STDERR_FILE)
        self.assertTrue(page["text"])
        (self.root / STDERR_FILE).unlink()
        with self.assertRaises(OutputFileError) as caught:
            self.page(STDERR_FILE)
        self.assertEqual(404, caught.exception.status)

    def test_a_file_the_controller_does_not_have_is_listed_but_not_read(self):
        # Through the runner service the output/ files stay on the target.
        (self.root / STDOUT_FILE).unlink()
        listed = {item["path"]: item["readable"] for item in listing(self.root)}
        self.assertEqual({STDOUT_FILE: False, STDERR_FILE: True,
                          CAPTURE_FILE: True}, listed)
        with self.assertRaises(OutputFileError) as caught:
            self.page(STDOUT_FILE)
        self.assertEqual(404, caught.exception.status)
        self.assertIn("saved on the target", caught.exception.detail)

    def test_a_run_with_no_log_lists_nothing(self):
        (self.root / RUN_LOG).unlink()
        self.assertEqual([], listing(self.root))

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

    def test_a_symlink_is_never_followed_even_inside_the_run_directory(self):
        out = self.root / OUTPUT_DIR
        (out / "link.stdout.log").symlink_to(
            Path(self._directory.name) / "elsewhere.txt")
        # A link that stays inside would let a record name one file and be
        # served another, so it is refused as well.
        (out / "inside.stdout.log").symlink_to(self.root / RUN_LOG)
        for name in ("link.stdout.log", "inside.stdout.log"):
            with self.subTest(name=name), self.assertRaises(OutputFileError) as caught:
                confined(self.root, f"{OUTPUT_DIR}/{name}")
            self.assertEqual(400, caught.exception.status)
            self.assertIn("symbolic link", caught.exception.detail)

    def test_a_symlink_to_a_directory_out_of_the_run_is_not_followed(self):
        outside = Path(self._directory.name) / "outside"
        outside.mkdir()
        (outside / "x.log").write_text("not yours\n")
        (self.root / "away").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OutputFileError) as caught:
            confined(self.root, "away/x.log")
        self.assertEqual(400, caught.exception.status)
        self.assertIn("symbolic link", caught.exception.detail)

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

    def test_the_panel_sits_beside_the_log_tail_only_when_a_file_is_open(self):
        self.assertIn('id="live-file"', self.html)
        self.assertIn('id="file-view"', self.html)
        # Without a file the log tail keeps the whole wide row.
        self.assertIn('"overview checklist" "log log" "file file"', self.css)
        self.assertIn('.live-showing-file', self.css)
        self.assertIn('"overview checklist" "log file"', self.css)
        self.assertIn("live-showing-file", self.js)

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

    def test_a_file_that_stayed_on_the_target_is_listed_but_not_a_link(self):
        self.assertIn("file.readable === false", self.js)
        self.assertIn("(on the target)", self.js)

    def test_the_list_follows_the_run_and_the_build_files_are_reachable(self):
        # Refreshed as commands save their output, not only on selection.
        self.assertIn('kind === "command_finished" || kind === "output_files"',
                      self.js)
        self.assertIn("Build and preflight output: ", self.js)
        self.assertIn("runFiles(", self.js)

    def test_choosing_another_run_closes_the_open_file(self):
        self.assertIn("state.openFile = null;", self.js)

    def test_the_header_warns_when_the_file_on_disk_is_not_the_recorded_one(self):
        self.assertIn("page.file_bytes !== page.bytes", self.js)
        self.assertIn("not the file the run recorded", self.js)


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

    async def test_a_recorded_symlink_does_not_serve_another_file(self):
        run_id = await self.stored_run()
        link = self.results / run_id / OUTPUT_DIR / "002-command-1.stdout.log"
        link.unlink()
        link.symlink_to(self.results / run_id / RUN_LOG)
        status, payload, _ = await self.http(
            "GET", f"/api/runs/{run_id}/file?path={STDOUT_FILE}")
        self.assertEqual(400, status, payload)
        self.assertNotIn("run_started", json.dumps(payload))

    async def test_a_run_that_is_not_there_is_refused(self):
        status, _payload, _ = await self.http("GET", "/api/runs/absent/files")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
