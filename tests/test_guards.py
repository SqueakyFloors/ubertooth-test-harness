"""Guard Ubertooths: other registered boards stay unchanged during transition tests."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ubertooth_harness import cli, transitions
from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.plan import load_plan
from ubertooth_harness.cases import guard_roles
from ubertooth_harness.planner import PlanCompiler, TestInvocation
from ubertooth_harness.preflight import PreflightPolicy
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import StateCaseError
from tests.test_sequencer import CollectSink, run_contract
from tests.test_transitions import (
    HOST_API,
    FakeUbertooth,
    HostVersionPassthrough,
    patch_host_api,
)

A, B, C = "a" * 32, "b" * 32, "c" * 32
KGV = "e" * 64
GUARD_QUERIES = {option for _key, option, _pattern, _convert in transitions.GUARD_READBACK}
GUARD_KEYS = tuple(key for key, *_rest in transitions.GUARD_READBACK) + (
    transitions.GUARD_CRC_KEY,
)
READS_PER_GUARD = len(GUARD_KEYS)
GUARDED_TESTS = (
    "Ubertooth util compositions",
    "Radio setting transitions",
    "BLE CRC transitions",
    "BLE mode conflicts",
    "Ubertooth util conflicts",
    "Ubertooth util modes",
    "Multi-board selection",
)


def setUpModule():
    patch_host_api()


def hardware(serials, host="Host_Linux"):
    boards = "".join(
        f"""  UT_{index}:
    type: Ubertooth
    attached to: {host}
    identity: {{serial: {serial}}}
"""
        for index, serial in enumerate(serials)
    )
    return f"""resources:
  {host}:
    type: Host
    platform: linux
    roles: [test target]
    defaults: {{Ubertooth: UT_0}}
{boards}"""


def compile_plan(plan_text, serials, suite="fast"):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "hardware.yaml").write_text(hardware(serials))
        (root / "plan.yaml").write_text(plan_text)
        registry = FileResourceRegistry.load(root / "hardware.yaml")
        invocations = PlanCompiler(builtin_catalog(), registry).compile(
            load_plan(root / "plan.yaml"), suite
        )
        return invocations, registry


def step_plan(test_name, ubertooth="default", extra=""):
    return f"""plan: guards
