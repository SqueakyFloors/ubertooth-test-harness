"""A required step whose own hardware is missing is blocked, not left out.

#265 made a step for hardware this registry has not got not applicable, so
one plan can be compiled against every bench. Two absences are not like
that: `alternate Ubertooths` with one registered board, and
`exclusive Ubertooth` where another board is on no power controller. The
missing hardware *is* the test, so leaving the step out quietly would read
as a pass for a gate nobody measured. Run planning records it as blocked,
compiles and runs everything unrelated, and the run fails (#269).
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.test_cli import make_source
from ubertooth_harness import cli
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler, PlanningError, TestInvocation
from ubertooth_harness.resources import (
    FileResourceRegistry,
    MissingRequiredHardware,
    RegistryError,
)
from ubertooth_harness.topology import plan_exclusive_ubertooth


A, B = "a" * 32, "b" * 32
KGV = "e" * 64

# One Ubertooth per host, and a hub that can switch the Linux host's board
# off. So `alternate Ubertooths` has no second board on either host, and
# `exclusive Ubertooth` has nothing to switch.
ONE_BOARD = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults: {Ubertooth: UT_Linux0}
  Host_Mac:
    type: Host
    platform: macos
    roles: [test target]
    defaults: {Ubertooth: UT_Mac0}
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
    power: {controller: Hub_Linux0, port: 0}
  UT_Mac0:
    type: Ubertooth
    attached to: Host_Mac
    identity: {serial: "cccccccccccccccccccccccccccccccc"}
  Hub_Linux0:
    type: USB power controller
    attached to: Host_Linux
    identity: {serial: "0x12345678"}
    driver: acroname BrainStem USBHub3+
    capabilities: [per-port VBUS switching, power readback]
"""

# A second board on the Linux host that no controller can switch off.
UNMAPPED_SECOND_BOARD = """  UT_Linux1:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
"""

MAPPED_SECOND_BOARD = """  UT_Linux1:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
    power: {controller: Hub_Linux0, port: 1}
"""

# Binds no hardware and runs on every platform, so a suite always has
# something to run and the blocked step is what the assertions are about.
HOST_STEP = """    - Host CLI surface:
        hosts: each
"""

EXCLUSIVE_STEP = """    - Exclusive Ubertooth topology:
        hosts: [Host_Linux]
        platforms: [linux]
        Ubertooth: default
        exclusive Ubertooth: true
        capture duration: 5s
"""

MAC_POLLING_STEP = """    - MAC polling soak:
        hosts: [Host_Linux]
        platforms: [linux]
        Ubertooth: default
        advertiser: DK_Linux0
        exclusive Ubertooth: true
        duration: 1h
        query every: 1s
        log every: 1s
        bucket count: 5
        bucket rotation: 2s
        listener silence timeout: 10s
        empty buckets restart after: 15s
        restart suppression: 5s
        restart timeout extension: 20s
        post-restart wait: 10s
        search timeout: 120s
        maximum consecutive misses: 1
        maximum restarts: 0
"""

DK = """  DK_Linux0:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [advertiser, central]
    identity: {jlink serial: "000123456789", BLE address: "C0:00:00:00:00:02"}
    capabilities: [BLE central, DLE]
"""


def flash_step(*hosts: str) -> str:
    """A `Flash reliability` step for the named hosts. Alternation names its
    hosts by alias (`each` is refused when the plan loads), so the platform
    of the host running a dry run decides which alias the step carries."""
    platforms = "[" + ", ".join(
        "linux" if host == "Host_Linux" else "macos" for host in hosts
    ) + "]"
    return f"""    - Flash reliability:
        hosts: [{", ".join(hosts)}]
        platforms: {platforms}
        Ubertooth: default
        flashes: 2
        image sequence: [test build, KGV]
        KGV sha256: {KGV}
        KGV revision: 2020-12-R1
        alternate Ubertooths: true
"""


FLASH_STEP = flash_step("Host_Linux")
# Stands in for a `Flash reliability` step naming whichever host the machine
# running the tests is, which is the only host a dry run may use.
FLASH_PLACEHOLDER = "<flash step for this platform's host>"


def plan_text(*steps: str) -> str:
    return "plan: required hardware\nsuites:\n  release:\n" + "".join(steps)


