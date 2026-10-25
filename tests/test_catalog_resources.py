import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.planner import PlanCompiler, PlanningError
from ubertooth_harness.plan import PlanError, load_plan
from ubertooth_harness.resources import FileResourceRegistry, RegistryError


HARDWARE = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults: {Ubertooth: UT_Linux0}
  Host_Mac:
    type: Host
    platform: macos
    roles: [test target]
    defaults: {Ubertooth: UT_Mac0}
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: linux-0}
    power: {controller: Hub_Linux0, port: 0}
  UT_Linux1:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: linux-1}
    power: {controller: Hub_Linux0, port: 1}
  UT_Mac0:
    type: Ubertooth
    attached to: Host_Mac
    identity: {serial: mac-0}
  DK_Linux0:
    type: nRF52840 DK
    attached to: Host_Linux
    identity: {probe serial: dk-0}
    capabilities: [BLE central, DLE]
  Hub_Linux0:
    type: USB power controller
    attached to: Host_Linux
    identity: {serial: "0x12345678"}
    driver: acroname BrainStem USBHub3+
    capabilities: [per-port VBUS switching, power readback]
"""


class CatalogTests(unittest.TestCase):

    def test_catalog_uses_exact_human_readable_names(self):
        catalog = builtin_catalog()

        test = catalog.get("Host version")

        self.assertEqual("Host version", test.name)
        self.assertEqual(("ubertooth-util", "-H"), test.command({}))
        with self.assertRaises(CatalogError):
            catalog.get("host_version")

    def test_catalog_rejects_unknown_arguments(self):
        definition = builtin_catalog().get("Host version")

        with self.assertRaisesRegex(CatalogError, "unknown argument"):
            definition.normalize_arguments({"command": "other"})

    def test_debug_register_command_uses_serial_and_normalized_register(self):
        definition = builtin_catalog().get("Debug register read")
        arguments = definition.normalize_arguments(
            {"hosts": ["Host_Linux"], "Ubertooth": "default", "register": "0x0A"}
        )

        cases = definition.command_cases(
            arguments,
            {"Ubertooth": "UT_Linux0"},
            {"Ubertooth": {"serial": "ABC123"}},
        )

        self.assertEqual(
            ("ubertooth-debug", "-U", "abc123", "-r", "0x0a"),
            cases[0].argv,
        )

    def test_host_cli_surface_covers_every_always_built_tool(self):
        definition = builtin_catalog().get("Host CLI surface")

        cases = definition.command_cases({"hosts": "each"}, {}, {})
        helps = [c for c in cases if c.argv[1:] == ("-h",) and not c.needs]
        optional = [c for c in cases if c.needs.bluez_tools]

        self.assertEqual(11, len(helps))
        self.assertEqual({"ubertooth-scan", "ubertooth-follow"}, {c.argv[0] for c in optional})
        self.assertEqual(
            {
                "ubertooth-rx",
                "ubertooth-tx",
                "ubertooth-dump",
                "ubertooth-util",
                "ubertooth-btle",
                "ubertooth-dfu",
                "ubertooth-specan",
                "ubertooth-ego",
                "ubertooth-afh",
                "ubertooth-ducky",
                "ubertooth-debug",
            },
            {case.argv[0] for case in helps},
        )
        util = next(case for case in helps if case.argv[0] == "ubertooth-util")
        rx = next(case for case in helps if case.argv[0] == "ubertooth-rx")
        self.assertEqual((0,), util.expected_returncodes)
        self.assertEqual((0,), rx.expected_returncodes)
        self.assertTrue(definition.continue_on_case_failure)

    def test_ubertooth_identity_builds_named_isolated_and_composed_cases(self):
        definition = builtin_catalog().get("Ubertooth identity")

        cases = definition.command_cases(
            {"hosts": ("Host_Linux",), "Ubertooth": "default"},
            {"Ubertooth": "UT_Linux0"},
            {"Ubertooth": {"serial": "ABC123"}},
        )

        self.assertEqual(
            (
                "firmware-version",
                "firmware-compile-info",
                "device-serial",
                "part-id",
                "board-id",
                "complete-readback-composition",
            ),
            tuple(case.case_id for case in cases),
        )
        self.assertEqual(
            ("ubertooth-util", "-U", "abc123", "-v"), cases[0].argv
        )
        self.assertEqual(
            ("ubertooth-util", "-U", "abc123", "-vVspb"), cases[-1].argv
        )
        self.assertTrue(definition.continue_on_case_failure)

    def test_mac_polling_soak_rejects_negative_failure_limits(self):
        definition = builtin_catalog().get("MAC polling soak")
        arguments = {
            "hosts": ["Host_Linux"],
            "platforms": ["linux"],
            "Ubertooth": "default",
            "advertiser": "DK_Linux0",
            "exclusive Ubertooth": True,
            "duration": "8h",
            "query every": "1s",
            "log every": "1s",
            "bucket count": 5,
            "bucket rotation": "2s",
            "listener silence timeout": "10s",
            "empty buckets restart after": "15s",
            "restart suppression": "5s",
            "restart timeout extension": "20s",
            "post-restart wait": "10s",
            "search timeout": "120s",
            "maximum consecutive misses": -1,
            "maximum restarts": 0,
        }

        with self.assertRaisesRegex(CatalogError, "maximum consecutive misses"):
            definition.normalize_arguments(arguments)


class NamedPlanTests(unittest.TestCase):

    def write(self, directory, text):
        path = Path(directory) / "plan.yaml"
        path.write_text(text)
        return path

    def test_one_key_step_loads_direct_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                """plan: named
