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
unchecked or skipped; report.md shows it, as does the browser live view) and
print anything but a match on stderr.
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

# `static const struct dfu_part DFU_PARTS[] = { ... };`. The table ends at a
# closing brace at the start of a line, so a brace inside it cannot end it.
_TABLE = re.compile(r"DFU_PARTS\[\]\s*=\s*\{(.*?)^\};", re.S | re.M)
# One entry: `{ 0x25011722, "LPC1754", 128 * 1024 },`.
_ENTRY = re.compile(r"\{\s*([^,{}]+?)\s*,\s*\"([^\"]*)\"\s*,\s*([^,{}]+?)\s*,?\s*\}")
# An integer literal with the suffixes C allows on one: `0x4000`, `128u`.
_LITERAL = re.compile(r"(0[xX][0-9a-fA-F]+|\d+)[uUlL]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")


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
    reported by the caller rather than guessed at.
    """
    stripped = text.replace("(", " ").replace(")", " ").strip()
    if not stripped:
        return None
    value = 1
    for factor in stripped.split("*"):
        found = _LITERAL.fullmatch(factor.strip())
        if found is None:
            return None
        value *= int(found.group(1), 0)
    return value


def _without_comments(text: str) -> str:
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", text))


def parse_part_table(text: str) -> tuple[PartEntry, ...] | None:
    """The DFU_PARTS entries in ``text``, or None when it holds no table.

    An entry written in a way this cannot read raises PartTableUnreadable:
    dropping it would hide exactly the difference this is here to find.
    """
    found = _TABLE.search(text)
    if found is None:
        return None
    entries = []
    for part_id, name, flash_size in _ENTRY.findall(_without_comments(found.group(1))):
        identifier, size = parse_number(part_id), parse_number(flash_size)
        if identifier is None or size is None:
            raise PartTableUnreadable(
                f"the DFU_PARTS entry for {name or 'an unnamed part'} is written "
                f"in a way this cannot read ({part_id.strip()}, {flash_size.strip()})"
            )
        entries.append(PartEntry(identifier, name, size))
    if not entries:
        raise PartTableUnreadable("DFU_PARTS holds no entry this can read")
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
    result = subprocess.run(
        ["git", "-C", str(source), "show", f"{ref}:{path}"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode != 0:
        if "does not exist" in result.stderr or "exists on disk, but not in" in result.stderr:
            return None
        raise RuntimeError(
            f"cannot read {path} at {ref} in {source}: {result.stderr.strip()}"
        )
    return result.stdout


def fork_parts_at(source: Path | str, ref: str) -> ForkParts | None:
    """The part figures at ``ref`` of a fork checkout, or None for none.

    None covers a fork commit from before the part table landed, which has no
    figures to compare with. Git errors other than a missing path raise
    RuntimeError; a tree that has the table but not the two figures derived
    from it alongside raises PartTableUnreadable, since that is a tree this
    module no longer knows how to read rather than one without a table.
    """
    texts = [_show(source, ref, path) for path in SOURCE_FILES]
    if not any(text is not None for text in texts):
        return None
    joined = "\n".join(text for text in texts if text is not None)
    parts = parse_part_table(joined)
    if parts is None:
        return None
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

    Each difference says what the fork has, what the harness copy has, and
    what it costs a run, so a reader knows which entry to add without going
    to the fork's source.
    """
    if fork is None:
        return None
    problems = []
    known = set(PART_FLASH_SIZES)
    for flash_size in fork.flash_sizes:
        if flash_size in known:
            continue
        labels = ", ".join(fork.labels_for(flash_size))
        problems.append(
            f"it has {labels} with {describe_flash_size(flash_size)} of flash, which "
            f"PART_FLASH_SIZES does not list, so a correct read-back of "
            f"{upload_length(flash_size)} bytes on such a part would be reported as "
            "a length no part explains"
        )
    for flash_size in sorted(known - set(fork.flash_sizes)):
        problems.append(
            f"PART_FLASH_SIZES lists {describe_flash_size(flash_size)}, which no part "
            "in its table has"
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
        return PartTableCheck(
            "skipped",
            f"DFU part table not checked: {label} has no DFU part table to compare with",
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
