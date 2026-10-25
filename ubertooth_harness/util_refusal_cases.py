"""Case builder for Util mode refusals: ubertooth-util -f and -i, no Ubertooth.

``ubertooth-util -f`` (DFU) and ``-i`` (ISP) leave the application firmware,
so the harness never runs them against a board. Since fork 4ed4a1d
(util_check_modes(), host/ubertooth-tools/src/util_modes.c) a command line
with two or more of the mode options -q -n -e -t -x -I -i -f is refused before
any device is opened, exit 1, naming the given modes in that fixed order:

    error: options -i -f cannot be combined; give only one of -q -n -e -t -x -I -i -f

and -z (read or set) with a mode is refused next, naming the first mode in the
same order: ``error: -z cannot be combined with -f`` (ubertooth-util.md,
DESCRIPTION and -z). Every command runs as ``env UBERTOOTH_TEST_NO_DEVICES=1
ubertooth-util ...``, as in Refused command lines, so a regression that
accepted a combination would find no board instead of stranding one; the
switch's "no board is selected" note is forbidden in every refusal, and so is
the -f deprecation note, which the tool prints only once those checks pass.
The first step reads the built libubertooth (refusal.py probe, running no
tool) and the case skips when it lacks the switch (forks before b6d8ede).

Comprehensive coverage adds three modes, a repeated mode, -z with two modes
(the mode refusal comes first), and a stray word. Two of its cases are strict
and fail at 89d9197:

- SqueakyFloors/ubertooth#332: -N prints the count and exits 0, silently
  dropping -f and -i.
- SqueakyFloors/ubertooth#333: -f -U all prints the -f deprecation note above
  the -U all refusal.
"""

from __future__ import annotations

import re
from itertools import combinations

from ubertooth_harness.refusal_cases import NO_BOARD_NOTE, probe_step, tool_step
from ubertooth_harness.stateful import StateCase, Step

TOOL = "ubertooth-util"
# The fork's mode order (util_modes.h), which every message follows.
MODES = ("-q", "-n", "-e", "-t", "-x", "-I", "-i", "-f")
LABELS = {
    "-q": "led-specan", "-n": "range-test", "-e": "repeater", "-t": "tx-test",
    "-x": "xmas", "-I": "identify", "-i": "isp", "-f": "dfu",
}
FIRMWARE_LEAVING = ("-i", "-f")
# Printed after the mode and -z checks, only for a command line they accept.
DFU_NOTE = r"ubertooth-util -f is no longer required"
FORBIDDEN = (NO_BOARD_NOTE, DFU_NOTE)
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
    """refuse-<words>, e.g. refuse-f-isp for ('-f', '-i'), refuse-z60-f for
    ('-z-60', '-f'). -i and -I are told apart."""
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
    """-f and -i with every other mode, and with each other, in both orders."""
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
    """-z (read) and -z<level> (set) with -f and with -i, in both orders."""
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


def _comprehensive_cases():
    both = ("-f", "-i")
    cases = [
        _case("three-modes", "util -f -i -x refused naming all three", (
            _refuse(("-f", "-i", "-x"), (modes_pattern(("-f", "-i", "-x")),)),
            _refuse(("-x", "-i", "-f"), (modes_pattern(("-f", "-i", "-x")),)),
        ), "refused (exit 1) naming -x -i -f"),
        _case("repeated-mode", "util -f -f -i refused naming each mode once", (
            _refuse(("-f", "-f", "-i"), (modes_pattern(both),)),
            _refuse(("-i", "-i", "-f"), (modes_pattern(both),)),
        ), "refused (exit 1) naming -i -f"),
        # The mode check runs before the -z check.
        _case("squelch-with-two-modes", "util -z -f -i: the mode refusal comes first", (
            _refuse(("-z", "-f", "-i"), (modes_pattern(both),),
                    forbidden=FORBIDDEN + (r"-z cannot be combined",)),
        ), "refused (exit 1) naming -i -f; no -z message"),
        # A stray word is refused while the command line is read, first.
        _case("modes-and-stray", "util -f -i with a stray word: the stray word is refused", (
            _refuse(("-f", "-i", STRAY), (rf"(?m)^error: unexpected argument '{STRAY}'$",),
                    forbidden=FORBIDDEN + (r"cannot be combined",)),
        ), "refused (exit 1) naming the stray word"),
        # Strict: fails at 89d9197 (SqueakyFloors/ubertooth#332). -N prints
        # the count, exit 0, and drops the mode.
        _case("count-with-mode", "util -N with -f or -i is refused, not dropped", tuple(
            _refuse(words, (r"(?m)^error: ",)) for words in
            (("-f", "-N"), ("-N", "-f"), ("-i", "-N"), ("-N", "-i"))
        ), "refused (exit 1) before any board is asked for"),
        # Strict: fails at 89d9197 (SqueakyFloors/ubertooth#333) on the -f
        # step: the deprecation note comes before the -U all refusal.
        _case("all-with-mode", "util -U all with -f or -i: only the -U all refusal", tuple(
            _refuse(words, (r"(?m)^error: -U all only supports the read-only queries ",))
            for words in (("-U", "all", "-i"), ("-U", "all", "-f"))
        ), "refused (exit 1) with the -U all message alone"),
    ]
    return cases


def util_refusal_cases(arguments, _resources, _resource_identities):
    """Host-only cases: ubertooth-util runs with UBERTOOTH_TEST_NO_DEVICES=1."""
    cases = _pair_cases() + _squelch_cases()
    if arguments["coverage"] == "comprehensive":
        cases += _comprehensive_cases()
    return tuple(cases)
