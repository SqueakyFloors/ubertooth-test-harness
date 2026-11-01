"""Catalog duration estimates (the GUI countdown)."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest

from ubertooth_harness.catalog import (
    DEFAULT_PROFILE,
    ESTIMATES,
    Estimate,
    builtin_catalog,
)
from ubertooth_harness.gui import list_plans, planned_invocations
from ubertooth_harness.manifest import InvocationIdentity
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.stateful import COVERAGE_PROFILES

from tests.test_run_isolation import HARDWARE

REPOSITORY = Path(__file__).resolve().parents[1]
# Plans whose tests are not implemented yet: nothing to estimate.
UNIMPLEMENTED_PLANS = {"backlog"}


def arguments(definition, **raw):
    return definition.normalize_arguments(raw)


class EstimateTests(unittest.TestCase):

    def setUp(self):
        self.catalog = builtin_catalog()

    def test_every_implemented_test_has_an_estimate_for_each_profile(self):
        for name in self.catalog.names:
            definition = self.catalog.get(name)
            with self.subTest(name):
                if not definition.implemented:
                    self.assertNotIn(name, ESTIMATES)
                    continue
                profiles = (
                    set(COVERAGE_PROFILES) if "coverage" in definition.arguments
                    else {DEFAULT_PROFILE}
                )
                self.assertEqual(profiles, set(definition.estimates))
                for estimate in definition.estimates.values():
                    self.assertGreaterEqual(estimate.fixed_s, 0)
                    self.assertGreaterEqual(estimate.per_duration, 0)
                    # Scaling by a duration needs a duration argument.
                    if estimate.per_duration:
                        self.assertTrue(
                            {"duration", "capture duration"} & set(definition.arguments)
                        )
                    if estimate.per_count:
                        self.assertIn(estimate.per_count[0], definition.arguments)

    def test_estimates_name_only_catalog_tests(self):
        self.assertLessEqual(set(ESTIMATES), set(self.catalog.names))

    def test_comprehensive_takes_longer_than_smoke(self):
        for name, profiles in ESTIMATES.items():
            if "smoke" in profiles:
                with self.subTest(name):
                    self.assertLess(profiles["smoke"].fixed_s,
                                    profiles["comprehensive"].fixed_s)

    def test_coverage_selects_the_profile(self):
        definition = self.catalog.get("Argument validation")
        self.assertEqual(15.8, definition.estimated_seconds(
            arguments(definition, coverage="smoke")))
        self.assertEqual(92.2, definition.estimated_seconds(
            arguments(definition, coverage="comprehensive")))

    def test_duration_scales_the_capture_steps(self):
        definition = self.catalog.get("Raw receive")
        # 9 s plus 20 captures of the duration (the release plan: 3s, 69 s).
        self.assertEqual(69.0, definition.estimated_seconds(
            arguments(definition, coverage="comprehensive", duration="3s")))
        self.assertEqual(129.0, definition.estimated_seconds(
            arguments(definition, coverage="comprehensive", duration="6s")))
        self.assertEqual(3.6, definition.estimated_seconds(
            arguments(definition, coverage="smoke", duration="500ms")))

    def test_capture_duration_and_per_flash(self):
        exclusive = self.catalog.get("Exclusive Ubertooth topology")
        self.assertEqual(15.0, exclusive.estimated_seconds(arguments(
            exclusive, platforms=["linux"], **{"exclusive Ubertooth": True})))
        self.assertEqual(70.0, exclusive.estimated_seconds(arguments(
            exclusive, platforms=["linux"],
            **{"exclusive Ubertooth": True, "capture duration": "1m"})))
        flash = self.catalog.get("Flash reliability")
        raw = {
            "platforms": ["macos"], "flashes": 4, "image sequence": ["KGV", "test build"],
            "KGV sha256": "e" * 64, "KGV revision": "r",
        }
        # The load runs: four flashes in 10.4-10.7 s.
        self.assertEqual(10.6, flash.estimated_seconds(arguments(flash, **raw)))
        self.assertEqual(265.0, flash.estimated_seconds(
            arguments(flash, **{**raw, "flashes": 100})))

    def test_unimplemented_tests_run_for_their_duration(self):
        monitor = self.catalog.get("BLE monitor")
        self.assertEqual(10, monitor.estimated_seconds(arguments(monitor, duration="10s")))
        soak = self.catalog.get("BLE soak")
        self.assertEqual(1800, soak.estimated_seconds(
            arguments(soak, duration="30m", **{"sample every": "5s"})))
        hid = self.catalog.get("Uberducky HID")
        self.assertIsNone(hid.estimated_seconds(arguments(hid, Uberducky="x")))

    def test_missing_scaling_argument_is_no_estimate_not_an_error(self):
        # A per-duration estimate on a call without a duration argument, and
        # a per-count estimate without its count: None, never an exception.
        definition = replace(
            self.catalog.get("Host version"),
            estimates=MappingProxyType({
                DEFAULT_PROFILE: Estimate(1.0, per_duration=2, per_count=("flashes", 1)),
            }),
        )
        self.assertIsNone(definition.estimated_seconds({}))
        self.assertIsNone(definition.estimated_seconds({"duration": "2s"}))
        self.assertIsNone(definition.estimated_seconds({"flashes": 3}))
        self.assertIsNone(definition.estimated_seconds({"duration": "2s", "flashes": True}))
        self.assertIsNone(definition.estimated_seconds({"duration": "bad", "flashes": 3}))
        self.assertEqual(8.0, definition.estimated_seconds({"duration": "2s", "flashes": 3}))
        # No duration argument on a test without calibrated estimates.
        bare = replace(self.catalog.get("BLE monitor"), estimates=None)
        self.assertIsNone(bare.estimated_seconds({}))
        self.assertIsNone(bare.estimated_seconds({"duration": "bad"}))

    def test_estimate_rounds_to_a_tenth(self):
        self.assertEqual(0.3, Estimate(0.1, per_duration=0.1).seconds({"duration": "2s"}))

    def test_every_repository_suite_has_a_countdown(self):
        plans = {plan["name"]: plan for plan in list_plans(REPOSITORY / "testplans")}
        for name, plan in plans.items():
            self.assertIsNone(plan["error"], name)
            if name in UNIMPLEMENTED_PLANS:
                continue
            for suite in plan["suites"]:
                with self.subTest(plan=name, suite=suite["name"]):
                    self.assertEqual(0, suite["unestimated_tests"])
                    self.assertGreater(suite["estimated_s"], 0)

    def test_release_estimate_is_the_calibration_run(self):
        # The release plan run at fork 7bb07c9 took 621.0 s of test time, and
        # Failed capture files (comprehensive, measured on the same macOS
        # host) adds 15.1 s, and Interrupting a capture 45.0 s. The release
        # suite is the non-soak release gate and is kept well inside half an
        # hour of test time.
        plans = {plan["name"]: plan for plan in list_plans(REPOSITORY / "testplans")}
        (suite,) = plans["release"]["suites"]
        self.assertAlmostEqual(681.1, suite["estimated_s"], delta=0.2)
        self.assertLessEqual(suite["estimated_s"], 30 * 60)


def manifest_event(invocations):
    """The run's manifest record, as the controller stores it (JSON)."""
    return json.loads(json.dumps({
        "event_type": "manifest", "status": "passed",
        "details": {"manifest": {"invocations": [
            InvocationIdentity.from_invocation(item).to_dict() for item in invocations
        ]}},
    }))


