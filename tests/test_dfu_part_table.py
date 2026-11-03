"""The warning when the DFU flash sizes fall behind the fork's part table (#283).

Offline: the fork is a throwaway local git repository, never a clone. The
fixtures below are the fork's own text, so a parser that stops reading how it
writes those figures fails here rather than in a run.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ubertooth_harness import cli, dfuimage, dfuparts
from ubertooth_harness.dfuparts import (
    ForkParts,
    PartEntry,
    PartTableUnreadable,
    check_sources,
    drift_warning,
    fork_parts_at,
    parse_define,
    parse_number,
    parse_part_table,
)
from ubertooth_harness.events import (
    REPORT,
    RUN_LOG,
    EventPublisher,
    JsonlEventSink,
    RunEvent,
    TerminalEventSink,
)
from ubertooth_harness.manifest import SourceRevision
from ubertooth_harness.report import write_report


# host/ubertooth-tools/src/dfu_recovery.c at the fork's main, with the
# comment above the table kept: the entries are written with a comment in
# front of them and tabs for indent, which is what has to be read.
PART_TABLE_C = """\
/* The LPC175x/176x parts and the flash each one has, from the part
 * identification numbers in the NXP LPC176x/5x user manual. */
static const struct dfu_part DFU_PARTS[] = {
	{ 0x25001110, "LPC1751",  32 * 1024 },
	{ 0x25001118, "LPC1751",  32 * 1024 },
	{ 0x25001121, "LPC1752",  64 * 1024 },
	{ 0x25011722, "LPC1754", 128 * 1024 },
	{ 0x25011723, "LPC1756", 256 * 1024 },
	{ 0x25013F37, "LPC1758", 512 * 1024 },
	{ 0x25113737, "LPC1759", 512 * 1024 },
	{ 0x26012033, "LPC1763", 256 * 1024 },
	{ 0x26011922, "LPC1764", 128 * 1024 },
	{ 0x26013733, "LPC1765", 256 * 1024 },
	{ 0x26013F33, "LPC1766", 256 * 1024 },
	{ 0x26012837, "LPC1767", 512 * 1024 },
	{ 0x26013F37, "LPC1768", 512 * 1024 },
	{ 0x26113F37, "LPC1769", 512 * 1024 },
};

#define DFU_PART_COUNT (sizeof(DFU_PARTS) / sizeof(DFU_PARTS[0]))
"""

# dfu_recovery.h and dfu.h: the other two figures a read-back length follows.
RECOVERY_H = """\
/* The part the application is linked for, and so the size assumed for a part
 * that cannot be identified. */
#define DFU_ASSUMED_FLASH_SIZE (128u * 1024u)
"""
DFU_H = """\
#define BOOTLOADER_OFFSET 0x0
#define BOOTLOADER_SIZE 0x4000
"""

FORK_FILES = {
    "host/ubertooth-tools/src/dfu_recovery.c": PART_TABLE_C,
    "host/ubertooth-tools/src/dfu_recovery.h": RECOVERY_H,
    "host/ubertooth-tools/src/dfu.h": DFU_H,
}

# One more part, with more flash than any entry in the harness's copy has:
# the difference this check is for. No LPC175x/176x part has 1 MiB, which is
# the point -- it stands for whatever part the fork adds next.
GROWN_PART = '\t{ 0x281D3747, "LPC17-1M", 1024 * 1024 },\n'
GROWN_TABLE_C = PART_TABLE_C.replace(
    '\t{ 0x26113F37, "LPC1769", 512 * 1024 },\n',
    '\t{ 0x26113F37, "LPC1769", 512 * 1024 },\n' + GROWN_PART,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def _fork_parts(**changes) -> ForkParts:
    """The fork's own figures, with any of them replaced."""
    figures = {
        "parts": parse_part_table(PART_TABLE_C),
        "assumed_flash_size": 128 * 1024,
        "application_offset": 0x4000,
    }
    return ForkParts(**{**figures, **changes})


