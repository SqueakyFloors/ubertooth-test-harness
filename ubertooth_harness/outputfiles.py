"""Saved output files, and the checks that read them.

A command's stdout and stderr are written to ``output/<command id>.stdout.log``
and ``.stderr.log`` as they arrive, so a mode that runs for hours costs the
harness no memory (rawlog.py opens them). ``CommandResult.stdout`` and
``.stderr`` keep only a bounded window of each stream for the live view, so a
check must not read them directly: it reads the stream through
``CommandOutput``, which streams the file back a block at a time. A command
whose output stayed small enough for run.jsonl has no file, and its
``CommandOutput`` reads the complete in-memory copy instead; either way the
check sees the same bytes it would have seen before.

Every check that reads output records what it read (its pattern, the streams,
and what it found) in ``CommandOutput.checks``. The sequencer writes those
records to run.jsonl, which is what lets ``ubertooth-harness validate`` run
the same checks again against the saved files.
"""

import collections
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Iterable, Iterator, Sequence

OUTPUT_DIR = "output"
STREAMS = ("stdout", "stderr")

# Stream names used in an output file record. "capture" is a file a tool
# wrote itself (a PCAP, a -d dump); "target serial" is a target device's
# shell traffic, which a case writes beside its captures.
CAPTURE_STREAM = "capture"
TARGET_SERIAL_STREAM = "target serial"
# A target's shell traffic is recorded under this suffix, so a file a case
# leaves in its directory is listed as the target's and not as a capture.
TARGET_SERIAL_SUFFIX = ".serial.log"


def stream_for(path: Path) -> str:
    """Which stream a file a case left behind belongs to.

    A target device's shell traffic goes to ``<name>.serial.log``; anything
    else a case writes is a capture (a PCAP, a PCAPNG, a ``-d`` dump).
    """
    if path.name.endswith(TARGET_SERIAL_SUFFIX):
        return TARGET_SERIAL_STREAM
    return CAPTURE_STREAM

# A file is read back in blocks of about this many characters, extended to
# the next line ending, so memory stays flat however long the stream ran.
READ_BLOCK_CHARS = 256 * 1024
# How far a pattern may reach back across a block boundary. Every pattern the
# harness searches for matches inside one line, far below this.
SEARCH_OVERLAP_CHARS = 8 * 1024
# Lines a pattern may cover in search_lines; the longest the harness has is
# the four-line range test result.
LINE_SPAN = 8
# The most one block, or one line, may grow to. A stream with no line endings
# (a binary dump a tool wrote to stdout) has no line to end a block at, so
# without this a single read would take the whole file.
MAX_BLOCK_CHARS = READ_BLOCK_CHARS + SEARCH_OVERLAP_CHARS
# Blocks for hashing, in bytes.
HASH_BLOCK_BYTES = 1024 * 1024

# How a command says on stdout that it cannot answer on this host, with the
# reason the harness reports as the skip.
SKIP_LINE = r"(?m)^SKIP: (.+)$"
# Characters of a match kept in a check's record.
MATCH_CHARS = 200


