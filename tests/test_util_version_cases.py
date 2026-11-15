"""Util version conflicts: the matrix, the cases, and the probe.

No hardware and nothing built. `FakeUtil` below is `ubertooth-util`'s argument
handling as [ubertooth#1255](https://github.com/SqueakyFloors/ubertooth/issues/1255)
asks for it, with a switch for each way a build could get it wrong, including
the behavior before it. Its own option set comes from the fork's getopt string
rather than from the module under test, so the matrix is compared with two
things it was not derived from: that string, and the `ubertooth-util` table of
COVERAGE.md.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness import (
    refusal_cases, util_version_cases, utilconflict,
)
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import StateCase
from tests.test_manifest_preflight import LIBBTBB_RELEASE, RELEASE
from tests.test_sequencer import CollectSink, run_contract
from tests.test_tool_cases import failed_names

NAME = "Util version conflicts"
REPOSITORY = Path(__file__).resolve().parents[1]
NOTE = "note: UBERTOOTH_TEST_NO_DEVICES is set, so no board is selected\n"
USAGE = ("ubertooth-util - command line utility for Ubertooth Zero and "
         "Ubertooth One\n\nCommon options:\n\t-h this help\n")
# ubertooth-util's own getopt string (host/ubertooth-tools/src/ubertooth-util.c),
# which is where the fake's option set comes from: an independent statement of
# what options there are, so the matrix is not compared with itself.
UTIL_OPTIONS = ":U:hn::meiIprsStvbBwl::a::C::c::d::q::z::VNx"
# The refusal, copied from the fork rather than taken from the module under
# test: ubertooth-util.c in fork #1260 (for ubertooth#1255) prints it as
# `error: -V cannot be combined with -%c; -V prints the version and ends the
# command, so run them as separate commands`, and its own command-line matrix
# records the same line in cli_spec.json. Taking it from
# util_version_cases.CONFLICT_REASON would have let the two agree on wording
# the tool never prints, which is exactly the mistake the first round of this
# test made.
FORK_REFUSAL = ("error: -V cannot be combined with {option}; -V prints the "
                "version and ends the command, so run them as separate "
                "commands")
# The three -V keeps, so every other letter of that string is suppressed.
KEPT = ("U", "h", "V")
# The options that take a value, attached or as the next word.
OPTIONAL_VALUE = "lacCdqzn"
# What a value the fake checks must be, for the one case that gives a bad one.
BAD_VALUES = {("a", "8"): "error: PA level must be a single digit 0-7 (got 8)\n"}


def suppressed_letters() -> set[str]:
    """The letters of UTIL_OPTIONS that -V would suppress."""
    letters = {character for character in UTIL_OPTIONS
               if character.isalnum()}
    return letters - set(KEPT)


class FakeUtil:
    """ubertooth-util as ubertooth#1255 asks for it.

    The order it decides in: the option loop (a value given twice, then a bad
    value), the operand check, the -V conflict check, then -V and -h, which
    both print and end the command. With no board on the bus -V answers the
    host line and one firmware line saying nothing is attached, exit 0, and
    says nothing about the no-devices switch, which belongs to a command that
    wanted a board.

    ``bugs`` switches on one way of getting it wrong, each of which some case
    here must catch:

    * no-conflict-check: the behavior before ubertooth#1255 -- -V prints and
      ends the command, dropping the option beside it;
    * only-after-version: the conflict is noticed only for an option written
      after -V;
    * wrong-words: the refusal says something else;
    * version-then-refuse: the version answer is printed and the refusal
      follows it;
    * note-after-refusal, second-error-line: the refusal says more than one
      thing;
    * mode-started: a mode's start line comes out with the refusal;
    * board-opened: the refusal comes after the board lookup;
    * selector-refused, help-refused: an option -V keeps is refused;
    * help-first: -h prints the usage above the version answer;
    * conflict-first: the conflict is decided before the value, repeat and
      operand checks, so a bad command line is reported as a conflict.

    ``probe_says_yes`` is what the probe answers: by default it follows the
    behavior, so the pre-#1255 build reports "no" and the cases skip. Setting
    it True with no-conflict-check on is the build that has to fail rather
    than skip.
    """

    RELEASE_VERSION = f"Host tools version: {RELEASE} (libbtbb {LIBBTBB_RELEASE})"
    NOT_ATTACHED = "Firmware version: no Ubertooth attached"

    def __init__(self, *, bugs=(), probe_says_yes=None, switch=True):
        self.bugs = set(bugs)
        self.switch = switch
        self.strict = "no-conflict-check" not in self.bugs
        self.probe_says_yes = (self.strict if probe_says_yes is None
                               else probe_says_yes)
        self.commands = []

    # -- the harness's own commands ----------------------------------------

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-V"):
            # Preflight's own read, outside the no-devices switch.
            return self._result(command, 0, f"{self.RELEASE_VERSION}\n")
        if command[0] == sys.executable:
            assert Path(command[1]).name == "utilconflict.py", command
            assert command[2] == "probe", command
            return self._result(command, 0,
                                f"Version conflicts: {self._probe()}\n")
        assert command[:3] == refusal_cases.NO_DEVICES + ("ubertooth-util",), command
        return self._util(command, list(command[3:]))

    def _probe(self) -> str:
        if not self.switch:
            return "no: /p/lib/libubertooth.so.1 predates UBERTOOTH_TEST_NO_DEVICES"
        if not self.probe_says_yes:
            return ("no: ubertooth-util -V -B exits 0, so this build drops -B "
                    "rather than refusing it (ubertooth#1255 is not in it)")
        return "yes (/p/bin/ubertooth-util)"

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    # -- the tool ----------------------------------------------------------

    def _util(self, command, words):
        given, operands, with_value = [], [], set()
        bad_value = repeated = ""
        index = 0
        while index < len(words):
            word = words[index]
            index += 1
            if not word.startswith("-") or word == "-":
                operands.append(word)
                continue
            option, value = word[1], word[2:] or None
            if option == "U":
                if value is None and index < len(words):
                    value = words[index]
                    index += 1
            elif option not in OPTIONAL_VALUE:
                value = None
            elif value is None and index < len(words):
                # An optional value may be the next word (-c 2426, -z -90),
                # which opt_separate_value() takes when it reads as a number;
                # only -z takes a negative one.
                nxt = words[index]
                numeric = (nxt.lstrip("-").isdigit() if option == "z"
                           else nxt.isdigit())
                if numeric:
                    value = nxt
                    index += 1
            if value is not None:
                if option in with_value and not repeated:
                    repeated = (f"error: option '-{option}' given more than "
                                "once\n")
                with_value.add(option)
                if not bad_value:
                    bad_value = BAD_VALUES.get((option, value), "")
                # -c and -C are one channel setting written two ways, so a
                # value given to both is refused as the second is read.
                if option in "cC" and not bad_value:
                    other = "C" if option == "c" else "c"
                    if other in with_value:
                        bad_value = (f"error: -{option} cannot be combined "
                                     f"with -{other}; they are the same "
                                     "channel setting written two ways\n")
            given.append((option, value))
        letters = [option for option, _value in given]
        conflict = self._conflict(words, letters)
        if "conflict-first" in self.bugs and conflict:
            return self._refuse(command, conflict)
        # The option loop: a value given twice, then a bad value.
        if repeated:
            return self._refuse(command, repeated)
        if bad_value:
            return self._refuse(command, bad_value)
        # Then the one-argument rule, before -V.
        if operands:
            return self._refuse(
                command, f"error: unexpected argument '{operands[0]}'\n")
        if conflict:
            return self._refuse(command, conflict)
        if "V" not in letters and "h" not in letters:
            # No case here gets this far: every command line carries one.
            return self._result(command, 1, "", NOTE
                                + "error: no Ubertooth devices found\n")
        answer = ""
        if "h" in letters and "help-first" in self.bugs:
            answer += USAGE
        if "V" in letters:
            answer += f"{self.RELEASE_VERSION}\n{self.NOT_ATTACHED}\n"
        if "h" in letters and "help-first" not in self.bugs:
            answer += USAGE
        return self._result(command, 0, answer)

    def _conflict(self, words, letters) -> str:
        """The refusal this command line earns, or "" for none.

        The option named is the first one written that -V does not use, which
        is what the fork's own command-line matrix pins.
        """
        if "V" not in letters:
            return ""
        if "no-conflict-check" in self.bugs:
            return ""
        suppressed = [option for option in letters
                      if option in suppressed_letters()]
        if "selector-refused" in self.bugs and "U" in letters:
            suppressed.insert(0, "U")
        if "help-refused" in self.bugs and "h" in letters:
            suppressed.insert(0, "h")
        if not suppressed:
            return ""
        if "only-after-version" in self.bugs:
            # Only an option written after -V is noticed, which is what
            # running both orders is for.
            after = [word for word in words[words.index("-V") + 1:]
                     if word.startswith("-") and word != "-"]
            if not any(word[1] in suppressed for word in after):
                return ""
        option = f"-{suppressed[0]}"
        if "last-option-named" in self.bugs:
            option = f"-{suppressed[-1]}"
        if "transposed" in self.bugs:
            # The two options the other way round in the message, which is
            # what the harness expected before it was held to the fork's own
            # line.
            return (f"error: {option} cannot be combined with -V; -V reports "
                    "the version and ends the command, so run them as "
                    "separate commands\n")
        if "wrong-words" in self.bugs:
            return f"error: {option} and -V cannot be used together\n"
        return FORK_REFUSAL.format(option=option) + "\n"

    def _refuse(self, command, message):
        stdout = ""
        stderr = ""
        if "board-opened" in self.bugs:
            stderr += NOTE
        if "version-then-refuse" in self.bugs:
            stdout += f"{self.RELEASE_VERSION}\n{self.NOT_ATTACHED}\n"
        if "mode-started" in self.bugs:
            stderr += "Entering LED specan mode (threshold -85 dBm)\n"
        stderr += message
        if "second-error-line" in self.bugs:
            stderr += "error: no Ubertooth devices found\n"
        if "note-after-refusal" in self.bugs:
            stderr += "note: run the two commands separately\n"
        return self._result(command, 1, stdout, stderr)


def cases(coverage):
    definition = builtin_catalog().get(NAME)
    arguments = definition.normalize_arguments(
        {"hosts": ["Host_Mac"], "coverage": coverage})
    return arguments, definition.command_cases(arguments, {}, {})


def run(executor, coverage="comprehensive"):
    arguments, _cases = cases(coverage)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = TestInvocation(f"001-{NAME}", NAME, "Host_Mac", arguments, {}, {})
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(executor, event_sink=sink).run(
            (invocation,), root, run_id="named-run", manifest=manifest,
            preflight_checks=checks,
        )
        return summary, sink.events


def refusing_case_names(coverage="comprehensive") -> set[str]:
    """The names of the cases that require a conflict refusal.

    Every case whose steps carry the refusal pattern, read from the cases
    themselves: a build with no check at all must fail exactly these, and
    naming them is what a count of them could not do.
    """
    _arguments, built = cases(coverage)
    # The patterns are regexes, so the reason appears in them escaped.
    wanted = re.escape(util_version_cases.CONFLICT_REASON)
    return {case.name for case in built
            if any(wanted in pattern
                   for step in case.steps for pattern in step.output_patterns)}


# The ubertooth-util option table of COVERAGE.md, which is how the harness
# records what options the tool has: every option named in the first cell of a
# row, so a row naming two of them (`-U<index|serial>` beside another
# spelling) contributes both.
_COVERAGE_ROW = re.compile(r"^\|(?P<cell>[^|]*)\|")
_COVERAGE_OPTION = re.compile(r"`-(?P<letter>[A-Za-z0-9])")


def coverage_letters() -> set[str]:
    """The option letters COVERAGE.md's ubertooth-util table lists."""
    text = (REPOSITORY / "COVERAGE.md").read_text(encoding="utf-8")
    section = text.split("\n## ubertooth-util\n", 1)
    assert len(section) == 2, "COVERAGE.md has no '## ubertooth-util' section"
    found = set()
    for line in section[1].splitlines():
        if line.startswith("## "):
            break
        row = _COVERAGE_ROW.match(line)
        if row is None:
            continue
        found.update(match.group("letter")
                     for match in _COVERAGE_OPTION.finditer(row.group("cell")))
    assert len(found) > 20, found
    return found


