"""Case builders for ubertooth-specan, ubertooth-debug, and peerless
ubertooth-btle settings.

Command contracts come from the fork sources (host/ubertooth-tools/src and
host/libubertooth/src/ubertooth_callback.c). Every case records what it can
change, runs with the usual cleanup, and is guarded like the transition tests.

Comprehensive cases also cover fork fixes, so they fail on builds before them:

- SqueakyFloors/ubertooth#173 (fixed by #180, 20bcf11): a specan run first
  reported samples from the previous run's range.
- SqueakyFloors/ubertooth#164 (fixed by #170, 89afc1c): ubertooth-debug
  exited with the low byte of the last register value.
- SqueakyFloors/ubertooth#171 (fixed by #178, 09e9a46): ubertooth-btle -a
  with a bad value set garbage, and -a skipped a following -v.
- SqueakyFloors/ubertooth#191 (fixed by #204, aa4f620): stopping
  ubertooth-specan skipped the idle reset (LEDs, channel, squelch kept).
"""

from __future__ import annotations

import re
from pathlib import Path
import sys

from ubertooth_harness import specandump
from ubertooth_harness.hostneeds import Needs
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    Step,
    nothing_on_stdout,
    require,
)
from ubertooth_harness.transitions import (
    BOOT_DEFAULT_CHANNEL,
    CHANNEL,
    CRC_PATTERN,
    LEDS,
    MODE_RESET_SETTINGS,
    MODE_TIMEOUT_S,
    PA_LEVEL,
    RESET_TIMEOUT_S,
    RESTORED_SETTINGS,
    SQUELCH,
    _btle,
    _cleanup,
    _crc_read,
    _identity,
    _read_originals,
    _serial,
    _stop,
    _util,
)

BAND = (2402, 2480)
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def duration_seconds(value: str) -> float:
    """'8s' -> 8.0 (the catalog has already validated the form)."""
    match = re.fullmatch(r"([1-9][0-9]*)(ms|s|m|h)", value)
    return int(match[1]) * _UNITS[match[2]]


# ---------------------------------------------------------------------------
# ubertooth-specan
#
# Output modes (cb_specan): default "<seconds>, <MHz>, <RSSI>"; -g "<MHz> <RSSI>"
# with a blank line after each sweep; -G "<seconds> <MHz> <RSSI>" with the same
# blank lines; -d writes 3-byte binary records (MHz big-endian, int8 RSSI) to
# the file instead of stdout (only a bare "-d -" means stdout, fork 053363a).
# -v only adds stderr. Stdout carries only the samples: "Ubertooth stopped",
# errors and progress are on stderr (fork fcadf34, ubertooth-specan.md
# DIAGNOSTICS), and a run that cannot write its samples says so once and
# exits 1.

_LINE_FORMATS = {
    "default": re.compile(r"^(?P<t>\d+\.\d+), (?P<f>\d+), (?P<r>-?\d+)$"),
    "-g": re.compile(r"^(?P<f>\d+) (?P<r>-?\d+)$"),
    "-G": re.compile(r"^(?P<t>\d+\.\d+) (?P<f>\d+) (?P<r>-?\d+)$"),
}
# int8 RSSI as the firmware reports it (dBm + 54, see Spectrum RSSI sanity);
# anything outside is a parse problem.
_RSSI_RANGE = (-128, 127)


