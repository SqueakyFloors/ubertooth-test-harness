"""The two judgements the installed-library checks in CI need (#333).

`scripts/ci.sh build` installs the fork's host tree into a prefix and then
reads that install the way something outside the tree would: with
`pkg-config`, with `find_package(ubertooth)`, and by configuring
`host/ubertooth-tools` on its own against it. Two of its steps need more than
a shell comparison, and they live here so a unit test can run them:

    python -m ubertooth_harness.installedlib configure-identity LOG --prefix P
    python -m ubertooth_harness.installedlib age-headers DIR [--function NAME]

`configure-identity` reads the `-- libbtbb: ` line of a configure log and
requires it to name the library the install holds, the commit that library was
built from, and the commit the tree pins, so a measurement taken from a build
can be read back to a libbtbb. Either wording passes: the two agree ("...,
which is the commit this tree is built against (<pin>)") or they do not
("...; this tree is built against <pin>"). The harness's own libbtbb pin may
differ from the fork's on purpose, and `scripts/ci.sh` warns about that
separately, so this does not judge which commit it is -- only that both ends
are named.

`age-headers` makes an install look like one from an older commit of the fork:
it renames one of the functions `FindUBERTOOTH.cmake` probes for in the
installed headers, leaving the library and the headers in place. Configuring
the tools against that must fail naming the function, rather than passing and
leaving the compiler to report a page of missing symbols.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

# The line FindBTBB.cmake's btbb_report_pin() prints, as CMake prefixes it.
LIBBTBB_LINE = "-- libbtbb: "
# What the library was built from: an abbreviated commit, or the reason the
# build could not read one.
_BUILT_FROM = re.compile(r"built from commit [0-9a-f]{7,40}|commit unknown \(")
# The pin, which is written in full wherever it is named.
_PINNED = re.compile(r"built against \(?[0-9a-f]{40}\)?")
# One of LIBUBERTOOTH_REQUIRED_FUNCTIONS (FindUBERTOOTH.cmake), declared in
# ubertooth_control.h. Any of the four would do; this one is the newest of
# them, so an install from an older commit is missing it for real.
DEFAULT_FUNCTION = "cmd_get_bootloader_info"
# What the renamed function becomes: a name no header of any commit declares.
_ABSENT = "cmd_absent_bootloader_info"
_HEADERS = "ubertooth*.h"


class InstalledLibraryError(RuntimeError):
    """The install does not describe itself the way the checks require."""


def configure_identity(text: str, prefix: str) -> str:
    """The configure's libbtbb line, or raise InstalledLibraryError.

    ``prefix`` is the install the build wrote, which the line has to name:
    a configure that found another libbtbb altogether describes a build
    nobody asked for.
    """
    lines = [line.strip() for line in text.splitlines()
             if line.startswith(LIBBTBB_LINE)]
    if len(lines) != 1:
        raise InstalledLibraryError(
            f"the configure printed {len(lines)} '{LIBBTBB_LINE.strip()}' lines, "
            "expected exactly one")
    line = lines[0]
    if prefix not in line:
        raise InstalledLibraryError(
            f"the configure found a libbtbb outside {prefix}: {line}")
    if not _BUILT_FROM.search(line):
        raise InstalledLibraryError(
            f"the configure does not say which commit the libbtbb it found was "
            f"built from: {line}")
    if not _PINNED.search(line):
        raise InstalledLibraryError(
            f"the configure does not name the commit the tree is built "
            f"against: {line}")
    return line


def age_headers(directory: Path, function: str = DEFAULT_FUNCTION) -> list[str]:
    """Rename ``function`` wherever the installed headers declare it.

    Returns the headers edited, and raises when none declares it: a renamed
    or removed function in the fork would otherwise leave the step passing
    for the wrong reason.
    """
    edited = []
    for path in sorted(directory.glob(_HEADERS)):
        text = path.read_text()
        if function in text:
            path.write_text(text.replace(function, _ABSENT))
            edited.append(path.name)
    if not edited:
        raise InstalledLibraryError(
            f"no header in {directory} declares {function}, so this cannot "
            "make the install look older; check "
            "LIBUBERTOOTH_REQUIRED_FUNCTIONS in FindUBERTOOTH.cmake")
    return edited


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="installedlib.py")
    sub = parser.add_subparsers(dest="command", required=True)
    identity = sub.add_parser(
        "configure-identity", help="the configure names both libbtbb commits")
    identity.add_argument("log", type=Path)
    identity.add_argument("--prefix", required=True)
    older = sub.add_parser(
        "age-headers", help="make an install look like an older commit's")
    older.add_argument("directory", type=Path)
    older.add_argument("--function", default=DEFAULT_FUNCTION)
    args = parser.parse_args(argv)

    try:
        if args.command == "configure-identity":
            print(configure_identity(args.log.read_text(errors="replace"),
                                     args.prefix))
        else:
            print("edited: " + ", ".join(age_headers(args.directory,
                                                     args.function)))
    except (InstalledLibraryError, OSError) as error:
        print(f"installed library: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
