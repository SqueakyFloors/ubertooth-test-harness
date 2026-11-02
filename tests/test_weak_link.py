"""`weak link:`, the checks it is for, and the plan that carries them (#168)."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ubertooth_harness import weaklink
from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.plan import (
    PlanError,
    check_runnable,
    load_plan,
    unimplemented_tests,
    unmeasured_attenuations,
)
from ubertooth_harness.planner import PlanCompiler, PlanningError
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.rf import NORMAL_LINK, PlacementError, PlacementRequirement

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "testplans"
WEAK_LINK_PLAN = PLANS / "weak-link.yaml"

# A bench with one board at each attenuation the weak-link plan asks for, so
# every suite of it can be compiled here. The real bench has one attenuator
# at a time; this registry is what each suite would run against.
HARDWARE = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults:
      Ubertooth: UT_Open
      nRF52840 DK: DK_Central
      central: DK_Central
      peripheral: DK_Peripheral
      ESP32 Classic peer: ESP32_Acceptor
      acceptor: ESP32_Acceptor
      initiator: ESP32_Initiator
  UT_Open:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: open-0}
  UT_Weak20:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: weak-20}
    rf: {antenna: true, attenuation_db: 20, distance_m: 1}
  UT_Weak40:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: weak-40}
    rf: {antenna: true, attenuation_db: 40, distance_m: 1}
  DK_Central:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [advertiser, central, peripheral]
    identity: {jlink serial: "000123456789", BLE address: "C0:00:00:00:00:01"}
  DK_Peripheral:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [advertiser, central, peripheral]
    identity: {jlink serial: "000123456790", BLE address: "C0:00:00:00:00:02"}
  ESP32_Acceptor:
    type: ESP32 Classic peer
    attached to: Host_Linux
    roles: [acceptor, initiator]
    identity:
      bridge serial: "0123456789abcdef0123456789abcdef"
      BD_ADDR: "C8:00:00:00:00:01"
  ESP32_Initiator:
    type: ESP32 Classic peer
    attached to: Host_Linux
    roles: [acceptor, initiator]
    identity:
      bridge serial: "fedcba9876543210fedcba9876543210"
      BD_ADDR: "C8:00:00:00:00:02"
"""


def registry(directory) -> FileResourceRegistry:
    path = Path(directory) / "hardware.yaml"
    path.write_text(HARDWARE)
    return FileResourceRegistry.load(path)


def requirements_of(test_name, arguments):
    """What a plan entry's arguments require of each role's placement."""
    definition = builtin_catalog().get(test_name)
    return dict(definition.placement_requirements(
        definition.normalize_arguments(arguments)
    ))


class ArgumentTests(unittest.TestCase):

    def test_one_role_at_an_attenuation(self):
        self.assertEqual({"Ubertooth": 20.0}, dict(weaklink.weak_link({"Ubertooth": 20})))

    def test_an_integer_becomes_a_float_so_a_plan_may_write_either(self):
        for written in (20, 20.0, 19.5):
            with self.subTest(written):
                asked = weaklink.weak_link({"receiver": written})
                self.assertIsInstance(asked["receiver"], float)

    def test_it_must_be_a_non_empty_mapping(self):
        for bad in (None, {}, [], "20", 20):
            with self.subTest(bad):
                with self.assertRaisesRegex(PlacementError, "non-empty mapping"):
                    weaklink.weak_link(bad)

    def test_it_names_one_role_only(self):
        """Two attenuated boards would give a figure belonging to neither
        attenuation, so the argument refuses a second role rather than
        leaving the invariant to the plan author."""
        with self.assertRaisesRegex(
            PlacementError, r"names one role, not responder, sender"
        ):
            weaklink.weak_link({"sender": 20, "responder": 40})

    def test_a_plan_cannot_attenuate_two_roles_of_one_test(self):
        with self.assertRaisesRegex(CatalogError, "names one role"):
            builtin_catalog().get("Two-device range test").normalize_arguments({
                "host": "Host_Linux", "sender": "default", "responder": "default",
                "allow RF transmission": True,
                "weak link": {"sender": 20, "responder": 40},
            })

    def test_a_role_name_must_be_a_non_empty_string(self):
        for bad in ({"": 20}, {3: 20}):
            with self.subTest(bad):
                with self.assertRaisesRegex(PlacementError, "non-empty string"):
                    weaklink.weak_link(bad)

    def test_the_attenuation_must_be_a_finite_number(self):
        with self.assertRaisesRegex(PlacementError, "must be a number"):
            weaklink.weak_link({"Ubertooth": "20"})
        # True is an int in Python; an attenuation of True is not a number.
        with self.assertRaisesRegex(PlacementError, "must be a number"):
            weaklink.weak_link({"Ubertooth": True})
        with self.assertRaisesRegex(PlacementError, "finite"):
            weaklink.weak_link({"Ubertooth": float("inf")})

    def test_zero_and_below_are_refused_as_the_opposite_of_a_weak_link(self):
        for bad in (0, -1, -20.0):
            with self.subTest(bad):
                with self.assertRaisesRegex(PlacementError, "above 0 dB"):
                    weaklink.weak_link({"Ubertooth": bad})


