"""DFU image files and read-backs for the DFU images and DFU image checks tests.

Run as ``python <this file> COMMAND ...`` from a transition step. Every
command prints short summary lines (never image bytes) and exits 0 when the
check holds, 1 when it does not, and 2 for a usage error. File commands use
only the standard library, so they run from any working directory. ``synth``
writes a synthetic binary and its .dfu for runs without a firmware build,
``make`` the bad and edge images, and ``state`` lists a directory (and, with
``--before``, says what changed since an earlier listing).

The layout follows host/ubertooth-tools/src/ubertooth-dfu.c and dfu.h:

- ``-s file.bin`` writes ``file.dfu``: the binary, then a 16-byte suffix
  (bcdDevice 0, idProduct 0x6003, idVendor 0x1d50, bcdDFU 0x0100, "UFD",
  bLength 16) and a CRC-32 (initial 0xffffffff, no final inversion) over
  everything before the CRC.
- ``-d`` writes the whole file, suffix included, in 256-byte blocks from
  flash 0x4000, padding the last block with 0xff.
- ``-u`` reads flash 0x4000-0x3ffff (245760 bytes) raw: the written file,
  its padding, then whatever the rest of the application region holds.

``answers``, ``after-reject`` and ``restore`` also run ubertooth-util and
ubertooth-dfu (from PATH) on the selected board; see their functions. They
print a command record (``rawlog.format_command_record``: the exact argv, each
stdout and stderr line, exit code, and duration) for every command they run,
so the run transcript shows each command sent and each response received.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import time
from typing import Callable, Sequence
import zlib

if __package__ in (None, ""):
    # Run as a script: make the package importable for the record format.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness.executor import CommandResult  # noqa: E402
from ubertooth_harness.rawlog import CommandCapture, format_command_record  # noqa: E402

SUFFIX_LENGTH = 16
VENDOR = 0x1D50
PRODUCT = 0x6003
BCD_DFU = 0x0100
# check_suffix() in ubertooth-dfu.c accepts both; sign() writes 0x0100.
ACCEPTED_BCD_DFU = (0x0100, 0x0101)
SIGNATURE = b"UFD"
BLOCK_SIZE = 256
APPLICATION_OFFSET = 0x4000
APPLICATION_SIZE = 0x40000 - APPLICATION_OFFSET

# Bad images ``make`` writes, each from a valid image.
BAD_KINDS = (
    "corrupt", "truncated", "no-suffix", "text", "bad-signature",
    "wrong-vendor", "wrong-product",
)
# More images ``make`` writes for the DFU image checks test, from a valid
# image: the first six must be refused, the last two accepted.
EDGE_KINDS = (
    "empty", "short", "suffix-only", "long-suffix-length", "dfu-2.0", "no-marker",
    "dfu-1.1", "wildcard",
)
# The synthetic image ``synth`` writes when a run has no firmware build.
SYNTHETIC_REVISION = "harness-synthetic"

_VERSION_RE = re.compile(r"Firmware version:\s+(?P<revision>\S+)\s+\(API:")


def dfu_crc(data: bytes) -> int:
    return zlib.crc32(data) ^ 0xFFFFFFFF


def suffix_problems(image: bytes) -> list[str]:
    """Why ``image`` is not a valid Ubertooth DFU file ([] when it is)."""
    if len(image) < SUFFIX_LENGTH:
        return [f"{len(image)} bytes, shorter than the 16-byte suffix"]
    device, product, vendor, bcd, signature, length, crc = struct.unpack(
        "<HHHH3sBI", image[-SUFFIX_LENGTH:]
    )
    problems = []
    if length != SUFFIX_LENGTH:
        problems.append(f"bLength {length}")
    if signature != SIGNATURE:
        problems.append(f"signature {signature!r}")
    if bcd not in ACCEPTED_BCD_DFU:
        problems.append(f"bcdDFU {bcd:04x}")
    if (vendor, product) != (VENDOR, PRODUCT):
        problems.append(f"VID:PID {vendor:04x}:{product:04x}")
    if device != 0:
        problems.append(f"bcdDevice {device:04x}")
    if crc != dfu_crc(image[:-4]):
        problems.append(f"CRC 0x{crc:08x}, expected 0x{dfu_crc(image[:-4]):08x}")
    return problems


def _resuffix(image: bytearray) -> bytes:
    image[-4:] = struct.pack("<I", dfu_crc(bytes(image[:-4])))
    return bytes(image)


def with_suffix(body: bytes, *, vendor=VENDOR, product=PRODUCT, bcd=BCD_DFU,
                signature=SIGNATURE, length=SUFFIX_LENGTH) -> bytes:
    """``body`` and a DFU suffix as ``-s`` writes it, fields overridable."""
    data = body + struct.pack("<HHHH3sB", 0, product, vendor, bcd, signature, length)
    return data + struct.pack("<I", dfu_crc(data))


def synthetic_binary(revision: str = SYNTHETIC_REVISION) -> bytes:
    """A deterministic stand-in firmware binary for runs without a build.

    It carries the ``ubertooth <revision> (`` marker ``-d ... -r`` reads the
    expected revision from, between filler bytes, and is not a whole number
    of 256-byte blocks. It is never written to a board.
    """
    filler = bytes((index * 37 + 11) % 251 for index in range(4000))
    marker = f"ubertooth {revision} (synthetic image for host-only checks)\0".encode()
    return filler[:2048] + marker + filler[2048:]


def make_edge(kind: str, image: bytes) -> bytes:
    """An EDGE_KINDS image derived from the valid ``image``."""
    body = image[:-SUFFIX_LENGTH]
    if kind == "empty":
        return b""
    if kind == "short":
        return image[:SUFFIX_LENGTH - 1]
    if kind == "suffix-only":
        return with_suffix(b"")
    if kind == "long-suffix-length":
        # bLength 200 in an 80-byte file: longer than the file itself.
        return with_suffix(body[:64], length=200)
    if kind == "dfu-2.0":
        return with_suffix(body, bcd=0x0200)
    if kind == "no-marker":
        return with_suffix(body.replace(b"ubertooth ", b"Ubertooth "))
    if kind == "dfu-1.1":
        return with_suffix(body, bcd=0x0101)
    if kind == "wildcard":
        return with_suffix(body, vendor=0xFFFF, product=0xFFFF)
    raise ValueError(f"unknown image kind '{kind}'")


def directory_state(directory: Path) -> dict[str, str]:
    """Each entry's size or "dir", mode and SHA-256 prefix, by name."""
    state = {}
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            state[entry.name] = "dir"
            continue
        data = entry.read_bytes()
        mode = stat.S_IMODE(entry.stat().st_mode)
        state[entry.name] = (f"{len(data)} bytes, mode {mode:o}, "
                             f"sha256 {hashlib.sha256(data).hexdigest()[:16]}")
    return state


def state_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """What differs between two directory_state() results ([] when nothing)."""
    changes = [f"{name} added ({after[name]})" for name in after if name not in before]
    changes += [f"{name} removed" for name in before if name not in after]
    changes += [f"{name} changed ({before[name]} -> {after[name]})"
                for name in before if name in after and before[name] != after[name]]
    return changes


def make_bad(kind: str, image: bytes) -> bytes:
    """A bad image of ``kind`` derived from the valid ``image``."""
    body = bytearray(image)
    if kind == "corrupt":
        # One payload byte flipped: only the CRC can tell.
        body[len(body) // 3] ^= 0xFF
        return bytes(body)
    if kind == "truncated":
        return bytes(body[: len(body) // 2])
    if kind == "no-suffix":
        return bytes(body[:-SUFFIX_LENGTH])
    if kind == "text":
        return b"This is a text file, not Ubertooth firmware.\n"
    if kind == "bad-signature":
        body[-5 - 3:-5] = b"XYZ"
        return _resuffix(body)
    if kind == "wrong-vendor":
        struct.pack_into("<H", body, len(body) - 12, 0x1234)
        return _resuffix(body)
    if kind == "wrong-product":
        struct.pack_into("<H", body, len(body) - 14, 0x0001)
        return _resuffix(body)
    raise ValueError(f"unknown bad image kind '{kind}'")


def readback_problems(readback: bytes, image: bytes) -> list[str]:
    """Why ``readback`` (from -u) does not hold ``image`` as -d writes it."""
    problems = []
    if len(readback) != APPLICATION_SIZE:
        problems.append(f"read-back is {len(readback)} bytes, expected {APPLICATION_SIZE}")
    written = readback[: len(image)]
    if written != image:
        first = next(
            (i for i, (a, b) in enumerate(zip(written, image)) if a != b),
            min(len(written), len(image)),
        )
        problems.append(
            f"application flash differs from the image at 0x{APPLICATION_OFFSET + first:05x}"
        )
    padded = -(-len(image) // BLOCK_SIZE) * BLOCK_SIZE
    padding = readback[len(image):padded]
    if len(readback) >= padded and padding != b"\xff" * len(padding):
        problems.append("last block padding is not 0xff")
    return problems


# ---------------------------------------------------------------------------
# Device commands

Runner = Callable[[Sequence[str], float], CommandResult]

# Per-command timeouts. The step that runs a device command gets a timeout
# derived from these (worst_case_s), so it never kills the helper mid-flash.
REVISION_TIMEOUT_S = 5.0
DFU_COUNT_TIMEOUT_S = 10.0
DFU_WRITE_TIMEOUT_S = 40.0
DFU_READ_TIMEOUT_S = 30.0
APP_WAIT_S = 10.0
POLL_S = 0.5

_IOREG = ("ioreg", "-p", "IOUSB", "-l", "-w0")


def run_command(argv: Sequence[str], timeout_s: float) -> CommandResult:
    """Run a host tool without a shell. A timeout is exit -1, timed_out."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, errors="replace",
            timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as error:
        def text(value):
            if isinstance(value, bytes):
                return value.decode(errors="replace")
            return value or ""
        return CommandResult(tuple(argv), -1, text(error.stdout), text(error.stderr),
                             time.monotonic() - start, True)
    except OSError as error:
        return CommandResult(tuple(argv), 127, "", f"{error}\n",
                             time.monotonic() - start, False)
    return CommandResult(tuple(argv), result.returncode, result.stdout, result.stderr,
                         time.monotonic() - start, False)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _usb_device_lines(stdout: str) -> str:
    """One ioreg line per USB device, without the property dump."""
    return "".join(line + "\n" for line in stdout.splitlines() if "+-o " in line)


def dfu_device_count(call: Callable[..., CommandResult] | None = None) -> int:
    """Attached Ubertooth One DFU bootloaders (1d50:6003).

    The harness runs on macOS hosts as well as Linux. macOS has no
    /sys/bus/usb, and this helper uses only the standard library (no libusb,
    and lsusb is not installed by default), so it counts devices in ioreg's
    IOUSB plane instead; idVendor and idProduct are printed in decimal there.
    """
    if sys.platform == "darwin":
        if call is None:
            result = run_command(_IOREG, DFU_COUNT_TIMEOUT_S)
        else:
            result = call(_IOREG, DFU_COUNT_TIMEOUT_S, shown=_usb_device_lines)
        count = 0
        for block in result.stdout.split("+-o ")[1:]:
            if f'"idVendor" = {VENDOR}' in block and f'"idProduct" = {PRODUCT}' in block:
                count += 1
        return count
    count = 0
    root = Path("/sys/bus/usb/devices")
    for device in root.iterdir() if root.is_dir() else ():
        try:
            vendor = (device / "idVendor").read_text().strip().lower()
            product = (device / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if (vendor, product) == (f"{VENDOR:04x}", f"{PRODUCT:04x}"):
            count += 1
    return count


def wait_worst_s() -> float:
    """Longest wait_revision(): a last poll that starts just before the deadline."""
    return APP_WAIT_S + REVISION_TIMEOUT_S + POLL_S


def leave_dfu_worst_s(guards: int) -> float:
    return DFU_COUNT_TIMEOUT_S + guards * REVISION_TIMEOUT_S + DFU_READ_TIMEOUT_S


def worst_case_s(command: str, guards: int = 0) -> float:
    """Longest a device command can take if every tool call uses its timeout."""
    wait = wait_worst_s()
    if command == "answers":
        return wait
    if command == "after-reject":
        # -v, then a read-back (selected, or from DFU after the guard reads).
        return REVISION_TIMEOUT_S + max(leave_dfu_worst_s(guards), DFU_READ_TIMEOUT_S) + wait
    if command == "restore":
        # -v, a failed read-back, -v, leave DFU, wait, rewrite, read-back, wait.
        return (REVISION_TIMEOUT_S + DFU_READ_TIMEOUT_S + REVISION_TIMEOUT_S
                + leave_dfu_worst_s(guards) + wait + DFU_WRITE_TIMEOUT_S
                + DFU_READ_TIMEOUT_S + wait)
    raise ValueError(f"'{command}' runs no device command")


def step_timeout_s(command: str, guards: int = 0) -> float:
    """Timeout for a step running ``command``: the worst case plus a quarter,
    and 15 s for interpreter start-up and file reads."""
    return worst_case_s(command, guards) * 1.25 + 15.0


class Board:
    """The selected board's tool commands, with injectable seams for tests."""

    def __init__(self, serial, guards=(), *, run: Runner = run_command,
                 dfu_count: Callable[[], int] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, log=print,
                 utc_now: Callable[[], datetime.datetime] = _utc_now):
        self.serial = serial
        self.guards = tuple(guards)
        self.run = run
        self.dfu_count = dfu_count or (lambda: dfu_device_count(self.call))
        self.sleep = sleep
        self.clock = clock
        self.log = log
        self.utc_now = utc_now

    def call(self, argv, timeout_s, *, shown: Callable[[str], str] | None = None) -> CommandResult:
        """Run one command and print its record: argv, output, exit, duration.

        ``shown`` reduces a long stdout to the lines worth keeping; the record
        then says how many lines were left out.
        """
        capture = CommandCapture(self.utc_now)
        result = self.run(tuple(argv), timeout_s)
        capture.finish()
        record = result
        if shown is not None:
            kept = shown(result.stdout)
            omitted = len(result.stdout.splitlines()) - len(kept.splitlines())
            record = dataclasses.replace(result, stdout=kept)
            if omitted:
                self.log(f"(stdout of {argv[0]}: {omitted} property lines left out, "
                         "device lines kept)")
        self.log(format_command_record(record, capture).rstrip("\n"))
        return result

    def revision(self, serial=None) -> str | None:
        result = self.call(("ubertooth-util", "-U", serial or self.serial, "-v"),
                           REVISION_TIMEOUT_S)
        output = result.stdout + result.stderr
        match = _VERSION_RE.search(output) if result.returncode == 0 else None
        return match["revision"] if match else None

    def wait_revision(self) -> str | None:
        """The application's revision once it answers (it re-enumerates after -r)."""
        deadline = self.clock() + APP_WAIT_S
        while True:
            revision = self.revision()
            if revision is not None or self.clock() >= deadline:
                return revision
            self.sleep(POLL_S)

    def _dfu(self, argv, timeout_s) -> tuple[int, str]:
        result = self.call(argv, timeout_s)
        return result.returncode, result.stdout + result.stderr

    def read_back(self, path: Path) -> str:
        """``-u PATH -r`` on the selected application; "" or why it failed."""
        code, output = self._dfu(
            ("ubertooth-dfu", "-U", self.serial, "-u", str(path), "-r"), DFU_READ_TIMEOUT_S
        )
        return "" if code == 0 else f"read-back exited {code}: {_last_line(output)}"

    def leave_dfu(self, path: Path) -> str:
        """Return a board stuck in the DFU bootloader without writing.

        DFU mode has no serial, so the selected board is identified by
        elimination: exactly one DFU device and every guard answering in its
        application. ``ubertooth-dfu -u PATH -r`` without -U then adopts that
        device, reads it back, and detaches it; it writes nothing, even if the
        device vanished in between and it opened an application instead.
        """
        count = self.dfu_count()
        if count != 1:
            return f"{count} DFU devices attached; recover the board by hand"
        for guard in self.guards:
            if self.revision(guard) is None:
                return (f"guard board {guard} does not answer, so the DFU device may be it; "
                        "recover by hand")
        code, output = self._dfu(("ubertooth-dfu", "-u", str(path), "-r"), DFU_READ_TIMEOUT_S)
        if code != 0:
            return f"DFU read-back and reset exited {code}: {_last_line(output)}"
        self.log("Left DFU: read back the bootloader's flash and reset it")
        return ""

    def write(self, image: Path) -> str:
        code, output = self._dfu(
            ("ubertooth-dfu", "-U", self.serial, "-d", str(image), "-r"), DFU_WRITE_TIMEOUT_S
        )
        return "" if code == 0 else f"rewrite exited {code}: {_last_line(output)}"


def _last_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.strip() and not line.startswith("DFU +")]
    return lines[-1] if lines else "no output"


def after_reject(board: Board, image: Path, revision: str, readback: Path) -> int:
    """Check the board right after ``-d <bad image> -r`` was refused.

    It must still answer in its application with ``revision``, and its flash
    must still hold ``image``. A board left in the DFU bootloader is reported,
    read back there, and returned to its application.
    """
    problems = []
    failure = ""
    seen = board.revision()
    if seen is None:
        problems.append("board left its application (no answer to ubertooth-util -v)")
        failure = board.leave_dfu(readback)
        if failure:
            print(f"After rejection: {problems[0]}; {failure}")
            return 1
    else:
        print(f"Board answers: {seen}")
        if seen != revision:
            problems.append(f"firmware revision {seen}, expected {revision}")
        failure = board.read_back(readback)
        if failure:
            problems.append(failure)
    if not failure:
        differences = readback_problems(readback.read_bytes(), image.read_bytes())
        print("Application flash unchanged" if not differences else "; ".join(differences))
        problems.extend(differences)
    returned = board.wait_revision()
    if returned is None:
        problems.append("board did not return to its application")
    else:
        print(f"Board in its application: {returned}")
        if seen is None and returned != revision:
            # Left in DFU (fork #198): the revision was not read before.
            problems.append(f"firmware revision {returned} after leaving DFU, expected {revision}")
    if problems:
        print("After rejection: " + "; ".join(problems))
        return 1
    print("After rejection: ok")
    return 0


def restore(board: Board, image: Path, revision: str, readback: Path) -> int:
    """Leave the board in its application with ``image`` in flash.

    Leaves the DFU bootloader if needed (without writing), reads the flash
    back, and rewrites ``image`` only when the flash differs.
    """
    failed_read_back = ""
    if board.revision() is None:
        failure = board.leave_dfu(readback)
        if failure:
            print(f"Restore: {failure}")
            return 1
    else:
        failed_read_back = board.read_back(readback)
        if failed_read_back:
            # A failed -u ends without its -r reset (fork #208): return the
            # board, but fail the cleanup so the run stops on the record.
            if board.revision() is not None:
                print(f"Restore: {failed_read_back}")
                return 1
            failure = board.leave_dfu(readback)
            if failure:
                print(f"Restore: {failed_read_back}; {failure}")
                return 1
    if board.wait_revision() is None:
        print("Restore: board did not return to its application")
        return 1
    data = image.read_bytes()
    if readback_problems(readback.read_bytes(), data):
        print("Flash differs from the test image; rewriting it")
        failure = board.write(image)
        if failure:
            print(f"Restore: {failure}")
            return 1
        failure = board.read_back(readback)
        if failure or readback_problems(readback.read_bytes(), data):
            print(f"Restore: flash still differs after the rewrite {failure}".rstrip())
            return 1
    seen = board.wait_revision()
    if seen != revision:
        print(f"Restore: firmware revision {seen}, expected {revision}")
        return 1
    if failed_read_back:
        print(f"Restore: {failed_read_back}; the board was left in DFU, then returned with "
              f"the test image in flash and firmware {seen}")
        return 1
    print(f"Restored: application flash holds the test image, firmware {seen}")
    return 0


# ---------------------------------------------------------------------------
# Command line

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dfuimage.py")
    sub = parser.add_subparsers(dest="command", required=True)
    strip = sub.add_parser("strip", help="write the image without its suffix")
    strip.add_argument("image")
    strip.add_argument("output")
    strip.add_argument("--read-only", action="store_true")
    copy = sub.add_parser("copy")
    copy.add_argument("source")
    copy.add_argument("output")
    make = sub.add_parser("make", help="write a bad image")
    make.add_argument("kind", choices=BAD_KINDS + EDGE_KINDS)
    make.add_argument("image")
    make.add_argument("output")
    synth = sub.add_parser("synth", help="write a synthetic binary and its .dfu")
    synth.add_argument("binary")
    synth.add_argument("output")
    state = sub.add_parser("state", help="list a directory with a digest of its state")
    state.add_argument("directory")
    state.add_argument("--before", help="a 'Directory entries' value to compare with")
    check = sub.add_parser("check-suffix", help="OUTPUT is BINARY plus a valid suffix")
    check.add_argument("binary")
    check.add_argument("output")
    check.add_argument("reference")
    check.add_argument("--reference-name", default="the build's .dfu")
    same = sub.add_parser("same")
    same.add_argument("first")
    same.add_argument("second")
    absent = sub.add_parser("absent")
    absent.add_argument("path")
    regular = sub.add_parser("regular", help="PATH is a regular file, not a symlink")
    regular.add_argument("path")
    regular.add_argument("--mode", help="its permission bits, in octal")
    sub.add_parser("not-root", help="say whether this process runs as root")
    state.add_argument("--allow", action="append", default=[],
                       help="an entry that may change (or appear)")
    readback = sub.add_parser("check-readback")
    readback.add_argument("readback")
    readback.add_argument("image")
    answers = sub.add_parser("answers", help="the application answers with REVISION")
    answers.add_argument("--serial", required=True)
    answers.add_argument("--revision", required=True)
    for name in ("after-reject", "restore"):
        device = sub.add_parser(name)
        device.add_argument("--serial", required=True)
        device.add_argument("--guard", action="append", default=[])
        device.add_argument("--image", required=True)
        device.add_argument("--revision", required=True)
        device.add_argument("--readback", required=True)
    return parser


def main(argv: list[str] | None = None, *, board_factory=Board) -> int:
    try:
        args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as error:
        return 2 if error.code else 0
    try:
        return _main(args, board_factory)
    except OSError as error:
        print(f"{args.command}: {error}")
        return 1


def _main(args, board_factory) -> int:
    command = args.command
    if command == "strip":
        image = Path(args.image).read_bytes()
        problems = suffix_problems(image)
        if problems:
            print("Source image suffix invalid: " + "; ".join(problems))
            return 1
        output = Path(args.output)
        output.write_bytes(image[:-SUFFIX_LENGTH])
        if args.read_only:
            os.chmod(output, 0o444)
        print(f"Stripped suffix: {len(image) - SUFFIX_LENGTH} bytes")
        return 0
    if command == "copy":
        Path(args.output).write_bytes(Path(args.source).read_bytes())
        print("Copied")
        return 0
    if command == "make":
        source = Path(args.image).read_bytes()
        if args.kind in BAD_KINDS:
            data = make_bad(args.kind, source)
            Path(args.output).write_bytes(data)
            print(f"Bad image ({args.kind}): {len(data)} bytes")
        else:
            data = make_edge(args.kind, source)
            Path(args.output).write_bytes(data)
            print(f"Edge image ({args.kind}): {len(data)} bytes")
        return 0
    if command == "synth":
        binary = synthetic_binary()
        Path(args.binary).write_bytes(binary)
        Path(args.output).write_bytes(with_suffix(binary))
        print(f"Synthetic image: {len(binary)} bytes, .dfu {len(binary) + SUFFIX_LENGTH} "
              f"bytes, revision {SYNTHETIC_REVISION}")
        return 0
    if command == "state":
        state = directory_state(Path(args.directory))
        for name, value in state.items():
            print(f"{name}: {value}")
        print("Directory entries: " + json.dumps(state, sort_keys=True))
        if args.before is None:
            return 0
        before = json.loads(args.before)
        changes = state_changes(
            {k: v for k, v in before.items() if k not in args.allow},
            {k: v for k, v in state.items() if k not in args.allow})
        if changes:
            print("Directory changed: " + "; ".join(changes))
            return 1
        print(f"Directory unchanged: {len(state)} entries")
        return 0
    if command == "check-suffix":
        binary = Path(args.binary).read_bytes()
        output = Path(args.output).read_bytes()
        problems = suffix_problems(output)
        if output[:-SUFFIX_LENGTH] != binary or len(output) != len(binary) + SUFFIX_LENGTH:
            problems.append(
                f"{len(output)} bytes do not start with the {len(binary)}-byte binary"
            )
        if output != Path(args.reference).read_bytes():
            problems.append(f"differs from {args.reference_name}")
        if problems:
            print("DFU suffix wrong: " + "; ".join(problems))
            return 1
        print(f"DFU suffix: VID:PID {VENDOR:04x}:{PRODUCT:04x}, bcdDFU {BCD_DFU:04x}, "
              f"CRC ok, identical to {args.reference_name}")
        return 0
    if command == "same":
        if Path(args.first).read_bytes() != Path(args.second).read_bytes():
            print(f"Changed: {args.first} differs from {args.second}")
            return 1
        print("Unchanged")
        return 0
    if command == "regular":
        entry = os.lstat(args.path)
        mode = stat.S_IMODE(entry.st_mode)
        if not stat.S_ISREG(entry.st_mode):
            print(f"Not a regular file: {args.path} "
                  f"({'a symlink' if stat.S_ISLNK(entry.st_mode) else 'mode %o' % entry.st_mode})")
            return 1
        if args.mode is not None and mode != int(args.mode, 8):
            print(f"Mode {mode:o}, not {args.mode}: {args.path}")
            return 1
        print(f"Regular file: {entry.st_size} bytes, mode {mode:o}")
        return 0
    if command == "not-root":
        uid = os.geteuid()
        print("Root: " + ("yes, a read-only directory does not stop root" if uid == 0
                          else f"no (uid {uid})"))
        return 0
    if command == "absent":
        if Path(args.path).exists():
            print(f"Created: {args.path}")
            return 1
        print("Not created")
        return 0
    if command == "check-readback":
        problems = readback_problems(Path(args.readback).read_bytes(),
                                     Path(args.image).read_bytes())
        if problems:
            print("Read-back mismatch: " + "; ".join(problems))
            return 1
        print(f"Read-back: {APPLICATION_SIZE} bytes, image and 0xff padding match")
        return 0
    if command == "answers":
        board = board_factory(args.serial)
        seen = board.wait_revision()
        if seen == args.revision:
            print(f"Board answers with the expected revision {seen}")
        else:
            print(f"Board answers {seen}, expected {args.revision}" if seen
                  else "Board does not answer")
        return 0 if seen == args.revision else 1
    board = board_factory(args.serial, args.guard)
    image, readback = Path(args.image), Path(args.readback)
    if command == "after-reject":
        return after_reject(board, image, args.revision, readback)
    return restore(board, image, args.revision, readback)


if __name__ == "__main__":
    sys.exit(main())
