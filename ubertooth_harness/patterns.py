"""Output patterns shared by catalog tests and transition cases."""

import re


FIRMWARE_PATTERN = r"Firmware version:\s+\S+"
COMPILE_INFO_PATTERN = r"\bubertooth\s+\S+\s+\("
# The build string on the line that says what it is, so a `Firmware build:`
# line that carries no reading cannot be passed by a build string found
# somewhere else in the output.
FIRMWARE_BUILD_PATTERN = rf"(?m)^Firmware build: .*{COMPILE_INFO_PATTERN}"

# The two other lines of the `-V` answer, which libubertooth prints for every
# tool (host/libubertooth/src/ubertooth_version.c): the host tools' own
# version, and the board's build string under its own label. Since fork
# f898fe5 (SqueakyFloors/ubertooth#813) `-V` is the version command and ends
# the command, so this answer is the whole of what a command line carrying it
# prints; the build string used to come out bare, with no label.
HOST_VERSION_LINE = r"(?m)^Host tools version: libubertooth \S+"
# Which libbtbb was loaded, which the host version line cannot say; "unknown"
# where the path could not be found, so the line is always there.
LIBBTBB_PATH_LINE = r"(?m)^libbtbb loaded from: \S"

# `ubertooth-util -v` names the image the build came from when the firmware
# carries an info block ("Firmware version: bluetooth_rxtx git-1c7f06d6
# (API:1.35)"), and leaves it out when it does not ("Firmware version:
# 2020-12-R1 (API:1.07)"). Both forms are accepted, and any image name, where
# the harness only reads the revision off a board.
IMAGE_NAME_PATTERN = r"(?:\S+\s+)?"
FIRMWARE_VERSION_RE = re.compile(
    rf"Firmware version:\s+{IMAGE_NAME_PATTERN}(?P<revision>\S+)\s+\(API:"
)
# The preflight identity check is stricter than the reads above: the run's
# tests need the application image, so a board on another image (btbr, a
# bench image) is a mismatch even when it was built from the same commit.
APPLICATION_IMAGE_PATTERN = r"(?:bluetooth_rxtx\s+)?"
PART_ID_PATTERN = r"Part ID:\s+[0-9A-Fa-f]+"
BOARD_ID_PATTERN = r"Board ID Number:\s+\d+"

# How long to give a `-V`, wherever one is asked. `ubertooth-util -H` only
# printed the host's own lines, but `-V` goes on to read every attached board,
# and a board that does not answer costs a libusb control timeout or two
# before `-V` gives up on it and prints why. The usual 5 s is not enough for
# that on a host with several boards.
HOST_VERSION_TIMEOUT_S = 20.0


def serial_pattern(serial: str) -> str:
    return rf"Serial No:\s+{re.escape(serial.lower())}(?:\s|$)"


# Git abbreviates a commit to as many hex digits as it needs for the hash to
# be unique in that clone, and it never uses fewer than this many.
GIT_SHORT_HASH_MIN = 7

_GIT_HASH_REVISION = re.compile(rf"\Agit-([0-9a-f]{{{GIT_SHORT_HASH_MIN},40}})\Z")


def revision_pattern(expected: str) -> str:
    """Regex matching a revision string that names the same commit as ``expected``.

    The same commit can be stamped with abbreviations of different lengths:
    a board flashed from one clone reports ``git-1c7f06d6`` while the host
    tools built from another report ``git-1c7f06d``. Both name the same
    commit, so the pattern accepts a reported hash when it is a prefix of the
    expected hash, or the expected hash is a prefix of it. Either way the
    shorter one must be at least GIT_SHORT_HASH_MIN digits long.

    A revision that is not a plain git hash (a tag, or a hash carrying the
    ``*`` marker a modified source adds) is matched exactly. The caller
    supplies what must follow, so a ``*`` marker on the reported revision
    still fails to match.
    """
    match = _GIT_HASH_REVISION.match(expected)
    if match is None:
        return re.escape(expected)
    digits = match.group(1)
    # A longer abbreviation of the same commit, then every shorter one.
    alternatives = [digits + "[0-9a-f]*"]
    alternatives += [
        digits[:length]
        for length in range(len(digits) - 1, GIT_SHORT_HASH_MIN - 1, -1)
    ]
    return "git-(?:" + "|".join(alternatives) + ")"


def same_revision(expected: str, reported: str) -> bool:
    """Whether ``reported`` names the same commit as ``expected``.

    The two can be abbreviations of different lengths of one commit, so this
    is the comparison to use instead of ``==`` on revision strings.
    """
    return re.fullmatch(revision_pattern(expected), reported) is not None


def firmware_version_pattern(expected: str) -> str:
    """`ubertooth-util -v` output for the application image built from ``expected``.

    Only the application image the harness tests may be named, so a board on
    another image does not match.
    """
    return (
        rf"Firmware version:\s+{APPLICATION_IMAGE_PATTERN}"
        rf"{revision_pattern(expected)}(?:\s|\()"
    )


def compile_info_pattern(expected: str) -> str:
    """`ubertooth-util -V` output for firmware built from ``expected``.

    The image name the firmware reports comes before the build string
    (`bluetooth_rxtx: ubertooth git-1c7f06d6 (...)`), so the pattern starts
    at the build string itself.
    """
    return rf"\bubertooth\s+{revision_pattern(expected)}\s+\("
