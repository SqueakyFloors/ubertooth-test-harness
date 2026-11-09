"""One comprehensive release configuration, as a graph of child runs (#403).

Three things are tested here, and none of them needs a board.

The configuration itself, against the committed plans: every stage names a
suite that loads and is runnable, the fast stage keeps exactly the checks no
other stage covers, the waves overlap only what may overlap, and the whole
graph's preflight refuses what cannot be started.

The record and the matrix, from data alone: a stored gate survives a round
trip, a malformed one is refused, and one verdict comes out of the stages --
passing only when every required stage passed.

And the controller, driven with scripted runners over real loopback
WebSockets (the fixture ``tests/test_run_groups.py`` uses) against a
configuration of its own over synthetic plans: a stage starts when the stages
it waits for have settled, stages on different hosts start together, a failure
abandons what required it and leaves the rest alone, a cancel stops the
stages still to come, and the whole gate sends one completion notice instead
of one per stage.
"""

import asyncio
import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ubertooth_harness import releasegate
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.cli import main
from ubertooth_harness.gui import WEB_DIR
from ubertooth_harness.controller import Controller, ControllerServer
from ubertooth_harness.releasegate import (
    BOOTLOADER_RECORD,
    BOOTLOADER_UPDATE,
    CANCELED,
    COMPREHENSIVE,
    DISK_BUDGET,
    ERROR,
    FAILED,
    GATES_DIR,
    IMMUTABLE_REVISION,
    NO_PASSED_IN_IMAGE,
    NOT_REACHED,
    PASSED,
    PENDING,
    REQUIRED_STAGE_HOSTS,
    RUNNING,
    SKIPPED,
    Stage,
    WALL_TIME,
    compile_stages,
    configuration,
    enabled,
    gate_from_record,
    gate_view,
    new_gate,
    pending_decisions,
    preflight,
    schedule,
    stage_tests,
    suite_steps,
    superseded_tests,
    wall_estimate,
)

try:
    from test_controller import TIMEOUT_S, fake_ref_source, wait_until
    from test_run_groups import LINUX, MAC, RunGroupTestCase, ScriptedRunner
except ImportError:  # run as tests.test_release_gate
    from tests.test_controller import TIMEOUT_S, fake_ref_source, wait_until
    from tests.test_run_groups import (
        LINUX, MAC, RunGroupTestCase, ScriptedRunner,
    )

REPOSITORY = Path(__file__).resolve().parents[1]
PLANS = REPOSITORY / "testplans"
BENCH = {LINUX: "linux", MAC: "macos"}
# The operating systems a scripted runner registers with, as
# tests/test_run_groups.py writes them.
OPERATING_SYSTEMS = {
    "linux": {"system": "Linux", "release": "6.8", "distribution": "Ubuntu 24.04"},
    "macos": {"system": "Darwin", "release": "24.1.0", "macos": "15.1"},
}


def check(report, name):
    """One preflight check out of a report, by name."""
    for entry in report["checks"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"no {name} check in {[c['name'] for c in report['checks']]}")


def compiled(hosts=(LINUX, MAC), *, with_bootloader=False, plans_dir=PLANS,
             stages=None):
    """The configuration compiled for a bench, with its waves."""
    chosen = stages or enabled(
        configuration(COMPREHENSIVE), with_bootloader=with_bootloader
    )
    plans = compile_stages(
        chosen, plans_dir, list(hosts),
        platforms={host: BENCH.get(host) for host in hosts},
    )
    return plans, schedule(plans)


# ----- the configuration ---------------------------------------------------


class ConfigurationTests(unittest.TestCase):
    """The committed configuration, against the committed plans."""

    def test_the_only_configuration_is_the_comprehensive_one(self):
        self.assertEqual([COMPREHENSIVE], sorted(releasegate.CONFIGURATIONS))
        with self.assertRaisesRegex(releasegate.ReleaseGateError, "unknown"):
            configuration("release-everything")

    def test_every_stage_names_a_runnable_committed_suite(self):
        catalog = builtin_catalog()
        for stage in configuration(COMPREHENSIVE):
            with self.subTest(stage.name):
                steps = suite_steps(PLANS, stage, catalog)
                self.assertTrue(steps, f"{stage.suite} has no step")

    def test_a_stage_naming_a_suite_nobody_has_is_refused(self):
        catalog = builtin_catalog()
        for stage, expected in (
            (Stage("x", "fast", "nowhere", ""), "unknown suite"),
            (Stage("x", "nowhere", "fast", ""), "cannot read"),
            (Stage("x", "backlog", "backlog", ""), "plan of planned tests"),
            (Stage("x", "fast", "fast", "", tests=("No such test",)), "has no"),
        ):
            with self.subTest(expected):
                with self.assertRaisesRegex(releasegate.ReleaseGateError, expected):
                    suite_steps(PLANS, stage, catalog)

    def test_every_requirement_names_a_stage_of_the_configuration(self):
        stages = configuration(COMPREHENSIVE)
        names = {stage.name for stage in stages}
        for stage in stages:
            for required in stage.requires:
                self.assertIn(required, names, f"{stage.name} requires {required}")

    def test_the_required_stages_the_issue_asks_for_are_all_there(self):
        # The bounded, recurring suites a release has to pass: the fast-only
        # checks, the main release suite, the three comprehensive profiles,
        # the alternate image, the characterized weak-link suites, and the
        # application firmware. The bootloader is opt-in, and the soaks are
        # not here at all.
        plans, _waves = compiled()
        suites = {plan.stage.suite for plan in plans}
        for suite in (
            "fast", "release", "ble-comprehensive", "classic-comprehensive",
            "hci-comprehensive", "btbr-comprehensive", "ble-weak-link",
            "connection-weak-link", "classic-weak-link", "firmware",
        ):
            self.assertIn(suite, suites)
        self.assertFalse(
            [suite for suite in suites if "soak" in suite],
            "no soak is a release gate",
        )

    def test_the_bootloader_stage_is_the_only_opt_in_one(self):
        stages = configuration(COMPREHENSIVE)
        opt_in = [stage for stage in stages if stage.opt_in]
        self.assertEqual(1, len(opt_in))
        self.assertEqual(BOOTLOADER_UPDATE, opt_in[0].bootloader)
        self.assertTrue(opt_in[0].condition)
        without = {stage.name for stage in enabled(stages)}
        self.assertNotIn(opt_in[0].name, without)
        self.assertIn(
            opt_in[0].name,
            {stage.name for stage in enabled(stages, with_bootloader=True)},
        )
        # Every other stage leaves its boards' bootloaders alone.
        self.assertEqual(
            {BOOTLOADER_RECORD},
            {stage.bootloader for stage in stages if not stage.opt_in},
        )

    def test_build_options_come_from_what_the_suites_declare(self):
        plans, _waves = compiled()
        options = {plan.name: plan for plan in plans}
        # The alternate image is asked for by the one stage about it, and
        # nothing else asks for it.
        self.assertTrue(options["btbr"].with_btbr)
        self.assertEqual(
            ["btbr"], [plan.name for plan in plans if plan.with_btbr]
        )
        # The release suite runs the Python tools comprehensively and checks
        # DFU images, so it asks for both of those builds.
        self.assertTrue(options["release"].with_python)
        self.assertTrue(options["release"].with_firmware)
        # Every stage builds and flashes: a release is qualified on tools and
        # firmware built from the commit under test.
        self.assertEqual(
            [], [plan.name for plan in plans if not (plan.build and plan.flash_build)]
        )

    def test_each_stage_reports_the_device_roles_it_binds(self):
        plans = {plan.name: plan for plan in compiled()[0]}
        self.assertEqual(
            ["Ubertooth: default", "acceptor: default", "initiator: default"],
            list(plans["classic"].resources),
        )
        # A weak-link receiver is named by the attenuation its plan pins it
        # to, not by an alias: the registry is the target's.
        self.assertIn("Ubertooth: 20 dB", plans["classic-weak-link"].resources)
        self.assertIn("Bluetooth adapter: default", plans["hci"].resources)

    def test_a_stage_runs_only_on_the_hosts_that_run_a_step_of_it(self):
        plans = {plan.name: plan for plan in compiled()[0]}
        # The fast checks and the release suite run on both hosts; the peer
        # and adapter suites name the Linux host their fixtures are on.
        self.assertEqual([LINUX, MAC], list(plans["fast-only"].hosts))
        self.assertEqual([LINUX, MAC], list(plans["release"].hosts))
        for name in ("ble", "classic", "hci", "btbr"):
            self.assertEqual([LINUX], list(plans[name].hosts), name)


