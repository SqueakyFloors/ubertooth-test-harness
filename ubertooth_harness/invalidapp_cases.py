"""The Reset into invalid application case: a cold boot into the bootloader.

The board is given an image the bootloader must refuse to start, its USB
power is cut and restored, and it has to come up as a DFU bootloader device
rather than as its application. Then the run's own image goes back on it.

Why a power cycle and not the reset ``ubertooth-dfu -r`` already does: the
decision under test is the one the bootloader makes on every power-up, and
the failure it guards against is a board that faults on each one. A reset
over USB exercises the same check, but only a cold boot shows the board
coming up on its own.

What the verdict rests on is the bootloader already in the board's flash,
which is not what this run built: ubertooth-dfu writes the application
region only, and a bootloader is replaced over ISP or JTAG. So the board has
to be one whose bootloader came from this tree with the startable check in
it. A board carrying the factory bootloader, or one from before that check,
jumps into the erased vectors and faults on every power-up -- the failure
this test is written to catch, on a board that never had the fix. So the
plan names a board the registry pairs with a debug programmer: that board can
have a bootloader installed and read back over its programming header, and
holding it holds the programmer with it, which is the recovery path if it
does fault. `ubertooth-util -B` reports what a board has.

Why it is destructive. If the bootloader does start the unstartable image,
the board faults, stops answering USB, and needs the P4 entry pin bridged by
hand at the bench; nothing the harness can do reaches it. So the test needs a
board someone can get to, and the maintainer's agreement, before it runs.
The image is written to the board's flash on purpose, so the case must be the
last one on its board, like the DFU images test.

What makes the image refusable is the first of the bootloader's checks, the
two words at the start of the application region: this image has both of them
erased, the state an interrupted download leaves there. It is not the LPC
vector checksum at offset 0x1c, nor the checksum over the whole region. The
bootloader does ask for both of those, but ubertooth-dfu fills both in as it
writes the first block, so an image can only fail them by not reaching the
flash whole -- and an image that fails the two words is refused before either
is read (firmware/bootloader/app_image.h, app_image_region_startable()).
Everything else about the file stays valid, suffix and firmware info block
included, so ubertooth-dfu has no reason to refuse it.
"""

from __future__ import annotations

from ubertooth_harness import dfuimage, invalidapp
from ubertooth_harness.cases import guard_roles
from ubertooth_harness.helpersteps import (
    case_file,
    helper_step,
    script_path,
    tool_argv,
)
from ubertooth_harness.patterns import IMAGE_NAME_PATTERN
from ubertooth_harness.stateful import (
    APPLICATION_IMAGE_LABEL,
    EXPECTED_REVISION_KEY,
    POWER_CYCLE_PORT_KEY,
    REGISTRY_PATH_KEY,
    Observation,
    StateCase,
    Step,
    artifact_key,
    require,
)
from ubertooth_harness.transitions import _serial, _util

TEST_NAME = "Reset into invalid application"
TEST_IMAGE = APPLICATION_IMAGE_LABEL
BAD_IMAGE = "unstartable.dfu"
CONTROLLER_ARGUMENT = "USB power controller"

_HELPER = script_path(invalidapp)
_REVISION_PATTERN = (
    rf"Firmware version:\s+{IMAGE_NAME_PATTERN}(?P<value>\S+)\s+\(API:"
)
# ubertooth-dfu's stage line once every block is in the flash, and the line
# it prints only when the board came back running the image. The unstartable
# image must reach the flash and the board must not come back running it.
_PROGRAMMED = r"firmware programming completed"
_VERIFIED = r"Verified firmware revision"
# The write, plus the post-reset verification's own polling (20 polls at
# 250 ms) which this image is expected to use in full.
WRITE_TIMEOUT_S = 60.0


def _image(context):
    return require(context, artifact_key(TEST_IMAGE))


def _revision(context):
    return require(context, EXPECTED_REVISION_KEY)


def _registry(context):
    return require(context, REGISTRY_PATH_KEY)


def _revision_step(serial, step_id, name, key, expected=_revision):
    """``ubertooth-util -v``, recording the revision it reports.

    ``expected`` compares it with the run's own: the cleanup does that,
    because by then this test has written that image. The step before the
    case does not, since the test establishes the firmware it verifies and
    preflight therefore never required it: the board may be running anything
    when the case starts, and what the case needs of it is only that it
    answers in its application.
    """
    return Step(step_id, name, _util(serial, "-v"),
                observe=Observation(key, _REVISION_PATTERN, expected))


def _expected_port(context):
    return require(context, POWER_CYCLE_PORT_KEY)


def _power_args(board_alias, controller_alias):
    """What every power step is given.

    The port is not among them: it comes from the board's own registry
    entry, read by the helper. What is passed is the port the planner
    resolved when the run was recorded, as a check -- a registry edited
    between planning and this step would otherwise switch a port the run
    record does not name.
    """
    return ("--hardware", _registry, "--board", board_alias,
            "--controller", controller_alias,
            "--expect-port", _expected_port, "--allow-power-changes")


