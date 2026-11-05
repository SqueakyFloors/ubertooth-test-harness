"""Run a tool with chosen descriptors, and judge what it left behind.

The capture destinations a tool can be given include standard output and the
paths that name it, so a case about them has to decide where descriptor 1 and
a descriptor of its own point before the tool starts. A harness step cannot:
it runs an argument vector with the harness's own streams. This module is that
step's command, so each run is still one ordinary logged command.

Run as ``python <this file> COMMAND ...``. It imports only the standard
library, so it runs from any working directory, and prints summary lines only
-- never capture bytes, which is why ``run`` can put a capture on the tool's
standard output and still be read in a run record:

    python stdoutdest.py run [--stdout PATH [--append]] [--fd N=PATH]
                             [--fd N=stdout] -- TOOL ARG...
    python stdoutdest.py seed DIR NAME...
    python stdoutdest.py link DIR [--symlink NAME=TARGET]
                                  [--hardlink NAME=TARGET]
    python stdoutdest.py unlink DIR NAME...
    python stdoutdest.py check DIR [--sha256 NAME=HEX] [--prefix NAME=HEX:LEN]
                                   [--absent NAME] [--kept NAME]
                                   [--symlink NAME=TARGET]
                                   [--hardlink NAME=TARGET] [--also NAME]

``run`` exits with the tool's own status, so a step declares the status it
expects as it does for a tool it runs directly. Without ``--stdout`` the tool
inherits this process's standard output, which is the harness's, so a case
whose capture goes to a descriptor of its own still reads the tool's report
through the usual checks. Standard error is always inherited, so a diagnostic
reaches the run record whatever the capture was given.

``check`` is one statement about the directory: every named file is as it is
said to be, and nothing else is there. ``--sha256`` is a file's whole
contents, which is how the replay input is shown to have come through a
refused command line byte for byte; ``--prefix`` is the first LEN bytes of a
longer file, for the one run that is meant to have appended to its own input.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys

# A descriptor number that may be handed to a tool. 0, 1 and 2 are the
# standard streams, which --stdout and the inherited streams decide.
_LOWEST_FD = 3
_STDOUT = "stdout"


def seed_contents(name: str) -> bytes:
    """Known contents for a file a command under test must not write."""
    return f"stdout-destination seed for {name}\n".encode()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pair(text: str) -> tuple[str, str]:
    name, separator, value = text.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError(f"{text!r} is not NAME=VALUE")
    return name, value


def _fd(text: str) -> tuple[int, str]:
    number, target = _pair(text)
    try:
        value = int(number)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{number!r} is not a descriptor number")
    if value < _LOWEST_FD:
        raise argparse.ArgumentTypeError(
            f"descriptor {value} is a standard stream; use {_LOWEST_FD} or above")
    return value, target


def _size(path: Path) -> str:
    try:
        return f"{path.stat().st_size} bytes"
    except OSError as error:
        return f"unreadable ({error.strerror})"


def run(argv, stdout: Path | None, append: bool, fds) -> tuple[int, list[str]]:
    """Run ``argv`` with ``stdout`` and ``fds`` where they are asked for.

    Returns the tool's exit status and the lines describing where each
    descriptor pointed. The file a descriptor is opened on must not exist
    already (it is the run's own), except for an ``--append`` standard
    output, which is how a run is pointed at a file it is also reading.
    """
    numbers = [number for number, _name in fds]
    if len(set(numbers)) != len(numbers):
        raise ValueError("a descriptor number may be given once, and "
                         + ", ".join(str(number) for number in numbers)
                         + " repeats one")
    opened = []
    duplicated = []
    notes = []
    try:
        # The file descriptors first, so that each takes the number it was
        # asked for before the stdout file is opened: opening that one first
        # could land it on a number a --fd then replaced, which would send
        # the tool's standard output to the wrong file and leave one
        # descriptor to be closed twice.
        for number, name in fds:
            if name == _STDOUT:
                continue
            # os.open rather than open(): the descriptor is handed to the
            # tool by number, so this function owns it and closes it itself.
            # A file object on the same descriptor would close it a second
            # time, which is the one EBADF this can give.
            source = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            if source == number:
                # The open already landed on that number, so there is nothing
                # to duplicate and this is the descriptor to close at the end.
                duplicated.append(number)
            else:
                try:
                    # dup2 leaves the new descriptor inheritable, which is
                    # what pass_fds then keeps. Recorded only once it has
                    # happened, so a failure does not leave the cleanup
                    # closing a number nothing opened.
                    os.dup2(source, number)
                    duplicated.append(number)
                finally:
                    os.close(source)
            notes.append(f"fd {number} -> {Path(name).name}")
        if stdout is None:
            target = None
        else:
            target = open(stdout, "ab" if append else "xb")
            opened.append(target)
            notes.append(f"stdout -> {stdout.name} "
                         f"({'append' if append else 'new'})")
        for number, name in fds:
            if name != _STDOUT:
                continue
            if target is None:
                raise ValueError(
                    f"--fd {number}={_STDOUT} needs --stdout, so there is a "
                    "file to point it at")
            if target.fileno() != number:
                os.dup2(target.fileno(), number)
                duplicated.append(number)
            # Where the stdout file's own open landed on that number there is
            # nothing to duplicate, and the file object is what closes it:
            # closing it here as well is the one EBADF this can give.
            notes.append(f"fd {number} -> the same stream as stdout")
        completed = subprocess.run(
            list(argv), stdout=target, stderr=None, check=False,
            pass_fds=tuple(numbers),
        )
    finally:
        for number in duplicated:
            os.close(number)
        for handle in opened:
            handle.close()
    return completed.returncode, notes


def _problem_for(directory: Path, args) -> str:
    """'' when the directory is as ``args`` says, else the first problem."""
    for name, expected in args.sha256:
        path = directory / name
        if not path.is_file():
            return f"{name} is gone"
        found = _digest(path.read_bytes())
        if found != expected:
            return (f"{name} holds {path.stat().st_size} bytes hashing to "
                    f"{found[:12]}, expected {expected[:12]}")
    for name, expected in args.prefix:
        digest, separator, length = expected.partition(":")
        if not separator or not length.isdigit():
            return (f"--prefix {name}={expected} is not NAME=HEX:LENGTH, so "
                    "there is no length to compare")
        size = int(length)
        path = directory / name
        if not path.is_file():
            return f"{name} is gone"
        data = path.read_bytes()
        if len(data) <= size:
            return (f"{name} holds {len(data)} bytes, so nothing was added to "
                    f"the {size} it started with")
        found = _digest(data[:size])
        if found != digest:
            return (f"{name} no longer starts with the {size} bytes it held "
                    f"(hash {found[:12]}, expected {digest[:12]})")
    for name in args.absent:
        path = directory / name
        if path.exists() or path.is_symlink():
            return f"{name} exists; it was not to be created"
    for name in args.kept:
        path = directory / name
        if not path.is_file():
            return f"{name} is gone"
        if path.read_bytes() != seed_contents(name):
            return (f"{name} changed ({path.stat().st_size} bytes, seeded "
                    f"{len(seed_contents(name))})")
    for name, target in args.symlink:
        path = directory / name
        if not path.is_symlink():
            return f"{name} is no longer a symbolic link"
        if os.readlink(path) != target:
            return f"{name} now points at {os.readlink(path)}, not {target}"
    for name, target in args.hardlink:
        path, other = directory / name, directory / target
        if not path.is_file() or not other.is_file():
            return f"{name} or {target} is gone"
        # Device and inode: an inode number alone is only unique within one
        # filesystem.
        if (path.stat().st_dev, path.stat().st_ino) != (
                other.stat().st_dev, other.stat().st_ino):
            return f"{name} is no longer the same file as {target}"
    named = {name for name, _value in args.sha256}
    named |= {name for name, _value in args.prefix}
    named |= set(args.kept) | set(args.also)
    named |= {name for name, _target in args.symlink}
    named |= {name for name, _target in args.hardlink}
    named |= {target for _name, target in args.hardlink}
    extra = sorted(entry.name for entry in directory.iterdir()
                   if entry.name not in named)
    if extra:
        return "unexpected file(s) left behind: " + ", ".join(extra)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stdoutdest.py")
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="run a tool with chosen descriptors")
    runner.add_argument("--stdout", type=Path, default=None)
    runner.add_argument("--append", action="store_true")
    runner.add_argument("--fd", type=_fd, action="append", default=[],
                        metavar="N=PATH")
    runner.add_argument("tool", nargs=argparse.REMAINDER)

    seeder = sub.add_parser("seed", help="write files with known contents")
    seeder.add_argument("directory", type=Path)
    seeder.add_argument("names", nargs="+")

    unlinker = sub.add_parser("unlink", help="remove names a case created")
    unlinker.add_argument("directory", type=Path)
    unlinker.add_argument("names", nargs="+")

    linker = sub.add_parser("link", help="create links to a file")
    linker.add_argument("directory", type=Path)
    linker.add_argument("--symlink", type=_pair, action="append", default=[],
                        metavar="NAME=TARGET")
    linker.add_argument("--hardlink", type=_pair, action="append", default=[],
                        metavar="NAME=TARGET")

    checker = sub.add_parser("check", help="compare a directory with what is expected")
    checker.add_argument("directory", type=Path)
    checker.add_argument("--sha256", type=_pair, action="append", default=[],
                         metavar="NAME=HEX")
    checker.add_argument("--prefix", type=_pair, action="append", default=[],
                         metavar="NAME=HEX:LEN")
    checker.add_argument("--absent", action="append", default=[])
    checker.add_argument("--kept", action="append", default=[])
    checker.add_argument("--symlink", type=_pair, action="append", default=[],
                         metavar="NAME=TARGET")
    checker.add_argument("--hardlink", type=_pair, action="append", default=[],
                         metavar="NAME=TARGET")
    checker.add_argument("--also", action="append", default=[])
    args = parser.parse_args(argv)

    if args.command == "run":
        # Only the separator argparse left at the front: a "--" later on
        # belongs to the tool, which is how a tool is told that what follows
        # is not an option.
        tool = list(args.tool)
        if tool and tool[0] == "--":
            tool.pop(0)
        if not tool:
            print("Stdout destination problem: no tool to run")
            return 2
        try:
            status, notes = run(tool, args.stdout, args.append, args.fd)
        except (OSError, ValueError) as error:
            print(f"Stdout destination problem: {error}")
            return 2
        where = "; ".join(notes) or "the harness's own streams"
        print(f"Stdout destination: {Path(tool[0]).name} exit {status}; {where}")
        for path in ([args.stdout] if args.stdout else []) + [
                Path(name) for _number, name in args.fd if name != _STDOUT]:
            print(f"{path.name}: {_size(path)}")
        return status
    if args.command == "seed":
        args.directory.mkdir(parents=True, exist_ok=True)
        for name in args.names:
            (args.directory / name).write_bytes(seed_contents(name))
        print(f"Seeded: {len(args.names)} file(s) in {args.directory}: "
              + ", ".join(args.names))
        return 0
    if args.command == "unlink":
        # A case that made a link the run must not open afterwards takes it
        # back here, in its cleanup, so it goes whether the case passed or
        # failed: a link to a standard stream left in the case directory
        # would be read when the run measures the files the case wrote.
        removed = []
        for name in args.names:
            path = args.directory / name
            if path.is_symlink() or path.exists():
                os.unlink(path)
                removed.append(name)
        print(f"Removed: {len(removed)} of {len(args.names)} name(s) in "
              f"{args.directory}" + (": " + ", ".join(removed) if removed else ""))
        return 0
    if args.command == "link":
        for name, target in args.symlink:
            (args.directory / name).symlink_to(target)
        for name, target in args.hardlink:
            os.link(args.directory / target, args.directory / name)
        print(f"Linked: {len(args.symlink)} symbolic, {len(args.hardlink)} hard, "
              f"in {args.directory}")
        return 0
    if not args.directory.is_dir():
        print(f"Stdout destination problem: {args.directory} is not a directory")
        return 1
    problem = _problem_for(args.directory, args)
    if problem:
        print(f"Stdout destination problem: {problem}")
        return 1
    print(f"Stdout destination check: {len(args.sha256)} unchanged, "
          f"{len(args.prefix)} appended to, {len(args.absent)} absent, "
          f"{len(args.kept)} as seeded, nothing else in the directory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