class DeduplicationTests(unittest.TestCase):
    """What one stage leaves to another (``superseded_tests``)."""

    def test_the_fast_stage_keeps_only_what_no_other_stage_covers(self):
        plans = {plan.name: plan for plan in compiled()[0]}
        fast = plans["fast-only"]
        catalog = builtin_catalog()
        whole = {
            step.test_name
            for step in suite_steps(PLANS, fast.stage, catalog)
        }
        kept = set(fast.tests)
        self.assertTrue(kept)
        self.assertTrue(kept < whole)
        # Each dropped test is named with the stage that covers it, and that
        # stage really runs it.
        for name, covered_by in fast.dropped:
            with self.subTest(name):
                self.assertNotIn(name, kept)
                self.assertIn(name, {
                    step.test_name for step in suite_steps(
                        PLANS, plans[covered_by].stage, catalog
                    )
                })
        # Nothing the fast suite holds is both kept and dropped.
        self.assertEqual(whole, kept | {name for name, _ in fast.dropped})

    def test_a_smoke_case_whose_comprehensive_profile_is_elsewhere_is_dropped(self):
        plans = {plan.name: plan for plan in compiled()[0]}
        # The release suite runs these comprehensively; the fast suite ran
        # them at smoke.
        for name in ("Argument validation", "BLE CRC transitions"):
            self.assertNotIn(name, plans["fast-only"].tests)

    def test_a_test_covered_on_one_host_only_is_not_dropped_for_the_other(self):
        # A stage that runs a test on both hosts keeps it when the stage
        # that would cover it runs on the Linux host alone: dropping it
        # would quietly lose the Mac's measurement.
        with tempfile.TemporaryDirectory() as directory:
            plans_dir = Path(directory)
            (plans_dir / "both.yaml").write_text(
                "plan: both\nsuites:\n  both:\n    - Host version:\n"
                "        hosts: each\n", encoding="utf-8",
            )
            (plans_dir / "one.yaml").write_text(
                "plan: one\nsuites:\n  one:\n    - Host version:\n"
                f"        hosts: [{LINUX}]\n", encoding="utf-8",
            )
            stages = (
                Stage("both", "both", "both", "", drop_superseded=True),
                Stage("one", "one", "one", ""),
            )
            catalog = builtin_catalog()
            dropped = superseded_tests(
                stages, plans_dir, catalog,
                {"both": (LINUX, MAC), "one": (LINUX,)}, BENCH,
            )
            self.assertEqual((), dropped["both"])
            # On a Linux-only bench the same test is covered, so it goes.
            dropped = superseded_tests(
                stages, plans_dir, catalog,
                {"both": (LINUX,), "one": (LINUX,)}, BENCH,
            )
            self.assertEqual((("Host version", "one"),), dropped["both"])

    def test_a_stage_that_names_its_tests_keeps_exactly_those(self):
        stage = Stage("x", "fast", "fast", "", tests=("Ubertooth identity",))
        steps = suite_steps(PLANS, stage, builtin_catalog())
        self.assertEqual(("Ubertooth identity",), stage_tests(stage, steps))
        # A stage that runs its whole suite names no test at all, so a suite
        # that gains one does not need an edit here.
        plain = Stage("y", "fast", "fast", "")
        self.assertEqual((), stage_tests(plain, steps))


class ScheduleTests(unittest.TestCase):
    """Which stages may run at the same time (``schedule``)."""

    def test_stages_sharing_a_host_never_share_a_wave(self):
        plans, waves = compiled()
        hosts = {plan.name: set(plan.hosts) for plan in plans}
        for wave in waves:
            for first in wave:
                for second in wave:
                    if first != second:
                        self.assertFalse(
                            hosts[first] & hosts[second],
                            f"{first} and {second} share a host",
                        )

    def test_the_fast_stage_is_first_and_the_writers_are_last(self):
        plans, waves = compiled(with_bootloader=True)
        order = [name for wave in waves for name in wave]
        self.assertEqual("fast-only", order[0])
        self.assertEqual(
            ["btbr", "application-firmware", "bootloader"], order[-3:]
        )
        writes = {plan.name for plan in plans if plan.stage.writes_firmware}
        self.assertEqual({"btbr", "application-firmware", "bootloader"}, writes)

    def test_stages_on_different_hosts_share_a_wave(self):
        with tempfile.TemporaryDirectory() as directory:
            plans_dir = Path(directory)
            for name, host in (("one", LINUX), ("two", MAC)):
                (plans_dir / f"{name}.yaml").write_text(
                    f"plan: {name}\nsuites:\n  {name}:\n    - Host version:\n"
                    f"        hosts: [{host}]\n", encoding="utf-8",
                )
            stages = (
                Stage("one", "one", "one", ""),
                Stage("two", "two", "two", ""),
            )
            _plans, waves = compiled(stages=stages, plans_dir=plans_dir)
            self.assertEqual((("one", "two"),), waves)

    def test_a_requirement_nothing_satisfies_is_a_mistake_here(self):
        stages = (
            Stage("one", "fast", "fast", "", requires=("two",)),
            Stage("two", "fast", "fast", "", requires=("one",)),
        )
        plans = compile_stages(stages, PLANS, [LINUX], platforms=BENCH)
        with self.assertRaisesRegex(
            releasegate.ReleaseGateError, "cannot be ordered"
        ):
            schedule(plans)

    def test_the_wall_estimate_costs_a_wave_its_slowest_stage(self):
        stages = (
            Stage("one", "fast", "fast", ""),
            Stage("two", "fast", "fast", ""),
        )
        plans = compile_stages(stages, PLANS, [LINUX], platforms=BENCH)
        one, two = plans
        # Two stages in one wave cost the slower of them plus that host's
        # build; in two waves they cost both.
        together = wall_estimate(plans, (("one", "two"),), {LINUX: 10.0})
        apart = wall_estimate(plans, (("one",), ("two",)), {LINUX: 10.0})
        self.assertEqual(round(one.estimated_s + 10.0, 1), together)
        self.assertEqual(round(one.estimated_s + two.estimated_s + 20.0, 1), apart)
        # An unestimated stage leaves the total unknown rather than short.
        self.assertIsNone(wall_estimate(
            [one, dataclasses.replace(two, estimated_s=None)],
            (("one",), ("two",)),
        ))


