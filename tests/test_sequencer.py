import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from ubertooth_harness.catalog import TestCatalog, TestDefinition
from ubertooth_harness.events import RunEvent
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.manifest import SourceRevision, build_run_manifest
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.preflight import PreflightCheck
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import ATTEMPT_KEY, StateCase, Step


class CollectSink:
    def __init__(self):
        self.events: list[RunEvent] = []

    def emit(self, event):
        self.events.append(event)


class FakeExecutor:
    def __init__(self, fail_after=None):
        self.commands = []
        self.fail_after = fail_after

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        self.commands.append(tuple(argv))
        output = "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
        if self.fail_after is not None and len(self.commands) > self.fail_after:
            output = "wrong output"
        if on_output:
            on_output("stdout", output)
        return CommandResult(tuple(argv), 0, output, "", 0.01, False)


class IdentityExecutor(FakeExecutor):
    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if command == ("ubertooth-util", "-V"):
            return super().run(
                argv,
                timeout_s=timeout_s,
                cancellation=cancellation,
                on_output=on_output,
            )
        self.commands.append(command)
        option = command[-1]
        outputs = {
            "-v": "Firmware version: git-test (API:1.07)\n",
            # The whole -V answer, which is what -V prints wherever it
            # appears: it ends the command (fork f898fe5).
            "-V": (
                "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
                "libbtbb loaded from: /p/lib/libbtbb.so.1\n"
                "Firmware version: git-test (API:1.07)\n"
                "Firmware build: ubertooth git-test (builder@host) timestamp\n"
            ),
            "-s": "Serial No: abc123\n",
            "-p": "missing expected part output\n",
            "-b": "Board ID Number: 1 (Ubertooth One)\n",
            "-vspb": (
                "Firmware version: git-test (API:1.07)\n"
                "Serial No: abc123\n"
                "Part ID: 1234ABCD\n"
                "Board ID Number: 1 (Ubertooth One)\n"
            ),
        }
        output = outputs[option]
        if on_output:
            on_output("stdout", output)
        return CommandResult(command, 0, output, "", 0.01, False)


def run_contract(directory: Path, invocation):
    """Manifest and preflight for one invocation or a tuple of them."""
    invocations = invocation if isinstance(invocation, tuple) else (invocation,)
    invocation = invocations[0]
    plan = directory / "plan.yaml"
    hardware = directory / "hardware.yaml"
    plan.write_text("plan\n")
    hardware.write_text("hardware\n")
    source = SourceRevision(
        "Ubertooth",
        "/source",
        "https://example.invalid/ubertooth.git",
        "test-ref",
        "a" * 40,
        "test",
    )
    manifest = build_run_manifest(
        run_id="named-run",
        plan_name="named",
        suite="fast",
        host=invocation.host,
        plan_path=plan,
        hardware_path=hardware,
        sources=(source,),
        invocations=invocations,
        created_at="2026-10-22T00:00:00+00:00",
    )
    check = PreflightCheck(
        "preflight-001-host-version",
        "Host version",
        invocation.host,
        None,
        {},
        ("ubertooth-util", "-V"),
        (r"libubertooth\s+\S+\s+\(git\-test\)",),
        "git-test",
        # As PreflightPolicy builds it: -V reads every attached board after
        # the host line and exits 1 if one cannot be read.
        accepted_returncodes=(0, 1),
    )
    return manifest, (check,)


