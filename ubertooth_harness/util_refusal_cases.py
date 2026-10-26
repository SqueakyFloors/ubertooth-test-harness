"""Case builder for Util mode refusals: ubertooth-util -i, and -f, no Ubertooth.

``ubertooth-util -i`` (ISP) leaves the application firmware, so the harness
never runs it against a board. Since fork bbc1b49 (util_check_modes(),
host/ubertooth-tools/src/util_modes.c) a command line with two or more of the
mode options -q -n -e -t -x -I -i is refused before any device is opened,
exit 1, naming the given modes in that fixed order:

    error: options -x -i cannot be combined; give only one of -q -n -e -t -x -I -i

and since 4ed4a1d -z (read or set) with a mode is refused next, naming the
first mode in the same order: ``error: -z cannot be combined with -i``
(ubertooth-util.md, DESCRIPTION and -z). Fork db5649b removed -f (DFU mode):
it is refused as an unknown option, ``error: unrecognized option '-f'``, exit 1,
before any mode check. Every command runs as ``env UBERTOOTH_TEST_NO_DEVICES=1
ubertooth-util ...``, as in Refused command lines, so a regression that
accepted a combination would find no board instead of stranding one; the
switch's "no board is selected" note is forbidden in every refusal. The first
step reads the built libubertooth (refusal.py probe, running no tool) and the
case skips when it lacks the switch (forks before b6d8ede).

Comprehensive coverage adds three modes, a repeated mode, -z with two modes
(the mode refusal comes first), a stray word, -N with -i, and -U all with -i.
Since db5649b -N prints the count and the command carries on (fork #332 was
fixed by that design change), so -N -i prints 0, then runs -i, which finds no
board: exit 1 with "could not open Ubertooth device". -U all -i gives the
-U all refusal alone (fork #333 went with -f).
"""

from __future__ import annotations

import re
from itertools import combinations

from ubertooth_harness.refusal_cases import NO_BOARD_NOTE, probe_step, tool_step
from ubertooth_harness.stateful import StateCase, Step

TOOL = "ubertooth-util"
# The fork's mode order (util_modes.h), which every message follows.
MODES = ("-q", "-n", "-e", "-t", "-x", "-I", "-i")
LABELS = {
    "-q": "led-specan", "-n": "range-test", "-e": "repeater", "-t": "tx-test",
    "-x": "xmas", "-I": "identify", "-i": "isp",
}
FIRMWARE_LEAVING = ("-i",)
FORBIDDEN = (NO_BOARD_NOTE,)
STRAY = "stray"


def _ordered(modes) -> tuple[str, ...]:
    return tuple(mode for mode in MODES if mode in modes)


def modes_pattern(modes) -> str:
    """The refusal of ``modes``, named in the fork's order, as a whole line."""
    return (r"(?m)^error: options " + re.escape(" ".join(_ordered(modes)))
            + r" cannot be combined; give only one of "
            + re.escape(" ".join(MODES)) + r"$")


def squelch_pattern(mode: str) -> str:
    return rf"(?m)^error: -z cannot be combined with {re.escape(mode)}$"


def _step_id(words) -> str:
    """refuse-<words>, e.g. refuse-x-isp for ('-x', '-i'), refuse-z60-isp for
    ('-z-60', '-i'). -i and -I are told apart."""
    parts = []
    for word in words:
        if word == "-i":
            parts.append("isp")
        elif word == "-I":
            parts.append("identify")
        else:
            parts.append(re.sub(r"[^a-z0-9]+", "", word.lower()) or "x")
    return "refuse-" + "-".join(parts)


def _refuse(words, patterns, *, forbidden=FORBIDDEN, returncodes=(1,)) -> Step:
    return tool_step(_step_id(words), TOOL, tuple(words), returncodes=returncodes,
                     patterns=tuple(patterns), forbidden=tuple(forbidden))


def _host_version() -> Step:
    # Nothing on a device to restore. -H never opens one; it records that the
    # tool still runs.
    return tool_step("host-version", TOOL, ("-H",), returncodes=(0,),
                     patterns=(r"libubertooth ",), forbidden=(NO_BOARD_NOTE,))


def _case(case_id, name, steps, expected_state) -> StateCase:
    return StateCase(case_id, name, (probe_step(TOOL), *steps), (_host_version(),),
                     expected_state=expected_state)


