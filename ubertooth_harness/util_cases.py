"""Case builders for ubertooth-util board-side modes and multi-board selection.

Command contracts come from the fork sources (host/ubertooth-tools/src/
ubertooth-util.c, host/libubertooth/src/ubertooth.c, and firmware/
bluetooth_rxtx):

- ``-I`` (identify) and ``-q[20-120]`` (LED spectrum analyzer) run on the
  board: the command stops any prior mode, starts the mode, prints its start
  line, and returns 0 at once. ``-S`` or any other mode option ends them.
  The identify blink toggles all three LEDs together every 167 ms
  (UBERTOOTH_IDENTIFY_TOGGLE_MS). ``-q<N>`` sets the threshold to -N dBm
  (bare ``-q`` is -85 dBm, UBERTOOTH_LED_SPECAN_N_DEFAULT); -120 dBm is the
  bottom of the CC2400's RSSI range, so ``-q120`` lights every LED, and -20
  dBm is far above ambient noise. Values outside 20-120 are refused on the
  host (Argument validation).
- While a mode runs, the configured channel and PA level are kept; stopping
  the mode runs the firmware idle reset (channel 2441 MHz, squelch -120, LEDs
  off; PA kept). The -q cases check it too since fork aa4f620
  (SqueakyFloors/ubertooth#191).
- ``-m`` reads the range-test result. A board that has not completed a range
  test since it started has none: it prints "no range test result yet" and
  exits 0 (fork 7180f74, SqueakyFloors/ubertooth#190).
- ``-e`` (repeater) on one board and ``-n`` (range test) on another run the
  two-board range test (firmware/bluetooth_rxtx/cc2400_rangetest.c); see
  "Two-device range test" below.
- ``-U all -<queries>`` and read-only identity queries without ``-U`` (with
  more than one board) print ``<label> (<index>): <value>`` for every attached
  board in enumeration order, so every line says what it is and which board it
  came from (SqueakyFloors/ubertooth#610). Compile info has no label of its
  own and keeps the bare ``<index>: <value>`` form. A board that could not be
  opened gets ``unavailable, <reason>`` as the value of every query asked of
  it. A query that failed on a board that did open gets ``read failed``,
  unless that report has a diagnosis of its own (an unknown board ID, say),
  which it prints in the value's place instead. Either way the ``error:``
  line goes to stderr, the other boards are still listed, and the exit status
  is 1. ``-U<index>`` opens the board at that
  index of the same order, and an index with no board is refused with
  "no Ubertooth at index"
  (fork 0c7e9c0, SqueakyFloors/ubertooth#188). Any other option without
  ``-U`` refuses to pick a board.

LED reads (``-d``) can stall while LED specan runs
(SqueakyFloors/ubertooth#213), so the LED sampler retries a failed read once
and reports the retry on stderr; a read that fails twice in a row still fails
the case.
"""

from __future__ import annotations

from dataclasses import replace
import re
import sys
from pathlib import Path

from ubertooth_harness import ledblink
from ubertooth_harness.patterns import (
    BOARD_ID_PATTERN,
    COMPILE_INFO_PATTERN,
    FIRMWARE_PATTERN,
    HOST_VERSION_TIMEOUT_S,
    PART_ID_PATTERN,
    serial_pattern,
)
from ubertooth_harness.stateful import (
    Observation,
    StateCase,
    StateCaseError,
    Step,
    no_diagnostics_on_stdout,
    nothing_on_stdout,
    require,
)
from ubertooth_harness.transitions import (
    BOOT_DEFAULT_CHANNEL,
    CHANNEL,
    LEDS,
    MODE_RESET_SETTINGS,
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
    guard_boards,
)


# ---------------------------------------------------------------------------
# LED sampling
#
# Three separate control reads make one -d report, so a sample taken while the
# identify blink toggles can mix states; the checks only need whole-on and
# whole-off samples to appear (blink) or every sample whole-on/off (steady).
# A -d read takes about 0.28 s, so a fixed 0.05 s pause gives a sample period
# close to the 0.334 s blink period; the blink sampler jitters its pauses.
#
# A read that fails (SqueakyFloors/ubertooth#213: a control request stalls
# while LED specan runs) is retried once; its partial output is dropped, and a
# "LED sample <i>: -d exited <code>, retrying" line goes to stderr so the run
# record shows every retry. A retry that fails too ends the sampler with exit 1.
#
# The steady checks (every LED on, or off) take LED_SAMPLES samples with the
# shell loop below. The blink check runs ledblink.py instead: it samples until
# a whole-on and a whole-off sample have both appeared, up to
# LED_BLINK_MAX_SAMPLES samples or LED_BLINK_DEADLINE_S seconds on a monotonic
# clock, the deadline covering reads in flight (stopped with SIGTERM), so it
# ends within LED_BLINK_DEADLINE_S + ledblink.GRACE_S. Its pauses are jittered
# by one blink period, so the samples cannot lock to one phase.

LED_SAMPLES = 12
LED_SAMPLE_INTERVAL_S = "0.05"
LED_BLINK_MAX_SAMPLES = 60
LED_BLINK_DEADLINE_S = 10
LED_SAMPLE_TIMEOUT_S = 30.0
LED_RETRY_LINE = r"LED sample \d+: -d exited \d+, retrying"
_LEDBLINK = str(Path(ledblink.__file__).resolve())
LED_SAMPLER = (
    'i=0; while [ "$i" -lt "$2" ]; do sleep "$3"; '
    'out=$("$0" -U "$1" -d); code=$?; '
    'if [ "$code" -ne 0 ]; then '
    'echo "LED sample $i: -d exited $code, retrying" >&2; '
    'out=$("$0" -U "$1" -d) || exit 1; fi; '
    'printf "%s\\n" "$out"; i=$((i + 1)); done'
)
ALL_ON, ALL_OFF = "1/1/1", "0/0/0"