def sweep_check(fmt: str, low: int, high: int):
    """A Step.output_check: every sample line parses and lies in [low, high],
    every frequency of the range was reported, and RSSI is an int8."""
    pattern = _LINE_FORMATS[fmt]

    def check(output: str) -> str:
        seen = set()
        outside = {}
        bad = 0
        samples = 0
        progress = 0
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith("HARD TIMEOUT"):
                continue
            if line.startswith(("Ubertooth stopped", "error: ", "warning: ", "note: ")):
                progress += 1
                continue
            match = pattern.match(line)
            if match is None:
                bad += 1
                continue
            samples += 1
            frequency = int(match["f"])
            rssi = int(match["r"])
            if not _RSSI_RANGE[0] <= rssi <= _RSSI_RANGE[1]:
                bad += 1
            if low <= frequency <= high:
                seen.add(frequency)
            else:
                outside[frequency] = outside.get(frequency, 0) + 1
        problems = []
        if progress:
            problems.append(f"{progress} progress or diagnostic lines on stdout "
                            "(they go to stderr)")
        if samples == 0:
            problems.append("no sample lines")
        missing = sorted(set(range(low, high + 1)) - seen)
        if samples and missing:
            problems.append(f"no samples for {len(missing)} frequencies (first {missing[0]})")
        if outside:
            first = sorted(outside)[:5]
            problems.append(
                f"{sum(outside.values())} samples outside {low}-{high} MHz "
                f"(for example {', '.join(map(str, first))})"
            )
        if bad > 5:
            # A few lines can be cut by the stop signal; more is a format change.
            problems.append(f"{bad} lines that are not {fmt} samples")
        return "; ".join(problems)

    return check


_SPECAN_CHECKER = str(Path(specandump.__file__).resolve())


def _specan_case(serial, case_id, name, options, fmt, low, high, seconds, *,
                 dump_file=None, stderr_pattern=None, extra_check=None,
                 expected_state=None):
    steps = [
        _stop(serial, "establish-idle", "Establish idle before reading"),
    ]
    steps.extend(_read_originals(serial, MODE_RESET_SETTINGS))
    if dump_file is None:
        argv = _specan_argv(serial, options)
    else:
        def argv(context, options=options):
            path = str(Path(require(context, CAPTURE_DIR_KEY)) / dump_file)
            return _specan_argv(serial, options + ("-d", path))
    steps.append(Step(
        "sweep",
        f"Sweep {low}-{high} MHz ({fmt})",
        argv,
        timeout_s=seconds,
        mutates=True,
        continuous=True,
        output_check=(None if dump_file is not None
                      else _all_checks(sweep_check(fmt, low, high), extra_check)),
        output_patterns=(stderr_pattern,) if stderr_pattern else (),
    ))
    steps.append(_stop(serial))
    if dump_file is not None:
        steps.append(Step(
            "verify-dump",
            "Verify the -d file samples",
            lambda context: (
                sys.executable, _SPECAN_CHECKER,
                str(Path(require(context, CAPTURE_DIR_KEY)) / dump_file),
                str(low), str(high),
            ),
            output_patterns=(r"Specan dump: \d+ samples",),
        ))
    return StateCase(
        case_id,
        name,
        tuple(steps),
        _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=expected_state or (
            f"every reported frequency within {low}-{high} MHz and each one "
            "reported; channel, squelch, and LEDs restored"
        ),
        capture_dir=dump_file is not None,
    )


def _all_checks(*checks):
    """One output_check that runs every check given and joins their problems."""
    present = tuple(check for check in checks if check is not None)

    def check(output):
        return "; ".join(problem for problem in (c(output) for c in present) if problem)

    return check


def _specan_argv(serial, options):
    return ("ubertooth-specan", "-U", serial) + tuple(options)


# -d takes any value but a bare "-" as a file name, including one that starts
# with "-" (fork 053363a, SqueakyFloors/ubertooth#231; before it any value
# starting with "-" streamed the samples to stdout). The file name has to be
# relative to show that, so the command runs in the case's capture directory.
DASH_DUMP = "-x.bin"
_IN_DIRECTORY = "import os, sys; os.chdir(sys.argv[1]); os.execvp(sys.argv[2], sys.argv[2:])"


def _no_samples_on_stdout(output: str) -> str:
    # The dump goes to the file, so stdout must stay empty: a binary sample
    # can decode to printable replacement characters, so any byte counts.
    if output.strip():
        return f"stdout is not empty ({len(output)} characters)"
    return ""


