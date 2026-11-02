"""Durations learned from finished runs (ubertooth_harness/learned.py).

No hardware and no runner: the samples come from synthetic run.jsonl
records, and the API tests drive the controller on 127.0.0.1 with a scripted
protocol-v2 runner, as the other controller tests do.
"""

import asyncio
from dataclasses import replace
import datetime
import json
from pathlib import Path
import tempfile
import threading
from types import MappingProxyType
import unittest
from unittest.mock import patch

from ubertooth_harness.catalog import Estimate, builtin_catalog
from ubertooth_harness.controller import Controller
from ubertooth_harness.gui import (
    estimate_fields,
    learned_stages,
    list_plans,
    planned_invocations,
    run_stage_estimates,
)
from ubertooth_harness.learned import (
    MAX_SAMPLES,
    STORE_FILE,
    EstimateStore,
    Key,
    invocation_key,
)
from ubertooth_harness.plan import load_plan

try:
    from test_controller import (
        TIMEOUT_S, ControllerTestCase, FakeRunner, wait_until,
    )
except ImportError:  # run as tests.test_learned_estimates
    from tests.test_controller import (
        TIMEOUT_S, ControllerTestCase, FakeRunner, wait_until,
    )

REPOSITORY = Path(__file__).resolve().parents[1]
BASE = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
HOST = "Host_A"
# A test whose estimate scales with its duration: 9 s plus 20 x duration.
RAW_RECEIVE = {"coverage": "comprehensive", "duration": "3s"}
# A test with a fixed estimate (15.8 s) and no scaling argument.
VALIDATION = {"coverage": "smoke"}


def stamp(offset: float) -> str:
    """A run record's timestamp: UTC, milliseconds, no offset suffix."""
    moment = BASE + datetime.timedelta(seconds=offset)
    return moment.replace(tzinfo=None).isoformat(timespec="milliseconds")


def record(kind, status, at, **details) -> dict:
    return {
        "schema_version": 1, "run_id": "run-1", "sequence": 1,
        "timestamp": stamp(at) if at is not None else None,
        "event_type": kind, "status": status, "details": details,
    }


def passed_records(
    name, arguments, durations, *, host=HOST, status="passed", first=0.0
) -> list[dict]:
    """One test_started/test_finished pair per duration, back to back."""
    events, at = [], first
    for index, seconds in enumerate(durations, 1):
        invocation = f"{index:03d}-{name}"
        events.append(record("test_started", "running", at, invocation_id=invocation,
                             test_name=name, host=host, arguments=arguments))
        at += seconds
        events.append(record("test_finished", status, at, invocation_id=invocation,
                             test_name=name))
        at += 0.5
    return events


def key_of(name, arguments, host=HOST) -> Key:
    found = invocation_key(builtin_catalog(), name, arguments, host)
    assert found is not None, name
    return found[0]


class StoreTestCase(unittest.TestCase):

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.path = self.root / STORE_FILE
        self.logs = []
        self.store = EstimateStore.load(self.path, log=self.logs.append)

    def learn(self, events, *, host=HOST, store=None):
        return (store or self.store).learn_from_run(events, host=host)


