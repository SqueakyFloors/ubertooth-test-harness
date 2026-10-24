import hashlib
import json
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.events import RunEvent
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.flash import (
    FirmwareArtifact,
    FlashReliabilityRunner,
    FlashRunRequest,
    HarnessError,
)
from ubertooth_harness.events import open_run_publisher
from ubertooth_harness.plan import load_plan
from ubertooth_harness.report import load_events, write_report

TIMESTAMP = re.compile(r"^\d\d:\d\d:\d\d\.\d{3} ")

# Reliability expectations (flash count, image order, allowances) live in the plan file
SLOW_PLAN = load_plan(Path(__file__).resolve().parents[1] / "testplans" / "slow.yaml")


class CollectEventSink:
    def __init__(self):
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)


class CancellingEventSink(CollectEventSink):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def emit(self, event: RunEvent) -> None:
        super().emit(event)
        if event.event_type == "flash_started":
            self.token.cancel("UI canceled before write")


class FailingEventSink:
    def emit(self, event: RunEvent) -> None:
        raise RuntimeError("display disconnected")


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeExecutor:
    def __init__(self, clock, image_revisions, current_revision="git-kgv"):
        self.clock = clock
        self.image_revisions = image_revisions
        self.current_revision = current_revision
        self.guard_revisions = {}
        self.commands = []
        self.flash_count = 0
        self.fail_flash_at = None
        self.mismatch_at = None
        self.change_guard_at = None
        self.mutate_artifact_at = None
        self.mutate_artifact_path = None
        self.cancel_token = None
        self.cancel_after_flash = None
        self.cancel_on_failed_flash = False

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        self.commands.append((tuple(argv), timeout_s))
        self.clock.advance(0.1)
        if argv[0] == "ubertooth-dfu":
            self.flash_count += 1
            if self.fail_flash_at == self.flash_count:
                if self.cancel_on_failed_flash and self.cancel_token is not None:
                    self.cancel_token.cancel("operator requested")
                if on_output is not None:
                    on_output("stderr", "flash failed")
                return CommandResult(tuple(argv), 7, "", "flash failed", 0.1, False)
            self.current_revision = self.image_revisions[argv[argv.index("-d") + 1]]
            if self.mismatch_at == self.flash_count:
                self.current_revision = "git-wrong"
            if self.change_guard_at == self.flash_count and self.guard_revisions:
                serial = next(iter(self.guard_revisions))
                self.guard_revisions[serial] = "git-changed"
            if self.mutate_artifact_at == self.flash_count:
                Path(self.mutate_artifact_path).write_bytes(b"changed-during-run")
            if on_output is not None:
                on_output("stdout", "Verified firmware revision\n")
            return CommandResult(
                tuple(argv), 0, "Verified firmware revision\n", "", 0.1, False
            )
        if argv[0] == "ubertooth-util":
            serial = argv[argv.index("-U") + 1]
            revision = (
                self.current_revision
                if serial == "selected"
                else self.guard_revisions[serial]
            )
            if (
                serial == "selected"
                and self.flash_count == self.cancel_after_flash
                and self.cancel_token is not None
            ):
                self.cancel_token.cancel("operator requested")
                self.cancel_after_flash = None
            stdout = f"Firmware version: {revision} (API:1.07)\n"
            if on_output is not None:
                on_output("stdout", stdout)
            return CommandResult(
                tuple(argv),
                0,
                stdout,
                "",
                0.1,
                False,
            )
        raise AssertionError(f"unexpected command: {argv}")


def run_flash(executor, clock, request, directory, *, cancellation=None, sink=None):
    """Run the flash sequence into a run.jsonl and report.md, as run-suite does."""
    results = Path(directory) / "results"
    publisher = open_run_publisher(results, "test-run", sink)
    summary = FlashReliabilityRunner(executor, clock=clock).run(
        request, publisher, cancellation=cancellation
    )
    write_report(results)
    return summary