def _specan_dash_file_case(serial, seconds):
    low, high = 2430, 2440
    steps = [_stop(serial, "establish-idle", "Establish idle before reading")]
    steps.extend(_read_originals(serial, MODE_RESET_SETTINGS))
    steps.append(Step(
        "sweep",
        f"ubertooth-specan -d {DASH_DUMP} in the capture directory",
        lambda context: (sys.executable, "-c", _IN_DIRECTORY, require(context, CAPTURE_DIR_KEY))
        + _specan_argv(serial, ("-l", str(low), "-u", str(high), "-d", DASH_DUMP)),
        timeout_s=seconds, mutates=True, continuous=True,
        output_check=_no_samples_on_stdout,
    ))
    steps.append(_stop(serial))
    steps.append(Step(
        "verify-dump",
        f"Verify the samples in {DASH_DUMP}",
        lambda context: (sys.executable, _SPECAN_CHECKER,
                         str(Path(require(context, CAPTURE_DIR_KEY)) / DASH_DUMP),
                         str(low), str(high)),
        output_patterns=(r"Specan dump: \d+ samples",),
    ))
    return StateCase(
        "dump-file-dash-name",
        f"-d {DASH_DUMP} writes the file {DASH_DUMP}, not stdout",
        tuple(steps),
        _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=(f"samples in the file {DASH_DUMP}, none on stdout; channel, "
                        "squelch, and LEDs restored"),
        capture_dir=True,
    )


# A -d file that takes no data: the first failed write ends the sweep, which
# says so once and exits 1 by itself (fork fcadf34, ubertooth-specan.md
# DIAGNOSTICS). /dev/full exists on Linux only.
FULL_DEVICE = "/dev/full"
WRITE_FAILED = (r"(?m)^error: writing samples failed: (?:wrote \d of 3 bytes|"
                r"could not flush the samples) \(No space left on device\)$")
_WRITE_FAILED_TWICE = r"(?s)writing samples failed.*writing samples failed"


def _specan_full_device_case(serial, seconds):
    steps = [_stop(serial, "establish-idle", "Establish idle before reading")]
    steps.extend(_read_originals(serial, MODE_RESET_SETTINGS))
    steps.append(Step(
        "sweep",
        f"ubertooth-specan -d {FULL_DEVICE} ends by itself",
        _specan_argv(serial, ("-d", FULL_DEVICE)),
        # A full-band sweep fills a stdio buffer within a second.
        timeout_s=max(seconds, 5.0) + 5.0, mutates=True,
        expected_returncodes=(1,),
        output_patterns=(WRITE_FAILED,),
        forbidden_patterns=(_WRITE_FAILED_TWICE,),
        output_check=nothing_on_stdout,
    ))
    steps.append(_stop(serial))
    return StateCase(
        "dump-file-full-device",
        f"-d {FULL_DEVICE}: the failed write ends the run, exit 1",
        tuple(steps),
        _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=("one 'error: writing samples failed' line on stderr, exit 1 "
                        "before the harness stops it; channel, squelch, and LEDs "
                        "restored"),
        needs=Needs(platforms=("linux",)),
    )


# ---------------------------------------------------------------------------
# A mode stop resets the board (fork aa4f620, SqueakyFloors/ubertooth#191)
#
# Stopping any mode runs the firmware idle reset: LEDs off, channel 2441 MHz,
# squelch -120; the PA level is kept. The case presets channel, squelch, and
# every LED away from those values, runs the tool for a bounded time (the
# harness ends it with SIGTERM), stops the board with -S, and reads them back.

STOP_RESET_SQUELCH = "-60"


