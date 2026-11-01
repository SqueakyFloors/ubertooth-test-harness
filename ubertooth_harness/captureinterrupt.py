"""Interrupt a running capture with a real SIGINT, and say what happened.

A transition step runs ``python <this file> interrupt ...`` as an ordinary
logged command. It imports only the standard library, so it runs from any
working directory, and it prints summary lines only: never a byte of the
capture, and only the last few lines of what the tool printed.

    python captureinterrupt.py interrupt --when growing --grow FILE
        --min-bytes N [--settle S] [--ready-deadline S]
        [--exit-deadline S] [--expect-status N]
        [--capture-file kept|absent|either] -- TOOL ARG...
    python captureinterrupt.py interrupt --when early --grow FILE
        [--delay S] [--attempts N] [--exit-deadline S]
        [--capture-file ...] -- TOOL ARG...

Why a helper rather than a shell line. The signal has to reach **the tool**,
not a wrapper and not a process group: ``timeout -s INT`` would send it to
whatever ``timeout`` is watching, and killing a process group cannot show
that one Ctrl-C to the tool itself ends the run. So the tool is started
directly (``subprocess.Popen``, no shell), the signal goes to that process's
own pid exactly once, and the process is then waited for and reaped.

It is started in a process group of its own, for two reasons. It lets this
report what the run left behind: a process the tool had started, or the tool
itself escaping the wait (``os.killpg(pgid, 0)``). And it lets this leave
nothing running whatever happens: the group is taken down on the way out of
every attempt, and a SIGTERM or SIGINT to this helper -- which is what a
canceled run or a step deadline sends -- takes it down and then re-raises, so
a tool with a mode started cannot outlive the step that started it.

When the signal goes in:

- ``--when growing`` waits until ``--grow`` has at least ``--min-bytes``
  bytes, and until ``--settle`` seconds have passed, so the capture is known
  to be running and not merely started. A capture that never gets that far by
  ``--ready-deadline``, and a tool that exits on its own before the signal,
  are failures: every tool these cases run streams whether or not there is
  traffic, so neither is something the bench can decide.
- ``--when early`` sends it ``--delay`` seconds in, before the capture can
  have got going, so it lands while the board is being opened or just after
  the mode has started. Which of the two it was does not change what is
  required -- a prompt exit with nothing left behind -- and that is the
  point: a signal that arrives before the mode starts used to be thrown away,
  and a run that threw it away would still be capturing at the deadline.
  Exactly one outcome says nothing either way, a signal that arrived before
  the tool had installed its handler and so killed it; that attempt is taken
  again, up to ``--attempts`` times, and nothing else is ever retried, so a
  real failure cannot be retried away.

What it checks, and reports as ``Interrupt problem: ...`` with exit 1:

- the tool exited within ``--exit-deadline`` of the one signal, so one Ctrl-C
  ended it rather than being ignored or needing a second;
- it exited with ``--expect-status`` and was not killed by a signal;
- nothing is left in its process group once it has been reaped;
- the capture file stopped growing: its size is read again after the exit, so
  a mode still streaming into it shows up;
- ``--capture-file absent``, that the file was never created at all;
- the streams are where they belong: no ``error:``, ``warning:``, ``note:``
  or progress line on standard output, and the teardown's own lines on
  standard error, so a tool that printed nothing anywhere does not pass
  (the fork's rule, cli-output.md).

Whatever the outcome, the tool's own standard output and standard error are
written beside the capture, one pair per attempt
(``<grow file>.<attempt>.stdout.log`` and ``.stderr.log``), and the capture of
an attempt that is taken again is kept as ``<grow file>.<attempt>``, so every
attempt keeps its evidence.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

# How long to wait, after the tool has exited, before reading the capture
# file's size a second time: a mode still streaming would add to it.
SETTLE_AFTER_EXIT_S = 0.5
# Lines of the tool's output kept in the report, per stream.
REPORT_LINES = 6
# How often the readiness and exit waits look again.
POLL_S = 0.02
# How long the group a failed attempt left behind gets to go away.
TEARDOWN_WAIT_S = 5.0
# A line that belongs on standard error, never on standard output: the fork
# keeps stdout for what the user asked for (host/doc, DIAGNOSTICS). The same
# prefixes as stateful._DIAGNOSTIC_LINE, which checks this for the ordinary
# steps, plus ubertooth-dump's per-record progress line, which is the one
# these captures produce (libubertooth cb_dump_full).
DIAGNOSTIC = re.compile(
    r"^(?:error: |warning: |note: |Entering |Starting |Stopping ubertooth|"
    r"Resetting ubertooth|Flashing LEDs|Setting squelch|Ubertooth stopped|"
    r"rx block timestamp )")


# The attempts that have started and not yet been taken down, so a signal
# to this helper can leave nothing running (see _catch_our_own_stop).
_LIVE: list = []


def _tail(path: Path, lines: int = REPORT_LINES) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return [line for line in text.split("\n") if line][-lines:]


def _diagnostics_on_stdout(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return [line for line in text.split("\n") if DIAGNOSTIC.match(line)]


def _size(path: Path) -> int:
    """The file's size, or -1 when it is not there."""
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _captured(size: int) -> str:
    return "no capture file yet" if size < 0 else f"{size} byte(s) captured"


