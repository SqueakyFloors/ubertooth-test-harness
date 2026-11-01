"""Classic (BR/EDR) cases against the ESP32 peer pair: the cases and the registry."""

from pathlib import Path
import tempfile
import unittest

from ubertooth_harness import esp32peer, leases
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.classic_cases import (
    AUDIO_SETUP_S,
    AUDIO_TYPE,
    AUDIO_TYPE_NAME,
    MIN_PACKETS,
    PAIR_SETUP_S,
    follow_classic_cases,
    known_classic_cases,
    record_fields_cases,
    no_piconet_packets,
    piconet_packets,
    survey_lists,
    survey_omits,
)
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.preflight import PreflightPolicy
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from tests.test_sequencer import run_contract
from ubertooth_harness.stateful import CAPTURE_DIR_KEY, StateCaseError

ROOT = Path(__file__).resolve().parents[1]
SERIAL = "0123456789abcdef0123456789abcdef"
ACCEPTOR_BRIDGE = "aaaa0000aaaa0000aaaa0000aaaa0000"
INITIATOR_BRIDGE = "bbbb1111bbbb1111bbbb1111bbbb1111"
ACCEPTOR_ADDRESS = "C8:85:41:2A:1B:2A"
INITIATOR_ADDRESS = "20:50:0D:E9:C5:BA"
CENTRAL_LAP, CENTRAL_UAP = "e9c5ba", "0d"
OTHER_LAP, OTHER_UAP = "2a1b2a", "41"
RESOURCES = {"Ubertooth": "UT_Linux0", "acceptor": "ESP32_Linux0",
             "initiator": "ESP32_Linux1"}
IDENTITIES = {
    "Ubertooth": {"serial": SERIAL},
    "acceptor": {"bridge serial": ACCEPTOR_BRIDGE, "BD_ADDR": ACCEPTOR_ADDRESS},
    "initiator": {"bridge serial": INITIATOR_BRIDGE, "BD_ADDR": INITIATOR_ADDRESS},
}
REGISTRY = f"""
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [test target]
    defaults:
      Ubertooth: UT_Linux0
      acceptor: ESP32_Linux0
      initiator: ESP32_Linux1
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {{serial: {SERIAL}}}
  ESP32_Linux0:
    type: ESP32 Classic peer
    attached to: Host_Linux
    roles: [acceptor, initiator]
    identity:
      bridge serial: "{ACCEPTOR_BRIDGE}"
      BD_ADDR: "{ACCEPTOR_ADDRESS}"
  ESP32_Linux1:
    type: ESP32 Classic peer
    attached to: Host_Linux
    roles: [acceptor, initiator]
    identity:
      bridge serial: "{INITIATOR_BRIDGE}"
      BD_ADDR: "{INITIATOR_ADDRESS}"
"""
# One packet line of each piconet, as ubertooth-rx prints them.
PACKET = ("systime=1793317467 ch=28 LAP={lap} err=0 clkn=29234 clk_offset=2492 "
          "s=0 n=-88 snr=88")
SURVEY = "Survey Results\n??:??:0D:E9:C5:BA\n"


def arguments(test, coverage="smoke", duration="25s"):
    return builtin_catalog().get(test).normalize_arguments(
        {"coverage": coverage, "duration": duration})