class PlacementTests(unittest.TestCase):

    def test_the_named_role_sits_at_exactly_that_attenuation(self):
        self.assertEqual(
            PlacementRequirement(antenna=True, min_attenuation_db=20.0,
                                 max_attenuation_db=20.0),
            weaklink.weak_receiver(20),
        )

    def test_every_other_role_must_be_an_ordinary_board(self):
        wanted = weaklink.placements(
            {"Ubertooth": 40}, {"Ubertooth": "Ubertooth", "central": "nRF52840 DK",
                                "peripheral": "nRF52840 DK"},
        )
        self.assertEqual(weaklink.weak_receiver(40), wanted["Ubertooth"])
        self.assertEqual(NORMAL_LINK, wanted["central"])
        self.assertEqual(NORMAL_LINK, wanted["peripheral"])

    def test_no_weak_link_asked_for_requires_nothing(self):
        self.assertEqual({}, dict(weaklink.placements({}, {"Ubertooth": "Ubertooth"})))

    def test_a_role_the_test_does_not_bind_is_refused_not_dropped(self):
        """The caller checks role names, so reaching here with an unknown one
        is a mistake; silently returning no requirement would select any
        board."""
        with self.assertRaisesRegex(PlacementError, "does not bind: receiver"):
            weaklink.placements({"receiver": 20}, {"Ubertooth": "Ubertooth"})

    def test_unmeasured_reports_the_attenuations_with_no_bench_measurement(self):
        self.assertEqual((), weaklink.unmeasured([20, 20.0]))
        self.assertEqual((30.0, 40.0), weaklink.unmeasured([40, 20, 30, 40]))
        for planned in weaklink.PLANNED_ATTENUATIONS_DB:
            with self.subTest(planned):
                self.assertFalse(weaklink.measured(planned))
        # A figure is measured or planned, never both.
        self.assertFalse(
            set(weaklink.MEASURED_ATTENUATIONS_DB)
            & set(weaklink.PLANNED_ATTENUATIONS_DB)
        )

    def test_attenuations_are_described_for_a_refusal(self):
        self.assertEqual("20 dB", weaklink.describe_attenuations([20]))
        self.assertEqual("30 and 40 dB", weaklink.describe_attenuations([30, 40.0]))
        self.assertEqual("no attenuation", weaklink.describe_attenuations([]))


class CatalogArgumentTests(unittest.TestCase):

    def test_every_test_that_binds_hardware_accepts_it(self):
        catalog = builtin_catalog()
        for name in catalog.names:
            definition = catalog.get(name)
            with self.subTest(name):
                self.assertEqual(
                    bool(definition.hardware_roles),
                    "weak link" in definition.arguments,
                )

    def test_it_overrides_the_tests_own_ordinary_link_default(self):
        # The capture tests ask for an ordinary board themselves; a plan that
        # asks for a weak link is what overrides that, or they could never be
        # pointed at the attenuated board.
        definition = builtin_catalog().get("Capture known BLE advertiser")
        self.assertEqual(NORMAL_LINK, (definition.rf_defaults or {})["Ubertooth"])
        wanted = requirements_of("Capture known BLE advertiser", {
            "hosts": ["Host_Linux"], "Ubertooth": "default", "advertiser": "default",
            "weak link": {"Ubertooth": 20}, "coverage": "smoke", "duration": "5s",
        })
        self.assertEqual(weaklink.weak_receiver(20), wanted["Ubertooth"])
        self.assertEqual(NORMAL_LINK, wanted["advertiser"])

    def test_a_role_the_test_does_not_bind_is_refused(self):
        with self.assertRaisesRegex(CatalogError, r"weak link: unknown hardware role"):
            builtin_catalog().get("Capture known BLE advertiser").normalize_arguments({
                "hosts": ["Host_Linux"], "Ubertooth": "default",
                "advertiser": "default", "weak link": {"responder": 20},
                "coverage": "smoke", "duration": "5s",
            })

    def test_rf_and_weak_link_together_are_refused(self):
        with self.assertRaisesRegex(CatalogError, "not both"):
            builtin_catalog().get("Spectrum RSSI sanity").normalize_arguments({
                "hosts": ["Host_Linux"], "Ubertooth": "default", "duration": "1s",
                "weak link": {"Ubertooth": 20},
                "rf": {"Ubertooth": {"min_attenuation_db": 20}},
            })

    def test_a_test_that_binds_no_hardware_cannot_be_given_one(self):
        with self.assertRaisesRegex(CatalogError, "unknown argument"):
            builtin_catalog().get("Host version").normalize_arguments(
                {"weak link": {"Ubertooth": 20}}
            )


