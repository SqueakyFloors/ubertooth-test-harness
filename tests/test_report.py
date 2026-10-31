import json
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.events import open_run_publisher
from ubertooth_harness.report import load_events, render_run, render_runs, write_report


SERIAL = "aaaa0000"


def _serial_set(registered: str) -> set[str]:
    """What `_Run.serials()` collects for one registered J-Link serial."""
    from ubertooth_harness.report import _Run

    return _Run([{
        "run_id": "r", "event_type": "manifest", "status": "passed",
        "timestamp": "2026-01-01T00:00:00",
        "details": {"manifest": {"invocations": [
            {"resource_identities": {"advertiser": {"jlink serial": registered}}},
        ]}},
    }]).serials()


def write_run(
    directory: Path, *, finish=True, host="Host_Mac", source=None,
    dk_serial=None, peer_serial=None, dk_device=None,
):
    publisher = open_run_publisher(directory, directory.name)
    identities = {"Ubertooth": {"serial": SERIAL}}
    if dk_serial:
        identities["advertiser"] = {"jlink serial": dk_serial,
                                    "BLE address": "C0:11:22:33:44:55"}
        if dk_device:
            identities["advertiser"]["serial device"] = dk_device
    if peer_serial:
        identities["peer"] = {"bridge serial": peer_serial,
                              "BD_ADDR": "AA:BB:CC:11:22:33"}
    publisher.emit("run_started", "running", {"plan_name": "Plan", "suite": "fast", "host": host})
    publisher.emit("manifest", "passed", {"manifest": {
        "host": host,
        "plan_name": "Plan",
        "suite": "fast",
        "sources": [{"name": "Ubertooth", "requested_ref": "main", "commit": "a" * 40,
                     "expected_revision": "git-aaaaaaa", "repository": "repo",
                     **(source or {})}],
        "artifacts": [],
        "invocations": [{"resource_identities": identities}],
        "environment": {"os": {"system": "Darwin", "architecture": "arm64"}},
    }})
    publisher.emit("test_started", "running", {
        "invocation_id": "001-Ubertooth identity", "test_name": "Ubertooth identity",
        "host": host, "resources": {"Ubertooth": "UT_0"},
    })
    publisher.emit("command_started", "running", {"command_id": "c1", "argv": ["ubertooth-util", "-U", SERIAL, "-s"]})
    publisher.emit("command_output", "running", {"command_id": "c1", "stream": "stdout", "text": f"Serial No: {SERIAL}\n"})
    publisher.emit("command_finished", "passed", {"command_id": "c1", "returncode": 0, "duration_s": 0.1})
    if finish:
        publisher.emit("test_finished", "passed", {
            "invocation_id": "001-Ubertooth identity", "test_name": "Ubertooth identity",
            "reason": "", "failed_cases": 0, "skipped_cases": 0,
        })
        publisher.emit("run_finished", "passed", {
            "completed_tests": 1, "requested_tests": 1, "completed_preflight_checks": 0,
            "requested_preflight_checks": 0, "skipped_cases": 0, "failure_reason": "",
        })


