"""What bootloader a board has, and installing a newer one over a programmer.

A board's bootloader is the first 16 KiB of its flash. `ubertooth-dfu` talks
to it, so it is the one part of flash an ordinary firmware write cannot
replace: a board keeps the bootloader it came with through every update until
somebody puts another one there over a serial in-system programming adapter or
JTAG. The Ubertooth fork reports which one a board has on
`ubertooth-util -B`::

    Bootloader: 2.00 2026-11-DEV1+22, CRC32:0x9d2fe1b4
    Bootloader: 2.00 git-1a2b3c4, CRC32:0x9d2fe1b4
    Bootloader: no version block (factory or earlier, treated as 1.x), CRC32:0x5f0a73c1

The build string in the block is the version the tree reported when the
bootloader was built: the release it came from since #352 (fork #1230), and a
commit hash before that, which is the second form above and is what a board
flashed from an older commit carries.

The first form comes from a 48-byte version block the fork's bootloader
carries at a fixed offset; the second is every board as shipped, and any
bootloader built before the block existed. The CRC-32 is of the whole 16 KiB
region, worked out on the board, and is on the line either way.

This module answers three questions for a run.

**What is installed.** ``parse_identity`` reads that line into a
``BootloaderIdentity``: the version, the build string, and the region hash.

**What ought to be installed.** ``BootloaderArtifactBuilder`` builds the
bootloader from the resolved Ubertooth source and records a
``BootloaderArtifact``: the source commit it was built from, the version its
block carries, its build string, its size, and the CRC-32 the board will
report once it is installed. Nothing here guesses that number: the region a
board holds is the image padded to 16 KiB with the 0xff of erased flash, with
the vector checksum filled in, and the hash is the ordinary reflected CRC-32
over it -- the same definition the fork's own bootloader updater uses
(``firmware/bl_update/bl_payload.S`` and ``bl_update_file.c`` there).

**Which commit "latest" means.** The build string in the block is the whole
repository's revision at build time, so a bootloader built from today's main
and one built from yesterday's differ in that string -- and therefore in the
region hash -- even when nothing the bootloader is made of changed. Comparing
against the resolved commit would then demand a physical write to every board
on most days, for no change in behavior. So the artifact is built from the
newest commit, reachable from the resolved one, that touched the sources the
bootloader image is built from (``resolve_pin``). That commit is recorded with
the artifact, and ``verify_source_coverage`` reads the bootloader's own
makefile to check that no source it names falls outside the paths the pin
watches, so the pin cannot quietly stop covering the image.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import struct
import subprocess
import time
from typing import Callable, Mapping, Sequence
import zlib

from ubertooth_harness import release
from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.events import EventPublisher
from ubertooth_harness.executor import CommandExecutor, CommandResult
from ubertooth_harness.flash import HarnessError
from ubertooth_harness.patterns import (
    FIRMWARE_VERSION_RE,
    GIT_SHORT_HASH_MIN,
    same_revision,
)
from ubertooth_harness.rawlog import record_output
from ubertooth_harness.resources import DEBUG_PROGRAMMER


class BootloaderError(HarnessError):
    """A bootloader artifact, identity, or update cannot be had.

    A ``HarnessError``, so the command line reports it as the refusal it is
    rather than letting it out as a traceback.
    """


# ----- the fork's own numbers ----------------------------------------------
#
# All of these are in host/libubertooth/src/ubertooth_interface.h in the
# Ubertooth fork, which the firmware and the host tools both read them from.
# `verify_interface_constants` puts them against that header at the commit a
# run resolved, so a change there is reported rather than silently making
# every comparison here wrong.

INTERFACE_HEADER = "host/libubertooth/src/ubertooth_interface.h"
# The bootloader's region of flash: the 16 KiB below the application.
REGION_START = 0x0000
REGION_LENGTH = 0x4000
# What erased flash reads, and therefore what the region above the image holds.
ERASED_BYTE = 0xFF
# The version block: where it sits, what it starts with, and its layout.
VERSION_BLOCK_OFFSET = 0x100
VERSION_BLOCK_MAGIC = b"UBTBLVER"
VERSION_BLOCK_FORMAT = 1
VERSION_BLOCK_LENGTH = 48
BUILD_FIELD_LENGTH = 32
_OFF_FORMAT = 8
_OFF_VERSION = 10
_OFF_BUILD = 16
# The word of the vector table the LPC boot ROM adds up: the eight words at
# the bottom of the region must come to zero, so the eighth holds the two's
# complement of the seven before it. Whatever programs the region fills it in,
# so the region a board holds differs from the image file in these four bytes.
CHECKSUM_WORD_INDEX = 7
CHECKED_WORDS = 8
# The code-read-protection word. Setting it locks the part's debug and
# programming interfaces for good, so an artifact that asks for one is refused
# rather than written.
CRP_OFFSET = 0x2FC
CRP_LENGTH = 4
# The four values the boot ROM acts on (UM10360, "Code Read Protection").
CRP_VALUES = (0x12345678, 0x87654321, 0x43218765, 0x4E697370)

# The macro that says which bootloader generation this tree builds, and the
# request number the application answers `-B` from. A commit that has neither
# cannot report its bootloader at all.
_VERSION_MACRO = "UBERTOOTH_BOOTLOADER_VERSION"
_INFO_REQUEST = "UBERTOOTH_GET_BOOTLOADER_INFO"
_INTERFACE_MACROS = {
    _VERSION_MACRO: None,  # read, not compared: the fork may raise it
    "UBERTOOTH_BOOTLOADER_REGION_START": REGION_START,
    "UBERTOOTH_BOOTLOADER_REGION_LENGTH": REGION_LENGTH,
    "UBERTOOTH_BL_VERSION_FORMAT": VERSION_BLOCK_FORMAT,
    "UBERTOOTH_BL_VERSION_LENGTH": VERSION_BLOCK_LENGTH,
    "UBERTOOTH_BL_VERSION_BUILD_LEN": BUILD_FIELD_LENGTH,
    "UBERTOOTH_BL_VERSION_OFF_FORMAT": _OFF_FORMAT,
    "UBERTOOTH_BL_VERSION_OFF_VERSION": _OFF_VERSION,
    "UBERTOOTH_BL_VERSION_OFF_BUILD": _OFF_BUILD,
    "UBERTOOTH_LPC_CRP_OFFSET": CRP_OFFSET,
    "UBERTOOTH_LPC_CRP_LENGTH": CRP_LENGTH,
}
_MACRO_RE = re.compile(
    r"(?m)^#define\s+(?P<name>[A-Z_0-9]+)\s+(?P<value>0x[0-9a-fA-F]+|\d+)u?\b"
)
_HEADER_TIMEOUT_S = 20.0


# ----- the identity a board reports ----------------------------------------

#: The label `ubertooth-util -B` prints its one line under.
BOOTLOADER_LABEL = "Bootloader"
#: The detail the line carries for a board whose bootloader has no version
#: block, which is every board as shipped. It is an answer, not a failure, and
#: the command exits 0.
NO_VERSION_BLOCK_DETAIL = "no version block (factory or earlier, treated as 1.x)"
#: The generation such a board belongs to. Numbering starts at 2.00 with the
#: first bootloader to carry a block, so a board with none is older than that.
FACTORY_GENERATION = "1.x"

_IDENTITY_LINE_RE = re.compile(
    rf"(?m)^{BOOTLOADER_LABEL}: (?P<detail>.+), CRC32:0x(?P<crc>[0-9a-fA-F]{{8}})$"
)
# `%x.%02x` of a binary-coded-decimal version: 0x0200 reads as 2.00.
_VERSION_DETAIL_RE = re.compile(
    r"\A(?P<version>[0-9a-f]+\.[0-9a-f]{2}) (?P<build>\S+)\Z"
)
#: Pattern saying the command answered at all, whatever it answered. The same
#: pattern ``identity_from_match`` reads a match of, so a caller that searched
#: a command's saved output does not have to search it twice.
IDENTITY_LINE_PATTERN = _IDENTITY_LINE_RE.pattern
#: How long to give one `-B`: it is a single control transfer against a board
#: whose report was worked out at startup.
IDENTITY_TIMEOUT_S = 5.0


def identity_argv(serial: str) -> tuple[str, ...]:
    return ("ubertooth-util", "-U", serial, "-B")


@dataclass(frozen=True)
class BootloaderIdentity:
    """Which bootloader a board has, as its own `-B` line says.

    ``version`` and ``build`` are None when the region carries no version
    block this host can read; ``detail`` is then the reason the tool gave,
    which is the whole of what the line can say about such a bootloader. The
    hash is always there, and is what identifies one.
    """

    region_crc32: int
    detail: str
    version: str | None = None
    build: str | None = None

    @property
    def has_version_block(self) -> bool:
        return self.version is not None and self.build is not None

    @property
    def generation(self) -> str:
        """The bootloader generation, as a reader would name it."""
        return self.version if self.has_version_block else FACTORY_GENERATION

    @property
    def crc32_text(self) -> str:
        return f"0x{self.region_crc32:08x}"

    def describe(self) -> str:
        """One clause naming this bootloader, for a message or a report."""
        if self.has_version_block:
            return f"{self.version} {self.build}, CRC32:{self.crc32_text}"
        return f"{self.detail}, CRC32:{self.crc32_text}"

    def line(self) -> str:
        """The `-B` line itself, as the tool prints it."""
        if self.has_version_block:
            detail = f"{self.version} {self.build}"
        else:
            detail = self.detail
        return f"{BOOTLOADER_LABEL}: {detail}, CRC32:{self.crc32_text}"

    def matches(self, other: "BootloaderIdentity") -> bool:
        """Is this the same bootloader image as ``other``?

        All three of version, build string, and region hash, never the
        version on its own: two different 2.00 images are two different
        bootloaders, and the hash is what tells them apart. A board with no
        version block matches nothing that has one, whatever its hash.
        """
        return (
            self.has_version_block
            and other.has_version_block
            and self.version == other.version
            and self.build == other.build
            and self.region_crc32 == other.region_crc32
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "build": self.build,
            "generation": self.generation,
            "region_crc32": self.crc32_text,
            "has_version_block": self.has_version_block,
            "detail": self.detail,
        }


def names_commit(build: str, commit: str) -> bool:
    """Does a bootloader's build string name ``commit``?

    `git-<hash>`, where the hash is an abbreviation of the commit, which is
    what a bootloader built from a commit before #352 (fork #1230) carries.
    Git abbreviates to as many digits as that clone needs, and the artifact's
    build string and the pin come from two checkouts, so the hash is compared
    as a prefix rather than for equality. A `*` (a modified tree) fails,
    because nobody could build those bytes again.
    """
    if not build.startswith("git-"):
        return False
    digits = build[len("git-"):].lower()
    return (
        len(digits) >= GIT_SHORT_HASH_MIN
        and all(character in "0123456789abcdef" for character in digits)
        and commit.lower().startswith(digits)
    )


def names_build(build: str, commit: str, expected_version: str) -> bool:
    """Is ``build`` the string a build of ``commit`` reports?

    Since #352 (fork #1230) the block carries the release the build came
    from, which ``release.build_version`` works out the same way the makefile
    does, so the two are compared for equality: a release name is one string
    the whole way through and there is no shorter form of it to accept. A
    build from an older commit stamps a commit hash, and ``expected_version``
    says so by being one; then the hash is compared against the commit as
    above. A `*` fails either way.
    """
    if expected_version.startswith("git-"):
        return names_commit(build, commit)
    return bool(expected_version) and build == expected_version


def identity_from_match(match: re.Match | None) -> BootloaderIdentity | None:
    """A ``IDENTITY_LINE_PATTERN`` match as an identity, or None for no match.

    For a caller that searched the command's saved output rather than a
    string it holds, so one place reads the line either way.
    """
    if match is None:
        return None
    crc = int(match["crc"], 16)
    detail = match["detail"]
    fields = _VERSION_DETAIL_RE.match(detail)
    if fields is None:
        # No block, or a block this host would not read: the detail is the
        # tool's own reason and the hash is what is left to go on.
        return BootloaderIdentity(region_crc32=crc, detail=detail)
    return BootloaderIdentity(
        region_crc32=crc,
        detail=detail,
        version=fields["version"],
        build=fields["build"],
    )


def parse_identity(text: str) -> BootloaderIdentity | None:
    """The bootloader identity in a `-B` answer, or None when there is none.

    None covers firmware that refuses the request, a board that could not be
    opened, and any other output with no `Bootloader:` line in it.
    """
    return identity_from_match(_IDENTITY_LINE_RE.search(text or ""))


def identity_pattern(expected: "BootloaderIdentity") -> str:
    """Pattern matching exactly the `-B` line of ``expected``."""
    return r"(?m)^" + re.escape(expected.line()) + r"$"


# ----- the region a board will hold ---------------------------------------


def vector_checksum(region: bytes) -> int:
    """The checksum word a bootloader's vector table needs.

    The LPC boot ROM adds up the eight words at the bottom of flash and
    starts the code only when they come to zero, so the eighth word holds the
    two's complement of the seven in front of it (UM10360, "Criterion for
    valid user code").
    """
    if len(region) < CHECKED_WORDS * 4:
        raise BootloaderError(
            f"a bootloader region is at least {CHECKED_WORDS * 4} bytes; "
            f"got {len(region)}"
        )
    words = struct.unpack_from(f"<{CHECKSUM_WORD_INDEX}I", region, 0)
    return (0 - sum(words)) & 0xFFFFFFFF


def region_image(image: bytes) -> bytes:
    """The whole 16 KiB region as a board will hold it, from a built image.

    The image itself, padded to the region length with the 0xff of erased
    flash, and then the vector checksum filled in. Both steps matter to the
    hash: padding with anything else, or leaving the checksum word as the
    file has it, gives a number no board ever reports.
    """
    if not image:
        raise BootloaderError("the built bootloader image is empty")
    if len(image) > REGION_LENGTH:
        raise BootloaderError(
            f"the built bootloader image is {len(image)} bytes, past the "
            f"{REGION_LENGTH}-byte bootloader region"
        )
    region = bytearray(image)
    region += bytes([ERASED_BYTE]) * (REGION_LENGTH - len(region))
    struct.pack_into("<I", region, CHECKSUM_WORD_INDEX * 4, vector_checksum(region))
    return bytes(region)


def region_crc32(region: bytes) -> int:
    """The hash a board reports for this region.

    The ordinary reflected CRC-32 -- the one zlib, PNG and Ethernet use --
    which is what the firmware works out over the region at startup
    (``bl_report_crc32`` in the fork's ``firmware/common/bl_report.c``).
    """
    if len(region) != REGION_LENGTH:
        raise BootloaderError(
            f"the region to hash is {len(region)} bytes, not {REGION_LENGTH}"
        )
    return zlib.crc32(region) & 0xFFFFFFFF


def read_version_block(region: bytes) -> tuple[str, str]:
    """The version and build string a region's version block carries.

    Raises when there is no readable block: an artifact without one would
    install a bootloader that reports itself exactly as a factory one does,
    which would make the board harder to identify than it was.
    """
    block = region[VERSION_BLOCK_OFFSET:VERSION_BLOCK_OFFSET + VERSION_BLOCK_LENGTH]
    if len(block) != VERSION_BLOCK_LENGTH:
        raise BootloaderError(
            "the built bootloader image is too short to hold a version block"
        )
    if not block.startswith(VERSION_BLOCK_MAGIC):
        raise BootloaderError(
            "the built bootloader image carries no version block at offset "
            f"0x{VERSION_BLOCK_OFFSET:x}, so a board flashed with it could not "
            "report which bootloader it has"
        )
    (block_format,) = struct.unpack_from("<H", block, _OFF_FORMAT)
    if block_format != VERSION_BLOCK_FORMAT:
        raise BootloaderError(
            f"the built bootloader image carries a version block in format "
            f"{block_format}, not {VERSION_BLOCK_FORMAT}, which this harness reads"
        )
    (value,) = struct.unpack_from("<H", block, _OFF_VERSION)
    raw = block[_OFF_BUILD:_OFF_BUILD + BUILD_FIELD_LENGTH]
    build = raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    if not build or not build.isprintable() or "\ufffd" in build:
        raise BootloaderError(
            "the built bootloader image's version block holds no readable "
            "build string"
        )
    # The tool prints the version as `%x.%02x` of a binary-coded decimal word.
    version = f"{(value >> 8) & 0xFF:x}.{value & 0xFF:02x}"
    return version, build


def refuse_code_read_protection(region: bytes) -> None:
    """Refuse a region that asks the part to lock its own interfaces.

    Setting the code-read-protection word cannot be undone, so a region
    carrying one of its values is never written, however it got there.
    """
    (word,) = struct.unpack_from("<I", region, CRP_OFFSET)
    if word in CRP_VALUES:
        raise BootloaderError(
            f"the built bootloader image asks for code read protection "
            f"(0x{word:08x} at offset 0x{CRP_OFFSET:x}), which cannot be undone"
        )


# ----- Intel hex, which is what the programmer takes -----------------------

_HEX_RECORD_BYTES = 32


def intel_hex(data: bytes, *, start: int = 0, record_bytes: int = _HEX_RECORD_BYTES) -> str:
    """``data`` as an Intel hex file, one record per ``record_bytes``.

    Only 16-bit data records and the end-of-file record, which is all the
    bootloader region needs: it ends at 0x4000.
    """
    if start + len(data) > 0x10000:
        raise BootloaderError("Intel hex here covers only the first 64 KiB")
    lines = []
    for offset in range(0, len(data), record_bytes):
        chunk = data[offset:offset + record_bytes]
        address = start + offset
        record = bytes([len(chunk), (address >> 8) & 0xFF, address & 0xFF, 0x00]) + chunk
        checksum = (-sum(record)) & 0xFF
        lines.append(":" + record.hex().upper() + f"{checksum:02X}")
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


# ----- which commit "latest" means ----------------------------------------

#: The paths the bootloader image is built from, as repository paths. The pin
#: is the newest commit that touched any of them, so a commit elsewhere in the
#: tree does not change the artifact and does not ask for a write to a board.
#: ``firmware/common`` is here whole rather than file by file: the image links
#: several of its sources and both of its linker scripts, and a conservative
#: path set can only ever ask for a write that was not needed, never miss one
#: that was.
PIN_PATHS = (
    "firmware/bootloader",
    "firmware/common",
    INTERFACE_HEADER,
)
_PIN_TIMEOUT_S = 30.0

_BOOTLOADER_MAKEFILE = "firmware/bootloader/Makefile"
# A variable used in a source list, and every assignment the makefile makes.
# The values come from the makefile itself rather than being written here: a
# hardcoded path would keep being substituted after the makefile moved a
# source, and the coverage check below would then pass while the pin no longer
# covered it.
_VAR_REFERENCE = re.compile(r"\$\((?P<name>[A-Za-z_0-9]+)\)")
_VAR_ASSIGNMENT = re.compile(
    r"(?m)^(?P<name>[A-Za-z_0-9]+)[ \t]*(?::|\?|\+|!)?=[ \t]*(?P<value>[^\n#]*)"
)
# Every assignment form make takes, `+=` included: a source appended rather
# than listed must be covered too, or the pin would miss the one file that
# changed. An unknown form is refused by _makefile_sources below.
_ASSIGNMENT_RE = re.compile(
    r"(?m)^(?P<name>SRC|CPPSRC|ASRC|LINKER_SCRIPT)[ \t]*(?::|\?|\+|!)?="
    r"(?P<value>(?:[^\n\\]|\\\n)*)"
)


def _git(source: Path | str, *arguments: str, timeout_s: float) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), *arguments],
            capture_output=True, text=True, check=False, timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BootloaderError(f"git command failed in {source}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise BootloaderError(f"git command failed in {source}: {detail}")
    return result.stdout


def read_interface_header(source) -> str:
    """The fork's shared interface header at the resolved commit (test seam)."""
    return _git(
        source.source_path, "show", f"{source.commit}:{INTERFACE_HEADER}",
        timeout_s=_HEADER_TIMEOUT_S,
    )


def _macros(text: str) -> dict[str, int]:
    found = {}
    for match in _MACRO_RE.finditer(text):
        found.setdefault(match["name"], int(match["value"], 0))
    return found


def supported_at(source, *, read=read_interface_header) -> bool:
    """Can the Ubertooth commit this run resolved report its bootloader?

    A commit from before `-B` existed answers no, and a run against one
    records that rather than failing a check its tools cannot answer.
    """
    try:
        text = read(source)
    except BootloaderError:
        return False
    return _VERSION_MACRO in _macros(text) and _INFO_REQUEST in text


def verify_interface_constants(source, *, read=read_interface_header) -> int:
    """The bootloader version the resolved commit builds, with the layout checked.

    Returns the ``UBERTOOTH_BOOTLOADER_VERSION`` word. Raises when the commit
    cannot report a bootloader at all, or when one of the numbers this module
    works from has moved: every comparison here is over bytes at fixed
    offsets, so a changed offset would make them quietly wrong.
    """
    text = read(source)
    macros = _macros(text)
    if _VERSION_MACRO not in macros or _INFO_REQUEST not in text:
        raise BootloaderError(
            f"{source.name} {source.commit[:12]} cannot report a board's "
            "bootloader: its interface header has no "
            f"{_VERSION_MACRO} or {_INFO_REQUEST}"
        )
    differences = []
    for name, expected in _INTERFACE_MACROS.items():
        if expected is None:
            continue
        value = macros.get(name)
        if value is None:
            differences.append(f"{name} is not defined")
        elif value != expected:
            differences.append(f"{name} is 0x{value:x}, not 0x{expected:x}")
    # The version block's own offset, however the header spells it: a number,
    # or another macro this header defines (today UBERTOOTH_FW_INFO_OFFSET).
    # Every read in this module is at a fixed offset, so an offset that moved
    # and was not noticed would make all of them wrong.
    spelled = re.search(
        r"#define\s+UBERTOOTH_BL_VERSION_OFFSET\s+(?P<value>\S+)", text
    )
    if spelled is None:
        differences.append("UBERTOOTH_BL_VERSION_OFFSET is not defined")
    else:
        written = spelled["value"]
        offset = macros.get(written.rstrip("uU")) if not written.startswith(
            ("0x", "0X")
        ) and not written.isdigit() else int(written.rstrip("uU"), 0)
        if offset is None:
            differences.append(
                f"UBERTOOTH_BL_VERSION_OFFSET is {written}, which this harness "
                "cannot read as a number"
            )
        elif offset != VERSION_BLOCK_OFFSET:
            differences.append(
                f"the version block offset is 0x{offset:x}, not "
                f"0x{VERSION_BLOCK_OFFSET:x}"
            )
    magic = re.search(
        r"#define\s+UBERTOOTH_BL_VERSION_MAGIC\s+\"([^\"]*)\"", text
    )
    if magic is None or magic.group(1).encode() != VERSION_BLOCK_MAGIC:
        differences.append(
            "UBERTOOTH_BL_VERSION_MAGIC is not "
            f"{VERSION_BLOCK_MAGIC.decode()}"
        )
    if differences:
        raise BootloaderError(
            f"{INTERFACE_HEADER} at {source.name} {source.commit[:12]} no longer "
            "agrees with what this harness reads a bootloader region by: "
            + "; ".join(differences)
        )
    return macros[_VERSION_MACRO]


def _assignments(text: str) -> dict[str, str]:
    """Every variable the makefile sets, by its first assignment."""
    assigned: dict[str, str] = {}
    for match in _VAR_ASSIGNMENT.finditer(text):
        assigned.setdefault(match["name"], match["value"].strip())
    return assigned


def _expand(value: str, assigned: Mapping[str, str], where: str,
            seen: tuple[str, ...] = ()) -> str:
    """``value`` with the makefile's own variables substituted.

    A variable the makefile does not set, and a definition that refers to
    itself, are refused: this expansion decides which files the pin has to
    cover, so a value it had to guess at would be a pin that misses one.
    """
    def one(match: re.Match) -> str:
        name = match["name"]
        if name in seen:
            raise BootloaderError(
                f"{_BOOTLOADER_MAKEFILE} defines {name} in terms of itself, so "
                f"{where} cannot be read"
            )
        if name not in assigned:
            raise BootloaderError(
                f"{_BOOTLOADER_MAKEFILE} builds {where} from $({name}), which "
                "it does not set, so this harness cannot say which files the "
                "image is built from"
            )
        return _expand(assigned[name], assigned, where, seen + (name,))

    return _VAR_REFERENCE.sub(one, value)


def _linker_script_dirs(text: str, assigned: Mapping[str, str]) -> tuple[str, ...]:
    """Where the makefile looks for its linker script, in its order.

    ``LINKER_SCRIPT_DIRS`` there: the image's own directory and the library
    paths. A script named without a directory is recorded at the first of
    these that has it, so the sources a run reports are paths that exist.
    """
    value = assigned.get("LINKER_SCRIPT_DIRS")
    if value is None:
        return (".",)
    expanded = _expand(value, assigned, "LINKER_SCRIPT_DIRS")
    found = tuple(item for item in shlex.split(expanded, comments=True) if item)
    return found or (".",)


def _makefile_sources(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Every file the bootloader makefile names as part of its image.

    Paths as written, relative to ``firmware/bootloader``, with the makefile's
    own variables put back in, and the directories it searches for a linker
    script named without one.
    """
    assigned = _assignments(text)
    found: list[str] = []
    for match in _ASSIGNMENT_RE.finditer(text):
        value = _expand(
            match["value"].replace("\\\n", " "), assigned, match["name"]
        )
        found += [item for item in shlex.split(value, comments=True) if item]
    return tuple(dict.fromkeys(found)), _linker_script_dirs(text, assigned)


def _in_tree(source_path: Path | str, commit: str, path: Path) -> bool:
    """Does ``path`` exist in the tree at ``commit``?"""
    try:
        _git(
            source_path, "cat-file", "-e", f"{commit}:{path.as_posix()}",
            timeout_s=_PIN_TIMEOUT_S,
        )
    except BootloaderError:
        return False
    return True


def verify_source_coverage(
    source_path: Path | str, commit: str, *, paths: Sequence[str] = PIN_PATHS
) -> tuple[str, ...]:
    """Check that every bootloader source falls under the pinned paths.

    Returns the sources it found. Raises when one of them is outside
    ``paths``, because the pin would then not move when that file changed and
    a board would be left on a bootloader the harness called current.
    """
    text = _git(
        source_path, "show", f"{commit}:{_BOOTLOADER_MAKEFILE}",
        timeout_s=_PIN_TIMEOUT_S,
    )
    prefix = Path(_BOOTLOADER_MAKEFILE).parent
    covered = [Path(item) for item in paths]
    named, script_dirs = _makefile_sources(text)
    sources = []
    uncovered = []
    for name in named:
        # Names are relative to firmware/bootloader. The linker script is
        # searched for in . and ../common; both are covered, so taking it as
        # a path in the image's own directory is enough here.
        candidates = [name]
        if "/" not in name:
            # A bare name is a linker script, which the makefile searches for
            # in the directories it lists.
            candidates = [f"{where}/{name}" for where in script_dirs]
        path = None
        for candidate in candidates:
            try:
                resolved = _normalize(prefix / candidate)
            except ValueError as error:
                raise BootloaderError(
                    f"{_BOOTLOADER_MAKEFILE} at {commit[:12]} names "
                    f"{candidate!r}, which is outside the repository: {error}"
                ) from error
            path = path or resolved
            if _in_tree(source_path, commit, resolved):
                path = resolved
                break
        sources.append(path.as_posix())
        if not any(
            path == candidate or candidate in path.parents for candidate in covered
        ):
            uncovered.append(path.as_posix())
    if uncovered:
        raise BootloaderError(
            f"{_BOOTLOADER_MAKEFILE} at {commit[:12]} builds the bootloader from "
            + ", ".join(sorted(set(uncovered)))
            + ", which the pinned paths ("
            + ", ".join(paths)
            + ") do not cover; the pin would not move when those files change"
        )
    return tuple(sources)


def _normalize(path: Path) -> Path:
    """``path`` with ``..`` segments resolved, without touching the filesystem."""
    parts: list[str] = []
    for part in path.parts:
        if part == ".":
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"path leaves the repository: {path.as_posix()}")
            parts.pop()
            continue
        parts.append(part)
    return Path(*parts) if parts else Path(".")


@dataclass(frozen=True)
class BootloaderPin:
    """The commit a run builds its bootloader artifact from."""

    commit: str
    short_commit: str
    subject: str
    paths: tuple[str, ...]
    sources: tuple[str, ...]
    resolved_commit: str

    @property
    def is_resolved_commit(self) -> bool:
        return self.commit == self.resolved_commit

    def to_dict(self) -> dict:
        return {
            "commit": self.commit,
            "short_commit": self.short_commit,
            "subject": self.subject,
            "paths": list(self.paths),
            "sources": list(self.sources),
            "resolved_commit": self.resolved_commit,
            "is_resolved_commit": self.is_resolved_commit,
        }


def resolve_pin(
    source, *, paths: Sequence[str] = PIN_PATHS
) -> BootloaderPin:
    """The newest commit reachable from ``source`` that touched the bootloader.

    ``source`` is the resolved Ubertooth ``manifest.SourceRevision``. The
    answer is a commit of that history, so the artifact is reproducible from
    the run's own record; it is the resolved commit itself whenever that
    commit changed the bootloader.
    """
    sources = verify_source_coverage(source.source_path, source.commit, paths=paths)
    text = _git(
        source.source_path, "log", "-1", "--format=%H%x00%h%x00%s",
        source.commit, "--", *paths,
        timeout_s=_PIN_TIMEOUT_S,
    ).strip("\n")
    if not text:
        raise BootloaderError(
            f"no commit reachable from {source.name} {source.commit[:12]} touches "
            + ", ".join(paths)
            + "; there is no bootloader source to pin"
        )
    commit, short_commit, subject = (text.split("\x00") + ["", ""])[:3]
    return BootloaderPin(
        commit=commit.strip().lower(),
        short_commit=short_commit.strip().lower(),
        subject=subject.strip(),
        paths=tuple(paths),
        sources=sources,
        resolved_commit=source.commit,
    )


# ----- building and recording the artifact --------------------------------

#: File names the artifact is written under, in the run's own directory.
IMAGE_NAME = "bootloader.bin"
REGION_NAME = "bootloader-region.bin"
REGION_HEX_NAME = "bootloader-region.hex"
#: The run-manifest artifact name of the region image, as `--artifact` and a
#: plan would write one.
ARTIFACT_NAME = "bootloader region"

BUILD_PHASE = "bootloader"
CLONE_TIMEOUT_S = 300.0
GIT_TIMEOUT_S = 120.0
MAKE_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class BootloaderArtifact:
    """A bootloader built from the pinned commit, and what it will report."""

    pin: BootloaderPin
    version: str
    build: str
    bootloader_version_word: int
    image_bytes: int
    region_bytes: int
    region_crc32: int
    region_path: Path
    region_hex_path: Path
    image_path: Path
    region_sha256: str

    def identity(self) -> BootloaderIdentity:
        """The `-B` line a board carrying this bootloader reports."""
        return BootloaderIdentity(
            region_crc32=self.region_crc32,
            detail=f"{self.version} {self.build}",
            version=self.version,
            build=self.build,
        )

    def describe(self) -> str:
        return self.identity().describe()

    def to_dict(self) -> dict:
        return {
            "pin": self.pin.to_dict(),
            "version": self.version,
            "build": self.build,
            "bootloader_version_word": f"0x{self.bootloader_version_word:04x}",
            "image_bytes": self.image_bytes,
            "region_bytes": self.region_bytes,
            "region_crc32": f"0x{self.region_crc32:08x}",
            "region_sha256": self.region_sha256,
            "region_path": str(self.region_path),
            "region_hex_path": str(self.region_hex_path),
            "image_path": str(self.image_path),
            "identity": self.identity().to_dict(),
        }


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class BootloaderArtifactBuilder:
    """Build the bootloader at the pin and record what it will report.

    The resolved commit's checkout is never changed: the pinned commit is
    checked out as a detached worktree of the clone the run already made, and
    the image is built there. Every command is recorded in the run, as the
    build's and the flash stage's are.
    """

    def __init__(self, executor: CommandExecutor):
        self.executor = executor
        self._output_dir: Path | None = None

    def _run(
        self,
        publisher: EventPublisher,
        command_id: str,
        operation: str,
        argv: Sequence[str],
        timeout_s: float,
        cancellation: CancellationToken | None = None,
    ) -> CommandResult:
        owner = {"phase": BUILD_PHASE, "command_id": command_id,
                 "operation": operation}
        publisher.emit("command_started", "running", {**owner, "argv": list(argv)})
        result, spilled = record_output(
            publisher,
            lambda on_output: self.executor.run(
                list(argv), timeout_s=timeout_s, cancellation=cancellation,
                on_output=on_output,
            ),
            details=dict(owner),
            output_dir=self._output_dir,
            spill_name=command_id,
        )
        status = (
            "canceled" if result.cancelled
            else "passed" if result.returncode == 0 and not result.timed_out
            else "failed"
        )
        publisher.emit("command_finished", status, {
            **owner, "returncode": result.returncode,
            "duration_s": result.duration_s, "timed_out": result.timed_out,
            "cancelled": result.cancelled, **spilled,
        })
        if result.cancelled:
            raise BootloaderError(
                f"building the bootloader was canceled during {operation}"
            )
        if result.timed_out or result.returncode != 0:
            reason = "timed out" if result.timed_out else f"exited {result.returncode}"
            raise BootloaderError(
                f"building the bootloader failed: {' '.join(argv)} {reason}"
            )
        return result

    def build(
        self,
        source,
        publisher: EventPublisher,
        *,
        checkout: Path,
        work_dir: Path,
        output_dir: Path,
        cancellation: CancellationToken | None = None,
        pin: BootloaderPin | None = None,
    ) -> BootloaderArtifact:
        """Build the artifact; ``checkout`` is a clone of the fork to work from.

        ``work_dir`` holds the pinned worktree and the artifact files, and
        stays in the run directory as the run's evidence of what was built.
        """
        self._output_dir = Path(output_dir)
        work_dir = Path(work_dir)
        token = cancellation or CancellationToken()
        version_word = verify_interface_constants(source)
        pin = pin or resolve_pin(source)
        publisher.emit("bootloader_build_started", "running", {
            "pin": pin.to_dict(),
            "bootloader_version_word": f"0x{version_word:04x}",
            "work_dir": str(work_dir),
        })
        try:
            artifact = self._build(
                publisher, token, checkout, work_dir, pin, version_word
            )
        except BootloaderError as error:
            publisher.emit("bootloader_build_finished", "failed",
                           {"reason": str(error), "pin": pin.to_dict()})
            raise
        publisher.emit("bootloader_build_finished", "passed", {
            "reason": "", "artifact": artifact.to_dict(),
        })
        return artifact

    def _build(
        self, publisher, token, checkout: Path, work_dir: Path,
        pin: BootloaderPin, version_word: int,
    ) -> BootloaderArtifact:
        work_dir.mkdir(parents=True, exist_ok=True)
        tree = work_dir / "source"
        self._run(
            publisher, "bootloader-worktree",
            "check out the pinned bootloader source",
            ("git", "-C", str(checkout), "worktree", "add", "--quiet",
             "--detach", str(tree), pin.commit),
            GIT_TIMEOUT_S, token,
        )
        # The makefile stamps the build string from `git log` in the tree, and
        # marks it `*` when `git status` finds a change; a fresh worktree can
        # look modified until the index is refreshed.
        self._run(
            publisher, "bootloader-refresh-index", "refresh the worktree index",
            ("git", "-C", str(tree), "update-index", "-q", "--refresh"),
            GIT_TIMEOUT_S, token,
        )
        self._run(
            publisher, "bootloader-verify-clean",
            "the pinned bootloader source is unmodified",
            ("git", "-C", str(tree), "diff-index", "--quiet", "HEAD", "--"),
            GIT_TIMEOUT_S, token,
        )
        self._run(
            publisher, "bootloader-make", "build the bootloader image",
            # `bin` only: the hex a programmer takes is written from the
            # whole region below, not from the makefile's own hex, which
            # covers the image alone.
            ("make", "-C", str(tree / "firmware" / "bootloader"), "bin"),
            MAKE_TIMEOUT_S, token,
        )
        built = tree / "firmware" / "bootloader" / IMAGE_NAME
        if not built.is_file():
            raise BootloaderError(
                f"the bootloader build wrote no {IMAGE_NAME} in {built.parent}"
            )
        image = built.read_bytes()
        region = region_image(image)
        refuse_code_read_protection(region)
        version, build = read_version_block(region)
        expected_version = f"{(version_word >> 8) & 0xFF:x}.{version_word & 0xFF:02x}"
        if version != expected_version:
            raise BootloaderError(
                f"the built bootloader reports version {version}, but "
                f"{INTERFACE_HEADER} at the pin defines {expected_version}"
            )
        # What a build at the pin reports, worked out the way the makefile
        # works it out: the release the pin came from, or the commit hash an
        # older commit's build stamps (release.py).
        try:
            expected_build = release.build_version(
                tree, pin.commit, kind="Ubertooth", short_commit=pin.short_commit
            ).value
        except release.ReleaseVersionError as error:
            raise BootloaderError(
                f"the version a bootloader built at {pin.short_commit} reports "
                f"cannot be worked out: {error}"
            ) from error
        if not names_build(build, pin.commit, expected_build):
            # A build string that is not the pinned commit's means the image
            # would be rebuilt, and rewritten to a board, on the next run. The
            # `*` a modified tree adds is refused for the same reason: nobody
            # could build those bytes again.
            raise BootloaderError(
                f"the built bootloader carries build string {build!r}, which is "
                f"not the {expected_build!r} a build at the pinned commit "
                f"{pin.short_commit} reports; its identity would not be "
                "reproducible"
            )
        region_path = work_dir / REGION_NAME
        region_path.write_bytes(region)
        hex_path = work_dir / REGION_HEX_NAME
        hex_path.write_text(intel_hex(region, start=REGION_START), encoding="ascii")
        image_path = work_dir / IMAGE_NAME
        image_path.write_bytes(image)
        return BootloaderArtifact(
            pin=pin,
            version=version,
            build=build,
            bootloader_version_word=version_word,
            image_bytes=len(image),
            region_bytes=len(region),
            region_crc32=region_crc32(region),
            region_path=region_path,
            region_hex_path=hex_path,
            image_path=image_path,
            region_sha256=_sha256(region),
        )


# ----- the update path over a paired programmer ---------------------------

#: What a run may be asked to do about its boards' bootloaders.
#:
#: ``record`` reads `-B` on every board the run requires firmware of, and
#: records what it found,
#: and is the default for a bench run and for the release suite: a bootloader
#: write is a physical operation on a board that cannot be undone over USB, so
#: it is never implicit. ``check`` also compares what it found with the run's
#: own artifact and fails the run when a board is on an older or different
#: one. ``update`` installs the artifact on a stale board whose registry pairs
#: it with a programmer, and fails on a stale board that has none.
MODE_RECORD = "record"
MODE_CHECK = "check"
MODE_UPDATE = "update"
MODES = (MODE_RECORD, MODE_CHECK, MODE_UPDATE)
#: The modes that need an artifact to compare against, and so a build.
COMPARING_MODES = (MODE_CHECK, MODE_UPDATE)

#: The programmer command the Ubertooth repository documents for installing a
#: bootloader (``docs/source/programming.rst`` there, and the `program` target
#: of ``firmware/bootloader/Makefile``), with the repository's baud rate and
#: crystal frequency.
PROGRAMMER_TOOL = "lpc21isp"
PROGRAMMER_BAUD = "230400"
PROGRAMMER_CRYSTAL_KHZ = "4000"
#: One ISP write of 16 KiB at that baud rate, with the part's own erase and
#: the adapter's reset handshake in front of it.
PROGRAM_TIMEOUT_S = 180.0
#: The exit status a shell gives for a program it could not start, which the
#: executor passes on: the programmer never ran, so nothing was written.
NOT_STARTED = 127
#: After the write the board starts its application again and re-enumerates.
REBOOT_TIMEOUT_S = 20.0
VERIFY_ATTEMPTS = 6
VERIFY_RETRY_S = 2.0
UPDATE_PHASE = "bootloader-update"


def program_argv(hex_path: Path | str, device: str, *, tool: str = PROGRAMMER_TOOL):
    """The repository's documented programmer command line."""
    return (
        tool, "-control", str(hex_path), device,
        PROGRAMMER_BAUD, PROGRAMMER_CRYSTAL_KHZ,
    )


@dataclass(frozen=True)
class BootloaderBoard:
    """A selected Ubertooth, and the programmer the registry pairs with it."""

    alias: str
    serial: str
    programmer_alias: str | None = None
    programmer_serial: str | None = None
    programmer_device: str | None = None

    @property
    def has_programmer(self) -> bool:
        return self.programmer_alias is not None

    @property
    def can_be_programmed(self) -> bool:
        return self.has_programmer and bool(self.programmer_device)

    def to_dict(self) -> dict:
        return {
            "resource": self.alias,
            "serial": self.serial,
            "programmer": self.programmer_alias,
            "programmer_serial": self.programmer_serial,
            "programmer_device": self.programmer_device,
        }


def programmer_for(registry, alias: str):
    """The debug programmer wired to ``alias``, or None.

    Only a programmer whose own ``programs:`` names this board: a chain's far
    end is wired to another programmer, not to the board, and could not write
    its flash.
    """
    for other in registry.pair_members(alias):
        resource = registry.get(other)
        if resource.type == DEBUG_PROGRAMMER and resource.programs == alias:
            return resource
    return None


def boards_for_bootloader(registry, aliases: Sequence[str]) -> tuple[BootloaderBoard, ...]:
    """The selected Ubertooths with their programmers, in the order given."""
    boards = []
    for alias in dict.fromkeys(aliases):
        resource = registry.get(alias)
        serial = str(resource.identity.get("serial", "") or "").strip().lower()
        if not serial:
            raise BootloaderError(
                f"Ubertooth {alias} has no registered serial, so its bootloader "
                "cannot be read"
            )
        programmer = programmer_for(registry, alias)
        boards.append(BootloaderBoard(
            alias=alias,
            serial=serial,
            programmer_alias=programmer.alias if programmer else None,
            programmer_serial=(
                str(programmer.identity.get("serial", "")) if programmer else None
            ),
            programmer_device=(
                str(programmer.identity.get("serial device", "")) or None
                if programmer else None
            ),
        ))
    return tuple(boards)


def no_programmer_reason(board: BootloaderBoard, installed, expected) -> str:
    """Why a stale board cannot be brought to the expected bootloader here.

    The missing capability is named, because that is what somebody has to act
    on: a bootloader lives in the region `ubertooth-dfu` itself runs from, so
    an ordinary firmware write cannot replace it. Installing one needs a
    serial in-system programming adapter or JTAG on the board's header, and
    the harness only ever writes one through a programmer its registry pairs
    with that board.
    """
    if not board.has_programmer:
        missing = (
            f"no debug programmer is registered for {board.alias} "
            f"('programs: {board.alias}' on a 'Debug programmer' resource)"
        )
    else:
        missing = (
            f"{DEBUG_PROGRAMMER} {board.programmer_alias} has no 'serial device' "
            "in the registry, so there is no port to program through"
        )
    return (
        f"{board.alias} has bootloader {installed.describe()}, and this run "
        f"expects {expected.describe()}: {missing}. A bootloader cannot be "
        "written through the region ubertooth-dfu runs from, so installing one "
        f"needs {PROGRAMMER_TOOL} on a programmer wired to the board's "
        "in-system programming header"
    )


@dataclass(frozen=True)
class BoardBootloaderResult:
    """What the run found on one board, and what it did about it."""

    board: BootloaderBoard
    status: str  # current, updated, stale, unreadable, failed, skipped
    installed: BootloaderIdentity | None
    reason: str = ""
    written: bool = False
    previous: BootloaderIdentity | None = None

    def to_dict(self) -> dict:
        return {
            **self.board.to_dict(),
            "status": self.status,
            "written": self.written,
            "installed": self.installed.to_dict() if self.installed else None,
            "previous": self.previous.to_dict() if self.previous else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BootloaderStageSummary:
    mode: str
    status: str
    reason: str
    expected: BootloaderIdentity | None
    boards: tuple[BoardBootloaderResult, ...]

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "status": self.status,
            "reason": self.reason,
            "expected": self.expected.to_dict() if self.expected else None,
            "boards": [item.to_dict() for item in self.boards],
        }


class BootloaderUpdater:
    """Read the bootloader of each board given, and install the artifact where asked.

    One board at a time, with hard deadlines, inside the run's own locks: the
    caller must already hold each board, its programmer (the registry pairs
    them, so a lease of either takes both), and this host's firmware lock,
    because programming a board's flash while another command uses it leaves
    the board needing hands on it.

    A failed write is a hard failure that keeps every command and its output,
    and is never retried: a second attempt over a programmer that has already
    half-written the region would make the board's state less knowable, not
    more. Recovery is a deliberate run with the same programmer.
    """

    def __init__(
        self,
        executor: CommandExecutor,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.executor = executor
        self.sleep = sleep
        self._output_dir: Path | None = None

    def _run(
        self,
        publisher: EventPublisher,
        command_id: str,
        operation: str,
        argv: Sequence[str],
        timeout_s: float,
        cancellation: CancellationToken | None = None,
        *,
        owner: Mapping | None = None,
    ) -> CommandResult:
        details = {**dict(owner or {}), "phase": UPDATE_PHASE,
                   "command_id": command_id, "operation": operation}
        publisher.emit("command_started", "running",
                       {**details, "argv": list(argv)})
        result, spilled = record_output(
            publisher,
            lambda on_output: self.executor.run(
                list(argv), timeout_s=timeout_s, cancellation=cancellation,
                on_output=on_output,
            ),
            details=dict(details),
            output_dir=self._output_dir,
            spill_name=command_id,
        )
        status = (
            "canceled" if result.cancelled
            else "passed" if result.returncode == 0 and not result.timed_out
            else "failed"
        )
        publisher.emit("command_finished", status, {
            **details, "returncode": result.returncode,
            "duration_s": result.duration_s, "timed_out": result.timed_out,
            "cancelled": result.cancelled, **spilled,
        })
        return result

    def _read_identity(
        self, publisher, board, command_id, operation, cancellation,
    ) -> tuple[CommandResult, BootloaderIdentity | None]:
        result = self._run(
            publisher, command_id, operation, identity_argv(board.serial),
            IDENTITY_TIMEOUT_S, cancellation, owner={"resource": board.alias},
        )
        return result, parse_identity(result.stdout)

    def run(
        self,
        boards: Sequence[BootloaderBoard],
        publisher: EventPublisher,
        *,
        mode: str,
        artifact: BootloaderArtifact | None,
        output_dir: Path,
        application_revision: str,
        cancellation: CancellationToken | None = None,
        holds_host_lock: bool = False,
        programmer_tool: str = PROGRAMMER_TOOL,
    ) -> BootloaderStageSummary:
        if mode not in MODES:
            raise BootloaderError(
                f"unknown bootloader mode '{mode}'; use " + ", ".join(MODES)
            )
        if mode in COMPARING_MODES and artifact is None:
            raise BootloaderError(
                f"--bootloader {mode} needs a bootloader artifact to compare "
                "against; the run did not build one"
            )
        if mode == MODE_RECORD and artifact is not None:
            # Recording compares nothing, so an artifact here would only be a
            # caller that meant one of the other two modes.
            raise BootloaderError(
                "--bootloader record compares nothing, so it takes no "
                "bootloader artifact"
            )
        if mode == MODE_UPDATE and not holds_host_lock:
            # Fail before any board is touched: writing flash without this
            # host's firmware lock can run beside another command's power
            # change or write.
            raise BootloaderError(
                "installing a bootloader writes a board's flash and must hold "
                "this host's firmware lock; the run is not holding it"
            )
        self._output_dir = Path(output_dir)
        token = cancellation or CancellationToken()
        expected = artifact.identity() if artifact else None
        publisher.emit("bootloader_stage_started", "running", {
            "mode": mode,
            "expected": expected.to_dict() if expected else None,
            "boards": [board.to_dict() for board in boards],
        })
        results: list[BoardBootloaderResult] = []
        status, reason = "passed", ""
        for board in boards:
            if token.is_cancelled():
                status, reason = "canceled", f"canceled: {token.reason}"
                break
            outcome = self._one_board(
                publisher, board, mode, artifact, expected, token, programmer_tool,
                application_revision,
            )
            results.append(outcome)
            publisher.emit(
                "bootloader_identity",
                {"current": "passed", "updated": "passed"}.get(outcome.status, outcome.status),
                outcome.to_dict(),
            )
            if outcome.status in ("stale", "unreadable", "failed"):
                status, reason = "failed", outcome.reason
                break
            if outcome.status == "canceled":
                status, reason = "canceled", outcome.reason
                break
        summary = BootloaderStageSummary(
            mode=mode, status=status, reason=reason, expected=expected,
            boards=tuple(results),
        )
        publisher.emit("bootloader_stage_finished", status, summary.to_dict())
        return summary

    def _one_board(
        self, publisher, board, mode, artifact, expected, token, programmer_tool,
        application_revision,
    ) -> BoardBootloaderResult:
        result, installed = self._read_identity(
            publisher, board, f"bootloader-read-{board.alias}",
            "bootloader_identity_before", token,
        )
        if result.cancelled:
            return BoardBootloaderResult(
                board, "canceled", None, f"canceled: {token.reason}"
            )
        if installed is None:
            detail = (
                "command timed out" if result.timed_out
                else f"command exited {result.returncode}"
            )
            reason = (
                f"{board.alias} did not report its bootloader "
                f"(ubertooth-util -B: {detail})"
            )
            if mode == MODE_RECORD:
                # Recording only: say so and carry on. The preflight check
                # over the same read is what fails a run whose board cannot
                # answer, so this stage does not fail it twice.
                return BoardBootloaderResult(board, "skipped", None, reason)
            return BoardBootloaderResult(board, "unreadable", None, reason)
        if expected is None or installed.matches(expected):
            return BoardBootloaderResult(board, "current", installed)
        if mode != MODE_UPDATE or not board.can_be_programmed:
            # Only `update` writes. Anything else reports what it found, so a
            # mode this stage does not know cannot reach the programmer.
            return BoardBootloaderResult(
                board, "stale", installed,
                no_programmer_reason(board, installed, expected)
                if mode == MODE_UPDATE else
                f"{board.alias} has bootloader {installed.describe()}, and this "
                f"run expects {expected.describe()}",
            )
        return self._update_board(
            publisher, board, artifact, expected, installed, token, programmer_tool,
            application_revision,
        )

    def _update_board(
        self, publisher, board, artifact, expected, installed, token, programmer_tool,
        application_revision,
    ) -> BoardBootloaderResult:
        publisher.emit("bootloader_update_started", "running", {
            **board.to_dict(),
            "installed": installed.to_dict(),
            "expected": expected.to_dict(),
            "image": str(artifact.region_hex_path),
            "image_sha256": artifact.region_sha256,
        })
        argv = program_argv(
            artifact.region_hex_path, board.programmer_device, tool=programmer_tool
        )
        result = self._run(
            publisher, f"bootloader-program-{board.alias}",
            "bootloader_program", argv, PROGRAM_TIMEOUT_S, token,
            owner={"resource": board.alias},
        )
        if result.cancelled:
            # A write stopped part-way leaves the region in a state only the
            # board can say, and this run is not going to ask: the identity
            # is left unknown rather than guessed, and the next run reads it.
            return BoardBootloaderResult(
                board, "canceled", None, f"canceled: {token.reason}",
                written=True, previous=installed,
            )
        if result.returncode == NOT_STARTED and not result.timed_out:
            # The shell's status for a program it could not start. Nothing
            # reached the board, so its bootloader is the one it had: saying
            # the region is in an unknown state would send somebody to the
            # bench for a board nobody touched.
            return BoardBootloaderResult(
                board, "failed", installed,
                f"{programmer_tool} did not start for {board.alias} "
                f"({' '.join(argv)} exited {NOT_STARTED}); nothing was written, "
                f"so {board.alias} still has {installed.describe()}. Install "
                f"{programmer_tool} and check that "
                f"{board.programmer_device} is there",
                previous=installed,
            )
        if result.timed_out or result.returncode != 0:
            detail = "timed out" if result.timed_out else f"exited {result.returncode}"
            return BoardBootloaderResult(
                board, "failed", None,
                f"programming {board.alias} through {board.programmer_alias} "
                f"failed: {' '.join(argv)} {detail}. The region is in an unknown "
                "state and is not written again here; recover it with the same "
                "programmer",
                written=True, previous=installed,
            )
        # The board restarts its application and re-enumerates after the
        # write; give it a few tries before calling it gone.
        read = None
        now = None
        for attempt in range(1, VERIFY_ATTEMPTS + 1):
            if token.is_cancelled():
                return BoardBootloaderResult(
                    board, "canceled", None, f"canceled: {token.reason}",
                    written=True, previous=installed,
                )
            read, now = self._read_identity(
                publisher, board, f"bootloader-verify-{board.alias}-{attempt}",
                "bootloader_identity_after", token,
            )
            if read.cancelled:
                return BoardBootloaderResult(
                    board, "canceled", now, f"canceled: {token.reason}",
                    written=True, previous=installed,
                )
            if now is not None:
                break
            if attempt < VERIFY_ATTEMPTS:
                self.sleep(VERIFY_RETRY_S)
        if now is None:
            return BoardBootloaderResult(
                board, "failed", None,
                f"{board.alias} did not report its bootloader after the write, "
                f"in {VERIFY_ATTEMPTS} reads over "
                f"{VERIFY_ATTEMPTS * VERIFY_RETRY_S:.0f}s; the board's "
                "application may not be running",
                written=True, previous=installed,
            )
        if not now.matches(expected):
            return BoardBootloaderResult(
                board, "failed", now,
                f"{board.alias} reports bootloader {now.describe()} after the "
                f"write, not the {expected.describe()} that was written",
                written=True, previous=installed,
            )
        # The application the run requires is above the bootloader region and
        # the write does not touch it, so saying it still runs is cheap and is
        # what an operator wants to read before the tests start.
        application = self._run(
            publisher, f"bootloader-application-{board.alias}",
            "application_version_after_bootloader",
            ("ubertooth-util", "-U", board.serial, "-v"),
            IDENTITY_TIMEOUT_S, token, owner={"resource": board.alias},
        )
        reported = FIRMWARE_VERSION_RE.search(application.stdout)
        if application.returncode != 0 or reported is None or not same_revision(
            application_revision, reported["revision"]
        ):
            found = reported["revision"] if reported else "nothing readable"
            return BoardBootloaderResult(
                board, "failed", now,
                f"{board.alias} has the expected bootloader after the write, "
                f"but its application reports {found}, not the run's "
                f"{application_revision}",
                written=True, previous=installed,
            )
        publisher.emit("bootloader_update_finished", "passed", {
            **board.to_dict(),
            "previous": installed.to_dict(),
            "installed": now.to_dict(),
        })
        return BoardBootloaderResult(
            board, "updated", now, "", written=True, previous=installed
        )



# ----- what preflight asks of each board ----------------------------------


@dataclass(frozen=True)
class BootloaderExpectation:
    """What a run requires of its boards' bootloaders, for preflight.

    ``expected`` is None in ``record`` mode, where the check only asks that
    the board answered. ``boards`` is the selected board for each alias, so a
    refusal can name the programmer the bench has not got.
    """

    mode: str
    expected: BootloaderIdentity | None
    boards: Mapping[str, BootloaderBoard]

    @property
    def compares(self) -> bool:
        return self.expected is not None and self.mode in COMPARING_MODES

    def patterns(self) -> tuple[str, ...]:
        if self.compares:
            return (identity_pattern(self.expected),)
        return (IDENTITY_LINE_PATTERN,)

    def failure(self, alias: str | None, installed: BootloaderIdentity | None) -> str:
        """Why this board's `-B` answer is not what the run requires."""
        board = self.boards.get(alias or "")
        where = alias or "the selected board"
        if installed is None:
            # No line at all. The command's own exit status is what says why,
            # and PreflightCheck.result_failure reports it where there is one;
            # this wording is for a caller that has only the output, so it
            # names the usual cause without claiming it.
            return (
                f"{where} did not report its bootloader; firmware older than "
                "the bootloader read refuses it, which a flash with this run's "
                "build fixes"
            )
        if not self.compares:
            # Recording only, and the board answered: nothing here can fail.
            return ""
        if installed.matches(self.expected):
            return ""
        if self.mode == MODE_UPDATE and board is not None and not board.can_be_programmed:
            return no_programmer_reason(board, installed, self.expected)
        tail = ""
        if board is not None and not board.has_programmer:
            tail = (
                "; no debug programmer is registered for it, so this run cannot "
                "install one"
            )
        elif board is not None and not board.can_be_programmed:
            tail = (
                f"; {DEBUG_PROGRAMMER} {board.programmer_alias} is registered "
                "for it with no 'serial device', so there is no port to program "
                "through"
            )
        elif board is not None:
            tail = (
                f"; run with --bootloader {MODE_UPDATE} to install it through "
                f"{board.programmer_alias}"
            )
        return (
            f"{where} has bootloader {installed.describe()}, and this run "
            f"expects {self.expected.describe()}{tail}"
        )
