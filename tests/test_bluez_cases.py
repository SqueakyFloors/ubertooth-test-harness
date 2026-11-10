"""The BlueZ-tool cases: the adapter resource, the commands, and the checks.

Everything here runs against the output samples in tests/fake_bluez.py and
against the real esp32peer.py driver talking to the ESP32 firmware fake. The
cases themselves have run on a bench with a USB adapter, and several of the
checks below carry the figures those runs gave; docs/tests.md records per
case what they settled and what they left open.
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
    BOARD_SETUP_S,
    FOLLOW_SETUP_S,
    INQUIRY_MIN_S,
    INQUIRY_S,
    NAME_READS_S,
    inquiry_found,
    inquiry_scan_cases,
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
INITIATOR_NAME = fake_bluez.INITIATOR_NAME
CONTEXT = {CAPTURE_DIR_KEY: "/run/captures/hci"}
# The same plus a recorded value for every board setting a cleanup restores,
# so that a restore step's command line resolves and can be read.
RESTORE_CONTEXT = {
    **CONTEXT,
    "original channel": "2441",
    "original channel-index": "2441",
    "original squelch": "-90",
    "original leds": "0/0/0",
    "original crc": "1",
    "original pa-level": "7",
}

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

    def test_the_plan_has_no_entry_in_the_release_suite(self):
        """The gate runs this plan as a stage of its own (`hci`, the
        comprehensive suite, in releasegate.py) rather than as entries in
        `release.yaml`, so neither test name belongs in that file. The two
        peer faults that once kept the plan out of the gate altogether
        (#310, #311) were fixed in #316 and re-counted at 0 of 92 pair
        sessions (#322)."""
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
        for suite, counts in (("hci", (2, 2)), ("hci-comprehensive", (2, 3))):
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

    def test_comprehensive_adds_no_case_to_the_scan(self):
        """`-s -x` is not exercised: it fetches its report over a connection
        to each device, and the acceptor is in the pair's link throughout, so
        the bench had it report no address at all (#88)."""
        self.assertEqual(["inquiry", "survey"],
                         list(scan_cases("comprehensive")))
        self.assertEqual(list(scan_cases()), list(scan_cases("comprehensive")))

    def test_no_case_runs_x(self):
        for case in scan_cases("comprehensive").values():
            with self.subTest(case.case_id):
                self.assertNotIn("-x", tool_argv(case))

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

    def test_the_capture_window_is_the_duration_and_may_not_end_early(self):
        argv = driver_argv(scan_cases()["inquiry"])
        # -t bounds the tool's own capture, so the driver does not interrupt
        # it; it only refuses a run that stopped early.
        self.assertNotIn("--interrupt-after", argv)
        self.assertEqual(str(25 + INQUIRY_MIN_S - 0.5),
                         argv[argv.index("--min-seconds") + 1])

    def test_the_floor_bounds_the_sweep_and_not_only_the_run(self):
        """-t's alarm is armed after the inquiry, so a floor of the window
        alone would be satisfied by the inquiry with no sweep at all."""
        with_s = driver_argv(scan_cases()["inquiry"])
        without = driver_argv(scan_cases()["survey"])
        self.assertEqual(str(25 + INQUIRY_MIN_S - 0.5),
                         with_s[with_s.index("--min-seconds") + 1])
        self.assertEqual("24.5", without[without.index("--min-seconds") + 1])
        # The inquiry runs its 8 units of 1.28 s however many devices answer.
        self.assertAlmostEqual(10.24, INQUIRY_MIN_S)

    def test_the_deadline_allows_for_the_inquiry_the_t_window_does_not_cover(self):
        """ubertooth-scan arms its -t alarm only after the BlueZ inquiry."""
        with_s = driver_argv(scan_cases()["inquiry"])
        without = driver_argv(scan_cases()["survey"])
        self.assertEqual(str(25 + INQUIRY_S + NAME_READS_S + BOARD_SETUP_S + 5.0),
                         with_s[with_s.index("--timeout") + 1])
        self.assertEqual(str(25 + NAME_READS_S + BOARD_SETUP_S + 5.0),
                         without[without.index("--timeout") + 1])
        self.assertGreater(float(with_s[with_s.index("--timeout") + 1]),
                           float(without[without.index("--timeout") + 1]))

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
                    ["adapter-free", "adapter-still-up", "reset-initiator",
                     "reset-acceptor", "cleanup-stop"], cleanup[:5])
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

    def test_the_setup_allowance_covers_the_teardown_and_the_board_work(self):
        """The link is dropped (10 s) and the board opened, checked, and set
        up, all before the receive loop."""
        from ubertooth_harness.bluez_cases import (
            BOARD_SETUP_S, HCI_READ_S, PAGE_S, TEARDOWN_S)
        self.assertEqual(PAGE_S + 2 * HCI_READ_S + TEARDOWN_S + BOARD_SETUP_S,
                         FOLLOW_SETUP_S)
        self.assertGreater(FOLLOW_SETUP_S, PAGE_S + 2 * HCI_READ_S)

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
        # Three reads and two waits, not three of each.
        self.assertAlmostEqual(3 * 1.0 + 2 * 0.2, AFH_READ_S)
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

    def test_the_pcap_records_must_belong_to_the_piconet(self):
        """Measured: all 258 records carried the piconet's LAP and that LAP
        and UAP as the reference address, so those are required. None passed
        a header HEC and 35 carried `transport 3` -- the access code is found
        and the header does not decode (fork #518) -- so the HEC floor is 0
        and the transport nibble is let be, and nothing else is."""
        case = follow_cases("comprehensive")["follow-target-pcap"]
        step = next(s for s in case.steps if s.step_id == "verify-records")
        argv = list(step.argv({CAPTURE_DIR_KEY: "/run/captures/hci"}))
        self.assertIn("--lap", argv)
        self.assertEqual(CENTRAL_LAP, argv[argv.index("--lap") + 1])
        self.assertEqual(CENTRAL_LAP, argv[argv.index("--reference-lap") + 1])
        self.assertEqual(CENTRAL_UAP, argv[argv.index("--reference-uap") + 1])
        self.assertEqual("0", argv[argv.index("--min-hec-valid") + 1])
        self.assertIn("--any-transport", argv)
        self.assertEqual(str(MIN_PACKETS), argv[argv.index("--min-packets") + 1])

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
        """Against the step's own patterns, not the fixture alone: a step
        whose patterns changed would otherwise keep passing."""
        step = steps(scan_cases()["inquiry"])["adapter-up"]
        text = fake_bluez.hciconfig(up=False)
        self.assertRegex(text, re.compile(r"(?m)^\s+DOWN\b"))
        self.assertFalse(
            all(re.search(pattern, text) for pattern in step.output_patterns),
            "an adapter that is down satisfied every pattern of the read")

    def test_an_adapter_at_another_address_fails_the_read(self):
        """The hciN number is attach order; the address is the adapter."""
        step = steps(scan_cases()["inquiry"])["adapter-up"]
        address = next(pattern for pattern in step.output_patterns
                       if "BD Address" in pattern)
        self.assertNotRegex(
            fake_bluez.hciconfig(address="00:11:22:33:44:55"),
            re.compile(address))

    def test_a_free_adapter_passes_the_read(self):
        case = follow_cases()["follow-target"]
        step = next(s for s in case.cleanup if s.step_id == "adapter-free")
        self.assertEqual(("hcitool", "-i", fake_bluez.ADAPTER, "con"),
                         step.resolve_argv({}))
        self.patterns(step, fake_bluez.hcitool_con())

    def test_the_cleanup_also_rereads_the_adapter_itself(self):
        """hcitool con lists links only for a controller that is up, so a
        free read alone would pass for an adapter that went away."""
        case = follow_cases()["follow-target"]
        step = next(s for s in case.cleanup if s.step_id == "adapter-still-up")
        self.assertEqual(("hciconfig", fake_bluez.ADAPTER),
                         step.resolve_argv({}))
        self.patterns(step, fake_bluez.hciconfig(up=True))
        self.assertNotRegex(fake_bluez.hciconfig(up=False),
                            re.compile(r"(?m)^\s+UP\b"))

    def test_an_established_link_on_the_adapter_fails_the_read(self):
        """Any address, not the peers' alone: the scan pages the 00:00:
        address it assembles, and a link to a peer cannot survive the
        driver's own reset of both boards."""
        case = follow_cases()["follow-target"]
        for step_id in ("adapter-idle", "adapter-free"):
            step = next(s for s in list(case.steps) + list(case.cleanup)
                        if s.step_id == step_id)
            for address in (ACCEPTOR_ADDRESS, fake_bluez.OTHER_ADDRESS,
                            "00:00:0D:E9:C5:BA"):
                with self.subTest(step_id=step_id, address=address):
                    text = fake_bluez.hcitool_con(address)
                    self.assertTrue(
                        any(re.search(pattern, text)
                            for pattern in step.forbidden_patterns),
                        f"a link to {address} was not caught")

    def test_a_page_still_in_flight_is_not_held_against_the_run(self):
        """The scan's own name reads leave pending links the kernel times
        out; the bench showed three of them, gone shortly after."""
        case = follow_cases()["follow-target"]
        step = next(s for s in case.cleanup if s.step_id == "adapter-free")
        pending = fake_bluez.hcitool_con(ACCEPTOR_ADDRESS,
                                         "00:00:0D:E9:C5:BA", state=2)
        self.patterns(step, pending)


class OutputCheckTests(unittest.TestCase):
    """The checks over each tool's own standard output."""

    def test_the_inquiry_check_wants_both_peers_and_one_name(self):
        check = inquiry_found({ACCEPTOR_ADDRESS: ACCEPTOR_NAME,
                               INITIATOR_ADDRESS: INITIATOR_NAME})
        found = fake_bluez.scan_inquiry(
            (fake_bluez.OTHER_ADDRESS, "Someone's speaker"),
            (ACCEPTOR_ADDRESS, ACCEPTOR_NAME),
            (INITIATOR_ADDRESS, INITIATOR_NAME))
        self.assertEqual("", check(found))
        self.assertIn("and not " + ACCEPTOR_ADDRESS + ", " + INITIATOR_ADDRESS,
                      check(fake_bluez.scan_inquiry()))
        # One peer listed and the other not is the same failure.
        self.assertIn("and not " + INITIATOR_ADDRESS, check(
            fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, ACCEPTOR_NAME))))

    def test_one_peer_name_is_enough_and_none_is_not(self):
        """One name read is the floor; see inquiry_found()."""
        check = inquiry_found({ACCEPTOR_ADDRESS: ACCEPTOR_NAME,
                               INITIATOR_ADDRESS: INITIATOR_NAME})
        # One peer's read went unanswered and the other's worked, which is
        # what two of the bench's nineteen runs looked like.
        self.assertEqual("", check(fake_bluez.scan_inquiry(
            (fake_bluez.OTHER_ADDRESS, "[unknown]"),
            (ACCEPTOR_ADDRESS, "[unknown]"),
            (INITIATOR_ADDRESS, INITIATOR_NAME))))
        self.assertIn("no peer came back under its own name", check(
            fake_bluez.scan_inquiry((ACCEPTOR_ADDRESS, "[unknown]"),
                                    (INITIATOR_ADDRESS, "[unknown]"))))

    def test_a_name_on_another_peers_line_fails(self):
        """What the bench printed once: a neighbor's name against a peer's
        address, while the neighbor's own read had just failed. That passed
        the one-name floor, because the other peer was under its own name
        (fork #1175)."""
        check = inquiry_found({ACCEPTOR_ADDRESS: ACCEPTOR_NAME,
                               INITIATOR_ADDRESS: INITIATOR_NAME})
        problem = check(fake_bluez.scan_inquiry(
            (fake_bluez.OTHER_ADDRESS, "[unknown]"),
            (ACCEPTOR_ADDRESS, "[TV] Someone's television"),
            (INITIATOR_ADDRESS, INITIATOR_NAME)))
        self.assertIn("came back as", problem)
        self.assertIn(ACCEPTOR_ADDRESS, problem)
        self.assertIn("came from somewhere else", problem)
        # Both peers swapped onto each other's lines is the same fault, and
        # the floor alone would miss it too.
        both = check(fake_bluez.scan_inquiry(
            (ACCEPTOR_ADDRESS, INITIATOR_NAME),
            (INITIATOR_ADDRESS, ACCEPTOR_NAME)))
        self.assertIn(ACCEPTOR_ADDRESS, both)
        self.assertIn(INITIATOR_ADDRESS, both)
        self.assertIn("no peer came back under its own name", both)

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

    def test_the_maps_check_reads_the_map_that_follows_this_piconet(self):
        """The scan prints one map per result, so a map line elsewhere in
        the output belongs to another piconet."""
        check = scan_results_list(CENTRAL_LAP, with_map=True)
        self.assertEqual("", check(fake_bluez.scan_survey()))
        # This piconet listed with no map of its own, another with one.
        self.assertIn("has no 'AFH map:' line after it", check(
            f"??:??:{CENTRAL_UAP.upper()}:E9:C5:BA\n"
            + fake_bluez.scan_survey(lap="2a1b2a", uap="41")))
        self.assertIn("not LAP " + CENTRAL_LAP,
                      check(fake_bluez.scan_survey(lap="2a1b2a", uap="41")))

    def test_the_follow_check_wants_the_centrals_packets_and_nothing_else(self):
        check = steps(follow_cases()["follow-target"])["pair-and-run"].output_check
        self.assertEqual("", check(fake_bluez.packet_lines(count=MIN_PACKETS)))
        self.assertIn("expected at least", check(fake_bluez.packet_lines(count=1)))
        self.assertIn("not " + CENTRAL_LAP, check(
            fake_bluez.packet_lines(count=MIN_PACKETS)
            + fake_bluez.packet_lines(lap="2a1b2a", count=1)))

    def test_the_afh_case_pins_the_disabled_answer_the_bench_gives(self):
        """The peer reports AFH off on the link the page made, so the tool
        says so and follows without it: measured, so that is what is pinned,
        and a map it could not have had is forbidden."""
        step = steps(follow_cases()["follow-target-afh"])["pair-and-run"]
        self.assertIn(
            r"(?m)^the remote device is not using AFH; following without it$",
            step.output_patterns)
        self.assertEqual(
            (r"(?m)^AFH map read: ", r"(?m)^AFH map: 0x", r"(?m)^error: "),
            step.forbidden_patterns)
        for line in ("AFH map read: 79 of 79 channels in use",
                     "AFH map: 0x" + "ff" * 9 + "7f",
                     "error: AFH map read failed: Input/output error"):
            with self.subTest(line):
                self.assertTrue(
                    any(re.search(pattern, line)
                        for pattern in step.forbidden_patterns), line)
        # It still has to report the piconet.
        check = step.output_check
        self.assertEqual("", check(fake_bluez.packet_lines(count=MIN_PACKETS)))
        self.assertIn("expected at least", check(""))


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

    def test_the_two_follow_cases_pin_the_two_lines_the_tool_prints(self):
        """Without -a the tool says it is not using AFH; with -a it reports
        that the remote is not, which is what this bench answers."""
        plain = fake_bluez.follow_progress()
        with_afh = fake_bluez.follow_progress(afh=True)
        self.assertIn("Not use AFH", plain)
        self.assertNotIn("Not use AFH", with_afh)
        self.matched(follow_cases()["follow-target-afh"], with_afh,
                     skip=self.DRIVER)
        # Neither case may see a map: -a was told there is none.
        for text in (plain, with_afh):
            for pattern in steps(
                follow_cases()["follow-target-afh"]
            )["pair-and-run"].forbidden_patterns:
                with self.subTest(pattern=pattern):
                    self.assertIsNone(re.search(pattern, text))

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

    def test_every_case_pins_a_zero_exit(self):
        for cases in (scan_cases("comprehensive"), follow_cases("comprehensive")):
            for case_id, case in cases.items():
                with self.subTest(case_id):
                    step = steps(case)["pair-and-run"]
                    self.assertEqual((0,), step.expected_returncodes)
                    exits = [pattern for pattern in step.output_patterns
                             if "command exit:" in pattern]
                    self.assertEqual(
                        [r"(?m)^command exit: 0 after \d+\.\d s$"], exits)


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


