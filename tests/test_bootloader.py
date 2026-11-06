"""Bootloader identity, the artifact, and the paired-programmer update path.

No hardware: a fake `ubertooth-util -B`, a fake `lpc21isp`, and a real git
repository holding a bootloader makefile that stamps its build string the way
the fork's does.
"""

import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import unittest
import zlib

from types import MappingProxyType, SimpleNamespace

from ubertooth_harness import bootloader
from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.events import RUN_LOG, open_run_publisher
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.resources import FileResourceRegistry, Resource

from tests.executables import write_executable


SERIAL_A = "aaaa0000aaaa0000aaaa0000aaaa0000"
SERIAL_B = "bbbb1111bbbb1111bbbb1111bbbb1111"
PROGRAMMER_SERIAL = "A000AA00"
PROGRAMMER_DEVICE = "/dev/serial/by-id/usb-Vendor_Adapter_A000AA00-if00-port0"
RUN_REVISION = "git-1a2b3c4"


def version_block(version: int = 0x0200, build: str = "git-1a2b3c4",
                  magic: bytes = bootloader.VERSION_BLOCK_MAGIC,
                  block_format: int = bootloader.VERSION_BLOCK_FORMAT) -> bytes:
    block = bytearray(bootloader.VERSION_BLOCK_LENGTH)
    block[0:len(magic)] = magic
    struct.pack_into("<H", block, 8, block_format)
    struct.pack_into("<H", block, 10, version)
    encoded = build.encode()
    block[16:16 + len(encoded)] = encoded
    return bytes(block)


def built_image(build: str = "git-1a2b3c4", size: int = 0x2800, **block) -> bytes:
    """An image shaped like a built bootloader: vectors, then the block.

    The first eight words are a plausible vector table with the checksum word
    left at zero, which is how the toolchain writes it.
    """
    image = bytearray(b"\x00" * size)
    struct.pack_into("<I", image, 0, 0x10008000)   # initial stack pointer
    struct.pack_into("<I", image, 4, 0x00000201)   # Thumb reset vector
    for index in range(2, 7):
        struct.pack_into("<I", image, index * 4, 0x00000301 + index)
    offset = bootloader.VERSION_BLOCK_OFFSET
    image[offset:offset + bootloader.VERSION_BLOCK_LENGTH] = version_block(
        build=build, **block
    )
    return bytes(image)


class IdentityTests(unittest.TestCase):

    def test_a_bootloader_from_the_fork_reports_version_build_and_hash(self):
        identity = bootloader.parse_identity(
            "Bootloader: 2.00 git-1a2b3c4, CRC32:0x9d2fe1b4\n"
        )
        self.assertEqual("2.00", identity.version)
        self.assertEqual("git-1a2b3c4", identity.build)
        self.assertEqual(0x9D2FE1B4, identity.region_crc32)
        self.assertTrue(identity.has_version_block)
        self.assertEqual("2.00", identity.generation)
        self.assertEqual("2.00 git-1a2b3c4, CRC32:0x9d2fe1b4", identity.describe())

    def test_a_factory_bootloader_is_an_answer_with_only_its_hash(self):
        identity = bootloader.parse_identity(
            "Bootloader: no version block (factory or earlier, treated as "
            "1.x), CRC32:0x5f0a73c1\n"
        )
        self.assertFalse(identity.has_version_block)
        self.assertIsNone(identity.version)
        self.assertEqual(0x5F0A73C1, identity.region_crc32)
        self.assertEqual(bootloader.FACTORY_GENERATION, identity.generation)
        self.assertEqual(bootloader.NO_VERSION_BLOCK_DETAIL, identity.detail)

    def test_a_block_this_host_cannot_read_keeps_the_hash_and_no_version(self):
        identity = bootloader.parse_identity(
            "Bootloader: the bootloader version block is in a format this "
            "host does not know, CRC32:0x11223344\n"
        )
        self.assertFalse(identity.has_version_block)
        self.assertEqual(0x11223344, identity.region_crc32)

    def test_output_with_no_bootloader_line_reports_nothing(self):
        for text in ("", "error: bootloader information read failed\n",
                     "Bootloader: 2.00 git-1a2b3c4\n"):
            self.assertIsNone(bootloader.parse_identity(text), text)

    def test_the_line_the_identity_prints_is_the_one_the_tool_prints(self):
        for text in (
            "Bootloader: 2.00 git-1a2b3c4, CRC32:0x9d2fe1b4",
            "Bootloader: no version block (factory or earlier, treated as "
            "1.x), CRC32:0x5f0a73c1",
        ):
            self.assertEqual(text, bootloader.parse_identity(text + "\n").line())

    def test_two_different_images_of_one_version_are_not_the_same_bootloader(self):
        one = bootloader.parse_identity(
            "Bootloader: 2.00 git-1a2b3c4, CRC32:0x9d2fe1b4\n")
        same = bootloader.parse_identity(
            "Bootloader: 2.00 git-1a2b3c4, CRC32:0x9d2fe1b4\n")
        other_hash = bootloader.parse_identity(
            "Bootloader: 2.00 git-1a2b3c4, CRC32:0x00000001\n")
        other_build = bootloader.parse_identity(
            "Bootloader: 2.00 git-9999999, CRC32:0x9d2fe1b4\n")
        self.assertTrue(one.matches(same))
        self.assertFalse(one.matches(other_hash))
        self.assertFalse(one.matches(other_build))

    def test_a_bootloader_with_no_block_matches_nothing_that_has_one(self):
        factory = bootloader.parse_identity(
            "Bootloader: no version block (factory or earlier, treated as "
            "1.x), CRC32:0x9d2fe1b4\n")
        fork = bootloader.parse_identity(
            "Bootloader: 2.00 git-1a2b3c4, CRC32:0x9d2fe1b4\n")
        self.assertFalse(factory.matches(fork))
        self.assertFalse(fork.matches(factory))
        # Not even against itself: there is no version block to agree on.
        self.assertFalse(factory.matches(factory))

    def test_the_pattern_matches_only_that_one_line(self):
        import re

        identity = bootloader.parse_identity(
            "Bootloader: 2.00 git-1a2b3c4, CRC32:0x9d2fe1b4\n")
        pattern = bootloader.identity_pattern(identity)
        self.assertTrue(re.search(pattern, identity.line()))
        self.assertFalse(re.search(
            pattern, "Bootloader: 2.00 git-1a2b3c4, CRC32:0x00000001"))


class BuildStringTests(unittest.TestCase):
    """Which commit a bootloader's build string names."""

    COMMIT = "abf677c5b1234567890abcdef1234567890abcde"

    def test_an_abbreviation_of_the_commit_names_it(self):
        for digits in ("abf677c", "abf677c5b", "abf677c5b123456"):
            with self.subTest(digits):
                self.assertTrue(
                    bootloader.names_commit(f"git-{digits}", self.COMMIT)
                )

    def test_another_commit_does_not(self):
        self.assertFalse(bootloader.names_commit("git-1234567", self.COMMIT))

    def test_a_modified_tree_does_not(self):
        """`*` means bytes nobody could build again."""
        self.assertFalse(
            bootloader.names_commit("git-abf677c5b*", self.COMMIT)
        )

    def test_a_hash_shorter_than_git_ever_writes_does_not(self):
        self.assertFalse(bootloader.names_commit("git-abf67", self.COMMIT))

    def test_something_that_is_not_a_build_string_does_not(self):
        for build in ("", "2020-12-R1", "abf677c5b"):
            with self.subTest(build):
                self.assertFalse(bootloader.names_commit(build, self.COMMIT))


