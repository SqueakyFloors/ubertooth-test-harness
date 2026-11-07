"""`Intermittent follow soak`: its cases, its arguments, and its plan.

No hardware and no driver run: what is checked here is the command the case
builds, the deadline it gives it, the relationships the catalog refuses when
a plan loads, that the committed plan compiles against the example bench and
leases exactly the three devices it drives, and the two properties #368 asks
for -- no guard boards and no firmware write, so the soak can hold its own
devices for days without holding the host.
"""

from pathlib import Path
import tempfile
import unittest

from ubertooth_harness import follow_soak_cases, followsoak, leases, nrfdk
from ubertooth_harness.follow_soak_cases import (
    TEST_NAME,
    follow_soak_cases as build_cases,
    step_timeout,
    timings,
)
from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.stateful import CAPTURE_DIR_KEY, StateCaseError

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "testplans" / "follow-soak.yaml"
SERIAL = "0123456789abcdef0123456789abcdef"
CENTRAL = "c0:00:00:00:00:02"
PERIPHERAL = "c0:00:00:00:00:03"
IDENTITIES = {
    "Ubertooth": {"serial": SERIAL},
    "central": {"jlink serial": "001050000002", "BLE address": CENTRAL},
    "peripheral": {"jlink serial": "001050000003", "BLE address": PERIPHERAL},
}
RESOURCES = {"Ubertooth": "UT_Linux0", "central": "DK_Linux1",
             "peripheral": "DK_Linux2"}
CONTEXT = {CAPTURE_DIR_KEY: "/run/captures/follow-soak"}


def arguments(**extra):
    raw = {"duration": "4h", **extra}
    return builtin_catalog().get(TEST_NAME).normalize_arguments(raw)


def argv_of(step, context=None):
    """A step's command, whether it is a fixed tuple or built from context."""
    context = CONTEXT if context is None else context
    return list(step.argv(context) if callable(step.argv) else step.argv)


def options(argv):
    """The driver's own options, parsed from the argv the case built."""
    return followsoak.options_from(
        followsoak.build_parser().parse_args([str(item) for item in argv[2:]]))