suites:
  fast:
    - Host version:
        hosts: [Host_Linux]
""",
            )

            plan = load_plan(path)

            step = plan.steps("fast")[0]
            self.assertEqual("Host version", step.test_name)
            self.assertEqual(("Host_Linux",), step.arguments["hosts"])

    def test_rejects_multiple_test_names_in_one_step(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                """plan: bad
suites:
  fast:
    - Host version: {}
      Help smoke: {}
""",
            )

            with self.assertRaisesRegex(PlanError, "exactly one test name"):
                load_plan(path)

    def test_rejects_legacy_command_or_unknown_test(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                """plan: bad
suites:
  fast:
    - id: old
      cmd: ubertooth-util -H
""",
            )

            with self.assertRaises(PlanError):
                load_plan(path)


class ResourceRegistryTests(unittest.TestCase):

    def registry(self, directory, text=HARDWARE):
        path = Path(directory) / "hardware.yaml"
        path.write_text(text)
        return FileResourceRegistry.load(path)

    def test_default_and_each_resolution_are_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(directory)

            self.assertEqual(
                ("UT_Linux0",),
                registry.resolve_hardware("Host_Linux", "Ubertooth", "default"),
            )
            self.assertEqual(
                ("UT_Linux0", "UT_Linux1"),
                registry.resolve_hardware("Host_Linux", "Ubertooth", "each"),
            )
            self.assertEqual(
                ("UT_Linux1",),
                registry.resolve_hardware("Host_Linux", "Ubertooth", "UT_Linux1"),
            )
            with self.assertRaises(RegistryError):
                registry.resolve_hardware("Host_Linux", "Ubertooth", "any")

    def test_rejects_duplicate_or_invalid_power_port_mapping(self):
        duplicate = HARDWARE.replace(
            "power: {controller: Hub_Linux0, port: 1}",
            "power: {controller: Hub_Linux0, port: 0}",
        )
        invalid = HARDWARE.replace(
            "power: {controller: Hub_Linux0, port: 1}",
            "power: {controller: Hub_Linux0, port: 8}",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RegistryError, "duplicate power port"):
                self.registry(directory, duplicate)
            with self.assertRaisesRegex(RegistryError, "port must be 0-7"):
                self.registry(directory, invalid)

    def test_startech_controller_requires_ports_1_to_3_and_device_identity(self):
        startech = HARDWARE.replace(
            'identity: {serial: "0x12345678"}\n    driver: acroname BrainStem USBHub3+\n'
            '    capabilities: [per-port VBUS switching, power readback]',
            'identity: {serial device: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_X-if00-port0}\n'
            '    driver: startech managed USB hub\n'
            '    capabilities: [per-port VBUS switching]',
        ).replace(
            "power: {controller: Hub_Linux0, port: 0}",
            "power: {controller: Hub_Linux0, port: 3}",
        )
        control_port = startech.replace(
            "power: {controller: Hub_Linux0, port: 1}",
            "power: {controller: Hub_Linux0, port: 4}",
        )
        no_identity = startech.replace("serial device:", "path:")
        unstable = startech.replace(
            "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_X-if00-port0", "/dev/ttyUSB0"
        )
        with tempfile.TemporaryDirectory() as directory:
            self.registry(directory, startech)
            with self.assertRaisesRegex(RegistryError, "port must be 1-3"):
                self.registry(directory, control_port)
            no_capability = startech.replace(
                "capabilities: [per-port VBUS switching]", "capabilities: []"
            )
            with self.assertRaisesRegex(RegistryError, "lacks required capabilities"):
                self.registry(directory, no_capability)
            for text in (no_identity, unstable):
                with self.assertRaisesRegex(RegistryError, "serial device identity"):
                    self.registry(directory, text)

    def test_rejects_duplicate_hardware_identity(self):
        duplicate = HARDWARE.replace("serial: linux-1", "serial: linux-0")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RegistryError, "duplicate identity"):
                self.registry(directory, duplicate)

    def test_rejects_default_on_wrong_host(self):
        invalid = HARDWARE.replace("Ubertooth: UT_Linux0", "Ubertooth: UT_Mac0")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RegistryError, "default.*attached"):
                self.registry(directory, invalid)

    def test_reconciles_present_missing_and_unknown_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(directory)
            discovered = (
                {"type": "Ubertooth", "identity": {"serial": "linux-0"}},
                {"type": "nRF52840 DK", "identity": {"serial": "linux-1"}},
                {"type": "Ubertooth", "identity": {"serial": "unknown"}},
            )

            result = registry.reconcile("Host_Linux", discovered)

            self.assertEqual(("UT_Linux0",), result.present)
            self.assertNotIn("UT_Linux1", result.missing)
            self.assertIn("DK_Linux0", result.missing)
            self.assertEqual(("UT_Linux1",), result.conflicting)
            self.assertEqual(1, len(result.unknown))


class PlanningTests(unittest.TestCase):

    def prepare(self, directory, plan_text):
        registry_path = Path(directory) / "hardware.yaml"
        registry_path.write_text(HARDWARE)
        plan_path = Path(directory) / "plan.yaml"
        plan_path.write_text(plan_text)
        return load_plan(plan_path), FileResourceRegistry.load(registry_path)

    def test_default_ubertooth_expands_once_per_registered_host(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, registry = self.prepare(
                directory,
                """plan: default devices
