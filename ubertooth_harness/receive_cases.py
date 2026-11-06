"""Case builders for receive runs that need no peer: ubertooth-rx,
ubertooth-afh (Classic Bluetooth receive), ubertooth-dump, and ubertooth-ego
-r (Raw receive), plus ubertooth-rx -i, which needs no Ubertooth at all (Raw
receive replay, a synthetic fixture from rxreplay.py).

The cases check exit status, timeouts, output shape, and the structure of
the files the tools write, never decoded packet content. Checkers print
summaries only, so no capture bytes reach the run record. Every case reads
the settings a mode stop resets, runs one bounded receive, and restores them
with the usual cleanup. Command contracts come from the fork sources
(host/ubertooth-tools/src, host/libubertooth/src/ubertooth.c and
ubertooth_callback.c).

ubertooth-rx -i printed the replay time instead of the recorded systime
(SqueakyFloors/ubertooth#202) and ended a read error or a cut last record
silently with exit 0 (#203) before fork a55ac89; those cases are ordinary
ones now. ubertooth-rx -u without -l is refused before a device is opened
since fork 57469f2 (#236).

ubertooth-afh with an unknown -U exited 139 (segfault) before fork 219a5b0
(SqueakyFloors/ubertooth#185); that case is an ordinary one now.

Raw receive (comprehensive) alternates bounded ubertooth-btle -n and
ubertooth-dump -d runs and requires that no capture hold packets from the
previous mode (SqueakyFloors/ubertooth#318: at fork 89d9197 every dump
started with two records from before it; fixed in 9e31dd5 and ed30131).
"""

from __future__ import annotations

import hashlib
import math
import re
import sys
import time
from pathlib import Path

from ubertooth_harness import (
    bredrrecord, capturefile, rxdump, rxreplay, stalecheck, stdoutdest,
)
from ubertooth_harness.hostneeds import NO_NEEDS, Needs
from ubertooth_harness.outputfiles import text_lines
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    Step,
    require,
)
from ubertooth_harness.tool_cases import duration_seconds
from ubertooth_harness.transitions import (
    MODE_RESET_SETTINGS,
    _CAPTURE_PATTERN,
    _cleanup,
    _crc_read,
    _read_originals,
    _serial,
    _stop,
)

# A tool that ends itself after -t N may take this long beyond N to stop the
# device and close its files before the harness calls it hung.
EXIT_SLACK_S = 5.0
# ... and must not exit more than this much before N.
EARLY_EXIT_S = 0.5
# ubertooth-dump delivers about 2000 records per second on hardware; a stalled
# stream gives far fewer.
DUMP_MIN_RECORDS_PER_S = 50
# Link types libbtbb writes for BR/EDR captures (-r PCAPNG, -q PCAP).
LINKTYPE_BREDR = str(capturefile.LINKTYPE_BLUETOOTH_BREDR_BB)
# A serial no registered board has: ubertooth_start_select() finds nothing.
ABSENT_SERIAL = "f" * 32

_CAPTURE_CHECKER = str(Path(capturefile.__file__).resolve())
_DUMP_CHECKER = str(Path(rxdump.__file__).resolve())
_RECORD_CHECKER = str(Path(bredrrecord.__file__).resolve())


def seconds_arg(arguments) -> int:
    """The duration as whole seconds for -t (at least 1)."""
    return max(1, math.ceil(duration_seconds(arguments["duration"])))


def _path(name):
    def path(context):
        return str(Path(require(context, CAPTURE_DIR_KEY)) / name)
    return path


def _case(serial, case_id, name, steps, expected_state, *, capture_dir=False):
    return StateCase(
        case_id,
        name,
        (_stop(serial, "establish-idle", "Establish idle before reading"),)
        + _read_originals(serial, MODE_RESET_SETTINGS)
        + tuple(steps),
        _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=expected_state + "; channel, squelch, and LEDs restored",
        capture_dir=capture_dir,
    )


def _capture_header(step_id, name, file_name, header_format):
    return Step(
        step_id,
        name,
        lambda context: (sys.executable, _CAPTURE_CHECKER, _path(file_name)(context)),
        observe=Observation(
            f"{step_id} header", _CAPTURE_PATTERN,
            f"{header_format}, link type: {LINKTYPE_BREDR}",
        ),
    )


def _capture_absent(step_id, name, file_name):
    return Step(
        step_id,
        name,
        lambda context: (sys.executable, _CAPTURE_CHECKER, "--absent",
                         _path(file_name)(context)),
        output_patterns=(r"Capture file absent: ",),
    )


def _dump_check(step_id, name, file_name, fmt, *, min_records=0, types="br,keep-alive"):
    return Step(
        step_id,
        name,
        lambda context: (
            sys.executable, _DUMP_CHECKER, _path(file_name)(context), fmt,
            "--min-records", str(min_records), "--types", types,
        ),
        output_patterns=(rf"Receive dump: \d+ {fmt} records",),
    )


# ---------------------------------------------------------------------------
# ubertooth-rx
#
# Sniffs all LAPs (or surveys with -z) until -t N seconds pass, then stops the
# device and exits 0. Each access code found prints
# "systime=<s> ch=<index> LAP=<hex6> err=<n> clkn=... s=.. n=.. snr=..";
# libbtbb may add lines of its own (UAP and clock progress). -z then prints
# "Survey Results" on standard error and one ??:??:[UAP:]LAP line per piconet
# on standard output (fork 1c1b41b7, SqueakyFloors/ubertooth#1082: the heading
# is for a person reading along, so standard output holds only the addresses a
# script parses, the split ubertooth-scan prints "Scan results:" with).
# Without -c it
# sweeps every channel; -c <index> fixes one. -e caps the access-code errors
# reported. -r/-q create BR/EDR PCAPNG/PCAP files (link type 255) at start;
# -d writes one full 68-byte record per packet found (possibly none).
#
# The three power columns, and the one rule that ties them together: the
# ratio is a figure exactly when both the figures it comes from are.
#
# A level column reads `none` where the board's reading is at or above the
# top of the scale the host models, which says no more than "at least this
# loud" and is not a measurement of this packet (fork
# SqueakyFloors/ubertooth#1192). The noise column reads `none` where the
# board had no background estimate for the channel (fork 532adc19). Either
# one missing leaves the ratio `none`, so the four combinations below are
# all of them, and a line with a ratio beside a missing figure is malformed.
#
# Both words are the same `none` a capture record has no room for: a record
# still carries 0 dBm there, which is why the record decoder's own dBm range
# accepts it and only these printed lines read the word.
_POWERS = (
    # Both figures measured, so the ratio is a figure too.
    r"(?:s=-?\d+ n=-?\d+ snr=-?\d+"
    # No level, whatever the noise did: no ratio either.
    r"|s=none n=(?:-?\d+|none) snr=none"
    # A level but no background to compare it with: likewise.
    r"|s=-?\d+ n=none snr=none)"
)

# The packet line and the survey result line, with the fields a check reads
# picked out. classic_cases.py reads the LAP from the same two patterns, so a
# change in what ubertooth-rx prints is one edit, not two.
PACKET_LINE = re.compile(
    rf"^systime=\d+ ch=\s*(?P<ch>\d+) LAP=(?P<lap>[0-9a-f]{{6}}) "
    rf"err=(?P<err>\d+) clkn=(?P<clkn>\d+) clk_offset=(?P<offset>\d+) "
    rf"{_POWERS}$"
)
# The clock and the slot offset a packet line carries. Nothing checks their
# range: the fork masks the clock with CLKN_MASK and takes the offset modulo
# CLK100NS_PER_SLOT before printing either (ubertooth_callback.c), so a
# bound would be unreachable by construction. They are captured here so a
# check that can say something about them has them to read.
SURVEY_LINE = re.compile(
    r"^\?\?:\?\?:(?:\?\?|[0-9A-F]{2}):"
    r"(?P<lap>[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2})$"
)
# The heading the survey results are listed under. It goes to standard error,
# so a -z step asks for it in its patterns (which read both streams) and the
# stdout checks below refuse it: a heading among the addresses is what the
# fork's split was made to prevent.
SURVEY_HEADING = "Survey Results"
SURVEY_HEADING_PATTERN = rf"(?m)^{SURVEY_HEADING}$"


def packet_check(*, channel=None, max_errors=None, survey=False):
    """A Step.output_check for ubertooth-rx stdout."""

    def check(output) -> str:
        problems = []
        channels = set()
        worst = 0
        heading = False
        bad_results = 0
        for line in text_lines(output):
            match = PACKET_LINE.match(line)
            if match is not None:
                # Packet lines count wherever they are, as they did before
                # the survey block was read in the same pass.
                channels.add(int(match["ch"]))
                worst = max(worst, int(match["err"]))
            elif survey and line.strip():
                # A survey's standard output is packet lines and addresses;
                # the heading and every other diagnostic belong on standard
                # error. The step's patterns are what require the heading
                # there, since a run with no piconet in range lists no
                # address at all.
                if line.strip() == SURVEY_HEADING:
                    heading = True
                elif not SURVEY_LINE.match(line):
                    bad_results += 1
        if channel is not None:
            other = sorted(channels - {channel})
            if other:
                problems.append(f"packets on channel {other[0]}, not {channel}")
        if max_errors is not None and worst > max_errors:
            problems.append(f"a packet with {worst} access-code errors (max {max_errors})")
        if survey:
            if heading:
                problems.append(
                    f"the {SURVEY_HEADING!r} heading is on stdout, among the "
                    "addresses a script parses")
            if bad_results:
                problems.append(
                    f"{bad_results} stdout lines that are neither packet lines "
                    "nor piconet addresses")
        return "; ".join(problems)

    return check


