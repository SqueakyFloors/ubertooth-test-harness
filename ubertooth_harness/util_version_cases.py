"""Case builder for Util version conflicts: ubertooth-util -V beside the
options it would otherwise suppress, with no Ubertooth.

`-V` is terminal. ubertooth-util prints the version answer and returns before
any report, setter or mode runs (ubertooth-util.c: the `if (do_version)` block
returns above the mode checks), so until
[ubertooth#1255](https://github.com/SqueakyFloors/ubertooth/issues/1255) a
command line such as `-V -B` or `-s -V` exits 0 with the other half of it
dropped without a word. The harness had comprehensive argument coverage and
did not catch that, because nothing asked what `-V` does to the option beside
it (harness #431). This test asks, for every option there is.

What a conflict must be, per command line:

  * exit 1;
  * the one error line below, naming the suppressed option and `-V`, and no
    second error, note or warning line, so a reader has one thing to act on;
  * nothing at all on standard output, which is where the version answer
    goes: a refusal that printed half an answer would be worse than either;
  * no mode start line;
  * no board asked for, which the no-devices switch shows. Every command runs
    as ``env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util ...``, as in Refused
    command lines, so a regression that accepted the combination would find
    no board rather than acting on one, and the switch's "no board is
    selected" note is forbidden in every refusal. That is also what stands in
    here for "no device state change": a command line refused before any
    board is opened cannot have changed one. ubertooth-util has no file
    option, so there is no file for a refusal to create.

Both orders, every time. The fork sets its flags while it reads the command
line and decides once the whole line has parsed, so `-V -B` and `-B -V` are
the same command line and must give the same message; a tool that only
noticed the option written after `-V` would pass one order and fail the other.

What stays composable, and is required to:

  * `-U <selector>` and `-U all`, which scope the version report rather than
    being suppressed by it: `-V` reads the board `-U` names, or every attached
    board without one (libubertooth's ubertooth_print_versions()).
  * `-h`, which the fork intentionally keeps beside `-V`: a command line with
    both prints both, the version first so the usage text does not scroll it
    away (ubertooth-util.c). ubertooth#1255 allows that only while it is
    intentional, so the case requires both answers rather than assuming one.

The matrix is derived from the option tables the harness already owns, so an
option added to the harness cannot escape it: the reads from
`transitions.QUERY_OPTIONS`, the setters from the `transitions.Setting`
objects, the modes from `util_refusal_cases.MODES`, and `OTHER_OPTIONS` for
the six ubertooth-util options no table owns. `tests/test_util_version_cases`
holds the union to the ubertooth-util table of COVERAGE.md, less the three
options kept above and the two the fork removed, so a new option shows up as
a failing unit test rather than as a quiet gap.

Ordering. The conflict is decided after the command line has parsed and
before the version answer, so a bad option value, an option given a value
twice, and a stray word are all reported instead of it (they are found in the
option loop and the operand check, which run first). Comprehensive coverage
requires each of those three, and that no conflict line comes out beside
them.

The expected wording is one function, `conflict_error`, because the harness
and fork #1255 have to agree on it exactly; if the fork lands other words,
this is the one place that changes. The cases skip until the built tool has
the check at all (`utilconflict.py` probe), so this runs green against a fork
that predates ubertooth#1255 rather than failing every case on it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ubertooth_harness import transitions, util_refusal_cases, utilconflict
from ubertooth_harness.hostneeds import tool_needs
from ubertooth_harness.patterns import (
    HOST_VERSION_LINE,
    VERSION_ANSWER_PATTERNS,
)
from ubertooth_harness.refusal_cases import (
    NO_BOARD_NOTE, ONE_ERROR_ONLY, tool_step,
)
from ubertooth_harness.stateful import (
    Observation,
    StateCase,
    StateCaseError,
    Step,
    nothing_on_stdout,
)

TOOL = "ubertooth-util"
VERSION = "-V"
HELP = "-h"
SELECTOR = "-U"
# The selector the kept cases name. No board is opened in any case here, so it
# need not exist, and it is not a registry serial, so a reader cannot mistake
# it for a bench board; it starts with a letter, since libubertooth reads an
# all-digit selector as a USB index.
SELECTOR_VALUE = "ba11"
ALL = "all"
STRAY = "stray"

_HELPER = str(Path(utilconflict.__file__).resolve())

# ---------------------------------------------------------------------------
# The expected refusal

# Why the two cannot be combined, as the message says it. One string, because
# the harness and the fork have to agree on it to the character: ubertooth#1255
# is where it lands, and this is the only place the harness states it.
CONFLICT_REASON = ("-V reports the version and ends the command, so run them "
                   "as separate commands")


def conflict_error(option: str) -> str:
    """The refusal of ``option`` beside -V, as a whole line of its own."""
    return (r"(?m)^error: " + re.escape(option)
            + r" cannot be combined with -V; " + re.escape(CONFLICT_REASON)
            + r"$")


def any_conflict_error(options) -> str:
    """The refusal of any one of ``options``, for a command line carrying
    several: which one a message names is the fork's to choose, and nothing
    here pins it."""
    return (r"(?m)^error: (?:"
            + "|".join(re.escape(option) for option in options)
            + r") cannot be combined with -V; " + re.escape(CONFLICT_REASON)
            + r"$")


# The version answer, which a refusal must not print any of. Both labels, on
# either stream: standard output is required empty as well, and these say
# which output a failure found rather than only that there was some.
VERSION_OUTPUT = (r"(?m)^Host tools version:", r"(?m)^Firmware version")
# The three things a refused command line must be, from where each is already
# stated: no mode start line (transitions.ANY_MODE_START), one error line and
# no note or warning beside it (refusal_cases.ONE_ERROR_ONLY), and no board
# asked for (the no-devices switch's note).
MODE_START = transitions.ANY_MODE_START
FORBIDDEN = (NO_BOARD_NOTE, MODE_START) + VERSION_OUTPUT + ONE_ERROR_ONLY

# The first line of ubertooth-util's usage text, which -h prints and -V alone
# must not.
USAGE_LINE = r"(?m)^ubertooth-util - command line utility"
# Both answers in the fork's order: the usage text comes last so it does not
# scroll the version away (ubertooth-util.c).
VERSION_BEFORE_USAGE = r"(?s)Host tools version:.*ubertooth-util - command line"
# What -V answers with no board on the bus: the host line, then one firmware
# line saying nothing is attached, and no build line (fork #1233). Required
# of every command line that keeps -V, so a kept combination answers in full
# rather than merely exiting 0.
NOTHING_ATTACHED = r"(?m)^Firmware version: no Ubertooth attached$"
VERSION_ANSWER = VERSION_ANSWER_PATTERNS + (HOST_VERSION_LINE, NOTHING_ATTACHED)
# A kept combination is not a refusal: no error line of any kind.
NO_ERROR = (r"(?m)^error:", r"cannot be combined")

# ---------------------------------------------------------------------------
# The matrix, from the tables the harness already owns

# The settings whose set form is a ubertooth-util option, by the option that
# writes them. Taken from the Setting objects rather than written out, so a
# setting added to the harness brings its option with it; the value is any one
# the setting itself takes, since what is under test is the option beside -V
# and not the value.
SETTINGS = (transitions.CHANNEL, transitions.CHANNEL_INDEX,
            transitions.PA_LEVEL, transitions.SQUELCH, transitions.USR_LED,
            transitions.LEDS)
SETTER_VALUES = {"-c": "2426", "-C": "24", "-a": "5", "-z": "-60",
                 "-l": "1", "-d": "1"}
# The ubertooth-util options no harness table owns: the two that act on the
# board without reading or setting a value, the two records, the range-test
# result, and the bus count. Each is suppressed by -V exactly as a report is.
OTHER_OPTIONS = (
    ("-S", "stop"),
    ("-r", "reset"),
    ("-m", "range-result"),
    ("-N", "count"),
    ("-B", "bootloader"),
    ("-w", "watchdog"),
)
# The options -V keeps, which are therefore in no conflict case.
KEPT_OPTIONS = (VERSION, SELECTOR, HELP)
# Options the fork removed, so neither kept nor conflicting: -f (fork
# db5649b, use ubertooth-dfu) and the undocumented -9 (fork 5ac22a8,
# ubertooth#350). Declared so the unit test that compares this matrix with
# COVERAGE.md's ubertooth-util table knows why they are absent.
REMOVED_OPTIONS = ("-f", "-9")

# One label per option, for the case IDs: a case ID is lower case, so -i and
# -I cannot be told apart by their letter, and -c and -C are both "channel" in
# the tables they come from. The unit tests require one label per conflicting
# option, so a new option has to be named here too.
LABELS = {
    "-v": "firmware", "-s": "serial", "-p": "part", "-b": "board",
    "-a": "pa-level", "-C": "channel-index", "-c": "channel-mhz",
    "-z": "squelch", "-l": "usr-led", "-d": "all-leds",
    "-q": "led-specan", "-n": "range-test", "-e": "repeater",
    "-t": "tx-test", "-x": "xmas", "-I": "identify", "-i": "isp",
    "-S": "stop", "-r": "reset", "-m": "range-result", "-N": "count",
    "-B": "bootloader", "-w": "watchdog",
}
# The one mode that takes a value, so the matrix covers a mode written both
# ways; -q<N> is the LED specan threshold (util_cases).
MODE_VALUES = {"-q": "120"}
# The smoke subset: one of each kind, with the two ubertooth#1255 names
# first. Comprehensive runs every option.
SMOKE_OPTIONS = ("-B", "-s", "-a", "-I", "-N")


def _conflicting() -> tuple[str, ...]:
    """Every ubertooth-util option -V suppresses, from the owned tables.

    Reports, then modes, then setters, then the rest, each in its table's own
    order, with an option that is in two tables kept at its first appearance.
    """
    options: list[str] = []
    for option, _label, _pattern in transitions.QUERY_OPTIONS:
        options.append(option)
    options += [option for option in util_refusal_cases.MODES
                if option not in options]
    options += [setting.set_prefix for setting in SETTINGS
                if setting.set_prefix not in options]
    options += [option for option, _label in OTHER_OPTIONS
                if option not in options]
    kept = [option for option in options if option in KEPT_OPTIONS]
    if kept:
        raise StateCaseError(
            "the matrix must not hold an option -V keeps: " + " ".join(kept))
    missing = sorted(set(options) - set(LABELS))
    if missing:
        raise StateCaseError(
            "every conflicting option needs a label: " + " ".join(missing))
    return tuple(options)


CONFLICTING = _conflicting()


def option_forms(option: str) -> tuple[str, ...]:
    """The words this option is written as: bare, and with a value where it
    takes one."""
    value = SETTER_VALUES.get(option) or MODE_VALUES.get(option)
    return (option,) if value is None else (option, f"{option}{value}")


# ---------------------------------------------------------------------------
# Steps and cases


def probe_step() -> Step:
    return Step(
        "probe",
        "Check that this build refuses the options -V would suppress (reads "
        f"the libubertooth beside {TOOL}, then runs -V "
        f"{utilconflict.PROBE_OPTION} with no board)",
        (sys.executable, _HELPER, "probe", TOOL),
        timeout_s=utilconflict.PROBE_TIMEOUT_S + 10.0,
        observe=Observation(
            "version conflicts", r"Version conflicts: (?P<value>[^\n]+)",
            reject=lambda value: "" if value.startswith("yes") else value,
        ),
    )


def _step_id(prefix: str, word: str) -> str:
    """refuse-b-after-version, refuse-c2426-before-version: the option as it
    was written, with anything that is not a case-ID character taken out, and
    which side of -V it was on."""
    written = re.sub(r"[^a-z0-9]+", "", word.lower()) or "x"
    return f"{prefix}-{written}"


def _refuse(step_id, words, patterns, *, forbidden=FORBIDDEN) -> Step:
    """One refused command line: exit 1, the message, nothing on stdout."""
    return tool_step(
        step_id, TOOL, tuple(words), returncodes=(1,),
        patterns=tuple(patterns), forbidden=tuple(forbidden),
        check=nothing_on_stdout)


def _accept(step_id, words, patterns, *, forbidden=NO_ERROR) -> Step:
    """One command line that keeps -V: exit 0 with the whole answer."""
    return tool_step(step_id, TOOL, tuple(words), returncodes=(0,),
                     patterns=tuple(patterns), forbidden=tuple(forbidden))


def _host_version() -> Step:
    """The cleanup: nothing on a device to restore, so this records that the
    tool still runs and still answers -V in full."""
    return _accept("host-version", (VERSION,), VERSION_ANSWER)


def _case(case_id, name, steps, expected_state) -> StateCase:
    return StateCase(case_id, name, (probe_step(), *steps), (_host_version(),),
                     expected_state=expected_state, needs=tool_needs(TOOL))


def _conflict_case(option: str) -> StateCase:
    """One option, every form of it, on both sides of -V."""
    steps = []
    for word in option_forms(option):
        pattern = (conflict_error(option),)
        written = _step_id("refuse", word)
        steps.append(_refuse(f"{written}-after-version", (VERSION, word), pattern))
        steps.append(_refuse(f"{written}-before-version", (word, VERSION), pattern))
    written = " ".join(f"-V {word}" for word in option_forms(option))
    return _case(
        f"conflict-{LABELS[option]}",
        f"util {option} with -V refused in both orders",
        tuple(steps),
        f"each of {written} and its reverse refused (exit 1) naming {option} "
        "and -V, with no version answer, no mode start, and no board asked "
        "for",
    )


def _kept_cases():
    """What -V keeps: its selector, and the help the fork prints beside it."""
    selector = (SELECTOR, SELECTOR_VALUE)
    return [
        _case("version-alone", "util -V alone answers in full", (
            _accept("version", (VERSION,), VERSION_ANSWER,
                    forbidden=NO_ERROR + (USAGE_LINE,)),
        ), "exit 0 with the host line and one firmware line saying nothing is "
           "attached, and no usage text"),
        _case("version-with-selector",
              "util -U <selector> -V keeps the selector", (
                  _accept("selector-first", selector + (VERSION,),
                          VERSION_ANSWER),
                  _accept("version-first", (VERSION,) + selector,
                          VERSION_ANSWER),
              ), "exit 0 with the whole version answer in both orders: -U "
                 "scopes the report rather than being suppressed by it"),
        _case("version-with-all-selector",
              "util -U all -V keeps the listing selector", (
                  _accept("selector-first", (SELECTOR, ALL, VERSION),
                          VERSION_ANSWER),
                  _accept("version-first", (VERSION, SELECTOR, ALL),
                          VERSION_ANSWER),
              ), "exit 0 with the whole version answer in both orders, and no "
                 "-U all refusal: -V reports every attached board anyway"),
        _case("version-with-help",
              "util -V -h prints both answers, the version first", (
                  _accept("version-first", (VERSION, HELP),
                          VERSION_ANSWER + (USAGE_LINE, VERSION_BEFORE_USAGE)),
                  _accept("help-first", (HELP, VERSION),
                          VERSION_ANSWER + (USAGE_LINE, VERSION_BEFORE_USAGE)),
              ), "exit 0 with the version answer and the usage text, the "
                 "version first, in both orders"),
    ]


def _order_cases():
    """The three refusals decided before the conflict is, and a command line
    carrying two conflicting options."""
    # A bad option value is found in the option loop, before the command line
    # has finished parsing.
    bad_value = _case(
        "bad-value-first", "util -V -a8: the value is refused, not the conflict",
        (
            _refuse("refuse-a8", (VERSION, "-a8"),
                    (r"(?m)^error: PA level must be a single digit 0-7 \(got 8\)$",),
                    forbidden=FORBIDDEN + (r"cannot be combined",)),
        ),
        "refused (exit 1) for the PA level, with no conflict line and no "
        "version answer")
    # opt_check_once() runs in the loop too, before the value is looked at.
    repeated = _case(
        "repeat-first", "util -V -c2426 -c2450: the repeat is refused first", (
            _refuse("refuse-repeated-channel", (VERSION, "-c2426", "-c2450"),
                    (r"(?m)^error: option '-c' given more than once$",),
                    forbidden=FORBIDDEN + (r"cannot be combined",)),
        ),
        "refused (exit 1) for the repeated -c, with no conflict line and no "
        "version answer")
    # The operand check runs after the loop and before -V, so a typo such as
    # "- V" is reported rather than dropped.
    stray = _case(
        "stray-word-first", f"util -V -B {STRAY}: the stray word is refused first",
        (
            _refuse("refuse-stray", (VERSION, "-B", STRAY),
                    (rf"(?m)^error: unexpected argument '{STRAY}'$",),
                    forbidden=FORBIDDEN + (r"cannot be combined",)),
        ),
        "refused (exit 1) naming the stray word, with no conflict line and no "
        "version answer")
    # Two suppressed options at once. Which of them the message names is the
    # fork's to choose, so the case requires a conflict line naming one of
    # them and the one-error rule around it, rather than pinning the choice.
    both = _case(
        "two-conflicting-options", "util -V -s -B: one refusal for two options",
        (
            _refuse("refuse-version-first", (VERSION, "-s", "-B"),
                    (any_conflict_error(("-s", "-B")),)),
            _refuse("refuse-version-last", ("-s", "-B", VERSION),
                    (any_conflict_error(("-s", "-B")),)),
        ),
        "refused (exit 1) with one conflict line naming -s or -B, no second "
        "error line, and no version answer")
    return [bad_value, repeated, stray, both]


def util_version_cases(arguments, _resources, _resource_identities):
    """Host-only cases: ubertooth-util runs with UBERTOOTH_TEST_NO_DEVICES=1."""
    comprehensive = arguments["coverage"] == "comprehensive"
    options = CONFLICTING if comprehensive else SMOKE_OPTIONS
    cases = [_conflict_case(option) for option in options] + _kept_cases()
    if comprehensive:
        cases += _order_cases()
    return tuple(cases)
