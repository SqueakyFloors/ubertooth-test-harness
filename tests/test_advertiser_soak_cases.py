"""`BLE address parity soak`: its cases, its arguments, and its plan.

No hardware and no driver run: what is checked here is the command the case
builds, the deadline it gives it, the relationships the catalog refuses when
a plan loads, and that the committed plan compiles against the example bench
and leases the boards it names.
"""

from pathlib import Path
import tempfile
import unittest

from ubertooth_harness import addresssoak, advertiser_soak_cases, leases
from ubertooth_harness.advertiser_soak_cases import (
    TEST_NAME,
    address_soak_cases,
    step_timeout,
    timings,
)
from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.stateful import CAPTURE_DIR_KEY, StateCaseError

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "testplans" / "address-soak.yaml"
SERIAL = "0123456789abcdef0123456789abcdef"
IDENTITIES = {
    "Ubertooth": {"serial": SERIAL},
    "advertiser": {"jlink serial": "001050000001", "BLE address": "c3:11:22:33:44:55"},
}
CONTEXT = {CAPTURE_DIR_KEY: "/run/captures/soak"}


def arguments(**extra):
    raw = {"duration": "4h", **extra}
    return builtin_catalog().get(TEST_NAME).normalize_arguments(raw)


def options(argv):
    """The driver's own options, parsed from the argv the case built."""
    return addresssoak.options_from(
        addresssoak.build_parser().parse_args([str(item) for item in argv[2:]]))


class CaseTests(unittest.TestCase):

    def cases(self, **extra):
        return address_soak_cases(
            arguments(**extra), {"Ubertooth": "UT_Linux0", "advertiser": "DK_Linux0"},
            IDENTITIES)

    def test_two_cases_the_advertiser_check_and_the_soak(self):
        self.assertEqual(["advertiser-ready", "address-parity-soak"],
                         [case.case_id for case in self.cases()])

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
        # The DK is released first, then the board is stopped and restored.
        self.assertEqual("advertiser-idle", soak.cleanup[0].step_id)
        self.assertEqual("cleanup-stop", soak.cleanup[1].step_id)
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
            "duration": "1h", "channel": 39, "interval": 250, "payload": "aabb",
            "advertiser on": "20s", "advertiser off": "40s", "sample every": "2m",
            "presence window": "3s", "presence windows": 4,
            "liveness timeout": "90s", "detect within": "15s",
            "minimum detection share": 0.8, "maximum gap": "15s",
            "minimum packets per minute": 25,
            "maximum window disagreement share": 0.1, "maximum restarts": 2,
            "maximum memory growth": 4096, "restart probe": True,
            "capture file limit": 64, "capture budget": 512,
        })[1].steps[-1].resolve_argv(CONTEXT)
        self.assertTrue(argv[1].endswith("addresssoak.py"))
        found = options(argv)
        self.assertEqual(Path("/run/captures/soak"), found.out)
        self.assertEqual(SERIAL, found.serial)
        self.assertEqual("C3:11:22:33:44:55", found.address)
        self.assertEqual(39, found.channel)
        self.assertEqual(3600.0, found.duration_s)
        self.assertEqual("1050000001", found.jlink)
        self.assertEqual(250, found.interval_ms)
        self.assertEqual("aabb", found.payload)
        self.assertEqual((20.0, 40.0), (found.on_s, found.off_s))
        self.assertEqual(120.0, found.sample_every_s)
        self.assertEqual((3.0, 4), (found.window_s, found.windows_needed))
        self.assertEqual(90.0, found.liveness_timeout_s)
        self.assertEqual((15.0, 0.8),
                         (found.detect_within_s, found.min_detection_share))
        self.assertEqual((15.0, 25), (found.max_gap_s, found.min_packets_per_minute))
        self.assertEqual(0.1, found.max_disagreement_share)
        self.assertEqual(2, found.max_restarts)
        self.assertEqual(4096, found.max_memory_growth_kb)
        self.assertTrue(found.restart_probe)
        self.assertEqual(64 * 1024 * 1024, found.capture_limit_bytes)
        self.assertEqual(512 * 1024 * 1024, found.capture_budget_bytes)

    def test_the_defaults_leave_the_optional_figures_out(self):
        argv = self.cases()[1].steps[-1].resolve_argv(CONTEXT)
        self.assertNotIn("--max-memory-growth-kb", argv)
        self.assertNotIn("--restart-probe", argv)
        self.assertIn("--dk-alias", argv)  # the registry alias, so its shell is logged
        self.assertNotIn("--dk-port", argv)
        found = options(argv)
        self.assertIsNone(found.max_memory_growth_kb)
        self.assertFalse(found.restart_probe)
        self.assertEqual(256 * 1024 * 1024, found.capture_limit_bytes)

    def test_a_payload_of_none_reaches_the_driver_as_none(self):
        argv = self.cases(payload="none")[1].steps[-1].resolve_argv(CONTEXT)
        self.assertIn("none", argv)
        self.assertEqual("", options(argv).payload)

    def test_a_registered_serial_port_is_passed(self):
        identities = {**IDENTITIES, "advertiser": {**IDENTITIES["advertiser"],
                                                   "serial device": "/dev/cu.dk0"}}
        argv = address_soak_cases(arguments(), {}, identities)[1].steps[-1].resolve_argv(
            CONTEXT)
        self.assertEqual(["--dk-port", "/dev/cu.dk0"], list(argv[-2:]))

    def test_without_an_alias_no_option_is_passed(self):
        argv = address_soak_cases(arguments(), {}, IDENTITIES)[1].steps[-1].resolve_argv(
            CONTEXT)
        self.assertNotIn("--dk-alias", argv)

    def test_the_advertiser_identity_is_checked_as_every_dk_test_checks_it(self):
        for identity, reason in (
            ({}, "requires an advertiser"),
            ({"jlink serial": "x", "BLE address": "C3:11:22:33:44:55"}, "J-Link serial"),
            ({"jlink serial": "1050000001", "BLE address": "43:11:22:33:44:55"},
             "not a static random"),
        ):
            with self.assertRaisesRegex(StateCaseError, reason):
                address_soak_cases(arguments(), {},
                                   {**IDENTITIES, "advertiser": identity})

    def test_a_registry_with_no_ubertooth_serial_is_refused(self):
        with self.assertRaisesRegex(StateCaseError, "requires a registered serial"):
            address_soak_cases(arguments(), {},
                               {"advertiser": IDENTITIES["advertiser"]})


