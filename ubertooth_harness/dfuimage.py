"""DFU image files and read-backs for the DFU images and DFU image checks tests.

The file and directory commands here say nothing about DFU, so other tests
that have to state what a directory holds use them too: Failed capture files
runs ``state``, ``not-root`` and ``symlink``.

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
  flash 0x4000, padding the last block with 0xff. It fills in two things as
  it writes the image's first block, so flash holds the file's bytes
  everywhere else and the tool's own in those fields: the LPC vector checksum
  at 0x1c, and, in an image that carries an image check block, the record of
  the download at 0xea-0xf7 inside it.
- ``-u`` reads the application region raw, from flash 0x4000 to the end of
  the part's flash: the written file, its padding, then whatever the rest of
  the region holds. How long that is follows the part, so the command says
  what it is about to read and the length comes from there.

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
from typing import Callable, NamedTuple, Sequence
import zlib

if __package__ in (None, ""):
    # Run as a script: make the package importable for the record format.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness.executor import CommandResult  # noqa: E402
from ubertooth_harness.patterns import FIRMWARE_VERSION_RE  # noqa: E402
from ubertooth_harness.rawlog import CommandCapture, format_command_record  # noqa: E402

SUFFIX_LENGTH = 16
VENDOR = 0x1D50
PRODUCT = 0x6003
BCD_DFU = 0x0100
# The firmware info block every image from the fork carries, 64 bytes at a
# fixed offset just past the vector table (host/libubertooth/src/
# ubertooth_interface.h, firmware/common/fw_info.c). ubertooth-dfu reads it
# straight out of a file to name the image and the revision, and refuses a
# file that has none, so an image the harness builds has to carry one.
FW_INFO_OFFSET = 0x100
FW_INFO_LENGTH = 64
FW_INFO_MAGIC = b"UBTFINFO"
FW_INFO_FORMAT = 1
FW_INFO_IMAGE_LEN = 16
FW_INFO_REVISION_LEN = 32
# check_suffix() in ubertooth-dfu.c accepts both; sign() writes 0x0100.
ACCEPTED_BCD_DFU = (0x0100, 0x0101)
SIGNATURE = b"UFD"
BLOCK_SIZE = 256
APPLICATION_OFFSET = 0x4000
# How much flash a -u read-back covers (fork 10c3381,
# SqueakyFloors/ubertooth#257). It follows the part, which differs between
# boards: dfu_upload_length() in host/ubertooth-tools/src/dfu_recovery.c
# takes the flash size behind the part identification number and subtracts
# the bootloader's region. That number is readable only while the board is
# still running its application, so a board already in the bootloader, and a
# part the host has no entry for, get the size the firmware is linked for
# instead (DFU_ASSUMED_FLASH_SIZE). The sizes below are the LPC175x/176x
# flash sizes in that file's part table. They are a copy of it, so a run
# reads the fork's own table at the commit under test and warns when the two
# differ (dfuparts.py, #283).
PART_FLASH_SIZES = (32 * 1024, 64 * 1024, 128 * 1024, 256 * 1024, 512 * 1024)
ASSUMED_FLASH_SIZE = 128 * 1024


def describe_flash_size(flash_size: int) -> str:
    """A flash size written as the fork's own messages write it."""
    if flash_size > 0 and flash_size % 1024 == 0:
        return f"{flash_size // 1024} KiB"
    return f"{flash_size} bytes"


def upload_length(flash_size: int) -> int:
    """Bytes a -u read-back covers on a part with ``flash_size`` of flash.

    ``flash_size`` 0 is a part the tool could not identify, which it reads as
    the size the firmware is linked for.
    """
    if flash_size == 0:
        flash_size = ASSUMED_FLASH_SIZE
    return max(flash_size - APPLICATION_OFFSET, 0)


# Every length a read-back can report, so one that reports any other figure
# is reporting something neither the part table nor the assumption explains.
READBACK_LENGTHS = tuple(sorted(
    {upload_length(size) for size in PART_FLASH_SIZES} - {0}))

