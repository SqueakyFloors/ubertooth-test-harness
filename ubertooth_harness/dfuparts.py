"""Compare the flash sizes a DFU read-back is checked against with the fork's table.

`dfuimage.PART_FLASH_SIZES` is the harness's own copy of the distinct flash
sizes in the Ubertooth fork's DFU part table (`DFU_PARTS` in
host/ubertooth-tools/src/dfu_recovery.c), and `dfuimage.READBACK_LENGTHS` is
derived from it. A `-u` that reports a length outside that set is reported as
a problem, which is deliberate: a read-back figure neither the table nor the
linked-for assumption explains gets noticed rather than passed over.

What a hand copy costs is that a part the fork adds later with another flash
size turns a correct read-back into a failure, and nothing kept the two
copies in step (#283). This reads the figures out of the fork's checkout at
the commit a run resolved and warns when they differ, naming the part whose
entry the harness copy is missing, the way `libbtbbpin` compares the libbtbb
pin with the fork's `install.sh`.

Nothing here fails a run. The read-back check stays the harness's own copy,
so a length no part explains is still loud; this is what says to add the
entry. `run-suite` and `build` record it for the Ubertooth commit they
resolve as a `dfu_part_table` record in run.jsonl (status passed, warning,
unchecked, or skipped; report.md shows it, as does the browser live view) and
print a difference, or a comparison that stopped working, on stderr.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, NamedTuple, Sequence

from ubertooth_harness.dfuimage import (
    APPLICATION_OFFSET,
    ASSUMED_FLASH_SIZE,
    PART_FLASH_SIZES,
    describe_flash_size,
)

EVENT = "dfu_part_table"

# Set (to anything) when the check was already shown for this build, so the
# build and run-suite steps record it without printing it a second and third
# time, as the libbtbb pin check does.
SHOWN_ENV = "UBERTOOTH_HARNESS_DFU_PART_TABLE_SHOWN"

# The fork files the figures live in. All of them are read and searched
# together, so a #define that moves between these headers is still found.
SOURCE_FILES = (
    "host/ubertooth-tools/src/dfu_recovery.c",
    "host/ubertooth-tools/src/dfu_recovery.h",
    "host/ubertooth-tools/src/dfu.h",
)

# The files the table and the two figures beside it are read out of.
PART_TABLE_FILES = SOURCE_FILES[:2]

# What a tree that has these figures at all names somewhere, whatever it
# calls the files they are in: the table, the function whose arithmetic the
# harness mirrors, and the size that function falls back to. A tree that
# names none of them keeps nothing the harness mirrors, and is passed over; a
# tree that names any of them and whose figures this cannot read has moved or
# rewritten them, which is reported. Three names rather than one, because any
# one of them can be renamed on its own and the check must not fall silent
# for that.
TREE_MARKERS = ("DFU_PARTS", "dfu_upload_length", "DFU_ASSUMED_FLASH_SIZE")

# A part list being defined: an array of the fork's own part type. The type
# is what makes a list one the tool can look a part up in, so this finds a
# list whatever its array is called and whatever file holds it -- the table
# itself, a second list beside it, one added for another family. Keying on
# the array's name instead missed `struct dfu_part LPC18_PARTS[]`, and
# reported an ordinary derived array that happened to be named after the
# table.
#
# Written for `git grep --extended-regexp`, which is POSIX ERE and has no \t
# escape, so whitespace is a character class. A file this matches is then
# read, and the declaration has to survive having its comments and its
# string literals blanked: text alone cannot tell a declaration from a man
# page quoting one.
#
# Two patterns, because either key alone leaves a list unseen: one on the
# part type, which finds an array called anything, and one on the table's own
# name, which finds an array of any type. Both require the array to be
# given a value, `[...] =`, so a declaration or a prototype that mentions the
# type is not read as a list -- `extern const struct dfu_part DFU_PARTS[];`
# and `size_t count(const struct dfu_part parts[])` are not lists.
PART_LIST_DEFINITIONS = (
    r"(struct[[:space:]]+dfu_part|dfu_part_t)[[:space:]]*\*?[[:space:]]*"
    r"(const[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\[[^]]*\][[:space:]]*=",
    r"DFU_PARTS[A-Za-z0-9_]*[[:space:]]*\[[^]]*\][[:space:]]*=",
)
# The same two for confirming a file git grep found. `git grep` matches
# within a line, so a declaration split over two lines is found only here;
# that is a miss in the other direction, named with the limits below.
_PART_LIST_DEFINITION_RE = re.compile(
    r"(?:(?:struct\s+dfu_part|dfu_part_t)\s*\*?\s*(?:const\s+)?"
    r"[A-Za-z_][A-Za-z0-9_]*|DFU_PARTS[A-Za-z0-9_]*)\s*\[[^]]*\]\s*=")
# A C character literal, so a `'"'` in a source cannot be read as the start
# of a string and swallow the rest of the file.
_CHAR_LITERAL = re.compile(r"'(?:\\.|[^'\\])'")
# A test's own fixture is not what the tool compiles: the fork's host tests
# build arrays of their own to compare the table against, and reporting a run
# over one would be crying wolf.
TEST_PATH_PARTS = ("/tests/", "/test/", "/unit-tests/", "/unittests/",
                   "/testing/")
# A test file that sits beside the sources rather than under a directory of
# its own, written either way round.
_TEST_FILE_RE = re.compile(r"(^|/)(test[_-][^/]*|[^/]*[_-]test)\.[A-Za-z.]+$")
# Nor is a document that quotes the table. These are named rather than C
# sources being named, so that a list in a generated header's `.in` or in an
# included fragment still counts: what the tool compiles is not decided by a
# file extension, but a man page is certainly not it.
PROSE_SUFFIXES = (".md", ".rst", ".txt", ".1", ".adoc", ".html")

# The part of the tree whose part list decides a read-back length: the host
# tool's. The bootloader keeps a table of its own to bound its own read-back
# (firmware/bootloader/flash.h), and a firmware unit test in the fork fails
# if the two disagree, so a part list under firmware/ is already held to the
# host's and is not this module's to compare.
HOST_TREE = "host/"

# The function the three figures are read for. Its body has to name them, or
# the fork works them out from something else and the figures read here are
# no longer the ones it compiles. What this cannot see is the arithmetic
# itself: a body that still names all three and subtracts one term more
# compares as a match. Two other trees are beyond it as well: one that
# renames all three of TREE_MARKERS at once, which leaves nothing here to
# find, and a second part list whose type is a typedef under a name of its
# own AND whose array is not named for the table, which neither key below
# matches. The read-back step catches all three, by checking the length the
# tool reports against the flash the same line says the part has
# (dfuimage.ReadbackPlan._length_follows_the_reported_part).
UPLOAD_LENGTH_FUNCTION = "dfu_upload_length"
_FUNCTION_BODY = re.compile(
    rf"\b{UPLOAD_LENGTH_FUNCTION}\s*\([^)]*\)\s*\{{(.*?)^\}}", re.S | re.M)

# `static const struct dfu_part DFU_PARTS[] = { ... };`. The table ends at the
# `};` that closes it, which is the only one at the start of a line (an entry
# line ends in `},`), so a brace inside the table cannot end it.
# The brackets may hold a size: `DFU_PARTS[DFU_PART_COUNT] = {` is the same
# table, and refusing it would report a run over an ordinary edit.
_TABLE = re.compile(
    r"\bDFU_PARTS\b\s*\[[^]]*\]\s*=\s*\{(.*?)^[ \t]*\}[ \t]*;", re.S | re.M)
# One braced row of the table body, and the three fields a row must hold:
# `{ 0x25011722, "LPC1754", 128 * 1024 },`. A row is matched as a whole, so a
# row written any other way is reported rather than passed over.
_ROW = re.compile(r"\{([^{}]*)\}")
_FIELDS = re.compile(
    r"\s*(?P<id>[^,]+?)\s*,\s*\"(?P<name>[^\"]*)\"\s*,\s*(?P<flash>[^,]+?)\s*,?\s*")
# An integer literal with the suffixes C allows on one: `0x4000`, `128u`.
_LITERAL = re.compile(r"(0[xX][0-9a-fA-F]+|\d+)[uUlL]*")
# A fork checkout on an unresponsive filesystem must not hold up a run that
# already has the bench: this is advisory, so it gives up instead. The budget
# is for the whole check, not per git command, because it takes several of
# them (one read per file in SOURCE_FILES, and a search for each thing looked
# for in the tree). A warm clone takes about two seconds.
GIT_TIMEOUT_S = 15.0


class PartTableUnreadable(RuntimeError):
    """The fork writes one of these figures in a way this module cannot read."""


class Deadline:
    """One budget for a whole check, shared by the git commands it takes.

    A per-command timeout would let a check that takes five of them hold a
    run for five times as long, and by then the run has its boards.
    """

    def __init__(self, budget_s: float = GIT_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic):
        self.budget_s = budget_s
        self._clock = clock
        self._ends_at = clock() + budget_s

    def remaining(self) -> float:
        """What is left of the budget.

        Spent is spent: this raises rather than handing out a sliver, or each
        command left would still start and the budget would not be a budget.
        """
        left = self._ends_at - self._clock()
        if left <= 0:
            raise RuntimeError(
                f"the {self.budget_s:.0f}s this check is given is spent")
        return left


class PartEntry(NamedTuple):
    """One row of the fork's DFU part table."""

    part_id: int
    name: str
    flash_size: int

    def label(self) -> str:
        """How to name this part to a reader: its name, or its identifier."""
        return self.name or f"part 0x{self.part_id:08x}"