class DeadlineTests(unittest.TestCase):
    """The step's deadline is the window plus what the run spends outside it."""

    def test_the_deadline_is_the_duration_plus_the_setup_allowance(self):
        self.assertEqual(3600.0 + advertiser_soak_cases.SETUP_ALLOWANCE_S,
                         step_timeout(arguments(duration="1h")))

    def test_each_allowed_restart_and_the_probe_add_their_own_allowance(self):
        base = step_timeout(arguments(duration="1h"))
        self.assertEqual(base + 2 * advertiser_soak_cases.RESTART_ALLOWANCE_S,
                         step_timeout(arguments(duration="1h",
                                                **{"maximum restarts": 2})))
        self.assertEqual(base + advertiser_soak_cases.PROBE_ALLOWANCE_S,
                         step_timeout(arguments(duration="1h",
                                                **{"restart probe": True})))

    def test_every_committed_suite_gets_a_deadline_past_its_window(self):
        plan = load_plan(PLAN, builtin_catalog())
        for suite, steps in plan.suites.items():
            with self.subTest(suite=suite):
                values = steps[0].arguments
                self.assertGreater(
                    step_timeout(values),
                    advertiser_soak_cases.seconds(values["duration"]))

    def test_the_step_carries_that_deadline(self):
        values = arguments(duration="4h")
        step = address_soak_cases(values, {}, IDENTITIES)[1].steps[-1]
        self.assertEqual(step_timeout(values), step.timeout_s)