def stop_reset_steps(serial, run_step):
    """Steps around ``run_step`` (a continuous Step): read PA and the settings
    the stop resets, preset them, run, -S, and check the idle reset."""
    target = CHANNEL.pick(0)
    return (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        # No step writes PA; cleanup restores it anyway (-a0 sets PA 0, so
        # a board at PA 0 runs the case too).
        Step("read-pa-level", "Read PA level", _util(serial, PA_LEVEL.query),
             observe=Observation(PA_LEVEL.original, PA_LEVEL.pattern)),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step("set-non-defaults",
             "Set channel, squelch, and every LED away from the stop defaults",
             lambda context: _util(serial, CHANNEL.set_arg(target(context)),
                                   SQUELCH.set_arg(STOP_RESET_SQUELCH), "-d1"),
             mutates=True, output_check=nothing_on_stdout),
        CHANNEL.verify(serial, "verify-channel-set", "Channel set before the run",
                       "channel before run", target),
        SQUELCH.verify(serial, "verify-squelch-set", "Squelch set before the run",
                       "squelch before run", STOP_RESET_SQUELCH),
        LEDS.verify(serial, "verify-leds-set", "Every LED on before the run",
                    "leds before run", "1/1/1"),
        run_step,
        _stop(serial, "mode-stop", "Stop the board with -S"),
        LEDS.verify(serial, "verify-leds-after-stop", "LEDs off after the stop",
                    "leds after stop", LEDS.after_mode_stop),
        CHANNEL.verify(serial, "verify-channel-after-stop",
                       "Channel reset by the stop", "channel after stop",
                       BOOT_DEFAULT_CHANNEL),
        SQUELCH.verify(serial, "verify-squelch-after-stop",
                       "Squelch reset by the stop", "squelch after stop",
                       SQUELCH.after_mode_stop),
        PA_LEVEL.verify(serial, "verify-pa-level-after-stop",
                        "PA level kept by the stop", "pa-level after stop",
                        lambda context: require(context, PA_LEVEL.original)),
    )


STOP_RESET_STATE = ("LEDs off, channel 2441 MHz, squelch -120, PA kept after -S")


def _specan_stop_reset_case(serial, seconds):
    run = Step("sweep", "Run ubertooth-specan (bounded, SIGTERM)",
               _specan_argv(serial, ()), timeout_s=seconds, mutates=True,
               continuous=True, output_check=sweep_check("default", *BAND))
    return StateCase(
        "stop-resets-board",
        "Stopping ubertooth-specan resets LEDs, channel, and squelch",
        stop_reset_steps(serial, run),
        _cleanup(serial, MODE_RESET_SETTINGS + (PA_LEVEL,)),
        expected_state=f"full-band sweep; {STOP_RESET_STATE}; originals restored",
    )


def spectrum_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Spectrum analyzer")
    seconds = duration_seconds(arguments["duration"])
    cases = [
        _specan_case(serial, "full-band", "Full band sweep (default range)",
                     (), "default", *BAND, seconds),
    ]
    if arguments["coverage"] == "comprehensive":
        # Each case whose range differs from the previous case's (narrow,
        # single, top, -g, -d) reported one stale 16-sample packet before
        # fork 20bcf11 (SqueakyFloors/ubertooth#173). -G and -v reuse the -g
        # range.
        cases += [
            _specan_case(serial, "narrow-after-full", "2402-2410 MHz after a full-band run",
                         ("-l", "2402", "-u", "2410"), "default", 2402, 2410, seconds),
            _specan_case(serial, "single-frequency", "Single frequency 2440 MHz",
                         ("-l", "2440", "-u", "2440"), "default", 2440, 2440, seconds),
            _specan_case(serial, "top-of-band", "Top of band 2470-2480 MHz",
                         ("-l", "2470", "-u", "2480"), "default", 2470, 2480, seconds),
            _specan_case(serial, "gnuplot", "feedgnuplot output (-g)",
                         ("-g", "-l", "2402", "-u", "2420"), "-g", 2402, 2420, seconds),
            _specan_case(serial, "gnuplot-3d", "3D feedgnuplot output (-G)",
                         ("-G", "-l", "2402", "-u", "2420"), "-G", 2402, 2420, seconds),
            _specan_case(serial, "verbose", "Verbose (-v) keeps the stdout format",
                         ("-v", "-l", "2402", "-u", "2420"), "default", 2402, 2420, seconds),
            _specan_case(serial, "dump-file", "Binary samples to a file (-d)",
                         ("-l", "2430", "-u", "2440"), "default", 2430, 2440, seconds,
                         dump_file="specan.bin"),
            _specan_dash_file_case(serial, seconds),
            _specan_full_device_case(serial, seconds),
            _specan_stop_reset_case(serial, seconds),
        ]
    return tuple(cases)