suites:
  fast:
    - Firmware version:
        hosts: each
        Ubertooth: default
""",
            )

            invocations = PlanCompiler(builtin_catalog(), registry).compile(
                plan, "fast"
            )
            self.assertEqual(
                (("Host_Linux", "UT_Linux0"), ("Host_Mac", "UT_Mac0")),
                tuple(
                    (item.host, item.resources["Ubertooth"])
                    for item in invocations
                ),
            )

            self.assertEqual(2, len(invocations))
            self.assertEqual(
                ("linux-0", "mac-0"),
                tuple(
                    item.resource_identities["Ubertooth"]["serial"]
                    for item in invocations
                ),
            )

    def test_each_expands_deterministically_for_one_host(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, registry = self.prepare(
                directory,
                """plan: each device
suites:
  fast:
    - Firmware version:
        hosts: [Host_Linux]
        Ubertooth: each
""",
            )

            invocations = PlanCompiler(builtin_catalog(), registry).compile(
                plan, "fast"
            )

            self.assertEqual(
                ("UT_Linux0", "UT_Linux1"),
                tuple(item.resources["Ubertooth"] for item in invocations),
            )
            self.assertEqual(
                ("001-Firmware version", "002-Firmware version"),
                tuple(item.invocation_id for item in invocations),
            )

    def test_two_device_test_honors_explicit_host_and_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, registry = self.prepare(
                directory,
                """plan: range
suites:
  slow:
    - Two-device range test:
        host: Host_Linux
        sender: UT_Linux0
        responder: UT_Linux1
""",
            )

            invocations = PlanCompiler(builtin_catalog(), registry).compile(
                plan, "slow"
            )

            self.assertEqual(1, len(invocations))
            self.assertEqual("Host_Linux", invocations[0].host)
            self.assertEqual(
                {"sender": "UT_Linux0", "responder": "UT_Linux1"},
                dict(invocations[0].resources),
            )

    def test_each_is_rejected_for_multi_role_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, registry = self.prepare(
                directory,
                """plan: ambiguous range
