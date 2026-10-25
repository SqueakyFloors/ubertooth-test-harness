import io
import os
import re
import shlex
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ubertooth_harness import dfu_cases, dfuimage
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.manifest import FileIdentity, SourceRevision, build_run_manifest
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.report import load_events, render_run
from ubertooth_harness.preflight import PreflightCheck, PreflightError, PreflightPolicy
from ubertooth_harness.sequencer import NamedTestSequencer
from tests.test_sequencer import CollectSink

SERIAL = "abc123"
GUARD = "def456"
REVISION = "git-test"


def signed(payload: bytes, vendor=dfuimage.VENDOR, product=dfuimage.PRODUCT) -> bytes:
    """payload plus the suffix ubertooth-dfu sign() writes."""
    body = payload + struct.pack("<HHHH3sB", 0, product, vendor, dfuimage.BCD_DFU,
                                 dfuimage.SIGNATURE, dfuimage.SUFFIX_LENGTH)
    return body + struct.pack("<I", dfuimage.dfu_crc(body))


def firmware(revision=REVISION, size=3000) -> bytes:
    marker = f"ubertooth {revision} (builder@host) timestamp\0".encode()
    return signed(bytes(range(256)) * (size // 256) + marker)


class FakeDfuBoard:
    """ubertooth-util -v and ubertooth-dfu -s/-d/-u/-r, following ubertooth-dfu.c.

    By default it follows fork main 6fee30c without #267: a bad -d image is
    refused before any device is opened. Flags switch on the fork bugs the
    cases catch (the 3b22c95 behavior), or other failures: ``stuck_on_reject``
    (#198: refused only after entering DFU, and left there), ``accept_wrong_id``
    (#199), ``double_suffix`` and ``needs_write_access`` (#200),
    ``detach_exit_1`` (#267: -d -r exits 1 after a good flash), and ``bad_readback``,
    ``partial_write_on_reject``, ``reject_exit_zero``, ``stays_in_dfu_after_upload``,
    ``wrong_sign_vendor``, ``dfu_count_extra`` for failures no fork has shown.
    """

    def __init__(self, image: bytes, **flags):
        self.flags = flags
        self.mode = "app"
        self.flash = bytearray(b"\xff" * dfuimage.APPLICATION_SIZE)
        self._program(image)
        self.revision = REVISION
        self.commands = []
        self.now = 0.0
        self.selected_uploads = 0

    # -- helpers -------------------------------------------------------
    def _program(self, data: bytes):
        padded = -(-len(data) // 256) * 256
        self.flash[:padded] = data + b"\xff" * (padded - len(data))

    def _board(self, serial, guards=()):
        def run(argv, timeout_s):
            return self.run(argv, timeout_s=timeout_s)

        def sleep(seconds):
            self.now += seconds

        return dfuimage.Board(
            serial, guards, run=run,
            dfu_count=lambda: (self.mode == "dfu") + self.flags.get("dfu_count_extra", 0),
            sleep=sleep, clock=lambda: self.now, log=print,
        )

    @staticmethod
    def _result(argv, code, stdout="", stderr=""):
        return CommandResult(tuple(argv), code, stdout, stderr, 0.01, False)

    # -- executor ------------------------------------------------------
    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if command == ("ubertooth-util", "-H"):
            return self._result(command, 0, "libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n")
        self.commands.append(command)
        if command[:2] == (sys.executable, dfu_cases._CHECKER):
            out = io.StringIO()
            with redirect_stdout(out):
                code = dfuimage.main(list(command[2:]), board_factory=self._board)
            return self._result(command, code, out.getvalue())
        if command[0] == "ubertooth-util":
            serial = command[command.index("-U") + 1]
            if serial == GUARD:
                return self._result(command, 0, "Firmware version: git-guard (API:1.10)\n")
            if serial != SERIAL or self.mode != "app":
                return self._result(command, 1, "", f"no Ubertooth found with serial matching '{serial}'\n")
            return self._result(command, 0, f"Firmware version: {self.revision} (API:1.10)\n")
        assert command[0] == "ubertooth-dfu", command
        return self._dfu(command)

    def _dfu(self, command):
        options = list(command[1:])
        if "-s" in options:
            return self._sign(command, Path(options[options.index("-s") + 1]))
        selected = options[options.index("-U") + 1] if "-U" in options else None
        assert selected in (None, SERIAL), command
        if selected is None:
            if self.mode != "dfu":
                # Never reached by the harness: it would pick the default board.
                raise AssertionError(f"no-selector DFU command with no DFU device: {command}")
        elif self.mode != "app":
            return self._result(command, 1, "", "unable to open selected application device\n")
        if "-d" in options:
            path = Path(options[options.index("-d") + 1])
            if not os.access(path, os.R_OK | (os.W_OK if self.flags.get("needs_write_access") else 0)):
                return self._result(command, 1, "", f"{path}: Permission denied\n")
            # A partial write needs the old path that refused only in DFU.
            if not (self.flags.get("stuck_on_reject") or self.flags.get("partial_write_on_reject")):
                # check_download_image() before any device is opened (e8edd15).
                refusal = self._refusal(path.read_bytes(), "-r" in options)
                if refusal:
                    code = 0 if self.flags.get("reject_exit_zero") else 1
                    return self._result(command, code, "Checking firmware signature\n", refusal)
        stderr = "DFU +0.000s: operation started\n"
        self.mode = "dfu"
        if "-u" in options:
            if selected is not None:
                self.selected_uploads += 1
                if self.selected_uploads in self.flags.get("failing_uploads", ()):
                    # upload() fails and main() skips the -r detach (fork #208).
                    Path(options[options.index("-u") + 1]).write_bytes(b"\xff" * 512)
                    return self._result(command, 157, "....\n",
                                        stderr + "LIBUSB_ERROR_OTHER\nFirmware upload failed\n")
            Path(options[options.index("-u") + 1]).write_bytes(self._read())
        if "-d" in options:
            data = path.read_bytes()
            refusal = self._refusal(data, "-r" in options)
            if refusal:
                if self.flags.get("partial_write_on_reject"):
                    # Sector 0x4000 erased and its first block written.
                    block = data[:256]
                    self.flash[:4096] = block + b"\xff" * (4096 - len(block))
                code = 0 if self.flags.get("reject_exit_zero") else 1
                return self._result(command, code, "Switching selected device to DFU mode...\n",
                                    stderr + refusal)
            stderr += "DFU +0.100s: firmware programming attempt 1 started\n"
            self._program(data)
            self.revision = self._marker(data)
        if "-r" in options:
            if not (self.flags.get("stays_in_dfu_after_upload") and "-u" in options):
                self.mode = "app"
            if "-d" in options:
                if self.flags.get("detach_exit_1"):
                    return self._result(command, 1, "", stderr + "error: control message unsupported\n"
                                        "Detach failed: could not enter DFU mode\n")
                stderr += f"Verified firmware revision: {self.revision}\n"
        return self._result(command, 0, "", stderr)

    def _read(self):
        data = bytearray(self.flash)
        if self.flags.get("bad_readback"):
            data[100] ^= 0xFF
        return bytes(data)

    @staticmethod
    def _marker(data):
        start = data.find(b"ubertooth ") + len(b"ubertooth ")
        end = data.find(b" (", start)
        return data[start:end].decode() if start >= len(b"ubertooth ") and end > start else None

    def _refusal(self, data, reset):
        """check_suffix() order and messages, then the -r revision marker."""
        if len(data) < 16:
            return "Error: could not read 16-byte DFU suffix (file truncated?)\n"
        _dev, product, vendor, bcd, sig, length, crc = struct.unpack("<HHHH3sBI", data[-16:])
        refusal = (
            f"Unknown DFU suffix length: {length}\n" if length != 16
            else f"Unknown DFU version: {bcd:04x}\n" if bcd not in (0x0100, 0x0101)
            else "DFU Signature mismatch: not a DFU file\n" if sig != b"UFD"
            else "CRC mismatch: calculated: 0x0, found: 0x1\n" if crc != dfuimage.dfu_crc(data[:-4])
            else f"DFU suffix is for USB vendor 0x{vendor:04x}, not 0x1d50: "
            "this image is for another device\n" if vendor != 0x1D50
            and not self.flags.get("accept_wrong_id")
            else f"DFU suffix is for USB product 0x{product:04x}, not 0x6003: "
            "this image is for another device\n" if product != 0x6003
            and not self.flags.get("accept_wrong_id")
            else ""
        )
        if refusal:
            return refusal + "Signature check failed, firmware will not be updated\n"
        if reset and self._marker(data) is None:
            return "Unable to determine expected firmware revision from image\n"
        return ""

    def _sign(self, command, path):
        if path.suffix != ".bin":
            return self._result(command, 1, "Error: -s requires a .bin\n")
        if self.flags.get("needs_write_access") and not os.access(path, os.W_OK):
            return self._result(command, 1, "", f"{path}: Permission denied\n")
        data = path.read_bytes()
        if not self.flags.get("double_suffix") and not dfuimage.suffix_problems(data):
            return self._result(command, 1, "", f"error: {path} already has a DFU suffix, "
                                f"so {path.with_suffix('.dfu')} was not written\n")
        vendor = 0x1234 if self.flags.get("wrong_sign_vendor") else dfuimage.VENDOR
        path.with_suffix(".dfu").write_bytes(signed(data, vendor=vendor))
        return self._result(command, 0)


# Fork 3b22c95, before the #198/#199/#200 fixes.
OLD_FORK = dict(stuck_on_reject=True, accept_wrong_id=True, double_suffix=True,
                needs_write_access=True)
# Fork main 6fee30c: those fixed, #267 open.
FORK_MAIN = dict(detach_exit_1=True)
CORRECT = dict()


def identities(guard=False):
    found = {"Ubertooth": {"serial": SERIAL}}
    if guard:
        found["guard Ubertooth 1"] = {"serial": GUARD}
    return found


def cases_for(coverage="smoke", guard=False):
    definition = builtin_catalog().get("DFU images")
    arguments = definition.normalize_arguments(
        {"hosts": ["Host_Linux"], "Ubertooth": "default", "coverage": coverage})
    return definition.command_cases(arguments, {"Ubertooth": "UT_Linux0"}, identities(guard))


def run_dfu(board, image_bytes, *, coverage="smoke", artifact=True, change_image=False):
    definition = builtin_catalog().get("DFU images")
    arguments = definition.normalize_arguments(
        {"hosts": ["Host_Linux"], "Ubertooth": "default", "coverage": coverage})
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        image = root / "bluetooth_rxtx.dfu"
        image.write_bytes(image_bytes)
        (root / "plan.yaml").write_text("plan\n")
        (root / "hardware.yaml").write_text("hardware\n")
        invocation = TestInvocation("001-dfu", "DFU images", "Host_Linux", arguments,
                                    {"Ubertooth": "UT_Linux0"}, identities())
        manifest = build_run_manifest(
            run_id="named-run", plan_name="named", suite="fast", host="Host_Linux",
            plan_path=root / "plan.yaml", hardware_path=root / "hardware.yaml",
            sources=(SourceRevision("Ubertooth", "/source", "https://example.invalid/u.git",
                                    "test-ref", "a" * 40, "test"),),
            invocations=(invocation,),
            artifacts=(FileIdentity.from_path("test build", image),) if artifact else (),
            created_at="2026-10-22T00:00:00+00:00",
        )
        check = PreflightCheck("preflight-001-host-version", "Host version", "Host_Linux",
                               None, {}, ("ubertooth-util", "-H"),
                               (r"libubertooth\s+\S+\s+\(git\-test\)",), REVISION)
        if change_image:
            original_run = board.run

            def run(argv, **kwargs):
                if tuple(argv) == ("ubertooth-util", "-H"):
                    image.write_bytes(image_bytes + b"x")
                return original_run(argv, **kwargs)
            board.run = run
        sink = CollectSink()
        summary = NamedTestSequencer(board, event_sink=sink).run(
            (invocation,), root / "run", run_id="named-run", manifest=manifest,
            preflight_checks=(check,))
        # The report.md the run would get, rendered before the run dir goes.
        board.report = render_run(load_events(root / "run" / "run.jsonl"))
        return summary, sink.events


def failed(events):
    return sorted(e.details["case_id"].removeprefix("001-dfu-") for e in events
                  if e.event_type == "check_finished" and e.status == "failed"
                  and e.details.get("phase") == "test")


ALL_REJECTS = [f"reject-{kind}" for kind in dfuimage.BAD_KINDS]


class DfuImageFileTests(unittest.TestCase):

    def test_every_bad_kind_is_invalid_and_the_source_valid(self):
        image = firmware()
        self.assertEqual([], dfuimage.suffix_problems(image))
        for kind in dfuimage.BAD_KINDS:
            with self.subTest(kind):
                self.assertNotEqual([], dfuimage.suffix_problems(dfuimage.make_bad(kind, image)))
        vendor = dfuimage.suffix_problems(dfuimage.make_bad("wrong-vendor", image))
        self.assertEqual(["VID:PID 1234:6003"], vendor)  # CRC still valid
        self.assertEqual(["signature b'XYZ'"],
                         dfuimage.suffix_problems(dfuimage.make_bad("bad-signature", image)))

    def test_suffix_layout_matches_the_tool(self):
        # The 2020-12-R1 style suffix: 00 00 03 60 50 1d 00 01 'UFD' 10 crc.
        tail = signed(b"")[:12]
        self.assertEqual(bytes.fromhex("0000036050 1d0001 554644 10".replace(" ", "")), tail)

    def test_readback_comparison(self):
        image = firmware()
        board = FakeDfuBoard(image)
        self.assertEqual([], dfuimage.readback_problems(board._read(), image))
        board.flags["bad_readback"] = True
        self.assertIn("differs from the image at 0x04064",
                      dfuimage.readback_problems(board._read(), image)[0])
        padded = bytearray(FakeDfuBoard(image)._read())
        padded[len(image)] = 0
        self.assertIn("padding", dfuimage.readback_problems(bytes(padded), image)[0])
        self.assertIn("bytes, expected 245760", dfuimage.readback_problems(image, image)[0])

    def test_leave_dfu_refuses_without_a_unique_dfu_device_or_with_a_silent_guard(self):
        board = FakeDfuBoard(firmware(), dfu_count_extra=1)
        board.mode = "dfu"
        with redirect_stdout(io.StringIO()):
            self.assertIn("2 DFU devices", board._board(SERIAL).leave_dfu(Path("/nonexistent")))
        board = FakeDfuBoard(firmware())
        board.mode = "dfu"
        helper = board._board(SERIAL, ("silent",))
        with redirect_stdout(io.StringIO()):
            self.assertIn("guard board silent does not answer", helper.leave_dfu(Path("/x")))
        self.assertEqual("dfu", board.mode)
        self.assertFalse(any(c[0] == "ubertooth-dfu" for c in board.commands))

    def test_leave_dfu_never_writes(self):
        board = FakeDfuBoard(firmware())
        board.mode = "dfu"
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            self.assertEqual("", board._board(SERIAL, (GUARD,)).leave_dfu(Path(d) / "r.bin"))
        dfu = [c for c in board.commands if c[0] == "ubertooth-dfu"]
        self.assertEqual([("ubertooth-dfu", "-u", dfu[0][2], "-r")], dfu)
        self.assertEqual("app", board.mode)


class DfuCaseShapeTests(unittest.TestCase):

    def test_smoke_writes_first_then_suffix_comprehensive_adds_the_rest(self):
        self.assertEqual(["write-read-back", "suffix"], [c.case_id for c in cases_for()])
        full = [c.case_id for c in cases_for("comprehensive")]
        self.assertEqual(["write-read-back", "suffix", "suffix-refuses-dfu",
                          "suffix-already-suffixed", "suffix-read-only-input"] + ALL_REJECTS, full)

    def test_device_commands_select_the_board_and_never_omit_reset(self):
        context = {"artifact test_build": "/t.dfu", "expected firmware revision": REVISION,
                   "capture dir": "/cap"}
        for case in cases_for("comprehensive", guard=True):
            for step in case.steps + case.cleanup:
                argv = step.resolve_argv(context)
                with self.subTest(case.case_id, step=step.step_id):
                    if argv[0] in ("ubertooth-util", "ubertooth-dfu") and "-s" not in argv:
                        self.assertEqual(SERIAL, argv[argv.index("-U") + 1])
                    if argv[0] == "ubertooth-dfu" and ("-d" in argv or "-u" in argv):
                        self.assertIn("-r", argv)
                    if "--serial" in argv and argv[2] in ("after-reject", "restore"):
                        self.assertEqual(GUARD, argv[argv.index("--guard") + 1])

    def test_establishes_firmware_so_it_must_be_last_on_its_board(self):
        class Registry:
            def get(self, alias):
                return type("R", (), {"type": "Ubertooth"})()

        policy = PreflightPolicy(Registry())
        dfu = TestInvocation("001", "DFU images", "H", {}, {"Ubertooth": "U"}, {})
        after = TestInvocation("002", "Firmware version", "H", {}, {"Ubertooth": "U"}, {})
        flash = TestInvocation("002", "Flash reliability", "H", {}, {"Ubertooth": "U"}, {})
        with self.assertRaises(PreflightError):
            policy.check_order((dfu, after))
        policy.check_order((after, dfu, flash))


class DfuRunTests(unittest.TestCase):

    def run_with(self, coverage="comprehensive", image=None, **flags):
        image = image or firmware()
        board = FakeDfuBoard(image, **flags)
        summary, events = run_dfu(board, image, coverage=coverage)
        return board, summary, events

    def test_passes_on_a_correct_tool_and_ends_on_the_test_image(self):
        board, summary, events = self.run_with(**CORRECT)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(("app", REVISION), (board.mode, board.revision))
        self.assertEqual([], dfuimage.readback_problems(bytes(board.flash), firmware()))

    def test_fork_main_6fee30c_fails_only_the_write_on_267(self):
        # SqueakyFloors/ubertooth#267: -d -r exits 1 after a good flash. Only
        # write-read-back writes; every cleanup finds the test image in place.
        board, summary, events = self.run_with(**FORK_MAIN)
        self.assertEqual(["write-read-back"], failed(events))
        self.assertIn("ubertooth-dfu -d <test image> -r: command exited 1", summary.failure_reason)
        self.assertEqual(("app", REVISION), (board.mode, board.revision))
        _board, smoke, events = self.run_with("smoke", **FORK_MAIN)
        self.assertEqual(["write-read-back"], failed(events))

    def test_old_fork_fails_only_the_linked_cases_and_smoke_passes(self):
        board, summary, events = self.run_with(**OLD_FORK)
        self.assertFalse(summary.passed)
        self.assertEqual(sorted(ALL_REJECTS + ["suffix-already-suffixed", "suffix-read-only-input"]),
                         failed(events))
        # Refused only after opening the device (#198); every case was
        # restored, so the whole test ran and the board is back.
        self.assertIn("operation started' was found", summary.failure_reason)
        self.assertEqual(("app", REVISION), (board.mode, board.revision))
        self.assertEqual([], dfuimage.readback_problems(bytes(board.flash), firmware()))
        _board, smoke, _events = self.run_with("smoke", **OLD_FORK)
        self.assertTrue(smoke.passed, smoke.failure_reason)

    def test_board_left_on_another_image_is_written_first(self):
        other = firmware("git-other", size=5000)
        board = FakeDfuBoard(other, **CORRECT)
        board.revision = "git-other"
        summary, _events = run_dfu(board, firmware(), coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(REVISION, board.revision)

    def test_each_failure_fails_its_case(self):
        expectations = {
            "partial_write_on_reject": ALL_REJECTS,
            "reject_exit_zero": ALL_REJECTS,
            "stays_in_dfu_after_upload": ["write-read-back"],
            "wrong_sign_vendor": ["suffix", "suffix-read-only-input"],
        }
        for flag, cases in expectations.items():
            with self.subTest(flag):
                _board, summary, events = self.run_with(**CORRECT, **{flag: True})
                self.assertFalse(summary.passed)
                self.assertEqual(sorted(cases), failed(events))

    def test_a_read_back_that_never_matches_fails_and_stops_the_run(self):
        _board, summary, events = self.run_with(**CORRECT, bad_readback=True)
        self.assertEqual(["write-read-back"], failed(events))
        self.assertIn("Read-back mismatch", summary.failure_reason)
        self.assertIn("flash still differs after the rewrite", summary.failure_reason)
        finished = [e for e in events if e.event_type == "test_finished"][0]
        self.assertTrue(finished.details["stops_run"])

    def test_a_partial_write_is_reported_and_repaired(self):
        board, summary, events = self.run_with(**CORRECT, partial_write_on_reject=True)
        # The attempt fails first (it opened the device); cleanup repairs flash.
        self.assertIn("operation started' was found", summary.failure_reason)
        self.assertEqual([], dfuimage.readback_problems(bytes(board.flash), firmware()))
        rewrites = [c for c in board.commands if c[0] == "ubertooth-dfu" and "-d" in c
                    and c[c.index("-d") + 1].endswith("bluetooth_rxtx.dfu")]
        self.assertEqual(1 + 7, len(rewrites))  # the first write, then one per reject case

    def test_a_board_stuck_with_two_dfu_devices_stops_the_run(self):
        board, summary, events = self.run_with(**OLD_FORK, dfu_count_extra=1)
        self.assertFalse(summary.passed)
        finished = [e for e in events if e.event_type == "test_finished"][0]
        self.assertTrue(finished.details["stops_run"])
        self.assertEqual("dfu", board.mode)
        self.assertIn("recover the board by hand", summary.failure_reason)
        self.assertEqual(["reject-corrupt", "suffix-already-suffixed", "suffix-read-only-input"],
                         failed(events))

    def test_a_failed_read_back_step_is_recovered_and_the_run_goes_on(self):
        board, summary, events = self.run_with("smoke", **CORRECT, failing_uploads={1})
        self.assertEqual(["write-read-back"], failed(events))
        self.assertIn("ubertooth-dfu -u readback.bin -r: command exited 157", summary.failure_reason)
        finished = [e for e in events if e.event_type == "test_finished"][0]
        self.assertFalse(finished.details["stops_run"])
        self.assertEqual(("app", REVISION), (board.mode, board.revision))

    def test_a_failed_cleanup_read_back_returns_the_board_but_stops_the_run(self):
        board, summary, events = self.run_with("smoke", **CORRECT, failing_uploads={2})
        self.assertEqual(["write-read-back"], failed(events))
        self.assertIn("read-back exited 157: Firmware upload failed; the board was left in DFU",
                      summary.failure_reason)
        finished = [e for e in events if e.event_type == "test_finished"][0]
        self.assertTrue(finished.details["stops_run"])
        self.assertEqual(("app", REVISION), (board.mode, board.revision))
        outputs = "".join(e.details.get("text", "") for e in events
                          if e.event_type == "command_output")
        self.assertIn("[E] LIBUSB_ERROR_OTHER", outputs)  # the tool's own output is kept

    def test_a_wrong_written_revision_fails(self):
        image = firmware()
        board = FakeDfuBoard(image, **CORRECT)
        original = board._marker
        board._marker = lambda data: "git-other" if data == image else original(data)
        summary, _events = run_dfu(board, image, coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertIn("expected written revision=git-test, observed git-other",
                      summary.failure_reason)

    def test_missing_test_image_is_refused_before_any_command(self):
        board = FakeDfuBoard(firmware(), **CORRECT)
        with self.assertRaisesRegex(ValueError, "pass --artifact \"test build=PATH\""):
            run_dfu(board, firmware(), artifact=False)
        self.assertEqual([], board.commands)

    def test_an_image_changed_during_the_run_fails_without_device_commands(self):
        board = FakeDfuBoard(firmware(), **CORRECT)
        summary, events = run_dfu(board, firmware(), coverage="smoke", change_image=True)
        self.assertFalse(summary.passed)
        self.assertIn("test_build image changed", summary.failure_reason)
        self.assertEqual([], [c for c in board.commands if c[0] != "ubertooth-util"
                              or "-H" not in c])
        refused = [e for e in events if e.event_type == "check_finished"
                   and e.details.get("phase") == "test"]
        self.assertEqual("not_run", refused[0].details["cleanup_status"])

    def unreadable_after(self, good_reads, **run_args):
        """Run with the test image unreadable after ``good_reads`` hashes
        (manifest creation, then its verify_files)."""
        from ubertooth_harness.manifest import RunManifest
        board = FakeDfuBoard(firmware(), **CORRECT)
        original = RunManifest.hash_file
        reads = []

        def hash_file(path):
            if Path(path).name == "bluetooth_rxtx.dfu":
                reads.append(path)
                if len(reads) > good_reads:
                    raise PermissionError(13, "Permission denied", str(path))
            return original(path)
        with mock.patch.object(RunManifest, "hash_file", staticmethod(hash_file)):
            return board, run_dfu(board, firmware(), **run_args)

    def test_an_unreadable_test_image_is_a_planning_error(self):
        with self.assertRaisesRegex(ValueError, "cannot read the test_build image: "
                                                ".*Permission denied"):
            self.unreadable_after(2)

    def test_an_image_unreadable_at_case_start_fails_with_no_cleanup(self):
        board, (summary, events) = self.unreadable_after(4, coverage="smoke")  # + one per smoke case
        self.assertFalse(summary.passed)
        self.assertIn("cannot read the test_build image", summary.failure_reason)
        refused = [e for e in events if e.event_type == "check_finished"
                   and e.details.get("phase") == "test"]
        self.assertEqual("not_run", refused[0].details["cleanup_status"])
        self.assertEqual([], [c for c in board.commands if "-H" not in c])


class DfuHelperRecordTests(unittest.TestCase):
    """Every device command the helper runs is printed as a command record."""

    def run_helper(self, board, command, guards=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "test.dfu"
            image.write_bytes(firmware())
            argv = [command, "--serial", SERIAL, "--image", str(image),
                    "--revision", REVISION, "--readback", str(root / "r.bin")]
            for guard in guards:
                argv += ["--guard", guard]
            calls = []
            original = board.run

            def run(argv, **kwargs):
                result = original(argv, **kwargs)
                calls.append(result)
                return result
            board.run = run
            out = io.StringIO()
            with redirect_stdout(out):
                code = dfuimage.main(argv, board_factory=board._board)
            return code, out.getvalue(), calls

    def assert_records(self, output, calls):
        self.assertTrue(calls)
        for result in calls:
            with self.subTest(result.argv):
                self.assertRegex(output, r"\d{4}-\d\d-\d\dT[\d:.]+ \[C\] "
                                 + re.escape(shlex.join(result.argv)) + "\n")
                tool = Path(result.argv[0]).name
                self.assertIn(f"[R] {tool} exit {result.returncode} in 0.010s", output)
                for line in (result.stdout + result.stderr).splitlines():
                    self.assertIn(line, output)
        self.assertEqual(len(calls), output.count(" [C] "))

    def test_restore_prints_every_command_and_its_response(self):
        # A rewrite path with a failed command: the board holds another image.
        board = FakeDfuBoard(firmware("git-other", size=5000), **CORRECT)
        board.revision = "git-other"
        board.flags["failing_uploads"] = {1}
        code, output, calls = self.run_helper(board, "restore", (GUARD,))
        self.assertEqual(1, code)
        self.assertIn(157, [c.returncode for c in calls])
        self.assertIn("[E] LIBUSB_ERROR_OTHER", output)
        self.assertIn("[E] Verified firmware revision: git-test", output)
        self.assertIn("[O] Firmware version: git-guard (API:1.10)", output)
        self.assert_records(output, calls)

    def test_after_reject_and_answers_print_every_command(self):
        board = FakeDfuBoard(firmware(), **FORK_MAIN)
        board.mode = "dfu"
        code, output, calls = self.run_helper(board, "after-reject", (GUARD,))
        self.assertEqual(1, code)
        self.assertIn("[E] no Ubertooth found with serial matching 'abc123'", output)
        self.assert_records(output, calls)
        board = FakeDfuBoard(firmware(), **CORRECT)
        out = io.StringIO()
        calls = []
        original = board.run
        board.run = lambda argv, **kw: calls.append(original(argv, **kw)) or calls[-1]
        with redirect_stdout(out):
            self.assertEqual(0, dfuimage.main(["answers", "--serial", SERIAL, "--revision",
                                               REVISION], board_factory=board._board))
        self.assert_records(out.getvalue(), calls)

    def test_report_shows_the_helpers_device_commands(self):
        board = FakeDfuBoard(firmware(), **CORRECT)
        summary, _events = run_dfu(board, firmware(), coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        # The restore helper's records appear as its output lines, like the
        # harness's own command records.
        self.assertRegex(board.report, r"\[O\] \d{4}-\d\d-\d\dT[\d:.]+ \[C\] ubertooth-dfu "
                                       r"-U abc123 -u \S+restore-readback\.bin -r\n")
        self.assertRegex(board.report, r"\[O\] \d{4}-\d\d-\d\dT[\d:.]+ \[R\] ubertooth-dfu "
                                       r"exit 0 in 0\.010s; \[E\] DFU \+0\.000s: operation started")
        self.assertRegex(board.report, r"\[O\] \d{4}-\d\d-\d\dT[\d:.]+ \[R\] ubertooth-util "
                                       r"exit 0 in 0\.010s; \[O\] Firmware version: git-test")

    def test_a_timed_out_command_is_recorded(self):
        lines = []
        board = dfuimage.Board(
            SERIAL, run=lambda argv, timeout: CommandResult(
                tuple(argv), -1, "partial\n", "", timeout, True),
            log=lines.append)
        self.assertIsNone(board.revision())
        record = "\n".join(lines)
        self.assertIn("[C] ubertooth-util -U abc123 -v", record)
        self.assertIn("[R] ubertooth-util exit -1 in 5.000s, TIMED OUT; [O] partial", record)

    def test_ioreg_dfu_count_is_recorded_with_device_lines_only(self):
        ioreg = (
            "+-o Root  <class IORegistryEntry>\n"
            "  +-o Ubertooth One@01100000  <class IOUSBHostDevice>\n"
            '      "idProduct" = 24579\n      "idVendor" = 7504\n'
            "  +-o Keyboard@01200000  <class IOUSBHostDevice>\n"
            '      "idProduct" = 1\n      "idVendor" = 2\n'
        )
        lines = []
        board = dfuimage.Board(
            SERIAL, run=lambda argv, timeout: CommandResult(tuple(argv), 0, ioreg, "", 0.2, False),
            log=lines.append)
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(1, board.dfu_count())
        record = "\n".join(lines)
        self.assertIn("[C] ioreg -p IOUSB -l -w0", record)
        self.assertIn("[O]   +-o Ubertooth One@01100000", record)
        self.assertNotIn("idVendor", record)
        self.assertIn("4 property lines left out", record)
        self.assertIn("[R] ioreg exit 0 in 0.200s", record)


class DfuTimeoutTests(unittest.TestCase):

    def test_worst_cases_sum_the_helpers_own_timeouts(self):
        wait = 10 + 5 + 0.5
        self.assertEqual(wait, dfuimage.worst_case_s("answers"))
        for guards in (0, 1, 3):
            leave = 10 + 5 * guards + 30
            with self.subTest(guards=guards):
                self.assertEqual(5 + 30 + 5 + leave + wait + 40 + 30 + wait,
                                 dfuimage.worst_case_s("restore", guards))
                self.assertEqual(5 + leave + wait, dfuimage.worst_case_s("after-reject", guards))
                for command in ("answers", "after-reject", "restore"):
                    self.assertGreater(dfuimage.step_timeout_s(command, guards),
                                       dfuimage.worst_case_s(command, guards) + 10)
        # The review's examples: about 135 s with one guard, 150 s with three.
        self.assertGreater(dfuimage.step_timeout_s("restore", 1), 135 * 1.25)
        self.assertGreater(dfuimage.step_timeout_s("restore", 3), 150 * 1.25)

    def test_the_slowest_restore_fits_its_bound(self):
        """Every call uses its full timeout on the longest path: a failed
        read-back that leaves DFU, then a rewrite."""
        now = [0.0]
        image = firmware()

        def run(argv, timeout):
            now[0] += timeout
            if argv[0] == "ubertooth-util":
                answers = argv[2] == GUARD or calls["util"] != 1
                calls["util"] += argv[2] == SERIAL
                return CommandResult(tuple(argv), 0 if answers else 1,
                                     f"Firmware version: {REVISION} (API:1.10)\n"
                                     if answers else "", "", timeout, False)
            path = Path(argv[argv.index("-u") + 1]) if "-u" in argv else None
            calls["dfu"] += 1
            if calls["dfu"] == 1:
                return CommandResult(tuple(argv), 157, "", "failed\n", timeout, False)
            if path is not None:
                flash = image if calls["dfu"] == 4 else b"\0" * 16
                path.write_bytes(flash + b"\xff" * (dfuimage.APPLICATION_SIZE - len(flash)))
            return CommandResult(tuple(argv), 0, "", "", timeout, False)

        def dfu_count():
            now[0] += dfuimage.DFU_COUNT_TIMEOUT_S
            return 1

        calls = {"util": 0, "dfu": 0}
        board = dfuimage.Board(SERIAL, (GUARD,), run=run, dfu_count=dfu_count,
                               sleep=lambda s: now.__setitem__(0, now[0] + s),
                               clock=lambda: now[0], log=lambda line: None)
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()) as out:
            path = Path(d) / "image.dfu"
            path.write_bytes(image)
            self.assertEqual(1, dfuimage.restore(board, path, REVISION, Path(d) / "r.bin"))
        self.assertIn("the board was left in DFU, then returned", out.getvalue())
        self.assertEqual(4, calls["dfu"])  # failed -u, -u from DFU, -d, -u
        self.assertLessEqual(now[0], dfuimage.worst_case_s("restore", 1))
        self.assertGreater(now[0], 150)

    def test_case_steps_use_the_derived_timeouts(self):
        for guard, count in ((False, 0), (True, 1)):
            for case in cases_for("comprehensive", guard=guard):
                for step in case.steps + case.cleanup:
                    command = {"restore": "restore", "after": "after-reject",
                               "returned": "answers"}.get(step.step_id)
                    if command:
                        with self.subTest(case.case_id, step=step.step_id, guard=guard):
                            self.assertEqual(dfuimage.step_timeout_s(command, count),
                                             step.timeout_s)


class DfuNitTests(unittest.TestCase):

    def test_bcd_dfu_0101_is_accepted_like_check_suffix(self):
        body = bytearray(firmware())
        struct.pack_into("<H", body, len(body) - 10, 0x0101)
        body[-4:] = struct.pack("<I", dfuimage.dfu_crc(bytes(body[:-4])))
        self.assertEqual([], dfuimage.suffix_problems(bytes(body)))
        struct.pack_into("<H", body, len(body) - 10, 0x0200)
        body[-4:] = struct.pack("<I", dfuimage.dfu_crc(bytes(body[:-4])))
        self.assertEqual(["bcdDFU 0200"], dfuimage.suffix_problems(bytes(body)))

    def test_after_reject_compares_the_revision_after_leaving_dfu(self):
        board = FakeDfuBoard(firmware(), **FORK_MAIN)
        board.mode = "dfu"
        board.revision = "git-other"
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()) as out:
            image = Path(d) / "i.dfu"
            image.write_bytes(firmware())
            code = dfuimage.after_reject(board._board(SERIAL), image, REVISION, Path(d) / "r.bin")
        self.assertEqual(1, code)
        self.assertIn("firmware revision git-other after leaving DFU, expected git-test",
                      out.getvalue())


if __name__ == "__main__":
    unittest.main()