class Attempt:
    """One run of the tool, with one signal sent to it."""

    def __init__(self, argv, grow: Path, out, attempt: int = 1):
        self.argv = list(argv)
        self.grow = grow
        self.out = out
        self.attempt = attempt
        self.stdout_path = Path(f"{grow}.{attempt}.stdout.log")
        self.stderr_path = Path(f"{grow}.{attempt}.stderr.log")
        self.process = None
        self.sent_at = None
        self.waited_s = 0.0
        self.final_size = -1
        self.size_at_signal = -1

    def start(self) -> None:
        self.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self._stdout = self.stdout_path.open("wb")
        self._stderr = self.stderr_path.open("wb")
        self.started = time.monotonic()
        # A process group of its own, so what the run left behind can be seen
        # and then taken down. The signal below still goes to this one
        # process.
        self.process = subprocess.Popen(
            self.argv, stdout=self._stdout, stderr=self._stderr,
            stdin=subprocess.DEVNULL, start_new_session=True)

    def wait_until_growing(self, min_bytes: int, settle_s: float,
                           deadline_s: float) -> str:
        """'' once the capture is under way, else what went wrong."""
        limit = self.started + deadline_s
        while time.monotonic() < limit:
            if self.process.poll() is not None:
                return (f"the tool exited {self.process.returncode} on its own, "
                        f"{_size(self.grow)} bytes in, before it was interrupted")
            if (_size(self.grow) >= min_bytes
                    and time.monotonic() - self.started >= settle_s):
                return ""
            time.sleep(POLL_S)
        return (f"{self.grow.name} held {_size(self.grow)} bytes of the "
                f"{min_bytes} wanted after {deadline_s:g}s, so no capture was "
                "running to interrupt")

    def wait_before_signal(self, delay_s: float) -> None:
        """Let the tool get as far as it can in ``delay_s``."""
        limit = self.started + delay_s
        while time.monotonic() < limit and self.process.poll() is None:
            time.sleep(POLL_S)

    def interrupt(self) -> None:
        """One SIGINT, to the tool's own process and nothing else."""
        self.size_at_signal = _size(self.grow)
        self.sent_at = time.monotonic()
        self.out(f"Interrupt: one SIGINT to pid {self.process.pid} "
                 f"{self.sent_at - self.started:.3f}s in, with "
                 f"{_captured(self.size_at_signal)}")
        os.kill(self.process.pid, signal.SIGINT)

    def wait_for_exit(self, deadline_s: float) -> str:
        """'' when the tool exited and was reaped in time, else why not."""
        try:
            self.process.wait(timeout=deadline_s)
        except subprocess.TimeoutExpired:
            self.waited_s = time.monotonic() - self.sent_at
            return (f"it was still running {deadline_s:g}s after the SIGINT, so "
                    "one Ctrl-C did not end it")
        self.waited_s = time.monotonic() - self.sent_at
        return ""

    def close(self) -> None:
        self._stdout.close()
        self._stderr.close()

    @property
    def killed_by_the_signal(self) -> bool:
        """Did the SIGINT kill the tool, rather than being handled by it?

        That happens when it arrived before the tool installed its handler,
        which says nothing either way about the behavior under test.
        """
        return (self.process is not None and self.sent_at is not None
                and (self.process.returncode or 0) < 0)

    def stop_whatever_is_left(self) -> str:
        """Take down anything still in the tool's group; say what was there.

        Runs for every attempt, including one whose leader has already
        exited: a child the tool started would otherwise keep the board and
        outlive this step.
        """
        if self.process is None:
            return ""
        pgid = self.process.pid
        if not self.group_left():
            return ""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                break
            deadline = time.monotonic() + TEARDOWN_WAIT_S
            while time.monotonic() < deadline and self.group_left():
                time.sleep(POLL_S)
            if not self.group_left():
                break
        # Reap the leader if it was still running, so no zombie is left.
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=TEARDOWN_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
        return f"the process group of pid {pgid} had to be taken down"

    def group_left(self) -> bool:
        """Is anything still in the process group the tool had to itself?

        The leader is reaped by ``wait()``, so a group that still answers
        holds something the tool started.
        """
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def problems(self, *, expect_status: int, capture_file: str) -> list[str]:
        found = []
        status = self.process.returncode
        if status is None:
            found.append("it never exited")
        elif status < 0:
            found.append(f"it was killed by signal {-status} instead of handling "
                         "the SIGINT itself")
        elif status != expect_status:
            found.append(f"it exited {status}, not {expect_status}")
        if self.group_left():
            found.append("a process was left behind in the group it ran in")
        if capture_file == "absent":
            if self.grow.exists():
                found.append(f"{self.grow.name} was created ({_size(self.grow)} "
                             "bytes), by a run that never opened a board")
        elif capture_file == "either" and not self.grow.exists():
            # The signal landed before the files were created, which is one
            # of the two windows this attempt could have hit.
            pass
        else:
            time.sleep(SETTLE_AFTER_EXIT_S)
            after = _size(self.grow)
            if after < 0:
                found.append(f"{self.grow.name} is gone")
            elif after != self.final_size:
                found.append(f"{self.grow.name} grew from {self.final_size} to "
                             f"{after} bytes after the tool exited, so the "
                             "capture was still running")
        diagnostics = _diagnostics_on_stdout(self.stdout_path)
        if diagnostics:
            found.append(f"{diagnostics[0]!r} is on standard output; progress "
                         "and diagnostics go to standard error")
        if _size(self.stderr_path) <= 0:
            # Every one of these tools says what it is doing and what it
            # stopped; a run that said nothing anywhere has not shown that
            # its progress goes to standard error.
            found.append("it printed nothing on standard error, where its "
                         "progress and its teardown belong")
        return found

    def report(self) -> None:
        self.final_size = _size(self.grow)
        self.out(f"Interrupt exit: status {self.process.returncode} "
                 f"{self.waited_s:.2f}s after the SIGINT; "
                 f"{_captured(self.final_size)}, "
                 f"{_captured(self.size_at_signal)} at the signal")
        for stream, path in (("stdout", self.stdout_path),
                             ("stderr", self.stderr_path)):
            tail = _tail(path)
            self.out(f"Interrupt {stream}: {_size(path)} byte(s)"
                     + (f", last {len(tail)} line(s):" if tail else ", empty"))
            for line in tail:
                self.out(f"  {stream}| {line}")


