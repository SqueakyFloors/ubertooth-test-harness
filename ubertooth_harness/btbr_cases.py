"""`Classic btbr console`: the alternate firmware image, end to end.

`ubertooth-btbr` is the only tool that needs a firmware image other than
`bluetooth_rxtx`, and nothing else runs that image: a board is set up for one
or the other, and switching means reflashing it (host/doc/ubertooth-btbr.md,
FIRMWARE). So this is the one test that puts another image on a board, and
the single case it generates does the whole of it in one flash session:

0. run the tool's own command line for the option values it must refuse
   before it looks for a board at all, which needs neither the board nor the
   image (btbrconsole.py, ``options``);
1. read what the board is carrying, then write the run's `btbr` image;
2. check the board reports that image **by name** at the run's revision
   (`image_name_pattern` plus the revision observation). The
   application-image check every other board gets would turn this board
   down, and a check that read the revision alone would pass a board on
   either image;
3. reset the registered ESP32 Classic peer, which leaves it discoverable and
   connectable, so what an earlier test did to the peer cannot reach this one;
4. drive one console session against that peer (btbrconsole.py): the prompt,
   `?`, the refusals the prompt answers without touching the radio, an
   inquiry that hears the peer by address and by name, a page that connects to
   it and runs the LMP exchange, the chosen packet types transmitted on that
   connection, the page again, the two scan modes, `idle` after each, and the
   split between the capture on standard output and the progress on standard
   error;
5. always, in cleanup, write the application image back, check the board
   reports it, and return the peer to its boot state.

The cleanup is what guarantees the restoration: it runs after a failure or a
cancellation too, and a cleanup failure fails the case, so a board cannot be
left on `btbr` with the rest of the run none the wiser. The test also declares
that it establishes the firmware on its board, which keeps it last on that
board and stops a later test from running on an image this one put there.

Both images come from the run manifest, so they are of the same commit as the
host tools. A firmware build makes the application image; the alternate one
is added when the run asked for it (`--with-btbr`) or when a selected test
declares it needs it, which this test does below, so `--build --with-firmware
--with-python` carries both (build.py). `--artifact "btbr build=PATH"` names
one by hand.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import sys

from ubertooth_harness import btbrconsole, esp32peer
from ubertooth_harness.classic_cases import (
    ACCEPTOR_ROLE,
    _peer,
    _reset_step,
)
from ubertooth_harness.hostneeds import Needs
from ubertooth_harness.patterns import (
    APPLICATION_IMAGE,
    BTBR_IMAGE,
    COMPILE_INFO_PATTERN,
    FIRMWARE_UNSUPPORTED_PATTERN,
    image_name_pattern,
)
from ubertooth_harness.pytools import SKIP_EXIT
from ubertooth_harness.stateful import (
    APPLICATION_IMAGE_LABEL,
    BTBR_IMAGE_LABEL,
    EXPECTED_REVISION_KEY,
    Observation,
    StateCase,
    StateCaseError,
    Step,
    artifact_key,
    require,
)
from ubertooth_harness.transitions import (
    GUARD_BOOTLOADER_PATTERN,
    GUARD_UNSUPPORTED,
    _serial,
    _util,
)

TEST_NAME = "Classic btbr console"
CASE_ID = "btbr-console"
# Manifest artifact labels: the alternate image this case writes, and the
# application image its cleanup writes back.
BTBR_LABEL = BTBR_IMAGE_LABEL
APPLICATION_LABEL = APPLICATION_IMAGE_LABEL

# What the case needs of the host and of the run, so a host or a run that
# cannot do it leaves the case out before anything is flashed rather than
# stopping at case planning: the peers are reached through /dev/serial/by-id,
# which is Linux's; `ubertooth-btbr` is installed only by a --with-python
# build; and the two firmware images are named apart, because a run can carry
# the application image without the alternate one.
NEEDS = Needs(platforms=("linux",), python_tools=True, firmware_build=True,
              btbr_build=True)

_CONSOLE = str(Path(btbrconsole.__file__).resolve())
_PEER_RESET = str(Path(esp32peer.__file__).resolve())
# A write and the board's reset. Four seconds each way on the lab bench, and
# the DFU cases allow the same.
FLASH_TIMEOUT_S = 45.0
# What ubertooth-dfu reports once the image is in flash and read back.
_WRITTEN_REVISION = r"Verified firmware revision: (?P<value>\S+)"
# The revision on a `ubertooth-util -v` line, whether or not it names an
# image; which image it is comes from the pattern beside this observation.
_REVISION = r"Firmware version:\s+(?:\S+\s+)?(?P<value>\S+)\s+\(API:"
# The build string on a `-V` line, which is image-agnostic.
_BUILD_REVISION = r"\bubertooth\s+(?P<value>\S+)\s+\("
VERSION_TIMEOUT_S = 20.0
# The bootloader line, read once under each of the two images. The first read
# is the baseline the second is held to.
_BTBR_BOOTLOADER_KEY = "btbr bootloader"
_RESTORED_BOOTLOADER_KEY = "restored bootloader"
# One control transfer against a board whose report was worked out at
# startup. The same figure bootloader.IDENTITY_TIMEOUT_S gives the preflight
# read; written out here because importing that module from a catalog case
# would close an import cycle, and tests/test_btbr_cases.py keeps the two in
# step.
BOOTLOADER_TIMEOUT_S = 5.0


def _image(label):
    def path(context):
        return require(context, artifact_key(label))
    return path


def _revision(context):
    return require(context, EXPECTED_REVISION_KEY)


def _flash_step(serial: str, step_id: str, name: str, label: str,
                key: str) -> Step:
    return Step(
        step_id, name,
        lambda context: ("ubertooth-dfu", "-U", serial, "-d",
                         _image(label)(context), "-r"),
        timeout_s=FLASH_TIMEOUT_S, mutates=True,
        observe=Observation(key, _WRITTEN_REVISION, _revision),
    )


def _image_step(serial: str, step_id: str, name: str, image: str, key: str) -> Step:
    """The board reports ``image``, named, at the run's revision."""
    return Step(
        step_id, name, _util(serial, "-v"),
        output_patterns=(image_name_pattern(image),),
        observe=Observation(key, _REVISION, _revision),
    )