def registry_with(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "hardware.yaml"
        path.write_text(text)
        return FileResourceRegistry.load(path)


def packets(lap, count):
    return "\n".join(PACKET.format(lap=lap) for _ in range(count)) + "\n"


class CheckTests(unittest.TestCase):
    """What each case accepts and what it reports as a failure."""

    def test_enough_packets_of_the_piconet_pass(self):
        check = piconet_packets(CENTRAL_LAP, 3, only=True)
        self.assertEqual("", check(packets(CENTRAL_LAP, 3)))

    def test_too_few_packets_are_counted_in_the_failure(self):
        check = piconet_packets(CENTRAL_LAP, 3, only=True)
        self.assertEqual("2 packet lines with LAP e9c5ba, expected at least 3",
                         check(packets(CENTRAL_LAP, 2)))

    def test_a_filtered_run_must_report_no_other_piconet(self):
        check = piconet_packets(CENTRAL_LAP, 1, only=True)
        self.assertEqual("packet lines with LAP 2a1b2a, not e9c5ba",
                         check(packets(CENTRAL_LAP, 2) + packets(OTHER_LAP, 1)))

    def test_an_unfiltered_run_may_report_other_piconets(self):
        check = piconet_packets(CENTRAL_LAP, 1, only=False)
        self.assertEqual("", check(packets(CENTRAL_LAP, 1) + packets(OTHER_LAP, 4)))

    def test_a_lap_that_is_not_the_centrals_must_report_nothing(self):
        check = no_piconet_packets(OTHER_LAP)
        self.assertEqual("", check("note: nothing decoded\n"))
        self.assertIn("1 packet lines (LAP 2a1b2a), expected none",
                      check(packets(OTHER_LAP, 1)))

    def test_the_survey_must_list_the_centrals_lap(self):
        self.assertEqual("", survey_lists(CENTRAL_LAP)(packets(CENTRAL_LAP, 2) + SURVEY))
        self.assertEqual("the survey listed e9c5ba, not LAP 2a1b2a",
                         survey_lists(OTHER_LAP)(SURVEY))
        self.assertEqual("the survey listed nothing, not LAP e9c5ba",
                         survey_lists(CENTRAL_LAP)("systime=1 nonsense\n"))

    def test_a_survey_line_with_no_uap_still_names_its_lap(self):
        self.assertEqual("", survey_lists(CENTRAL_LAP)(
            "Survey Results\n??:??:??:E9:C5:BA\n"))

    def test_the_swapped_case_requires_the_other_lap_and_not_the_centrals(self):
        self.assertEqual("the survey listed LAP e9c5ba, which is not the central's",
                         survey_omits(CENTRAL_LAP)(SURVEY))
        self.assertEqual("", survey_omits(OTHER_LAP)(SURVEY))


class CaptureCaseTests(unittest.TestCase):

    def test_smoke_cases(self):
        # Mandatory preflight already opens each bridge and checks its
        # version and registered address, so no case repeats that.
        cases = known_classic_cases(arguments("Capture known Classic device"), {},
                                    IDENTITIES)
        self.assertEqual(["survey", "sweep"], [case.case_id for case in cases])
        self.assertEqual(("linux",), cases[0].needs.platforms)

    def test_comprehensive_adds_a_capture_file_and_the_swapped_roles(self):
        cases = known_classic_cases(
            arguments("Capture known Classic device", "comprehensive"), {}, IDENTITIES)
        self.assertEqual(["survey", "sweep", "survey-pcapng",
                          "survey-roles-swapped"],
                         [case.case_id for case in cases])
        swapped = cases[-1]
        stream = {step.step_id: step for step in swapped.steps}["stream-and-receive"]
        argv = stream.resolve_argv({})
        # The peers are bound the other way round, so the other board pages.
        self.assertEqual(INITIATOR_ADDRESS, argv[argv.index("--acceptor-address") + 1])
        self.assertEqual(ACCEPTOR_ADDRESS, argv[argv.index("--initiator-address") + 1])
        self.assertIn(rf"(?m)^central LAP: {OTHER_LAP}$",
                      stream.output_patterns)

    def test_the_cleanup_resets_each_peer_through_the_driver(self):
        case = known_classic_cases(arguments("Capture known Classic device"), {},
                                   IDENTITIES)[0]
        reset = {step.step_id: step for step in case.cleanup}["reset-acceptor"]
        argv = reset.resolve_argv({})
        self.assertTrue(argv[1].endswith("esp32peer.py"))
        self.assertEqual(["reset", "--device",
                          esp32peer.default_device(ACCEPTOR_BRIDGE),
                          "--address", ACCEPTOR_ADDRESS, "--role", "acceptor"],
                         list(argv[2:]))

    def test_the_aliases_reach_the_driver_for_the_run_transcript(self):
        # With them, esp32peer.py marks every line of its conversation with
        # the two shells, so the run's transcript holds the pair's side.
        case = known_classic_cases(arguments("Capture known Classic device"),
                                   RESOURCES, IDENTITIES)[0]
        steps = {s.step_id: s for s in case.steps}
        argv = list(steps["stream-and-receive"].resolve_argv({}))
        self.assertEqual("ESP32_Linux0", argv[argv.index("--acceptor-alias") + 1])
        self.assertEqual("ESP32_Linux1", argv[argv.index("--initiator-alias") + 1])
        reset = {s.step_id: s for s in case.cleanup}["reset-acceptor"]
        reset_argv = list(reset.resolve_argv({}))
        self.assertEqual("ESP32_Linux0", reset_argv[reset_argv.index("--alias") + 1])

    def test_the_capture_step_runs_the_tool_after_a_double_dash(self):
        cases = {case.case_id: case for case in known_classic_cases(
            arguments("Capture known Classic device"), {}, IDENTITIES)}
        step = {s.step_id: s for s in cases["sweep"].steps}["stream-and-receive"]
        argv = list(step.resolve_argv({}))
        self.assertEqual(["--", "ubertooth-rx", "-U", SERIAL, "-t", "25"],
                         argv[argv.index("--"):])
        self.assertEqual(
            esp32peer.default_device(INITIATOR_BRIDGE),
            argv[argv.index("--initiator") + 1])
        # The capture's own floor is the driver's to enforce: it times the
        # child, where this step's duration is the whole session.
        self.assertEqual("24.5", argv[argv.index("--min-seconds") + 1])
        # The deadline covers the pair's bring-up as well as the capture.
        self.assertGreater(step.timeout_s, 25.0 + PAIR_SETUP_S - 1)
        self.assertEqual(0.0, step.min_duration_s)
        self.assertTrue(step.mutates)

    def test_a_capture_file_goes_in_the_cases_capture_directory(self):
        cases = {case.case_id: case for case in known_classic_cases(
            arguments("Capture known Classic device", "comprehensive"), {}, IDENTITIES)}
        case = cases["survey-pcapng"]
        self.assertTrue(case.capture_dir)
        step = {s.step_id: s for s in case.steps}["stream-and-receive"]
        argv = list(step.resolve_argv({CAPTURE_DIR_KEY: "/run/captures/x"}))
        self.assertEqual(["-r", "/run/captures/x/survey.pcapng"], argv[-2:])
        verify = {s.step_id: s for s in case.steps}["verify-capture"]
        self.assertEqual("/run/captures/x/survey.pcapng",
                         verify.resolve_argv({CAPTURE_DIR_KEY: "/run/captures/x"})[2])

    def test_every_case_resets_both_peers_and_restores_the_board(self):
        for case in known_classic_cases(
            arguments("Capture known Classic device", "comprehensive"), {}, IDENTITIES
        ):
            with self.subTest(case.case_id):
                cleanup = [step.step_id for step in case.cleanup]
                self.assertEqual(["reset-initiator", "reset-acceptor",
                                  "cleanup-stop"], cleanup[:3])
                self.assertEqual("restored-device-identity", cleanup[-1])


class FollowCaseTests(unittest.TestCase):

    def test_smoke_follows_the_central_and_nothing_else(self):
        cases = follow_classic_cases(arguments("Follow Classic Bluetooth piconet"),
                                     {}, IDENTITIES)
        self.assertEqual(["follow-lap-uap", "follow-other-lap"],
                         [case.case_id for case in cases])
        steps = {case.case_id: {s.step_id: s for s in case.steps} for case in cases}
        follow = steps["follow-lap-uap"]["stream-and-receive"].resolve_argv({})
        self.assertEqual(["-l", CENTRAL_LAP, "-u", CENTRAL_UAP], list(follow[-4:]))
        other = steps["follow-other-lap"]["stream-and-receive"].resolve_argv({})
        self.assertEqual(["-l", OTHER_LAP, "-u", OTHER_UAP], list(other[-4:]))

    def test_comprehensive_adds_the_lap_only_run_and_a_pcap(self):
        cases = follow_classic_cases(
            arguments("Follow Classic Bluetooth piconet", "comprehensive"), {},
            IDENTITIES)
        self.assertEqual(["follow-lap-uap", "follow-other-lap",
                          "follow-lap-only", "follow-pcap"],
                         [case.case_id for case in cases])

    def test_the_follow_case_checks_the_tools_packet_lines(self):
        case = follow_classic_cases(arguments("Follow Classic Bluetooth piconet"),
                                    {}, IDENTITIES)[0]
        check = {s.step_id: s for s in case.steps}["stream-and-receive"].output_check
        self.assertEqual("", check(packets(CENTRAL_LAP, MIN_PACKETS)))
        self.assertIn("expected at least", check(packets(CENTRAL_LAP, 1)))
        # A line that is not a packet line is not one of this piconet's: a
        # truncated one does not count towards the floor.
        self.assertIn("expected at least",
                      check("systime=1 ch=28 LAP=e9c5ba\n" * MIN_PACKETS))


class RecordFieldCaseTests(unittest.TestCase):
    """Classic capture record fields: what each case captures and checks."""

    def cases(self, coverage="smoke"):
        return {case.case_id: case
                for case in record_fields_cases(
                    arguments("Classic capture record fields", coverage),
                    RESOURCES, IDENTITIES)}

    def steps(self, case):
        return {step.step_id: step for step in case.steps}

    def test_smoke_captures_the_piconet_both_ways_round(self):
        cases = self.cases()
        self.assertEqual(["follow-fields", "swapped-fields"], list(cases))
        self.assertEqual(("linux",), cases["follow-fields"].needs.platforms)
        self.assertTrue(cases["follow-fields"].capture_dir)

    def test_comprehensive_adds_the_audio_link_and_a_survey(self):
        self.assertEqual(["follow-fields", "swapped-fields", "audio-fields",
                          "survey-fields"], list(self.cases("comprehensive")))

    def test_the_follow_case_writes_both_formats_and_checks_each(self):
        steps = self.steps(self.cases()["follow-fields"])
        argv = list(steps["stream-and-receive"].resolve_argv(
            {CAPTURE_DIR_KEY: "/run/x"}))
        self.assertEqual(["--", "ubertooth-rx", "-U", SERIAL, "-t", "25",
                          "-l", CENTRAL_LAP, "-u", CENTRAL_UAP,
                          "-q", "/run/x/records.pcap",
                          "-r", "/run/x/records.pcapng"],
                         argv[argv.index("--"):])
        for step_id, name in (("verify-pcap", "records.pcap"),
                              ("verify-pcapng", "records.pcapng")):
            checks = list(steps[step_id].resolve_argv({CAPTURE_DIR_KEY: "/run/x"}))
            self.assertTrue(checks[1].endswith("bredrrecord.py"))
            self.assertEqual(f"/run/x/{name}", checks[2])
            self.assertEqual(CENTRAL_LAP, checks[checks.index("--lap") + 1])
            self.assertEqual(CENTRAL_LAP,
                             checks[checks.index("--reference-lap") + 1])
            self.assertEqual(CENTRAL_UAP,
                             checks[checks.index("--reference-uap") + 1])
            self.assertEqual(str(MIN_PACKETS),
                             checks[checks.index("--min-packets") + 1])
            self.assertIn("--min-reference-uap", checks)
            self.assertIn("--min-hec-valid", checks)
            self.assertIn(r"BR/EDR record problem:",
                          steps[step_id].forbidden_patterns)
        # Only the PCAPNG carries the address the capture was following, and
        # the tool knows its LAP and UAP but never its NAP.
        pcapng = list(steps["verify-pcapng"].resolve_argv({CAPTURE_DIR_KEY: "/x"}))
        self.assertEqual(CENTRAL_LAP, pcapng[pcapng.index("--following-lap") + 1])
        self.assertEqual(CENTRAL_UAP, pcapng[pcapng.index("--following-uap") + 1])
        self.assertEqual("ff", pcapng[pcapng.index("--following-uap-mask") + 1])
        self.assertNotIn(
            "--following-lap",
            list(steps["verify-pcap"].resolve_argv({CAPTURE_DIR_KEY: "/x"})))

    def test_the_swapped_case_follows_the_other_board(self):
        steps = self.steps(self.cases()["swapped-fields"])
        argv = list(steps["stream-and-receive"].resolve_argv(
            {CAPTURE_DIR_KEY: "/x"}))
        # The peers are bound the other way round, so the other board pages.
        self.assertEqual(INITIATOR_ADDRESS, argv[argv.index("--acceptor-address") + 1])
        self.assertEqual(ACCEPTOR_ADDRESS, argv[argv.index("--initiator-address") + 1])
        self.assertEqual(["-l", OTHER_LAP, "-u", OTHER_UAP],
                         argv[argv.index("--") + 6:argv.index("-r")])
        checks = list(steps["verify-pcapng"].resolve_argv({CAPTURE_DIR_KEY: "/x"}))
        self.assertEqual(OTHER_LAP, checks[checks.index("--lap") + 1])
        self.assertEqual(OTHER_LAP, checks[checks.index("--following-lap") + 1])
        self.assertEqual(OTHER_UAP, checks[checks.index("--following-uap") + 1])

    def test_the_audio_case_drives_the_pairs_audio_mode(self):
        case = self.cases("comprehensive")["audio-fields"]
        steps = self.steps(case)
        step = steps["stream-and-receive"]
        argv = list(step.resolve_argv({CAPTURE_DIR_KEY: "/x"}))
        self.assertEqual("audio", argv[2])
        self.assertEqual(AUDIO_TYPE, argv[argv.index("--type") + 1])
        self.assertIn("--min-frames", argv)
        # Its report must name the type the link settled on, and the
        # deadline covers the longer bring-up.
        self.assertTrue(any("audio link: esco" in pattern
                            for pattern in step.output_patterns))
        self.assertGreater(step.timeout_s, 25.0 + AUDIO_SETUP_S - 1)
        # The tool's own packet lines need the piconet's clock, which it does
        # not settle on from audio traffic, so the records carry the check.
        self.assertIsNone(step.output_check)
        checks = list(steps["verify-pcapng"].resolve_argv({CAPTURE_DIR_KEY: "/x"}))
        self.assertEqual(AUDIO_TYPE_NAME,
                         checks[checks.index("--require-types") + 1])

    def test_the_survey_case_asks_for_a_recovered_uap_and_no_address(self):
        steps = self.steps(self.cases("comprehensive")["survey-fields"])
        argv = list(steps["stream-and-receive"].resolve_argv({CAPTURE_DIR_KEY: "/x"}))
        self.assertEqual(["-z", "-r", "/x/survey.pcapng"],
                         argv[argv.index("--") + 6:])
        checks = list(steps["verify-pcapng"].resolve_argv({CAPTURE_DIR_KEY: "/x"}))
        # A survey records whatever is in range under no reference LAP, so
        # the LAP and the reference address are not asked for.
        for absent in ("--lap", "--reference-lap", "--reference-uap",
                       "--min-hec-valid", "--following-lap"):
            self.assertNotIn(absent, checks)
        self.assertIn("--min-reference-uap", checks)

    def test_the_capture_step_checks_the_clock_beside_each_packet(self):
        from ubertooth_harness.receive_cases import (
            CLK100NS_PER_SLOT,
            CLKN_TICKS,
            clock_check,
        )

        check = clock_check()
        good = ("systime=1793525441 ch=76 LAP=e9c5ba err=0 clkn=75132 "
                "clk_offset=2104 s=-36 n=-46 snr=10")
        self.assertEqual("", check(good))
        self.assertIn("past the", check(good.replace(
            "clkn=75132", f"clkn={CLKN_TICKS}")))
        self.assertIn("into its slot", check(good.replace(
            "clk_offset=2104", f"clk_offset={CLK100NS_PER_SLOT}")))
        # The followed cases compose it with the packet-line count.
        step = self.steps(self.cases()["follow-fields"])["stream-and-receive"]
        self.assertIn("into its slot", step.output_check(
            packets(CENTRAL_LAP, MIN_PACKETS)
            + good.replace("clk_offset=2104", "clk_offset=99999") + "\n"))

    def test_the_steps_patterns_match_what_the_driver_really_prints(self):
        """Each pattern against the driver's own report, not a copy of it.

        A pattern is written from what the driver is meant to print, so the
        two can drift apart and nothing fails until a bench run. This builds
        both reports from the firmware fake and matches every pattern
        against them.
        """
        import io
        import re as regex
        import sys as system

        from tests.fake_esp32 import FakePeerFirmware, opener

        for audio, cases in ((False, self.cases()),
                             (True, self.cases("comprehensive"))):
            acceptor = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                        version=esp32peer.AUDIO_FIRMWARE)
            initiator = FakePeerFirmware(INITIATOR_ADDRESS,
                                         version=esp32peer.AUDIO_FIRMWARE)
            acceptor.link(initiator)
            open_one = opener({"/dev/a": acceptor, "/dev/b": initiator})
            peers = (esp32peer.Peer("/dev/a", "acceptor", ACCEPTOR_ADDRESS,
                                    opener=open_one),
                     esp32peer.Peer("/dev/b", "initiator", INITIATOR_ADDRESS,
                                    opener=open_one))
            report = io.StringIO()
            drive = esp32peer.audio if audio else esp32peer.stream
            extra = ({"packet_type": AUDIO_TYPE, "min_frames": 10} if audio
                     else {"min_writes": 10})
            drive(*peers, (system.executable, "-c", "pass"), out=report,
                  settle_s=0.0, **extra)
            case = cases["audio-fields" if audio else "follow-fields"]
            step = self.steps(case)["stream-and-receive"]
            for pattern in step.output_patterns:
                with self.subTest(audio=audio, pattern=pattern):
                    self.assertRegex(report.getvalue(), regex.compile(pattern))

    def test_every_case_resets_both_peers_and_restores_the_board(self):
        for case_id, case in self.cases("comprehensive").items():
            with self.subTest(case_id):
                cleanup = [step.step_id for step in case.cleanup]
                self.assertEqual(["reset-initiator", "reset-acceptor",
                                  "cleanup-stop"], cleanup[:3])
                self.assertEqual("restored-device-identity", cleanup[-1])