def _run_once(argv, grow, options, out,
              attempt_number) -> tuple[Attempt, str, list[str]]:
    """(the attempt, '' or what went wrong before the signal, the problems).

    The verdict is reached before anything the attempt left behind is taken
    down, so a process it left is a problem and not something this tidied
    away before looking.
    """
    attempt = Attempt(argv, grow, out, attempt_number)
    _LIVE.append(attempt)
    attempt.start()
    reason, problems = "", []
    try:
        if options.when == "growing":
            reason = attempt.wait_until_growing(
                options.min_bytes, options.settle, options.ready_deadline)
        else:
            attempt.wait_before_signal(options.delay)
            if attempt.process.poll() is not None:
                reason = (f"the tool exited {attempt.process.returncode} "
                          f"within {options.delay:g}s, before the SIGINT")
        if reason:
            return attempt, reason, []
        attempt.interrupt()
        reason = attempt.wait_for_exit(options.exit_deadline)
        attempt.report()
        if not reason:
            problems = attempt.problems(expect_status=options.expect_status,
                                        capture_file=options.capture_file)
        return attempt, reason, problems
    finally:
        left = attempt.stop_whatever_is_left()
        if left:
            out(f"Interrupt teardown: {left}")
        attempt.close()
        if attempt in _LIVE:
            _LIVE.remove(attempt)


