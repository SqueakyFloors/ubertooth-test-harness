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
    SIGKILL reaped as signal 9, pid gone, nothing left in its process group

(one line), and writes the capture's size and SHA-256 to ``--record``. The
``verify`` subcommand reads them back and requires the file to be unchanged,
so the evidence the killed tool left is known to have survived the rest of the
case. Both exit 0 when every check holds and 1 otherwise, after printing one
``Killed capture problem: <reason>`` line.

"Reaped" is three separate facts, because a process whose exit status has been
read can still be there, and a process that is still there still owns its open
device: the wait returned, what it returned is -SIGKILL, and signal 0 to that
pid then fails with ESRCH.

Nothing left behind is the fourth. The tool is started in a session of its
own, so it leads a process group that holds it and anything it starts, and
only the tool is signalled; signal 0 to that group afterwards must fail with
ESRCH as well. Checking the group rather than searching the process table by
name is both exact and cheap: it asks about this run's processes only, so
another capture of the same tool on another board is never mistaken for one of
ours, and it needs no `ps`, which not every host lets a test run. What it
cannot see is a process that left the group by starting a session of its own;
the host tools start no processes at all, so there is nothing of theirs to miss.

Imports only the standard library, so it runs from any working directory.
"""

from __future__ import annotations

import argparse
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


def digest(path: Path) -> tuple[int, str]:
    """(size, SHA-256) of a file, read in blocks."""
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            sha.update(block)
            size += len(block)
    return size, sha.hexdigest()


def _absent(send) -> bool:
    try:
        send()
    except ProcessLookupError:
        return True
    except PermissionError:
        # Something is there; this process may not signal it.
        return False
    return False


def pid_gone(pid: int) -> bool:
    """Is this pid absent from the process table, a zombie included?

    Signal 0 to a process whose exit status nobody has read yet succeeds, so
    this is false until the child has been waited for as well as killed.
    """
    return _absent(lambda: os.kill(pid, 0))


def group_gone(pgid: int) -> bool:
    """Is there nothing left in this process group?"""
    return _absent(lambda: os.killpg(pgid, 0))


def clear_group(process, pgid: int) -> None:
    """Leave nothing of this run behind, whatever its outcome.

    The whole process group, not just the tool: a child it started would
    otherwise keep the device while the rest of the case ran against it. The
    tool is waited for, so it does not stay in the table as the group's only
    member. Best effort throughout -- this runs on paths that are already
    reporting a problem, and the problem is what the caller says.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=REAP_WAIT_S)
    except (subprocess.TimeoutExpired, OSError):
        pass


def wait_for_data(process, capture: Path, min_bytes: int, max_wait_s: float) -> int:
    """Bytes in ``capture`` once it holds ``min_bytes``.

    Raises RuntimeError if the tool exits first (a capture that ends by itself
    leaves the board idle and tests nothing) or if the data never arrives.
    """
    deadline = time.monotonic() + max_wait_s
    while True:
        written = capture.stat().st_size if capture.exists() else 0
        if written >= min_bytes:
            return written
        if process.poll() is not None:
            raise RuntimeError(
                f"the capture exited {process.returncode} by itself after "
                f"{written} bytes; nothing was killed"
            )
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
    started = time.monotonic()
    # No shell and no wrapper, so process.pid is the tool's own pid and the
    # signal below reaches the process that holds the board. Its own session,
    # which makes it the leader of a process group of its own: the group is
    # its pid, it holds anything the tool starts, and nothing else is in it.
    process = subprocess.Popen(argv, start_new_session=True)
    pgid = process.pid
    try:
        written = wait_for_data(process, capture, min_bytes, max_wait_s)
    except RuntimeError:
        # However this ends, nothing of the tool's may be left streaming into
        # the next step: the case's cleanup needs the board back.
        clear_group(process, pgid)
        raise
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
    if not pid_gone(process.pid):
        raise RuntimeError(f"pid {process.pid} is still in the process table")
    if not group_gone(pgid):
        # Not a race with the kernel tearing the tool down: that is over by
        # the time the wait above returned. Something of the tool's is still
        # there, so take it down before reporting it, or it would hold the
        # device through the rest of the case.
        clear_group(process, pgid)
        raise RuntimeError(
            f"process group {pgid} was not empty: {tool} left a process behind "
            "that may still own the device (it has been killed)"
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
        f"in {ran_s:.1f} s, SIGKILL reaped as signal {signal.SIGKILL}, pid "
        "gone, nothing left in its process group"
    )


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
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"Killed capture problem: {error}")
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