class RegionTests(unittest.TestCase):

    def test_the_region_is_the_image_padded_with_erased_flash(self):
        image = built_image(size=0x2800)
        region = bootloader.region_image(image)
        self.assertEqual(bootloader.REGION_LENGTH, len(region))
        self.assertEqual(set(region[len(image):]), {bootloader.ERASED_BYTE})
        # Everything but the checksum word is the image byte for byte.
        word = bootloader.CHECKSUM_WORD_INDEX * 4
        self.assertEqual(image[:word], region[:word])
        self.assertEqual(image[word + 4:len(image)], region[word + 4:len(image)])

    def test_the_installed_vectors_add_up_to_zero(self):
        region = bootloader.region_image(built_image())
        words = struct.unpack_from(f"<{bootloader.CHECKED_WORDS}I", region, 0)
        self.assertEqual(0, sum(words) & 0xFFFFFFFF)
        # The file leaves that word at zero, so the hash of the file's bytes is
        # a number no board reports.
        self.assertNotEqual(
            bootloader.region_crc32(region),
            zlib.crc32(built_image() + bytes([bootloader.ERASED_BYTE])
                       * (bootloader.REGION_LENGTH - len(built_image())))
            & 0xFFFFFFFF,
        )

    def test_the_hash_is_the_usual_crc32(self):
        # The check value of the reflected CRC-32 over "123456789", which the
        # fork's own test pins too.
        self.assertEqual(0xCBF43926, zlib.crc32(b"123456789") & 0xFFFFFFFF)
        region = bootloader.region_image(built_image())
        self.assertEqual(zlib.crc32(region) & 0xFFFFFFFF,
                         bootloader.region_crc32(region))

    def test_an_image_that_does_not_fit_the_region_is_refused(self):
        with self.assertRaises(bootloader.BootloaderError) as caught:
            bootloader.region_image(b"\x00" * (bootloader.REGION_LENGTH + 1))
        self.assertIn("past the", str(caught.exception))
        with self.assertRaises(bootloader.BootloaderError):
            bootloader.region_image(b"")

    def test_the_version_block_is_read_out_of_the_region(self):
        region = bootloader.region_image(built_image(build="git-abc1234"))
        self.assertEqual(("2.00", "git-abc1234"),
                         bootloader.read_version_block(region))

    def test_an_image_with_no_readable_block_is_refused(self):
        cases = {
            "no version block": built_image(magic=b"NOTBLVER"),
            "format": built_image(block_format=2),
            "build string": built_image(build="\x01\x02"),
        }
        for what, image in cases.items():
            with self.subTest(what):
                with self.assertRaises(bootloader.BootloaderError):
                    bootloader.read_version_block(bootloader.region_image(image))

    def test_an_image_asking_for_code_read_protection_is_refused(self):
        image = bytearray(built_image())
        struct.pack_into("<I", image, bootloader.CRP_OFFSET,
                         bootloader.CRP_VALUES[0])
        region = bootloader.region_image(bytes(image))
        with self.assertRaises(bootloader.BootloaderError) as caught:
            bootloader.refuse_code_read_protection(region)
        self.assertIn("code read protection", str(caught.exception))
        # The ordinary image asks for none.
        bootloader.refuse_code_read_protection(
            bootloader.region_image(built_image()))


def parse_hex(text: str) -> bytes:
    out = bytearray()
    for line in text.splitlines():
        if not line:
            continue
        assert line.startswith(":"), line
        raw = bytes.fromhex(line[1:])
        assert sum(raw) & 0xFF == 0, line
        count, address, kind = raw[0], (raw[1] << 8) | raw[2], raw[3]
        if kind == 1:
            break
        assert kind == 0, kind
        if len(out) < address + count:
            out.extend(b"\xff" * (address + count - len(out)))
        out[address:address + count] = raw[4:4 + count]
    return bytes(out)


class IntelHexTests(unittest.TestCase):

    def test_the_hex_holds_the_whole_region_and_ends_with_the_eof_record(self):
        region = bootloader.region_image(built_image())
        text = bootloader.intel_hex(region)
        self.assertEqual(":00000001FF", text.splitlines()[-1])
        self.assertEqual(region, parse_hex(text))

    def test_every_record_carries_its_own_checksum(self):
        text = bootloader.intel_hex(b"\x01\x02\x03")
        first = text.splitlines()[0]
        self.assertEqual(":03000000010203F7", first)


class MakefileCoverageTests(unittest.TestCase):

    def test_the_sources_the_makefile_names_are_read_back(self):
        text = (
            "TARGET = bootloader\n"
            "LIBS_PATH = ../common\n"
            "LPCUSB_PATH = $(LIBS_PATH)/lpcusb/target\n"
            "SRC = bl_version.c \\\n"
            "      $(LIBS_PATH)/ubertooth.c \\\n"
            "      $(LPCUSB_PATH)/usbinit.c\n"
            "CPPSRC = $(TARGET).cpp dfu.cpp\n"
            "LINKER_SCRIPT = LPC17xx_Linker_Script_bootloader.ld\n"
            "LINKER_SCRIPT_DIRS = . $(LIBS_PATH)\n"
        )
        self.assertEqual(
            (
                (
                    "bl_version.c", "../common/ubertooth.c",
                    "../common/lpcusb/target/usbinit.c",
                    "bootloader.cpp", "dfu.cpp",
                    "LPC17xx_Linker_Script_bootloader.ld",
                ),
                (".", "../common"),
            ),
            bootloader._makefile_sources(text),
        )

    def test_the_makefile_s_own_values_are_followed_not_assumed(self):
        """A path the makefile moves is read from it, never substituted stale."""
        named, _dirs = bootloader._makefile_sources(
            "LIBS_PATH = ../shared\nSRC = $(LIBS_PATH)/ubertooth.c\n"
        )
        self.assertEqual(("../shared/ubertooth.c",), named)

    def test_the_real_fork_makefile_reads(self):
        """Against the fork itself, so the parser cannot drift from it."""
        fork = Path("/Users/ruswagne/ubertooth-fork")
        text = subprocess.run(
            ["git", "-C", str(fork), "show",
             "squeaky/main:firmware/bootloader/Makefile"],
            capture_output=True, text=True, check=False,
        ).stdout if (fork / ".git").exists() else ""
        if not text:
            self.skipTest("no fork checkout of main to read")
        named, dirs = bootloader._makefile_sources(text)
        self.assertIn("bl_version.c", named)
        self.assertIn("../common/lpcusb/target/usbinit.c", named)
        self.assertIn("LPC17xx_Linker_Script_bootloader.ld", named)
        self.assertEqual((".", "../common"), dirs)

    def test_a_source_appended_with_plus_equals_is_read_too(self):
        """A source the pin must cover cannot hide behind `+=`."""
        named, _dirs = bootloader._makefile_sources(
            "SRC = bl_version.c\n"
            "SRC += ../elsewhere/added.c\n"
            "CPPSRC := bootloader.cpp\n"
        )
        self.assertEqual(
            ("bl_version.c", "../elsewhere/added.c", "bootloader.cpp"), named
        )

    def test_a_variable_the_makefile_does_not_set_is_refused(self):
        with self.assertRaises(bootloader.BootloaderError) as caught:
            bootloader._makefile_sources("SRC = $(SOMEWHERE)/thing.c\n")
        self.assertIn("$(SOMEWHERE), which it does not set",
                      str(caught.exception))

    def test_a_variable_defined_in_terms_of_itself_is_refused(self):
        with self.assertRaises(bootloader.BootloaderError) as caught:
            bootloader._makefile_sources(
                "LIBS_PATH = $(LIBS_PATH)/more\nSRC = $(LIBS_PATH)/a.c\n"
            )
        self.assertIn("in terms of itself", str(caught.exception))