class ParseTests(unittest.TestCase):

    def test_integer_forms_the_fork_writes_these_figures_in(self):
        for text, value in (
            ("0x4000", 0x4000), ("0x0", 0), ("128", 128), ("128u", 128),
            ("128 * 1024", 128 * 1024), ("(128u * 1024u)", 128 * 1024),
            ("16 * 1024 * 1024", 16 * 1024 * 1024), ("0x25011722", 0x25011722),
            # C reads a leading zero as octal.
            ("010", 8), ("0100 * 2", 128),
        ):
            with self.subTest(text):
                self.assertEqual(value, parse_number(text))

    def test_a_figure_this_cannot_read_is_none_not_a_guess(self):
        """None, never an exception: this runs inside a run that must not
        fail on an advisory check."""
        for text in ("", "   ", "flash_size(part)", "1 + 2", "128 << 10", "FOO",
                     # Not a C literal at all: 8 and 9 are not octal digits.
                     "08", "0779"):
            with self.subTest(text):
                self.assertIsNone(parse_number(text))

    def test_the_forks_table_is_read_entry_by_entry(self):
        parts = parse_part_table(PART_TABLE_C)
        self.assertEqual(14, len(parts))
        self.assertEqual(PartEntry(0x25011722, "LPC1754", 128 * 1024), parts[3])
        self.assertEqual(dfuimage.PART_FLASH_SIZES,
                         _fork_parts().flash_sizes)
        # Named once each, in table order, for a message a reader can act on.
        self.assertEqual(("LPC1758", "LPC1759", "LPC1767", "LPC1768", "LPC1769"),
                         _fork_parts().labels_for(512 * 1024))
        self.assertEqual((), _fork_parts().labels_for(1024 * 1024))

    def test_a_comment_inside_the_table_is_not_an_entry(self):
        text = PART_TABLE_C.replace(
            '\t{ 0x25001121, "LPC1752",  64 * 1024 },\n',
            '\t/* { 0x25001121, "LPC1752", 64 * 1024 }, withdrawn */\n'
            '\t{ 0x25001121, "LPC1752",  64 * 1024 }, // the 64 KiB part\n',
        )
        self.assertEqual(14, len(parse_part_table(text)))

    def test_a_part_without_a_name_is_still_named_to_a_reader(self):
        self.assertEqual("part 0x25011722",
                         PartEntry(0x25011722, "", 128 * 1024).label())

    def test_no_table_is_none(self):
        self.assertIsNone(parse_part_table("/* no table here */\n"))

    def test_an_entry_this_cannot_read_is_reported_not_dropped(self):
        text = PART_TABLE_C.replace('"LPC1756", 256 * 1024',
                                    '"LPC1756", FLASH_256K')
        with self.assertRaisesRegex(PartTableUnreadable, r"LPC1756.*FLASH_256K"):
            parse_part_table(text)
        empty = "static const struct dfu_part DFU_PARTS[] = {\n};\n"
        with self.assertRaisesRegex(PartTableUnreadable, "holds no entry"):
            parse_part_table(empty)

    def test_a_row_written_another_way_is_reported_not_passed_over(self):
        """Every row has to be accounted for. A row this cannot read would
        otherwise drop a part and leave the comparison reporting a match,
        which is the whole failure #283 is about."""
        for description, row in (
            ("a fourth column", '\t{ 0x281D3747, "LPC17-1M", 1024 * 1024, 1 },\n'),
            ("a name that is not a literal", '\t{ 0x281D3747, NULL, 1024 * 1024 },\n'),
            ("a name from a macro", '\t{ 0x281D3747, PART_NAME, 1024 * 1024 },\n'),
            ("designated initializers",
             '\t{ .part_id = 0x281D3747, .flash_size = 1024 * 1024 },\n'),
            ("a macro instead of a row", '\tLPC_PART(0x281D3747, "LPC17-1M", 1024),\n'),
            ("two fields", '\t{ 0x281D3747, 1024 * 1024 },\n'),
        ):
            with self.subTest(description):
                text = PART_TABLE_C.replace(
                    '\t{ 0x26113F37, "LPC1769", 512 * 1024 },\n',
                    '\t{ 0x26113F37, "LPC1769", 512 * 1024 },\n' + row)
                with self.assertRaises(PartTableUnreadable):
                    parse_part_table(text)

    def test_an_array_whose_name_merely_ends_in_the_tables_is_not_the_table(self):
        text = PART_TABLE_C.replace("DFU_PARTS[]", "BOOTLOADER_DFU_PARTS[]")
        self.assertIsNone(parse_part_table(text))

    def test_an_old_table_left_in_a_comment_is_not_the_table(self):
        """A commented-out table above the real one must not be read as the
        real one, or the comparison would report the sizes the fork left
        behind."""
        # Only the array, since the fork's comment above it would close the
        # one wrapped around it.
        array = PART_TABLE_C[PART_TABLE_C.index("static const"):]
        commented = "/*\n" + array + "\n*/\n" + GROWN_TABLE_C
        self.assertEqual(parse_part_table(GROWN_TABLE_C), parse_part_table(commented))
        self.assertIn(1024 * 1024,
                      [part.flash_size for part in parse_part_table(commented)])

    def test_two_tables_at_once_are_reported_not_chosen_between(self):
        """A table kept behind `#if 0` is not a comment, so both are there;
        which one a read-back follows is not this module's to decide."""
        both = PART_TABLE_C + "\n#if 0\n" + GROWN_TABLE_C + "\n#endif\n"
        with self.assertRaisesRegex(PartTableUnreadable, "2 DFU_PARTS tables"):
            parse_part_table(both)

    def test_defines_are_read_and_a_missing_one_is_none(self):
        self.assertEqual(128 * 1024,
                         parse_define(RECOVERY_H, "DFU_ASSUMED_FLASH_SIZE"))
        self.assertEqual(0, parse_define(DFU_H, "BOOTLOADER_OFFSET"))
        self.assertEqual(0x4000, parse_define(DFU_H, "BOOTLOADER_SIZE"))
        self.assertIsNone(parse_define(DFU_H, "DFU_ASSUMED_FLASH_SIZE"))
        # A trailing comment belongs to neither the name nor the value.
        self.assertEqual(0x4000, parse_define(
            "#define BOOTLOADER_SIZE 0x4000 /* 16 KiB */\n", "BOOTLOADER_SIZE"))

    def test_a_define_this_cannot_read_is_reported(self):
        with self.assertRaisesRegex(PartTableUnreadable, r"not a figure this can read"):
            parse_define("#define BOOTLOADER_SIZE (SECTOR * 4)\n", "BOOTLOADER_SIZE")


