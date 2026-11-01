"""Recover AFH map: the cases, their commands, and the plans that name it."""

from pathlib import Path
import unittest

from ubertooth_harness import afhmap, esp32peer
from ubertooth_harness.afh_cases import (
    AC_ERRORS,
    DETECTION_MAX_STRAY_SHARE,
    HALF_BAND_MAP,
    MAX_STRAY_SHARE,
    MINIMUM_MAP,
    MIN_DROPPED_CHANNELS,
    MIN_FOUND_SHARE,
    PAIR_SETUP_S,
    afh_map_cases,
)
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.plan import load_plan
from ubertooth_harness.stateful import (
    ATTEMPT_KEY,
    CAPTURE_DIR_KEY,
    StateCaseError,
)

from tests.test_classic_cases import (
    ACCEPTOR_ADDRESS,
    IDENTITIES,
    INITIATOR_BRIDGE,
    RESOURCES,
    SERIAL,
)

ROOT = Path(__file__).resolve().parents[1]
TEST = "Recover AFH map"
CENTRAL_LAP, CENTRAL_UAP = "e9c5ba", "0d"
CAPTURES = "/run/captures/afh"
CONTEXT = {CAPTURE_DIR_KEY: CAPTURES}


def arguments(coverage="smoke", duration="2m"):
    return builtin_catalog().get(TEST).normalize_arguments(
        {"coverage": coverage, "duration": duration})


def cases(coverage="smoke", duration="2m"):
    return afh_map_cases(arguments(coverage, duration), RESOURCES, IDENTITIES)


def steps(case):
    return {step.step_id: step for step in case.steps}


class MapTests(unittest.TestCase):
    """The two maps the cases put on the air."""

    def test_the_half_band_map_keeps_forty_channels(self):
        self.assertEqual(frozenset(range(0, 40)),
                         afhmap.channels_from_hex(HALF_BAND_MAP))

    def test_the_minimum_map_keeps_the_twenty_the_spec_allows(self):
        # Twenty is the floor the firmware enforces as well, so this map is
        # the edge case: one channel fewer is refused.
        self.assertEqual(20, len(afhmap.channels_from_hex(MINIMUM_MAP)))
        self.assertEqual(esp32peer.AFH_MIN_CHANNELS,
                         len(afhmap.channels_from_hex(MINIMUM_MAP)))

    def test_both_maps_are_ones_the_peer_firmware_accepts(self):
        for wanted in (HALF_BAND_MAP, MINIMUM_MAP):
            with self.subTest(wanted):
                self.assertEqual(wanted, esp32peer.channel_map(wanted))


