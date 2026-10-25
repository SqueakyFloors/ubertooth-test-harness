"""Seed, check, and list the files of the Refused command lines cases.

Run as ``python <this file> COMMAND ...`` from a transition step, so each
action is an ordinary logged command. It imports only the standard library,
so it runs from any working directory, and prints summary lines only:

    python refusal.py probe TOOL
    python refusal.py seed DIR NAME...
    python refusal.py check DIR [--absent NAME...] [--kept NAME...]
    python refusal.py files DIR

``probe`` finds TOOL on PATH and reads the bytes of the libubertooth beside it
(``<bin>/../lib``), without running anything: forks before b6d8ede ignore
UBERTOOTH_TEST_NO_DEVICES, so a tool built from one would open an attached
board. It prints ``No-device switch: yes (<library>)`` or ``No-device switch:
no: <reason>``, and exits 0 either way; the case skips on "no".

``seed`` writes each NAME with its own known contents. ``check`` passes when
every --absent NAME is missing, every --kept NAME still holds its seeded
contents, and the directory holds nothing else (no stray or temporary file).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SWITCH = b"UBERTOOTH_TEST_NO_DEVICES"
# Shared-library names libubertooth installs as (Linux, then macOS).
_LIBRARY_GLOBS = ("libubertooth.so*", "libubertooth*.dylib")


def seed_contents(name: str) -> bytes:
    return f"refused-command-line seed for {name}\n".encode()


def find_library(tool_path: Path) -> Path | None:
    """The libubertooth installed with a tool: <prefix>/lib or lib64."""
    prefix = tool_path.resolve().parent.parent
    for directory in (prefix / "lib", prefix / "lib64"):
        for pattern in _LIBRARY_GLOBS:
            for candidate in sorted(directory.glob(pattern)):
                if candidate.is_file():
                    return candidate.resolve()
    return None


def probe(tool: str, path: str | None = None) -> str:
    found = shutil.which(tool, path=path)
    if found is None:
        return f"no: {tool} is not installed in this run (not on PATH)"
    library = find_library(Path(found))
    if library is None:
        return f"no: no libubertooth beside {found}"
    if SWITCH not in library.read_bytes():
        return (f"no: {library} predates UBERTOOTH_TEST_NO_DEVICES "
                "(fork b6d8ede), so a tool could open a board")
    return f"yes ({library})"


def check(directory: Path, absent, kept) -> str:
    """'' when the directory is as seeded, else the first problem."""
    for name in absent:
        if (directory / name).exists():
            size = (directory / name).stat().st_size
            return f"{name} exists ({size} bytes); a refused command line created it"
    for name in kept:
        path = directory / name
        if not path.is_file():
            return f"{name} is gone"
        if path.read_bytes() != seed_contents(name):
            size = path.stat().st_size
            return (f"{name} changed ({size} bytes, seeded "
                    f"{len(seed_contents(name))}); a refused command line wrote it")
    extra = sorted(entry.name for entry in directory.iterdir()
                   if entry.name not in set(kept))
    if extra:
        return "unexpected file(s) left behind: " + ", ".join(extra)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refusal.py")
    sub = parser.add_subparsers(dest="command", required=True)
    probe_parser = sub.add_parser("probe", help="does the built library know the switch")
    probe_parser.add_argument("tool")
    seed_parser = sub.add_parser("seed", help="write files with known contents")
    seed_parser.add_argument("directory", type=Path)
    seed_parser.add_argument("names", nargs="+")
    check_parser = sub.add_parser("check", help="compare the directory with the seed")
    check_parser.add_argument("directory", type=Path)
    check_parser.add_argument("--absent", nargs="*", default=[])
    check_parser.add_argument("--kept", nargs="*", default=[])
    files_parser = sub.add_parser("files", help="list a directory's files and sizes")
    files_parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)

    if args.command == "probe":
        print(f"No-device switch: {probe(args.tool)}")
        return 0
    if args.command == "seed":
        args.directory.mkdir(parents=True, exist_ok=True)
        for name in args.names:
            (args.directory / name).write_bytes(seed_contents(name))
        print(f"Seeded: {len(args.names)} file(s) in {args.directory}: "
              + ", ".join(args.names))
        return 0
    if args.command == "check":
        problem = check(args.directory, args.absent, args.kept)
        if problem:
            print(f"Refusal check problem: {problem}")
            return 1
        print(f"Refusal check: {len(args.absent)} absent, {len(args.kept)} "
              "unchanged, nothing else in the directory")
        return 0
    entries = sorted(args.directory.iterdir()) if args.directory.is_dir() else []
    for entry in entries:
        size = entry.stat().st_size if entry.is_file() else "dir"
        print(f"{entry.name}: {size}")
    print(f"Refusal files: {len(entries)} in {args.directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
