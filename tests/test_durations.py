"""The one human-readable duration format, and the surfaces that carry it (#395).

A measured duration stays a number for machines; the text beside it comes
from :mod:`ubertooth_harness.durations` and from nowhere else, so the
terminal summary, report.md, the GUI, the completion email and a release's
evidence cannot drift into four different ways of saying an hour.
"""

import dataclasses
import math
import unittest

from ubertooth_harness.durations import human_duration, human_duration_or_none
from ubertooth_harness.notify import duration_text
from ubertooth_harness.rungroups import wall_seconds
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