class PreflightTests(unittest.TestCase):
    """The whole graph, judged before any part of it runs."""

    def test_a_bench_that_can_run_everything_passes(self):
        plans, waves = compiled()
        report = preflight(plans, waves, ref="main", commit="a" * 40)
        self.assertTrue(report["passed"], report["refusal"])
        self.assertEqual("", report["refusal"])
        self.assertIn("a" * 12, check(report, IMMUTABLE_REVISION)["detail"])

    def test_a_ref_that_did_not_resolve_blocks_the_gate(self):
        plans, waves = compiled()
        for commit in (None, "", "main", "abc123"):
            with self.subTest(commit):
                report = preflight(plans, waves, ref="main", commit=commit)
                self.assertFalse(report["passed"])
                self.assertIn("one revision", report["refusal"])

    def test_a_required_stage_with_no_host_blocks_the_gate(self):
        # A bench of a Mac alone runs none of the peer suites, and those are
        # required, so nothing is started rather than a third of the gate.
        plans, waves = compiled(hosts=(MAC,))
        report = preflight(plans, waves, ref="main", commit="a" * 40)
        self.assertFalse(report["passed"])
        self.assertIn("classic", check(report, REQUIRED_STAGE_HOSTS)["detail"])

    def test_an_optional_stage_with_no_host_is_skipped_not_blocking(self):
        plans, waves = compiled()
        optional = [plan for plan in plans if plan.stage.optional]
        self.assertTrue(optional, "the configuration has an optional stage")
        # The same graph with that stage unable to run anywhere still starts.
        reduced = [
            plan if not plan.stage.optional
            else dataclasses.replace(plan, hosts=(), refusal="no board")
            for plan in plans
        ]
        report = preflight(
            reduced, [wave for wave in waves if wave != (optional[0].name,)],
            ref="main", commit="a" * 40,
        )
        self.assertTrue(report["passed"], report["refusal"])
        self.assertIn("skipping", check(report, REQUIRED_STAGE_HOSTS)["detail"])

    def test_a_stage_needing_a_passed_in_image_blocks_the_gate(self):
        # Flash reliability alternates against a pinned known-good image,
        # which only run-suite --artifact can pass: a dispatched run cannot,
        # so such a stage is refused before anything is built.
        stages = (Stage("slow", "slow", "slow", ""),)
        plans, waves = compiled(stages=stages, hosts=(LINUX,))
        report = preflight(plans, waves, ref="main", commit="a" * 40)
        self.assertFalse(report["passed"])
        self.assertIn("KGV", check(report, NO_PASSED_IN_IMAGE)["detail"])

    def test_a_host_with_no_board_blocks_the_gate(self):
        # The one piece of a bench the controller itself knows: a host whose
        # registry holds no board USB can see runs no stage of this gate, and
        # that is worth refusing before a build.
        plans, waves = compiled()
        report = preflight(
            plans, waves, ref="main", commit="a" * 40,
            boards={LINUX: 2, MAC: 0},
        )
        self.assertFalse(report["passed"])
        self.assertIn(MAC, check(report, releasegate.BOARD_ON_EVERY_HOST)["detail"])
        report = preflight(
            plans, waves, ref="main", commit="a" * 40,
            boards={LINUX: 2, MAC: 1},
        )
        self.assertTrue(report["passed"], report["refusal"])
        # With nothing reported it is not judged, and does not block.
        report = preflight(plans, waves, ref="main", commit="a" * 40)
        check_view = check(report, releasegate.BOARD_ON_EVERY_HOST)
        self.assertEqual((True, False), (check_view["passed"], check_view["blocking"]))

    def test_a_full_disk_blocks_the_gate_and_a_missing_one_does_not(self):
        plans, waves = compiled()
        report = preflight(
            plans, waves, ref="main", commit="a" * 40, results_dir=".",
            minimum_free_bytes=2 ** 62,
        )
        self.assertFalse(report["passed"])
        self.assertIn("free", check(report, DISK_BUDGET)["detail"])
        report = preflight(plans, waves, ref="main", commit="a" * 40)
        self.assertTrue(check(report, DISK_BUDGET)["passed"])

    def test_the_wall_time_is_a_warning_not_a_refusal(self):
        plans, waves = compiled()
        report = preflight(
            plans, waves, ref="main", commit="a" * 40, target_wall_s=1.0,
        )
        self.assertTrue(report["passed"], report["refusal"])
        self.assertFalse(check(report, WALL_TIME)["passed"])
        self.assertTrue(report["warnings"])

    def test_the_committed_configuration_fits_the_procedures_target(self):
        # The recurring non-soak gate is meant to stay under two hours so
        # that it and the separate four-hour soak keep a release inside six
        # (docs/release-testing.md section 6). The estimate is test time plus
        # what a build is learned to take, which is zero on a bench that has
        # learned nothing, so this is a floor; a configuration whose tests
        # alone passed the target would be the thing to catch.
        plans, waves = compiled(with_bootloader=True)
        report = preflight(plans, waves, ref="main", commit="a" * 40)
        self.assertTrue(check(report, WALL_TIME)["passed"],
                        check(report, WALL_TIME)["detail"])
        self.assertLess(report["estimated_s"], releasegate.TARGET_WALL_SECONDS)


# ----- the record and the matrix -------------------------------------------


# The matrix tests read durations off their own records, so the gate they
# build starts before the child groups they script.
EPOCH = "2026-01-01T00:00:00.000+00:00"


def gate(hosts=(LINUX, MAC), **fields):
    plans, waves = compiled(hosts=hosts, **fields)
    record = new_gate(
        COMPREHENSIVE, hosts, plans, ref="main", commit="a" * 40, waves=waves,
    )
    record.created_at = EPOCH
    return record


class GateRecordTests(unittest.TestCase):

    def test_a_record_survives_a_round_trip(self):
        original = gate()
        again = gate_from_record(json.loads(json.dumps(original.record())))
        self.assertEqual(original.record(), again.record())
        self.assertTrue(again.gate_id.startswith("gate-"))
        # The stages are stored as they were asked for, so the gate reports
        # them even after a plan file changes under it.
        self.assertEqual(
            [entry["stage"] for entry in original.stages],
            [entry["stage"] for entry in again.stages],
        )

    def test_an_unreadable_record_is_refused(self):
        good = gate().record()
        for change, expected in (
            ({"gate_id": "../escape"}, "invalid gate_id"),
            ({"gate_id": 7}, "invalid gate_id"),
            ({"hosts": "Host_Linux"}, "hosts must be"),
            ({"created_at": 5}, "created_at"),
            ({"ref": 5}, "ref must be"),
            ({"with_bootloader": "yes"}, "with_bootloader"),
            ({"stages": []}, "stages must be"),
            ({"waves": ["one"]}, "waves must be"),
            ({"config": ""}, "config must be"),
        ):
            with self.subTest(change):
                with self.assertRaisesRegex(ValueError, expected):
                    gate_from_record({**good, **change})
        with self.assertRaisesRegex(ValueError, "name a stage twice"):
            gate_from_record({**good, "stages": [good["stages"][0]] * 2})
        with self.assertRaisesRegex(ValueError, "not a stage outcome"):
            gate_from_record({
                **good,
                "stages": [{**good["stages"][0], "outcome": "fine"}, *good["stages"][1:]],
            })
        # Every field a dispatch decision reads: a record whose order is a
        # word, or whose requires is a string, would be loaded and then raise
        # (or silently lose the requirement) where the next stage is chosen.
        for change, expected in (
            ({"order": "soon"}, "order must be"),
            ({"order": True}, "order must be"),
            ({"requires": "fast-only"}, "requires must be"),
            ({"tests": [7]}, "tests must be"),
            ({"plan": ""}, "plan must be"),
            ({"bootloader": None}, "bootloader must be"),
            ({"optional": "no"}, "optional must be"),
            ({"build": 1}, "build must be"),
            ({"hosts": "Host_Linux"}, "hosts must be"),
        ):
            with self.subTest(change):
                with self.assertRaisesRegex(ValueError, expected):
                    gate_from_record({
                        **good,
                        "stages": [{**good["stages"][0], **change},
                                   *good["stages"][1:]],
                    })
        with self.assertRaisesRegex(ValueError, "same run group"):
            gate_from_record({
                **good,
                "stages": [
                    {**entry, "group_id": "grp-one"} for entry in good["stages"][:2]
                ] + list(good["stages"][2:]),
            })

    def test_an_outcome_never_overwrites_a_stage_that_has_a_group(self):
        record = gate()
        self.assertTrue(record.set_stage_group("ble", "grp-1"))
        self.assertFalse(record.set_stage_outcome("ble", SKIPPED, "no"))
        self.assertEqual("grp-1", record.stage("ble")["group_id"])
        self.assertTrue(record.set_stage_outcome("hci", SKIPPED, "no adapter"))
        # And an outcome is recorded once: the first reason stands.
        self.assertFalse(record.set_stage_outcome("hci", FAILED, "something else"))
        self.assertEqual("no adapter", record.stage("hci")["error"])
        with self.assertRaisesRegex(releasegate.ReleaseGateError, "not a stage outcome"):
            record.set_stage_outcome("classic", "fine", "no")


