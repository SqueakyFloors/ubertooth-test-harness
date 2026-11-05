"""Cases for `BLE address parity soak`: one long capture, read two ways.

The test is a configurable soak: one Ubertooth pinned by serial runs
``ubertooth-btle -n`` for the plan's duration while one registered nRF52840
DK advertises its registered address on a schedule, and the same capture is
read both by the documented ``AdvA:`` text line and by decoding the capture
records. ``addresssoak.py`` is the whole of that run -- the schedule, the
capture's liveness and restarts, the two readers, and the verdict -- because
a run of days cannot be a list of harness steps: a step is one command with
one deadline, and what this test measures is what happened between its start
and its end.

Two cases. The first is the same advertiser check every DK test starts with
(advertiser_cases: the board runs the pinned firmware and reports its
registered address, and the run's image is flashed when it runs anything
else). The second is the soak itself: the board is brought to idle and its
channel, squelch, and LEDs are recorded, the driver runs, and the cleanup
leaves the DK idle in the advertiser role and puts the Ubertooth's settings
back.

What the driver carries, and why each figure is a plan argument rather than a
constant here, is in docs/tests.md and in addresssoak.py's own docstring. The
one thing decided here is the step's deadline: the duration the plan asked
for, plus what the run spends outside its own window (the board and
advertiser checks at either end), plus an allowance per restart the plan
allows, since each one costs a stop, a watchdog read, and the pause before
the next try. A plan whose figures could not judge what they ask for is
refused when the plan loads, before any board is leased
(catalog.`_check_address_soak`).
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Mapping

from ubertooth_harness import addresssoak
from ubertooth_harness.advertiser_cases import _advertiser, _dk_idle, _ensure_case
from ubertooth_harness.tool_cases import duration_seconds
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    StateCase,
    Step,
    require,
)
from ubertooth_harness.transitions import (
    MODE_RESET_SETTINGS,
    _cleanup,
    _read_originals,
    _serial,
    _stop,
)

TEST_NAME = "BLE address parity soak"

_DRIVER = str(Path(addresssoak.__file__).resolve())

# What the step costs outside the capture window: the board read and the
# watchdog read before it, the advertiser's settings, the closing watchdog
# read, the board reads afterwards, and the advertiser's final stop. Every
# one of those is a bounded command (addresssoak.UTIL_TIMEOUT_S each), so
# this is their sum with room above it rather than a guess.
SETUP_ALLOWANCE_S = 120.0
# What one restart the plan allows may cost: the stop and its grace, a
# watchdog read, a board reset and the read after it, and the pause before
# the next attempt.
RESTART_ALLOWANCE_S = 60.0
# The probe's own stop, restart, and watchdog read.
PROBE_ALLOWANCE_S = 60.0


def seconds(value: str) -> float:
    """A plan duration (`30s`, `5m`, `4h`) in seconds."""
    return duration_seconds(value)


def timings(arguments: Mapping) -> addresssoak.Timings:
    """The plan's figures, in seconds, as the driver's own checks read them.

    One place where a plan argument becomes a figure, so the check the
    catalog makes when a plan loads is the check the driver makes on the
    bench rather than a second opinion about it.
    """
    return addresssoak.Timings(
        duration_s=seconds(arguments["duration"]),
        on_s=seconds(arguments["advertiser on"]),
        off_s=seconds(arguments["advertiser off"]),
        window_s=seconds(arguments["presence window"]),
        windows_needed=arguments["presence windows"],
        liveness_timeout_s=seconds(arguments["liveness timeout"]),
        detect_within_s=seconds(arguments["detect within"]),
        max_gap_s=seconds(arguments["maximum gap"]),
        sample_every_s=seconds(arguments["sample every"]),
    )


def step_timeout(arguments: Mapping) -> float:
    """The deadline for the driver's step, from the plan's own figures."""
    allowance = (SETUP_ALLOWANCE_S
                 + RESTART_ALLOWANCE_S * arguments["maximum restarts"]
                 + (PROBE_ALLOWANCE_S if arguments["restart probe"] else 0.0))
    return seconds(arguments["duration"]) + allowance


def driver_argv(serial: str, dk: Mapping, arguments: Mapping):
    """The driver command, with the run's own capture directory as its output."""
    def argv(context):
        command = [
            sys.executable, _DRIVER,
            "--out", str(Path(require(context, CAPTURE_DIR_KEY))),
            "--serial", serial,
            "--address", dk["address"],
            "--channel", str(arguments["channel"]),
            "--duration", f"{seconds(arguments['duration']):g}",
            "--jlink", dk["serial"],
            "--interval", str(arguments["interval"]),
            "--payload", arguments["payload"] or "none",
            "--advertiser-on", f"{seconds(arguments['advertiser on']):g}",
            "--advertiser-off", f"{seconds(arguments['advertiser off']):g}",
            "--sample-every", f"{seconds(arguments['sample every']):g}",
            "--presence-window", f"{seconds(arguments['presence window']):g}",
            "--presence-windows", str(arguments["presence windows"]),
            "--liveness-timeout", f"{seconds(arguments['liveness timeout']):g}",
            "--detect-within", f"{seconds(arguments['detect within']):g}",
            "--min-detection-share", f"{arguments['minimum detection share']:g}",
            "--max-gap", f"{seconds(arguments['maximum gap']):g}",
            "--min-packets-per-minute",
            str(arguments["minimum packets per minute"]),
            "--max-disagreement-share",
            f"{arguments['maximum window disagreement share']:g}",
            "--max-restarts", str(arguments["maximum restarts"]),
            "--max-capture-bytes",
            str(arguments["capture file limit"] * 1024 * 1024),
        ]
        if dk["port"]:
            command += ["--dk-port", dk["port"]]
        if dk.get("alias"):
            # With the alias, every line to and from the DK's shell is
            # written to the run's transcript (targets.shell_line).
            command += ["--dk-alias", dk["alias"]]
        if arguments.get("maximum memory growth") is not None:
            command += ["--max-memory-growth-kb",
                        str(arguments["maximum memory growth"])]
        if arguments["restart probe"]:
            command.append("--restart-probe")
        return command

    return argv


def _soak_case(serial: str, dk: Mapping, arguments: Mapping) -> StateCase:
    duration = arguments["duration"]
    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step("soak",
             f"ubertooth-btle -n -A{arguments['channel']} for {duration}, read as "
             "AdvA text and as capture records",
             driver_argv(serial, dk, arguments),
             timeout_s=step_timeout(arguments), mutates=True,
             output_patterns=(r"(?m)^SOAK VERDICT: pass$",),
             # The driver prints one FAIL: line per objective it did not
             # meet, and the verdict line above is what passes the step; the
             # forbidden pattern is here so a run that somehow printed both
             # is a failure rather than a pass.
             forbidden_patterns=(r"(?m)^FAIL: ",)),
    )
    return StateCase(
        "address-parity-soak",
        f"A {duration} advertising capture: the text and record readers agree "
        "about a known address",
        steps,
        (_dk_idle(dk),) + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=(
            "the advertiser met the configured detection objective, both readers "
            "agreed, the capture never died unexplained and stopped cleanly, and "
            "the board answers with its channel, squelch, and LEDs restored"),
        capture_dir=True,
    )


def address_soak_cases(arguments, resources, resource_identities):
    serial = _serial(resource_identities, TEST_NAME)
    dk = _advertiser(resources, resource_identities)
    return (_ensure_case(dk), _soak_case(serial, dk, arguments))