def parse_number(text: str) -> int | None:
    """``text`` as an integer, or None when it is not a form this can read.

    These figures are written as a literal or as literals multiplied
    together, with optional `u` suffixes and parentheses: `0x4000`,
    `128 * 1024`, `(128u * 1024u)`. A figure written any other way is
    reported by the caller rather than guessed at, so this returns None for
    anything it cannot read and raises nothing.
    """
    stripped = text.replace("(", " ").replace(")", " ").strip()
    if not stripped:
        return None
    value = 1
    for factor in stripped.split("*"):
        found = _LITERAL.fullmatch(factor.strip())
        if found is None:
            return None
        literal = found.group(1)
        # C reads a leading zero as octal, which Python's base 0 refuses.
        if literal[:2].lower() == "0x":
            base = 16
        elif len(literal) > 1 and literal[0] == "0":
            base = 8
        else:
            base = 10
        try:
            value *= int(literal, base)
        except ValueError:
            return None
    return value


def _without_comments(text: str, *, strings: bool = True) -> str:
    """``text`` with its C comments blanked out, line for line.

    One pass left to right, the way a compiler reads it, because the order
    matters: a `/*` inside a `//` comment opens nothing, and neither opens
    inside a string literal. Stripping the two kinds in separate passes let
    such a `/*` run on to the next `*/` and swallow a table row with it.
    A comment becomes spaces and keeps its newlines, so what is left still
    lines up with the file.
    """
    out: list[str] = []
    state = "code"
    index, end = 0, len(text)
    while index < end:
        char, pair = text[index], text[index:index + 2]
        if state == "code":
            if pair in ("//", "/*"):
                state = "line" if pair == "//" else "block"
                out.append("  ")
                index += 2
                continue
            if char == "'":
                # A character literal, `'"'` among them, which must not be
                # read as a string starting and swallow the rest of the file.
                found = _CHAR_LITERAL.match(text, index)
                if found:
                    out.append(found.group(0))
                    index = found.end()
                    continue
            if char == '"':
                state = "string"
            out.append(char)
        elif state == "line":
            # A newline ends it. C continues a line comment over a trailing
            # backslash; not following that leaves more text to read rather
            # than less, so a row can only become loud, never disappear.
            state = "code" if char == "\n" else state
            out.append(char if char == "\n" else " ")
        elif state == "block":
            if pair == "*/":
                state = "code"
                out.append("  ")
                index += 2
                continue
            out.append("\n" if char == "\n" else " ")
        else:  # inside a string literal
            if char == "\\" and index + 1 < end:
                out.append(text[index:index + 2] if strings else "  ")
                index += 2
                continue
            if char == '"':
                state = "code"
                out.append(char)
            else:
                # Kept for the table, whose part names are string literals;
                # blanked when the question is whether a file declares
                # something, since a declaration quoted in a string is text
                # about code and not code.
                out.append(char if strings else " ")
        index += 1
    return "".join(out)


