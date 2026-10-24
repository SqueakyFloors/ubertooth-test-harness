"""Case builders for receive runs that need no peer: ubertooth-rx,
ubertooth-afh (Classic Bluetooth receive), ubertooth-dump, and ubertooth-ego
-r (Raw receive).

The cases check exit status, timeouts, output shape, and the structure of
the files the tools write, never decoded packet content. Checkers print
summaries only, so no capture bytes reach the run record. Every case reads
the settings a mode stop resets, runs one bounded receive, and restores them
with the usual cleanup. Command contracts come from the fork sources
(host/ubertooth-tools/src, host/libubertooth/src/ubertooth.c and
ubertooth_callback.c).

Some comprehensive cases assert the documented behavior that the fork does not
deliver yet; they fail until the linked fork issue is fixed:

- SqueakyFloors/ubertooth#185: ubertooth-afh crashes (exit 139) when the
  selected board is not found.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

from ubertooth_harness import capturefile, rxdump
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
# "Survey Results" and one ??:??:[UAP:]LAP line per piconet. Without -c it
# sweeps every channel; -c <index> fixes one. -e caps the access-code errors
# reported. -r/-q create BR/EDR PCAPNG/PCAP files (link type 255) at start;
# -d writes one full 68-byte record per packet found (possibly none).

_PACKET = re.compile(
    r"^systime=\d+ ch=\s*(?P<ch>\d+) LAP=[0-9a-f]{6} err=(?P<err>\d+) clkn=\d+ "
    r"clk_offset=\d+ s=-?\d+ n=-?\d+ snr=-?\d+$"
)
_SURVEY_LINE = re.compile(
    r"^\?\?:\?\?:(?:\?\?|[0-9A-F]{2}):[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}$"
)


def packet_check(*, channel=None, max_errors=None, survey=False):
    """A Step.output_check for ubertooth-rx stdout."""

    def check(output: str) -> str:
        lines = output.splitlines()
        problems = []
        packets = [m for m in map(_PACKET.match, lines) if m]
        if channel is not None:
            other = sorted({int(m["ch"]) for m in packets} - {channel})
            if other:
                problems.append(f"packets on channel {other[0]}, not {channel}")
        if max_errors is not None:
            worst = max((int(m["err"]) for m in packets), default=0)
            if worst > max_errors:
                problems.append(f"a packet with {worst} access-code errors (max {max_errors})")
        if survey:
            if "Survey Results" not in lines:
                problems.append("no 'Survey Results' line")
            else:
                results = lines[lines.index("Survey Results") + 1:]
                bad = [line for line in results if not _SURVEY_LINE.match(line)]
                if bad:
                    problems.append(f"{len(bad)} survey lines that are not piconet addresses")
        return "; ".join(problems)

    return check


def _rx(serial, *options):
    return ("ubertooth-rx", "-U", serial) + tuple(options)


def _rx_run(serial, seconds, options, *, name, check=None, patterns=(), files=()):
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
    )


def _rx_case(serial, seconds, case_id, name, options, expected_state, *,
             check=None, patterns=(), files=(), verify=()):
    return _case(
        serial, case_id, name,
        (_rx_run(serial, seconds, options, name=f"ubertooth-rx -t {seconds} "
                 + " ".join(options + tuple(o for o, _f in files)),
                 check=check, patterns=patterns, files=files),) + tuple(verify),
        f"exits 0 by itself after about {seconds}s; " + expected_state,
        capture_dir=bool(files),
    )


# ---------------------------------------------------------------------------
# ubertooth-afh
#
# Needs -l and -u. With -t N it detects the initial map for N s, prints
# "AFH map: 0x<20 hex>", then monitors map changes until signalled; with no
# piconet in range nothing more is printed (SqueakyFloors/ubertooth#187: the
# no-packet map is all zeros). -r prints "<time> <80 x 0/1>" every second.

_AFH_MAP = r"(?m)^AFH map: 0x[0-9a-f]{20}$"
_AFH_TARGET = ("-l", "446688", "-u", "22")
_AFH_R_LINE = re.compile(r"^\d+ [01]{80}$")


def afh_r_check(minimum):
    def check(output):
        lines = [line for line in output.splitlines() if line.strip()]
        good = [line for line in lines if _AFH_R_LINE.match(line)]
        problems = []
        if len(good) < minimum:
            problems.append(f"{len(good)} map lines, expected at least {minimum}")
        if len(good) != len(lines):
            problems.append(f"{len(lines) - len(good)} lines that are not '<time> <map>'")
        return "; ".join(problems)
    return check


def _afh(serial, *options):
    return ("ubertooth-afh", "-U", serial) + _AFH_TARGET + tuple(options)


def _afh_case(serial, seconds, case_id, name, options, expected_state, *, check=None,
              patterns=(_AFH_MAP,)):
    return _case(serial, case_id, name, (
        Step("receive", name, _afh(serial, *options),
             timeout_s=seconds + 3.0 if "-t" in options else float(seconds),
             mutates=True, continuous=True, output_patterns=patterns,
             output_check=check),
        _stop(serial),
    ), expected_state)


def classic_receive_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Classic Bluetooth receive")
    n = seconds_arg(arguments)
    absent = ABSENT_SERIAL if serial != ABSENT_SERIAL else "0" * 32
    cases = [
        _rx_case(serial, n, "rx-survey-timeout", "Survey (-z -t) ends by itself",
                 ("-z",), "survey results listed",
                 check=packet_check(survey=True)),
        _rx_case(serial, n, "rx-capture-pcapng", "PCAPNG capture (-r)", (),
                 "-r wrote a pcapng file with link type 255",
                 files=(("-r", "rx.pcapng"),),
                 verify=(_capture_header("verify-capture", "Verify the -r file header",
                                         "rx.pcapng", "pcapng"),)),
        _afh_case(serial, n, "afh-initial-timeout", "AFH map after the -t detection timeout",
                  ("-t", str(n)),
                  "initial map printed after -t, then monitoring until stopped"),
    ]
    if arguments["coverage"] == "comprehensive":
        cases += [
            _rx_case(serial, n, "rx-sweep-timeout", "Sniff all LAPs (-t) ends by itself",
                     (), "packet lines only in the documented format"),
            _rx_case(serial, n, "rx-fixed-channel", "Fixed channel (-c 39)", ("-c", "39"),
                     "every packet on channel 39", check=packet_check(channel=39)),
            _rx_case(serial, n, "rx-ac-errors-0", "No access-code errors (-e 0)", ("-e", "0"),
                     "no packet with access-code errors", check=packet_check(max_errors=0)),
            _rx_case(serial, n, "rx-ac-errors-4", "Four access-code errors (-e 4)", ("-e", "4"),
                     "no packet with more than 4 errors", check=packet_check(max_errors=4)),
            _rx_case(serial, n, "rx-sweep-deprecated", "-s prints its deprecation notice",
                     ("-s",), "deprecation notice printed",
                     patterns=(r"the -s argument is deprecated",)),
            _rx_case(serial, n, "rx-capture-pcap", "PCAP capture (-q)", (),
                     "-q wrote a pcap file with link type 255",
                     files=(("-q", "rx.pcap"),),
                     verify=(_capture_header("verify-capture", "Verify the -q file header",
                                             "rx.pcap", "pcap"),)),
            _rx_case(serial, n, "rx-capture-repeated-first-wins",
                     "Repeated -r: the first file is written, the extra ignored", (),
                     "only the first -r file written",
                     patterns=(r"Ignoring extra capture file: ",),
                     files=(("-r", "first.pcapng"), ("-r", "second.pcapng")),
                     verify=(
                         _capture_header("verify-first", "Verify the first -r file",
                                         "first.pcapng", "pcapng"),
                         _capture_absent("verify-second-absent",
                                         "Verify the extra -r file was not created",
                                         "second.pcapng"),
                     )),
            _rx_case(serial, n, "rx-dump-file", "Packet dump (-d)", (),
                     "-d wrote whole 68-byte BR packet records (none without traffic)",
                     files=(("-d", "rx.dump"),),
                     verify=(_dump_check("verify-dump", "Verify the -d file records",
                                         "rx.dump", "full", types="br"),)),
            _afh_case(serial, n, "afh-ac-errors", "AFH with -e 2 accepted",
                      ("-t", str(n), "-e", "2"), "accepted; initial map printed"),
            _afh_case(serial, n, "afh-map-every-second", "AFH map every second (-r)",
                      ("-r",), "one '<time> <80 bits>' line per second",
                      check=afh_r_check(max(1, n - 1)), patterns=()),
            # Fails until SqueakyFloors/ubertooth#185: exit 139 (segfault).
            _case(serial, "afh-board-not-found", "AFH with an unknown -U exits 1", (
                Step("reject", "ubertooth-afh -U <absent serial>",
                     ("ubertooth-afh", "-U", absent) + _AFH_TARGET + ("-t", "1"),
                     expected_returncodes=(1,),
                     output_patterns=(r"no Ubertooth found",)),
            ), "exit 1 with a message; the selected board untouched"),
        ]
    return tuple(cases)


# ---------------------------------------------------------------------------
# ubertooth-dump
#
# Streams received symbols until signalled: full 68-byte records, or with -b
# 401-byte ASCII bitstream records, to stdout or -d <file>. -c / -l set basic
# rate or LE modulation (the last one wins); the firmware programs CC2400
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
# sscanf cannot read is rejected with "Invalid access code" and exit 1.

_EGO_PACKET = re.compile(r"^time=\d+ delta_t=-?\d+\.\d{6} ms freq=(?P<f>\d+) ?$")


def ego_check(frequency, length):
    def check(output):
        lines = output.splitlines()
        problems = []
        for index, line in enumerate(lines):
            match = _EGO_PACKET.match(line)
            if match is None:
                continue
            if int(match["f"]) != frequency:
                problems.append(f"a packet on {match['f']} MHz, not {frequency}")
                break
            data = lines[index + 1].split() if index + 1 < len(lines) else []
            if len(data) != length:
                problems.append(f"a packet with {len(data)} bytes, not {length}")
                break
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
            _dump_case(serial, n, "dump-le-then-classic", "-l -c: the last (-c) wins",
                       ("-l", "-c"), "full", modulation="basic rate"),
            _dump_case(serial, n, "dump-classic-then-le", "-c -l: the last (-l) wins",
                       ("-c", "-l"), "full", modulation="LE"),
            _ego_case(serial, n, "ego-length-1", "E-GO capture length -l 1", ("-l", "1"),
                      length=1),
            _ego_case(serial, n, "ego-length-48", "E-GO capture length -l 48", ("-l", "48"),
                      length=48),
            _ego_case(serial, n, "ego-access-code", "E-GO access code -a 630f9ffe",
                      ("-a", "630f9ffe")),
            _case(serial, "ego-reject-bad-access-code", "E-GO rejects -a zz", (
                Step("reject", "ubertooth-ego -r -a zz",
                     ("ubertooth-ego", "-U", serial, "-r", "-c", "2440", "-a", "zz"),
                     expected_returncodes=(1,), output_patterns=(r"Invalid access code",),
                     mutates=True),
            ), "rejected with exit 1 before any mode starts"),
        ]
    return tuple(cases)