def passed_group(group_id, hosts, *, status=PASSED, start=0, end=30, reason=None):
    """A child group's view as the matrix reads it."""
    return {
        "group_id": group_id, "status": status,
        "members": [
            {
                "host": host, "run_id": f"{group_id}-{host}", "status": status,
                "error": None,
                "run": {
                    "run_id": f"{group_id}-{host}", "status": status,
                    "commit": "a" * 40,
                    "created_at": f"2026-01-01T00:00:{start:02d}.000+00:00",
                    "finished_at": f"2026-01-01T00:00:{end:02d}.000+00:00",
                    "duration_s": float(end - start),
                    "duration_hms": f"{end - start}.000s",
                    "reason": reason,
                    "runner_run_directory": f"/runs/{group_id}-{host}",
                },
            }
            for host in hosts
        ],
    }


class MatrixTests(unittest.TestCase):
    """One verdict and one stage matrix out of the children's own records."""

    def test_a_gate_runs_while_a_stage_is_still_to_come(self):
        record = gate()
        record.set_stage_group("fast-only", "grp-1")
        view = gate_view(record, {"grp-1": passed_group("grp-1", [LINUX, MAC])})
        self.assertEqual(RUNNING, view["status"])
        self.assertIsNone(view["finished_at"])
        rows = {row["stage"]: row for row in view["stages"]}
        self.assertEqual(PASSED, rows["fast-only"]["status"])
        self.assertEqual(PENDING, rows["classic"]["status"])
        self.assertEqual(30.0, rows["fast-only"]["duration_s"])
        self.assertEqual("30.000s", rows["fast-only"]["duration_hms"])
        # Each run's identity, evidence and timing are on the row.
        run = rows["fast-only"]["runs"][0]
        self.assertEqual("a" * 40, run["commit"])
        self.assertEqual("/api/runs/grp-1-Host_Linux/report.md", run["report"])
        self.assertEqual("/runs/grp-1-Host_Linux", run["run_directory"])

    def test_a_gate_passes_only_when_every_required_stage_passed(self):
        record = gate()
        groups = {}
        for index, entry in enumerate(record.stages, start=1):
            group_id = f"grp-{index}"
            record.set_stage_group(entry["stage"], group_id)
            groups[group_id] = passed_group(group_id, entry["hosts"])
        record.dispatched_at = "2026-01-01T00:01:00.000+00:00"
        view = gate_view(record, groups)
        self.assertEqual(PASSED, view["status"])
        self.assertEqual(view["required_asked"], view["required_passed"])
        self.assertIsNotNone(view["duration_s"])
        # One failed stage is the gate's verdict, whichever stage it is.
        required = next(
            entry["stage"] for entry in record.stages if not entry["optional"]
        )
        groups[
            record.stage(required)["group_id"]
        ] = passed_group(
            record.stage(required)["group_id"], [LINUX], status=FAILED,
            reason="the suite failed",
        )
        view = gate_view(record, groups)
        self.assertEqual(FAILED, view["status"])
        row = {entry["stage"]: entry for entry in view["stages"]}
        self.assertEqual("the suite failed", row[required]["reason"])

    def test_a_skipped_optional_stage_does_not_fail_the_gate(self):
        record = gate()
        groups = {}
        for index, entry in enumerate(record.stages, start=1):
            if entry["optional"]:
                record.set_stage_outcome(
                    entry["stage"], SKIPPED, "this bench has no such board"
                )
                continue
            group_id = f"grp-{index}"
            record.set_stage_group(entry["stage"], group_id)
            groups[group_id] = passed_group(group_id, entry["hosts"])
        record.dispatched_at = "2026-01-01T00:01:00.000+00:00"
        view = gate_view(record, groups)
        self.assertEqual(PASSED, view["status"])
        skipped = [row for row in view["stages"] if row["status"] == SKIPPED]
        self.assertTrue(skipped)
        self.assertIn("no such board", skipped[0]["reason"])
        # Skipped is not a problem: it ran nowhere, so there is nothing to
        # report about it beyond its own row.
        self.assertEqual([], view["optional_problems"])

    def test_an_optional_stage_that_failed_is_reported_beside_the_verdict(self):
        record = gate()
        groups = {}
        for index, entry in enumerate(record.stages, start=1):
            group_id = f"grp-{index}"
            record.set_stage_group(entry["stage"], group_id)
            groups[group_id] = passed_group(
                group_id, entry["hosts"],
                status=FAILED if entry["optional"] else PASSED,
                reason="the link was too weak" if entry["optional"] else None,
            )
        record.dispatched_at = "2026-01-01T00:01:00.000+00:00"
        view = gate_view(record, groups)
        # An optional stage does not decide the verdict -- a bench without
        # its fixture would never produce a pass -- but what became of it is
        # reported rather than swallowed.
        self.assertEqual(PASSED, view["status"])
        self.assertEqual(
            [FAILED], [entry["status"] for entry in view["optional_problems"]]
        )
        self.assertIn(
            "too weak", view["optional_problems"][0]["reason"]
        )

    def test_a_stage_whose_group_is_gone_is_an_error_not_a_stage_to_run(self):
        record = gate()
        record.set_stage_group("fast-only", "grp-1")
        view = gate_view(record, {})
        row = view["stages"][0]
        self.assertEqual(releasegate.GROUP_GONE, row["reason"])
        # Not pending: a pending stage is one to start, and this one has run
        # already, on boards a writer stage would then write twice. Not
        # not_reached either, which would read as a stage nobody got to.
        self.assertEqual(ERROR, row["status"])
        self.assertNotIn("fast-only", pending_decisions(view["stages"])[0])

    def test_a_btbr_restore_no_later_stage_verified_is_reported(self):
        record = gate()
        groups = {}
        for index, entry in enumerate(record.stages, start=1):
            if entry["order"] > 0 and not entry["restores_application_image"]:
                # The writers after btbr never ran, so nothing wrote and
                # preflighted the application image after it.
                record.set_stage_outcome(
                    entry["stage"], NOT_REACHED, "it was not run")
                continue
            group_id = f"grp-{index}"
            record.set_stage_group(entry["stage"], group_id)
            groups[group_id] = passed_group(group_id, entry["hosts"])
        record.dispatched_at = "2026-01-01T00:01:00.000+00:00"
        view = gate_view(record, groups)
        self.assertEqual(
            ["btbr"], [entry["stage"] for entry in view["unverified_restores"]]
        )
        # And when a later writer on that board did pass, it is verified.
        for entry in record.stages:
            if entry["order"] > 0 and not entry["restores_application_image"]:
                entry["outcome"] = None
                record.set_stage_group(entry["stage"], f"grp-w-{entry['stage']}")
                groups[f"grp-w-{entry['stage']}"] = passed_group(
                    f"grp-w-{entry['stage']}", entry["hosts"])
        self.assertEqual([], gate_view(record, groups)["unverified_restores"])

    def test_the_two_totals_are_named_apart(self):
        # The gate's own figure is every stage's test time added up; the wall
        # estimate preflight reports costs a wave its slowest stage and adds
        # each stage's build, so one is not the other.
        record = gate()
        view = gate_view(record, {})
        self.assertIsNotNone(view["test_seconds_total"])
        self.assertNotIn("estimated_s", view)

    def test_the_summary_names_the_gates_it_does_not_measure(self):
        view = gate_view(gate(), {})
        self.assertTrue(view["manual_gates"])
        self.assertIn(
            "installer", " ".join(entry["name"] for entry in view["manual_gates"])
        )
        self.assertTrue(view["excluded"])
        self.assertIn(
            "soak", " ".join(entry["name"] for entry in view["excluded"])
        )


