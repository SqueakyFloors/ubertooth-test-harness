"""RF placement: the registry's `rf:`, selection by it, and what is recorded."""

import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.events import open_run_publisher
from ubertooth_harness.manifest import InvocationIdentity
from ubertooth_harness.plan import PlanError, load_plan
from ubertooth_harness.planner import PlanCompiler, PlanningError, TestInvocation
from ubertooth_harness.report import write_report
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from ubertooth_harness.rf import (
    DEFAULT_PLACEMENT,
    NORMAL_LINK,
    Placement,
    PlacementError,
    PlacementRequirement,
    describe,
    placement,
    requirement,
    requirements,
)


# UT_Linux1 is the weak-link board: attenuated, and away from the others.
HARDWARE = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults: {Ubertooth: UT_Linux0, nRF52840 DK: DK_Linux0}
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: linux-0}
  UT_Linux1:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: linux-1}
    rf: {antenna: true, attenuation_db: 20, distance_m: 1}
  UT_Linux2:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: linux-2}
  DK_Linux0:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [advertiser]
    identity: {jlink serial: "000123456789", BLE address: "C0:00:00:00:00:02"}
"""


def registry(directory, text=HARDWARE) -> FileResourceRegistry:
    path = Path(directory) / "hardware.yaml"
    path.write_text(text)
    return FileResourceRegistry.load(path)


def plan(directory, text):
    path = Path(directory) / "plan.yaml"
    path.write_text(text)
    return load_plan(path)


def compile_suite(directory, plan_text, hardware_text=HARDWARE):
    return PlanCompiler(builtin_catalog(), registry(directory, hardware_text)).compile_suite(
        plan(directory, plan_text), "fast"
    )


class PlacementTests(unittest.TestCase):

    def test_an_rf_mapping_defaults_to_an_ordinary_board(self):
        self.assertEqual(Placement(True, 0.0, None), placement({}))
        self.assertEqual(DEFAULT_PLACEMENT, placement({}))
        self.assertEqual(
            Placement(False, 20.0, 1.5),
            placement({"antenna": False, "attenuation_db": 20, "distance_m": 1.5}),
        )

    def test_refuses_values_that_cannot_describe_a_bench(self):
        for raw, message in (
            ({"antenna": "yes"}, "antenna must be true or false"),
            ({"attenuation_db": "20"}, "attenuation_db must be a number"),
            ({"attenuation_db": True}, "attenuation_db must be a number"),
            ({"attenuation_db": -1}, "attenuation_db must not be negative"),
            ({"attenuation_db": float("inf")}, "must be a finite number"),
            ({"distance_m": 0}, "distance_m must be positive"),
            ({"distance_m": -2}, "distance_m must be positive"),
            ({"attenuaton_db": 20}, "unknown key"),
            ([], "rf must be a mapping"),
        ):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(PlacementError, message):
                    placement(raw)

    def test_a_requirement_reads_the_bounds_and_the_antenna(self):
        self.assertEqual(
            PlacementRequirement(None, 10.0, None),
            requirement({"min_attenuation_db": 10}),
        )
        self.assertEqual(
            PlacementRequirement(True, None, 0.0),
            requirement({"antenna": True, "max_attenuation_db": 0}),
        )
        with self.assertRaisesRegex(PlacementError, "above max_attenuation_db"):
            requirement({"min_attenuation_db": 30, "max_attenuation_db": 20})
        with self.assertRaisesRegex(PlacementError, "non-empty mapping"):
            requirement({})
        with self.assertRaisesRegex(PlacementError, "unknown key"):
            requirement({"attenuation_db": 20})

    def test_a_requirement_that_asks_for_nothing_is_refused(self):
        # `min_attenuation_db:` with nothing after it would otherwise read as
        # "any placement" and drop the test's own requirement with it, so a
        # weak-link plan would quietly select any board.
        for raw in ({"min_attenuation_db": None}, {"antenna": None},
                    {"max_attenuation_db": None, "min_attenuation_db": None}):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    PlacementError, "must set at least one of"
                ):
                    requirement(raw)

    def test_a_board_with_no_rf_counts_as_an_ordinary_one(self):
        # This is what lets `max_attenuation_db: 0` select the normal boards
        # without every registry having to spell out `rf:` on each of them.
        self.assertTrue(NORMAL_LINK.matches(None))
        self.assertFalse(PlacementRequirement(min_attenuation_db=10).matches(None))

    def test_a_requirement_matches_only_a_board_placed_for_it(self):
        weak = Placement(True, 20.0, 1)
        self.assertTrue(PlacementRequirement(min_attenuation_db=10).matches(weak))
        self.assertTrue(PlacementRequirement(min_attenuation_db=20).matches(weak))
        self.assertFalse(PlacementRequirement(min_attenuation_db=30).matches(weak))
        self.assertFalse(NORMAL_LINK.matches(weak))
        self.assertTrue(
            PlacementRequirement(max_attenuation_db=20).matches(weak)
        )
        self.assertFalse(NORMAL_LINK.matches(Placement(False, 0.0, None)))
        self.assertTrue(
            PlacementRequirement(antenna=False).matches(Placement(False, 0.0, None))
        )

    def test_descriptions_read_as_plain_text(self):
        self.assertEqual("20 dB, antenna, 1 m away", Placement(True, 20, 1).describe())
        self.assertEqual("0 dB, no antenna", Placement(False, 0, None).describe())
        self.assertEqual("0 dB, antenna (not recorded)", describe(None))
        self.assertEqual("20 dB, antenna", describe({"attenuation_db": 20}))
        self.assertEqual(
            "attenuation of at least 10 dB",
            PlacementRequirement(min_attenuation_db=10).describe(),
        )
        self.assertEqual(
            "attenuation of at most 0 dB and its antenna attached",
            NORMAL_LINK.describe(),
        )
        self.assertEqual(
            "attenuation of exactly 20 dB",
            PlacementRequirement(min_attenuation_db=20, max_attenuation_db=20).describe(),
        )

    def test_a_record_this_version_cannot_read_is_shown_as_it_stands(self):
        # A run directory written by another version of the harness must not
        # stop a report.
        self.assertEqual("depth=3", describe({"depth": 3}))

    def test_a_plan_argument_is_one_requirement_per_role(self):
        self.assertEqual(
            {"responder": PlacementRequirement(min_attenuation_db=10)},
            dict(requirements({"responder": {"min_attenuation_db": 10}})),
        )
        with self.assertRaisesRegex(PlacementError, "rf responder: .*unknown key"):
            requirements({"responder": {"attenuation": 10}})
        with self.assertRaisesRegex(PlacementError, "non-empty mapping"):
            requirements({})


class RegistryPlacementTests(unittest.TestCase):

    def test_the_registry_exposes_the_recorded_placement(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = registry(directory)

            self.assertEqual(Placement(True, 20.0, 1.0), loaded.get("UT_Linux1").rf)
            self.assertIsNone(loaded.get("UT_Linux0").rf)

    def test_a_bad_placement_names_the_resource(self):
        broken = HARDWARE.replace("attenuation_db: 20", "attenuation_db: -3")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                RegistryError, "resource UT_Linux1: rf attenuation_db must not be negative"
            ):
                registry(directory, broken)

    def test_a_unit_with_no_radio_cannot_have_a_placement(self):
        hosted = HARDWARE.replace(
            "roles: [controller, test target]",
            "roles: [controller, test target]\n    rf: {attenuation_db: 20}",
        )
        controlled = HARDWARE + """  Hub_Linux0:
    type: USB power controller
    attached to: Host_Linux
    identity: {serial: "0x12345678"}
    driver: acroname BrainStem USBHub3+
    capabilities: [per-port VBUS switching, power readback]
    rf: {attenuation_db: 20}