def _table_rows(body: str) -> list[str]:
    """The braced rows of a table body, refusing a body with anything else.

    Every row of the table has to be accounted for. A row written without
    braces -- a macro call, say -- leaves text between the braced rows, and
    that is reported: reading such a table as the rows it happened to
    recognize would hide a part, which is the difference this is here to find.
    """
    rows, between = [], []
    end = 0
    for found in _ROW.finditer(body):
        between.append(body[end:found.start()])
        rows.append(found.group(1))
        end = found.end()
    between.append(body[end:])
    leftover = "".join(between).replace(",", "").strip()
    if leftover:
        raise PartTableUnreadable(
            "the DFU_PARTS table holds a row this cannot read "
            f"({' '.join(leftover.split())[:80]})"
        )
    return rows


def parse_part_table(text: str) -> tuple[PartEntry, ...] | None:
    """The DFU_PARTS entries in ``text``, or None when it holds no table.

    An entry written in a way this cannot read raises PartTableUnreadable,
    and so does a table whose rows are not all entries: dropping one would
    hide exactly the difference this is here to find. So does more than one
    table, since which of them a read-back length follows is then not this
    module's to decide -- a table left behind `#if 0` beside the real one
    reads that way, and a commented-out one is gone before the search.
    """
    bodies = _TABLE.findall(_without_comments(text))
    if not bodies:
        return None
    if len(bodies) > 1:
        raise PartTableUnreadable(
            f"{len(bodies)} DFU_PARTS tables here, so which one a read-back "
            "length follows is not clear"
        )
    entries = []
    for row in _table_rows(bodies[0]):
        fields = _FIELDS.fullmatch(row)
        identifier = parse_number(fields["id"]) if fields else None
        size = parse_number(fields["flash"]) if fields else None
        if fields is None or identifier is None or size is None:
            raise PartTableUnreadable(
                "a DFU_PARTS entry is written in a way this cannot read "
                f"({{{' '.join(row.split())}}})"
            )
        entries.append(PartEntry(identifier, fields["name"], size))
    if not entries:
        raise PartTableUnreadable("the DFU_PARTS table holds no entry")
    return tuple(entries)


