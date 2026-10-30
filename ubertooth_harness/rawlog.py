"""Command record format used in report.md transcripts.

Every line starts with a timestamp (absolute UTC by default; report.md passes
elapsed time since the run started) and a tag::

    [C] command sent (shell-quoted)
    [O] a line the command wrote to stdout
    [E] a line the command wrote to stderr
    [R] the result: command name, exit code, duration, and any timeout or
        cancellation

A command that printed one line is two lines; the response follows the result::

    2026-10-23T23:49:01.114 [C] ubertooth-util -V
    2026-10-23T23:49:01.155 [R] ubertooth-util exit 0 in 0.040s; [O] Host tools version: libubertooth 1.1 (git-f81348f)

Longer output stamps each line when it arrived, in arrival order, then the
result line. Timestamps use the same clock and format as ``run.jsonl``.
"""

import collections
import dataclasses
import datetime
import json
from pathlib import Path
import re
import shlex
from typing import Callable

from ubertooth_harness.executor import CommandResult
from ubertooth_harness.outputfiles import (
    OUTPUT_DIR,
    STREAMS,
    CommandOutput,
    OutputFile,
    StreamFile,
)


def _stamp(moment: datetime.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    utc = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return utc.isoformat(timespec="milliseconds")


class CommandCapture:
    """Records when each piece of command output arrived."""

    def __init__(self, utc_now: Callable[[], datetime.datetime]):
        self._utc_now = utc_now
        self.started = utc_now()
        self.finished: datetime.datetime | None = None
        self.chunks: list[tuple[datetime.datetime, str, str]] = []

    def on_output(self, stream: str, text: str) -> None:
        self.chunks.append((self._utc_now(), stream, text))

    def finish(self) -> None:
        self.finished = self._utc_now()


def _output_lines(
    result: CommandResult, capture: CommandCapture, finished: datetime.datetime
) -> list[tuple[datetime.datetime, str, str]]:
    chunks = capture.chunks
    if not chunks:
        # The executor did not stream; fall back to the collected output.
        chunks = [
            (finished, name, text)
            for name, text in (("stdout", result.stdout), ("stderr", result.stderr))
            if text
        ]
    lines: list[tuple[datetime.datetime, str, str]] = []
    pending = {"stdout": "", "stderr": ""}
    for moment, stream, text in chunks:
        if stream == NOTE:
            # A harness note between output records ends any partial line.
            for name, rest in pending.items():
                if rest:
                    lines.append((moment, name, rest.rstrip("\r")))
            pending = {"stdout": "", "stderr": ""}
            lines.extend((moment, NOTE, line) for line in text.splitlines())
            continue
        pending[stream] = pending.get(stream, "") + text
        while "\n" in pending[stream]:
            line, pending[stream] = pending[stream].split("\n", 1)
            lines.append((moment, stream, line.rstrip("\r")))
    for stream, rest in pending.items():
        if rest:
            lines.append((finished, stream, rest.rstrip("\r")))
    return lines


# Pseudo stream for harness [M] notes placed among a command's output lines.
NOTE = "note"
TAGS = {"stdout": "[O]", "stderr": "[E]", NOTE: "[M]"}

# Per-command limits on the output records kept in run.jsonl (#49): the first
# RECORD_OUTPUT_EVENTS records or RECORD_OUTPUT_BYTES, and the last
# RECORD_TAIL_EVENTS records or RECORD_TAIL_BYTES. Past the first limits the
# command's full output goes to output/<command id>.log. Bytes are those the
# text takes in run.jsonl (UTF-8 of its JSON string, escapes included).
RECORD_OUTPUT_BYTES = 64 * 1024
RECORD_OUTPUT_EVENTS = 200
RECORD_TAIL_BYTES = 16 * 1024
RECORD_TAIL_EVENTS = 50
RECORD_PIECE_CHARS = 1024
# Distinct line shapes listed in the summary of the output left out.
SUMMARY_SHAPES = 10

_DIGITS = re.compile(r"\d+")


def line_shape(line: str) -> str:
    """A line with every run of digits replaced by N, so repeated progress
    lines ('rx block timestamp 283441 * 100 nanoseconds') count as one."""
    return _DIGITS.sub("N", line)


class _LineSplitter:
    def __init__(self):
        self.pending = {"stdout": "", "stderr": ""}

    def feed(self, stream: str, text: str) -> list[str]:
        self.pending[stream] = self.pending.get(stream, "") + text
        *lines, self.pending[stream] = self.pending[stream].split("\n")
        return [line.rstrip("\r") for line in lines]

    def rest(self):
        for stream, text in self.pending.items():
            if text:
                yield stream, text.rstrip("\r")
        self.pending = {"stdout": "", "stderr": ""}


class CommandOutputFiles:
    """A command's full output: one file per stream, exactly as it arrived.

    ``output/<command id>.stdout.log`` and ``.stderr.log`` hold what the
    command wrote on that stream, with nothing of the harness's own added, so
    a check reading one of them sees what the command printed. The text is as
    the harness decoded it (UTF-8, any byte that is not valid UTF-8 replaced,
    as the transcript has always shown it), and the SHA-256 is the hash of
    the file. Each file is opened on its stream's first chunk, so a command
    that writes nothing to stderr leaves no stderr file.
    """

    def __init__(self, output_dir: Path, name: str):
        self._output_dir = output_dir
        self._base = _output_base(output_dir, name)
        self._open: dict[str, StreamFile] = {}
        self.files: tuple[OutputFile, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """The files opened so far, relative to the run directory."""
        return tuple(self._open[stream].name for stream in STREAMS
                     if stream in self._open)

    def write(self, stream: str, text: str) -> None:
        if stream not in self._open:
            self._open[stream] = StreamFile(
                self._base.with_name(f"{self._base.name}.{stream}.log"),
                stream,
                relative_to=self._output_dir,
            )
        self._open[stream].write(text)

    def close(self) -> tuple[OutputFile, ...]:
        self.files = tuple(
            self._open[stream].close() for stream in STREAMS if stream in self._open
        )
        self._open.clear()
        return self.files


class OmittedOutput:
    """Counts of the output left out of run.jsonl, per stream and line shape."""

    def __init__(self):
        self.chars = {"stdout": 0, "stderr": 0}
        self.lines = {"stdout": 0, "stderr": 0}
        self.shapes: dict[tuple[str, str], int] = {}
        self._lines = _LineSplitter()

    def _count(self, stream: str, line: str) -> None:
        self.lines[stream] += 1
        key = (stream, line_shape(line))
        self.shapes[key] = self.shapes.get(key, 0) + 1

    def add(self, stream: str, text: str) -> None:
        self.chars[stream] = self.chars.get(stream, 0) + len(text)
        for line in self._lines.feed(stream, text):
            self._count(stream, line)

    def summary(self) -> dict:
        for stream, line in self._lines.rest():
            self._count(stream, line)
        common = sorted(self.shapes.items(), key=lambda item: (-item[1], item[0]))
        return {
            "chars": dict(self.chars),
            "lines": dict(self.lines),
            "distinct_shapes": len(self.shapes),
            "common_shapes": [
                {"stream": stream, "shape": shape[:200], "count": count}
                for (stream, shape), count in common[:SUMMARY_SHAPES]
            ],
        }


def record_bytes(text: str) -> int:
    """Bytes ``text`` takes in a run.jsonl record (JSON string, UTF-8)."""
    return len(json.dumps(text).encode("utf-8")) - 2


Chunk = tuple  # (moment: datetime, stream: str, text: str)


class CappedOutput:
    """The output records one command leaves in run.jsonl, within the
    RECORD_* limits, plus its full output in a spill file once past them.

    ``record(stream, text, received)`` emits one ``command_output`` record
    (``received`` is None while the command runs, else the chunk's arrival
    time as a run.jsonl UTC timestamp); ``open_files()`` returns the
    CommandOutputFiles to write the whole output to.
    The first records are emitted as output arrives. Past the head limits
    the newest output is held as a tail; ``finish(note)`` emits
    ``note(details)`` for the part left out, then the tail, so report.md and
    the GUI still see how the command ended. The newest chunk is always kept,
    so a harness marker the executor appends at the end (timeout,
    cancellation, OUTPUT SINK ERROR) reaches run.jsonl.

    A command that stays inside the head limits writes no file, and its
    complete output is in run.jsonl and in CommandResult. Once the files are
    open every chunk goes to them, the held head first, so each stream's file
    holds that stream in full from its first byte.
    """

    def __init__(
        self,
        record: Callable[[str, str, str | None], None],
        open_files: Callable[[], CommandOutputFiles],
    ):
        self._record = record
        self._open_files = open_files
        self._head_events = 0
        self._head_bytes = 0
        self._head: list[Chunk] = []  # copied into the files when they open
        self._spilling = False
        self._log: CommandOutputFiles | None = None
        self.files: tuple[OutputFile, ...] = ()
        self._tail: collections.deque = collections.deque()
        self._tail_bytes = 0
        self._omitted = OmittedOutput()
        # Last character on each stream left out before the tail, to start
        # the tail at a line boundary.
        self._dropped_end = {"stdout": "", "stderr": ""}
        self._dropped_at: datetime.datetime | None = None

    @property
    def spilled(self) -> bool:
        return self._spilling

    def add(self, moment: datetime.datetime, stream: str, text: str) -> None:
        # At most 12 bytes per character in JSON (an escaped surrogate pair),
        # so each record stays well inside the event details limit.
        for start in range(0, len(text), RECORD_PIECE_CHARS):
            self._add(moment, stream, text[start:start + RECORD_PIECE_CHARS])

    def _add(self, moment: datetime.datetime, stream: str, text: str) -> None:
        size = record_bytes(text)
        if not self._spilling and (
            self._head_events < RECORD_OUTPUT_EVENTS
            and self._head_bytes + size <= RECORD_OUTPUT_BYTES
        ):
            self._head_events += 1
            self._head_bytes += size
            self._head.append((moment, stream, text))
            self._record(stream, text, None)
            return
        self._spilling = True
        # The tail first: if the spill file fails, the newest output is still
        # kept for run.jsonl.
        self._tail.append((moment, stream, text, size))
        self._tail_bytes += size
        while len(self._tail) > 1 and (
            len(self._tail) > RECORD_TAIL_EVENTS or self._tail_bytes > RECORD_TAIL_BYTES
        ):
            dropped_at, dropped_stream, dropped, dropped_size = self._tail.popleft()
            self._tail_bytes -= dropped_size
            self._drop(dropped_at, dropped_stream, dropped)
        if self._log is None:
            self._log = self._open_files()
            for _moment, held_stream, held_text in self._head:
                self._log.write(held_stream, held_text)
            self._head.clear()
        self._log.write(stream, text)

    def _drop(self, moment: datetime.datetime, stream: str, text: str) -> None:
        self._dropped_at = moment
        if text:
            self._omitted.add(stream, text)
            self._dropped_end[stream] = text[-1]

    def close(self) -> None:
        if self._log is not None:
            self.files = self._log.close()

    def finish(self, note: Callable[[dict], None]) -> dict:
        """Emit the left-out note and the tail; details for command_finished."""
        if not self._spilling:
            return {}
        tail = []
        for moment, stream, text, _size in self._tail:
            if self._dropped_end.get(stream, "") not in ("", "\n"):
                # Starts inside a line that was left out: begin at the next line.
                cut = text.find("\n") + 1 if "\n" in text else len(text)
                self._drop(moment, stream, text[:cut])
                text = text[cut:]
                if not text:
                    continue
            tail.append((moment, stream, text))
        details = {
            "output_files": [saved.to_dict() for saved in self.files],
            "output_omitted": self._omitted.summary(),
        }
        note_details = dict(details)
        if self._dropped_at is not None:
            note_details["received"] = _stamp(self._dropped_at)  # end of the gap
        note(note_details)
        for moment, stream, text in tail:
            self._record(stream, text, _stamp(moment))
        return details


# Chunk size for output a non-streaming executor returns only at the end.
COLLECTED_CHUNK_CHARS = 4096


def _output_base(output_dir: Path, name: str) -> Path:
    """output/<name> for a command's stream files; a repeated name is numbered."""
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in name)
    base = output_dir / OUTPUT_DIR / safe
    number = 2
    while any(
        base.with_name(f"{base.name}.{stream}.log").exists() for stream in STREAMS
    ):
        base = base.with_name(f"{safe}-{number}")
        number += 1
    return base


def record_output(
    publisher,
    run: Callable[[Callable[[str, str], None]], CommandResult],
    *,
    details: dict,
    output_dir: Path,
    spill_name: str,
) -> tuple[CommandResult, dict]:
    """Run ``run(on_output)``, recording its output in run.jsonl within the
    per-command limits (RECORD_*), and each stream in full in
    ``output/<spill_name>.<stream>.log`` once past them.

    Each ``command_output`` and ``command_output_omitted`` record carries
    ``details`` (the owner and ``command_id``). Returns the result and the
    details to add to ``command_finished`` (``output_files`` and
    ``output_omitted`` when output was left out, else nothing). The result
    carries its ``outputfiles.CommandOutput``, which is how a check reads the
    whole stream rather than the bounded copy in ``result.stdout``.
    """
    streamed = []

    def record(stream: str, text: str, received) -> None:
        output_details = {**details, "stream": stream, "text": text}
        if received is not None:
            # A tail record emitted after the command ended.
            output_details["received"] = received
        publisher.emit("command_output", "running", output_details)

    def note(omitted: dict) -> None:
        publisher.emit("command_output_omitted", "running", {**details, **omitted})

    capped = CappedOutput(record, lambda: CommandOutputFiles(output_dir, spill_name))

    def output(stream: str, text: str) -> None:
        streamed.append(stream)
        capped.add(publisher.utc_now(), stream, text)

    try:
        result = run(output)
        if not streamed:
            # The executor did not stream; record its collected output so
            # the run directory still holds every response.
            for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                for start in range(0, len(text), COLLECTED_CHUNK_CHARS):
                    output(stream, text[start:start + COLLECTED_CHUNK_CHARS])
    finally:
        capped.close()
    finished = capped.finish(note)
    result = dataclasses.replace(result, output=CommandOutput(
        run_dir=output_dir,
        files=capped.files,
        stdout=result.stdout,
        stderr=result.stderr,
    ))
    return result, finished


def result_text(result: CommandResult, *, stopped: bool = False) -> str:
    """'[R] <tool> exit <code> in <seconds>s', plus timeout or cancellation.

    ``stopped`` marks a continuous mode that reached its planned deadline,
    which is the expected end rather than a timeout.
    """
    tool = Path(result.argv[0]).name if result.argv else "command"
    text = f"[R] {tool} exit {result.returncode} in {result.duration_s:.3f}s"
    if result.timed_out:
        text += ", stopped at its planned deadline" if stopped else ", TIMED OUT"
    if result.cancelled:
        text += ", CANCELLED"
    return text


def format_command_record(
    result: CommandResult,
    capture: CommandCapture,
    *,
    stopped: bool = False,
    stamp: Callable[[datetime.datetime], str] | None = None,
) -> str:
    """Render one executed command: the command sent and the response received.

    ``stamp`` formats each line's time; the default is absolute UTC.
    """
    fmt = stamp or _stamp
    finished = capture.finished or capture.started
    summary = result_text(result, stopped=stopped)
    out = [f"{fmt(capture.started)} [C] {shlex.join(result.argv)}"]
    lines = _output_lines(result, capture, finished)
    if not lines:
        out.append(f"{fmt(finished)} {summary}; no output")
    elif len(lines) == 1:
        _moment, stream, text = lines[0]
        out.append(f"{fmt(finished)} {summary}; {TAGS[stream]} {text}")
    else:
        for moment, stream, text in lines:
            out.append(f"{fmt(moment)} {TAGS[stream]} {text}")
        out.append(f"{fmt(finished)} {summary}")
    return "\n".join(out) + "\n"
