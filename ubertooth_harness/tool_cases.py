"""Case builders for ubertooth-specan, ubertooth-debug, and peerless
ubertooth-btle settings.

Command contracts come from the fork sources (host/ubertooth-tools/src and
host/libubertooth/src/ubertooth_callback.c). Every case records what it can
change, runs with the usual cleanup, and is guarded like the transition tests.

Some comprehensive cases assert the documented behavior that the fork does not
deliver yet; they fail until the linked fork issue is fixed:

- SqueakyFloors/ubertooth#173: a specan run first reports samples from the
  previous run's range (narrow-range cases after a wider run).
- SqueakyFloors/ubertooth#164 (fixed on fork main by #170, 89afc1c):
  ubertooth-debug built before it exits with the low byte of the last
  register value (any register whose low byte is not 0).
- SqueakyFloors/ubertooth#171: ubertooth-btle -a with a bad value, and -a
  combined with -v.
"""

from __future__ import annotations

import re
from pathlib import Path
import sys

from ubertooth_harness import specandump
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    Step,
    require,
)
from ubertooth_harness.transitions import (
    CRC_PATTERN,
    MODE_RESET_SETTINGS,
    RESET_TIMEOUT_S,
    RESTORED_SETTINGS,
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
# Output modes (cb_specan): default "<seconds>, <MHz>, <dBm>"; -g "<MHz> <dBm>"
# with a blank line after each sweep; -G "<seconds> <MHz> <dBm>" with the same
# blank lines; -d writes 3-byte binary records (MHz big-endian, int8 dBm) to
# the file instead of stdout. -v only adds stderr.

_LINE_FORMATS = {
    "default": re.compile(r"^(?P<t>\d+\.\d+), (?P<f>\d+), (?P<r>-?\d+)$"),
    "-g": re.compile(r"^(?P<f>\d+) (?P<r>-?\d+)$"),
    "-G": re.compile(r"^(?P<t>\d+\.\d+) (?P<f>\d+) (?P<r>-?\d+)$"),
}
# int8 dBm as the firmware reports it; anything outside is a parse problem.
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
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith(("HARD TIMEOUT", "Ubertooth stopped")):
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
                 dump_file=None, stderr_pattern=None):
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
        output_check=None if dump_file is not None else sweep_check(fmt, low, high),
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
        expected_state=(
            f"every reported frequency within {low}-{high} MHz and each one "
            "reported; channel, squelch, and LEDs restored"
        ),
        capture_dir=dump_file is not None,
    )


def _specan_argv(serial, options):
    return ("ubertooth-specan", "-U", serial) + tuple(options)


def spectrum_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Spectrum analyzer")
    seconds = duration_seconds(arguments["duration"])
    cases = [
        _specan_case(serial, "full-band", "Full band sweep (default range)",
                     (), "default", *BAND, seconds),
    ]
    if arguments["coverage"] == "comprehensive":
        # Each case whose range differs from the previous case's (narrow,
        # single, top, -g, -d) fails until SqueakyFloors/ubertooth#173 is
        # fixed: the run first reports one stale 16-sample packet. -G and -v
        # reuse the -g range, so they pass either way.
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
        ]
    return tuple(cases)


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
                                    codes=(1,), patterns=(r"ERROR: register address",))),
            _debug_case(serial, "reject-verbosity-3", "Reject -v3",
                        _debug_step(serial, "reject", "Reject -v3", ("-r", "0x00", "-v3"),
                                    codes=(1,),
                                    patterns=(r"error: verbosity must be 0-2 \(got 3\)",))),
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
                     output_patterns=(r"Resetting ubertooth device",), mutates=True),
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
            # Failed before SqueakyFloors/ubertooth#171 was fixed (09e9a46): -a returned before -v.
            _btle_case(serial, "access-address-with-crc", "-a -v reports both", (
                Step("read-both", "Read access address and CRC", _btle(serial, "-a", "-v"),
                     output_patterns=(_AA_PATTERN, CRC_PATTERN),
                     forbidden_patterns=(r"systime=",)),
            ), expected_state="access address and CRC both reported, no sniffing"),
            # Failed before SqueakyFloors/ubertooth#171 was fixed (09e9a46): a bad value set garbage.
            _btle_case(serial, "reject-bad-access-address", "Reject -azzzz", (
                Step("reject", "Reject -azzzz", _btle(serial, "-azzzz"),
                     expected_returncodes=(1,), mutates=True),
                _aa_read(serial, "verify-unchanged", "Verify access address unchanged",
                         "access address after rejection",
                         lambda context: require(context, "original access address")),
            ), expected_state="rejected with exit 1; access address unchanged"),
        ]
    return tuple(cases)