def parse_define(text: str, name: str) -> int | None:
    """The value of ``#define name ...`` in ``text``, or None for no such define.

    A define that is there and is not a figure this can read raises
    PartTableUnreadable, for the same reason a table entry does.
    """
    found = re.findall(rf"^[ \t]*#[ \t]*define[ \t]+{re.escape(name)}[ \t]+(.+)$",
                       _without_comments(text), re.M)
    if not found:
        return None
    values = {parse_number(value) for value in found}
    if len(values) > 1:
        # Two definitions, one per branch of an #if, or one per file: which
        # of them the fork compiles is not this module's to choose, for the
        # same reason two tables are not.
        raise PartTableUnreadable(
            f"{name} is defined {len(found)} times here, with different values, "
            "so which one the fork compiles is not clear"
        )
    value = values.pop()
    if value is None:
        raise PartTableUnreadable(
            f"{name} is not a figure this can read ({found[0].strip()})"
        )
    return value


@dataclass(frozen=True)
class ForkParts:
    """The figures a read-back length follows, read out of one fork commit."""

    parts: tuple[PartEntry, ...]
    assumed_flash_size: int
    application_offset: int

    @property
    def flash_sizes(self) -> tuple[int, ...]:
        """The distinct flash sizes in the table, smallest first."""
        return tuple(sorted({part.flash_size for part in self.parts}))

    def labels_for(self, flash_size: int) -> tuple[str, ...]:
        """The parts with ``flash_size`` of flash, in table order, each once."""
        labels: list[str] = []
        for part in self.parts:
            if part.flash_size == flash_size and part.label() not in labels:
                labels.append(part.label())
        return tuple(labels)


