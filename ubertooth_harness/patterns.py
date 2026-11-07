"""Output patterns shared by catalog tests and transition cases."""

import re


FIRMWARE_PATTERN = r"Firmware version:\s+\S+"

# The lines of the `-V` answer, which libubertooth prints for every tool
# (host/libubertooth/src/ubertooth_version.c): the host tools' own version,
# then the firmware version of each board it read. Since fork f898fe5
# (SqueakyFloors/ubertooth#813) `-V` is the version command and ends the
# command, so this answer is the whole of what a command line carrying it
# prints.
#
# Two lines for one board, and nothing else. A third line carried the image's
# build string until fork #1233, which took it out: an image records only its
# version now, and the `Firmware version:` line beside it already names that,
# so the build string said nothing a reader of the answer needed. The harness
# holds the current release to the current answer (#358), so no pattern here
# accepts a line, or a version, that an older fork commit printed.
#
# The libbtbb half of the host line, whatever it carries: a version, and the
# bracket closing at the end of the line. Required wherever the line is
# matched, even where the check is only about the tools' own half, because a
# line missing it is not the line libubertooth prints.
LIBBTBB_BRACKET = r"\(libbtbb[ \t]+[^)\s\n]+[ \t]*\)"
# The whole of that first line, to the end of it: anything else is not the
# line libubertooth prints. Which versions it must carry is
# preflight.host_version_patterns()'s business, where the resolved commit is
# known; a case has only the tool in front of it, and this is the one
# statement of the line's layout, so a case cannot hold it to a different
# strictness from the next case's.
HOST_VERSION_LINE = rf"(?m)^Host tools version: \S+[ \t]+{LIBBTBB_BRACKET}$"
# The label the build string stood under until fork #1233. No commit this
# validation accepts prints one, so its presence is what says an answer is
# not the current one: the two commits between the release names (fork
# ce13614c6) and #1233 report a release and print the string, and nothing
# else tells them from the release under test. Written as a requirement that
# the whole output carries no such line, so a reader of a failed check sees
# what was wanted.
NO_BUILD_LINE = r"\A(?![\s\S]*(?m:^Firmware build:))"
# The whole `-V` answer for one board, as the consecutive lines libubertooth
# prints, in its order: a pattern per line would pass an answer whose lines
# arrived in any order or with others between them. It is not anchored to the
# end of the output, because a tool may put a warning or a note on standard
# error beside it; what may not be there is the build line, which
# NO_BUILD_LINE is required alongside this.
VERSION_ANSWER = (
    # Written exactly as HOST_VERSION_LINE writes it, so the same line is not
    # held to two strictnesses; what this adds is the board's line under it.
    rf"(?m)^Host tools version: \S+[ \t]+{LIBBTBB_BRACKET}\n"
    # Not "read failed" or "unavailable, <reason>", which is what the line
    # carries when the board answered nothing.
    r"Firmware version: (?!read failed$|unavailable, )\S[^\n]*$"
)
# That answer and nothing standing in for the line fork #1233 took out.
VERSION_ANSWER_PATTERNS = (VERSION_ANSWER, NO_BUILD_LINE)

# `ubertooth-util -v` names the image the build came from when the firmware
# carries an info block ("Firmware version: bluetooth_rxtx git-1c7f06d6
# (API:1.35)"), and leaves it out when it does not ("Firmware version:
# 2020-12-R1 (API:1.07)"). Both forms are accepted, and any image name, where
# the harness only reads the revision off a board.
IMAGE_NAME_PATTERN = r"(?:\S+\s+)?"
FIRMWARE_VERSION_RE = re.compile(
    r"Firmware version:\s+(?:(?P<image>\S+)\s+)?(?P<revision>\S+)\s+\(API:"
)
# The image name every test but the ubertooth-btbr one needs on a board, and
# the alternate image that one flashes. A board carrying `btbr` runs none of
# the other tools (host/doc/ubertooth-btbr.md, FIRMWARE), which is why the
# preflight check below names the image it found rather than only saying the
# identity did not match.
APPLICATION_IMAGE = "bluetooth_rxtx"
BTBR_IMAGE = "btbr"
# The receive-only build of the application image, which the release ships as
# `bluetooth_rx_only.*` and which reports the image name below. It is the same
# sources with the transmit half left out, and libubertooth accepts it
# wherever it accepts the application image (ubertooth.c,
# UBERTOOTH_TOOLS_IMAGE_RX_ONLY); the requests it leaves out are the transmit
# ones, the transmit power level among them.
RX_ONLY_IMAGE = "bluetooth_rx"
# The preflight identity check is stricter than the reads above: the run's
# tests need the application image, so a board on another image (btbr, a
# bench image) is a mismatch even when it was built from the same commit.
APPLICATION_IMAGE_PATTERN = rf"(?:{APPLICATION_IMAGE}\s+)?"