class CaseTests(unittest.TestCase):

    def test_smoke_runs_the_two_maps(self):
        self.assertEqual(["half-band", "minimum-channels"],
                         [case.case_id for case in cases()])
        self.assertEqual(("linux",), cases()[0].needs.platforms)

    def test_comprehensive_adds_the_detection_phase(self):
        self.assertEqual(["half-band", "minimum-channels", "half-band-detection"],
                         [case.case_id for case in cases("comprehensive")])

    def test_the_capture_runs_ubertooth_afh_with_no_access_code_errors(self):
        # -e 0 is the measurement this test rests on: with the tool's default
        # of two allowed bit errors the bench marked the whole band used.
        argv = list(steps(cases()[0])["stream-and-recover"].resolve_argv(
            CONTEXT))
        self.assertEqual(["--", "ubertooth-afh", "-U", SERIAL, "-l", CENTRAL_LAP,
                          "-u", CENTRAL_UAP, "-e", str(AC_ERRORS), "-r"],
                         argv[argv.index("--"):])
        self.assertEqual(0, AC_ERRORS)

    def test_the_map_and_both_files_reach_the_driver(self):
        argv = list(steps(cases()[0])["stream-and-recover"].resolve_argv(
            CONTEXT))
        self.assertEqual(HALF_BAND_MAP, argv[argv.index("--map") + 1])
        self.assertEqual(f"{CAPTURES}/afh-peer-report-1.json",
                         argv[argv.index("--map-report") + 1])
        self.assertEqual(f"{CAPTURES}/afh-maps-1.txt",
                         argv[argv.index("--command-output") + 1])
        self.assertEqual(esp32peer.default_device(INITIATOR_BRIDGE),
                         argv[argv.index("--initiator") + 1])
        self.assertEqual(ACCEPTOR_ADDRESS,
                         argv[argv.index("--acceptor-address") + 1])
        self.assertTrue(cases()[0].capture_dir)

    def test_the_capture_is_stopped_on_time_and_may_not_stop_early(self):
        # ubertooth-afh watches for map changes once it has a map, so the
        # interrupt is the planned end of the capture.
        step = steps(cases()[0])["stream-and-recover"]
        argv = list(step.resolve_argv(CONTEXT))
        self.assertEqual("120.0", argv[argv.index("--interrupt-after") + 1])
        self.assertEqual("119.5", argv[argv.index("--min-seconds") + 1])
        self.assertGreater(step.timeout_s, 120.0 + PAIR_SETUP_S - 1)
        self.assertTrue(step.mutates)

    def test_the_detection_case_is_interrupted_after_its_own_window(self):
        # -t ends the detection phase and the map is printed then, so the
        # interrupt has to wait past it.
        detection = cases("comprehensive")[-1]
        argv = list(steps(detection)["stream-and-recover"].resolve_argv(
            CONTEXT))
        self.assertEqual(["-t", "120"], argv[-2:])
        self.assertGreater(float(argv[argv.index("--interrupt-after") + 1]), 120.0)

    def test_the_step_checks_what_the_driver_reports_about_both_maps(self):
        patterns = steps(cases()[0])["stream-and-recover"].output_patterns
        self.assertIn(rf"(?m)^channel map requested: {HALF_BAND_MAP} used 40$",
                      patterns)
        self.assertIn(r"(?m)^channel map in force: stable$", patterns)
        for at in esp32peer.SAMPLE_POINTS:
            self.assertTrue(
                any(f"in force at the {at} " in pattern for pattern in patterns),
                at)
        # And that the pair really streamed, as the Classic cases do.
        self.assertIn(rf"(?m)^central LAP: {CENTRAL_LAP}$", patterns)
        self.assertIn(r"(?m)^pair streamed: yes$", patterns)

    def test_the_r_case_wants_map_lines_and_the_detection_case_a_map(self):
        self.assertIn(r"(?m)^\d+ [01]{80}$",
                      steps(cases()[0])["stream-and-recover"].output_patterns)
        self.assertIn(r"(?m)^AFH map: 0x[0-9a-f]{20}$",
                      steps(cases("comprehensive")[-1])
                      ["stream-and-recover"].output_patterns)

    def test_the_comparison_is_a_step_of_its_own_with_the_tolerances(self):
        step = steps(cases()[0])["judge-map"]
        argv = list(step.resolve_argv(CONTEXT))
        self.assertTrue(argv[1].endswith("afhmap.py"))
        self.assertEqual(f"{CAPTURES}/afh-peer-report-1.json",
                         argv[argv.index("--peer-report") + 1])
        self.assertEqual(f"{CAPTURES}/afh-maps-1.txt",
                         argv[argv.index("--maps") + 1])
        self.assertEqual(str(MIN_FOUND_SHARE),
                         argv[argv.index("--min-found-share") + 1])
        self.assertEqual(str(MAX_STRAY_SHARE),
                         argv[argv.index("--max-stray-share") + 1])
        self.assertEqual(str(MIN_DROPPED_CHANNELS),
                         argv[argv.index("--min-dropped-channels") + 1])
        self.assertIn(r"(?m)^afh recovery: ok$", step.output_patterns)
        # The found line is asserted as strictly as the tolerance allows: the
        # two counts have to be the same number only when every in-force
        # channel is demanded.
        strict = r"(?m)^in-force channels found: (\d+) of \1 \(100%\)$"
        loose = r"(?m)^in-force channels found: \d+ of \d+ \(\d+%\)$"
        self.assertIn(strict if MIN_FOUND_SHARE >= 1.0 else loose,
                      step.output_patterns)

    def test_the_capture_and_the_comparison_are_retried_together(self):
        # Whether a channel was heard depends on where the sweep and the hop
        # sequence met, and the peers keep making the traffic, so a second
        # window costs only its own time.
        case = cases()[0]
        self.assertEqual(("stream-and-recover", "judge-map"), case.retry_steps)
        self.assertEqual(2, case.attempts)
        identifiers = [step.step_id for step in case.steps]
        first = identifiers.index("stream-and-recover")
        self.assertEqual(["stream-and-recover", "judge-map"],
                         identifiers[first:first + 2])

    def test_each_attempt_writes_its_own_pair_of_files(self):
        # A retry that overwrote the first attempt's files would leave the run
        # directory with only the last window's measurement.
        case = cases()[0]
        names = []
        for attempt in ("1", "2"):
            context = {**CONTEXT, ATTEMPT_KEY: attempt}
            argv = list(steps(case)["stream-and-recover"].resolve_argv(context))
            judge = list(steps(case)["judge-map"].resolve_argv(context))
            names.append((argv[argv.index("--command-output") + 1],
                          judge[judge.index("--maps") + 1]))
        self.assertEqual((f"{CAPTURES}/afh-maps-1.txt",) * 2, names[0])
        self.assertEqual((f"{CAPTURES}/afh-maps-2.txt",) * 2, names[1])

    def test_the_detection_case_gets_a_looser_stray_ceiling(self):
        # Its map fills single channel gaps and never drops a channel, so it
        # is an upper bound; the -r cases are where selectivity is measured.
        detection = cases("comprehensive")[-1]
        argv = list(steps(detection)["judge-map"].resolve_argv(CONTEXT))
        self.assertEqual(str(DETECTION_MAX_STRAY_SHARE),
                         argv[argv.index("--max-stray-share") + 1])
        self.assertGreater(DETECTION_MAX_STRAY_SHARE, MAX_STRAY_SHARE)
        # Loose, not absent: a -t map marking the whole band used still fails.
        self.assertLess(DETECTION_MAX_STRAY_SHARE, 1.0)
        # The two -r cases keep the tighter one.
        for case in cases():
            with self.subTest(case.case_id):
                argv = list(steps(case)["judge-map"].resolve_argv(CONTEXT))
                self.assertEqual(str(MAX_STRAY_SHARE),
                                 argv[argv.index("--max-stray-share") + 1])

    def test_every_case_resets_both_peers_and_restores_the_board(self):
        for case in cases("comprehensive"):
            with self.subTest(case.case_id):
                cleanup = [step.step_id for step in case.cleanup]
                self.assertEqual(["reset-initiator", "reset-acceptor",
                                  "cleanup-stop"], cleanup[:3])
                self.assertEqual("restored-device-identity", cleanup[-1])

    def test_both_peers_are_required(self):
        for role in ("acceptor", "initiator"):
            with self.subTest(role), self.assertRaisesRegex(
                StateCaseError, f"requires an? {role}"
            ):
                afh_map_cases(arguments(), {}, {**IDENTITIES, role: {}})