"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                RegistryError, "Host_Linux is a Host and cannot have an rf placement"
            ):
                registry(directory, hosted)
            with self.assertRaisesRegex(
                RegistryError,
                "Hub_Linux0 is a USB power controller and cannot have an rf placement",
            ):
                registry(directory, controlled)

    def test_placed_lists_only_the_boards_that_satisfy_a_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = registry(directory)

            self.assertEqual(
                ("UT_Linux1",),
                loaded.placed(
                    "Host_Linux", "Ubertooth",
                    PlacementRequirement(min_attenuation_db=10),
                ),
            )
            self.assertEqual(
                ("UT_Linux0", "UT_Linux2"),
                loaded.placed("Host_Linux", "Ubertooth", NORMAL_LINK),
            )

    def test_default_takes_the_host_default_only_when_it_is_placed_for_the_role(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = registry(directory)

            self.assertEqual(
                ("UT_Linux0",),
                loaded.resolve_hardware("Host_Linux", "Ubertooth", "default"),
            )
            # The host default is not attenuated, so the lowest-alias board
            # that is takes the role.
            self.assertEqual(
                ("UT_Linux1",),
                loaded.resolve_hardware(
                    "Host_Linux", "Ubertooth", "default", None,
                    PlacementRequirement(min_attenuation_db=10),
                ),
            )

    def test_each_covers_the_matching_boards_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = registry(directory)

            self.assertEqual(
                ("UT_Linux0", "UT_Linux2"),
                loaded.resolve_hardware(
                    "Host_Linux", "Ubertooth", "each", None, NORMAL_LINK
                ),
            )

    def test_an_explicit_alias_that_cannot_satisfy_the_requirement_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = registry(directory)

            with self.assertRaisesRegex(
                RegistryError,
                "resource UT_Linux1 does not have attenuation of at most 0 dB "
                r"and its antenna attached \(it has 20 dB, antenna, 1 m away\)",
            ):
                loaded.resolve_hardware(
                    "Host_Linux", "Ubertooth", "UT_Linux1", None, NORMAL_LINK
                )

    def test_no_board_placed_for_the_role_is_refused_naming_what_there_is(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = registry(directory)

            with self.assertRaisesRegex(
                RegistryError,
                "host Host_Linux has no Ubertooth with attenuation of at least 30 dB "
                r"\(UT_Linux0 has 0 dB, antenna \(not recorded\); UT_Linux1 has "
                r"20 dB, antenna, 1 m away; UT_Linux2 has 0 dB, antenna "
                r"\(not recorded\)\)",
            ):
                loaded.resolve_hardware(
                    "Host_Linux", "Ubertooth", "each", None,
                    PlacementRequirement(min_attenuation_db=30),
                )


class PlacementSelectionTests(unittest.TestCase):

    def test_a_plan_can_ask_for_the_weak_link_board(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = compile_suite(directory, """plan: rf
suites:
  fast:
    - Ubertooth identity:
        hosts: [Host_Linux]
        rf:
          Ubertooth: {min_attenuation_db: 10}
""")

            self.assertEqual(
                "UT_Linux1", compiled.invocations[0].resources["Ubertooth"]
            )

    def test_a_capture_test_keeps_off_the_attenuated_board_by_default(self):
        # The host's default Ubertooth is the attenuated one here, so the
        # test's own requirement is what moves the capture to another board.
        attenuated_default = HARDWARE.replace(
            "defaults: {Ubertooth: UT_Linux0,", "defaults: {Ubertooth: UT_Linux1,"
        )
        with tempfile.TemporaryDirectory() as directory:
            compiled = compile_suite(directory, """plan: rf
suites:
  fast:
    - Classic Bluetooth receive:
        hosts: [Host_Linux]
        coverage: smoke
        duration: 3s
    - Ubertooth identity:
        hosts: [Host_Linux]
""", attenuated_default)

            chosen = {
                item.test_name: item.resources["Ubertooth"]
                for item in compiled.invocations
            }
            self.assertEqual(
                {"Classic Bluetooth receive": "UT_Linux0",
                 "Ubertooth identity": "UT_Linux1"},
                chosen,
            )

    def test_each_leaves_an_attenuated_board_out_of_a_capture_test(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = compile_suite(directory, """plan: rf
suites:
  fast:
    - Raw receive:
        hosts: [Host_Linux]
        Ubertooth: each
        coverage: smoke
        duration: 3s
""")

            self.assertEqual(
                ["UT_Linux0", "UT_Linux2"],
                [item.resources["Ubertooth"] for item in compiled.invocations],
            )

    def test_a_plan_overrides_the_test_default_for_that_role(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = compile_suite(directory, """plan: rf
suites:
  fast:
    - Classic Bluetooth receive:
        hosts: [Host_Linux]
        coverage: smoke
        duration: 3s
        rf:
          Ubertooth: {min_attenuation_db: 10}
""")

            self.assertEqual(
                "UT_Linux1", compiled.invocations[0].resources["Ubertooth"]
            )

    def test_an_explicit_alias_the_requirement_rules_out_fails_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError, "UT_Linux1 does not have attenuation of at most 0 dB"
            ):
                compile_suite(directory, """plan: rf
suites:
  fast:
    - Classic Bluetooth receive:
        hosts: [Host_Linux]
        Ubertooth: UT_Linux1
        coverage: smoke
        duration: 3s
""")

    def test_a_host_with_no_board_placed_for_the_role_is_not_applicable(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError,
                "no selected host can run it: Host_Linux: no Ubertooth on this host "
                "has attenuation of at least 40 dB for the Ubertooth",
            ):
                compile_suite(directory, """plan: rf
suites:
  fast:
    - Ubertooth identity:
        hosts: [Host_Linux]
        rf:
          Ubertooth: {min_attenuation_db: 40}
""")

    def test_one_host_without_it_is_left_out_and_another_still_runs(self):
        two_hosts = HARDWARE + """  Host_Mac:
    type: Host
    platform: macos
    roles: [test target]
    defaults: {Ubertooth: UT_Mac0}
  UT_Mac0:
    type: Ubertooth
    attached to: Host_Mac
    identity: {serial: mac-0}
"""
        with tempfile.TemporaryDirectory() as directory:
            compiled = compile_suite(directory, """plan: rf
suites:
  fast:
    - Ubertooth identity:
        hosts: each
        rf:
          Ubertooth: {min_attenuation_db: 10}
""", two_hosts)

            self.assertEqual(
                ["UT_Linux1"],
                [item.resources["Ubertooth"] for item in compiled.invocations],
            )
            left_out = compiled.for_host("Host_Mac")
            self.assertEqual(1, len(left_out))
            self.assertIn(
                "no Ubertooth on this host has attenuation of at least 10 dB for "
                "the Ubertooth (UT_Mac0 has 0 dB, antenna (not recorded))",
                left_out[0].reason,
            )

    def test_rf_names_a_role_the_test_binds(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanError, "unknown hardware role.* receiver; this test binds Ubertooth"
            ):
                plan(directory, """plan: rf
suites:
  fast:
    - Ubertooth identity:
        hosts: [Host_Linux]
        rf:
          receiver: {min_attenuation_db: 10}
""")

    def test_a_test_that_binds_no_hardware_does_not_accept_rf(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PlanError, "unknown argument"):
                plan(directory, """plan: rf
suites:
  fast:
    - Host version:
        hosts: [Host_Linux]
        rf:
          Ubertooth: {min_attenuation_db: 10}
""")

    def test_an_empty_rf_requirement_is_refused_rather_than_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PlanError, "must set at least one of"):
                plan(directory, """plan: rf
suites:
  fast:
    - Classic Bluetooth receive:
        hosts: [Host_Linux]
        coverage: smoke
        duration: 3s
        rf:
          Ubertooth: {min_attenuation_db:}
""")

    def test_an_rf_requirement_that_cannot_hold_is_refused_at_load(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PlanError, "above max_attenuation_db"):
                plan(directory, """plan: rf
suites:
  fast:
    - Ubertooth identity:
        hosts: [Host_Linux]
        rf:
          Ubertooth: {min_attenuation_db: 30, max_attenuation_db: 10}
""")

    def test_the_catalog_resolves_the_requirement_per_role(self):
        definition = builtin_catalog().get("Classic Bluetooth receive")

        self.assertEqual(
            {"Ubertooth": NORMAL_LINK}, dict(definition.placement_requirements({}))
        )
        arguments = definition.normalize_arguments({
            "coverage": "smoke", "duration": "3s",
            "rf": {"Ubertooth": {"min_attenuation_db": 10}},
        })
        self.assertEqual(
            {"Ubertooth": PlacementRequirement(min_attenuation_db=10)},
            dict(definition.placement_requirements(arguments)),
        )
        self.assertEqual(
            {}, dict(builtin_catalog().get("Ubertooth identity").placement_requirements({}))
        )

    def test_rf_is_rejected_by_name_on_a_test_with_no_hardware(self):
        with self.assertRaisesRegex(CatalogError, "unknown argument"):
            builtin_catalog().get("Host version").normalize_arguments({"rf": {}})


class PlacementRecordTests(unittest.TestCase):

    def test_every_board_the_run_used_has_its_placement_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = compile_suite(directory, """plan: rf
suites:
  fast:
    - Ubertooth identity:
        hosts: [Host_Linux]
        rf:
          Ubertooth: {min_attenuation_db: 10}
""")

            placements = compiled.invocations[0].resource_placements
            self.assertEqual(
                {"Ubertooth": {"antenna": True, "attenuation_db": 20.0,
                               "distance_m": 1.0}},
                {role: dict(record) for role, record in placements.items()},
            )

    def test_a_guard_board_with_a_placement_is_recorded_too(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = compile_suite(directory, """plan: rf
suites:
  fast:
    - Raw receive:
        hosts: [Host_Linux]
        Ubertooth: UT_Linux0
        coverage: smoke
        duration: 3s
""")

            invocation = compiled.invocations[0]
            guard = next(
                role for role, alias in invocation.resources.items()
                if alias == "UT_Linux1"
            )
            self.assertEqual(
                20.0, invocation.resource_placements[guard]["attenuation_db"]
            )
            # UT_Linux0 and UT_Linux2 record no placement, so neither appears.
            self.assertEqual([guard], list(invocation.resource_placements))

    def test_what_each_role_asked_of_its_placement_is_recorded(self):
        # Why a role got the resource it did: a run that passed over the host
        # default, or left a board out of `each`, is otherwise unexplained.
        with tempfile.TemporaryDirectory() as directory:
            compiled = compile_suite(directory, """plan: rf
suites:
  fast:
    - Raw receive:
        hosts: [Host_Linux]
        Ubertooth: each
        coverage: smoke
        duration: 3s
""")

            self.assertEqual(
                {"Ubertooth": {"antenna": True, "max_attenuation_db": 0.0}},
                {role: dict(record) for role, record
                 in compiled.invocations[0].placement_requirements.items()},
            )
            recorded = InvocationIdentity.from_invocation(
                compiled.invocations[0]
            ).to_dict()
            self.assertEqual(
                {"Ubertooth": {"antenna": True, "max_attenuation_db": 0.0}},
                recorded["placement_requirements"],
            )

    def test_a_test_with_no_requirement_records_none(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = compile_suite(directory, """plan: rf
suites:
  fast:
    - Ubertooth identity:
        hosts: [Host_Linux]
""")

            self.assertEqual({}, dict(compiled.invocations[0].placement_requirements))

    def test_the_manifest_record_carries_the_placements(self):
        invocation = TestInvocation(
            "001-Ubertooth identity", "Ubertooth identity", "Host_Linux", {},
            {"Ubertooth": "UT_Linux1"},
            resource_placements=MappingProxyType(
                {"Ubertooth": MappingProxyType({"attenuation_db": 20.0})}
            ),
        )

        recorded = InvocationIdentity.from_invocation(invocation).to_dict()

        self.assertEqual(
            {"Ubertooth": {"attenuation_db": 20.0}}, recorded["resource_placements"]
        )

    def test_an_invocation_without_a_placement_records_an_empty_mapping(self):
        invocation = TestInvocation(
            "001-Host version", "Host version", "Host_Linux", {}, {}
        )

        self.assertEqual(
            {}, InvocationIdentity.from_invocation(invocation).to_dict()[
                "resource_placements"]
        )

    def test_the_run_log_names_the_placement_of_the_board_under_test(self):
        from tests.test_sequencer import CollectSink, FakeExecutor, run_contract
        from ubertooth_harness.sequencer import NamedTestSequencer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invocation = TestInvocation(
                "001-Host version", "Host version", "Host_Mac", {},
                {"Ubertooth": "UT_Mac0"},
                resource_placements=MappingProxyType(
                    {"Ubertooth": MappingProxyType(
                        {"antenna": True, "attenuation_db": 20.0})}
                ),
            )
            manifest, checks = run_contract(root, invocation)

            NamedTestSequencer(FakeExecutor(), event_sink=CollectSink()).run(
                (invocation,), root, run_id="named-run", manifest=manifest,
                preflight_checks=checks,
            )

            started = [
                json.loads(line)
                for line in (root / "run.jsonl").read_text().splitlines()
                if json.loads(line)["event_type"] == "test_started"
            ]
            self.assertEqual(
                {"Ubertooth": {"antenna": True, "attenuation_db": 20.0}},
                started[0]["details"]["rf"],
            )


class PlacementReportTests(unittest.TestCase):

    def write_run(self, directory: Path) -> Path:
        run = directory / "run-rf"
        publisher = open_run_publisher(run, run.name)
        publisher.emit("run_started", "running", {
            "plan_name": "Plan", "suite": "fast", "host": "Host_Linux"})
        publisher.emit("manifest", "passed", {"manifest": {
            "host": "Host_Linux", "plan_name": "Plan", "suite": "fast",
            "sources": [], "artifacts": [],
            "invocations": [{
                "test_name": "Raw receive",
                "resources": {"Ubertooth": "UT_Linux1"},
                "resource_placements": {
                    "Ubertooth": {"antenna": True, "attenuation_db": 20.0,
                                  "distance_m": 1.0},
                },
                "placement_requirements": {
                    "Ubertooth": {"min_attenuation_db": 10.0},
                },
            }],
            "environment": {},
        }})
        publisher.emit("test_started", "running", {
            "invocation_id": "001-Raw receive", "test_name": "Raw receive",
            "host": "Host_Linux", "resources": {"Ubertooth": "UT_Linux1"},
            "rf": {"Ubertooth": {"antenna": True, "attenuation_db": 20.0,
                                 "distance_m": 1.0}},
        })
        publisher.emit("test_finished", "passed", {
            "invocation_id": "001-Raw receive", "test_name": "Raw receive",
            "reason": "", "failed_cases": 0, "skipped_cases": 0})
        publisher.emit("run_finished", "passed", {
            "completed_tests": 1, "requested_tests": 1,
            "completed_preflight_checks": 0, "requested_preflight_checks": 0,
            "skipped_cases": 0, "failure_reason": ""})
        return run

    def test_the_report_says_what_the_numbers_were_taken_at(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.write_run(Path(directory))

            report = write_report(run).read_text()

            self.assertIn("| RF placement | UT_Linux1: 20 dB, antenna, 1 m away |", report)
            self.assertIn(
                "| RF required | Raw receive Ubertooth: attenuation of at least "
                "10 dB |",
                report,
            )
            self.assertIn(
                "| 001-Raw receive | Host_Linux (Ubertooth=UT_Linux1 "
                "[20 dB, antenna, 1 m away]) | passed |",
                report,
            )
            # Not repeated on every transcript heading: it cannot change
            # within a run, and the setup row and the result cell carry it.
            self.assertIn(
                "=== TEST 001-Raw receive: Raw receive on Host_Linux "
                "(Ubertooth=UT_Linux1) ===",
                report,
            )



class PlacementSerializationTests(unittest.TestCase):
    """A plan's `rf:` has to survive the manifest and run.jsonl as JSON."""

    def arguments(self):
        return builtin_catalog().get("Classic Bluetooth receive").normalize_arguments({
            "coverage": "smoke", "duration": "3s",
            "rf": {"Ubertooth": {"min_attenuation_db": 10}},
        })

    def test_a_plan_requirement_is_kept_as_a_plain_record(self):
        self.assertEqual(
            {"Ubertooth": {"min_attenuation_db": 10.0}},
            {role: dict(record) for role, record in self.arguments()["rf"].items()},
        )

    def test_the_manifest_of_a_plan_with_rf_can_be_written(self):
        from tests.test_sequencer import run_contract

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invocation = TestInvocation(
                "001-Classic Bluetooth receive", "Classic Bluetooth receive",
                "Host_Mac", self.arguments(), {"Ubertooth": "UT_Mac0"},
            )
            manifest, _checks = run_contract(root, invocation)
            path = root / "manifest.json"
            manifest.write_atomic(path)

            recorded = json.loads(path.read_text())
            self.assertEqual(
                {"Ubertooth": {"min_attenuation_db": 10.0}},
                recorded["invocations"][0]["arguments"]["rf"],
            )

    def test_the_run_log_of_a_plan_with_rf_can_be_written(self):
        from tests.test_sequencer import CollectSink, FakeExecutor, run_contract
        from ubertooth_harness.sequencer import NamedTestSequencer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invocation = TestInvocation(
                "001-Host version", "Host version", "Host_Mac",
                MappingProxyType({"rf": self.arguments()["rf"]}), {},
            )
            manifest, checks = run_contract(root, invocation)

            NamedTestSequencer(FakeExecutor(), event_sink=CollectSink()).run(
                (invocation,), root, run_id="named-run", manifest=manifest,
                preflight_checks=checks,
            )

            started = next(
                json.loads(line)
                for line in (root / "run.jsonl").read_text().splitlines()
                if json.loads(line)["event_type"] == "test_started"
            )
            self.assertEqual(
                {"Ubertooth": {"min_attenuation_db": 10.0}},
                started["details"]["arguments"]["rf"],
            )

    def test_a_recorded_placement_of_the_wrong_shape_does_not_stop_a_report(self):
        for bad in ("20 dB", 20, ["a"], None):
            with self.subTest(bad=bad):
                self.assertIsInstance(describe(bad), str)


class PlacementRoleTests(unittest.TestCase):
    """A placement-matching unit that cannot take the role is not a candidate."""

    HARDWARE = HARDWARE + """  DK_Linux1:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [peripheral]
    identity: {jlink serial: "000123456790", BLE address: "C0:00:00:00:00:03"}
    rf: {attenuation_db: 20}
"""

    def test_placed_can_require_the_role_as_well(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = registry(directory, self.HARDWARE)
            asked = PlacementRequirement(min_attenuation_db=10)

            self.assertEqual(
                ("DK_Linux1",),
                loaded.placed("Host_Linux", "nRF52840 DK", asked),
            )
            self.assertEqual(
                (), loaded.placed("Host_Linux", "nRF52840 DK", asked, "advertiser")
            )

    def test_each_and_default_both_skip_a_matching_unit_that_cannot_take_the_role(self):
        # A DK placed as asked but registered for another role must not be
        # offered for this one, by either selector.
        asked = PlacementRequirement(min_attenuation_db=10)
        with tempfile.TemporaryDirectory() as directory:
            loaded = registry(directory, self.HARDWARE)

            for selector in ("each", "default"):
                with self.subTest(selector=selector):
                    with self.assertRaisesRegex(
                        RegistryError,
                        "host Host_Linux has no nRF52840 DK with attenuation of "
                        "at least 10 dB",
                    ):
                        loaded.resolve_hardware(
                            "Host_Linux", "nRF52840 DK", selector, "advertiser", asked
                        )

    def test_an_explicit_alias_is_refused_for_its_role_not_its_placement(self):
        asked = PlacementRequirement(min_attenuation_db=10)
        with tempfile.TemporaryDirectory() as directory:
            loaded = registry(directory, self.HARDWARE)

            with self.assertRaisesRegex(
                RegistryError, "DK_Linux1 declares the role\\(s\\) peripheral, not advertiser"
            ):
                loaded.resolve_hardware(
                    "Host_Linux", "nRF52840 DK", "DK_Linux1", "advertiser", asked
                )

    def test_the_host_is_not_applicable_rather_than_failing_the_whole_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError,
                "no selected host can run it: Host_Linux: no nRF52840 DK on this "
                "host has attenuation of at least 10 dB for the advertiser",
            ):
                compile_suite(directory, """plan: rf
suites:
  fast:
    - Capture known BLE advertiser:
        hosts: [Host_Linux]
        coverage: smoke
        duration: 5s
        rf:
          advertiser: {min_attenuation_db: 10}
""", self.HARDWARE)

if __name__ == "__main__":
    unittest.main()
