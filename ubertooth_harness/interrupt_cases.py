"""Case builder for Interrupting a capture: one Ubertooth, no peer.

Ctrl-C is how a capture normally ends, and the fork says what that must mean.
``ubertooth_catch_stop_signals()`` (libubertooth, ubertooth.c) installs a
handler that only raises a flag; the capture loop reads it on its own thread,
ends the stream, stops the mode, closes the capture files, and the run exits
0 with whatever it caught. The handlers go on **before** the board is opened,
so a signal arriving while one is being found is not thrown away: the open
gives up quietly, nothing is created, and the run still exits 0
(fork #839). One Ctrl-C is meant to be enough: a second was once needed to
leave the detection phase of ``ubertooth-afh`` (fork #795), and a run killed
instead of stopped leaves its mode running on the board for the next command
to meet.

The harness had no case for any of this. Bounded modes are stopped with
SIGTERM; the one place a fork tool met a SIGINT was the no-selector follow
case, where ``timeout -s INT`` is a deadline and the expected outcome is
status 124 with SIGKILL behind it, so nothing was asserted about handling the
signal; and the cases that do check a SIGINT send it to tshark. So these cases
send a real SIGINT to the tool itself, and then ask the board.

How the moment is chosen. A signal sent at a fixed time proves nothing about
what the run was doing, so the two cases that can wait for evidence do.
``ubertooth-dump -d`` and ``ubertooth-specan -d`` stream whether or not there
is traffic, and each flushes what it writes, so the capture file growing past
a floor is proof that the mode is running, and that is when the signal goes
in. A capture that never gets there, or a tool that exits on its own first,
fails the case: these streams do not depend on the air, so neither outcome is
something the bench decides.

``ubertooth-btle -n -r -q`` has no such proof to offer. It creates both
capture files after the board is open and before the mode requests, and
libbtbb sizes a PCAPNG in pages, so the file reaches its floor the moment it
appears; what picks the moment there is the settling time, and the case rests
on the exit and the teardown rather than on having caught a running mode.
``docs/tests.md`` says so under its limits.

The early case does not try to pick the board-open window. A lookup takes 10
to 20 ms on the bench host (measured), so aiming inside it would mean tens of
tries, and each try is a board lookup like any other. Instead it signals a few
milliseconds in and requires the same of either window, which is what catches
the regression: a signal the open threw away left the run capturing until
something else stopped it, so it would still be running at the deadline.

The signal goes to the tool's own process, once
([`captureinterrupt.py`](captureinterrupt.py)): not to a process group, and
not through a ``timeout`` wrapper, which would be signalled instead of the
tool. The helper then waits for that process, reaps it, and reports whether
anything was left in the session it ran in, whether the capture file kept
growing after the exit, and whether any progress or error line landed on
standard output. Each case then reads the capture file it was left with, and
asks the board for its identity on a short deadline, before the usual
cleanup restores the channel, the squelch, and the LEDs and reads the
identity again.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ubertooth_harness import capturefile, captureinterrupt, rxdump, specandump
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
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

TEST_NAME = "Interrupting a capture"
_INTERRUPT = str(Path(captureinterrupt.__file__).resolve())
_CAPTURE_CHECKER = str(Path(capturefile.__file__).resolve())
_DUMP_CHECKER = str(Path(rxdump.__file__).resolve())
_SPECAN_CHECKER = str(Path(specandump.__file__).resolve())

# How long after the one SIGINT the tool has to be gone. The teardown stops
# the mode and closes the capture files over USB, which takes well under a
# second on a board that is answering; this is generous, and a tool that
# needs a second Ctrl-C never gets there at all.
EXIT_DEADLINE_S = 5.0
# How long a capture may take to get going before the case gives up and
# reports a skip rather than interrupting nothing.
READY_DEADLINE_S = 20.0
# The capture has to be running, not merely started, before the signal.
SETTLE_S = 1.5
# Whole 68-byte records a dump must hold. Raw receive takes ubertooth-dump at
# about 2000 records a second on hardware (receive_cases.py), so this is a
# fraction of a second's worth, and it says only that the stream was running
# and the file was closed on a record boundary.
DUMP_MIN_RECORDS = 20
DUMP_MIN_BYTES = rxdump.FULL_RECORD * DUMP_MIN_RECORDS
# ubertooth-specan writes 3-byte samples; a 3-second run on the bench gave
# 32-50 kB, so a few thousand bytes is a fraction of a second once going.
SPECAN_MIN_BYTES = 3000
SPECAN_LOW, SPECAN_HIGH = 2402, 2480
# libbtbb writes the PCAPNG section header as the capture starts; its size
# differs by host (it is sized in pages), so the floor is only "the header
# is there".
HEADER_MIN_BYTES = 28
# The board has to answer the next command at once. A board left in a mode,
# or wedged, does not.
ANSWER_DEADLINE_S = 5.0
# The early case sends its signal this many seconds in, and takes up to this
# many tries. Measured on the Linux bench host: a board lookup finishes in
# 10-20 ms, so a signal sent a few milliseconds in lands either while the
# board is being opened or just after the mode has started, and both are
# windows a run has to survive. The tries are for the one outcome that says
# nothing either way, a signal that beat the handler install and killed the
# tool; nothing else is retried.
EARLY_ATTEMPTS = 6
EARLY_DELAY_S = 0.005


def _path(name):
    def path(context):
        return str(Path(require(context, CAPTURE_DIR_KEY)) / name)
    return path


def _interrupt_step(name, tool_argv, *, grow, when="growing", min_bytes=1,
                    settle=0.0, expect_status=0, capture_file="kept",
                    attempts=1) -> Step:
    """One run of the tool, one SIGINT to it, and the report."""
    def argv(context):
        options = [
            sys.executable, _INTERRUPT, "interrupt", "--when", when,
            "--grow", _path(grow)(context), "--min-bytes", str(min_bytes),
            "--settle", f"{settle:g}", "--ready-deadline", f"{READY_DEADLINE_S:g}",
            "--exit-deadline", f"{EXIT_DEADLINE_S:g}",
            "--expect-status", str(expect_status),
            "--capture-file", capture_file,
            "--attempts", str(attempts), "--delay", f"{EARLY_DELAY_S:g}",
        ]
        return tuple(options) + ("--",) + tuple(
            word(context) if callable(word) else word for word in tool_argv)

    return Step(
        "interrupt", name, argv,
        # The helper's own bounds decide the outcome; this only stops a
        # helper that hung from holding the board forever.
        timeout_s=(READY_DEADLINE_S + EXIT_DEADLINE_S) * attempts + 30.0,
        mutates=True,
        output_patterns=(r"(?m)^Interrupt done: one SIGINT ended the run in ",),
        forbidden_patterns=(r"(?m)^Interrupt problem: ",),
    )


def _dump_check(file_name) -> Step:
    return Step(
        "check-dump",
        f"{file_name} holds at least {DUMP_MIN_RECORDS} whole records and no "
        "partial one",
        lambda context: (sys.executable, _DUMP_CHECKER, _path(file_name)(context),
                         "full", "--min-records", str(DUMP_MIN_RECORDS)),
        output_patterns=(r"Receive dump: \d+ full records",),
    )


def _capture_check(step_id, file_name, header_format, link_type) -> Step:
    return Step(
        step_id, f"{file_name} is a {header_format} a reader can open",
        lambda context: (sys.executable, _CAPTURE_CHECKER, "--packets",
                         _path(file_name)(context)),
        output_patterns=(rf"Capture format: {header_format}, link type: "
                         rf"{link_type}, packets: \d+$",),
    )


def _specan_check(file_name) -> Step:
    return Step(
        "check-specan", f"{file_name} holds whole 3-byte samples in band",
        lambda context: (sys.executable, _SPECAN_CHECKER, _path(file_name)(context),
                         str(SPECAN_LOW), str(SPECAN_HIGH)),
        output_patterns=(r"Specan dump: \d+ samples, ",),
    )


def _answers_at_once(serial) -> Step:
    """The board answers the next command straight away, with no retry."""
    return Step(
        "answers", "The board answers ubertooth-util -v at once",
        ("ubertooth-util", "-U", serial, "-v"), timeout_s=ANSWER_DEADLINE_S,
        observe=Observation("firmware after the interrupt",
                            r"(?m)^Firmware version:\s+(?P<value>\S+)"),
    )


def _case(serial, case_id, name, steps, expected_state) -> StateCase:
    return StateCase(
        case_id, name,
        (_stop(serial, "establish-idle", "Establish idle before capturing"),)
        + _read_originals(serial, MODE_RESET_SETTINGS)
        + tuple(steps),
        _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=expected_state + "; the board answers at once, and the "
        "channel, squelch, and LEDs are restored",
        capture_dir=True,
    )


def interrupt_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, TEST_NAME)
    cases = [
        # A bulk capture that streams whatever the air holds: the file
        # growing is the proof the mode is running when the signal arrives.
        _case(serial, "dump-interrupted",
              "ubertooth-dump -d interrupted once it is streaming", (
                  _interrupt_step(
                      "One SIGINT to ubertooth-dump -d <dir>/dump.bin, once it "
                      f"has written {DUMP_MIN_RECORDS} records",
                      ("ubertooth-dump", "-U", serial, "-d", _path("dump.bin")),
                      grow="dump.bin", min_bytes=DUMP_MIN_BYTES, settle=SETTLE_S),
                  _dump_check("dump.bin"),
                  _answers_at_once(serial),
              ),
              "one SIGINT ends it with exit 0 within "
              f"{EXIT_DEADLINE_S:g}s, nothing is left behind, the dump stops "
              f"growing and holds {DUMP_MIN_RECORDS} or more whole records"),
        # A poll-driven capture with two capture writers: both files must be
        # readable afterwards, which is what a teardown that closed them
        # properly leaves.
        _case(serial, "btle-interrupted",
              "ubertooth-btle -n -r -q interrupted after its captures are open", (
                  _interrupt_step(
                      "One SIGINT to ubertooth-btle -n -r <dir>/le.pcapng -q "
                      f"<dir>/le.pcap, {SETTLE_S:g}s after its PCAPNG appears",
                      ("ubertooth-btle", "-U", serial, "-n",
                       "-r", _path("le.pcapng"), "-q", _path("le.pcap")),
                      grow="le.pcapng", min_bytes=HEADER_MIN_BYTES, settle=SETTLE_S),
                  _capture_check("check-pcapng", "le.pcapng", "pcapng",
                                 capturefile.LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR),
                  _capture_check("check-pcap", "le.pcap", "pcap",
                                 capturefile.LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR),
                  _answers_at_once(serial),
              ),
              "one SIGINT ends it with exit 0, and both capture files are "
              "complete enough for a reader to open and walk"),
    ]
    if arguments["coverage"] != "comprehensive":
        return tuple(cases)
    cases += [
        _case(serial, "specan-interrupted",
              "ubertooth-specan -d interrupted once it is sweeping", (
                  _interrupt_step(
                      "One SIGINT to ubertooth-specan -d <dir>/specan.bin, once "
                      f"it has written {SPECAN_MIN_BYTES} bytes",
                      ("ubertooth-specan", "-U", serial, "-d", _path("specan.bin")),
                      grow="specan.bin", min_bytes=SPECAN_MIN_BYTES, settle=SETTLE_S),
                  _specan_check("specan.bin"),
                  _answers_at_once(serial),
              ),
              "one SIGINT ends it with exit 0, and the samples end on a whole "
              "3-byte record inside the band"),
        # A signal that arrives before the capture can be going: while the
        # board is being opened, or just after the mode has started. Either
        # way the run has to end on that one signal with exit 0. A signal in
        # the open window used to be thrown away, and a run that threw it
        # away would still be capturing when the deadline came.
        _case(serial, "early-interrupted",
              "ubertooth-rx interrupted before its capture can be going", (
                  _interrupt_step(
                      f"One SIGINT to ubertooth-rx -q <dir>/early.pcap "
                      f"{EARLY_DELAY_S:g}s in, while the board is being opened "
                      "or just after the mode starts",
                      ("ubertooth-rx", "-U", serial, "-q", _path("early.pcap")),
                      grow="early.pcap", when="early", capture_file="either",
                      attempts=EARLY_ATTEMPTS),
                  _answers_at_once(serial),
              ),
              "the one SIGINT ends the run with exit 0 whichever window it "
              "landed in, nothing is left streaming, and no capture file is "
              "left growing"),
    ]
    return tuple(cases)
