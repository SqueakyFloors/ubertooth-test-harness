import sys
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness import refusal_cases, util_refusal_cases
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import StateCase
from tests.test_sequencer import CollectSink, run_contract
from tests.test_tool_cases import failed_names

NAME = "Util mode refusals"
REPOSITORY = Path(__file__).resolve().parents[1]
NOTE = "note: UBERTOOTH_TEST_NO_DEVICES is set, so no board is selected\n"
MODES = "qnetxIi"  # util_modes.h order
USAGE = "ubertooth-util - command line utility\nUsage: ...\n"
DFU_NOTE = "ubertooth-util -f is no longer required - use ubertooth-dfu instead\n"
# What libubertooth says when a selection found no board at all, since fork
# 753269b (#655); it used to be an unprefixed "could not open Ubertooth device".
NO_DEVICES_FOUND = "error: no Ubertooth devices found\n"
COUNT_CASE = "count-with-mode"


class FakeUtil:
    """ubertooth-util's argument checks as at fork main db5649b: unknown
    options (-f was removed), stray word, -h/-V, util_check_modes(), -z with
    a mode, -U all, then -N, which prints the count and carries on to the
    board lookup. ``bugs``: pairs-accepted (no mode check), squelch-dropped
    (no -z check), order-as-given (modes named in command order), dfu-mode
    (-f still a mode, as before db5649b), count-drops (-N prints the count
    and exits 0, dropping the rest, as before db5649b), old-no-board-line
    (a selection that found nothing says the unprefixed "could not open
    Ubertooth device", as before 753269b). ``switch`` False is a library from
    before b6d8ede."""

    def __init__(self, *, bugs=(), switch=True):
        self.bugs = set(bugs)
        self.switch = switch
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-V"):
            return self._result(command, 0, "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0\n")
        if command[0] == sys.executable:
            assert Path(command[1]).name == "refusal.py" and command[2] == "probe", command
            answer = ("yes (/p/lib/libubertooth.so.1)" if self.switch else
                      "no: /p/lib/libubertooth.so.1 predates UBERTOOTH_TEST_NO_DEVICES")
            return self._result(command, 0, f"No-device switch: {answer}\n")
        assert command[:3] == refusal_cases.NO_DEVICES + ("ubertooth-util",), command
        return self._util(command, list(command[3:]))

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    def _util(self, command, words):
        modes_known = MODES + ("f" if "dfu-mode" in self.bugs else "")
        given, operands, device, squelch = [], [], None, False
        index = 0
        while index < len(words):
            word = words[index]
            index += 1
            if not word.startswith("-") or word == "-":
                operands.append(word)
                continue
            option = word[1]
            if option == "f" and "dfu-mode" not in self.bugs:
                return self._result(command, 1, "", "error: unrecognized option '-f'\n"
                                    + USAGE)
            if option == "U":
                device = word[2:] or words[index]
                index += 0 if word[2:] else 1
                continue
            if option == "z":
                squelch = True
            given.append(option)
        if operands:
            return self._result(command, 1, "",
                                f"error: unexpected argument '{operands[0]}'\n")
        if "V" in given and len(given) == 1:
            # The whole -V answer with the no-device switch set: the host
            # lines, then nothing attached, exit 0 and no switch note.
            return self._result(
                command, 0,
                "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0\n"
                "libbtbb loaded from: /p/lib/libbtbb.so.1\n"
                "Firmware version: no Ubertooth attached\n")
        if "h" in given:
            return self._result(command, 0, USAGE)
        modes = [mode for mode in modes_known if mode in given]
        if "order-as-given" in self.bugs:
            modes = list(dict.fromkeys(option for option in given if option in modes_known))
        if len(modes) > 1 and "pairs-accepted" not in self.bugs:
            return self._result(command, 1, "", "error: options "
                                + " ".join("-" + mode for mode in modes)
                                + " cannot be combined; give only one of "
                                + " ".join("-" + mode for mode in modes_known) + "\n")
        if squelch and modes and "squelch-dropped" not in self.bugs:
            return self._result(command, 1, "",
                                f"error: -z cannot be combined with -{modes[0]}\n")
        if device == "all":
            return self._result(command, 1, "",
                                "error: -U all only supports the read-only queries -b, "
                                "-v, -V, -p and -s; select one device with "
                                "-U<index|serial> for other options\n")
        no_board = ("could not open Ubertooth device\n"
                    if "old-no-board-line" in self.bugs else NO_DEVICES_FOUND)
        err = DFU_NOTE if "f" in given else ""
        if "N" in given:
            err += NOTE
            if "count-drops" in self.bugs or given == ["N"]:
                return self._result(command, 0, "0\n", err)
            return self._result(command, 1, "0\n", err + no_board)
        return self._result(command, 1, "", err + NOTE + no_board)


def cases(coverage):
    definition = builtin_catalog().get(NAME)
    arguments = definition.normalize_arguments({"hosts": ["Host_Mac"], "coverage": coverage})
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


