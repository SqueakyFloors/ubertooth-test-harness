"""Known target records, their report sections, and the shell transcript."""

import tempfile
import unittest
from pathlib import Path

from ubertooth_harness import nrfdk, targets
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.events import open_run_publisher
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.rawlog import line_tag
from ubertooth_harness.report import load_events, render_run
from ubertooth_harness.sequencer import NamedTestSequencer

ROOT = Path(__file__).resolve().parents[1]

DK_ROLES = {"Ubertooth": "Ubertooth", "advertiser": targets.NRF52840_DK}
DK_RESOURCES = {"Ubertooth": "UT_Linux0", "advertiser": "DK_Linux1"}
DK_IDENTITIES = {
    "Ubertooth": {"serial": "a" * 32},
    "advertiser": {"jlink serial": "000123456789",
                   "BLE address": "C0:00:00:00:00:01"},
}

PEER_ROLES = {"Ubertooth": "Ubertooth", "initiator": targets.ESP32_CLASSIC_PEER}
PEER_RESOURCES = {"Ubertooth": "UT_Linux0", "initiator": "ESP32_Linux1"}
PEER_IDENTITIES = {
    "Ubertooth": {"serial": "a" * 32},
    "initiator": {"bridge serial": "0a1b2c3d", "BD_ADDR": "20:50:0D:AA:BB:CC"},
}


class FirmwareVersionTests(unittest.TestCase):
    """The versions this module reports are the firmware's own."""

    def test_dk_firmware_is_the_version_its_driver_requires(self):
        self.assertEqual(nrfdk.FIRMWARE_VERSION, targets.DK_FIRMWARE)
        header = (ROOT / "firmware" / "nrf52840dk" / "src" / "roles.h").read_text()
        self.assertIn(f'#define UTDK_FW_VERSION "{targets.DK_FIRMWARE}"', header)

    def test_peer_firmware_is_the_version_the_peer_firmware_reports(self):
        header = (ROOT / "firmware" / "esp32-classic-peer" / "main" / "peer.h").read_text()
        self.assertIn(f'#define UTESP_FW_VERSION "{targets.PEER_FIRMWARE}"', header)

    def test_the_peer_s_device_path_is_the_one_its_firmware_readme_documents(self):
        readme = (ROOT / "firmware" / "esp32-classic-peer" / "README.md").read_text()
        self.assertIn(targets.PEER_BY_ID.replace("{serial}", "<serial>"), readme)