class AdapterSelectionTests(unittest.TestCase):
    """Two adapters on one host are not interchangeable.

    A bench with two of them is the case this guards, and what each program
    does with no device named differs, so no single output check catches it:

    - the two fork tools take the host's first available controller, and
      would drive the wrong radio with nothing in their output saying which;
    - ``hciconfig`` with no device lists every controller and ``hcitool con``
      with no ``-i`` reports every up controller's links, so the adapter
      steps' own patterns would still match -- the bound adapter's block is
      in that output -- while the links refused belonged to any controller.

    That is why this checks the commands rather than their output. Each case
    is built against a *second* adapter, and nothing it runs may name the
    first one or leave the choice out. The ``hciN`` name is only attach
    order, so the address is checked with it: naming ``hci1`` while reading
    ``hci0``'s address would be the same mistake one level down.
    """

    SECOND_DEVICE = "hci1"
    SECOND_ADDRESS = "AC:A7:F1:5B:C3:C9"
    # Every command either tool or BlueZ tool runs against a controller, and
    # the option each one takes to name it. `hciconfig` takes the device as
    # its first argument, with no option at all.
    SELECTORS = {"hciconfig": None, "hcitool": "-i",
                 "ubertooth-scan": "-b", "ubertooth-follow": "-b"}

    def identities(self):
        identities = dict(IDENTITIES)
        identities[ADAPTER_ROLE] = {"hci device": self.SECOND_DEVICE,
                                    "BD_ADDR": self.SECOND_ADDRESS}
        return identities

    def cases(self):
        identities = self.identities()
        for coverage in ("smoke", "comprehensive"):
            for build in (scan_cases, follow_cases):
                for case in build(coverage, identities).values():
                    yield coverage, case

    def commands(self, case):
        """Every step's argv, cleanup included, with the tool's own line.

        A restore step builds its command from what an earlier step read, so
        the context carries recorded values for all of them: the point here
        is which device each command names, and a step left out for want of
        a recorded value is a step not checked.
        """
        for step in case.steps + case.cleanup:
            argv = list(step.resolve_argv(RESTORE_CONTEXT))
            yield step, argv
            if "--" in argv:
                yield step, argv[argv.index("--") + 1:]

    def test_every_controller_command_names_the_adapter_it_was_given(self):
        for coverage, case in self.cases():
            for step, argv in self.commands(case):
                if not argv:
                    continue
                program = Path(argv[0]).name
                if program not in self.SELECTORS:
                    continue
                option = self.SELECTORS[program]
                with self.subTest(coverage=coverage, case=case.case_id,
                                  step=step.step_id, program=program):
                    if option is None:
                        self.assertEqual(self.SECOND_DEVICE, argv[1])
                        continue
                    self.assertIn(option, argv)
                    self.assertEqual(self.SECOND_DEVICE,
                                     argv[argv.index(option) + 1])

    def test_no_case_mentions_the_adapter_it_was_not_given(self):
        """Not in a command, a pattern, a step name, or the expected state.

        `fake_bluez.ADAPTER` is the first adapter's name, and a check written
        against it rather than against the bound one would pass on the bench
        that has only that adapter and fail on the one that has both.
        """
        default = fake_bluez.ADAPTER
        self.assertNotEqual(default, self.SECOND_DEVICE)
        for coverage, case in self.cases():
            text = [case.name, case.expected_state]
            for step, argv in self.commands(case):
                text.extend(argv)
            for step in case.steps + case.cleanup:
                text.append(step.name)
                text.extend(step.output_patterns)
                text.extend(step.forbidden_patterns)
            for piece in text:
                with self.subTest(coverage=coverage, case=case.case_id,
                                  piece=piece):
                    self.assertNotRegex(str(piece), rf"\b{default}\b")

    def test_the_adapter_reads_check_the_address_of_the_one_it_was_given(self):
        """The identity half: the right name, and the right radio under it."""
        wanted = re.escape(self.SECOND_ADDRESS)
        for coverage, case in self.cases():
            reads = [step for step in case.steps + case.cleanup
                     if step.step_id in ("adapter-up", "adapter-still-up")]
            self.assertEqual(2, len(reads), case.case_id)
            for step in reads:
                with self.subTest(coverage=coverage, case=case.case_id,
                                  step=step.step_id):
                    self.assertTrue(
                        any(wanted in pattern
                            for pattern in step.output_patterns),
                        f"{step.step_id} does not require "
                        f"{self.SECOND_ADDRESS}: {step.output_patterns}")
                    self.assertNotIn(
                        fake_bluez.ADAPTER_ADDRESS,
                        "".join(step.output_patterns))

    def test_one_radio_registered_under_two_names_is_refused(self):
        """Two aliases, two `hciN` names, one address: one radio twice.

        The reverse case -- two adapters sharing an `hci device` -- is
        refused above. This one is what a bench produces by registering the
        same adapter again after it came back under another name: the lease
        keys are built from the address, so the two aliases would share one
        lease while the cases drove two devices.
        """
        second = f"""
  BT_Linux1:
    type: {ADAPTER_ROLE}
    attached to: Host_Linux
    identity:
      hci device: {self.SECOND_DEVICE}
      BD_ADDR: "{fake_bluez.ADAPTER_ADDRESS}"
"""
        with self.assertRaisesRegex(RegistryError, "both registered at"):
            registry_with(ADAPTER_ONLY + second)

    def test_two_adapters_with_their_own_addresses_are_accepted(self):
        second = f"""
  BT_Linux1:
    type: {ADAPTER_ROLE}
    attached to: Host_Linux
    identity:
      hci device: {self.SECOND_DEVICE}
      BD_ADDR: "{self.SECOND_ADDRESS}"
"""
        registry = registry_with(ADAPTER_ONLY + second)
        self.assertEqual(
            self.SECOND_ADDRESS,
            registry.resources["BT_Linux1"].identity["BD_ADDR"])

    def test_no_plan_binds_more_than_one_adapter_to_a_test(self):
        """The bench keeps one adapter; a second is for a second runner.

        A plan row binds one resource per role, so this is a property of the
        role rather than of the row -- which is the point: there is no
        `Bluetooth adapter 2` role to bind, so nothing can come to require
        two at once.
        """
        for path in sorted((ROOT / "testplans").glob("*.yaml")):
            plan = load_plan(path)
            for suite, entries in plan.suites.items():
                for entry in entries:
                    bound = [key for key in entry.arguments
                             if str(key).startswith(ADAPTER_ROLE)]
                    with self.subTest(plan=path.name, suite=suite,
                                      test=entry.test_name):
                        self.assertLessEqual(len(bound), 1, bound)


if __name__ == "__main__":
    unittest.main()
