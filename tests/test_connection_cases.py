from pathlib import Path
import tempfile
import unittest

from ubertooth_harness import conncapture, leases, nrfdk
from ubertooth_harness.advertiser_cases import FIRMWARE_LABEL
from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.connection_cases import (
    ADV_CHANNEL,
    CYCLE_MS,
    WRITE_BYTES,
    known_connection_cases,
)
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from ubertooth_harness.stateful import CAPTURE_DIR_KEY, StateCaseError, artifact_key

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


class CaseTests(unittest.TestCase):
    def test_smoke_and_comprehensive_cases(self):
        self.assertEqual(
            ["pair-ready", "follow-pcapng"],
            [case.case_id for case in known_connection_cases(arguments(), {}, IDENTITIES)])
        self.assertEqual(
            ["pair-ready", "follow-pcapng", "follow-pcap",
             "pcapng-connection-option-libbtbb-21"],
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
             "--period", "100", "--cycle", str(CYCLE_MS), "--autoupdate", "200"],
            list(steps["central-start"].resolve_argv(context)[2:]))
        self.assertEqual(
            ("ubertooth-btle", "-U", SERIAL, "-f", f"-A{ADV_CHANNEL}", "-r",
             "/run/captures/x/capture.pcapng"),
            steps["capture"].resolve_argv(context))
        self.assertTrue(steps["capture"].continuous)
        self.assertEqual(8.0, steps["capture"].timeout_s)
        self.assertEqual(
            ["/run/captures/x/capture.pcapng", "--central", CENTRAL,
             "--peripheral", PERIPHERAL,
             # 50 ms is 40 units of 1.25 ms.
             "--interval-units", "40", "--latency", "0",
             "--timeout-units", str(conncapture.DEFAULT_TIMEOUT_UNITS),
             "--min-data-packets", "5", "--min-data-length", "28",
             "--long-packets", "1"],
            list(steps["check-capture"].resolve_argv(context)[2:]))

    def test_the_central_must_have_connected_written_and_updated(self):
        case = known_connection_cases(arguments(), {}, IDENTITIES)[1]
        check = {step.step_id: step for step in case.steps}["central-report"].output_check
        good = ("role: central\n"
                "traffic: writes 74 received 18300 bytes updates 3 connects 3\n")
        self.assertEqual("", check(good))
        for line, reason in (
            ("traffic: writes 0 received 0 bytes updates 1 connects 1", "wrote nothing"),
            ("traffic: writes 5 received 5 bytes updates 0 connects 1", "never accepted"),
            ("traffic: writes 0 received 0 bytes updates 0 connects 0", "no connection"),
            ("role: central", "no traffic line"),
        ):
            self.assertIn(reason, check(line))

    def test_both_tests_hold_the_host_lock_because_they_flash_a_dk(self):
        catalog = builtin_catalog()
        for name in ("Capture known BLE advertiser", "Capture known BLE connection"):
            self.assertTrue(catalog.get(name).writes_firmware, name)

    def test_the_libbtbb_case_asks_for_the_option_check(self):
        cases = {case.case_id: case for case in known_connection_cases(
            arguments("comprehensive"), {}, IDENTITIES)}
        case = cases["pcapng-connection-option-libbtbb-21"]
        steps = {step.step_id: step for step in case.steps}
        argv = steps["check-capture"].resolve_argv({CAPTURE_DIR_KEY: "/c"})
        self.assertEqual("--connection-info-option", argv[-1])
        self.assertIn("libbtbb#21", case.name)

    def test_the_pcap_case_uses_the_pcap_option(self):
        cases = {case.case_id: case for case in known_connection_cases(
            arguments("comprehensive"), {}, IDENTITIES)}
        steps = {step.step_id: step for step in cases["follow-pcap"].steps}
        self.assertEqual(("ubertooth-btle", "-U", SERIAL, "-f",
                          f"-A{ADV_CHANNEL}", "-q", "/c/capture.pcap"),
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


class RegistryTests(unittest.TestCase):
    def test_role_defaults_pick_a_dk_per_role(self):
        registry = registry_with(REGISTRY)
        invocations = PlanCompiler(builtin_catalog(), registry).compile(
            load_plan(ROOT / "testplans" / "ble.yaml"), "connection")
        self.assertEqual(1, len(invocations))
        self.assertEqual({"Ubertooth": "UT_Linux0", "central": "DK_Linux1",
                          "peripheral": "DK_Linux2"},
                         dict(invocations[0].resources))

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