def _show(source: Path | str, ref: str, path: str,
          deadline: Deadline | None = None) -> str | None:
    """``path`` at ``ref`` of a checkout, or None when that commit has none.

    Git runs under ``LC_ALL=C``, since a missing path is told from a failure
    by git's own wording.
    """
    deadline = deadline or Deadline()
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "show", f"{ref}:{path}"],
            capture_output=True, text=True, check=False,
            timeout=deadline.remaining(),
            # A byte that is not valid here is a byte in a C comment: it must
            # not raise out of a check that is only advisory.
            errors="replace",
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        # A run that reaches here already holds its boards, so this gives up
        # rather than waiting on the checkout.
        raise RuntimeError(
            f"reading {path} at {ref} in {source} took longer than the "
            f"{deadline.budget_s:.0f}s this check is given"
        ) from None
    if result.returncode != 0:
        if "does not exist" in result.stderr or "exists on disk, but not in" in result.stderr:
            return None
        raise RuntimeError(
            f"cannot read {path} at {ref} in {source}: {result.stderr.strip()}"
        )
    return result.stdout


def _holds_a_part_list(source: Path | str, ref: str, path: str,
                       deadline: Deadline) -> bool:
    """Whether ``path`` really defines a part list, not just text that reads
    like one.

    A grep over a tree finds a man page quoting the table in a fenced block,
    a test holding the declaration in a string literal, and a commented-out
    copy. None of those is a list the fork compiles, and reporting a run over
    one would be crying wolf. So the declaration has to survive having its
    comments and its string literals blanked. The file's name is not what
    decides: a list in a generated header's `.in`, or in an included
    fragment, is compiled like any other.
    """
    if any(part in f"/{path}" for part in TEST_PATH_PARTS):
        return False
    if _TEST_FILE_RE.search(path):
        return False
    if path.endswith(PROSE_SUFFIXES):
        return False
    # A read that fails raises, rather than being reported as a part list
    # nothing here ever saw.
    text = _show(source, ref, path, deadline)
    if text is None:
        return False
    return part_lists_in(text) > 0


def part_lists_in(text: str) -> int:
    """How many part lists ``text`` defines.

    A declaration that matches one of the patterns is only a list when what
    it is given looks like part entries: rows of an identifier, a name in
    quotes, and a size. That is what tells the table, and a second list of
    any type or name, from an array merely named after the table -- a
    `DFU_PARTS_BY_SIZE[] = { 32, 64, 128 }` holds no names and is no part
    list. Comments and the insides of strings are blanked first, so a
    commented-out copy and a declaration quoted in a string count for
    nothing; the quotes themselves stay, which is what a row is known by.
    """
    clean = _without_comments(text, strings=False)
    lists = 0
    for found in _PART_LIST_DEFINITION_RE.finditer(clean):
        tail = clean[found.end():]
        end = tail.find("};")
        body = tail if end < 0 else tail[:end]
        if any(_FIELDS.fullmatch(row) for row in _ROW.findall(body)):
            lists += 1
    return lists


def tree_mentions(source: Path | str, ref: str, pattern: str,
                  deadline: Deadline | None = None) -> list[str]:
    """Files at ``ref`` matching ``pattern``, wherever in the tree they are.

    Read on every check, because where these figures are is not something to
    assume: a tree that holds them only in files this does not read has moved
    them, and both of those have to be told from a tree that names them
    nowhere, which is simply older than all of this.
    """
    deadline = deadline or Deadline()
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "grep", "--name-only", "--extended-regexp",
             pattern, ref],
            capture_output=True, text=True, check=False, errors="replace",
            timeout=deadline.remaining(), env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"searching {ref} in {source} for {pattern} took longer than the "
            f"{deadline.budget_s:.0f}s this check is given"
        ) from None
    # git grep exits 1 for no match, and above that for a real failure.
    if result.returncode > 1:
        raise RuntimeError(
            f"cannot search {ref} in {source} for {pattern}: {result.stderr.strip()}"
        )
    # Each line is `<ref>:<path>`; the ref is the caller's own.
    return [line.split(":", 1)[-1] for line in result.stdout.splitlines() if line]