# ---------------------------------------------------------------------------
# Spectrum RSSI sanity
#
# ubertooth-specan prints the CC2400 RSSI register as the firmware reads it
# (bluetooth_rxtx.c specan(): cc2400_get(RSSI) >> 8), not dBm: the register
# reads RSSI_OFFSET (-54) dB above the input power (the firmware's
# rssi_threshold comment, -54 dBm - 30 = -84 dBm; specan-ui adds -54).
# The checks need no signal source, only that the receiver measures:
#
# - every frequency's lowest reading, a noise floor, is between -115 and
#   -80 dBm (the CC2400 floor is about -100 dBm; one quiet sample per
#   frequency is enough, so busy Wi-Fi channels still have one);
# - the mean reading is negative (below -54 dBm): most samples are noise;
# - the readings vary (at least three values): the register is live.

RSSI_OFFSET_DB = -54
NOISE_FLOOR_DBM = (-115, -80)
_LIVE_VALUES = 3


def rssi_sanity_check(output: str) -> str:
    """A Step.output_check on default-format specan samples."""
    floor = {}
    total = 0
    count = 0
    values = set()
    for line in output.splitlines():
        match = _LINE_FORMATS["default"].match(line.strip())
        if match is None:
            continue  # sweep_check reports lines that are not samples
        frequency, rssi = int(match["f"]), int(match["r"])
        floor[frequency] = min(rssi, floor.get(frequency, rssi))
        total += rssi
        count += 1
        values.add(rssi)
    if not count:
        return ""  # sweep_check reports "no sample lines"
    problems = []
    low, high = NOISE_FLOOR_DBM
    off = {f: r + RSSI_OFFSET_DB for f, r in floor.items()
           if not low <= r + RSSI_OFFSET_DB <= high}
    if off:
        first = sorted(off)[:3]
        problems.append(
            f"noise floor outside {low} to {high} dBm at {len(off)} frequencies "
            f"(for example {', '.join(f'{f} MHz {off[f]} dBm' for f in first)})"
        )
    mean = total / count
    if mean >= 0:
        problems.append(f"mean reading {mean:.1f} ({mean + RSSI_OFFSET_DB:.1f} dBm) "
                        "is not negative")
    if len(values) < _LIVE_VALUES:
        problems.append(f"only {len(values)} distinct readings in {count} samples "
                        f"({', '.join(map(str, sorted(values)))}): RSSI is not live")
    return "; ".join(problems)