def git(path: Path, *arguments: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    result = subprocess.run(
        ["git", "-C", str(path), *arguments], capture_output=True, text=True,
        check=True, env=environment,
    )
    return result.stdout.strip()


MAKEFILE = """\
# A stand-in for the fork's bootloader makefile: the same source variables,
# and the same build string, stamped from git in this directory.
TARGET = bootloader
LIBS_PATH = ../common
SRC = bl_version.c \\
      $(LIBS_PATH)/ubertooth.c
CPPSRC = $(TARGET).cpp
LINKER_SCRIPT = LPC17xx_Linker_Script_bootloader.ld
LINKER_SCRIPT_DIRS = . $(LIBS_PATH)

VERSION_NOW := git-$(shell git log --pretty=format:'%h' -n 1)

bin hex:
\tpython3 make_image.py $(VERSION_NOW)
"""

MAKE_IMAGE = '''\
"""Write a bootloader.bin carrying the version block, as the real build does."""
import struct
import sys

build = sys.argv[1]
size = 0x2800
image = bytearray(b"\\x00" * size)
struct.pack_into("<I", image, 0, 0x10008000)
struct.pack_into("<I", image, 4, 0x00000201)
for index in range(2, 7):
    struct.pack_into("<I", image, index * 4, 0x00000301 + index)
block = bytearray(48)
block[0:8] = b"UBTBLVER"
struct.pack_into("<H", block, 8, 1)
struct.pack_into("<H", block, 10, 0x0200)
block[16:16 + len(build)] = build.encode()
image[0x100:0x100 + 48] = block
open("bootloader.bin", "wb").write(bytes(image))
'''

INTERFACE_HEADER = """\
#define UBERTOOTH_BOOTLOADER_VERSION 0x0200
#define UBERTOOTH_BOOTLOADER_REGION_START 0x00000000u
#define UBERTOOTH_BOOTLOADER_REGION_LENGTH 0x4000u
#define UBERTOOTH_BL_VERSION_MAGIC "UBTBLVER"
#define UBERTOOTH_FW_INFO_OFFSET 0x100
#define UBERTOOTH_BL_VERSION_OFFSET UBERTOOTH_FW_INFO_OFFSET
#define UBERTOOTH_BL_VERSION_FORMAT 1
#define UBERTOOTH_BL_VERSION_LENGTH 48
#define UBERTOOTH_BL_VERSION_BUILD_LEN 32
#define UBERTOOTH_BL_VERSION_OFF_FORMAT 8
#define UBERTOOTH_BL_VERSION_OFF_VERSION 10
#define UBERTOOTH_BL_VERSION_OFF_BUILD 16
#define UBERTOOTH_LPC_CRP_OFFSET 0x2fc
#define UBERTOOTH_LPC_CRP_LENGTH 4
#define UBERTOOTH_GET_BOOTLOADER_INFO 60
"""


def make_fork(root: Path) -> Path:
    """A tiny repository shaped like the fork, as far as this module reads it."""
    source = root / "ubertooth"
    (source / "firmware" / "bootloader").mkdir(parents=True)
    (source / "firmware" / "common").mkdir(parents=True)
    (source / "host" / "libubertooth" / "src").mkdir(parents=True)
    git(root, "init", "--quiet", "-b", "main", str(source))
    boot = source / "firmware" / "bootloader"
    (boot / "Makefile").write_text(MAKEFILE)
    (boot / "make_image.py").write_text(MAKE_IMAGE)
    (boot / "bl_version.c").write_text("/* the version block */\n")
    (boot / "bootloader.cpp").write_text("/* the bootloader */\n")
    (boot / "LPC17xx_Linker_Script_bootloader.ld").write_text("/* script */\n")
    (source / "firmware" / "common" / "ubertooth.c").write_text("/* shared */\n")
    (source / "host" / "libubertooth" / "src"
     / "ubertooth_interface.h").write_text(INTERFACE_HEADER)
    (source / "README").write_text("nothing to do with the bootloader\n")
    git(source, "add", "-A")
    git(source, "commit", "--quiet", "-m", "the bootloader")
    return source


def source_revision(path: Path, ref: str = "HEAD"):
    commit = git(path, "rev-parse", ref)
    return SimpleNamespace(
        name="Ubertooth", source_path=str(path), commit=commit,
        short_commit=git(path, "rev-parse", "--short", commit),
        expected_revision=f"git-{git(path, 'rev-parse', '--short', commit)}",
    )


class PinTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False)
        )
        self.source = make_fork(self.root)

    def test_the_pin_is_the_commit_that_touched_the_bootloader(self):
        pin = bootloader.resolve_pin(source_revision(self.source))
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), pin.commit)
        self.assertTrue(pin.is_resolved_commit)

    def test_a_commit_elsewhere_in_the_tree_does_not_move_the_pin(self):
        first = git(self.source, "rev-parse", "HEAD")
        (self.source / "README").write_text("an unrelated change\n")
        git(self.source, "commit", "--quiet", "-am", "docs only")
        pin = bootloader.resolve_pin(source_revision(self.source))
        self.assertEqual(first, pin.commit)
        self.assertFalse(pin.is_resolved_commit)
        self.assertNotEqual(pin.commit, git(self.source, "rev-parse", "HEAD"))

    def test_a_change_to_a_shared_source_does_move_it(self):
        first = git(self.source, "rev-parse", "HEAD")
        (self.source / "firmware" / "common" / "ubertooth.c").write_text("/* new */\n")
        git(self.source, "commit", "--quiet", "-am", "shared source")
        pin = bootloader.resolve_pin(source_revision(self.source))
        self.assertNotEqual(first, pin.commit)
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), pin.commit)

    def test_a_source_outside_the_pinned_paths_is_refused(self):
        makefile = self.source / "firmware" / "bootloader" / "Makefile"
        makefile.write_text(
            MAKEFILE.replace("SRC = bl_version.c", "SRC = ../../host/elsewhere.c")
        )
        git(self.source, "commit", "--quiet", "-am", "a source nobody watches")
        with self.assertRaises(bootloader.BootloaderError) as caught:
            bootloader.resolve_pin(source_revision(self.source))
        self.assertIn("host/elsewhere.c", str(caught.exception))
        self.assertIn("would not move", str(caught.exception))

    def test_the_linker_script_is_recorded_where_the_build_finds_it(self):
        script = self.source / "firmware" / "bootloader" / \
            "LPC17xx_Linker_Script_bootloader.ld"
        shared = self.source / "firmware" / "common" / script.name
        script.rename(shared)
        git(self.source, "add", "-A")
        git(self.source, "commit", "--quiet", "-m", "share the linker script")
        pin = bootloader.resolve_pin(source_revision(self.source))
        self.assertIn("firmware/common/" + script.name, pin.sources)
        self.assertNotIn("firmware/bootloader/" + script.name, pin.sources)

    def test_a_version_block_offset_that_moved_is_reported(self):
        header = (self.source / "host" / "libubertooth" / "src"
                  / "ubertooth_interface.h")
        header.write_text(
            INTERFACE_HEADER.replace(
                "#define UBERTOOTH_BL_VERSION_OFFSET UBERTOOTH_FW_INFO_OFFSET",
                "#define UBERTOOTH_BL_VERSION_OFFSET 0x200",
            )
        )
        git(self.source, "commit", "--quiet", "-am", "move the block")
        with self.assertRaises(bootloader.BootloaderError) as caught:
            bootloader.verify_interface_constants(source_revision(self.source))
        self.assertIn("the version block offset is 0x200", str(caught.exception))

    def test_a_version_block_offset_that_is_gone_is_reported(self):
        header = (self.source / "host" / "libubertooth" / "src"
                  / "ubertooth_interface.h")
        header.write_text(
            INTERFACE_HEADER.replace(
                "#define UBERTOOTH_BL_VERSION_OFFSET UBERTOOTH_FW_INFO_OFFSET\n", ""
            )
        )
        git(self.source, "commit", "--quiet", "-am", "drop the offset")
        with self.assertRaises(bootloader.BootloaderError) as caught:
            bootloader.verify_interface_constants(source_revision(self.source))
        self.assertIn("UBERTOOTH_BL_VERSION_OFFSET is not defined",
                      str(caught.exception))

    def test_the_interface_numbers_are_put_against_the_commit(self):
        source = source_revision(self.source)
        self.assertTrue(bootloader.supported_at(source))
        self.assertEqual(0x0200, bootloader.verify_interface_constants(source))

    def test_a_moved_offset_is_reported_rather_than_read_wrongly(self):
        header = (self.source / "host" / "libubertooth" / "src"
                  / "ubertooth_interface.h")
        header.write_text(
            INTERFACE_HEADER.replace(
                "#define UBERTOOTH_BL_VERSION_OFF_BUILD 16",
                "#define UBERTOOTH_BL_VERSION_OFF_BUILD 20",
            )
        )
        git(self.source, "commit", "--quiet", "-am", "move a field")
        with self.assertRaises(bootloader.BootloaderError) as caught:
            bootloader.verify_interface_constants(source_revision(self.source))
        self.assertIn("UBERTOOTH_BL_VERSION_OFF_BUILD", str(caught.exception))

    def test_a_commit_with_no_bootloader_read_is_not_supported(self):
        header = (self.source / "host" / "libubertooth" / "src"
                  / "ubertooth_interface.h")
        header.write_text("#define UBERTOOTH_API_VERSION 0x0100\n")
        git(self.source, "commit", "--quiet", "-am", "before the read existed")
        source = source_revision(self.source)
        self.assertFalse(bootloader.supported_at(source))
        with self.assertRaises(bootloader.BootloaderError) as caught:
            bootloader.verify_interface_constants(source)
        self.assertIn("cannot report a board's bootloader", str(caught.exception))


class RealExecutor:
    """Runs the commands the artifact build needs, recording each one."""

    def __init__(self):
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None, cwd=None):
        self.commands.append(tuple(argv))
        assert timeout_s > 0, argv
        result = subprocess.run(
            list(argv), capture_output=True, text=True,
            cwd=cwd, timeout=timeout_s, check=False,
        )
        return CommandResult(
            argv=tuple(argv), returncode=result.returncode,
            stdout=result.stdout, stderr=result.stderr,
            duration_s=0.01, timed_out=False,
        )


class ArtifactTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False)
        )
        self.source = make_fork(self.root)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.publisher = open_run_publisher(self.run_dir, "bootloader-test")

    def build(self, **overrides):
        source = source_revision(self.source)
        return bootloader.BootloaderArtifactBuilder(RealExecutor()).build(
            source, self.publisher,
            checkout=Path(overrides.pop("checkout", self.source)),
            work_dir=self.run_dir / "bootloader",
            output_dir=self.run_dir,
            **overrides,
        )

    def records(self, kind):
        lines = (self.run_dir / RUN_LOG).read_text().splitlines()
        return [
            json.loads(line) for line in lines
            if json.loads(line)["event_type"] == kind
        ]

    def test_the_artifact_records_what_the_board_will_report(self):
        artifact = self.build()
        pin = git(self.source, "rev-parse", "--short", "HEAD")
        self.assertEqual("2.00", artifact.version)
        self.assertEqual(f"git-{pin}", artifact.build)
        self.assertEqual(bootloader.REGION_LENGTH, artifact.region_bytes)
        self.assertEqual(0x2800, artifact.image_bytes)
        region = artifact.region_path.read_bytes()
        self.assertEqual(bootloader.region_crc32(region), artifact.region_crc32)
        # The identity is the `-B` line a board with it reports.
        self.assertEqual(
            f"Bootloader: 2.00 git-{pin}, CRC32:0x{artifact.region_crc32:08x}",
            artifact.identity().line(),
        )
        # And the hex a programmer takes holds exactly that region.
        self.assertEqual(region, parse_hex(artifact.region_hex_path.read_text()))

    def test_the_build_is_recorded_in_the_run(self):
        artifact = self.build()
        started = self.records("bootloader_build_started")
        finished = self.records("bootloader_build_finished")
        self.assertEqual(1, len(started))
        self.assertEqual("passed", finished[-1]["status"])
        self.assertEqual(
            artifact.pin.commit, started[0]["details"]["pin"]["commit"]
        )
        self.assertEqual(
            f"0x{artifact.region_crc32:08x}",
            finished[-1]["details"]["artifact"]["region_crc32"],
        )
        # Every command it ran is in the run's records.
        argvs = [
            event["details"]["argv"] for event in self.records("command_started")
        ]
        self.assertTrue(any(argv[:3] == ["git", "-C", str(self.source)]
                            for argv in argvs), argvs)
        self.assertTrue(any(argv[0] == "make" for argv in argvs), argvs)

    def test_the_artifact_is_built_from_the_pinned_commit_not_the_resolved_one(self):
        pinned = git(self.source, "rev-parse", "--short", "HEAD")
        (self.source / "README").write_text("an unrelated change\n")
        git(self.source, "commit", "--quiet", "-am", "docs only")
        artifact = self.build()
        self.assertEqual(f"git-{pinned}", artifact.build)
        self.assertFalse(artifact.pin.is_resolved_commit)

    def test_an_image_built_from_a_modified_tree_is_refused(self):
        """A `*` build string is nobody's to build again, so it is not an artifact."""
        makefile = self.source / "firmware" / "bootloader" / "Makefile"
        makefile.write_text(MAKEFILE.replace(
            "VERSION_NOW := git-$(shell git log --pretty=format:'%h' -n 1)",
            "VERSION_NOW := git-$(shell git log --pretty=format:'%h' -n 1)*",
        ))
        git(self.source, "commit", "--quiet", "-am", "stamp a modified tree")
        with self.assertRaises(bootloader.BootloaderError) as caught:
            self.build()
        self.assertIn("would not be reproducible", str(caught.exception))
        self.assertIn("*", str(caught.exception))

    def test_an_image_whose_build_string_is_not_the_pin_is_refused(self):
        makefile = self.source / "firmware" / "bootloader" / "Makefile"
        makefile.write_text(MAKEFILE.replace(
            "VERSION_NOW := git-$(shell git log --pretty=format:'%h' -n 1)",
            "VERSION_NOW := git-0000000",
        ))
        git(self.source, "commit", "--quiet", "-am", "stamp something else")
        with self.assertRaises(bootloader.BootloaderError) as caught:
            self.build()
        self.assertIn("would not be reproducible", str(caught.exception))
        self.assertEqual("failed",
                         self.records("bootloader_build_finished")[-1]["status"])


def registry(*, programmer=True, device=PROGRAMMER_DEVICE):
    resources = {
        "Host_Local": Resource(
            alias="Host_Local", type="Host", platform="linux", roles=(),
            defaults=MappingProxyType({}), attached_to=None,
            identity=MappingProxyType({}), capabilities=(),
            power=MappingProxyType({}), driver=None,
        ),
        "UT_A": Resource(
            alias="UT_A", type="Ubertooth", platform=None, roles=(),
            defaults=MappingProxyType({}), attached_to="Host_Local",
            identity=MappingProxyType({"serial": SERIAL_A}), capabilities=(),
            power=MappingProxyType({}), driver=None,
        ),
        "UT_B": Resource(
            alias="UT_B", type="Ubertooth", platform=None, roles=(),
            defaults=MappingProxyType({}), attached_to="Host_Local",
            identity=MappingProxyType({"serial": SERIAL_B}), capabilities=(),
            power=MappingProxyType({}), driver=None,
        ),
    }
    if programmer:
        identity = {"serial": PROGRAMMER_SERIAL}
        if device:
            identity["serial device"] = device
        resources["Prog_A"] = Resource(
            alias="Prog_A", type="Debug programmer", platform=None, roles=(),
            defaults=MappingProxyType({}), attached_to="Host_Local",
            identity=MappingProxyType(identity), capabilities=(),
            power=MappingProxyType({}), driver=None, programs="UT_A",
        )
    return FileResourceRegistry(resources)


class RegistryPairingTests(unittest.TestCase):

    def test_the_board_with_a_programmer_can_be_programmed(self):
        boards = bootloader.boards_for_bootloader(registry(), ["UT_A", "UT_B"])
        self.assertEqual(("UT_A", "UT_B"), tuple(b.alias for b in boards))
        self.assertTrue(boards[0].can_be_programmed)
        self.assertEqual("Prog_A", boards[0].programmer_alias)
        self.assertEqual(PROGRAMMER_DEVICE, boards[0].programmer_device)
        self.assertFalse(boards[1].has_programmer)
        self.assertFalse(boards[1].can_be_programmed)

    def test_a_programmer_with_no_port_is_paired_but_not_usable(self):
        boards = bootloader.boards_for_bootloader(
            registry(device=None), ["UT_A"]
        )
        self.assertTrue(boards[0].has_programmer)
        self.assertFalse(boards[0].can_be_programmed)

    def test_a_board_with_no_registered_serial_is_a_refusal_not_a_traceback(self):
        from ubertooth_harness.flash import HarnessError

        resources = dict(registry().resources)
        resources["UT_A"] = Resource(
            alias="UT_A", type="Ubertooth", platform=None, roles=(),
            defaults=MappingProxyType({}), attached_to="Host_Local",
            identity=MappingProxyType({}), capabilities=(),
            power=MappingProxyType({}), driver=None,
        )
        resources.pop("Prog_A")
        with self.assertRaises(HarnessError) as caught:
            bootloader.boards_for_bootloader(
                FileResourceRegistry(resources), ["UT_A"]
            )
        self.assertIn("no registered serial", str(caught.exception))

    def test_the_documented_programmer_command_is_what_is_run(self):
        self.assertEqual(
            ("lpc21isp", "-control", "/tmp/region.hex", PROGRAMMER_DEVICE,
             "230400", "4000"),
            bootloader.program_argv("/tmp/region.hex", PROGRAMMER_DEVICE),
        )


class FakeProgrammerBench:
    """Boards answering `-B` and `-v`, and a programmer that writes one."""

    def __init__(self, *, installed, application=RUN_REVISION):
        # serial -> the `-B` line that board answers with
        self.installed = dict(installed)
        self.application = dict.fromkeys(self.installed, application)
        self.commands = []
        self.program_fails = False
        self.after_program = {}  # serial -> what `-B` says after a write
        self.silent_after = {}   # serial -> reads left unanswered after a write
        self.written = []

    def answer(self, argv, serial):
        left = self.silent_after.get(serial, 0)
        if left:
            self.silent_after[serial] = left - 1
            return self.result(
                argv, 1, "", "error: bootloader information read failed\n"
            )
        return self.result(argv, 0, self.installed[serial] + "\n", "")

    @staticmethod
    def result(argv, returncode, stdout, stderr, duration_s=0.01):
        return CommandResult(
            argv=tuple(argv), returncode=returncode, stdout=stdout,
            stderr=stderr, duration_s=duration_s, timed_out=False,
        )

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None, cwd=None):
        command = tuple(argv)
        self.commands.append(command)
        assert timeout_s > 0, command
        if command[:2] == ("ubertooth-util", "-U") and command[3] == "-B":
            return self.answer(command, command[2])
        if command[:2] == ("ubertooth-util", "-U") and command[3] == "-v":
            revision = self.application.get(command[2], "")
            return self.result(
                command, 0 if revision else 1,
                (f"Firmware version: bluetooth_rxtx {revision} (API:1.58)\n"
                 if revision else ""),
                "",
            )
        if command[0] == "lpc21isp":
            self.written.append(command)
            if self.program_fails:
                return self.result(
                    command, 1, "", "can't synchronize with the chip\n", 0.5
                )
            # Which board this adapter is wired to is the bench's business;
            # there is one here.
            serial = next(iter(self.installed))
            self.installed[serial] = self.after_program.get(
                serial, self.installed[serial]
            )
            return self.result(
                command, 0, "Download Finished... taking 3 seconds\n", "", 0.5
            )
        raise AssertionError(f"unexpected command {command}")


FORK_LINE = "Bootloader: 2.00 git-abc1234, CRC32:0x11112222"
FACTORY_LINE = (
    "Bootloader: no version block (factory or earlier, treated as 1.x), "
    "CRC32:0x5f0a73c1"
)
# What firmware older than the request prints instead of a line, as the fork
# prints it: libubertooth's one wording for a stalled request, then
# ubertooth-util naming the read that failed. Firmware from before the
# bootloader read, such as the 2020-12-R1 image a flash test alternates in,
# answers this way.
REFUSAL_LINE = (
    "error: could not read the bootloader information: the board's firmware "
    "does not support it (stalled, libusb -9)\n"
    "note: the board reports firmware API version 1.07, libubertooth 1.07 "
    "requires 1.35\n"
    "error: bootloader information read failed\n"
)