def led_sampler_argv(serial: str) -> tuple[str, ...]:
    return (
        "sh", "-c", LED_SAMPLER, "ubertooth-util", serial,
        str(LED_SAMPLES), LED_SAMPLE_INTERVAL_S,
    )


def led_blink_sampler_argv(serial: str) -> tuple[str, ...]:
    return (
        sys.executable, _LEDBLINK, "ubertooth-util", serial,
        "--max-samples", str(LED_BLINK_MAX_SAMPLES),
        "--deadline", str(LED_BLINK_DEADLINE_S),
        "--interval", LED_SAMPLE_INTERVAL_S,
    )


def led_samples(stdout: str) -> list[str]:
    return [
        "/".join(match.group("usr", "rx", "tx"))
        for match in re.finditer(LEDS.pattern, stdout)
    ]


def _sample_count(samples) -> str:
    if len(samples) != LED_SAMPLES:
        return f"expected {LED_SAMPLES} LED samples, got {len(samples)}"
    return ""


def blink_check(stdout: str) -> str:
    """Identify is running: whole-on and whole-off samples both appear."""
    samples = led_samples(stdout)
    if not samples:
        return "no LED samples"
    if ALL_ON not in samples or ALL_OFF not in samples:
        return (f"LEDs did not blink in {ledblink.plural(len(samples), 'sample')} "
                "(USR/RX/TX samples: " + ", ".join(samples) + ")")
    return ""


def steady_check(expected: str, what: str):
    def check(stdout: str) -> str:
        samples = led_samples(stdout)
        reason = _sample_count(samples)
        if reason:
            return reason
        if any(sample != expected for sample in samples):
            return (
                f"LEDs not {what} in every sample (USR/RX/TX samples: "
                + ", ".join(samples) + ")"
            )
        return ""
    return check


def any_samples_check(stdout: str) -> str:
    return _sample_count(led_samples(stdout))


def _sample_step(serial, step_id, name, check) -> Step:
    # Room for a retry of every sample; the blink sampler stops by its own
    # deadline plus the SIGTERM grace, well before this.
    argv = (led_blink_sampler_argv(serial) if check is blink_check
            else led_sampler_argv(serial))
    return Step(step_id, name, argv, timeout_s=LED_SAMPLE_TIMEOUT_S,
                output_check=check)


# ---------------------------------------------------------------------------
# Board-side modes: -I and -q

MODE_SQUELCH = "-60"
# -I reports the blink once, on stderr, naming the selector so the stop can be
# pasted (fork fcadf34, ubertooth-util.md DIAGNOSTICS); there is no separate
# "Flashing LEDs" line any more.
def identify_start(serial: str) -> str:
    return (rf"(?m)^note: the board is blinking; stop it with ubertooth-util "
            rf"-U {re.escape(serial)} -S or any mode option$")


# The "Flashing LEDs on ubertooth device ..." line -I printed on stdout before
# fork fcadf34 removed it: a forbidden pattern, so no mode start (-I or any
# other) prints it on either stream.
REMOVED_FLASHING_LINE = r"Flashing LEDs"


def specan_start(threshold: int) -> str:
    return rf"Entering LED specan mode \(threshold -{threshold} dBm\)"


BLINK = ("blinks", blink_check)
LIT = ("lights every LED", steady_check(ALL_ON, "all on"))
SAMPLED = ("reports the LEDs", any_samples_check)


def _mode_case(serial, case_id, name, options, start, behavior, *, prior=None,
               extra_patterns=()):
    """Start a board-side mode over non-default settings, check it runs, stop
    it with -S, and check the stop reset and that it really stopped.

    ``prior`` starts another mode first, which the mode under test must end:
    the new mode starts from idle, so the channel is 2441 MHz while it runs.
    """
    target = CHANNEL.pick(0)
    label, check = behavior
    steps = [
        _stop(serial, "establish-idle", "Establish idle before reading"),
        PA_LEVEL.read_original(serial),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step(
            "set-non-defaults",
            "Set channel, squelch, and every LED away from the stop defaults",
            lambda context: _util(
                serial, CHANNEL.set_arg(target(context)),
                SQUELCH.set_arg(MODE_SQUELCH), "-d1",
            ),
            mutates=True,
            # Setters are silent, -z <n> too (no "Setting squelch" line).
            output_check=nothing_on_stdout,
        ),
        CHANNEL.verify(serial, "verify-channel-set", "Channel set before the mode",
                       "channel before mode", target),
        SQUELCH.verify(serial, "verify-squelch-set", "Squelch set before the mode",
                       "squelch before mode", MODE_SQUELCH),
    ]
    if prior is not None:
        prior_options, prior_start, prior_behavior = prior
        steps += [
            Step("start-prior-mode", "Start " + " ".join(prior_options),
                 _util(serial, *prior_options), output_patterns=(prior_start,),
                 forbidden_patterns=(REMOVED_FLASHING_LINE,),
                 output_check=no_diagnostics_on_stdout, mutates=True),
            _sample_step(serial, "sample-prior-mode",
                         f"{' '.join(prior_options)} {prior_behavior[0]}",
                         prior_behavior[1]),
        ]
    command = " ".join(options)
    steps += [
        Step("start-mode", f"Start {command}", _util(serial, *options),
             output_patterns=(start,) + tuple(extra_patterns),
             forbidden_patterns=(REMOVED_FLASHING_LINE,),
             output_check=no_diagnostics_on_stdout, mutates=True),
        _sample_step(serial, "sample-mode", f"{command} {label}", check),
    ]
    if prior is None:
        steps.append(CHANNEL.verify(serial, "verify-channel-during-mode",
                                    "Channel kept while the mode runs",
                                    "channel during mode", target))
    else:
        # Starting a mode first stops the prior one, which runs the idle reset.
        steps.append(CHANNEL.verify(serial, "verify-channel-during-mode",
                                    "Channel reset by ending the prior mode",
                                    "channel during mode", BOOT_DEFAULT_CHANNEL))
    steps += [
        PA_LEVEL.verify(serial, "verify-pa-level-during-mode",
                        "PA level kept while the mode runs", "pa-level during mode",
                        lambda context: require(context, PA_LEVEL.original)),
        _stop(serial, "mode-stop", "Stop the mode with -S"),
        _sample_step(serial, "sample-after-stop", "LEDs stay off after the stop",
                     steady_check(ALL_OFF, "off")),
        CHANNEL.verify(serial, "verify-channel-after-stop",
                       "Channel reset by the mode stop", "channel after stop",
                       BOOT_DEFAULT_CHANNEL),
        SQUELCH.verify(serial, "verify-squelch-after-stop",
                       "Squelch reset by the mode stop", "squelch after stop",
                       SQUELCH.after_mode_stop),
        PA_LEVEL.verify(serial, "verify-pa-level-after-stop",
                        "PA level kept by the mode stop", "pa-level after stop",
                        lambda context: require(context, PA_LEVEL.original)),
    ]
    stopped = "-S stops it (LEDs off, channel 2441, squelch -120, PA kept)"
    kept = "channel and PA kept" if prior is None else "channel 2441 and PA kept"
    return StateCase(
        case_id,
        name,
        tuple(steps),
        _cleanup(serial, MODE_RESET_SETTINGS + (PA_LEVEL,)),
        expected_state=(
            f"{command} returns at once and the board {label}; {kept} while it "
            f"runs; {stopped}; original channel, squelch, LEDs, and PA restored"
        ),
    )