class CaseTests(unittest.TestCase):

    def cases(self, **extra):
        return build_cases(arguments(**extra), RESOURCES, IDENTITIES)

    def test_two_cases_the_fixture_check_and_the_soak(self):
        self.assertEqual(["fixture-ready", "intermittent-follow-soak"],
                         [case.case_id for case in self.cases()])

    def test_the_fixture_case_checks_both_dks_and_never_flashes(self):
        fixture = self.cases()[0]
        self.assertEqual(
            ["identify-peripheral", "identify-central", "ready-peripheral",
             "ready-central"],
            [step.step_id for step in fixture.steps])
        # No artifact is accepted and no image reaches a DK: a test that
        # flashed would make run-suite hold the host lock for the window.
        self.assertEqual((), fixture.optional_artifacts)
        for step in fixture.steps:
            with self.subTest(step.step_id):
                self.assertNotIn("--hex", argv_of(step))
                self.assertNotIn("ensure", argv_of(step))
        # It leaves both boards idle in the advertiser role.
        self.assertEqual(["peripheral-idle", "central-idle"],
                         [step.step_id for step in fixture.cleanup])

    def test_the_ready_step_requires_the_pinned_firmware_role_and_address(self):
        (peripheral,) = [step for step in self.cases()[0].steps
                         if step.step_id == "ready-peripheral"]
        self.assertEqual(
            (rf"(?m)^firmware: {nrfdk.FIRMWARE_VERSION}$",
             r"(?m)^role: peripheral$",
             rf"(?m)^address: {PERIPHERAL.upper()}$"),
            peripheral.output_patterns)
        self.assertIn("role", argv_of(peripheral))
        self.assertIn("--role", argv_of(peripheral))
        self.assertIn("peripheral", argv_of(peripheral))

    def test_the_soak_case_reads_and_restores_the_board(self):
        soak = self.cases()[1]
        # Every setting ubertooth-util can write back, because the recovery
        # ladder may run a full reset, which also returns the PA level.
        self.assertEqual(
            ["establish-idle", "read-original-channel", "read-original-pa-level",
             "read-original-squelch", "read-original-leds", "soak"],
            [step.step_id for step in soak.steps])
        self.assertIn("restore-pa-level",
                      [step.step_id for step in soak.cleanup])
        # Both DKs are released first, then the board is stopped and restored.
        self.assertEqual(["peripheral-idle", "central-idle", "cleanup-stop"],
                         [step.step_id for step in soak.cleanup[:3]])
        self.assertEqual("restored-device-identity", soak.cleanup[-1].step_id)
        self.assertTrue(soak.capture_dir)

    def test_the_step_requires_the_verdict_and_forbids_a_failure_line(self):
        step = self.cases()[1].steps[-1]
        self.assertEqual((r"(?m)^SOAK VERDICT: pass$",), step.output_patterns)
        self.assertEqual((r"(?m)^FAIL: ",), step.forbidden_patterns)
        self.assertTrue(step.mutates)
        self.assertFalse(step.continuous)

    def test_the_driver_is_given_every_figure_the_plan_set(self):
        argv = self.cases(**{
            "duration": "1h", "channel": 39, "advertising": "30s",
            "connected": "40s", "absent": "30s", "checkpoint every": "10m",
            "sample every": "2m", "advertising interval": 250,
            "connection interval": 50, "write bytes": 100, "write period": 20,
            "presence window": "3s", "presence windows": 4,
            "liveness timeout": "90s", "detect within": "15s",
            "minimum detection share": 0.8, "connect within": "15s",
            "acquire within": "20s", "minimum follow share": 0.6,
            "minimum connection packets": 5, "quiet after": "4s",
            "maximum gap": "20s", "maximum restarts": 2,
            "maximum memory growth": 4096, "capture file limit": 32,
            "capture budget": 512,
        })[1].steps[-1].argv(CONTEXT)
        found = options(argv)
        self.assertEqual(Path("/run/captures/follow-soak"), found.out)
        self.assertEqual(SERIAL, found.serial)
        self.assertEqual(39, found.channel)
        self.assertEqual(3600.0, found.duration_s)
        self.assertEqual((30.0, 40.0, 30.0),
                         (found.advertising_s, found.connected_s, found.absent_s))
        self.assertEqual(600.0, found.checkpoint_every_s)
        self.assertEqual(120.0, found.sample_every_s)
        self.assertEqual((250, 50, 100, 20),
                         (found.adv_interval_ms, found.conn_interval_ms,
                          found.write_bytes, found.write_period_ms))
        self.assertEqual((3.0, 4), (found.window_s, found.windows_needed))
        self.assertEqual(90.0, found.liveness_timeout_s)
        self.assertEqual((15.0, 0.8),
                         (found.detect_within_s, found.min_detection_share))
        self.assertEqual((15.0, 20.0, 0.6, 5),
                         (found.connect_within_s, found.acquire_within_s,
                          found.min_follow_share, found.min_connection_packets))
        self.assertEqual((4.0, 20.0), (found.quiet_after_s, found.max_gap_s))
        self.assertEqual(2, found.max_restarts)
        self.assertEqual(4096, found.max_memory_growth_kb)
        self.assertEqual((32 * 1024 * 1024, 512 * 1024 * 1024),
                         (found.capture_limit_bytes, found.capture_budget_bytes))
        # Both DKs are named, and the addresses are normalized the one way.
        self.assertEqual(PERIPHERAL.upper(), found.peripheral_address)
        self.assertEqual(CENTRAL.upper(), found.central_address)
        self.assertEqual("1050000003", found.peripheral_jlink)
        self.assertEqual("1050000002", found.central_jlink)

    def test_the_defaults_leave_the_optional_figure_out(self):
        argv = self.cases()[1].steps[-1].argv(CONTEXT)
        self.assertNotIn("--max-memory-growth-kb", argv)
        self.assertIsNone(options(argv).max_memory_growth_kb)

    def test_a_registered_serial_port_is_passed_for_each_dk(self):
        identities = {
            **IDENTITIES,
            "central": {**IDENTITIES["central"], "serial device": "/dev/dk-1"},
            "peripheral": {**IDENTITIES["peripheral"],
                           "serial device": "/dev/dk-2"},
        }
        argv = build_cases(arguments(), RESOURCES,
                           identities)[1].steps[-1].argv(CONTEXT)
        self.assertEqual("/dev/dk-2", options(argv).peripheral_port)
        self.assertEqual("/dev/dk-1", options(argv).central_port)
        # And each board's registry alias, so the run's transcript holds the
        # whole conversation with both shells.
        self.assertEqual("DK_Linux2", options(argv).peripheral_alias)
        self.assertEqual("DK_Linux1", options(argv).central_alias)

    def test_one_dk_in_both_roles_is_refused(self):
        identities = {**IDENTITIES, "central": IDENTITIES["peripheral"]}
        with self.assertRaisesRegex(StateCaseError, "needs two DKs"):
            build_cases(arguments(), RESOURCES, identities)

    def test_a_registry_with_no_ubertooth_serial_is_refused(self):
        identities = {key: value for key, value in IDENTITIES.items()
                      if key != "Ubertooth"}
        with self.assertRaisesRegex(StateCaseError, "requires a registered serial"):
            build_cases(arguments(), RESOURCES, identities)

    def test_a_dk_address_that_is_not_static_random_is_refused(self):
        identities = {**IDENTITIES,
                      "peripheral": {**IDENTITIES["peripheral"],
                                     "BLE address": "01:02:03:04:05:06"}}
        with self.assertRaisesRegex(StateCaseError, "static random"):
            build_cases(arguments(), RESOURCES, identities)