def artifact_for(line=FORK_LINE, *, path=Path("/tmp/region.hex")):
    """An artifact whose identity is ``line``, without building one."""
    identity = bootloader.parse_identity(line + "\n")
    return bootloader.BootloaderArtifact(
        pin=bootloader.BootloaderPin(
            commit="a" * 40, short_commit="abc1234", subject="bootloader",
            paths=bootloader.PIN_PATHS, sources=("firmware/bootloader/bl_version.c",),
            resolved_commit="a" * 40,
        ),
        version=identity.version, build=identity.build,
        bootloader_version_word=0x0200,
        image_bytes=0x2800, region_bytes=bootloader.REGION_LENGTH,
        region_crc32=identity.region_crc32,
        region_path=path.with_suffix(".bin"), region_hex_path=path,
        image_path=path.with_name("bootloader.bin"),
        region_sha256="0" * 64,
    )


class UpdaterTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False)
        )
        self.publisher = open_run_publisher(self.root, "bootloader-update")

    def boards(self, **kwargs):
        return bootloader.boards_for_bootloader(registry(**kwargs), ["UT_A"])

    def run_stage(self, bench, boards, *, mode, artifact, host_lock=True):
        updater = bootloader.BootloaderUpdater(bench, sleep=lambda _s: None)
        return updater.run(
            boards, self.publisher, mode=mode, artifact=artifact,
            output_dir=self.root, application_revision=RUN_REVISION,
            holds_host_lock=host_lock,
        )

    def test_a_board_already_on_the_artifact_is_not_written(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FORK_LINE})
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(),
        )
        self.assertEqual("passed", summary.status)
        self.assertEqual("current", summary.boards[0].status)
        self.assertEqual([], bench.written)

    def test_a_factory_board_with_a_programmer_is_updated_and_verified(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        bench.after_program = {SERIAL_A: FORK_LINE}
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(),
        )
        self.assertEqual("passed", summary.status)
        outcome = summary.boards[0]
        self.assertEqual("updated", outcome.status)
        self.assertTrue(outcome.written)
        self.assertEqual("1.x", outcome.previous.generation)
        self.assertEqual("2.00", outcome.installed.version)
        self.assertEqual(
            [("lpc21isp", "-control", "/tmp/region.hex", PROGRAMMER_DEVICE,
              "230400", "4000")],
            bench.written,
        )
        # The read before, the write, the read after, and the application read.
        self.assertIn(("ubertooth-util", "-U", SERIAL_A, "-v"), bench.commands)

    def test_a_stale_board_with_no_programmer_fails_and_names_what_is_missing(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        boards = bootloader.boards_for_bootloader(
            registry(programmer=False), ["UT_A"]
        )
        summary = self.run_stage(
            bench, boards, mode=bootloader.MODE_UPDATE, artifact=artifact_for(),
        )
        self.assertEqual("failed", summary.status)
        self.assertEqual("stale", summary.boards[0].status)
        self.assertIn("no debug programmer is registered for UT_A",
                      summary.reason)
        self.assertIn("in-system programming header", summary.reason)
        self.assertEqual([], bench.written)

    def test_check_only_never_writes_even_with_a_programmer(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_CHECK,
            artifact=artifact_for(),
        )
        self.assertEqual("failed", summary.status)
        self.assertEqual("stale", summary.boards[0].status)
        self.assertEqual([], bench.written)

    def test_recording_only_needs_no_artifact_and_never_writes(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_RECORD, artifact=None,
            host_lock=False,
        )
        self.assertEqual("passed", summary.status)
        self.assertEqual("current", summary.boards[0].status)
        self.assertEqual([], bench.written)

    def test_a_failed_write_is_a_hard_failure_and_is_not_retried(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        bench.program_fails = True
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(),
        )
        self.assertEqual("failed", summary.status)
        self.assertEqual(1, len(bench.written))
        self.assertTrue(summary.boards[0].written)
        self.assertIn("unknown state", summary.reason)
        self.assertIn("recover it with the same programmer", summary.reason)

    def test_a_write_that_installed_something_else_fails(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        bench.after_program = {
            SERIAL_A: "Bootloader: 2.00 git-abc1234, CRC32:0x99999999"
        }
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(),
        )
        self.assertEqual("failed", summary.status)
        self.assertIn("not the 2.00 git-abc1234, CRC32:0x11112222 that was "
                      "written", summary.reason)

    def test_a_board_that_goes_quiet_after_the_write_is_read_again(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        bench.after_program = {SERIAL_A: FORK_LINE}
        bench.silent_after = {SERIAL_A: 0}
        # Quiet for two reads after the write, then answers.
        original = bench.run

        def run(argv, **kwargs):
            if tuple(argv)[0] == "lpc21isp":
                bench.silent_after[SERIAL_A] = 2
            return original(argv, **kwargs)

        bench.run = run
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(),
        )
        self.assertEqual("passed", summary.status)
        self.assertEqual("updated", summary.boards[0].status)

    def test_a_board_that_never_answers_after_the_write_fails(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        bench.after_program = {SERIAL_A: FORK_LINE}
        original = bench.run

        def run(argv, **kwargs):
            if tuple(argv)[0] == "lpc21isp":
                bench.silent_after[SERIAL_A] = 99
            return original(argv, **kwargs)

        bench.run = run
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(),
        )
        self.assertEqual("failed", summary.status)
        self.assertIn("did not report its bootloader after the write",
                      summary.reason)

    def test_an_application_that_no_longer_runs_the_revision_fails(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        bench.after_program = {SERIAL_A: FORK_LINE}
        bench.application[SERIAL_A] = "git-9999999"
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(),
        )
        self.assertEqual("failed", summary.status)
        self.assertIn("its application reports git-9999999", summary.reason)

    def test_an_update_without_this_host_s_firmware_lock_is_refused(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        with self.assertRaises(bootloader.BootloaderError) as caught:
            self.run_stage(
                bench, self.boards(), mode=bootloader.MODE_UPDATE,
                artifact=artifact_for(), host_lock=False,
            )
        self.assertIn("firmware lock", str(caught.exception))
        self.assertEqual([], bench.commands)

    def test_recording_with_an_artifact_is_refused(self):
        """Recording compares nothing, so an artifact means another mode."""
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        with self.assertRaises(bootloader.BootloaderError) as caught:
            self.run_stage(
                bench, self.boards(), mode=bootloader.MODE_RECORD,
                artifact=artifact_for(),
            )
        self.assertIn("compares nothing", str(caught.exception))
        self.assertEqual([], bench.commands)

    def test_a_programmer_that_never_started_wrote_nothing(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        original = bench.run

        def run(argv, **kwargs):
            if tuple(argv)[0] == "lpc21isp":
                bench.written.append(tuple(argv))
                return bench.result(argv, 127, "", "command not found\n")
            return original(argv, **kwargs)

        bench.run = run
        summary = self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(),
        )
        self.assertEqual("failed", summary.status)
        outcome = summary.boards[0]
        # Nothing reached the board, so nobody is sent to the bench for it.
        self.assertFalse(outcome.written)
        self.assertIn("did not start", outcome.reason)
        self.assertIn("still has no version block", outcome.reason)
        self.assertIn(PROGRAMMER_DEVICE, outcome.reason)
        self.assertNotIn("unknown state", outcome.reason)

    def test_a_comparing_mode_without_an_artifact_is_refused(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FORK_LINE})
        with self.assertRaises(bootloader.BootloaderError) as caught:
            self.run_stage(
                bench, self.boards(), mode=bootloader.MODE_CHECK, artifact=None,
            )
        self.assertIn("did not build one", str(caught.exception))
        self.assertEqual([], bench.commands)

    def test_an_unknown_mode_is_refused(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FORK_LINE})
        with self.assertRaises(bootloader.BootloaderError):
            self.run_stage(
                bench, self.boards(), mode="install-everything",
                artifact=artifact_for(),
            )

    def test_cancellation_between_boards_stops_the_stage(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FORK_LINE})
        token = CancellationToken()
        token.cancel("the operator asked")
        updater = bootloader.BootloaderUpdater(bench, sleep=lambda _s: None)
        summary = updater.run(
            self.boards(), self.publisher, mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(), output_dir=self.root,
            application_revision=RUN_REVISION, cancellation=token,
            holds_host_lock=True,
        )
        self.assertEqual("canceled", summary.status)
        self.assertEqual([], bench.written)

    def test_the_stage_records_each_board_in_the_run(self):
        bench = FakeProgrammerBench(installed={SERIAL_A: FACTORY_LINE})
        bench.after_program = {SERIAL_A: FORK_LINE}
        self.run_stage(
            bench, self.boards(), mode=bootloader.MODE_UPDATE,
            artifact=artifact_for(),
        )
        events = [
            json.loads(line)
            for line in (self.root / RUN_LOG).read_text().splitlines()
        ]
        kinds = [event["event_type"] for event in events]
        for kind in ("bootloader_stage_started", "bootloader_update_started",
                     "bootloader_identity", "bootloader_update_finished",
                     "bootloader_stage_finished"):
            self.assertIn(kind, kinds)
        identity = next(
            event for event in events
            if event["event_type"] == "bootloader_identity"
        )
        self.assertEqual("updated", identity["details"]["status"])
        self.assertTrue(identity["details"]["written"])
        self.assertEqual("1.x", identity["details"]["previous"]["generation"])


