"""Cases for `Board state between runs`: what a killed capture leaves behind.

A tool that exits normally, or is stopped with SIGTERM, sends STOP on the way
out and leaves the board idle. A terminal or an SSH session that disappears
does not: the tool dies where it stands and the board keeps running the mode
it was in, with packets of that mode still in its endpoint. These cases make
that state on purpose -- killedcapture.py SIGKILLs the capture tool itself --
and then require the next run on the same board to be clean anyway.

What makes the answer knowable without an RF peer: both tools used here
produce data with nothing on the air (`ubertooth-dump` about 2000 records a
second, `ubertooth-specan` about 16 kB a second), so each capture's own
contents are the evidence, and each leaves a mode the other does not use.

- `ubertooth-dump` writes 68-byte records carrying the firmware's packet type,
  so a record left from the spectrum sweep before it (type SPECAN) fails by
  type, and a record from before the stream started fails by its clock, which
  restarts at mode entry (rxdump.stale_records(), and
  SqueakyFloors/ubertooth#318).
- `ubertooth-specan -l -u` states the band to sweep before it asks for its
  mode, and writes the frequency of every sample, so a sample outside that
  band, or one more byte than whole samples, says either that the stated range
  was lost at mode start or that a record of the previous mode is still in the
  stream.

The warning the second command must print comes from libubertooth's
ubertooth_bulk_init() (fork e657807, SqueakyFloors/ubertooth#438): it reads
the mode the board is in, stops it, and waits until the board says it has
left it, because UBERTOOTH_STOP only asks and the mode's loop can hand the
endpoint another packet in the meantime. The step requires the first warning
and forbids the second ("it has not stopped after N ms"), which the same code
prints when the wait runs out.

What that pair of checks shows is that the board was in a mode, that the
capture found it, and that the board reported leaving it within the wait. It
does not separate a build that waits from one that warns and starts anyway:
the only difference between those two is a packet or so of the previous mode
in the new capture, which is the leak the record checks below look for, and a
run where nothing happens to leak cannot tell them apart.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ubertooth_harness import killedcapture, rxdump, specandump
from ubertooth_harness.outputfiles import text_lines
from ubertooth_harness.patterns import FIRMWARE_PATTERN, serial_pattern
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    Step,
    nothing_on_stdout,
    require,
)
from ubertooth_harness.receive_cases import DUMP_MIN_RECORDS_PER_S, seconds_arg
from ubertooth_harness.transitions import (
    BOOT_DEFAULT_CHANNEL,
    CHANNEL,
    LEDS,
    MODE_RESET_SETTINGS,
    PA_BOOT_DEFAULT,
    PA_LEVEL,
    RESET_TIMEOUT_S,
    RESTORED_SETTINGS,
    SQUELCH,
    _cleanup,
    _read_originals,
    _serial,
    _stop,
    _util,
)

TEST_NAME = "Board state between runs"

# Firmware modes (ubertooth_interface.h) the two capture tools put the board
# in, which is what the warning below names. ubertooth-dump asks for
# MODE_RX_SYMBOLS and ubertooth-specan for MODE_SPECAN.
MODE_RX_SYMBOLS = 1
MODE_SPECAN = 4

# The band the sweep states before it asks for its mode. Five frequencies is
# enough for the checker to require every one of them, and narrow enough that
# a sweep covers the range many times over in a few seconds.
SWEEP_LOW = 2440
SWEEP_HIGH = 2444

# The MDMCTRL register, read as the modulation the board is set to; the value
# is whatever the last mode left, so only the answer is required here.
_MDMCTRL_ANSWER = r"%MDMCTRL = 0x[0-9A-Fa-f]{4}"

# How long the kill step may take: killedcapture.py waits up to its own
# --max-wait for the capture to produce data, then up to its reap window.
_KILL_TIMEOUT_S = killedcapture.MAX_WAIT_S + killedcapture.REAP_WAIT_S + 10.0

_KILLER = str(Path(killedcapture.__file__).resolve())
_DUMP_CHECKER = str(Path(rxdump.__file__).resolve())
_SPECAN_CHECKER = str(Path(specandump.__file__).resolve())

# The two warnings ubertooth_bulk_init() can print, by their fixed wording.
_STILL_IN_MODE = "the board is still in mode {mode} from an earlier run"
_NOT_STOPPED = "it has not stopped after"


def _path(name):
    def path(context):
        return str(Path(require(context, CAPTURE_DIR_KEY)) / name)
    return path


def _warnings_off_stdout(*texts):
    """A Step.output_check: these warnings belong on stderr, not stdout.

    Together with an output_patterns entry for the same wording, which is
    searched in both streams, this is how the case says the warning was
    printed on stderr.
    """
    def check(output) -> str:
        for line in text_lines(output):
            for text in texts:
                if text in line:
                    return f"{text!r} is on stdout; warnings go to stderr"
        return ""
    return check


def _boot_defaults(serial: str) -> tuple[Step, ...]:
    """Reset the board and require the documented boot defaults."""
    return (
        Step("reset", "Full reset before the capture",
             _util(serial, "-r"), timeout_s=RESET_TIMEOUT_S, mutates=True,
             output_patterns=(r"Resetting ubertooth device",),
             output_check=nothing_on_stdout),
        Step("verify-firmware-at-boot", "Firmware answers after the reset",
             _util(serial, "-v"), output_patterns=(FIRMWARE_PATTERN,)),
    ) + tuple(
        setting.verify(serial, f"verify-{setting.key}-at-boot",
                       f"{setting.key} is the boot default",
                       f"{setting.key} at boot defaults", setting.after_mode_stop)
        for setting in MODE_RESET_SETTINGS
    ) + (
        PA_LEVEL.verify(serial, "verify-pa-level-at-boot",
                        "pa-level is the boot default",
                        "pa-level at boot defaults", PA_BOOT_DEFAULT),
    )


def _kill_step(tool_argv, capture: str, record: str) -> Step:
    """Run a capture and SIGKILL the tool process itself."""
    def argv(context):
        return (
            sys.executable, _KILLER, "kill",
            "--capture", _path(capture)(context),
            "--record", _path(record)(context),
            "--",
        ) + tuple(item(context) if callable(item) else item for item in tool_argv)

    return Step(
        "kill-capture",
        f"SIGKILL {tool_argv[0]} while it is streaming",
        argv,
        timeout_s=_KILL_TIMEOUT_S,
        mutates=True,
        # The board is left in its mode with no STOP sent, so the kill must be
        # of the tool itself and the process must be gone, zombie included.
        output_patterns=(
            r"SIGKILL reaped as signal 9, pid gone, nothing left in its "
            r"process group",
        ),
        observe=Observation("killed capture", r"Killed capture: (?P<value>\S+) pid \d+",
                            tool_argv[0]),
    )


def _second_capture(step_id: str, name: str, tool_argv, seconds: int, mode: int) -> Step:
    """A capture started on the board the killed run left in ``mode``."""
    still_in_mode = _STILL_IN_MODE.format(mode=mode)

    def argv(context):
        return tuple(item(context) if callable(item) else item for item in tool_argv)

    return Step(
        step_id, name, argv,
        timeout_s=float(seconds), mutates=True, continuous=True,
        output_patterns=(
            rf"warning: {still_in_mode}; stopping it first",
        ),
        # The board has to have reported leaving the mode inside the wait:
        # this is the line printed when the wait ran out instead.
        forbidden_patterns=(rf"warning: {_NOT_STOPPED} \d+ ms",),
        output_check=_warnings_off_stdout(still_in_mode, _NOT_STOPPED),
    )


def _evidence_steps(serial: str, capture: str, record: str) -> tuple[Step, ...]:
    """The killed capture is still exactly what the killed tool left, and the
    board answers every read on the first try."""
    return (
        Step("verify-killed-capture-kept",
             "The killed capture is unchanged since the kill",
             lambda context: (sys.executable, _KILLER, "verify",
                              "--capture", _path(capture)(context),
                              "--record", _path(record)(context)),
             output_patterns=(rf"Killed capture evidence: {re.escape(capture)} "
                              r"unchanged, \d+ bytes, SHA-256 [0-9a-f]{64}",)),
        Step("read-firmware-after-kill", "Firmware version answers",
             _util(serial, "-v"), output_patterns=(FIRMWARE_PATTERN,)),
        Step("read-serial-after-kill", "Device serial answers",
             _util(serial, "-s"), output_patterns=(serial_pattern(serial),)),
        CHANNEL.verify(serial, "read-channel-after-kill", "Channel answers",
                       "channel after the kill", BOOT_DEFAULT_CHANNEL),
        PA_LEVEL.verify(serial, "read-pa-level-after-kill", "PA level answers",
                        "pa-level after the kill", PA_BOOT_DEFAULT),
        SQUELCH.verify(serial, "read-squelch-after-kill", "Squelch answers",
                       "squelch after the kill", SQUELCH.after_mode_stop),
        LEDS.verify(serial, "read-leds-after-kill", "LEDs answer",
                    "leds after the kill", LEDS.after_mode_stop),
        Step("read-modulation-after-kill", "Modulation answers",
             ("ubertooth-debug", "-U", serial, "-r", "0x03", "-v0"),
             output_patterns=(_MDMCTRL_ANSWER,)),
    )


def _case(serial: str, case_id: str, name: str, *, killed_argv, capture: str,
          record: str, second: Step, verify: tuple[Step, ...],
          expected_state: str) -> StateCase:
    return StateCase(
        case_id,
        name,
        (_stop(serial, "establish-idle", "Establish idle before reading"),)
        + _read_originals(serial, RESTORED_SETTINGS)
        + _boot_defaults(serial)
        + (_kill_step(killed_argv, capture, record), second)
        + (_stop(serial, "stop-second-capture", "Return the board to idle"),)
        + verify
        + _evidence_steps(serial, capture, record),
        _cleanup(serial, RESTORED_SETTINGS),
        expected_state=expected_state,
        capture_dir=True,
    )


def _killed_specan_case(serial: str, seconds: int) -> StateCase:
    records = DUMP_MIN_RECORDS_PER_S * seconds
    dump = "after-killed-specan.bin"
    return _case(
        serial,
        "killed-specan-then-dump",
        "A symbol dump after a killed spectrum sweep",
        capture="killed-specan.bin",
        record="killed-specan.json",
        killed_argv=("ubertooth-specan", "-U", serial, "-l", str(SWEEP_LOW),
                     "-u", str(SWEEP_HIGH), "-d", _path("killed-specan.bin")),
        second=_second_capture("second-capture",
                               "Dump symbols on the board left sweeping",
                               ("ubertooth-dump", "-U", serial, "-d", _path(dump)),
                               seconds, MODE_SPECAN),
        verify=(Step("verify-second-capture",
              "The dump holds only its own basic-rate records",
              lambda context: (sys.executable, _DUMP_CHECKER, _path(dump)(context),
                               "full", "--min-records", str(records),
                               "--types", "br,keep-alive", "--max-stale", "0"),
              output_patterns=(r"Receive dump: \d+ full records, 0 stale \(0 LE\)",)),),
        expected_state=(
            f"the dump holds at least {records} basic-rate records, none of "
            "them left from the sweep; the board answers every read; the "
            "killed sweep's file is kept as it was; channel, PA level, "
            "squelch, and LEDs restored"),
    )


def _killed_dump_case(serial: str, seconds: int) -> StateCase:
    sweep = "after-killed-dump.bin"
    frequencies = SWEEP_HIGH - SWEEP_LOW + 1
    return _case(
        serial,
        "killed-dump-then-specan",
        "A spectrum sweep after a killed symbol dump",
        capture="killed-dump.bin",
        record="killed-dump.json",
        killed_argv=("ubertooth-dump", "-U", serial, "-d", _path("killed-dump.bin")),
        second=_second_capture(
            "second-capture",
            f"Sweep {SWEEP_LOW}-{SWEEP_HIGH} MHz on the board left dumping",
            ("ubertooth-specan", "-U", serial, "-l", str(SWEEP_LOW),
             "-u", str(SWEEP_HIGH), "-d", _path(sweep)),
            seconds, MODE_RX_SYMBOLS),
        verify=(Step(
            "verify-second-capture",
            f"Every sample of the sweep is within {SWEEP_LOW}-{SWEEP_HIGH} MHz",
            lambda context: (sys.executable, _SPECAN_CHECKER, _path(sweep)(context),
                             str(SWEEP_LOW), str(SWEEP_HIGH)),
            observe=Observation(
                "sweep samples",
                r"Specan dump: \d+ samples, (?P<value>\d+ frequencies in "
                r"\d+-\d+, \d+ outside)",
                f"{frequencies} frequencies in {SWEEP_LOW}-{SWEEP_HIGH}, 0 outside")),),
        expected_state=(
            f"the sweep covers {SWEEP_LOW}-{SWEEP_HIGH} MHz with no sample "
            "outside it and no partial record, so the stated range survived "
            "mode start and no record of the dump is in the stream; the board "
            "answers every read; the killed dump's file is kept as it was; "
            "channel, PA level, squelch, and LEDs restored"),
    )


def board_state_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, TEST_NAME)
    seconds = seconds_arg(arguments)
    return (
        _killed_specan_case(serial, seconds),
        _killed_dump_case(serial, seconds),
    )