class DeadlineTests(unittest.TestCase):

    def test_the_deadline_is_the_duration_plus_the_allowances(self):
        found = step_timeout(arguments(duration="4h"))
        # Four hours, the setup allowance, and one allowance per checkpoint
        # the window has room for (eight at 30 minutes).
        self.assertEqual(
            4 * 3600.0 + follow_soak_cases.SETUP_ALLOWANCE_S
            + 8 * follow_soak_cases.CHECKPOINT_ALLOWANCE_S,
            found)

    def test_each_allowed_restart_adds_its_own_allowance(self):
        without = step_timeout(arguments(duration="4h"))
        with_two = step_timeout(arguments(**{"duration": "4h",
                                             "maximum restarts": 2}))
        self.assertEqual(without + 2 * follow_soak_cases.RESTART_ALLOWANCE_S,
                         with_two)

    def test_a_window_with_room_for_one_checkpoint_still_gets_an_allowance(self):
        # The catalog refuses an interval past half the duration, so a run
        # always has room for two; the floor is here so an odd figure cannot
        # produce a deadline with no checkpoint allowance at all.
        self.assertEqual(1, follow_soak_cases.checkpoints_expected(
            {"duration": "10m", "checkpoint every": "6m"}))

    def test_every_committed_suite_gets_a_deadline_past_its_window(self):
        catalog = builtin_catalog()
        plan = load_plan(PLAN, catalog)
        for suite, steps in plan.suites.items():
            (step,) = steps
            values = catalog.get(TEST_NAME).normalize_arguments(step.arguments)
            with self.subTest(suite=suite):
                self.assertGreater(step_timeout(values),
                                   follow_soak_cases.seconds(values["duration"]))

    def test_the_step_carries_that_deadline(self):
        values = arguments(duration="24h")
        step = build_cases(values, RESOURCES, IDENTITIES)[1].steps[-1]
        self.assertEqual(step_timeout(values), step.timeout_s)


