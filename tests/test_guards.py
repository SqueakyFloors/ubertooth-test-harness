"""Guard Ubertooths: other registered boards stay unchanged during transition tests."""

import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ubertooth_harness import bootloader, cli, transitions
from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.catalog import TestCatalog, builtin_catalog
from ubertooth_harness.patterns import (
    APPLICATION_IMAGE,
    BTBR_IMAGE,
    RX_ONLY_IMAGE,
)
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.plan import load_plan
from ubertooth_harness.cases import guard_roles
from ubertooth_harness.planner import PlanCompiler, TestInvocation
from ubertooth_harness.preflight import PreflightPolicy
from ubertooth_harness.rawlog import STAGE_BASELINE, STAGE_STATE
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
# The query options a guard read may use, written out rather than taken from
# GUARD_READBACK: a write option added to the read set would satisfy a check
# derived from that set.
GUARD_QUERIES = {"-v", "-V", "-s", "-B", "-C", "-a", "-z", "-d"}
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


def destructive_catalog(test_name):
    """``test_name`` as a destructive test, for the guard rule that adds.

    The destructive test of the catalog needs a bench the fakes here cannot
    stand in for (a hub to switch, a bootloader to come up in), so the rule
    is exercised on a test they can run.
    """
    return TestCatalog(
        [replace(builtin_catalog().get(test_name), destructive=True)]
    )


