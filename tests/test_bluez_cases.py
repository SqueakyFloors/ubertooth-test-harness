"""The BlueZ-tool cases: the adapter resource, the commands, and the checks.

No bench has a Bluetooth adapter yet (issue #88), so everything here runs
against the output samples in tests/fake_bluez.py and against the real
esp32peer.py driver talking to the ESP32 firmware fake. What a run on real
hardware has still to settle is recorded in docs/tests.md, per case.
"""

import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness import esp32peer, leases
from ubertooth_harness.bluez_cases import (
    FOLLOW_TEST_NAME,
    INQUIRY_TEST_NAME,
    adapter,
    follow_over_hci_cases,
    AFH_READ_S,
    EXTENDED_REPORT_S,
    FOLLOW_SETUP_S,
    INQUIRY_S,
    NAME_READS_S,
    extended_report,
    inquiry_found,
    inquiry_scan_cases,
    maps_reported,
    scan_results_list,
)
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.classic_cases import MIN_PACKETS
from ubertooth_harness.plan import check_runnable, load_plan
from ubertooth_harness.planner import PlanCompiler, PlanningError
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from ubertooth_harness.stateful import CAPTURE_DIR_KEY, StateCaseError

from tests import fake_bluez
from tests.test_classic_cases import (
    ACCEPTOR_ADDRESS,
    ACCEPTOR_BRIDGE,
    INITIATOR_ADDRESS,
    INITIATOR_BRIDGE,
    REGISTRY,
    SERIAL,
)

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "testplans" / "hci.yaml"
ADAPTER_ROLE = "Bluetooth adapter"
ADAPTER_ALIAS = "BT_Linux0"
CENTRAL_LAP, CENTRAL_UAP = fake_bluez.CENTRAL_LAP, fake_bluez.CENTRAL_UAP
ACCEPTOR_NAME = fake_bluez.ACCEPTOR_NAME
CONTEXT = {CAPTURE_DIR_KEY: "/run/captures/hci"}

RESOURCES = {"Ubertooth": "UT_Linux0", ADAPTER_ROLE: ADAPTER_ALIAS,
             "acceptor": "ESP32_Linux0", "initiator": "ESP32_Linux1"}
IDENTITIES = {
    "Ubertooth": {"serial": SERIAL},
    ADAPTER_ROLE: {"hci device": fake_bluez.ADAPTER,
                   "BD_ADDR": fake_bluez.ADAPTER_ADDRESS},
    "acceptor": {"bridge serial": ACCEPTOR_BRIDGE, "BD_ADDR": ACCEPTOR_ADDRESS},
    "initiator": {"bridge serial": INITIATOR_BRIDGE,
                  "BD_ADDR": INITIATOR_ADDRESS},
}
ADAPTER_BLOCK = f"""
  {ADAPTER_ALIAS}:
    type: {ADAPTER_ROLE}
    attached to: Host_Linux
    identity:
      hci device: {fake_bluez.ADAPTER}
      BD_ADDR: "{fake_bluez.ADAPTER_ADDRESS}"
"""
# The Classic registry of tests/test_classic_cases.py, with the adapter added
# and named as the host's default.
ADAPTER_REGISTRY = REGISTRY.replace(
    "      initiator: ESP32_Linux1\n",
    f"      initiator: ESP32_Linux1\n      {ADAPTER_ROLE}: {ADAPTER_ALIAS}\n",
    1,
) + ADAPTER_BLOCK
# A host and nothing but the adapter, for the identity checks: the shared
# registry above also holds peers with a BD_ADDR of their own, so a patch
# aimed at the adapter's keys would land on theirs.
ADAPTER_ONLY = f"""
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [test target]
    defaults: {{}}
{ADAPTER_BLOCK}"""


def arguments(test_name, coverage="smoke", duration="25s"):
    return builtin_catalog().get(test_name).normalize_arguments(
        {"coverage": coverage, "duration": duration})


def scan_cases(coverage="smoke", identities=None):
    return {
        case.case_id: case
        for case in inquiry_scan_cases(
            arguments(INQUIRY_TEST_NAME, coverage), RESOURCES,
            identities or IDENTITIES)
    }


def follow_cases(coverage="smoke", identities=None):
    return {
        case.case_id: case
        for case in follow_over_hci_cases(
            arguments(FOLLOW_TEST_NAME, coverage), RESOURCES,
            identities or IDENTITIES)
    }