class LearningTests(StoreTestCase):

    def test_passed_invocations_become_samples_keyed_by_the_arguments(self):
        kept = self.learn(passed_records("Raw receive", RAW_RECEIVE, [70.0, 68.0]))
        self.assertEqual(2, kept)
        key = key_of("Raw receive", RAW_RECEIVE)
        self.assertEqual(
            Key("test", "Raw receive", HOST, "comprehensive", ("duration", 3.0)), key
        )
        self.assertEqual([70.0, 68.0], self.store.samples(key))

    def test_only_passed_invocations_count(self):
        for status in ("failed", "skipped", "canceled", "not_applicable", "running"):
            with self.subTest(status):
                store = EstimateStore(self.path, log=self.logs.append)
                self.assertEqual(
                    0,
                    self.learn(
                        passed_records("Raw receive", RAW_RECEIVE, [70.0], status=status),
                        store=store,
                    ),
                )
                self.assertEqual([], store.keys())

    def test_records_a_sample_cannot_be_read_from_are_ignored(self):
        name = "Raw receive"
        finished = record("test_finished", "passed", 5.0, invocation_id="001-x",
                          test_name=name)
        # No test_started: nothing to measure from.
        self.assertEqual(0, self.learn([finished]))
        # Unparsable and missing timestamps, and a clock that went backwards.
        started = record("test_started", "running", 10.0, invocation_id="001-x",
                         test_name=name, host=HOST, arguments=RAW_RECEIVE)
        for broken in ({"timestamp": "not a time"}, {"timestamp": None},
                       {"timestamp": stamp(1.0)}):
            with self.subTest(broken):
                self.assertEqual(0, self.learn([started, {**finished, **broken}]))
        # An unknown test name, and arguments the catalog refuses.
        self.assertEqual(0, self.learn(passed_records("No such test", {}, [3.0])))
        self.assertEqual(
            0, self.learn(passed_records("Raw receive", {"coverage": "wrong"}, [3.0]))
        )
        # Not a mapping, and a test_finished with no invocation.
        self.assertEqual(0, self.learn(["not an event", 7, None]))
        self.assertEqual(
            0, self.learn([record("test_finished", "passed", 1.0, test_name=name)])
        )

    def test_the_invocations_own_host_wins_over_the_run_target(self):
        self.learn(
            passed_records("Argument validation", VALIDATION, [10.0], host="Host_Mac"),
            host="Host_Linux",
        )
        self.assertEqual(
            [10.0], self.store.samples(key_of("Argument validation", VALIDATION,
                                              "Host_Mac"))
        )

    def test_samples_of_different_hosts_profiles_and_durations_stay_apart(self):
        self.learn(passed_records("Raw receive", RAW_RECEIVE, [70.0]))
        self.learn(passed_records("Raw receive", RAW_RECEIVE, [71.0], host="Host_B"),
                   host="Host_B")
        self.learn(passed_records(
            "Raw receive", {"coverage": "smoke", "duration": "3s"}, [4.0]))
        self.learn(passed_records(
            "Raw receive", {"coverage": "comprehensive", "duration": "6s"}, [130.0]))
        self.assertEqual(
            {
                Key("test", "Raw receive", HOST, "comprehensive", ("duration", 3.0)):
                    [70.0],
                Key("test", "Raw receive", "Host_B", "comprehensive", ("duration", 3.0)):
                    [71.0],
                Key("test", "Raw receive", HOST, "smoke", ("duration", 3.0)): [4.0],
                Key("test", "Raw receive", HOST, "comprehensive", ("duration", 6.0)):
                    [130.0],
            },
            {key: self.store.samples(key) for key in self.store.keys()},
        )

    def test_equal_durations_written_differently_share_a_key(self):
        self.learn(passed_records(
            "Spectrum analyzer", {"coverage": "smoke", "duration": "3s"}, [10.0]))
        self.learn(passed_records(
            "Spectrum analyzer", {"coverage": "smoke", "duration": "3000ms"}, [11.0]))
        (key,) = self.store.keys()
        self.assertEqual(("duration", 3.0), key.units)
        self.assertEqual([10.0, 11.0], self.store.samples(key))

    def test_a_test_that_scaled_by_two_arguments_is_left_to_the_catalog(self):
        # No catalog estimate scales by a duration and a count at once; one
        # key could carry only one of them, so such a test is not learned.
        catalog = builtin_catalog()
        definition = replace(
            catalog.get("Raw receive"),
            estimates=MappingProxyType({
                "smoke": Estimate(9.0, per_duration=20, per_count=("flashes", 1)),
            }),
        )
        with patch.object(catalog, "get", return_value=definition):
            self.assertIsNone(invocation_key(
                catalog, "Raw receive", {"coverage": "smoke", "duration": "3s"}, HOST))

    def test_flash_reliability_is_keyed_by_its_flash_count(self):
        arguments = {
            "platforms": ["macos"], "flashes": 4, "KGV sha256": "e" * 64,
            "KGV revision": "r", "image sequence": ["KGV", "test build"],
        }
        self.learn(passed_records("Flash reliability", arguments, [12.0]))
        (key,) = self.store.keys()
        self.assertEqual(("flashes", 4.0), key.units)

    def test_only_the_last_samples_are_kept(self):
        self.learn(passed_records("Argument validation", VALIDATION,
                                [float(n) for n in range(1, MAX_SAMPLES + 4)]))
        self.assertEqual(
            [float(n) for n in range(4, MAX_SAMPLES + 4)],
            self.store.samples(key_of("Argument validation", VALIDATION)),
        )