def _line_count(text: str) -> int:
    """Lines in ``text``: a last line without a line ending still counts."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


@dataclass(frozen=True)
class OutputFile:
    """One saved file: where it is, what wrote it, and what is in it.

    ``path`` is relative to the run directory and always uses forward
    slashes, so a record written on one host reads the same on another.
    """

    path: str
    stream: str
    bytes: int
    lines: int
    sha256: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "stream": self.stream,
            "bytes": self.bytes,
            "lines": self.lines,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value) -> "OutputFile":
        return cls(
            path=str(value.get("path", "")),
            stream=str(value.get("stream", "")),
            bytes=int(value.get("bytes", 0)),
            lines=int(value.get("lines", 0)),
            sha256=str(value.get("sha256", "")),
        )


def measure(
    path: Path, stream: str, *, relative_to: Path, count_lines: bool = True
) -> OutputFile:
    """Read ``path`` once to record its size, line count, and SHA-256.

    ``count_lines`` is False for a file whose lines mean nothing, such as a
    binary capture; its record then holds 0 lines.
    """
    digest = hashlib.sha256()
    size = 0
    lines = 0
    last = b""
    with path.open("rb") as handle:
        while True:
            block = handle.read(HASH_BLOCK_BYTES)
            if not block:
                break
            digest.update(block)
            size += len(block)
            if count_lines:
                lines += block.count(b"\n")
            last = block[-1:]
    if count_lines and size and last != b"\n":
        lines += 1
    return OutputFile(
        path=path.relative_to(relative_to).as_posix(),
        stream=stream,
        bytes=size,
        lines=lines,
        sha256=digest.hexdigest(),
    )


class StreamFile:
    """One stream of one command, written to ``output/`` as it arrives.

    Flushed after every chunk, so a run that is interrupted keeps everything
    received so far. The size, line count, and SHA-256 are computed while
    writing, so no second pass over a multi-gigabyte stream is needed, and
    they describe the file: the command's output as the harness read it,
    decoded as UTF-8 with any byte that is not valid UTF-8 replaced, which is
    the same text the transcript and the checks have always seen.
    """

    def __init__(self, path: Path, stream: str, *, relative_to: Path):
        self.path = path
        self.stream = stream
        self.name = path.relative_to(relative_to).as_posix()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("x", encoding="utf-8", newline="")
        self._digest = hashlib.sha256()
        self._bytes = 0
        self._lines = 0
        self._ends_with_newline = True

    def write(self, text: str) -> None:
        if not text:
            return
        self._handle.write(text)
        self._handle.flush()
        encoded = text.encode("utf-8")
        self._digest.update(encoded)
        self._bytes += len(encoded)
        self._lines += text.count("\n")
        self._ends_with_newline = text.endswith("\n")

    def close(self) -> OutputFile:
        self._handle.close()
        lines = self._lines + (0 if self._ends_with_newline else 1)
        return OutputFile(
            path=self.name,
            stream=self.stream,
            bytes=self._bytes,
            lines=lines,
            sha256=self._digest.hexdigest(),
        )


def _blocks(part) -> Iterator[str]:
    """``part`` (a Path to read, or text already in memory) in blocks of about
    READ_BLOCK_CHARS characters, ending at a line ending where there is one.

    A block is extended to the end of its line, so a ``$`` at the end of a
    block is a real line ending and not the edge of the block. The extension
    is bounded: a stream with no line endings at all, such as a binary dump
    on stdout, would otherwise be read in one piece.
    """
    if isinstance(part, str):
        yield part
        return
    with part.open(encoding="utf-8", errors="replace", newline="") as handle:
        carried = ""
        while True:
            block = carried + handle.read(READ_BLOCK_CHARS)
            carried = ""
            if not block:
                return
            while not block.endswith("\n") and len(block) < MAX_BLOCK_CHARS:
                more = handle.read(READ_BLOCK_CHARS)
                if not more:
                    break
                cut = more.find("\n")
                if cut < 0:
                    block += more
                    continue
                block += more[:cut + 1]
                carried = more[cut + 1:]
                break
            yield block


class LineSource:
    """The lines of one or more saved streams, re-read on every pass.

    Iterating never holds more than one block in memory, so a check over a
    soak's output costs the same as one over a two-line answer. It can be
    iterated more than once, which is what lets several checks read the same
    stream.
    """

    def __init__(self, parts: Sequence):
        self._parts = tuple(parts)

    def __iter__(self) -> Iterator[str]:
        pending = ""
        for part in self._parts:
            for block in _blocks(part):
                pending += block
                *complete, pending = pending.split("\n")
                for line in complete:
                    yield line.rstrip("\r")
                while len(pending) > MAX_BLOCK_CHARS:
                    # A line longer than this is given to the check in pieces,
                    # so a stream with no line endings costs no more memory
                    # than any other.
                    yield pending[:MAX_BLOCK_CHARS]
                    pending = pending[MAX_BLOCK_CHARS:]
        if pending:
            yield pending.rstrip("\r")


def find_lines(source, pattern: str, *, span: int = LINE_SPAN) -> Iterator[re.Match]:
    """Every match of ``pattern`` in a line source, in order.

    For a check that is given lines but reads a pattern covering a few
    consecutive lines, such as the three-line LED status block or the
    four-line range test result. The lines pass through a window of ``span``
    lines, and a match is reported by the window whose first line it starts
    in, so each one is reported once and memory stays flat. A pattern
    covering more than ``span`` lines is not found.
    """
    expression = re.compile(pattern)
    window: collections.deque = collections.deque()
    for line in text_lines(source):
        window.append(line)
        if len(window) <= span:
            continue
        yield from _matches_starting_in_first(expression, window)
        window.popleft()
    while window:
        yield from _matches_starting_in_first(expression, window)
        window.popleft()


def _matches_starting_in_first(expression, window) -> Iterator[re.Match]:
    first = len(window[0])
    for match in expression.finditer("\n".join(window)):
        if match.start() > first:
            return
        yield match


def search_lines(source, pattern: str, *, span: int = LINE_SPAN) -> re.Match | None:
    """The first match of ``pattern`` in a line source, or None."""
    return next(find_lines(source, pattern, span=span), None)


def search_parts(parts: Sequence, pattern: str) -> re.Match | None:
    """Search the parts' concatenation for ``pattern``, a block at a time.

    The search keeps the last SEARCH_OVERLAP_CHARS characters of each block,
    so a match that reaches back no further than that across a block
    boundary is still found. Blocks end at a line ending, so an anchored
    pattern sees the same line endings it would see in the whole text.
    """
    expression = re.compile(pattern)
    buffer = ""
    for part in parts:
        for block in _blocks(part):
            buffer += block
            if len(buffer) < READ_BLOCK_CHARS + SEARCH_OVERLAP_CHARS:
                continue
            match = expression.search(buffer)
            if match is not None:
                return match
            buffer = buffer[-SEARCH_OVERLAP_CHARS:]
    return expression.search(buffer)


@dataclass(frozen=True)
class OutputCheck:
    """One check that read a command's output, recorded so it can run again.

    ``kind`` is how to run it again: ``expected`` and ``forbidden`` search
    for ``pattern``, ``observed`` reads a value out of it, ``skip-line``
    reads the command's own SKIP reason. ``case`` is a check written as code
    (``Step.output_check``) rather than as a pattern; its result is recorded
    but ``validate`` cannot run it again, and says so.
    """

    kind: str
    name: str
    pattern: str = ""
    streams: tuple[str, ...] = STREAMS
    files: tuple[str, ...] = ()
    found: bool | None = None
    # What the pattern matched, so a re-run can tell a pattern that matches
    # something else from one that matches the same text. The ``value`` group
    # when the pattern has one, else the whole match.
    matched: str = ""
    # The value the harness read out of the match, after any mapping of its
    # own. Recorded for the reader; a re-run compares ``matched``.
    value: str = ""
    reason: str = ""

    RERUNNABLE = ("expected", "forbidden", "observed", "skip-line")

    @property
    def rerunnable(self) -> bool:
        """Can ``ubertooth-harness validate`` run this check again?

        A pattern can; a check written as code (``kind`` ``case``) cannot,
        because its record holds its verdict and not its logic.
        """
        return self.kind in self.RERUNNABLE

    def to_dict(self) -> dict:
        value = {
            "kind": self.kind,
            "name": self.name,
            "streams": list(self.streams),
            "files": list(self.files),
            "reason": self.reason,
        }
        if self.pattern:
            value["pattern"] = self.pattern
        if self.found is not None:
            value["found"] = self.found
        if self.matched:
            value["matched"] = self.matched
        if self.value:
            value["value"] = self.value
        return value

    @classmethod
    def from_dict(cls, value) -> "OutputCheck":
        return cls(
            kind=str(value.get("kind", "")),
            name=str(value.get("name", "")),
            pattern=str(value.get("pattern", "")),
            streams=tuple(str(name) for name in value.get("streams", STREAMS)),
            files=tuple(str(name) for name in value.get("files", ())),
            found=value.get("found"),
            matched=str(value.get("matched", "")),
            value=str(value.get("value", "")),
            reason=str(value.get("reason", "")),
        )


@dataclass
class CommandOutput:
    """One command's output, read back from wherever it was saved.

    A command whose output passed the run.jsonl limits has a file per stream
    (``files``); a shorter one has its complete output in ``stdout`` and
    ``stderr``. Checks read through ``search`` and ``lines``, which take
    whichever of the two is there, so a check behaves the same either way and
    ``validate`` can repeat it against the files afterwards.
    """

    run_dir: Path | None = None
    files: tuple[OutputFile, ...] = ()
    stdout: str = ""
    stderr: str = ""
    checks: list[OutputCheck] = field(default_factory=list)
    # The run record fields naming the command these files belong to (its
    # phase, case, and command_id). The sequencer sets it so a check record
    # can be matched with its command.
    owner: dict = field(default_factory=dict)

    def file_for(self, stream: str) -> OutputFile | None:
        for saved in self.files:
            if saved.stream == stream:
                return saved
        return None

    def _parts(self, streams: Sequence[str]):
        parts = []
        for stream in streams:
            saved = self.file_for(stream)
            if saved is not None and self.run_dir is not None:
                parts.append(self.run_dir / saved.path)
            else:
                text = self.stdout if stream == "stdout" else self.stderr
                if text:
                    parts.append(text)
        return parts

    def paths_read(self, streams: Sequence[str]) -> tuple[str, ...]:
        """The saved files a check over ``streams`` read, for its record."""
        names = []
        for stream in streams:
            saved = self.file_for(stream)
            if saved is not None:
                names.append(saved.path)
        return tuple(names)

    def lines(self, stream: str = "stdout") -> LineSource:
        """``stream``'s lines, without their line endings."""
        return LineSource(self._parts((stream,)))

    def search(self, pattern: str, streams: Sequence[str] = STREAMS) -> re.Match | None:
        """The first match of ``pattern`` in the streams' concatenation."""
        return search_parts(self._parts(streams), pattern)

    def record(self, check: OutputCheck) -> OutputCheck:
        """Keep ``check`` in the run record so ``validate`` can repeat it."""
        self.checks.append(check)
        return check

    def note_pattern(
        self,
        kind: str,
        name: str,
        pattern: str,
        match: re.Match | None,
        *,
        streams: Sequence[str] = STREAMS,
        value: str = "",
        reason: str = "",
    ) -> None:
        """Record a pattern check and what it matched."""
        matched = ""
        if match is not None:
            matched = (match.groupdict().get("value") or match[0] or "")
        self.record(OutputCheck(
            kind=kind,
            name=name,
            pattern=pattern,
            streams=tuple(streams),
            files=self.paths_read(streams),
            found=match is not None,
            matched=matched[:MATCH_CHARS],
            value=value,
            reason=reason,
        ))

    def note_case(
        self, name: str, reason: str, *, streams: Sequence[str] = ("stdout",)
    ) -> None:
        """Record a check written as code, with the verdict it reached."""
        self.record(OutputCheck(
            kind="case",
            name=name,
            streams=tuple(streams),
            files=self.paths_read(streams),
            reason=reason,
        ))