class ArgumentTests(unittest.TestCase):

    def test_the_timings_a_plan_sets_are_what_the_driver_checks(self):
        found = timings(arguments(**{"duration": "4h", "advertising": "25s",
                                     "connected": "35s", "absent": "25s"}))
        self.assertEqual(14400.0, found.duration_s)
        self.assertEqual((25.0, 35.0, 25.0),
                         (found.advertising_s, found.connected_s, found.absent_s))
        self.assertEqual(85.0, found.cycle_s)
        followsoak.check_timings(found)

    def test_every_relationship_the_driver_refuses_fails_the_plan(self):
        # The catalog's check is the driver's, so each refusal reaches a plan
        # before any board is leased. One case per rule, with the words the
        # driver uses.
        for extra, expected in (
                ({"detect within": "30s"},
                 "could end before its own detection deadline"),
                ({"acquire within": "40s"},
                 "could end before its own follow deadline"),
                ({"connect within": "20s"},
                 "would miss the follow deadline measured from the same moment"),
                ({"quiet after": "30s"},
                 "no absent phase would have a stretch to judge"),
                ({"liveness timeout": "45s"},
                 "the cycle's own silence would restart the capture"),
                ({"maximum gap": "5s"},
                 "the wait for the first packet of a phase would fail it"),
                ({"maximum gap": "25s"},
                 "no gap inside a phase could ever reach it"),
                ({"checkpoint every": "80s"},
                 "would interrupt every absent phase"),
                ({"duration": "2m"}, "less than three"),
                ({"advertising interval": 1000},
                 "advertising events in a 2s presence window"),
                ({"minimum connection packets": 1000},
                 "writes the central makes"),
                ({"capture file limit": 8192, "capture budget": 1024},
                 "is what the whole run's captures may cost")):
            with self.subTest(**extra):
                with self.assertRaises(CatalogError) as caught:
                    arguments(**extra)
                self.assertIn(expected, str(caught.exception))

    def test_the_values_each_argument_takes(self):
        for extra, expected in (
                ({"channel": 36}, "advertising channel 37, 38 or 39"),
                ({"channel": 37.0}, "advertising channel 37, 38 or 39"),
                ({"connection interval": 5}, "connection interval of 8-4000 ms"),
                ({"write bytes": 500}, "which is what one link-layer packet"),
                ({"write bytes": 0}, "which is what one link-layer packet"),
                ({"minimum follow share": 0}, "fraction above 0 and at most 1"),
                ({"minimum follow share": 1.5}, "fraction above 0 and at most 1"),
                ({"write period": 0}, "positive"),
                ({"presence windows": 0}, "positive")):
            with self.subTest(**extra):
                with self.assertRaises(CatalogError) as caught:
                    arguments(**extra)
                self.assertIn(expected, str(caught.exception))

    def test_the_test_takes_no_coverage_profile(self):
        definition = builtin_catalog().get(TEST_NAME)
        self.assertNotIn("coverage", definition.arguments)
        # And every test that binds hardware takes these three.
        for name in ("platforms", "rf", "required hardware"):
            self.assertIn(name, definition.arguments)

    def test_the_soak_guards_nothing_and_writes_no_firmware(self):
        # The two properties that let it run beside other work for days
        # (#368). A test that wrote firmware would make run-suite hold the
        # host lock for the whole window; a test that guarded would bind
        # every other Ubertooth on the host.
        definition = builtin_catalog().get(TEST_NAME)
        self.assertFalse(definition.writes_firmware)
        self.assertFalse(definition.guards_other_ubertooths)
        self.assertFalse(definition.switches_power)
        # It does not write the Ubertooth's firmware either, so preflight
        # still requires the resolved revision on the board.
        self.assertFalse(definition.establishes_firmware)

    def test_the_ubertooth_wants_an_ordinary_link(self):
        wanted = builtin_catalog().get(TEST_NAME).placement_requirements(
            arguments())
        self.assertTrue(wanted["Ubertooth"].antenna)
        self.assertEqual(0, wanted["Ubertooth"].max_attenuation_db)


class PlanTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()
        cls.plan = load_plan(PLAN, cls.catalog)
        cls.registry = FileResourceRegistry.load(ROOT / "hardware.example.yaml")

    def test_the_four_suites_are_the_documented_durations(self):
        self.assertEqual(
            ["follow-soak", "follow-soak-4h", "follow-soak-24h",
             "follow-soak-72h"],
            list(self.plan.suites))
        durations = {}
        for name, steps in self.plan.suites.items():
            (step,) = steps
            self.assertEqual(TEST_NAME, step.test_name)
            durations[name] = step.arguments["duration"]
        self.assertEqual({"follow-soak": "5m", "follow-soak-4h": "4h",
                          "follow-soak-24h": "24h", "follow-soak-72h": "72h"},
                         durations)

    def test_every_suite_allows_no_unplanned_restart(self):
        for name, steps in self.plan.suites.items():
            with self.subTest(suite=name):
                self.assertEqual(0, steps[0].arguments["maximum restarts"])

    def test_every_suite_takes_at_least_two_checkpoints(self):
        for name, steps in self.plan.suites.items():
            values = self.catalog.get(TEST_NAME).normalize_arguments(
                steps[0].arguments)
            with self.subTest(suite=name):
                self.assertGreaterEqual(
                    follow_soak_cases.checkpoints_expected(values), 2)

    def test_the_soak_is_in_no_other_plan(self):
        # Five minutes at its shortest and three days at its longest, so it
        # belongs in no plan a release or a quick check runs.
        for path in sorted((ROOT / "testplans").glob("*.yaml")):
            if path == PLAN:
                continue
            plan = load_plan(path, self.catalog)
            with self.subTest(plan=path.name):
                named = [step.test_name for steps in plan.suites.values()
                         for step in steps]
                self.assertNotIn(TEST_NAME, named)

    def test_every_suite_compiles_against_the_example_bench(self):
        for suite in self.plan.suites:
            with self.subTest(suite=suite):
                invocations = PlanCompiler(self.catalog, self.registry).compile(
                    self.plan, suite)
                (invocation,) = invocations
                self.assertEqual("DK_Linux1", invocation.resources["central"])
                self.assertEqual("DK_Linux2", invocation.resources["peripheral"])
                cases = self.catalog.get(invocation.test_name).command_cases(
                    invocation.arguments, invocation.resources,
                    invocation.resource_identities)
                self.assertEqual(2, len(cases))

    def test_the_run_leases_only_the_three_devices_it_drives(self):
        registry = registry_with(REGISTRY)
        invocations = PlanCompiler(self.catalog, registry).compile(
            self.plan, "follow-soak")
        boards = leases.boards_for_invocations(invocations, registry)
        # UT_Linux1 is registered on the same host and is not taken: every
        # command names its board by serial, so there is nothing a guard
        # would protect against, and host-only work or a run on other
        # devices can go on beside this one.
        self.assertEqual(
            [("UT_Linux0", SERIAL),
             ("DK_Linux1", "nrf52840dk-1050000002"),
             ("DK_Linux2", "nrf52840dk-1050000003")],
            [(board.alias, board.key) for board in boards])

    def test_the_run_takes_no_host_lock(self):
        registry = registry_with(REGISTRY)
        invocations = PlanCompiler(self.catalog, registry).compile(
            self.plan, "follow-soak")
        self.assertFalse(leases.needs_host_lock(invocations, self.catalog))


REGISTRY = f"""
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [test target]
    defaults:
      Ubertooth: UT_Linux0
      central: DK_Linux1
      peripheral: DK_Linux2

  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity:
      serial: "{SERIAL}"

  UT_Linux1:
    type: Ubertooth
    attached to: Host_Linux
    identity:
      serial: "fedcba9876543210fedcba9876543210"

  DK_Linux1:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [central]
    identity:
      jlink serial: "001050000002"
      BLE address: "{CENTRAL.upper()}"

  DK_Linux2:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [peripheral]
    identity:
      jlink serial: "001050000003"
      BLE address: "{PERIPHERAL.upper()}"
"""


def registry_with(text: str) -> FileResourceRegistry:
    directory = tempfile.mkdtemp()
    path = Path(directory) / "hardware.yaml"
    path.write_text(text, encoding="utf-8")
    return FileResourceRegistry.load(path)


if __name__ == "__main__":
    unittest.main()