def _pair_cases():
    """-i with every other mode, in both orders."""
    cases = []
    for first, second in combinations(MODES, 2):
        if not {first, second} & set(FIRMWARE_LEAVING):
            continue
        pattern = modes_pattern((first, second))
        named = " ".join(_ordered((first, second)))
        cases.append(_case(
            f"{LABELS[first]}-and-{LABELS[second]}",
            f"util {first} {second} refused in both orders",
            (_refuse((first, second), (pattern,)), _refuse((second, first), (pattern,))),
            f"refused (exit 1) naming {named}, before any board is asked for",
        ))
    return cases


def _squelch_cases():
    """-z (read) and -z<level> (set) with -i, in both orders."""
    cases = []
    for mode in FIRMWARE_LEAVING:
        pattern = (squelch_pattern(mode),)
        steps = tuple(_refuse(words, pattern) for words in (
            ("-z", mode), (mode, "-z"), ("-z-60", mode), (mode, "-z-60")))
        cases.append(_case(
            f"squelch-with-{LABELS[mode]}", f"util -z with {mode} refused",
            steps, f"refused (exit 1): -z cannot be combined with {mode}, before "
            "any board is asked for",
        ))
    return cases


def _unknown_dfu_case():
    """-f was removed in fork db5649b: an unknown option, refused first."""
    pattern = (r"(?m)^error: unrecognized option '-f'$",)
    forbidden = FORBIDDEN + (r"cannot be combined",)
    return _case("dfu-unknown", "util -f is refused as an unknown option", tuple(
        _refuse(words, pattern, forbidden=forbidden) for words in (("-f",), ("-f", "-i"))
    ), "refused (exit 1): unrecognized option '-f', before any board is asked for")


def _comprehensive_cases():
    three = ("-q", "-x", "-i")
    cases = [
        _case("three-modes", "util -x -i -q refused naming all three", (
            _refuse(("-x", "-i", "-q"), (modes_pattern(three),)),
            _refuse(("-i", "-q", "-x"), (modes_pattern(three),)),
        ), "refused (exit 1) naming -q -x -i"),
        _case("repeated-mode", "util -i -i -x refused naming each mode once", (
            _refuse(("-i", "-i", "-x"), (modes_pattern(("-x", "-i")),)),
        ), "refused (exit 1) naming -x -i"),
        # The mode check runs before the -z check.
        _case("squelch-with-two-modes", "util -z -x -i: the mode refusal comes first", (
            _refuse(("-z", "-x", "-i"), (modes_pattern(("-x", "-i")),),
                    forbidden=FORBIDDEN + (r"-z cannot be combined",)),
        ), "refused (exit 1) naming -x -i; no -z message"),
        # A stray word is refused while the command line is read, first.
        _case("modes-and-stray", "util -x -i with a stray word: the stray word is refused", (
            _refuse(("-x", "-i", STRAY), (rf"(?m)^error: unexpected argument '{STRAY}'$",),
                    forbidden=FORBIDDEN + (r"cannot be combined",)),
        ), "refused (exit 1) naming the stray word"),
        # Since fork db5649b -N prints the count and the command carries on:
        # -i then runs and finds no board. The switch's note is expected here.
        _case("count-with-mode", "util -N with -i prints the count, then runs -i", tuple(
            _refuse(words, (r"(?m)^0$", r"could not open Ubertooth device"), forbidden=())
            for words in (("-N", "-i"), ("-i", "-N"))
        ), "exit 1: the count 0, then -i finds no board"),
        _case("all-with-mode", "util -U all with -i: only the -U all refusal", (
            _refuse(("-U", "all", "-i"),
                    (r"(?m)^error: -U all only supports the read-only queries ",)),
        ), "refused (exit 1) with the -U all message alone"),
    ]
    return cases


def util_refusal_cases(arguments, _resources, _resource_identities):
    """Host-only cases: ubertooth-util runs with UBERTOOTH_TEST_NO_DEVICES=1."""
    cases = _pair_cases() + _squelch_cases() + [_unknown_dfu_case()]
    if arguments["coverage"] == "comprehensive":
        cases += _comprehensive_cases()
    return tuple(cases)