# ---------------------------------------------------------------------------
# -m with no range-test result

RANGE_RESULT_NONE = r"(?m)^no range test result yet$"
RANGE_RESULT_VALUES = r"request PA level|request number|reply PA level|reply number|mismatch on byte"


def _range_result_case(serial, case_id, name):
    reads = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"),
    ) + _read_originals(serial, MODE_RESET_SETTINGS + (PA_LEVEL,))
    return StateCase(
        case_id,
        name,
        reads + (
            # No case ever runs a range test (it transmits), so after a reset
            # the board has no result.
            Step("reset", "Full reset (clears any range-test result)",
                 _util(serial, "-r"), timeout_s=RESET_TIMEOUT_S,
                 output_patterns=(r"Resetting ubertooth device",),
                 output_check=nothing_on_stdout, mutates=True),
            Step("verify-firmware-after-reset", "Firmware answers after reset",
                 _util(serial, "-v"), output_patterns=(FIRMWARE_PATTERN,)),
            Step(
                "range-result",
                "Range test result with no range test run",
                _util(serial, "-m"),
                output_patterns=(RANGE_RESULT_NONE,),
                forbidden_patterns=(RANGE_RESULT_VALUES, r"read failed", r"(?i)invalid"),
            ),
        ),
        _cleanup(serial, MODE_RESET_SETTINGS + (PA_LEVEL,), crc=True),
        expected_state=(
            "-m after a reset exits 0 saying there is no range test result yet, "
            "with no result values; every setting the reset changed restored"
        ),
    )


def util_mode_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Ubertooth util modes")
    identify = (("-I",), identify_start(serial), BLINK)
    # -120 dBm, the bottom of the radio's RSSI range: every reading lights.
    specan_lit = (("-q120",), specan_start(120), LIT)
    cases = [
        _mode_case(serial, "identify-blinks-until-stop", "-I blinks until -S",
                   *identify),
        _mode_case(serial, "led-specan-120-lights-leds",
                   "-q120 (threshold -120 dBm) lights every LED", *specan_lit),
        _range_result_case(serial, "range-result-none-after-reset",
                           "-m with no range-test result"),
    ]
    if arguments["coverage"] == "comprehensive":
        cases += [
            # -20 dBm is far above ambient noise, and bare -q (-85 dBm) lights
            # only for strong enough signals: both just run until -S.
            _mode_case(serial, "led-specan-20-runs-until-stop",
                       "-q20 runs until -S", ("-q20",), specan_start(20), SAMPLED),
            _mode_case(serial, "led-specan-default-runs-until-stop",
                       "Bare -q (threshold -85 dBm) runs until -S",
                       ("-q",), specan_start(85), SAMPLED),
            _mode_case(serial, "identify-replaces-led-specan",
                       "-I ends a running -q120 and blinks", *identify,
                       prior=specan_lit),
            _mode_case(serial, "led-specan-replaces-identify",
                       "-q120 ends a running -I and lights every LED", *specan_lit,
                       prior=identify),
            _mode_case(serial, "identify-after-serial-query",
                       "-s -I reports the serial, then blinks",
                       ("-s", "-I"), identify_start(serial), BLINK,
                       extra_patterns=(rf"Serial No: {re.escape(serial)}",)),
        ]
    return tuple(cases)


