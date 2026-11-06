"""Output patterns shared by catalog tests and transition cases."""

import re


FIRMWARE_PATTERN = r"Firmware version:\s+\S+"
COMPILE_INFO_PATTERN = r"\bubertooth\s+\S+\s+\("

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
# The whole `-V` answer for one board, as the four consecutive lines
# libubertooth prints, in its order: a pattern per line would pass an answer
# whose lines arrived in any order or with others between them. It is not
# anchored to the end of the output, because a tool may put a warning or a
# note on standard error beside it.
VERSION_ANSWER = (
    r"(?m)^Host tools version: libubertooth \S+[^\n]*\n"
    r"libbtbb loaded from: \S[^\n]*\n"
    # Not "read failed" or "unavailable, <reason>", which is what the line
    # carries when the board answered nothing; the build line below is not
    # printed at all then, so this only says so in the pattern as well.
    r"Firmware version: (?!read failed$|unavailable, )\S[^\n]*\n"
    rf"Firmware build: [^\n]*{COMPILE_INFO_PATTERN}"
)

# `ubertooth-util -v` names the image the build came from when the firmware
# carries an info block ("Firmware version: bluetooth_rxtx git-1c7f06d6
# (API:1.35)"), and leaves it out when it does not ("Firmware version:
# 2020-12-R1 (API:1.07)"). Both forms are accepted, and any image name, where
# the harness only reads the revision off a board.
IMAGE_NAME_PATTERN = r"(?:\S+\s+)?"
FIRMWARE_VERSION_RE = re.compile(
    rf"Firmware version:\s+(?:(?P<image>\S+)\s+)?(?P<revision>\S+)\s+\(API:"
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
    the info block (`Firmware version: 2020-12-R1 (API:1.07)`), where there
    was no match, and where the match came from a pattern with no image group
    at all, as the compile-info one has none.
    """
    if match is None:
        return None
    return match.groupdict().get("image")


PART_ID_PATTERN = r"Part ID:\s+[0-9A-Fa-f]+"
BOARD_ID_PATTERN = r"Board ID Number:\s+\d+"

# A board refuses a request its firmware has not got by stalling it, and the
# stall is all the host sees; libubertooth reports it in this one wording
# (ubertooth_report_unsupported_request() in
# host/libubertooth/src/ubertooth_api_version.c, which fork #636 made the only
# wording for it). The `libusb -9` tail is there when the stall came back as
# an error code, which a synchronous control request does.
#
# Two kinds of firmware answer this way: an image that implements less than
# the application one (the alternate `btbr` image refuses the radio settings),
# and firmware older than the request (2020-12-R1 refuses the bootloader read
# and the watchdog record). Neither is a board that changed or a read that
# failed, so a check that can say so records it rather than failing. One thing
# it cannot be told apart from: a wedged board stalls everything sent to it.
FIRMWARE_UNSUPPORTED_PATTERN = (
    r"error: [^\n]*: the board's firmware does not support it "
    r"\(stalled(?:, libusb -9)?\)"
)

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


def compile_info_pattern(expected: str) -> str:
    """`ubertooth-util -V` output for firmware built from ``expected``.

    The image name the firmware reports comes before the build string
    (`bluetooth_rxtx: ubertooth git-1c7f06d6 (...)`), so the pattern starts
    at the build string itself.
    """
    return rf"\bubertooth\s+{revision_pattern(expected)}\s+\("


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
