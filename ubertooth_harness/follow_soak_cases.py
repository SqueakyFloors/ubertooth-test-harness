"""Cases for `Intermittent follow soak`: one capture across a changing link.

The test is a configurable soak: one Ubertooth pinned by serial runs
``ubertooth-btle -f`` for the plan's duration while two registered nRF52840
DKs cycle through advertising, connected, and absent, and the capture is
judged on whether the board kept up with each change.
``followsoak.py`` is the whole of that run -- the cycle, the capture's
liveness and restarts, the planned watchdog checkpoints, the readers, and the
verdict -- because a run of days cannot be a list of harness steps: a step is
one command with one deadline, and what this test measures is what happened
between its start and its end.

Two cases. The first checks the fixture: both DKs answer, run the pinned
firmware at their registered addresses, and take the role the soak needs. It
**never flashes**, which is the one deliberate difference from every other DK
test here: flashing makes a test one that writes firmware, and run-suite then
holds the host lock for the whole run (leases.`needs_host_lock`) -- three days
of it on the longest suite, with no other hardware run allowed on the host
meanwhile. #368 asks for the opposite, that this soak run beside other work,
so the case refuses a DK on another firmware version and says which test to
run first. The second case is the soak itself: the board is brought to idle
and its channel, squelch, and LEDs are recorded, the driver runs, and the
cleanup leaves both DKs idle in the advertiser role and puts the Ubertooth's
settings back.

For the same reason the test guards no other Ubertooth
(catalog, `guards_other_ubertooths`): every command the driver runs names its
board by serial, so there is nothing a guard would protect against, and a
guard would bind every board on the host for the window.

What the driver carries, and why each figure is a plan argument rather than a
constant here, is in docs/tests.md and in followsoak.py's own docstring. The
one thing decided here is the step's deadline: the duration the plan asked
for, plus what the run spends outside its own window, plus an allowance per
checkpoint and per restart the plan allows. A plan whose figures could not
judge what they ask for is refused when the plan loads, before any board is
leased (catalog.`_check_follow_soak`).
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Mapping

from ubertooth_harness import followsoak, nrfdk
from ubertooth_harness.advertiser_cases import DK_TIMEOUT_S, _dk, _dk_idle
from ubertooth_harness.connection_cases import (
    CENTRAL_ROLE,
    PERIPHERAL_ROLE,
    _pair,
)
from ubertooth_harness.tool_cases import duration_seconds
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    StateCase,
    Step,
    require,
)
from ubertooth_harness.transitions import (
    RESTORED_SETTINGS,
    _cleanup,
    _read_originals,
    _serial,
    _stop,
)

TEST_NAME = "Intermittent follow soak"

_DRIVER = str(Path(followsoak.__file__).resolve())

# What the step costs outside the capture window: the board read and the
# watchdog read before it, both DKs' settings, the wait for the capture to
# open its file, the final stop and the wait for the tool, the closing
# watchdog read, and the five board reads afterwards. Every one of those is a
# bounded command, and their worst case is about 160 s, so this is that with
# room above it rather than a guess.
SETUP_ALLOWANCE_S = 300.0
# What one checkpoint costs: the clean stop and its grace, up to three board
# reads while it waits for the board to answer, the watchdog read, and the
# next capture's start-up.
CHECKPOINT_ALLOWANCE_S = 45.0
# What one restart the plan allows may cost: the stop and its grace, a
# watchdog read, a board reset and the read after it, and the pause before
# the next attempt.
RESTART_ALLOWANCE_S = 60.0


def seconds(value: str) -> float:
    """A plan duration (`30s`, `5m`, `4h`) in seconds."""
    return duration_seconds(value)


def timings(arguments: Mapping) -> followsoak.Timings:
    """The plan's figures, in seconds, as the driver's own checks read them.

    One place where a plan argument becomes a figure, so the check the catalog
    makes when a plan loads is the check the driver makes on the bench rather
    than a second opinion about it.
    """
    return followsoak.Timings(
        duration_s=seconds(arguments["duration"]),
        advertising_s=seconds(arguments["advertising"]),
        connected_s=seconds(arguments["connected"]),
        absent_s=seconds(arguments["absent"]),
        checkpoint_every_s=seconds(arguments["checkpoint every"]),
        sample_every_s=seconds(arguments["sample every"]),
        window_s=seconds(arguments["presence window"]),
        windows_needed=arguments["presence windows"],
        liveness_timeout_s=seconds(arguments["liveness timeout"]),
        detect_within_s=seconds(arguments["detect within"]),
        connect_within_s=seconds(arguments["connect within"]),
        acquire_within_s=seconds(arguments["acquire within"]),
        quiet_after_s=seconds(arguments["quiet after"]),
        max_gap_s=seconds(arguments["maximum gap"]),
        adv_interval_ms=arguments["advertising interval"],
        write_period_ms=arguments["write period"],
        min_connection_packets=arguments["minimum connection packets"],
    )


def checkpoints_expected(arguments: Mapping) -> int:
    """How many checkpoints the window has room for, for the deadline.

    One per `checkpoint every` seconds of the window, and at least one, since
    the catalog refuses a plan whose interval leaves none.
    """
    every = seconds(arguments["checkpoint every"])
    return max(1, int(seconds(arguments["duration"]) // every))


def step_timeout(arguments: Mapping) -> float:
    """The deadline for the driver's step, from the plan's own figures."""
    allowance = (SETUP_ALLOWANCE_S
                 + CHECKPOINT_ALLOWANCE_S * checkpoints_expected(arguments)
                 + RESTART_ALLOWANCE_S * arguments["maximum restarts"])
    return seconds(arguments["duration"]) + allowance


def driver_argv(serial: str, central: Mapping, peripheral: Mapping,
                arguments: Mapping):
    """The driver command, with the run's own capture directory as its output."""
    def argv(context):
        command = [
            sys.executable, _DRIVER,
            "--out", str(Path(require(context, CAPTURE_DIR_KEY))),
            "--serial", serial,
            "--channel", str(arguments["channel"]),
            "--duration", f"{seconds(arguments['duration']):g}",
            "--peripheral-jlink", peripheral["serial"],
            "--peripheral-address", peripheral["address"],
            "--central-jlink", central["serial"],
            "--central-address", central["address"],
            "--advertising", f"{seconds(arguments['advertising']):g}",
            "--connected", f"{seconds(arguments['connected']):g}",
            "--absent", f"{seconds(arguments['absent']):g}",
            "--checkpoint-every", f"{seconds(arguments['checkpoint every']):g}",
            "--sample-every", f"{seconds(arguments['sample every']):g}",
            "--adv-interval", str(arguments["advertising interval"]),
            "--conn-interval", str(arguments["connection interval"]),
            "--write-bytes", str(arguments["write bytes"]),
            "--write-period", str(arguments["write period"]),
            "--presence-window", f"{seconds(arguments['presence window']):g}",
            "--presence-windows", str(arguments["presence windows"]),
            "--liveness-timeout", f"{seconds(arguments['liveness timeout']):g}",
            "--detect-within", f"{seconds(arguments['detect within']):g}",
            "--connect-within", f"{seconds(arguments['connect within']):g}",
            "--acquire-within", f"{seconds(arguments['acquire within']):g}",
            "--quiet-after", f"{seconds(arguments['quiet after']):g}",
            "--max-gap", f"{seconds(arguments['maximum gap']):g}",
            "--min-detection-share", f"{arguments['minimum detection share']:g}",
            "--min-follow-share", f"{arguments['minimum follow share']:g}",
            "--min-connection-packets",
            str(arguments["minimum connection packets"]),
            "--max-restarts", str(arguments["maximum restarts"]),
            "--max-capture-bytes",
            str(arguments["capture file limit"] * 1024 * 1024),
            "--max-total-capture-bytes",
            str(arguments["capture budget"] * 1024 * 1024),
        ]
        for role, dk in (("peripheral", peripheral), ("central", central)):
            if dk["port"]:
                command += [f"--{role}-port", dk["port"]]
            if dk.get("alias"):
                # With the alias, every line to and from that DK's shell is
                # written to the run's transcript (targets.shell_line).
                command += [f"--{role}-alias", dk["alias"]]
        if arguments.get("maximum memory growth") is not None:
            command += ["--max-memory-growth-kb",
                        str(arguments["maximum memory growth"])]
        return command

    return argv


