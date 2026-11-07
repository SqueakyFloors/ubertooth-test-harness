"""The controller's run history: paging, filters, and the retention window.

The history grows for as long as a bench runs, so it is read a page at a
time, newest first, and listing it reads only the runs' own summaries: no
stored transcript is parsed to draw a table of runs (#136). An optional
retention window removes old run directories; without one nothing is ever
deleted.

Every test here works in a temporary results directory of its own.
"""

import contextlib
import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from ubertooth_harness.cli import _parser, main

from ubertooth_harness.controller import (
    DEFAULT_RUN_PAGE,
    MAX_RUN_PAGE,
    Controller,
    ControllerConfigError,
    ControllerError,
    Run,
)
from ubertooth_harness.events import REPORT, RUN_LOG
from ubertooth_harness.rungroups import GROUPS_DIR

try:
    from test_controller import ControllerTestCase, FakeRunner, event, wait_until
except ImportError:  # run as tests.test_run_history
    from tests.test_controller import (
        ControllerTestCase,
        FakeRunner,
        event,
        wait_until,
    )

EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def stamp(minutes: float) -> str:
    """A created_at stamp as the controller writes them, minutes after EPOCH."""
    when = EPOCH + datetime.timedelta(minutes=minutes)
    return when.isoformat(timespec="milliseconds")