# What -s must not print now that it only states the default (fork 7b46ec4):
# the notice it printed before 5ac22a8, or any warning or error.
SWEEP_NOTICES = (r"(?i)deprecated", r"(?m)^(?:warning|error): ")
# The -h line for -s (fork 7b46ec4, ubertooth-rx.c usage()).
SWEEP_HELP = r"(?m)^\s+-s sweep all channels - the default; a -c channel wins over it$"


def _rx(serial, *options):
    return ("ubertooth-rx", "-U", serial) + tuple(options)


def _rx_run(serial, seconds, options, *, name, check=None, patterns=(), forbidden=(),
            files=()):
    """Run ubertooth-rx -t N; it must exit 0 by itself, about N s later.
    ``files`` maps an option to a file name in the capture directory."""
    def argv(context):
        extra = []
        for option, file_name in files:
            extra += [option, _path(file_name)(context)]
        return _rx(serial, "-t", str(seconds), *options, *extra)

    return Step(
        "receive",
        name,
        argv,
        timeout_s=seconds + EXIT_SLACK_S,
        min_duration_s=seconds - EARLY_EXIT_S,
        mutates=True,
        output_check=check,
        output_patterns=patterns,
        forbidden_patterns=forbidden,
    )


def _rx_case(serial, seconds, case_id, name, options, expected_state, *,
             check=None, patterns=(), forbidden=(), files=(), verify=()):
    return _case(
        serial, case_id, name,
        (_rx_run(serial, seconds, options, name=f"ubertooth-rx -t {seconds} "
                 + " ".join(options + tuple(o for o, _f in files)),
                 check=check, patterns=patterns, forbidden=forbidden,
                 files=files),) + tuple(verify),
        f"exits 0 by itself after about {seconds}s; " + expected_state,
        capture_dir=bool(files),
    )


# ---------------------------------------------------------------------------
# ubertooth-afh
#
# Needs -l and -u. With -t N it detects the initial map for N s. If a packet
# from the piconet arrived it prints "AFH map: 0x<20 hex>" and monitors map
# changes until signalled; if none did (no piconet in range, as on the lab
# bench) it says so on stderr, stops the board, and exits 2 (fork 3b22c95,
# SqueakyFloors/ubertooth#187: it used to print an all-zero map and keep
# running). -r prints "<time> <80 x 0/1>" every second until signalled.

_AFH_MAP = r"(?m)^AFH map: 0x[0-9a-f]{20}$"
_AFH_LAP, _AFH_UAP = "446688", "22"
_AFH_TARGET = ("-l", _AFH_LAP, "-u", _AFH_UAP)
_AFH_R_LINE = re.compile(r"^\d+ [01]{80}$")
AFH_NO_PACKETS_EXIT = 2


def afh_no_packets_pattern(seconds):
    address = ":".join((_AFH_UAP,) + tuple(_AFH_LAP[i:i + 2] for i in (0, 2, 4)))
    return (rf"error: no packets from \?\?:\?\?:{address} within {seconds} s; "
            r"AFH map unknown, not monitoring")


def afh_r_check(minimum):
    def check(output):
        total = 0
        good = 0
        for line in text_lines(output):
            if not line.strip():
                continue
            total += 1
            if _AFH_R_LINE.match(line):
                good += 1
        problems = []
        if good < minimum:
            problems.append(f"{good} map lines, expected at least {minimum}")
        if good != total:
            problems.append(f"{total - good} lines that are not '<time> <map>'")
        return "; ".join(problems)
    return check


def _afh(serial, *options):
    return ("ubertooth-afh", "-U", serial) + _AFH_TARGET + tuple(options)


def _afh_case(serial, seconds, case_id, name, options, expected_state, *, check=None,
              patterns=(_AFH_MAP,)):
    return _case(serial, case_id, name, (
        Step("receive", name, _afh(serial, *options), timeout_s=float(seconds),
             mutates=True, continuous=True, output_patterns=patterns,
             output_check=check),
        _stop(serial),
    ), expected_state)


def _afh_no_packets_case(serial, seconds, case_id, name, options, expected_state):
    """-t N with no piconet: exit 2 by itself about N s later, saying so."""
    return _case(serial, case_id, name, (
        Step("receive", name, _afh(serial, "-t", str(seconds), *options),
             timeout_s=seconds + EXIT_SLACK_S, min_duration_s=seconds - EARLY_EXIT_S,
             expected_returncodes=(AFH_NO_PACKETS_EXIT,), mutates=True,
             output_patterns=(afh_no_packets_pattern(seconds),),
             forbidden_patterns=(_AFH_MAP,)),
        _stop(serial),
    ), f"exits {AFH_NO_PACKETS_EXIT} by itself after about {seconds}s, saying no "
       "packet arrived; no map printed; " + expected_state)


def classic_receive_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Classic Bluetooth receive")
    n = seconds_arg(arguments)
    absent = ABSENT_SERIAL if serial != ABSENT_SERIAL else "0" * 32
    cases = [
        _rx_case(serial, n, "rx-survey-timeout", "Survey (-z -t) ends by itself",
                 ("-z",), "survey results listed under the heading on stderr",
                 check=packet_check(survey=True),
                 patterns=(SURVEY_HEADING_PATTERN,)),
        _rx_case(serial, n, "rx-capture-pcapng", "PCAPNG capture (-r)", (),
                 "-r wrote a pcapng file with link type 255",
                 files=(("-r", "rx.pcapng"),),
                 verify=(_capture_header("verify-capture", "Verify the -r file header",
                                         "rx.pcapng", "pcapng"),)),
        _afh_no_packets_case(serial, n, "afh-timeout-no-packets",
                             "AFH -t with no piconet in range", (),
                             "the board stopped"),
    ]
    if arguments["coverage"] == "comprehensive":
        cases += [
            _rx_case(serial, n, "rx-sweep-timeout", "Sniff all LAPs (-t) ends by itself",
                     (), "packet lines only in the documented format"),
            _rx_case(serial, n, "rx-fixed-channel", "Fixed channel (-c 39)", ("-c", "39"),
                     "every packet on channel 39", check=packet_check(channel=39)),
            # -s states the default sweep (fork 7b46ec4, #361; ubertooth-rx.md):
            # accepted with no notice, and a -c channel wins over it.
            _rx_case(serial, n, "rx-sweep-explicit", "Explicit sweep (-s)", ("-s",),
                     "packet lines only in the documented format; no notice",
                     check=packet_check(), forbidden=SWEEP_NOTICES),
            _rx_case(serial, n, "rx-sweep-fixed-channel-wins", "-s -c 39: the channel wins",
                     ("-s", "-c", "39"), "every packet on channel 39; no notice",
                     check=packet_check(channel=39), forbidden=SWEEP_NOTICES),
            _rx_case(serial, n, "rx-ac-errors-0", "No access-code errors (-e 0)", ("-e", "0"),
                     "no packet with access-code errors", check=packet_check(max_errors=0)),
            _rx_case(serial, n, "rx-ac-errors-4", "Four access-code errors (-e 4)", ("-e", "4"),
                     "no packet with more than 4 errors", check=packet_check(max_errors=4)),
            _rx_case(serial, n, "rx-capture-pcap", "PCAP capture (-q)", (),
                     "-q wrote a pcap file with link type 255",
                     files=(("-q", "rx.pcap"),),
                     verify=(_capture_header("verify-capture", "Verify the -q file header",
                                             "rx.pcap", "pcap"),)),
            _rx_case(serial, n, "rx-dump-file", "Packet dump (-d)", (),
                     "-d wrote whole 68-byte BR packet records (none without traffic)",
                     files=(("-d", "rx.dump"),),
                     verify=(_dump_check("verify-dump", "Verify the -d file records",
                                         "rx.dump", "full", types="br"),)),
            _afh_no_packets_case(serial, n, "afh-ac-errors", "AFH with -e 2 accepted",
                                 ("-e", "2"), "-e 2 accepted"),
            _afh_case(serial, n, "afh-map-every-second", "AFH map every second (-r)",
                      ("-r",), "one '<time> <80 bits>' line per second",
                      check=afh_r_check(max(1, n - 2)), patterns=()),  # start-up eats about a second
            # Exited 139 (segfault) before fork 219a5b0
            # (SqueakyFloors/ubertooth#185).
            _case(serial, "afh-board-not-found", "AFH with an unknown -U exits 1", (
                Step("reject", "ubertooth-afh -U <absent serial>",
                     ("ubertooth-afh", "-U", absent) + _AFH_TARGET + ("-t", "1"),
                     expected_returncodes=(1,),
                     output_patterns=(rf"no Ubertooth found with serial matching '{absent}'",)),
            ), "exit 1 with a message; the selected board untouched"),
        ]
    return tuple(cases)


