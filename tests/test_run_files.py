"""Serving one page of a run's saved output file, and the GUI viewer (#187).

A run a target runner carried out keeps its ``output/`` files in the runner's
own run directory, so the controller asks that runner for a page (#226): the
protocol pair, the runner serving it under the same confinement, and the
route forwarding it are all here.
"""

import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from ubertooth_harness.events import RUN_LOG
from ubertooth_harness.outputfiles import OUTPUT_DIR, OutputFile, saved_output
from ubertooth_harness.protocol import (
    MAX_FILE_BYTES,
    MAX_FILE_PAGE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_controller_message,
    decode_runner_message,
    encode_controller_message,
    encode_runner_message,
)
from ubertooth_harness.runfiles import (
    MAX_PAGE_BYTES,
    PAGE_BYTES,
    OutputFileError,
    confined,
    listing,
    read_output_page,
)
from ubertooth_harness.controller import (
    MAX_PENDING_FILES_PER_TARGET,
    ControllerError,
)
from ubertooth_harness import runner_service
from ubertooth_harness.runner_service import MAX_FILE_REQUESTS, RunnerService

try:  # the controller and runner tests' fixtures, by whichever import path works
    from test_controller import ControllerTestCase, FakeRunner, wait_until
    from test_runner_service import StubController, RunnerFixture, TIMEOUT_S
except ImportError:  # pragma: no cover - depends on how the suite is run
    from tests.test_controller import ControllerTestCase, FakeRunner, wait_until
    from tests.test_runner_service import StubController, RunnerFixture, TIMEOUT_S

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


def page_for(path=None, offset=0, limit=PAGE_BYTES, body=BODY, stream="stdout"):
    """One page as ``read_output_page`` builds it, for a scripted runner."""
    data = body.encode("utf-8")
    chunk = data[offset:offset + limit]
    return {
        "path": STDOUT_FILE if path is None else path,
        "stream": stream,
        "bytes": len(data),
        "lines": body.count("\n"),
        "sha256": _sha(body),
        "file_bytes": len(data),
        "offset": offset,
        "length": len(chunk),
        "eof": offset + len(chunk) >= len(data),
        "page_bytes": limit,
        "text": chunk.decode("utf-8", "replace"),
    }


def malformed_runs() -> dict:
    """run.jsonl records that are JSON but describe no readable file.

    A stored record is ordinary data, and bad data takes more shapes than a
    wrong number: each of these raises a different exception inside the file
    list, and none of them may end as silence or as a server failure.
    """
    saved = file_record(STDOUT_FILE, "stdout", BODY)
    files = lambda value: [event(1, "command_finished", {  # noqa: E731
        "phase": "test", "invocation_id": "002",
        "command_id": "002-command-1", "output_files": [value],
    })]
    return {
        "a size that is not a number": files({**saved, "bytes": "no"}),
        "a size too large for an integer": files(
            {**saved, "bytes": float("inf")}),
        "details that are not an object": [
            event(1, "command_finished", "not an object")],
        "a check whose files are not a list": [
            *files(saved),
            event(2, "output_checks", {
                "phase": "test", "invocation_id": "002",
                "command_id": "002-command-1",
                "checks": [{"kind": "expected", "name": "x", "files": 5}],
            }),
        ],
    }


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

    def test_a_file_on_the_target_is_a_link_while_its_runner_is_connected(self):
        # It opens like any other file: the controller asks the runner for a
        # page. Only while that runner is away is it listed and not a link.
        self.assertIn("file.readable === false && onTarget", self.js)
        self.assertIn("(on the target)", self.js)
        self.assertIn("function runnerConnected(", self.js)
        self.assertIn("target.connected && target.online", self.js)
        # And the list follows the target's status, so a link appears when
        # its runner comes back without the page being reloaded.
        self.assertIn('data.type === "target"', self.js)

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