def write_run(
    results: Path,
    run_id: str,
    *,
    created_at: str,
    finished_at: str | None = None,
    target: str = "Host_A",
    status: str = "passed",
    ref: str = "main",
    plan: str = "fast",
    suite: str = "fast",
    records: int = 0,
    group_id: str | None = None,
    report: bool = False,
) -> Path:
    """One stored run directory, as the controller would have left it."""
    directory = results / run_id
    directory.mkdir(parents=True)
    record = {
        "run_id": run_id, "target": target, "status": status,
        "created_at": created_at, "finished_at": finished_at, "ref": ref,
        "plan": plan, "suite": suite, "learned_durations": True,
        "group_id": group_id, "report": report,
    }
    (directory / "run.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if records:
        with (directory / RUN_LOG).open("w", encoding="utf-8") as stream:
            for sequence in range(1, records + 1):
                stream.write(json.dumps(event(run_id, sequence)) + "\n")
    return directory


class CountingController(Controller):
    """A controller that counts how often it reads a stored transcript."""

    transcript_reads = 0

    def _log_bytes(self, run):
        self.transcript_reads += 1
        return super()._log_bytes(run)


class HistoryFixture(unittest.TestCase):

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.logs = []
        self.addCleanup(self._directory.cleanup)

    def controller(self, *, cls=Controller, **kwargs) -> Controller:
        return cls(self.results, log=self.logs.append, **kwargs)


class PagingTests(HistoryFixture):

    def many_runs(self, count: int) -> list[str]:
        """``count`` stored runs, newest first, one minute apart."""
        for index in range(count):
            write_run(
                self.results, f"run-{index:03d}", created_at=stamp(index),
                finished_at=stamp(index + 0.5),
            )
        return [f"run-{index:03d}" for index in reversed(range(count))]

    def test_a_page_holds_the_newest_fifty_runs_with_a_cursor_for_the_rest(self):
        newest_first = self.many_runs(120)
        controller = self.controller()
        page = controller.run_page()
        self.assertEqual(DEFAULT_RUN_PAGE, page["limit"])
        self.assertEqual(50, page["returned"])
        self.assertEqual(120, page["total"])
        self.assertEqual(120, page["matching"])
        self.assertTrue(page["has_more"])
        self.assertEqual(
            newest_first[:50], [run["run_id"] for run in page["runs"]]
        )
        last = page["runs"][-1]
        self.assertEqual(last["created_at"], page["next_before"])
        self.assertEqual(last["run_id"], page["next_before_run_id"])

    def test_paging_through_the_history_lists_every_run_once(self):
        newest_first = self.many_runs(120)
        controller = self.controller()
        listed, page, pages = [], controller.run_page(limit=25), 1
        while True:
            listed.extend(run["run_id"] for run in page["runs"])
            if not page["has_more"]:
                break
            page = controller.run_page(
                limit=25, before=page["next_before"],
                before_run_id=page["next_before_run_id"],
            )
            pages += 1
        self.assertEqual(5, pages)
        self.assertEqual(newest_first, listed)
        self.assertEqual(len(set(listed)), len(listed))

    def test_runs_stamped_alike_are_ordered_by_run_id_and_not_lost(self):
        # Two runs can share a created_at; the run ID settles their order, so
        # a page boundary between them neither repeats nor loses one.
        for run_id in ("run-a", "run-b", "run-c"):
            write_run(self.results, run_id, created_at=stamp(0))
        controller = self.controller()
        first = controller.run_page(limit=2)
        second = controller.run_page(
            limit=2, before=first["next_before"],
            before_run_id=first["next_before_run_id"],
        )
        self.assertEqual(["run-c", "run-b"], [r["run_id"] for r in first["runs"]])
        self.assertEqual(["run-a"], [r["run_id"] for r in second["runs"]])
        self.assertFalse(second["has_more"])

    def test_a_cursor_without_a_run_id_keeps_every_run_of_that_instant(self):
        # ?before=<created_at> alone means "at or older than that instant",
        # so nothing stamped there is skipped past. It is not on its own a
        # paging cursor: a page that ends in the middle of an instant would
        # serve those runs again, which is why the answer's cursor carries
        # the run ID and why paging uses both.
        for run_id in ("run-a", "run-b", "run-c"):
            write_run(self.results, run_id, created_at=stamp(0))
        write_run(self.results, "run-old", created_at=stamp(-1))
        controller = self.controller()
        page = controller.run_page(before=stamp(0))
        self.assertEqual(
            ["run-c", "run-b", "run-a", "run-old"],
            [run["run_id"] for run in page["runs"]],
        )
        # With the run ID the answer gives, the page goes on from exactly
        # where it stopped.
        first = controller.run_page(limit=1)
        self.assertEqual(["run-c"], [run["run_id"] for run in first["runs"]])
        listed, page = [], first
        while page["has_more"]:
            page = controller.run_page(
                limit=1, before=page["next_before"],
                before_run_id=page["next_before_run_id"],
            )
            listed.extend(run["run_id"] for run in page["runs"])
        self.assertEqual(["run-b", "run-a", "run-old"], listed)

    def test_filters_by_target_status_and_ref(self):
        write_run(self.results, "run-1", created_at=stamp(1), target="Host_A",
                  status="passed", ref="main")
        write_run(self.results, "run-2", created_at=stamp(2), target="Host_B",
                  status="failed", ref="main")
        write_run(self.results, "run-3", created_at=stamp(3), target="Host_A",
                  status="failed", ref="v1.2")
        controller = self.controller()
        for filters, expected in (
            ({"target": "Host_A"}, ["run-3", "run-1"]),
            ({"status": "failed"}, ["run-3", "run-2"]),
            ({"ref": "main"}, ["run-2", "run-1"]),
            ({"target": "Host_A", "status": "failed"}, ["run-3"]),
            ({"target": "Host_B", "ref": "v1.2"}, []),
        ):
            with self.subTest(**filters):
                page = controller.run_page(**filters)
                self.assertEqual(expected, [run["run_id"] for run in page["runs"]])
                self.assertEqual(len(expected), page["matching"])
                # Every stored run, beside the ones the filters match.
                self.assertEqual(3, page["total"])
                self.assertEqual(filters.get("target"), page["filters"]["target"])
                self.assertEqual(filters.get("status"), page["filters"]["status"])
                self.assertEqual(filters.get("ref"), page["filters"]["ref"])

    def test_filters_and_paging_work_together(self):
        for index in range(10):
            write_run(
                self.results, f"run-{index:02d}", created_at=stamp(index),
                target="Host_A" if index % 2 else "Host_B",
            )
        controller = self.controller()
        first = controller.run_page(limit=2, target="Host_A")
        self.assertEqual(["run-09", "run-07"], [r["run_id"] for r in first["runs"]])
        self.assertEqual(5, first["matching"])
        second = controller.run_page(
            limit=2, target="Host_A", before=first["next_before"],
            before_run_id=first["next_before_run_id"],
        )
        self.assertEqual(["run-05", "run-03"], [r["run_id"] for r in second["runs"]])

    def test_a_request_the_controller_cannot_answer_is_refused(self):
        write_run(self.results, "run-1", created_at=stamp(0))
        controller = self.controller()
        for kwargs, detail in (
            ({"limit": 0}, "between 1 and"),
            ({"limit": MAX_RUN_PAGE + 1}, "between 1 and"),
            ({"limit": True}, "whole number"),
            ({"before": "yesterday"}, "created_at"),
            ({"before": "2026-13-45T99"}, "created_at"),
            ({"before_run_id": "run-1"}, "needs before"),
            ({"before": stamp(0), "before_run_id": "../run-1"}, "run ID"),
            ({"status": "PASSED"}, "run status"),
            ({"status": "../.."}, "run status"),
            ({"target": "not a host"}, "registry host name"),
            ({"ref": "main\n"}, "ref name"),
            ({"ref": "r" * 257}, "ref name"),
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ControllerError) as caught:
                    controller.run_page(**kwargs)
                self.assertEqual(422, caught.exception.status)
                self.assertIn(detail, caught.exception.detail)

    def test_an_empty_history_is_an_empty_page(self):
        page = self.controller().run_page()
        self.assertEqual([], page["runs"])
        self.assertEqual(0, page["total"])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_before"])


class TranscriptTests(HistoryFixture):
    """Listing the history reads summaries, never stored transcripts."""

    def test_startup_and_listing_parse_no_stored_transcript(self):
        for index in range(5):
            write_run(
                self.results, f"run-{index}", created_at=stamp(index),
                finished_at=stamp(index + 0.5), records=200,
            )
        controller = self.controller(cls=CountingController)
        self.assertEqual(0, controller.transcript_reads)
        page = controller.run_page()
        self.assertEqual(0, controller.transcript_reads)
        self.assertEqual(5, page["returned"])
        # The record counts belong to the transcript, so they are null
        # rather than a zero that would read as a run that recorded nothing.
        for run in page["runs"]:
            self.assertIsNone(run["event_count"])
            self.assertIsNone(run["malformed_lines"])
            self.assertFalse(run["transcript_read"])

    def test_asking_for_a_runs_records_reads_and_counts_them(self):
        write_run(self.results, "run-1", created_at=stamp(0),
                  finished_at=stamp(1), records=3)
        controller = self.controller(cls=CountingController)
        run = controller.runs["run-1"]
        events = controller.read_events(run)
        self.assertEqual([1, 2, 3], [record["sequence"] for record in events])
        self.assertEqual(1, controller.transcript_reads)
        self.assertTrue(run.transcript_read)
        self.assertEqual(3, run.view()["event_count"])
        self.assertEqual(3, run.view()["last_event_sequence"])

    def test_a_running_run_is_loaded_with_its_records(self):
        # Its sequence numbers are what recognizes a runner's replay after a
        # reconnect, so they are read at startup rather than on demand.
        write_run(self.results, "run-1", created_at=stamp(0), status="running",
                  records=2)
        controller = self.controller(cls=CountingController)
        self.assertEqual(1, controller.transcript_reads)
        self.assertEqual({1, 2}, controller.runs["run-1"].seen)

    def test_reading_a_runs_records_never_truncates_its_transcript(self):
        # A record being appended as a reader reads it is not the reader's
        # to drop: GET /api/runs/{run_id} on a running run reads the same
        # file the runner's records are being appended to.
        directory = write_run(
            self.results, "run-1", created_at=stamp(0), status="running", records=2
        )
        log = directory / RUN_LOG
        with log.open("a", encoding="utf-8") as stream:
            stream.write('{"sequence": 3, "run_id": "run-1"')  # a partial line
        controller = self.controller()
        before = log.read_bytes()
        run = controller.runs["run-1"]
        # Startup repaired it: the run was loaded before the partial line
        # was written, so read it back as it now stands.
        self.assertEqual([1, 2], [record["sequence"] for record in
                                  controller.read_events(run)])
        self.assertEqual(before, log.read_bytes())
        # And the first read of a stored run's records does repair it.
        finished = write_run(
            self.results, "run-2", created_at=stamp(1), finished_at=stamp(2),
            records=2,
        )
        with (finished / RUN_LOG).open("a", encoding="utf-8") as stream:
            stream.write('{"sequence": 3')
        controller = self.controller()
        controller._read_transcript(controller.runs["run-2"])
        self.assertTrue((finished / RUN_LOG).read_bytes().endswith(b"\n"))
        self.assertEqual({1, 2}, controller.runs["run-2"].seen)

    def test_a_transcript_that_cannot_be_read_leaves_the_report_alone(self):
        directory = write_run(
            self.results, "run-1", created_at=stamp(0), finished_at=stamp(1),
            records=2, report=True,
        )
        (directory / REPORT).write_text("# the real report\n", encoding="utf-8")
        controller = self.controller()
        run = controller.runs["run-1"]
        (directory / RUN_LOG).chmod(0o000)
        self.addCleanup((directory / RUN_LOG).chmod, 0o600)
        written, problem = controller._write_report(run, "passed")
        self.assertTrue(written)
        self.assertIn("could not read", problem)
        self.assertEqual("# the real report\n", (directory / REPORT).read_text())


class RetentionTests(HistoryFixture):

    def old_and_new(self) -> Controller:
        write_run(self.results, "run-old", created_at=stamp(0),
                  finished_at=stamp(1))
        write_run(
            self.results, "run-new",
            created_at=(
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=1)
            ).isoformat(timespec="milliseconds"),
            finished_at=datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(timespec="milliseconds"),
        )
        return self.controller(keep_runs_days=7)

    def test_runs_are_kept_for_ever_by_default(self):
        write_run(self.results, "run-ancient", created_at=stamp(0),
                  finished_at=stamp(1))
        controller = self.controller()
        self.assertIsNone(controller.keep_runs_days)
        self.assertEqual([], controller.prune_runs())
        self.assertIn("run-ancient", controller.runs)
        self.assertTrue((self.results / "run-ancient").is_dir())

    def test_a_window_removes_only_the_runs_past_it(self):
        controller = self.old_and_new()
        # The window was applied as the controller started.
        self.assertEqual(["run-new"], list(controller.runs))
        self.assertFalse((self.results / "run-old").exists())
        self.assertTrue((self.results / "run-new").is_dir())
        self.assertTrue(any("retention: removed 1" in line for line in self.logs),
                        self.logs)

    def test_a_running_run_is_never_removed(self):
        write_run(self.results, "run-stuck", created_at=stamp(0), status="running")
        controller = self.controller(keep_runs_days=1)
        self.assertEqual([], controller.prune_runs())
        self.assertIn("run-stuck", controller.runs)
        self.assertTrue((self.results / "run-stuck").is_dir())

    def test_a_run_that_cannot_be_dated_is_kept(self):
        write_run(self.results, "run-odd", created_at="whenever")
        controller = self.controller(keep_runs_days=1)
        self.assertEqual([], controller.prune_runs())
        self.assertTrue((self.results / "run-odd").is_dir())

    def test_an_unfinished_but_terminal_run_is_dated_by_its_start(self):
        write_run(self.results, "run-lost", created_at=stamp(0), status="lost")
        controller = self.controller(keep_runs_days=1)
        # Removed as the controller started: a run that never reported its
        # end is dated by when it was created.
        self.assertEqual({}, controller.runs)
        self.assertFalse((self.results / "run-lost").exists())

    def test_the_estimate_store_and_the_group_directory_are_not_run_directories(self):
        write_run(self.results, "run-old", created_at=stamp(0), finished_at=stamp(1),
                  group_id="group-1")
        groups = self.results / GROUPS_DIR
        groups.mkdir()
        (groups / "group-1.json").write_text(
            json.dumps({
                "group_id": "group-1", "created_at": stamp(0),
                "dispatched_at": stamp(0), "ref": "main", "commit": None,
                "plan": "fast", "suite": "fast", "tests": [],
                "build": False, "flash_build": False, "with_firmware": False,
                "with_btbr": False, "with_python": False,
                "hosts": ["Host_A"],
                "members": [{"host": "Host_A", "run_id": "run-old", "error": None}],
            }) + "\n",
            encoding="utf-8",
        )
        estimates = self.results / "estimates.json"
        estimates.write_text(
            json.dumps({"updated_at": stamp(0), "entries": []}) + "\n",
            encoding="utf-8",
        )
        controller = self.controller(keep_runs_days=1)
        self.assertEqual([], list(controller.runs))
        # The group whose only run is gone goes with it; the learned
        # durations beside the run directories stay.
        self.assertEqual({}, controller.groups)
        self.assertFalse((groups / "group-1.json").exists())
        self.assertTrue(estimates.is_file())
        self.assertTrue(groups.is_dir())

    def test_a_group_with_a_kept_run_keeps_its_record(self):
        write_run(self.results, "run-old", created_at=stamp(0), finished_at=stamp(1),
                  group_id="group-1")
        write_run(self.results, "run-kept", created_at=stamp(0), status="running",
                  group_id="group-1")
        groups = self.results / GROUPS_DIR
        groups.mkdir()
        (groups / "group-1.json").write_text(
            json.dumps({
                "group_id": "group-1", "created_at": stamp(0),
                "dispatched_at": stamp(0), "ref": "main", "commit": None,
                "plan": "fast", "suite": "fast", "tests": [],
                "build": False, "flash_build": False, "with_firmware": False,
                "with_btbr": False, "with_python": False,
                "hosts": ["Host_A", "Host_B"],
                "members": [
                    {"host": "Host_A", "run_id": "run-old", "error": None},
                    {"host": "Host_B", "run_id": "run-kept", "error": None},
                ],
            }) + "\n",
            encoding="utf-8",
        )
        controller = self.controller(keep_runs_days=1)
        self.assertEqual(["run-kept"], list(controller.runs))
        self.assertIn("group-1", controller.groups)
        self.assertTrue((groups / "group-1.json").is_file())

    def test_a_directory_that_is_not_the_runs_own_is_kept(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "treasure.txt").write_text("keep me\n", encoding="utf-8")
        controller = self.controller(keep_runs_days=1)
        # Records the controller never wrote: a run whose name is not a flat
        # directory name, one whose directory is outside the results
        # directory, and one whose stored record names another run.
        write_run(self.results, "run-other", created_at=stamp(0), finished_at=stamp(1))
        (self.results / "run-other" / "run.json").write_text(
            json.dumps({"run_id": "run-impostor"}) + "\n", encoding="utf-8"
        )
        controller.runs = {
            "..": Run(run_id="..", target="Host_A", directory=self.results.parent,
                      status="failed", created_at=stamp(0)),
            "run-away": Run(run_id="run-away", target="Host_A", directory=elsewhere,
                            status="failed", created_at=stamp(0)),
            "run-other": Run(
                run_id="run-other", target="Host_A",
                directory=self.results / "run-other", status="failed",
                created_at=stamp(0),
            ),
        }
        self.assertEqual([], controller.prune_runs())
        self.assertTrue(elsewhere.is_dir())
        self.assertTrue((elsewhere / "treasure.txt").is_file())
        self.assertTrue(self.results.is_dir())
        self.assertTrue((self.results / "run-other").is_dir())
        self.assertEqual(3, len(controller.runs))
        for part in ("not a flat directory name", "is not the run directory",
                     "does not record run"):
            self.assertTrue(any(part in line for line in self.logs), self.logs)

    def test_a_symbolic_link_in_place_of_a_run_directory_is_kept(self):
        target = self.root / "treasure"
        target.mkdir()
        (target / "run.json").write_text(
            json.dumps({
                "run_id": "run-link", "target": "Host_A", "status": "passed",
                "created_at": stamp(0), "finished_at": stamp(1),
            }) + "\n",
            encoding="utf-8",
        )
        (self.results / "run-link").symlink_to(target, target_is_directory=True)
        controller = self.controller(keep_runs_days=1)
        self.assertEqual([], controller.prune_runs())
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "run.json").is_file())
        self.assertTrue(
            any("a symbolic link" in line for line in self.logs), self.logs
        )

    def test_a_window_outside_zero_is_refused(self):
        for days in (0, -1, float("nan"), float("inf"), "7", True):
            with self.subTest(days), self.assertRaises(ControllerConfigError):
                self.controller(keep_runs_days=days)

    def test_the_window_is_applied_again_only_when_it_is_due(self):
        now = [0.0]
        controller = self.controller(
            keep_runs_days=1, prune_interval_s=60, clock=lambda: now[0]
        )
        write_run(self.results, "run-old", created_at=stamp(0), finished_at=stamp(1))
        controller._load_runs()
        self.assertEqual([], controller.prune_due())  # not due yet
        self.assertIn("run-old", controller.runs)
        now[0] = 61.0
        self.assertEqual(["run-old"], controller.prune_due())
        self.assertFalse((self.results / "run-old").exists())

    def test_nothing_is_due_without_a_window(self):
        write_run(self.results, "run-old", created_at=stamp(0), finished_at=stamp(1))
        controller = self.controller()
        self.assertEqual([], controller.prune_due())
        self.assertIn("run-old", controller.runs)