class IdentityTests(unittest.TestCase):

    def test_both_peers_are_required_with_an_address(self):
        for role, identity, reason in (
            ("acceptor", {}, "requires an acceptor"),
            ("initiator", {}, "requires an initiator"),
            ("acceptor", {"bridge serial": ACCEPTOR_BRIDGE}, "BD_ADDR must look like"),
        ):
            with self.subTest(role), self.assertRaisesRegex(StateCaseError, reason):
                known_classic_cases(arguments("Capture known Classic device"), {},
                                    {**IDENTITIES, role: identity})

    def test_one_board_cannot_be_both_peers(self):
        with self.assertRaisesRegex(StateCaseError, "needs two ESP32 peers"):
            known_classic_cases(arguments("Capture known Classic device"), {},
                                {**IDENTITIES, "initiator": IDENTITIES["acceptor"]})

    def test_a_registered_serial_device_wins_over_the_derived_path(self):
        path = "/dev/serial/by-id/usb-chosen-if00-port0"
        identities = {**IDENTITIES,
                      "acceptor": {**IDENTITIES["acceptor"], "serial device": path}}
        case = known_classic_cases(arguments("Capture known Classic device"), {},
                                   identities)[0]
        step = {s.step_id: s for s in case.steps}["stream-and-receive"]
        argv = list(step.resolve_argv({}))
        self.assertEqual(path, argv[argv.index("--acceptor") + 1])