class DecisionTests(unittest.TestCase):
    """Which stage may start now (``pending_decisions``)."""

    def rows(self, *entries):
        """A matrix of (stage, status, hosts, requires, order) tuples."""
        return [
            {
                "stage": name, "status": status, "hosts": list(hosts),
                "requires": list(requires), "order": order, "optional": optional,
            }
            for name, status, hosts, requires, order, optional in entries
        ]

    def test_the_first_stage_starts_and_the_rest_wait(self):
        rows = self.rows(
            ("one", PENDING, [LINUX], (), 0, False),
            ("two", PENDING, [LINUX], ("one",), 0, False),
        )
        self.assertEqual((("one",), ()), pending_decisions(rows))

    def test_two_stages_on_different_hosts_start_together(self):
        rows = self.rows(
            ("one", PENDING, [LINUX], (), 0, False),
            ("two", PENDING, [MAC], (), 0, False),
            ("three", PENDING, [LINUX], (), 0, False),
        )
        ready, abandoned = pending_decisions(rows)
        self.assertEqual(("one", "two"), ready)
        self.assertEqual((), abandoned)

    def test_a_host_already_busy_makes_its_stage_wait(self):
        rows = self.rows(("one", PENDING, [LINUX], (), 0, False))
        self.assertEqual(((), ()), pending_decisions(rows, busy_hosts=[LINUX]))
        rows = self.rows(
            ("one", RUNNING, [LINUX], (), 0, False),
            ("two", PENDING, [LINUX], (), 0, False),
        )
        self.assertEqual(((), ()), pending_decisions(rows))

    def test_a_writer_waits_for_every_earlier_stage_on_its_host(self):
        rows = self.rows(
            ("capture", PENDING, [LINUX], (), 0, False),
            ("writer", PENDING, [LINUX], (), 1, False),
        )
        self.assertEqual(("capture",), pending_decisions(rows)[0])
        rows = self.rows(
            ("capture", PASSED, [LINUX], (), 0, False),
            ("writer", PENDING, [LINUX], (), 1, False),
        )
        self.assertEqual(("writer",), pending_decisions(rows)[0])
        # A failed earlier stage does not hold a writer that does not
        # require it: the order is about the board's firmware, not about
        # whether the capture worked.
        rows = self.rows(
            ("capture", FAILED, [LINUX], (), 0, False),
            ("writer", PENDING, [LINUX], (), 1, False),
        )
        self.assertEqual(("writer",), pending_decisions(rows)[0])

    def test_a_requirement_that_did_not_pass_abandons_the_stage(self):
        rows = self.rows(
            ("one", FAILED, [LINUX], (), 0, False),
            ("two", PENDING, [LINUX], ("one",), 0, False),
            ("three", PENDING, [MAC], (), 0, False),
        )
        ready, abandoned = pending_decisions(rows)
        # The independent stage still runs: one failure stops what depended
        # on it and nothing else.
        self.assertEqual(("three",), ready)
        self.assertEqual("two", abandoned[0][0])
        self.assertIn("failed", abandoned[0][1])

    def test_a_skipped_optional_requirement_abandons_nothing(self):
        rows = self.rows(
            ("one", SKIPPED, [LINUX], (), 0, True),
            ("two", PENDING, [LINUX], ("one",), 0, False),
        )
        self.assertEqual((("two",), ()), pending_decisions(rows))
        # A required stage that was skipped is another matter: nothing
        # measured it, so what required it is abandoned.
        rows = self.rows(
            ("one", SKIPPED, [LINUX], (), 0, False),
            ("two", PENDING, [LINUX], ("one",), 0, False),
        )
        ready, abandoned = pending_decisions(rows)
        self.assertEqual(((), "two"), (ready, abandoned[0][0]))


# ----- the controller ------------------------------------------------------


# A configuration of the controller's own, over synthetic plans: four stages
# that need no hardware, on a Linux host and a Mac. `smoke` runs on both;
# `linux` and `mac` run on one each, so they may overlap; `writer` runs last
# on both and requires `linux`.
GATE_PLANS = {
    "smoke": ("smoke", "each"),
    "linuxplan": ("linux", LINUX),
    "macplan": ("mac", MAC),
    "writerplan": ("writer", "each"),
}
TEST_CONFIG = "test-gate"
TEST_STAGES = (
    Stage("smoke", "smoke", "smoke", "the cheap first stage"),
    Stage("linux", "linuxplan", "linux", "the Linux host's own stage",
          requires=("smoke",)),
    Stage("mac", "macplan", "mac", "the Mac's own stage", requires=("smoke",)),
    Stage("writer", "writerplan", "writer", "the stage that writes flash",
          order=1, requires=("smoke", "linux"), writes_firmware=True),
)


class BoardRunner(ScriptedRunner):
    """A scripted runner that registers one board USB can see.

    A gate preflights that every host a required stage runs on has one, so a
    runner with an empty registry is refused -- which is tested too
    (``PreflightTests``), with the gate tests driving a bench that has a
    board.
    """

    async def register(self, host=None, interval=0.2, active=None):
        await self.send(
            "register", host=host or self.host, hostname=f"{self.host}-box",
            os=OPERATING_SYSTEMS[self.platform], python="3.12.6",
            ubertooth_serials=[self.serial], enumeration_error=None,
            boards=[{
                "alias": f"UT_{self.host}", "serial": self.serial,
                "present": True, "held": False, "holder": "",
                "firmware_revision": None, "power_controller": None,
                "power_port": None,
            }],
            heartbeat_interval_s=interval, active_run_id=active,
        )