def fork_parts_at(source: Path | str, ref: str,
                  deadline: Deadline | None = None) -> ForkParts | None:
    """The part figures at ``ref`` of a fork checkout, or None for none.

    None means one thing: this commit names none of TREE_MARKERS anywhere, so
    it is older than the figures themselves and has none to compare with.
    Every other way of not finding them raises PartTableUnreadable -- the
    table renamed, moved to a file not read here, written another way, a
    second list elsewhere, or the length worked out from something other than
    the three figures read here. A check that cannot see the figures says so;
    only a tree that never had them is passed over. Git errors other than a
    missing path raise RuntimeError.
    """
    deadline = deadline or Deadline()
    texts = {path: _show(source, ref, path, deadline) for path in SOURCE_FILES}
    read = [path for path in SOURCE_FILES if texts[path] is not None]
    # Where the tree keeps these figures, rather than where this module hopes
    # they are. Read on every check, so a part list in a file this does not
    # read is as visible as a table that moved out of the files it reads.
    marked = {pattern: tree_mentions(source, ref, pattern, deadline)
              for pattern in TREE_MARKERS}
    if not any(marked.values()):
        # Nothing this reads is named anywhere in the tree. Whether the files
        # happen to exist says nothing: dfu_recovery.c arrived before the
        # part table did, so 51 of 474 real fork refs are such a commit, and
        # treating the file's presence as a rename made every one of them cry
        # wolf. The names are the signal, and all of them being gone means
        # this fork keeps none of what the harness mirrors.
        return None
    defined = [path for pattern in PART_LIST_DEFINITIONS
               for path in tree_mentions(source, ref, pattern, deadline)]
    elsewhere = sorted(
        path for path in set(defined) - set(read)
        if path.startswith(HOST_TREE) and _holds_a_part_list(source, ref, path, deadline)
    )
    if elsewhere:
        raise PartTableUnreadable(
            f"a part list is defined in {', '.join(elsewhere)}, which this does "
            f"not read: the flash sizes come from "
            f"{' and '.join(PART_TABLE_FILES)}, so a part list elsewhere in the "
            "host tools is one this has not compared"
        )
    joined = "\n".join(texts[path] for path in read)
    # A second list in the files that were read, which the search above
    # cannot see because these are the files it reads from. One table is
    # compared; a second one beside it would not be.
    lists = {path: part_lists_in(texts[path]) for path in read}
    if sum(lists.values()) > 1:
        holding = ", ".join(f"{path} ({count})"
                            for path, count in lists.items() if count)
        raise PartTableUnreadable(
            f"{sum(lists.values())} part lists are defined in {holding}, and "
            "only the DFU_PARTS table is compared, so the others are lists "
            "this has not looked at"
        )
    parts = parse_part_table(joined)
    if parts is None:
        where = ", ".join(sorted({path for paths in marked.values()
                                  for path in paths}))
        raise PartTableUnreadable(
            f"no DFU_PARTS table in {' or '.join(PART_TABLE_FILES)}, which this "
            f"reads the flash sizes out of, though {where} names what it looks "
            "for: the table was renamed, moved, or written in a way this does "
            "not recognize"
        )
    figures = {}
    for name in ("DFU_ASSUMED_FLASH_SIZE", "BOOTLOADER_OFFSET", "BOOTLOADER_SIZE"):
        figures[name] = parse_define(joined, name)
    missing = [name for name, value in figures.items() if value is None]
    if missing:
        raise PartTableUnreadable(
            f"a DFU part table is there, but {' and '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not, so the read-back "
            "lengths cannot be worked out from it"
        )
    _check_figures_are_the_ones_used(joined, figures)
    return ForkParts(parts, figures["DFU_ASSUMED_FLASH_SIZE"],
                     figures["BOOTLOADER_OFFSET"] + figures["BOOTLOADER_SIZE"])