def run_events(directory):
    return load_events(Path(directory) / "results" / "run.jsonl")


class FlashRunnerTests(unittest.TestCase):

    def make_step(self, kgv_sha256, flashes=None, base=None):
        # Defaults come from the slow plan, so the plan file is the single source
        # for flash count, image order, and failure allowances.
        base = base or SLOW_PLAN.flash_steps[0]
        flashes = base.flashes if flashes is None else flashes
        return replace(
            base,
            flashes=flashes,
            kgv_sha256=kgv_sha256,
            required_verified_flashes=flashes,
        )

    def prepare_request(self, directory, flashes=None, guards=(), base=None):
        root = Path(directory)
        test_image = root / "test.dfu"
        kgv_image = root / "kgv.dfu"
        test_image.write_bytes(b"test-image")
        kgv_image.write_bytes(b"kgv-image")
        kgv_hash = hashlib.sha256(kgv_image.read_bytes()).hexdigest()
        step = self.make_step(kgv_hash, flashes, base)
        artifacts = {
            "test_build": FirmwareArtifact(
                "test_build", test_image, "git-test", None
            ),
            "kgv": FirmwareArtifact("kgv", kgv_image, "git-kgv", kgv_hash),
        }
        request = FlashRunRequest(
            step=step,
            serial="selected",
            artifacts=artifacts,
            per_flash_timeout_s=25,
            # Fake clock advances 0.1s per command (2 per flash); scale with plan size
            total_timeout_s=max(120, step.flashes),
            guard_serials=tuple(guards),
        )
        return request, artifacts

    def test_runs_plan_flash_count_alternating_exact_verified_flashes(self):
        for base in SLOW_PLAN.flash_steps:
            with self.subTest(step=base.id):
                self._assert_full_plan_run(base)

    def _assert_full_plan_run(self, base):
        flashes = base.flashes
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, base=base)
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            sink = CollectEventSink()
            summary = run_flash(executor, clock, request, directory, sink=sink)

            sequence = base.image_sequence
            expected_images = [sequence[i % len(sequence)] for i in range(flashes)]
            self.assertTrue(summary.passed)
            self.assertEqual("passed", summary.status)
            self.assertEqual(flashes, summary.attempted_flashes)
            self.assertEqual(flashes, summary.verified_flashes)
            self.assertEqual(
                artifacts[expected_images[-1]].expected_revision, summary.final_revision
            )
            flash_commands = [
                command
                for command, _timeout in executor.commands
                if command[0] == "ubertooth-dfu"
            ]
            self.assertEqual(flashes, len(flash_commands))
            revisions = [
                executor.image_revisions[command[command.index("-d") + 1]]
                for command in flash_commands
            ]
            self.assertEqual(
                [artifacts[name].expected_revision for name in expected_images], revisions
            )
            events = [
                json.loads(line)
                for line in (Path(directory) / "results" / "run.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(sink.events), len(events))
            self.assertEqual("flash_run_started", events[0]["event_type"])
            self.assertEqual("flash_run_finished", events[-1]["event_type"])
            self.assertEqual("passed", events[-1]["status"])
            self.assertEqual(
                list(range(1, len(events) + 1)), [event["sequence"] for event in events]
            )
            self.assertTrue(all(event["schema_version"] == 1 for event in events))
            # Every executed command emits exactly one started/finished pair
            for event_type in ("command_started", "command_finished"):
                self.assertEqual(
                    len(executor.commands),
                    sum(event["event_type"] == event_type for event in events),
                )
            self.assertEqual(
                ["report.md", "run.jsonl"],
                sorted(p.name for p in (Path(directory) / "results").iterdir()),
            )

    @staticmethod
    def transcript(directory):
        report = (Path(directory) / "results" / "report.md").read_text()
        return report.split("```text\n", 1)[1].split("```", 1)[0].splitlines()

    def test_report_records_commands_sent_and_responses_received(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, guards=("guard",))
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            executor.guard_revisions["guard"] = "git-guard"

            summary = run_flash(executor, clock, request, directory)

            self.assertTrue(summary.passed)
            lines = self.transcript(directory)
            for line in lines:
                self.assertRegex(line, TIMESTAMP)
            self.assertIn(" [C] ubertooth-util -U selected -v", lines[1])
            self.assertIn(" [C] ubertooth-util -U guard -v", lines[3])
            self.assertIn("[O] Firmware version: git-guard (API:1.07)", lines[4])
            headers = [i for i, line in enumerate(lines) if re.search(r"     \+\+\+ flash \d+: ", line)]
            self.assertEqual(summary.attempted_flashes, len(headers))
            for index in headers:
                block = lines[index:index + 8]
                # Flash, read back, and unselected-device check: two lines each.
                self.assertIn(" [C] ubertooth-dfu -U selected -d ", block[1])
                self.assertIn(" exit 0 in ", block[2])
                self.assertIn("[O] Verified firmware revision", block[2])
                self.assertIn(" [C] ubertooth-util -U selected -v", block[3])
                self.assertIn("[O] Firmware version: ", block[4])
                self.assertIn(" [C] ubertooth-util -U guard -v", block[5])
                self.assertIn("[O] Firmware version: git-guard (API:1.07)", block[6])
                self.assertIn("EXIT: passed, read back", block[7])

    def test_failed_flash_shows_error_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=5)
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            executor.fail_flash_at = 3

            run_flash(executor, clock, request, directory)

            lines = self.transcript(directory)
            header = next(i for i, line in enumerate(lines) if "     +++ flash 3: " in line)
            self.assertIn(" [C] ubertooth-dfu -U selected -d ", lines[header + 1])
            self.assertIn(" exit 7 in ", lines[header + 2])
            self.assertIn("[E] flash failed", lines[header + 2])

    def test_stops_on_first_flash_command_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=5)
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            executor.fail_flash_at = 3

            summary = run_flash(executor, clock, request, directory)

            self.assertFalse(summary.passed)
            self.assertEqual(3, summary.attempted_flashes)
            self.assertEqual(2, summary.verified_flashes)
            self.assertIn("flash command", summary.failure_reason)
            events = [
                json.loads(line)
                for line in (Path(directory) / "results" / "run.jsonl").read_text().splitlines()
            ]
            self.assertEqual("failed", events[-1]["status"])
            self.assertEqual("flash_run_finished", events[-1]["event_type"])
            self.assertEqual(
                3,
                sum(event["event_type"] == "flash_finished" for event in events),
            )

    def test_stops_on_revision_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=4)
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            executor.mismatch_at = 2

            summary = run_flash(executor, clock, request, directory)

            self.assertFalse(summary.passed)
            self.assertEqual(2, summary.attempted_flashes)
            self.assertEqual(1, summary.verified_flashes)
            self.assertIn("revision mismatch", summary.failure_reason)

    def test_rejects_kgv_hash_mismatch_before_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=2)
            bad = FirmwareArtifact("kgv", artifacts["kgv"].path, "git-kgv", "0" * 64)
            request = FlashRunRequest(
                step=request.step,
                serial=request.serial,
                artifacts={**artifacts, "kgv": bad},
                per_flash_timeout_s=25,
                total_timeout_s=120,
            )
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )

            with self.assertRaisesRegex(HarnessError, "sha256"):
                run_flash(executor, clock, request, directory)
            self.assertEqual([], executor.commands)

    def test_stops_if_artifact_changes_during_run(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=4)
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            executor.mutate_artifact_at = 1
            executor.mutate_artifact_path = artifacts["kgv"].path

            summary = run_flash(executor, clock, request, directory)

            self.assertFalse(summary.passed)
            self.assertEqual(1, summary.attempted_flashes)
            self.assertEqual(1, summary.verified_flashes)
            self.assertIn("artifact changed", summary.failure_reason)

    def test_cancellation_before_first_flash_runs_no_state_changing_command(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=4)
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            token = CancellationToken()
            token.cancel("operator requested")
            sink = CollectEventSink()

            summary = run_flash(executor, clock, request, directory, cancellation=token, sink=sink)

            self.assertEqual("canceled", summary.status)
            self.assertFalse(summary.passed)
            self.assertEqual(0, summary.attempted_flashes)
            self.assertFalse(
                any(command[0][0] == "ubertooth-dfu" for command in executor.commands)
            )
            self.assertEqual(["flash_run_started", "flash_run_finished"], [
                event.event_type for event in sink.events
            ])
            self.assertEqual("canceled", sink.events[-1].status)

    def test_cancellation_between_flashes_stops_before_next_flash(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=4)
            clock = FakeClock()
            token = CancellationToken()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            executor.cancel_token = token
            executor.cancel_after_flash = 1

            summary = run_flash(executor, clock, request, directory, cancellation=token)

            self.assertEqual("canceled", summary.status)
            self.assertEqual(1, summary.attempted_flashes)
            self.assertEqual(1, summary.verified_flashes)
            self.assertEqual(1, executor.flash_count)

    def test_live_sink_failure_does_not_interrupt_flash_run(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=2)
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )

            summary = run_flash(executor, clock, request, directory, sink=FailingEventSink())

            self.assertTrue(summary.passed)
            self.assertEqual(2, summary.verified_flashes)
            events = [
                json.loads(line)
                for line in (Path(directory) / "results" / "run.jsonl").read_text().splitlines()
            ]
            self.assertEqual("flash_run_finished", events[-1]["event_type"])
            self.assertEqual("passed", events[-1]["status"])

    def test_sink_cancellation_at_flash_start_prevents_the_write(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=2)
            clock = FakeClock()
            token = CancellationToken()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            sink = CancellingEventSink(token)

            summary = run_flash(executor, clock, request, directory, cancellation=token, sink=sink)

            self.assertEqual("canceled", summary.status)
            self.assertEqual(0, summary.attempted_flashes)
            self.assertEqual(
                [
                    "flash_run_started",
                    "command_started",
                    "command_output",
                    "command_finished",
                    "flash_started",
                    "flash_finished",
                    "flash_run_finished",
                ],
                [event.event_type for event in sink.events],
            )
            flash_finished = next(
                event for event in sink.events
                if event.event_type == "flash_finished"
            )
            self.assertEqual("canceled", flash_finished.status)
            self.assertEqual(0, executor.flash_count)

    def test_cancellation_after_final_verified_flash_remains_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=1)
            clock = FakeClock()
            token = CancellationToken()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            executor.cancel_token = token
            executor.cancel_after_flash = 1

            summary = run_flash(executor, clock, request, directory, cancellation=token)

            self.assertEqual("passed", summary.status)
            self.assertTrue(summary.passed)
            self.assertEqual(1, summary.verified_flashes)
            self.assertTrue(token.is_cancelled())

    def test_command_failure_wins_over_concurrent_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=4)
            clock = FakeClock()
            token = CancellationToken()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            executor.cancel_token = token
            executor.cancel_on_failed_flash = True
            executor.fail_flash_at = 1

            summary = run_flash(executor, clock, request, directory, cancellation=token)

            self.assertEqual("failed", summary.status)
            self.assertIn("flash command", summary.failure_reason)
            self.assertTrue(token.is_cancelled())

    def test_fails_if_unselected_device_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(
                directory, flashes=4, guards=("guard",)
            )
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )
            executor.guard_revisions["guard"] = "git-guard"
            executor.change_guard_at = 2

            summary = run_flash(executor, clock, request, directory)

            self.assertFalse(summary.passed)
            self.assertEqual(2, summary.attempted_flashes)
            self.assertIn("unselected device", summary.failure_reason)

    def test_total_deadline_stops_before_next_flash(self):
        with tempfile.TemporaryDirectory() as directory:
            request, artifacts = self.prepare_request(directory, flashes=5)
            request = FlashRunRequest(
                step=request.step,
                serial=request.serial,
                artifacts=request.artifacts,
                per_flash_timeout_s=25,
                total_timeout_s=0.25,
            )
            clock = FakeClock()
            executor = FakeExecutor(
                clock,
                {str(a.path): a.expected_revision for a in artifacts.values()},
            )

            summary = run_flash(executor, clock, request, directory)

            self.assertFalse(summary.passed)
            self.assertLess(summary.attempted_flashes, 5)
            self.assertIn("total deadline", summary.failure_reason)


