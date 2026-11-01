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
    SIGKILL reaped as signal 9, nothing left in its process group

(one line), and writes the capture's size and SHA-256 to ``--record``. What
the tool itself printed goes to a file beside its capture, not to the streams
this process was given: a tool that somehow outlived this process would hold
those open, and the harness waits for them to close before it finishes the
step. The
``verify`` subcommand reads them back and requires the file to be unchanged,
so the evidence the killed tool left is known to have survived the rest of the
case. Both exit 0 when every check holds and 1 otherwise, after printing one
``Killed capture problem: <reason>`` line.

Two facts have to hold before the case goes on. The wait for the child
returned -SIGKILL, so the tool died of the signal where it stood rather than
ending some other way. And signal 0 to the tool's process group then fails
with ESRCH, so nothing of the run is left: the tool is started in a session of
its own, which makes it the leader of a group that holds it and anything it
starts, and a process whose exit status nobody has read is still in its group,
so the one check covers a child left behind and a tool that was somehow not
reaped. A process that is still there still owns its open device, which is why
this is checked at all rather than taken for granted.

Checking the group rather than searching the process table by name is exact
and cheap: it asks about this run's processes only, so another capture of the
same tool on another board is never mistaken for one of ours, and it needs no
`ps`, which not every host lets a test run. What it cannot see is a process
that left the group by starting a session of its own; the host tools start no
processes at all, so there is nothing of theirs to miss.

The session of its own has one cost, which ``_stopped_on_signal()`` below
pays: the tool is outside the process group the harness signals when a step
runs out of time or an operator stops a run, so those signals are caught here
and passed on to it.

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
# How long the tool's process group may take to disappear after the tool has
# been reaped. It is normally gone already; this is for a loaded machine.
GROUP_WAIT_S = 5.0


def digest(path: Path) -> tuple[int, str]:
    """(size, SHA-256) of a file, read in blocks."""
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            sha.update(block)
            size += len(block)
    return size, sha.hexdigest()


def group_gone(pgid: int) -> bool:
    """Is there nothing left in this process group at this instant?

    A process whose exit status nobody has read yet is still in its group, so
    this answers for the tool and for anything it started at once.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Something is there; this process may not signal it.
        return False
    return False


def wait_for_group_gone(pgid: int, max_wait_s: float = GROUP_WAIT_S) -> bool:
    """The same question, given a moment to settle.

    A wait that has returned the killed tool's status does not mean the
    kernel has finished taking its process group apart, and on a loaded
    machine that takes long enough to see. A process the tool really left
    behind does not go away while this waits, so giving the answer a moment
    costs nothing and is the difference between a check and a race.
    """
    deadline = time.monotonic() + max_wait_s
    while True:
        if group_gone(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)


def clear_group(process, pgid: int, *, wait_s: float = REAP_WAIT_S) -> None:
    """Leave nothing of this run behind, whatever its outcome.

    The whole process group, not just the tool: a child it started would
    otherwise keep the device while the rest of the case ran against it.

    Nothing is signalled once the group is empty. A group id is only free to
    be used again then, so a group that answers is almost certainly this
    run's: the exception is an id handed to a new session leader in the moment
    between the question and the signal, which is accepted deliberately,
    because a process left holding the board for the rest of the case is the
    worse outcome by a wide margin.

    ``wait_s`` is short on the signal path, where whatever signalled this
    process is not waiting long for it.

    Best effort throughout: this runs on paths that are already reporting a
    problem, and the problem is what the caller reports.
    """
    if not group_gone(pgid):
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
        with output.open("wb") as handle:
            process = subprocess.Popen(argv, stdout=handle, stderr=handle,
                                       start_new_session=True)
        running["process"], running["pgid"] = process, process.pid
        # A signal that arrived while the tool was being started found nothing
        # to take down and said so; now there is.
        running["start"]()
        try:
            return _kill(process, process.pid, tool, argv, capture, record,
                         min_bytes, max_wait_s, started, output)
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
    # The wait above is the reap, and an unreaped process keeps its group, so
    # this one check covers the tool and anything it started: a tool that was
    # somehow not reaped would still be in its group, and so would a child.
    if not wait_for_group_gone(pgid):
        clear_group(process, pgid)
        raise RuntimeError(
            f"process group {pgid} still had something in it {GROUP_WAIT_S:g} s "
            f"after the kill: {tool} left something behind that may still own "
            "the device (it has been killed)"
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
        f"in {ran_s:.1f} s, SIGKILL reaped as signal {signal.SIGKILL}, "
        f"nothing left in its process group; its own output in {output.name}"
    )


@contextlib.contextmanager
def _stopped_on_signal(running):
    """Take the capture down if this process is signalled.

    The tool runs in a session of its own, which is what makes the group check
    mean something, and which also puts it outside the group the harness
    signals when a step runs out of time or the operator stops a run. So the
    signals the harness sends are caught here and passed on to the tool,
    instead of leaving it streaming with nobody waiting for it.

    ``running`` is filled in by the caller once the tool is started, so the
    handlers can go on before that. A signal in between is remembered rather
    than acted on, and ``running["start"]()`` acts on it as soon as there is a
    tool to take down: handling it there and then would find nothing, and the
    tool would be left streaming with this process gone.
    """
    taken = {}
    arrived = []

    def start():
        for number in arrived:
            stop(number, None)

    running["start"] = start

    def stop(number, _frame):
        if "process" not in running:
            arrived.append(number)
            return
        clear_group(running["process"], running["pgid"], wait_s=SIGNAL_WAIT_S)
        # Then what the signal would have done, now there is nothing to lose:
        # the caller sees the run end the way it asked for. For SIGINT that
        # is Python's own handler, which raises KeyboardInterrupt, and main()
        # turns that into the usual one-line problem report.
        signal.signal(number, taken.get(number, signal.SIG_DFL))
        os.kill(os.getpid(), number)

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