def _ready_step(dk: Mapping, role: str) -> Step:
    """The DK answers, runs the pinned firmware, and takes its role.

    `nrfdk.py role` leaves the board in that role with both roles' boot
    settings, and prints the firmware version and address the patterns check,
    so one command does the asserting and the leaving-ready. No image is
    passed and none is flashed: see this module's docstring.
    """
    return Step(
        f"ready-{role}",
        f"The {role} (J-Link {dk['serial']}) runs {nrfdk.FIRMWARE_VERSION} at "
        f"{dk['address']} in its role",
        _dk(dk, "role", "--role", role),
        timeout_s=DK_TIMEOUT_S, mutates=True,
        output_patterns=(
            rf"(?m)^firmware: {nrfdk.FIRMWARE_VERSION}$",
            rf"(?m)^role: {role}$",
            rf"(?m)^address: {dk['address']}$",
        ))


def _fixture_case(central: Mapping, peripheral: Mapping) -> StateCase:
    return StateCase(
        "fixture-ready",
        f"Both DKs run {nrfdk.FIRMWARE_VERSION} at their registered addresses, "
        "in the roles the soak drives",
        (
            Step("identify-peripheral",
                 "Find the peripheral's J-Link probe and port",
                 _dk(peripheral, "identify"), timeout_s=DK_TIMEOUT_S,
                 output_patterns=(
                     rf"(?m)^J-Link serial: {peripheral['serial']}$",)),
            Step("identify-central", "Find the central's J-Link probe and port",
                 _dk(central, "identify"), timeout_s=DK_TIMEOUT_S,
                 output_patterns=(rf"(?m)^J-Link serial: {central['serial']}$",)),
            _ready_step(peripheral, PERIPHERAL_ROLE),
            _ready_step(central, CENTRAL_ROLE),
        ),
        (_dk_idle(peripheral, "peripheral-idle"),
         _dk_idle(central, "central-idle")),
        expected_state=(f"both DKs run {nrfdk.FIRMWARE_VERSION}, idle, in the "
                        "advertiser role"),
    )


