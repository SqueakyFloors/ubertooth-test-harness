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
    check_suite_runnable,
    unimplemented_tests,
    unjudgeable_attenuations,
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


def one_step_plan(test_name, body):
    """A one-step runnable plan, for exercising the runnable gates."""
    directory = tempfile.mkdtemp()
    path = Path(directory) / "p.yaml"
    path.write_text(
        "plan: One\nsuites:\n  s:\n"
        f"    - {test_name}:\n        hosts: [Host_Linux]\n" + body
    )
    return load_plan(path, builtin_catalog())


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
        self.assertEqual((), weaklink.unmeasured([20, 20.0, 40, 40.0]))
        # 50 dB is deliberately not the exemplar here: the bench HAS been
        # taken to it (CHARACTERIZED_UNUSABLE_DB), and `unmeasured` draws no
        # such distinction, so using it would read as the wording this
        # module now avoids. 25 and 30 dB are figures nobody has fitted.
        self.assertEqual((25.0, 30.0), weaklink.unmeasured([40, 20, 30, 25, 30]))
        self.assertTrue(weaklink.measured(40))
        for planned in weaklink.PLANNED_ATTENUATIONS_DB:
            with self.subTest(planned):
                self.assertFalse(weaklink.measured(planned))
        # A figure is measured or planned, never both.
        self.assertFalse(
            set(weaklink.MEASURED_ATTENUATIONS_DB)
            & set(weaklink.PLANNED_ATTENUATIONS_DB)
        )

    def test_a_point_found_unusable_is_not_measured_and_not_planned(self):
        """A characterized-unusable figure stays out of a runnable plan.

        It has been taken to, so "not measured" would be wrong to say, but a
        test may not be pointed at it either: nothing can be judged there.
        """
        self.assertTrue(weaklink.CHARACTERIZED_UNUSABLE_DB)
        for value, reason in weaklink.CHARACTERIZED_UNUSABLE_DB.items():
            with self.subTest(value):
                self.assertIsInstance(value, float)
                self.assertFalse(weaklink.measured(value))
                self.assertNotIn(value, weaklink.PLANNED_ATTENUATIONS_DB)
                # The reason is what a refusal quotes, so it has to say
                # something rather than name the figure again.
                self.assertGreater(len(reason), 40)

    def test_a_point_found_unusable_is_refused_with_its_reason(self):
        """Not "is not measured": the reason says which way is worth trying."""
        unusable = sorted(weaklink.CHARACTERIZED_UNUSABLE_DB)[0]
        why = weaklink.unjudgeable(
            weaklink.weak_receiver(unusable),
            "Classic capture record fields", "Ubertooth",
        )
        self.assertIn(f"needs {unusable:g} dB", why)
        self.assertIn("found unusable", why)
        self.assertIn(weaklink.CHARACTERIZED_UNUSABLE_DB[unusable], why)
        self.assertNotIn("is not measured", why)

    def test_an_attenuation_nobody_has_tried_is_still_plainly_unmeasured(self):
        """The new branch must not swallow the ordinary case.

        The figure is derived rather than written down, so characterizing
        another point cannot turn this into a confusing failure.
        """
        untried = 5.0 + max((*weaklink.MEASURED_ATTENUATIONS_DB,
                             *weaklink.PLANNED_ATTENUATIONS_DB,
                             *weaklink.CHARACTERIZED_UNUSABLE_DB))
        self.assertEqual(
            f"needs {untried:g} dB, which is not measured",
            weaklink.unjudgeable(weaklink.weak_receiver(untried),
                                 "Classic capture record fields", "Ubertooth"),
        )

    def test_an_int_attenuation_reaches_the_unusable_branch_too(self):
        """An int bound must behave as the float does.

        Both ways into a requirement coerce today (``weak_receiver`` and
        ``rf._number``), so the ``float()`` in the lookup is defensive. It is
        tested through a requirement built directly, which is the only way to
        get an int that far, so the branch is covered rather than the
        coercion being tested twice.
        """
        unusable = sorted(weaklink.CHARACTERIZED_UNUSABLE_DB)[0]
        built = PlacementRequirement(
            antenna=True,
            min_attenuation_db=int(unusable),
            max_attenuation_db=int(unusable),
        )
        self.assertIsInstance(built.min_attenuation_db, int)
        self.assertIn("found unusable", weaklink.unjudgeable(
            built, "Classic capture record fields", "Ubertooth"))

    def test_no_check_sits_at_a_figure_found_unusable(self):
        """Otherwise the unusable branch would be unreachable, and worse.

        ``measured_for`` is consulted first, so a non-pending ``Check`` at an
        unusable figure would make a plan pointed there *runnable* and judge
        a test against a link that settled nothing.
        """
        for check in weaklink.CHECKS:
            with self.subTest(check.test, at=check.attenuation_db):
                if check.attenuation_db in weaklink.CHARACTERIZED_UNUSABLE_DB:
                    self.assertTrue(
                        check.pending,
                        f"{check.test} claims a measurement at "
                        f"{check.attenuation_db:g} dB, which "
                        "CHARACTERIZED_UNUSABLE_DB says settled nothing",
                    )

    def test_attenuations_are_described_for_a_refusal(self):
        self.assertEqual("20 dB", weaklink.describe_attenuations([20]))
        self.assertEqual("20 and 40 dB",
                         weaklink.describe_attenuations(
                             weaklink.MEASURED_ATTENUATIONS_DB))
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
    """What is left in the not-runnable plan, and why each entry is there."""

    def test_it_is_not_runnable_and_its_remaining_suite_says_why(self):
        catalog = builtin_catalog()
        plan = load_plan(WEAK_LINK_PLAN, catalog)
        self.assertFalse(plan.runnable)
        self.assertEqual(["weak-link-unmeasured"], sorted(plan.suites))
        # The unmeasured suite is refused for both reasons a plan can be:
        # two entries have no implementation, and one asks for a link no
        # capture figure exists for.
        self.assertEqual(
            ("Transmit power steps", "Receiver sensitivity"),
            unimplemented_tests(plan, catalog, "weak-link-unmeasured"),
        )
        check_runnable(plan, catalog)

    def test_the_dk_pair_entries_are_no_longer_waiting(self):
        """Measured 2026-11-04 once the bench had two DKs; they moved to
        ble.yaml's connection-weak-link suite."""
        catalog = builtin_catalog()
        plan = load_plan(WEAK_LINK_PLAN, catalog)
        self.assertNotIn("weak-link-needs-a-dk-pair", plan.suites)
        for test in ("Capture known BLE connection",
                     "Follow a BLE connection across its end"):
            with self.subTest(test):
                self.assertNotIn(test, [s.test_name for suite in plan.suites
                                        for s in plan.steps(suite)])

    def test_a_loose_lower_bound_is_refused_in_a_runnable_plan(self):
        """`rf:` can write a bare `min_attenuation_db`, and which board that
        binds depends on what the bench holds. Only an exact pin at a
        measured figure, or no attenuation, can be judged."""
        for spelling in ("{min_attenuation_db: 20}",
                         "{min_attenuation_db: 20, max_attenuation_db: 40}",
                         "{max_attenuation_db: 40}",
                         "{antenna: true}"):
            with self.subTest(spelling):
                with self.assertRaisesRegex(PlanError, "does not pin the attenuation"):
                    check_runnable(self._runnable_with(f"rf: {{Ubertooth: {spelling}}}"),
                                   builtin_catalog())
        # An ordinary board, and an exact pin this test is measured at, are
        # both fine. The advertiser capture is the test measured at 20 and
        # 40 dB, so it is the one that can carry an exact pin.
        advertiser = ("        advertiser: default\n        coverage: smoke\n"
                      "        duration: 5s\n        interval: 100\n"
                      "        payload: c0ffee\n")
        for spelling in ("{max_attenuation_db: 0, antenna: true}",
                         "{min_attenuation_db: 20, max_attenuation_db: 20}",
                         "{min_attenuation_db: 40, max_attenuation_db: 40}"):
            with self.subTest(spelling):
                check_runnable(one_step_plan(
                    "Capture known BLE advertiser",
                    advertiser + f"        rf: {{Ubertooth: {spelling}}}\n"),
                    builtin_catalog())

    def _runnable_with(self, argument):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "runnable.yaml"
        path.write_text(
            "plan: Guessing\n"
            "suites:\n"
            "  s:\n"
            "    - Classic Bluetooth receive:\n"
            "        hosts: [Host_Linux]\n"
            "        coverage: smoke\n"
            "        duration: 5s\n"
            f"        {argument}\n"
        )
        return load_plan(path, builtin_catalog())

    def test_a_runnable_plan_cannot_ask_for_an_unmeasured_attenuation(self):
        """30 dB is the attenuator the bench has not had: asking for it in a
        runnable plan is refused, naming it and what is measured."""
        with self.assertRaisesRegex(
            PlanError,
            r"Classic Bluetooth receive: Ubertooth needs 30 dB, "
            r"which is not measured",
        ) as raised:
            check_runnable(self._runnable_with("weak link: {Ubertooth: 30}"),
                           builtin_catalog())
        self.assertIn("measured at 20 and 40 dB", str(raised.exception))

    def test_the_same_refusal_covers_the_rf_spelling(self):
        with self.assertRaisesRegex(PlanError, "30 dB"):
            check_runnable(
                self._runnable_with("rf: {Ubertooth: {min_attenuation_db: 30, "
                                    "max_attenuation_db: 30}}"),
                builtin_catalog())