class ExpectationTests(unittest.TestCase):
    """What preflight requires of each board, and how it says a board is not it."""

    def expectation(self, mode, line=FORK_LINE, **kwargs):
        boards = bootloader.boards_for_bootloader(registry(**kwargs), ["UT_A"])
        expected = (
            bootloader.parse_identity(line + "\n")
            if mode in bootloader.COMPARING_MODES else None
        )
        return bootloader.BootloaderExpectation(
            mode=mode, expected=expected,
            boards={board.alias: board for board in boards},
        )

    def test_recording_only_asks_that_the_board_answered(self):
        expectation = self.expectation(bootloader.MODE_RECORD)
        self.assertEqual(
            (bootloader.IDENTITY_LINE_PATTERN,), expectation.patterns()
        )
        installed = bootloader.parse_identity(FACTORY_LINE + "\n")
        self.assertEqual("", expectation.failure("UT_A", installed))

    def test_a_board_that_did_not_answer_is_told_how_to_make_it_answer(self):
        for mode in bootloader.MODES:
            with self.subTest(mode):
                failure = self.expectation(mode).failure("UT_A", None)
                self.assertIn("did not report its bootloader", failure)
                self.assertIn("a flash with this run's build fixes", failure)

    def test_checking_compares_the_whole_identity(self):
        expectation = self.expectation(bootloader.MODE_CHECK)
        self.assertEqual(
            (bootloader.identity_pattern(expectation.expected),),
            expectation.patterns(),
        )
        self.assertEqual(
            "", expectation.failure(
                "UT_A", bootloader.parse_identity(FORK_LINE + "\n")
            )
        )
        failure = expectation.failure(
            "UT_A", bootloader.parse_identity(FACTORY_LINE + "\n")
        )
        self.assertIn("UT_A has bootloader no version block", failure)
        self.assertIn("expects 2.00 git-abc1234", failure)
        self.assertIn("--bootloader update to install it through Prog_A", failure)

    def test_checking_a_bench_with_no_programmer_says_it_cannot_install_one(self):
        expectation = self.expectation(bootloader.MODE_CHECK, programmer=False)
        failure = expectation.failure(
            "UT_A", bootloader.parse_identity(FACTORY_LINE + "\n")
        )
        self.assertIn("no debug programmer is registered for it", failure)

    def test_checking_a_programmer_with_no_port_says_which_field_is_missing(self):
        """Not "register a programmer" for one that is already registered."""
        expectation = self.expectation(bootloader.MODE_CHECK, device=None)
        failure = expectation.failure(
            "UT_A", bootloader.parse_identity(FACTORY_LINE + "\n")
        )
        self.assertIn("Prog_A is registered for it with no 'serial device'",
                      failure)
        self.assertNotIn("no debug programmer is registered", failure)

    def test_updating_a_board_with_no_programmer_names_the_capability(self):
        expectation = self.expectation(bootloader.MODE_UPDATE, programmer=False)
        failure = expectation.failure(
            "UT_A", bootloader.parse_identity(FACTORY_LINE + "\n")
        )
        self.assertIn("no debug programmer is registered for UT_A", failure)
        self.assertIn("lpc21isp", failure)
        self.assertIn("in-system programming header", failure)


class EstablishedBoardExpectationTests(unittest.TestCase):
    """A board whose application image a test of the suite writes (#327)."""

    @staticmethod
    def expectation(mode, *, established=("UT_A",)):
        expected = (
            bootloader.parse_identity(FORK_LINE + "\n")
            if mode in bootloader.COMPARING_MODES else None
        )
        return bootloader.BootloaderExpectation(
            mode=mode, expected=expected, boards={},
            established=frozenset(established),
        )

    def test_only_an_established_board_may_say_it_has_not_got_the_request(self):
        expectation = self.expectation(bootloader.MODE_CHECK)
        self.assertTrue(expectation.tolerates_refusal("UT_A"))
        self.assertFalse(expectation.tolerates_refusal("UT_B"))
        self.assertFalse(expectation.tolerates_refusal(None))

    def test_its_recorded_expectation_names_both_answers(self):
        for mode in bootloader.MODES:
            with self.subTest(mode):
                expectation = self.expectation(mode)
                (pattern,) = expectation.patterns("UT_A")
                for answer in (FORK_LINE, FACTORY_LINE):
                    # In a comparing mode only the run's own line matches; in
                    # record mode any line does. Either way the refusal does.
                    self.assertEqual(
                        mode not in bootloader.COMPARING_MODES
                        or answer == FORK_LINE,
                        re.search(pattern, answer + "\n") is not None,
                        answer,
                    )
                self.assertIsNotNone(re.search(pattern, REFUSAL_LINE))
                # Another board of the same run is held to the line alone.
                (strict,) = expectation.patterns("UT_B")
                self.assertIsNone(re.search(strict, REFUSAL_LINE))

    def test_an_answer_it_did_give_is_compared_like_any_other(self):
        expectation = self.expectation(bootloader.MODE_CHECK)
        self.assertEqual("", expectation.failure(
            "UT_A", bootloader.parse_identity(FORK_LINE + "\n")))
        failure = expectation.failure(
            "UT_A", bootloader.parse_identity(FACTORY_LINE + "\n"))
        self.assertIn("UT_A has bootloader no version block", failure)
        self.assertIn("expects 2.00 git-abc1234", failure)

    def test_a_stale_bootloader_there_is_not_sent_to_the_update_option(self):
        """`update` installs on the boards whose firmware preflight requires."""
        failure = self.expectation(bootloader.MODE_CHECK).failure(
            "UT_A", bootloader.parse_identity(FACTORY_LINE + "\n"))
        self.assertIn("--bootloader update does not install one there",
                      failure)
        self.assertIn("over its programmer by hand", failure)
        self.assertNotIn("run with --bootloader update to install it through",
                         failure)

    def test_the_note_says_why_nothing_was_compared(self):
        note = self.expectation(bootloader.MODE_CHECK).not_read_note("UT_A")
        self.assertIn("UT_A says its firmware has not got the bootloader read",
                      note)
        self.assertIn("nothing was compared", note)


class PreflightCompileTests(unittest.TestCase):
    """The bootloader read as one of the run's mandatory preflight checks."""

    def compile(self, expectation, invocations=None):
        from tests.test_manifest_preflight import make_invocations, prepare_preflight
        from ubertooth_harness.preflight import PreflightPolicy
        from ubertooth_harness.resources import FileResourceRegistry

        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = prepare_preflight(directory)
            invocations = invocations or make_invocations()
            policy = PreflightPolicy(
                FileResourceRegistry.load(root / "hardware.yaml")
            )
            return policy.compile(
                invocations, manifest.with_invocations(invocations),
                bootloader=expectation,
            )

    @staticmethod
    def expectation(mode, line=FORK_LINE, alias="UT_Linux1"):
        board = bootloader.BootloaderBoard(
            alias=alias, serial="abcdef0123456789",
            programmer_alias="Prog_Linux0", programmer_serial=PROGRAMMER_SERIAL,
            programmer_device=PROGRAMMER_DEVICE,
        )
        expected = (
            bootloader.parse_identity(line + "\n")
            if mode in bootloader.COMPARING_MODES else None
        )
        return bootloader.BootloaderExpectation(
            mode=mode, expected=expected, boards={alias: board},
        )

    def test_the_read_is_one_check_per_board_after_the_firmware_checks(self):
        checks = self.compile(self.expectation(bootloader.MODE_RECORD))
        names = [check.name for check in checks]
        self.assertEqual(
            ["Host version", "Firmware version", "Firmware compile info",
             "Device serial", "Bootloader identity"],
            names,
        )
        read = checks[-1]
        self.assertEqual(
            ("ubertooth-util", "-U", "abcdef0123456789", "-B"), read.argv
        )
        self.assertEqual(bootloader.IDENTITY_TIMEOUT_S, read.timeout_s)
        self.assertEqual((bootloader.IDENTITY_LINE_PATTERN,),
                         read.expected_patterns)

    def test_a_run_on_a_commit_without_the_read_has_no_such_check(self):
        checks = self.compile(None)
        self.assertNotIn("Bootloader identity", [check.name for check in checks])

    def test_the_check_reads_the_line_not_the_exit_status(self):
        check = self.compile(self.expectation(bootloader.MODE_CHECK))[-1]
        # `-B` exits 1 for a version block this host would not read, and the
        # line says so; the check's reason comes from the line.
        unreadable = CommandResult(
            argv=check.argv, returncode=1,
            stdout="Bootloader: the bootloader version block is in a format "
                   "this host does not know, CRC32:0x11223344\n",
            stderr="error: the bootloader version block is in a format this "
                   "host does not know\n",
            duration_s=0.01, timed_out=False,
        )
        failure = check.result_failure(unreadable)
        self.assertIn("has bootloader the bootloader version block", failure)
        matching = CommandResult(
            argv=check.argv, returncode=0, stdout=FORK_LINE + "\n", stderr="",
            duration_s=0.01, timed_out=False,
        )
        self.assertEqual("", check.result_failure(matching))

    def test_a_read_that_failed_for_another_reason_names_its_own_failure(self):
        """No board, or a board somebody else holds, is not old firmware."""
        check = self.compile(self.expectation(bootloader.MODE_RECORD))[-1]
        result = CommandResult(
            argv=check.argv, returncode=1, stdout="",
            stderr="error: no Ubertooth device found\n",
            duration_s=0.01, timed_out=False,
        )
        failure = check.result_failure(result)
        self.assertIn("did not report its bootloader", failure)
        self.assertIn("exited 1", failure)
        self.assertNotIn("a flash with this run's build fixes", failure)

    def test_firmware_that_refuses_the_read_is_told_to_be_flashed(self):
        """The one failure a reflash does fix, which exits 1 with no line."""
        check = self.compile(self.expectation(bootloader.MODE_RECORD))[-1]
        result = CommandResult(
            argv=check.argv, returncode=0, stdout="", stderr="",
            duration_s=0.01, timed_out=False,
        )
        failure = check.result_failure(result)
        self.assertIn("a flash with this run's build fixes", failure)

    def test_a_timed_out_read_is_a_failure_whatever_the_line_said(self):
        check = self.compile(self.expectation(bootloader.MODE_RECORD))[-1]
        result = CommandResult(
            argv=check.argv, returncode=-9, stdout="", stderr="",
            duration_s=5.0, timed_out=True,
        )
        self.assertIn("timed out", check.result_failure(result))

    def test_the_verdict_of_a_board_that_answered_is_read_off_the_line(self):
        check = self.compile(self.expectation(bootloader.MODE_CHECK))[-1]
        for line, verdict in ((FORK_LINE, "current"), (FACTORY_LINE, "stale")):
            with self.subTest(verdict):
                installed, status, reason = check.bootloader_reading(
                    CommandResult(argv=check.argv, returncode=0,
                                  stdout=line + "\n", stderr="",
                                  duration_s=0.01, timed_out=False)
                )
                self.assertIsNotNone(installed)
                self.assertEqual(verdict, status)
                self.assertEqual(verdict == "stale", bool(reason))