class ArgumentTests(unittest.TestCase):
    """What the catalog refuses before a board is leased."""

    def test_the_timings_a_plan_sets_are_what_the_driver_checks(self):
        values = arguments(duration="4h")
        found = timings(values)
        self.assertEqual(addresssoak.Timings(
            duration_s=14400.0, on_s=30.0, off_s=30.0, window_s=2.0,
            windows_needed=5, liveness_timeout_s=60.0, detect_within_s=10.0,
            max_gap_s=15.0, sample_every_s=30.0, interval_ms=100,
            min_packets_per_minute=10), found)
        addresssoak.check_timings(found)

    def test_every_relationship_the_driver_refuses_fails_the_plan(self):
        for extra, reason in (
            ({"detect within": "40s"}, "is longer than the 30s"),
            ({"presence windows": 40}, "sustained presence"),
            ({"liveness timeout": "30s"},
             "an off period and the wait for the next detection"),
            ({"advertiser on": "500ms"}, "'advertiser on' (0.5s) is shorter"),
            ({"maximum gap": "5s"}, "is shorter than 'detect within'"),
            ({"maximum gap": "30s"}, "is not shorter than the 30s"),
            ({"presence window": "50ms"}, "'presence window' (0.05s) is shorter"),
            ({"duration": "1m"}, "less than two 60s schedule cycles"),
            ({"sample every": "2h"}, "leaves fewer than three samples"),
            ({"sample every": "500ms"}, "'sample every' (0.5s) is shorter"),
        ):
            with self.subTest(**extra):
                with self.assertRaises(CatalogError) as caught:
                    arguments(**extra)
                self.assertIn(TEST_NAME, str(caught.exception))
                self.assertIn(reason, str(caught.exception))

    def test_the_values_each_argument_takes(self):
        for extra in ({"channel": 36}, {"channel": 40}, {"interval": 19},
                      {"payload": "abc"}, {"presence windows": 0},
                      {"minimum detection share": 0}, {"minimum detection share": 1.5},
                      {"maximum window disagreement share": -0.1},
                      {"maximum restarts": -1}, {"maximum memory growth": 0},
                      {"restart probe": "yes"}, {"capture file limit": 0},
                      {"capture budget": 0}, {"capture budget": -1},
                      {"advertiser on": "30"}, {"sample every": "0s"}):
            with self.subTest(**extra):
                with self.assertRaises(CatalogError):
                    arguments(**extra)

    def test_a_share_may_be_written_as_a_whole_number_of_one(self):
        self.assertEqual(1.0, arguments(**{"minimum detection share": 1})[
            "minimum detection share"])

    def test_the_test_takes_no_coverage_profile(self):
        definition = builtin_catalog().get(TEST_NAME)
        self.assertNotIn("coverage", definition.arguments)
        # And every test that binds hardware takes these three.
        for name in ("platforms", "rf", "required hardware"):
            self.assertIn(name, definition.arguments)

    def test_the_test_is_declared_as_one_that_writes_dk_firmware(self):
        definition = builtin_catalog().get(TEST_NAME)
        self.assertTrue(definition.writes_firmware)
        self.assertTrue(definition.guards_other_ubertooths)
        # It does not write the Ubertooth's firmware, so preflight still
        # requires the resolved revision on the board.
        self.assertFalse(definition.establishes_firmware)


class PlanTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()
        cls.plan = load_plan(PLAN, cls.catalog)
        cls.registry = FileResourceRegistry.load(ROOT / "hardware.example.yaml")

    def test_the_four_suites_are_the_documented_durations(self):
        self.assertEqual(
            ["address-parity", "address-soak-4h", "address-soak-24h",
             "address-soak-72h"],
            list(self.plan.suites))
        durations = {}
        for name, steps in self.plan.suites.items():
            (step,) = steps
            self.assertEqual(TEST_NAME, step.test_name)
            durations[name] = step.arguments["duration"]
        self.assertEqual({"address-parity": "1m", "address-soak-4h": "4h",
                          "address-soak-24h": "24h", "address-soak-72h": "72h"},
                         durations)

    def test_only_the_short_suite_carries_the_restart_probe(self):
        probes = {name: steps[0].arguments["restart probe"]
                  for name, steps in self.plan.suites.items()}
        self.assertEqual({"address-parity": True, "address-soak-4h": False,
                          "address-soak-24h": False, "address-soak-72h": False},
                         probes)

    def test_every_suite_allows_no_unplanned_restart(self):
        for name, steps in self.plan.suites.items():
            with self.subTest(suite=name):
                self.assertEqual(0, steps[0].arguments["maximum restarts"])

    def test_the_soak_is_in_no_other_plan(self):
        # It is minutes at its shortest and three days at its longest, so it
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
                self.assertEqual("DK_Linux0", invocation.resources["advertiser"])
                cases = self.catalog.get(invocation.test_name).command_cases(
                    invocation.arguments, invocation.resources,
                    invocation.resource_identities)
                self.assertEqual(2, len(cases))

    def test_the_run_leases_the_board_and_the_advertiser(self):
        registry = registry_with(REGISTRY)
        invocations = PlanCompiler(self.catalog, registry).compile(
            self.plan, "address-parity")
        boards = leases.boards_for_invocations(invocations, registry)
        self.assertEqual([("UT_Linux0", SERIAL), ("DK_Linux0", "nrf52840dk-1050000001")],
                         [(board.alias, board.key) for board in boards])


REGISTRY = f"""
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [test target]
    defaults:
      Ubertooth: UT_Linux0
      nRF52840 DK: DK_Linux0
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {{serial: {SERIAL}}}
  DK_Linux0:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [advertiser]
    identity:
      jlink serial: "001050000001"
      BLE address: "C3:11:22:33:44:55"
"""


def registry_with(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "hardware.yaml"
        path.write_text(text, encoding="utf-8")
        return FileResourceRegistry.load(path)


if __name__ == "__main__":
    unittest.main()