# ---------------------------------------------------------------------------
# Two-device range test
#
# How the firmware runs it (firmware/bluetooth_rxtx/cc2400_rangetest.c):
#
# - The responder runs the repeater (-e). It listens on the board's channel.
#   When a request arrives with a good CRC, it waits a moment and sends it
#   back as a reply.
# - The sender runs the range test (-n). It sends a 22-byte request that
#   carries its own serial number. It sends 16 copies at each PA level from 0
#   (-25 dBm) up to 7 (0 dBm), then listens for the reply.
# - The repeater copies the PA level and copy number of the first request it
#   received into the reply. It sends its reply the same way, 16 copies at
#   each PA level, and adds its own level and copy number.
# - The sender keeps the first reply with a good CRC and compares the reply's
#   first 18 bytes with its request. The test then ends by itself and the
#   board goes idle. The repeater keeps running until -S.
#
# `-m` on the sender then prints four lines: the lowest PA level and copy
# number that reached the repeater ("request"), and the same for the way back
# ("reply"). A reply that differs from the request is printed as "error:
# invalid range test: mismatch on byte <n>" with exit 1. The repeater itself
# keeps no result, so `-m` on the responder still says "no range test result
# yet".
#
# Both modes run on the board's stored channel (*chan_ptr: transmit on it,
# receive with FSDIV one below it), so the two boards must be on the same
# one. Leaving either mode runs the idle reset in bluetooth_rxtx.c
# (cc2400_idle(): channel = 2441, LEDs off): the range test asks for idle
# itself when it ends, and the repeater's -S does. The test therefore sets
# both boards to RANGE_CHANNEL (2410, not the 2441 that -r leaves) before
# -e and -n, so the channel reads after the test show that reset ran, not
# just the value -r left behind.
#
# The check: a fresh result (both boards reset first, so neither has an old
# one), with each PA level between 0 and the plan's "maximum PA level" and
# each copy number between 0 and 15. The boards in a lab sit centimetres
# apart, so a working link gets through at a low level; the plan sets how
# low. The responder must answer USB with its RX LED on once -e started and
# before -n, so a failing run tells a wedged repeater from a wedged sender,
# and again after the sender's wait; -S must stop it (fork #26 is the
# firmware bug where the repeater answered nothing).

RANGE_PA_LEVELS = 8  # PA levels 0-7 (FREND.PA_LEVEL)
# Both boards run the test here: any channel other than the 2441 that -r and
# the idle reset leave.
RANGE_CHANNEL = "2410"
RANGE_COPIES = 16  # copies sent at each PA level
# A working round trip takes well under a second: 128 short packets each way
# plus the repeater's turnaround delay. The sender is polled for this long.
RANGE_RESULT_DEADLINE_S = 10
RANGE_POLL_INTERVAL_S = "0.25"
# One -m to a board that does not answer USB takes about 1 s (libubertooth's
# serial-read timeout), so the last poll may end that long after the
# deadline; the rest is headroom.
RANGE_POLL_TIMEOUT_S = RANGE_RESULT_DEADLINE_S + 5.0
RANGE_RESULT = (
    r"(?m)^request PA level : (?P<request_pa>\d+)\n"
    r"request number   : (?P<request_number>\d+)\n"
    r"reply PA level   : (?P<reply_pa>\d+)\n"
    r"reply number     : (?P<reply_number>\d+)$"
)
RANGE_MISMATCH = r"invalid range test: mismatch on byte \d+"
# The bytes cc2400_rangetest() compares (reply byte n against its request).
RANGE_COMPARED_FIELDS = (
    ((0, 0), "length"),
    ((1, 1), "packet type"),
    ((2, 17), "sender serial number"),
)
REPEATER_START = r"(?m)^Starting repeater$"
RANGE_TEST_START = r"(?m)^Starting range test$"
# Reads the sender's -m until it reports a result or a mismatch, or the
# deadline passes. Each read whose output or exit status differs from the
# previous one is logged as one line ("read <n>: exit <code>: <output>") on
# stdout, so the record shows how the board answered while the test ran
# without a line for every poll. Exits 0 once a result or a mismatch is
# seen; at the deadline prints "no range test result within ..." and exits
# 1. range_poll_check turns that log into the step's failure reason.
# Arguments: ubertooth-util, serial, deadline (s), interval (s).
RANGE_POLL = (
    'end=$(( $(date +%s) + $2 )); i=0; last=; '
    'while :; do i=$((i + 1)); '
    'out=$("$0" -U "$1" -m 2>&1); code=$?; '
    'this="exit $code: $(printf "%s" "$out" | tr "\\n" " ")"; '
    '[ "$this" = "$last" ] || printf "read %s: %s\\n" "$i" "$this"; last=$this; '
    'case "$out" in *"request PA level"*|*"mismatch on byte"*) exit 0;; esac; '
    'if [ "$(date +%s)" -ge "$end" ]; then '
    'echo "no range test result within $2 s ($i reads)"; exit 1; fi; '
    'sleep "$3"; done'
)


def range_poll_argv(serial: str) -> tuple[str, ...]:
    return ("sh", "-c", RANGE_POLL, "ubertooth-util", serial,
            str(RANGE_RESULT_DEADLINE_S), RANGE_POLL_INTERVAL_S)


def range_field(byte: int) -> str:
    """The request field a mismatching byte belongs to."""
    for (first, last), name in RANGE_COMPARED_FIELDS:
        if first <= byte <= last:
            return name if first == last else f"{name} byte {byte - first}"
    return "a byte the firmware does not compare"


def range_poll_check(stdout: str) -> str:
    """The poll log: a result passes; a mismatch or no result names why."""
    reads = re.findall(r"(?m)^read \d+: (.*)$", stdout)
    mismatch = re.search(r"mismatch on byte (\d+)", stdout)
    if mismatch is not None:
        byte = int(mismatch[1])
        return (f"the reply differs from the request at byte {byte} "
                f"({range_field(byte)})")
    if "request PA level" in stdout:
        return ""
    deadline = re.search(r"(?m)^no range test result within .*$", stdout)
    summary = deadline[0] if deadline else "the poll ended without a result"
    last = reads[-1] if reads else "no read logged"
    return f"{summary}; last read: {last}"