# ---------------------------------------------------------------------------
# ubertooth-dump
#
# Streams received symbols until signalled: full 68-byte records, or with -b
# 401-byte ASCII bitstream records, to stdout or -d <file>. -c / -l set basic
# rate or LE modulation, and one command line may name only one of them (fork
# d3429234, SqueakyFloors/ubertooth#650: the pair used to mean whichever came
# last, with the other dropped without a word); the firmware programs CC2400
# MDMCTRL with 0x0029 (160 kHz deviation) or 0x0040 (250 kHz) when the stream
# starts, and the register keeps that value after the mode stops, which the
# modulation cases read with ubertooth-debug.

_MDMCTRL = {"basic rate": "0029", "LE": "0040"}


def _mdmctrl_read(serial, expected):
    return Step(
        "verify-modulation",
        f"Verify MDMCTRL shows {expected} modulation",
        ("ubertooth-debug", "-U", serial, "-r", "0x03", "-v0"),
        observe=Observation("mdmctrl", r"%MDMCTRL = 0x(?P<value>[0-9A-Fa-f]{4})",
                            _MDMCTRL[expected]),
    )


def _dump_case(serial, seconds, case_id, name, options, fmt, *, modulation=None):
    file_name = f"{case_id}.bin"
    steps = [
        Step("receive", name,
             lambda context: ("ubertooth-dump", "-U", serial) + tuple(options)
             + ("-d", _path(file_name)(context)),
             timeout_s=float(seconds), mutates=True, continuous=True,
             output_patterns=(r"rx block timestamp \d+",)),
        _stop(serial),
        _dump_check("verify-dump", "Verify the -d file records", file_name, fmt,
                    min_records=DUMP_MIN_RECORDS_PER_S * seconds),
    ]
    state = f"{fmt} records written to the file"
    if modulation is not None:
        steps.append(_mdmctrl_read(serial, modulation))
        state += f"; {modulation} modulation used"
    return _case(serial, case_id, name, steps, state, capture_dir=True)


# ---------------------------------------------------------------------------
# ubertooth-ego -r
#
# Receives on -c <MHz> until signalled; each packet prints
# "time=<clk> delta_t=<ms> ms freq=<MHz> " and a line of -l hex bytes. With no
# E-GO board in range nothing is printed. -a sets the access code; a value
# that is not 2, 4, 6 or 8 hex digits is rejected with exit 1.

_EGO_PACKET = re.compile(r"^time=\d+ delta_t=-?\d+\.\d{6} ms freq=(?P<f>\d+) ?$")


def ego_check(frequency, length):
    def check(output):
        problems = []
        # The data bytes of a packet are on the line after its header, so
        # each line is judged against the header before it.
        header = None
        for line in text_lines(output):
            if header is not None:
                data = line.split()
                if len(data) != length:
                    problems.append(f"a packet with {len(data)} bytes, not {length}")
                    break
                header = None
            match = _EGO_PACKET.match(line)
            if match is None:
                continue
            if int(match["f"]) != frequency:
                problems.append(f"a packet on {match['f']} MHz, not {frequency}")
                break
            header = match
        if header is not None and not problems:
            # A header with no line after it: no data bytes at all.
            problems.append(f"a packet with 0 bytes, not {length}")
        return "; ".join(problems)
    return check


def _ego_case(serial, seconds, case_id, name, options, *, frequency=2440, length=18):
    return _case(serial, case_id, name, (
        Step("receive", name,
             ("ubertooth-ego", "-U", serial, "-r", "-c", str(frequency)) + tuple(options),
             timeout_s=float(seconds), mutates=True, continuous=True,
             output_check=ego_check(frequency, length)),
        _stop(serial),
    ), f"receives on {frequency} MHz until stopped")


# ---------------------------------------------------------------------------
# Stale packets across a mode change (SqueakyFloors/ubertooth#318)
#
# A capture holds only packets from the mode it started. ubertooth-btle -n
# reads its packets with UBERTOOTH_POLL control requests, not the bulk IN
# endpoint ubertooth-dump reads, so the two read paths meet only at a mode
# change. The case starts with one bounded btle -n, then repeats
# STALE_ITERATIONS times: a bounded dump -d (it follows a btle), stop, a
# bounded btle -n -A37 -q (it follows that dump), stop. Every stream is
# stopped with SIGTERM. stalecheck.py then counts, per iteration, the dump
# records left from before the dump started (the clock restarts at mode
# entry; LE records are never basic-rate symbols) and the -q packets on any
# RF channel but 0 (advertising index 37), and the step requires none.

STALE_ITERATIONS = 5
_STALE_CHECKER = str(Path(stalecheck.__file__).resolve())