class DriftWarningTests(unittest.TestCase):

    def test_the_forks_own_figures_are_quiet(self):
        """What the check is for: today's fork table is today's copy."""
        self.assertIsNone(drift_warning(_fork_parts()))
        self.assertIsNone(drift_warning(None))

    def test_a_part_the_copy_lacks_names_the_part_the_size_and_the_cost(self):
        grown = _fork_parts(parts=parse_part_table(GROWN_TABLE_C))
        warning = drift_warning(grown, fork_label="Ubertooth abc1234")
        self.assertIn("Ubertooth abc1234", warning)
        self.assertIn("LPC17-1M", warning)
        self.assertIn("1024 KiB of flash", warning)
        self.assertIn("PART_FLASH_SIZES does not list", warning)
        # The read-back such a board would report, which is what fails today.
        self.assertIn(f"{dfuimage.upload_length(1024 * 1024)} bytes", warning)

    def test_the_quoted_length_follows_the_forks_own_application_region(self):
        """A fork that moved both the table and the bootloader region: the
        length a board reports is the fork's arithmetic, not the copy's."""
        moved = _fork_parts(parts=parse_part_table(GROWN_TABLE_C),
                            application_offset=0x8000)
        warning = drift_warning(moved)
        self.assertIn(f"read-back of {1024 * 1024 - 0x8000} bytes", warning)
        self.assertIn("its application region starts at 0x8000", warning)

    def test_a_size_the_fork_dropped_is_reported_too(self):
        smaller = _fork_parts(parts=tuple(
            part for part in _fork_parts().parts if part.flash_size != 32 * 1024))
        warning = drift_warning(smaller)
        self.assertIn("PART_FLASH_SIZES lists 32 KiB, which no part in its table has",
                      warning)

    def test_the_assumed_size_and_the_application_offset_are_compared(self):
        assumed = drift_warning(_fork_parts(assumed_flash_size=256 * 1024))
        self.assertIn("it assumes 256 KiB for a part it cannot identify", assumed)
        self.assertIn("ASSUMED_FLASH_SIZE is 128 KiB", assumed)
        offset = drift_warning(_fork_parts(application_offset=0x8000))
        self.assertIn("its application region starts at 0x8000", offset)
        self.assertIn("APPLICATION_OFFSET is 0x4000", offset)

    def test_a_size_that_is_not_a_whole_kib_is_given_in_bytes(self):
        odd = _fork_parts(parts=(PartEntry(0x1, "LPC17xx", 100_000),))
        self.assertIn("with 100000 bytes of flash", drift_warning(odd))


