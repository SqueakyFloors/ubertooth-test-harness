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


def firmware(revision=REVISION, size=3000, image_check=False) -> bytes:
    """Filler carrying the firmware info block -d reads the revision from.

    ``image_check`` adds the image check block an image from the fork's
    current tree carries, which -d fills in as it writes the first block.
    """
    body = bytearray(bytes(range(256)) * (size // 256))
    # An image this tree builds leaves the vector checksum word empty: the
    # application's vector table does not fill it in and no build step does
    # either, so -d is what puts a value there.
    body[dfuimage.VECTOR_CHECKSUM_OFFSET:dfuimage.VECTOR_CHECKSUM_OFFSET + 4] = bytes(4)
    block = dfuimage.fw_info_block(revision=revision)
    body[dfuimage.FW_INFO_OFFSET:dfuimage.FW_INFO_OFFSET + len(block)] = block
    if image_check:
        offset = dfuimage.IMAGE_CHECK_OFFSET
        body[offset:offset + dfuimage.IMAGE_CHECK_LENGTH] = (
            dfuimage.IMAGE_CHECK_MAGIC
            + struct.pack("<HHIII", dfuimage.IMAGE_CHECK_FORMAT, 0, 0, 0, 0)
            + bytes(8))
    return signed(bytes(body))


# Two parts from the fork's table (host/ubertooth-tools/src/dfu_recovery.c),
# so a fake board can answer as a 128 KiB part and as a 256 KiB one.
PARTS = {128 * 1024: (0x25011722, "LPC1754"), 256 * 1024: (0x25011723, "LPC1756")}
# What a -u covers on the Ubertooth One's own part, which is also what the
# tool assumes for a board already in the bootloader.
READBACK_LENGTH = dfuimage.upload_length(128 * 1024)


def plan_line(flash_size=128 * 1024, *, identified=True) -> str:
    """plan_upload()'s line. A board already in the bootloader cannot report
    its part, so the tool assumes the size the firmware is linked for."""
    length = dfuimage.upload_length(flash_size if identified else 0)
    start = dfuimage.APPLICATION_OFFSET
    line = f"Reading back {length} bytes, 0x{start:x}-0x{start + length - 1:x}: "
    if not identified:
        return line + (f"the part could not be read, so assuming the "
                       f"{dfuimage.ASSUMED_FLASH_SIZE // 1024} KiB part this "
                       "firmware is linked for\n")
    part_id, name = PARTS[flash_size]
    return line + (f"part 0x{part_id:08x} ({name}) has "
                   f"{flash_size // 1024} KiB of flash\n")


class FakeDfuBoard:
    """ubertooth-util -v and ubertooth-dfu -s/-d/-u/-r, following ubertooth-dfu.c.

    By default it follows fork main 6fee30c without #267: a bad -d image is
    refused before any device is opened. Flags switch on the fork bugs the
    cases catch (the 3b22c95 behavior), or other failures: ``stuck_on_reject``
    (#198: refused only after entering DFU, and left there), ``accept_wrong_id``
    (#199), ``double_suffix`` and ``needs_write_access`` (#200),
    ``detach_exit_1`` (#267: -d -r exits 1 after a good flash), and ``bad_readback``,
    ``bad_vector_checksum``, ``no_plan_line`` (a -u that says nothing about
    the range it covers, as earlier builds did), ``no_first_block_fill`` (a -d
    that fills in neither field, as builds before fork 31228b0 did),
    ``odd_plan_line`` (a -u that reports a range no part has),
    ``partial_write_on_reject``,
    ``reject_exit_zero``, ``stays_in_dfu_after_upload``, ``wrong_sign_vendor``,
    ``dfu_count_extra`` for failures no fork has shown.

    ``flash_size`` is the part's whole flash, which decides how much a -u
    read-back covers and what the command says it is reading.
    """

    def __init__(self, image: bytes, flash_size=128 * 1024, *,
                 serial=None, guards=None, **flags):
        self.flags = flags
        # Which board this is and which other boards answer beside it. The
        # defaults are the short selectors these tests use; a test whose
        # registry carries real 32-hex serials passes its own.
        self.serial = (serial or SERIAL).lower()
        self.guards = {guard.lower() for guard in (guards or (GUARD,))}
        self.mode = "app"
        self.flash_size = flash_size
        self.flash = bytearray(b"\xff" * dfuimage.upload_length(flash_size))
        self._program(image)
        self.revision = REVISION
        self.commands = []
        self.now = 0.0
        self.selected_uploads = 0

    # -- helpers -------------------------------------------------------
    def _program(self, data: bytes):
        """Write ``data`` as -d does: whole blocks, the last one padded, and
        the first block's vector checksum and image check record filled in."""
        written = data if self.flags.get("no_first_block_fill") else \
            dfuimage.written_image(data)
        padded = -(-len(written) // 256) * 256
        self.flash[:padded] = written + b"\xff" * (padded - len(written))

    def plan_line(self, selected) -> str:
        """What this board's -u says it is about to read. Without a selector
        the board is already in DFU, where its part cannot be read."""
        if self.flags.get("odd_plan_line"):
            return ("Reading back 1024 bytes, 0x4000-0x43ff: part 0x25011722 "
                    "(LPC1754) has 128 KiB of flash\n")
        return plan_line(self.flash_size, identified=selected is not None)

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
        if command == ("ubertooth-util", "-V"):
            return self._result(command, 0, "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n")
        self.commands.append(command)
        if command[:2] == (sys.executable, dfu_cases._CHECKER):
            out = io.StringIO()
            with redirect_stdout(out):
                code = dfuimage.main(list(command[2:]), board_factory=self._board)
            return self._result(command, code, out.getvalue())
        if command[0] == "ubertooth-util":
            serial = command[command.index("-U") + 1].lower()
            if serial in self.guards:
                return self._result(command, 0, "Firmware version: git-guard (API:1.10)\n")
            if serial != self.serial or self.mode != "app":
                return self._result(command, 1, "", f"no Ubertooth found with serial matching '{serial}'\n")
            return self._result(command, 0, f"Firmware version: {self.revision} (API:1.10)\n")
        assert command[0] == "ubertooth-dfu", command
        return self._dfu(command)

    def _dfu(self, command):
        options = list(command[1:])
        if "-s" in options:
            return self._sign(command, Path(options[options.index("-s") + 1]))
        selected = options[options.index("-U") + 1] if "-U" in options else None
        assert selected in (None, self.serial), command
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
                refusal = self._refusal(path, path.read_bytes())
                if refusal:
                    code = 0 if self.flags.get("reject_exit_zero") else 1
                    return self._result(command, code, "Checking firmware signature\n", refusal)
        stderr = "DFU +0.000s: operation started\n"
        self.mode = "dfu"
        if "-u" in options:
            # plan_upload() says what it is about to read before it reads it,
            # so the line is there whether or not the transfer then fails.
            if not self.flags.get("no_plan_line"):
                stderr += self.plan_line(selected)
            if selected is not None:
                self.selected_uploads += 1
                if self.selected_uploads in self.flags.get("failing_uploads", ()):
                    # upload() fails and main() skips the -r detach (fork #208).
                    Path(options[options.index("-u") + 1]).write_bytes(b"\xff" * 512)
                    return self._result(command, 157, "....\n",
                                        stderr + "LIBUSB_ERROR_OTHER\nFirmware upload failed\n")
            Path(options[options.index("-u") + 1]).write_bytes(self._read(selected))
        if "-d" in options:
            data = path.read_bytes()
            refusal = self._refusal(path, data)
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
            if self.flags.get("checks_vectors") and not self.startable:
                # The bootloader will not start what is in the flash
                # (firmware/bootloader/app_image.h), so the reset leaves the
                # board in DFU and the post-reset check cannot pass.
                self.mode = "dfu"
                if "-d" in options:
                    return self._result(
                        command, 1, "", stderr + "DFU +0.300s: firmware "
                        "programming completed\n"
                        "error: the firmware was written, but the board did "
                        "not come back on USB: power-cycle it and read its "
                        "revision with ubertooth-util -V before flashing it "
                        "again\n")
            if "-d" in options:
                if self.flags.get("detach_exit_1"):
                    return self._result(command, 1, "", stderr + "error: control message unsupported\n"
                                        "Detach failed: could not enter DFU mode\n")
                stderr += f"Verified firmware revision: {self.revision}\n"
        return self._result(command, 0, "", stderr)

    @property
    def startable(self) -> bool:
        """Whether the bootloader would start what is in the flash now.

        Only consulted with ``checks_vectors``: the filler images the other
        tests write carry no realistic vector table, so modeling the check
        for all of them would refuse images those tests expect to run.
        """
        return not dfuimage.startable_problems(bytes(self.flash))

    def _read(self, selected=SERIAL):
        """The dump -u writes: as much of the region as the command asked
        for, which on a board in the bootloader is the assumed size."""
        length = dfuimage.upload_length(self.flash_size if selected is not None else 0)
        data = bytearray(self.flash[:length])
        if self.flags.get("bad_readback"):
            data[100] ^= 0xFF
        if self.flags.get("bad_vector_checksum"):
            data[dfuimage.VECTOR_CHECKSUM_OFFSET] ^= 0xFF
        return bytes(data)

    @staticmethod
    def _marker(data):
        """The revision the image names, from its firmware info block."""
        info = dfuimage.fw_info(data)
        return info.revision if info is not None else None

    def _refusal(self, path, data):
        """dfu_image_check()'s order and messages, then the firmware info
        block. Each refusal is one line: the file, the reason, and that the
        board was left alone."""
        if len(data) < 16:
            reason = f"Too short for a 16-byte DFU suffix: the file is {len(data)} byte(s)"
        else:
            _dev, product, vendor, bcd, sig, length, crc = struct.unpack(
                "<HHHH3sBI", data[-16:])
            reason = (
                f"Unknown DFU suffix length: {length}" if length != 16
                else f"Unknown DFU version: {bcd:04x}" if bcd not in (0x0100, 0x0101)
                else "DFU Signature mismatch: not a DFU file" if sig != b"UFD"
                else "CRC mismatch: calculated: 0x0, found: 0x1"
                if crc != dfuimage.dfu_crc(data[:-4])
                else f"DFU suffix is for USB vendor 0x{vendor:04x}, not 0x1d50: "
                "this image is for another device" if vendor != 0x1D50
                and not self.flags.get("accept_wrong_id")
                else f"DFU suffix is for USB product 0x{product:04x}, not 0x6003: "
                "this image is for another device" if product != 0x6003
                and not self.flags.get("accept_wrong_id")
                # The suffix is fine; a file from another tree has no block.
                else "" if self._marker(data) is not None
                else "there is no firmware info block here at offset "
                f"0x{dfuimage.FW_INFO_OFFSET:x}"
            )
        if not reason:
            return ""
        return f"error: {path}: {reason}; firmware will not be updated\n"

    def _sign(self, command, path):
        if path.suffix != ".bin":
            return self._result(command, 1, "error: -s requires a .bin\n")
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
                               None, {}, ("ubertooth-util", "-V"),
                               (r"libubertooth\s+\S+\s+\(git\-test\)",), REVISION)
        if change_image:
            original_run = board.run

            def run(argv, **kwargs):
                if tuple(argv) == ("ubertooth-util", "-V"):
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


def flash_problems(board, image=None):
    """readback_problems() over the fake's whole flash, with the range a -u
    on the selected board reports."""
    return dfuimage.readback_problems(
        bytes(board.flash), firmware() if image is None else image,
        plan=dfuimage.readback_plan(board.plan_line(SERIAL)))


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
        plan = dfuimage.readback_plan(board.plan_line(SERIAL))
        self.assertEqual([], dfuimage.readback_problems(board._read(), image, plan=plan))
        board.flags["bad_readback"] = True
        self.assertIn("differs from the image at 0x04064",
                      dfuimage.readback_problems(board._read(), image, plan=plan)[0])
        padded = bytearray(FakeDfuBoard(image)._read())
        padded[len(image)] = 0
        self.assertIn("padding",
                      dfuimage.readback_problems(bytes(padded), image, plan=plan)[0])
        self.assertIn("bytes, not the 114688 the command reported",
                      dfuimage.readback_problems(image, image, plan=plan)[0])

    def test_leave_dfu_refuses_without_a_unique_dfu_device_or_with_a_silent_guard(self):
        board = FakeDfuBoard(firmware(), dfu_count_extra=1)
        board.mode = "dfu"
        with redirect_stdout(io.StringIO()):
            self.assertIn("2 DFU devices",
                          board._board(SERIAL).leave_dfu(Path("/nonexistent")).failure)
        board = FakeDfuBoard(firmware())
        board.mode = "dfu"
        helper = board._board(SERIAL, ("silent",))
        with redirect_stdout(io.StringIO()):
            self.assertIn("guard board silent does not answer",
                          helper.leave_dfu(Path("/x")).failure)
        self.assertEqual("dfu", board.mode)
        self.assertFalse(any(c[0] == "ubertooth-dfu" for c in board.commands))

    def test_leave_dfu_never_writes(self):
        board = FakeDfuBoard(firmware())
        board.mode = "dfu"
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            left = board._board(SERIAL, (GUARD,)).leave_dfu(Path(d) / "r.bin")
        self.assertEqual("", left.failure)
        # The part cannot be read in the bootloader, so the dump is the
        # assumed length rather than the part's own.
        self.assertEqual(READBACK_LENGTH, left.plan.length)
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
                   "capture dir": "/cap",
                   dfu_cases.READBACK_RANGE_KEY: plan_line().strip()}
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
        self.assertEqual([], flash_problems(board))

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
        self.assertEqual([], flash_problems(board))
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
        self.assertEqual([], flash_problems(board))
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
                              or "-V" not in c])
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
        self.assertEqual([], [c for c in board.commands if "-V" not in c])


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
                self.assertIn(f"[X] {tool} exit {result.returncode} in 0.010s", output)
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
        # Two stderr lines, so the record puts each on its own: the DFU stage
        # and the range the read-back reported.
        self.assertRegex(board.report, r"\[O\] \d{4}-\d\d-\d\dT[\d:.]+ \[E\] DFU "
                                       r"\+0\.000s: operation started\n")
        self.assertRegex(board.report, r"\[O\] \d{4}-\d\d-\d\dT[\d:.]+ \[E\] Reading back "
                                       r"114688 bytes, 0x4000-0x1ffff: part 0x25011722 "
                                       r"\(LPC1754\) has 128 KiB of flash\n")
        self.assertRegex(board.report, r"\[O\] \d{4}-\d\d-\d\dT[\d:.]+ \[X\] ubertooth-dfu "
                                       r"exit 0 in 0\.010s\n")
        self.assertRegex(board.report, r"\[O\] \d{4}-\d\d-\d\dT[\d:.]+ \[X\] ubertooth-util "
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
        self.assertIn("[X] ubertooth-util exit -1 in 5.000s, TIMED OUT; [O] partial", record)

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
        self.assertIn("[X] ioreg exit 0 in 0.200s", record)


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
            # Every -u says what it is about to read before it reads it,
            # including the one that then fails.
            reported = plan_line(identified="-U" in argv) if path is not None else ""
            if calls["dfu"] == 1:
                return CommandResult(tuple(argv), 157, "", reported + "failed\n",
                                     timeout, False)
            if path is not None:
                flash = dfuimage.written_image(image) if calls["dfu"] == 4 else b"\0" * 16
                path.write_bytes(flash + b"\xff" * (READBACK_LENGTH - len(flash)))
            return CommandResult(tuple(argv), 0, "", reported, timeout, False)

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


class DfuWrittenImageTests(unittest.TestCase):
    """What -d fills in as it writes an image's first block (#278).

    The checks here are the criteria the board applies, not a second copy of
    the harness's arithmetic: the eight vector words have to add up to zero
    (NXP UM10360), the record has to describe this download, and the whole
    region written has to add up to zero as well
    (firmware/bootloader/app_image.h).
    """

    def test_the_vector_checksum_makes_the_eight_words_add_up_to_zero(self):
        for image_check in (False, True):
            with self.subTest(image_check=image_check):
                image = firmware(image_check=image_check)
                block = dfuimage.written_first_block(image)
                self.assertEqual(0, sum(struct.unpack_from("<8I", block)) & 0xFFFFFFFF)
                # The file leaves the word zero, so this one was filled in.
                self.assertEqual(0, struct.unpack_from(
                    "<I", image, dfuimage.VECTOR_CHECKSUM_OFFSET)[0])
                self.assertNotEqual(0, struct.unpack_from(
                    "<I", block, dfuimage.VECTOR_CHECKSUM_OFFSET)[0])

    def test_the_image_check_record_describes_the_download(self):
        image = firmware(image_check=True)
        block = dfuimage.written_first_block(image)
        blocks = dfuimage.image_block_count(image)
        flags, length, complement, checksum = struct.unpack_from(
            "<HIII", block, dfuimage.IMAGE_CHECK_OFFSET + dfuimage.IMAGE_CHECK_OFF_FLAGS)
        self.assertEqual(0x0003, flags)  # the vector check and the region check
        self.assertEqual(blocks * 256, length)  # whole blocks, the last padded
        self.assertEqual(length ^ 0xFFFFFFFF, complement)
        self.assertNotEqual(0, checksum)
        # What the download writes adds up to zero, the record included.
        region = block + b"".join(dfuimage.image_block(image, index)
                                  for index in range(1, blocks))
        self.assertEqual(blocks * 256, len(region))
        self.assertEqual(0, dfuimage.image_check_sum(region))

    def test_an_image_with_no_check_block_keeps_those_bytes(self):
        """Only an image from a tree that has the block carries it, and the
        tool writes over those 32 bytes in no other image."""
        image = firmware()
        self.assertFalse(dfuimage.carries_image_check(dfuimage.image_block(image, 0)))
        self.assertEqual((dfuimage.VECTOR_CHECKSUM_FIELD,), dfuimage.written_fields(image))
        offset = dfuimage.IMAGE_CHECK_OFFSET
        written = dfuimage.written_image(image)
        self.assertEqual(image[offset:offset + dfuimage.IMAGE_CHECK_LENGTH],
                         written[offset:offset + dfuimage.IMAGE_CHECK_LENGTH])

    def test_only_the_two_documented_ranges_differ_from_the_file(self):
        """ubertooth-dfu(1): a read-back can differ from the file only inside
        the four bytes at 0x1c and the fourteen from 0xea to 0xf8."""
        vector = set(range(0x1C, 0x20))
        record = set(range(0xEA, 0xF8))
        for image_check, allowed in ((False, vector), (True, vector | record)):
            with self.subTest(image_check=image_check):
                image = firmware(image_check=image_check)
                written = dfuimage.written_image(image)
                self.assertEqual(len(image), len(written))
                differ = {index for index, (a, b) in enumerate(zip(written, image))
                          if a != b}
                self.assertEqual(set(), differ - allowed)
                self.assertTrue(differ)  # something really was filled in

    def test_the_written_fields_are_the_fourteen_bytes_from_0xea(self):
        fields = dfuimage.written_fields(firmware(image_check=True))
        self.assertEqual((dfuimage.VECTOR_CHECKSUM_FIELD,) + dfuimage.IMAGE_CHECK_FIELDS,
                         fields)
        record = dfuimage.IMAGE_CHECK_FIELDS
        self.assertEqual(0xEA, record[0].offset)
        self.assertEqual(0xF8, record[-1].offset + record[-1].size)
        self.assertEqual(14, sum(field.size for field in record))
        # No gaps: each field starts where the one before it ended.
        self.assertEqual([field.offset for field in record[1:]],
                         [field.offset + field.size for field in record[:-1]])

    def test_a_block_in_a_format_this_reader_does_not_know_is_left_alone(self):
        """A reader that cannot read the block makes no check, which is what
        lets a bootloader tolerate an image newer than itself. The tool fills
        in nothing it cannot read either, so those bytes stay the file's."""
        body = bytearray(firmware(image_check=True))
        struct.pack_into("<H", body,
                         dfuimage.IMAGE_CHECK_OFFSET + dfuimage.IMAGE_CHECK_OFF_FORMAT, 2)
        image = signed(bytes(body[:-dfuimage.SUFFIX_LENGTH]))
        self.assertFalse(dfuimage.carries_image_check(dfuimage.image_block(image, 0)))
        self.assertEqual((dfuimage.VECTOR_CHECKSUM_FIELD,), dfuimage.written_fields(image))
        written = dfuimage.written_image(image)
        self.assertEqual(image[0xE0:0x100], written[0xE0:0x100])

    def test_an_image_that_already_carries_the_right_checksum_keeps_it(self):
        """ubertooth-dfu(1): the word is written over rather than checked, so
        an image carrying the right one is unchanged by it."""
        body = bytearray(firmware())
        block = dfuimage.image_block(bytes(body), 0)
        struct.pack_into("<I", body, dfuimage.VECTOR_CHECKSUM_OFFSET,
                         dfuimage.vector_checksum(block))
        image = signed(bytes(body[:-dfuimage.SUFFIX_LENGTH]))
        self.assertEqual([], dfuimage.suffix_problems(image))
        self.assertEqual(image, dfuimage.written_image(image))

    def test_a_short_image_has_no_field_to_compare(self):
        """The tool pads the block it writes, so a file that ends before a
        field has nothing to compare there. Such an image is refused by -d
        anyway; the comparison must not read past the file either way."""
        short = dfuimage.with_suffix(b"")
        self.assertEqual(16, len(short))
        self.assertEqual((), dfuimage.written_fields(short))
        self.assertEqual(short, dfuimage.written_image(short))
        self.assertEqual(b"", dfuimage.written_image(b""))


class DfuReadbackLengthTests(unittest.TestCase):
    """The read-back length follows the part, and comes from what the
    command reported rather than from an assumed part (#278)."""

    def test_the_part_table_gives_each_length(self):
        self.assertEqual(114688, dfuimage.upload_length(128 * 1024))
        self.assertEqual(245760, dfuimage.upload_length(256 * 1024))
        self.assertEqual(507904, dfuimage.upload_length(512 * 1024))
        # An unidentified part is read as the size the firmware is linked for.
        self.assertEqual(114688, dfuimage.upload_length(0))
        self.assertEqual((16384, 49152, 114688, 245760, 507904),
                         dfuimage.READBACK_LENGTHS)

    def test_the_reported_line_is_read_the_way_the_tool_prints_it(self):
        for line, length, part in (
            (plan_line(128 * 1024), 114688,
             "part 0x25011722 (LPC1754) has 128 KiB of flash"),
            (plan_line(256 * 1024), 245760,
             "part 0x25011723 (LPC1756) has 256 KiB of flash"),
            (plan_line(identified=False), 114688,
             "the part could not be read, so assuming the 128 KiB part this "
             "firmware is linked for"),
            # An ID the fork's table has no entry for, so the tool falls back
            # to the size the firmware is linked for.
            ("Reading back 114688 bytes, 0x4000-0x1ffff: part 0x25001122 is not "
             "known here, so assuming the 128 KiB part this firmware is linked "
             "for\n", 114688,
             "part 0x25001122 is not known here, so assuming the 128 KiB part "
             "this firmware is linked for"),
        ):
            with self.subTest(part):
                plan = dfuimage.readback_plan("DFU +0.000s: operation started\n" + line)
                self.assertEqual((length, 0x4000, 0x4000 + length - 1, part), tuple(plan))
                self.assertEqual([], plan.problems())

    def test_both_part_sizes_pass_with_the_length_the_command_reported(self):
        image = firmware(image_check=True)
        for flash_size, length in ((128 * 1024, 114688), (256 * 1024, 245760)):
            with self.subTest(flash_size=flash_size):
                board = FakeDfuBoard(image, flash_size=flash_size)
                plan = dfuimage.readback_plan(board.plan_line(SERIAL))
                dump = board._read()
                self.assertEqual((length, length), (plan.length, len(dump)))
                self.assertEqual([], dfuimage.readback_problems(dump, image, plan=plan))

    def test_neither_part_size_is_accepted_for_the_other(self):
        image = firmware(image_check=True)
        small = FakeDfuBoard(image, flash_size=128 * 1024)
        large = FakeDfuBoard(image, flash_size=256 * 1024)
        self.assertEqual(
            ["read-back is 245760 bytes, not the 114688 the command reported"],
            dfuimage.readback_problems(
                large._read(), image, plan=dfuimage.readback_plan(small.plan_line(SERIAL))))
        self.assertEqual(
            ["read-back is 114688 bytes, not the 245760 the command reported"],
            dfuimage.readback_problems(
                small._read(), image, plan=dfuimage.readback_plan(large.plan_line(SERIAL))))

    def test_a_read_back_that_reported_no_range_is_refused(self):
        image = firmware(image_check=True)
        board = FakeDfuBoard(image)
        self.assertIsNone(dfuimage.readback_plan("DFU +0.000s: operation started\n"))
        self.assertEqual(["the read-back did not report the range it covers"],
                         dfuimage.readback_problems(board._read(), image, plan=None))

    def test_a_reported_range_that_does_not_add_up_is_refused(self):
        plan = dfuimage.readback_plan(
            "Reading back 114688 bytes, 0x4100-0x1fffe: part 0x25011722 (LPC1754) "
            "has 128 KiB of flash\n")
        self.assertEqual(
            ["the read-back starts at 0x4100, not the application region's 0x4000",
             "0x4100-0x1fffe is not 114688 bytes"], plan.problems())
        odd = dfuimage.readback_plan("Reading back 1024 bytes, 0x4000-0x43ff: part "
                                     "0x25011722 (LPC1754) has 128 KiB of flash\n")
        self.assertEqual(
            ["the read-back covers 1024 bytes, which no part in the harness's copy "
             "of the fork's part table explains (32 KiB, 64 KiB, 128 KiB, 256 KiB, "
             "512 KiB); that length would be the application region of a part with "
             "17 KiB of flash",
             # The same line named a 128 KiB part, so the length does not
             # follow the part the command itself reported.
             "the read-back covers 1024 bytes, but the command reported a part "
             "with 128 KiB of flash, whose application region is 114688 bytes"],
            odd.problems())
        # A plan that does not add up fails the comparison as well.
        self.assertIn("0x4100-0x1fffe is not 114688 bytes",
                      dfuimage.readback_problems(FakeDfuBoard(firmware())._read(),
                                                 firmware(), plan=plan))


class DfuReadbackComparisonTests(unittest.TestCase):
    """What the comparison names when a read-back differs (#278): a field -d
    fills in, by field, and anything else by offset."""

    def setUp(self):
        self.image = firmware(image_check=True)
        self.board = FakeDfuBoard(self.image)
        self.plan = dfuimage.readback_plan(self.board.plan_line(SERIAL))
        self.good = self.board._read()
        self.assertEqual([], self.problems(self.good))

    def problems(self, dump):
        return dfuimage.readback_problems(bytes(dump), self.image, plan=self.plan)

    def test_corruption_in_each_written_field_names_that_field(self):
        fields = dfuimage.written_fields(self.image)
        self.assertEqual(5, len(fields))
        for field in fields:
            with self.subTest(field.name):
                dump = bytearray(self.good)
                dump[field.offset] ^= 0xFF
                problems = self.problems(dump)
                self.assertEqual(1, len(problems), problems)
                self.assertIn(field.name, problems[0])
                self.assertIn(f"at 0x{0x4000 + field.offset:05x} is", problems[0])

    def test_a_field_left_as_the_file_carries_it_is_refused(self):
        """The fields are checked against what the writer should have worked
        out. A board holding the file's own empty values fails, rather than
        passing because those bytes are allowed to differ from the file."""
        dump = bytearray(self.good)
        dump[:len(self.image)] = self.image
        problems = self.problems(dump)
        self.assertEqual([field.name for field in dfuimage.written_fields(self.image)],
                         [problem.split(" at 0x")[0] for problem in problems])
        self.assertTrue(all("is 0x0" in problem for problem in problems), problems)

    def test_corruption_outside_those_fields_is_named_by_offset(self):
        # Around the vector checksum, inside the image check magic and format
        # version, just past the record, and in a later block.
        for offset in (0x00, 0x1B, 0x20, 0xE0, 0xE9, 0xF8, 0xFF, 0x100,
                       len(self.image) - 1):
            with self.subTest(offset=hex(offset)):
                dump = bytearray(self.good)
                dump[offset] ^= 0xFF
                self.assertEqual(
                    ["application flash differs from the image at "
                     f"0x{0x4000 + offset:05x}"], self.problems(dump))

    def test_a_field_and_a_byte_elsewhere_are_both_reported(self):
        offset = dfuimage.VECTOR_CHECKSUM_OFFSET
        dump = bytearray(self.good)
        dump[offset] ^= 0xFF
        dump[0x200] ^= 0xFF
        found = int.from_bytes(dump[offset:offset + 4], "little")
        want = int.from_bytes(self.good[offset:offset + 4], "little")
        self.assertEqual(
            [f"vector checksum at 0x0401c is 0x{found:08x}, expected 0x{want:08x}",
             "application flash differs from the image at 0x04200"],
            self.problems(dump))

    def test_a_short_dump_is_reported_rather_than_read_past(self):
        problems = self.problems(self.good[:1000])
        self.assertIn("read-back is 1000 bytes, not the 114688 the command reported",
                      problems)
        self.assertIn(f"read-back holds 1000 of the image's {len(self.image)} bytes",
                      problems)

    def test_padding_and_the_rest_of_the_region_are_unchanged(self):
        dump = bytearray(self.good)
        dump[len(self.image)] = 0
        self.assertEqual(["last block padding is not 0xff"], self.problems(dump))


class DfuReadbackRunTests(unittest.TestCase):
    """The DFU images test against boards whose parts and tools differ (#278)."""

    def test_a_256_kib_part_passes_the_whole_test(self):
        image = firmware(image_check=True)
        board = FakeDfuBoard(image, flash_size=256 * 1024)
        summary, _events = run_dfu(board, image, coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(245760, len(board.flash))

    def test_a_128_kib_part_passes_the_whole_test(self):
        image = firmware(image_check=True)
        board = FakeDfuBoard(image, flash_size=128 * 1024)
        summary, _events = run_dfu(board, image, coverage="comprehensive")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(114688, len(board.flash))

    def test_a_board_that_kept_the_wrong_vector_checksum_fails(self):
        image = firmware(image_check=True)
        board = FakeDfuBoard(image, bad_vector_checksum=True)
        summary, events = run_dfu(board, image, coverage="smoke")
        self.assertEqual(["write-read-back"], failed(events))
        self.assertIn("vector checksum at 0x0401c", summary.failure_reason)
        self.assertIn("flash still differs after the rewrite", summary.failure_reason)

    def restore_with(self, image_check=True, **flags):
        image = firmware(image_check=image_check)
        board = FakeDfuBoard(image, **flags)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "test.dfu"
            path.write_bytes(image)
            with redirect_stdout(io.StringIO()) as out:
                code = dfuimage.restore(board._board(SERIAL), path, REVISION,
                                        root / "r.bin")
        return board, code, out.getvalue()

    def test_a_cleanup_whose_read_back_describes_itself_oddly_does_not_rewrite(self):
        """The flash holds the image either way, so there is nothing a
        rewrite could fix and another read-back would say the same: the
        cleanup fails without spending a flash cycle on a shared board."""
        for flags, reason in (
            ({"no_plan_line": True}, "the read-back did not report the range it covers"),
            ({"odd_plan_line": True},
             "the read-back covers 1024 bytes, which no part in the harness's copy "
             "of the fork's part table explains"),
        ):
            with self.subTest(**flags):
                board, code, output = self.restore_with(**flags)
                self.assertEqual(1, code)
                self.assertIn(f"Restore: {reason}", output)
                self.assertEqual([], [command for command in board.commands
                                      if command[0] == "ubertooth-dfu"
                                      and "-d" in command])

    def test_a_cleanup_whose_flash_differs_does_rewrite(self):
        board, code, output = self.restore_with(bad_vector_checksum=True)
        self.assertEqual(1, code)
        self.assertIn("Flash differs from the test image; rewriting it", output)
        self.assertIn("Restore: flash still differs after the rewrite: vector "
                      "checksum at 0x0401c is ", output)
        self.assertEqual(1, len([command for command in board.commands
                                 if command[0] == "ubertooth-dfu" and "-d" in command]))

    def test_a_build_that_reports_the_range_and_fills_nothing_in_fails_on_the_field(self):
        """Fork 10c3381 to 31228b0: the range is reported, the fields are not
        filled in, so the dump equals the file and the vector checksum alone
        is named. An image of that era carries no image check block."""
        board, code, output = self.restore_with(image_check=False,
                                                no_first_block_fill=True)
        self.assertEqual(1, code)
        self.assertIn("Flash differs from the test image; rewriting it", output)
        self.assertIn("Restore: flash still differs after the rewrite: vector "
                      "checksum at 0x0401c is 0x00000000, expected 0x", output)

    def test_a_tool_that_reports_nothing_and_fills_nothing_in_rewrites_once(self):
        """A -u that says nothing about its range, with a -d that fills in
        nothing: the flash really does differ from the image as today's -d
        writes it, so the cleanup rewrites once and then fails on both.

        No fork build is this pair -- one older than 10c3381 fills nothing in
        but cannot read a 128 KiB part back at all (ubertooth#233) -- so this
        pins the cleanup's own arithmetic rather than a build's behavior.
        """
        board, code, output = self.restore_with(image_check=False, no_plan_line=True,
                                                no_first_block_fill=True)
        self.assertEqual(1, code)
        self.assertIn("Flash differs from the test image; rewriting it", output)
        self.assertEqual(1, len([command for command in board.commands
                                 if command[0] == "ubertooth-dfu" and "-d" in command]))
        self.assertIn("Restore: flash still differs after the rewrite: the read-back "
                      "did not report the range it covers; vector checksum at "
                      "0x0401c is 0x00000000", output)

    def test_an_image_from_before_the_check_block_passes_on_its_own_tool(self):
        """Between fork 31228b0 and d91bb06 the checksum is filled in and
        there is no image check block on either side, so the case passes with
        the vector checksum as the only field checked."""
        image = firmware(image_check=False)
        board = FakeDfuBoard(image)
        summary, _events = run_dfu(board, image, coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual([], flash_problems(board, image))

    def test_a_tool_that_reports_no_range_fails_the_read_back_step(self):
        """An earlier -u said nothing about what it was reading. The step
        records the line, so a tool that prints none fails there rather than
        having its dump compared against an assumed part."""
        image = firmware(image_check=True)
        board = FakeDfuBoard(image, no_plan_line=True)
        summary, events = run_dfu(board, image, coverage="smoke")
        self.assertEqual(["write-read-back"], failed(events))
        self.assertIn(f"{dfu_cases.READBACK_RANGE_KEY} was not reported",
                      summary.failure_reason)


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