class MedianTests(StoreTestCase):

    def samples(self, values, name="Argument validation", arguments=VALIDATION):
        self.learn(passed_records(name, arguments, values))
        return key_of(name, arguments)

    def test_the_median_needs_three_samples(self):
        key = self.samples([10.0, 12.0])
        self.assertIsNone(self.store.lookup(key))
        self.learn(passed_records("Argument validation", VALIDATION, [14.0]))
        learned = self.store.lookup(key)
        self.assertEqual(12.0, learned.seconds)
        self.assertEqual(3, learned.samples)
        self.assertFalse(learned.scaled)

    def test_the_median_ignores_one_slow_run(self):
        key = self.samples([10.0, 11.0, 12.0, 60.0])
        self.assertEqual(11.5, self.store.lookup(key).seconds)

    def test_an_outlier_is_dropped_once_the_median_rests_on_three_samples(self):
        key = self.samples([10.0, 11.0, 12.0])
        self.assertFalse(self.store.add(key, 500.0))
        self.assertFalse(self.store.add(key, 0.2))
        self.assertEqual([10.0, 11.0, 12.0], self.store.samples(key))
        self.assertEqual(2, self.store.outliers)
        self.assertTrue(any("outlier" in line for line in self.logs))
        # A good sample in between means the next odd one is an outlier
        # again, not the start of a new window.
        self.assertTrue(self.store.add(key, 11.5))
        self.assertFalse(self.store.add(key, 500.0))

    def test_a_test_that_really_got_slower_is_learned_again(self):
        key = self.samples([10.0, 11.0, 12.0])
        for value in (500.0, 505.0):
            self.assertFalse(self.store.add(key, value))
        # Three in a row that agree are not outliers: the test itself now
        # takes that long, and those readings are the new window.
        self.assertTrue(self.store.add(key, 510.0))
        self.assertEqual([500.0, 505.0, 510.0], self.store.samples(key))
        self.assertEqual(505.0, self.store.lookup(key).seconds)
        self.assertTrue(any("not 11s; starting again" in line for line in self.logs))

    def test_rejected_samples_that_disagree_stay_outliers(self):
        key = self.samples([10.0, 11.0, 12.0])
        # Wild readings on both sides say nothing about a new duration.
        for value in (500.0, 0.2, 480.0, 0.3, 510.0):
            self.assertFalse(self.store.add(key, value))
        self.assertEqual([10.0, 11.0, 12.0], self.store.samples(key))
        self.assertEqual(11.0, self.store.lookup(key).seconds)
        # Two that agree after them are still not enough on their own.
        self.assertFalse(self.store.add(key, 505.0))
        self.assertTrue(self.store.add(key, 500.0))

    def test_ten_times_the_median_is_still_a_sample(self):
        # The bound is a factor of ten, so a run that took eleven seconds
        # where the median is eleven hundred milliseconds is kept.
        for value, kept in ((110.0, True), (1.1, True), (111.0, False), (1.0, False)):
            with self.subTest(value):
                store = EstimateStore(self.path, log=self.logs.append)
                key = key_of("Argument validation", VALIDATION)
                for sample in (10.0, 11.0, 12.0):
                    store.add(key, sample)
                self.assertEqual(kept, store.add(key, value))

    def test_one_odd_early_sample_cannot_lock_out_the_good_ones(self):
        key = key_of("Argument validation", VALIDATION)
        self.assertTrue(self.store.add(key, 900.0))
        for value in (10.0, 11.0, 12.0):
            self.assertTrue(self.store.add(key, value))
        # It still weighs on the median until the window moves past it.
        self.assertEqual(11.5, self.store.lookup(key).seconds)
        for value in (10.0, 11.0, 12.0, 10.5, 11.5, 12.5, 11.0):
            self.store.add(key, value)
        self.assertNotIn(900.0, self.store.samples(key))
        self.assertEqual(11.0, self.store.lookup(key).seconds)

    def test_unusable_sample_values_are_refused(self):
        key = key_of("Argument validation", VALIDATION)
        # Zero included: the records carry milliseconds, so a measured zero
        # means the pair of records is wrong, and three of them would learn
        # an estimate of no time at all.
        for value in (0, 0.0, -1.0, None, "12", True, float("inf"), float("nan"),
                      10**9):
            with self.subTest(value):
                self.assertFalse(self.store.add(key, value))
        self.assertEqual([], self.store.samples(key))
        self.assertIsNone(self.store.lookup(key))

    def test_the_store_keeps_a_bounded_number_of_keys(self):
        store = EstimateStore(self.path, log=self.logs.append, max_keys=3)
        established = key_of("Argument validation", VALIDATION)
        for value in (10.0, 11.0, 12.0):
            store.add(established, value)
        for seconds in range(1, 6):
            store.add(key_of("Spectrum analyzer",
                             {"coverage": "smoke", "duration": f"{seconds}s"}), 9.0)
        self.assertEqual(3, len(store.keys()))
        # The key with the most samples is never dropped for a one-off.
        self.assertEqual([10.0, 11.0, 12.0], store.samples(established))
        self.assertTrue(any("is full" in line for line in self.logs))

    def test_a_sample_below_the_catalogs_fixed_part_does_not_scale(self):
        # Raw receive: 9 s fixed plus 20 s per second of duration. A 3 s
        # capture that took 7 s contradicts the fixed part, so it says
        # nothing about the rate and is left out of the scaling.
        catalog = builtin_catalog()
        other = {"coverage": "comprehensive", "duration": "6s"}
        self.learn(passed_records("Raw receive", RAW_RECEIVE, [7.0, 8.0, 72.0]))
        self.assertIsNone(self.store.lookup_test(catalog, "Raw receive", other, HOST))
        self.learn(passed_records("Raw receive", RAW_RECEIVE, [73.0, 74.0]))
        # Only the three samples above 9 s: (72, 73, 74 - 9) / 3 per second.
        learned = self.store.lookup_test(catalog, "Raw receive", other, HOST)
        self.assertEqual(137.0, learned.seconds)
        self.assertEqual(3, learned.samples)

    def test_a_learned_rate_applies_to_another_duration(self):
        # Three comprehensive Raw receive runs at 3 s: 9 s fixed, 20 s per
        # second of duration in the catalog; measured 10 s plus 21 per second.
        self.learn(passed_records("Raw receive", RAW_RECEIVE, [72.0, 73.0, 74.0]))
        other = {"coverage": "comprehensive", "duration": "6s"}
        learned = self.store.lookup_test(
            builtin_catalog(), "Raw receive", other, HOST)
        # The rate rests on every duration of this test, this one included
        # once it has a sample or two of its own.
        # (73 - 9) / 3 = 21.33 per second, so 9 + 6 x 21.33.
        self.assertEqual(137.0, learned.seconds)
        self.assertEqual(3, learned.samples)
        self.assertTrue(learned.scaled)
        # The exact duration wins as soon as it has samples of its own.
        self.learn(passed_records("Raw receive", other, [130.0, 131.0, 132.0]))
        exact = self.store.lookup_test(builtin_catalog(), "Raw receive", other, HOST)
        self.assertEqual(131.0, exact.seconds)
        self.assertFalse(exact.scaled)

    def test_a_rate_counts_this_durations_own_samples_too(self):
        # Two 6 s runs and one 3 s run: too few at 6 s for a median, so the
        # rate of all three stands in, and the count says three.
        catalog = builtin_catalog()
        other = {"coverage": "comprehensive", "duration": "6s"}
        self.learn(passed_records("Raw receive", other, [129.0, 131.0]))
        self.learn(passed_records("Raw receive", RAW_RECEIVE, [69.0]))
        learned = self.store.lookup_test(catalog, "Raw receive", other, HOST)
        # Rates of (129 - 9)/6, (131 - 9)/6 and (69 - 9)/3: a median of 20.
        self.assertEqual(129.0, learned.seconds)
        self.assertEqual(3, learned.samples)
        self.assertTrue(learned.scaled)

    def test_nothing_is_scaled_without_units_or_without_samples(self):
        catalog = builtin_catalog()
        # A fixed estimate has no units to scale by.
        self.learn(passed_records("Argument validation", VALIDATION, [10.0, 11.0]))
        self.assertIsNone(
            self.store.lookup_test(catalog, "Argument validation", VALIDATION, HOST))
        # Another test's samples, and another host's, do not scale this one.
        self.learn(passed_records("Raw receive", RAW_RECEIVE, [72.0, 73.0, 74.0]))
        self.assertIsNone(self.store.lookup_test(
            catalog, "Raw receive", {"coverage": "smoke", "duration": "6s"}, HOST))
        self.assertIsNone(self.store.lookup_test(
            catalog, "Raw receive", RAW_RECEIVE, "Host_B"))
        self.assertIsNone(self.store.lookup_test(catalog, "Raw receive", RAW_RECEIVE, ""))