class _ForkRepo:
    """A throwaway fork, built once for the class: one commit before the DFU
    recovery sources, then one holding them.

    A test that needs another commit takes a branch of its own, so `main`
    stays at the base commit and no test depends on another's order.
    """

    _branches = 0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.repo = Path(cls._tmp.name) / "ubertooth"
        cls.repo.mkdir()
        _git(cls.repo, "init", "-q", "-b", "main")
        (cls.repo / "README").write_text("before the DFU recovery sources\n")
        _git(cls.repo, "add", "README")
        _git(cls.repo, "commit", "-q", "-m", "no DFU recovery sources yet")
        cls.untabled = _git(cls.repo, "rev-parse", "HEAD")
        cls.tabled = cls._commit(FORK_FILES, (), "the DFU recovery sources")

    @classmethod
    def _commit(cls, files: dict[str, str], removed, message: str) -> str:
        for name, text in files.items():
            path = cls.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            _git(cls.repo, "add", name)
        for name in removed:
            _git(cls.repo, "rm", "-q", name)
        _git(cls.repo, "commit", "-q", "-m", message)
        return _git(cls.repo, "rev-parse", "HEAD")

    def _commit_files(self, files: dict[str, str], message: str,
                      removed: tuple[str, ...] = ()) -> str:
        """A commit over the base holding ``files``, on a branch of its own."""
        type(self)._branches += 1
        branch = f"case-{type(self)._branches}"
        _git(self.repo, "checkout", "-q", "-b", branch, self.tabled)
        try:
            return self._commit(files, removed, message)
        finally:
            _git(self.repo, "checkout", "-q", "main")

    def _sources(self, commit: str):
        return (SourceRevision("Ubertooth", str(self.repo), "", "main",
                               commit, commit[:7]),)

    def _run_dir(self) -> Path:
        out = tempfile.TemporaryDirectory()
        self.addCleanup(out.cleanup)
        return Path(out.name)