class MeasuredSuiteTests(unittest.TestCase):
    """The entries the 2026-11-02 measurements promoted into runnable plans."""

    # Where each promoted check now lives, and at what attenuation.
    PROMOTED = {
        ("ble.yaml", "ble-weak-link"): [
            ("Capture known BLE advertiser", 20.0),
            ("Capture known BLE advertiser", 40.0),
        ],
        ("classic.yaml", "classic-weak-link"): [
            ("Follow Classic Bluetooth piconet", 20.0),
        ],
        ("ble.yaml", "connection-weak-link"): [
            ("Capture known BLE connection", 40.0),
            ("Follow a BLE connection across its end", 20.0),
        ],
        ("range.yaml", "range-weak-link"): [
            ("Two-device range test", 20.0),
        ],
    }

    def test_each_promoted_suite_is_runnable_and_asks_for_its_attenuation(self):
        catalog = builtin_catalog()
        for (name, suite), expected in self.PROMOTED.items():
            with self.subTest(f"{name}:{suite}"):
                plan = load_plan(PLANS / name, catalog)
                self.assertTrue(plan.runnable)
                # Runnable through both gates, the per-plan and the per-suite.
                check_runnable(plan, catalog)
                check_suite_runnable(plan, suite, catalog)
                found = []
                for step in plan.steps(suite):
                    definition = catalog.get(step.test_name)
                    for want in definition.placement_requirements(
                        step.arguments
                    ).values():
                        if want.min_attenuation_db:
                            found.append((step.test_name,
                                          want.min_attenuation_db))
                self.assertEqual(sorted(expected), sorted(found))

    def test_the_gate_is_per_test_not_per_bench(self):
        """40 dB is measured for a BLE advertiser capture and not for a
        Classic capture file, so a bench-wide list would admit the second on
        the first's evidence. This is the distinction the gate has to make."""
        catalog = builtin_catalog()
        self.assertTrue(weaklink.measured(40))
        self.assertTrue(weaklink.measured_for(
            "Capture known BLE advertiser", "Ubertooth", 40))
        self.assertFalse(weaklink.measured_for(
            "Classic capture record fields", "Ubertooth", 40))
        accepted = one_step_plan(
            "Capture known BLE advertiser",
            "        advertiser: default\n        coverage: smoke\n"
            "        duration: 5s\n        interval: 100\n"
            "        payload: c0ffee\n        weak link: {Ubertooth: 40}\n")
        check_runnable(accepted, catalog)
        refused = one_step_plan(
            "Classic capture record fields",
            "        acceptor: default\n        initiator: default\n"
            "        coverage: smoke\n        duration: 25s\n"
            "        weak link: {Ubertooth: 40}\n")
        with self.assertRaisesRegex(
            PlanError, "measured on this bench but not for this test"
        ):
            check_runnable(refused, catalog)

    def test_a_role_is_part_of_the_evidence(self):
        """The range test is measured at 20 dB for its `responder`, so the
        same attenuation asked of its `sender` is not covered."""
        self.assertTrue(weaklink.measured_for(
            "Two-device range test", "responder", 20))
        self.assertFalse(weaklink.measured_for(
            "Two-device range test", "sender", 20))

    def test_every_promoted_entry_matches_its_ordinary_counterpart(self):
        """"No threshold moved" means what it says: each promoted entry's
        normalized arguments equal a named ordinary entry's, bar the weak
        link and whatever the comment in the plan explains."""
        catalog = builtin_catalog()
        # (promoted plan, suite, ordinary plan, suite, allowed differences)
        pairs = [
            ("ble.yaml", "ble-weak-link", "ble.yaml", "ble",
             "Capture known BLE advertiser", set()),
            ("classic.yaml", "classic-weak-link", "classic.yaml", "classic",
             "Follow Classic Bluetooth piconet", set()),
            ("ble.yaml", "connection-weak-link", "ble.yaml", "connection",
             "Capture known BLE connection", set()),
            ("ble.yaml", "connection-weak-link", "ble.yaml", "connection",
             "Follow a BLE connection across its end", set()),
        ]
        for name, suite, oname, osuite, test, allowed in pairs:
            definition = catalog.get(test)
            ordinary = next(
                definition.normalize_arguments(step.arguments)
                for step in load_plan(PLANS / oname, catalog).steps(osuite)
                if step.test_name == test
            )
            promoted = [
                definition.normalize_arguments(step.arguments)
                for step in load_plan(PLANS / name, catalog).steps(suite)
                if step.test_name == test
            ]
            self.assertTrue(promoted, f"{suite} has no {test} entry")
            for arguments in promoted:
                differ = {
                    key for key in set(arguments) | set(ordinary)
                    if arguments.get(key) != ordinary.get(key)
                }
                with self.subTest(f"{suite}:{test}"):
                    self.assertEqual(
                        {"weak link"} | allowed, differ,
                        f"{test} in {suite} differs from {osuite} by {differ}")