def _check_figures_are_the_ones_used(text: str, figures: dict) -> None:
    """Refuse figures the fork no longer works a read-back length out of.

    Reading three `#define`s says what they are, not that they are still what
    the length follows. So the function the harness mirrors has to name them
    itself: that is the one place their meaning lives. A fork that renamed
    them and left these behind at their old values is caught that way.

    What this does not see is a body that still names all three and works the
    length out differently; the read-back step catches that, by the length
    the tool reports against the flash the same line gives.
    """
    # Comments first, as the table and the defines are read: a figure named
    # only in a comment is not a use, and a commented-out copy of the
    # function is not the definition.
    body = _FUNCTION_BODY.search(_without_comments(text))
    if body is None:
        raise PartTableUnreadable(
            f"the figures are here, but no {UPLOAD_LENGTH_FUNCTION}() this can "
            f"read, so what the fork works a read-back length out of is not "
            "visible here"
        )
    # Whole words: a fork that namespaced these to DFU_BOOTLOADER_OFFSET or
    # DFU_ASSUMED_FLASH_SIZE_V2 names none of them, and substring containment
    # would have read the new names as the old ones.
    unused = [name for name in figures
              if not re.search(rf"\b{re.escape(name)}\b", body.group(1))]
    if unused:
        raise PartTableUnreadable(
            f"{UPLOAD_LENGTH_FUNCTION}() does not name "
            f"{' or '.join(sorted(unused))} any more, so this cannot confirm "
            "that the figures read here are the ones a read-back length "
            "follows; a rename that leaves the arithmetic alone reads the "
            "same way"
        )


def drift_warning(fork: ForkParts | None, *,
                  fork_label: str = "the Ubertooth fork") -> str | None:
    """A one-line warning when the harness's figures differ from the fork's.

    Each difference says what the fork has and what the harness copy has,
    so a reader knows which entry to put right without going to the fork's
    source, and what it costs a run where it costs anything: a part the copy
    lacks fails a read-back it should pass, while a size the fork has dropped
    only leaves the copy stale.
    """
    if fork is None:
        return None
    problems = []
    known = set(PART_FLASH_SIZES)
    for flash_size in fork.flash_sizes:
        if flash_size in known:
            continue
        labels = ", ".join(fork.labels_for(flash_size))
        # The length such a board really reports follows the fork's own
        # application region, which may have moved along with the table.
        length = max(flash_size - fork.application_offset, 0)
        problems.append(
            f"it has {labels} with {describe_flash_size(flash_size)} of flash, which "
            f"PART_FLASH_SIZES does not list, so a correct read-back of {length} "
            "bytes on such a part would be reported as a length no part explains"
        )
    for flash_size in sorted(known - set(fork.flash_sizes)):
        problems.append(
            f"PART_FLASH_SIZES lists {describe_flash_size(flash_size)}, which no part "
            "in its table has any more, so the copy is stale"
        )
    if fork.assumed_flash_size != ASSUMED_FLASH_SIZE:
        problems.append(
            f"it assumes {describe_flash_size(fork.assumed_flash_size)} for a part it "
            f"cannot identify, where ASSUMED_FLASH_SIZE is "
            f"{describe_flash_size(ASSUMED_FLASH_SIZE)}"
        )
    if fork.application_offset != APPLICATION_OFFSET:
        problems.append(
            f"its application region starts at 0x{fork.application_offset:x}, where "
            f"APPLICATION_OFFSET is 0x{APPLICATION_OFFSET:x}"
        )
    if not problems:
        return None
    return (f"the DFU flash sizes a read-back is checked against differ from "
            f"{fork_label}'s part table: " + "; ".join(problems))


@dataclass(frozen=True)
class PartTableCheck:
    """The outcome of comparing the harness's copy with the fork's table.

    ``status``: passed (the same figures), warning (they differ), unchecked
    (the check could not run; ``message`` says why), or skipped (the fork
    commit has no part table to compare with).
    """

    status: str
    message: str
    ubertooth_commit: str
    fork_flash_sizes: tuple[int, ...] = ()

    def details(self) -> dict:
        return {
            "message": self.message,
            "ubertooth_commit": self.ubertooth_commit,
            "harness_flash_sizes": list(PART_FLASH_SIZES),
            "fork_flash_sizes": list(self.fork_flash_sizes),
        }