def _bootloader_step(serial: str, step_id: str, name: str, key: str) -> Step:
    """Record `ubertooth-util -B` while the board runs one of the two images.

    `ubertooth-dfu` writes the application region only, so a board keeps its
    bootloader across both of this case's writes, and both images implement
    the request (the alternate image's own vendor request handler answers
    it). The reading is taken under each: once the run's `btbr` image is on
    the board, and again in the cleanup with the application image back, so
    the run record carries the board's bootloader as each image reports it
    (#327, #329).

    Recorded, not compared. A read that refuses is the reading too, which is
    what a fork commit with the `btbr` image but without `-B` answers, and
    what a board carrying firmware older than the request answers before the
    write; this test establishes its board's image, so it is the one test
    whose board can arrive on such firmware. Asserting the two lines equal
    would mean a cleanup step that compares against a baseline a failed case
    may never have recorded, and a cleanup step that cannot run stops the
    test whether it is skipped or failed (sequencer.py, ``cleanup_unverified``),
    so a case that failed at its own write would be reported as a board left
    unrestored. The equality is worth asserting and needs a way to say
    "compare only if there is something to compare with", which is
    ubertooth-test-harness#366.
    """
    pattern, value = _tolerant_bootloader()
    return Step(
        step_id, name, _util(serial, "-B"), timeout_s=BOOTLOADER_TIMEOUT_S,
        # A board whose firmware has not got the request says so and exits 1.
        expected_returncodes=(0, 1),
        observe=Observation(key, pattern, convert=value),
    )


def _tolerant_bootloader():
    """The `-B` pattern and convert: the line, or "this host could not read it".

    The same two answers the guard reads accept, and the same marker, so one
    reader understands a bootloader reading wherever it was taken
    (transitions.GUARD_UNSUPPORTED).
    """
    combined = (
        rf"(?:{GUARD_BOOTLOADER_PATTERN})"
        rf"|(?P<unsupported>{FIRMWARE_UNSUPPORTED_PATTERN})"
    )

    def value(match):
        if match.groupdict().get("unsupported"):
            return GUARD_UNSUPPORTED
        return match.group("value")

    return combined, value


def console_argv(serial: str, peer, *, comprehensive: bool) -> tuple[str, ...]:
    argv = (
        sys.executable, _CONSOLE, "session",
        "--serial", serial,
        "--peer", peer["address"],
        "--peer-name", peer["name"],
        # The session resets the peer between pages: `idle` tells the peer
        # nothing, so it holds the link the last page made and answers no LMP
        # on the next one (btbrconsole.py, PEER_RESET_ROLE). The driver and the
        # bridge are passed rather than found, so the run record shows both.
        "--peer-reset", _PEER_RESET,
        "--peer-device", peer["device"],
        "--inquiry-seconds", f"{btbrconsole.DEFAULT_INQUIRY_S:g}",
        "--page-seconds", f"{btbrconsole.DEFAULT_PAGE_S:g}",
        "--hold-seconds", f"{btbrconsole.DEFAULT_HOLD_S:g}",
    )
    if comprehensive:
        argv += ("--comprehensive", "--no-answer-seconds",
                 f"{btbrconsole.DEFAULT_NO_ANSWER_S:g}")
    return argv