def matched_image(match: re.Match | None) -> str | None:
    """The image name a match of FIRMWARE_VERSION_RE names, if it names one.

    None where the line carries no image name, which is firmware older than
    the info block (`Firmware version: 2020-12-R1 (API:1.07)`), and where
    there was no match.
    """
    if match is None:
        return None
    return match.groupdict().get("image")


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
# The longest abbreviation a revision string carries: a sha1 object name in
# full. A repository using sha256 object names has longer commits, and no
# build stamps more than this into a revision.
GIT_HASH_MAX = 40

_GIT_HASH_REVISION = re.compile(
    rf"\Agit-([0-9a-f]{{{GIT_SHORT_HASH_MIN},{GIT_HASH_MAX}}})\Z"
)


def commit_revision(commit: str) -> str:
    """The revision string naming ``commit``, to compare with a reported one.

    A commit longer than a revision string ever carries (a sha256 object
    name) is cut to GIT_HASH_MAX digits, which ``revision_pattern`` then
    compares as a prefix of what the build stamped. Without the cut the
    whole value would be matched literally, and no build would ever report
    it.
    """
    return f"git-{commit.strip().lower()[:GIT_HASH_MAX]}"


def revision_pattern(expected: str) -> str:
    """Regex matching a revision string that names the same commit as ``expected``.

    The same commit can be stamped with abbreviations of different lengths:
    a board flashed from one clone reports ``git-1c7f06d6`` while the host
    tools built from another report ``git-1c7f06d``. Both name the same
    commit, so the pattern accepts a reported hash when it is a prefix of the
    expected hash, or the expected hash is a prefix of it. Either way the
    shorter one must be at least GIT_SHORT_HASH_MIN digits long.

    A revision that is not a plain git hash is matched exactly: a release name
    (``2026-11-DEV1+22``, which is what every build reports since #352), a
    tag, or a hash carrying the ``*`` marker a modified source adds. A release
    name is one string the whole way through -- the tag the build came from
    and how many commits it is past it -- so there is no shorter form of it to
    accept, and a build one commit further on is a different version, which is
    the point of it. The caller supplies what must follow, so a ``*`` marker
    on the reported revision still fails to match.
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


def image_name_pattern(image: str) -> str:
    """`ubertooth-util -v` output for a board carrying ``image``, by name.

    The revision is deliberately left out: a case knows the run's revision
    only while it runs, so it compares that through an observation, and this
    pattern is what says which of the two images answered. The name is
    required, which is what the application-image pattern above cannot do:
    firmware older than the firmware info block reports a bare revision, and
    every image other than `bluetooth_rxtx` is newer than that block.
    """
    return rf"Firmware version:\s+{re.escape(image)}\s+\S+\s+\(API:"


# btle-extcap's sentence for a capture filter it cannot apply (fork ba3edb15,
# SqueakyFloors/ubertooth#806, NO_CAPTURE_FILTER in btle-extcap.py). The
# script prints it twice over: as the answer to Wireshark's filter
# verification call, and as "warning: <sentence> (got <filter>)" on standard
# error when a capture is started with a filter anyway, so a reader sees the
# same sentence either way. Two tests read it (BLE extcap capture and BLE
# extcap in tshark) and so does the fake script the unit tests run, so the
# wording lives here once.
NO_CAPTURE_FILTER = ("this interface does not apply capture filters;"
                     " use a display filter")


def capture_filter_warning(capture_filter: str) -> str:
    """The warning line a capture started with ``capture_filter`` prints."""
    return f"warning: {NO_CAPTURE_FILTER} (got {capture_filter})"


def capture_filter_warning_pattern(capture_filter: str) -> str:
    """``capture_filter_warning`` as a pattern anchored to its own line."""
    return r"(?m)^" + re.escape(capture_filter_warning(capture_filter)) + r"$"