def _stale_case(serial, seconds, iterations=STALE_ITERATIONS):
    def btle(step_id, name, capture=None):
        def argv(context):
            extra = ("-q", _path(capture)(context)) if capture else ()
            return ("ubertooth-btle", "-U", serial, "-n", "-A37") + extra
        return Step(step_id, name, argv, timeout_s=float(seconds), mutates=True,
                    continuous=True)

    def dump(step_id, name, file_name):
        return Step(step_id, name,
                    lambda context: ("ubertooth-dump", "-U", serial, "-d",
                                     _path(file_name)(context)),
                    timeout_s=float(seconds), mutates=True, continuous=True,
                    output_patterns=(r"rx block timestamp \d+",))

    dumps = [f"stale-{i}.bin" for i in range(1, iterations + 1)]
    captures = [f"stale-{i}.pcap" for i in range(1, iterations + 1)]
    steps = [btle("ble-0", "BLE sniff (-n -A37) before the first dump"),
             _stop(serial, "stop-ble-0")]
    for i, (dump_file, capture) in enumerate(zip(dumps, captures), 1):
        steps += [
            dump(f"dump-{i}", f"Iteration {i}: dump -d after BLE", dump_file),
            _stop(serial, f"stop-dump-{i}"),
            btle(f"ble-{i}", f"Iteration {i}: BLE -n -A37 -q after the dump", capture),
            _stop(serial, f"stop-ble-{i}"),
        ]
    steps.append(Step(
        "verify-no-stale",
        f"Count packets left from the previous mode in {iterations} iterations",
        lambda context: (sys.executable, _STALE_CHECKER, "--dumps",
                         *(_path(f)(context) for f in dumps), "--captures",
                         *(_path(f)(context) for f in captures), "--channel", "0"),
        observe=Observation(
            "stale iterations", r"stale iterations: (?P<value>BR dump \d+/\d+, "
            r"BLE capture \d+/\d+)",
            f"BR dump 0/{iterations}, BLE capture 0/{iterations}"),
    ))
    # btle -n mode entry clears CRC verification (reset_le()), so the case
    # records and restores it as the BLE CRC capture cases do.
    return StateCase(
        "stale-packets-across-mode-change",
        f"No packets from the previous mode, BLE and BR alternated {iterations} times",
        (_stop(serial, "establish-idle", "Establish idle before reading"),
         _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"))
        + _read_originals(serial, MODE_RESET_SETTINGS) + tuple(steps),
        _cleanup(serial, MODE_RESET_SETTINGS, crc=True),
        expected_state=(
            f"every dump holds only its own records and every -q capture only "
            f"channel 37 packets, in all {iterations} iterations (per-iteration "
            "counts recorded); CRC, channel, squelch, and LEDs restored"),
        capture_dir=True,
    )


def raw_receive_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Raw receive")
    n = seconds_arg(arguments)
    cases = [
        _dump_case(serial, n, "dump-full", "Full records to a file (-d)", (), "full"),
        _dump_case(serial, n, "dump-bitstream", "Bitstream to a file (-b -d)", ("-b",), "bits"),
        _ego_case(serial, n, "ego-rx", "E-GO continuous receive (-r -c 2440)", ()),
    ]
    if arguments["coverage"] == "comprehensive":
        cases += [
            _dump_case(serial, n, "dump-le", "LE modulation (-l)", ("-l",), "full",
                       modulation="LE"),
            # Each modulation option on its own, now that the two together
            # are refused: a capture uses one demodulator, so -c and -l in
            # one command line are no longer the last one with the other
            # dropped (fork d3429234, #650). The refusal itself needs no
            # board and is in Refused command lines.
            _dump_case(serial, n, "dump-classic", "Basic rate modulation (-c)",
                       ("-c",), "full", modulation="basic rate"),
            _ego_case(serial, n, "ego-length-1", "E-GO capture length -l 1", ("-l", "1"),
                      length=1),
            _ego_case(serial, n, "ego-length-48", "E-GO capture length -l 48", ("-l", "48"),
                      length=48),
            _ego_case(serial, n, "ego-access-code", "E-GO access code -a 630f9ffe",
                      ("-a", "630f9ffe")),
            _case(serial, "ego-reject-bad-access-code", "E-GO rejects -a zz", (
                Step("reject", "ubertooth-ego -r -a zz",
                     ("ubertooth-ego", "-U", serial, "-r", "-c", "2440", "-a", "zz"),
                     expected_returncodes=(1,),
                     output_patterns=(
                         r"error: access code must be 2, 4, 6 or 8 hex digits \(got zz\)",),
                     mutates=True),
            ), "rejected with exit 1 before any mode starts"),
            # SqueakyFloors/ubertooth#318, fixed in fork 9e31dd5 and ed30131.
            _stale_case(serial, n),
        ]
    return tuple(cases)


# ---------------------------------------------------------------------------
# ubertooth-rx -i (Raw receive replay)
#
# -i FILE decodes a saved capture instead of opening a device (no -U, no USB):
# stream_rx_file() feeds each whole 68-byte record (rxdump.py's "full"
# format) to cb_rx, with systime set to the record's host time (fork a55ac89,
# SqueakyFloors/ubertooth#202). At the end of the file main() exits 0; a read
# error ("error: reading <file> failed: <reason>", for example a directory) or
# a last record cut short ("error: <file> ends in a partial record: N of 68
# bytes, ignored") is reported on stderr after whatever was decoded, exit 1
# (#203). An -i file fopen() cannot open prints "error: <name>: <strerror>",
# exit 1 (fork 5ac22a8, #350); so does a -d file it cannot open, before any
# record is decoded or device opened. -u without -l is refused before the
# input or any device is used: "error: -u <UAP> requires -l <LAP>", exit 1
# (fork 57469f2, #236). The other options work as live: -l/-u, -z, -e,
# -r/-q/-d.
# The fixture is synthetic (rxreplay.py), never an over-the-air capture.

_REPLAY = str(Path(rxreplay.__file__).resolve())
_FIXTURE = "replay.bin"
_A, _B = f"{rxreplay.LAP_A:06x}", f"{rxreplay.LAP_B:06x}"
# libbtbb's UAP and CLK6 progress lines; with the fixture's clocks the
# receiver clock is the central's clock, so the CLK1-6 offset is 0. Which of
# the two notes the address-part search prints is decided by BTBB_UAP_VALID:
# the CLK6 note where the flag was already set, the UAP note where this
# search is what sets it (btbb_uap_from_header). On a command line that
# states no UAP that means the UAP note, and only that, because the flag is
# never set when the search runs: a settled UAP sets CLK6_VALID with it, so
# the search is not asked again, and the one thing that clears CLK6_VALID is
# reset(), which clears BTBB_UAP_VALID too and whose keep_stated_uap() puts
# back only a UAP the caller stated. So the forbidden pattern below cannot
# fire on a discovery restart either; what would print it again is a library
# that went back to re-running the search after a UAP it found itself.
_CLK6_ZERO = r"CLK6 = 0x0 found after \d+ total packets"
# Any clock that search reports, whatever its value: what replay-lap-uap
# forbids, where the note it must print is the UAP one.
_CLK6_FOUND = r"CLK6 = 0x[0-9a-f]+ found after"
# The CLK1-27 search choosing its candidates, which is where a settled
# CLK1-6 leads next.
_CLK27_CANDIDATES = r"\d+ initial CLK1-27 candidates"
_ANY_FOUND = r"(?:UAP|CLK6) = 0x[0-9a-f]+ found after"
_UAP_NEEDS_LAP = r"error: -u <UAP> requires -l <LAP>"


def _fixture_step(base_time, variant="full"):
    return Step(
        "fixture",
        f"Write the synthetic {variant} replay fixture",
        lambda context: (sys.executable, _REPLAY, "fixture", _path(_FIXTURE)(context),
                         "--variant", variant, "--base-time", str(base_time)),
        output_patterns=(rf"Replay fixture: {variant}, ",),
    )


def _replay_step(name, options, *, check=None, patterns=(), forbidden=(),
                 files=(), returncodes=(0,), source=_FIXTURE):
    def argv(context):
        extra = []
        for option, file_name in files:
            extra += [option, _path(file_name)(context)]
        return ("ubertooth-rx", "-i", _path(source)(context)) + tuple(options) + tuple(extra)

    return Step(
        "replay", name, argv, timeout_s=20.0, expected_returncodes=returncodes,
        output_check=check, output_patterns=patterns, forbidden_patterns=forbidden,
    )


def _replay_case(case_id, name, steps, expected_state, *, needs=NO_NEEDS,
                 remove=()):
    # A name the case made that the run must not open when it measures the
    # files the case wrote -- a link to a standard stream -- is taken back
    # first, so it goes whether the case passed or failed.
    cleanup = ()
    if remove:
        cleanup += (Step("remove-links", "Remove " + ", ".join(remove),
                         lambda context: (sys.executable, _STDOUTDEST, "unlink",
                                          require(context, CAPTURE_DIR_KEY),
                                          *remove),
                         # Of how many it was asked for, which a case
                         # cancelled before it made any can still satisfy:
                         # a cleanup failure leaves the case unverified,
                         # which stops the whole run, and there is no device
                         # state here to lose. So this says the helper did
                         # its work and not that a link had to be there; a
                         # link left behind is caught after the cleanup, by
                         # the run measuring the files the case wrote, which
                         # cannot read a standard stream.
                         output_patterns=(rf"Removed: \d+ of {len(remove)} "
                                          r"name",)),)
    return StateCase(
        case_id,
        name,
        tuple(steps),
        # Nothing on a device to restore; list what the case wrote (names and
        # sizes only) so the record shows the files were left in the run.
        cleanup + (Step("list-files", "List the files the case wrote",
              lambda context: (sys.executable, _REPLAY, "files",
                               require(context, CAPTURE_DIR_KEY)),
              output_patterns=(r"Replay files: \d+ in ",)),),
        expected_state=expected_state,
        capture_dir=True,
        needs=needs,
    )


def _capture_packets(step_id, name, file_name, header_format, packets):
    return Step(
        step_id,
        name,
        lambda context: (sys.executable, _CAPTURE_CHECKER, "--packets",
                         _path(file_name)(context)),
        output_patterns=(
            rf"Capture format: {header_format}, link type: {LINKTYPE_BREDR}, "
            rf"packets: {packets}$",
        ),
    )


# What a replay of the full fixture, filtered to LAP A, has to hold.
#
# The record count is the fixture's own: the harness wrote the input, so
# every packet of that LAP is one record. The rest are floors of one,
# because what they are for is making a check fire at all. On live traffic a
# payload-bearing record turns up about 11 times in 2620 and a settled
# address part about one run in nine, so the checks that read a payload's own
# length field, recompute its CRC24, and hold a record's transport nibble to
# the payload it names are checks waiting for a record; one record each is
# what sets them going (ubertooth-test-harness#339, #344). How many of the
# eight this libbtbb settles a UAP for, and from which one, is the library's
# own business and would make a floor of four a check on the library's
# bookkeeping rather than on the fields; `--settling-record` is what asks
# about the record that matters.
_LAP_A_RECORDS = 8
# The DH1 the fixture carries, as libbtbb names the type.
_PAYLOAD_TYPE = "DH1/2-DH1"


def _record_fields(step_id, name, file_name, *, following=False):
    """Read every record of a filtered replay capture, field by field."""
    def argv(context):
        options = (
            "--lap", f"{rxreplay.LAP_A:06x}",
            "--reference-lap", f"{rxreplay.LAP_A:06x}",
            "--reference-uap", f"{rxreplay.UAP_A:02x}",
            "--min-packets", str(_LAP_A_RECORDS),
            "--min-reference-uap", "1",
            "--min-hec-valid", "1",
            "--min-with-payload", "1",
            "--min-crc-valid", "1",
            "--require-types", _PAYLOAD_TYPE,
            "--settling-record",
            "--signal-dbm", str(rxreplay.SIGNAL_DBM),
            "--no-measured-noise",
        )
        if following:
            # A PCAPNG records the address the capture was following; this
            # command line states a LAP and no UAP, so the mask is empty.
            options += ("--following-lap", f"{rxreplay.LAP_A:06x}",
                        "--following-uap-mask", "0")
        return (sys.executable, _RECORD_CHECKER,
                _path(file_name)(context)) + options

    return Step(
        step_id, name, argv, timeout_s=30.0,
        output_patterns=(
            rf"(?m)^BR/EDR records: {_LAP_A_RECORDS} in ",
            r"(?m)^BR/EDR payloads: [1-9]\d* records carry one, ",
            # The transport the payload earns, which is the field a libbtbb
            # that stopped naming one would leave empty (#339).
            r"(?m)^BR/EDR transports: ACL \([1-9]\d*\)",
            rf"(?m)^BR/EDR signal: {rxreplay.SIGNAL_DBM} to "
            rf"{rxreplay.SIGNAL_DBM} dBm$",
            r"(?m)^BR/EDR noise: no record reports a measured figure$",
        ),
        forbidden_patterns=(r"(?m)^BR/EDR record problem: ",),
    )


# --- where a replay's capture goes, and the pairs that are one file ----------
#
# A capture option's value may be standard output: the documented "-", a path
# that names this process's descriptor 1 ("/dev/stdout", "/dev/fd/1", and
# "/proc/self/fd/1" on Linux), or a symbolic link whose chain ends at one of
# those (ubertooth(7), OPTION VALUES). The capture then owns the stream: the
# tool's own standard output goes to the null device for the rest of the run
# (opt_parse.c, take_stdout), so no packet report can land in the middle of a
# capture, while a diagnostic still reaches standard error. "/dev/fd/N" for
# any other N is not that stream but whatever the caller opened there, so the
# capture goes to that file and the report stays on standard output.
#
# Two destinations that are one file are refused before either is opened,
# naming both options ("error: -r and -q cannot both write to <file>", or
# "... to one file (<first> and <second>)" where the two names differ). The
# file is identified rather than the name, so a second spelling, a symbolic
# link, a hard link, and standard output pointed at a named file are all
# caught; "/dev/null" twice is not, since nothing is kept there. A destination
# that is the dump "-i" is replaying is refused the same way
# ("error: -q would write over the dump -i is replaying (<name>)"), and the
# one redirection no option names -- the decode itself going into that dump --
# is one warning line, the shell having already opened the file (fork #1203).
#
# Each case states the whole directory afterwards, with the input's SHA-256,
# so a refusal that truncated or rewrote the file it was reading fails.

_STDOUTDEST = str(Path(stdoutdest.__file__).resolve())
# What a capture written to standard output must not hold, and what a run that
# gave the stream away must not print: the packet report.
_PACKET_LINE_PATTERN = r"(?m)^systime=\d+ ch=\s*\d+ LAP=[0-9a-f]{6} err=\d+ "
_STDOUT_PATHS = ("/dev/stdout", "/dev/fd/1")
_PROC_STDOUT_PATH = "/proc/self/fd/1"


def _dest_run_step(step_id, name, options, *, stdout=None, append=False, fds=(),
                   returncodes=(0,), patterns=(), forbidden=(), check=None,
                   source=_FIXTURE):
    """One ubertooth-rx replay run with chosen descriptors (stdoutdest.py).

    ``stdout`` names a file in the case directory to put the tool's standard
    output on; without one the tool inherits the harness's, so the usual
    output checks read its report. ``fds`` are (number, file name) pairs, or
    (number, "stdout") for a descriptor the caller pointed at the stream
    itself. Standard error is always the harness's.
    """
    def argv(context):
        where = []
        if stdout is not None:
            where += ["--stdout", _path(stdout)(context)]
            if append:
                where.append("--append")
        for number, target in fds:
            value = target if target == "stdout" else _path(target)(context)
            where += ["--fd", f"{number}={value}"]
        # A plain word is passed as written; ("file", NAME) is a path in the
        # case's own directory, which only the run knows, and ("spelling",
        # SUFFIX) is that directory with SUFFIX appended as text, so a
        # redundant "/./" survives to the tool (Path would drop it).
        words = []
        for word in options:
            if not isinstance(word, tuple):
                words.append(word)
            elif word[0] == "spelling":
                words.append(require(context, CAPTURE_DIR_KEY) + word[1])
            else:
                words.append(_path(word[1])(context))
        words = tuple(words)
        return ((sys.executable, _STDOUTDEST, "run") + tuple(where)
                + ("--", "ubertooth-rx", "-i", _path(source)(context)) + words)

    return Step(
        step_id, name, argv, timeout_s=20.0,
        expected_returncodes=tuple(returncodes),
        output_patterns=tuple(patterns), forbidden_patterns=tuple(forbidden),
        output_check=check,
    )


def _dest_seed_step(names) -> Step:
    return Step(
        "seed", "Seed " + ", ".join(names) + " with known contents",
        lambda context: (sys.executable, _STDOUTDEST, "seed",
                         require(context, CAPTURE_DIR_KEY), *names),
        output_patterns=(rf"Seeded: {len(names)} file",),
    )


def _dest_link_step(symlinks=(), hardlinks=()) -> Step:
    what = [f"{name} -> {target}" for name, target in symlinks]
    what += [f"{name} as another name for {target}" for name, target in hardlinks]

    def argv(context):
        words = []
        for name, target in symlinks:
            words += ["--symlink", f"{name}={target}"]
        for name, target in hardlinks:
            words += ["--hardlink", f"{name}={target}"]
        return ((sys.executable, _STDOUTDEST, "link",
                 require(context, CAPTURE_DIR_KEY)) + tuple(words))

    return Step("link", "Link " + "; ".join(what), argv,
                output_patterns=(rf"Linked: {len(symlinks)} symbolic, "
                                 rf"{len(hardlinks)} hard,",))


def _dest_check_step(name, *, unchanged=(), prefix=(), absent=(), kept=(),
                     symlinks=(), hardlinks=(), also=(),
                     step_id="verify-directory") -> Step:
    def argv(context):
        words = []
        for file_name, digest in unchanged:
            words += ["--sha256", f"{file_name}={digest}"]
        for file_name, digest, length, added in prefix:
            words += ["--prefix", f"{file_name}={digest}:{length}:{added}"]
        for file_name in absent:
            words += ["--absent", file_name]
        for file_name in kept:
            words += ["--kept", file_name]
        for file_name, target in symlinks:
            words += ["--symlink", f"{file_name}={target}"]
        for file_name, target in hardlinks:
            words += ["--hardlink", f"{file_name}={target}"]
        for file_name in also:
            words += ["--also", file_name]
        return ((sys.executable, _STDOUTDEST, "check",
                 require(context, CAPTURE_DIR_KEY)) + tuple(words))

    return Step(step_id, name, argv,
                output_patterns=(r"Stdout destination check: ",))


_PCAPNG_NOT_STDOUT = (r"(?m)^error: PCAPNG capture to stdout is not supported; "
                      r"use -q -$")
# Every spelling of that stream -r is given, as the user writes it: the
# documented "-", the two fixed names for descriptor 1, and a link to one.
_PCAPNG_STDOUT_WAYS = (
    ("-", "-"), ("/dev/stdout", "/dev/stdout"), ("/dev/fd/1", "/dev/fd/1"),
    ("<dir>/to-stdout", ("file", "to-stdout")),
)
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _one_file(options: tuple[str, str], first: str, second: str, *,
              first_is_text=False, second_is_text=False) -> str:
    """The refusal for two destinations that are one file under two names.

    ``options`` are the two capture options as the refusal names them, which
    is the order ubertooth-rx.c lists its destinations in (-r, -q, -d) and
    not the order they were written. Each name is the destination as the user
    wrote it, which for a file in the case's own directory is an absolute
    path only the run knows, so the pattern allows whatever leads up to the
    name; a name that is not a path ("standard output", "/dev/fd/3") is
    matched as it stands.
    """
    def text(name, as_written):
        return re.escape(name) if as_written else rf"\S*{re.escape(name)}"

    return (rf"(?m)^error: {re.escape(options[0])} and {re.escape(options[1])} "
            r"cannot both write to one file \("
            + text(first, first_is_text) + " and "
            + text(second, second_is_text) + r"\)$")


def _both_stdout(options: tuple[str, str]) -> str:
    """The refusal for two captures that both ask for standard output."""
    return (rf"(?m)^error: {re.escape(options[0])} and {re.escape(options[1])} "
            r"cannot both write to standard output$")


def _over_input(option: str, named: str) -> str:
    """The refusal for a destination that is the dump being replayed.

    ``named`` is the destination as the user wrote it, which for a file in
    the case's own directory is an absolute path only the run knows: the
    pattern allows whatever leads up to the name.
    """
    text = (re.escape(named) if named == "standard output"
            else rf"\S*{re.escape(named)}")
    return (rf"(?m)^error: {re.escape(option)} would write over the dump -i is "
            rf"replaying \({text}\)$")


def _stdout_destination_cases(base, count, digest):
    """Smoke: the stream a capture is given, and the pairs that are one file."""
    fixture = (_FIXTURE, digest)
    cases = [
        # The capture owns standard output, so the packet report is printed
        # nowhere at all: not into the capture, and not on standard error
        # either, which the forbidden pattern covers for both streams.
        _replay_case("replay-stdout-dash", "-i -q - writes the capture to stdout", (
            _fixture_step(base),
            _dest_run_step("capture", "ubertooth-rx -i <fixture> -q - > stdout.pcap",
                           ("-q", "-"), stdout="stdout.pcap",
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _capture_packets("verify-capture", "Verify the stdout capture",
                             "stdout.pcap", "pcap", count),
            _dest_check_step("Verify the fixture came through unchanged",
                             unchanged=(fixture,), also=("stdout.pcap",)),
        ), f"exit 0; pcap, link type 255, {count} packets on stdout; no packet "
           "line on either stream; the fixture unchanged"),
        # The paths that name descriptor 1 mean the same stream as "-".
        _replay_case("replay-stdout-paths",
                     "-i -q /dev/stdout and /dev/fd/1 are that stream too", (
            _fixture_step(base),
        ) + tuple(
            step
            for index, path in enumerate(_STDOUT_PATHS)
            for step in (
                _dest_run_step(f"capture-{index}",
                               f"ubertooth-rx -i <fixture> -q {path} > {index}.pcap",
                               ("-q", path), stdout=f"{index}.pcap",
                               forbidden=(_PACKET_LINE_PATTERN,)),
                _capture_packets(f"verify-capture-{index}",
                                 f"Verify the capture {path} wrote",
                                 f"{index}.pcap", "pcap", count),
            )
        ) + (
            _dest_check_step("Verify the fixture came through unchanged",
                             unchanged=(fixture,),
                             also=tuple(f"{index}.pcap"
                                        for index in range(len(_STDOUT_PATHS)))),
        ), f"each path writes pcap, link type 255, {count} packets to the "
           "redirected stream; no packet line on either stream"),
        # Linux names descriptor 1 here as well; macOS has no /proc, where
        # the path is an ordinary name and nothing creates it.
        _replay_case("replay-stdout-proc-path",
                     f"-i -q {_PROC_STDOUT_PATH} is that stream on Linux", (
            _fixture_step(base),
            _dest_run_step("capture",
                           f"ubertooth-rx -i <fixture> -q {_PROC_STDOUT_PATH} "
                           "> proc.pcap", ("-q", _PROC_STDOUT_PATH),
                           stdout="proc.pcap",
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _capture_packets("verify-capture", "Verify the capture it wrote",
                             "proc.pcap", "pcap", count),
            _dest_check_step("Verify the fixture came through unchanged",
                             unchanged=(fixture,), also=("proc.pcap",)),
        ), f"pcap, link type 255, {count} packets on the redirected stream; no "
           "packet line on either stream", needs=Needs(platforms=("linux",))),
        # Another descriptor is whatever the caller opened on it, so the
        # capture goes there and the report keeps standard output.
        _replay_case("replay-stdout-other-fd",
                     "-i -q /dev/fd/3 is the caller's file, not stdout", (
            _fixture_step(base),
            _dest_run_step("capture", "ubertooth-rx -i <fixture> -q /dev/fd/3 "
                           "(3 on fd3.pcap)", ("-q", "/dev/fd/3"),
                           fds=((3, "fd3.pcap"),),
                           check=rxreplay.packets_check(
                               rxreplay.expected_packets())),
            _capture_packets("verify-capture", "Verify the file descriptor 3 held",
                             "fd3.pcap", "pcap", count),
            _dest_check_step("Verify the fixture came through unchanged",
                             unchanged=(fixture,), also=("fd3.pcap",)),
        ), f"exit 0; the {count} packet lines still on stdout; pcap, link type "
           f"255, {count} packets in the file descriptor 3 held"),
        # Two destinations, one file: refused before either is opened.
        _replay_case("replay-destinations-collide",
                     "-i -q and -r on one file is refused", (
            _fixture_step(base),
            _dest_seed_step(("collide.pcap",)),
            _dest_run_step("refuse", "ubertooth-rx -i <fixture> -q <collide> "
                           "-r <collide>",
                           ("-q", ("file", "collide.pcap"),
                            "-r", ("file", "collide.pcap")),
                           returncodes=(1,),
                           patterns=(r"(?m)^error: -r and -q cannot both write to "
                                     r"\S*collide\.pcap$",),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _dest_check_step("Verify both files are as they were",
                             unchanged=(fixture,), kept=("collide.pcap",)),
        ), "refused (exit 1) naming -r and -q; collide.pcap unchanged; the "
           "fixture unchanged; no packet decoded"),
        # A destination that is the dump being replayed.
        _replay_case("replay-destination-is-input",
                     "-i -d naming the dump being replayed is refused", (
            _fixture_step(base),
            _dest_run_step("refuse", "ubertooth-rx -i <fixture> -d <fixture>",
                           ("-d", ("file", _FIXTURE)), returncodes=(1,),
                           patterns=(_over_input("-d", _FIXTURE),),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _dest_check_step("Verify the fixture is byte for byte as it was",
                             unchanged=(fixture,)),
        ), "refused (exit 1) naming -d and -i; the fixture unchanged and still "
           "the only file; no packet decoded"),
    ]
    return cases


def _stdout_destination_comprehensive(base, count, digest, size):
    """Comprehensive: the other spellings, the links, and the one warning."""
    fixture = (_FIXTURE, digest)
    return [
        # The file is identified rather than the name, and the one pair that
        # keeps nothing is allowed.
        _replay_case("replay-destinations-spelled-differently",
                     "-i: one file under two names is refused, /dev/null twice "
                     "is not", (
            _fixture_step(base),
            _dest_run_step("refuse", "ubertooth-rx -i <fixture> -q <dir>/cap.pcap "
                           "-d <dir>/./cap.pcap",
                           ("-q", ("file", "cap.pcap"),
                            "-d", ("spelling", "/./cap.pcap")),
                           returncodes=(1,),
                           patterns=(r"(?m)^error: -q and -d cannot both write to "
                                     r"one file \(\S*/cap\.pcap and "
                                     r"\S*/\./cap\.pcap\)$",),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _dest_run_step("null-twice",
                           "ubertooth-rx -i <fixture> -q /dev/null -d /dev/null",
                           ("-q", "/dev/null", "-d", "/dev/null"),
                           check=rxreplay.packets_check(
                               rxreplay.expected_packets())),
            _dest_check_step("Verify nothing was created", unchanged=(fixture,),
                             absent=("cap.pcap",)),
        ), "the pair under two spellings refused (exit 1) naming both; "
           "/dev/null twice accepted and decoded; cap.pcap never created"),
        # A writer writes through a link, and a hard link is the same file.
        _replay_case("replay-destination-links-to-input",
                     "-i: a symbolic and a hard link to the dump are refused", (
            _fixture_step(base),
            _dest_link_step(symlinks=(("link.bin", _FIXTURE),),
                            hardlinks=(("hard.bin", _FIXTURE),)),
            _dest_run_step("refuse-symlink",
                           "ubertooth-rx -i <fixture> -d <dir>/link.bin",
                           ("-d", ("file", "link.bin")), returncodes=(1,),
                           patterns=(_over_input("-d", "link.bin"),),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _dest_run_step("refuse-hardlink",
                           "ubertooth-rx -i <fixture> -q <dir>/hard.bin",
                           ("-q", ("file", "hard.bin")), returncodes=(1,),
                           patterns=(_over_input("-q", "hard.bin"),),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _dest_check_step("Verify the fixture and both links are as they were",
                             unchanged=(fixture,),
                             symlinks=(("link.bin", _FIXTURE),),
                             hardlinks=(("hard.bin", _FIXTURE),)),
        ), "both refused (exit 1) naming the link the user wrote; the fixture "
           "unchanged; the links still what they were"),
        # The file is identified and not the name for the destination pair
        # check too, so a link, a hard link, and a descriptor beside the file
        # it holds are each one file under two names.
        _replay_case("replay-destinations-one-file-by-link",
                     "-i: a link, a hard link, and a descriptor beside the file "
                     "it holds", (
            _fixture_step(base),
            _dest_seed_step(("pair.pcap",)),
            _dest_link_step(symlinks=(("pair.link", "pair.pcap"),),
                            hardlinks=(("pair.hard", "pair.pcap"),)),
            _dest_run_step("refuse-symlink",
                           "ubertooth-rx -i <fixture> -q <pair.pcap> -d <pair.link>",
                           ("-q", ("file", "pair.pcap"), "-d", ("file", "pair.link")),
                           returncodes=(1,), patterns=(_one_file(("-q", "-d"), "pair.pcap",
                                                                 "pair.link"),),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _dest_run_step("refuse-hardlink",
                           "ubertooth-rx -i <fixture> -q <pair.pcap> -d <pair.hard>",
                           ("-q", ("file", "pair.pcap"), "-d", ("file", "pair.hard")),
                           returncodes=(1,), patterns=(_one_file(("-q", "-d"), "pair.pcap",
                                                                 "pair.hard"),),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            # "-" beside the name the shell pointed that stream at.
            _dest_run_step("refuse-stdout-and-file",
                           "ubertooth-rx -i <fixture> -q - -d <shared.bin>, with "
                           "stdout on shared.bin",
                           ("-q", "-", "-d", ("file", "shared.bin")),
                           stdout="shared.bin", returncodes=(1,),
                           patterns=(_one_file(("-q", "-d"), "standard output",
                                               "shared.bin", first_is_text=True),),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            # /dev/fd/N beside the name the caller opened on N. The tool
            # asks the descriptor rather than the path (path_file() ->
            # named_descriptor() -> fstat), so this holds on either
            # platform, which looking the path up would not: on macOS
            # stat("/dev/fd/3") reports the descriptor filesystem's own
            # device number and not the file's.
            _dest_run_step("refuse-descriptor-and-file",
                           "ubertooth-rx -i <fixture> -q <fd3.bin> -d /dev/fd/3, "
                           "with 3 on fd3.bin",
                           ("-q", ("file", "fd3.bin"), "-d", "/dev/fd/3"),
                           fds=((3, "fd3.bin"),), returncodes=(1,),
                           patterns=(_one_file(("-q", "-d"), "fd3.bin", "/dev/fd/3",
                                               second_is_text=True),),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            # Two captures that both ask for that stream belong to the
            # one-capture-on-standard-output rule instead, which refuses the
            # second however it is spelled, a link to the stream included.
            _dest_run_step("refuse-both-stdout",
                           "ubertooth-rx -i <fixture> -q - -d /dev/stdout, with "
                           "stdout on both.bin",
                           ("-q", "-", "-d", "/dev/stdout"), stdout="both.bin",
                           returncodes=(1,),
                           patterns=(_both_stdout(("-q", "-d")),),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _dest_check_step("Verify nothing was written to any of them",
                             unchanged=(fixture, ("shared.bin", _EMPTY_SHA256),
                                        ("fd3.bin", _EMPTY_SHA256),
                                        ("both.bin", _EMPTY_SHA256)),
                             kept=("pair.pcap",),
                             symlinks=(("pair.link", "pair.pcap"),),
                             hardlinks=(("pair.hard", "pair.pcap"),)),
        ), "each pair refused (exit 1) naming both options and both names as "
           "written, and two captures both asking for standard output refused "
           "by that rule instead; the seeded file unchanged, the two the run "
           "opened still empty, the links still what they were"),
        # PcapNG seeks back over what it has written, which no stream allows,
        # so -r refuses every destination that reaches standard output while
        # the command line is read, a link to it included.
        _replay_case("replay-pcapng-not-stdout",
                     "-i -r refuses every destination that reaches stdout", (
            _fixture_step(base),
            _dest_link_step(symlinks=(("to-stdout", "/dev/stdout"),)),
        ) + tuple(
            _dest_run_step(f"refuse-{index}",
                           f"ubertooth-rx -i <fixture> -r {shown}, with stdout "
                           f"on {index}.out",
                           ("-r", value), stdout=f"{index}.out",
                           returncodes=(1,),
                           patterns=(_PCAPNG_NOT_STDOUT,),
                           forbidden=(_PACKET_LINE_PATTERN,))
            for index, (shown, value) in enumerate(_PCAPNG_STDOUT_WAYS)
        ) + (
            # Each refusal's own standard output has to be empty: a run that
            # wrote a PCAPNG header before refusing would be a capture in the
            # run record, which is the thing these destinations are about.
            _dest_check_step("Verify nothing was created or written",
                             unchanged=((_FIXTURE, digest),)
                                       + tuple((f"{index}.out", _EMPTY_SHA256)
                                               for index in
                                               range(len(_PCAPNG_STDOUT_WAYS))),
                             symlinks=(("to-stdout", "/dev/stdout"),)),
        ), "each refused (exit 1) with 'PCAPNG capture to stdout is not "
           "supported; use -q -'; nothing created and the link left alone",
           remove=("to-stdout",)),
        # Standard output pointed at the dump: a capture option is refused,
        # and the decode, which no option names, is one warning line.
        _replay_case("replay-stdout-is-input",
                     "-i with stdout on the dump: -q refused, the decode warned "
                     "about", (
            _fixture_step(base),
            _dest_run_step("refuse", "ubertooth-rx -i <fixture> -q - >> <fixture>",
                           ("-q", "-"), stdout=_FIXTURE, append=True,
                           returncodes=(1,),
                           patterns=(_over_input("-q", "standard output"),),
                           forbidden=(_PACKET_LINE_PATTERN,)),
            _dest_check_step("Verify the refusal added nothing to the fixture",
                             unchanged=(fixture,), step_id="verify-refusal"),
            _dest_run_step("warn", "ubertooth-rx -i <fixture> >> <fixture>",
                           (), stdout=_FIXTURE, append=True,
                           patterns=(r"(?m)^warning: the decode is going to the "
                                     r"dump -i is replaying, so the capture is "
                                     r"lost$",)),
            _dest_check_step("Verify the decode was appended to the fixture",
                             prefix=((_FIXTURE, digest, size, 500),),
                             step_id="verify-warning"),
        ), "the capture option refused (exit 1) with the fixture untouched; the "
           "bare decode exits 0 with one warning line and its records still at "
           "the start of the file"),
    ]


def replay_cases(arguments, _resources, _resource_identities, *, now=None):
    # Record host times lie an hour before the cases are built: inside
    # rxdump.py's window around the file time, and distinct from the replay.
    base = int(time.time() if now is None else now) - rxreplay.FIXTURE_AGE_S
    full = rxreplay.expected_packets()
    lap_a = rxreplay.expected_packets(lap=rxreplay.LAP_A)
    lap_b = rxreplay.expected_packets(lap=rxreplay.LAP_B)
    count = len(full)
    cases = [
        _replay_case("replay-decode-lap", "-i decodes every LAP in the fixture", (
            _fixture_step(base),
            _replay_step("ubertooth-rx -i <fixture>", (),
                         check=rxreplay.uap_check(full),
                         forbidden=(rxreplay.ANY_UAP_NOTE,)),
        ), f"exit 0; {count} packet lines with the fixture's LAPs, errors, and "
           "channels in order; decoy records skipped; no UAP without -l"),
        # The packet whose CRC settles a UAP the library worked out for
        # itself settles CLK1-6 with it, so the clock search starts from that
        # same packet: the UAP note, then the candidates it chose from the
        # hops the UAP search had already remembered (libbtbb 7e1cdf0, #137;
        # the fork took it on in #1135). A CLK6 note on this command line is
        # the behavior before that commit, where the address-part search ran
        # again on every later packet, reported CLK1-6 over and over, and
        # never reached CLK1-27 -- so it is forbidden rather than merely
        # unexpected, and a revert fails this case in CI with no board. The
        # CLK6 note belongs to the command that states the UAP, which
        # replay-uap-given is, and so does the settled CLK1-6 value: this
        # command line prints no figure that names the offset, and the
        # candidate count is matched as a number rather than as the
        # library's own, which would break on any honest change to it.
        _replay_case("replay-lap-uap", "-i -l <LAP> derives the UAP", (
            _fixture_step(base),
            _replay_step(f"ubertooth-rx -i <fixture> -l {_A}", ("-l", _A),
                         check=rxreplay.uap_check(lap_a),
                         patterns=(rxreplay.uap_note(rxreplay.UAP_A),
                                   _CLK27_CANDIDATES),
                         forbidden=(_CLK6_FOUND,)),
        ), f"only LAP {_A} lines; UAP {rxreplay.UAP_A:02x} found and the "
           "CLK1-27 search started from the packet that found it; no CLK6 note"),
        # The one case that reads a capture's records rather than its header
        # and packet count. Its answer is known, which is what the live
        # Classic tests cannot have: `Classic capture record fields` runs the
        # same decoder over the bench's own traffic, where a payload-bearing
        # record turns up about 11 times in 2620 and a settled address part
        # about one run in nine, so the payload, CRC and transport rules there
        # are checks waiting for a record to fire on. Here they fire every
        # run, on a board-free host, so a change to any of them fails in CI.
        _replay_case("replay-lap-records",
                     f"-i -l {_A} -r: every record's fields", (
            _fixture_step(base),
            _replay_step(f"ubertooth-rx -i <fixture> -l {_A} -r", ("-l", _A),
                         check=rxreplay.uap_check(lap_a),
                         patterns=(rxreplay.uap_note(rxreplay.UAP_A),),
                         files=(("-r", "records.pcapng"),)),
            _capture_packets("verify-capture", "Verify the -r file",
                             "records.pcapng", "pcapng", len(lap_a)),
            _record_fields("verify-records", "Read every record of the -r file",
                           "records.pcapng", following=True),
        ), f"pcapng, link type 255, {len(lap_a)} records: all of LAP {_A} with "
           f"that LAP and UAP {rxreplay.UAP_A:02x} as the reference address, "
           "the record that settled the UAP carrying its own header and the "
           "payload whose CRC settled it, that payload's length field and "
           "CRC24 agreeing and its transport named ACL, every record at the "
           f"fixture's {rxreplay.SIGNAL_DBM} dBm with no measured noise, and "
           f"the file naming LAP {_A} with an empty UAP mask as the address "
           "it followed"),
        _replay_case("replay-pcapng", "-i -r replays into PCAPNG", (
            _fixture_step(base),
            _replay_step("ubertooth-rx -i <fixture> -r", (),
                         check=rxreplay.packets_check(full),
                         files=(("-r", "replay.pcapng"),)),
            _capture_packets("verify-capture", "Verify the -r file", "replay.pcapng",
                             "pcapng", count),
        ), f"pcapng, link type 255, {count} packets"),
        _replay_case("replay-pcap", "-i -q replays into PCAP", (
            _fixture_step(base),
            _replay_step("ubertooth-rx -i <fixture> -q", (),
                         check=rxreplay.packets_check(full),
                         files=(("-q", "replay.pcap"),)),
            _capture_packets("verify-capture", "Verify the -q file", "replay.pcap",
                             "pcap", count),
        ), f"pcap, link type 255, {count} packets"),
        # main() opens the -d file before any device or record (fork
        # 5ac22a8, #350), so this needs no board even without -i; -i keeps
        # it off USB if the open were ever moved after the lookup. In smoke
        # so CI's host run covers it: it needs only the built ubertooth-rx.
        _replay_case("replay-dump-unopenable", "-i -d to a file it cannot open", (
            _fixture_step(base),
            _replay_step("ubertooth-rx -i <fixture> -d <missing dir>/x.bin", (),
                         returncodes=(1,), check=rxreplay.packets_check([]),
                         patterns=(r"(?m)^error: .*no-such-dir/x\.bin: "
                                   r"No such file or directory$",),
                         forbidden=(r"ubertooth-rx - passive Classic Bluetooth",),
                         files=(("-d", "no-such-dir/x.bin"),)),
            # The whole directory, not one name: only the fixture, unchanged.
            Step("verify-directory", "Verify the directory holds only the unchanged fixture",
                 lambda context: (sys.executable, _REPLAY, "only-fixture",
                                  require(context, CAPTURE_DIR_KEY), _FIXTURE,
                                  "--base-time", str(base)),
                 output_patterns=(rf"Replay directory: only {re.escape(_FIXTURE)}, "
                                  r"unchanged",)),
        ), "exit 1 with 'error: <file>: No such file or directory', no usage, "
           "no packet decoded, nothing created or changed"),
        # -s states the default sweep (fork 7b46ec4, #361), so the replay is
        # the same with it; -h lists it as the default. In smoke so CI's host
        # run covers it.
        _replay_case("replay-sweep-explicit", "-i -s decodes as without -s", (
            _fixture_step(base),
            _replay_step("ubertooth-rx -i <fixture> -s", ("-s",),
                         check=rxreplay.packets_check(full), forbidden=SWEEP_NOTICES),
            Step("help", "ubertooth-rx -h lists -s as the default sweep",
                 ("ubertooth-rx", "-h"), timeout_s=10.0, output_patterns=(SWEEP_HELP,)),
        ), f"exit 0; the same {count} packet lines; no notice; -h lists -s as the "
           "default"),
    ]
    fixture = rxreplay.fixture_bytes(base)
    digest = hashlib.sha256(fixture).hexdigest()
    cases += _stdout_destination_cases(base, count, digest)
    if arguments["coverage"] != "comprehensive":
        return tuple(cases)
    survey = {
        f"??:??:{rxreplay.UAP_A:02X}:" + ":".join(f"{_A.upper()}"[i:i + 2] for i in (0, 2, 4)),
        "??:??:??:" + ":".join(f"{_B.upper()}"[i:i + 2] for i in (0, 2, 4)),
    }
    truncated = rxreplay.expected_packets("truncated")
    cases += [
        _replay_case("replay-survey", "-i -z lists both piconets", (
            _fixture_step(base),
            _replay_step("ubertooth-rx -i <fixture> -z", ("-z",),
                         check=rxreplay.survey_check(survey),
                         patterns=(SURVEY_HEADING_PATTERN,)),
        ), "the heading on stderr and, on stdout, LAP A with its UAP and "
           "LAP B without one"),
        _replay_case("replay-lap-filter", f"-i -l {_B}: only that LAP", (
            _fixture_step(base),
            _replay_step(f"ubertooth-rx -i <fixture> -l {_B}", ("-l", _B),
                         check=rxreplay.uap_check(lap_b),
                         forbidden=(rxreplay.ANY_UAP_NOTE,)),
        ), f"only LAP {_B} lines (ID packets: no UAP)"),
        _replay_case("replay-uap-given", "-i -l -u with the right UAP", (
            _fixture_step(base),
            _replay_step(f"ubertooth-rx -i <fixture> -l {_A} -u {rxreplay.UAP_A:02x}",
                         ("-l", _A, "-u", f"{rxreplay.UAP_A:02x}"),
                         check=rxreplay.packets_check(lap_a),
                         patterns=(_CLK6_ZERO, _CLK27_CANDIDATES),
                         forbidden=(r"failed to confirm UAP",)),
        ), "UAP confirmed, CLK6 offset 0, hop reversal started"),
        _replay_case("replay-uap-wrong", "-i -l -u with a wrong UAP confirms nothing", (
            _fixture_step(base),
            _replay_step(f"ubertooth-rx -i <fixture> -l {_A} -u 11",
                         ("-l", _A, "-u", "11"),
                         check=rxreplay.packets_check(lap_a),
                         forbidden=(_ANY_FOUND, r"initial CLK1-27 candidates")),
        ), "packet lines only; no UAP or clock reported"),
        _replay_case("replay-ac-errors-0", "-i -e 0 drops the packet with an error", (
            _fixture_step(base),
            _replay_step("ubertooth-rx -i <fixture> -e 0", ("-e", "0"),
                         check=rxreplay.packets_check(rxreplay.expected_packets(max_errors=0))),
        ), "the access code with one bit error is not reported"),
        _replay_case("replay-dump", "-i -d writes the packet records back", (
            _fixture_step(base),
            _replay_step("ubertooth-rx -i <fixture> -d", (),
                         check=rxreplay.packets_check(full),
                         files=(("-d", "replay.dump"),)),
            _dump_check("verify-dump", "Verify the -d file records", "replay.dump",
                        "full", min_records=count, types="br"),
            Step("verify-records", "Compare the -d records with the fixture",
                 lambda context: (sys.executable, _REPLAY, "dump-check",
                                  _path("replay.dump")(context), _path(_FIXTURE)(context)),
                 output_patterns=(rf"Replay dump: {count} records match the fixture",)),
        ), f"{count} records, byte-identical to the fixture records with packets"),
        _replay_case("replay-lap-pcap", f"-i -l {_A} -q: only that LAP captured", (
            _fixture_step(base),
            _replay_step(f"ubertooth-rx -i <fixture> -l {_A} -q", ("-l", _A),
                         check=rxreplay.packets_check(lap_a),
                         files=(("-q", "replay.pcap"),)),
            _capture_packets("verify-capture", "Verify the -q file", "replay.pcap",
                             "pcap", len(lap_a)),
            # The same records in the other format. A PCAP carries no
            # interface option, so the address followed is the one thing the
            # PCAPNG case checks and this one cannot.
            _record_fields("verify-records", "Read every record of the -q file",
                           "replay.pcap"),
        ), f"pcap, link type 255, {len(lap_a)} records, every field as in the "
           "PCAPNG case"),
        _replay_case("replay-empty", "-i of an empty file", (
            _fixture_step(base, "empty"),
            _replay_step("ubertooth-rx -i <empty>", (), check=rxreplay.packets_check([])),
        ), "exit 0, no packet lines"),
        _replay_case("replay-truncated", "-i of a file whose last record is cut", (
            _fixture_step(base, "truncated"),
            _replay_step("ubertooth-rx -i <truncated>", (), returncodes=(1,),
                         check=rxreplay.packets_check(truncated),
                         patterns=(rf"error: .*{re.escape(_FIXTURE)} ends in a partial record: "
                                   rf"{rxreplay.RECORD - rxreplay.TRUNCATED_BYTES} of "
                                   rf"{rxreplay.RECORD} bytes, ignored",)),
        ), f"exit 1; the {len(truncated)} packets in whole records, then the partial "
           "record reported"),
        _replay_case("replay-missing", "-i of a missing file", (
            _replay_step("ubertooth-rx -i <missing>", (), returncodes=(1,),
                         patterns=(r"(?m)^error: .*missing\.bin: No such file or directory$",),
                         # A runtime file failure prints no usage (5ac22a8).
                         forbidden=(r"ubertooth-rx - passive Classic Bluetooth",),
                         source="missing.bin"),
        ), "exit 1 with 'error: <file>: No such file or directory'"),
        _replay_case("replay-systime", "-i packet lines carry the recorded systime", (
            _fixture_step(base),
            _replay_step("ubertooth-rx -i <fixture>", (), check=lambda output:
                         rxreplay.systime_check(output, [
                             base + index for index in rxreplay.packet_record_indices()])),
        ), "each packet line's systime is its record's host time"),
        _replay_case("replay-unreadable", "-i of a directory reports the read error", (
            Step("make-directory", "Create a directory to pass as -i",
                 lambda context: (sys.executable, "-c",
                                  "import os, sys; os.makedirs(sys.argv[1], exist_ok=True)",
                                  _path("a-directory")(context))),
            _replay_step("ubertooth-rx -i <directory>", (), returncodes=(1,),
                         patterns=(r"error: reading .*a-directory failed: Is a directory",),
                         source="a-directory"),
        ), "exit 1: 'error: reading <directory> failed: Is a directory'"),
        _replay_case("replay-uap-without-lap", "-i -u without -l is refused", (
            _fixture_step(base),
            _replay_step(f"ubertooth-rx -i <fixture> -u {rxreplay.UAP_A:02x}",
                         ("-u", f"{rxreplay.UAP_A:02x}"), returncodes=(1,),
                         check=rxreplay.packets_check([]),
                         patterns=(_UAP_NEEDS_LAP,)),
        ), "exit 1 with 'requires -l <LAP>' before any record is decoded"),
    ]
    cases += _stdout_destination_comprehensive(base, count, digest, len(fixture))
    return tuple(cases)
