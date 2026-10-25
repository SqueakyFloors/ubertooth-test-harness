"""Catalog duration estimates (the GUI countdown)."""

from pathlib import Path
import unittest

from ubertooth_harness.catalog import (
    DEFAULT_PROFILE,
    ESTIMATES,
    Estimate,
    builtin_catalog,
)
from ubertooth_harness.gui import list_plans
from ubertooth_harness.plan import load_plan

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
                    {"smoke", "comprehensive"} if "coverage" in definition.arguments
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
        self.assertEqual(13.0, exclusive.estimated_seconds(arguments(
            exclusive, platforms=["linux"], **{"exclusive Ubertooth": True})))
        self.assertEqual(68.0, exclusive.estimated_seconds(arguments(
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
        # The release plan run at fork 7bb07c9 took 621.0 s of test time.
        plans = {plan["name"]: plan for plan in list_plans(REPOSITORY / "testplans")}
        (suite,) = plans["release"]["suites"]
        load_plan(REPOSITORY / "testplans" / "release.yaml")
        self.assertAlmostEqual(621.0, suite["estimated_s"], delta=1.0)


if __name__ == "__main__":
    unittest.main()