class PatternTests(unittest.TestCase):

    def test_messages_follow_the_fork_order(self):
        self.assertRegex(
            "error: options -x -i cannot be combined; give only one of "
            "-q -n -e -t -x -I -i\n",
            util_refusal_cases.modes_pattern(("-i", "-x")))
        self.assertNotRegex(
            "error: options -i -x cannot be combined; give only one of "
            "-q -n -e -t -x -I -i\n",
            util_refusal_cases.modes_pattern(("-i", "-x")))
        # The mode list before db5649b, which named -f, no longer matches.
        self.assertNotRegex(
            "error: options -x -i cannot be combined; give only one of "
            "-q -n -e -t -x -I -i -f\n",
            util_refusal_cases.modes_pattern(("-i", "-x")))
        # -i and -I are different options.
        self.assertNotRegex(
            "error: options -x -I cannot be combined; give only one of "
            "-q -n -e -t -x -I -i\n",
            util_refusal_cases.modes_pattern(("-i", "-x")))
        self.assertRegex("error: -z cannot be combined with -i\n",
                         util_refusal_cases.squelch_pattern("-i"))
        self.assertNotRegex("error: -z cannot be combined with -I\n",
                            util_refusal_cases.squelch_pattern("-i"))


class CaseTests(unittest.TestCase):

    def test_case_sets_and_no_board(self):
        definition = builtin_catalog().get(NAME)
        self.assertTrue(definition.implemented)
        self.assertEqual({}, dict(definition.hardware_roles or {}))
        _a, smoke = cases("smoke")
        # -i with each of the six other modes; -z with -i; -f unknown.
        self.assertEqual(6 + 1 + 1, len(smoke))
        _a, full = cases("comprehensive")
        self.assertEqual(len(smoke) + 6, len(full))
        self.assertEqual(len(full), len({case.case_id for case in full}))
        self.assertNotIn("-f", util_refusal_cases.MODES)
        smoke_ids = {case.case_id for case in smoke}
        pairs = set()
        for case in full:
            self.assertIsInstance(case, StateCase)
            self.assertEqual("probe", case.steps[0].step_id)
            for step in case.steps + case.cleanup:
                argv = step.resolve_argv({})
                self.assertFalse(step.mutates or step.continuous)
                if argv[0] == sys.executable:
                    continue
                # Every tool runs with the switch, one command per step.
                self.assertEqual(refusal_cases.NO_DEVICES + ("ubertooth-util",), argv[:3])
                self.assertEqual(" ".join(argv), step.name)
                if case.case_id in smoke_ids and step not in case.cleanup and len(argv) == 5:
                    pairs.add(argv[3:])
                if step not in case.cleanup:
                    self.assertEqual((1,), step.expected_returncodes)
                    # The -N case runs -i, so the switch's note is printed.
                    if case.case_id == COUNT_CASE:
                        self.assertNotIn(refusal_cases.NO_BOARD_NOTE, step.forbidden_patterns)
                    else:
                        self.assertIn(refusal_cases.NO_BOARD_NOTE, step.forbidden_patterns)
        # Every ordered pair with -i, in both orders.
        modes = util_refusal_cases.MODES
        expected = {(a, b) for a in modes for b in modes if a != b and "-i" in (a, b)}
        self.assertEqual(expected, pairs & expected)
        self.assertEqual(12, len(expected))

    def test_fork_main_passes_everything(self):
        summary, _events = run(FakeUtil(), coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        summary, _events = run(FakeUtil())
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_an_accepted_pair_fails_every_pair_case(self):
        summary, events = run(FakeUtil(bugs=("pairs-accepted",)), coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(6, len(failed_names(events)))

    def test_dropped_squelch_fails_the_squelch_case(self):
        summary, events = run(FakeUtil(bugs=("squelch-dropped",)), coverage="smoke")
        self.assertEqual(["util -z with -i refused"], failed_names(events))

    def test_command_order_fails_every_pair_case(self):
        summary, events = run(FakeUtil(bugs=("order-as-given",)), coverage="smoke")
        self.assertEqual(6, len(failed_names(events)))

    def test_dfu_still_a_mode_fails_the_unknown_option_and_pair_cases(self):
        # Before db5649b: -f is a mode, so the mode list in every pair
        # refusal names it too.
        summary, events = run(FakeUtil(bugs=("dfu-mode",)), coverage="smoke")
        failed = failed_names(events)
        self.assertIn("util -f is refused as an unknown option", failed)
        self.assertEqual(6 + 1, len(failed))

    def test_count_dropping_the_mode_fails_the_count_case(self):
        summary, events = run(FakeUtil(bugs=("count-drops",)))
        self.assertEqual(["util -N with -i prints the count, then runs -i"],
                         failed_names(events))

    def test_the_old_no_board_line_fails_the_count_case(self):
        # The wording before fork 753269b: the case asked for it until #174.
        summary, events = run(FakeUtil(bugs=("old-no-board-line",)))
        self.assertEqual(["util -N with -i prints the count, then runs -i"],
                         failed_names(events))
        self.assertIn("error: no Ubertooth devices found",
                      summary.failure_reason)

    def test_old_library_skips_and_runs_only_the_host_version(self):
        old = FakeUtil(switch=False)
        summary, _events = run(old)
        self.assertTrue(summary.passed, summary.failure_reason)
        tools = {c[3:] for c in old.commands if c[:2] == refusal_cases.NO_DEVICES}
        self.assertEqual({("-V",)}, tools)


class PlanTests(unittest.TestCase):

    def test_plans_use_the_test(self):
        catalog = builtin_catalog()
        expected = {"fast.yaml": "smoke", "host.yaml": "smoke", "release.yaml": "comprehensive"}
        for filename, profile in expected.items():
            plan = load_plan(REPOSITORY / "testplans" / filename, catalog)
            entries = [entry for suite in plan.suites.values() for entry in suite
                       if entry.test_name == NAME]
            self.assertEqual([profile], [entry.arguments["coverage"] for entry in entries],
                             filename)


if __name__ == "__main__":
    unittest.main()
