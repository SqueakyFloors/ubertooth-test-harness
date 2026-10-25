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
- ``-U all -<queries>`` and read-only identity queries without ``-U`` (with
  more than one board) print ``<index>: <line>`` for every attached board in
  enumeration order; ``-U<index>`` opens the board at that index of the same
  order, and an index with no board is refused with "no Ubertooth at index"
  (fork 0c7e9c0, SqueakyFloors/ubertooth#188). Any other option without
  ``-U`` refuses to pick a board.

LED reads (``-d``) can stall while LED specan runs
(SqueakyFloors/ubertooth#213), so the LED sampler retries a failed read once
and reports the retry on stderr; a read that fails twice in a row still fails
the case.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ubertooth_harness import ledblink
from ubertooth_harness.patterns import (
    BOARD_ID_PATTERN,
    COMPILE_INFO_PATTERN,
    FIRMWARE_PATTERN,
    PART_ID_PATTERN,
)
from ubertooth_harness.stateful import (
    Observation,
    StateCase,
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
    SQUELCH,
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
# Multi-board selection
#
# Every command is read-only (identity queries, -N, or a refused option), so
# the guard boards are only read; the guard checks prove it.

_LISTING_LINE = re.compile(r"^(?P<index>\d+): (?P<text>.*)$")
_SERIAL_RE = r"[0-9a-f]{32}"
IDENTITY_QUERIES = ("-v", "-V", "-s", "-p", "-b")
# Report lines per board for -vVspb, in ubertooth-util's report order.
_BLOCK_PATTERNS = (
    ("board ID", BOARD_ID_PATTERN),
    ("firmware", FIRMWARE_PATTERN),
    ("compile info", COMPILE_INFO_PATTERN),
    ("part ID", PART_ID_PATTERN),
    ("serial", rf"^Serial No: {_SERIAL_RE}$"),
)


def _listing(stdout):
    """(index, text) per line, or a reason when a line is not a listing line."""
    entries = []
    for line in stdout.splitlines():
        match = _LISTING_LINE.match(line)
        if match is None:
            return None, f"line is not '<index>: <value>': {line!r}"
        entries.append((int(match["index"]), match["text"]))
    return entries, ""


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
        indexes = [index for index, _text in entries]
        if indexes != list(range(len(entries))):
            return f"indices {indexes} are not 0..{len(entries) - 1} in order"
        return _same_boards(registered, [text for _index, text in entries])
    return check


def identity_listing_check(registered):
    """`-vVspb` listing: per board, in index order, the five report lines."""
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
            for (index, text), (label, pattern) in zip(block, _BLOCK_PATTERNS):
                if index != board:
                    return f"board {board} block has a line for index {index}"
                if not re.search(pattern, text, re.MULTILINE):
                    return f"board {board} {label} line {text!r} does not match"
            serials.append(block[-1][1].removeprefix("Serial No: "))
        return _same_boards(registered, serials)
    return check


def _index_key(index):
    return f"serial at listing index {index}"


def _multi_case(serial, case_id, name, steps, expected_state):
    return StateCase(case_id, name, tuple(steps), (_identity(serial),),
                     expected_state=expected_state)


def _listing_step(step_id, name, argv, check):
    return Step(step_id, name, argv, output_check=check)


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
            observe=Observation(_index_key(index),
                                rf"(?m)^{index}: (?P<value>{_SERIAL_RE})$"),
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
            "one '<index>: <serial>' line per registered board, indices in order",
        ),
        _multi_case(
            serial, "no-selector-lists-every-registered-serial",
            "-s without -U lists every registered board and only those",
            (_listing_step("list", "-s without -U", ("ubertooth-util", "-s"),
                           serials),),
            "one '<index>: <serial>' line per registered board, indices in order",
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
        cases += [
            _multi_case(
                serial, "all-lists-every-identity-query",
                f"-U all -{every} reports every query for every board",
                (_listing_step("list", f"-U all -{every}",
                               ("ubertooth-util", "-U", "all", f"-{every}"),
                               identities),),
                "five prefixed report lines per registered board, in index order",
            ),
            _multi_case(
                serial, "no-selector-lists-every-identity-query",
                f"-{every} without -U reports every query for every board",
                (_listing_step("list", f"-{every} without -U",
                               ("ubertooth-util", f"-{every}"), identities),),
                "five prefixed report lines per registered board, in index order",
            ),
            _multi_case(
                serial, "no-selector-other-option-refused",
                "-C without -U refuses to pick a board",
                (Step(
                    "refuse", "-C without -U", ("ubertooth-util", "-C"),
                    expected_returncodes=(1,),
                    output_patterns=(r"multiple Ubertooth devices found",)
                    + tuple(rf"Device \d+: Serial No: {s}" for s in registered),
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