def record(publisher, sources: Sequence) -> None:
    """Record the comparison for a run's sources, if it has an Ubertooth one.

    Every run records it, not only one that builds: the flash sizes are
    checked against whatever fork commit a run resolves, and the harness's
    copy falling behind costs a run with installed tools just as much. The
    caller that emitted ``run_started`` records it, so it lands once.

    Nothing here fails a run, and that holds whatever a fork tree or a bug
    in this module throws: the statuses above cover what is foreseen, and
    anything else is recorded as unchecked rather than left to end the run
    without even a ``run_finished``.
    """
    # Read before the try, so a handler can name the commit without going
    # back to the thing that may have raised.
    commit = ""
    try:
        for source in sources:
            if source.name == "Ubertooth":
                commit = source.commit
                break
    except Exception:  # noqa: BLE001 - see the docstring
        pass
    try:
        check = check_sources(sources)
    except Exception as error:  # noqa: BLE001 - see the docstring
        check = PartTableCheck(
            "unchecked",
            f"DFU part table not checked: {type(error).__name__}: {error}",
            commit,
        )
    if check is None:
        return
    try:
        publisher.emit(EVENT, check.status, check.details())
    except Exception as error:  # noqa: BLE001 - see the docstring
        # A message too long for one record, say: the record is worth less
        # than the run, so say what happened in the shortest form there is.
        publisher.emit(EVENT, "unchecked", {
            "message": f"DFU part table not recorded: {type(error).__name__}",
            "ubertooth_commit": commit,
            "harness_flash_sizes": list(PART_FLASH_SIZES),
            "fork_flash_sizes": [],
        })


def check_sources(sources: Sequence,
                  deadline: Deadline | None = None) -> PartTableCheck | None:
    """Compare the harness's copy with the fork's table at a run's Ubertooth
    commit (manifest.SourceRevision objects).

    None when the run has no Ubertooth source, which is no run at all.
    """
    ubertooth = next((source for source in sources if source.name == "Ubertooth"), None)
    if ubertooth is None:
        return None
    label = f"Ubertooth {ubertooth.short_commit}"

    try:
        fork = fork_parts_at(ubertooth.source_path, ubertooth.commit, deadline)
    except (OSError, RuntimeError) as error:
        return PartTableCheck(
            "unchecked", f"DFU part table not checked: {error}", ubertooth.commit
        )
    if fork is None:
        markers = ", ".join(TREE_MARKERS[:-1]) + f", and {TREE_MARKERS[-1]}"
        return PartTableCheck(
            "skipped",
            f"DFU part table not checked: {label} names none of {markers} "
            "anywhere, so it is older than the figures and has none to "
            "compare with",
            ubertooth.commit,
        )
    warning = drift_warning(fork, fork_label=label)
    if warning is not None:
        return PartTableCheck("warning", warning, ubertooth.commit, fork.flash_sizes)
    sizes = ", ".join(describe_flash_size(size) for size in fork.flash_sizes)
    return PartTableCheck(
        "passed",
        f"a DFU read-back is checked against the flash sizes {label} has in its "
        f"part table ({sizes})",
        ubertooth.commit,
        fork.flash_sizes,
    )


def main(argv=None) -> int:
    """Compare the harness's copy with one fork commit, from the command line.

    `scripts/ci.sh dfu-part-table` runs this against the fork's main, so a
    part the fork adds shows up on the workflow run page and not only in the
    run record of whoever happens to start a run next.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ubertooth_harness.dfuparts",
        description="warn when the DFU flash sizes differ from the fork's part table",
    )
    parser.add_argument("--ubertooth-source", type=Path, required=True)
    parser.add_argument("--ubertooth-ref", default="HEAD")
    args = parser.parse_args(argv)
    label = f"Ubertooth {args.ubertooth_ref}"
    try:
        fork = fork_parts_at(args.ubertooth_source, args.ubertooth_ref)
    except (OSError, RuntimeError) as error:
        message = str(error)
    else:
        if fork is None:
            # Nothing to warn about: this commit is older than the figures,
            # which every release before the part table is. The run record
            # calls this skipped and stays quiet about it too.
            markers = ", ".join(TREE_MARKERS[:-1]) + f", and {TREE_MARKERS[-1]}"
            print(f"{label} names none of {markers}, so it has no part table "
                  "to compare with")
            return 0
        else:
            message = drift_warning(fork, fork_label=label)
            if message is None:
                sizes = ", ".join(describe_flash_size(size)
                                  for size in fork.flash_sizes)
                print(f"the DFU flash sizes match {label}'s part table ({sizes})")
                return 0
    # A warning, not a failure: the read-back check itself stays loud, and a
    # fork that has moved on is for someone to follow, not for CI to block.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=DFU part table::{message}")
    print(f"warning: {message}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