def interrupt(argv, options, out=print) -> int:
    grow = Path(options.grow)
    for index in range(1, options.attempts + 1):
        delay = options.delay * index
        if options.attempts > 1:
            out(f"Interrupt attempt {index} of {options.attempts}, "
                f"{delay:.3f}s in")
        shown = options
        shown.delay = delay
        attempt, reason, problems = _run_once(argv, grow, shown, out, index)
        if reason:
            out(f"Interrupt problem: {reason}")
            return 1
        # The one outcome that says nothing either way: the signal arrived
        # before the tool had installed its handler, so it was killed by it.
        # Nothing else is retried, so a real failure cannot be retried away.
        if attempt.killed_by_the_signal and index < options.attempts:
            out(f"Interrupt retry: the SIGINT arrived before the handler was "
                f"installed and killed the tool (signal "
                f"{-attempt.process.returncode}); keeping "
                f"{attempt.stdout_path.name} and {attempt.stderr_path.name}")
            # Keep this attempt's capture under its own name.
            if grow.exists() and not grow.is_dir():
                grow.replace(Path(f"{grow}.{index}"))
            continue
        if problems:
            out("Interrupt problem: " + "; ".join(problems))
            return 1
        out(f"Interrupt done: one SIGINT ended the run in {attempt.waited_s:.2f}s "
            f"with status {attempt.process.returncode}, nothing left behind, "
            + ("no capture file created" if not grow.exists()
               else f"{attempt.final_size} byte(s) captured and no more"))
        return 0
    return 1  # pragma: no cover - the loop always returns


def _catch_our_own_stop() -> None:
    """A SIGTERM or SIGINT to this helper leaves no tool running.

    A canceled run or a step deadline sends one, and the tool has a mode
    started on a board, so it must not outlive this process. The handler
    takes down whatever is running and then lets the signal do what it
    would have done.
    """
    def handler(signum, _frame):
        for attempt in list(_LIVE):
            attempt.stop_whatever_is_left()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, handler)


def main(raw=None) -> int:
    parser = argparse.ArgumentParser(prog="captureinterrupt.py")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("interrupt", help="interrupt a capture with one SIGINT")
    run.add_argument("--when", choices=("growing", "early"), required=True)
    run.add_argument("--grow", required=True,
                     help="the capture file the tool writes; the tool's own "
                          "streams are saved beside it")
    run.add_argument("--min-bytes", type=int, default=1)
    run.add_argument("--settle", type=float, default=0.0)
    run.add_argument("--ready-deadline", type=float, default=20.0)
    run.add_argument("--exit-deadline", type=float, default=5.0)
    run.add_argument("--expect-status", type=int, default=0)
    run.add_argument("--capture-file", choices=("kept", "absent", "either"),
                     default="kept",
                     help="kept: it must be there and have stopped growing; "
                          "absent: it must never have been created; either: "
                          "whichever happened, and it must have stopped")
    run.add_argument("--attempts", type=int, default=1)
    run.add_argument("--delay", type=float, default=0.005)
    run.add_argument("tool", nargs=argparse.REMAINDER)
    options = parser.parse_args(raw)
    tool = list(options.tool)
    if tool and tool[0] == "--":
        tool = tool[1:]
    if not tool:
        parser.error("name the tool to run after --")
    if options.attempts < 1:
        parser.error("--attempts must be 1 or more")
    _catch_our_own_stop()
    return interrupt(tool, options)


if __name__ == "__main__":
    sys.exit(main())
