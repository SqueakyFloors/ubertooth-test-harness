"""The two judgements the installed-library checks in CI need (#333).

`scripts/ci.sh build` installs the fork's host tree into a prefix and then
reads that install the way something outside the tree would: with
`pkg-config`, with `find_package(ubertooth)`, and by configuring
`host/ubertooth-tools` on its own against it. Two of its steps need more than
a shell comparison, and they live here so a unit test can run them:

    python -m ubertooth_harness.installedlib configure-identity LOG --prefix P
                                             [--expected-pin REF]
    python -m ubertooth_harness.installedlib fork-pin FINDBTBB.cmake
    python -m ubertooth_harness.installedlib consumer-identity OUT --prefix P
                                             --version-line LINE
    python -m ubertooth_harness.installedlib under-prefix LOG --prefix P
                                             --marker TEXT
    python -m ubertooth_harness.installedlib same-path FIRST SECOND [--what T]
    python -m ubertooth_harness.installedlib age-headers DIR [--function NAME]

`consumer-identity` reads what a program built against the install printed:
the version line has to be the one the build's own tools print, which names
the libubertooth revision, so a system library that answered instead is
caught; the libbtbb it loaded has to be the install's; and, the program being
run with the no-devices switch, it has to say that no Ubertooth is attached.

`under-prefix` requires the path a configure log reports after a marker to be
inside the prefix, and `same-path` requires two paths to be one directory.
Both compare resolved paths: an install describes itself in paths worked out
from where its own files are, and on macOS /tmp is a symbolic link to
/private/tmp, so comparing the text would fail for a reason that is not the
install's.

`configure-identity` reads the `-- libbtbb: ` line of a configure log and
requires it to name the library the install holds, the commit that library was
built from, and the commit the tree pins, so a measurement taken from a build
can be read back to a libbtbb. Either commit-naming wording passes: the two
agree ("..., which is the commit this tree is built against (<pin>)") or they
do not ("...; this tree is built against <pin>"). The harness's own libbtbb
pin may differ from the fork's on purpose, and `scripts/ci.sh` warns about
that separately, so this does not judge which commit it is -- only that both
ends are named.

`--expected-pin` is the pin the fork's own `FindBTBB.cmake` sets, which
`fork-pin` reads out of it, so the step compares the configure's pin with the
tree's rather than only checking that it is a commit.

The wordings that name no commit are a failure here. FindBTBB prints one when
it cannot read a commit out of the library, which a libbtbb built from a
source archive or with git's abbreviation set very short would be; CI builds
libbtbb from a clone at a pinned commit, so there it would mean the stamp, or
FindBTBB's reading of it, had stopped working, and the step would otherwise
pass while saying nothing.

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
# The commit the library was built from: git's abbreviation, which is seven
# hex digits or more, bounded so that a longer run of them is not read as a
# commit with something after it. libbtbb appends "*" for a tree with
# uncommitted changes, which FindBTBB reports as its own wording and is still
# a commit named.
_BUILT_FROM = re.compile(r"built from commit (?![0-9a-fA-F]{41})"
                         r"(?P<commit>[0-9a-fA-F]{7,40})(?![0-9a-fA-F])")
# The pin, which is written in full wherever it is named, in whichever case
# the fork writes it (FindBTBB's btbb_pin_is_commit reads either).
_PINNED = re.compile(r"built against \(?(?<![0-9a-fA-F])"
                     r"(?P<pin>[0-9a-fA-F]{40})(?![0-9a-fA-F])\)?")
# Where the library it found is named in that line: the first field, between
# the prefix of the line and the comma before what it says about the commit.
_LIBRARY_IN_LINE = re.compile(r"^-- libbtbb: (?P<library>[^,]+),")
# The pin the fork's own FindBTBB.cmake sets.
_FORK_PIN = re.compile(r'set\(LIBBTBB_PINNED_REF\s+"?(?P<pin>[0-9a-fA-F]{7,40})"?\s*\)')
# What a program built against the install prints: libubertooth's own version
# line, the libbtbb it loaded, and, with the no-devices switch set, that there
# is no board (ubertooth_print_versions).
_LOADED_FROM = "libbtbb loaded from: "
_NO_BOARD = "Firmware version: no Ubertooth attached"
# One of LIBUBERTOOTH_REQUIRED_FUNCTIONS (FindUBERTOOTH.cmake), declared in
# ubertooth_control.h. Any of the four would do; this one is the newest of
# them, so an install from an older commit is missing it for real.
DEFAULT_FUNCTION = "cmd_get_bootloader_info"
# What the renamed function becomes: a name no header of any commit declares.
_ABSENT = "cmd_absent_bootloader_info"
_HEADERS = "ubertooth*.h"


class InstalledLibraryError(RuntimeError):
    """The install does not describe itself the way the checks require."""


def configure_identity(text: str, prefix: str,
                       expected_pin: str | None = None) -> str:
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
    library = _LIBRARY_IN_LINE.match(line)
    if library is None:
        raise InstalledLibraryError(
            f"the configure's libbtbb line names no library: {line}")
    # The path, resolved, and inside the prefix: a sibling directory whose
    # name merely begins with the prefix's text is another install.
    found, root = resolved(library["library"].strip()), resolved(prefix)
    if root not in found.parents:
        raise InstalledLibraryError(
            f"the configure found a libbtbb at {found}, which is not in {root}")
    if not _BUILT_FROM.search(line):
        raise InstalledLibraryError(
            f"the configure does not name the commit the libbtbb it found was "
            f"built from: {line}")
    pinned = _PINNED.search(line)
    if pinned is None:
        raise InstalledLibraryError(
            f"the configure does not name the commit the tree is built "
            f"against: {line}")
    if expected_pin is not None and (
            pinned["pin"].lower() != expected_pin.lower()):
        raise InstalledLibraryError(
            f"the configure says the tree is built against {pinned['pin']}, "
            f"where its own FindBTBB.cmake sets {expected_pin}")
    return line


def fork_pin(text: str) -> str:
    """The pin the fork's FindBTBB.cmake sets (LIBBTBB_PINNED_REF)."""
    found = _FORK_PIN.search(text)
    if found is None:
        raise InstalledLibraryError(
            "FindBTBB.cmake sets no LIBBTBB_PINNED_REF this can read")
    return found["pin"]