def output_of(source) -> CommandOutput:
    """``source`` as a CommandOutput.

    ``source`` is a CommandResult (its output files when it has them, else
    its in-memory copy), a CommandOutput, or a plain string holding output
    already in memory. The string form is what a caller with no run
    directory uses, and what the unit tests pass.
    """
    if isinstance(source, CommandOutput):
        return source
    if isinstance(source, str):
        return CommandOutput(stdout=source)
    saved = getattr(source, "output", None)
    if isinstance(saved, CommandOutput):
        return saved
    return CommandOutput(
        stdout=getattr(source, "stdout", "") or "",
        stderr=getattr(source, "stderr", "") or "",
    )


def saved_files(details) -> tuple[OutputFile, ...]:
    """The output file records in one run.jsonl event's details."""
    listed = details.get("output_files") or details.get("files") or ()
    return tuple(
        OutputFile.from_dict(value) for value in listed if isinstance(value, dict)
    )


class OutsideRun(ValueError):
    """A recorded path does not name a file inside the run directory."""


def confined(run_dir: Path, path: str) -> Path:
    """``path`` inside ``run_dir``, or raise OutsideRun.

    Every recorded path is relative to the run directory and names a file the
    run wrote there, so a path that is absolute, that climbs out with ``..``,
    or that passes through a symbolic link is refused rather than opened. A
    run record is ordinary data, so what it says about a path is checked and
    not trusted.

    No symbolic link is followed at all, not only one that leaves the
    directory: a link inside the run directory would otherwise let a record
    name one file and be served another, which is exactly what reading only
    the recorded files is meant to prevent.
    """
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise OutsideRun(f"{path!r} is not a path inside the run directory")
    root = run_dir.resolve()
    full = root / candidate
    # Every component below the run directory, the last one included. Checked
    # before resolving, because resolving replaces a link with its target.
    component = full
    while component != root:
        if component.is_symlink():
            raise OutsideRun(f"{path!r} passes through a symbolic link")
        parent = component.parent
        if parent == component:
            break
        component = parent
    resolved = full.resolve()
    if resolved != root and root not in resolved.parents:
        raise OutsideRun(f"{path!r} leaves the run directory")
    return resolved