class ProtocolMessageTests(unittest.TestCase):
    """The run_file request and its two answers (#226)."""

    def request(self, **overrides):
        fields = dict(
            request_id="f1", run_id="run-1", path=STDOUT_FILE, offset=0,
            limit=PAGE_BYTES,
        )
        fields.update(overrides)
        return encode_controller_message("run_file", **fields)

    def test_the_request_and_its_answers_round_trip(self):
        message = decode_controller_message(self.request(offset=64, limit=128))
        self.assertEqual(
            ("run_file", "run-1", STDOUT_FILE, 64, 128),
            (message["type"], message["run_id"], message["path"],
             message["offset"], message["limit"]),
        )
        page = decode_runner_message(encode_runner_message(
            "run_file_page", request_id="f1", run_id="run-1", page=page_for()))
        self.assertEqual(BODY[:PAGE_BYTES], page["page"]["text"])
        self.assertEqual(len(BODY.encode()), page["page"]["bytes"])
        refused = decode_runner_message(encode_runner_message(
            "run_file_refused", request_id="f1", run_id="run-1", status=404,
            reason="no such file"))
        self.assertEqual(404, refused["status"])

    def test_a_request_that_could_not_name_a_recorded_file_is_rejected(self):
        for field, value in (
            ("path", "/etc/passwd"),
            ("path", ""),
            ("path", "output/x\x00.log"),
            ("path", "o" * (1024 + 1)),
            ("offset", -1),
            ("offset", 1.5),
            # Bounded here rather than left to how long a number an
            # interpreter will read.
            ("offset", MAX_FILE_BYTES + 1),
            ("limit", 0),
            ("limit", MAX_FILE_PAGE_BYTES + 1),
            ("run_id", "../other"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ProtocolError):
                    self.request(**{field: value})

    def test_an_answer_that_does_not_fit_the_page_shape_is_rejected(self):
        for change in (
            {"text": "x" * (MAX_FILE_PAGE_BYTES + 1)},
            {"page_bytes": MAX_FILE_PAGE_BYTES + 1},
            {"offset": -1},
            {"offset": MAX_FILE_BYTES + 1},
            {"bytes": MAX_FILE_BYTES + 1},
            {"eof": "yes"},
            {"path": "/absolute.log"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ProtocolError):
                    encode_runner_message(
                        "run_file_page", request_id="f1", run_id="run-1",
                        page={**page_for(), **change},
                    )
        # A field more or less than the page carries is rejected as well.
        for page in ({k: v for k, v in page_for().items() if k != "sha256"},
                     {**page_for(), "extra": 1}):
            with self.assertRaises(ProtocolError):
                encode_runner_message(
                    "run_file_page", request_id="f1", run_id="run-1", page=page)

    def test_a_refusal_carries_only_a_status_the_route_can_answer(self):
        for status in (200, 500, 418, True):
            with self.subTest(status=status), self.assertRaises(ProtocolError):
                encode_runner_message(
                    "run_file_refused", request_id="f1", run_id="run-1",
                    status=status, reason="no")

    def test_the_version_says_the_pair_is_there(self):
        # The protocol has no negotiation, so the pair arrived with a bump:
        # version 4, and no version since has taken it away.
        self.assertGreaterEqual(PROTOCOL_VERSION, 4)


class RunnerPageTests(unittest.IsolatedAsyncioTestCase):
    """The runner serving a page of one of its own runs, under the same
    confinement as the controller's route (#226)."""

    RUN_ID = "run-20261031-000001"

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.fixture = RunnerFixture(Path(self._directory.name))
        self.run_dir = self.fixture.output / self.RUN_ID
        self.run_dir.mkdir(parents=True)
        write_run(self.run_dir)
        self.logs = []

    def tearDown(self):
        self._directory.cleanup()

    async def asyncSetUp(self):
        self.stub = await StubController().__aenter__()
        self.service = RunnerService(
            self.fixture.config(self.stub.url), log=self.logs.append,
        )
        self.task = asyncio.create_task(self.service.run_forever())
        await self.stub.next("register")

    async def asyncTearDown(self):
        self.service.request_stop()
        await asyncio.wait_for(self.task, TIMEOUT_S)
        await self.stub.__aexit__(None, None, None)

    async def ask(self, path, *, run_id=None, offset=0, limit=PAGE_BYTES,
                  request_id="f1"):
        await self.stub.send(encode_controller_message(
            "run_file", request_id=request_id,
            run_id=self.RUN_ID if run_id is None else run_id,
            path=path, offset=offset, limit=limit,
        ))
        return await self.stub.next(skip=("heartbeat",))

    async def refusal(self, path, **kwargs):
        answer = await self.ask(path, **kwargs)
        self.assertEqual("run_file_refused", answer["type"], answer)
        # Never says where the run directory is on the target.
        self.assertNotIn(str(self.fixture.output), answer["reason"])
        self.assertNotIn(str(self._directory.name), answer["reason"])
        return answer

    async def test_a_recorded_file_of_one_of_its_runs_is_served(self):
        answer = await self.ask(STDOUT_FILE, offset=10, limit=40)
        self.assertEqual("run_file_page", answer["type"], answer)
        self.assertEqual(("f1", self.RUN_ID), (answer["request_id"], answer["run_id"]))
        page = answer["page"]
        self.assertEqual(BODY[10:50], page["text"])
        self.assertEqual((10, 40, False), (page["offset"], page["length"], page["eof"]))
        self.assertEqual(len(BODY.encode()), page["bytes"])
        self.assertEqual(_sha(BODY), page["sha256"])

    async def test_the_last_page_and_an_offset_past_the_end_say_so(self):
        size = len(BODY.encode())
        self.assertTrue((await self.ask(STDOUT_FILE, offset=size - 5))["page"]["eof"])
        past = await self.ask(STDOUT_FILE, offset=size * 2)
        self.assertEqual(("", True), (past["page"]["text"], past["page"]["eof"]))

    async def test_a_run_this_runner_does_not_have_is_refused(self):
        answer = await self.refusal(STDOUT_FILE, run_id="run-somebody-else")
        self.assertEqual(404, answer["status"])
        self.assertIn("no run", answer["reason"])

    async def test_a_path_the_run_did_not_record_is_refused(self):
        (self.run_dir / "secret.txt").write_text("secret\n")
        for path in ("secret.txt", RUN_LOG, f"{OUTPUT_DIR}/other.log"):
            with self.subTest(path=path):
                answer = await self.refusal(path)
                self.assertEqual(404, answer["status"])
                self.assertIn("no saved output file", answer["reason"])

    async def test_a_path_that_climbs_out_of_the_run_directory_is_refused(self):
        # A path that climbs out is not one the run recorded, so it is
        # refused before the path itself is looked at; ConfinementTests
        # covers the check that refuses it even if a record named it.
        (self.fixture.output / "elsewhere.txt").write_text("not yours\n")
        for path in ("../elsewhere.txt", f"{OUTPUT_DIR}/../../elsewhere.txt", ".."):
            with self.subTest(path=path):
                answer = await self.refusal(path)
                self.assertEqual(404, answer["status"])
                self.assertIn("no saved output file", answer["reason"])
                self.assertNotIn("not yours", answer["reason"])

    async def test_a_recorded_path_that_leaves_the_run_is_refused(self):
        # A run.jsonl record is ordinary data, so a record that names a path
        # outside the run directory is refused by the path check rather than
        # opened: being recorded is necessary, never sufficient.
        (self.fixture.output / "elsewhere.txt").write_text("not yours\n")
        climbing = "../elsewhere.txt"
        write_run(self.run_dir, events=[event(1, "command_finished", {
            "phase": "test", "invocation_id": "002",
            "command_id": "002-command-1", "returncode": 0,
            "output_files": [file_record(climbing, "stdout", "not yours\n")],
        })])
        answer = await self.refusal(climbing)
        self.assertEqual(400, answer["status"])
        self.assertIn("not a path inside the run directory", answer["reason"])
        self.assertNotIn("not yours", answer["reason"])

    async def test_an_absolute_path_never_reaches_the_run_directory(self):
        # The schema refuses it, so the runner answers protocol_error and
        # does not look at any file at all.
        await self.stub.send(json.dumps({
            "version": PROTOCOL_VERSION, "type": "run_file", "request_id": "f1",
            "run_id": self.RUN_ID, "path": "/etc/passwd", "offset": 0,
            "limit": PAGE_BYTES,
        }))
        answer = await self.stub.next(skip=("heartbeat",))
        self.assertEqual("protocol_error", answer["type"], answer)
        self.assertIn("path", answer["reason"])

    async def test_a_symbolic_link_is_not_served_under_a_recorded_name(self):
        served = self.run_dir / STDOUT_FILE
        served.unlink()
        served.symlink_to(self.run_dir / RUN_LOG)
        answer = await self.refusal(STDOUT_FILE)
        self.assertEqual(400, answer["status"])
        self.assertIn("symbolic link", answer["reason"])

    async def test_a_run_directory_that_is_a_symbolic_link_is_refused(self):
        # As for a file inside a run, no link is followed for the run
        # directory: one that pointed beside itself would pass a check on
        # the parent alone and serve another run's records.
        (self.fixture.output / "run-linked").symlink_to(
            self.run_dir, target_is_directory=True)
        answer = await self.refusal(STDOUT_FILE, run_id="run-linked")
        self.assertEqual(404, answer["status"])
        self.assertIn("no run", answer["reason"])

    async def test_a_record_a_page_cannot_be_built_from_is_refused(self):
        # A run.jsonl record is data, and bad data takes many shapes. None of
        # them may end without an answer: the caller on the other side would
        # wait out its timeout for one that never comes.
        for shape, events in malformed_runs().items():
            with self.subTest(shape=shape):
                write_run(self.run_dir, events=events)
                answer = await self.refusal(STDOUT_FILE)
                self.assertEqual(404, answer["status"])
                self.assertIn("cannot be served", answer["reason"])

    async def test_a_record_the_protocol_cannot_carry_is_refused(self):
        write_run(self.run_dir, events=[event(1, "command_finished", {
            "phase": "test", "invocation_id": "002",
            "command_id": "002-command-1", "returncode": 0,
            "output_files": [{**file_record(STDOUT_FILE, "stdout", BODY),
                              "lines": -5}],
        })])
        answer = await self.refusal(STDOUT_FILE)
        self.assertEqual(404, answer["status"])
        self.assertIn("cannot carry", answer["reason"])

    async def test_a_recorded_file_that_is_gone_is_refused_plainly(self):
        (self.run_dir / STDOUT_FILE).unlink()
        answer = await self.refusal(STDOUT_FILE)
        self.assertEqual(404, answer["status"])
        # It is this target's own run directory, so "on the target" would be
        # absurd here.
        self.assertNotIn("on the target, not here", answer["reason"])
        self.assertIn("no longer in its run directory", answer["reason"])

    async def test_a_read_outlives_the_connection_rather_than_being_forgotten(self):
        # Cancelling the task would not stop the read it waits on, only
        # forget it, and the next connection would admit as many requests
        # again on top of it.
        finish = threading.Event()
        reading = threading.Semaphore(0)
        real = runner_service.read_output_page

        def slow(*args, **kwargs):
            reading.release()
            finish.wait(TIMEOUT_S)
            return real(*args, **kwargs)

        runner_service.read_output_page = slow
        try:
            await self.stub.send(encode_controller_message(
                "run_file", request_id="f1", run_id=self.RUN_ID,
                path=STDOUT_FILE, offset=0, limit=64))
            await asyncio.to_thread(reading.acquire, True, TIMEOUT_S)
            await self.stub.connections[-1].close()
            await asyncio.sleep(0.2)
            # Still there, so the next connection counts it against the four
            # this runner answers at once.
            self.assertEqual(1, len(self.service._file_tasks))
            task, = self.service._file_tasks
            finish.set()
            await wait_until(task.done)
            self.assertFalse(task.cancelled())
            self.assertIsNone(task.exception())
        finally:
            finish.set()
            runner_service.read_output_page = real

    async def test_more_requests_at_once_than_it_serves_are_refused_as_busy(self):
        reading = threading.Semaphore(0)
        finish = threading.Event()
        real = runner_service.read_output_page

        def slow(*args, **kwargs):
            reading.release()
            finish.wait(TIMEOUT_S)
            return real(*args, **kwargs)

        runner_service.read_output_page = slow
        try:
            for number in range(MAX_FILE_REQUESTS):
                await self.stub.send(encode_controller_message(
                    "run_file", request_id=f"f{number}", run_id=self.RUN_ID,
                    path=STDOUT_FILE, offset=0, limit=64))
            # Every slot is taken once each request has reached the read.
            for _ in range(MAX_FILE_REQUESTS):
                await asyncio.to_thread(reading.acquire, True, TIMEOUT_S)
            answer = await self.ask(STDOUT_FILE, request_id="one-too-many")
            self.assertEqual("run_file_refused", answer["type"], answer)
            self.assertEqual((503, "one-too-many"),
                             (answer["status"], answer["request_id"]))
        finally:
            finish.set()
            runner_service.read_output_page = real
        # The held requests are still answered.
        answered = set()
        while len(answered) < MAX_FILE_REQUESTS:
            served = await self.stub.next("run_file_page", skip=("heartbeat",))
            answered.add(served["request_id"])
        self.assertEqual({f"f{n}" for n in range(MAX_FILE_REQUESTS)}, answered)


class ForwardedFileTests(ControllerTestCase):
    """The route asking the runner that owns the run for a page (#226)."""

    RUN_ID = "run-on-target"

    async def stored_run(self, run_id=RUN_ID, target="Host_A"):
        """A run as the runner service leaves it here: run.jsonl, no files."""
        directory = self.results / run_id
        directory.mkdir(parents=True)
        write_run(directory)
        shutil.rmtree(directory / OUTPUT_DIR)
        shutil.rmtree(directory / Path(CAPTURE_FILE).parts[0])
        (directory / "run.json").write_text(json.dumps({
            "run_id": run_id, "target": target, "status": "passed",
            "ref": "main", "plan": "fast", "suite": "fast",
            "created_at": "2026-10-30T00:00:00+00:00",
            "finished_at": "2026-10-30T00:01:00+00:00",
            "runner_run_directory": "/runs/" + run_id,
        }) + "\n")
        await self.server.stop()
        await self.start_controller()
        return run_id

    async def get_file(self, run_id, path=STDOUT_FILE, query=""):
        return asyncio.create_task(self.http(
            "GET", f"/api/runs/{run_id}/file?path={path}{query}"))

    async def test_the_list_marks_the_files_as_not_readable_here(self):
        run_id = await self.stored_run()
        status, payload, _ = await self.http("GET", f"/api/runs/{run_id}/files")
        self.assertEqual(200, status, payload)
        self.assertEqual([False, False, False],
                         [file["readable"] for file in payload["files"]])

    async def test_a_page_is_fetched_from_the_runner_that_has_the_file(self):
        run_id = await self.stored_run()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            get = await self.get_file(run_id, query="&offset=0&limit=50")
            request = await runner.recv()
            self.assertEqual("run_file", request["type"])
            self.assertEqual((run_id, STDOUT_FILE, 0, 50),
                             (request["run_id"], request["path"],
                              request["offset"], request["limit"]))
            await runner.send(
                "run_file_page", request_id=request["request_id"], run_id=run_id,
                page=page_for(STDOUT_FILE, 0, 50))
            status, payload, _ = await get
            self.assertEqual(200, status, payload)
            self.assertEqual(BODY[:50], payload["text"])
            self.assertEqual(len(BODY.encode()), payload["bytes"])
            self.assertEqual({}, self.controller._pending_files)

    async def test_a_page_larger_than_one_message_carries_is_asked_smaller(self):
        run_id = await self.stored_run()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            get = await self.get_file(run_id, query=f"&limit={MAX_PAGE_BYTES}")
            request = await runner.recv()
            self.assertEqual(MAX_FILE_PAGE_BYTES, request["limit"])
            await runner.send(
                "run_file_page", request_id=request["request_id"], run_id=run_id,
                page=page_for(STDOUT_FILE, 0, MAX_FILE_PAGE_BYTES))
            status, payload, _ = await get
            self.assertEqual(200, status, payload)
            # What a page is, so a reader pages on by the right step.
            self.assertEqual(MAX_FILE_PAGE_BYTES, payload["page_bytes"])

    async def test_the_runner_s_refusal_is_the_caller_s_answer(self):
        run_id = await self.stored_run()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            get = await self.get_file(run_id)
            request = await runner.recv()
            await runner.send(
                "run_file_refused", request_id=request["request_id"],
                run_id=run_id, status=404,
                reason=f"{STDOUT_FILE!r} is no longer in its run directory")
            status, payload, _ = await get
            self.assertEqual(404, status, payload)
            self.assertIn("no longer in its run directory", payload["detail"])

    async def test_a_file_on_a_target_whose_runner_is_away_is_a_503(self):
        run_id = await self.stored_run()
        status, payload, _ = await self.http(
            "GET", f"/api/runs/{run_id}/file?path={STDOUT_FILE}")
        self.assertEqual(503, status, payload)
        self.assertIn("Host_A", payload["detail"])
        self.assertIn(STDOUT_FILE, payload["detail"])
        # The reason names the path only, never a path on this host.
        self.assertNotIn(str(self.results), payload["detail"])

    async def test_a_runner_that_drops_fails_the_waiting_request(self):
        run_id = await self.stored_run()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            get = await self.get_file(run_id)
            await runner.recv()
            await runner.ws.close()
            status, payload, _ = await get
        self.assertEqual(503, status, payload)
        self.assertIn("disconnected", payload["detail"])

    async def test_an_answer_for_another_file_is_ignored_and_the_right_one_serves(self):
        run_id = await self.stored_run()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            get = await self.get_file(run_id)
            request = await runner.recv()
            for page in (page_for(STDERR_FILE, body="warning: busy\n"),
                         page_for(STDOUT_FILE)):
                await runner.send(
                    "run_file_page", request_id=request["request_id"],
                    run_id=run_id, page=page)
            status, payload, _ = await get
            self.assertEqual(200, status, payload)
            self.assertEqual(STDOUT_FILE, payload["path"])
            target = self.controller.targets["Host_A"]
            self.assertEqual(1, target.protocol_errors)
            self.assertIn("another file", target.last_protocol_error)

    async def test_an_answer_for_another_place_in_the_file_is_ignored(self):
        run_id = await self.stored_run()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            get = await self.get_file(run_id, query="&offset=100&limit=50")
            request = await runner.recv()
            for page in (page_for(STDOUT_FILE, 0, 50),
                         page_for(STDOUT_FILE, 100, 50)):
                await runner.send(
                    "run_file_page", request_id=request["request_id"],
                    run_id=run_id, page=page)
            status, payload, _ = await get
            self.assertEqual(200, status, payload)
            self.assertEqual(100, payload["offset"])
            target = self.controller.targets["Host_A"]
            self.assertEqual(1, target.protocol_errors)
            self.assertIn("another place", target.last_protocol_error)

    async def test_an_offset_past_the_end_may_come_back_clamped(self):
        # A read here clamps an offset past the end to the end, so an answer
        # that does the same is the right answer, not another place.
        run_id = await self.stored_run()
        size = len(BODY.encode())
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            get = await self.get_file(run_id, query=f"&offset={size * 2}")
            request = await runner.recv()
            await runner.send(
                "run_file_page", request_id=request["request_id"], run_id=run_id,
                page=page_for(STDOUT_FILE, size, PAGE_BYTES))
            status, payload, _ = await get
            self.assertEqual(200, status, payload)
            self.assertEqual(("", True), (payload["text"], payload["eof"]))
            self.assertEqual(0, self.controller.targets["Host_A"].protocol_errors)

    async def test_a_cancelled_caller_does_not_keep_a_slot(self):
        run_id = await self.stored_run()
        run = self.controller.get_run(run_id)
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            asked = asyncio.create_task(
                self.controller.read_run_file(run, STDOUT_FILE, 0, 64))
            await runner.recv()
            asked.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asked
            # Whatever became of its caller, the request is not holding one
            # of the eight slots this target has.
            self.assertEqual({}, self.controller._pending_files)

    async def test_a_run_directory_that_is_a_symbolic_link_is_refused(self):
        # As on the runner: no link is followed for the run directory, so a
        # link in the results store cannot serve whatever it points at.
        run_id = await self.stored_run()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        write_run(elsewhere)
        directory = self.results / run_id
        shutil.rmtree(directory)
        directory.symlink_to(elsewhere, target_is_directory=True)
        for path in (f"/api/runs/{run_id}/files",
                     f"/api/runs/{run_id}/file?path={STDOUT_FILE}"):
            with self.subTest(path=path):
                status, payload, _ = await self.http("GET", path)
                self.assertEqual(404, status, payload)
                self.assertIn("no directory in this results store",
                              payload["detail"])
                self.assertNotIn(str(self.root), payload["detail"])

    async def test_an_answer_naming_another_run_is_ignored(self):
        run_id = await self.stored_run()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            get = await self.get_file(run_id)
            request = await runner.recv()
            await runner.send(
                "run_file_refused", request_id=request["request_id"],
                run_id="another-run", status=404, reason="no")
            await wait_until(
                lambda: self.controller.targets["Host_A"].protocol_errors == 1)
            await runner.send(
                "run_file_page", request_id=request["request_id"], run_id=run_id,
                page=page_for(STDOUT_FILE))
            status, payload, _ = await get
            self.assertEqual(200, status, payload)

    async def test_what_the_run_never_recorded_is_refused_without_asking(self):
        run_id = await self.stored_run()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            # Only a path the run's own run.jsonl records is asked of the
            # runner, so a path the caller invents is answered here.
            for path in (RUN_LOG, "..%2frun.json", "secret.txt",
                         "output%2F..%2F..%2Fetc%2Fpasswd"):
                with self.subTest(path=path):
                    status, payload, _ = await self.http(
                        "GET", f"/api/runs/{run_id}/file?path={path}")
                    self.assertEqual(404, status, payload)
                    self.assertEqual({}, self.controller._pending_files)
            # Nothing was asked of the runner, so a page request after them
            # is the first message it sees.
            get = await self.get_file(run_id)
            request = await runner.recv()
            self.assertEqual("run_file", request["type"])
            await runner.send(
                "run_file_refused", request_id=request["request_id"],
                run_id=run_id, status=404, reason="gone")
            await get

    async def test_a_bad_offset_or_limit_is_refused_without_asking(self):
        run_id = await self.stored_run()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            for query in ("&offset=-1", "&limit=0", "&limit=-5"):
                with self.subTest(query=query):
                    status, payload, _ = await self.http(
                        "GET",
                        f"/api/runs/{run_id}/file?path={STDOUT_FILE}{query}")
                    self.assertEqual(400, status, payload)
            self.assertEqual({}, self.controller._pending_files)

    async def test_more_requests_than_are_held_for_one_target_are_refused(self):
        run_id = await self.stored_run()
        run = self.controller.get_run(run_id)
        over = 4
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            # All at once, through the controller rather than HTTP: the cap
            # has to hold when every caller reaches it before any answer
            # comes back, and that many HTTP calls would be that many
            # blocked threads in the test's own pool.
            asked = [
                asyncio.create_task(
                    self.controller.read_run_file(run, STDOUT_FILE, 0, 64))
                for _ in range(MAX_PENDING_FILES_PER_TARGET + over)
            ]
            await wait_until(lambda: sum(task.done() for task in asked) == over)
            self.assertEqual(MAX_PENDING_FILES_PER_TARGET,
                             len(self.controller._pending_files))
            for task in asked:
                if not task.done():
                    continue
                with self.assertRaises(ControllerError) as caught:
                    task.result()
                self.assertEqual(503, caught.exception.status)
                self.assertIn("already waiting", caught.exception.detail)
            for _ in range(MAX_PENDING_FILES_PER_TARGET):
                self.assertEqual("run_file", (await runner.recv())["type"])
            waiting = [task for task in asked if not task.done()]
            self.assertEqual(MAX_PENDING_FILES_PER_TARGET, len(waiting))
            for request_id in list(self.controller._pending_files):
                await runner.send(
                    "run_file_page", request_id=request_id, run_id=run_id,
                    page=page_for(STDOUT_FILE, 0, 64))
            for task in waiting:
                self.assertEqual(BODY[:64], (await task)["text"])

    async def test_an_answer_nobody_waits_for_is_discarded_not_counted(self):
        run_id = await self.stored_run()
        run = self.controller.get_run(run_id)
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            self.controller.reply_timeout_s = 0.1
            with self.assertRaises(ControllerError) as caught:
                await self.controller.read_run_file(run, STDOUT_FILE, 0, 64)
            self.assertEqual(504, caught.exception.status)
            request = await runner.recv()
            # A page nobody waits for can do nothing for anyone, so the
            # request is dropped rather than kept for the grace period.
            self.assertEqual({}, self.controller._pending_files)
            await runner.send(
                "run_file_page", request_id=request["request_id"], run_id=run_id,
                page=page_for(STDOUT_FILE, 0, 64))
            await wait_until(
                lambda: any("discarded a late" in line for line in self.logs))
            # Answering late is not a fault of the runner's.
            self.assertEqual(0, self.controller.targets["Host_A"].protocol_errors)

    async def test_a_record_the_list_cannot_be_built_from_is_a_refusal(self):
        # A stored record is data: a 500 with a traceback is the wrong answer
        # for a run.jsonl that holds, say, a size which is not a number.
        run_id = await self.stored_run()
        log = self.results / run_id / RUN_LOG
        for shape, events in malformed_runs().items():
            log.write_text(
                "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
            for path in (f"/api/runs/{run_id}/files",
                         f"/api/runs/{run_id}/file?path={STDOUT_FILE}"):
                with self.subTest(shape=shape, path=path):
                    status, payload, _ = await self.http("GET", path)
                    self.assertEqual(404, status, payload)
                    self.assertIn("cannot be read from", payload["detail"])
                    self.assertNotIn(str(self.results), payload["detail"])


if __name__ == "__main__":
    unittest.main()