class StageTests(StoreTestCase):

    def test_build_preflight_and_flash_durations_are_learned(self):
        events = [
            record("build_started", "running", 0.0, with_firmware=False),
            record("build_finished", "passed", 300.0, reason=""),
            record("preflight_started", "running", 301.0, requested_checks=3),
            record("preflight_finished", "passed", 302.5, requested_checks=3),
            record("flash_run_finished", "passed", 400.0, duration_s=62.5,
                   requested_flashes=4, invocation_id="003-Flash reliability"),
        ]
        self.assertEqual(3, self.learn(events))
        self.assertEqual(
            {
                Key("build", "", HOST, "host tools", None): [300.0],
                Key("preflight", "", HOST, "", ("checks", 3.0)): [1.5],
                Key("flash", "", HOST, "", ("flashes", 4.0)): [62.5],
            },
            {key: self.store.samples(key) for key in self.store.keys()},
        )

    def test_a_firmware_build_is_a_key_of_its_own(self):
        self.learn([record("build_started", "running", 0.0, with_firmware=True),
                    record("build_finished", "passed", 900.0)])
        self.assertEqual("with firmware", self.store.keys()[0].profile)

    def test_a_flash_run_without_a_usable_count_is_not_learned(self):
        for count in (None, 0, -2, True, "four"):
            with self.subTest(count):
                self.assertEqual(0, self.learn([
                    record("flash_run_finished", "passed", 10.0, duration_s=62.5,
                           requested_flashes=count)]))
        self.assertEqual([], self.store.keys())

    def test_a_stage_that_did_not_pass_teaches_nothing(self):
        self.assertEqual(0, self.learn([
            record("build_started", "running", 0.0, with_firmware=False),
            record("build_finished", "failed", 10.0, reason="cmake failed"),
            record("preflight_started", "running", 11.0, requested_checks=2),
            record("preflight_finished", "canceled", 12.0, requested_checks=2),
            record("flash_run_finished", "failed", 20.0, duration_s=5.0,
                   requested_flashes=2),
        ]))

    def test_preflight_scales_with_its_number_of_checks(self):
        for _ in range(3):
            self.learn([record("preflight_started", "running", 0.0, requested_checks=2),
                        record("preflight_finished", "passed", 2.0, requested_checks=2)])
        learned = self.store.lookup_stage("preflight", HOST, units=("checks", 4))
        self.assertEqual(4.0, learned.seconds)
        self.assertTrue(learned.scaled)
        self.assertIsNone(self.store.lookup_stage("preflight", "Host_B",
                                                  units=("checks", 2)))
        self.assertIsNone(self.store.lookup_stage("build", HOST, profile="host tools"))

    def test_entries_report_what_is_known_per_stage(self):
        for seconds in (300.0, 310.0, 320.0):
            self.learn([record("build_started", "running", 0.0, with_firmware=False),
                        record("build_finished", "passed", seconds)])
        self.learn([record("preflight_started", "running", 0.0, requested_checks=2),
                    record("preflight_finished", "passed", 2.0, requested_checks=2)])
        self.assertEqual(
            [
                {"stage": "build", "name": "", "host": HOST, "profile": "host tools",
                 "units": None, "estimated_s": 310.0, "estimate_source": "learned",
                 "estimate_samples": 3, "estimate_scaled": False,
                 "per_unit_s": None},
                {"stage": "preflight", "name": "", "host": HOST, "profile": "",
                 "units": {"argument": "checks", "value": 2.0}, "estimated_s": None,
                 "estimate_source": "none", "estimate_samples": 1,
                 "estimate_scaled": False, "per_unit_s": None},
            ],
            self.store.entries(host=HOST, stages=("build", "preflight", "flash")),
        )
        self.assertEqual([], self.store.entries(host="Host_B"))

    def test_entries_without_a_stage_filter_include_the_tests(self):
        self.learn(passed_records("Raw receive", RAW_RECEIVE, [70.0, 71.0, 72.0]))
        self.learn([record("preflight_started", "running", 0.0, requested_checks=2),
                    record("preflight_finished", "passed", 2.0, requested_checks=2)])
        entries = {entry["stage"]: entry for entry in self.store.entries()}
        self.assertEqual({"test", "preflight"}, set(entries))
        self.assertEqual(71.0, entries["test"]["estimated_s"])
        # A test's fixed part belongs to the catalog, so the store reports
        # no rate for it.
        self.assertIsNone(entries["test"]["per_unit_s"])
        self.assertEqual({"argument": "duration", "value": 3.0},
                         entries["test"]["units"])