class EstablishedBoardReadTests(unittest.TestCase):
    """The read of a board whose application image the suite writes (#327).

    `Reset into invalid application` is about the bootloader already in its
    board's flash and writes the application region only, so leaving that
    board out left the check off exactly where it matters most. The board may
    still be carrying firmware older than the request when preflight runs,
    which is what the read now records instead of failing on.
    """

    @staticmethod
    def establishing_invocations():
        """One invocation of a test that writes its board's own image."""
        from ubertooth_harness.manifest import TestInvocation

        identity = MappingProxyType({"serial": "abcdef0123456789"})
        return (
            TestInvocation(
                "001-Flash reliability",
                "Flash reliability",
                "Host_Linux",
                MappingProxyType({"hosts": ("Host_Linux",),
                                  "Ubertooth": "default"}),
                MappingProxyType({"Ubertooth": "UT_Linux0"}),
                MappingProxyType({"Ubertooth": identity}),
            ),
        )

    def test_such_a_board_is_named_and_gets_the_read_it_used_to_be_left_out_of(self):
        from tests.test_manifest_preflight import prepare_preflight
        from ubertooth_harness.preflight import PreflightPolicy
        from ubertooth_harness.resources import FileResourceRegistry

        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = prepare_preflight(directory)
            invocations = self.establishing_invocations()
            policy = PreflightPolicy(
                FileResourceRegistry.load(root / "hardware.yaml")
            )
            # The complement of the boards a building run flashes, which is
            # what the stage installs on and what the read used to cover.
            self.assertEqual((), policy.boards_needing_firmware(invocations))
            self.assertEqual(("UT_Linux0",),
                             policy.boards_establishing_firmware(invocations))
            expectation = bootloader.BootloaderExpectation(
                mode=bootloader.MODE_CHECK,
                expected=bootloader.parse_identity(FORK_LINE + "\n"),
                boards={}, established=frozenset({"UT_Linux0"}),
            )
            checks = policy.compile(
                invocations, manifest.with_invocations(invocations),
                bootloader=expectation,
            )
        names = [check.name for check in checks]
        # Its firmware revision is still not required -- the test writes it --
        # and its bootloader now is.
        self.assertNotIn("Firmware version", names)
        self.assertIn("Device serial", names)
        self.assertIn("Bootloader identity", names)

    def compile(self, mode, *, established=True):
        from tests.test_manifest_preflight import (
            make_invocations, prepare_preflight,
        )
        from ubertooth_harness.preflight import PreflightPolicy
        from ubertooth_harness.resources import FileResourceRegistry

        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = prepare_preflight(directory)
            invocations = make_invocations()
            registry = FileResourceRegistry.load(root / "hardware.yaml")
            policy = PreflightPolicy(registry)
            alias = policy.boards_needing_firmware(invocations)[0]
            expectation = bootloader.BootloaderExpectation(
                mode=mode,
                expected=(
                    bootloader.parse_identity(FORK_LINE + "\n")
                    if mode in bootloader.COMPARING_MODES else None
                ),
                boards={},
                established=frozenset([alias] if established else []),
            )
            checks = policy.compile(
                invocations, manifest.with_invocations(invocations),
                bootloader=expectation,
            )
        return [check for check in checks
                if check.name == "Bootloader identity"][0]

    @staticmethod
    def result(check, *, returncode, stdout="", stderr=""):
        """The command's result, with the output view a run gives it.

        The view is attached, as the executor attaches it, so a caller can
        read back the checks `result_failure` recorded against it; without
        one, every `output_of` call makes a fresh view of its own.
        """
        from ubertooth_harness.outputfiles import CommandOutput

        return CommandResult(argv=check.argv, returncode=returncode,
                             stdout=stdout, stderr=stderr, duration_s=0.01,
                             timed_out=False,
                             output=CommandOutput(stdout=stdout,
                                                  stderr=stderr))

    def test_the_read_covers_it_and_accepts_the_refusal_as_the_reading(self):
        check = self.compile(bootloader.MODE_CHECK)
        # `-B` exits 1 when the firmware refuses it, as every read that
        # failed does, so the line is what tells this from a real failure.
        result = self.result(check, returncode=1, stderr=REFUSAL_LINE)
        installed, status, reason = check.bootloader_reading(result)
        self.assertIsNone(installed)
        self.assertEqual(bootloader.NOT_READ, status)
        self.assertEqual("", reason)
        self.assertEqual("", check.result_failure(result))

    def test_a_board_the_suite_does_not_write_still_has_to_answer(self):
        check = self.compile(bootloader.MODE_CHECK, established=False)
        result = self.result(check, returncode=1, stderr=REFUSAL_LINE)
        _installed, status, reason = check.bootloader_reading(result)
        self.assertEqual("unreadable", status)
        self.assertIn("did not report its bootloader", reason)
        self.assertIn("did not report its bootloader",
                      check.result_failure(result))

    def test_an_answer_from_such_a_board_is_compared(self):
        check = self.compile(bootloader.MODE_CHECK)
        matching = self.result(check, returncode=0, stdout=FORK_LINE + "\n")
        self.assertEqual("", check.result_failure(matching))
        stale = self.result(check, returncode=0, stdout=FACTORY_LINE + "\n")
        _installed, status, _reason = check.bootloader_reading(stale)
        self.assertEqual("stale", status)
        failure = check.result_failure(stale)
        self.assertIn("has bootloader no version block", failure)
        self.assertIn("--bootloader update does not install one there",
                      failure)

    def test_both_answers_record_one_check_that_passed(self):
        """A check that found nothing counts against the run wherever it is
        read back, so a board that legitimately refused the read must not
        leave one behind."""
        from ubertooth_harness.outputfiles import output_of
        from ubertooth_harness.report import _check_failed

        check = self.compile(bootloader.MODE_CHECK)
        for name, result in (
            ("answered", self.result(check, returncode=0,
                                     stdout=FORK_LINE + "\n")),
            ("refused", self.result(check, returncode=1, stderr=REFUSAL_LINE)),
        ):
            with self.subTest(name):
                self.assertEqual("", check.result_failure(result))
                checks = output_of(result).checks
                self.assertEqual(1, len(checks))
                self.assertEqual("observed", checks[0].kind)
                self.assertFalse(_check_failed(checks[0]))
        # A board that answered with something else is the case that does
        # come out against the run.
        stale = self.result(check, returncode=0, stdout=FACTORY_LINE + "\n")
        self.assertNotEqual("", check.result_failure(stale))
        (recorded,) = output_of(stale).checks
        self.assertTrue(_check_failed(recorded))

    def test_a_board_the_suite_does_not_write_records_the_line_as_before(self):
        from ubertooth_harness.outputfiles import output_of

        check = self.compile(bootloader.MODE_CHECK, established=False)
        result = self.result(check, returncode=0, stdout=FORK_LINE + "\n")
        self.assertEqual("", check.result_failure(result))
        kinds = [c.kind for c in output_of(result).checks]
        self.assertEqual(["observed", "expected"], kinds)

    def test_another_failure_of_the_read_is_still_a_failure(self):
        """A board nothing can open did not refuse the request."""
        check = self.compile(bootloader.MODE_RECORD)
        result = self.result(check, returncode=1,
                             stderr="error: no Ubertooth device found\n")
        _installed, status, reason = check.bootloader_reading(result)
        self.assertEqual("unreadable", status)
        self.assertIn("exited 1", reason)
        self.assertNotIn("has not got", reason)

    def test_the_run_record_carries_the_verdict_and_why(self):
        from ubertooth_harness.sequencer import _record_bootloader_identity

        check = self.compile(bootloader.MODE_CHECK)
        emitted = []

        class Publisher:
            def emit(self, event_type, status, details):
                emitted.append((event_type, status, details))

        _record_bootloader_identity(
            Publisher(), check,
            self.result(check, returncode=1, stderr=REFUSAL_LINE), "",
        )
        (event_type, status, details) = emitted[-1]
        self.assertEqual("bootloader_identity", event_type)
        self.assertEqual("passed", status)
        self.assertEqual(bootloader.NOT_READ, details["status"])
        self.assertIsNone(details["installed"])
        self.assertIn("has not got the bootloader read", details["reason"])
        self.assertIn("nothing was compared", details["reason"])

    def test_the_report_names_the_board_and_the_reason(self):
        from ubertooth_harness.report import _bootloader_section

        run = SimpleNamespace(events=[{
            "event_type": "bootloader_identity",
            "details": {
                "resource": "UT_Linux0", "serial": SERIAL_A,
                "mode": bootloader.MODE_CHECK, "installed": None,
                "expected": None, "written": False,
                "status": bootloader.NOT_READ,
                "reason": "UT_Linux0 says its firmware has not got the "
                          "bootloader read",
            },
        }])
        text = "\n".join(_bootloader_section(run))
        self.assertIn("UT_Linux0", text)
        self.assertIn(bootloader.NOT_READ, text)
        self.assertIn("has not got the bootloader read", text)