class ForkCheckoutTests(_ForkRepo, unittest.TestCase):
    """fork_parts_at and check_sources against a local repository."""

    def test_the_figures_come_from_the_commit_under_test(self):
        fork = fork_parts_at(self.repo, "main")
        self.assertEqual(dfuimage.PART_FLASH_SIZES, fork.flash_sizes)
        self.assertEqual((128 * 1024, 0x4000),
                         (fork.assumed_flash_size, fork.application_offset))

    def test_a_bad_ref_raises_rather_than_reading_as_no_table(self):
        with self.assertRaises(RuntimeError):
            fork_parts_at(self.repo, "no-such-ref")

    def test_a_commit_without_the_recovery_sources_has_nothing_to_compare(self):
        """The one thing None means. dfu.h is older than the part table, so a
        commit that has only that still counts as having no table."""
        self.assertIsNone(fork_parts_at(self.repo, self.untabled))
        only_dfu_h = self._commit_files(
            {}, "before dfu_recovery.c",
            removed=("host/ubertooth-tools/src/dfu_recovery.c",
                     "host/ubertooth-tools/src/dfu_recovery.h"))
        self.assertIsNone(fork_parts_at(self.repo, only_dfu_h))

    def test_a_table_that_moved_or_was_renamed_is_reported_not_skipped(self):
        """The failure this check must never have: the comparison stops
        working and says nothing, so drift walks past it."""
        for message, files in (
            ("renamed", {"host/ubertooth-tools/src/dfu_recovery.c":
                         PART_TABLE_C.replace("DFU_PARTS[]", "DFU_PART_TABLE[]")}),
            ("moved elsewhere", {"host/ubertooth-tools/src/dfu_recovery.c":
                                 "/* the table lives in dfu_parts.c now */\n"}),
        ):
            with self.subTest(message):
                commit = self._commit_files(files, f"table {message}")
                check = check_sources(self._sources(commit))
                self.assertEqual("unchecked", check.status)
                self.assertIn("no DFU_PARTS table in", check.message)
                self.assertIn("renamed, moved, or written in a way", check.message)
                # Only the files the table could be in, not every file read.
                self.assertNotIn("src/dfu.h", check.message)

    def test_a_reindented_table_is_still_read(self):
        indented = PART_TABLE_C.replace("\n};", "\n\t};")
        self.assertEqual(14, len(parse_part_table(indented)))

    def test_a_table_without_the_figures_beside_it_is_reported(self):
        commit = self._commit_files(
            {"host/ubertooth-tools/src/dfu.h": "/* moved */\n"}, "drop the offsets")
        with self.assertRaisesRegex(
            PartTableUnreadable,
            "BOOTLOADER_OFFSET and BOOTLOADER_SIZE are not",
        ):
            fork_parts_at(self.repo, commit)

    def test_a_run_against_todays_fork_passes_and_names_the_sizes(self):
        check = check_sources(self._sources(self.tabled))
        self.assertEqual("passed", check.status)
        self.assertIn(f"Ubertooth {self.tabled[:7]}", check.message)
        self.assertIn("32 KiB, 64 KiB, 128 KiB, 256 KiB, 512 KiB", check.message)
        self.assertEqual(
            {"message": check.message, "ubertooth_commit": self.tabled,
             "harness_flash_sizes": list(dfuimage.PART_FLASH_SIZES),
             "fork_flash_sizes": list(dfuimage.PART_FLASH_SIZES)},
            check.details(),
        )

    def test_a_run_against_a_grown_table_warns_and_records_both_sides(self):
        grown = self._commit_files(
            {"host/ubertooth-tools/src/dfu_recovery.c": GROWN_TABLE_C},
            "a part with more flash")
        check = check_sources(self._sources(grown))
        self.assertEqual("warning", check.status)
        self.assertIn("LPC17-1M", check.message)
        self.assertEqual(list(dfuimage.PART_FLASH_SIZES) + [1024 * 1024],
                         check.details()["fork_flash_sizes"])

    def test_a_commit_with_no_table_is_skipped_not_a_warning(self):
        check = check_sources(self._sources(self.untabled))
        self.assertEqual("skipped", check.status)
        # Both readings, since the sources cannot tell them apart.
        self.assertIn("has neither", check.message)
        self.assertIn("from before the part table or has moved it", check.message)
        self.assertIn("dfu_recovery.c", check.message)
        self.assertEqual([], check.details()["fork_flash_sizes"])

    def test_a_byte_that_is_not_utf8_does_not_raise_out_of_the_check(self):
        """One odd byte in a comment is not a reason to fail a run: the
        module's whole promise is that nothing here does."""
        path = self.repo / "host/ubertooth-tools/src/dfu_recovery.c"
        _git(self.repo, "checkout", "-q", "-b", "latin1", self.tabled)
        try:
            path.write_bytes(PART_TABLE_C.encode() + b"/* Ren\xe9 */\n")
            _git(self.repo, "add", str(path.relative_to(self.repo)))
            _git(self.repo, "commit", "-q", "-m", "a byte that is not UTF-8")
            commit = _git(self.repo, "rev-parse", "HEAD")
        finally:
            _git(self.repo, "checkout", "-q", "main")
        check = check_sources(self._sources(commit))
        self.assertEqual("passed", check.status)

    def test_the_check_says_why_it_could_not_run(self):
        gone = (SourceRevision("Ubertooth", str(self.repo / "gone"), "", "main",
                               self.tabled, self.tabled[:7]),)
        check = check_sources(gone)
        self.assertEqual("unchecked", check.status)
        self.assertIn("DFU part table not checked:", check.message)
        unreadable = self._commit_files(
            {"host/ubertooth-tools/src/dfu_recovery.c":
             PART_TABLE_C.replace("128 * 1024", "FLASH_128K")},
            "a table written another way")
        check = check_sources(self._sources(unreadable))
        self.assertEqual("unchecked", check.status)
        self.assertIn("FLASH_128K", check.message)
        # A run with no Ubertooth source is no run at all.
        self.assertIsNone(check_sources(()))

    def test_the_check_is_recorded_in_run_jsonl_and_report_md(self):
        grown = self._commit_files(
            {"host/ubertooth-tools/src/dfu_recovery.c": GROWN_TABLE_C},
            "a part with more flash")
        out = self._run_dir()
        publisher = EventPublisher("run", JsonlEventSink(out / RUN_LOG))
        publisher.emit("run_started", "running", {})
        cli._record_dfu_part_table(publisher, self._sources(grown))
        publisher.emit("run_finished", "passed", {})
        write_report(out)
        events = [json.loads(line) for line in (out / RUN_LOG).read_text().splitlines()]
        record = events[1]
        self.assertEqual(("dfu_part_table", "warning"),
                         (record["event_type"], record["status"]))
        self.assertIn("LPC17-1M", record["details"]["message"])
        report = (out / REPORT).read_text()
        self.assertRegex(report, r"\| DFU part table \| \*\*warning\*\*: .*LPC17-1M")

    def test_a_match_is_recorded_without_a_warning_row(self):
        out = self._run_dir()
        publisher = EventPublisher("run", JsonlEventSink(out / RUN_LOG))
        publisher.emit("run_started", "running", {})
        cli._record_dfu_part_table(publisher, self._sources(self.tabled))
        publisher.emit("run_finished", "passed", {})
        write_report(out)
        # A match is the row's own sentence, with no status in front of it:
        # only a difference is worth a reader's attention.
        row = next(line for line in (out / REPORT).read_text().splitlines()
                   if line.startswith("| DFU part table |"))
        self.assertIn("a DFU read-back is checked against the flash sizes", row)
        self.assertNotIn("**", row)

    def test_the_terminal_prints_a_difference_and_nothing_on_a_match(self):
        def shown(status: str) -> str:
            event = RunEvent(1, "run", 2, "2026-11-03T00:00:00.000", "dfu_part_table",
                             status, {"message": "sizes differ"})
            stream = io.StringIO()
            TerminalEventSink(stream).emit(event)
            return stream.getvalue()

        self.assertEqual("warning: sizes differ\n", shown("warning"))
        self.assertEqual("warning: sizes differ\n", shown("unchecked"))
        # Nothing a reader can act on: a match, and a commit without the
        # table, leave the report row to say so.
        self.assertEqual("", shown("passed"))
        self.assertEqual("", shown("skipped"))
        # The browser live view shows the same two, and stays quiet about the
        # same two.
        js = (Path(__file__).resolve().parents[1] / "ubertooth_harness" / "web"
              / "app.js").read_text(encoding="utf-8")
        self.assertIn('kind === "dfu_part_table" && (status === "warning" '
                      '|| status === "unchecked")', js)