# What ubertooth-dfu fills in as it writes an image's first block, for the
# bootloader's checks (fork 31228b0, SqueakyFloors/ubertooth#1037, and
# d91bb06, #1048): prepare_first_block() in
# host/ubertooth-tools/src/ubertooth-dfu.c, and the image check block's
# layout in host/libubertooth/src/ubertooth_interface.h. An image this tree
# builds leaves both empty, and these are the only bytes the tool writes
# over, so a read-back holds the file everywhere else.
#
# The word at offset 0x1c of the vector table is the two's complement of the
# seven words in front of it, so all eight add up to zero (NXP LPC176x/5x
# user manual UM10360, "Criterion for valid user code").
VECTOR_CHECKSUM_OFFSET = 0x1C
VECTOR_CHECKSUM_WORDS = 7
# The image check block: 32 bytes the image carries, of which the tool fills
# in the flags, the length this download writes, its complement, and the
# checksum over everything written. The magic and the format version are the
# image's own, and say whether the block is there to fill in at all.
IMAGE_CHECK_OFFSET = 0xE0
IMAGE_CHECK_LENGTH = 32
IMAGE_CHECK_MAGIC = b"UBTIMGCK"
IMAGE_CHECK_FORMAT = 1
IMAGE_CHECK_OFF_FORMAT = 8
IMAGE_CHECK_OFF_FLAGS = 10
IMAGE_CHECK_OFF_LENGTH = 12
IMAGE_CHECK_OFF_LENGTH_COMPLEMENT = 16
IMAGE_CHECK_OFF_CHECKSUM = 20
# UBERTOOTH_IMAGE_CHECK_VECTOR | UBERTOOTH_IMAGE_CHECK_REGION: the tool asks
# the board for both checks, the vector table's checksum and the sum over the
# whole region the download wrote.
IMAGE_CHECK_FLAGS = 0x0001 | 0x0002


class WrittenField(NamedTuple):
    """A field ubertooth-dfu fills in, so a read-back of it holds what the
    tool worked out rather than what the image file carried. ``offset`` is
    from the start of the application region."""

    name: str
    offset: int
    size: int


VECTOR_CHECKSUM_FIELD = WrittenField("vector checksum", VECTOR_CHECKSUM_OFFSET, 4)
# The four fields run from 0xea to 0xf7 with no gaps, which is the "fourteen
# from 0xea to 0xf8" ubertooth-dfu(1) names as the second range a read-back
# may differ in.
IMAGE_CHECK_FIELDS = (
    WrittenField("image check flags",
                 IMAGE_CHECK_OFFSET + IMAGE_CHECK_OFF_FLAGS, 2),
    WrittenField("image check region length",
                 IMAGE_CHECK_OFFSET + IMAGE_CHECK_OFF_LENGTH, 4),
    WrittenField("image check length complement",
                 IMAGE_CHECK_OFFSET + IMAGE_CHECK_OFF_LENGTH_COMPLEMENT, 4),
    WrittenField("image check region checksum",
                 IMAGE_CHECK_OFFSET + IMAGE_CHECK_OFF_CHECKSUM, 4),
)

# The first two words of an image and the bounds the bootloader puts them in
# (firmware/bootloader/app_image.h, app_image_startable(); the flash bound is
# LPC_FLASH_SIZE_FALLBACK in firmware/bootloader/flash.h). An image whose
# first two words fall outside these is one the bootloader must refuse to
# start, leaving itself running so the next image can be written over USB.
#
# This is the first of the checks the bootloader makes, not all of them:
# app_image_region_startable() goes on to ask for the LPC vector checksum at
# offset 0x1c and, when the image carries an image check block, a checksum
# over the whole region. Both of those are filled in by ubertooth-dfu as it
# writes the image's first block, so they say whether what was written is
# still all there rather than anything about the file. The harness models
# this check alone, which is enough for what it builds images for: an image
# refused here is refused before either checksum is read.
APP_VECTORS_LENGTH = 8
APP_RAM_BASE = 0x10000000
APP_RAM_LIMIT = 0x10010000
APP_AHB_RAM_BASE = 0x2007C000
APP_AHB_RAM_LIMIT = 0x20084000
FLASH_TOP = 0x80000

# Images the bootloader must refuse to start, each made from a valid image.
# Every one keeps a valid DFU suffix and the firmware info block, so
# ubertooth-dfu accepts and writes it: what is wrong is in the two words
# above, which only the bootloader reads.
#
# The first is what the hardware case uses. The others are checked on the
# host only (tests/test_invalid_application.py): each is another way of
# failing the same check, so running them on a board would be another cold
# boot with the same risk behind the same decision.
UNSTARTABLE_KINDS = (
    # Both words erased, which is what an interrupted download leaves at the
    # start of the region (ubertooth-dfu.c, download()); the case on hardware
    # uses this one.
    "erased-vectors",
    "misaligned-stack", "stack-outside-ram", "arm-reset-vector",
    "reset-vector-in-bootloader",
)