def output_file_list(details) -> str:
    """The paths of the output files a record names, for one line of prose."""
    return ", ".join(saved.path for saved in saved_files(details))


def text_lines(source) -> Iterable[str]:
    """``source``'s lines, for a check that reads output line by line.

    ``source`` is the LineSource a step's ``output_check`` is given, or a
    plain string in a caller or test that has the text already. Either can be
    iterated more than once.
    """
    if isinstance(source, str):
        return source.splitlines()
    return source


@dataclass(frozen=True)
class SavedOutput:
    """One saved file as a run's records describe it, with what read it.

    ``owner`` is the run record fields naming where the file came from (its
    phase, invocation, case, and command), and ``checks`` are the recorded
    checks that read it. This is what the report's Output files table and the
    GUI's file list are built from; both read it from run.jsonl, so neither
    has to open the file to list it.
    """

    file: OutputFile
    owner: dict
    checks: tuple[OutputCheck, ...] = ()

    @property
    def group(self) -> str:
        """Which test or phase the file belongs to, for grouping a list."""
        return str(
            self.owner.get("invocation_id") or self.owner.get("phase") or "run"
        )


# Event types that record a saved file: a command's own streams, and the
# files a case's tools wrote.
FILE_EVENTS = ("command_finished", "output_files")
CHECKS_EVENT = "output_checks"
_OWNER_KEYS = ("phase", "invocation_id", "case_id", "check_id", "step_id",
               "command_id", "argv")