class PersistenceTests(StoreTestCase):

    def test_samples_survive_a_restart(self):
        key = key_of("Argument validation", VALIDATION)
        self.learn(passed_records("Argument validation", VALIDATION, [10.0, 11.0, 12.0]))
        self.assertTrue(self.path.is_file())
        reopened = EstimateStore.load(self.path, log=self.logs.append)
        self.assertEqual([10.0, 11.0, 12.0], reopened.samples(key))
        self.assertEqual(11.0, reopened.lookup(key).seconds)
        # Learning again appends to what was stored.
        reopened.learn_from_run(
            passed_records("Argument validation", VALIDATION, [13.0]), host=HOST)
        self.assertEqual(
            [10.0, 11.0, 12.0, 13.0],
            EstimateStore.load(self.path, log=self.logs.append).samples(key),
        )

    def test_the_file_is_replaced_atomically_and_is_readable_json(self):
        self.learn(passed_records("Argument validation", VALIDATION, [10.0]))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(1, data["version"])
        self.assertEqual(
            [{"stage": "test", "name": "Argument validation", "host": HOST,
              "profile": "smoke", "units": None, "samples": [10.0]}],
            data["entries"],
        )
        self.assertIsInstance(data["updated_at"], str)
        # No temporary file left beside it.
        self.assertEqual([STORE_FILE], [path.name for path in self.root.iterdir()])

    def test_nothing_is_written_when_nothing_was_learned(self):
        self.assertEqual(0, self.learn(passed_records(
            "Argument validation", VALIDATION, [10.0], status="failed")))
        self.assertFalse(self.path.exists())

    def test_a_file_holding_more_keys_than_the_bound_is_trimmed_at_load(self):
        self.path.write_text(json.dumps({"version": 1, "entries": [
            {"stage": "test", "name": f"Test {index}", "host": HOST,
             "profile": "smoke", "units": None,
             "samples": [1.0] * (index + 1)}
            for index in range(5)
        ]}), encoding="utf-8")
        store = EstimateStore.load(self.path, log=self.logs.append, max_keys=2)
        # The best-established keys stay; the rest make way at once rather
        # than one at a time on every later sample.
        self.assertEqual(["Test 3", "Test 4"],
                         sorted(key.name for key in store.keys()))
        self.assertTrue(any("is full" in line for line in self.logs))

    def test_an_unusable_file_is_reported_and_treated_as_empty(self):
        for content in ("", "not json", "[]", '{"entries": 3}',
                        '{"entries": [{"stage": "test"}]}',
                        '{"entries": [{"stage": "test", "name": "x", "host": "h", '
                        '"profile": "", "units": {"argument": "", "value": 1}, '
                        '"samples": [1]}]}',
                        '{"entries": [{"stage": "test", "name": "x", "host": "h", '
                        '"profile": "", "units": null, "samples": ["slow"]}]}'):
            with self.subTest(content):
                self.path.write_text(content, encoding="utf-8")
                store = EstimateStore.load(self.path, log=self.logs.append)
                self.assertEqual([], store.keys())
        self.assertTrue(self.logs)

    def test_a_directory_in_the_way_is_logged_not_raised(self):
        store = EstimateStore(self.root, log=self.logs.append)  # the directory itself
        store.add(key_of("Argument validation", VALIDATION), 10.0)
        store.save()
        self.assertTrue(any("could not write" in line for line in self.logs))


class PlanEstimateTests(StoreTestCase):
    """What /api/plans and the run's invocations report, without a server."""

    def fast_step(self):
        plan = load_plan(REPOSITORY / "testplans" / "fast.yaml", builtin_catalog())
        return plan.suites["fast"][0]

    def test_a_target_gets_its_own_learned_estimate(self):
        step = self.fast_step()
        key = key_of(step.test_name, step.arguments)
        for value in (4.0, 5.0, 6.0):
            self.store.add(key, value)
        plans = {plan["name"]: plan for plan in list_plans(
            REPOSITORY / "testplans", estimates=self.store, target=HOST)}
        (suite,) = [s for s in plans["fast"]["suites"] if s["name"] == "fast"]
        test = suite["tests"][0]
        self.assertEqual(step.test_name, test["name"])
        self.assertEqual(5.0, test["estimated_s"])
        self.assertEqual("learned", test["estimate_source"])
        self.assertEqual(3, test["estimate_samples"])
        self.assertNotEqual(5.0, test["catalog_estimated_s"])
        # The suite total counts the learned value.
        self.assertAlmostEqual(
            sum(item["estimated_s"] for item in suite["tests"]),
            suite["estimated_s"], delta=0.05,
        )
        # Another target, and no target at all, keep the catalog estimate.
        for other in (list_plans(REPOSITORY / "testplans", estimates=self.store,
                                 target="Host_B"),
                      list_plans(REPOSITORY / "testplans", estimates=self.store),
                      list_plans(REPOSITORY / "testplans")):
            plan = {item["name"]: item for item in other}["fast"]
            first = [s for s in plan["suites"] if s["name"] == "fast"][0]["tests"][0]
            self.assertEqual(test["catalog_estimated_s"], first["estimated_s"])
            self.assertEqual("catalog", first["estimate_source"])
            self.assertEqual(0, first["estimate_samples"])

    def test_estimate_fields_of_a_test_neither_source_knows(self):
        self.assertEqual(
            {"estimated_s": None, "catalog_estimated_s": None,
             "estimate_source": "none", "estimate_samples": 0,
             "estimate_scaled": False},
            estimate_fields(builtin_catalog(), "No such test", {},
                            estimates=self.store, host=HOST),
        )

    def test_planned_invocations_use_each_invocations_host(self):
        manifest = {"event_type": "manifest", "status": "passed", "details": {
            "requested_preflight_checks": 2,
            "manifest": {"invocations": [
                {"id": "001-Raw receive", "test_name": "Raw receive",
                 "host": host, "arguments": RAW_RECEIVE}
                for host in (HOST, "Host_B")
            ]},
        }}
        key = key_of("Raw receive", RAW_RECEIVE)
        for value in (70.0, 71.0, 72.0):
            self.store.add(key, value)
        planned = planned_invocations([manifest], estimates=self.store)
        self.assertEqual([71.0, 69.0], [item["estimated_s"] for item in planned])
        self.assertEqual(["learned", "catalog"],
                         [item["estimate_source"] for item in planned])
        self.assertEqual([3, 0], [item["estimate_samples"] for item in planned])
        self.assertEqual([69.0, 69.0],
                         [item["catalog_estimated_s"] for item in planned])

    def test_run_stage_estimates_follow_the_runs_own_plan(self):
        events = [{"event_type": "manifest", "status": "passed", "details": {
            "requested_preflight_checks": 3, "manifest": {"invocations": []}}}]
        for seconds in (300.0, 310.0, 320.0):
            self.store.add(Key("build", "", HOST, "host tools", None), seconds)
        for seconds in (1.0, 1.2, 1.4):
            self.store.add(Key("preflight", "", HOST, "", ("checks", 3.0)), seconds)
        self.assertEqual(
            [
                {"stage": "build", "profile": "host tools", "units": None,
                 "estimated_s": 310.0, "estimate_source": "learned",
                 "estimate_samples": 3, "estimate_scaled": False},
                {"stage": "preflight", "profile": "",
                 "units": {"argument": "checks", "value": 3.0},
                 "estimated_s": 1.2, "estimate_source": "learned",
                 "estimate_samples": 3, "estimate_scaled": False},
            ],
            run_stage_estimates(self.store, events, HOST, build=True),
        )
        # A run that builds nothing, and one whose manifest has not arrived.
        self.assertEqual(
            ["preflight"],
            [item["stage"] for item in run_stage_estimates(self.store, events, HOST)],
        )
        self.assertEqual([], run_stage_estimates(self.store, [], HOST, build=False))
        self.assertEqual([], run_stage_estimates(self.store, events, None, build=True))
        self.assertEqual([], run_stage_estimates(None, events, HOST, build=True))
        self.assertEqual([], learned_stages(None, HOST))
        self.assertEqual([], learned_stages(self.store, None))

    def test_a_firmware_build_is_estimated_as_a_firmware_build(self):
        # The two kinds of build are learned apart, and a run that asks for
        # the firmware image (#257) takes much the longer of them.
        for seconds in (300.0, 310.0, 320.0):
            self.store.add(Key("build", "", HOST, "host tools", None), seconds)
        for seconds in (600.0, 610.0, 620.0):
            self.store.add(Key("build", "", HOST, "with firmware", None), seconds)
        host_tools = run_stage_estimates(self.store, [], HOST, build=True)
        firmware = run_stage_estimates(
            self.store, [], HOST, build=True, with_firmware=True)
        self.assertEqual([("host tools", 310.0)],
                         [(s["profile"], s["estimated_s"]) for s in host_tools])
        self.assertEqual([("with firmware", 610.0)],
                         [(s["profile"], s["estimated_s"]) for s in firmware])
        # A run's own build_started record is the better answer than what it
        # was asked for: a run that builds the firmware only because it
        # flashes its boards says so there, and an adopted run has no request.
        started = [{"event_type": "build_started", "status": "running",
                    "details": {"with_firmware": True}}]
        self.assertEqual(
            [("with firmware", 610.0)],
            [(s["profile"], s["estimated_s"])
             for s in run_stage_estimates(self.store, started, HOST)],
        )


