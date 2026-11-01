import datetime
import itertools
import json
import re
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.events import open_run_publisher
from ubertooth_harness.rawlog import (
    RECORD_OUTPUT_BYTES,
    RECORD_PIECE_CHARS,
    CappedOutput,
    CommandOutputFiles,
    line_shape,
    record_bytes,
)
from ubertooth_harness.report import (
    LEGACY_OUTPUT_HEAD_CHARS,
    LEGACY_OUTPUT_HEAD_RECORDS,
    LEGACY_OUTPUT_TAIL_CHARS,
    LEGACY_OUTPUT_TAIL_RECORDS,
    REDACTED,
    _take_chunks,
    _tail_on_whole_lines,
    elapsed,
    load_events,
    render_run,
    render_runs,
    write_report,
)


SERIAL = "aaaa0000"
_START = datetime.datetime(2026, 1, 1)
# Output records run.jsonl keeps before it starts leaving output out, for
# output that arrives as one long line of plain characters.
PIECES_KEPT = RECORD_OUTPUT_BYTES // record_bytes("x" * RECORD_PIECE_CHARS)


def elapsed_stamp(ms: int) -> str:
    """The transcript's stamp that many milliseconds into the run."""
    return elapsed(_START)(_START + datetime.timedelta(milliseconds=ms))


def gap_in(report: str) -> int:
    """Characters the report says it left out of the transcript."""
    return int(re.search(
        r"\[M\] (\d+) more characters of output are in run\.jsonl", report,
    ).group(1))


def dump_events(texts: list[str]) -> list[dict]:
    """One command that printed ``texts`` on stderr, recorded with no cap."""
    records = [command_event(0, "command_started", "running",
                             {"argv": ["ubertooth-dump"]})]
    records += [
        command_event(ms, "command_output", "running",
                      {"stream": "stderr", "text": text})
        for ms, text in enumerate(texts, start=1)
    ]
    records.append(command_event(len(texts) + 1, "command_finished", "passed",
                                 {"returncode": 0, "duration_s": 9.0}))
    return records


def command_event(ms: int, event_type: str, status: str, details: dict) -> dict:
    """One record of command `c`, that many milliseconds into the run.

    Built here rather than through a publisher: a test of the output cap
    needs thousands of records, and writing them through run.jsonl and the
    console writer takes minutes.
    """
    moment = _START + datetime.timedelta(milliseconds=ms)
    return {
        "run_id": "run-a", "event_type": event_type, "status": status,
        "timestamp": moment.isoformat(timespec="milliseconds"),
        "details": {"command_id": "c", **details},
    }


def capped_events(text: str, stream: str = "stderr") -> list[dict]:
    """One command whose output run.jsonl capped as it was recorded.

    The records come from rawlog.CappedOutput itself, so where the cut falls
    is the real one, not one this test chose.
    """
    records = [command_event(0, "command_started", "running",
                             {"argv": ["ubertooth-dump"]})]
    moments = itertools.count(1)

    def record(name: str, piece: str, received: str | None) -> None:
        details = {"stream": name, "text": piece}
        if received:
            details["received"] = received
        records.append(
            command_event(next(moments), "command_output", "running", details))

    with tempfile.TemporaryDirectory() as spill:
        capped = CappedOutput(record, lambda: CommandOutputFiles(Path(spill), "c"))
        capped.add(_START, stream, text)
        details = capped.finish(lambda note: records.append(command_event(
            next(moments), "command_output_omitted", "running", note)))
        capped.close()
    records.append(command_event(next(moments), "command_finished", "passed",
                                 {"returncode": 0, "duration_s": 9.0, **details}))
    return records