# Bad images ``make`` writes, each from a valid image.
BAD_KINDS = (
    "corrupt", "truncated", "no-suffix", "text", "bad-signature",
    "wrong-vendor", "wrong-product",
)
# More images ``make`` writes for the DFU image checks test, from a valid
# image: the first six must be refused, the last two accepted.
EDGE_KINDS = (
    "empty", "short", "suffix-only", "long-suffix-length", "dfu-2.0", "no-fw-info",
    "dfu-1.1", "wildcard",
)
# The synthetic image ``synth`` writes when a run has no firmware build.
SYNTHETIC_REVISION = "harness-synthetic"
SYNTHETIC_IMAGE_NAME = "harness"
# The API version the synthetic block reports. It is not any version the
# tools speak, so ubertooth-dfu warns that the image and the tools report
# different ones; a synthetic image is not built for a board, so the harness
# does not track the fork's number here.
SYNTHETIC_API_VERSION = 0

_VERSION_RE = FIRMWARE_VERSION_RE


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


class FwInfo(NamedTuple):
    """A firmware info block read out of an image."""
    image: str
    revision: str
    api_version: int


def fw_info_block(image_name: str = SYNTHETIC_IMAGE_NAME,
                  revision: str = SYNTHETIC_REVISION,
                  api_version: int = SYNTHETIC_API_VERSION) -> bytes:
    """The 64-byte firmware info block, laid out as the fork reads it.

    Both strings are zero-padded to their field, so a reader must not take
    one that fills its field as terminated.
    """
    if len(image_name) > FW_INFO_IMAGE_LEN or len(revision) > FW_INFO_REVISION_LEN:
        raise ValueError(f"'{image_name}' or '{revision}' does not fit the block")
    return (FW_INFO_MAGIC
            + struct.pack("<HH", FW_INFO_FORMAT, api_version)
            + bytes(4)
            + image_name.encode().ljust(FW_INFO_IMAGE_LEN, b"\0")
            + revision.encode().ljust(FW_INFO_REVISION_LEN, b"\0"))


def fw_info(image: bytes) -> FwInfo | None:
    """The firmware info block ``image`` carries, or None when it has none.

    This is dfu_read_image_info() plus ubertooth_fw_info_parse() in the fork:
    the magic, a known format version, and two strings that are non-empty and
    printable.
    """
    block = image[FW_INFO_OFFSET:FW_INFO_OFFSET + FW_INFO_LENGTH]
    if len(block) < FW_INFO_LENGTH or block[:len(FW_INFO_MAGIC)] != FW_INFO_MAGIC:
        return None
    format_version, api_version = struct.unpack("<HH", block[8:12])
    if format_version != FW_INFO_FORMAT:
        return None

    def text(field: bytes) -> str | None:
        value = field.split(b"\0")[0]
        if not value or any(byte < 0x20 or byte > 0x7E for byte in value):
            return None
        return value.decode()

    name = text(block[16:16 + FW_INFO_IMAGE_LEN])
    revision = text(block[32:32 + FW_INFO_REVISION_LEN])
    if name is None or revision is None:
        return None
    return FwInfo(name, revision, api_version)


def synthetic_binary(revision: str = SYNTHETIC_REVISION) -> bytes:
    """A deterministic stand-in firmware binary for runs without a build.

    It carries a firmware info block naming ``revision`` at the fixed offset
    ``-d`` reads it from, between filler bytes, and is not a whole number of
    256-byte blocks. It is never written to a board.
    """
    filler = bytearray((index * 37 + 11) % 251 for index in range(4000))
    block = fw_info_block(revision=revision)
    filler[FW_INFO_OFFSET:FW_INFO_OFFSET + FW_INFO_LENGTH] = block
    return bytes(filler)


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
    if kind == "no-fw-info":
        # The suffix stays valid; only the block's magic is wiped, so the
        # file passes every suffix check and is refused for not being an
        # image from this tree.
        start, end = FW_INFO_OFFSET, FW_INFO_OFFSET + len(FW_INFO_MAGIC)
        return with_suffix(body[:start] + bytes(len(FW_INFO_MAGIC)) + body[end:])
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


def app_vectors(image: bytes) -> tuple[int, int]:
    """The initial stack pointer and reset vector at the start of an image."""
    if len(image) < APP_VECTORS_LENGTH:
        raise ValueError(
            f"{len(image)} bytes is shorter than the two vector words"
        )
    return struct.unpack("<II", image[:APP_VECTORS_LENGTH])


