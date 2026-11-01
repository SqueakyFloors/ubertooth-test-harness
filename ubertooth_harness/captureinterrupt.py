"""Interrupt a running capture with a real SIGINT, and say what happened.

A transition step runs ``python <this file> interrupt ...`` as an ordinary
logged command. It imports only the standard library, so it runs from any
working directory, and it prints summary lines only: never a byte of the
capture, and only the last few lines of what the tool printed.

    python captureinterrupt.py interrupt --when growing --grow FILE
        --min-bytes N [--settle S] [--ready-deadline S]
        [--exit-deadline S] [--expect-status N] [--no-capture-file]
        [--attempts N] [--delay S] -- TOOL ARG...

Why a helper rather than a shell line. The signal has to reach **the tool**,
not a wrapper and not a process group: `timeout -s INT` would send it to
whatever `timeout` is watching, and killing a process group cannot show that
one Ctrl-C to the tool itself ends the run. So the tool is started directly
(``subprocess.Popen``, no shell), the signal goes to that process's own pid
exactly once, and the process is then waited for and reaped. It is started in
a session of its own, which is what lets this report whether anything was
left behind: a process the tool had started, or the tool itself escaping the
wait. Nothing else in that session is ever signalled while the run is going.

When the signal goes in:

- ``--when growing`` waits until ``--grow`` has at least ``--min-bytes``
  bytes, and until ``--settle`` seconds have passed, so the capture is known
  to be running and not merely started. A file that never grows that far by
  ``--ready-deadline`` is a ``SKIP:``, not a failure: nothing was interrupted,
  so there is nothing to say.
- ``--when opening`` sends it ``--delay`` seconds in, while the board is
  being opened, and takes ``--attempts`` tries at a spread of delays. Which
  window the signal landed in is not a guess: a run stopped during the open
  ends quietly with exit 0, and one whose open finished says why it found no
  board and exits 1, so an attempt that was too late is told apart from one
  that landed and is simply taken again.

What it checks, and reports as ``Interrupt problem: ...`` with exit 1:

- the tool exited within ``--exit-deadline`` of the one signal, so one Ctrl-C
  ended it rather than being ignored or needing a second;
- it exited with ``--expect-status`` and was not killed by a signal, which is
  what happens when a tool has not installed its handler yet;
- nothing is left in its session once it has been reaped;
- the capture file stopped growing: its size is read again after the exit, so
  a mode still streaming into it shows up;
- with ``--no-capture-file``, that the file was never created at all;
- the streams are where they belong: no ``error:``, ``warning:``, ``note:``
  or progress line on standard output (the fork's rule, cli-output.md).

Whatever the outcome, the tool's own standard output and standard error are
written beside the capture as ``<grow file>.stdout.log`` and
``.stderr.log``, so a failed attempt keeps its evidence.
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
# A line that belongs on standard error, never on standard output: the fork
# keeps stdout for what the user asked for (host/doc, DIAGNOSTICS).
DIAGNOSTIC = re.compile(
    r"^(?:error: |warning: |note: |Entering |Starting |Stopping ubertooth|"
    r"Resetting ubertooth|Flashing LEDs|Setting squelch|Ubertooth stopped)")


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
    try:
        return path.stat().st_size
    except OSError:
        return -1


class Attempt:
    """One run of the tool, with one signal sent to it."""

    def __init__(self, argv, grow: Path, out):
        self.argv = list(argv)
        self.grow = grow
        self.out = out
        self.stdout_path = Path(str(grow) + ".stdout.log")
        self.stderr_path = Path(str(grow) + ".stderr.log")
        self.process = None
        self.sent_at = None
        self.size_at_signal = -1

    def start(self) -> None:
        self.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self._stdout = self.stdout_path.open("wb")
        self._stderr = self.stderr_path.open("wb")
        self.started = time.monotonic()
        # Its own session, so what is left behind can be seen afterwards. The
        # signal below still goes to this one process.
        self.process = subprocess.Popen(
            self.argv, stdout=self._stdout, stderr=self._stderr,
            stdin=subprocess.DEVNULL, start_new_session=True)

    def wait_until_growing(self, min_bytes: int, settle_s: float,
                           deadline_s: float) -> str:
        """'' once the capture is under way, else why it never got there."""
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
                 f"{self.sent_at - self.started:.2f}s in, with "
                 f"{self.size_at_signal} byte(s) captured")
        os.kill(self.process.pid, signal.SIGINT)

    def wait_for_exit(self, deadline_s: float) -> str:
        """'' when the tool exited and was reaped in time, else why not."""
        try:
            self.process.wait(timeout=deadline_s)
        except subprocess.TimeoutExpired:
            return (f"it was still running {deadline_s:g}s after the SIGINT, so "
                    "one Ctrl-C did not end it")
        finally:
            self.waited_s = time.monotonic() - self.sent_at
        return ""

    def close(self) -> None:
        self._stdout.close()
        self._stderr.close()

    def stop_whatever_is_left(self) -> None:
        """Leave no capture running behind us, whatever went wrong."""
        if self.process is None or self.process.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self.process.pid), sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                self.process.wait(timeout=5.0)
                return
            except subprocess.TimeoutExpired:
                continue

    def session_left(self) -> bool:
        """Is anything still in the session the tool had to itself?"""
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def problems(self, *, expect_status: int, no_capture_file: bool) -> list[str]:
        found = []
        status = self.process.returncode
        if status < 0:
            found.append(f"it was killed by signal {-status} instead of handling "
                         "the SIGINT itself")
        elif status != expect_status:
            found.append(f"it exited {status}, not {expect_status}")
        if self.session_left():
            found.append("a process was left behind in the session it ran in")
        if no_capture_file:
            if self.grow.exists():
                found.append(f"{self.grow.name} was created ({_size(self.grow)} "
                             "bytes), by a run that never opened a board")
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
        return found

    def report(self) -> None:
        self.final_size = _size(self.grow)
        self.out(f"Interrupt exit: status {self.process.returncode} "
                 f"{self.waited_s:.2f}s after the SIGINT; "
                 f"{self.final_size} byte(s) captured, "
                 f"{self.size_at_signal} at the signal")
        for stream, path in (("stdout", self.stdout_path),
                             ("stderr", self.stderr_path)):
            tail = _tail(path)
            self.out(f"Interrupt {stream}: {_size(path)} byte(s)"
                     + (f", last {len(tail)} line(s):" if tail else ", empty"))
            for line in tail:
                self.out(f"  {stream}| {line}")


def _run_once(argv, grow, options, out) -> tuple[Attempt, str, bool]:
    """(the attempt, '' or what went wrong, whether that is only a skip)."""
    attempt = Attempt(argv, grow, out)
    attempt.start()
    try:
        if options.when == "growing":
            reason = attempt.wait_until_growing(
                options.min_bytes, options.settle, options.ready_deadline)
            if reason:
                # Nothing was interrupted, so there is nothing to say: the
                # bench, not the tool, decided this.
                return attempt, reason, True
        else:
            attempt.wait_before_signal(options.delay)
            if attempt.process.poll() is not None:
                return attempt, (f"the tool exited {attempt.process.returncode} "
                                 f"within {options.delay:g}s, before the SIGINT"), True
        attempt.interrupt()
        late = attempt.wait_for_exit(options.exit_deadline)
        attempt.report()
        # A tool still running after the deadline is the failure this test is
        # for, never a skip.
        return attempt, late, False
    finally:
        attempt.stop_whatever_is_left()
        attempt.close()


def interrupt(argv, options, out=print) -> int:
    grow = Path(options.grow)
    delays = [options.delay * (index + 1) for index in range(options.attempts)]
    for index, delay in enumerate(delays, start=1):
        options.delay = delay
        if options.attempts > 1:
            out(f"Interrupt attempt {index} of {options.attempts}, "
                f"{delay:.3f}s in")
        for stale in (grow, Path(str(grow) + ".stdout.log"),
                      Path(str(grow) + ".stderr.log")):
            # Each attempt keeps its own evidence only until the next one
            # starts; the attempt that decided the case is the one left.
            if index > 1 and stale.exists() and not stale.is_dir():
                stale.unlink()
        attempt, reason, skippable = _run_once(argv, grow, options, out)
        if reason:
            if skippable and options.when == "opening" and index < options.attempts:
                out(f"Interrupt retry: {reason}")
                continue
            if skippable:
                out(f"SKIP: {reason}")
                return options.skip_status
            out(f"Interrupt problem: {reason}")
            return 1
        problems = attempt.problems(expect_status=options.expect_status,
                                    no_capture_file=options.no_capture_file)
        if problems and options.when == "opening" and index < options.attempts:
            out("Interrupt retry: " + "; ".join(problems))
            continue
        if problems:
            out("Interrupt problem: " + "; ".join(problems))
            return 1
        out(f"Interrupt done: one SIGINT ended the run in {attempt.waited_s:.2f}s "
            f"with status {attempt.process.returncode}, nothing left behind, "
            + ("no capture file created" if options.no_capture_file
               else f"{attempt.final_size} byte(s) captured and no more"))
        return 0
    out(f"SKIP: the SIGINT never landed while the board was being opened, in "
        f"{options.attempts} attempts")
    return options.skip_status


def main(raw=None) -> int:
    parser = argparse.ArgumentParser(prog="captureinterrupt.py")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("interrupt", help="interrupt a capture with one SIGINT")
    run.add_argument("--when", choices=("growing", "opening"), required=True)
    run.add_argument("--grow", required=True,
                     help="the capture file the tool writes")
    run.add_argument("--min-bytes", type=int, default=1)
    run.add_argument("--settle", type=float, default=0.0)
    run.add_argument("--ready-deadline", type=float, default=20.0)
    run.add_argument("--exit-deadline", type=float, default=5.0)
    run.add_argument("--expect-status", type=int, default=0)
    run.add_argument("--no-capture-file", action="store_true")
    run.add_argument("--attempts", type=int, default=1)
    run.add_argument("--delay", type=float, default=0.03)
    run.add_argument("--skip-status", type=int, default=3)
    run.add_argument("tool", nargs=argparse.REMAINDER)
    options = parser.parse_args(raw)
    tool = list(options.tool)
    if tool and tool[0] == "--":
        tool = tool[1:]
    if not tool:
        parser.error("name the tool to run after --")
    if options.attempts < 1:
        parser.error("--attempts must be 1 or more")
    return interrupt(tool, options)


if __name__ == "__main__":
    sys.exit(main())
