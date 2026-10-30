from pathlib import Path
import tempfile
import unittest

from ubertooth_harness import leases, nrfdk
from ubertooth_harness.advertiser_cases import FIRMWARE_LABEL, known_advertiser_cases
from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.preflight import PreflightPolicy
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from tests.test_sequencer import run_contract
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    MissingContext,
    StateCaseError,
    artifact_key,
)

ROOT = Path(__file__).resolve().parents[1]
SERIAL = "0123456789abcdef0123456789abcdef"
IDENTITIES = {
    "Ubertooth": {"serial": SERIAL},
    "advertiser": {"jlink serial": "001050000001", "BLE address": "c3:11:22:33:44:55"},
}
REGISTRY = f"""
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [test target]
    defaults:
      Ubertooth: UT_Linux0
      nRF52840 DK: DK_Linux0
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {{serial: {SERIAL}}}
  DK_Linux0:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [advertiser]
    identity:
      jlink serial: "001050000001"
      BLE address: "C3:11:22:33:44:55"
"""


def arguments(coverage="smoke", **extra):
    catalog = builtin_catalog()
    raw = {"coverage": coverage, "duration": "5s", **extra}
    return catalog.get("Capture known BLE advertiser").normalize_arguments(raw)


def registry_with(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "hardware.yaml"
        path.write_text(text)
        return FileResourceRegistry.load(path)


class AliasTests(unittest.TestCase):
    """With the board's alias, nrfdk.py marks its shell lines with it (#186)."""

    def test_every_dk_command_carries_the_registered_alias(self):
        driver = str(Path(nrfdk.__file__).resolve())
        context = {CAPTURE_DIR_KEY: "/tmp", "counter before": "1", "counter after": "2"}
        cases = known_advertiser_cases(
            arguments(), {"Ubertooth": "UT_Linux0", "advertiser": "DK_Linux0"},
            IDENTITIES)

        found = 0
        for case in cases:
            for step in case.steps + case.cleanup:
                try:
                    argv = list(step.resolve_argv(context))
                except MissingContext:
                    continue  # an Ubertooth cleanup step, not a DK command
                if argv[1:2] != [driver]:
                    continue
                found += 1
                self.assertEqual(["--alias", "DK_Linux0"], argv[5:7], argv)
        self.assertGreater(found, 0)

    def test_without_an_alias_no_option_is_passed(self):
        step = known_advertiser_cases(arguments(), {}, IDENTITIES)[0].steps[0]

        self.assertNotIn("--alias", step.resolve_argv({}))


class CaseTests(unittest.TestCase):
    def test_smoke_cases(self):
        cases = known_advertiser_cases(arguments(), {}, IDENTITIES)
        self.assertEqual(
            ["advertiser-ready", "capture-37", "capture-38", "capture-39",
             "absent-37-adv-38", "follow-37-pcapng"],
            [case.case_id for case in cases])
        self.assertEqual((FIRMWARE_LABEL,), cases[0].optional_artifacts)
        self.assertEqual((), cases[0].artifacts)

    def test_comprehensive_adds_every_absent_pair_and_variants(self):
        ids = [case.case_id for case in known_advertiser_cases(
            arguments("comprehensive"), {}, IDENTITIES)]
        self.assertEqual(17, len(ids))
        for case_id in ("absent-39-adv-38", "follow-39-pcapng", "adv-ind-37",
                        "all-channels-38", "slow-interval-37", "no-payload-39"):
            self.assertIn(case_id, ids)

    def test_ensure_flashes_only_with_the_artifact(self):
        ensure = known_advertiser_cases(arguments(), {}, IDENTITIES)[0].steps[1]
        argv = ensure.resolve_argv({})
        self.assertEqual(["ensure", "--jlink", "1050000001", "--address",
                          "C3:11:22:33:44:55"], list(argv[2:]))
        argv = ensure.resolve_argv({artifact_key(FIRMWARE_LABEL): "/images/utdk.hex"})
        self.assertEqual(["--hex", "/images/utdk.hex"], list(argv[-2:]))

    def test_capture_case_commands(self):
        case = known_advertiser_cases(arguments(interval=250, payload="AABB"), {},
                                      IDENTITIES)[2]
        steps = {step.step_id: step for step in case.steps}
        context = {CAPTURE_DIR_KEY: "/run/captures/x", "counter before": "3",
                   "counter after": "12"}
        self.assertEqual(
            ["advertise", "--jlink", "1050000001", "--chmap", "38", "--interval", "250",
             "--payload", "aabb", "--type", "nonconn"],
            list(steps["advertise"].resolve_argv(context)[2:]))
        self.assertEqual(
            ("ubertooth-btle", "-U", SERIAL, "-n", "-A38", "-q",
             "/run/captures/x/capture.pcap"),
            steps["capture"].resolve_argv(context))
        self.assertTrue(steps["capture"].continuous)
        self.assertEqual(5.0, steps["capture"].timeout_s)
        check = list(steps["check-capture"].resolve_argv(context)[2:])
        self.assertEqual(["/run/captures/x/capture.pcap", "--address", "C3:11:22:33:44:55",
                          "--channel", "38", "--payload", "aabb", "--chmap", "38", "--type",
                          "nonconn", "--counter-min", "3", "--counter-max", "12",
                          # 5 s at 250 ms: 20 events, a fifth of them; at most
                          # (5 + 3) s worth plus one.
                          "--min-packets", "4", "--max-packets", "33"], check)
        self.assertEqual("advertiser-idle", case.cleanup[0].step_id)
        self.assertEqual("cleanup-stop", case.cleanup[1].step_id)

    def test_absent_and_follow_cases(self):
        cases = {case.case_id: case for case in known_advertiser_cases(arguments(), {},
                                                                       IDENTITIES)}
        context = {CAPTURE_DIR_KEY: "/c", "counter before": "1", "counter after": "2"}
        absent = {step.step_id: step for step in cases["absent-37-adv-38"].steps}
        self.assertIn("--chmap", absent["advertise"].resolve_argv(context))
        self.assertEqual("38", absent["advertise"].resolve_argv(context)[6])
        self.assertEqual(["/c/capture.pcap", "--address", "C3:11:22:33:44:55",
                          "--channel", "37", "--absent"],
                         list(absent["check-capture"].resolve_argv(context)[2:]))
        follow = {step.step_id: step for step in cases["follow-37-pcapng"].steps}
        self.assertEqual(("ubertooth-btle", "-U", SERIAL, "-f", "-A37", "-r",
                          "/c/capture.pcapng"), follow["capture"].resolve_argv(context))

    def test_registered_port_is_passed(self):
        identities = {**IDENTITIES, "advertiser": {**IDENTITIES["advertiser"],
                                                   "serial device": "/dev/cu.usbmodem1"}}
        step = known_advertiser_cases(arguments(), {}, identities)[0].steps[0]
        self.assertEqual(["--port", "/dev/cu.usbmodem1"], list(step.resolve_argv({})[-2:]))

    def test_advertiser_identity_is_checked(self):
        for identity, reason in (
            ({}, "requires an advertiser"),
            ({"jlink serial": "x", "BLE address": "C3:11:22:33:44:55"}, "J-Link serial"),
            ({"jlink serial": "1050000001", "BLE address": "43:11:22:33:44:55"},
             "not a static random"),
        ):
            with self.assertRaisesRegex(StateCaseError, reason):
                known_advertiser_cases(arguments(), {}, {**IDENTITIES, "advertiser": identity})

    def test_arguments(self):
        self.assertEqual("", arguments(payload="none")["payload"])
        self.assertEqual(100, arguments()["interval"])
        for name, value in (("interval", 19), ("interval", 10241), ("payload", "abc"),
                            ("payload", "00" * 13)):
            with self.assertRaises(CatalogError):
                arguments(**{name: value})


class RegistryTests(unittest.TestCase):
    def test_dk_registry_rules(self):
        registry_with(REGISTRY)
        for old, new, reason in (
            ('jlink serial: "001050000001"', "jlink serial: abc", "jlink serial"),
            ("roles: [advertiser]", "roles: [sniffer]", "roles must be"),
            ('      BLE address: "C3:11:22:33:44:55"\n', "", "requires a 'BLE address'"),
            ('BLE address: "C3:11:22:33:44:55"', 'BLE address: "C3-11"', "must look like"),
            ('jlink serial: "001050000001"', 'jlink serial: "001050000001"\n'
             "      probe serial: x", "unknown key"),
        ):
            with self.assertRaisesRegex(RegistryError, reason):
                registry_with(REGISTRY.replace(old, new))

    def test_example_registry_and_ble_plan_compile(self):
        registry = FileResourceRegistry.load(ROOT / "hardware.example.yaml")
        plan = load_plan(ROOT / "testplans" / "ble.yaml")
        for suite, count in (("ble", 6), ("ble-comprehensive", 17)):
            invocation = PlanCompiler(builtin_catalog(), registry).compile(plan, suite)[0]
            self.assertEqual("DK_Linux0", invocation.resources["advertiser"])
            cases = builtin_catalog().get(invocation.test_name).command_cases(
                invocation.arguments, invocation.resources, invocation.resource_identities)
            self.assertEqual(count, len(cases))

    def test_dk_is_leased_by_jlink_serial(self):
        registry = registry_with(REGISTRY)
        plan_text = (ROOT / "testplans" / "ble.yaml").read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ble.yaml"
            path.write_text(plan_text)
            invocations = PlanCompiler(builtin_catalog(), registry).compile(
                load_plan(path), "ble")
        boards = leases.boards_for_invocations(invocations, registry)
        # Sorted by key: an Ubertooth's hex serial, then the DK's.
        self.assertEqual([("UT_Linux0", SERIAL), ("DK_Linux0", "nrf52840dk-1050000001")],
                         [(board.alias, board.key) for board in boards])

    def test_preflight_checks_the_dk_probe(self):
        registry = registry_with(REGISTRY)
        invocations = PlanCompiler(builtin_catalog(), registry).compile(
            load_plan(ROOT / "testplans" / "ble.yaml"), "ble")
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = run_contract(Path(directory), tuple(invocations))
        checks = PreflightPolicy(registry).compile(invocations, manifest)
        dk = [check for check in checks if check.resource_alias == "DK_Linux0"]
        self.assertEqual(1, len(dk))
        self.assertEqual(["identify", "--jlink", "1050000001"], list(dk[0].argv[2:]))
        self.assertTrue(dk[0].argv[1].endswith("nrfdk.py"))


if __name__ == "__main__":
    unittest.main()