class ReportTests(unittest.TestCase):

    def test_report_has_setup_results_and_headed_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run)

            report = (write_report(run)).read_text()

            self.assertIn("| Result | **passed**: 1/1 tests", report)
            self.assertIn(
                "| 001-Ubertooth identity | Host_Mac (Ubertooth=UT_0) |  | passed |", report)
            self.assertIn("=== TEST 001-Ubertooth identity: Ubertooth identity on Host_Mac (Ubertooth=UT_0) ===", report)
            self.assertIn(f"[C] ubertooth-util -U {SERIAL} -s", report)
            self.assertIn(f"[O] Serial No: {SERIAL}", report)
            self.assertIn(SERIAL, report)  # serials are shown by default

    def test_source_shows_the_ref_and_its_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run)
            self.assertIn(
                f"| Ubertooth | `main` = `{'a' * 40}` (git-aaaaaaa) from repo |",
                write_report(run).read_text(),
            )

    def test_source_chosen_by_name_shows_the_name_and_the_commit(self):
        # A controller run: the runner passes the listed commit as the ref
        # and the branch name as ref_name.
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, source={"requested_ref": "a" * 40, "ref_name": "main"})
            self.assertIn(
                f"| Ubertooth | `main` at `{'a' * 40}` (git-aaaaaaa) from repo |",
                write_report(run).read_text(),
            )

    def test_serials_can_be_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run)

            report = render_run(load_events(run / "run.jsonl"), redact_serials=True)

            self.assertNotIn(SERIAL, report)
            self.assertIn("<serial>", report)

    def test_devices_row_names_a_dk_by_its_jlink_serial(self):
        # A BLE run's devices are an Ubertooth and one or two nRF52840 DKs.
        # The DK's registry identity has no `serial`, so the row used to name
        # it with nothing at all.
        from ubertooth_harness.report import _Run, _setup_rows

        records = []
        identities = (
            ("UT_Linux4", {"serial": SERIAL}),
            ("DK_Linux1", {"jlink serial": "001050210121"}),
        )
        for index, (alias, identity) in enumerate(identities):
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

        devices = dict(_setup_rows(_Run(records)))["Devices"]

        self.assertIn(f"UT_Linux4 serial {SERIAL}", devices)
        self.assertIn("DK_Linux1 serial 001050210121", devices)

    def test_a_short_serial_is_masked_only_where_it_stands_alone(self):
        # A registry entry short enough to read as an ordinary number
        # ("000123", which the registry takes and the tools write as 123):
        # masking it inside every longer number would mangle the transcript.
        from ubertooth_harness.report import _redact

        self.assertEqual(
            "--jlink <serial>; 1234 packets in 7123 us",
            _redact("--jlink 123; 1234 packets in 7123 us",
                    _serial_set("000123")),
        )
        # Its padded width is long enough to mask anywhere, so the path that
        # carries it is masked whatever follows the serial.
        self.assertEqual(
            "port /dev/cu.usbmodem<serial>1",
            _redact("port /dev/cu.usbmodem0000000001231",
                    _serial_set("000123")),
        )
        # A real serial is masked wherever it appears, including inside the
        # name of a device path.
        self.assertEqual(
            "port /dev/cu.usbmodem<serial>1",
            _redact("port /dev/cu.usbmodem0010502101211",
                    _serial_set("001050210121")),
        )

    def test_redaction_masks_32_hex_tokens_but_not_longer_digests(self):
        from ubertooth_harness.report import _redact

        serial = "0123456789abcdef0123456789ABCDEF"
        git_sha = "0123456789abcdef0123456789abcdef01234567"
        sha256 = "f" * 64
        text = f"extra {serial}; commit {git_sha}; digest {sha256}"

        redacted = _redact(text, ())

        self.assertEqual(
            f"extra <serial>; commit {git_sha}; digest {sha256}", redacted
        )

    def test_redaction_covers_guard_and_preflight_serials(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, finish=False)
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("flash_run_started", "running", {
                "serial": SERIAL, "guard_serials": ["cccc2222"], "requested_flashes": 2,
            })
            publisher.emit("command_started", "running", {"command_id": "g", "argv": ["ubertooth-util", "-U", "cccc2222", "-v"]})
            publisher.emit("command_finished", "passed", {"command_id": "g", "returncode": 0, "duration_s": 0.1})

            report = render_run(load_events(run / "run.jsonl"), redact_serials=True)

            self.assertNotIn("cccc2222", report)
            self.assertNotIn(SERIAL, report)

    def test_redaction_masks_a_dk_jlink_serial_in_both_widths(self):
        # The registry pads a J-Link serial to 12 digits and preflight puts it
        # on nrfdk.py's command line as a number, so the report holds both.
        registered, as_a_number = "001050210121", "1050210121"
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, finish=False, dk_serial=registered)
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("command_started", "running", {
                "command_id": "d",
                "argv": ["python", "nrfdk.py", "identify", "--jlink", as_a_number],
            })
            publisher.emit("command_output", "running", {
                "command_id": "d", "stream": "stdout",
                "text": f"J-Link serial: {as_a_number}\n",
            })
            publisher.emit("command_finished", "passed", {
                "command_id": "d", "returncode": 0, "duration_s": 0.1,
            })

            report = render_run(load_events(run / "run.jsonl"), redact_serials=True)

            self.assertNotIn(as_a_number, report)
            self.assertNotIn(registered, report)
            self.assertIn("--jlink <serial>", report)

    def test_redaction_masks_an_esp32_peer_bridge_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, finish=False, peer_serial="e9c5ba112233")
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("command_started", "running", {
                "command_id": "p", "argv": ["esp32-peer", "--serial", "e9c5ba112233"],
            })
            publisher.emit("command_finished", "passed", {
                "command_id": "p", "returncode": 0, "duration_s": 0.1,
            })

            report = render_run(load_events(run / "run.jsonl"), redact_serials=True)

            self.assertNotIn("e9c5ba112233", report)

    def test_redaction_masks_the_serial_inside_a_device_path(self):
        # The by-id name carries the probe's serial, so masking the serial is
        # enough: replacing the whole path would leave the reader less.
        path = "/dev/serial/by-id/usb-SEGGER_J-Link_001050210121-if00"
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, finish=False, dk_serial="001050210121",
                      dk_device=path)
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("command_started", "running", {
                "command_id": "d", "argv": ["nrfdk.py", "identify", "--port", path],
            })
            publisher.emit("command_finished", "passed", {
                "command_id": "d", "returncode": 0, "duration_s": 0.1,
            })

            report = render_run(load_events(run / "run.jsonl"), redact_serials=True)

            self.assertIn(
                "/dev/serial/by-id/usb-SEGGER_J-Link_<serial>-if00", report)
            self.assertNotIn("001050210121", report)

    def test_a_serial_followed_by_a_digit_in_a_path_is_masked(self):
        # macOS names a probe /dev/cu.usbmodem<serial><interface>, so a digit
        # follows the serial. The path is also discovered at run time off
        # Linux, so masking cannot rely on the registry holding it.
        path = "/dev/cu.usbmodem0010502101211"
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, finish=False, dk_serial="001050210121",
                      dk_device=path)
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("command_started", "running", {
                "command_id": "d", "argv": ["nrfdk.py", "identify", "--port", path],
            })
            publisher.emit("command_output", "running", {
                "command_id": "d", "stream": "stdout", "text": f"port: {path}\n",
            })
            publisher.emit("command_finished", "passed", {
                "command_id": "d", "returncode": 0, "duration_s": 0.1,
            })

            report = render_run(load_events(run / "run.jsonl"), redact_serials=True)

            self.assertNotIn("001050210121", report)
            self.assertIn("--port /dev/cu.usbmodem<serial>1", report)

    def test_a_short_serial_does_not_break_a_32_hex_token(self):
        # The token rule masks an Ubertooth serial the run never named (an
        # unregistered board in a failure reason); a short serial in the
        # explicit list must not cut one in half first.
        from ubertooth_harness.report import _redact

        self.assertEqual(
            "enumerated: <serial>",
            _redact("enumerated: aaaaf123ffaaaaaaaaaaaaaaaaaaaaaa",
                    _serial_set("000123")),
        )

    def test_the_padded_width_is_the_one_a_dk_port_name_carries(self):
        # _SERIAL_PADDED_DIGITS restates the padding in targets.DK_BY_ID;
        # this keeps the two in step.
        from ubertooth_harness import targets
        from ubertooth_harness.report import _SERIAL_PADDED_DIGITS

        path = targets.device_path(
            targets.KINDS[targets.NRF52840_DK], "123456", "linux")

        self.assertIn("0" * (_SERIAL_PADDED_DIGITS - 6) + "123456", path)

    def test_serial_widths_cover_padding_but_not_short_numbers(self):
        from ubertooth_harness.report import _serial_widths

        self.assertEqual({"1050210121", "001050210121"}, _serial_widths("001050210121"))
        self.assertEqual({"1050210121", "001050210121"}, _serial_widths("1050210121"))
        self.assertEqual({"12345", "000000012345"}, _serial_widths("12345"))
        # A serial written in hex is masked as the registry holds it, and an
        # Ubertooth's 32 digits are not a J-Link serial in another width.
        self.assertEqual(set(), _serial_widths("aaaa0000"))
        self.assertEqual(set(), _serial_widths("1" * 32))

    def test_report_shows_every_recorded_output_record(self):
        # run.jsonl output is capped when recorded (rawlog.RECORD_*), so the
        # report keeps every record, including how a long command ended.
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("command_started", "running", {"command_id": "c", "argv": ["ubertooth-btle", "-f"]})
            chunk = "x" * 4000 + "\n"
            for _ in range(20):
                publisher.emit("command_output", "running", {"command_id": "c", "stream": "stdout", "text": chunk})
            publisher.emit("command_output", "running", {"command_id": "c", "stream": "stdout", "text": "the end\n"})
            publisher.emit("command_finished", "stopped", {"command_id": "c", "returncode": 124, "duration_s": 4.0, "timed_out": True})

            report = render_run(load_events(run / "run.jsonl"))

            self.assertEqual(20, report.count(chunk.rstrip()))
            self.assertIn("[O] the end", report)
            self.assertNotIn("more characters of output", report)

    def test_older_runs_with_offset_timestamps_render_elapsed_times(self):
        events = [
            {"run_id": "old", "sequence": 1, "timestamp": "2026-10-23T23:00:00.000000+00:00",
             "event_type": "run_started", "status": "running", "details": {}},
            {"run_id": "old", "sequence": 2, "timestamp": "2026-10-23T23:00:01.500",
             "event_type": "run_finished", "status": "passed", "details": {}},
        ]

        report = render_run(events)

        self.assertIn("00:00:00.000     === RUN old", report)
        self.assertIn("00:00:01.500     === RUN passed ===", report)

    def test_unfinished_run_still_renders(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run, finish=False)
            with (run / "run.jsonl").open("a") as stream:
                stream.write('{"truncated": ')  # crash mid-write

            report = render_run(load_events(run / "run.jsonl"))

            self.assertIn("incomplete (no run_finished record)", report)
            self.assertIn("did not finish", report)

    def test_several_runs_in_one_report(self):
        with tempfile.TemporaryDirectory() as directory:
            mac = Path(directory) / "mac-run"
            linux = Path(directory) / "linux-run"
            write_run(mac, host="Host_Mac")
            write_run(linux, host="Host_Linux")

            report = render_runs([mac, linux], title="Release validation")

            self.assertTrue(report.startswith("# Release validation"))
            self.assertIn("| `mac-run` | Host_Mac | passed |", report)
            self.assertIn("| `linux-run` | Host_Linux | passed |", report)
            self.assertNotIn("## Transcript", report)

    def test_records_are_valid_json_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run)

            lines = (run / "run.jsonl").read_text().splitlines()

            self.assertEqual("run_started", json.loads(lines[0])["event_type"])
            self.assertEqual("run_finished", json.loads(lines[-1])["event_type"])


if __name__ == "__main__":
    unittest.main()