class StageBoardTests(unittest.TestCase):
    """Which boards the stage reads, compares, and installs on (#327).

    `testplans/power.yaml` is the suite that exposed this: its one Ubertooth
    is the board `Reset into invalid application` writes, so the stage used
    to find no board at all and preflight got no expectation -- the read was
    off exactly where the bootloader is the subject of the test.
    """

    @staticmethod
    def args(mode, *, boards=(), established=()):
        return SimpleNamespace(
            bootloader=mode, bootloader_boards=tuple(boards),
            bootloader_established=tuple(established),
        )

    def stage(self, args, *, artifact=None):
        from unittest import mock
        from ubertooth_harness import cli

        emitted = []

        class Publisher:
            def emit(self, event_type, status, details):
                emitted.append((event_type, status, details))

        class Builder:
            def __init__(self, _executor):
                pass

            def build(self, *_args, **_kwargs):
                return artifact

        source = SimpleNamespace(name="Ubertooth", short_commit="abc1234",
                                 expected_revision=RUN_REVISION)
        built = SimpleNamespace(plan=SimpleNamespace(work_dir=Path("/tmp/w")))
        with mock.patch.object(bootloader, "supported_at", lambda _s: True), \
                mock.patch.object(bootloader, "BootloaderArtifactBuilder",
                                  Builder):
            expectation, built_artifact = cli._bootloader_stage(
                args, (source,), built, None, Publisher(), Path("/tmp/run"),
                None,
            )
        return expectation, built_artifact, emitted

    def test_a_suite_whose_only_board_it_writes_is_still_read(self):
        expectation, _artifact, emitted = self.stage(
            self.args(bootloader.MODE_RECORD, established=["UT_Linux0"])
        )
        self.assertIsNotNone(expectation)
        self.assertEqual(frozenset({"UT_Linux0"}), expectation.established)
        self.assertEqual({}, dict(expectation.boards))
        self.assertEqual(
            [], [e for e in emitted if e[1] == "skipped"]
        )

    def test_a_suite_with_no_board_at_all_skips_the_stage(self):
        expectation, artifact, emitted = self.stage(
            self.args(bootloader.MODE_RECORD)
        )
        self.assertIsNone(expectation)
        self.assertIsNone(artifact)
        (_type, status, details) = emitted[-1]
        self.assertEqual("skipped", status)
        self.assertIn("no board of this suite is read", details["reason"])

    def test_update_says_it_has_no_board_to_install_on_and_still_compares(self):
        """The write covers the boards whose firmware preflight requires."""
        artifact = artifact_for()
        expectation, _built, emitted = self.stage(
            self.args(bootloader.MODE_UPDATE, established=["UT_Linux0"]),
            artifact=artifact,
        )
        self.assertIsNotNone(expectation)
        self.assertEqual(artifact.identity(), expectation.expected)
        skipped = [e for e in emitted if e[1] == "skipped"]
        self.assertTrue(skipped)
        self.assertIn("no board of this suite is installed on",
                      skipped[-1][2]["reason"])
        # Nothing was written: no update record of any kind.
        self.assertEqual([], [e for e in emitted
                              if e[0].startswith("bootloader_update")])


class ProgrammerFakeTests(unittest.TestCase):
    """The fake programmer on PATH, for a test that wants a real process."""

    def test_a_fake_lpc21isp_takes_the_documented_command_line(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: subprocess.run(["rm", "-rf", str(root)], check=False)
        )
        written = root / "written.txt"
        tool = write_executable(root / "lpc21isp", f"""#!/bin/sh
# Records the command line and the image it was given, as a bench programmer
# would write it to a board.
printf '%s\\n' "$*" >> {written}
exit 0
""")
        region = bootloader.region_image(built_image())
        hex_path = root / "region.hex"
        hex_path.write_text(bootloader.intel_hex(region))
        argv = bootloader.program_argv(hex_path, PROGRAMMER_DEVICE, tool=str(tool))
        self.assertEqual(0, subprocess.run(list(argv), check=False).returncode)
        self.assertEqual(
            f"-control {hex_path} {PROGRAMMER_DEVICE} 230400 4000",
            written.read_text().strip(),
        )


if __name__ == "__main__":
    unittest.main()


class ReportSectionTests(unittest.TestCase):
    """The report's Bootloader section, built from a run's own records."""

    @staticmethod
    def render(records):
        from ubertooth_harness.report import render_run

        events = [{
            "schema_version": 1, "run_id": "r", "sequence": 1,
            "timestamp": "2026-11-03T06:00:00", "event_type": "run_started",
            "status": "running", "details": {},
        }]
        for index, (kind, status, details) in enumerate(records, start=2):
            events.append({
                "schema_version": 1, "run_id": "r", "sequence": index,
                "timestamp": "2026-11-03T06:00:0%d" % min(index, 9),
                "event_type": kind, "status": status, "details": details,
            })
        events.append({
            "schema_version": 1, "run_id": "r", "sequence": len(events) + 1,
            "timestamp": "2026-11-03T06:01:00", "event_type": "run_finished",
            "status": "passed", "details": {"reason": ""},
        })
        return render_run(events)

    def test_a_board_this_run_wrote_says_so_after_preflight_read_it_again(self):
        """The stage writes, then preflight reads: the row keeps both facts."""
        installed = bootloader.parse_identity(FORK_LINE + "\n").to_dict()
        previous = bootloader.parse_identity(FACTORY_LINE + "\n").to_dict()
        text = self.render([
            ("bootloader_identity", "passed", {
                "resource": "UT_A", "serial": SERIAL_A, "mode": "update",
                "installed": installed, "previous": previous,
                "expected": installed, "written": True, "status": "updated",
                "reason": "",
            }),
            ("bootloader_identity", "passed", {
                "resource": "UT_A", "serial": SERIAL_A, "mode": "update",
                "installed": installed, "previous": None,
                "expected": installed, "written": False, "status": "current",
                "reason": "",
            }),
            ("bootloader_stage_finished", "passed", {
                "mode": "update", "reason": "", "expected": installed,
                "boards": [],
            }),
        ])
        self.assertIn("## Bootloader", text)
        row = next(
            line for line in text.splitlines()
            if line.startswith("| UT_A ")
        )
        self.assertIn("2.00 git-abc1234, CRC32:0x11112222", row)
        self.assertIn("| updated ", row)
        self.assertIn("| yes ", row)

    def test_recording_only_says_nothing_was_compared(self):
        text = self.render([
            ("bootloader_identity", "passed", {
                "resource": "UT_A", "serial": SERIAL_A, "mode": "record",
                "installed": bootloader.parse_identity(
                    FACTORY_LINE + "\n").to_dict(),
                "previous": None, "expected": None, "written": False,
                "status": "current", "reason": "",
            }),
        ])
        self.assertIn("Mode: `record`", text)
        self.assertIn("recorded what each board has", text)
        self.assertIn("no version block", text)

    def test_a_run_with_no_such_record_has_no_section(self):
        self.assertNotIn("## Bootloader", self.render([]))