def options_argv() -> tuple[str, ...]:
    """The command line that checks the option values, which needs no board."""
    return (sys.executable, _CONSOLE, "options")


def console_budget(*, comprehensive: bool) -> float:
    """Every wait the driver may sit out, which it works out per check.

    The step's deadline is this and a little more, so a check that fails ends
    in the driver's own reason rather than in the harness killing the step.
    The measured sessions are 27 s and 89 s (docs/tests.md); this is the
    backstop.
    """
    return btbrconsole.worst_case_seconds(comprehensive)


def _reset_peer(step_id: str, peer) -> Step:
    """Reset the peer, and check it is the board the registry names.

    The firmware builds the device name from the board's own LAP and does not
    let it be set, so the name in its status is exactly what the inquiry check
    looks for in the Extended Inquiry Response. Reading it here is what says
    the registry and the board agree, rather than the session hunting for a
    name no board on the bench answers with.
    """
    step = _reset_step(step_id, peer, ACCEPTOR_ROLE)
    return replace(step, output_patterns=step.output_patterns + (
        rf"(?m)^{ACCEPTOR_ROLE} role: {ACCEPTOR_ROLE} name: "
        rf"{re.escape(peer['name'])}$",
    ))


def _options_step() -> Step:
    """The option values the tool refuses before it looks for a board.

    No board and no image: the value is refused as the command line is read,
    which is why this runs before the write rather than inside the session.
    The `-U` each refusal carries names no board by index, and the control run
    shows that selector reaching the stage that looks for one, so what the
    step reports is the order and not just the message.
    """
    refusals = len(btbrconsole.OPTION_REFUSALS) + 1
    return Step(
        "option-values",
        "ubertooth-btbr refuses an option value outside the radio's field "
        "before it looks for a board",
        options_argv(),
        timeout_s=btbrconsole.OPTION_TIMEOUT_S * (refusals + 1),
        skip_returncode=SKIP_EXIT,
        output_patterns=(
            r"(?m)^check option -U \S+ -f 64: 'error: -f must be 0-63 "
            r"\(got 64\)', and no board looked for$",
            r"(?m)^check option -U \S+ -b 4: 'error: -b must be 0-3 "
            r"\(got 4\)', and no board looked for$",
            r"(?m)^check option control: .* reached the device stage: ",
            rf"(?m)^options: {refusals} passed of {refusals}, 0 failed$",
        ),
        forbidden_patterns=(r"(?m)^check option: FAILED: ",),
    )


def _console_step(serial: str, peer, *, comprehensive: bool) -> Step:
    """One console session: the whole of what this test is for."""
    checks = len(btbrconsole.checks(comprehensive))
    runs = len(btbrconsole.TX_RUNS) + (
        len(btbrconsole.TX_RUNS_COMPREHENSIVE) if comprehensive else 0)
    repeats = (btbrconsole.PAGE_REPEATS_COMPREHENSIVE if comprehensive
               else btbrconsole.PAGE_REPEATS)
    return Step(
        "console-session",
        f"ubertooth-btbr runs {checks} checks down one prompt against "
        f"{peer['name']}",
        console_argv(serial, peer, comprehensive=comprehensive),
        timeout_s=console_budget(comprehensive=comprehensive) + 30.0,
        mutates=True,
        skip_returncode=SKIP_EXIT,
        output_patterns=(
            rf"(?m)^peer address: {peer['address']}$",
            r"(?m)^prompt: up after \d+\.\d s$",
            rf"(?m)^check bad-address: {len(btbrconsole.BAD_ADDRESSES)} "
            r"addresses the prompt will not take refused, prompt up$",
            rf"(?m)^check tx-refusals: {len(btbrconsole.TX_REFUSALS)} tx "
            r"command lines refused, prompt up, no packet printed$",
            r"(?m)^check zero-address: .* twelve digits$",
            rf"(?m)^check inquiry: peer {peer['bdaddr']} answered after ",
            rf"(?m)^check page: connected to {peer['address']} in ",
            rf"(?m)^check page-peer-name: LMP_NAME_RES carried "
            rf"'{peer['name']}'$",
            rf"(?m)^check tx-formats: {runs} run\(s\) transmitted, each the "
            r"packet its format gives: ",
            r"(?m)^check tx-link: the link carried a packet ",
            r"(?m)^check lmp-unhandled: .*, each named once$",
            rf"(?m)^check page-repeat: {repeats} further page\(s\) completed "
            r"and named the peer$",
            r"(?m)^check scan-modes: page_scan, inquiry_scan each started, "
            r"held, and returned to standby$",
            r"(?m)^check scheduler-reports: ",
            r"(?m)^check no-detach: the peer took the connection and this end "
            r"did not hang up",
            r"(?m)^check streams: \d+ capture records on stdout, nothing "
            r"else, and none on stderr$",
            r"(?m)^session exit: 0 after \d+\.\d s$",
            rf"(?m)^checks: {checks} passed of {checks}, 0 failed, "
            rf"0 not run$",
        ),
        forbidden_patterns=(r"(?m)^check \S+: FAILED: ",
                            r"(?m)^session: FAILED: "),
    )