def _owner(details) -> dict:
    return {key: details[key] for key in _OWNER_KEYS if key in details}


def saved_output(events) -> tuple[SavedOutput, ...]:
    """Every saved file a run recorded, in the order the run produced them.

    Each file carries the checks whose record names it. A file named by no
    check carries none: a capture, or a stream no check read.
    """
    # One pass, so ``events`` may be a generator reading run.jsonl a line at
    # a time (output_records). The checks are collected as they come and
    # attached at the end, since a command's checks are recorded after its
    # files.
    by_path: dict[str, list[OutputCheck]] = {}
    found: list[tuple[OutputFile, dict]] = []
    seen: set[OutputFile] = set()
    for event in events:
        kind = event.get("event_type")
        details = event.get("details") or {}
        if kind == CHECKS_EVENT:
            for value in details.get("checks") or []:
                check = OutputCheck.from_dict(value)
                for name in check.files:
                    by_path.setdefault(name, []).append(check)
        elif kind in FILE_EVENTS:
            for file in saved_files(details):
                # The same file is named by command_finished and by the
                # note before it, so a repeat is normal; a repeat that
                # describes the file differently is not, and listing both is
                # the point of a table carrying hashes.
                if file in seen:
                    continue
                seen.add(file)
                found.append((file, _owner(details)))
    return tuple(
        SavedOutput(file, owner, tuple(by_path.get(file.path, ())))
        for file, owner in found
    )