def _soak_case(serial: str, central: Mapping, peripheral: Mapping,
               arguments: Mapping) -> StateCase:
    duration = arguments["duration"]
    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        # Every setting ubertooth-util can write back, not only the ones a
        # mode stop clears: the recovery ladder may run `ubertooth-util -r`,
        # a full reset, which also returns the PA level to its boot default.
        *_read_originals(serial, RESTORED_SETTINGS),
        Step("soak",
             f"ubertooth-btle -f -A{arguments['channel']} for {duration}, across "
             f"a known advertiser and connection going away and coming back, "
             f"with a watchdog checkpoint every {arguments['checkpoint every']}",
             driver_argv(serial, central, peripheral, arguments),
             timeout_s=step_timeout(arguments), mutates=True,
             output_patterns=(r"(?m)^SOAK VERDICT: pass$",),
             # The driver prints one FAIL: line per objective it did not
             # meet, and the verdict line above is what passes the step; the
             # forbidden pattern is here so a run that somehow printed both
             # is a failure rather than a pass.
             forbidden_patterns=(r"(?m)^FAIL: ",)),
    )
    return StateCase(
        "intermittent-follow-soak",
        f"A {duration} follow capture: a known advertiser and a known "
        "connection go away and come back on a schedule",
        steps,
        (_dk_idle(peripheral, "peripheral-idle"),
         _dk_idle(central, "central-idle")) + _cleanup(serial, RESTORED_SETTINGS),
        expected_state=(
            "the peripheral was found and sustained when it advertised, the "
            "connection was acquired and carried packets when it was up, both "
            "fell silent when the fixture went away, every watchdog checkpoint "
            "stopped the capture cleanly and read the board, and the board "
            "answers with its channel, PA level, squelch, and LEDs restored"),
        capture_dir=True,
    )


def follow_soak_cases(arguments, resources, resource_identities):
    serial = _serial(resource_identities, TEST_NAME)
    central, peripheral = _pair(resources, resource_identities, TEST_NAME)
    return (_fixture_case(central, peripheral),
            _soak_case(serial, central, peripheral, arguments))
