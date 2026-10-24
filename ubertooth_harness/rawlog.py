"""Command record format used in report.md transcripts.

Every line starts with a timestamp (absolute UTC by default; report.md passes
elapsed time since the run started) and a tag::

    [C] command sent (shell-quoted)
    [O] a line the command wrote to stdout
    [E] a line the command wrote to stderr
    [R] the result: command name, exit code, duration, and any timeout or
        cancellation

A command that printed one line is two lines; the response follows the result::

    2026-10-23T23:49:01.114 [C] ubertooth-util -H
    2026-10-23T23:49:01.155 [R] ubertooth-util exit 0 in 0.040s; [O] libubertooth 1.1 (git-f81348f)

Longer output stamps each line when it arrived, in arrival order, then the
result line. Timestamps use the same clock and format as ``run.jsonl``.
"""

import datetime
from pathlib import Path
import shlex
from typing import Callable

from ubertooth_harness.executor import CommandResult


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
        pending[stream] = pending.get(stream, "") + text
        while "\n" in pending[stream]:
            line, pending[stream] = pending[stream].split("\n", 1)
            lines.append((moment, stream, line.rstrip("\r")))
    for stream, rest in pending.items():
        if rest:
            lines.append((finished, stream, rest.rstrip("\r")))
    return lines


TAGS = {"stdout": "[O]", "stderr": "[E]"}


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
