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

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import NamedTuple, Sequence

from ubertooth_harness.dfuimage import (
    APPLICATION_OFFSET,
    ASSUMED_FLASH_SIZE,
    PART_FLASH_SIZES,
    describe_flash_size,
    upload_length,
)

EVENT = "dfu_part_table"

# The fork files the figures live in. All of them are read and searched
# together, so a #define that moves between these headers is still found.
SOURCE_FILES = (
    "host/ubertooth-tools/src/dfu_recovery.c",
    "host/ubertooth-tools/src/dfu_recovery.h",
    "host/ubertooth-tools/src/dfu.h",
)

# The file the table itself lives in. Whether it is there is what tells a
# commit from before the DFU recovery sources from one whose table this can
# no longer find: the first has nothing to compare with, the second is a
# difference to report.
PART_TABLE_FILES = SOURCE_FILES[:2]

# `static const struct dfu_part DFU_PARTS[] = { ... };`. The table ends at the
# `};` that closes it, which is the only one at the start of a line (an entry
# line ends in `},`), so a brace inside the table cannot end it.
_TABLE = re.compile(r"\bDFU_PARTS\b\s*\[\]\s*=\s*\{(.*?)^[ \t]*\}[ \t]*;", re.S | re.M)
# One braced row of the table body, and the three fields a row must hold:
# `{ 0x25011722, "LPC1754", 128 * 1024 },`. A row is matched as a whole, so a
# row written any other way is reported rather than passed over.
_ROW = re.compile(r"\{([^{}]*)\}")
_FIELDS = re.compile(
    r"\s*(?P<id>[^,]+?)\s*,\s*\"(?P<name>[^\"]*)\"\s*,\s*(?P<flash>[^,]+?)\s*,?\s*")
# An integer literal with the suffixes C allows on one: `0x4000`, `128u`.
_LITERAL = re.compile(r"(0[xX][0-9a-fA-F]+|\d+)[uUlL]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")
# A fork checkout on an unresponsive filesystem must not hold up a run that
# already has the bench: this is advisory, so it gives up instead.
GIT_TIMEOUT_S = 30.0


class PartTableUnreadable(RuntimeError):
    """The fork writes one of these figures in a way this module cannot read."""


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


def _without_comments(text: str) -> str:
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", text))


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
    hide exactly the difference this is here to find.
    """
    found = _TABLE.search(text)
    if found is None:
        return None
    entries = []
    for row in _table_rows(_without_comments(found.group(1))):
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
    found = re.search(rf"^[ \t]*#[ \t]*define[ \t]+{re.escape(name)}[ \t]+(.+)$",
                      _without_comments(text), re.M)
    if found is None:
        return None
    value = parse_number(found.group(1))
    if value is None:
        raise PartTableUnreadable(
            f"{name} is not a figure this can read ({found.group(1).strip()})"
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


def _show(source: Path | str, ref: str, path: str) -> str | None:
    """``path`` at ``ref`` of a checkout, or None when that commit has none.

    Git runs under ``LC_ALL=C``, since a missing path is told from a failure
    by git's own wording.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "show", f"{ref}:{path}"],
            capture_output=True, text=True, check=False, timeout=GIT_TIMEOUT_S,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        # A run that reaches here already holds its boards, so this gives up
        # rather than waiting on the checkout.
        raise RuntimeError(
            f"reading {path} at {ref} in {source} took longer than "
            f"{GIT_TIMEOUT_S:.0f}s"
        ) from None
    if result.returncode != 0:
        if "does not exist" in result.stderr or "exists on disk, but not in" in result.stderr:
            return None
        raise RuntimeError(
            f"cannot read {path} at {ref} in {source}: {result.stderr.strip()}"
        )
    return result.stdout


def fork_parts_at(source: Path | str, ref: str) -> ForkParts | None:
    """The part figures at ``ref`` of a fork checkout, or None for none.

    None means only one thing: this commit has no DFU recovery sources at
    all, so there is nothing to compare with. A commit that has them and
    whose figures this cannot find -- the table renamed, moved to a file not
    listed here, or written another way -- raises PartTableUnreadable, so a
    comparison that stopped working is reported and not taken for a commit
    without a table. Git errors other than a missing path raise RuntimeError.
    """
    texts = {path: _show(source, ref, path) for path in SOURCE_FILES}
    if all(texts[path] is None for path in PART_TABLE_FILES):
        return None
    joined = "\n".join(text for text in texts.values() if text is not None)
    parts = parse_part_table(joined)
    if parts is None:
        present = ", ".join(path for path in PART_TABLE_FILES
                            if texts[path] is not None)
        # Reported, not taken for a commit without a table: the two cannot be
        # told apart from the sources, and treating a comparison that stopped
        # working as nothing to compare is how drift would walk past it.
        raise PartTableUnreadable(
            f"no DFU_PARTS table in {present}, which this reads the flash sizes "
            "out of: either this commit is from before the table, or the table "
            "was renamed or moved"
        )
    assumed = parse_define(joined, "DFU_ASSUMED_FLASH_SIZE")
    offset = parse_define(joined, "BOOTLOADER_OFFSET")
    size = parse_define(joined, "BOOTLOADER_SIZE")
    missing = [name for name, value in (
        ("DFU_ASSUMED_FLASH_SIZE", assumed),
        ("BOOTLOADER_OFFSET", offset),
        ("BOOTLOADER_SIZE", size),
    ) if value is None]
    if missing:
        raise PartTableUnreadable(
            f"a DFU part table is there, but {' and '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not, so the read-back "
            "lengths cannot be worked out from it"
        )
    return ForkParts(parts, assumed, offset + size)


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


def check_sources(sources: Sequence) -> PartTableCheck | None:
    """Compare the harness's copy with the fork's table at a run's Ubertooth
    commit (manifest.SourceRevision objects).

    None when the run has no Ubertooth source, which is no run at all.
    """
    ubertooth = next((source for source in sources if source.name == "Ubertooth"), None)
    if ubertooth is None:
        return None
    label = f"Ubertooth {ubertooth.short_commit}"

    try:
        fork = fork_parts_at(ubertooth.source_path, ubertooth.commit)
    except (OSError, RuntimeError) as error:
        return PartTableCheck(
            "unchecked", f"DFU part table not checked: {error}", ubertooth.commit
        )
    if fork is None:
        files = " or ".join(PART_TABLE_FILES)
        return PartTableCheck(
            "skipped",
            f"DFU part table not checked: {label} has no {files}, so there is no "
            "part table to compare with",
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