class RetentionCommandTests(HistoryFixture):
    """``ubertooth-harness controller --keep-runs DAYS``."""

    def test_the_option_is_offered_and_defaults_to_keeping_every_run(self):
        parser = _parser()
        args = parser.parse_args(["controller"])
        self.assertIsNone(args.keep_runs)
        self.assertEqual(30.0, parser.parse_args(
            ["controller", "--keep-runs", "30"]
        ).keep_runs)
        self.assertIn("--keep-runs", parser.format_help() + _controller_help(parser))

    def test_a_window_outside_zero_is_refused_before_anything_is_served(self):
        stderr = io.StringIO()
        with patch("socket.socket.bind", side_effect=AssertionError("bound")), \
                contextlib.redirect_stderr(stderr):
            result = main([
                "controller", "--port", "0", "--results-dir", str(self.results),
                "--keep-runs", "0",
            ])
        self.assertEqual(2, result)
        self.assertIn("more than 0 days", stderr.getvalue())


def _controller_help(parser) -> str:
    """The controller subcommand's own help text."""
    for action in parser._actions:  # the one subparsers action
        if hasattr(action, "choices") and action.choices:
            return action.choices["controller"].format_help()
    raise AssertionError("no subcommands")


class HistoryApiTests(ControllerTestCase):
    """The HTTP surface of the paged history."""

    async def test_the_endpoint_pages_filters_and_refuses(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner, run_id="run-1")
            await runner.send(
                "event", run_id="run-1", event=event("run-1", 1, "run_started")
            )
            await wait_until(lambda: self.controller.runs["run-1"].seen == {1})
        status, payload, _ = await self.http("GET", "/api/runs")
        self.assertEqual(200, status, payload)
        self.assertEqual(["run-1"], [run["run_id"] for run in payload["runs"]])
        self.assertEqual(50, payload["limit"])
        self.assertEqual(1, payload["total"])
        self.assertFalse(payload["has_more"])
        # One page of one, with a cursor that lists what is behind it.
        status, payload, _ = await self.http("GET", "/api/runs?limit=1")
        self.assertEqual(200, status, payload)
        self.assertFalse(payload["has_more"])
        created = payload["runs"][0]["created_at"]
        cursor = urlencode({"before": created, "before_run_id": "run-1"})
        status, payload, _ = await self.http("GET", f"/api/runs?{cursor}")
        self.assertEqual([], payload["runs"])
        # The filters.
        for query, expected in (
            ("target=Host_A", ["run-1"]),
            ("target=Host_B", []),
            ("status=running", ["run-1"]),
            ("status=passed", []),
            ("ref=main", ["run-1"]),
            ("ref=v1.2", []),
        ):
            with self.subTest(query):
                status, payload, _ = await self.http("GET", f"/api/runs?{query}")
                self.assertEqual(200, status, payload)
                self.assertEqual(
                    expected, [run["run_id"] for run in payload["runs"]]
                )
        for query in (
            "limit=0", f"limit={MAX_RUN_PAGE + 1}", "limit=many",
            "before=yesterday", "before_run_id=run-1", "status=PASSED",
            "target=not%20a%20host",
        ):
            with self.subTest(query):
                status, payload, _ = await self.http("GET", f"/api/runs?{query}")
                self.assertEqual(422, status, payload)
        self.assertIn(
            "GET /api/runs", (await self.http("GET", "/"))[1]["endpoints"]
        )

    async def test_a_restarted_controller_still_recognizes_a_replay(self):
        # The run is stored as finished, so its transcript is not read at
        # startup; a runner that reports its records again still has them
        # recognized, and the report is not replaced by a short one.
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner, run_id="run-1")
            records = [
                event("run-1", 1, "run_started"),
                event("run-1", 2, "test_started", name="Host version"),
            ]
            for record in records:
                await runner.send("event", run_id="run-1", event=record)
            await runner.send(
                "run_finished", request_id=message["request_id"], run_id="run-1",
                status="passed", exit_code=0, run_directory=str(self.root),
            )
            await wait_until(lambda: self.controller.runs["run-1"].status == "passed")
        report = (self.results / "run-1" / REPORT).read_text()
        await self.server.stop()
        await self.start_controller()
        self.assertFalse(self.controller.runs["run-1"].transcript_read)
        async with FakeRunner(self.port) as runner:
            await self.registered(runner, active="run-1")
            for record in records:
                await runner.send("event", run_id="run-1", event=record)
            await wait_until(
                lambda: self.controller.runs["run-1"].duplicate_events == 2
            )
        lines = (self.results / "run-1" / RUN_LOG).read_text().splitlines()
        self.assertEqual([1, 2], [json.loads(line)["sequence"] for line in lines])
        self.assertEqual(report, (self.results / "run-1" / REPORT).read_text())


if __name__ == "__main__":
    unittest.main()
