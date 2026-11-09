"""The one human-readable duration format, and the surfaces that carry it (#395).

A measured duration stays a number for machines; the text beside it comes
from :mod:`ubertooth_harness.durations` and from nowhere else, so the
terminal summary, report.md, the GUI, the completion email, and a release's
evidence cannot drift into four different ways of saying an hour. The same
module writes the whole-second form a figure still being counted is read in,
which is a lease's age and how long it has been held (#398).
"""

import dataclasses
import datetime
import math
import unittest
from pathlib import Path

from ubertooth_harness import leases
from ubertooth_harness.controller import Run
from ubertooth_harness.durations import (
    coarse_duration,
    human_duration,
    human_duration_or_none,
)
from ubertooth_harness.notify import duration_text
from ubertooth_harness.rungroups import RunGroup, group_view, host_steps, wall_seconds
from ubertooth_harness.sequencer import SequencerSummary


class FormatTests(unittest.TestCase):
    def test_every_scale_a_run_is_measured_on(self):
        for seconds, text in (
            # Sub-second, which is most commands and some whole tests.
            (0.0, "0.000s"),
            (0.004, "0.004s"),
            (0.25, "0.250s"),
            (0.9999, "1.000s"),
            # Seconds, where the unit is the only one shown.
            (1.0, "1.000s"),
            (34.536, "34.536s"),
            (59.9994, "59.999s"),
            # Minutes, and the second rolling over into one.
            (59.9996, "1m 00.000s"),
            (60.0, "1m 00.000s"),
            (65.5, "1m 05.500s"),
            (634.536, "10m 34.536s"),
            # Hours, including the issue's own example.
            (3600.0, "1h 00m 00.000s"),
            (4234.535980591027, "1h 10m 34.536s"),
            (4234.536, "1h 10m 34.536s"),
            (86399.999, "23h 59m 59.999s"),
            # Days, which is what a soak is read in.
            (86400.0, "1d 00h 00m 00.000s"),
            (90061.5, "1d 01h 01m 01.500s"),
            (259200.0 + 4234.536, "3d 01h 10m 34.536s"),
            (99 * 86400.0, "99d 00h 00m 00.000s"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(human_duration(seconds), text)

    def test_the_parts_add_back_up_to_the_seconds_they_came_from(self):
        """No rounding of a part can lose or invent a millisecond."""
        for seconds in (0.0006, 1.9999, 59.9999, 3599.9999, 86399.9999,
                        4234.535980591027, 123456.789):
            with self.subTest(seconds=seconds):
                text = human_duration(seconds)
                self.assertEqual(round(_parse(text), 3), round(seconds, 3))

    def test_a_duration_with_hours_is_as_wide_as_another_with_hours(self):
        """Padding after the leading unit keeps a table column aligned."""
        self.assertEqual(
            len(human_duration(3600 + 5.5)), len(human_duration(3600 + 3000.5))
        )
        self.assertEqual(human_duration(3605.5), "1h 00m 05.500s")

    def test_an_integer_is_a_duration_too(self):
        self.assertEqual(human_duration(139), "2m 19.000s")

    def test_a_clock_that_went_backwards_keeps_its_sign(self):
        """A negative duration is bad data, and saying 0.000s would hide it."""
        self.assertEqual(human_duration(-0.5), "-0.500s")
        self.assertEqual(human_duration(-4234.536), "-1h 10m 34.536s")

    def test_what_is_not_a_duration_is_refused(self):
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    human_duration(value)

    def test_a_duration_never_measured_passes_through(self):
        self.assertIsNone(human_duration_or_none(None))
        self.assertEqual(human_duration_or_none(0.25), "0.250s")


class CoarseFormatTests(unittest.TestCase):
    """The whole-second form, for a figure read off a running clock (#398)."""

    def test_every_scale_a_lease_or_a_wait_reaches(self):
        for seconds, text in (
            (0.0, "0s"),
            (12.0, "12s"),
            (45.4, "45s"),
            (59.9, "59s"),
            (60.0, "1m 00s"),
            (252.0, "4m 12s"),
            (3600.0, "1h 00m 00s"),
            (3723.0, "1h 02m 03s"),
            (4234.536, "1h 10m 34s"),
            # A lease nobody released is read in days, as a soak is.
            (90123.0, "1d 01h 02m 03s"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(coarse_duration(seconds), text)

    def test_a_part_second_is_dropped_rather_than_rounded_up(self):
        """A figure counting up must not claim a second that has not passed."""
        for seconds in (0.9, 11.999, 59.999, 3599.999):
            with self.subTest(seconds=seconds):
                self.assertEqual(
                    _parse(coarse_duration(seconds)), float(int(seconds))
                )

    def test_both_forms_say_the_same_words_about_the_same_length_of_time(self):
        """The one thing #398 is about: the units and the padding agree.

        At a whole second the two texts differ in the milliseconds and in
        nothing else, so neither can start spelling an hour its own way.
        """
        for seconds in (0.0, 12.0, 60.0, 252.0, 3723.0, 90123.0):
            with self.subTest(seconds=seconds):
                self.assertEqual(
                    human_duration(seconds).replace(".000s", "s"),
                    coarse_duration(seconds),
                )

    def test_a_duration_with_hours_is_as_wide_as_another_with_hours(self):
        self.assertEqual(
            len(coarse_duration(3605)), len(coarse_duration(6600))
        )
        self.assertEqual(coarse_duration(3605), "1h 00m 05s")

    def test_a_clock_that_went_backwards_keeps_its_sign(self):
        self.assertEqual(coarse_duration(-5), "-5s")

    def test_what_is_not_a_duration_is_refused(self):
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    coarse_duration(value)


class SurfaceTests(unittest.TestCase):
    """Each place a finished duration is summarized says the same words."""

    def test_the_run_summary_carries_both_the_number_and_the_words(self):
        summary = SequencerSummary(
            run_id="20260101T120000Z-0123456789ab",
            status="passed",
            passed=True,
            requested_preflight_checks=1,
            completed_preflight_checks=1,
            requested_tests=2,
            completed_tests=2,
            failure_reason="",
            duration_s=4234.535980591027,
        )
        record = dataclasses.asdict(summary)
        # The number automation reads is the measurement, unrounded.
        self.assertEqual(record["duration_s"], 4234.535980591027)
        self.assertEqual(record["duration_hms"], "1h 10m 34.536s")

    def test_the_words_cannot_be_passed_in_beside_a_different_number(self):
        """duration_hms is derived, so no caller can make the two disagree."""
        with self.assertRaises(TypeError):
            SequencerSummary(
                run_id="20260101T120000Z-0123456789ab",
                status="passed",
                passed=True,
                requested_preflight_checks=0,
                completed_preflight_checks=0,
                requested_tests=0,
                completed_tests=0,
                failure_reason="",
                duration_s=1.0,
                duration_hms="all day",
            )

    def test_the_completion_email_says_it_the_same_way(self):
        seconds = wall_seconds("2026-01-01T12:00:00Z", "2026-01-01T13:12:05Z")
        self.assertEqual(
            duration_text("2026-01-01T12:00:00Z", "2026-01-01T13:12:05Z"),
            human_duration(seconds),
        )
        self.assertEqual(
            duration_text("2026-01-01T12:00:00Z", "2026-01-01T13:12:05Z"),
            "1h 12m 05.000s",
        )

    def test_the_email_still_says_when_it_was_not_recorded(self):
        self.assertEqual(duration_text(None, "2026-01-01T12:00:00Z"), "not recorded")

    def test_a_step_says_its_own_span_in_the_same_words(self):
        """Every step cell the GUI draws carries both figures."""
        (preflight,) = host_steps([
            _record(1, "preflight_started", "running", "09:12:33.004"),
            _record(2, "preflight_finished", "passed", "09:13:07.540"),
        ], run_status="passed")
        self.assertEqual(preflight["elapsed_s"], 34.536)
        self.assertEqual(preflight["elapsed_hms"], "34.536s")
        self.assertEqual(
            preflight["elapsed_hms"], human_duration(preflight["elapsed_s"])
        )

    def test_a_finished_group_says_how_long_it_took(self):
        view = group_view(_group(), {"run-1": {
            "run_id": "run-1", "status": "passed",
            "finished_at": "2026-11-05T10:23:07.540",
        }})
        self.assertEqual(view["duration_s"], 4234.536)
        self.assertEqual(view["duration_hms"], "1h 10m 34.536s")

    def test_a_group_that_started_no_run_has_no_duration(self):
        """Not 0.000s, which would read as a group that ran instantly."""
        group = _group()
        group.members = [{"host": "Host_Linux", "run_id": None,
                          "error": "that target is busy"}]
        view = group_view(group, {})
        self.assertIsNone(view["duration_s"])
        self.assertIsNone(view["duration_hms"])

    def test_a_finished_run_says_how_long_it_took_and_a_running_one_does_not(self):
        finished = Run(
            run_id="20260101T120000Z-0123456789ab", target="Host_Linux",
            directory=Path("."), status="passed",
            created_at="2026-01-01T12:00:00.000",
            finished_at="2026-01-01T13:12:05.000",
        ).view()
        self.assertEqual(finished["duration_s"], 4325.0)
        self.assertEqual(finished["duration_hms"], "1h 12m 05.000s")
        running = Run(
            run_id="20260101T140000Z-0123456789ab", target="Host_Linux",
            directory=Path("."), created_at="2026-01-01T14:00:00.000",
        ).view()
        self.assertIsNone(running["duration_s"])
        self.assertIsNone(running["duration_hms"])

    def test_a_leases_age_and_held_for_come_from_the_same_formatter(self):
        """`boards` had a compact format of its own until #398."""
        self.assertEqual(leases.describe_seconds(3723), coarse_duration(3723))
        held = leases.held_for(
            {"acquired_at": "2026-01-01T01:00:00"},
            datetime.datetime(2026, 1, 1, 1, 4, 11, 500000),
        )
        self.assertEqual(held, coarse_duration(251.5))
        self.assertEqual(held, "4m 11s")

    def test_an_age_the_lock_file_does_not_record_is_left_unsaid(self):
        """A lock file is another command's: nothing read out of one raises."""
        now = datetime.datetime(2026, 1, 1, 1, 0, 0)
        self.assertEqual(leases.held_for({}, now), "")
        self.assertEqual(leases.held_for({"acquired_at": "whenever"}, now), "")
        # A clock that went backwards between the two stamps.
        self.assertEqual(
            leases.held_for({"acquired_at": "2026-01-01T02:00:00"}, now), "0s"
        )


def _record(sequence: int, kind: str, status: str, clock: str) -> dict:
    """One run.jsonl record at a time of day, as a run writes them."""
    return {
        "schema_version": 1, "run_id": "run-1", "sequence": sequence,
        "timestamp": f"2026-11-05T{clock}", "event_type": kind,
        "status": status, "details": {},
    }


def _group() -> RunGroup:
    """One group of one host, asked for at a known instant."""
    return RunGroup(
        group_id="grp-" + "a" * 32,
        hosts=["Host_Linux"],
        plan="release",
        suite="release",
        created_at="2026-11-05T09:12:33.004",
        dispatched_at="2026-11-05T09:12:33.004",
        members=[{"host": "Host_Linux", "run_id": "run-1", "error": None}],
    )


def _parse(text: str) -> float:
    """The seconds a formatted duration names, read back out of the text."""
    sign = -1 if text.startswith("-") else 1
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0.0
    for part in text.lstrip("-").split(" "):
        total += float(part[:-1]) * units[part[-1]]
    return sign * total


if __name__ == "__main__":
    unittest.main()