class GateControllerTestCase(RunGroupTestCase):
    """A controller whose plans and configuration are the test's own."""

    async def runners(self, hosts=(LINUX, MAC), interval=0.2):
        """Both scripted runners, each with a board, closed with the test."""
        made = []
        for host in hosts:
            runner = BoardRunner(
                self.port, host, "macos" if host == MAC else "linux",
                serial=("b" if host == MAC else "a") * 32,
            )
            await runner.__aenter__()
            self.addAsyncCleanup(runner.__aexit__)
            await self.registered(runner, host, interval=interval)
            made.append(runner)
        return made

    async def asyncSetUp(self):
        self._plans = tempfile.TemporaryDirectory()
        self.plans = Path(self._plans.name)
        self.addCleanup(self._plans.cleanup)
        for name, (suite, host) in GATE_PLANS.items():
            hosts = "each" if host == "each" else f"[{host}]"
            (self.plans / f"{name}.yaml").write_text(
                f"plan: {name}\nsuites:\n  {suite}:\n    - Host version:\n"
                f"        hosts: {hosts}\n", encoding="utf-8",
            )
        self._configurations = patch.dict(
            releasegate.CONFIGURATIONS, {TEST_CONFIG: TEST_STAGES}
        )
        self._configurations.start()
        self.addCleanup(self._configurations.stop)
        await super().asyncSetUp()

    async def start_controller(self):
        self.controller = Controller(
            self.results, reply_timeout_s=5, monitor_interval_s=0.05,
            log=self.logs.append, ref_source=fake_ref_source(),
            plans_dir=self.plans,
        )
        self.server = await ControllerServer(self.controller, 0, quiet=True).start()
        self.port = self.server.port

    async def start_gate(self, runners, *, hosts=None, run_ids=None, **body):
        """POST /api/release-gates and accept the first stage's child runs."""
        request = {
            "config": TEST_CONFIG, "ref": "main",
            "hosts": list(hosts or [runner.host for runner in runners]),
            **body,
        }
        post = asyncio.create_task(
            self.http("POST", "/api/release-gates", request)
        )
        await self.accept(runners, run_ids or {
            runner.host: f"smoke-{runner.host}" for runner in runners
        })
        status, payload, _ = await post
        self.assertEqual(201, status, payload)
        return payload["gate"]

    async def accept(self, runners, run_ids):
        """Answer each runner's outstanding run request with a run ID."""
        requests = {}
        for runner in runners:
            requests[runner.host] = await runner.recv()
        for runner in reversed(list(runners)):
            await runner.send(
                "run_accepted", request_id=requests[runner.host]["request_id"],
                run_id=run_ids[runner.host],
            )
        return requests

    async def gate(self, gate_id):
        status, payload, _ = await self.http("GET", f"/api/release-gates/{gate_id}")
        self.assertEqual(200, status, payload)
        return payload["gate"]

    def stage_rows(self, view):
        return {row["stage"]: row for row in view["stages"]}

    async def settled(self, gate_id, stage, *statuses):
        """Wait until one stage of the gate reaches one of these statuses."""
        await wait_until(
            lambda: self.stage_rows(
                self.controller.view_gate(self.controller.gates[gate_id])
            )[stage]["status"] in statuses,
            timeout=TIMEOUT_S,
        )

    async def finish_stage(self, view, stage, runners, status="passed"):
        """Finish every child run of one stage, and wait for the gate to move."""
        rows = self.stage_rows(
            await self.gate(view["gate_id"])
        )
        group = self.controller.groups[rows[stage]["group_id"]]
        for runner in runners:
            run_id = next(
                entry["run_id"] for entry in group.members
                if entry["host"] == runner.host
            )
            await self.finish(runner, run_id, status)
        await self.settled(view["gate_id"], stage, status)


class GateStartTests(GateControllerTestCase):

    async def test_the_first_stage_starts_and_the_record_is_stored(self):
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        self.assertEqual(RUNNING, view["status"])
        self.assertEqual([LINUX, MAC], view["hosts"])
        rows = self.stage_rows(view)
        self.assertEqual(RUNNING, rows["smoke"]["status"])
        self.assertEqual(PENDING, rows["linux"]["status"])
        # The gate record is stored beside the run directories.
        path = self.results / GATES_DIR / f"{view['gate_id']}.json"
        self.assertEqual(
            view["gate_id"], json.loads(path.read_text())["gate_id"]
        )
        # Its child is an ordinary run group: one run per host, with the
        # stage's own plan, suite and build options.
        group = self.controller.groups[rows["smoke"]["group_id"]]
        self.assertEqual([LINUX, MAC], group.hosts)
        self.assertEqual(("smoke", "smoke", True, True),
                         (group.plan, group.suite, group.build, group.flash_build))

    async def test_every_stage_runs_the_one_commit_the_ref_resolved_to(self):
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        requests = []
        await self.finish_stage(view, "smoke", [linux, mac])
        # The second wave: the two single-host stages start together.
        for runner, run_id in ((linux, "linux-1"), (mac, "mac-1")):
            message = await runner.recv()
            requests.append(message)
            await runner.send(
                "run_accepted", request_id=message["request_id"], run_id=run_id
            )
        self.assertEqual({view["commit"]}, {m["commit"] for m in requests})
        self.assertEqual({"main"}, {m["ref"] for m in requests})
        self.assertEqual({"linux", "mac"}, {m["suite"] for m in requests})

    async def test_a_host_that_never_registered_is_refused(self):
        await self.runners([LINUX])
        status, payload, _ = await self.http("POST", "/api/release-gates", {
            "config": TEST_CONFIG, "ref": "main", "hosts": [LINUX, "Host_Nowhere"],
        })
        self.assertEqual(404, status, payload)
        self.assertEqual({}, self.controller.gates)

    async def test_a_bench_that_cannot_run_a_required_stage_starts_nothing(self):
        # The Mac alone runs neither the Linux stage nor what requires it, and
        # both are required: the gate is refused before any run starts.
        linux, mac = await self.runners()
        status, payload, _ = await self.http("POST", "/api/release-gates", {
            "config": TEST_CONFIG, "ref": "main", "hosts": [MAC],
        })
        self.assertEqual(422, status, payload)
        self.assertIn("linux", payload["detail"])
        self.assertEqual({}, self.controller.gates)
        self.assertEqual({}, self.controller.runs)

    async def test_an_unknown_configuration_is_refused(self):
        await self.runners([LINUX])
        status, payload, _ = await self.http("POST", "/api/release-gates", {
            "config": "release-everything", "ref": "main", "hosts": [LINUX],
        })
        self.assertEqual(422, status, payload)
        self.assertIn("unknown release configuration", payload["detail"])

    async def test_the_configuration_listing_starts_nothing(self):
        await self.runners()
        status, payload, _ = await self.http(
            "GET",
            f"/api/release-configurations?config={TEST_CONFIG}"
            f"&host={LINUX}&host={MAC}",
        )
        self.assertEqual(200, status, payload)
        self.assertEqual(
            ["smoke", "linux", "mac", "writer"],
            [stage["stage"] for stage in payload["stages"]],
        )
        self.assertEqual([["smoke"], ["linux", "mac"], ["writer"]], payload["waves"])
        self.assertTrue(payload["preflight"]["passed"])
        self.assertEqual({}, self.controller.runs)
        # And the one check it cannot make without a ref is left out rather
        # than answered.
        self.assertNotIn(
            IMMUTABLE_REVISION,
            [check["name"] for check in payload["preflight"]["checks"]],
        )