def _recover_args(serial, guards):
    args = ("recover", "--serial", serial)
    for guard in guards:
        args += ("--guard", guard)
    return args + ("--image", _image, "--revision", _revision)


def _steps(serial, guards, board_alias, controller_alias):
    return (
        _revision_step(serial, "read-original-revision",
                       "Read firmware revision", "original revision",
                       expected=None),
        helper_step("make", "Write an image the bootloader must refuse",
                    _HELPER, "make", "--image", _image,
                    "--output", case_file(BAD_IMAGE),
                    pattern=r"Unstartable: "),
        Step("write", f"ubertooth-dfu -d {BAD_IMAGE} -r",
             tool_argv("ubertooth-dfu", "-U", serial, "-d",
                       case_file(BAD_IMAGE), "-r"),
             timeout_s=WRITE_TIMEOUT_S, mutates=True,
             # The host accepts the file and writes it; the reset then
             # leaves the board in its bootloader, so the revision check
             # after the reset cannot pass and the command exits 1.
             #
             # Exit 0 is accepted so that the forbidden line below is what
             # decides. What that line catches is a write that did not take:
             # a board still running the application it had answers the
             # revision check, and ubertooth-dfu reports a verified
             # revision, which looks like success and is not this test. (A
             # board that did jump into the erased vectors cannot print it:
             # it faults at once and leaves USB, which the cycle step and
             # its diagnosis are there for.)
             expected_returncodes=(0, 1),
             output_patterns=(_PROGRAMMED,),
             forbidden_patterns=(_VERIFIED,)),
        helper_step("cycle", "Power-cycle the board and read the bus",
                    _HELPER, "cycle", *_power_args(board_alias, controller_alias),
                    "--serial", serial,
                    *[part for guard in guards for part in ("--guard", guard)],
                    pattern=r"Power cycle: the bootloader refused",
                    timeout_s=invalidapp.step_timeout_s("cycle"), mutates=True),
        helper_step("recover", "Write the test image back and check the revision",
                    _HELPER, *_recover_args(serial, guards),
                    pattern=r"Recovered: the board started the test image",
                    timeout_s=invalidapp.step_timeout_s("recover", len(guards)),
                    mutates=True),
    )


def _cleanup(serial, guards, board_alias, controller_alias):
    """Always: the port powered, then the board back on the test image.

    Both steps run whatever happened before them, as every cleanup step
    does. The port comes first because the image step needs a powered board,
    so where the port was the problem this is what puts it right; it is also
    what restores power when the cycle step's own command was stopped, which
    the harness does at a step deadline. (A SIGKILL of the whole run is
    different: no cleanup runs then, and the port and the board are put
    right by hand. The next run cannot do it, because preflight reads the
    board's serial before any case.)

    The port step reads before it writes, so the ordinary path changes
    nothing. The image step always writes. On the path where the case's own
    recovery step has already written and verified the image that is one
    more write than is needed, and it is still what this does: the cleanup
    cannot see what the steps did, a board that started the unstartable
    image reports the same revision as the test image, and a verified write
    costs less than being wrong about which of those it is.
    """
    return (
        helper_step("restore-port-power", "Board's port is powered",
                    _HELPER, "port", *_power_args(board_alias, controller_alias),
                    pattern=r"Port: \S+ port \d+ is powered",
                    timeout_s=invalidapp.step_timeout_s("port"), mutates=True),
        helper_step("restore-image", "Board in its application with the test image",
                    _HELPER, *_recover_args(serial, guards),
                    pattern=r"Recovered: ",
                    timeout_s=invalidapp.step_timeout_s("recover", len(guards)),
                    mutates=True),
        _revision_step(serial, "verify-restored-revision",
                       "Verify restored firmware revision", "restored revision"),
    )


def invalid_application_cases(arguments, resources, resource_identities):
    serial = _serial(resource_identities, TEST_NAME)
    guards = tuple(
        resource_identities[role]["serial"].lower()
        for role in guard_roles(resource_identities)
    )
    board_alias = resources["Ubertooth"]
    # The hub the plan names. The planner has already checked it is the one
    # the board is registered on (topology.plan_power_cycle); the helper
    # checks it again against the registry as it reads it.
    controller_alias = arguments[CONTROLLER_ARGUMENT]
    return (
        StateCase(
            "unstartable-application",
            "A cold boot with an unstartable image leaves the bootloader running",
            _steps(serial, guards, board_alias, controller_alias),
            _cleanup(serial, guards, board_alias, controller_alias),
            expected_state=(
                "the board comes up as a DFU bootloader device after the power "
                "cycle, then runs the test image again"
            ),
            capture_dir=True,
            artifacts=(TEST_IMAGE,),
        ),
    )


# Re-exported so the catalog and the docs name one place for the kind of
# image the case writes.
UNSTARTABLE_KIND = invalidapp.UNSTARTABLE_KIND
UNSTARTABLE_KINDS = dfuimage.UNSTARTABLE_KINDS