suites:
  fast:
    - {test_name}:
        hosts: each
        Ubertooth: {ubertooth}
{extra}"""


class Bench:
    """Several fake boards on one host, routed by the last -U selector.

    ``leak`` simulates a host-tool bug: it runs after every command and may
    change any board's state.
    """

    def __init__(self, *boards, leak=None):
        self.boards = {board.serial: board for board in boards}
        self.leak = leak
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        selections = [command[i + 1] for i, item in enumerate(command) if item == "-U"]
        board = self.boards[selections[-1]]
        result = board.run(
            argv, timeout_s=timeout_s, cancellation=cancellation, on_output=on_output
        )
        if self.leak is not None:
            self.leak(command, self.boards)
        return result

    def commands_for(self, serial):
        return [c for c in self.commands if "-U" in c and c[c.index("-U") + 1] == serial]


def guarded_invocation(test_name, coverage="smoke", guards=(B,)):
    definition = builtin_catalog().get(test_name)
    raw = {"hosts": ["Host_Linux"], "Ubertooth": "default"}
    if "coverage" in definition.arguments:
        raw["coverage"] = coverage
    if "allow RF transmission" in definition.arguments:
        raw["allow RF transmission"] = True
    resources = {"Ubertooth": "UT_0"}
    identities = {"Ubertooth": {"serial": A}}
    for index, serial in enumerate(guards, 1):
        resources[f"guard Ubertooth {index}"] = f"UT_{index}"
        identities[f"guard Ubertooth {index}"] = {"serial": serial}
    return TestInvocation(
        f"001-{test_name}",
        test_name,
        "Host_Linux",
        definition.normalize_arguments(raw),
        resources,
        identities,
    )


def run_guarded(invocation, bench, cancellation=None):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(
            HostVersionPassthrough(bench), event_sink=sink
        ).run(
            (invocation,),
            root,
            run_id="named-run",
            manifest=manifest,
            preflight_checks=checks,
            cancellation=cancellation,
        )
        report = (root / "report.md").read_text()
        return summary, sink.events, report


def guard_checks(events):
    started = {
        e.details["case_id"] for e in events
        if e.event_type == "check_started" and e.details.get("guard")
    }
    return [
        e for e in events
        if e.event_type == "check_finished" and e.details.get("case_id") in started
    ]


def state_cases(events):
    return [
        e for e in events
        if e.event_type == "check_finished" and e.details.get("phase") == "test"
        and "-guard-" not in e.details["case_id"]
    ]


class GuardPlanningTests(unittest.TestCase):

    def test_transition_tests_bind_every_other_board_as_a_guard(self):
        for name in GUARDED_TESTS:
            extra = ""
            if name not in ("BLE mode conflicts", "Ubertooth util conflicts"):
                extra = "        coverage: smoke\n"
            if name == "Ubertooth util conflicts":
                extra = "        allow RF transmission: true\n"
            (invocation,), _ = compile_plan(step_plan(name, extra=extra), (A, B, C))
            with self.subTest(name):
                self.assertEqual(
                    ("guard Ubertooth 1", "guard Ubertooth 2"),
                    guard_roles(invocation.resources),
                )
                self.assertEqual("UT_1", invocation.resources["guard Ubertooth 1"])
                self.assertEqual(
                    C, invocation.resource_identities["guard Ubertooth 2"]["serial"]
                )

    def test_single_board_host_and_unguarded_tests_bind_no_guard(self):
        (single,), _ = compile_plan(
            step_plan("Radio setting transitions", extra="        coverage: smoke\n"), (A,)
        )
        self.assertEqual((), guard_roles(single.resources))
        (identity,), _ = compile_plan(step_plan("Ubertooth identity"), (A, B))
        self.assertEqual({"Ubertooth": "UT_0"}, dict(identity.resources))

    def test_each_selection_guards_the_other_board(self):
        first, second = compile_plan(
            step_plan("BLE CRC transitions", "each", "        coverage: smoke\n"), (A, B)
        )[0]
        self.assertEqual(("UT_0", "UT_1"), (first.resources["Ubertooth"],
                                            first.resources["guard Ubertooth 1"]))
        self.assertEqual(("UT_1", "UT_0"), (second.resources["Ubertooth"],
                                            second.resources["guard Ubertooth 1"]))

    def test_guard_only_board_gets_a_serial_preflight_not_a_revision_check(self):
        (invocation,), registry = compile_plan(
            step_plan("Radio setting transitions", extra="        coverage: smoke\n"), (A, B)
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = run_contract(Path(directory), invocation)
            checks = PreflightPolicy(registry).compile((invocation,), manifest)
        by_board = {}
        for check in checks:
            by_board.setdefault(check.resource_alias, []).append(check.name)
        self.assertEqual(
            ["Firmware version", "Firmware compile info", "Device serial"], by_board["UT_0"]
        )
        # Guard-only: serial plus an API floor (the host API), not a revision.
        self.assertEqual(["Device serial", "Guard firmware API"], by_board["UT_1"])
        self.assertIn(("ubertooth-util", "-U", B, "-s"), [c.argv for c in checks])
        (api_check,) = [c for c in checks if c.name == "Guard firmware API"]
        self.assertEqual(HOST_API, api_check.minimum_api)
        older = transitions.format_api(HOST_API - 1)
        newer = transitions.format_api(HOST_API + 1)
        ok = CommandResult(api_check.argv, 0, f"Firmware version: git-x (API:{newer})\n", "", 0.01, False)
        old = CommandResult(api_check.argv, 0, f"Firmware version: git-x (API:{older})\n", "", 0.01, False)
        self.assertEqual("", api_check.result_failure(ok))
        self.assertIn("is older than the host tools API", api_check.result_failure(old))

    def test_host_api_is_parsed_from_the_resolved_source_and_fails_closed(self):
        from types import SimpleNamespace
        from ubertooth_harness.stateful import StateCaseError

        source = SimpleNamespace(name="Ubertooth", source_path="/x", commit="c" * 40)
        with mock.patch.object(transitions, "read_host_api_header",
                               return_value="#define UBERTOOTH_API_VERSION 0x0108\n"):
            self.assertEqual(0x0108, transitions.host_api_version(source))
        for text in ("", "#define UBERTOOTH_API_VERSION 0x0107\n#define UBERTOOTH_API_VERSION 0x0108\n"):
            with self.subTest(text=text), mock.patch.object(
                transitions, "read_host_api_header", return_value=text
            ), self.assertRaisesRegex(StateCaseError, "guarded tests refused"):
                transitions.host_api_version(source)

    def test_all_digit_serial_is_refused(self):
        from ubertooth_harness.stateful import StateCaseError

        with self.assertRaisesRegex(StateCaseError, "device index"):
            transitions.guard_boards({
                "Ubertooth": {"serial": A},
                "guard Ubertooth 1": {"serial": "1" * 32},
            })

    def test_guard_reads_do_not_count_toward_the_firmware_order_check(self):
        text = f"""plan: guards
