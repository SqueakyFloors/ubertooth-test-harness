from pathlib import Path
import tempfile
import unittest

from ubertooth_harness import conncapture, leases, nrfdk
from ubertooth_harness.advertiser_cases import FIRMWARE_LABEL
from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.connection_cases import (
    ADV_CHANNEL,
    AFTER_UPDATE_MS,
    CAPTURE_ATTEMPTS,
    CYCLE_MS,
    FOLLOW_END_TEST_NAME,
    MAX_QUIET_MS,
    MIN_CONNECTION_PACKETS,
    MIN_CONNECTIONS,
    MIN_CYCLES,
    WRITE_PERIOD_MS,
    WRITE_BYTES,
    follow_end_cases,
    known_connection_cases,
)
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from ubertooth_harness.stateful import (
    ATTEMPT_KEY,
    CAPTURE_DIR_KEY,
    StateCaseError,
    artifact_key,
)

ROOT = Path(__file__).resolve().parents[1]
SERIAL = "0123456789abcdef0123456789abcdef"
CENTRAL = "C0:7D:CD:95:52:86"
PERIPHERAL = "C1:92:EC:E7:71:BC"
IDENTITIES = {
    "Ubertooth": {"serial": SERIAL},
    "central": {"jlink serial": "001050000001", "BLE address": CENTRAL},
    "peripheral": {"jlink serial": "001050000002", "BLE address": PERIPHERAL},
}
REGISTRY = f"""
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [test target]
    defaults:
      Ubertooth: UT_Linux0
      central: DK_Linux1
      peripheral: DK_Linux2
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {{serial: {SERIAL}}}
  DK_Linux1:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [central]
    identity:
      jlink serial: "001050000001"
      BLE address: "{CENTRAL}"
  DK_Linux2:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [peripheral]
    identity:
      jlink serial: "001050000002"
      BLE address: "{PERIPHERAL}"
"""


def arguments(coverage="smoke", **extra):
    raw = {"coverage": coverage, "duration": "8s", **extra}
    return builtin_catalog().get("Capture known BLE connection").normalize_arguments(raw)