def steps(case):
    return {step.step_id: step for step in case.steps}


def tool_argv(case):
    """The command line of the tool under test, after the driver's `--`."""
    argv = list(steps(case)["pair-and-run"].resolve_argv(CONTEXT))
    return argv[argv.index("--") + 1:]


def driver_argv(case):
    argv = list(steps(case)["pair-and-run"].resolve_argv(CONTEXT))
    return argv[:argv.index("--")]


def registry_with(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "hardware.yaml"
        path.write_text(text, encoding="utf-8")
        return FileResourceRegistry.load(path)


class AdapterResourceTests(unittest.TestCase):
    """The registry's Bluetooth adapter."""

    def test_the_adapter_needs_an_hci_device_and_its_own_address(self):
        registry_with(ADAPTER_ONLY)
        for key, reason in (
            ("hci device", "requires an 'hci device' identity"),
            ("BD_ADDR", "requires a 'BD_ADDR' identity"),
        ):
            with self.subTest(reason):
                # Renamed rather than removed, so the identity is still a
                # mapping and this is the missing key it is about.
                with self.assertRaisesRegex(RegistryError, reason):
                    registry_with(ADAPTER_ONLY.replace(
                        f"      {key}:", "      description:", 1))

    def test_an_hci_device_that_is_not_hciN_is_refused(self):
        for device in ("hci", "0", "/dev/hci0", "hci0000", "HCI0"):
            with self.subTest(device):
                with self.assertRaisesRegex(RegistryError, "hci device"):
                    registry_with(ADAPTER_ONLY.replace(
                        f"hci device: {fake_bluez.ADAPTER}",
                        f"hci device: {device}", 1))

    def test_an_adapter_takes_no_roles(self):
        with self.assertRaisesRegex(RegistryError, "takes no roles"):
            registry_with(ADAPTER_ONLY.replace(
                f"    type: {ADAPTER_ROLE}\n",
                f"    type: {ADAPTER_ROLE}\n    roles: [controller]\n", 1))

    def test_an_unknown_identity_key_is_refused(self):
        with self.assertRaisesRegex(RegistryError, "unknown key"):
            registry_with(ADAPTER_ONLY.replace(
                f"      hci device: {fake_bluez.ADAPTER}\n",
                f"      hci device: {fake_bluez.ADAPTER}\n      serial: x\n", 1))

    def test_the_adapter_is_leased_by_its_address(self):
        registry = registry_with(ADAPTER_REGISTRY)
        invocations = PlanCompiler(builtin_catalog(), registry).compile(
            load_plan(PLAN), "hci")
        boards = leases.boards_for_invocations(invocations, registry)
        expected = "bluez-" + fake_bluez.ADAPTER_ADDRESS.replace(":", "").lower()
        self.assertIn((ADAPTER_ALIAS, expected),
                      [(board.alias, board.key) for board in boards])

    def test_two_hosts_adapters_called_hci0_get_different_lease_keys(self):
        """hciN is attach order, so the address is what names the lock."""
        from ubertooth_harness.resources import Resource
        keys = {
            leases.resource_key(Resource(
                alias=alias, type=ADAPTER_ROLE, platform=None, roles=(),
                defaults={}, attached_to="Host_Linux",
                identity={"hci device": "hci0", "BD_ADDR": address},
                capabilities=(), power={}, driver=None))
            for alias, address in (("BT_A", "00:1A:7D:DA:71:13"),
                                   ("BT_B", "00:1A:7D:DA:71:14"))
        }
        self.assertEqual(2, len(keys))

    def test_a_bench_without_an_adapter_is_told_what_it_has_not_got(self):
        """Every step of this plan binds the adapter, so a bench without one
        has nothing to run and is refused, naming the missing resource.

        A bench without an adapter leaves the two tests out of any suite
        that holds other work, as not applicable (#267); this plan holds
        only these two, so the whole suite is empty and that is an error
        rather than a run that does nothing.
        """
        registry = registry_with(REGISTRY)
        compiler = PlanCompiler(builtin_catalog(), registry)
        with self.assertRaisesRegex(
            PlanningError, rf"no default {ADAPTER_ROLE}"
        ):
            compiler.compile_suite(load_plan(PLAN), "hci")

    def test_a_bench_without_an_adapter_leaves_the_tests_out_of_a_wider_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.yaml"
            path.write_text(
                "plan: mixed\nsuites:\n  mixed:\n"
                "    - Ubertooth count:\n        hosts: [Host_Linux]\n"
                "        Ubertooth: default\n"
                f"    - {INQUIRY_TEST_NAME}:\n        hosts: [Host_Linux]\n"
                "        Ubertooth: default\n        coverage: smoke\n"
                "        duration: 25s\n",
                encoding="utf-8")
            compiled = PlanCompiler(
                builtin_catalog(), registry_with(REGISTRY)
            ).compile_suite(load_plan(path), "mixed")
        self.assertEqual(["Ubertooth count"],
                         [item.test_name for item in compiled.invocations])
        self.assertEqual([INQUIRY_TEST_NAME],
                         [item.test_name for item in compiled.not_applicable])
        self.assertIn(f"no default {ADAPTER_ROLE}",
                      compiled.not_applicable[0].reason)


class PlanTests(unittest.TestCase):

    def test_the_plan_is_runnable_and_names_both_tests(self):
        catalog = builtin_catalog()
        plan = load_plan(PLAN, catalog)
        check_runnable(plan, catalog)
        for suite in ("hci", "hci-comprehensive"):
            self.assertEqual([INQUIRY_TEST_NAME, FOLLOW_TEST_NAME],
                             [step.test_name for step in plan.steps(suite)])

    def test_the_plan_is_not_in_the_release_suites(self):
        """Nothing here has run against a real adapter, so it does not gate a
        release (issue #88)."""
        release = (ROOT / "testplans" / "release.yaml").read_text(encoding="utf-8")
        for name in (INQUIRY_TEST_NAME, FOLLOW_TEST_NAME):
            self.assertNotIn(name, release)

    def test_the_inquiry_scan_has_left_the_backlog(self):
        backlog = (ROOT / "testplans" / "backlog.yaml").read_text(encoding="utf-8")
        self.assertNotIn(f"- {INQUIRY_TEST_NAME}:", backlog)

    def test_the_example_registry_and_the_plan_compile(self):
        registry = FileResourceRegistry.load(ROOT / "hardware.example.yaml")
        catalog = builtin_catalog()
        plan = load_plan(PLAN, catalog)
        for suite, counts in (("hci", (2, 2)), ("hci-comprehensive", (3, 3))):
            invocations = PlanCompiler(catalog, registry).compile(plan, suite)
            self.assertEqual(len(counts), len(invocations))
            for invocation, count in zip(invocations, counts):
                self.assertEqual(ADAPTER_ALIAS,
                                 invocation.resources[ADAPTER_ROLE])
                cases = catalog.get(invocation.test_name).command_cases(
                    invocation.arguments, invocation.resources,
                    invocation.resource_identities)
                self.assertEqual(count, len(cases))


class IdentityTests(unittest.TestCase):

    def test_a_test_without_an_adapter_is_refused(self):
        without = {role: identity for role, identity in IDENTITIES.items()
                   if role != ADAPTER_ROLE}
        with self.assertRaisesRegex(StateCaseError, "requires a Bluetooth adapter"):
            inquiry_scan_cases(arguments(INQUIRY_TEST_NAME), RESOURCES, without)

    def test_an_adapter_address_that_is_not_an_address_is_refused(self):
        broken = dict(IDENTITIES)
        broken[ADAPTER_ROLE] = {"hci device": "hci0", "BD_ADDR": "nope"}
        with self.assertRaisesRegex(StateCaseError, "BD_ADDR must look like"):
            inquiry_scan_cases(arguments(INQUIRY_TEST_NAME), RESOURCES, broken)

    def test_an_adapter_registered_at_a_peer_address_is_refused(self):
        """Every check tells the piconet from the adapter by address."""
        clashing = dict(IDENTITIES)
        clashing[ADAPTER_ROLE] = {"hci device": "hci0",
                                  "BD_ADDR": INITIATOR_ADDRESS}
        with self.assertRaisesRegex(StateCaseError, "both registered at"):
            follow_over_hci_cases(arguments(FOLLOW_TEST_NAME), RESOURCES,
                                  clashing)


class InquiryCaseTests(unittest.TestCase):

    def test_smoke_runs_the_two_halves_separately(self):
        self.assertEqual(["inquiry", "survey"], list(scan_cases()))

    def test_comprehensive_adds_the_extended_report(self):
        self.assertEqual(["inquiry", "survey", "extended-report"],
                         list(scan_cases("comprehensive")))

    def test_the_tool_is_pointed_at_both_radios_and_bounds_its_own_capture(self):
        self.assertEqual(
            ["ubertooth-scan", "-U", SERIAL, "-b", fake_bluez.ADAPTER,
             "-t", "25", "-s"],
            tool_argv(scan_cases()["inquiry"]))
        # Without -s only the Ubertooth half runs, which needs no inquiry.
        self.assertEqual(
            ["ubertooth-scan", "-U", SERIAL, "-b", fake_bluez.ADAPTER,
             "-t", "25"],
            tool_argv(scan_cases()["survey"]))
        self.assertEqual(
            ["ubertooth-scan", "-U", SERIAL, "-b", fake_bluez.ADAPTER,
             "-t", "25", "-s", "-x"],
            tool_argv(scan_cases("comprehensive")["extended-report"]))

    def test_the_capture_window_is_the_duration_and_may_not_end_early(self):
        argv = driver_argv(scan_cases()["inquiry"])
        # -t bounds the tool's own capture, so the driver does not interrupt
        # it; it only refuses a run that stopped early.
        self.assertNotIn("--interrupt-after", argv)
        self.assertEqual("24.5", argv[argv.index("--min-seconds") + 1])

    def test_the_deadline_allows_for_the_inquiry_the_t_window_does_not_cover(self):
        """ubertooth-scan arms its -t alarm only after the BlueZ inquiry."""
        with_s = driver_argv(scan_cases()["inquiry"])
        without = driver_argv(scan_cases()["survey"])
        self.assertEqual(str(25 + INQUIRY_S + NAME_READS_S + 5.0),
                         with_s[with_s.index("--timeout") + 1])
        self.assertEqual(str(25 + NAME_READS_S + 5.0),
                         without[without.index("--timeout") + 1])
        self.assertGreater(float(with_s[with_s.index("--timeout") + 1]),
                           float(without[without.index("--timeout") + 1]))

    def test_the_extended_report_gets_its_own_much_longer_deadline(self):
        """-x fetches a report over a connection per device it found."""
        argv = driver_argv(scan_cases("comprehensive")["extended-report"])
        self.assertEqual(
            str(25 + INQUIRY_S + NAME_READS_S + EXTENDED_REPORT_S + 5.0),
            argv[argv.index("--timeout") + 1])
        plain = driver_argv(scan_cases()["inquiry"])
        self.assertGreater(float(argv[argv.index("--timeout") + 1]),
                           float(plain[plain.index("--timeout") + 1]))

    def test_every_case_reads_the_adapter_first(self):
        for case_id, case in scan_cases("comprehensive").items():
            with self.subTest(case_id):
                step = steps(case)["adapter-up"]
                self.assertEqual(("hciconfig", fake_bluez.ADAPTER),
                                 step.resolve_argv({}))

    def test_every_case_frees_the_adapter_before_it_resets_the_peers(self):
        """A peer reset drops its links, so a later free check says nothing."""
        for case_id, case in scan_cases("comprehensive").items():
            with self.subTest(case_id):
                cleanup = [step.step_id for step in case.cleanup]
                self.assertEqual(
                    ["adapter-free", "reset-initiator", "reset-acceptor",
                     "cleanup-stop"], cleanup[:4])
                self.assertEqual("restored-device-identity", cleanup[-1])

    def test_every_case_checks_the_adapter_is_free_before_the_tool_runs(self):
        for case_id, case in scan_cases("comprehensive").items():
            with self.subTest(case_id):
                order = [step.step_id for step in case.steps]
                self.assertLess(order.index("adapter-idle"),
                                order.index("pair-and-run"))

    def test_the_cases_declare_linux_bluez_and_the_two_host_tools(self):
        needs = scan_cases()["inquiry"].needs
        self.assertEqual(("linux",), needs.platforms)
        self.assertEqual(("ubertooth-scan",), needs.bluez_tools)
        self.assertEqual(("hciconfig", "hcitool"), needs.host_tools)


class FollowCaseTests(unittest.TestCase):

    def test_smoke_follows_the_central_with_and_without_afh(self):
        self.assertEqual(["follow-target", "follow-target-afh"],
                         list(follow_cases()))

    def test_comprehensive_adds_the_pcap(self):
        self.assertEqual(
            ["follow-target", "follow-target-afh", "follow-target-pcap"],
            list(follow_cases("comprehensive")))

    def test_the_target_is_the_piconet_central_not_the_acceptor(self):
        """The initiator pages, so its LAP is the access code on the air."""
        self.assertEqual(
            ["ubertooth-follow", "-U", SERIAL, "-b", fake_bluez.ADAPTER,
             "-t", INITIATOR_ADDRESS],
            tool_argv(follow_cases()["follow-target"]))
        self.assertEqual(
            ["ubertooth-follow", "-U", SERIAL, "-b", fake_bluez.ADAPTER,
             "-t", INITIATOR_ADDRESS, "-a"],
            tool_argv(follow_cases()["follow-target-afh"]))

    def test_the_window_is_the_capture_plus_what_the_tool_does_before_it(self):
        """The page and the clock reads are inside the window the driver
        interrupts at, so a window of the duration alone would leave a slow
        page no capture, and the floor could not catch it."""
        argv = driver_argv(follow_cases()["follow-target"])
        window = 25 + FOLLOW_SETUP_S
        self.assertEqual(str(window), argv[argv.index("--interrupt-after") + 1])
        # The floor is the whole window: the driver decides when it ends, so
        # a command that returned sooner stopped for a reason of its own.
        self.assertEqual(str(window - 0.5),
                         argv[argv.index("--min-seconds") + 1])
        self.assertGreater(window, 25.0)
        step = steps(follow_cases()["follow-target"])["pair-and-run"]
        self.assertTrue(step.mutates)
        self.assertGreater(step.timeout_s, window)

    def test_the_afh_case_also_allows_for_the_channel_map_read(self):
        plain = driver_argv(follow_cases()["follow-target"])
        with_afh = driver_argv(follow_cases()["follow-target-afh"])
        self.assertEqual(str(25 + FOLLOW_SETUP_S + AFH_READ_S),
                         with_afh[with_afh.index("--interrupt-after") + 1])
        self.assertGreater(
            float(with_afh[with_afh.index("--interrupt-after") + 1]),
            float(plain[plain.index("--interrupt-after") + 1]))

    def test_the_pcap_case_names_the_file_in_the_case_directory(self):
        case = follow_cases("comprehensive")["follow-target-pcap"]
        self.assertTrue(case.capture_dir)
        self.assertEqual(
            ["ubertooth-follow", "-U", SERIAL, "-b", fake_bluez.ADAPTER,
             "-t", INITIATOR_ADDRESS, "-q",
             "/run/captures/hci/follow.pcap"],
            tool_argv(case))
        self.assertEqual(["verify-capture", "verify-records"],
                         [step.step_id for step in case.steps][-2:])

    def test_the_pcap_records_are_decoded_and_held_to_the_centrals_lap(self):
        """A BR/EDR capture of the right size says nothing about whose
        packets are in it."""
        case = follow_cases("comprehensive")["follow-target-pcap"]
        argv = list(steps(case)["verify-records"].resolve_argv(CONTEXT))
        self.assertEqual("/run/captures/hci/follow.pcap", argv[2])
        self.assertEqual(CENTRAL_LAP, argv[argv.index("--lap") + 1])
        self.assertEqual(CENTRAL_LAP, argv[argv.index("--reference-lap") + 1])
        self.assertEqual(CENTRAL_UAP, argv[argv.index("--reference-uap") + 1])
        self.assertIn("--min-hec-valid", argv)

    def test_no_case_runs_l_and_u_without_t(self):
        """The page then goes to a guessed 00:00: address, and whether it
        arrives is not knowable in advance, so no case asserts an outcome."""
        for case in follow_cases("comprehensive").values():
            with self.subTest(case.case_id):
                self.assertIn("-t", tool_argv(case))
                self.assertNotIn("-l", tool_argv(case))

    def test_the_cases_declare_linux_bluez_and_the_two_host_tools(self):
        needs = follow_cases()["follow-target"].needs
        self.assertEqual(("linux",), needs.platforms)
        self.assertEqual(("ubertooth-follow",), needs.bluez_tools)
        self.assertEqual(("hciconfig", "hcitool"), needs.host_tools)


class AdapterStepTests(unittest.TestCase):
    """The hciconfig and hcitool steps, against BlueZ's own output."""

    def patterns(self, step, text, *, forbidden_found=()):
        for pattern in step.output_patterns:
            with self.subTest(expected=pattern):
                self.assertRegex(text, re.compile(pattern))
        for pattern in step.forbidden_patterns:
            with self.subTest(forbidden=pattern):
                found = re.search(pattern, text) is not None
                self.assertEqual(pattern in forbidden_found, found)

    def test_an_adapter_that_is_up_passes_the_read(self):
        step = steps(scan_cases()["inquiry"])["adapter-up"]
        self.patterns(step, fake_bluez.hciconfig(up=True))

    def test_an_adapter_that_is_down_fails_the_read(self):
        step = steps(scan_cases()["inquiry"])["adapter-up"]
        text = fake_bluez.hciconfig(up=False)
        self.assertNotRegex(text, re.compile(r"(?m)^\s+UP RUNNING\b"))
        self.assertRegex(text, re.compile(r"(?m)^\s+DOWN\b"))

    def test_an_adapter_at_another_address_fails_the_read(self):
        """The hciN number is attach order; the address is the adapter."""
        step = steps(scan_cases()["inquiry"])["adapter-up"]
        address = next(pattern for pattern in step.output_patterns
                       if "BD Address" in pattern)
        self.assertNotRegex(
            fake_bluez.hciconfig(address="00:11:22:33:44:55"),
            re.compile(address))

    def test_a_free_adapter_passes_the_cleanup_read(self):
        case = follow_cases()["follow-target"]
        step = next(s for s in case.cleanup if s.step_id == "adapter-free")
        self.assertEqual(("hcitool", "-i", fake_bluez.ADAPTER, "con"),
                         step.resolve_argv({}))
        self.patterns(step, fake_bluez.hcitool_con())

    def test_a_link_left_on_the_adapter_fails_the_cleanup_read(self):
        case = follow_cases()["follow-target"]
        step = next(s for s in case.cleanup if s.step_id == "adapter-free")
        for address in (ACCEPTOR_ADDRESS, INITIATOR_ADDRESS):
            with self.subTest(address):
                text = fake_bluez.hcitool_con(address)
                found = [pattern for pattern in step.forbidden_patterns
                         if re.search(pattern, text)]
                self.assertEqual([re.escape(address)], found)

    def test_a_link_to_someone_elses_device_is_not_held_against_the_run(self):
        case = follow_cases()["follow-target"]
        step = next(s for s in case.cleanup if s.step_id == "adapter-free")
        text = fake_bluez.hcitool_con(fake_bluez.OTHER_ADDRESS)
        self.patterns(step, text)


class OutputCheckTests(unittest.TestCase):
    """The checks over each tool's own standard output."""

    def test_the_inquiry_check_wants_the_peer_by_address_and_name(self):
        check = inquiry_found(ACCEPTOR_ADDRESS, ACCEPTOR_NAME)
        found = fake_bluez.scan_inquiry(
            (fake_bluez.OTHER_ADDRESS, "Someone's speaker"),
            (ACCEPTOR_ADDRESS, ACCEPTOR_NAME))
        self.assertEqual("", check(found))
        self.assertIn("not " + ACCEPTOR_ADDRESS, check(fake_bluez.scan_inquiry()))
        self.assertIn(
            "names it",
            check(fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, "[unknown]"))))

    def test_the_peer_name_is_derived_from_its_address(self):
        self.assertEqual(ACCEPTOR_NAME, esp32peer.device_name(ACCEPTOR_ADDRESS))
        self.assertEqual("UT-ESP32-E9C5BA",
                         esp32peer.device_name(INITIATOR_ADDRESS))

    def test_the_survey_check_reads_the_scan_results(self):
        check = scan_results_list(CENTRAL_LAP)
        self.assertEqual("", check(fake_bluez.scan_survey()))
        # A piconet whose UAP the sweep did not recover is still listed by
        # its LAP, and that is what -l takes.
        self.assertEqual("", check(fake_bluez.scan_survey(uap="")))
        # With a name read back from it, which the tool appends.
        self.assertEqual("", check(fake_bluez.scan_survey(name=ACCEPTOR_NAME)))
        self.assertIn("not LAP " + CENTRAL_LAP,
                      check(fake_bluez.scan_survey(lap="2a1b2a", uap="41")))
        self.assertIn("listed nothing", check(""))

    def test_the_map_check_counts_the_lines_the_scan_prints_per_piconet(self):
        self.assertEqual("", maps_reported()(fake_bluez.scan_survey()))
        self.assertIn("0 'AFH map:' lines", maps_reported()(
            fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, ACCEPTOR_NAME))))
        self.assertEqual("", maps_reported(2)(
            fake_bluez.scan_survey() + fake_bluez.scan_survey(lap="2a1b2a")))

    def test_the_extended_report_case_checks_both_halves_and_the_block(self):
        """-x also exits 1 for a dead capture stream, so the case requires
        the piconet in the survey results too."""
        case = scan_cases("comprehensive")["extended-report"]
        check = steps(case)["pair-and-run"].output_check
        whole = (fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, ACCEPTOR_NAME))
                 + fake_bluez.scan_extended()
                 + fake_bluez.scan_survey())
        self.assertEqual("", check(whole))
        self.assertIn("not LAP " + CENTRAL_LAP, check(
            fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, ACCEPTOR_NAME))
            + fake_bluez.scan_extended()))
        self.assertIn("holds no LMP version", check(
            fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, ACCEPTOR_NAME))
            + "\tBD Address:  " + ACCEPTOR_ADDRESS + "\n"
            + "\tDevice Name: " + ACCEPTOR_NAME + "\n"
            + fake_bluez.scan_survey()))

    def test_the_extended_report_is_read_inside_the_right_devices_block(self):
        """Another device's report must not stand in for a missing line."""
        check = extended_report(ACCEPTOR_ADDRESS, ACCEPTOR_NAME)
        self.assertEqual("", check(
            fake_bluez.scan_inquiry((fake_bluez.OTHER_ADDRESS, "A speaker"))
            + fake_bluez.scan_extended(fake_bluez.OTHER_ADDRESS, "A speaker")
            + fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, ACCEPTOR_NAME))
            + fake_bluez.scan_extended()))
        # The acceptor is listed, another device has the only full report.
        self.assertIn("gives its address as nothing", check(
            fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, ACCEPTOR_NAME))
            + fake_bluez.scan_inquiry((fake_bluez.OTHER_ADDRESS, "A speaker"))
            + fake_bluez.scan_extended(fake_bluez.OTHER_ADDRESS, "A speaker")))
        self.assertIn("no device line", check(fake_bluez.scan_extended()))
        self.assertIn("names it", check(
            fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, ACCEPTOR_NAME))
            + fake_bluez.scan_extended(ACCEPTOR_ADDRESS, "Someone else")))

    def test_the_follow_check_wants_the_centrals_packets_and_nothing_else(self):
        check = steps(follow_cases()["follow-target"])["pair-and-run"].output_check
        self.assertEqual("", check(fake_bluez.packet_lines(count=MIN_PACKETS)))
        self.assertIn("expected at least", check(fake_bluez.packet_lines(count=1)))
        self.assertIn("not " + CENTRAL_LAP, check(
            fake_bluez.packet_lines(count=MIN_PACKETS)
            + fake_bluez.packet_lines(lap="2a1b2a", count=1)))

    def test_the_afh_follow_check_also_wants_the_map_the_tool_printed(self):
        check = steps(
            follow_cases()["follow-target-afh"])["pair-and-run"].output_check
        packets = fake_bluez.packet_lines(count=MIN_PACKETS)
        self.assertIn("0 'AFH map:' lines", check(packets))
        self.assertEqual("", check(fake_bluez.WHOLE_BAND_MAP + "\n" + packets))


