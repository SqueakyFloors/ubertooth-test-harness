import unittest

from ubertooth_harness import transitions
from ubertooth_harness.stateful import StateCase
from tests.test_transitions import SERIAL, FakeUbertooth, cases_for, run_test, steps


class ParsingUbertooth(FakeUbertooth):
    """FakeUbertooth plus the argument checks of the other tools (fork sources).

    ``accept`` names case IDs whose argument a broken tool would accept, so a
    test can prove the case catches it.
    """

    ERRORS = {
        "-c2401": "error: frequency must be 2402-2480 MHz (got 2401)",
        "-c2481": "error: frequency must be 2402-2480 MHz (got 2481)",
        "-C79": "error: channel must be 0-78 (got 79)",
    }

    def __init__(self, *args, accept=(), change=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.accept = set(accept)  # applied and exit 0
        self.change = set(change)  # refused with exit 1, but the value applied anyway

    def _apply(self, options):
        if "-a5" in options:
            self.state["pa"] = "5"
        elif options[0].startswith("-c"):
            self.state["channel"] = options[0][2:]

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        tool, options = command[0], command[3:]
        rejection = next(
            (r for r in transitions.REJECTIONS
             if r.tool == tool and r.options == options), None
        )
        if rejection is None:
            return super().run(argv, timeout_s=timeout_s, cancellation=cancellation,
                               on_output=on_output)
        self.commands.append(command)
        if rejection.case_id in self.accept | self.change:
            self._apply(options)
        if rejection.case_id in self.accept:
            return self._result(command, 0, "", "", on_output)
        message = rejection.error.replace("\\", "")
        return self._result(command, 1, "", message + "\n", on_output)


class ArgumentValidationTests(unittest.TestCase):

    def test_smoke_has_one_case_per_tool_and_comprehensive_has_all(self):
        _args, smoke = cases_for("Argument validation", "smoke")
        _args, full = cases_for("Argument validation", "comprehensive")
        tools = {r.tool for r in transitions.REJECTIONS}
        smoke_tools = {r.tool for r in transitions.REJECTIONS if r.smoke}
        self.assertEqual(tools, smoke_tools)
        self.assertEqual(len(tools), len(smoke))
        self.assertEqual(len(transitions.REJECTIONS), len(full))
        self.assertTrue(all(isinstance(case, StateCase) for case in full))

    def test_every_rejected_command_selects_the_board_by_serial(self):
        _args, full = cases_for("Argument validation", "comprehensive")
        for case in full:
            reject = next(step for step in case.steps if step.step_id == "reject")
            argv = reject.resolve_argv({})
            with self.subTest(case.case_id):
                self.assertEqual(["-U", SERIAL], list(argv[1:3]))
                self.assertEqual((1,), reject.expected_returncodes)
                self.assertTrue(reject.output_patterns)
                self.assertTrue(reject.forbidden_patterns)

    def test_no_case_uses_an_option_that_would_transmit_if_accepted(self):
        transmitting = {
            "ubertooth-btle": {"-s", "-i", "-I", "-j", "-J"},
            "ubertooth-ducky": {"-q", "-b"},
            "ubertooth-ego": {"-i", "-j"},
            "ubertooth-util": {"-t", "-n", "-e"},
        }
        for rejection in transitions.REJECTIONS:
            flags = {item[:2] for item in rejection.options}
            with self.subTest(rejection.case_id):
                self.assertFalse(flags & transmitting.get(rejection.tool, set()))

    def test_rejections_pass_and_leave_the_device_unchanged(self):
        device = ParsingUbertooth(crc="1")

        summary, events = run_test("Argument validation", device, coverage="comprehensive")

        self.assertTrue(summary.passed, summary.failure_reason)
        rejected = steps(events, step_id="reject")
        self.assertEqual(len(transitions.REJECTIONS), len(rejected))
        self.assertTrue(all(e.status == "passed" for e in rejected))
        self.assertEqual("1", device.state["crc"])

    def failed_cases(self, events):
        return [
            e.details["case_id"] for e in events
            if e.event_type == "check_finished" and e.status == "failed"
        ]

    def test_an_accepted_argument_fails_the_case(self):
        device = ParsingUbertooth(crc="1", accept={"util-frequency-below-band"})

        summary, events = run_test("Argument validation", device, coverage="comprehensive")

        self.assertFalse(summary.passed)
        failed = self.failed_cases(events)
        self.assertEqual(1, len(failed))
        self.assertTrue(failed[0].endswith("reject-util-frequency-below-band"))
        self.assertIn("command exited 0", summary.failure_reason)
        self.assertEqual("2441", device.state["channel"])  # cleanup restored it

    def test_a_refused_argument_that_still_changes_the_device_fails_the_case(self):
        device = ParsingUbertooth(crc="1", change={"util-all-with-setter"})

        summary, events = run_test("Argument validation", device, coverage="comprehensive")

        self.assertFalse(summary.passed)
        failed = self.failed_cases(events)
        self.assertEqual(1, len(failed))
        self.assertTrue(failed[0].endswith("reject-util-all-with-setter"))
        self.assertIn("Verify PA level unchanged", summary.failure_reason)

if __name__ == "__main__":
    unittest.main()