class MatrixTests(unittest.TestCase):
    """The matrix against two statements it was not derived from."""

    def test_the_matrix_is_every_option_the_fork_has_but_the_three_v_keeps(self):
        self.assertEqual(
            suppressed_letters(),
            {option[1] for option in util_version_cases.CONFLICTING},
        )
        self.assertEqual(23, len(util_version_cases.CONFLICTING))

    def test_the_matrix_matches_the_coverage_document(self):
        # A ubertooth-util option added to COVERAGE.md and not here, or here
        # and not there, fails: that is what keeps a new option from escaping
        # the check.
        kept = {option[1] for option in util_version_cases.KEPT_OPTIONS}
        removed = {option[1] for option in util_version_cases.REMOVED_OPTIONS}
        self.assertEqual(
            coverage_letters() - kept - removed,
            {option[1] for option in util_version_cases.CONFLICTING},
        )
        # The three kept and the two removed are all really in that table, so
        # neither list can excuse an option the document never names.
        self.assertTrue(kept | removed <= coverage_letters())

    def test_the_matrix_comes_from_the_tables_the_harness_owns(self):
        from ubertooth_harness import transitions, util_refusal_cases
        reads = {option for option, _label, _pattern in transitions.QUERY_OPTIONS}
        setters = {setting.set_prefix for setting in util_version_cases.SETTINGS}
        others = {option for option, _label in util_version_cases.OTHER_OPTIONS}
        self.assertEqual(
            set(util_version_cases.CONFLICTING),
            reads | set(util_refusal_cases.MODES) | setters | others,
        )
        # Every setter form names a value the setting itself takes.
        self.assertEqual(setters, set(util_version_cases.SETTER_VALUES))

    def test_every_conflicting_option_has_a_label_and_a_case_of_its_own(self):
        self.assertEqual(set(util_version_cases.CONFLICTING),
                         set(util_version_cases.LABELS))
        _a, full = cases("comprehensive")
        conflicts = {case.case_id for case in full
                     if case.case_id.startswith("conflict-")}
        self.assertEqual(
            {f"conflict-{util_version_cases.LABELS[option]}"
             for option in util_version_cases.CONFLICTING},
            conflicts,
        )

    def test_no_option_v_keeps_is_in_the_matrix(self):
        for option in util_version_cases.KEPT_OPTIONS:
            self.assertNotIn(option, util_version_cases.CONFLICTING)

    def test_the_smoke_subset_is_one_of_each_kind(self):
        self.assertTrue(
            set(util_version_cases.SMOKE_OPTIONS)
            <= set(util_version_cases.CONFLICTING))
        # The two ubertooth#1255 names, a setter, a mode, and the count.
        self.assertIn("-B", util_version_cases.SMOKE_OPTIONS)
        self.assertIn("-s", util_version_cases.SMOKE_OPTIONS)