class GateProgressTests(GateControllerTestCase):

    async def test_each_stage_starts_when_the_stages_it_waits_for_settle(self):
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        await self.finish_stage(view, "smoke", [linux, mac])
        # Wave two: one stage per host, started together.
        await self.accept([linux, mac], {LINUX: "linux-1", MAC: "mac-1"})
        await self.settled(view["gate_id"], "linux", RUNNING)
        rows = self.stage_rows(await self.gate(view["gate_id"]))
        self.assertEqual(RUNNING, rows["mac"]["status"])
        self.assertEqual(PENDING, rows["writer"]["status"])
        await self.finish(linux, "linux-1", "passed")
        await self.finish(mac, "mac-1", "passed")
        await self.settled(view["gate_id"], "writer", RUNNING, PENDING)
        # Wave three: the writer, on both hosts, once both are free.
        await self.accept([linux, mac], {LINUX: "writer-1", MAC: "writer-2"})
        await self.settled(view["gate_id"], "writer", RUNNING)
        await self.finish(linux, "writer-1", "passed")
        await self.finish(mac, "writer-2", "passed")
        await wait_until(
            lambda: self.controller.view_gate(
                self.controller.gates[view["gate_id"]]
            )["status"] == PASSED,
            timeout=TIMEOUT_S,
        )
        final = await self.gate(view["gate_id"])
        self.assertEqual(PASSED, final["status"])
        self.assertEqual(4, final["stages_passed"])
        self.assertIsNotNone(final["duration_s"])
        self.assertIsNotNone(final["duration_hms"])
        # Every stage carries its run IDs and where its evidence is.
        rows = self.stage_rows(final)
        self.assertEqual(
            {"writer-1", "writer-2"},
            {run["run_id"] for run in rows["writer"]["runs"]},
        )
        self.assertEqual(
            "/api/runs/writer-1/report.md",
            next(run["report"] for run in rows["writer"]["runs"]
                 if run["host"] == LINUX),
        )

    async def test_a_failure_abandons_what_required_it_and_nothing_else(self):
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        await self.finish_stage(view, "smoke", [linux, mac])
        await self.accept([linux, mac], {LINUX: "linux-1", MAC: "mac-1"})
        await self.settled(view["gate_id"], "linux", RUNNING)
        await self.finish(linux, "linux-1", "failed", exit_code=1)
        await self.finish(mac, "mac-1", "passed")
        await wait_until(
            lambda: self.stage_rows(self.controller.view_gate(
                self.controller.gates[view["gate_id"]]
            ))["writer"]["status"] == NOT_REACHED,
            timeout=TIMEOUT_S,
        )
        final = await self.gate(view["gate_id"])
        rows = self.stage_rows(final)
        self.assertEqual(FAILED, rows["linux"]["status"])
        # The independent stage finished; the dependent one was not run, and
        # says which stage stopped it.
        self.assertEqual(PASSED, rows["mac"]["status"])
        self.assertEqual(NOT_REACHED, rows["writer"]["status"])
        self.assertIn("linux", rows["writer"]["reason"])
        self.assertEqual(FAILED, final["status"])
        self.assertIsNotNone(final["finished_at"])

    async def test_a_failed_first_stage_leaves_the_rest_unrun(self):
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        await self.finish_stage(view, "smoke", [linux, mac], "failed")
        await wait_until(
            lambda: not self.controller.view_gate(
                self.controller.gates[view["gate_id"]]
            )["active"],
            timeout=TIMEOUT_S,
        )
        final = await self.gate(view["gate_id"])
        self.assertEqual(FAILED, final["status"])
        self.assertEqual(
            [FAILED, NOT_REACHED, NOT_REACHED, NOT_REACHED],
            [row["status"] for row in final["stages"]],
        )
        self.assertEqual(0, final["stages_passed"])

    async def test_a_cancel_stops_the_stages_still_to_come(self):
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        status, payload, _ = await self.http(
            "POST", f"/api/release-gates/{view['gate_id']}/cancel",
            {"reason": "the bench is needed"},
        )
        self.assertEqual(202, status, payload)
        self.assertTrue(payload["gate"]["cancel_requested"])
        self.assertEqual(
            ["smoke"], [entry["stage"] for entry in payload["canceled"]]
        )
        # Each running child was asked to cancel; the runner answers as it
        # does for a group.
        for runner in (linux, mac):
            message = await runner.recv()
            self.assertEqual("cancel", message["type"])
            await runner.send("cancel_accepted", run_id=message["run_id"])
        group = self.controller.groups[
            self.stage_rows(payload["gate"])["smoke"]["group_id"]
        ]
        for runner in (linux, mac):
            run_id = next(
                entry["run_id"] for entry in group.members
                if entry["host"] == runner.host
            )
            await self.finish(runner, run_id, "canceled")
        await wait_until(
            lambda: not self.controller.view_gate(
                self.controller.gates[view["gate_id"]]
            )["active"],
            timeout=TIMEOUT_S,
        )
        final = await self.gate(view["gate_id"])
        self.assertEqual(CANCELED, final["status"])
        self.assertEqual(
            [CANCELED] * 4, [row["status"] for row in final["stages"]]
        )
        self.assertIn("the bench is needed", final["stages"][1]["reason"])
        # A gate that is over cannot be cancelled again.
        status, payload, _ = await self.http(
            "POST", f"/api/release-gates/{view['gate_id']}/cancel", {"reason": ""}
        )
        self.assertEqual(409, status, payload)

    async def test_a_stored_gate_is_read_back_after_a_restart(self):
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        gate_id = view["gate_id"]
        await self.server.stop()
        await self.start_controller()
        self.assertIn(gate_id, self.controller.gates)
        again = self.controller.view_gate(self.controller.gates[gate_id])
        self.assertEqual(
            [row["stage"] for row in view["stages"]],
            [row["stage"] for row in again["stages"]],
        )

    async def test_the_history_lists_the_gates_newest_first(self):
        linux, mac = await self.runners()
        first = await self.start_gate([linux, mac])
        await self.finish_stage(first, "smoke", [linux, mac], "failed")
        await wait_until(
            lambda: not self.controller.view_gate(
                self.controller.gates[first["gate_id"]]
            )["active"],
            timeout=TIMEOUT_S,
        )
        second = await self.start_gate(
            [linux, mac],
            run_ids={LINUX: "smoke-2-linux", MAC: "smoke-2-mac"},
        )
        status, payload, _ = await self.http("GET", "/api/release-gates")
        self.assertEqual(200, status, payload)
        self.assertEqual(
            [second["gate_id"], first["gate_id"]],
            [gate["gate_id"] for gate in payload["gates"]],
        )


class Collect:
    """A notifier that keeps the notices instead of sending them."""

    enabled = True
    log = staticmethod(lambda message: None)

    class config:
        subject_prefix = ""
        gui_base_url = "http://127.0.0.1:8765"

    def __init__(self):
        self.notices = []

    async def deliver(self, notice):
        self.notices.append(notice)


class GateBusyHostTests(GateControllerTestCase):
    """A host something else is using is not a stage that failed."""

    async def test_a_busy_host_leaves_the_stage_to_be_tried_again(self):
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        await self.finish_stage(view, "smoke", [linux, mac])
        await self.accept([linux, mac], {LINUX: "linux-1", MAC: "mac-1"})
        await self.settled(view["gate_id"], "linux", RUNNING)
        # The Mac's own stage ends, and a run of somebody else's starts there
        # before the writer stage can have it.
        await self.finish(mac, "mac-1", "passed")
        post, message = await self.start_run(mac, MAC)
        await mac.send(
            "run_accepted", request_id=message["request_id"], run_id="other-1")
        status, payload, _ = await post
        self.assertEqual(201, status, payload)
        await self.finish(linux, "linux-1", "passed")
        # The writer wants both hosts and one is taken: it waits, pending,
        # rather than being recorded as a stage that failed.
        await asyncio.sleep(0.4)
        rows = self.stage_rows(await self.gate(view["gate_id"]))
        self.assertEqual(PENDING, rows["writer"]["status"])
        self.assertEqual(RUNNING, (await self.gate(view["gate_id"]))["status"])
        # Once that run ends, the stage is tried again and starts.
        await self.finish(mac, "other-1", "passed")
        await self.accept([linux, mac], {LINUX: "writer-1", MAC: "writer-2"})
        await self.settled(view["gate_id"], "writer", RUNNING)