class BoardsExecutor:
    """Several boards by serial; ubertooth-dfu writes the board named by -U."""

    def __init__(self, clock, image_revisions, revisions):
        self.clock = clock
        self.image_revisions = image_revisions
        self.revisions = dict(revisions)
        self.commands = []
        self.hooks = []  # callables (argv, flashes_so_far) -> CommandResult | None

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        argv = tuple(argv)
        self.commands.append(argv)
        self.clock.advance(0.1)
        flashes = sum(1 for c in self.commands if c[0] == "ubertooth-dfu")
        for hook in self.hooks:
            result = hook(argv, flashes)
            if result is not None:
                return result
        serial = argv[argv.index("-U") + 1]
        if argv[0] == "ubertooth-dfu":
            self.revisions[serial] = self.image_revisions[argv[argv.index("-d") + 1]]
            stdout = "Verified firmware revision\n"
        else:
            stdout = f"Firmware version: {self.revisions[serial]} (API:1.07)\n"
        for hook in getattr(self, "after", []):
            hook(argv, flashes)
        if on_output is not None:
            on_output("stdout", stdout)
        return CommandResult(argv, 0, stdout, "", 0.1, False)


class AlternatingFlashTests(unittest.TestCase):
    """Flashes rotate across several boards (A, B, A, B, ...)."""

    make_step = FlashRunnerTests.make_step
    prepare_request = FlashRunnerTests.prepare_request

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = self.tmp.name

    def build(self, flashes=6, *, boards=("second",), guards=(), start=None):
        request, artifacts = self.prepare_request(self.directory, flashes=flashes, guards=guards)
        request = replace(request, rotate_serials=tuple(boards))
        self.artifacts = artifacts
        self.clock = FakeClock()
        revisions = {serial: "git-kgv" for serial in ("selected", *boards)}
        revisions.update({serial: "git-guard" for serial in guards})
        revisions.update(start or {})
        self.executor = BoardsExecutor(
            self.clock, {str(a.path): a.expected_revision for a in artifacts.values()},
            revisions,
        )
        self.executor.after = []
        return request

    def run_request(self, request, cancellation=None):
        sink = CollectEventSink()
        summary = run_flash(self.executor, self.clock, request, self.directory,
                            cancellation=cancellation, sink=sink)
        return summary, sink.events

    def flashes(self):
        return [(c[c.index("-U") + 1], c[c.index("-d") + 1])
                for c in self.executor.commands if c[0] == "ubertooth-dfu"]

    def test_flashes_rotate_across_boards_and_each_alternates_images(self):
        summary, events = self.run_request(self.build())

        self.assertTrue(summary.passed, summary.failure_reason)
        test = str(self.artifacts["test_build"].path)
        kgv = str(self.artifacts["kgv"].path)
        self.assertEqual(
            [("selected", test), ("second", test), ("selected", kgv),
             ("second", kgv), ("selected", test), ("second", test)],
            self.flashes(),
        )
        self.assertEqual(("selected", "second"), summary.serials)
        self.assertEqual({"selected": 3, "second": 3}, summary.verified_flashes_by_serial)
        self.assertEqual({"selected": "git-test", "second": "git-test"}, summary.final_revisions)
        started = [e.details["serial"] for e in events if e.event_type == "flash_started"]
        self.assertEqual(["selected", "second"] * 3, started)
        checks = [e for e in events if e.event_type == "command_started"
                  and e.details["operation"] == "selected_version_unchanged_after_flash"]
        self.assertEqual(6, len(checks))
        report = (Path(self.directory) / "results" / "report.md").read_text()
        self.assertIn("6 flashes alternating across selected, second", report)
        self.assertIn("+++ flash 2: test_build to second", report)
        self.assertIn("6/6 verified (selected: 3, second: 3)", report)

    def test_each_board_starts_with_an_image_it_is_not_running(self):
        summary, _ = self.run_request(self.build(4, start={"second": "git-test"}))

        self.assertTrue(summary.passed, summary.failure_reason)
        test = str(self.artifacts["test_build"].path)
        kgv = str(self.artifacts["kgv"].path)
        self.assertEqual([test, kgv, kgv, test], [image for _, image in self.flashes()])

    def test_three_boards_rotate_in_order(self):
        summary, _ = self.run_request(self.build(6, boards=("second", "third")))

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(["selected", "second", "third"] * 2,
                         [serial for serial, _ in self.flashes()])

    def test_uneven_flash_count_is_refused(self):
        request = self.build(100, boards=("second", "third"))
        with self.assertRaisesRegex(HarnessError, "cannot alternate evenly across 3"):
            FlashReliabilityRunner(self.executor).validate(request)

    def test_change_on_a_board_not_being_flashed_fails_the_run(self):
        request = self.build()

        def change_first(argv, flashes):
            if argv[0] == "ubertooth-dfu" and flashes == 2:
                self.executor.revisions["selected"] = "git-changed"

        self.executor.after.append(change_first)
        summary, _ = self.run_request(request)

        self.assertFalse(summary.passed)
        self.assertIn("alternated board selected changed or became unavailable after "
                      "flash 2 (expected git-test, got git-changed)", summary.failure_reason)
        self.assertEqual("git-changed", summary.final_revisions["selected"])
        self.assertEqual(1, summary.verified_flashes)

    def test_failed_write_names_the_board_and_marks_it_unknown(self):
        request = self.build()
        self.executor.hooks.append(
            lambda argv, n: CommandResult(argv, 7, "", "DFU error", 0.1, False)
            if argv[0] == "ubertooth-dfu" and n == 2 else None
        )
        summary, _ = self.run_request(request)

        self.assertIn("flash command failed at iteration 2 on second", summary.failure_reason)
        self.assertEqual({"selected": "git-test", "second": ""}, summary.final_revisions)
        self.assertEqual({"selected": 1, "second": 1}, summary.attempted_flashes_by_serial)

    def test_single_board_matching_every_image_fails_with_a_summary(self):
        request = self.build(2, boards=())
        # Both images claim the board's current revision: no observable write.
        request = replace(request, artifacts={
            label: replace(artifact, expected_revision="git-kgv")
            for label, artifact in request.artifacts.items()
        })
        runner = FlashReliabilityRunner(self.executor, clock=self.clock)
        runner._validate_request = lambda _request: {}  # skip distinct-revision check
        from ubertooth_harness.events import EventPublisher

        sink = CollectEventSink()
        summary = runner.run(request, EventPublisher("r", sink))

        self.assertEqual("failed", summary.status)
        self.assertIn("current revision of selected matches every image", summary.failure_reason)
        self.assertEqual([], self.flashes())
        self.assertEqual("flash_run_finished", sink.events[-1].event_type)

    def test_report_marks_only_written_boards_as_written(self):
        request = self.build()
        self.executor.hooks.append(
            lambda argv, n: CommandResult(argv, 7, "", "DFU error", 0.1, False)
            if argv[0] == "ubertooth-dfu" and n == 1 else None
        )
        summary, events = self.run_request(request)
        from ubertooth_harness.report import _Run, _setup_rows

        records = []
        for index, (alias, serial) in enumerate((("UT_A", "selected"), ("UT_B", "second"))):
            check = {"phase": "preflight", "check_id": f"p{index}", "check_name": "Device serial"}
            records.append({"run_id": "r", "event_type": "check_started", "status": "running",
                            "timestamp": "2026-01-01T00:00:00+00:00",
                            "details": {**check, "resource": alias,
                                        "resource_identity": {"serial": serial}}})
            records.append({"run_id": "r", "event_type": "check_finished", "status": "passed",
                            "timestamp": "2026-01-01T00:00:00+00:00", "details": check})
        records += [
            {"run_id": "r", "event_type": e.event_type, "status": e.status,
             "timestamp": "2026-01-01T00:00:01+00:00", "details": dict(e.details)}
            for e in events if e.event_type == "flash_run_finished"
        ]
        rows = dict(_setup_rows(_Run(records)))
        self.assertIn("UT_A serial selected: firmware written by this run; final unknown",
                      rows["Devices"])
        self.assertIn("UT_B serial second: firmware not written by this run; read git-kgv",
                      rows["Devices"])

    def test_readback_mismatch_on_a_rotated_board_names_it(self):
        request = self.build()

        def wrong(argv, flashes):
            if argv[:3] == ("ubertooth-dfu", "-U", "second"):
                self.executor.revisions["second"] = "git-weird"

        self.executor.after.append(wrong)
        summary, _ = self.run_request(request)

        self.assertIn("revision mismatch after flash 2 on second: expected git-test, "
                      "got git-weird", summary.failure_reason)
        self.assertEqual("git-weird", summary.final_revisions["second"])

    def test_baseline_failure_on_a_rotated_board_stops_before_writing(self):
        request = self.build()
        self.executor.hooks.append(
            lambda argv, n: CommandResult(argv, 1, "", "no Ubertooth found", 0.1, False)
            if argv[:3] == ("ubertooth-util", "-U", "second") else None
        )
        summary, _ = self.run_request(request)

        self.assertIn("unable to query alternated board second", summary.failure_reason)
        self.assertEqual([], self.flashes())

    def test_cancellation_mid_rotation_stops_after_a_verified_flash(self):
        request = self.build()
        token = CancellationToken()

        def cancel(argv, flashes):
            if argv[0] == "ubertooth-dfu" and flashes == 3:
                token.cancel("operator")

        self.executor.after.append(cancel)
        summary, _ = self.run_request(request, cancellation=token)

        self.assertEqual("canceled", summary.status)
        self.assertEqual({"selected": 2, "second": 1}, summary.verified_flashes_by_serial)
        self.assertEqual(3, len(self.flashes()))

    def test_guards_are_still_checked_while_rotating(self):
        request = self.build(2, guards=("guard",))

        def change_guard(argv, flashes):
            if argv[0] == "ubertooth-dfu" and flashes == 2:
                self.executor.revisions["guard"] = "git-changed"

        self.executor.after.append(change_guard)
        summary, _ = self.run_request(request)

        self.assertIn("unselected device guard changed or became unavailable after "
                      "flash 2 (expected git-guard, got git-changed)", summary.failure_reason)

    def test_index_and_suffix_selectors_are_refused(self):
        request = self.build(2, guards=("guard",))
        runner = FlashReliabilityRunner(self.executor)
        for name, bad in (
            ("index", replace(request, rotate_serials=("1",))),
            ("suffix", replace(request, rotate_serials=("lected",))),
            ("guard suffix", replace(request, guard_serials=("ected",))),
            ("duplicate", replace(request, rotate_serials=("selected",))),
            ("guard overlap", replace(request, rotate_serials=("guard",))),
        ):
            with self.subTest(name), self.assertRaises(HarnessError):
                runner.validate(bad)


if __name__ == "__main__":
    unittest.main()