class KnownTargetTests(unittest.TestCase):

    def test_a_dk_case_names_the_board_its_port_firmware_and_address(self):
        found = targets.known_targets(
            DK_ROLES, DK_RESOURCES, DK_IDENTITIES, platform="linux")

        self.assertEqual(1, len(found))
        self.assertEqual(
            {
                "type": "nRF52840 DK",
                "role": "advertiser",
                "alias": "DK_Linux1",
                "serial": "000123456789",
                "serial_label": "J-Link",
                "port": "/dev/serial/by-id/usb-SEGGER_J-Link_000123456789-if00",
                "firmware_required": targets.DK_FIRMWARE,
                "identity": {"BLE address": "C0:00:00:00:00:01"},
            },
            found[0],
        )
        self.assertEqual(
            "Run against known target: nRF52840 DK advertiser DK_Linux1 "
            "(J-Link 000123456789, "
            "/dev/serial/by-id/usb-SEGGER_J-Link_000123456789-if00), "
            f"requires firmware {targets.DK_FIRMWARE}, BLE address C0:00:00:00:00:01",
            targets.summary(found),
        )
        self.assertEqual("DK_Linux1 (advertiser)", targets.column(found))

    def test_an_esp32_case_names_the_bridge_and_the_address_with_lap_and_uap(self):
        found = targets.known_targets(
            PEER_ROLES, PEER_RESOURCES, PEER_IDENTITIES, platform="linux")

        self.assertEqual(1, len(found))
        self.assertEqual("ESP32 Classic peer", found[0]["type"])
        self.assertEqual("0a1b2c3d", found[0]["serial"])
        self.assertEqual(
            "/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_"
            "Controller_0a1b2c3d-if00-port0",
            found[0]["port"],
        )
        self.assertEqual(
            "Run against known target: ESP32 Classic peer initiator ESP32_Linux1 "
            "(bridge 0a1b2c3d, /dev/serial/by-id/"
            "usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_0a1b2c3d-if00-port0), "
            f"requires firmware {targets.PEER_FIRMWARE}, "
            "BD_ADDR 20:50:0D:AA:BB:CC (LAP aabbcc, UAP 0d)",
            targets.summary(found),
        )
        self.assertEqual("ESP32_Linux1 (initiator)", targets.column(found))

    def test_a_test_with_no_known_target_says_so(self):
        found = targets.known_targets(
            {"Ubertooth": "Ubertooth"}, {"Ubertooth": "UT_Linux0"},
            {"Ubertooth": {"serial": "a" * 32}}, platform="linux")

        self.assertEqual((), found)
        self.assertEqual(
            "No known target: no nRF52840 DK or ESP32 Classic peer took part "
            "in this test",
            targets.summary(found),
        )
        self.assertEqual("none", targets.column(found))

    def test_a_registered_serial_device_is_used_instead_of_the_derived_path(self):
        identities = {**DK_IDENTITIES, "advertiser": {
            **DK_IDENTITIES["advertiser"], "serial device": "/dev/ttyACM3"}}

        found = targets.known_targets(
            DK_ROLES, DK_RESOURCES, identities, platform="linux")

        self.assertEqual("/dev/ttyACM3", found[0]["port"])

    def test_no_path_is_derived_where_the_host_has_no_stable_one(self):
        found = targets.known_targets(
            DK_ROLES, DK_RESOURCES, DK_IDENTITIES, platform="macos")

        self.assertEqual("", found[0]["port"])
        self.assertNotIn("/dev/", targets.summary(found))

    def test_both_dks_of_a_connection_test_are_named_in_the_test_s_order(self):
        roles = {"Ubertooth": "Ubertooth", "central": targets.NRF52840_DK,
                 "peripheral": targets.NRF52840_DK}
        resources = {"Ubertooth": "UT_Linux0", "central": "DK_Linux1",
                     "peripheral": "DK_Linux2"}
        identities = {
            "central": {"jlink serial": "000123456789",
                        "BLE address": "C0:00:00:00:00:02"},
            "peripheral": {"jlink serial": "000123456790",
                           "BLE address": "C0:00:00:00:00:03"},
        }

        found = targets.known_targets(roles, resources, identities)

        self.assertEqual(["central", "peripheral"], [t["role"] for t in found])
        self.assertTrue(targets.summary(found).startswith(
            "Run against known targets: nRF52840 DK central DK_Linux1"))
        self.assertEqual("DK_Linux1 (central), DK_Linux2 (peripheral)",
                         targets.column(found))


class ShellLineTests(unittest.TestCase):

    def test_a_shell_line_carries_the_alias_and_the_direction(self):
        self.assertEqual("[DK_Linux1 >] adv start",
                         targets.shell_line("DK_Linux1", targets.SENT, "adv start"))
        self.assertEqual("[DK_Linux1 <] OK",
                         targets.shell_line("DK_Linux1", targets.RECEIVED, "OK"))

    def test_an_unknown_direction_is_refused(self):
        with self.assertRaises(ValueError):
            targets.shell_line("DK_Linux1", "?", "adv start")

    def test_a_shell_line_on_stderr_is_tagged_with_the_alias(self):
        self.assertEqual(
            ("[DK_Linux1 >]", "adv start"),
            line_tag("stderr", "[DK_Linux1 >] adv start"),
        )
        self.assertEqual(
            ("[DK_Linux1 <]", '{"adv": true}'),
            line_tag("stderr", '[DK_Linux1 <] {"adv": true}'),
        )

    def test_other_output_keeps_its_stream_tag(self):
        self.assertEqual(("[E]", "error: nothing"), line_tag("stderr", "error: nothing"))
        self.assertEqual(("[O]", "[DK_Linux1 >] adv start"),
                         line_tag("stdout", "[DK_Linux1 >] adv start"))
        # Not a shell line: no direction, and no alias.
        self.assertEqual(("[E]", "[warning] late"), line_tag("stderr", "[warning] late"))
        self.assertIsNone(targets.shell_tag("[ >] adv start"))


class _Recorder:
    def __init__(self):
        self.events = []

    def emit(self, event_type, status, details):
        self.events.append((event_type, status, details))


class _Registry:
    def __init__(self, platform):
        self.platform = platform

    def get(self, _alias):
        return self