suites:
  fast:
    - Flash reliability:
        hosts: [Host_Linux]
        platforms: [linux]
        Ubertooth: UT_1
        flashes: 2
        image sequence: [test build, KGV]
        KGV sha256: {KGV}
        KGV revision: 2020-12-R1
    - Radio setting transitions:
        hosts: each
        Ubertooth: UT_0
        coverage: smoke
"""
        invocations, registry = compile_plan(text, (A, B))
        self.assertEqual("UT_1", invocations[1].resources["guard Ubertooth 1"])
        PreflightPolicy(registry).check_order(invocations)  # does not raise

    def test_guards_need_full_distinct_serials(self):
        with self.assertRaisesRegex(StateCaseError, "guard Ubertooth 1 needs a full"):
            transitions.guard_boards({
                "Ubertooth": {"serial": A}, "guard Ubertooth 1": {"serial": "b" * 8},
            })
        with self.assertRaisesRegex(StateCaseError, "Ubertooth needs a full"):
            transitions.guard_boards({
                "Ubertooth": {"serial": "abc123"}, "guard Ubertooth 1": {"serial": B},
            })
        with self.assertRaisesRegex(StateCaseError, "must differ"):
            transitions.guard_boards({
                "Ubertooth": {"serial": A}, "guard Ubertooth 1": {"serial": A.upper()},
            })
        self.assertEqual((), transitions.guard_boards({"Ubertooth": {"serial": "abc"}}))

    def test_run_suite_refuses_short_serials_before_any_device_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hardware.yaml").write_text(hardware((A, "bbbb1111")))
            (root / "plan.yaml").write_text(
                step_plan("Radio setting transitions", extra="        coverage: smoke\n")
            )
            argv = ["run-suite", "--plan", str(root / "plan.yaml"), "--suite", "fast",
                    "--hardware", str(root / "hardware.yaml"), "--host", "Host_Linux",
                    "--ubertooth-source", ".", "--ubertooth-ref", "HEAD",
                    "--output", str(root / "runs")]
            with mock.patch.object(cli, "_detected_platform", return_value="linux"), \
                    mock.patch.object(cli, "_resolve_sources") as resolve, \
                    mock.patch("sys.stderr") as stderr:
                code = cli.main(argv)
            message = "".join(call.args[0] for call in stderr.write.call_args_list)
            self.assertEqual(2, code)
            self.assertIn("guard Ubertooth 1 needs a full 32-hex-digit serial", message)
            resolve.assert_not_called()
            self.assertFalse((root / "runs").exists())

    def test_validate_plan_warns_about_short_guard_serials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hardware.yaml").write_text(hardware((A, "bbbb1111")))
            (root / "plan.yaml").write_text(
                step_plan("Radio setting transitions", extra="        coverage: smoke\n")
            )
            with mock.patch("sys.stdout") as stdout:
                code = cli.main(["validate-plan", str(root / "plan.yaml"),
                                 "--hardware", str(root / "hardware.yaml")])
            printed = "".join(call.args[0] for call in stdout.write.call_args_list)
            self.assertEqual(0, code)
            self.assertIn("warning: guard Ubertooth 1 needs a full 32-hex-digit serial",
                          printed)
            self.assertIn("run-suite refuses this invocation", printed)
            (root / "hardware.yaml").write_text(hardware((A, B)))
            with mock.patch("sys.stdout") as stdout:
                cli.main(["validate-plan", str(root / "plan.yaml"),
                          "--hardware", str(root / "hardware.yaml")])
            printed = "".join(call.args[0] for call in stdout.write.call_args_list)
            self.assertNotIn("warning:", printed)


def leak_once(target, mutate, selected=A):
    """A host-tool bug: the first command to ``selected`` changes ``target`` once."""
    fired = []

    def leak(command, boards):
        if not fired and command[2] == selected:
            fired.append(command)
            mutate(boards[target])
    return leak


def flip(value):
    return "0" if value == "1" else "1"


# One disturbance per guard read, keyed by the read's setting name.
GUARD_CHANGES = {
    "firmware": lambda board: setattr(board, "firmware", board.firmware + "-reflashed"),
    "compile-info": lambda board: setattr(
        board, "compile_info", board.compile_info + " rebuilt"
    ),
    "channel": lambda board: board.state.update(
        channel=str(int(board.state["channel"]) + 1)
    ),
    "pa-level": lambda board: board.state.update(pa=str((int(board.state["pa"]) + 1) % 8)),
    "squelch": lambda board: board.state.update(
        squelch=str(int(board.state["squelch"]) - 1)
    ),
    "leds": lambda board: board.state.update(usr=flip(board.state["usr"])),
    transitions.GUARD_CRC_KEY: lambda board: board.state.update(
        crc=flip(board.state["crc"])
    ),
}


def failed_guard_reads(events):
    return [
        e.details["step_name"] for e in events
        if e.event_type == "step_finished" and e.details["stage"] == "guard"
        and e.status == "failed"
    ]


def test_finished(events):
    (finished,) = [e for e in events if e.event_type == "test_finished"]
    return finished


class GuardExecutionTests(unittest.TestCase):

    def assert_guard_commands_read_only(self, bench, serial):
        commands = bench.commands_for(serial)
        self.assertTrue(commands)
        for command in commands:
            self.assertEqual(serial, command[2], command)
            if command[0] == "ubertooth-btle":
                self.assertEqual(("-v",), command[3:], command)
            else:
                self.assertEqual("ubertooth-util", command[0])
                self.assertEqual(1, len(command[3:]), command)
                self.assertIn(command[3], GUARD_QUERIES, command)

    def test_untouched_guard_passes_with_baseline_and_a_check_after_every_case(self):
        guard = FakeUbertooth(serial=B, crc="1")
        # Expected values come from the guard's own baseline, not defaults.
        guard.state.update(channel="2470", pa="0", squelch="-75", usr="0", rx="1", tx="1")
        before = dict(guard.state)
        bench = Bench(FakeUbertooth(serial=A), guard)
        invocation = guarded_invocation("Radio setting transitions")

        summary, events, report = run_guarded(invocation, bench)

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(before, guard.state)
        self.assert_guard_commands_read_only(bench, B)
        checks = guard_checks(events)
        cases = state_cases(events)
        self.assertEqual(1 + len(cases), len(checks))
        self.assertEqual("Guard Ubertooths baseline", checks[0].details["case_name"])
        self.assertTrue(all(check.status == "passed" for check in checks))
        reads = [e for e in events if e.event_type == "step_finished"
                 and e.details["stage"] == "guard"]
        baseline = {
            e.details["step_name"]: e.details["observed"] for e in reads[:READS_PER_GUARD]
        }
        self.assertEqual({
            "guard Ubertooth 1 firmware":
                f"{guard.firmware} (API:{transitions.format_api(guard.api)})",
            "guard Ubertooth 1 compile-info": guard.compile_info,
            "guard Ubertooth 1 channel": before["channel"],
            "guard Ubertooth 1 pa-level": before["pa"],
            "guard Ubertooth 1 squelch": before["squelch"],
            "guard Ubertooth 1 leds": "/".join(before[led] for led in ("usr", "rx", "tx")),
            "guard Ubertooth 1 crc": before["crc"],
        }, baseline)
        for read in reads[READS_PER_GUARD:]:
            self.assertEqual(baseline[read.details["step_name"]], read.details["expected"])
            self.assertEqual(read.details["expected"], read.details["observed"])
        # Every guard command is in the run record with output, exit code, and duration.
        guard_ids = {
            e.details["command_id"] for e in events
            if e.event_type == "command_started" and e.details["argv"][1:3] == ["-U", B]
        }
        finished = [e for e in events if e.event_type == "command_finished"
                    and e.details["command_id"] in guard_ids]
        self.assertEqual(len(guard_ids), len(finished))
        self.assertTrue(all("duration_s" in e.details and "returncode" in e.details
                            for e in finished))
        self.assertTrue(any(e.event_type == "command_output"
                            and e.details["command_id"] in guard_ids for e in events))
        self.assertIn("guard Ubertooth 1=UT_1", report)
        self.assertIn("+++ Guard Ubertooths baseline +++", report)
        self.assertIn(f"ubertooth-util -U {B} -C", report)
        self.assertIn("Current frequency: 2470 MHz", report)

    def test_a_changed_guard_fails_the_test_and_stops_it(self):
        def leak(command, boards):
            # A host-tool bug: a channel set reaches every board.
            for option in command[3:]:
                if command[0] == "ubertooth-util" and option.startswith("-c") and len(option) > 2:
                    for board in boards.values():
                        board.state["channel"] = option[2:]

        guard = FakeUbertooth(serial=B)
        guard.state["channel"] = "2470"
        bench = Bench(FakeUbertooth(serial=A), guard, leak=leak)
        summary, events, report = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        self.assertFalse(summary.passed)
        self.assertEqual("failed", summary.status)
        checks = guard_checks(events)
        self.assertEqual("failed", checks[-1].status)
        self.assertIn("guard Ubertooth 1 channel", checks[-1].details["reason"])
        self.assertIn("expected", checks[-1].details["reason"])
        failed = [e for e in events if e.event_type == "step_finished"
                  and e.details["stage"] == "guard" and e.status == "failed"]
        self.assertEqual(["guard Ubertooth 1 channel"],
                         [e.details["step_name"] for e in failed])
        self.assertEqual("2470", failed[0].details["expected"])
        # The test stopped at the first case that disturbed the guard.
        self.assertEqual(1, len(state_cases(events)))
        self.assertIn("Guard Ubertooths unchanged after channel-mode-stop-restore", summary.failure_reason)
        self.assertIn(
            "check and restore guard Ubertooth 1 (UT_1, serial " + B + ") by hand",
            checks[-1].details["reason"],
        )
        self.assertIn("EXIT: failed", report)

    def test_read_only_test_checks_guards_once_after_the_test(self):
        guard = FakeUbertooth(serial=B)
        bench = Bench(FakeUbertooth(serial=A), guard)
        summary, events, _ = run_guarded(
            guarded_invocation("Ubertooth util compositions"), bench
        )
        self.assertTrue(summary.passed, summary.failure_reason)
        names = [check.details["case_name"] for check in guard_checks(events)]
        self.assertEqual(
            ["Guard Ubertooths baseline", "Guard Ubertooths unchanged after the test"], names
        )
        self.assert_guard_commands_read_only(bench, B)

    def test_unreadable_guard_baseline_fails_before_any_case(self):
        guard = FakeUbertooth(serial=B, fail=lambda argv: argv[-1] == "-z")
        selected = FakeUbertooth(serial=A)
        bench = Bench(selected, guard)
        summary, events, _ = run_guarded(guarded_invocation("BLE CRC transitions"), bench)
        self.assertFalse(summary.passed)
        self.assertEqual([], bench.commands_for(A))
        self.assertEqual([], state_cases(events))
        (check,) = guard_checks(events)
        self.assertEqual("failed", check.status)
        self.assertIn("guard Ubertooth 1 squelch: command exited 1", check.details["reason"])
        self.assertIn("no case ran", check.details["reason"])
        # Every read still ran, so the record holds the whole baseline attempt.
        self.assertEqual(READS_PER_GUARD, len(bench.commands_for(B)))

    def test_every_guard_is_checked(self):
        guards = (FakeUbertooth(serial=B), FakeUbertooth(serial=C))
        bench = Bench(FakeUbertooth(serial=A), *guards)
        summary, _events, _ = run_guarded(
            guarded_invocation("BLE mode conflicts", guards=(B, C)), bench
        )
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assert_guard_commands_read_only(bench, B)
        self.assert_guard_commands_read_only(bench, C)
        self.assertEqual(len(bench.commands_for(B)), len(bench.commands_for(C)))

    def test_guards_are_still_checked_after_a_canceled_case(self):
        token = CancellationToken()
        selected = FakeUbertooth(
            serial=A, cancel_on=lambda argv: argv[0] == "ubertooth-btle" and "-n" in argv
        )
        bench = Bench(selected, FakeUbertooth(serial=B))
        summary, events, _ = run_guarded(
            guarded_invocation("BLE CRC transitions"), bench, cancellation=token
        )
        self.assertEqual("canceled", summary.status)
        checks = guard_checks(events)
        self.assertEqual(2, len(checks))
        self.assertEqual("passed", checks[-1].status)
        self.assertIn("after crc-enabled-during-no-follow-adv37", checks[-1].details["case_name"])

    def test_guard_with_an_older_api_never_runs_ubertooth_btle(self):
        older = HOST_API - 1
        guard = FakeUbertooth(serial=B, api=older)
        bench = Bench(FakeUbertooth(serial=A), guard)
        summary, events, _ = run_guarded(guarded_invocation("BLE CRC transitions"), bench)

        self.assertFalse(summary.passed)
        self.assertEqual(0, guard.stops)
        self.assertEqual([], [c for c in bench.commands_for(B) if c[0] == "ubertooth-btle"])
        self.assertEqual([], bench.commands_for(A))
        self.assertEqual([], state_cases(events))
        (check,) = guard_checks(events)
        self.assertEqual("failed", check.status)
        reason = check.details["reason"]
        self.assertIn(f"firmware API {transitions.format_api(older)} is older", reason)
        self.assertIn("too old to read CRC safely", reason)
        self.assertEqual([f"guard Ubertooth 1 {transitions.GUARD_CRC_KEY}"],
                         failed_guard_reads(events))
        (refused,) = [e for e in events if e.event_type == "step_finished"
                      and e.details["stage"] == "guard" and e.status == "failed"]
        self.assertIsNone(refused.details["argv"])
        # Every util query still ran; only the btle read was refused.
        self.assertEqual(READS_PER_GUARD - 1, len(bench.commands_for(B)))

    def test_btle_read_is_refused_without_a_confirmed_api(self):
        underived = transitions.GuardBoard("guard Ubertooth 1", B)
        (crc,) = [s for s in transitions.guard_steps((underived,))
                  if s.argv[0] == "ubertooth-btle"]
        self.assertIn("host tools API not derived", crc.refuse({}))
        board = transitions.GuardBoard("guard Ubertooth 1", B, host_api=HOST_API)
        (crc,) = [s for s in transitions.guard_steps((board,))
                  if s.argv[0] == "ubertooth-btle"]
        firmware_key = board.key(transitions.GUARD_FIRMWARE_KEY)
        current = transitions.format_api(HOST_API)
        newer = transitions.format_api(HOST_API + 1)
        self.assertIn("not confirmed", crc.refuse({}))
        self.assertIn("differs from its baseline",
                      crc.refuse({transitions.differs_key(firmware_key): "git-other (API:1.06)"}))
        self.assertIn("unreadable", crc.refuse({firmware_key: "git-test"}))
        self.assertEqual("", crc.refuse({firmware_key: f"git-test (API:{current})"}))
        self.assertEqual("", crc.refuse({firmware_key: f"git-test (API:{newer})"}))

    def test_each_guard_read_catches_its_own_difference(self):
        invocation = guarded_invocation("Radio setting transitions")
        first_case = builtin_catalog().get(invocation.test_name).command_cases(
            invocation.arguments, invocation.resources, invocation.resource_identities
        )[0]
        self.assertEqual(set(GUARD_KEYS), set(GUARD_CHANGES))
        for key, change in GUARD_CHANGES.items():
            with self.subTest(key):
                guard = FakeUbertooth(serial=B)
                bench = Bench(FakeUbertooth(serial=A), guard, leak=leak_once(B, change))
                summary, events, _ = run_guarded(invocation, bench)
                self.assertEqual("failed", summary.status)
                expected = [f"guard Ubertooth 1 {key}"]
                if key == transitions.GUARD_FIRMWARE_KEY:
                    # A changed firmware read records no API, so btle is refused.
                    expected.append(f"guard Ubertooth 1 {transitions.GUARD_CRC_KEY}")
                self.assertEqual(expected, failed_guard_reads(events))
                self.assertEqual(1, len(state_cases(events)))
                self.assertIn(
                    f"Guard Ubertooths unchanged after {first_case.case_id}",
                    summary.failure_reason,
                )
                self.assertEqual(0, guard.stops)

    def test_guard_change_during_a_read_only_test_is_caught_after_the_test(self):
        invocation = guarded_invocation("Ubertooth util compositions")
        cases = builtin_catalog().get(invocation.test_name).command_cases(
            invocation.arguments, invocation.resources, invocation.resource_identities
        )
        self.assertFalse(any(getattr(case, "state_changing", False) for case in cases))
        guard = FakeUbertooth(serial=B)
        bench = Bench(
            FakeUbertooth(serial=A), guard, leak=leak_once(B, GUARD_CHANGES["squelch"])
        )
        summary, events, report = run_guarded(invocation, bench)

        self.assertEqual("failed", summary.status)
        checks = guard_checks(events)
        self.assertEqual(
            ["Guard Ubertooths baseline", "Guard Ubertooths unchanged after the test"],
            [check.details["case_name"] for check in checks],
        )
        self.assertEqual("failed", checks[-1].status)
        self.assertEqual(["guard Ubertooth 1 squelch"], failed_guard_reads(events))
        finished = test_finished(events)
        self.assertEqual(0, finished.details["failed_cases"])
        self.assertEqual(1, finished.details["failed_guard_checks"])
        self.assertIn("| 0 + 1 guard check |", report)

    def test_only_the_changed_guard_is_named(self):
        bench = Bench(
            FakeUbertooth(serial=A), FakeUbertooth(serial=B), FakeUbertooth(serial=C),
            leak=leak_once(C, GUARD_CHANGES["pa-level"]),
        )
        invocation = guarded_invocation("Radio setting transitions", guards=(B, C))
        summary, events, _ = run_guarded(invocation, bench)

        self.assertEqual("failed", summary.status)
        self.assertEqual(["guard Ubertooth 2 pa-level"], failed_guard_reads(events))
        reason = guard_checks(events)[-1].details["reason"]
        alias = invocation.resources["guard Ubertooth 2"]
        self.assertIn(f"check and restore guard Ubertooth 2 ({alias}, serial {C})", reason)
        self.assertNotIn("guard Ubertooth 1 (", reason)
        # The unchanged first guard was still read in full in every check.
        self.assertEqual(len(bench.commands_for(B)), len(bench.commands_for(C)))

    def test_cancellation_during_the_baseline_runs_no_case(self):
        options = [option for _key, option, *_rest in transitions.GUARD_READBACK]
        canceled_at = options.index("-C")
        token = CancellationToken()
        guard = FakeUbertooth(serial=B, cancel_on=lambda argv: argv[-1] == "-C")
        bench = Bench(FakeUbertooth(serial=A), guard)
        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"), bench, cancellation=token
        )

        self.assertEqual("canceled", summary.status)
        self.assertEqual([], bench.commands_for(A))
        self.assertEqual([], state_cases(events))
        (check,) = guard_checks(events)
        self.assertEqual("canceled", check.status)
        # No read runs after the canceled one.
        self.assertEqual(canceled_at + 1, len(bench.commands_for(B)))
        self.assertEqual("canceled", test_finished(events).status)
        self.assertEqual(0, test_finished(events).details["failed_guard_checks"])

    def test_guard_difference_stops_a_continue_on_failure_test(self):
        invocation = guarded_invocation("Ubertooth util conflicts")
        definition = builtin_catalog().get(invocation.test_name)
        self.assertTrue(definition.continue_on_case_failure)
        cases = definition.command_cases(
            invocation.arguments, invocation.resources, invocation.resource_identities
        )
        self.assertGreater(len(cases), 1)
        bench = Bench(
            FakeUbertooth(serial=A), FakeUbertooth(serial=B),
            leak=leak_once(B, GUARD_CHANGES["channel"]),
        )
        summary, events, _ = run_guarded(invocation, bench)

        self.assertEqual("failed", summary.status)
        self.assertEqual(1, len(state_cases(events)))
        self.assertEqual(["guard Ubertooth 1 channel"], failed_guard_reads(events))
        self.assertIn(f"after {cases[0].case_id}", summary.failure_reason)

    def test_sequencer_refuses_guards_on_a_test_that_does_not_support_them(self):
        invocation = guarded_invocation("Ubertooth identity")
        with tempfile.TemporaryDirectory() as directory:
            manifest, checks = run_contract(Path(directory), invocation)
            bench = Bench(FakeUbertooth(serial=A), FakeUbertooth(serial=B))
            with self.assertRaisesRegex(ValueError, "does not support guard"):
                NamedTestSequencer(bench).run(
                    (invocation,), Path(directory), run_id="named-run",
                    manifest=manifest, preflight_checks=checks,
                )
            self.assertEqual([], bench.commands)


if __name__ == "__main__":
    unittest.main()
