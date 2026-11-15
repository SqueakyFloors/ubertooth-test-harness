"""Case builder for Util version conflicts: ubertooth-util -V beside the
options it would otherwise suppress, with no Ubertooth.

`-V` is terminal. ubertooth-util prints the version answer and returns before
any report, setter or mode runs (ubertooth-util.c: the `if (do_version)` block
returns above the mode checks), so before
[ubertooth#1255](https://github.com/SqueakyFloors/ubertooth/issues/1255) a
command line such as `-V -B` or `-s -V` exited 0 with the other half of it
dropped without a word. The harness had comprehensive argument coverage and
did not catch that, because nothing asked what `-V` does to the option beside
it (harness #431). This test asks, for every option there is.

What a conflict must be, per command line:

  * exit 1;
  * one error line, `error: -V cannot be combined with <option>; -V prints the
    version and ends the command, so run them as separate commands`, and no
    second error, note or warning line, so a reader has one thing to act on;
  * nothing at all on standard output, which is where the version answer and
    the `-N` count would go: a refusal that printed half an answer would be
    worse than either;
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
  * `-h`, whose usage text the fork prints after the version answer. It is one
    of the two options `-V` uses, so it does not excuse a third: `-V -h -b` is
    refused for `-b`, with no usage text either, which the fork's own
    command-line matrix pins.

The matrix is derived from the option tables the harness already owns, so an
option added to the harness cannot escape it: the reads from
`transitions.QUERY_OPTIONS`, the setters from every `transitions.Setting`
object the module defines, the modes from `util_refusal_cases.MODES`, and
`OTHER_OPTIONS` for the six ubertooth-util options no table owns.
`tests/test_util_version_cases` holds the union to two statements it was not
derived from: the ubertooth-util table of COVERAGE.md and the tool's own
getopt string, less the three options kept above and the two the fork removed.
So a new option shows up as a failing unit test rather than as a quiet gap.

Ordering. The conflict is decided once the whole command line has parsed and
before the version answer, so everything the option parser finds first is
reported instead: a bad option value, an option given a value twice, the
`-c`/`-C` clash, and a stray word. Comprehensive coverage requires each of
those four, and that no conflict line comes out beside them. Where a command
line carries several suppressed options, the first one written is the one
named, whichever side of `-V` it is on.

The expected wording is one function, `conflict_error`, because the harness
and the fork have to agree on it exactly: it is the line fork #1260 prints,
and if that changes, this is the one place the harness changes with it. The
cases skip until the built tool has the check at all (`utilconflict.py`
probe), so this runs green against a fork commit from before it rather than
failing every case on one.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ubertooth_harness import transitions, util_refusal_cases, utilconflict
from ubertooth_harness.hostneeds import tool_needs
from ubertooth_harness.patterns import VERSION_ANSWER_PATTERNS
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

# The refusal, as ubertooth-util prints it (fork #1260 for ubertooth#1255,
# ubertooth-util.c, and ubertooth-util.md OPTIONS under -V). Two statements:
# the reason, which every refusal carries, and the option named, which is the
# FIRST option written that -V does not use -- that is the one a reader would
# look for in the output, and the fork's own command-line matrix pins it.
CONFLICT_REASON = ("-V prints the version and ends the command, so run them "
                   "as separate commands")


def conflict_error(option: str) -> str:
    """The refusal naming ``option``, as a whole line of its own.

    `-V` comes first in the message and the suppressed option second, which
    is how the fork writes it; a message with them the other way round does
    not match.
    """
    return (r"(?m)^error: -V cannot be combined with " + re.escape(option)
            + r"; " + re.escape(CONFLICT_REASON) + r"$")


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
VERSION_ANSWER = VERSION_ANSWER_PATTERNS + (NOTHING_ATTACHED,)
# A kept combination is not a refusal: no error line of any kind.
NO_ERROR = (r"(?m)^error:", r"cannot be combined")

# ---------------------------------------------------------------------------
# The matrix, from the tables the harness already owns

# The settings whose set form is a ubertooth-util option. Collected from the
# `Setting` objects of `transitions` rather than listed, because the lists
# there (SMOKE_SETTINGS, RESTORED_SETTINGS and the rest) are each a subset:
# every Setting the module defines is one the harness writes with
# ubertooth-util, so this is the whole of them, and a setting added there
# brings its option with it. The value is any one the setting itself takes,
# since what is under test is the option beside -V and not the value.
SETTINGS = tuple(
    value for name, value in sorted(vars(transitions).items())
    if isinstance(value, transitions.Setting)
)
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
# The two modes that take a value, so the matrix covers a mode written both
# ways: -q<N> is the LED specan threshold and -n<seconds> the range test's own
# deadline (util_cases, catalog.py).
MODE_VALUES = {"-q": "120", "-n": "5"}
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
    tool still runs and still answers -V in full.

    -V reads the no-devices switch itself and reports what is attached, so it
    never prints the switch's own note: that note belongs to a command that
    wanted a board (libubertooth's ubertooth_print_versions()).
    """
    return _accept("host-version", (VERSION,), VERSION_ANSWER,
                   forbidden=NO_ERROR + (NO_BOARD_NOTE,))


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
    """What is decided before the conflict is, and the command lines carrying
    more than one suppressed option, where the first written is named."""
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
                    forbidden=FORBIDDEN + (r"cannot be combined with -V",)),
        ),
        "refused (exit 1) for the repeated -c, with no conflict line and no "
        "version answer")
    # -c and -C are one setting written two ways, which the option loop
    # refuses as it reads the second value (ubertooth-util.md, ARGUMENTS).
    channel_clash = _case(
        "channel-clash-first",
        "util -V -c2426 -C24: the -c/-C clash is refused first", (
            _refuse("refuse-channel-clash", (VERSION, "-c2426", "-C24"),
                    (r"(?m)^error: -C cannot be combined with -c; they are the "
                     r"same channel setting written two ways$",),
                    forbidden=FORBIDDEN + (r"cannot be combined with -V",)),
        ),
        "refused (exit 1) for the two spellings of one channel, with no "
        "conflict line and no version answer")
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
    # Several suppressed options at once: the first one written is named,
    # whichever order -V came in. These three command lines are the ones fork
    # #1260 pins in its own command-line matrix.
    several = _case(
        "first-suppressed-option-named",
        "util with two suppressed options names the first one written", (
            _refuse("refuse-version-first", (VERSION, "-s", "-B"),
                    (conflict_error("-s"),)),
            _refuse("refuse-version-last", ("-s", "-B", VERSION),
                    (conflict_error("-s"),)),
            # -N is suppressed like any other option, and no count is printed
            # before the refusal, which nothing_on_stdout requires.
            _refuse("refuse-count-first", (VERSION, "-N", "-B"),
                    (conflict_error("-N"),)),
        ),
        "each refused (exit 1) naming the first suppressed option written, "
        "with no count and no version answer on stdout")
    # -h is one of the two -V uses, so what is left to refuse is the third
    # option, and no usage text comes out either.
    help_and_one = _case(
        "help-does-not-excuse-another-option",
        "util -V -h -b: -h stands beside -V, -b does not", tuple(
            _refuse(step_id, words, (conflict_error("-b"),),
                    forbidden=FORBIDDEN + (USAGE_LINE,))
            for step_id, words in (
                ("refuse-version-help-board", (VERSION, HELP, "-b")),
                ("refuse-help-board-version", (HELP, "-b", VERSION)),
            )),
        "refused (exit 1) naming -b, with neither the version answer nor the "
        "usage text")
    # A value given as the next word is still that option's value, so the
    # conflict is the option and not a stray word (fork #156's next-word form).
    next_word = _case(
        "next-word-value", "util -V -c 2426: the next word is the value", (
            _refuse("refuse-next-word-channel", (VERSION, "-c", "2426"),
                    (conflict_error("-c"),),
                    forbidden=FORBIDDEN + (r"unexpected argument",)),
        ),
        "refused (exit 1) naming -c, not the value as a stray word, and with "
        "no version answer")
    return [bad_value, repeated, channel_clash, stray, several, help_and_one,
            next_word]


def util_version_cases(arguments, _resources, _resource_identities):
    """Host-only cases: ubertooth-util runs with UBERTOOTH_TEST_NO_DEVICES=1."""
    comprehensive = arguments["coverage"] == "comprehensive"
    options = CONFLICTING if comprehensive else SMOKE_OPTIONS
    cases = [_conflict_case(option) for option in options] + _kept_cases()
    if comprehensive:
        cases += _order_cases()
    return tuple(cases)