def range_result_check(maximum_pa: int):
    """Sender -m: the four result lines, levels 0..maximum_pa, copies 0..15."""
    def check(stdout: str) -> str:
        match = re.search(RANGE_RESULT, stdout)
        if match is None:
            first = stdout.strip().splitlines()[0] if stdout.strip() else "nothing"
            return f"no range test result (stdout: {first!r})"
        values = {name: int(value) for name, value in match.groupdict().items()}
        problems = []
        for direction in ("request", "reply"):
            level = values[f"{direction}_pa"]
            number = values[f"{direction}_number"]
            if level > maximum_pa:
                problems.append(
                    f"{direction} got through only at PA level {level}, above the "
                    f"plan's maximum {maximum_pa}")
            if number >= RANGE_COPIES:
                problems.append(
                    f"{direction} copy number {number} is not 0-{RANGE_COPIES - 1}")
        return "; ".join(problems)
    return check


def _board_settings(role: str):
    """The restored settings with step IDs and record keys for one board."""
    return tuple(replace(setting, key=f"{role}-{setting.key}")
                 for setting in RESTORED_SETTINGS)


def _role_cleanup(role: str, serial: str, settings) -> tuple[Step, ...]:
    """Stop, then restore CRC, channel, PA, squelch, LEDs, and check identity."""
    original_crc = f"original {role} crc"
    steps = [
        _stop(serial, f"{role}-cleanup-stop", f"{role.capitalize()}: return to idle"),
        Step(f"{role}-restore-crc", f"{role.capitalize()}: restore original CRC",
             lambda context: _btle(serial, f"-k{require(context, original_crc)}"),
             mutates=True),
        _crc_read(serial, f"{role}-verify-restored-crc",
                  f"{role.capitalize()}: verify restored CRC", f"restored {role} crc",
                  lambda context: require(context, original_crc)),
    ]
    for setting in settings:
        steps.extend(setting.restore(serial))
    steps.append(Step(
        f"{role}-restored-device-identity", f"{role.capitalize()}: restored device identity",
        _util(serial, "-s"), output_patterns=(serial_pattern(serial),)))
    return tuple(steps)


def _range_serials(resource_identities):
    """Sender and responder serials: full, distinct, never read as an index."""
    serials = []
    for role in ("sender", "responder"):
        try:
            serial = str(resource_identities[role]["serial"]).lower()
        except KeyError as error:
            raise StateCaseError(
                f"Two-device range test requires a registered {role} serial") from error
        # libubertooth matches -U against the start of a serial and reads an
        # all-digit value as a device index, so only a full serial is sure to
        # reach this board and no other.
        if not re.fullmatch(r"[0-9a-f]{32}", serial) or serial.isdigit():
            raise StateCaseError(
                f"Two-device range test needs a full 32-hex-digit {role} serial; "
                f"registered '{serial}'")
        serials.append(serial)
    if serials[0] == serials[1]:
        raise StateCaseError("Two-device range test needs two different boards")
    return tuple(serials)