def run_guarded(invocation, bench, cancellation=None, destructive=False):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(
            HostVersionPassthrough(bench), event_sink=sink,
            catalog=(
                destructive_catalog(invocation.test_name) if destructive else None
            ),
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
        # Guard-only: serial plus the host tools' own API, not a revision.
        self.assertEqual(["Device serial", "Guard firmware API"], by_board["UT_1"])
        self.assertIn(("ubertooth-util", "-U", B, "-s"), [c.argv for c in checks])
        (api_check,) = [c for c in checks if c.name == "Guard firmware API"]
        self.assertEqual(HOST_API, api_check.required_api)
        current = transitions.format_api(HOST_API)
        ok = CommandResult(api_check.argv, 0,
                           f"Firmware version: git-x (API:{current})\n", "", 0.01, False)
        self.assertEqual("", api_check.result_failure(ok))
        # Either way about: ubertooth-btle may refuse any other API and stop
        # the board, so the guard check does not read its CRC.
        for api in (HOST_API - 1, HOST_API + 1):
            other = CommandResult(
                api_check.argv, 0,
                f"Firmware version: git-x (API:{transitions.format_api(api)})\n",
                "", 0.01, False,
            )
            with self.subTest(transitions.format_api(api)):
                self.assertIn("is not the host tools API",
                              api_check.result_failure(other))

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

    def test_only_the_tests_ubertooth_roles_are_selectors(self):
        # A USB power controller also has a serial, but the test never
        # selects it with -U, so its short serial is not refused.
        identities = {
            "Ubertooth": {"serial": A},
            "USB power controller": {"serial": "0x1234abcd"},
            "guard Ubertooth 1": {"serial": B},
        }
        (guard,) = transitions.guard_boards(identities)
        self.assertEqual(B, guard.serial)
        # A multi-board test names its Ubertooth roles; each must be a full,
        # distinct serial.
        roles = transitions.ubertooth_roles(
            {"sender": "Ubertooth", "responder": "Ubertooth",
             "USB power controller": "USB power controller"})
        self.assertEqual(("sender", "responder"), roles)
        pair = {"sender": {"serial": A}, "responder": {"serial": C},
                "USB power controller": {"serial": "0x1234abcd"},
                "guard Ubertooth 1": {"serial": B}}
        self.assertEqual(1, len(transitions.guard_boards(pair, roles)))
        with self.assertRaisesRegex(StateCaseError, "responder needs a full"):
            transitions.guard_boards({**pair, "responder": {"serial": "c" * 8}}, roles)
        with self.assertRaisesRegex(StateCaseError, "must differ"):
            transitions.guard_boards({**pair, "responder": {"serial": B}}, roles)
        # Alternated boards are Ubertooths the test selects too.
        with self.assertRaisesRegex(StateCaseError, "alternated Ubertooth 2 needs a full"):
            transitions.guard_boards({
                "Ubertooth": {"serial": A}, "alternated Ubertooth 2": {"serial": "c" * 8},
                "guard Ubertooth 1": {"serial": B},
            })

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
    # The serial the board reports, not the one commands select it by: this is
    # a board answering as somebody else, which is what the read catches.
    transitions.GUARD_IDENTITY_KEY: lambda board: setattr(
        board, "reported_serial", "d" * 32
    ),
    transitions.GUARD_BOOTLOADER_KEY: lambda board: setattr(
        board, "bootloader", "2.00 git-test, CRC32:0x33334444"
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


# The two stages a read of a board the test must leave alone is recorded
# under: the baseline snapshot, and every later check of it (#342).
GUARD_STAGES = (STAGE_BASELINE, STAGE_STATE)


def is_guard_read(event) -> bool:
    return (
        event.event_type == "step_finished"
        and event.details["stage"] in GUARD_STAGES
    )


def read_name(details) -> str:
    """One read's name without the suffix a later check's name carries, so a
    baseline and the checks of it are looked up under one key (#342)."""
    return str(details["step_name"]).removesuffix(" unchanged")


def failed_guard_reads(events):
    return [
        read_name(e.details) for e in events
        if is_guard_read(e) and e.status == "failed"
    ]


def test_finished(events):
    (finished,) = [e for e in events if e.event_type == "test_finished"]
    return finished


class ReadOnlyGuardCommands:
    """Every command a guard was sent is one of the reads, and nothing else."""

    def assert_guard_commands_read_only(self, bench, serial):
        commands = bench.commands_for(serial)
        self.assertTrue(commands)
        for command in commands:
            self.assertEqual(serial, command[2], command)
            if command[0] == "ubertooth-btle":
                self.assertEqual(("-k",), command[3:], command)
            else:
                self.assertEqual("ubertooth-util", command[0])
                self.assertEqual(1, len(command[3:]), command)
                self.assertIn(command[3], GUARD_QUERIES, command)


class GuardExecutionTests(ReadOnlyGuardCommands, unittest.TestCase):

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
        reads = [e for e in events if is_guard_read(e)]
        # The baseline is recorded under its own stage, every later check of
        # it under the other, and a later check says it found it unchanged.
        self.assertEqual(
            [STAGE_BASELINE] * READS_PER_GUARD,
            [e.details["stage"] for e in reads[:READS_PER_GUARD]],
        )
        self.assertTrue(all(e.details["stage"] == STAGE_STATE
                            for e in reads[READS_PER_GUARD:]))
        self.assertTrue(all(e.details["step_name"].endswith(" unchanged")
                            for e in reads[READS_PER_GUARD:]))
        baseline = {
            e.details["step_name"]: e.details["observed"] for e in reads[:READS_PER_GUARD]
        }
        self.assertEqual({
            "guard Ubertooth 1 firmware":
                f"{guard.firmware} (API:{transitions.format_api(guard.api)})",
            "guard Ubertooth 1 identity": guard.reported_serial,
            "guard Ubertooth 1 bootloader": f"Bootloader: {guard.bootloader}",
            "guard Ubertooth 1 channel": before["channel"],
            "guard Ubertooth 1 pa-level": before["pa"],
            "guard Ubertooth 1 squelch": before["squelch"],
            "guard Ubertooth 1 leds": "/".join(before[led] for led in ("usr", "rx", "tx")),
            "guard Ubertooth 1 crc": before["crc"],
        }, baseline)
        for read in reads[READS_PER_GUARD:]:
            self.assertEqual(baseline[read_name(read.details)], read.details["expected"])
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
        failed = [e for e in events if is_guard_read(e) and e.status == "failed"]
        # A check of the baseline, so the read says it found it unchanged.
        self.assertEqual(["guard Ubertooth 1 channel unchanged"],
                         [e.details["step_name"] for e in failed])
        self.assertEqual([STAGE_STATE], [e.details["stage"] for e in failed])
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
        # The command failed for a reason of its own, which is not the board
        # saying it cannot answer: no reading comes of it, so the read fails.
        self.assertIn(
            "guard Ubertooth 1 squelch: guard Ubertooth 1 squelch was not reported",
            check.details["reason"],
        )
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

    def test_guard_with_another_api_never_runs_ubertooth_btle(self):
        for api in (HOST_API - 1, HOST_API + 1):
            with self.subTest(transitions.format_api(api)):
                self.assert_api_keeps_btle_off_the_guard(api)

    def assert_api_keeps_btle_off_the_guard(self, api):
        guard = FakeUbertooth(serial=B, api=api)
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
        self.assertIn(
            f"firmware API {transitions.format_api(api)} is not the host tools API",
            reason,
        )
        self.assertIn("its API check may stop the guard", reason)
        self.assertEqual([f"guard Ubertooth 1 {transitions.GUARD_CRC_KEY}"],
                         failed_guard_reads(events))
        (refused,) = [e for e in events if is_guard_read(e) and e.status == "failed"]
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
        self.assertIn("could not be read", crc.refuse({firmware_key: "git-test"}))
        self.assertEqual("", crc.refuse({firmware_key: f"git-test (API:{current})"}))
        # Newer firmware as well: ubertooth-btle may refuse any API that is
        # not the one the tools were built for, stopping the board as it goes.
        self.assertIn(f"API {newer} is not the host tools API {current}",
                      crc.refuse({firmware_key: f"git-test (API:{newer})"}))

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
        self.assertIn("| 0 + 1 state check |", report)

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


# The reads the alternate btbr image has not got, as the options that ask for
# them: it implements no channel and no squelch request
# (firmware/btbr/src/btctl/vendor_request_handler.c). It does answer who it
# is, which image and revision it runs, what built it, the bootloader under
# it, the LEDs, and the transmit power level. The CRC read never reaches the
# board at all: `ubertooth-btle` refuses a board carrying another of this
# tree's images as it opens it, which is the third reading this host cannot
# make of such a guard.
BTBR_STALLS = ("C", "c", "z")
BTBR_UNANSWERED = ("channel", "squelch", transitions.GUARD_CRC_KEY)


def image_guard(image, serial=B, stalls=(), revision="git-test"):
    """A guard carrying ``image``, refusing the reads ``stalls`` names."""
    return FakeUbertooth(
        serial=serial, firmware=f"{image} {revision}", unsupported=stalls
    )


def guard_reads(events, board="guard Ubertooth 1"):
    """``board``'s guard reads in order; the first check is its baseline."""
    return [
        e for e in events
        if is_guard_read(e) and e.details["step_name"].startswith(f"{board} ")
    ]


def guard_readings(events, board="guard Ubertooth 1"):
    """The baseline reading of each of ``board``'s reads, by setting name."""
    return {
        e.details["step_name"][len(board) + 1:]: e.details["observed"]
        for e in guard_reads(events, board)[:READS_PER_GUARD]
    }


class ImageAwareGuardTests(ReadOnlyGuardCommands, unittest.TestCase):
    """A guard is read and compared whatever image it carries (#275).

    None of these skips a read or writes to a guard: the reads are the same
    ones every guard gets, and what a board says it cannot answer is recorded
    as its reading and compared like any other.
    """

    def test_a_guard_on_the_alternate_image_is_read_and_compared_in_full(self):
        guard = image_guard(BTBR_IMAGE, stalls=BTBR_STALLS)
        bench = Bench(FakeUbertooth(serial=A), guard)
        before = dict(guard.state)

        summary, events, report = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(before, guard.state)
        self.assert_guard_commands_read_only(bench, B)
        readings = guard_readings(events)
        self.assertEqual(
            {key: transitions.GUARD_UNSUPPORTED for key in BTBR_UNANSWERED},
            {key: readings[key] for key in BTBR_UNANSWERED},
        )
        # What it does answer is a real reading, not a tolerated one.
        self.assertEqual(f"{BTBR_IMAGE} git-test (API:"
                         f"{transitions.format_api(guard.api)})",
                         readings[transitions.GUARD_FIRMWARE_KEY])
        self.assertEqual(B, readings[transitions.GUARD_IDENTITY_KEY])
        self.assertEqual(f"Bootloader: {guard.bootloader}",
                         readings[transitions.GUARD_BOOTLOADER_KEY])
        self.assertEqual(before["pa"], readings["pa-level"])
        self.assertEqual("1/0/0", readings["leds"])
        # Every read ran in every check, including the ones the board
        # refused, and each later check compared each reading to the baseline.
        reads = guard_reads(events)
        checks = guard_checks(events)
        self.assertEqual(READS_PER_GUARD * len(checks), len(reads))
        later = reads[READS_PER_GUARD:]
        self.assertTrue(later)
        for read in later:
            self.assertEqual(read.details["expected"], read.details["observed"])
        self.assertEqual(
            len(BTBR_UNANSWERED) * (len(checks) - 1),
            len([r for r in later
                 if r.details["expected"] == transitions.GUARD_UNSUPPORTED]),
        )
        self.assertIn(transitions.GUARD_UNSUPPORTED, report)

    def test_two_guards_on_the_alternate_image_do_not_block_the_suite(self):
        """The bench #329 was measured on: two boards left on btbr.

        Before the reads were image-aware, the baseline of each such guard
        gave three `command exited 1` lines and no case ran, so one board
        left on the alternate image blocked every guarded test on the host --
        which a bench that runs the btbr test is expected to have. Both
        guards are read in full here, both pass, and neither is written to or
        reflashed to make it answer more.
        """
        guards = [image_guard(BTBR_IMAGE, serial=serial, stalls=BTBR_STALLS)
                  for serial in (B, C)]
        bench = Bench(FakeUbertooth(serial=A), *guards)
        before = [dict(guard.state) for guard in guards]

        summary, events, _report = run_guarded(
            guarded_invocation("Radio setting transitions", guards=(B, C)),
            bench,
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual([], failed_guard_reads(events))
        for index, (guard, state) in enumerate(zip(guards, before), 1):
            self.assertEqual(state, guard.state)
            self.assertEqual(0, guard.stops)
            self.assert_guard_commands_read_only(bench, guard.serial)
            readings = guard_readings(events, f"guard Ubertooth {index}")
            self.assertEqual(
                {key: transitions.GUARD_UNSUPPORTED
                 for key in BTBR_UNANSWERED},
                {key: readings[key] for key in BTBR_UNANSWERED},
            )
            self.assertEqual(guard.serial,
                             readings[transitions.GUARD_IDENTITY_KEY])

    def test_the_alternate_image_guard_still_catches_what_it_does_answer(self):
        guard = image_guard(BTBR_IMAGE, stalls=BTBR_STALLS)
        bench = Bench(
            FakeUbertooth(serial=A), guard,
            leak=leak_once(B, GUARD_CHANGES["pa-level"]),
        )

        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        self.assertEqual("failed", summary.status)
        self.assertEqual(["guard Ubertooth 1 pa-level"], failed_guard_reads(events))

    def test_a_reading_that_appears_where_the_board_refused_one_fails(self):
        """btbr cannot answer the channel; a board that starts to is a change."""
        guard = image_guard(BTBR_IMAGE, stalls=BTBR_STALLS)
        bench = Bench(
            FakeUbertooth(serial=A), guard,
            leak=leak_once(B, lambda board: setattr(
                board, "unsupported", frozenset(board.unsupported - {"C", "c"})
            )),
        )

        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        self.assertEqual("failed", summary.status)
        self.assertEqual(["guard Ubertooth 1 channel"], failed_guard_reads(events))
        (failed,) = [e for e in events if is_guard_read(e) and e.status == "failed"]
        self.assertEqual(transitions.GUARD_UNSUPPORTED, failed.details["expected"])

    def test_the_crc_read_is_refused_by_the_board_image_not_skipped(self):
        """The btle read runs and its refusal is the reading it records."""
        guard = image_guard(BTBR_IMAGE, stalls=BTBR_STALLS)
        bench = Bench(FakeUbertooth(serial=A), guard)

        summary, events, report = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        btle = [c for c in bench.commands_for(B) if c[0] == "ubertooth-btle"]
        self.assertEqual([("ubertooth-btle", "-U", B, "-k")] * len(guard_checks(events)),
                         btle)
        self.assertEqual(0, guard.stops)
        self.assertEqual(
            transitions.GUARD_UNSUPPORTED,
            guard_readings(events)[transitions.GUARD_CRC_KEY],
        )
        self.assertIn(f"this tool needs {APPLICATION_IMAGE}", report)

    def test_the_settings_image_must_answer_its_own_settings(self):
        guard = image_guard(APPLICATION_IMAGE, stalls=("z",))
        bench = Bench(FakeUbertooth(serial=A), guard)

        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        self.assertFalse(summary.passed)
        self.assertEqual([], state_cases(events))
        self.assertEqual([], bench.commands_for(A))
        (check,) = guard_checks(events)
        reason = check.details["reason"]
        self.assertIn(f"runs {APPLICATION_IMAGE}, which implements squelch", reason)
        self.assertIn("says it cannot answer it", reason)
        self.assertIn("no case ran", reason)
        # The refusal is the reading, so the read itself did not fail.
        self.assertEqual([], failed_guard_reads(events))

    def test_a_partly_answered_led_read_fails_instead_of_being_recorded(self):
        """`-d` asks for three LEDs at once, so a stall there is not an answer.

        Were it recorded, this board -- which answered two of the three --
        would leave all three unguarded for the rest of the run.
        """
        self.assertEqual(("leds",), transitions.GUARD_STRICT_KEYS)
        guard = FakeUbertooth(
            serial=B, firmware=f"{BTBR_IMAGE} git-test",
            unsupported=BTBR_STALLS, stalled_leds=("tx",),
        )
        bench = Bench(FakeUbertooth(serial=A), guard)

        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        # The board did print the two LED lines it could.
        partial = [c for c in bench.commands_for(B) if c[3:] == ("-d",)]
        self.assertTrue(partial)
        answer = guard.run(list(partial[0]), timeout_s=5)
        self.assertIn("USR LED status: ", answer.stdout)
        self.assertIn("RX LED status : ", answer.stdout)
        self.assertNotIn("TX LED status", answer.stdout)
        self.assertEqual(1, answer.returncode)

        self.assertFalse(summary.passed)
        self.assertEqual([], state_cases(events))
        self.assertEqual(["guard Ubertooth 1 leds"], failed_guard_reads(events))
        (check,) = guard_checks(events)
        # Strict: the read is not allowed to exit 1 either.
        self.assertIn("guard Ubertooth 1 leds: command exited 1",
                      check.details["reason"])
        self.assertIn("no case ran", check.details["reason"])

    def test_a_board_that_stalls_every_read_fails_the_strict_one_too(self):
        """What a wedged board looks like: every request stalls."""
        guard = FakeUbertooth(
            serial=B, unsupported=("v", "V", "s", "B", "C", "c", "z", "a", "d"),
        )
        bench = Bench(FakeUbertooth(serial=A), guard)

        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        self.assertFalse(summary.passed)
        self.assertEqual([], state_cases(events))
        # The LED read fails outright, and the CRC read is refused because
        # this check could confirm no firmware API.
        self.assertEqual(
            ["guard Ubertooth 1 leds", f"guard Ubertooth 1 {transitions.GUARD_CRC_KEY}"],
            failed_guard_reads(events),
        )
        (check,) = guard_checks(events)
        reason = check.details["reason"]
        self.assertIn("guard Ubertooth 1 leds", reason)
        self.assertIn("answered none of", reason)
        self.assertIn("wedged rather than short of one", reason)

    def test_the_receive_only_build_is_held_to_the_settings_it_has(self):
        """It answers the same settings bar the transmit power level."""
        guard = image_guard(RX_ONLY_IMAGE, stalls=("a",))
        bench = Bench(FakeUbertooth(serial=A), guard)

        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(transitions.GUARD_UNSUPPORTED,
                         guard_readings(events)["pa-level"])
        # The rest of its settings are its own, so a refusal there is a board
        # that is not holding them.
        refusing = image_guard(RX_ONLY_IMAGE, stalls=("a", "z"))
        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"),
            Bench(FakeUbertooth(serial=A), refusing),
        )
        self.assertFalse(summary.passed)
        self.assertIn(f"runs {RX_ONLY_IMAGE}, which implements squelch",
                      guard_checks(events)[0].details["reason"])

    def test_a_guard_the_run_cannot_observe_is_refused_by_name(self):
        """A guard that answers none of the stable reads is not a guard.

        Firmware this host can read nothing of, which is what the rule is
        about; a wedged board reads that way too, and fails the strict LED
        read as well (above).
        """
        guard = image_guard(
            BTBR_IMAGE, stalls=("v", "V", "s", "B") + BTBR_STALLS
        )
        bench = Bench(FakeUbertooth(serial=A), guard)

        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"), bench
        )

        self.assertFalse(summary.passed)
        self.assertEqual([], state_cases(events))
        self.assertEqual([], bench.commands_for(A))
        (check,) = guard_checks(events)
        reason = check.details["reason"]
        self.assertIn(f"guard Ubertooth 1 (serial {B}) answered none of "
                      + ", ".join(transitions.GUARD_PINNING_KEYS), reason)
        self.assertIn("it runs an image it did not name", reason)
        self.assertIn("wedged rather than short of one", reason)
        self.assertIn("no case ran", reason)
        # The CRC read is the one that does not reach a board whose firmware
        # this check could not confirm: ubertooth-btle may stop a board
        # whose API is not the host tools' own, so it is refused and fails
        # the check.
        self.assertEqual([], [c for c in bench.commands_for(B)
                              if c[0] == "ubertooth-btle"])
        self.assertIn(f"guard Ubertooth 1 {transitions.GUARD_CRC_KEY}",
                      failed_guard_reads(events))

    def test_the_destructive_test_is_refused_by_the_same_rule(self):
        guard = image_guard(
            BTBR_IMAGE, stalls=("v", "V", "s", "B") + BTBR_STALLS
        )
        bench = Bench(FakeUbertooth(serial=A), guard)

        summary, events, _ = run_guarded(
            guarded_invocation("Radio setting transitions"), bench, destructive=True
        )

        self.assertFalse(summary.passed)
        self.assertEqual([], state_cases(events))
        self.assertIn("answered none of", guard_checks(events)[0].details["reason"])

    def test_a_guard_that_pins_one_reading_is_enough(self):
        board = transitions.GuardBoard("guard Ubertooth 1", B)
        readings = {
            board.key(key): transitions.GUARD_UNSUPPORTED
            for key, *_rest in transitions.GUARD_READBACK
        }
        (problem,) = transitions.guard_baseline_problems((board,), readings)
        self.assertIn("answered none of", problem)
        readings[board.key(transitions.GUARD_IDENTITY_KEY)] = B
        readings[board.key(transitions.GUARD_FIRMWARE_KEY)] = (
            f"{BTBR_IMAGE} git-test (API:1.35)"
        )
        self.assertEqual([], transitions.guard_baseline_problems(
            (board,), readings
        ))
        # The same readings under the settings image are a refusal to guard.
        readings[board.key(transitions.GUARD_FIRMWARE_KEY)] = (
            f"{APPLICATION_IMAGE} git-test (API:1.35)"
        )
        (problem,) = transitions.guard_baseline_problems((board,), readings)
        self.assertIn(f"runs {APPLICATION_IMAGE}", problem)
        # The LEDs are not on that list: their read is the strict one, which
        # every guard is held to whatever it runs.
        self.assertIn("channel, pa-level, squelch", problem)
        self.assertNotIn("leds", problem)

    def test_the_bootloader_pattern_reads_every_line_the_tool_prints(self):
        """Kept in step with bootloader.py, which renders the same line."""
        for identity in (
            bootloader.BootloaderIdentity(
                0x9D2FE1B4, "2.00 git-1a2b3c4", "2.00", "git-1a2b3c4"),
            bootloader.BootloaderIdentity(
                0x5F0A73C1, bootloader.NO_VERSION_BLOCK_DETAIL),
            bootloader.BootloaderIdentity(
                0x11223344,
                "the bootloader version block is in a format this host does "
                "not know"),
        ):
            with self.subTest(identity.detail):
                match = re.search(transitions.GUARD_BOOTLOADER_PATTERN,
                                  identity.line() + "\n")
                self.assertIsNotNone(match)
                self.assertEqual(identity.line(), match["value"])

    def test_only_two_answers_are_read_as_a_reading_this_host_cannot_make(self):
        """Every other line a failed read prints still fails the read.

        The whole weight of the tolerance rests on this: a line the board or
        the tool printed for some other reason must not become a value that
        then compares equal for the rest of the run.
        """
        board = transitions.GuardBoard("guard Ubertooth 1", B, host_api=HOST_API)
        # Every read that tolerates an unanswered request, each read the way
        # the run reads it: the lines on stderr, through the step's own
        # observation, which searches stdout first and stderr after.
        reads = [
            step for step in transitions.guard_steps((board,))
            if step.expected_returncodes == (0, 1)
        ]
        self.assertEqual(
            len([key for key, *_rest in transitions.GUARD_READBACK
                 if key not in transitions.GUARD_STRICT_KEYS]) + 1,
            len(reads),
        )

        def readings(text):
            """What each tolerant read makes of ``text`` on stderr."""
            return {
                step.name: step.observe.extract(
                    CommandResult(step.argv, 1, "", text, 0.01, False)
                )
                for step in reads
            }

        unsupported = (
            # libubertooth's stall wording, with and without the libusb code.
            "error: could not read the firmware information: the board's "
            "firmware does not support it (stalled, libusb -9)\n",
            "error: could not read the firmware information: the board's "
            "firmware does not support it (stalled)\n",
            # The image refusal ubertooth-btle prints as it opens a board.
            f"error: the Ubertooth is running {BTBR_IMAGE}; this tool needs "
            f"{APPLICATION_IMAGE}\n",
        )
        for text in unsupported:
            with self.subTest(text.split("\n")[0]):
                self.assertEqual(
                    {step.name: transitions.GUARD_UNSUPPORTED for step in reads},
                    readings(text),
                )

        # A value the board did print wins over a stall beside it. That is
        # the `-v` case on firmware older than the info block: the image
        # request stalls, libubertooth says so on stderr, and the revision
        # fallback still prints a whole version line on stdout. Without
        # stdout first, the firmware reading would be unsupported and the
        # settings requirements would fall away with the image name.
        firmware = f"2020-12-R1 (API:{transitions.format_api(HOST_API)})"
        (version_read,) = [
            step for step in reads
            if step.name.endswith(transitions.GUARD_FIRMWARE_KEY)
        ]
        self.assertEqual(firmware, version_read.observe.extract(CommandResult(
            version_read.argv, 1, f"Firmware version: {firmware}\n",
            unsupported[0], 0.01, False,
        )))

        fails = (
            # A board that has left the bus stalls requests too, and says so.
            "error: could not read the firmware information: the board is no "
            "longer attached\n",
            "error: could not read the firmware information: the board did "
            "not answer in time\n",
            # The tool's own line after any failed read, whatever the cause.
            "error: firmware information read failed\n",
            "libUSB Error: Other error:  (4)\n",
            # The API mismatch, in both the forms the tools print it.
            "error: Ubertooth firmware API version 1.57 found, libubertooth "
            "1.1 requires 1.58.\n",
            "warning: Ubertooth firmware API version 1.57 found, "
            "libubertooth 1.1 requires 1.58.\n",
            "",
        )
        for text in fails:
            with self.subTest(text.split("\n")[0] or "no output"):
                self.assertEqual(
                    {step.name: None for step in reads}, readings(text)
                )

    def test_the_image_name_comes_off_the_firmware_reading(self):
        self.assertEqual(APPLICATION_IMAGE, transitions.guard_image(
            f"{APPLICATION_IMAGE} git-1c7f06d6 (API:1.35)"
        ))
        self.assertEqual(BTBR_IMAGE, transitions.guard_image(
            f"{BTBR_IMAGE} git-1c7f06d6 (API:1.35)"
        ))
        # Firmware older than the info block names no image.
        self.assertIsNone(transitions.guard_image("2020-12-R1 (API:1.07)"))
        self.assertIsNone(transitions.guard_image(transitions.GUARD_UNSUPPORTED))
        self.assertIsNone(transitions.guard_image(None))
        self.assertIsNone(transitions.guard_image(""))


if __name__ == "__main__":
    unittest.main()
