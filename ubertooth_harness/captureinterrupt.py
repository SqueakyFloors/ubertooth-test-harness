"""Interrupt a running capture with a real SIGINT, and say what happened.

A transition step runs ``python <this file> interrupt ...`` as an ordinary
logged command. It imports only the standard library, so it runs from any
working directory, and it prints summary lines only: never a byte of the
capture, and only the last few lines of what the tool printed.

    python captureinterrupt.py interrupt --grow FILE --min-bytes N
        [--settle S] [--ready-deadline S] [--exit-deadline S]
        [--expect-status N] [--teardown-says PATTERN] -- TOOL ARG...

Why a helper rather than a shell line. The signal has to reach **the tool**,
not a wrapper and not a process group: ``timeout -s INT`` would send it to
whatever ``timeout`` is watching, and killing a process group cannot show
that one Ctrl-C to the tool itself ends the run. So the tool is started
directly (``subprocess.Popen``, no shell), the signal goes to that process's
own pid exactly once, and the process is then waited for and reaped.

It is started in a process group of its own, for two reasons. It lets this
say whether the run left a process behind -- one the tool had started, which
would still hold the board (``os.killpg(pgid, 0)``, asked after the leader has
been reaped). And it lets this leave nothing running whatever happens: the
group is taken down on the way out, and a SIGTERM, SIGINT or SIGHUP to this
helper -- which is what a canceled run or a step deadline sends -- takes it
down and then re-raises, so a tool with a mode started cannot outlive the step
that started it.

When the signal goes in. The wait is for evidence, not a clock: ``--grow``
must hold at least ``--min-bytes`` bytes, and ``--settle`` seconds must have
passed, so the capture is known to be running and not merely started. A
capture that never gets that far by ``--ready-deadline``, and a tool that
exits on its own before the signal, are failures: every tool this is used on
streams whether or not there is traffic, so neither is something the bench can
decide, and nothing here can pass by skipping.

What it checks, and reports as ``Interrupt problem: ...`` with exit 1:

- the tool exited within ``--exit-deadline`` of the one signal, so one Ctrl-C
  ended it rather than being ignored or needing a second;
- it exited with ``--expect-status`` and was not killed by a signal;
- nothing is left in its process group once it has been reaped;
- the capture file stopped growing: its size is read again after the exit, so
  a mode still streaming into it shows up;
- the streams are where they belong: no ``error:``, ``warning:`` or progress
  line on standard output (the fork's rule, cli-output.md), and, where
  ``--teardown-says`` gives a pattern, that line on standard error, so a
  teardown that went quiet or printed to the wrong stream is caught.

``note:`` is deliberately not in that list. ``ubertooth-btle`` prints
``note: N packet(s) held data that could not be decoded`` on its report
stream, which is standard output for a capture written to a file, and the
fork's cli-output.md says that is where it belongs; forbidding it would fail
a correct run on any bench with LE traffic.

Whatever the outcome, the tool's own standard output and standard error are
written beside the capture (``<grow file>.stdout.log`` and ``.stderr.log``),
so a failed run keeps its evidence.
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
# keeps stdout for what the user asked for (host/doc, DIAGNOSTICS). These are
# stateful._DIAGNOSTIC_LINE's prefixes, less "note: " and plus
# ubertooth-dump's per-record progress line (libubertooth cb_dump_full), which
# is the one these captures produce.
#
# "note: " is left out on purpose. ubertooth-btle prints its undecoded-packet
# note on the report stream, which is standard output when the capture goes to
# a file, and the fork's cli-output.md puts it there deliberately, so a bench
# with LE traffic would fail a correct run.
DIAGNOSTIC = re.compile(
    r"^(?:error: |warning: |Entering |Starting |Stopping ubertooth|"
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

    def __init__(self, argv, grow: Path, out):
        self.argv = list(argv)
        self.grow = grow
        self.out = out
        self.stdout_path = Path(f"{grow}.stdout.log")
        self.stderr_path = Path(f"{grow}.stderr.log")
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

    def problems(self, *, expect_status: int, teardown_says: str) -> list[str]:
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
        if teardown_says:
            # Only where the tool says something unconditionally: of the four
            # the cases run, ubertooth-dump prints a line per record and
            # ubertooth-specan prints "Ubertooth stopped", while
            # ubertooth-btle and ubertooth-rx print on their error and note
            # paths only, so a clean run of those two says nothing at all.
            text = ""
            if self.stderr_path.exists():
                text = self.stderr_path.read_text(encoding="utf-8",
                                                  errors="replace")
            if re.search(teardown_says, text) is None:
                found.append(f"nothing matching {teardown_says!r} is on "
                             "standard error, where this tool's progress and "
                             "its teardown belong")
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


def _run_once(argv, grow, options, out) -> tuple[Attempt, str, list[str]]:
    """(the attempt, '' or what went wrong before the signal, the problems).

    The verdict is reached before anything the attempt left behind is taken
    down, so a process it left is a problem and not something this tidied
    away before looking.
    """
    attempt = Attempt(argv, grow, out)
    _LIVE.append(attempt)
    attempt.start()
    reason, problems = "", []
    try:
        reason = attempt.wait_until_growing(
            options.min_bytes, options.settle, options.ready_deadline)
        if reason:
            return attempt, reason, []
        attempt.interrupt()
        reason = attempt.wait_for_exit(options.exit_deadline)
        if reason:
            # Nothing to report about an exit that has not happened.
            return attempt, reason, []
        attempt.report()
        problems = attempt.problems(expect_status=options.expect_status,
                                    teardown_says=options.teardown_says)
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
    if grow.exists():
        # A case always names a file its own run writes. One already there
        # would satisfy the growth wait before the tool had done anything,
        # and the signal would go in before the tool could handle it.
        out(f"Interrupt problem: {grow} is already there, so nothing it holds "
            "would be this run's")
        return 1
    attempt, reason, problems = _run_once(argv, grow, options, out)
    if reason:
        out(f"Interrupt problem: {reason}")
        return 1
    if problems:
        out("Interrupt problem: " + "; ".join(problems))
        return 1
    out(f"Interrupt done: one SIGINT ended the run in {attempt.waited_s:.2f}s "
        f"with status {attempt.process.returncode}, nothing left behind, "
        f"{attempt.final_size} byte(s) captured and no more")
    return 0


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
    run.add_argument("--grow", required=True,
                     help="the capture file the tool writes; the tool's own "
                          "streams are saved beside it")
    run.add_argument("--min-bytes", type=int, default=1)
    run.add_argument("--settle", type=float, default=0.0)
    run.add_argument("--ready-deadline", type=float, default=20.0)
    run.add_argument("--exit-deadline", type=float, default=5.0)
    run.add_argument("--expect-status", type=int, default=0)
    run.add_argument("--teardown-says", default="",
                     help="a pattern this tool prints on standard error "
                          "unconditionally, so a quiet or misdirected "
                          "teardown is caught")
    run.add_argument("tool", nargs=argparse.REMAINDER)
    options = parser.parse_args(raw)
    tool = list(options.tool)
    if tool and tool[0] == "--":
        tool = tool[1:]
    if not tool:
        parser.error("name the tool to run after --")
    _catch_our_own_stop()
    return interrupt(tool, options)


if __name__ == "__main__":
    sys.exit(main())