class PatternTests(unittest.TestCase):

    def test_the_pattern_matches_the_line_the_fork_prints(self):
        # FORK_REFUSAL is copied from fork #1260, not built from the module,
        # so this is the one assertion that would have caught the harness and
        # the fork agreeing on wording the tool never prints.
        for option in ("-B", "-s", "-N", "-i", "-c"):
            with self.subTest(option):
                self.assertRegex(FORK_REFUSAL.format(option=option) + "\n",
                                 util_version_cases.conflict_error(option))

    def test_the_message_is_matched_whole(self):
        line = FORK_REFUSAL.format(option="-B") + "\n"
        self.assertRegex(line, util_version_cases.conflict_error("-B"))
        # Another option, the two options the other way round, the earlier
        # wording, and a truncated line all fail to match.
        self.assertNotRegex(line, util_version_cases.conflict_error("-s"))
        self.assertNotRegex(
            "error: -B cannot be combined with -V; -V prints the version and "
            "ends the command, so run them as separate commands\n",
            util_version_cases.conflict_error("-B"))
        self.assertNotRegex(
            "error: -V cannot be combined with -B; -V reports the version and "
            "ends the command, so run them as separate commands\n",
            util_version_cases.conflict_error("-B"))
        self.assertNotRegex("error: -V cannot be combined with -B\n",
                            util_version_cases.conflict_error("-B"))
        # -i and -I are different options.
        self.assertNotRegex(FORK_REFUSAL.format(option="-I") + "\n",
                            util_version_cases.conflict_error("-i"))