class GateNoticeTests(GateControllerTestCase):
    """One completion notice for the gate, and none for its stages (#392)."""

    async def test_a_gate_that_passed_is_reported_when_its_last_stage_ends(self):
        # The run that ends the last stage leaves the gate terminal, so
        # nothing but that run's own end would ever reach the gate's notice.
        notifier = Collect()
        self.controller.notifier = notifier
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        await self.finish_stage(view, "smoke", [linux, mac])
        await self.accept([linux, mac], {LINUX: "linux-1", MAC: "mac-1"})
        await self.settled(view["gate_id"], "linux", RUNNING)
        await self.finish(linux, "linux-1", "passed")
        await self.finish(mac, "mac-1", "passed")
        await self.settled(view["gate_id"], "writer", RUNNING, PENDING)
        await self.accept([linux, mac], {LINUX: "writer-1", MAC: "writer-2"})
        await self.settled(view["gate_id"], "writer", RUNNING)
        await self.finish(linux, "writer-1", "passed")
        await self.finish(mac, "writer-2", "passed")
        await wait_until(lambda: notifier.notices, timeout=TIMEOUT_S)
        await asyncio.sleep(0.2)
        self.assertEqual(
            [f"gate-{view['gate_id']}"],
            [notice.key for notice in notifier.notices],
        )
        notice = notifier.notices[0]
        self.assertIn("PASSED", notice.subject)
        self.assertIn("4 of 4 required stages passed", notice.body)
        # The link opens the gate's own panel, not a run that does not exist.
        self.assertIn(f"/ui/#gate={view['gate_id']}", notice.body)

    async def test_one_notice_for_the_gate_and_none_for_its_stages(self):
        notifier = Collect()
        notices = notifier.notices
        self.controller.notifier = notifier
        linux, mac = await self.runners()
        view = await self.start_gate([linux, mac])
        await self.finish_stage(view, "smoke", [linux, mac], "failed")
        await wait_until(
            lambda: not self.controller.view_gate(
                self.controller.gates[view["gate_id"]]
            )["active"],
            timeout=TIMEOUT_S,
        )
        await wait_until(lambda: notices, timeout=TIMEOUT_S)
        await asyncio.sleep(0.2)
        self.assertEqual(
            [f"gate-{view['gate_id']}"], [notice.key for notice in notices]
        )
        body = notices[0].body
        self.assertIn(view["gate_id"], body)
        self.assertIn("smoke: failed", body)
        self.assertIn("installer acceptance", body)
        self.assertIn(TEST_CONFIG, notices[0].subject)


class GuiWiringTests(unittest.TestCase):
    """The page's gate half, checked statically as the rest of it is."""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        cls.css = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    def test_run_setup_offers_a_gate_beside_one_plan_and_suite(self):
        self.assertIn('<select id="what"', self.html)
        self.assertIn('<option value="gate">', self.html)
        self.assertIn('id="gate-config"', self.html)
        self.assertIn('id="gate-bootloader"', self.html)
        # And a panel of its own for the matrix, with a cancel button.
        for element in ("gate-panel", "gate-rows", "gate-cancel", "gate-overview"):
            self.assertIn(f'id="{element}"', self.html)
        self.assertIn(".panel-gate { grid-area: gate; }", self.css)

    def test_the_page_wires_to_the_gate_endpoints(self):
        for endpoint in (
            '"/api/release-gates"',
            "`/api/release-configurations?${query.toString()}`",
            "`/api/release-gates/${encodeURIComponent(gateId)}`",
            "`${gatePath(gateId)}/cancel`",
        ):
            self.assertIn(endpoint, self.js)

    def test_the_configuration_decides_the_plan_fields(self):
        # In gate mode the plan, suite, build and bootloader fields are held:
        # the configuration decides all of them.
        self.assertIn("if (gate) holdPlanFields();", self.js)
        self.assertIn("if (gateMode()) holdPlanFields();", self.js)
        self.assertIn('const PLAN_FIELDS = ["plan", "suite", "build", "flash-build", "bootloader"];',
                      self.js)

    def test_run_is_held_while_the_graph_cannot_be_run(self):
        self.assertIn("reason = gateRefusal();", self.js)
        self.assertIn("if (preflight.passed) return \"\";", self.js)

    def test_a_running_gate_is_asked_for_again_and_can_be_reopened(self):
        self.assertIn("const GATE_REFRESH_MS = 5000;", self.js)
        self.assertIn("if (!state.gate || !state.gate.active) return;", self.js)
        # The gate is in the address, so a reload comes back to it.
        self.assertIn("window.location.hash = `gate=${encodeURIComponent(gate.gate_id)}`;",
                      self.js)
        self.assertIn("openGateFromAddress();", self.js)

    def test_the_screenshots_of_this_change_are_committed(self):
        shots = REPOSITORY / "docs" / "screenshots"
        for name in (
            "403-run-setup-before.png", "403-run-setup-after.png",
            "403-gate-after.png",
        ):
            self.assertTrue((shots / name).is_file(), name)


class CommandLineTests(unittest.TestCase):
    """``release-gate`` shows a configuration without starting anything."""

    def show(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["release-gate", *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_it_prints_the_stages_the_waves_and_the_preflight(self):
        code, out, _err = self.show(
            "--hardware", str(REPOSITORY / "hardware.example.yaml"))
        self.assertEqual(0, code, out)
        self.assertIn("release-comprehensive on Host_Linux, Host_Mac", out)
        self.assertIn("wave 1: fast-only", out)
        self.assertIn("classic: classic/classic-comprehensive", out)
        self.assertIn("preflight:", out)
        self.assertIn("estimated wall time", out)
        # The gates it does not measure are named rather than left out.
        self.assertIn("installer acceptance", out)
        # And the bootloader stage is not there unless it is asked for.
        self.assertNotIn("bootloader: fast/fast", out)
        code, out, _err = self.show(
            "--hardware", str(REPOSITORY / "hardware.example.yaml"),
            "--with-bootloader")
        self.assertEqual(0, code, out)
        self.assertIn("bootloader: fast/fast", out)

    def test_json_carries_every_dropped_test(self):
        code, out, _err = self.show(
            "--hardware", str(REPOSITORY / "hardware.example.yaml"), "--json")
        self.assertEqual(0, code, out)
        view = json.loads(out)
        fast = next(stage for stage in view["stages"] if stage["stage"] == "fast-only")
        self.assertTrue(len(fast["dropped_tests"]) > 4)
        self.assertEqual(
            ["Debug register read", "Spectrum RSSI sanity"], fast["tests"]
        )

    def test_it_needs_hosts_and_a_ref_it_cannot_guess(self):
        # Refused the way every other harness command refuses: the reason on
        # standard error and exit 2.
        code, _out, err = self.show()
        self.assertEqual(2, code)
        self.assertIn("needs the hosts", err)
        code, _out, err = self.show(
            "--hardware", str(REPOSITORY / "hardware.example.yaml"),
            "--host", "Host_Nowhere")
        self.assertEqual(2, code)
        self.assertIn("not a Host", err)
        code, _out, err = self.show(
            "--hardware", str(REPOSITORY / "hardware.example.yaml"),
            "--controller", "http://127.0.0.1:8765")
        self.assertEqual(2, code)
        self.assertIn("needs --ref", err)

    def test_only_a_loopback_controller_is_dialed(self):
        for url in ("http://10.0.0.5:8765", "https://example.invalid",
                    "ftp://127.0.0.1"):
            with self.subTest(url):
                code, _out, err = self.show(
                    "--hardware", str(REPOSITORY / "hardware.example.yaml"),
                    "--ref", "main", "--controller", url)
                self.assertEqual(2, code)
                self.assertIn("loopback", err)

    def test_a_bench_that_cannot_run_a_required_stage_exits_non_zero(self):
        code, out, _err = self.show(
            "--hardware", str(REPOSITORY / "hardware.example.yaml"),
            "--host", "Host_Mac")
        self.assertEqual(1, code)
        self.assertIn("FAIL every required stage has a host", out)


if __name__ == "__main__":
    unittest.main()