class ControllerLearningTests(ControllerTestCase):
    """The controller learns when a run ends, and reports it over the API."""

    async def finished_run(self, runner, records, run_id="run-1", status="passed"):
        message = await self.accepted_run(runner, run_id=run_id)
        for index, item in enumerate(records, 1):
            await runner.send("event", run_id=run_id,
                              event={**item, "run_id": run_id, "sequence": index})
        await runner.send("run_finished", request_id=message["request_id"],
                          run_id=run_id, status=status, exit_code=0,
                          run_directory="/absent")
        await wait_until(lambda: self.controller.runs[run_id].status == status)

    def samples(self, name="Argument validation", arguments=VALIDATION):
        return self.controller.estimates.samples(key_of(name, arguments))

    async def test_a_finished_run_teaches_and_is_persisted(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            for index in range(3):
                await self.finished_run(
                    runner,
                    passed_records("Argument validation", VALIDATION, [10.0 + index]),
                    run_id=f"run-{index}",
                )
                await wait_until(lambda n=index: len(self.samples()) == n + 1)
        self.assertEqual([10.0, 11.0, 12.0], self.samples())
        self.assertTrue((self.results / STORE_FILE).is_file())
        # A fresh controller on the same results directory keeps them.
        restarted = Controller(self.results, log=self.logs.append)
        learned = restarted.estimates.lookup(key_of("Argument validation", VALIDATION))
        self.assertEqual(11.0, learned.seconds)
        self.assertEqual(3, learned.samples)

    async def test_a_run_reported_finished_twice_is_learned_once(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            records = passed_records("Argument validation", VALIDATION, [10.0])
            message = await self.accepted_run(runner)
            for index, item in enumerate(records, 1):
                await runner.send("event", run_id="run-1",
                                  event={**item, "run_id": "run-1", "sequence": index})
            for _ in range(2):
                # A runner whose connection drops as it reports the end
                # reports it again once it has reconnected.
                await runner.send("run_finished", request_id=message["request_id"],
                                  run_id="run-1", status="passed", exit_code=0,
                                  run_directory="/absent")
            await wait_until(lambda: len(self.samples()) == 1)
            await runner.heartbeat()
            await wait_until(lambda: self.controller.targets[HOST].last_heartbeat)
        self.assertEqual([10.0], self.samples())
        record_file = json.loads(
            (self.results / "run-1" / "run.json").read_text(encoding="utf-8"))
        self.assertTrue(record_file["learned_durations"])

    async def test_a_failed_run_still_teaches_from_its_passing_tests(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            records = passed_records("Argument validation", VALIDATION, [10.0])
            records += passed_records("Raw receive", RAW_RECEIVE, [70.0],
                                    status="failed", first=100.0)
            await self.finished_run(runner, records, status="failed")
            await wait_until(lambda: len(self.samples()) == 1)
        self.assertEqual([10.0], self.samples())
        self.assertEqual([], self.samples("Raw receive", RAW_RECEIVE))

    async def test_two_overlapping_reports_of_one_end_learn_it_once(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            for index, item in enumerate(
                passed_records("Argument validation", VALIDATION, [10.0]), 1
            ):
                await runner.send("event", run_id="run-1",
                                  event={**item, "run_id": "run-1", "sequence": index})
            await wait_until(
                lambda: len(self.controller.read_events(
                    self.controller.runs["run-1"])) == 2)
            del message
        run = self.controller.runs["run-1"]
        run.finished_at, run.status = "now", "passed"
        # Two ends dispatched at once (a replaced connection finishes the
        # message it was already handling).
        kept = await asyncio.gather(self.controller.learn(run),
                                    self.controller.learn(run))
        self.assertEqual([1, 0], sorted(kept, reverse=True))
        self.assertEqual([10.0], self.samples())

    async def test_a_run_that_ended_unlearned_is_picked_up_at_startup(self):
        # The controller stopped between recording a run's end and taking
        # its durations; the next one finishes the job.
        directory = self.results / "run-old"
        directory.mkdir(parents=True)
        (directory / "run.json").write_text(json.dumps({
            "run_id": "run-old", "target": HOST, "status": "passed",
            "created_at": stamp(0.0), "finished_at": stamp(20.0),
            "plan": "fast", "suite": "fast", "learned_durations": False,
        }), encoding="utf-8")
        (directory / "run.jsonl").write_text("".join(
            json.dumps({**item, "run_id": "run-old"}) + "\n"
            for item in passed_records("Argument validation", VALIDATION, [10.0])
        ), encoding="utf-8")
        # A run stored before durations were learned at all has no such
        # field and is left alone.
        old = self.results / "run-older"
        old.mkdir(parents=True)
        (old / "run.json").write_text(json.dumps({
            "run_id": "run-older", "target": HOST, "status": "passed",
            "created_at": stamp(0.0), "finished_at": stamp(20.0),
        }), encoding="utf-8")
        (old / "run.jsonl").write_text("".join(
            json.dumps({**item, "run_id": "run-older"}) + "\n"
            for item in passed_records("Raw receive", RAW_RECEIVE, [70.0])
        ), encoding="utf-8")
        await self.server.stop()
        await self.start_controller()
        await wait_until(lambda: len(self.samples()) == 1)
        self.assertEqual([10.0], self.samples())
        self.assertEqual([], self.samples("Raw receive", RAW_RECEIVE))
        self.assertTrue(self.controller.runs["run-old"].learned_durations)
        # Learned once: starting again adds nothing.
        await self.server.stop()
        await self.start_controller()
        self.assertEqual(0, await self.controller.learn_pending())
        self.assertEqual([10.0], self.samples())

    async def test_a_run_cannot_take_the_name_of_the_store(self):
        # A generated run ID never collides, but nothing stops a runner
        # from reporting one, and a directory of that name would sit where
        # the learned durations are kept.
        # Either case: on a Mac's filesystem they are the same file. (A
        # name starting with a dot is refused by the protocol first.)
        for run_id in (STORE_FILE, STORE_FILE.upper()):
            with self.subTest(run_id):
                self.logs.clear()
                async with FakeRunner(self.port) as runner:
                    await self.registered(runner, active=run_id)
                    await runner.send("run_finished", request_id="req-1",
                                      run_id=run_id, status="passed", exit_code=0,
                                      run_directory="/absent")
                    await wait_until(
                        lambda: any("reserved" in line for line in self.logs))
                    # Refused, and the connection carries on.
                    await runner.heartbeat()
                    await wait_until(
                        lambda: self.controller.targets[HOST].last_heartbeat)
                self.assertNotIn(run_id, self.controller.runs)
                self.assertFalse((self.results / run_id).is_dir())

    async def test_a_run_still_running_is_not_learned_at_startup(self):
        # Its runner may still be running it; the end report, or a target
        # that registers without it, decides.
        directory = self.results / "run-active"
        directory.mkdir(parents=True)
        (directory / "run.json").write_text(json.dumps({
            "run_id": "run-active", "target": HOST, "status": "running",
            "created_at": stamp(0.0), "learned_durations": False,
        }), encoding="utf-8")
        (directory / "run.jsonl").write_text("".join(
            json.dumps({**item, "run_id": "run-active"}) + "\n"
            for item in passed_records("Argument validation", VALIDATION, [10.0])
        ), encoding="utf-8")
        await self.server.stop()
        await self.start_controller()
        self.assertEqual(0, await self.controller.learn_pending())
        self.assertEqual([], self.samples())
        self.assertFalse(self.controller.runs["run-active"].learned_durations)

    async def test_a_run_the_runner_no_longer_has_teaches_what_it_recorded(self):
        # The runner restarts; its run is lost, and what passed in it took
        # as long as its records say.
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
            for index, item in enumerate(
                passed_records("Argument validation", VALIDATION, [10.0]), 1
            ):
                await runner.send("event", run_id="run-1",
                                  event={**item, "run_id": "run-1", "sequence": index})
            await wait_until(
                lambda: len(self.controller.read_events(
                    self.controller.runs["run-1"])) == 2)
        async with FakeRunner(self.port) as restarted:
            await self.registered(restarted)
            await wait_until(lambda: self.controller.runs["run-1"].status == "lost")
            await wait_until(lambda: len(self.samples()) == 1)
        self.assertEqual([10.0], self.samples())

    async def test_a_cancelled_or_failed_learn_leaves_the_run_for_later(self):
        run = self.controller.runs.get("run-1")
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
            for index, item in enumerate(
                passed_records("Argument validation", VALIDATION, [10.0]), 1
            ):
                await runner.send("event", run_id="run-1",
                                  event={**item, "run_id": "run-1", "sequence": index})
            await wait_until(
                lambda: len(self.controller.read_events(
                    self.controller.runs["run-1"])) == 2)
        run = self.controller.runs["run-1"]
        run.status, run.finished_at = "passed", "now"
        # Cancelled while the records are read: nothing learned, nothing
        # written, and the run is still waiting to be learned from.
        gate = threading.Event()
        self.addCleanup(gate.set)
        read_events = self.controller.read_events

        def blocked(target):
            gate.wait(TIMEOUT_S)
            return read_events(target)

        with patch.object(self.controller, "read_events", blocked):
            task = asyncio.create_task(self.controller.learn(run))
            await asyncio.sleep(0.05)
            task.cancel()
            gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(run.learned_durations)
        self.assertEqual([], self.samples())
        self.assertFalse((self.results / STORE_FILE).exists())
        # A store that cannot be written keeps neither the samples nor the
        # claim, so the run can be learned from later instead.
        with patch.object(EstimateStore, "save", return_value=False):
            self.assertEqual(0, await self.controller.learn(run))
        self.assertFalse(run.learned_durations)
        self.assertEqual([], self.samples())
        # A record that cannot be written is reported, not raised.
        with patch.object(self.controller, "_persist",
                          side_effect=OSError("read-only file system")):
            self.assertEqual(1, await self.controller.learn(run))
        self.assertEqual([10.0], self.samples())
        self.assertTrue(any("read-only file system" in line for line in self.logs))

    async def test_a_run_that_ended_in_an_error_teaches_too(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            for index, item in enumerate(
                passed_records("Argument validation", VALIDATION, [10.0]), 1
            ):
                await runner.send("event", run_id="run-1",
                                  event={**item, "run_id": "run-1", "sequence": index})
            await runner.send("run_error", request_id=message["request_id"],
                              run_id="run-1", exit_code=1,
                              reason="the run ended without a summary")
            await wait_until(lambda: self.controller.runs["run-1"].status == "error")
            await wait_until(lambda: len(self.samples()) == 1)
        self.assertEqual([10.0], self.samples())

    async def test_the_plans_endpoint_reports_the_targets_learned_estimates(self):
        step = load_plan(
            REPOSITORY / "testplans" / "fast.yaml", builtin_catalog()
        ).suites["fast"][0]
        key = key_of(step.test_name, step.arguments)
        for value in (4.0, 5.0, 6.0):
            self.controller.estimates.add(key, value)
        for seconds in (300.0, 310.0, 320.0):
            self.controller.estimates.add(Key("build", "", HOST, "host tools", None),
                                          seconds)
        status, payload, _ = await self.http("GET", f"/api/plans?target={HOST}")
        self.assertEqual(200, status)
        self.assertEqual(HOST, payload["target"])
        fast = {plan["name"]: plan for plan in payload["plans"]}["fast"]
        test = [s for s in fast["suites"] if s["name"] == "fast"][0]["tests"][0]
        self.assertEqual(
            {"estimated_s": 5.0, "estimate_source": "learned", "estimate_samples": 3},
            {name: test[name]
             for name in ("estimated_s", "estimate_source", "estimate_samples")},
        )
        self.assertEqual(
            [{"stage": "build", "name": "", "host": HOST, "profile": "host tools",
              "units": None, "estimated_s": 310.0, "estimate_source": "learned",
              "estimate_samples": 3, "estimate_scaled": False,
              "per_unit_s": None}],
            payload["stages"],
        )
        # Without a target the catalog estimate stands, as before.
        _, plain, _ = await self.http("GET", "/api/plans")
        plain_fast = {plan["name"]: plan for plan in plain["plans"]}["fast"]
        first = [s for s in plain_fast["suites"] if s["name"] == "fast"][0]["tests"][0]
        self.assertEqual(test["catalog_estimated_s"], first["estimated_s"])
        self.assertEqual("catalog", first["estimate_source"])
        self.assertEqual([], plain["stages"])
        self.assertIsNone(plain["target"])

    async def test_an_invalid_target_is_refused(self):
        for target in ("../etc", "Host A", "x" * 100, ""):
            with self.subTest(target):
                status, payload, _ = await self.http(
                    "GET", f"/api/plans?target={target.replace(' ', '%20')}")
                self.assertEqual(422, status, payload)

    async def test_the_run_detail_reports_learned_invocations_and_stages(self):
        key = key_of("Raw receive", RAW_RECEIVE)
        for value in (70.0, 71.0, 72.0):
            self.controller.estimates.add(key, value)
        for seconds in (1.0, 1.2, 1.4):
            self.controller.estimates.add(
                Key("preflight", "", HOST, "", ("checks", 2.0)), seconds)
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            del message
            await runner.send("event", run_id="run-1", event={
                **record("manifest", "passed", 0.0,
                         requested_preflight_checks=2,
                         manifest={"invocations": [
                             {"id": "001-Raw receive", "test_name": "Raw receive",
                              "host": HOST, "arguments": RAW_RECEIVE}]}),
                "run_id": "run-1", "sequence": 1})
            await wait_until(
                lambda: self.controller.read_events(self.controller.runs["run-1"]))
        status, payload, _ = await self.http("GET", "/api/runs/run-1")
        self.assertEqual(200, status)
        self.assertEqual(
            [{"invocation_id": "001-Raw receive", "test_name": "Raw receive",
              "estimated_s": 71.0, "catalog_estimated_s": 69.0,
              "estimate_source": "learned", "estimate_samples": 3,
              "estimate_scaled": False}],
            payload["invocations"],
        )
        self.assertEqual(
            [{"stage": "preflight", "profile": "",
              "units": {"argument": "checks", "value": 2.0}, "estimated_s": 1.2,
              "estimate_source": "learned", "estimate_samples": 3,
              "estimate_scaled": False}],
            payload["stages"],
        )


if __name__ == "__main__":
    unittest.main()
