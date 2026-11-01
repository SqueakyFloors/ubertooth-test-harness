"""afhmap.py: reading AFH maps, the per-minute series, and the tolerances."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ubertooth_harness import afhmap, esp32peer

HALF_BAND = afhmap.hex_from_channels(range(0, 40))
# What these controllers leave of it: they drop channels 0, 24, and 78 of
# their own accord, so a 40-channel request leads a 38-channel link.
IN_FORCE = afhmap.hex_from_channels(set(range(0, 40)) - {0, 24})
IN_FORCE_CHANNELS = afhmap.channels_from_hex(IN_FORCE)


def _report(requested=HALF_BAND, links=(IN_FORCE, IN_FORCE, IN_FORCE)):
    return {
        "requested": requested,
        "requested_used": len(afhmap.channels_from_hex(requested)),
        "samples": [
            {"at": at, "seconds": seconds, "link": link,
             "used": len(afhmap.channels_from_hex(link)), "mode": "on",
             "error": ""}
            for at, seconds, link in zip(esp32peer.SAMPLE_POINTS,
                                         (0.0, 60.0, 120.0), links)
        ],
        "command": ["ubertooth-afh", "-r"],
        "seconds": 120.0,
    }


def _bits(channels):
    return "".join("1" if channel in set(channels) else "0"
                   for channel in range(afhmap.MAP_BITS))


def _r_lines(maps, start=1000):
    return "".join(f"{start + index} {_bits(channels)}\n"
                   for index, channels in enumerate(maps))


class _Files:
    """A temporary peer report and map file, as a session leaves them."""

    def __init__(self, stack, report, maps):
        directory = Path(stack)
        self.report = directory / "afh-peer-report.json"
        self.report.write_text(json.dumps(report), encoding="utf-8")
        self.maps = directory / "afh-maps.txt"
        self.maps.write_text(maps, encoding="utf-8")


class MapFormatTests(unittest.TestCase):

    def test_bit_b_of_byte_i_is_channel_8i_plus_b(self):
        # The order the spec's classification command and libbtbb both use,
        # which is why a recovered map compares with a requested one as it
        # stands.
        self.assertEqual({0}, afhmap.channels_from_hex("01" + "00" * 9))
        self.assertEqual({7}, afhmap.channels_from_hex("80" + "00" * 9))
        self.assertEqual({8}, afhmap.channels_from_hex("0001" + "00" * 8))
        self.assertEqual({78}, afhmap.channels_from_hex("00" * 9 + "40"))

    def test_a_map_round_trips_through_its_hex(self):
        for channels in (range(0, 40), range(30, 50), range(79), (0, 24, 78)):
            with self.subTest(list(channels)[:3]):
                self.assertEqual(
                    frozenset(channels),
                    afhmap.channels_from_hex(afhmap.hex_from_channels(channels)))

    def test_the_reserved_eightieth_bit_is_refused(self):
        with self.assertRaisesRegex(afhmap.AfhMapError, "reserved 80th bit"):
            afhmap.channels_from_hex("ff" * 9 + "80")

    def test_a_map_that_is_not_twenty_hex_digits_is_refused(self):
        for value in ("", "ff", "ff" * 11, "zz" + "00" * 9):
            with self.subTest(value), self.assertRaisesRegex(
                afhmap.AfhMapError, "must be 20 hex digits"
            ):
                afhmap.channels_from_hex(value)

    def test_a_channel_outside_the_band_cannot_be_written(self):
        with self.assertRaisesRegex(afhmap.AfhMapError, "channel 79 is not one"):
            afhmap.hex_from_channels([79])

    def test_the_r_form_numbers_its_characters_by_channel(self):
        self.assertEqual(frozenset({0, 5, 78}),
                         afhmap.channels_from_bits(_bits([0, 5, 78])))
        with self.assertRaisesRegex(afhmap.AfhMapError, "80 characters"):
            afhmap.channels_from_bits("01" * 20)

    def test_channels_are_named_as_ranges_for_a_person(self):
        self.assertEqual("0-39", afhmap.runs(range(40)))
        self.assertEqual("0-38, 40, 42-43", afhmap.runs(list(range(39)) + [40, 42, 43]))
        self.assertEqual("none", afhmap.runs([]))


class PeerReportTests(unittest.TestCase):

    def test_the_in_force_map_is_read_from_the_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            files = _Files(directory, _report(), _r_lines([IN_FORCE_CHANNELS]))
            report = afhmap.load_peer_report(files.report)
        self.assertEqual(IN_FORCE, report.link)
        self.assertEqual(HALF_BAND, report.requested)
        self.assertEqual(3, len(report.samples))

    def test_a_link_map_that_changed_under_the_capture_is_refused(self):
        other = afhmap.hex_from_channels(range(0, 30))
        with tempfile.TemporaryDirectory() as directory:
            files = _Files(directory, _report(links=(IN_FORCE, other, IN_FORCE)),
                           _r_lines([IN_FORCE_CHANNELS]))
            with self.assertRaisesRegex(afhmap.AfhMapError,
                                        "changed under the capture"):
                afhmap.load_peer_report(files.report)

    def test_a_link_beyond_the_requested_map_is_refused(self):
        # The controller may drop channels the host asked to keep; a link
        # hopping over one it did not ask for is another piconet.
        wider = afhmap.hex_from_channels(range(0, 45))
        with tempfile.TemporaryDirectory() as directory:
            files = _Files(directory, _report(links=(wider,) * 3),
                           _r_lines([IN_FORCE_CHANNELS]))
            with self.assertRaisesRegex(afhmap.AfhMapError,
                                        r"channel\(s\) 40-44, which the requested"):
                afhmap.load_peer_report(files.report)

    def test_a_report_with_no_sample_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            files = _Files(directory, {"requested": HALF_BAND, "samples": []}, "")
            with self.assertRaisesRegex(afhmap.AfhMapError, "no link map sample"):
                afhmap.load_peer_report(files.report)

    def test_a_sample_field_that_is_not_a_number_is_refused(self):
        report = _report()
        report["samples"][1]["used"] = "thirty-eight"
        with tempfile.TemporaryDirectory() as directory:
            files = _Files(directory, report, _r_lines([IN_FORCE_CHANNELS]))
            with self.assertRaisesRegex(afhmap.AfhMapError,
                                        "middle sample of .* has a bad number"):
                afhmap.load_peer_report(files.report)

    def test_a_report_that_is_not_json_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(afhmap.AfhMapError, "is not JSON"):
                afhmap.load_peer_report(path)


class SeriesTests(unittest.TestCase):

    def test_the_last_one_a_second_map_is_the_recovered_map(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maps.txt"
            path.write_text(_r_lines([{1, 2}, {1, 2, 3}, {1, 2, 3, 4}]),
                            encoding="utf-8")
            series = afhmap.read_series(path)
        self.assertEqual(frozenset({1, 2, 3, 4}), series.recovered)
        self.assertEqual(3, len(series.channels))
        self.assertIn("one map a second", series.form)

    def test_the_last_printed_map_line_is_the_recovered_map(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maps.txt"
            path.write_text(f"AFH map: 0x{afhmap.hex_from_channels([1])}\n"
                            f"AFH map: 0x{IN_FORCE}\n", encoding="utf-8")
            series = afhmap.read_series(path)
        self.assertEqual(IN_FORCE_CHANNELS, series.recovered)
        self.assertEqual((), series.times)

    def test_a_file_with_both_forms_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maps.txt"
            path.write_text(_r_lines([{1}]) + f"AFH map: 0x{IN_FORCE}\n",
                            encoding="utf-8")
            with self.assertRaisesRegex(afhmap.AfhMapError, "one capture prints one"):
                afhmap.read_series(path)

    def test_a_file_with_no_map_says_the_tool_recovered_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maps.txt"
            path.write_text("Ubertooth stopped\n", encoding="utf-8")
            with self.assertRaisesRegex(afhmap.AfhMapError, "recovered nothing"):
                afhmap.read_series(path)

    def test_the_series_is_bucketed_by_the_minute_it_arrived_in(self):
        maps = [{1}] * 60 + [{1, 2}] * 60 + [{1, 2, 3}] * 10
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maps.txt"
            path.write_text(_r_lines(maps), encoding="utf-8")
            series = afhmap.read_series(path)
        self.assertEqual([(1, 60, frozenset({1})), (2, 60, frozenset({1, 2})),
                          (3, 10, frozenset({1, 2, 3}))],
                         afhmap.minute_buckets(series))

    def test_printed_map_lines_have_no_per_minute_series(self):
        self.assertEqual([], afhmap.minute_buckets(
            afhmap.Series((IN_FORCE_CHANNELS,), (), "AFH map: lines")))


class ComparisonTests(unittest.TestCase):

    def test_every_in_force_channel_found_and_no_stray_is_a_clean_recovery(self):
        comparison = afhmap.Comparison(IN_FORCE_CHANNELS, IN_FORCE_CHANNELS)
        self.assertEqual(1.0, comparison.found_share)
        self.assertEqual(0.0, comparison.stray_share)
        self.assertEqual(frozenset(), comparison.missing)

    def test_the_skirt_beside_the_block_counts_as_stray(self):
        recovered = IN_FORCE_CHANNELS | frozenset(range(40, 48))
        comparison = afhmap.Comparison(recovered, IN_FORCE_CHANNELS)
        self.assertEqual(1.0, comparison.found_share)
        self.assertEqual(8, len(comparison.stray))
        self.assertEqual(41, len(comparison.dropped))

    def test_a_missing_channel_is_named_in_the_failure(self):
        recovered = IN_FORCE_CHANNELS - {20}
        with self.assertRaisesRegex(afhmap.AfhMapError,
                                    r"36 of 38 in-force channels found.*missing 20"):
            afhmap.judge(afhmap.Comparison(recovered - {21}, IN_FORCE_CHANNELS),
                         min_found_share=1.0, max_stray_share=0.7,
                         min_dropped_channels=10)

    def test_too_many_stray_channels_fail_with_the_share(self):
        recovered = IN_FORCE_CHANNELS | frozenset(range(40, 79))
        with self.assertRaisesRegex(afhmap.AfhMapError,
                                    "39 of 41 channels out of force are marked"):
            afhmap.judge(afhmap.Comparison(recovered, IN_FORCE_CHANNELS),
                         min_found_share=1.0, max_stray_share=0.7,
                         min_dropped_channels=10)

    def test_a_tolerance_below_one_allows_a_channel_to_be_missed(self):
        recovered = IN_FORCE_CHANNELS - {20}
        self.assertEqual(["afh recovery: ok"],
                         afhmap.judge(afhmap.Comparison(recovered,
                                                        IN_FORCE_CHANNELS),
                                      min_found_share=0.95, max_stray_share=0.7,
                                      min_dropped_channels=10))

    def test_the_stray_share_is_left_unjudged_for_a_nearly_full_band(self):
        # A map that keeps 76 of 79 channels leaves three out of force,
        # which is too few for a share to mean anything.
        in_force = frozenset(range(afhmap.CHANNELS)) - {0, 24, 78}
        verdict = afhmap.judge(afhmap.Comparison(frozenset(range(afhmap.CHANNELS)),
                                                 in_force),
                               min_found_share=1.0, max_stray_share=0.7,
                               min_dropped_channels=10)
        self.assertIn("stray share: not judged, only 3 channels", verdict[0])
        self.assertEqual("afh recovery: ok", verdict[-1])


class CheckCommandTests(unittest.TestCase):
    """The step runs this as a command: what it prints and its exit status."""

    def _run(self, report, maps, *options):
        with tempfile.TemporaryDirectory() as directory:
            files = _Files(directory, report, maps)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = afhmap.main(["--peer-report", str(files.report),
                                    "--maps", str(files.maps), *options])
        return code, out.getvalue(), err.getvalue()

    def test_a_clean_recovery_prints_the_measurement_and_exits_0(self):
        maps = _r_lines([frozenset(range(10)), IN_FORCE_CHANNELS])
        code, out, err = self._run(_report(), maps)
        self.assertEqual(0, code, err)
        self.assertIn(f"requested map: {HALF_BAND} used 40 (0-39)", out)
        self.assertIn(f"in-force map: {IN_FORCE} used 38", out)
        self.assertIn("in-force samples: 3, all the same map", out)
        self.assertIn("in-force channels found: 38 of 38 (100%)", out)
        self.assertIn("in-force channels missing: none", out)
        self.assertIn("stray channels marked used: 0 of 41 out of force (0%)", out)
        self.assertIn("afh recovery: ok", out)
        self.assertEqual("", err)

    def test_the_per_minute_series_is_printed(self):
        maps = _r_lines([frozenset(range(1, 20))] * 60 + [IN_FORCE_CHANNELS] * 60)
        code, out, _err = self._run(_report(), maps)
        self.assertEqual(0, code)
        self.assertIn("minute 1: 60 map lines, used 19, in force 19 of 38, stray 0",
                      out)
        self.assertIn("minute 2: 60 map lines, used 38, in force 38 of 38, stray 0",
                      out)

    def test_a_missed_tolerance_still_prints_what_was_measured(self):
        # The measurement of a run that failed is the point of the run, so it
        # is on standard output and the reason alone is on standard error.
        maps = _r_lines([IN_FORCE_CHANNELS | frozenset(range(40, 79))])
        code, out, err = self._run(_report(), maps)
        self.assertEqual(1, code)
        self.assertIn("recovered map: ", out)
        self.assertIn("stray channels: 40-78", out)
        self.assertNotIn("afh recovery: ok", out)
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn("above the 70% this test allows", err)

    def test_the_tolerances_come_from_the_command_line(self):
        maps = _r_lines([IN_FORCE_CHANNELS - {20}])
        self.assertEqual(1, self._run(_report(), maps)[0])
        self.assertEqual(0, self._run(_report(), maps,
                                      "--min-found-share", "0.9")[0])

    def test_a_share_outside_zero_to_one_is_a_usage_error(self):
        maps = _r_lines([IN_FORCE_CHANNELS])
        with self.assertRaises(SystemExit) as raised:
            self._run(_report(), maps, "--max-stray-share", "2")
        self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