def rssi_sanity_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Spectrum RSSI sanity")
    seconds = duration_seconds(arguments["duration"])
    return (
        _specan_case(
            serial, "live-rssi", "Live RSSI over a full-band sweep", (), "default",
            *BAND, seconds, extra_check=rssi_sanity_check,
            expected_state=(
                f"full-band sweep; every frequency's noise floor {NOISE_FLOOR_DBM[0]} to "
                f"{NOISE_FLOOR_DBM[1]} dBm, negative mean, at least {_LIVE_VALUES} "
                "distinct readings; channel, squelch, and LEDs restored"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# ubertooth-debug
#
# -r takes one register, a comma list, or a start-end range (either order);
# each register prints "%NAME = 0xVALUE" then, at -v1 (default), one line per
# field, and at -v2 the reserved fields too. Reads change nothing.

_REG_LINE = r"(?m)^%(?:[A-Z0-9_]+|r[0-9A-Fa-f]{2}) = 0x[0-9A-Fa-f]{4}$"


def _register_order_check(expected):
    def check(output):
        names = re.findall(r"(?m)^%([A-Z0-9_]+|r[0-9A-Fa-f]{2}) = 0x", output)
        if names != list(expected):
            return f"registers {names}, expected {list(expected)}"
        return ""
    return check


def _field_lines(output):
    return [line for line in output.splitlines() if re.match(r"^[01.]{8} [01.]{8} ", line)]


def _verbosity_check(level):
    def check(output):
        fields = _field_lines(output)
        reserved = [line for line in fields if "(W0)" in line]
        if level == 0 and fields:
            return f"-v0 printed {len(fields)} field lines"
        if level >= 1 and not fields:
            return f"-v{level} printed no field lines"
        if level == 1 and reserved:
            return "-v1 printed reserved fields"
        if level == 2 and not reserved:
            return "-v2 printed no reserved fields"
        return ""
    return check


def _debug_step(serial, step_id, name, options, *, check=None, codes=(0,), patterns=(_REG_LINE,)):
    return Step(
        step_id,
        name,
        ("ubertooth-debug", "-U", serial) + tuple(options),
        expected_returncodes=codes,
        output_patterns=patterns,
        output_check=check,
    )


def _debug_case(serial, case_id, name, step):
    return StateCase(
        case_id,
        name,
        (_stop(serial, "establish-idle", "Establish idle before reading"),)
        + _read_originals(serial, MODE_RESET_SETTINGS)
        + (step,)
        + tuple(
            setting.verify(serial, f"verify-unchanged-{setting.key}",
                           f"Verify {setting.key} unchanged", f"{setting.key} after read",
                           lambda context, s=setting: require(context, s.original))
            for setting in MODE_RESET_SETTINGS
        ),
        _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state="registers read in order; nothing changed",
    )


def debug_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Debug registers")
    cases = [
        # Ends on 0x00 (0x8000), so the exit status is 0 even with
        # SqueakyFloors/ubertooth#164 builds; the exit-status case checks that bug.
        _debug_case(serial, "list", "Register list 0x02,0x01,0x00 in order",
                    _debug_step(serial, "read", "Read 0x02,0x01,0x00", ("-r", "0x02,0x01,0x00"),
                                check=_register_order_check(("FSDIV", "FSCTRL", "MAIN")))),
    ]
    if arguments["coverage"] == "comprehensive":
        cases += [
            # Ends on 0x03 (0x0040): exit 64 on builds before the #164 fix.
            _debug_case(serial, "range", "Register range 0x00-0x03",
                        _debug_step(serial, "read", "Read 0x00-0x03", ("-r", "0x00-0x03"),
                                    check=_register_order_check(("MAIN", "FSCTRL", "FSDIV", "MDMCTRL")))),
            _debug_case(serial, "reversed-range", "Reversed range 0x03-0x00 reads downwards",
                        _debug_step(serial, "read", "Read 0x03-0x00", ("-r", "0x03-0x00"),
                                    check=_register_order_check(("MDMCTRL", "FSDIV", "FSCTRL", "MAIN")))),
            # FSCTRL (0x01) reads 0x0010: exits 16 on builds before the #164 fix.
            _debug_case(serial, "exit-status", "A successful read of 0x01 exits 0",
                        _debug_step(serial, "read", "Read 0x01", ("-r", "0x01"))),
        ] + [
            _debug_case(serial, f"verbosity-{level}", f"Verbosity -v{level}",
                        _debug_step(serial, "read", f"Read 0x00 at -v{level}",
                                    ("-r", "0x00", f"-v{level}"), check=_verbosity_check(level)))
            for level in (0, 1, 2)
        ] + [
            _debug_case(serial, "reject-register-0x100", "Reject register 0x100",
                        _debug_step(serial, "reject", "Reject -r 0x100", ("-r", "0x100"),
                                    codes=(1,), patterns=(r"error: register address must be 0x00-0xff \(got 0x100\)",))),
            _debug_case(serial, "reject-verbosity-3", "Reject -v3",
                        _debug_step(serial, "reject", "Reject -v3", ("-r", "0x00", "-v3"),
                                    codes=(1,), patterns=(r"error: verbosity must be 0-2 \(got 3\)",))),
        ]
    return tuple(cases)


# ---------------------------------------------------------------------------
# ubertooth-btle settings that need no peer
#
# -a reads the access address ("Access address: xxxxxxxx"), -a<hex> sets it
# ("access address set to: xxxxxxxx"); ubertooth-util -r resets it to the
# advertising address 8e89bed6. -t<mac>[/mask] sets the follow target ("target
# set to: <mac>/<mask>"), -tnone clears it; the target cannot be read back.
# -x<0-32> sets the access-address error allowance for the capture callback.

ADVERTISING_AA = "8e89bed6"
_AA_PATTERN = r"Access address: (?P<value>[0-9a-f]{8})"
_AA_TEST_VALUE = "12345678"


def _aa_read(serial, step_id, name, key, expected=None):
    return Step(step_id, name, _btle(serial, "-a"),
                observe=Observation(key, _AA_PATTERN, expected))


def _aa_restore(serial):
    return (
        Step("restore-access-address", "Restore original access address",
             lambda context: _btle(serial, f"-a{require(context, 'original access address')}"),
             mutates=True),
        _aa_read(serial, "verify-restored-access-address", "Verify restored access address",
                 "restored access address",
                 lambda context: require(context, "original access address")),
    )


def _aa_cleanup(serial):
    return _cleanup(serial, ())[:-1] + _aa_restore(serial) + (_identity(serial),)


def _btle_case(serial, case_id, name, steps, *, expected_state, cleanup=None):
    return StateCase(
        case_id,
        name,
        (
            _stop(serial, "establish-idle", "Establish idle before reading"),
            _aa_read(serial, "read-original-access-address", "Read original access address",
                     "original access address"),
        ) + tuple(steps),
        cleanup or _aa_cleanup(serial),
        expected_state=expected_state,
    )


def btle_settings_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "BLE settings")

    def other_aa(context):
        return "8e89bed7" if require(context, "original access address") == _AA_TEST_VALUE else _AA_TEST_VALUE

    cases = [
        _btle_case(serial, "access-address-set-read", "Set and read back the access address", (
            Step("set", "Set access address", lambda c: _btle(serial, f"-a{other_aa(c)}"),
                 output_patterns=(r"access address set to: [0-9a-f]{8}",), mutates=True),
            _aa_read(serial, "verify-set", "Verify access address", "access address after set",
                     other_aa),
        ), expected_state="access address set and read back, then the original restored"),
        # The value as the next word (fork 0b63b01, SqueakyFloors/ubertooth#155).
        _btle_case(serial, "access-address-next-word", "-a <address> as the next word sets it", (
            Step("set", "Set access address with -a <address>",
                 lambda c: _btle(serial, "-a", other_aa(c)),
                 output_patterns=(r"access address set to: [0-9a-f]{8}",),
                 forbidden_patterns=(r"unexpected argument",), mutates=True),
            _aa_read(serial, "verify-set", "Verify access address", "access address after set",
                     other_aa),
        ), expected_state="access address set by the next word, then the original restored"),
        _btle_case(serial, "follow-target", "Set and clear the follow target", (
            Step("set-target", "Set follow target", _btle(serial, "-t22:44:66:88:aa:cc"),
                 output_patterns=(r"target set to: 22:44:66:88:aa:cc/48",), mutates=True),
            Step("set-masked-target", "Set masked follow target", _btle(serial, "-t22:44:66:88:aa:cc/24"),
                 output_patterns=(r"target set to: 22:44:66:88:aa:cc/24",), mutates=True),
            Step("clear-target", "Clear follow target", _btle(serial, "-tnone"),
                 output_patterns=(r"target cleared",), mutates=True),
        ), expected_state="target set, masked, and cleared (left cleared: it cannot be read back)"),
    ]
    if arguments["coverage"] == "comprehensive":
        cases += [
            _btle_case(serial, "reset-clears-access-address",
                       "ubertooth-util -r resets the access address", (
                _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"),
            ) + _read_originals(serial, RESTORED_SETTINGS) + (
                Step("set", "Set access address", lambda c: _btle(serial, f"-a{other_aa(c)}"),
                     mutates=True),
                Step("reset", "Full reset", _util(serial, "-r"), timeout_s=RESET_TIMEOUT_S,
                     output_patterns=(r"Resetting ubertooth device",),
                     output_check=nothing_on_stdout, mutates=True),
                _aa_read(serial, "verify-reset", "Verify advertising access address",
                         "access address after reset", ADVERTISING_AA),
            ), expected_state="access address back to the advertising address 8e89bed6; "
               "every setting the reset changed restored",
               cleanup=_cleanup(serial, RESTORED_SETTINGS, crc=True)[:-1]
               + _aa_restore(serial) + (_identity(serial),)),
            _btle_case(serial, "error-allowance-bounds", "-x0 and -x32 are accepted", (
                Step("x0", "-x0 with a CRC read", _btle(serial, "-x0", "-v"),
                     output_patterns=(CRC_PATTERN,)),
                Step("x32", "-x32 with a CRC read", _btle(serial, "-x32", "-v"),
                     output_patterns=(CRC_PATTERN,)),
            ), expected_state="both bounds accepted; nothing changed"),
            # -a used to return before -v (SqueakyFloors/ubertooth#171, fixed
            # in 09e9a46).
            _btle_case(serial, "access-address-with-crc", "-a -v reports both", (
                Step("read-both", "Read access address and CRC", _btle(serial, "-a", "-v"),
                     output_patterns=(_AA_PATTERN, CRC_PATTERN),
                     forbidden_patterns=(r"systime=",)),
            ), expected_state="access address and CRC both reported, no sniffing"),
            # A bad value used to set garbage (#171, fixed in 09e9a46).
            _btle_case(serial, "reject-bad-access-address", "Reject -azzzz", (
                Step("reject", "Reject -azzzz", _btle(serial, "-azzzz"),
                     expected_returncodes=(1,), mutates=True,
                     output_patterns=(
                         r"error: access address must be 1-8 hex digits \(got zzzz\)",),
                     forbidden_patterns=(r"access address set to",)),
                _aa_read(serial, "verify-unchanged", "Verify access address unchanged",
                         "access address after rejection",
                         lambda context: require(context, "original access address")),
            ), expected_state="rejected with exit 1; access address unchanged"),
            _promisc_stop_reset_case(serial),
        ]
    return tuple(cases)


def _promisc_stop_reset_case(serial):
    """ubertooth-btle -p (receive only: no -i/-I, so no jamming) stopped:
    the idle reset runs. Promiscuous mode resets the LE state (access address,
    CRC check), so both are recorded and restored."""
    run = Step("promiscuous", "Run ubertooth-btle -p (bounded, SIGTERM)",
               _btle(serial, "-p"), timeout_s=MODE_TIMEOUT_S, mutates=True,
               continuous=True)
    steps = stop_reset_steps(serial, run)
    return StateCase(
        "promiscuous-stop-resets-board",
        "Stopping ubertooth-btle -p resets LEDs, channel, and squelch",
        (steps[0],
         _aa_read(serial, "read-original-access-address", "Read original access address",
                  "original access address"),
         _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"))
        + steps[1:],
        _cleanup(serial, MODE_RESET_SETTINGS + (PA_LEVEL,), crc=True)[:-1]
        + _aa_restore(serial) + (_identity(serial),),
        expected_state=(f"-p runs until stopped; {STOP_RESET_STATE}; originals, "
                        "access address, and CRC restored"),
    )
