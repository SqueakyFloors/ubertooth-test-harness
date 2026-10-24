"""Case builders for ubertooth-util board-side modes and multi-board selection.

Command contracts come from the fork sources (host/ubertooth-tools/src/
ubertooth-util.c, host/libubertooth/src/ubertooth.c, and firmware/
bluetooth_rxtx):

- ``-I`` (identify) and ``-q[1-225]`` (LED spectrum analyzer) run on the
  board: the command stops any prior mode, starts the mode, prints its start
  line, and returns 0 at once. ``-S`` or any other mode option ends them.
  The identify blink toggles all three LEDs together every 167 ms
  (UBERTOOTH_IDENTIFY_TOGGLE_MS). ``-q<N>`` sets the threshold to -N dBm
  (bare ``-q`` is 225), so N >= 182 is at or below the lowest int8 RSSI and
  every reading lights its LED.
- While a mode runs, the configured channel and PA level are kept; stopping
  the mode runs the firmware idle reset (channel 2441 MHz, squelch -120, LEDs
  off; PA kept). Smoke checks only that -S ends -q, not its reset (#191).
- ``-m`` reads the range-test result. A board that has not completed a range
  test since it started has none; the read itself succeeds (exit 0).
- ``-U all -<queries>`` and read-only identity queries without ``-U`` (with
  more than one board) print ``<index>: <line>`` for every attached board in
  enumeration order; ``-U<index>`` opens the board at that index of the same
  order. Any other option without ``-U`` refuses to pick a board.

Some comprehensive cases assert the documented behavior that the fork does not
deliver yet; they fail until the linked fork issue is fixed:

- SqueakyFloors/ubertooth#191: stopping -q skips the idle reset, so the LEDs
  stay on and channel and squelch keep their values.
- SqueakyFloors/ubertooth#189: the firmware stores the LED-specan threshold
  in an int8, so -q183 to -q225 and bare -q never light an LED.
- SqueakyFloors/ubertooth#190: -m with no result says "invalid range test
  result" instead of saying there is no result.
- SqueakyFloors/ubertooth#188: with several boards, -U<index> past the end
  opens another USB device and exits 0.
"""

from __future__ import annotations

import re

from ubertooth_harness.patterns import (
    BOARD_ID_PATTERN,
    COMPILE_INFO_PATTERN,
    FIRMWARE_PATTERN,
    PART_ID_PATTERN,
)
from ubertooth_harness.stateful import Observation, StateCase, Step, require
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
# The interval keeps the sample period (about 0.12 s with the command time)
# away from the 0.334 s blink period, so the samples cannot alias to one phase.

LED_SAMPLES = 12
LED_SAMPLE_INTERVAL_S = "0.05"
LED_SAMPLER = (
    'i=0; while [ "$i" -lt "$2" ]; do sleep "$3"; '
    '"$0" -U "$1" -d || exit 1; i=$((i + 1)); done'
)
ALL_ON, ALL_OFF = "1/1/1", "0/0/0"