class RangeSuiteTests(unittest.TestCase):
    """The committed range suite says the same thing it said with `rf:`."""

    def test_the_shorthand_matches_the_placement_the_suite_spelled_out(self):
        spelled_out = requirements_of("Two-device range test", {
            "host": "Host_Linux", "sender": "default", "responder": "default",
            "allow RF transmission": True, "maximum PA level": 3,
            "rf": {
                "sender": {"max_attenuation_db": 0, "antenna": True},
                "responder": {"min_attenuation_db": 20, "max_attenuation_db": 20,
                              "antenna": True},
            },
        })
        (step,) = load_plan(PLANS / "range.yaml", builtin_catalog()).steps(
            "range-weak-link")
        self.assertEqual({"responder": 20}, dict(step.arguments["weak link"]))
        self.assertEqual(
            spelled_out, requirements_of("Two-device range test", step.arguments))


class WeakLinkPlanTests(unittest.TestCase):

    def test_it_is_not_runnable_for_both_of_the_reasons_it_states(self):
        """Two reasons, and the plan would be refused for either: two entries
        have no implementation, and the four 40 dB entries ask for an
        attenuation the bench has never been measured at."""
        catalog = builtin_catalog()
        plan = load_plan(WEAK_LINK_PLAN, catalog)
        self.assertFalse(plan.runnable)
        self.assertEqual(("Transmit power steps", "Receiver sensitivity"),
                         unimplemented_tests(plan, catalog))
        self.assertEqual(
            {
                "Classic capture record fields": (40.0,),
                "Capture known BLE connection": (40.0,),
                "Capture known Classic device": (40.0,),
                "Classic Bluetooth receive": (40.0,),
            },
            dict(unmeasured_attenuations(plan, catalog)),
        )
        # `runnable: false` is what makes check_runnable pass on it; the same
        # plan marked runnable is refused.
        check_runnable(plan, catalog)
        with self.assertRaises(PlanError):
            check_runnable(replace(plan, runnable=True), catalog)

    def test_a_runnable_plan_cannot_ask_for_an_unmeasured_attenuation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runnable.yaml"
            path.write_text(
                "plan: Guessing\n"
                "suites:\n"
                "  s:\n"
                "    - Classic Bluetooth receive:\n"
                "        hosts: [Host_Linux]\n"
                "        coverage: smoke\n"
                "        duration: 5s\n"
                "        weak link: {Ubertooth: 40}\n"
            )
            with self.assertRaisesRegex(
                PlanError, r"Classic Bluetooth receive at 40 dB"
            ) as raised:
                check_runnable(load_plan(path, builtin_catalog()), builtin_catalog())
        self.assertIn("measured at 20 dB", str(raised.exception))

    def test_the_same_refusal_covers_the_rf_spelling(self):
        """A pending check written out as `rf:` instead of `weak link:` is
        refused the same way: the rule reads the placement requirements, not
        the argument it came from."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runnable.yaml"
            path.write_text(
                "plan: Guessing\n"
                "suites:\n"
                "  s:\n"
                "    - Classic Bluetooth receive:\n"
                "        hosts: [Host_Linux]\n"
                "        coverage: smoke\n"
                "        duration: 5s\n"
                "        rf: {Ubertooth: {min_attenuation_db: 40}}\n"
            )
            with self.assertRaisesRegex(PlanError, "40 dB"):
                check_runnable(load_plan(path, builtin_catalog()), builtin_catalog())

    def test_every_suite_compiles_against_a_bench_that_has_the_attenuators(self):
        catalog = builtin_catalog()
        plan = load_plan(WEAK_LINK_PLAN, catalog)
        with tempfile.TemporaryDirectory() as directory:
            compiler = PlanCompiler(catalog, registry(directory))
            for suite in plan.suites:
                with self.subTest(suite):
                    compiled = compiler.compile_suite(
                        plan, suite, strict_hardware=True)
                    self.assertTrue(compiled.invocations, suite)

    def test_each_entry_binds_the_board_at_the_attenuation_it_asked_for(self):
        """The attenuation is not re-derived from the argument: the run
        records the placement of the board the planner bound, which is what a
        report quotes its figures as having been taken at."""
        catalog = builtin_catalog()
        plan = load_plan(WEAK_LINK_PLAN, catalog)
        expected = {check.test: check.attenuation_db for check in weaklink.CHECKS}
        with tempfile.TemporaryDirectory() as directory:
            compiler = PlanCompiler(catalog, registry(directory))
            seen = {}
            for suite in plan.suites:
                for invocation in compiler.compile_suite(
                    plan, suite, strict_hardware=True
                ).invocations:
                    role = next(
                        role for role, want in catalog.get(
                            invocation.test_name
                        ).placement_requirements(invocation.arguments).items()
                        if want.min_attenuation_db
                    )
                    placed = invocation.resource_placements[role]
                    seen[invocation.test_name] = placed["attenuation_db"]
                    self.assertIs(True, placed["antenna"])
        self.assertEqual(expected, seen)

    def test_a_bench_with_no_attenuated_board_is_told_what_is_missing(self):
        """The example registry has the host but no attenuated board, which is
        a plan error rather than a skip, and the message names the requirement
        and what each board is placed as instead."""
        catalog = builtin_catalog()
        plan = load_plan(WEAK_LINK_PLAN, catalog)
        compiler = PlanCompiler(
            catalog, FileResourceRegistry.load(ROOT / "hardware.example.yaml"))
        with self.assertRaises(PlanningError) as raised:
            compiler.compile_suite(plan, "weak-link-20db")
        message = str(raised.exception)
        self.assertIn("attenuation of exactly 20 dB and its antenna attached",
                      message)
        self.assertIn("UT_Linux0 has 0 dB", message)


class CheckTableTests(unittest.TestCase):
    """weaklink.CHECKS is the list of weak-link plan entries, so it and the
    plan have to agree; a check added to one without the other fails here."""

    def test_every_check_names_a_catalog_test(self):
        names = builtin_catalog().names
        for check in weaklink.CHECKS:
            with self.subTest(check.item):
                self.assertIn(check.test, names)
                self.assertIn(
                    check.weak_role,
                    builtin_catalog().get(check.test).hardware_roles or {},
                )

    def test_every_check_is_in_the_weak_link_plan_at_its_attenuation(self):
        plan = load_plan(WEAK_LINK_PLAN, builtin_catalog())
        asked = {
            step.test_name: dict(step.arguments["weak link"])
            for suite in plan.suites for step in plan.steps(suite)
        }
        self.assertEqual(
            {check.test: {check.weak_role: check.attenuation_db}
             for check in weaklink.CHECKS},
            {name: {role: float(db) for role, db in want.items()}
             for name, want in asked.items()},
        )

    def test_every_check_is_still_pending_a_measurement(self):
        # All nine are, which is why none is in a runnable plan. When one
        # stops being pending, this is where the change shows up, and its
        # entry moves out of weak-link.yaml.
        for check in weaklink.CHECKS:
            with self.subTest(check.item):
                self.assertTrue(check.pending, check.describe())
                self.assertIn(check.needs, check.describe())

    def test_no_check_sits_in_a_committed_runnable_plan(self):
        """Read from the placement requirements, not the argument, so a check
        spelled out as `rf:` is caught as well as one using `weak link:`."""
        catalog = builtin_catalog()
        pending = {
            (check.test, check.weak_role, check.attenuation_db)
            for check in weaklink.CHECKS if check.pending
        }
        checked = 0
        for path in sorted(PLANS.glob("*.yaml")):
            plan = load_plan(path, catalog)
            if not plan.runnable:
                continue
            for suite in plan.suites:
                for step in plan.steps(suite):
                    definition = catalog.get(step.test_name)
                    for role, want in definition.placement_requirements(
                        step.arguments
                    ).items():
                        if not want.min_attenuation_db:
                            continue
                        checked += 1
                        with self.subTest(f"{path.name}:{suite}:{step.test_name}"):
                            self.assertNotIn(
                                (step.test_name, role, want.min_attenuation_db),
                                pending,
                            )
        # `range-weak-link` is the one weak-link entry a runnable plan does
        # carry, so this loop is not vacuous.
        self.assertEqual(1, checked)


class DocumentationTests(unittest.TestCase):

    def test_the_two_new_tests_have_a_section_of_their_own(self):
        text = (ROOT / "docs" / "tests.md").read_text(encoding="utf-8")
        for name in ("Transmit power steps", "Receiver sensitivity"):
            with self.subTest(name):
                self.assertIn(f"\n## {name}\n", text)

    def test_the_shorthand_is_documented_where_rf_is(self):
        for path in (ROOT / "docs" / "tests.md", ROOT / "README.md",
                     ROOT / "DESIGN.md"):
            with self.subTest(path.name):
                self.assertIn("weak link:", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