class RegistryTests(unittest.TestCase):

    def test_peer_registry_rules(self):
        registry_with(REGISTRY)
        for old, new, reason in (
            (f'bridge serial: "{ACCEPTOR_BRIDGE}"', 'bridge serial: "a/b"',
             "requires a 'bridge serial'"),
            (f'bridge serial: "{ACCEPTOR_BRIDGE}"', 'bridge serial: ""',
             "requires a 'bridge serial'"),
            (f'      BD_ADDR: "{ACCEPTOR_ADDRESS}"\n', "", "requires a 'BD_ADDR'"),
            (f'BD_ADDR: "{ACCEPTOR_ADDRESS}"', 'BD_ADDR: "C8-85"', "requires a 'BD_ADDR'"),
            (f'BD_ADDR: "{ACCEPTOR_ADDRESS}"',
             f'BD_ADDR: "{ACCEPTOR_ADDRESS}"\n      tty: /dev/ttyUSB0', "unknown key"),
            (f'BD_ADDR: "{ACCEPTOR_ADDRESS}"',
             f'BD_ADDR: "{ACCEPTOR_ADDRESS}"\n      serial device: /dev/ttyUSB0',
             "must be a /dev/serial/by-id/ path"),
        ):
            with self.subTest(new), self.assertRaisesRegex(RegistryError, reason):
                registry_with(REGISTRY.replace(old, new, 1))

    def test_the_lap_and_uap_a_registry_writes_down_are_notes(self):
        # The lab registry records what the board's status reports, for a
        # person reading it. The registry takes them as written; the cases
        # refuse a note that disagrees with the address.
        text = REGISTRY.replace(
            f'BD_ADDR: "{ACCEPTOR_ADDRESS}"',
            f'BD_ADDR: "{ACCEPTOR_ADDRESS}"\n      LAP: "2a1b2a"\n'
            '      UAP: "41"\n      name: UT-ESP32-2A1B2A', 1)
        registry_with(text)
        registry_with(text.replace('LAP: "2a1b2a"', 'LAP: "2a1b2b"', 1))
        with self.assertRaisesRegex(
            StateCaseError,
            "the acceptor's registered LAP is 2a1b2b, but its BD_ADDR "
            "C8:85:41:2A:1B:2A gives 2a1b2a"
        ):
            known_classic_cases(
                arguments("Capture known Classic device"), {},
                {**IDENTITIES,
                 "acceptor": {**IDENTITIES["acceptor"], "LAP": "2a1b2b"}})

    def test_a_bridge_serial_is_taken_as_the_board_reports_it(self):
        # udev puts the USB iSerial string in the by-id name as the device
        # gives it, and a CP210x serial can be reprogrammed to any string,
        # so the case and the length are the board's to choose.
        for serial in ("AAAA0000AAAA0000AAAA0000AAAA0000", "0001", "esp-A.1"):
            with self.subTest(serial):
                registry = registry_with(REGISTRY.replace(ACCEPTOR_BRIDGE, serial, 1))
                peer = registry.get("ESP32_Linux0")
                self.assertEqual(serial, peer.identity["bridge serial"])
                self.assertEqual(f"esp32-{serial}", leases.resource_key(peer))
                self.assertIn(serial, esp32peer.default_device(serial))

    def test_a_peer_must_declare_a_role_the_firmware_has(self):
        # The host names its default by type here, so the role rule is what
        # the registry complains about.
        text = REGISTRY.replace("      acceptor: ESP32_Linux0\n"
                                "      initiator: ESP32_Linux1\n",
                                "      ESP32 Classic peer: ESP32_Linux0\n")
        registry_with(text)
        with self.assertRaisesRegex(RegistryError, "roles must be one or more of "
                                    "acceptor, initiator"):
            registry_with(text.replace("roles: [acceptor, initiator]",
                                       "roles: [primary]", 1))

    def test_the_example_registry_and_the_classic_plan_compile(self):
        registry = FileResourceRegistry.load(ROOT / "hardware.example.yaml")
        plan = load_plan(ROOT / "testplans" / "classic.yaml")
        catalog = builtin_catalog()
        for suite, counts in (("classic", (2, 2, 2)),
                              ("classic-comprehensive", (4, 4, 4)),
                              ("classic-long", (2,))):
            invocations = PlanCompiler(catalog, registry).compile(plan, suite)
            self.assertEqual(len(counts), len(invocations))
            for invocation, count in zip(invocations, counts):
                self.assertEqual("ESP32_Linux0", invocation.resources["acceptor"])
                self.assertEqual("ESP32_Linux1", invocation.resources["initiator"])
                cases = catalog.get(invocation.test_name).command_cases(
                    invocation.arguments, invocation.resources,
                    invocation.resource_identities)
                self.assertEqual(count, len(cases))

    def test_the_peers_are_leased_by_bridge_serial(self):
        registry = registry_with(REGISTRY)
        invocations = PlanCompiler(builtin_catalog(), registry).compile(
            load_plan(ROOT / "testplans" / "classic.yaml"), "classic")
        boards = leases.boards_for_invocations(invocations, registry)
        self.assertEqual(
            [("UT_Linux0", SERIAL), ("ESP32_Linux0", f"esp32-{ACCEPTOR_BRIDGE}"),
             ("ESP32_Linux1", f"esp32-{INITIATOR_BRIDGE}")],
            [(board.alias, board.key) for board in boards])

    def test_preflight_checks_both_bridges(self):
        registry = registry_with(REGISTRY)
        invocations = PlanCompiler(builtin_catalog(), registry).compile(
            load_plan(ROOT / "testplans" / "classic.yaml"), "classic")
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = run_contract(Path(directory), tuple(invocations))
        checks = PreflightPolicy(registry).compile(invocations, manifest)
        peers = [check for check in checks
                 if check.resource_alias in ("ESP32_Linux0", "ESP32_Linux1")]
        self.assertEqual(2, len(peers))
        self.assertTrue(peers[0].argv[1].endswith("esp32peer.py"))
        self.assertEqual(
            ["identify", "--device",
             esp32peer.default_device(ACCEPTOR_BRIDGE),
             "--address", ACCEPTOR_ADDRESS],
            list(peers[0].argv[2:]))