def startable_problems(image: bytes, flash_top: int = FLASH_TOP) -> list[str]:
    """Why app_image_startable() will not start this image ([] when it will).

    The question the bootloader asks first, of the two words at the start of
    the application region, read here so a test can say what it built and
    what it expects the board to do with it: a stack pointer inside RAM and
    word-aligned, and a reset vector with the Thumb bit set pointing above
    the bootloader and below ``flash_top``. That much tells an image from no
    image, which is what the images below are built to be.

    It is not the whole of what the bootloader asks, and "[]" is therefore
    not a promise that a board would start the image: the two checksums
    app_image_region_startable() goes on to make are over what was written to
    the flash, and ubertooth-dfu fills both in as it writes, so neither can
    be judged from a file. What "[]" does mean is that this check does not
    refuse it, and a non-empty list means the board refuses it here, before
    either checksum is read.
    """
    stack_pointer, reset_vector = app_vectors(image)
    problems = []
    in_local_ram = APP_RAM_BASE < stack_pointer <= APP_RAM_LIMIT
    in_ahb_ram = APP_AHB_RAM_BASE < stack_pointer <= APP_AHB_RAM_LIMIT
    if not in_local_ram and not in_ahb_ram:
        problems.append(
            f"stack pointer 0x{stack_pointer:08x} is outside RAM "
            f"(0x{APP_RAM_BASE:08x}-0x{APP_RAM_LIMIT:08x} and "
            f"0x{APP_AHB_RAM_BASE:08x}-0x{APP_AHB_RAM_LIMIT:08x})"
        )
    elif stack_pointer % 4:
        problems.append(
            f"stack pointer 0x{stack_pointer:08x} is not word-aligned"
        )
    if not reset_vector & 1:
        problems.append(
            f"reset vector 0x{reset_vector:08x} is not a Thumb address "
            "(bit 0 clear)"
        )
    else:
        entry = reset_vector & ~1
        if not APPLICATION_OFFSET <= entry < flash_top:
            problems.append(
                f"reset vector entry 0x{entry:08x} is outside the application "
                f"flash (0x{APPLICATION_OFFSET:05x}-0x{flash_top:05x})"
            )
    return problems


def make_unstartable(kind: str, image: bytes) -> bytes:
    """An image the bootloader must refuse to start, from a valid one.

    Only the first two words are changed, and the suffix is written again
    over the result, so the file stays a valid Ubertooth DFU image carrying
    the same firmware info block: the host writes it, and the board is left
    with an application it must not start.

    What ubertooth-dfu then fills in cannot rescue such an image. It writes a
    vector checksum over these words and an image check block describing what
    it wrote, so both checksums are correct on the board -- and
    app_image_startable() still refuses the two words, which it is asked
    before either of them.
    """
    if kind not in UNSTARTABLE_KINDS:
        raise ValueError(f"unknown unstartable image kind '{kind}'")
    body = bytearray(image[:-SUFFIX_LENGTH])
    if len(body) < APP_VECTORS_LENGTH:
        raise ValueError("source image is shorter than the two vector words")
    stack_pointer, reset_vector = app_vectors(bytes(body))
    if kind == "erased-vectors":
        stack_pointer = reset_vector = 0xFFFFFFFF
    elif kind == "misaligned-stack":
        # Inside RAM, two bytes off the word boundary the processor needs.
        stack_pointer = APP_RAM_BASE + 0x3FFE
    elif kind == "stack-outside-ram":
        # A peripheral address: in range for nothing the check accepts.
        stack_pointer = 0x40000000
    elif kind == "arm-reset-vector":
        reset_vector &= ~1
    else:  # reset-vector-in-bootloader
        # An entry inside the bootloader's own flash, below the application.
        reset_vector = 0x00000201
    struct.pack_into("<II", body, 0, stack_pointer, reset_vector)
    return with_suffix(bytes(body))


def readback_problems(readback: bytes, image: bytes) -> list[str]:
    """Why ``readback`` (from -u) does not hold ``image`` as -d writes it."""