class PlannedInvocationTests(unittest.TestCase):
    """The live countdown counts the invocations planned for the target."""

    def setUp(self):
        self.catalog = builtin_catalog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.yaml"
            path.write_text(HARDWARE, encoding="utf-8")
            self.registry = FileResourceRegistry.load(path)

    def compile(self, plan, suite):
        path = REPOSITORY / "testplans" / f"{plan}.yaml"
        return PlanCompiler(self.catalog, self.registry).compile(
            load_plan(path, self.catalog), suite)

    def test_each_ubertooth_is_one_estimated_invocation_per_board(self):
        # exclusive.yaml selects Ubertooth: each; the host has three boards.
        invocations = self.compile("exclusive", "exclusive")
        self.assertEqual(3, len(invocations))
        planned = planned_invocations([{"event_type": "run_started"},
                                       manifest_event(invocations)])
        self.assertEqual(
            [(item.invocation_id, "Exclusive Ubertooth topology", 15.0)
             for item in invocations],
            [(p["invocation_id"], p["test_name"], p["estimated_s"]) for p in planned],
        )
        # The plan listing gives one invocation and flags it as per board.
        plans = {plan["name"]: plan for plan in list_plans(REPOSITORY / "testplans")}
        (suite,) = plans["exclusive"]["suites"]
        self.assertEqual(15.0, suite["estimated_s"])
        self.assertEqual(1, suite["per_board_tests"])
        self.assertEqual([True], [test["per_board"] for test in suite["tests"]])
        self.assertEqual(3 * 15.0, sum(p["estimated_s"] for p in planned))

    def test_single_board_steps_match_the_plan_listing(self):
        invocations = self.compile("fast", "fast")
        planned = planned_invocations([manifest_event(invocations)])
        plans = {plan["name"]: plan for plan in list_plans(REPOSITORY / "testplans")}
        (suite,) = [s for s in plans["fast"]["suites"] if s["name"] == "fast"]
        self.assertEqual(0, suite["per_board_tests"])
        self.assertEqual([t["estimated_s"] for t in suite["tests"]],
                         [p["estimated_s"] for p in planned])

    def test_no_or_malformed_manifest(self):
        self.assertIsNone(planned_invocations([]))
        self.assertIsNone(planned_invocations([{"event_type": "manifest", "details": {}}]))
        self.assertIsNone(planned_invocations(
            [{"event_type": "manifest", "details": {"manifest": {"invocations": [1]}}}]))
        unknown = planned_invocations([{"event_type": "manifest", "details": {"manifest": {
            "invocations": [{"id": "001-x", "test_name": "No such test", "arguments": {}},
                            {"id": "002-y", "test_name": "Host version", "arguments": 3}],
        }}}])
        self.assertEqual([None, None], [item["estimated_s"] for item in unknown])


if __name__ == "__main__":
    unittest.main()