class ReportTests(unittest.TestCase):

    def test_the_devices_row_names_a_peer_by_its_bridge_serial(self):
        from ubertooth_harness.report import _Run, _setup_rows

        records = []
        for index, (alias, identity) in enumerate((
            ("UT_Linux0", {"serial": SERIAL}),
            ("ESP32_Linux0", {"bridge serial": ACCEPTOR_BRIDGE,
                              "BD_ADDR": ACCEPTOR_ADDRESS}),
        )):
            check = {"phase": "preflight", "check_id": f"p{index}",
                     "check_name": "Device serial"}
            records.append({"run_id": "r", "event_type": "check_started",
                            "status": "running",
                            "timestamp": "2026-01-01T00:00:00+00:00",
                            "details": {**check, "resource": alias,
                                        "resource_identity": identity}})
            records.append({"run_id": "r", "event_type": "check_finished",
                            "status": "passed",
                            "timestamp": "2026-01-01T00:00:00+00:00",
                            "details": check})
        rows = dict(_setup_rows(_Run(records)))
        self.assertIn(f"ESP32_Linux0 serial {ACCEPTOR_BRIDGE}", rows["Devices"])
        self.assertIn(f"UT_Linux0 serial {SERIAL}", rows["Devices"])


if __name__ == "__main__":
    unittest.main()