def range_test_cases(arguments, _resources, resource_identities):
    sender, responder = _range_serials(resource_identities)
    maximum_pa = arguments["maximum PA level"]
    boards = (("sender", sender), ("responder", responder))
    settings = {role: _board_settings(role) for role, _serial in boards}
    steps = []
    for role, serial in boards:
        label = role.capitalize()
        steps += [
            _stop(serial, f"{role}-establish-idle", f"{label}: establish idle"),
            _crc_read(serial, f"{role}-read-original-crc", f"{label}: read original CRC",
                      f"original {role} crc"),
            *_read_originals(serial, settings[role]),
        ]
    for role, serial in boards:
        label = role.capitalize()
        steps += [
            Step(f"{role}-reset", f"{label}: full reset (clears any range-test result)",
                 _util(serial, "-r"), timeout_s=RESET_TIMEOUT_S,
                 output_patterns=(r"Resetting ubertooth device",),
                 output_check=nothing_on_stdout, mutates=True),
            Step(f"{role}-firmware-after-reset", f"{label}: firmware answers after reset",
                 _util(serial, "-v"), output_patterns=(FIRMWARE_PATTERN,)),
            Step(f"{role}-no-result-before", f"{label}: no range-test result before the test",
                 _util(serial, "-m"), output_patterns=(RANGE_RESULT_NONE,),
                 forbidden_patterns=(RANGE_RESULT_VALUES,)),
        ]
    def setting(role, base):
        return next(item for item in settings[role] if item.key == f"{role}-{base.key}")

    sender_leds, sender_channel = setting("sender", LEDS), setting("sender", CHANNEL)
    responder_leds = setting("responder", LEDS)
    responder_channel = setting("responder", CHANNEL)
    channels = {"sender": sender_channel, "responder": responder_channel}
    for role, serial in boards:
        label = role.capitalize()
        steps += [
            Step(f"{role}-set-test-channel", f"{label}: set the test channel {RANGE_CHANNEL} MHz",
                 _util(serial, *channels[role].set_options(RANGE_CHANNEL)),
                 output_check=nothing_on_stdout, mutates=True),
            channels[role].verify(serial, f"{role}-test-channel",
                                  f"{label}: on the test channel",
                                  f"{role} test channel", RANGE_CHANNEL),
        ]

    def repeating(step_id, name):
        # The repeater lights RX while it listens; reading the LEDs also shows
        # the board answers USB with the repeater running.
        return Step(step_id, name, _util(responder, "-d"),
                    output_patterns=(r"(?m)^RX LED status : 1$",))

    steps += [
        Step("responder-start-repeater", "Responder: start the repeater (-e)",
             _util(responder, "-e"), output_patterns=(REPEATER_START,),
             output_check=nothing_on_stdout, mutates=True),
        # Before -n: a failure here is the repeater, not the sender.
        repeating("responder-repeating",
                  "Responder: repeating (RX LED on) and answering USB before -n"),
        responder_channel.verify(responder, "responder-channel-while-repeating",
                                 "Responder: the repeater keeps the test channel",
                                 "responder channel while repeating", RANGE_CHANNEL),
        Step("sender-start-range-test", "Sender: start the range test (-n)",
             _util(sender, "-n"), output_patterns=(RANGE_TEST_START,),
             output_check=nothing_on_stdout, mutates=True),
        Step("sender-wait-for-result",
             f"Sender: read -m until a result appears (up to {RANGE_RESULT_DEADLINE_S} s)",
             range_poll_argv(sender), timeout_s=RANGE_POLL_TIMEOUT_S,
             # The poll's own exit is not the verdict; its log is.
             expected_returncodes=(0, 1), output_check=range_poll_check),
        Step("sender-range-result", "Sender: range-test result",
             _util(sender, "-m"), output_check=range_result_check(maximum_pa),
             forbidden_patterns=(RANGE_MISMATCH,)),
        repeating("responder-repeating-after-test",
                  "Responder: still repeating (RX LED on) and answering USB after the test"),
        Step("responder-no-result", "Responder: the repeater keeps no result",
             _util(responder, "-m"), output_patterns=(RANGE_RESULT_NONE,),
             forbidden_patterns=(RANGE_RESULT_VALUES,)),
        sender_leds.verify(sender, "sender-idle-after-test",
                           "Sender: LEDs off once -n ended by itself",
                           "sender leds after test", "0/0/0"),
        sender_channel.verify(sender, "sender-channel-after-test",
                              "Sender: channel back to 2441 MHz (idle reset) once the test ended",
                              "sender channel after test", BOOT_DEFAULT_CHANNEL),
        _stop(responder, "responder-stop", "Responder: stop the repeater with -S"),
        responder_leds.verify(responder, "responder-leds-after-stop",
                              "Responder: LEDs off after the stop",
                              "responder leds after stop", "0/0/0"),
        responder_channel.verify(responder, "responder-channel-after-stop",
                                 "Responder: channel back to 2441 MHz (idle reset) after -S",
                                 "responder channel after stop", BOOT_DEFAULT_CHANNEL),
    ]
    return (
        StateCase(
            "round-trip",
            "Sender -n with the responder repeating (-e) reports a clean round trip",
            tuple(steps),
            # Responder first: it is the board still transmitting.
            _role_cleanup("responder", responder, settings["responder"])
            + _role_cleanup("sender", sender, settings["sender"]),
            expected_state=(
                "the sender's -m reports request and reply PA levels 0-"
                f"{maximum_pa} and copy numbers 0-{RANGE_COPIES - 1}; the sender is "
                f"idle at 2441 MHz once the test ends (both ran at {RANGE_CHANNEL}); "
                "the responder answers USB while repeating, before -n and after "
                "the test, keeps no result, and -S returns it to idle at 2441 MHz; "
                "every setting of both boards restored"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Multi-board selection
#
# Every command is read-only (identity queries, -N, or a refused option), so
# the guard boards are only read; the guard checks prove it.

# A listed line carries its report label and the board's index: "Firmware
# version (0): git-abc (API:1.37)". The tool falls back to the bare
# "0: <value>" form for a report line that does not start with its label, so
# that form is parsed too; every query the listing can show does carry a
# label, and each check says which one it expects per line, so a regression to
# the old unlabelled listing is rejected rather than accepted.
_LABELLED_LINE = re.compile(r"^(?P<label>[A-Za-z][^(]*?) \((?P<index>\d+)\): (?P<text>.*)$")
_BARE_LINE = re.compile(r"^(?P<index>\d+): (?P<text>.*)$")
# Values that stand in for a reading rather than carry one: a board the tool
# could not open at all, and a query whose read failed on a board it did open.
_UNAVAILABLE = "unavailable, "
_READ_FAILED = "read failed"
_SERIAL_RE = r"[0-9a-f]{32}"
_SERIAL_LABEL = "Serial No"
# libubertooth's refusal when several boards are attached and none was
# selected: one line that says what to do, then the boards, indented. Since
# fork 753269b (#655) the first line carries the "error: " prefix and says -U
# takes a serial too, and a listed board is "  Device 0: <serial>"; it used to
# be "multiple Ubertooth devices found! Use '-U' to specify device number"
# with "  Device 0: Serial No: <serial>" under it.
MULTIPLE_BOARDS = r"(?m)^" + re.escape(
    "error: multiple Ubertooth devices found; select one with -U <index|serial>"
) + r"$"
# One board of that refusal's list, as a whole line, indented by the two
# spaces libubertooth prints ("  Device %d: %s\n").
LISTED_BOARD = r"(?m)^ {2}Device %s: %s$"


def listed_board(index: str, serial: str) -> str:
    return LISTED_BOARD % (index, re.escape(serial))


# Report lines per board for -bvps, in ubertooth-util's report order: the
# query, its name here, the label it is listed under, and the line it must
# match. These are the four identity queries the listing can show
# (QUERY_COUNT in the fork's ubertooth-util.c). -V is not among them: it is
# the version command, answers for every board itself, and ends the command
# before the listing is reached (fork f898fe5, SqueakyFloors/ubertooth#813);
# version_listing_check() below covers that answer. This is the one place the
# harness states the tool's report protocol; the tests derive their fake from
# it rather than repeating it.
_BLOCK_PATTERNS = (
    ("-b", "board ID", "Board ID Number", BOARD_ID_PATTERN),
    ("-v", "firmware", "Firmware version", FIRMWARE_PATTERN),
    ("-p", "part ID", "Part ID", PART_ID_PATTERN),
    ("-s", "serial", _SERIAL_LABEL, rf"^{_SERIAL_LABEL}: {_SERIAL_RE}$"),
)
# The -V answer's own lines, in the order it prints them per board. The host
# tools' line comes first, once, and the libbtbb path line beside it is not
# about a board at all.
_HOST_VERSION_PREFIX = "Host tools version: "
_LIBBTBB_PATH_PREFIX = "libbtbb loaded from: "
_VERSION_REPORTS = (
    ("Firmware version", FIRMWARE_PATTERN),
    ("Firmware build", COMPILE_INFO_PATTERN),
)
_VERSION_LINE = re.compile(
    r"^(?P<label>Firmware version|Firmware build) \((?P<index>\d+)\): (?P<text>.*)$"
)
# The label each identity query is listed under, in report order.
LISTING_LABELS = tuple((query, label)
                       for query, _name, label, _pattern in _BLOCK_PATTERNS)
IDENTITY_QUERIES = tuple(query for query, _label in LISTING_LABELS)


def _listing(stdout):
    """(index, label, value) per listed line; label is "" for a line that
    carries the bare index prefix. Returns a reason instead when a line is not
    a listing line, or when a line stands in for a reading the tool could not
    take."""
    entries = []
    for line in stdout.splitlines():
        match = _LABELLED_LINE.match(line)
        label = match["label"] if match is not None else ""
        if match is None:
            match = _BARE_LINE.match(line)
            if match is None:
                return None, ("line is not '<label> (<index>): <value>' or "
                              f"'<index>: <value>': {line!r}")
        value = match["text"]
        if value.startswith(_UNAVAILABLE):
            return None, f"board {match['index']} could not be opened: {line!r}"
        if value.endswith(_READ_FAILED):
            return None, f"board {match['index']} query read failed: {line!r}"
        entries.append((int(match["index"]), label, value))
    return entries, ""


def _single_board_line(label, value):
    """A listed line put back into the form one board prints, so one set of
    patterns covers both."""
    return f"{label}: {value}" if label else value


def _same_boards(registered, listed):
    missing = sorted(set(registered) - set(listed))
    extra = sorted(set(listed) - set(registered))
    if missing or extra or len(listed) != len(set(listed)):
        return (
            f"listed serials {listed} are not exactly the registered boards "
            f"{sorted(registered)} (missing {missing}, unregistered {extra})"
        )
    return ""


def serial_listing_check(registered):
    """`-s` listing: one line per board, indices 0..n-1 in order, and exactly
    the registered serials."""
    def check(stdout):
        entries, reason = _listing(stdout)
        if reason:
            return reason
        indexes = [index for index, _label, _value in entries]
        if indexes != list(range(len(entries))):
            return f"indices {indexes} are not 0..{len(entries) - 1} in order"
        for index, label, _value in entries:
            if label != _SERIAL_LABEL:
                return (f"the line for board {index} is labelled {label!r}, "
                        f"not {_SERIAL_LABEL!r}")
        return _same_boards(registered,
                            [value for _index, _label, value in entries])
    return check


def version_listing_check(count):
    """`-V` with several boards: the host tools' version, then the firmware
    version and build of every board, each labelled with the board's index.

    `-V` is the version command, so it is not part of the identity listing:
    since fork f898fe5 it ends the command and reports every attached board
    itself. The answer carries no serial, so which board is which cannot be
    checked here; the identity listing does that.
    """
    def check(stdout):
        lines = stdout.splitlines()
        if not lines or not lines[0].startswith(_HOST_VERSION_PREFIX):
            first = lines[0] if lines else ""
            return f"the first line is not the host tools' version: {first!r}"
        reports = [line for line in lines[1:]
                   if not line.startswith(_LIBBTBB_PATH_PREFIX)]
        expected = [(label, index, pattern) for index in range(count)
                    for label, pattern in _VERSION_REPORTS]
        if len(reports) != len(expected):
            return (f"{len(reports)} report lines is not "
                    f"{len(_VERSION_REPORTS)} per board for {count} boards")
        for line, (label, index, pattern) in zip(reports, expected):
            match = _VERSION_LINE.match(line)
            if match is None:
                return ("line is not '<label> (<index>): <value>': "
                        f"{line!r}")
            if (match["label"], int(match["index"])) != (label, index):
                return (f"expected the {label} of board {index} here, "
                        f"got {line!r}")
            value = match["text"]
            if value.startswith(_UNAVAILABLE):
                return f"board {index} could not be opened: {line!r}"
            if value.endswith(_READ_FAILED):
                return f"board {index} read failed: {line!r}"
            if not re.search(pattern, _single_board_line(label, value)):
                return f"board {index} {label} line {line!r} does not match"
        return ""
    return check


def identity_listing_check(registered):
    """`-bvps` listing: per board, in index order, the four report lines."""
    def check(stdout):
        entries, reason = _listing(stdout)
        if reason:
            return reason
        per_board = len(_BLOCK_PATTERNS)
        if len(entries) % per_board:
            return f"{len(entries)} lines is not {per_board} per board"
        serials = []
        for board in range(len(entries) // per_board):
            block = entries[board * per_board:(board + 1) * per_board]
            for entry, expected in zip(block, _BLOCK_PATTERNS):
                index, label, value = entry
                _query, name, expected_label, pattern = expected
                if index != board:
                    return f"board {board} block has a line for index {index}"
                if label != expected_label:
                    return (f"board {board} {name} line is labelled {label!r}, "
                            f"not {expected_label!r}")
                line = _single_board_line(label, value)
                if not re.search(pattern, line, re.MULTILINE):
                    return f"board {board} {name} line {line!r} does not match"
            serials.append(block[-1][2])
        return _same_boards(registered, serials)
    return check


def _index_key(index):
    return f"serial at listing index {index}"


def _multi_case(serial, case_id, name, steps, expected_state):
    return StateCase(case_id, name, tuple(steps), (_identity(serial),),
                     expected_state=expected_state)


def _listing_step(step_id, name, argv, check):
    # A listing reads every attached board, and -V costs a libusb control
    # timeout or two for one that does not answer, so the usual 5 s is not
    # enough on a host with several boards (patterns.py).
    return Step(step_id, name, argv, timeout_s=HOST_VERSION_TIMEOUT_S,
                output_check=check)


def _single_board_skip(serial):
    reason = ("skipped: Multi-board selection needs two or more registered "
              "Ubertooths on the host")
    return StateCase(
        "needs-two-boards",
        "Multi-board selection needs two registered boards",
        (Step("count", "Count attached boards", ("ubertooth-util", "-N"),
              observe=Observation("attached boards", r"(?m)^(?P<value>\d+)$",
                                  reject=lambda _value: reason)),),
        (_identity(serial),),
        expected_state="skipped on a host with one registered Ubertooth",
    )


def multi_board_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Multi-board selection")
    guards = guard_boards(resource_identities)
    if not guards:
        # A host with one registered board (plans use hosts: each) skips the
        # test instead of failing the whole run at planning time.
        return (_single_board_skip(serial),)
    registered = (serial,) + tuple(board.serial for board in guards)
    count = len(registered)
    serials = serial_listing_check(registered)

    def lookup(index):
        return lambda context: require(context, _index_key(index))

    index_steps = [
        Step("count", "-N counts every registered board", ("ubertooth-util", "-N"),
             output_patterns=(rf"(?m)^{count}$",)),
    ]
    for index in range(count):
        index_steps.append(Step(
            f"list-index-{index}", f"-U all -s names the board at index {index}",
            ("ubertooth-util", "-U", "all", "-s"),
            output_check=serials,
            observe=Observation(
                _index_key(index),
                rf"(?m)^{_SERIAL_LABEL} \({index}\): (?P<value>{_SERIAL_RE})$"),
        ))
    for index in range(count):
        index_steps.append(Step(
            f"select-index-{index}", f"-U{index} -s opens the board listed at {index}",
            ("ubertooth-util", f"-U{index}", "-s"),
            observe=Observation(f"serial selected by -U{index}",
                                rf"(?m)^Serial No: (?P<value>{_SERIAL_RE})$",
                                lookup(index)),
        ))
    cases = [
        _multi_case(
            serial, "all-lists-every-registered-serial",
            "-U all -s lists every registered board and only those",
            (_listing_step("list", "-U all -s", ("ubertooth-util", "-U", "all", "-s"),
                           serials),),
            "one 'Serial No (<index>): <serial>' line per registered board, "
            "indices in order",
        ),
        _multi_case(
            serial, "no-selector-lists-every-registered-serial",
            "-s without -U lists every registered board and only those",
            (_listing_step("list", "-s without -U", ("ubertooth-util", "-s"),
                           serials),),
            "one 'Serial No (<index>): <serial>' line per registered board, "
            "indices in order",
        ),
        _multi_case(
            serial, "index-selects-listed-board",
            "-U<index> opens the board that index names in the listing",
            index_steps,
            "-N counts every board and each -U<index> reads the serial "
            "-U all -s lists at that index",
        ),
    ]
    if arguments["coverage"] == "comprehensive":
        every = "".join(option[1] for option in IDENTITY_QUERIES)
        identities = identity_listing_check(registered)
        versions = version_listing_check(count)
        per_board = len(_BLOCK_PATTERNS)
        listed = f"{per_board} labelled report lines per registered board, in index order"
        version_state = ("the host tools' version, then the firmware version and "
                         "build of every registered board, in index order")
        cases += [
            _multi_case(
                serial, "all-lists-every-identity-query",
                f"-U all -{every} reports every query for every board",
                (_listing_step("list", f"-U all -{every}",
                               ("ubertooth-util", "-U", "all", f"-{every}"),
                               identities),),
                listed,
            ),
            _multi_case(
                serial, "no-selector-lists-every-identity-query",
                f"-{every} without -U reports every query for every board",
                (_listing_step("list", f"-{every} without -U",
                               ("ubertooth-util", f"-{every}"), identities),),
                listed,
            ),
            # -V is the version command: it answers for every board itself,
            # with -U all and with no selector alike, and ends the command.
            _multi_case(
                serial, "all-reports-every-board-version",
                "-U all -V reports the version of every board",
                (_listing_step("list", "-U all -V",
                               ("ubertooth-util", "-U", "all", "-V"), versions),),
                version_state,
            ),
            _multi_case(
                serial, "no-selector-reports-every-board-version",
                "-V without -U reports the version of every board",
                (_listing_step("list", "-V without -U", ("ubertooth-util", "-V"),
                               versions),),
                version_state,
            ),
            _multi_case(
                serial, "no-selector-other-option-refused",
                "-C without -U refuses to pick a board",
                (Step(
                    "refuse", "-C without -U", ("ubertooth-util", "-C"),
                    expected_returncodes=(1,),
                    output_patterns=(MULTIPLE_BOARDS,)
                    + tuple(listed_board(r"\d+", s) for s in registered),
                    forbidden_patterns=(r"Current frequency",),
                ),),
                "exit 1 listing every board; no channel read from any board",
            ),
            # No option is given, so a regression that opens whatever USB
            # device is first on the bus only claims an interface and sends
            # no request (SqueakyFloors/ubertooth#188, fixed in 0c7e9c0).
            _multi_case(
                serial, "index-past-end-refused",
                f"-U{count} (past the last board) is refused",
                (Step(
                    "refuse", f"-U{count} with {count} boards",
                    ("ubertooth-util", f"-U{count}"),
                    expected_returncodes=(1,),
                    output_patterns=(
                        rf"error: no Ubertooth at index {count} \({count} attached\)",
                    ),
                ),),
                "an index with no board opens nothing and exits 1 saying how "
                "many boards are attached",
            ),
        ]
    return tuple(cases)