class PlanTests(unittest.TestCase):

    def test_the_test_is_no_longer_a_backlog_entry(self):
        backlog = [step.test_name for step
                   in load_plan(ROOT / "testplans" / "backlog.yaml").steps("backlog")]
        self.assertNotIn(TEST, backlog)
        self.assertTrue(builtin_catalog().get(TEST).implemented)

    def test_the_classic_plan_runs_it_beside_the_other_peer_tests(self):
        plan = load_plan(ROOT / "testplans" / "classic.yaml", builtin_catalog())
        for suite, coverage in (("classic", "smoke"),
                                ("classic-comprehensive", "comprehensive")):
            found = [step for step in plan.steps(suite) if step.test_name == TEST]
            with self.subTest(suite):
                self.assertEqual(1, len(found))
                self.assertEqual(coverage, found[0].arguments["coverage"])
                self.assertEqual("2m", found[0].arguments["duration"])

    def test_the_release_plan_does_not_name_it(self):
        # Every release entry runs on each registered host, and this test
        # needs the ESP32 peers, which one host has: naming a host there
        # fails planning on the others rather than skipping.
        plan = load_plan(ROOT / "testplans" / "release.yaml", builtin_catalog())
        self.assertEqual([], [step for step in plan.steps("release")
                              if step.test_name == TEST])


if __name__ == "__main__":
    unittest.main()