suites:
  slow:
    - Two-device range test:
        host: Host_Linux
        sender: each
        responder: each
""",
            )

            with self.assertRaisesRegex(PlanningError, "explicit role aliases"):
                PlanCompiler(builtin_catalog(), registry).compile(plan, "slow")

    def test_non_distinct_multi_role_assignment_fails_compilation(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, registry = self.prepare(
                directory,
                """plan: invalid range
suites:
  slow:
    - Two-device range test:
        host: Host_Linux
        sender: default
        responder: default
""",
            )

            with self.assertRaisesRegex(PlanningError, "distinct"):
                PlanCompiler(builtin_catalog(), registry).compile(plan, "slow")

    def test_mac_polling_plan_compiles_to_linux_sniffer_and_advertiser(self):
        project = Path(__file__).resolve().parents[1]
        plan = load_plan(project / "testplans" / "mac-polling.yaml")
        registry = FileResourceRegistry.load(project / "hardware.example.yaml")

        invocations = PlanCompiler(builtin_catalog(), registry).compile(
            plan, "mac-polling"
        )

        self.assertEqual(1, len(invocations))
        invocation = invocations[0]
        self.assertEqual("MAC polling soak", invocation.test_name)
        self.assertEqual("Host_Linux", invocation.host)
        self.assertEqual(
            {"Ubertooth": "UT_Linux0", "advertiser": "DK_Linux0"},
            dict(invocation.resources),
        )
        self.assertEqual(
            "REPLACE_WITH_ADVERTISING_MAC",
            invocation.resource_identities["advertiser"]["BLE address"],
        )
        self.assertEqual(5, invocation.arguments["bucket count"])
        self.assertEqual("2s", invocation.arguments["bucket rotation"])
        self.assertEqual("1s", invocation.arguments["query every"])
        self.assertTrue(invocation.arguments["exclusive Ubertooth"])
        self.assertEqual(1, len(invocation.power_isolation))
        action = invocation.power_isolation[0]
        expected_power = registry.get("UT_Linux1").power
        self.assertEqual("UT_Linux1", action.resource_alias)
        self.assertEqual(expected_power["controller"], action.controller_alias)
        self.assertEqual(expected_power["port"], action.port)
        self.assertFalse(action.power_enabled)

    def test_exclusive_ubertooth_requires_power_mapping_for_every_extra(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hardware = HARDWARE.replace(
                "    power: {controller: Hub_Linux0, port: 1}\n", ""
            )
            hardware_path = root / "hardware.yaml"
            hardware_path.write_text(hardware)
            plan_path = root / "plan.yaml"
            plan_path.write_text(
                """plan: exclusive
suites:
  mac-polling:
    - MAC polling soak:
        hosts: [Host_Linux]
        platforms: [linux]
        Ubertooth: default
        advertiser: DK_Linux0
        exclusive Ubertooth: true
        duration: 1h
        query every: 1s
        log every: 1s
        bucket count: 5
        bucket rotation: 2s
        listener silence timeout: 10s
        empty buckets restart after: 15s
        restart suppression: 5s
        restart timeout extension: 20s
        post-restart wait: 10s
        search timeout: 120s
        maximum consecutive misses: 1
        maximum restarts: 0
"""
            )
            plan = load_plan(plan_path)
            registry = FileResourceRegistry.load(hardware_path)

            with self.assertRaisesRegex(PlanningError, "power mapping"):
                PlanCompiler(builtin_catalog(), registry).compile(plan, "mac-polling")

    def test_platform_requirement_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, registry = self.prepare(
                directory,
                """plan: wrong platform
suites:
  slow:
    - Flash reliability:
        hosts: [Host_Mac]
        platforms: [linux]
        Ubertooth: default
        flashes: 2
        image sequence: [test build, KGV]
        KGV sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        KGV revision: 2020-12-R1
""",
            )

            with self.assertRaisesRegex(
                PlanningError, "no selected host can run it: Host_Mac: runs only on Linux"
            ):
                PlanCompiler(builtin_catalog(), registry).compile(plan, "slow")


if __name__ == "__main__":
    unittest.main()