class CheckTableTests(unittest.TestCase):
    """weaklink.CHECKS is the ledger of the nine checks #168 approved, plus
    the AFH one the measurements added. It and the plans have to agree."""

    def test_every_check_names_a_catalog_test_and_one_of_its_roles(self):
        catalog = builtin_catalog()
        for check in weaklink.CHECKS:
            with self.subTest(check.item):
                self.assertIn(check.test, catalog.names)
                self.assertIn(check.weak_role,
                              catalog.get(check.test).hardware_roles or {})

    def test_a_measured_check_sits_in_a_runnable_plan_at_its_attenuation(self):
        catalog = builtin_catalog()
        placed = {}
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
                        if want.min_attenuation_db:
                            placed.setdefault(
                                (step.test_name, role), set()
                            ).add(want.min_attenuation_db)
        for check in weaklink.CHECKS:
            with self.subTest(check.item):
                key = (check.test, check.weak_role)
                if check.pending:
                    self.assertNotIn(
                        check.attenuation_db, placed.get(key, set()),
                        f"{check.item} is pending but in a runnable plan")
                else:
                    self.assertIn(
                        check.attenuation_db, placed.get(key, set()),
                        f"{check.item} is measured but in no runnable plan")

    def test_a_pending_check_gives_its_reason_and_waits_in_the_plan(self):
        catalog = builtin_catalog()
        plan = load_plan(WEAK_LINK_PLAN, catalog)
        waiting = {
            (step.test_name, role, want.min_attenuation_db)
            for suite in plan.suites for step in plan.steps(suite)
            for role, want in catalog.get(
                step.test_name
            ).placement_requirements(step.arguments).items()
            if want.min_attenuation_db
        }
        pending = [check for check in weaklink.CHECKS if check.pending]
        # Eight are still pending; each names what it waits on and is carried
        # by the not-runnable plan rather than merely described.
        self.assertEqual(6, len(pending))
        for check in pending:
            with self.subTest(check.item):
                self.assertTrue(check.needs)
                self.assertIn(check.needs, check.describe())
                self.assertIn(
                    (check.test, check.weak_role, check.attenuation_db),
                    waiting,
                )

    def test_a_measured_check_describes_itself_without_a_reason(self):
        for check in weaklink.CHECKS:
            if check.pending:
                continue
            with self.subTest(check.item):
                self.assertNotIn("pending", check.describe())
                self.assertIn(f"{check.attenuation_db:g} dB", check.describe())



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