def btbr_console_cases(arguments, resources, resource_identities):
    serial = _serial(resource_identities, TEST_NAME)
    peer = _peer(resource_identities, ACCEPTOR_ROLE, TEST_NAME, resources)
    if not peer["name"]:
        raise StateCaseError(
            f"{TEST_NAME} needs the {ACCEPTOR_ROLE}'s registered name: the "
            "inquiry check reads it out of the Extended Inquiry Response the "
            "peer answers with")
    comprehensive = arguments["coverage"] == "comprehensive"
    sessions = int(arguments["sessions"])
    return tuple(
        _case(serial, peer, number, sessions, comprehensive=comprehensive)
        for number in range(1, sessions + 1)
    )


def _case(serial, peer, number: int, sessions: int, *,
          comprehensive: bool) -> StateCase:
    """One flash-and-session cycle, which is one case.

    With ``sessions`` greater than one the cycle repeats, for a long suite:
    each case writes the `btbr` image again, so a flash that only works the
    first time, or a page that only connects once, shows up. The first case
    keeps the plain ID whatever the count, so a single-session run's record
    reads the same as it always has.
    """
    case_id = CASE_ID if sessions == 1 else f"{CASE_ID}-{number}"
    steps = (
        # First, because it needs neither the image nor the board: a tool
        # that will not refuse an option value is worth knowing about before
        # the board's flash is written.
        _options_step(),
        Step("read-original-image", "Read the image the board is carrying",
             _util(serial, "-v"),
             observe=Observation("original firmware", _REVISION)),
        _flash_step(serial, "flash-btbr",
                    f"ubertooth-dfu writes the {BTBR_IMAGE} image", BTBR_LABEL,
                    f"{BTBR_IMAGE} image written"),
        _image_step(serial, "verify-btbr-image",
                    f"The board reports the {BTBR_IMAGE} image at the run's "
                    "revision", BTBR_IMAGE, "btbr revision"),
        _bootloader_step(serial, "read-btbr-bootloader",
                         f"Record the bootloader the {BTBR_IMAGE} image "
                         "reports", _BTBR_BOOTLOADER_KEY),
        Step("verify-btbr-build",
             f"The {BTBR_IMAGE} build string is the run's revision",
             _util(serial, "-V"), timeout_s=VERSION_TIMEOUT_S,
             output_patterns=(COMPILE_INFO_PATTERN,),
             observe=Observation("btbr build revision", _BUILD_REVISION,
                                 _revision)),
        _reset_peer("reset-peer-before", peer),
        _console_step(serial, peer, comprehensive=comprehensive),
    )
    cleanup = (
        _flash_step(serial, "restore-application",
                    f"Write the {APPLICATION_IMAGE} image back",
                    APPLICATION_LABEL, f"{APPLICATION_IMAGE} image written"),
        _image_step(serial, "verify-restored-image",
                    f"The board reports {APPLICATION_IMAGE} at the run's "
                    "revision again", APPLICATION_IMAGE, "restored revision"),
        _bootloader_step(serial, "read-restored-bootloader",
                         f"Record the bootloader {APPLICATION_IMAGE} reports",
                         _RESTORED_BOOTLOADER_KEY),
        _reset_peer("reset-peer-after", peer),
    )
    name = (f"The {BTBR_IMAGE} image runs a console session against "
            f"{peer['name']}")
    if sessions > 1:
        name += f" ({number} of {sessions})"
    return StateCase(
        case_id, name, steps, cleanup,
        expected_state=(
            f"the {APPLICATION_IMAGE} image written back and reported at the "
            f"run's revision; the {ACCEPTOR_ROLE} reset to its boot state"),
        artifacts=(BTBR_LABEL, APPLICATION_LABEL),
        needs=NEEDS,
    )