def ubertooth_manifest(serial: str) -> dict:
    """A manifest record naming one registered Ubertooth."""
    return {
        "run_id": "run-a", "event_type": "manifest", "status": "passed",
        "timestamp": _START.isoformat(timespec="milliseconds"),
        "details": {"manifest": {"invocations": [
            {"resource_identities": {"Ubertooth": {"serial": serial}}},
        ]}},
    }


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

    def test_a_serial_the_recorded_cap_cut_in_half_is_masked(self):
        # #214: run.jsonl caps output as it is recorded, in pieces of a fixed
        # size, so a serial on a long line can be cut in two. Nothing is
        # dropped from the tail here, so it is emitted as it arrived and the
        # second piece of the serial opens a line.
        serial = "cafebabedeadbeefcafeba9edead1ee0"
        head = PIECES_KEPT * RECORD_PIECE_CHARS  # characters the head keeps
        cut = 16  # digits of the serial the cut leaves in the head
        text = ("x" * (head - cut) + serial
                + " is idle\n" + "y" * 4 * RECORD_PIECE_CHARS + "\n")
        events = [ubertooth_manifest(serial)] + capped_events(text)

        plain = render_run(events)
        report = render_run(events, redact_serials=True)

        self.assertNotIn(serial, plain)  # the cut split it across two records
        self.assertIn(f"x{serial[:cut]}", plain)
        self.assertIn(f"[E] {serial[cut:]} is idle", plain)
        self.assertNotIn(serial[:cut], report)
        self.assertNotIn(serial[cut:], report)
        self.assertIn(f"[E] {REDACTED} is idle", report)

    def test_a_serial_the_report_cap_cut_in_half_is_masked(self):
        # The report's own fallback cap, for a run recorded before run.jsonl
        # capped output as it was written: it cuts a long line by character
        # count, which lands inside a serial just as readily.
        serial = "0123456789abcdef0123456789abcdef"
        cut = 16
        line = ("x" * (LEGACY_OUTPUT_HEAD_CHARS - cut) + serial
                + "y" * LEGACY_OUTPUT_TAIL_CHARS + "\n")
        events = [ubertooth_manifest(serial)] + dump_events([line])

        report = render_run(events, redact_serials=True)

        self.assertIn(serial[:cut], render_run(events))  # shown unredacted
        self.assertNotIn(serial[:cut], report)
        self.assertNotIn(serial[cut:], report)
        self.assertIn(f"x{REDACTED}", report)
        self.assertIn("more characters of output are in run.jsonl", report)

    def test_a_piece_of_a_serial_is_masked_wherever_a_cut_left_one(self):
        # Either side of the cut can be what survives, and the gap note or
        # the output around it can leave it at the start of a line, at the
        # end of one, or inside one.
        from ubertooth_harness.report import _redact

        serial = "0123456789abcdef0123456789abcdef"
        for piece in (serial[:20], serial[12:], serial[8:24]):
            self.assertEqual(
                "[O] boards: <serial> is idle\n",
                _redact(f"[O] boards: {piece} is idle\n", {serial}), piece)
        # A J-Link serial in the width the tools write it (_serial_widths).
        self.assertEqual(
            "[O] --jlink <serial>\n",
            _redact("[O] --jlink 10502101\n", _serial_set("001050210121")))

    def test_a_cut_serial_no_board_registered_is_masked_by_its_width(self):
        # The serial of a board the run never named (what _SERIAL_TOKEN
        # catches when it is whole) can only be recognized by its shape.
        from ubertooth_harness.report import _redact

        self.assertEqual(
            "[O] enumerated: <serial>\n",
            _redact("[O] enumerated: 1cc001038088cb956c43ad61\n", ()))
        # Narrower than that, an unknown value is left to read: masking it
        # would mask ordinary output.
        self.assertEqual(
            "[O] AA: 8e89bed6\n", _redact("[O] AA: 8e89bed6\n", ()))

    def test_hex_that_is_no_piece_of_a_serial_is_left_alone(self):
        from ubertooth_harness.report import _redact

        serial = "0123456789abcdef0123456789abcdef"
        kept = (
            "[M] commit 7f3b91e04c6a28d5ff10b7e93c4a6d82be915074\n"    # a SHA
            "[M] digest 3c9a1f7b60d4e825ab73cf0591d6e4820b7fa3c1"
            "95e8d470a62bf1039c7d5e84\n"                              # sha256
            "[O] transfer errors 00000000\n"   # one digit repeated
            "[O] clkn 0012abcd\n"              # too narrow, and in no serial
            # Wide enough to be masked by width, but all digits or all
            # letters, which a serial that wide is not.
            "[O] packets 123456789012345678901234\n"
            "[O] bits 010101010101010101010101\n"
            "[O] field ffffffffffffffffffffffff\n"
        )

        self.assertEqual(kept, _redact(kept, {serial}))

    def test_a_piece_a_known_serial_holds_is_masked_whatever_it_looks_like(self):
        # The shape test is for a board the run never named. A serial it does
        # name settles the question by itself, so a piece of one is masked
        # even where it reads as something else.
        from ubertooth_harness.report import _redact

        self.assertEqual(
            "[O] boards: <serial>\n",
            _redact("[O] boards: 00000000\n", {"0000000012ab34cd"}))

    def test_the_widths_a_piece_is_masked_at_are_bounded_at_both_ends(self):
        # What the rule leaves to read, so that widening it is a deliberate
        # change and not a passing test.
        from ubertooth_harness.report import _redact

        serial = "0123456789abcdef0123456789abcdef"
        # A piece of a known serial narrower than 8 digits.
        self.assertEqual("[O] b 0123456\n", _redact("[O] b 0123456\n", {serial}))
        # An unnamed board's serial cut to 23 digits, and a run of 36 that
        # no cap produced.
        for piece in ("1cc001038088cb956c43ad6", "1cc001038088cb956c43ad61c52000f512ab"):
            self.assertEqual(f"[O] found {piece}\n",
                             _redact(f"[O] found {piece}\n", ()), piece)

    def test_a_line_shape_a_serials_shape_holds_is_masked(self):
        # #220: the lines the record limits left out of run.jsonl are counted
        # by shape, and a shape is not the line, so masking the serial leaves
        # the shape as it is. A serial with its digits spread through it
        # leaves hex letters and their positions, which name the board.
        from ubertooth_harness.report import _redact

        serial = "1ab23cd45ef67ab89cd01ef23ab45cd6"
        shape = line_shape(serial)

        self.assertEqual("NabNcdNefNabNcdNefNabNcdN", shape)  # no 8-wide run
        self.assertEqual(
            f"  3 x [E] board {REDACTED} is idle\n",
            _redact(f"  3 x [E] board {shape} is idle\n", {serial}),
        )
        # The shape itself is held to 200 characters, so a long line's shape
        # can be cut inside the serial's; what the cut leaves is masked too.
        self.assertEqual(
            f"  1 x [E] {REDACTED}\n",
            _redact(f"  1 x [E] {shape[:12]}\n", {serial}),
        )

    def test_a_line_shape_written_against_a_word_is_masked_too(self):
        # A serial reaches the output it was cut from written straight
        # against what stood beside it (`ubertooth<serial>`, a device path),
        # so its shape arrives that way as well. The word it is written
        # against says nothing about whether the shape is there, including a
        # word that ends in a hex letter and so reads as more of the shape.
        from ubertooth_harness.report import _redact

        serial = "1ab23cd45ef67ab89cd01ef23ab45cd6"
        shape = line_shape(serial)

        for before in ("ubertooth", "/dev/cu.usbmodem", "id=Z", "0x",
                       "device", "face", "theN"):
            line = f"  1 x [E] {before}{shape} is idle\n"
            self.assertEqual(
                f"  1 x [E] {before}{REDACTED} is idle\n",
                _redact(line, {serial}), before,
            )

    def test_a_whole_shape_is_masked_however_few_letters_it_carries(self):
        # A serial with its digits in threes leaves a shape of three hex
        # letters, and three letters in known positions still name a board
        # on a small bench. A whole shape is masked for the same reason a
        # piece of a known serial is: the run naming that serial settles it.
        # What the letter count decides is whether a *piece* of a shape is
        # masked, which is all the rule has to go on for a cut shape.
        from ubertooth_harness.report import _redact

        serial = "1a2b3c45678901234567890123456789"

        self.assertEqual("NaNbNcN", line_shape(serial))
        self.assertEqual(
            f"  1 x [E] board {REDACTED} is idle\n",
            _redact(f"  1 x [E] board {line_shape(serial)} is idle\n", {serial}),
        )
        # A piece of a shape that narrow is left to read.
        self.assertEqual(
            "  1 x [E] x NabNc y\n",
            _redact("  1 x [E] x NabNc y\n",
                    {"1ab23cd45ef67ab89cd01ef23ab45cd6"}),
        )

    def test_a_loose_shape_that_begins_again_inside_itself_is_masked(self):
        # A shape looked for over the page is masked at every occurrence,
        # overlapping ones included: a bridge serial that repeats itself
        # shapes into something that begins again inside itself, and a
        # search that resumed after each match would leave the tail of the
        # second occurrence to read.
        from ubertooth_harness.report import _redact

        serial = "abc-1abc-1abc-1abc-1"
        shape = line_shape(serial)

        self.assertEqual("abc-Nabc-Nabc-Nabc-N", shape)
        for line in (f"  1 x [E] {shape}\n",
                     f"  1 x [E] abc-N{shape}\n",
                     f"  1 x [E] {shape}{shape}\n"):
            self.assertEqual(
                f"  1 x [E] {REDACTED}\n", _redact(line, {serial}), line)
        # Two occurrences with text between them are two masks.
        self.assertEqual(
            f"  1 x [E] x {REDACTED} y {REDACTED} z\n",
            _redact(f"  1 x [E] x {shape} y {shape} z\n", {serial}),
        )

    def test_a_character_that_grows_in_lower_case_does_not_shift_a_mask(self):
        # The page is searched with a pattern, not in a lower-cased copy of
        # itself: `İ` is one character that lower-cases to two, so positions
        # taken from such a copy would land late, cutting the page up and
        # publishing more of the shape with every one of them.
        from ubertooth_harness.report import _redact

        serial = "esp32-peer_1a2b"
        shape = line_shape(serial)

        for before in ("", "İ ", "İ" * 14 + "\n"):
            self.assertEqual(
                f"{before}  3 x [E] peer {REDACTED} ready\n",
                _redact(f"{before}  3 x [E] peer {shape} ready\n", {serial}),
                before,
            )

    def test_a_shape_that_repeats_itself_is_masked_whole(self):
        # A whole shape is masked before the pieces of one, so a word that
        # happens to read as more of a shape that repeats itself cannot
        # carry its start into a piece and leave the tail behind.
        from ubertooth_harness.report import _redact

        serial = "1ab2" * 8

        self.assertEqual("NabNabNabNabNabNabNabNabN", line_shape(serial))
        self.assertEqual(
            f"  1 x [E] ab{REDACTED} is idle\n",
            _redact(f"  1 x [E] ab{line_shape(serial)} is idle\n", {serial}),
        )

    def test_a_shape_of_one_character_does_not_cut_up_the_report(self):
        # A serial with one character of its own shapes to `Na`, and masking
        # that would take that letter out of every word of the report. One
        # character is where the line is drawn: such a shape is left as it
        # is, and the serial itself is still masked.
        from ubertooth_harness.report import _redact

        serial = "0000000000000000000000000000000a"
        page = (
            "  7 x [E] rx block timestamp N * N nanoseconds\n"
            "  1 x [E] isNaN NaN Nab\n"
            "| Host | a Linux host (Linux arm64) |\n"
            "  signal -70 dBm, name: Ubertooth One\n"
        )

        self.assertEqual("Na", line_shape(serial))
        self.assertEqual(page, _redact(page, {serial}))

    def test_a_serials_own_n_is_not_read_as_a_digit_placeholder(self):
        # A bridge serial may hold an N of its own, so what a shape says
        # about the board is counted from the runs of digits the serial held
        # and not from the N's in the shape, or a shape that names a board
        # reads as one that names nothing.
        from ubertooth_harness.report import _redact, _shape_own_chars

        serial = "N1a2"
        shape = line_shape(serial)

        self.assertEqual("NNaN", shape)
        self.assertEqual(2, _shape_own_chars(serial, shape))
        self.assertEqual(
            f"  1 x [E] x {REDACTED} y\n",
            _redact(f"  1 x [E] x {shape} y\n", {serial}),
        )

    def test_a_shape_no_hex_run_can_hold_is_masked_over_the_page(self):
        # An ESP32 peer's bridge serial is whatever string the bridge
        # reports, of any letters, digits and _.:+- (resources.py), so its
        # shape can hold characters no run of hex letters and N carries.
        # There is nothing to read it inside, so it is masked wherever it
        # sits; for that it has to say four characters' worth about the
        # board, since nothing else bounds where it is looked for.
        from ubertooth_harness.report import _redact

        serial = "esp32-peer_1a2b"
        shape = line_shape(serial)

        self.assertEqual("espN-peer_NaNb", shape)
        self.assertEqual(
            f"  3 x [E] peer {REDACTED} ready\n",
            _redact(f"  3 x [E] peer {shape} ready\n", {serial}),
        )
        self.assertEqual(
            f"  3 x [E] usb-{REDACTED}-if00\n",
            _redact(f"  3 x [E] usb-{shape}-if00\n", {serial}),
        )
        # Too little of its own to be looked for over a whole page: the
        # shape of `1ano2` would otherwise cut up `nanoseconds`.
        self.assertEqual(
            "  7 x [E] N nanoseconds\n",
            _redact("  7 x [E] N nanoseconds\n", {"1ano2"}),
        )

    def test_a_serial_or_shape_in_another_case_is_masked(self):
        # A tool prints in the case it likes, and the registry holds one.
        # The 32-hex token rule reaches a serial standing on its own, so
        # what needs the case to be ignored is one written against
        # something else.
        from ubertooth_harness.report import _redact

        serial = "1ab23cd45ef67ab89cd01ef23ab45cd6"
        serials = {serial, serial.lower()}  # what _Run.serials() collects

        self.assertEqual(
            f"[O] face{REDACTED} is idle\n",
            _redact(f"[O] face{serial.upper()} is idle\n", serials),
        )
        self.assertEqual(
            f"  1 x [E] {REDACTED}\n",
            _redact(f"  1 x [E] {line_shape(serial).upper()}\n", serials),
        )

    def test_a_line_shape_no_serials_shape_holds_is_left_to_read(self):
        # What the shape rule leaves readable, which is what the shapes are
        # in the report for.
        from ubertooth_harness.report import _redact

        serial = "1ab23cd45ef67ab89cd01ef23ab45cd6"
        kept = (
            "  7 x [E] rx block timestamp N * N nanoseconds\n"
            "  4 x [O] Serial No: N\n"           # a numeric serial's own shape
            "  2 x [E] systime=N ch=N LAP=N err=N\n"
            "  1 x [E] Nab is idle\n"            # too few letters to name a board
            "  1 x [E] deadbeef cafe\n"          # no N, so no shape token
            "  1 x [E] isNaN NaN banana\n"       # words a tool prints
            "  1 x [E] face deadN beefN\n"       # in no known serial's shape
        )

        self.assertEqual(kept, _redact(kept, {serial, "001050210121"}))

    def test_the_shape_summary_of_left_out_output_is_masked(self):
        # End to end: a command whose output passed the record limits leaves
        # the lines between the head and the tail counted by shape, and the
        # [M] note lists them.
        serial = "1ab23cd45ef67ab89cd01ef23ab45cd6"
        shape = line_shape(serial)
        line = f"board {serial} is idle\n"
        events = [ubertooth_manifest(serial)] + capped_events(line * 4000)

        plain = render_run(events)
        report = render_run(events, redact_serials=True)

        self.assertIn(f"x [E] board {shape} is idle", plain)
        self.assertNotIn(shape, report)
        self.assertIn(f"x [E] board {REDACTED} is idle", report)
        self.assertNotIn(serial, report)

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
    def test_an_outcome_for_another_step_leaves_the_open_one_open(self):
        # Records are read as they were written: an outcome naming a step
        # other than the open one closes nothing, and the open step still
        # gets the line its commands give it.
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("build_started", "running", {})
            publisher.emit("build_step_started", "running", {
                "step_id": "host-build", "step_name": "Build Ubertooth host tools",
            })
            publisher.emit("build_step_finished", "passed", {
                "step_id": "toolchain", "step_name": "Read toolchain versions",
                "reason": "",
            })
            publisher.emit("build_finished", "passed", {"reason": ""})

            report = render_run(load_events(run / "run.jsonl"))

            self.assertIn("--- Read toolchain versions EXIT: passed ---", report)
            self.assertIn("--- Build Ubertooth host tools EXIT: passed ---", report)

    def test_a_build_step_with_no_outcome_record_falls_back_to_its_commands(self):
        # A run recorded before the build said how each step ended has only
        # its commands to go on, so the step's line is latched to the first
        # command that did not pass.
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            publisher = open_run_publisher(run, "run-a")
            publisher.emit("build_started", "running", {})
            publisher.emit("build_step_started", "running", {
                "step_id": "host-build", "step_name": "Build Ubertooth host tools",
            })
            publisher.emit("command_started", "running", {
                "command_id": "b", "argv": ["cmake", "--build", "."],
                "phase": "build", "step_id": "host-build",
            })
            publisher.emit("command_finished", "failed", {
                "command_id": "b", "returncode": 2, "duration_s": 0.1,
                "phase": "build", "step_id": "host-build",
            })
            publisher.emit("build_finished", "failed", {"reason": "it exited 2"})

            report = render_run(load_events(run / "run.jsonl"))

            self.assertIn("--- Build Ubertooth host tools EXIT: failed ---", report)
            self.assertIn("=== BUILD EXIT: failed: it exited 2 ===", report)

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

    def test_output_recorded_before_the_cap_is_capped_by_the_report(self):
        # A run recorded before run.jsonl capped output at write time holds
        # every line, and rendering it printed megabytes. The report keeps
        # the start and the end, and says how much is only in run.jsonl.
        line = "rx block timestamp 283640 * 100 nanoseconds\n"
        records = 5000
        report = render_run(dump_events([line] * records + ["the end\n"]))
        printed = len(line) * records + len("the end\n")

        # Many small records: the record limits bind, because each costs a
        # timestamp and a tag of its own in the transcript.
        lines = LEGACY_OUTPUT_HEAD_RECORDS + LEGACY_OUTPUT_TAIL_RECORDS - 1
        self.assertEqual(lines, report.count(line.rstrip()))
        self.assertIn("[E] the end", report)  # how the command ended
        self.assertEqual(
            printed - (lines * len(line) + len("the end\n")), gap_in(report),
        )
        # The gap note is stamped where the output left out ends, as the note
        # a capped command leaves is: the last record before the tail, one
        # record per millisecond here.
        ends = records + 1 - LEGACY_OUTPUT_TAIL_RECORDS
        self.assertIn(f"{elapsed_stamp(ends)} [M] {gap_in(report)} more", report)

    def test_a_few_long_records_are_capped_by_size(self):
        line = "x" * 63 + "\n"
        record = line * 64  # 4 KiB, as a non-streaming command is recorded in
        records = 40
        report = render_run(dump_events([record] * records + ["the end\n"]))
        printed = len(record) * records + len("the end\n")

        self.assertLess(report.count(line.rstrip()) * len(line),
                        LEGACY_OUTPUT_HEAD_CHARS + LEGACY_OUTPUT_TAIL_CHARS)
        self.assertIn("[E] the end", report)
        # Everything but the start and the end of the output, give or take
        # the line the cap fell inside.
        self.assertAlmostEqual(
            printed - LEGACY_OUTPUT_HEAD_CHARS - LEGACY_OUTPUT_TAIL_CHARS,
            gap_in(report), delta=len(line),
        )
        # The end starts on a whole line, as run.jsonl's own tail does. (The
        # start may stop inside one, as run.jsonl's head does: it is bounded
        # by size and records, not by lines.)
        after = report.split("more characters of output are in run.jsonl\n", 1)[1]
        for text in after.splitlines():
            if "[E] x" in text:
                self.assertTrue(text.endswith(line.rstrip()), text)

    def test_the_cap_cuts_a_record_where_run_jsonl_would(self):
        chunk = [(_START, "stderr", "aaa\nbbb\nccc\n")]

        def took(**limits):
            taken, left_out = _take_chunks(chunk, **limits)
            kept = _tail_on_whole_lines(left_out, taken) if limits["from_end"] else taken
            return [text for _moment, _stream, text in kept]

        # From the end: a cut on a line boundary keeps that whole line, and
        # one inside a line starts at the next.
        self.assertEqual(["ccc\n"], took(chars=4, records=1, from_end=True))
        self.assertEqual(["ccc\n"], took(chars=6, records=1, from_end=True))
        # From the start: whole lines, or the fragment that fits when the
        # record holds no line boundary at all.
        self.assertEqual(["aaa\n"], took(chars=6, records=1, from_end=False))
        self.assertEqual(["aa"], [
            text for _moment, _stream, text in _take_chunks(
                [(_START, "stderr", "a" * 100)], chars=2, records=1, from_end=False,
            )[0]
        ])

    def test_a_left_out_line_running_through_several_records_is_dropped(self):
        # The line left out can run through more than one of the tail's
        # records; none of its pieces may render as a line of its own.
        texts = (["x\n"] * LEGACY_OUTPUT_HEAD_RECORDS
                 + ["this line was left out"]
                 + ["part one ", "part two\n"]
                 + ["y\n"] * (LEGACY_OUTPUT_TAIL_RECORDS - 2))

        report = render_run(dump_events(texts))

        printed = [text.split("[E] ", 1)[1] for text in report.splitlines()
                   if "[E] " in text]
        self.assertEqual([], [text for text in printed if "part" in text])
        self.assertEqual(["y"] * (LEGACY_OUTPUT_TAIL_RECORDS - 2),
                         printed[LEGACY_OUTPUT_HEAD_RECORDS:])
        # The gap is stamped at the last record left out, which is the
        # "part two" the line boundary dropped, not the record before it.
        ends = LEGACY_OUTPUT_HEAD_RECORDS + 3  # one record per millisecond
        self.assertIn(f"{elapsed_stamp(ends)} [M] {gap_in(report)} more", report)

    def test_a_tail_that_is_all_one_line_shows_the_gap_and_no_more(self):
        # Records holding the middle of one line leave nothing once the tail
        # starts at a line boundary, as run.jsonl's tail does: a fragment
        # rendered as a line would read as a line the command printed.
        report = render_run(dump_events(["a" * 70000, "b" * 9000, "c" * 10999 + "\n"]))

        printed = [text for text in report.splitlines() if "[E] " in text]
        self.assertEqual(1, len(printed))  # the head's own cut line
        self.assertEqual("a" * LEGACY_OUTPUT_HEAD_CHARS,
                         printed[0].split("[E] ", 1)[1])
        self.assertEqual(90000 - LEGACY_OUTPUT_HEAD_CHARS, gap_in(report))

    def test_the_harness_own_last_word_survives_the_cap(self):
        # A timeout, a cancellation or an output sink error begins on a line
        # of its own (the executor prefixes it), so the tail keeps it.
        report = render_run(dump_events(
            ["noise\n" * 20000, "\nHARD TIMEOUT after 60s\n"]
        ))

        self.assertIn("more characters of output are in run.jsonl", report)
        self.assertIn("[E] HARD TIMEOUT after 60s", report)

    def test_one_empty_record_over_the_limit_leaves_the_output_whole(self):
        # The record limits are reached but nothing is left out, so there is
        # no gap to report.
        texts = (["line\n"] * LEGACY_OUTPUT_HEAD_RECORDS + [""]
                 + ["line\n"] * LEGACY_OUTPUT_TAIL_RECORDS)

        report = render_run(dump_events(texts))

        self.assertEqual(len(texts) - 1, report.count("[E] line"))
        self.assertNotIn("more characters of output", report)

    def test_the_tail_does_not_start_inside_a_left_out_line(self):
        # A record boundary is not a line boundary: with the record limit
        # reached, the first record of the tail can carry the end of a line
        # whose start was left out.
        texts = []
        for index in range(125):
            texts += [f"line {index} ", f"end {index}\n"]
        texts.append("the end\n")  # 251 records, past the record limits

        report = render_run(dump_events(texts))

        after = report.split("more characters of output are in run.jsonl\n", 1)[1]
        printed = [text.split(" ", 1)[1] for text in after.splitlines()
                   if "[E] " in text]
        self.assertEqual("[E] the end", printed[-1])
        for text in printed[:-1]:
            self.assertRegex(text, r"^\[E\] line \d+ end \d+$")

    def test_the_cap_keeps_both_streams_in_arrival_order(self):
        events = [command_event(0, "command_started", "running",
                                {"argv": ["ubertooth-dump"]})]
        for index in range(3000):
            for stream in ("stdout", "stderr"):
                events.append(command_event(
                    len(events), "command_output", "running",
                    {"stream": stream, "text": f"{stream} {index}\n"},
                ))
        events.append(command_event(len(events), "command_finished", "passed",
                                    {"returncode": 0, "duration_s": 1.0}))

        report = render_run(events)

        transcript = [text for text in report.splitlines()
                      if "[O] " in text or "[E] " in text]
        self.assertEqual("[O] stdout 0", transcript[0].split(" ", 1)[1])
        self.assertEqual("[E] stderr 0", transcript[1].split(" ", 1)[1])
        self.assertEqual("[E] stderr 2999", transcript[-1].split(" ", 1)[1])

    def test_output_capped_when_it_was_recorded_is_shown_as_recorded(self):
        # The records a capped command leaves are already the start and the
        # end of its output, with its own [M] note between them: capping
        # them again would cut output the run deliberately kept.
        for named in ({"output_file": "output/c.log"},
                      {"output_files": [{"path": "output/c.stderr.log"}]}):
            with tempfile.TemporaryDirectory() as directory, self.subTest(**named):
                run = Path(directory) / "run-a"
                publisher = open_run_publisher(run, "run-a")
                publisher.emit("command_started", "running", {
                    "command_id": "c", "argv": ["ubertooth-dump"],
                })
                chunk = "x" * 1000 + "\n"
                for _ in range(100):
                    publisher.emit("command_output", "running", {
                        "command_id": "c", "stream": "stderr", "text": chunk,
                    })
                publisher.emit("command_output_omitted", "running", {
                    "command_id": "c", **named,
                    "output_omitted": {"lines": {"stderr": 7}, "chars": {"stderr": 70}},
                })
                publisher.emit("command_output", "running", {
                    "command_id": "c", "stream": "stderr", "text": "the end\n",
                })
                publisher.emit("command_finished", "passed", {
                    "command_id": "c", "returncode": 0, "duration_s": 9.0, **named,
                })

                report = render_run(load_events(run / "run.jsonl"))

                self.assertEqual(100, report.count(chunk.rstrip()))
                self.assertNotIn("more characters of output are in run.jsonl", report)
                self.assertIn("7 stderr lines (70 characters)", report)

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
            # Unmasked, the page stays a summary: each run's own report.md
            # carries its transcript with its serials in it.
            self.assertNotIn("## Transcript", report)

    def test_a_redacted_report_carries_each_runs_transcript(self):
        # #221: --redact-serials means the whole report, masked, and the
        # transcript is most of what a report is worth to a reader. It is
        # also where the serials the rules work hardest on appear: in a
        # command line, in a device path, in a board's answer.
        with tempfile.TemporaryDirectory() as directory:
            mac = Path(directory) / "mac-run"
            linux = Path(directory) / "linux-run"
            write_run(mac, host="Host_Mac")
            write_run(linux, host="Host_Linux")

            report = render_runs([mac, linux], redact_serials=True)

            self.assertEqual(2, report.count("### Transcript"))
            self.assertIn(f"[C] ubertooth-util -U {REDACTED} -s", report)
            self.assertIn(f"[O] Serial No: {REDACTED}", report)
            self.assertNotIn(SERIAL, report)
            # Each run's own sections sit under that run's heading, so a
            # reader scrolling through two transcripts can see which is which.
            self.assertIn("## Run `mac-run`", report)
            self.assertIn("### Results", report)
            self.assertNotIn("\n## Results\n", report)

    def test_a_runs_subsections_move_down_with_their_section(self):
        # Everything a run wrote moves a level, so a section that has
        # subsections (the watchdog's issue to file, a file's checks) keeps
        # them under it rather than beside it. What a command printed and
        # what a board answered stay as they are: a fenced block is quoted
        # output, and a reader may copy it out.
        from ubertooth_harness.report import _as_a_section

        page = (
            "# Ubertooth test run `run-a`\n\n## Watchdog\n\n"
            "### Issue to file on the fork\n\n"
            "````text\nTitle: a reset\n\n## the board printed this\n"
            "```text\n### and this\n```\n````\n"
        )

        self.assertEqual(
            "## Run `run-a`\n\n### Watchdog\n\n"
            "#### Issue to file on the fork\n\n"
            "````text\nTitle: a reset\n\n## the board printed this\n"
            "```text\n### and this\n```\n````\n",
            _as_a_section(page),
        )

    def test_one_runs_board_is_masked_in_the_others_transcript(self):
        # The page is masked once with every run's serials, not run by run:
        # the macOS and Linux runs of a release may be taken on one bench,
        # and a board one run names can be named in the other's output.
        with tempfile.TemporaryDirectory() as directory:
            mac = Path(directory) / "mac-run"
            linux = Path(directory) / "linux-run"
            write_run(mac, host="Host_Mac")
            write_run(linux, host="Host_Linux", dk_serial="001050210121")
            publisher = open_run_publisher(mac, "mac-run")
            publisher.emit("command_started", "running", {
                "command_id": "x", "argv": ["nrfdk.py", "--jlink", "1050210121"],
            })
            publisher.emit("command_finished", "passed", {
                "command_id": "x", "returncode": 0, "duration_s": 0.1,
            })

            report = render_runs([mac, linux], redact_serials=True)

            self.assertIn(f"[C] nrfdk.py --jlink {REDACTED}", report)
            self.assertNotIn("1050210121", report)

    def test_records_are_valid_json_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            write_run(run)

            lines = (run / "run.jsonl").read_text().splitlines()

            self.assertEqual("run_started", json.loads(lines[0])["event_type"])
            self.assertEqual("run_finished", json.loads(lines[-1])["event_type"])


if __name__ == "__main__":
    unittest.main()
