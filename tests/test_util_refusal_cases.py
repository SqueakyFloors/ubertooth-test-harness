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
MODES = "qnetxIif"  # util_modes.h order
DFU_NOTE = "ubertooth-util -f is no longer required - use ubertooth-dfu instead\n"
STRICT = {
    "util -N with -f or -i is refused, not dropped",
    "util -U all with -f or -i: only the -U all refusal",
}


class FakeUtil:
    """ubertooth-util's argument checks as at fork 89d9197: stray word, -h/-H,
    util_check_modes(), -z with a mode, the -f note, -U all, -N, then the
    board lookup. ``fixed`` models fixes of fork #332 (-N with another option
    refused) and #333 (the -f note after the -U all check). ``bugs``:
    pairs-accepted (no mode check), squelch-dropped (no -z check),
    note-early (the -f note before every check), order-as-given (modes named
    in command order). ``switch`` False is a library from before b6d8ede."""

    def __init__(self, *, fixed=False, bugs=(), switch=True):
        self.fixed = fixed
        self.bugs = set(bugs)
        self.switch = switch
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-H"):
            return self._result(command, 0, "libubertooth 1.1 (git-test), libbtbb 1.0\n")
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
        err = []
        given, operands, device, squelch = [], [], None, False
        index = 0
        while index < len(words):
            word = words[index]
            index += 1
            if not word.startswith("-") or word == "-":
                operands.append(word)
                continue
            option = word[1]
            if option == "U":
                device = word[2:] or words[index]
                index += 0 if word[2:] else 1
                continue
            if option == "z":
                squelch = True
            given.append(option)
        if "f" in given and "note-early" in self.bugs:
            err.append(DFU_NOTE)
        if operands:
            return self._result(command, 1, "", "".join(err)
                                + f"error: unexpected argument '{operands[0]}'\n")
        if "H" in given:
            return self._result(command, 0, "libubertooth 1.1 (git-test)\n", "".join(err))
        if "h" in given:
            return self._result(command, 0, "ubertooth-util - command line utility\n")
        modes = [mode for mode in MODES if mode in given]
        if "order-as-given" in self.bugs:
            modes = list(dict.fromkeys(option for option in given if option in MODES))
        if len(modes) > 1 and "pairs-accepted" not in self.bugs:
            return self._result(command, 1, "", "".join(err) + "error: options "
                                + " ".join("-" + mode for mode in modes)
                                + " cannot be combined; give only one of "
                                + " ".join("-" + mode for mode in MODES) + "\n")
        if squelch and modes and "squelch-dropped" not in self.bugs:
            return self._result(command, 1, "", "".join(err)
                                + f"error: -z cannot be combined with -{modes[0]}\n")
        if "f" in given and not self.fixed and "note-early" not in self.bugs:
            err.append(DFU_NOTE)
        if device == "all":
            return self._result(command, 1, "", "".join(err)
                                + "error: -U all only supports the read-only queries -b, "
                                "-v, -V, -p and -s; select one device with "
                                "-U<index|serial> for other options\n")
        if "f" in given and self.fixed:
            err.append(DFU_NOTE)
        if "N" in given:
            if self.fixed and len(given) > 1:
                return self._result(command, 1, "", "error: -N cannot be combined with "
                                    "other options\n")
            return self._result(command, 0, "0\n", "".join(err) + NOTE)
        return self._result(command, 1, "", "".join(err) + NOTE
                            + "could not open Ubertooth device\n")


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
            "error: options -i -f cannot be combined; give only one of "
            "-q -n -e -t -x -I -i -f\n",
            util_refusal_cases.modes_pattern(("-f", "-i")))
        self.assertNotRegex(
            "error: options -f -i cannot be combined; give only one of "
            "-q -n -e -t -x -I -i -f\n",
            util_refusal_cases.modes_pattern(("-f", "-i")))
        # -i and -I are different options.
        self.assertNotRegex(
            "error: options -I -f cannot be combined; give only one of "
            "-q -n -e -t -x -I -i -f\n",
            util_refusal_cases.modes_pattern(("-i", "-f")))
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
        # -f and -i with each of the six other modes, and with each other;
        # then -z with each.
        self.assertEqual(6 * 2 + 1 + 2, len(smoke))
        _a, full = cases("comprehensive")
        self.assertEqual(len(smoke) + 6, len(full))
        self.assertEqual(len(full), len({case.case_id for case in full}))
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
                    self.assertIn(refusal_cases.NO_BOARD_NOTE, step.forbidden_patterns)
                    self.assertIn(util_refusal_cases.DFU_NOTE, step.forbidden_patterns)
        # Every ordered pair with -f or -i, in both orders.
        modes = util_refusal_cases.MODES
        expected = {(a, b) for a in modes for b in modes
                    if a != b and {a, b} & {"-f", "-i"}}
        self.assertEqual(expected, pairs & expected)
        self.assertEqual(26, len(expected))

    def test_fork_tool_passes_smoke_and_fails_only_the_strict_cases(self):
        summary, _events = run(FakeUtil(), coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        summary, events = run(FakeUtil())
        self.assertFalse(summary.passed)
        self.assertEqual(sorted(STRICT), failed_names(events))

    def test_fixed_tool_passes_everything(self):
        summary, _events = run(FakeUtil(fixed=True))
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_an_accepted_pair_fails_every_pair_case(self):
        summary, events = run(FakeUtil(fixed=True, bugs=("pairs-accepted",)), coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(13, len(failed_names(events)))

    def test_dropped_squelch_fails_the_squelch_cases(self):
        summary, events = run(FakeUtil(fixed=True, bugs=("squelch-dropped",)), coverage="smoke")
        self.assertEqual(["util -z with -f refused", "util -z with -i refused"],
                         failed_names(events))

    def test_early_dfu_note_and_command_order_fail(self):
        summary, events = run(FakeUtil(fixed=True, bugs=("note-early",)), coverage="smoke")
        # Every case with -f: its six pairs, -i -f, and -z with -f.
        self.assertEqual(8, len(failed_names(events)))
        summary, events = run(FakeUtil(fixed=True, bugs=("order-as-given",)), coverage="smoke")
        self.assertEqual(13, len(failed_names(events)))

    def test_old_library_skips_and_runs_only_the_host_version(self):
        old = FakeUtil(switch=False)
        summary, _events = run(old)
        self.assertTrue(summary.passed, summary.failure_reason)
        tools = {c[3:] for c in old.commands if c[:2] == refusal_cases.NO_DEVICES}
        self.assertEqual({("-H",)}, tools)


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
