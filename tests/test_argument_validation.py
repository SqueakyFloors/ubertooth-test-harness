import unittest

from ubertooth_harness import transitions
from ubertooth_harness.stateful import StateCase
from tests.test_transitions import SERIAL, FakeUbertooth, cases_for, run_test, steps


MODELED_TOOLS = ("ubertooth-util", "ubertooth-btle")
STRAY_CASES = {r.case_id for r in transitions.REJECTIONS if "unexpected argument" in r.error}


class ParsingUbertooth(FakeUbertooth):
    """FakeUbertooth plus the argument checks of the other tools (fork sources).

    ``accept`` names case IDs whose argument a broken tool would accept, so a
    test can prove the case catches it.
    """

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
        # FakeUbertooth parses util and btle like the fork; echo the expected
        # error only for the other tools, -U all, and injected regressions.
        modeled = (
            tool in MODELED_TOOLS and "all" not in options
            and rejection is not None
            and rejection.case_id not in self.accept | self.change
        )
        if rejection is None or modeled:
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

    def test_a_board_at_pa_0_still_runs_every_case(self):
        device = ParsingUbertooth(crc="1")
        device.state["pa"] = "0"

        summary, events = run_test("Argument validation", device)

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(0, summary.skipped_cases)
        self.assertEqual("0", device.state["pa"])

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
        self.assertIn("Verify pa-level unchanged", summary.failure_reason)
        self.assertEqual("7", device.state["pa"])  # cleanup restored the original

    def test_tools_before_strict_parsing_and_the_z_mode_check_fail_those_cases(self):
        # Before fork 658730c (#172) a bad value read as a query or 0, and
        # before 4ed4a1d (#159) -z with a mode was dropped; both exit 0.
        new = {
            r.case_id for r in transitions.REJECTIONS
            if r.tool == "ubertooth-util" and (
                "(got" in r.error or "-z cannot be combined" in r.error
            )
        }
        # 16 strict-parsing values (3 of them as next words) and 3 -z modes.
        self.assertEqual(22, len(new))

        class PreFix(ParsingUbertooth):
            def _apply(self, options):
                pass  # accepted without a change the readback could see

        device = PreFix(crc="1", accept=new)
        summary, events = run_test("Argument validation", device, coverage="comprehensive")

        self.assertFalse(summary.passed)
        failed = self.failed_cases(events)
        self.assertEqual(len(new), len(failed))
        self.assertEqual(
            sorted(new),
            sorted(n for n in new if any(c.endswith(f"reject-{n}") for c in failed)),
        )
        self.assertTrue(all("command exited 0" in e.details.get("reason", "")
                            for e in events if e.event_type == "step_finished"
                            and e.status == "failed"))

    def test_every_tool_has_a_stray_word_case(self):
        tools = {r.tool for r in transitions.REJECTIONS}
        stray_tools = {r.tool for r in transitions.REJECTIONS if r.case_id in STRAY_CASES}
        self.assertEqual(tools, stray_tools)
        self.assertEqual(11, len(tools))  # every built tool but follow and scan (#40)

    def test_tools_that_ignore_stray_words_fail_every_stray_case(self):
        # Before fork #146 (util) and #155 (every other tool) a leftover word
        # was dropped and the rest of the command ran with exit 0.
        others = {r.case_id for r in transitions.REJECTIONS
                  if r.case_id in STRAY_CASES and r.tool not in MODELED_TOOLS}

        class Ignoring(ParsingUbertooth):
            def _apply(self, options):
                pass

        device = Ignoring(crc="1", accept=others, reject_strays=False)
        summary, events = run_test("Argument validation", device, coverage="comprehensive")

        self.assertFalse(summary.passed)
        failed = {c.rsplit("reject-", 1)[1] for c in self.failed_cases(events)}
        self.assertEqual(STRAY_CASES, failed)

    def test_a_stray_word_that_lets_the_set_run_fails_on_the_readback(self):
        # The command is refused, but a regression applied -c2470 first.
        device = ParsingUbertooth(crc="1", change={"util-stray-after-set"})

        summary, events = run_test("Argument validation", device, coverage="comprehensive")

        self.assertFalse(summary.passed)
        self.assertEqual(1, len(self.failed_cases(events)))
        self.assertIn("expected channel after rejection=2441, observed 2470",
                      summary.failure_reason)

    def test_util_and_btle_errors_come_from_the_fake_parser(self):
        device = FakeUbertooth()
        for rejection in transitions.REJECTIONS:
            if rejection.tool not in MODELED_TOOLS or "all" in rejection.options:
                continue
            result = device.run(rejection.argv(SERIAL), timeout_s=1)
            with self.subTest(rejection.case_id):
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertRegex(result.stdout + result.stderr, rejection.error)


if __name__ == "__main__":
    unittest.main()
