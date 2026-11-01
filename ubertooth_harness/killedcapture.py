"""Run a capture, SIGKILL the tool itself, and keep the file it left behind.

A transition step runs ``python <this file> kill --capture PATH --record PATH
[--min-bytes N] [--max-wait S] -- <argv>...`` as an ordinary logged command.
It starts <argv> as its own child -- no shell, no `timeout`, nothing else in
between -- waits until the capture file has grown past ``--min-bytes``, sends
that child SIGKILL, and waits for it. So the tool never runs its exit path and
never sends STOP, and the board is left in the mode the tool started. That is
the state a terminal or an SSH session disappearing leaves, which a run ended
with SIGTERM does not reproduce: the tool catches SIGTERM and stops the board.

It prints one summary line, never capture contents:

    Killed capture: ubertooth-dump pid 31337 streamed 139264 bytes in 1.2 s,
    SIGKILL reaped as signal 9; its own output in killed-dump.bin.out

(one line), and writes the capture's size and SHA-256 to ``--record``. What
the tool itself printed goes to a file beside its capture, not to the streams
this process was given: a tool that somehow outlived this process would hold
those open, and the harness waits for them to close before it finishes the
step. The
``verify`` subcommand reads them back and requires the file to be unchanged,
so the evidence the killed tool left is known to have survived the rest of the
case. Both exit 0 when every check holds and 1 otherwise, after printing one
``Killed capture problem: <reason>`` line.

What has to hold before the case goes on is that the wait for the child
returned -SIGKILL: the tool died of the signal where it stood, rather than
ending some other way, and its exit status has been read, so it is gone from
the process table rather than waiting there with its device still open.

That is the whole of what this can prove soundly. Whether the tool left a
child of its own behind cannot be asked afterwards: the tool's pid is also its
process group's id, and once the tool has been reaped that id is free to be
handed to the next process that starts a session of its own, which the harness
does for every step it runs, so a question about the group could be answered
by somebody else's process. The host tools start no processes at all, so there
is nothing of theirs to go looking for; a tool that did would need the process
table read by name, which not every host lets a test do.

The group is still what gets signalled when this run has to be taken down --
on a failure, or when the harness stops the step -- and only while the tool is
unreaped, which is when the group is certainly this run's. The tool runs in a
session of its own so that signalling the group reaches it and anything it
started and nothing else; the cost of that session is that the tool is outside
the group the harness signals, which is why ``_stopped_on_signal()`` below
catches those signals and passes them on.

Imports only the standard library, so it runs from any working directory.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# How long to wait for the capture to produce data before giving up, and how
# often to look. A capture that writes nothing has not shown the board was
# streaming when it was killed, which is the whole point of the run.
MAX_WAIT_S = 20.0
POLL_S = 0.05
# Enough bytes to be past any stdio buffer the tool uses: ubertooth-dump
# flushes each 68-byte record, ubertooth-specan buffers its 3-byte samples,
# and both produce data with nothing on the air (about 2000 records and about
# 16 kB a second respectively), so this arrives in well under a second.
MIN_BYTES = 4096
# How long to wait for the killed child. SIGKILL cannot be caught or blocked,
# so this only covers the kernel tearing the process down, which includes
# releasing its USB device.
REAP_WAIT_S = 10.0
# ... and how long on the signal path, where the harness gives a step a couple
# of seconds to end before it stops being polite. Taking the group down is the
# part that matters there; waiting for the status is not.
SIGNAL_WAIT_S = 0.5


def digest(path: Path) -> tuple[int, str]:
    """(size, SHA-256) of a file, read in blocks."""
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            sha.update(block)
            size += len(block)
    return size, sha.hexdigest()


def clear_group(process, pgid: int, *, wait_s: float = REAP_WAIT_S) -> None:
    """Leave nothing of this run behind, whatever its outcome.

    The whole process group, not just the tool: a child it started would
    otherwise keep the device while the rest of the case ran against it.

    Only while the tool's own exit status has not been read here, which is
    every path that needs this. Until then the tool is a member, so the group
    exists and is this run's. Afterwards the group may have emptied, and an
    empty group's id is free to be handed to the next process that starts a
    session of its own -- which the harness does for every step it runs -- so
    signalling it then could reach somebody else's processes.

    Best effort throughout: this runs on paths that are already reporting a
    problem, and the problem is what the caller reports.
    """
    if process.returncode is None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.wait(timeout=wait_s)
    except (subprocess.TimeoutExpired, OSError):
        pass



def wait_for_data(process, capture: Path, min_bytes: int, max_wait_s: float) -> int:
    """Bytes in ``capture`` once it holds ``min_bytes``.

    Raises RuntimeError if the tool exits first (a capture that ends by itself
    leaves the board idle and tests nothing) or if the data never arrives.
    """
    deadline = time.monotonic() + max_wait_s
    while True:
        # The tool first, so one that wrote its data and then ended is
        # reported as having ended by itself rather than as a kill that
        # found nothing to kill.
        ended = process.poll()
        written = capture.stat().st_size if capture.exists() else 0
        if ended is not None:
            raise RuntimeError(
                f"the capture exited {ended} by itself after {written} "
                "bytes; nothing was killed"
            )
        if written >= min_bytes:
            return written
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"the capture wrote {written} bytes in {max_wait_s:g} s, "
                f"less than the {min_bytes} it has to produce before the kill"
            )
        time.sleep(POLL_S)


def kill(argv, capture: Path, record: Path, min_bytes: int, max_wait_s: float) -> str:
    """Run ``argv``, SIGKILL it once it has produced data, and record what it
    left. Returns the summary line; raises RuntimeError with the reason when a
    check does not hold."""
    tool = Path(argv[0]).name
    output = capture.with_name(capture.name + ".out")
    started = time.monotonic()
    # No shell and no wrapper, so process.pid is the tool's own pid and the
    # signal below reaches the process that holds the board. Its own session,
    # which makes it the leader of a process group of its own: the group is
    # its pid, it holds anything the tool starts, and nothing else is in it.
    # However this ends -- a check that does not hold, a read error, the
    # harness's own deadline, a Ctrl-C -- no process of the tool's may be left
    # running into the next step. The mode it leaves the board in is the
    # case's cleanup to undo, and that always runs; a tool still streaming
    # with nobody waiting for it is not.
    #
    # The handlers go on before the tool is started, so there is no window
    # between the two in which a signal would leave it running.
    #
    # The tool's own output goes to a file beside its capture rather than to
    # the pipes this process was given. A tool that somehow outlived this
    # process would hold those pipes open, and the harness waits for them to
    # close before it finishes the step, so the step would hang with no
    # deadline left to save it.
    running = {}
    with _stopped_on_signal(running):
        running["before_start"]()
        with output.open("wb") as handle:
            process = subprocess.Popen(argv, stdout=handle, stderr=handle,
                                       start_new_session=True)
        # One store, so a signal handler running in the middle of this sees
        # either no capture or a whole one. Two would leave a window where it
        # saw half and failed on the other half.
        running["capture"] = process
        # A signal that arrived between the fork and that store found nothing
        # to take down and said so; now there is.
        running["after_start"]()
        try:
            return _kill(process, process.pid, tool, argv, capture, record,
                         min_bytes, max_wait_s, started, output)
        except RuntimeError as error:
            clear_group(process, process.pid)
            # Where to look for the tool's own words, which are the only clue
            # to a capture that never got going.
            raise RuntimeError(
                f"{error}; what {tool} printed is in {output.name}") from error
        except BaseException:
            clear_group(process, process.pid)
            raise


def _kill(process, pgid, tool, argv, capture, record, min_bytes, max_wait_s,
          started, output) -> str:
    written = wait_for_data(process, capture, min_bytes, max_wait_s)
    os.kill(process.pid, signal.SIGKILL)
    try:
        status = process.wait(timeout=REAP_WAIT_S)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"pid {process.pid} was still not reaped {REAP_WAIT_S:g} s after "
            "SIGKILL"
        ) from error
    ran_s = time.monotonic() - started
    if status != -signal.SIGKILL:
        raise RuntimeError(
            f"pid {process.pid} ended as {status}, not -{signal.SIGKILL} "
            "(SIGKILL); the tool was not killed where it ran"
        )
    size, checksum = digest(capture)
    record.write_text(json.dumps({
        "capture": capture.name,
        "size": size,
        "sha256": checksum,
        "argv": list(argv),
        "signal": signal.SIGKILL,
    }, indent=2) + "\n", encoding="utf-8")
    return (
        f"Killed capture: {tool} pid {process.pid} streamed {written} bytes "
        f"in {ran_s:.1f} s, SIGKILL reaped as signal {signal.SIGKILL}; "
        f"its own output in {output.name}"
    )


@contextlib.contextmanager
def _stopped_on_signal(running):
    """Take the capture down if this process is signalled.

    The tool runs in a session of its own, so it is outside the group the
    harness signals when a step runs out of time or an operator stops a run.
    Those signals are caught here and passed on to it, instead of leaving it
    streaming with nobody waiting for it.

    ``running["capture"]`` is set by the caller, in one store, once the tool
    is started, so the handlers can go on before that and a signal handler can
    never see half of it. A signal in between is remembered rather than acted
    on, and ``running["start"]()`` acts on it as soon as there is a tool to
    take down: handling it there and then would find nothing, and the tool
    would be left streaming with this process gone.
    """
    taken = {}
    arrived = []

    def resignal(number):
        """What the signal would have done, now there is nothing to lose: the
        caller sees the run end the way it asked for. For SIGINT that is
        Python's own handler, which raises KeyboardInterrupt, and main() turns
        that into the usual one-line problem report."""
        signal.signal(number, taken.get(number, signal.SIG_DFL))
        os.kill(os.getpid(), number)

    def before_start():
        """Nothing has been started yet, so a signal that arrived while the
        handlers were going on is acted on here rather than after starting a
        capture only to take it down again."""
        if arrived:
            resignal(arrived[0])

    def after_start():
        for number in arrived:
            stop(number, None)

    running["before_start"], running["after_start"] = before_start, after_start

    def stop(number, _frame):
        capture = running.get("capture")
        if capture is None:
            # Between the fork and the store there is nothing to take down
            # with, so the signal is remembered and after_start() acts on it.
            arrived.append(number)
            return
        # start_new_session() made it the leader of a session and so of a
        # process group of its own, which is its pid.
        clear_group(capture, capture.pid, wait_s=SIGNAL_WAIT_S)
        resignal(number)

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            taken[number] = signal.signal(number, stop)
        except (OSError, ValueError):  # not the main thread, or no such signal
            pass
    try:
        yield
    finally:
        for number, previous in taken.items():
            try:
                signal.signal(number, previous)
            except (OSError, ValueError):
                pass


def verify(capture: Path, record: Path) -> str:
    """Require ``capture`` to be byte for byte what the kill left."""
    saved = json.loads(record.read_text(encoding="utf-8"))
    size, checksum = digest(capture)
    if size != saved["size"] or checksum != saved["sha256"]:
        raise RuntimeError(
            f"{capture.name} is {size} bytes with SHA-256 {checksum[:16]}..., "
            f"not the {saved['size']} bytes and {saved['sha256'][:16]}... the "
            "killed capture left"
        )
    return (
        f"Killed capture evidence: {capture.name} unchanged, {size} bytes, "
        f"SHA-256 {checksum}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="killedcapture.py")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("kill", help="run a capture and SIGKILL it")
    run.add_argument("--capture", type=Path, required=True)
    run.add_argument("--record", type=Path, required=True)
    run.add_argument("--min-bytes", type=int, default=MIN_BYTES)
    run.add_argument("--max-wait", type=float, default=MAX_WAIT_S)
    run.add_argument("rest", nargs=argparse.REMAINDER)
    check = subcommands.add_parser("verify", help="the killed capture is unchanged")
    check.add_argument("--capture", type=Path, required=True)
    check.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "kill":
            command = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
            if not command:
                raise RuntimeError("no command to run after --")
            line = kill(command, args.capture, args.record, args.min_bytes,
                        args.max_wait)
        else:
            line = verify(args.capture, args.record)
    except KeyboardInterrupt:
        # Ctrl-C, or SIGINT from the harness: the capture has been taken down
        # by the handler above, so this is a report rather than a traceback.
        print("Killed capture problem: stopped by SIGINT")
        return 130
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"Killed capture problem: {error}")
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
