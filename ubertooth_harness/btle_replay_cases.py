"""Cases for ``ubertooth-btle -R``: a saved low-energy dump, replayed.

``-R FILE`` decodes a dump ``-d`` wrote, with no board and no connection, and
takes the major mode the dump was recorded with: the record says nothing about
which clock stamped it, so the mode is asked for rather than guessed (fork
SqueakyFloors/ubertooth#1129). That makes a replay the one path on which this
tool's decode has a known answer with no radio, no peer and no board at all,
which is what these cases use it for.

The dumps are synthetic ([`btlereplay.py`](btlereplay.py)), built from the
Bluetooth Core specification's own encoding; nothing here was captured over
the air. Two of them:

- a promiscuous capture of one connection, so a replay has to put back both
  halves of what the capture found: the five parameter records promiscuous
  mode sends, which carry no packet and so reach no capture file, and the
  CONNECT_IND that opened the connection, whose interval, latency, timeout,
  channel map, and hop increment say what the data packets on that access
  address belong to;
- one advertisement carrying three Manufacturer Specific Data fields, which
  is how the company identifier table is read: libbtbb names the two
  identifiers the Bluetooth SIG has registered and says ``not assigned`` for
  the one it has not (SqueakyFloors/libbtbb#75 and
  SqueakyFloors/libbtbb#128). The other reader of that table is
  ``ubertooth-scan -x``, which needs BlueZ, a board and a third idle Classic
  device, so the dump is the cheap way to it.

Only summaries and decoded field names reach the run record: the checkers
compare the fixture's own figures and print what differs, never a capture.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ubertooth_harness import btlereplay
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    StateCase,
    Step,
    require,
)

TEST_NAME = "BLE replay"

_FIXTURE = str(Path(btlereplay.__file__).resolve())
_CONNECTION = "connection.bin"
_ADVERTISING = "advertising.bin"
_DUMP = "again.bin"
# A replay of a handful of records returns at once; this is the limit that
# says the run hung rather than one it is expected to approach.
_TIMEOUT_S = 20.0

# The refusal a replay with no major mode gets, while the command line is
# read: a dump does not record which mode wrote it.
_NEEDS_A_MODE = (
    r"(?m)^error: -R needs -f, -n or -p: a dump does not record which mode "
    r"wrote it, and the modes stamp their packets with different clocks$"
)
# No packet line at all, for the run that is refused.
_PACKET_LINE_PATTERN = r"(?m)^systime=\d+ freq=\d+ addr=[0-9a-f]{8} "


# The two options this test is about, as `-h` lists them. A build too old to
# have them would otherwise fail on an unparsable refusal; this says what is
# missing instead.
_HELP_LINES = (
    r"(?m)^\s+-R <filename> replay a dump saved with -d, with its recorded "
    r"times$",
    r"(?m)^\s+-d <filename> dump records to binary file, for -R to replay$",
)


def _path(name):
    return lambda context: str(Path(require(context, CAPTURE_DIR_KEY)) / name)


def _help_step():
    return Step(
        "help", "ubertooth-btle -h lists -R and -d",
        ("ubertooth-btle", "-h"), timeout_s=_TIMEOUT_S,
        output_patterns=_HELP_LINES,
    )


def _fixture_step(base_time, variant, name):
    return Step(
        "fixture",
        f"Write the synthetic {variant} dump",
        lambda context: (sys.executable, _FIXTURE, "fixture", _path(name)(context),
                         "--variant", variant, "--base-time", str(base_time)),
        output_patterns=(rf"BLE replay fixture: {variant}, \d+ records, ",),
    )


def _replay_step(step_id, name, options, *, source, check=None, patterns=(),
                 forbidden=(), files=(), returncodes=(0,)):
    def argv(context):
        extra = []
        for option, file_name in files:
            extra += [option, _path(file_name)(context)]
        return (("ubertooth-btle",) + tuple(options)
                + ("-R", _path(source)(context)) + tuple(extra))

    return Step(
        step_id, name, argv, timeout_s=_TIMEOUT_S,
        expected_returncodes=tuple(returncodes), output_check=check,
        output_patterns=tuple(patterns), forbidden_patterns=tuple(forbidden),
    )


def _case(case_id, name, steps, expected_state):
    return StateCase(
        case_id,
        name,
        tuple(steps),
        # No board was opened, so there is nothing on a device to restore;
        # list what the case wrote (names and sizes only) so the run record
        # shows which files it left behind.
        (Step("list-files", "List the files the case wrote",
              lambda context: (sys.executable, _FIXTURE, "files",
                               require(context, CAPTURE_DIR_KEY)),
              output_patterns=(r"BLE replay files: \d+ in ",)),),
        expected_state=expected_state,
        capture_dir=True,
    )


def btle_replay_cases(arguments, _resources, _resource_identities, *, now=None):
    base = btlereplay.fixture_base_time(now)
    connection = btlereplay.expected_packets(base, "connection")
    advertising = btlereplay.expected_packets(base, "advertising")
    cases = [
        # The parameters and the CONNECT_IND together: a replay that printed
        # the packets and dropped the parameter records would leave a reader
        # with packets on an access address and nothing saying what they
        # belong to, which is what the dump format is for.
        _case("btle-replay-connection",
              "-p -R replays a promiscuous capture of one connection", (
            _help_step(),
            _fixture_step(base, "connection", _CONNECTION),
            _replay_step("replay", "ubertooth-btle -p -R <connection>", ("-p",),
                         source=_CONNECTION,
                         check=btlereplay.all_checks(
                             btlereplay.packets_check(connection),
                             btlereplay.lines_check(
                                 btlereplay.connection_lines()))),
        ), "exit 0; -h lists -R and -d; the five parameter lines, then the "
           "CONNECT_IND's access "
           "address, CRC seed, window, interval, latency, timeout, channel map "
           "and hop increment, and the two data packets on that access address, "
           "each packet line carrying its own record's host time"),
        # The company identifier table, read through the dump rather than
        # through ubertooth-scan, which needs BlueZ and two more devices.
        _case("btle-replay-companies",
              "-n -R names assigned and unassigned manufacturer identifiers", (
            _fixture_step(base, "advertising", _ADVERTISING),
            _replay_step("replay", "ubertooth-btle -n -R <advertising>", ("-n",),
                         source=_ADVERTISING,
                         check=btlereplay.all_checks(
                             btlereplay.packets_check(advertising),
                             btlereplay.lines_check(
                                 btlereplay.company_lines()))),
        ), "exit 0; one advertisement, with "
           f"'{btlereplay.RECENT_COMPANY[1]}' and "
           f"'{btlereplay.ORIGINAL_COMPANY[1]}' named and "
           f"'{btlereplay.UNASSIGNED_COMPANY[1]}' for the identifier the "
           "Bluetooth SIG has not registered"),
    ]
    if arguments["coverage"] != "comprehensive":
        return tuple(cases)
    cases += [
        # A dump written during a replay rewrites every record it read, the
        # parameter records included, so a promiscuous capture can be kept
        # and read again.
        _case("btle-replay-dump",
              "-p -R -d writes the records it read back, byte for byte", (
            _fixture_step(base, "connection", _CONNECTION),
            _replay_step("replay", "ubertooth-btle -p -R <connection> -d <again>",
                         ("-p",), source=_CONNECTION,
                         check=btlereplay.packets_check(connection),
                         files=(("-d", _DUMP),)),
            Step("verify-dump", "Compare the -d records with the dump replayed",
                 lambda context: (sys.executable, _FIXTURE, "dump-check",
                                  _path(_DUMP)(context),
                                  _path(_CONNECTION)(context)),
                 output_patterns=(r"BLE replay dump: \d+ records match the dump "
                                  r"replayed",)),
        ), "exit 0; the -d file is the replayed dump byte for byte, parameter "
           "records and all"),
        # The mode is part of what a replay needs, because the record does
        # not carry it: refused while the command line is read, so nothing
        # is decoded and nothing is written.
        _case("btle-replay-needs-a-mode", "-R without a major mode is refused", (
            _fixture_step(base, "connection", _CONNECTION),
            _replay_step("refuse", "ubertooth-btle -R <connection>", (),
                         source=_CONNECTION, returncodes=(1,),
                         patterns=(_NEEDS_A_MODE,),
                         forbidden=(_PACKET_LINE_PATTERN,)),
        ), "refused (exit 1) naming -f, -n, and -p; no packet decoded"),
    ]
    return tuple(cases)