def write(directory, steps, hardware=ONE_BOARD):
    root = Path(directory)
    (root / "hardware.yaml").write_text(hardware)
    (root / "plan.yaml").write_text(plan_text(*steps))
    return root


class BlockedPlanningTests(unittest.TestCase):

    def compile(self, directory, steps, hardware=ONE_BOARD, strict=False):
        root = write(directory, steps, hardware)
        return PlanCompiler(
            builtin_catalog(), FileResourceRegistry.load(root / "hardware.yaml")
        ).compile_suite(load_plan(root / "plan.yaml"), "release", strict_hardware=strict)

    def test_alternation_on_a_one_board_host_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(
                directory, (HOST_STEP, flash_step("Host_Linux", "Host_Mac"))
            )

            self.assertEqual(
                ("Host CLI surface", "Host CLI surface"),
                tuple(item.test_name for item in compiled.invocations),
            )
            self.assertEqual((), compiled.not_applicable)
            self.assertEqual(
                [
                    ("Host_Linux", "Flash reliability",
                     "alternate Ubertooths needs at least two registered "
                     "Ubertooths on Host_Linux, which has one"),
                    ("Host_Mac", "Flash reliability",
                     "alternate Ubertooths needs at least two registered "
                     "Ubertooths on Host_Mac, which has one"),
                ],
                [(item.host, item.test_name, item.reason)
                 for item in compiled.blocked],
            )

    def test_the_host_that_has_two_boards_runs_it_and_the_other_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(
                directory, (flash_step("Host_Linux", "Host_Mac"),),
                ONE_BOARD + MAPPED_SECOND_BOARD,
            )

            (invocation,) = compiled.invocations
            self.assertEqual("Host_Linux", invocation.host)
            (blocked,) = compiled.blocked
            self.assertEqual("Host_Mac", blocked.host)

    def test_an_extra_board_on_no_power_controller_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(
                directory, (HOST_STEP, EXCLUSIVE_STEP),
                ONE_BOARD + UNMAPPED_SECOND_BOARD,
            )

            self.assertEqual(
                ("Host CLI surface", "Host CLI surface"),
                tuple(item.test_name for item in compiled.invocations),
            )
            (blocked,) = compiled.blocked
            self.assertEqual("Host_Linux", blocked.host)
            self.assertEqual("Exclusive Ubertooth topology", blocked.test_name)
            self.assertEqual(
                "extra Ubertooth UT_Linux1 has no power mapping, so it cannot "
                "be switched off to leave UT_Linux0 alone",
                blocked.reason,
            )
            self.assertEqual(
                "Exclusive Ubertooth topology: blocked on Host_Linux: extra "
                "Ubertooth UT_Linux1 has no power mapping, so it cannot be "
                "switched off to leave UT_Linux0 alone",
                blocked.describe(),
            )

    def test_a_mapped_second_board_compiles_as_before(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(
                directory, (HOST_STEP, EXCLUSIVE_STEP),
                ONE_BOARD + MAPPED_SECOND_BOARD,
            )

            self.assertEqual((), compiled.blocked)
            exclusive = next(
                item for item in compiled.invocations
                if item.test_name == "Exclusive Ubertooth topology"
            )
            (action,) = exclusive.power_isolation
            self.assertEqual("UT_Linux1", action.resource_alias)
            self.assertEqual("Hub_Linux0", action.controller_alias)

    def test_each_blocked_record_belongs_to_its_own_host(self):
        """Another bench's missing hardware is not this run's failure."""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(
                directory, (HOST_STEP, EXCLUSIVE_STEP, flash_step("Host_Mac")),
                ONE_BOARD + UNMAPPED_SECOND_BOARD,
            )

            self.assertEqual(
                ["Exclusive Ubertooth topology"],
                [item.test_name
                 for item in compiled.blocked_for_host("Host_Linux")],
            )
            self.assertEqual(
                ["Flash reliability"],
                [item.test_name for item in compiled.blocked_for_host("Host_Mac")],
            )

    def test_the_invocation_ids_of_the_steps_that_do_run_stay_consecutive(self):
        """A blocked step takes no sequence number, so the IDs a reader and
        the manifest match on do not skip one."""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(
                directory,
                (flash_step("Host_Mac"), HOST_STEP, EXCLUSIVE_STEP),
                ONE_BOARD + UNMAPPED_SECOND_BOARD,
            )

            self.assertEqual(2, len(compiled.blocked))
            self.assertEqual(
                ["001-Host CLI surface", "002-Host CLI surface"],
                [item.invocation_id for item in compiled.invocations],
            )

    def test_a_suite_with_only_a_blocked_step_names_it(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError,
                "suite 'release' produced no runnable invocations; Flash "
                "reliability: blocked on ",
            ):
                self.compile(directory, (FLASH_STEP,))

    def test_a_blocked_step_is_named_before_a_not_applicable_one(self):
        """What this bench was meant to measure and cannot comes first."""
        absent = """    - Ubertooth identity:
        hosts: [Host_Lab]
        Ubertooth: default
"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError,
                r"blocked on .*; Ubertooth identity: not applicable on Host_Lab",
            ):
                self.compile(directory, (FLASH_STEP, absent))


class ConfigurationErrorTests(unittest.TestCase):
    """A registry that is wrong, rather than one that is missing hardware,
    is still an error nobody may run past."""

    def mapping(self, controller="Hub_Linux0", port=1):
        return (
            """  UT_Linux1:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
"""
            f"    power: {{controller: {controller}, port: {port}}}\n"
        )

    def load(self, hardware):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hardware.yaml").write_text(hardware)
            return FileResourceRegistry.load(root / "hardware.yaml")

    def test_a_malformed_power_mapping_is_refused_when_the_registry_loads(self):
        """Every way of writing the mapping wrongly, so none of them can
        reach planning and be mistaken for hardware this bench has not got."""
        cases = {
            "has unknown power controller": self.mapping(controller="Hub_Nope"),
            "power controller has wrong type": self.mapping(controller="UT_Mac0"),
            "power port must be 0-7": self.mapping(port=9),
            "power port must be a nonnegative integer":
                self.mapping(port="one"),
        }
        for message, extra in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(RegistryError, message):
                    self.load(ONE_BOARD + extra)

    def test_a_controller_without_the_capabilities_is_refused_at_load(self):
        hardware = ONE_BOARD.replace(
            "    capabilities: [per-port VBUS switching, power readback]",
            "    capabilities: [power readback]",
        )

        with self.assertRaisesRegex(
            RegistryError, "controller Hub_Linux0 lacks required capabilities"
        ):
            self.load(hardware + self.mapping())

    def test_absence_and_a_wrong_registry_are_different_exceptions(self):
        """The type is what decides whether the compile goes on, so the
        absence must not be a plain RegistryError and nothing else may be a
        MissingRequiredHardware."""
        registry = self.load(ONE_BOARD + UNMAPPED_SECOND_BOARD)

        with self.assertRaises(MissingRequiredHardware):
            plan_exclusive_ubertooth(registry, "Host_Linux", "UT_Linux0")
        # A board the plan selected that is not an Ubertooth on this host is
        # the plan's mistake, and stays one.
        with self.assertRaises(RegistryError) as wrong:
            plan_exclusive_ubertooth(registry, "Host_Linux", "Hub_Linux0")
        self.assertNotIsInstance(wrong.exception, MissingRequiredHardware)

    def test_a_short_serial_on_an_alternated_board_fails_planning(self):
        """Not absence: the board is here, and its registered serial is too
        short to select it with."""
        short = MAPPED_SECOND_BOARD.replace(
            '"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"', '"bbbb"'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = write(directory, (HOST_STEP, FLASH_STEP), ONE_BOARD + short)
            compiler = PlanCompiler(
                builtin_catalog(),
                FileResourceRegistry.load(root / "hardware.yaml"),
            )

            with self.assertRaisesRegex(
                PlanningError, "UT_Linux1 needs a full 32-hex-digit serial"
            ):
                compiler.compile_suite(load_plan(root / "plan.yaml"), "release")


class RunRecordTests(unittest.TestCase):
    """What a run records: the blocked step, the tests that still ran, and a
    failed run."""

    def run_suite(self, steps, hardware, host):
        from tests.test_sequencer import CollectSink, FakeExecutor, run_contract
        from ubertooth_harness.sequencer import NamedTestSequencer

        with tempfile.TemporaryDirectory() as directory:
            root = write(directory, steps, hardware)
            registry = FileResourceRegistry.load(root / "hardware.yaml")
            compiled = PlanCompiler(builtin_catalog(), registry).compile_suite(
                load_plan(root / "plan.yaml"), "release"
            )
            invocation = TestInvocation(
                "001-Host version", "Host version", host, {"hosts": "each"}, {},
            )
            manifest, checks = run_contract(root, invocation)

            summary = NamedTestSequencer(
                FakeExecutor(), event_sink=CollectSink()
            ).run(
                (invocation,), root, run_id="named-run", manifest=manifest,
                preflight_checks=checks,
                not_applicable=compiled.for_host(host),
                blocked=compiled.blocked_for_host(host),
            )
            events = [
                json.loads(line)
                for line in (root / "run.jsonl").read_text().splitlines()
            ]
            return summary, events, (root / "report.md").read_text()

    def test_the_run_fails_and_the_other_tests_still_ran(self):
        summary, events, report = self.run_suite(
            (HOST_STEP, EXCLUSIVE_STEP),
            ONE_BOARD + UNMAPPED_SECOND_BOARD,
            "Host_Linux",
        )

        self.assertFalse(summary.passed)
        self.assertEqual("failed", summary.status)
        self.assertEqual(1, summary.blocked_tests)
        # The test that could run did, and is counted as completed.
        self.assertEqual(1, summary.completed_tests)
        self.assertEqual(
            "Exclusive Ubertooth topology: blocked on Host_Linux: extra "
            "Ubertooth UT_Linux1 has no power mapping, so it cannot be "
            "switched off to leave UT_Linux0 alone",
            summary.failure_reason,
        )
        self.assertEqual(
            "passed",
            next(
                event["status"] for event in events
                if event["event_type"] == "test_finished"
            ),
        )
        (blocked,) = [
            event for event in events if event["event_type"] == "blocked"
        ]
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual(
            {
                "host": "Host_Linux",
                "test_name": "Exclusive Ubertooth topology",
                "reason": "extra Ubertooth UT_Linux1 has no power mapping, so "
                          "it cannot be switched off to leave UT_Linux0 alone",
            },
            blocked["details"],
        )
        capabilities = next(
            event["details"] for event in events
            if event["event_type"] == "host_capabilities"
        )
        self.assertEqual(1, capabilities["blocked"])
        self.assertIn("## Blocked", report)
        self.assertIn(
            "| Exclusive Ubertooth topology | Host_Linux | extra Ubertooth "
            "UT_Linux1 has no power mapping, so it cannot be switched off to "
            "leave UT_Linux0 alone |",
            report,
        )
        self.assertIn("1 tests blocked (see Blocked)", report)

    def test_a_blocked_test_and_a_not_applicable_one_are_told_apart(self):
        absent = """    - Ubertooth identity:
        hosts: [Host_Lab]
        Ubertooth: default
"""
        summary, events, report = self.run_suite(
            (HOST_STEP, EXCLUSIVE_STEP, absent),
            ONE_BOARD + UNMAPPED_SECOND_BOARD,
            "Host_Linux",
        )

        self.assertEqual(1, summary.blocked_tests)
        self.assertEqual(1, summary.not_applicable_tests)
        self.assertEqual("failed", summary.status)
        self.assertIn("## Blocked", report)
        self.assertIn("## Not applicable", report)
        # The not-applicable step is not what failed the run.
        self.assertNotIn("Ubertooth identity", summary.failure_reason)

    def test_two_blocked_tests_are_counted_and_the_rest_named(self):
        """Both of the host's exclusive steps want the board switched off,
        so one bench absence blocks two tests."""
        summary, _events, _report = self.run_suite(
            (HOST_STEP, EXCLUSIVE_STEP, MAC_POLLING_STEP),
            ONE_BOARD + UNMAPPED_SECOND_BOARD + DK,
            "Host_Linux",
        )

        self.assertEqual(2, summary.blocked_tests)
        self.assertIn("(and 1 more blocked test)", summary.failure_reason)

    def test_nothing_blocked_still_passes(self):
        summary, events, report = self.run_suite(
            (HOST_STEP, EXCLUSIVE_STEP),
            ONE_BOARD + MAPPED_SECOND_BOARD,
            "Host_Linux",
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(0, summary.blocked_tests)
        self.assertEqual([], [e for e in events if e["event_type"] == "blocked"])
        self.assertNotIn("## Blocked", report)


class GuiTests(unittest.TestCase):
    """The live run view shows a blocked test with the failures."""

    def test_a_blocked_test_is_listed_as_a_failure(self):
        from ubertooth_harness.gui import failure_summary

        failures = failure_summary(
            {"run_id": "r", "status": "failed", "reason": ""},
            [
                {
                    "event_type": "blocked",
                    "status": "blocked",
                    "details": {
                        "host": "Host_Linux",
                        "test_name": "Exclusive Ubertooth topology",
                        "reason": "extra Ubertooth UT_Linux1 has no power mapping",
                    },
                },
            ],
        )

        self.assertEqual(
            [
                ("Exclusive Ubertooth topology", "blocked",
                 "extra Ubertooth UT_Linux1 has no power mapping"),
                # The run's own failure row, which every failed run carries.
                ("run", "failed", ""),
            ],
            [(item["step"], item["status"], item["reason"]) for item in failures],
        )


class DryRunTests(unittest.TestCase):
    """`run-suite --dry-run` says what would run, and exits 1 when a required
    test could not."""

    def dry_run(self, *steps, hardware=ONE_BOARD):
        host = "Host_Mac" if sys.platform == "darwin" else "Host_Linux"
        steps = tuple(
            flash_step(host) if step is FLASH_PLACEHOLDER else step
            for step in steps
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _short = make_source(root)
            (root / "plan.yaml").write_text(plan_text(*steps))
            (root / "hardware.yaml").write_text(hardware)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main([
                    "run-suite", "--dry-run", "--plan", str(root / "plan.yaml"),
                    "--suite", "release", "--hardware", str(root / "hardware.yaml"),
                    "--host", host, "--ubertooth-source", str(source),
                    "--ubertooth-ref", "HEAD", "--output", str(root / "runs"),
                    "--lease-dir", str(root / "leases"),
                ])
            return code, out.getvalue(), err.getvalue(), host

    def test_a_blocked_step_is_named_and_the_dry_run_fails(self):
        code, text, errors, host = self.dry_run(HOST_STEP, FLASH_PLACEHOLDER)

        self.assertEqual(1, code)
        self.assertIn(
            f"Flash reliability: blocked on {host}: alternate Ubertooths "
            f"needs at least two registered Ubertooths on {host}",
            text,
        )
        self.assertIn("1 test(s) blocked", text)
        self.assertIn(
            f"error: 1 required test(s) cannot run on {host}", errors
        )

    def test_a_dry_run_with_nothing_blocked_still_exits_zero(self):
        code, text, _errors, _host = self.dry_run(HOST_STEP)

        self.assertEqual(0, code)
        self.assertNotIn("blocked", text)


class ValidatePlanTests(unittest.TestCase):
    """`validate-plan --hardware` is the strict one: the bench is named on
    purpose, so absence is a refusal, as it is for every other absence."""

    def validate(self, steps, hardware=ONE_BOARD):
        with tempfile.TemporaryDirectory() as directory:
            root = write(directory, steps, hardware)
            argv = [
                "validate-plan", str(root / "plan.yaml"), "--suite", "release",
                "--hardware", str(root / "hardware.yaml"),
            ]
            with mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
                code = cli.main(argv)
            printed = "".join(call.args[0] for call in out.write.call_args_list)
            errors = "".join(call.args[0] for call in err.write.call_args_list)
            return code, printed, errors

    def test_a_one_board_host_fails_validation(self):
        code, _printed, errors = self.validate((HOST_STEP, FLASH_STEP))

        self.assertEqual(2, code)
        self.assertIn(
            "error: alternate Ubertooths needs at least two registered "
            "Ubertooths on",
            errors,
        )

    def test_an_unmapped_extra_board_fails_validation(self):
        code, _printed, errors = self.validate(
            (HOST_STEP, EXCLUSIVE_STEP), ONE_BOARD + UNMAPPED_SECOND_BOARD
        )

        self.assertEqual(2, code)
        self.assertIn(
            "error: extra Ubertooth UT_Linux1 has no power mapping", errors
        )

    def test_a_bench_with_the_hardware_validates(self):
        code, printed, errors = self.validate(
            (HOST_STEP, EXCLUSIVE_STEP), ONE_BOARD + MAPPED_SECOND_BOARD
        )

        self.assertEqual(0, code, errors)
        self.assertIn("compiled release: 3 invocation(s)", printed)


if __name__ == "__main__":
    unittest.main()