def registry_with(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "hardware.yaml"
        path.write_text(text)
        return FileResourceRegistry.load(path)


class AliasTests(unittest.TestCase):
    """Each DK's own alias marks its shell lines, so the pair is told apart."""

    def test_each_dk_command_carries_that_board_s_alias(self):
        driver = str(Path(nrfdk.__file__).resolve())
        resources = {"Ubertooth": "UT_Linux0", "central": "DK_Linux1",
                     "peripheral": "DK_Linux2"}
        case = known_connection_cases(arguments(), resources, IDENTITIES)[0]

        aliases = {}
        for step in case.steps + case.cleanup:
            argv = list(step.resolve_argv({}))
            if argv[1:2] != [driver]:
                continue
            self.assertEqual("--alias", argv[5], argv)
            aliases[argv[4]] = argv[6]
        self.assertEqual({"1050000001": "DK_Linux1", "1050000002": "DK_Linux2"},
                         aliases)


class CaseTests(unittest.TestCase):
    def test_smoke_and_comprehensive_cases(self):
        self.assertEqual(
            ["pair-ready", "follow-pcapng"],
            [case.case_id for case in known_connection_cases(arguments(), {}, IDENTITIES)])
        self.assertEqual(
            ["pair-ready", "follow-pcapng", "follow-pcap",
             "pcapng-connection-option"],
            [case.case_id for case in known_connection_cases(
                arguments("comprehensive"), {}, IDENTITIES)])

    def test_ready_case_flashes_each_role_only_with_the_artifact(self):
        case = known_connection_cases(arguments(), {}, IDENTITIES)[0]
        self.assertEqual((FIRMWARE_LABEL,), case.optional_artifacts)
        steps = {step.step_id: step for step in case.steps}
        self.assertEqual(
            ["ensure", "--jlink", "1050000002", "--address", PERIPHERAL,
             "--role", "peripheral"],
            list(steps["ensure-peripheral"].resolve_argv({})[2:]))
        argv = steps["ensure-central"].resolve_argv(
            {artifact_key(FIRMWARE_LABEL): "/images/utdk.hex"})
        self.assertEqual(["--role", "central", "--hex", "/images/utdk.hex"],
                         list(argv[-4:]))
        # Both DKs are left idle, the peripheral first.
        self.assertEqual(["peripheral-idle", "central-idle"],
                         [step.step_id for step in case.cleanup])

    def test_capture_case_commands(self):
        case = known_connection_cases(arguments(interval=50, update=200), {},
                                      IDENTITIES)[1]
        steps = {step.step_id: step for step in case.steps}
        context = {CAPTURE_DIR_KEY: "/run/captures/x"}
        self.assertEqual(
            ["peripheral", "--jlink", "1050000002", "--dle", "on",
             "--chmap", str(ADV_CHANNEL)],
            list(steps["peripheral-start"].resolve_argv(context)[2:]))
        self.assertEqual(
            ["central", "--jlink", "1050000001", "--peer", PERIPHERAL,
             "--interval", "50", "--dle", "on", "--payload", "244",
             "--period", str(WRITE_PERIOD_MS), "--cycle", str(CYCLE_MS),
             "--autoupdate", "200"],
            list(steps["central-start"].resolve_argv(context)[2:]))
        self.assertEqual(
            ("ubertooth-btle", "-U", SERIAL, "-f", f"-A{ADV_CHANNEL}", "-r",
             "/run/captures/x/capture-1.pcapng"),
            steps["capture"].resolve_argv(context))
        self.assertTrue(steps["capture"].continuous)
        self.assertEqual(8.0, steps["capture"].timeout_s)
        self.assertEqual(
            ["/run/captures/x/capture-1.pcapng", "--central", CENTRAL,
             "--peripheral", PERIPHERAL,
             # 50 ms is 40 units of 1.25 ms.
             "--interval-units", "40", "--latency", "0",
             "--timeout-units", str(conncapture.DEFAULT_TIMEOUT_UNITS),
             "--min-data-packets", "5", "--min-valid-packets", "1",
             "--min-data-length", "28",
             "--long-packets", "1"],
            list(steps["check-capture"].resolve_argv(context)[2:]))

    def test_the_central_must_have_connected_written_and_updated(self):
        case = known_connection_cases(arguments(), {}, IDENTITIES)[1]
        check = {step.step_id: step for step in case.steps}["central-report"].output_check
        traffic = ("traffic: writes 74 received 18300 bytes updates 3 connects 3 "
                   "disconnects 2\n")
        update = f"update: asked {arguments()['update']} ms (48 units)\n"
        self.assertEqual("", check("role: central\n" + traffic + update))
        for text, reason in (
            ("traffic: writes 0 received 0 bytes updates 1 connects 1 "
             "disconnects 0", "wrote nothing"),
            ("traffic: writes 5 received 5 bytes updates 0 connects 1 "
             "disconnects 0", "never accepted"),
            ("traffic: writes 0 received 0 bytes updates 0 connects 0 "
             "disconnects 0", "no connection"),
            ("role: central", "no traffic line"),
            (traffic, "no update line"),
            # An update was accepted, but not the one that was asked for.
            (traffic + "update: asked 100 ms (80 units)\n",
             "accepted update was 100 ms, not the 60 ms"),
        ):
            self.assertIn(reason, check(text))

    def test_both_tests_hold_the_host_lock_because_they_flash_a_dk(self):
        catalog = builtin_catalog()
        for name in ("Capture known BLE advertiser", "Capture known BLE connection"):
            self.assertTrue(catalog.get(name).writes_firmware, name)

    def test_the_connection_option_case_asks_for_that_check(self):
        cases = {case.case_id: case for case in known_connection_cases(
            arguments("comprehensive"), {}, IDENTITIES)}
        case = cases["pcapng-connection-option"]
        steps = {step.step_id: step for step in case.steps}
        argv = steps["check-capture"].resolve_argv({CAPTURE_DIR_KEY: "/c"})
        self.assertEqual("--check-connection-info", argv[-1])
        self.assertIn("connection information", case.name)

    def test_each_capture_case_takes_the_capture_again(self):
        cases = known_connection_cases(arguments("comprehensive"), {}, IDENTITIES)
        for case in cases[1:]:
            with self.subTest(case.case_id):
                self.assertEqual(
                    ("capture", "stop", "central-report", "check-capture"),
                    case.retry_steps)
                self.assertEqual(CAPTURE_ATTEMPTS, case.attempts)
                # Named steps that really do run one after the other.
                self.assertIsNotNone(case.retry_window())
                steps = {step.step_id: step for step in case.steps}
                # Each attempt writes, and checks, a capture of its own.
                for attempt in ("1", "2"):
                    context = {CAPTURE_DIR_KEY: "/c", ATTEMPT_KEY: attempt}
                    capture = steps["capture"].resolve_argv(context)[-1]
                    self.assertRegex(
                        capture, rf"^/c/capture-{attempt}\.(pcap|pcapng)$")
                    self.assertEqual(
                        capture, steps["check-capture"].resolve_argv(context)[2])

    def test_the_pcap_case_uses_the_pcap_option(self):
        cases = {case.case_id: case for case in known_connection_cases(
            arguments("comprehensive"), {}, IDENTITIES)}
        steps = {step.step_id: step for step in cases["follow-pcap"].steps}
        self.assertEqual(("ubertooth-btle", "-U", SERIAL, "-f",
                          f"-A{ADV_CHANNEL}", "-q", "/c/capture-1.pcap"),
                         steps["capture"].resolve_argv({CAPTURE_DIR_KEY: "/c"}))

    def test_registered_ports_are_passed(self):
        identities = {
            **IDENTITIES,
            "central": {**IDENTITIES["central"], "serial device": "/dev/ttyACM4"},
        }
        step = known_connection_cases(arguments(), {}, identities)[0].steps[0]
        self.assertEqual(["--port", "/dev/ttyACM4"], list(step.resolve_argv({})[-2:]))

    def test_the_pair_identities_are_checked(self):
        for role, identity, reason in (
            ("central", {}, "requires a central"),
            ("peripheral", {}, "requires a peripheral"),
            ("central", {"jlink serial": "x", "BLE address": CENTRAL}, "J-Link serial"),
            ("central", {"jlink serial": "1050000001", "BLE address": CENTRAL},
             None),
            ("peripheral", {"jlink serial": "1050000003",
                            "BLE address": "43:11:22:33:44:55"}, "not a static random"),
        ):
            identities = {**IDENTITIES, role: identity}
            if reason is None:
                known_connection_cases(arguments(), {}, identities)
                continue
            with self.assertRaisesRegex(StateCaseError, reason):
                known_connection_cases(arguments(), {}, identities)

    def test_one_dk_cannot_be_both_ends(self):
        identities = {**IDENTITIES, "peripheral": IDENTITIES["central"]}
        with self.assertRaisesRegex(StateCaseError, "needs two DKs"):
            known_connection_cases(arguments(), {}, identities)

    def test_arguments(self):
        self.assertEqual((30, 60), (arguments()["interval"], arguments()["update"]))
        for name, value in (("interval", 7), ("interval", 4001), ("update", 0)):
            with self.assertRaises(CatalogError):
                arguments(**{name: value})

    def test_the_duration_must_span_one_reconnection(self):
        cycle = CYCLE_MS // 1000
        arguments(duration=f"{cycle + 1}s")
        for seconds in (cycle, 2 * cycle, 1):
            with self.assertRaisesRegex(CatalogError, "one of the central's"):
                arguments(duration=f"{seconds}s")


def follow_end_arguments(**extra):
    raw = {"duration": "18s", **extra}
    return builtin_catalog().get(FOLLOW_END_TEST_NAME).normalize_arguments(raw)


class FollowEndTests(unittest.TestCase):
    """Follow a BLE connection across its end: the multi-cycle capture."""

    def cases(self, **extra):
        return follow_end_cases(follow_end_arguments(**extra), {}, IDENTITIES)

    def test_the_pair_is_made_ready_and_then_one_capture_runs(self):
        self.assertEqual(["pair-ready", "follow-across-end"],
                         [case.case_id for case in self.cases()])

    def test_the_capture_spans_the_whole_duration_and_cycles_the_link(self):
        steps = {step.step_id: step for step in self.cases()[1].steps}
        context = {CAPTURE_DIR_KEY: "/c"}
        self.assertEqual(
            ("ubertooth-btle", "-U", SERIAL, "-f", f"-A{ADV_CHANNEL}", "-r",
             "/c/capture-1.pcapng"),
            steps["capture"].resolve_argv(context))
        self.assertEqual(18.0, steps["capture"].timeout_s)
        self.assertTrue(steps["capture"].continuous)
        self.assertIn("--cycle", steps["central-start"].resolve_argv(context))
        self.assertIn(str(CYCLE_MS), steps["central-start"].resolve_argv(context))

    def test_the_capture_check_asks_for_the_cycle_requirements(self):
        steps = {step.step_id: step for step in self.cases()[1].steps}
        argv = list(steps["check-capture"].resolve_argv({CAPTURE_DIR_KEY: "/c"}))
        for option, value in (("--min-connections", str(MIN_CONNECTIONS)),
                              ("--min-connection-packets",
                               str(MIN_CONNECTION_PACKETS)),
                              ("--after-update-ms", str(AFTER_UPDATE_MS)),
                              # The window the quiet check is measured
                              # against is the one the capture step runs for.
                              ("--capture-ms", "18000"),
                              ("--max-quiet-ms", str(MAX_QUIET_MS))):
            self.assertEqual(value, argv[argv.index(option) + 1])
        # A capture may hold no connection the file does not also announce.
        self.assertIn("--require-announced", argv)

    def test_the_central_must_have_cycled_the_link(self):
        """A window the central spent on one connection is not this test."""
        check = {step.step_id: step for step in self.cases()[1].steps
                 }["central-report"].output_check
        update = f"update: asked {follow_end_arguments()['update']} ms (48 units)\n"
        good = ("traffic: writes 300 received 70000 bytes updates 4 connects 4 "
                "disconnects 3\n")
        self.assertEqual("", check("role: central\n" + good + update))
        for traffic, reason in (
            # Three cycles' worth of writes, but the link never went down.
            ("traffic: writes 300 received 70000 bytes updates 1 connects 1 "
             "disconnects 0\n",
             "made 1 connection(s), fewer than the 3 the capture window spans"),
            # It connected enough times but reports no drops.
            ("traffic: writes 300 received 70000 bytes updates 4 connects 4 "
             "disconnects 1\n", "dropped 1 connection(s), fewer than the 2"),
        ):
            self.assertIn(reason, check("role: central\n" + traffic + update))

    def test_the_board_is_read_back_after_the_capture_and_not_retried(self):
        case = self.cases()[1]
        steps = {step.step_id: step for step in case.steps}
        # Reading the board back is outside the retry window: it has to see
        # the board as the last capture left it.
        self.assertEqual(("capture", "stop", "central-report", "check-capture"),
                         case.retry_steps)
        self.assertEqual(CAPTURE_ATTEMPTS, case.attempts)
        self.assertIsNotNone(case.retry_window())
        order = [step.step_id for step in case.steps]
        self.assertLess(order.index("check-capture"), order.index("read-back"))
        self.assertLess(order.index("read-back"), order.index("idle-after-capture"))
        self.assertEqual(("ubertooth-util", "-U", SERIAL, "-s", "-C"),
                         steps["read-back"].resolve_argv({}))
        # The board must read as idle, with the self-reset count it started on.
        self.assertEqual((r"(?m)^Now in idle mode\b",),
                         steps["idle-after-capture"].output_patterns)
        observed = steps["idle-after-capture"].observe
        self.assertEqual("3", observed.expected_value({"watchdog resets": "3"}))

    def test_the_duration_must_span_several_reconnections(self):
        cycle = CYCLE_MS // 1000
        follow_end_arguments(duration=f"{MIN_CYCLES * cycle + 1}s")
        for seconds in (MIN_CYCLES * cycle, cycle, 1, 31):
            with self.assertRaisesRegex(CatalogError, "duration must be longer"):
                follow_end_arguments(duration=f"{seconds}s")

    def test_it_needs_the_same_pair_and_says_so_by_its_own_name(self):
        identities = {**IDENTITIES, "central": {}}
        with self.assertRaisesRegex(StateCaseError, FOLLOW_END_TEST_NAME):
            follow_end_cases(follow_end_arguments(), {}, identities)


class RegistryTests(unittest.TestCase):
    def test_role_defaults_pick_a_dk_per_role(self):
        registry = registry_with(REGISTRY)
        invocations = PlanCompiler(builtin_catalog(), registry).compile(
            load_plan(ROOT / "testplans" / "ble.yaml"), "connection")
        self.assertEqual(["Capture known BLE connection", FOLLOW_END_TEST_NAME],
                         [invocation.test_name for invocation in invocations])
        for invocation in invocations:
            self.assertEqual({"Ubertooth": "UT_Linux0", "central": "DK_Linux1",
                              "peripheral": "DK_Linux2"},
                             dict(invocation.resources))

    def test_a_role_default_must_declare_that_role(self):
        with self.assertRaisesRegex(RegistryError, "declares the central role"):
            registry_with(REGISTRY.replace("central: DK_Linux1", "central: DK_Linux2"))

    def test_both_dks_are_leased(self):
        registry = registry_with(REGISTRY)
        invocations = PlanCompiler(builtin_catalog(), registry).compile(
            load_plan(ROOT / "testplans" / "ble.yaml"), "connection")
        boards = leases.boards_for_invocations(invocations, registry)
        self.assertEqual(
            [("UT_Linux0", SERIAL), ("DK_Linux1", "nrf52840dk-1050000001"),
             ("DK_Linux2", "nrf52840dk-1050000002")],
            [(board.alias, board.key) for board in boards])

    def test_the_example_registry_runs_the_connection_suite(self):
        registry = FileResourceRegistry.load(ROOT / "hardware.example.yaml")
        plan = load_plan(ROOT / "testplans" / "ble.yaml")
        invocations = PlanCompiler(builtin_catalog(), registry).compile(plan, "connection")
        bound = dict(invocations[0].resources)
        # Other registered Ubertooths on the host are bound as guards.
        self.assertEqual("UT_Linux0", bound["Ubertooth"])
        self.assertEqual(("DK_Linux1", "DK_Linux2"),
                         (bound["central"], bound["peripheral"]))
        cases = builtin_catalog().get(invocations[0].test_name).command_cases(
            invocations[0].arguments, invocations[0].resources,
            invocations[0].resource_identities)
        self.assertEqual(2, len(cases))

    def test_the_firmware_version_matches_the_sources(self):
        header = (ROOT / "firmware" / "nrf52840dk" / "src" / "roles.h").read_text()
        self.assertIn(f'#define UTDK_FW_VERSION "{nrfdk.FIRMWARE_VERSION}"', header)

    def test_the_pinned_firmware_numbers_match_the_sources(self):
        """A firmware change to any of these must not quietly fail every run."""
        source = (ROOT / "firmware" / "nrf52840dk" / "src" / "connection.c").read_text()
        for define, value in (
            # conncapture expects this supervision timeout in a CONNECT_IND.
            ("TIMEOUT_UNITS", conncapture.DEFAULT_TIMEOUT_UNITS),
            # The connection interval conversion nrfdk.conn_units() repeats.
            ("UNITS_PER_MS", "4 / 5"),
            # Bytes per write, which fills a 251-byte link-layer packet.
            ("PAYLOAD_MAX", "(CONFIG_BT_L2CAP_TX_MTU - 3)"),
        ):
            self.assertIn(f"#define {define} {value}", source)
        config = (ROOT / "firmware" / "nrf52840dk" / "prj.conf").read_text()
        self.assertIn(f"CONFIG_BT_CTLR_DATA_LENGTH_MAX={nrfdk.DATA_LENGTH_MAX}", config)
        self.assertIn(f"CONFIG_BT_L2CAP_TX_MTU={WRITE_BYTES + 3}", config)


if __name__ == "__main__":
    unittest.main()