class CaseTests(unittest.TestCase):

    def test_case_sets_and_no_board(self):
        definition = builtin_catalog().get(NAME)
        self.assertTrue(definition.implemented)
        self.assertEqual({}, dict(definition.hardware_roles or {}))
        _a, smoke = cases("smoke")
        # Five options, and the four combinations -V keeps.
        self.assertEqual(5 + 4, len(smoke))
        _a, full = cases("comprehensive")
        # Every option, the four kept, and seven about order and naming.
        self.assertEqual(23 + 4 + 7, len(full))
        self.assertEqual(len(full), len({case.case_id for case in full}))
        for case in full:
            self.assertIsInstance(case, StateCase)
            self.assertEqual("probe", case.steps[0].step_id)
            self.assertEqual(("host-version",),
                             tuple(step.step_id for step in case.cleanup))
            for step in case.steps + case.cleanup:
                argv = step.resolve_argv({})
                self.assertFalse(step.mutates or step.continuous)
                if argv[0] == sys.executable:
                    continue
                # Every tool runs with the switch, one command per step.
                self.assertEqual(
                    refusal_cases.NO_DEVICES + ("ubertooth-util",), argv[:3])
                self.assertEqual(" ".join(argv), step.name)

    def test_every_refusal_requires_exit_one_and_forbids_a_version_answer(self):
        _a, full = cases("comprehensive")
        refusals = [step for case in full for step in case.steps
                    if step.step_id.startswith("refuse-")]
        self.assertTrue(refusals)
        for step in refusals:
            with self.subTest(step.name):
                self.assertEqual((1,), step.expected_returncodes)
                self.assertIn(refusal_cases.NO_BOARD_NOTE, step.forbidden_patterns)
                for pattern in util_version_cases.VERSION_OUTPUT:
                    self.assertIn(pattern, step.forbidden_patterns)
                self.assertIn(util_version_cases.MODE_START,
                              step.forbidden_patterns)
                for pattern in util_version_cases.ONE_ERROR_ONLY:
                    self.assertIn(pattern, step.forbidden_patterns)
                # Standard output is where a version answer would go.
                self.assertIsNotNone(step.output_check)

    def test_every_option_is_written_on_both_sides_of_version(self):
        _a, full = cases("comprehensive")
        pairs = set()
        for case in full:
            if not case.case_id.startswith("conflict-"):
                continue
            for step in case.steps[1:]:
                words = step.resolve_argv({})[3:]
                self.assertEqual(2, len(words), words)
                pairs.add(words)
        for option in util_version_cases.CONFLICTING:
            for word in util_version_cases.option_forms(option):
                with self.subTest(word):
                    self.assertIn(("-V", word), pairs)
                    self.assertIn((word, "-V"), pairs)

    def test_fork_1260_passes_everything(self):
        summary, _events = run(FakeUtil(), coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        summary, _events = run(FakeUtil())
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_a_build_before_1255_skips_rather_than_failing(self):
        # The probe follows the behavior, so a fork commit that drops the
        # option beside -V reports "no" and every case skips. That is what
        # lets this test sit in a plan before fork #1260 lands.
        old = FakeUtil(bugs=("no-conflict-check",))
        summary, _events = run(old)
        self.assertTrue(summary.passed, summary.failure_reason)
        tools = {command[3:] for command in old.commands
                 if command[:2] == refusal_cases.NO_DEVICES}
        # Nothing but the cleanup -V ran.
        self.assertEqual({("-V",)}, tools)

    def test_an_old_library_skips_too(self):
        old = FakeUtil(switch=False)
        summary, _events = run(old)
        self.assertTrue(summary.passed, summary.failure_reason)
        tools = {command[3:] for command in old.commands
                 if command[:2] == refusal_cases.NO_DEVICES}
        self.assertEqual({("-V",)}, tools)

    def test_a_build_that_claims_the_check_and_drops_the_option_fails(self):
        # The one outcome worth designing against is the opposite of a skip:
        # a probe that said "no" while the check was there. Here the check is
        # not there and the probe says it is, so every case that asks for a
        # refusal fails and the ones that do not still pass.
        summary, events = run(
            FakeUtil(bugs=("no-conflict-check",), probe_says_yes=True))
        self.assertFalse(summary.passed)
        failed = set(failed_names(events))
        self.assertEqual(refusing_case_names(), failed)
        # The four pre-conflict order cases still pass: their refusals come
        # from the option loop and the operand check, which this build has.
        self.assertNotIn("util -V -B stray: the stray word is refused first",
                         failed)
        self.assertNotIn("util -V -a8: the value is refused, not the conflict",
                         failed)

    def test_noticing_only_the_option_after_version_fails_every_case(self):
        summary, events = run(FakeUtil(bugs=("only-after-version",)),
                              coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(refusing_case_names("smoke"), set(failed_names(events)))

    def test_the_transposed_wording_fails_every_case(self):
        # The wording the harness expected before it was held to the fork's
        # own line: the two options the other way round, and "reports" for
        # "prints".
        summary, events = run(FakeUtil(bugs=("transposed",)), coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(refusing_case_names("smoke"), set(failed_names(events)))

    def test_other_words_fail_every_case(self):
        summary, events = run(FakeUtil(bugs=("wrong-words",)), coverage="smoke")
        self.assertEqual(refusing_case_names("smoke"), set(failed_names(events)))

    def test_naming_the_last_suppressed_option_fails_that_case(self):
        # The message names the first option written, not the last.
        summary, events = run(FakeUtil(bugs=("last-option-named",)))
        self.assertEqual(
            ["util with two suppressed options names the first one written"],
            failed_names(events))

    def test_a_version_answer_before_the_refusal_fails_every_case(self):
        summary, events = run(FakeUtil(bugs=("version-then-refuse",)),
                              coverage="smoke")
        self.assertEqual(refusing_case_names("smoke"), set(failed_names(events)))

    def test_a_refusal_that_says_more_than_one_thing_fails(self):
        for bug in ("second-error-line", "note-after-refusal", "mode-started",
                    "board-opened"):
            with self.subTest(bug):
                summary, events = run(FakeUtil(bugs=(bug,)), coverage="smoke")
                self.assertFalse(summary.passed)
                self.assertEqual(refusing_case_names("smoke"),
                                 set(failed_names(events)))

    def test_refusing_the_selector_or_the_help_fails_the_kept_cases(self):
        summary, events = run(FakeUtil(bugs=("selector-refused",)),
                              coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(
            ["util -U <selector> -V keeps the selector",
             "util -U all -V keeps the listing selector"],
            sorted(failed_names(events)))
        summary, events = run(FakeUtil(bugs=("help-refused",)), coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(
            ["util -V -h prints both answers, the version first"],
            failed_names(events))

    def test_the_usage_text_above_the_version_fails_the_help_case(self):
        summary, events = run(FakeUtil(bugs=("help-first",)), coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(
            ["util -V -h prints both answers, the version first"],
            failed_names(events))

    def test_deciding_the_conflict_first_fails_the_order_cases(self):
        # A bad value, a repeat, the -c/-C clash and a stray word are all
        # reported before the conflict; a build that decided the conflict
        # first would answer a mistyped command line with the wrong
        # complaint.
        summary, events = run(FakeUtil(bugs=("conflict-first",)))
        self.assertEqual(
            ["util -V -B stray: the stray word is refused first",
             "util -V -a8: the value is refused, not the conflict",
             "util -V -c2426 -C24: the -c/-C clash is refused first",
             "util -V -c2426 -c2450: the repeat is refused first"],
            sorted(failed_names(events)))


class ProbeTests(unittest.TestCase):
    """`utilconflict.probe` against a prefix built here: no real tool."""

    def _prefix(self, root, *, script, library=b"\0UBERTOOTH_TEST_NO_DEVICES\0"):
        (root / "bin").mkdir()
        (root / "lib").mkdir()
        tool = root / "bin" / "ubertooth-util"
        tool.write_text(script)
        tool.chmod(0o755)
        if library is not None:
            (root / "lib" / "libubertooth.so.1.1").write_bytes(library)
        return str(root / "bin")

    def test_a_build_that_refuses_the_pair_reads_as_yes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._prefix(Path(directory),
                                script="#!/bin/sh\necho 'error: -B cannot be"
                                       " combined with -V' >&2\nexit 1\n")
            self.assertTrue(
                utilconflict.probe("ubertooth-util", path).startswith("yes ("))

    def test_a_build_that_drops_the_option_reads_as_no(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._prefix(Path(directory),
                                script="#!/bin/sh\necho 'Host tools version: x'\n")
            self.assertIn("exits 0", utilconflict.probe("ubertooth-util", path))

    def test_another_exit_status_reads_as_no(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._prefix(Path(directory), script="#!/bin/sh\nexit 2\n")
            self.assertIn("exited 2", utilconflict.probe("ubertooth-util", path))

    def test_an_old_library_is_reported_without_running_the_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "ran"
            path = self._prefix(
                root, script=f"#!/bin/sh\ntouch {marker}\nexit 1\n",
                library=b"\0getenv\0libusb\0")
            self.assertIn("predates UBERTOOTH_TEST_NO_DEVICES",
                          utilconflict.probe("ubertooth-util", path))
            self.assertFalse(marker.exists(),
                             "a library without the switch must not be run")

    def test_a_missing_tool_and_a_missing_library(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._prefix(Path(directory), script="#!/bin/sh\nexit 1\n",
                                library=None)
            self.assertIn("no libubertooth beside",
                          utilconflict.probe("ubertooth-util", path))
            self.assertIn("is not installed",
                          utilconflict.probe("ubertooth-nothing", path))

    def test_the_command_line_prints_one_line(self):
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, utilconflict.main(["probe", "ubertooth-nothing"]))
        self.assertTrue(out.getvalue().startswith("Version conflicts: no: "))


class PlanTests(unittest.TestCase):

    def test_plans_use_the_test(self):
        catalog = builtin_catalog()
        expected = {"fast.yaml": "smoke", "release.yaml": "comprehensive"}
        for filename, profile in expected.items():
            plan = load_plan(REPOSITORY / "testplans" / filename, catalog)
            entries = [entry for suite in plan.suites.values() for entry in suite
                       if entry.test_name == NAME]
            self.assertEqual(
                [profile], [entry.arguments["coverage"] for entry in entries],
                filename)

    def test_the_ci_plan_leaves_it_out_until_the_fork_change_lands(self):
        # scripts/ci.sh runs testplans/host.yaml in the build job and asserts
        # that no case skipped at run time. Until fork #1260 merges, every
        # case of this test skips there, so the entry waits: adding it now
        # would fail CI on both runners rather than measuring anything.
        plan = load_plan(REPOSITORY / "testplans" / "host.yaml", builtin_catalog())
        self.assertEqual(
            [], [entry.test_name for suite in plan.suites.values()
                 for entry in suite if entry.test_name == NAME])
        self.assertIn('assert summary["skipped_cases"] == 0',
                      (REPOSITORY / "scripts" / "ci.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