def resolved(path: str) -> Path:
    """``path`` with its links and redundant elements reduced."""
    return Path(path).resolve()


def same_path(first: str, second: str, what: str = "the path") -> Path:
    """``first`` and ``second`` as one path, or raise InstalledLibraryError."""
    one, other = resolved(first), resolved(second)
    if one != other:
        raise InstalledLibraryError(f"{what} is {one}, not {other}")
    return one


def under_prefix(text: str, prefix: str, marker: str) -> Path:
    """The path a log reports after ``marker``, which must be in ``prefix``.

    Exactly one line may carry the marker: two mean two answers and none of
    them decides what a build will link.
    """
    found = [line.strip()[len(marker):].strip()
             for line in text.splitlines() if line.strip().startswith(marker)]
    if len(found) != 1:
        raise InstalledLibraryError(
            f"{len(found)} lines carry {marker!r}, expected exactly one")
    path, root = resolved(found[0]), resolved(prefix)
    # Inside the prefix, not the prefix itself: the marked line names a file
    # the install holds.
    if root not in path.parents:
        raise InstalledLibraryError(
            f"{marker.strip()} {path}, which is not in {root}")
    return path


def consumer_identity(text: str, prefix: str, version_line: str) -> str:
    """What a program built against the install printed, or raise.

    ``version_line`` is the line the build's own tools print, which names the
    libubertooth revision: a program that printed another one was built or
    linked against another library.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    if version_line.strip() not in lines:
        raise InstalledLibraryError(
            "the program did not print the version line the build's own tools "
            f"print ({version_line.strip()!r}); it printed "
            + (repr(lines[0]) if lines else "nothing"))
    loaded = [line for line in lines if line.startswith(_LOADED_FROM)]
    if len(loaded) != 1:
        raise InstalledLibraryError(
            f"the program printed {len(loaded)} '{_LOADED_FROM.strip()}' lines, "
            "expected exactly one")
    library = loaded[0][len(_LOADED_FROM):].strip()
    path, root = resolved(library), resolved(prefix)
    if root not in path.parents:
        raise InstalledLibraryError(
            f"the program loaded libbtbb from {path}, which is not in {root}")
    if _NO_BOARD not in lines:
        raise InstalledLibraryError(
            f"the program did not print {_NO_BOARD!r}; it was to be run with "
            "UBERTOOTH_TEST_NO_DEVICES=1, so that the step is about the "
            "install and not about which boards are attached")
    return version_line.strip()


def age_headers(directory: Path, function: str = DEFAULT_FUNCTION) -> list[str]:
    """Rename ``function`` wherever the installed headers declare it.

    Returns the headers edited, and raises when none declares it: a renamed
    or removed function in the fork would otherwise leave the step passing
    for the wrong reason.
    """
    # On identifier boundaries, as FindUBERTOOTH.cmake searches the headers:
    # a plain substring would also rename the functions whose names begin
    # with this one (cmd_get_bootloader_info_quiet sits beside the default),
    # so the aged install would be missing more than the step says.
    name = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(function)}(?![A-Za-z0-9_])")
    edited = []
    for path in sorted(directory.glob(_HEADERS)):
        text = path.read_text()
        replaced, count = name.subn(_ABSENT, text)
        if count:
            path.write_text(replaced)
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
    identity.add_argument("--expected-pin", default=None)
    pin = sub.add_parser("fork-pin", help="the pin FindBTBB.cmake sets")
    pin.add_argument("module", type=Path)
    consumer = sub.add_parser(
        "consumer-identity", help="what a program built against it printed")
    consumer.add_argument("output", type=Path)
    consumer.add_argument("--prefix", required=True)
    consumer.add_argument("--version-line", required=True)
    inside = sub.add_parser(
        "under-prefix", help="a path a log reports is in the prefix")
    inside.add_argument("log", type=Path)
    inside.add_argument("--prefix", required=True)
    inside.add_argument("--marker", required=True)
    paths = sub.add_parser("same-path", help="two paths are one path")
    paths.add_argument("first")
    paths.add_argument("second")
    paths.add_argument("--what", default="the path")
    older = sub.add_parser(
        "age-headers", help="make an install look like an older commit's")
    older.add_argument("directory", type=Path)
    older.add_argument("--function", default=DEFAULT_FUNCTION)
    args = parser.parse_args(argv)

    try:
        if args.command == "configure-identity":
            print(configure_identity(args.log.read_text(errors="replace"),
                                     args.prefix, args.expected_pin))
        elif args.command == "fork-pin":
            print(fork_pin(args.module.read_text(errors="replace")))
        elif args.command == "consumer-identity":
            print(consumer_identity(args.output.read_text(errors="replace"),
                                    args.prefix, args.version_line))
        elif args.command == "under-prefix":
            print(under_prefix(args.log.read_text(errors="replace"),
                               args.prefix, args.marker))
        elif args.command == "same-path":
            print(same_path(args.first, args.second, args.what))
        else:
            print("edited: " + ", ".join(age_headers(args.directory,
                                                     args.function)))
    except (InstalledLibraryError, OSError) as error:
        print(f"installed library: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