def led_sampler_argv(serial: str) -> tuple[str, ...]:
    return (
        "sh", "-c", LED_SAMPLER, "ubertooth-util", serial,
        str(LED_SAMPLES), LED_SAMPLE_INTERVAL_S,
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
    reason = _sample_count(samples)
    if reason:
        return reason
    if ALL_ON not in samples or ALL_OFF not in samples:
        return "LEDs did not blink (USR/RX/TX samples: " + ", ".join(samples) + ")"
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
    return Step(step_id, name, led_sampler_argv(serial), timeout_s=10.0,
                output_check=check)


# ---------------------------------------------------------------------------
# Board-side modes: -I and -q

MODE_SQUELCH = "-60"
IDENTIFY_START = (
    r"Flashing LEDs on ubertooth device \S+\n"
    r"The board is blinking; stop it with ubertooth-util -S or any mode option"
)


def specan_start(threshold: int) -> str:
    return rf"Entering LED specan mode \(RSSI {threshold}\)"


BLINK = ("blinks", blink_check)
LIT = ("lights every LED", steady_check(ALL_ON, "all on"))
SAMPLED = ("reports the LEDs", any_samples_check)


def _mode_case(serial, case_id, name, options, start, behavior, *, prior=None,
               extra_patterns=(), stop_resets=True):
    """Start a board-side mode over non-default settings, check it runs, stop
    it with -S, and check the stop reset and that it really stopped.

    ``prior`` starts another mode first, which the mode under test must end.
    ``stop_resets=False`` checks only that the stop ended the mode, not the
    idle reset (for -q until SqueakyFloors/ubertooth#191 is fixed).
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
                 mutates=True),
            _sample_step(serial, "sample-prior-mode",
                         f"{' '.join(prior_options)} {prior_behavior[0]}",
                         prior_behavior[1]),
        ]
    command = " ".join(options)
    steps += [
        Step("start-mode", f"Start {command}", _util(serial, *options),
             output_patterns=(start,) + tuple(extra_patterns), mutates=True),
        _sample_step(serial, "sample-mode", f"{command} {label}", check),
    ]
    if prior is None:
        # After a prior mode the channel depends on how that mode's stop
        # reset it (#191 for -q), so it is only checked from idle.
        steps.append(CHANNEL.verify(serial, "verify-channel-during-mode",
                                    "Channel kept while the mode runs",
                                    "channel during mode", target))
    steps += [
        PA_LEVEL.verify(serial, "verify-pa-level-during-mode",
                        "PA level kept while the mode runs", "pa-level during mode",
                        lambda context: require(context, PA_LEVEL.original)),
        _stop(serial, "mode-stop", "Stop the mode with -S"),
    ]
    if stop_resets:
        steps += [
            _sample_step(serial, "sample-after-stop", "LEDs stay off after the stop",
                         steady_check(ALL_OFF, "off")),
            CHANNEL.verify(serial, "verify-channel-after-stop",
                           "Channel reset by the mode stop", "channel after stop",
                           BOOT_DEFAULT_CHANNEL),
            SQUELCH.verify(serial, "verify-squelch-after-stop",
                           "Squelch reset by the mode stop", "squelch after stop",
                           SQUELCH.after_mode_stop),
        ]
        stopped = "-S stops it (LEDs off, channel 2441, squelch -120, PA kept)"
    else:
        # SqueakyFloors/ubertooth#191: a -q stop skips the idle reset and
        # leaves the LEDs as the mode left them. Turn them off: a mode still
        # running would light them again.
        steps += [
            Step("clear-leds", "Turn every LED off after the stop",
                 _util(serial, "-d0"), mutates=True),
            _sample_step(serial, "sample-after-stop",
                         "LEDs stay off: the mode no longer drives them",
                         steady_check(ALL_OFF, "off")),
        ]
        stopped = "-S stops it (LEDs no longer driven, PA kept)"
    steps.append(
        PA_LEVEL.verify(serial, "verify-pa-level-after-stop",
                        "PA level kept by the mode stop", "pa-level after stop",
                        lambda context: require(context, PA_LEVEL.original)),
    )
    return StateCase(
        case_id,
        name,
        tuple(steps),
        _cleanup(serial, MODE_RESET_SETTINGS + (PA_LEVEL,)),
        expected_state=(
            f"{command} returns at once and the board {label}; channel and PA "
            f"kept while it runs; {stopped}; original channel, squelch, LEDs, "
            "and PA restored"
        ),
    )


# ---------------------------------------------------------------------------
# -m with no range-test result

RANGE_RESULT_ANY = r"(?i)range test result"
# Expected wording once SqueakyFloors/ubertooth#190 is fixed.
RANGE_RESULT_NONE = r"(?i)\bno range test result\b"
RANGE_RESULT_VALUES = r"request PA level|request number|reply PA level|reply number|mismatch on byte"


def _range_result_case(serial, case_id, name, patterns):
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
                 output_patterns=(r"Resetting ubertooth device",), mutates=True),
            Step("verify-firmware-after-reset", "Firmware answers after reset",
                 _util(serial, "-v"), output_patterns=(FIRMWARE_PATTERN,)),
            Step(
                "range-result",
                "Range test result with no range test run",
                _util(serial, "-m"),
                output_patterns=patterns,
                forbidden_patterns=(RANGE_RESULT_VALUES, r"read failed"),
            ),
        ),
        _cleanup(serial, MODE_RESET_SETTINGS + (PA_LEVEL,), crc=True),
        expected_state=(
            "-m after a reset exits 0 and reports no result values; every "
            "setting the reset changed restored"
        ),
    )


def util_mode_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Ubertooth util modes")
    identify = (("-I",), IDENTIFY_START, BLINK)
    specan_lit = (("-q182",), specan_start(182), LIT)
    # Every -q case except the strict stop case checks only that -S ended the
    # mode (SqueakyFloors/ubertooth#191), so each strict case fails for one
    # fork issue.
    q = {"stop_resets": False}
    cases = [
        _mode_case(serial, "identify-blinks-until-stop", "-I blinks until -S",
                   *identify),
        _mode_case(serial, "led-specan-182-lights-leds",
                   "-q182 (threshold -182 dBm) lights every LED", *specan_lit, **q),
        _range_result_case(serial, "range-result-none-after-reset",
                           "-m with no range-test result", (RANGE_RESULT_ANY,)),
    ]
    if arguments["coverage"] == "comprehensive":
        cases += [
            _mode_case(serial, "led-specan-1-runs-until-stop",
                       "-q1 runs until -S", ("-q1",), specan_start(1), SAMPLED, **q),
            # Strict: fail until SqueakyFloors/ubertooth#191 is fixed.
            _mode_case(serial, "led-specan-stop-runs-idle-reset",
                       "-S after -q182 turns the LEDs off and resets channel and "
                       "squelch", *specan_lit),
            # Strict: fail until SqueakyFloors/ubertooth#189 is fixed.
            _mode_case(serial, "led-specan-default-lights-leds",
                       "Bare -q (threshold -225 dBm) lights every LED",
                       ("-q",), specan_start(225), LIT, **q),
            _mode_case(serial, "led-specan-225-lights-leds",
                       "-q225 lights every LED", ("-q225",), specan_start(225), LIT,
                       **q),
            _mode_case(serial, "identify-replaces-led-specan",
                       "-I ends a running -q182 and blinks", *identify,
                       prior=specan_lit),
            _mode_case(serial, "led-specan-replaces-identify",
                       "-q182 ends a running -I and lights every LED", *specan_lit,
                       prior=identify, **q),
            _mode_case(serial, "identify-after-serial-query",
                       "-s -I reports the serial, then blinks",
                       ("-s", "-I"), IDENTIFY_START, BLINK,
                       extra_patterns=(rf"Serial No: {re.escape(serial)}",)),
            # Strict: fail until SqueakyFloors/ubertooth#190 is fixed.
            _range_result_case(serial, "range-result-none-says-no-result",
                               "-m says there is no range-test result",
                               (RANGE_RESULT_NONE,)),
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
            # Strict: fail until SqueakyFloors/ubertooth#188 is fixed. No
            # option is given, so the buggy open (of whatever USB device is
            # first on the bus) only claims an interface and sends no request.
            _multi_case(
                serial, "index-past-end-refused",
                f"-U{count} (past the last board) is refused",
                (Step(
                    "refuse", f"-U{count} with {count} boards",
                    ("ubertooth-util", f"-U{count}"),
                    expected_returncodes=(1,),
                ),),
                "an index with no board opens nothing and exits 1",
            ),
        ]
    return tuple(cases)