class ReadbackMessageTests(unittest.TestCase):
    """A read-back length no part explains names the entry that is missing."""

    def test_the_message_names_the_part_size_the_length_implies(self):
        plan = dfuimage.readback_plan(
            "Reading back 1032192 bytes, 0x4000-0xfffff: part 0x281D3747 "
            "(LPC17-1M) has 1024 KiB of flash\n")
        self.assertEqual(
            ["the read-back covers 1032192 bytes, which no part in the harness's "
             "copy of the fork's part table explains (32 KiB, 64 KiB, 128 KiB, "
             "256 KiB, 512 KiB); that length would be the application region of a "
             "part with 1024 KiB of flash"],
            plan.problems(),
        )

    def test_every_length_the_forks_table_explains_passes(self):
        for flash_size in dfuimage.PART_FLASH_SIZES:
            length = dfuimage.upload_length(flash_size)
            end = dfuimage.APPLICATION_OFFSET + length - 1
            with self.subTest(flash_size=flash_size):
                plan = dfuimage.readback_plan(
                    f"Reading back {length} bytes, 0x4000-0x{end:x}: part "
                    f"0x25011722 has {flash_size // 1024} KiB of flash\n")
                self.assertEqual([], plan.problems())

    def test_the_event_name_is_the_one_the_run_record_uses(self):
        self.assertEqual("dfu_part_table", dfuparts.EVENT)


if __name__ == "__main__":
    unittest.main()