def image_block_count(image: bytes) -> int:
    """Blocks -d writes for ``image``: the whole file, rounded up."""
    return -(-len(image) // BLOCK_SIZE)


def image_block(image: bytes, index: int) -> bytes:
    """Block ``index`` of ``image`` as -d sends it, the last one padded."""
    block = image[index * BLOCK_SIZE:(index + 1) * BLOCK_SIZE]
    return block + b"\xff" * (BLOCK_SIZE - len(block))


def vector_checksum(block: bytes) -> int:
    """fill_vector_checksum(): the word that makes the eight add up to zero."""
    words = struct.unpack_from(f"<{VECTOR_CHECKSUM_WORDS}I", block)
    return -sum(words) & 0xFFFFFFFF


def carries_image_check(block: bytes) -> bool:
    """image_carries_check_block(): is there a block here to fill in?

    Only an image from a tree that has one carries it, and the tool writes
    over those 32 bytes in no other image: in an older image, or one for
    another device, they are the image's own code or data.
    """
    check = block[IMAGE_CHECK_OFFSET:IMAGE_CHECK_OFFSET + IMAGE_CHECK_LENGTH]
    if len(check) < IMAGE_CHECK_LENGTH or not check.startswith(IMAGE_CHECK_MAGIC):
        return False
    (format_version,) = struct.unpack_from("<H", check, IMAGE_CHECK_OFF_FORMAT)
    return format_version == IMAGE_CHECK_FORMAT


def image_check_sum(data: bytes) -> int:
    """ubertooth_image_check_sum(): little-endian words, added up and wrapped.

    ``data`` is rounded down to a whole number of words, as the fork's own
    loop is.
    """
    words = len(data) // 4
    return sum(struct.unpack_from(f"<{words}I", data)) & 0xFFFFFFFF


def written_first_block(image: bytes) -> bytes:
    """``image``'s first block as -d writes it (prepare_first_block()).

    The vector checksum always, and the record of this download in the image
    check block when the image carries one. The checksum in that record
    covers every byte the download writes, this block among them, so the
    whole of it adds up to zero; the word is zero while the sum is worked out.
    """
    first = bytearray(image_block(image, 0))
    struct.pack_into("<I", first, VECTOR_CHECKSUM_OFFSET, vector_checksum(first))
    if not carries_image_check(first):
        return bytes(first)
    blocks = image_block_count(image)
    region_length = blocks * BLOCK_SIZE
    struct.pack_into("<HIII", first, IMAGE_CHECK_OFFSET + IMAGE_CHECK_OFF_FLAGS,
                     IMAGE_CHECK_FLAGS, region_length,
                     region_length ^ 0xFFFFFFFF, 0)
    total = image_check_sum(bytes(first))
    for index in range(1, blocks):
        total += image_check_sum(image_block(image, index))
    struct.pack_into("<I", first, IMAGE_CHECK_OFFSET + IMAGE_CHECK_OFF_CHECKSUM,
                     -total & 0xFFFFFFFF)
    return bytes(first)


def written_image(image: bytes) -> bytes:
    """``image`` as -d leaves it in flash, its block padding left off.

    Every byte is the file's own except the fields written_fields() names.
    """
    if not image:
        return b""
    return (written_first_block(image) + image[BLOCK_SIZE:])[:len(image)]


def written_fields(image: bytes) -> tuple[WrittenField, ...]:
    """The fields -d fills in for ``image``, in offset order.

    A field past the end of a short image is left out: the tool pads the
    block it writes, but there is nothing in the file to compare with.
    """
    if not image:
        return ()
    fields = (VECTOR_CHECKSUM_FIELD,)
    if carries_image_check(image_block(image, 0)):
        fields += IMAGE_CHECK_FIELDS
    return tuple(field for field in fields
                 if field.offset + field.size <= len(image))


# plan_upload()'s line, as both the step that records it and the parser that
# reads the fields out of it match it, so the two cannot drift apart. The
# step wraps this in its own (?P<value>...) group.
READBACK_LINE_PATTERN = (
    r"Reading back (?P<length>\d+) bytes, "
    r"0x(?P<start>[0-9a-fA-F]+)-0x(?P<end>[0-9a-fA-F]+): *(?P<part>[^\n]*)")
_READBACK_RE = re.compile(READBACK_LINE_PATTERN)
# The flash figure in that line's tail, whichever way it was reached: "part
# 0x25011722 (LPC1754) has 128 KiB of flash", and the two assuming forms,
# "assuming the 128 KiB part this firmware is linked for". Both end in the
# size, so one pattern reads either.
_REPORTED_FLASH_RE = re.compile(r"(?P<kib>\d+) KiB")


class ReadbackPlan(NamedTuple):
    """What a -u run said it was about to read (plan_upload() in
    ubertooth-dfu.c): the length, the flash range, and where the figure came
    from -- the part it identified, or the size it assumed instead."""

    length: int
    start: int
    end: int
    part: str

    def problems(self) -> list[str]:
        """Why this is not a range -u can read ([] when it is)."""
        problems = []
        if self.start != APPLICATION_OFFSET:
            problems.append(f"the read-back starts at 0x{self.start:x}, not the "
                            f"application region's 0x{APPLICATION_OFFSET:x}")
        if self.end != self.start + self.length - 1:
            problems.append(f"0x{self.start:x}-0x{self.end:x} is not "
                            f"{self.length} bytes")
        if self.length not in READBACK_LENGTHS:
            sizes = ", ".join(describe_flash_size(size) for size in PART_FLASH_SIZES)
            # A part the fork's table has grown since PART_FLASH_SIZES was
            # written lands here, which is the drift this is meant to report
            # rather than pass. The flash size this length would belong to
            # says which entry the copy is missing, rather than leaving a
            # reader to work it back from the figure; it is put as a
            # consequence, since a tool that misprinted the length lands here
            # too and then describes no part at all (#283).
            implied = describe_flash_size(self.length + APPLICATION_OFFSET)
            problems.append(f"the read-back covers {self.length} bytes, which no "
                            "part in the harness's copy of the fork's part table "
                            f"explains ({sizes}); that length would be the "
                            f"application region of a part with {implied} of flash")
        problems += self._length_follows_the_reported_part()
        return problems

    def _length_follows_the_reported_part(self) -> list[str]:
        """Why the length does not follow the flash size the command named.

        The line says both the length and the flash the part has, so the
        arithmetic between them can be checked rather than assumed. Without
        this, a length that is wrong for its part but right for some other
        part passes: 16384 bytes on a 64 KiB part is the application region
        of a 32 KiB one, and both are in the table.
        """
        flash_size = self.reported_flash_size()
        if flash_size is None:
            # Every wording plan_upload() has ends in the flash the figure
            # came from. One that does not is a line this cannot check the
            # length against, which is said rather than passed over: it would
            # otherwise switch this check off without anyone noticing.
            return ["the read-back line does not say how much flash the part "
                    f"has, so {self.length} bytes cannot be checked against "
                    f"it: {self.part!r}"]
        expected = upload_length(flash_size)
        if self.length == expected:
            return []
        return [f"the read-back covers {self.length} bytes, but the command "
                f"reported a part with {describe_flash_size(flash_size)} of "
                f"flash, whose application region is {expected} bytes"]

    def reported_flash_size(self) -> int | None:
        """The flash the command said the part has, or None when its line
        named no figure this can read."""
        found = _REPORTED_FLASH_RE.search(self.part)
        return int(found["kib"]) * 1024 if found else None


def readback_plan(output: str) -> ReadbackPlan | None:
    """The range the -u run in ``output`` reported, or None for none.

    The line goes to standard error with the rest of the tool's progress, so
    a caller passes it whatever the command printed on both streams. The
    first match is taken, as the step that records the line takes it, and
    plan_upload() prints it once per command: a retried read-back reuses the
    length it settled before the first attempt rather than planning again.
    """
    found = _READBACK_RE.search(output)
    if found is None:
        return None
    return ReadbackPlan(int(found["length"]), int(found["start"], 16),
                        int(found["end"], 16), found["part"].strip())


def written_problems(readback: bytes, image: bytes) -> list[str]:
    """Why the flash in ``readback`` does not hold ``image`` as -d writes it.

    A field the tool fills in is checked against what the tool should have
    worked out, and named when it does not hold it; every other byte has to
    be the image file's own, and the last block's padding 0xff. This says
    nothing about how long the dump is, so a caller can tell flash that
    really differs from a read-back that only described itself oddly.
    """
    expected = written_image(image)
    written = readback[:len(expected)]
    problems = []
    if len(written) < len(expected):
        problems.append(f"read-back holds {len(written)} of the image's "
                        f"{len(expected)} bytes")
        expected = expected[:len(written)]
    rest = bytearray(written)
    for field in written_fields(image):
        if field.offset + field.size > len(expected):
            continue
        found = written[field.offset:field.offset + field.size]
        want = expected[field.offset:field.offset + field.size]
        if found != want:
            digits = field.size * 2
            problems.append(
                f"{field.name} at 0x{APPLICATION_OFFSET + field.offset:05x} is "
                f"0x{int.from_bytes(found, 'little'):0{digits}x}, expected "
                f"0x{int.from_bytes(want, 'little'):0{digits}x}")
        # Reported above, so the sweep below covers the bytes -d copied.
        rest[field.offset:field.offset + field.size] = want
    if bytes(rest) != expected:
        first = next(index for index, (a, b) in enumerate(zip(rest, expected))
                     if a != b)
        problems.append("application flash differs from the image at "
                        f"0x{APPLICATION_OFFSET + first:05x}")
    padded = image_block_count(image) * BLOCK_SIZE
    padding = readback[len(image):padded]
    if len(readback) >= padded and padding != b"\xff" * len(padding):
        problems.append("last block padding is not 0xff")
    return problems


def readback_problems(readback: bytes, image: bytes, *,
                      plan: ReadbackPlan | None) -> list[str]:
    """Why ``readback`` (from -u) does not hold ``image`` as -d writes it.

    ``plan`` is the range the command reported (readback_plan()). How long a
    dump is follows the part the command identified, and nothing else here
    knows which part answered, so a command that reported no range leaves the
    length unchecked -- which is itself a problem rather than something to
    pass over.
    """
    problems = []
    if plan is None:
        problems.append("the read-back did not report the range it covers")
    else:
        problems += plan.problems()
        if len(readback) != plan.length:
            problems.append(f"read-back is {len(readback)} bytes, not the "
                            f"{plan.length} the command reported")
    problems += written_problems(readback, image)
    return problems


# ---------------------------------------------------------------------------
# Device commands

Runner = Callable[[Sequence[str], float], CommandResult]


class Readback(NamedTuple):
    """A -u run: why it failed ("" when it worked), and the range it said it
    was reading, which is what the dump's length is checked against."""

    failure: str
    plan: ReadbackPlan | None


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

    def read_back(self, path: Path) -> Readback:
        """``-u PATH -r`` on the selected application."""
        code, output = self._dfu(
            ("ubertooth-dfu", "-U", self.serial, "-u", str(path), "-r"), DFU_READ_TIMEOUT_S
        )
        failure = "" if code == 0 else f"read-back exited {code}: {_last_line(output)}"
        return Readback(failure, readback_plan(output))

    def adopt_dfu_device(self, revisions: dict | None = None) -> str:
        """"" when the one attached DFU device must be this board, else why not.

        DFU mode has no serial, so the board is identified by elimination:
        exactly one DFU device attached and every guard answering in its own
        application. A command without -U can then be aimed at it.

        ``revisions``, if given, receives each guard's revision as it is
        read, so a caller that is about to write without a selector can
        check afterwards that no guard was the board it reached.
        """
        count = self.dfu_count()
        if count != 1:
            return f"{count} DFU devices attached; recover the board by hand"
        for guard in self.guards:
            revision = self.revision(guard)
            if revisions is not None:
                revisions[guard] = revision
            if revision is None:
                return (f"guard board {guard} does not answer, so the DFU device may be it; "
                        "recover by hand")
        return ""

    def write_unselected(self, image: Path) -> str:
        """``-d IMAGE -r`` with no -U: write the one attached DFU device.

        For a board whose flash holds an application it cannot start, which
        is why it has no serial to select it by. Call adopt_dfu_device()
        first: without -U this writes whichever device answers DFU, and if
        none does by the time it runs, ubertooth-dfu takes the board a
        no-selector command would -- which is why the caller also checks
        afterwards that every guard still runs what it ran (changed_guards).
        """
        code, output = self._dfu(
            ("ubertooth-dfu", "-d", str(image), "-r"), DFU_WRITE_TIMEOUT_S
        )
        return "" if code == 0 else f"write exited {code}: {_last_line(output)}"

    def changed_guards(self, revisions: dict) -> list[str]:
        """Guards no longer running what ``revisions`` recorded for them.

        A revision is all there is to compare, so a guard that was already
        running the image that was written cannot be told from one that was
        not written at all. That is the ordinary case on a bench whose
        boards all carry the run's build, so this catches the write that
        went astray only where it changed something.
        """
        changed = []
        for guard, before in revisions.items():
            now = self.revision(guard)
            if now != before:
                changed.append(f"{guard} ran {before} and now reports {now}")
        return changed

    def leave_dfu(self, path: Path) -> Readback:
        """Return a board stuck in the DFU bootloader without writing.

        DFU mode has no serial, so the selected board is identified by
        elimination: exactly one DFU device and every guard answering in its
        application. ``ubertooth-dfu -u PATH -r`` without -U then adopts that
        device, reads it back, and detaches it; it writes nothing, even if the
        device vanished in between and it opened an application instead.
        """
        # The same elimination, and the same two messages, that
        # adopt_dfu_device() holds for every caller that writes without a
        # selector; here the failure is wrapped in main's Readback.
        failure = self.adopt_dfu_device()
        if failure:
            return Readback(failure, None)
        code, output = self._dfu(("ubertooth-dfu", "-u", str(path), "-r"), DFU_READ_TIMEOUT_S)
        plan = readback_plan(output)
        if code != 0:
            return Readback(f"DFU read-back and reset exited {code}: {_last_line(output)}",
                            plan)
        self.log("Left DFU: read back the bootloader's flash and reset it")
        return Readback("", plan)

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
    seen = board.revision()
    if seen is None:
        problems.append("board left its application (no answer to ubertooth-util -v)")
        read = board.leave_dfu(readback)
        if read.failure:
            print(f"After rejection: {problems[0]}; {read.failure}")
            return 1
    else:
        print(f"Board answers: {seen}")
        if seen != revision:
            problems.append(f"firmware revision {seen}, expected {revision}")
        read = board.read_back(readback)
        if read.failure:
            problems.append(read.failure)
    if not read.failure:
        differences = readback_problems(readback.read_bytes(), image.read_bytes(),
                                        plan=read.plan)
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
        read = board.leave_dfu(readback)
        if read.failure:
            print(f"Restore: {read.failure}")
            return 1
    else:
        read = board.read_back(readback)
        failed_read_back = read.failure
        if failed_read_back:
            # A failed -u ends without its -r reset (fork #208): return the
            # board, but fail the cleanup so the run stops on the record.
            if board.revision() is not None:
                print(f"Restore: {failed_read_back}")
                return 1
            read = board.leave_dfu(readback)
            if read.failure:
                print(f"Restore: {failed_read_back}; {read.failure}")
                return 1
    if board.wait_revision() is None:
        print("Restore: board did not return to its application")
        return 1
    data = image.read_bytes()
    dump = readback.read_bytes()
    problems = readback_problems(dump, data, plan=read.plan)
    if problems and not written_problems(dump, data):
        # The flash holds the image: what is wrong is what the read-back said
        # about itself (no range, or one that does not add up), and another
        # read-back would say the same. A rewrite would spend a flash cycle
        # on a shared board for nothing.
        print(f"Restore: {'; '.join(problems)}")
        return 1
    if problems:
        print("Flash differs from the test image; rewriting it")
        failure = board.write(image)
        if failure:
            print(f"Restore: {failure}")
            return 1
        # The rewrite may have been fine and the second read-back the thing
        # that failed, so the two are told apart and the reason is named.
        read = board.read_back(readback)
        if read.failure:
            print(f"Restore: the read-back after the rewrite failed: {read.failure}")
            return 1
        dump = readback.read_bytes()
        remaining = readback_problems(dump, data, plan=read.plan)
        if written_problems(dump, data):
            print("Restore: flash still differs after the rewrite: "
                  + "; ".join(remaining))
            return 1
        if remaining:
            # The flash holds the image; it is the read-back that is odd.
            print("Restore: after the rewrite: " + "; ".join(remaining))
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
    symlink = sub.add_parser("symlink", help="PATH is still a symbolic link to TARGET")
    symlink.add_argument("path")
    symlink.add_argument("--to", required=True, help="what it must still name")
    sub.add_parser("not-root", help="say whether this process runs as root")
    state.add_argument("--allow", action="append", default=[],
                       help="an entry that may change (or appear)")
    readback = sub.add_parser("check-readback")
    readback.add_argument("readback")
    readback.add_argument("image")
    readback.add_argument("--reported", required=True,
                          help="the 'Reading back ...' line the -u run printed, "
                               "which says how long the dump is")
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
    if command == "symlink":
        # A link is a path the user arranged, so a run that removed it, or
        # replaced it with a file of its own, has taken that choice away.
        path = Path(args.path)
        if not path.is_symlink():
            print(f"Not a symbolic link: {args.path}: "
                  + ("it is gone" if not path.exists() else "it is a file now"))
            return 1
        names = os.readlink(path)
        if names != args.to:
            print(f"Symbolic link changed: {args.path} -> {names}, not {args.to}")
            return 1
        print(f"Symbolic link: {args.path} -> {names}")
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
        plan = readback_plan(args.reported)
        data = Path(args.readback).read_bytes()
        problems = readback_problems(data, Path(args.image).read_bytes(), plan=plan)
        if problems:
            print("Read-back mismatch: " + "; ".join(problems))
            return 1
        # A --reported that named no range is one of those problems, so by
        # here the command did report one and the verdict can quote it.
        print(f"Read-back covers 0x{plan.start:x}-0x{plan.end:x}: {plan.part}")
        # Name the fields really checked: an image carrying no image check
        # block has only the vector checksum written over.
        checked = ", ".join(field.name
                            for field in written_fields(Path(args.image).read_bytes()))
        print(f"Read-back: {len(data)} bytes, image and 0xff padding match, with "
              f"{checked or 'no field'} as -d fills in")
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