class SequencerTests(unittest.TestCase):

    def test_runs_catalog_owned_host_test_and_streams_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = FakeExecutor()
            sink = CollectSink()
            invocation = TestInvocation(
                "001-Host version",
                "Host version",
                "Host_Mac",
                {},
                {},
            )
            manifest, checks = run_contract(root, invocation)

            summary = NamedTestSequencer(executor, event_sink=sink).run(
                (invocation,),
                root,
                run_id="named-run",
                manifest=manifest,
                preflight_checks=checks,
            )

            self.assertTrue(summary.passed)
            self.assertEqual(
                [("ubertooth-util", "-V"), ("ubertooth-util", "-V")],
                executor.commands,
            )
            self.assertEqual(
                [
                    "run_started",
                    "manifest",
                    "host_capabilities",
                    "preflight_started",
                    "check_started",
                    "command_started",
                    "command_output",
                    "command_finished",
                    "output_checks",
                    "check_finished",
                    "preflight_finished",
                    "test_started",
                    "known_target",
                    "check_started",
                    "command_started",
                    "command_output",
                    "command_finished",
                    "output_checks",
                    "check_finished",
                    "test_finished",
                    "run_finished",
                ],
                [event.event_type for event in sink.events],
            )
            rows = [
                json.loads(line)
                for line in (root / "run.jsonl").read_text().splitlines()
            ]
            self.assertEqual("run_started", rows[0]["event_type"])
            self.assertEqual("run_finished", rows[-1]["event_type"])
            produced = sorted(
                p.name for p in root.iterdir()
                if p.name not in ("plan.yaml", "hardware.yaml")
            )
            self.assertEqual(["report.md", "run.jsonl"], produced)
            report = (root / "report.md").read_text()
            self.assertIn("=== TEST 001-Host version: Host version on Host_Mac ===", report)
            self.assertIn("[C] ubertooth-util -V", report)
            transcript = report.split("```text\n", 1)[1].split("```", 1)[0].splitlines()
            for line in transcript:
                self.assertRegex(
                    line, r"^\d\d:\d\d:\d\d\.\d{3} "
                )
            commands = [i for i, line in enumerate(transcript) if " [C] ubertooth-util -V" in line]
            self.assertEqual(2, len(commands))  # preflight and the test
            for index in commands:
                result = transcript[index + 1]
                self.assertIn(" exit 0 in ", result)
                self.assertIn("[O] Host tools version: libubertooth 1.1 (git-test)", result)

    def test_grouped_readback_reports_case_failure_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sink = CollectSink()
            executor = IdentityExecutor()
            invocation = TestInvocation(
                "001-Ubertooth identity",
                "Ubertooth identity",
                "Host_Linux",
                {"hosts": ("Host_Linux",), "Ubertooth": "default"},
                {"Ubertooth": "UT_Linux0"},
                {"Ubertooth": {"serial": "abc123"}},
            )
            manifest, checks = run_contract(root, invocation)

            summary = NamedTestSequencer(executor, event_sink=sink).run(
                (invocation,),
                root,
                run_id="named-run",
                manifest=manifest,
                preflight_checks=checks,
            )

            self.assertFalse(summary.passed)
            self.assertIn("Part ID", summary.failure_reason)
            test_checks = [
                event
                for event in sink.events
                if event.event_type == "check_finished"
                and event.details.get("phase") == "test"
            ]
            self.assertEqual(6, len(test_checks))
            self.assertEqual("failed", test_checks[3].status)
            self.assertEqual("passed", test_checks[-1].status)
            self.assertEqual("-vspb", executor.commands[-1][-1])

    def test_an_unanswered_preflight_check_asks_its_fallback(self):
        """A fork commit that answers the host version on the older option."""
        class OlderFork:
            def __init__(self):
                self.commands = []

            def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
                command = tuple(argv)
                self.commands.append(command)
                if command[-1] == "-V":
                    message = "error: unrecognized option '-V'\n"
                    if on_output:
                        on_output("stderr", message)
                    return CommandResult(command, 1, "", message, 0.01, False)
                output = ("ubertooth-util - usage\n" if command[-1] == "-h" else
                          "Host tools version: libubertooth 1.1 (git-test), "
                          "libbtbb 1.0 (git-btbb)\n")
                if on_output:
                    on_output("stdout", output)
                return CommandResult(command, 0, output, "", 0.01, False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = OlderFork()
            sink = CollectSink()
            # "Help smoke", so the test after preflight asks -h: the point
            # here is the preflight check, not the test.
            invocation = TestInvocation(
                "001-Help smoke", "Help smoke", "Host_Mac", {}, {}
            )
            manifest, (check,) = run_contract(root, invocation)
            check = replace(check, fallback_argv=("ubertooth-util", "-H"),
                            fallback_when_missing="Host tools version:")

            summary = NamedTestSequencer(executor, event_sink=sink).run(
                (invocation,), root, run_id="named-run", manifest=manifest,
                preflight_checks=(check,),
            )

            self.assertTrue(summary.passed, summary.failure_reason)
            self.assertEqual(
                [("ubertooth-util", "-V"), ("ubertooth-util", "-H")],
                executor.commands[:2],
            )
            commands = [e.details["command_id"] for e in sink.events
                        if e.event_type == "command_started"
                        and e.details.get("phase") == "preflight"]
            self.assertEqual(["preflight-001-host-version-command-1",
                              "preflight-001-host-version-command-2"], commands)

    def test_stops_on_catalog_result_validation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = FakeExecutor(fail_after=1)
            invocation = TestInvocation(
                "001-Host version", "Host version", "Host_Mac", {}, {}
            )
            manifest, checks = run_contract(root, invocation)

            summary = NamedTestSequencer(executor).run(
                (invocation,),
                root,
                run_id="named-run",
                manifest=manifest,
                preflight_checks=checks,
            )

            self.assertFalse(summary.passed)
            self.assertIn("expected output", summary.failure_reason)
            self.assertEqual(1, summary.completed_preflight_checks)


class AttemptExecutor:
    """Answers every command, but the check only from ``passing_attempt`` on.

    Stands for a capture whose outcome depends on what arrived over the air.
    """

    def __init__(self, passing_attempt):
        self.commands = []
        self.passing_attempt = passing_attempt
        self.checks = 0

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-V"):
            output = ("Host tools version: libubertooth 1.1 (git-test), "
                      "libbtbb 1.0 (git-btbb)\n")
        elif command[0] == "check":
            self.checks += 1
            output = ("capture ok\n" if self.checks >= self.passing_attempt
                      else "the capture holds nothing\n")
        else:
            output = "ok\n"
        if on_output:
            on_output("stdout", output)
        return CommandResult(command, 0, output, "", 0.01, False)


class RetriedStepTests(unittest.TestCase):
    """A case may take a capture again (StateCase.retry_steps)."""

    def case(self, *, attempts=3, retry_steps=("capture", "check-capture")):
        return StateCase(
            "retried", "Retried capture",
            (Step("prepare", "Start the peers", ("prepare",),
                  output_patterns=(r"ok",)),
             Step("capture", "Capture",
                  lambda context: ("capture", context[ATTEMPT_KEY]),
                  mutates=True),
             Step("check-capture", "The capture holds the connection",
                  ("check",), output_patterns=(r"capture ok",))),
            (Step("clean", "Leave the board as it was", ("clean",)),),
            retry_steps=retry_steps, attempts=attempts,
        )

    def run_case(self, case, executor):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = TestCatalog([
                TestDefinition(
                    "Custom", "custom", MappingProxyType({}),
                    MappingProxyType({}),
                    command_builder=lambda *_args: (case,),
                )
            ])
            invocation = TestInvocation("001-Custom", "Custom", "Host_Linux",
                                        {}, {})
            manifest, checks = run_contract(root, invocation)
            sink = CollectSink()
            summary = NamedTestSequencer(
                executor, catalog=catalog, event_sink=sink,
            ).run((invocation,), root, run_id="named-run", manifest=manifest,
                  preflight_checks=checks)
            return summary, sink.events, (root / "report.md").read_text()

    @staticmethod
    def finished(events, event_type, stage="transition"):
        return [
            event for event in events
            if event.event_type == event_type
            and event.details.get("stage", stage) == stage
        ]

    def test_a_second_capture_passes_the_case(self):
        executor = AttemptExecutor(passing_attempt=2)
        summary, events, report = self.run_case(self.case(), executor)

        self.assertTrue(summary.passed, summary.failure_reason)
        # The peers were started once; the capture and its check ran twice,
        # each attempt writing its own capture.
        self.assertEqual(
            [("prepare",), ("capture", "1"), ("check",), ("capture", "2"),
             ("check",), ("clean",)],
            [command for command in executor.commands
             if command != ("ubertooth-util", "-V")])
        checks = [event.status for event in self.finished(events, "step_finished")
                  if event.details["step_id"] == "check-capture"]
        self.assertEqual(["failed", "passed"], checks)
        case_finished = self.finished(events, "check_finished", stage="transition")
        self.assertEqual(2, case_finished[-1].details["attempts"])
        self.assertIn("(attempt 2)", report)
        self.assertIn("EXIT: passed in 2 attempts", report)

    def test_every_attempt_keeps_its_own_saved_output(self):
        executor = AttemptExecutor(passing_attempt=2)
        _summary, events, _report = self.run_case(self.case(), executor)

        ids = [event.details.get("command_id")
               for event in events if event.event_type == "command_finished"]
        self.assertIn("001-Custom-retried-check-capture", ids)
        self.assertIn("001-Custom-retried-check-capture-try2", ids)

    def test_a_case_that_never_catches_it_fails_after_every_attempt(self):
        executor = AttemptExecutor(passing_attempt=99)
        summary, events, _report = self.run_case(self.case(attempts=2), executor)

        self.assertFalse(summary.passed)
        self.assertIn("The capture holds the connection", summary.failure_reason)
        self.assertEqual(
            2, len([c for c in executor.commands if c[0] == "capture"]))
        case_finished = self.finished(events, "check_finished", stage="transition")
        self.assertEqual(2, case_finished[-1].details["attempts"])

    def test_a_failure_before_the_last_step_of_the_group_is_not_retried(self):
        # The capture is in the group, but a mode that would not start is a
        # tool or board failure: running it again cannot help, and a later
        # attempt passing would hide it.
        executor = AttemptExecutor(passing_attempt=1)
        case = self.case()
        case = replace(
            case,
            steps=case.steps[:1]
            + (replace(case.steps[1], output_patterns=(r"never printed",)),)
            + case.steps[2:],
        )
        summary, _events, _report = self.run_case(case, executor)

        self.assertFalse(summary.passed)
        self.assertIn("Capture", summary.failure_reason)
        self.assertEqual(
            1, len([c for c in executor.commands if c[0] == "capture"]))

    def test_a_failure_outside_the_retried_steps_is_not_retried(self):
        executor = AttemptExecutor(passing_attempt=1)
        case = self.case(retry_steps=("check-capture",), attempts=3)
        # The first step's pattern is the one this executor never prints.
        case = replace(
            case,
            steps=(replace(case.steps[0], output_patterns=(r"never printed",)),)
            + case.steps[1:],
        )
        summary, _events, _report = self.run_case(case, executor)

        self.assertFalse(summary.passed)
        self.assertEqual(
            [("prepare",), ("clean",)],
            [command for command in executor.commands
             if command != ("ubertooth-util", "-V")])


if __name__ == "__main__":
    unittest.main()