class SequencerRecordTests(unittest.TestCase):
    """The run records each test's known target from what the planner bound."""

    def _record(self, test_name, resources, identities, platform="linux"):
        recorder = _Recorder()
        # No command runs here, so the sequencer needs no executor.
        NamedTestSequencer(None, registry=_Registry(platform))._record_known_targets(
            recorder,
            TestInvocation(
                invocation_id=f"001-{test_name}", test_name=test_name,
                host="Host_Linux", arguments={}, resources=resources,
                resource_identities=identities,
            ),
            builtin_catalog().get(test_name),
        )
        self.assertEqual(1, len(recorder.events))
        return recorder.events[0]

    def test_a_dk_test_records_the_board_the_planner_bound(self):
        event_type, status, details = self._record(
            "Capture known BLE advertiser", DK_RESOURCES, DK_IDENTITIES)

        self.assertEqual(("known_target", "recorded"), (event_type, status))
        self.assertEqual("001-Capture known BLE advertiser", details["invocation_id"])
        self.assertEqual(
            [{"alias": "DK_Linux1", "role": "advertiser", "type": "nRF52840 DK"}],
            [{key: target[key] for key in ("alias", "role", "type")}
             for target in details["targets"]],
        )
        self.assertIn("BLE address C0:00:00:00:00:01", details["summary"])

    def test_a_test_with_only_an_ubertooth_records_that_it_had_no_target(self):
        event_type, status, details = self._record(
            "Ubertooth identity", {"Ubertooth": "UT_Linux0"},
            {"Ubertooth": {"serial": "a" * 32}})

        self.assertEqual(("known_target", "none"), (event_type, status))
        self.assertEqual([], details["targets"])
        self.assertEqual(targets.NO_TARGET, details["summary"])

    def test_without_a_registry_the_host_s_derived_paths_are_left_out(self):
        recorder = _Recorder()
        NamedTestSequencer(None)._record_known_targets(
            recorder,
            TestInvocation(
                invocation_id="001-Capture known BLE advertiser",
                test_name="Capture known BLE advertiser", host="Host_Linux",
                arguments={}, resources=DK_RESOURCES,
                resource_identities=DK_IDENTITIES,
            ),
            builtin_catalog().get("Capture known BLE advertiser"),
        )

        self.assertEqual("", recorder.events[0][2]["targets"][0]["port"])


def _run(directory: Path) -> str:
    """A run with a DK test, an ESP32 test, and one with no known target."""
    publisher = open_run_publisher(directory, directory.name)
    publisher.emit("run_started", "running",
                   {"plan_name": "Plan", "suite": "fast", "host": "Host_Linux"})
    publisher.emit("manifest", "passed", {"manifest": {
        "host": "Host_Linux", "plan_name": "Plan", "suite": "fast",
        "sources": [], "artifacts": [], "invocations": [],
        "environment": {"os": {"system": "Linux", "architecture": "x86_64"}},
    }})
    tests = [
        ("001-Capture known BLE advertiser", "Capture known BLE advertiser",
         targets.known_targets(DK_ROLES, DK_RESOURCES, DK_IDENTITIES, "linux")),
        ("002-Classic traffic", "Classic traffic",
         targets.known_targets(PEER_ROLES, PEER_RESOURCES, PEER_IDENTITIES, "linux")),
        ("003-Host CLI surface", "Host CLI surface", ()),
    ]
    for index, (invocation, name, found) in enumerate(tests, 1):
        publisher.emit("test_started", "running", {
            "invocation_id": invocation, "test_name": name,
            "host": "Host_Linux", "resources": {"Ubertooth": "UT_Linux0"},
        })
        publisher.emit(
            "known_target", "recorded" if found else "none",
            targets.target_details(invocation, name, found),
        )
        if found:
            command = f"c{index}"
            publisher.emit("command_started", "running", {
                "invocation_id": invocation, "command_id": command,
                "argv": ["python", "nrfdk.py", "advertise"],
            })
            alias = found[0]["alias"]
            for stream, text in (
                ("stderr", targets.shell_line(alias, targets.SENT, "adv start") + "\n"),
                ("stderr", targets.shell_line(alias, targets.RECEIVED, "OK") + "\n"),
                ("stdout", "advertising: on type nonconn chmap 37 \n"),
            ):
                publisher.emit("command_output", "running", {
                    "invocation_id": invocation, "command_id": command,
                    "stream": stream, "text": text,
                })
            publisher.emit("command_finished", "passed", {
                "invocation_id": invocation, "command_id": command,
                "returncode": 0, "duration_s": 0.2,
            })
        publisher.emit("test_finished", "passed", {
            "invocation_id": invocation, "test_name": name, "reason": "",
            "failed_cases": 0, "skipped_cases": 0,
        })
    publisher.emit("run_finished", "passed", {
        "completed_tests": 3, "requested_tests": 3,
        "completed_preflight_checks": 0, "requested_preflight_checks": 0,
        "skipped_cases": 0, "failure_reason": "",
    })
    return render_run(load_events(directory / "run.jsonl"))


class ReportSectionTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.report = _run(Path(self.directory.name) / "run-186")

    def test_the_results_table_names_each_test_s_known_target(self):
        rows = [line for line in self.report.splitlines() if line.startswith("| 00")]
        self.assertIn("| DK_Linux1 (advertiser) |", rows[0])
        self.assertIn("| ESP32_Linux1 (initiator) |", rows[1])
        self.assertIn("| none |", rows[2])

    def test_the_known_targets_section_describes_every_test(self):
        section = self.report.split("## Known targets", 1)[1].split("## Transcript", 1)[0]
        self.assertIn(
            "- `001-Capture known BLE advertiser`: Run against known target: "
            "nRF52840 DK advertiser DK_Linux1 (J-Link 000123456789, "
            "/dev/serial/by-id/usb-SEGGER_J-Link_000123456789-if00), "
            f"requires firmware {targets.DK_FIRMWARE}, BLE address C0:00:00:00:00:01",
            section,
        )
        self.assertIn(
            "- `002-Classic traffic`: Run against known target: ESP32 Classic "
            "peer initiator ESP32_Linux1 (bridge 0a1b2c3d,",
            section,
        )
        self.assertIn("BD_ADDR 20:50:0D:AA:BB:CC (LAP aabbcc, UAP 0d)", section)
        self.assertIn(
            "- `003-Host CLI surface`: No known target: no nRF52840 DK or "
            "ESP32 Classic peer took part in this test",
            section,
        )

    def test_the_transcript_marks_each_test_s_known_target(self):
        transcript = self.report.split("```text", 1)[1]
        self.assertIn("[M] Run against known target: nRF52840 DK advertiser "
                      "DK_Linux1 ", transcript)
        self.assertIn("[M] No known target: no nRF52840 DK or ESP32 Classic "
                      "peer took part in this test", transcript)

    def test_every_shell_line_is_in_the_transcript_timestamped_with_its_alias(self):
        transcript = self.report.split("```text", 1)[1]
        self.assertRegex(
            transcript, r"(?m)^\d\d:\d\d:\d\d\.\d\d\d \[DK_Linux1 >\] adv start$")
        self.assertRegex(transcript, r"(?m)^\d\d:\d\d:\d\d\.\d\d\d \[DK_Linux1 <\] OK$")
        # The tool's own output is still tagged by its stream.
        self.assertRegex(
            transcript, r"(?m)^\d\d:\d\d:\d\d\.\d\d\d \[O\] advertising: on ")

    def test_the_legend_explains_the_alias_tags(self):
        self.assertIn("`[<alias> >]` a line sent to that known target's serial "
                      "shell and `[<alias> <]` a line it answered", self.report)


class OlderRunTests(unittest.TestCase):

    def test_a_run_recorded_without_these_records_keeps_its_old_report(self):
        """The section is left out rather than filled with "not recorded"."""
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-old"
            publisher = open_run_publisher(run, run.name)
            publisher.emit("run_started", "running",
                           {"plan_name": "Plan", "suite": "fast", "host": "Host_Mac"})
            publisher.emit("test_started", "running", {
                "invocation_id": "001-Host CLI surface",
                "test_name": "Host CLI surface", "host": "Host_Mac", "resources": {},
            })
            publisher.emit("test_finished", "passed", {
                "invocation_id": "001-Host CLI surface",
                "test_name": "Host CLI surface", "reason": "",
                "failed_cases": 0, "skipped_cases": 0,
            })
            report = render_run(load_events(run / "run.jsonl"))

            self.assertNotIn("## Known targets", report)
            # The column is there, with nothing recorded for the test.
            self.assertIn("| Known target |", report)
            self.assertRegex(report, r"(?m)^\| 001-Host CLI surface \| Host_Mac \|  \|")


if __name__ == "__main__":
    unittest.main()