class ToolPatternTests(unittest.TestCase):
    """Each tool-specific pattern against a sample of that tool's output."""

    def matched(self, case, text, *, skip=()):
        step = steps(case)["pair-and-run"]
        for pattern in step.output_patterns:
            if any(mark in pattern for mark in skip):
                continue
            with self.subTest(pattern=pattern):
                self.assertRegex(text, re.compile(pattern))

    # The driver's own report lines are checked against the real driver
    # below; here only the lines the tool prints.
    DRIVER = ("acceptor ", "initiator ", "central ", "link:", "command exit:",
              "pair streamed")

    def test_the_follow_progress_lines_match_the_tools_output(self):
        self.matched(follow_cases()["follow-target"],
                     fake_bluez.follow_progress(), skip=self.DRIVER)
        self.matched(follow_cases()["follow-target-afh"],
                     fake_bluez.follow_progress(afh=True), skip=self.DRIVER)

    def test_without_a_reads_the_tool_says_it_is_not_using_afh(self):
        plain = fake_bluez.follow_progress()
        with_afh = fake_bluez.follow_progress(afh=True)
        self.assertIn("Not use AFH", plain)
        self.assertNotIn("Not use AFH", with_afh)
        self.matched(follow_cases()["follow-target-afh"], with_afh,
                     skip=self.DRIVER)
        self.assertNotRegex(plain,
                            re.compile(r"(?m)^AFH map read: \d+ of 79 "))

    def test_the_extended_report_case_has_no_patterns_of_its_own(self):
        """Its three lines are read as one device's block, by the check."""
        step = steps(scan_cases("comprehensive")["extended-report"])["pair-and-run"]
        self.assertTrue(all(any(mark in pattern for mark in self.DRIVER)
                            for pattern in step.output_patterns))

    def test_every_driver_pattern_matches_the_drivers_own_report(self):
        """The pair's report, built by the real driver against the firmware fake.

        A pattern is written from what the driver is meant to print, so the
        two can drift apart with nothing failing until a bench run.
        """
        from tests.fake_esp32 import FakePeerFirmware, opener

        acceptor = FakePeerFirmware(ACCEPTOR_ADDRESS)
        initiator = FakePeerFirmware(INITIATOR_ADDRESS)
        acceptor.link(initiator)
        open_one = opener({"/dev/a": acceptor, "/dev/b": initiator})
        peers = (esp32peer.Peer("/dev/a", "acceptor", ACCEPTOR_ADDRESS,
                                opener=open_one),
                 esp32peer.Peer("/dev/b", "initiator", INITIATOR_ADDRESS,
                                opener=open_one))
        report = io.StringIO()
        esp32peer.stream(*peers, (sys.executable, "-c", "pass"), out=report,
                         settle_s=0.0, min_writes=10)
        text = report.getvalue()
        for case in (scan_cases()["inquiry"], follow_cases()["follow-target"]):
            with self.subTest(case.case_id):
                for pattern in steps(case)["pair-and-run"].output_patterns:
                    if not any(mark in pattern for mark in self.DRIVER):
                        continue
                    with self.subTest(pattern=pattern):
                        self.assertRegex(text, re.compile(pattern))

    def test_the_extended_report_case_takes_either_exit_status(self):
        """-x exits 1 when any device's report failed, and the devices it
        asks include whatever else in range answered the inquiry."""
        case = scan_cases("comprehensive")["extended-report"]
        step = steps(case)["pair-and-run"]
        self.assertEqual((0, 1), step.expected_returncodes)
        exits = [pattern for pattern in step.output_patterns
                 if "command exit:" in pattern]
        self.assertEqual([r"(?m)^command exit: [01] after \d+\.\d s$"], exits)
        for status in ("0", "1"):
            with self.subTest(status):
                self.assertRegex(f"command exit: {status} after 30.1 s",
                                 re.compile(exits[0]))

    def test_every_other_case_pins_a_zero_exit(self):
        for cases in (scan_cases("comprehensive"), follow_cases("comprehensive")):
            for case_id, case in cases.items():
                if case_id == "extended-report":
                    continue
                with self.subTest(case_id):
                    step = steps(case)["pair-and-run"]
                    self.assertEqual((0,), step.expected_returncodes)


class AdapterHelperTests(unittest.TestCase):

    def test_the_adapter_helper_reports_the_device_address_and_alias(self):
        self.assertEqual(
            {"device": fake_bluez.ADAPTER, "address": fake_bluez.ADAPTER_ADDRESS,
             "alias": ADAPTER_ALIAS},
            adapter(IDENTITIES, INQUIRY_TEST_NAME, RESOURCES))

    def test_the_address_is_normalized_to_the_form_the_tools_print(self):
        identities = dict(IDENTITIES)
        identities[ADAPTER_ROLE] = {"hci device": "hci1",
                                    "BD_ADDR": fake_bluez.ADAPTER_ADDRESS.lower()}
        self.assertEqual(fake_bluez.ADAPTER_ADDRESS,
                         adapter(identities, INQUIRY_TEST_NAME)["address"])


if __name__ == "__main__":
    unittest.main()
