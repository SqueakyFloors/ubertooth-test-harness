"""Two-device range test against a fake sender/responder pair of the fork."""

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness import util_cases
from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.planner import PlanCompiler, PlanningError, TestInvocation
from ubertooth_harness.plan import load_plan
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.stateful import StateCase
from tests.test_guards import A, B, C, guard_checks, hardware, run_guarded
from tests.test_transitions import FakeUbertooth, patch_host_api, steps

NAME = "Two-device range test"
NOT_ANSWERING = ("no Ubertooth found with serial matching '{serial}'\n"
                 "1 attached Ubertooth device did not answer the serial read\n")


def setUpModule():
    patch_host_api()


class Air:
    """What the radio link between the boards does to a range test.

    ``result``: the four values the sender's -m reports after a round trip
    (request PA, request number, reply PA, reply number); None means no reply
    ever arrives. ``mismatch``: the byte the reply differs at, or None.
    """

    def __init__(self, result=(0, 0, 0, 0), mismatch=None):
        self.result = result
        self.mismatch = mismatch
        self.boards = {}


class RangeBoard(FakeUbertooth):
    """FakeUbertooth plus -e, -n and -m, as cc2400_rangetest.c runs them.

    -e starts the repeater: RX LED on, PA level 0, until -S. -n with a
    repeater on another board records the Air's result and ends by itself
    with the idle reset (LEDs off, 2441 MHz) at PA level 7, where its sweep
    stopped. -r clears the result. Knobs model fork firmware bugs:

    - ``repeater_answers_usb=False``: fork #26 before fork 0684c11, the
      repeater serves no USB request until power is removed.
    - ``sender_answers_usb=False``: the sender serves no USB request while it
      waits for a reply that never comes.
    - ``idle_reset_keeps_channel=True``: a firmware whose idle reset left the
      stored channel alone.

    The round trip needs the repeater on the sender's channel, as both modes
    run on the stored channel.
    """

    def __init__(self, serial, air, *, repeater_answers_usb=True,
                 sender_answers_usb=True, idle_reset_keeps_channel=False, **kwargs):
        super().__init__(serial=serial, **kwargs)
        self.air = air
        air.boards[serial] = self
        self.result = None
        self.repeater_answers_usb = repeater_answers_usb
        self.sender_answers_usb = sender_answers_usb
        self.idle_reset_keeps_channel = idle_reset_keeps_channel

    def _stop_mode(self):
        channel = self.state["channel"]
        super()._stop_mode()
        if self.idle_reset_keeps_channel:
            self.state["channel"] = channel

    def deaf(self):
        if self.mode == "repeater":
            return not self.repeater_answers_usb
        return self.mode == "waiting" and not self.sender_answers_usb

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if self.deaf():
            self.commands.append(command)
            return self._result(command, 1, "", NOT_ANSWERING.format(serial=self.serial),
                                on_output)
        return super().run(argv, timeout_s=timeout_s, cancellation=cancellation,
                           on_output=on_output)

    def range_report(self):
        """What -m prints: (exit, stdout, stderr)."""
        if self.result is None:
            return 0, "no range test result yet\n", ""
        if self.result[0] == "mismatch":
            return 1, "", f"error: invalid range test: mismatch on byte {self.result[1]}\n"
        request_pa, request_number, reply_pa, reply_number = self.result
        return 0, (f"request PA level : {request_pa}\nrequest number   : {request_number}\n"
                   f"reply PA level   : {reply_pa}\nreply number     : {reply_number}\n"), ""

    def _util(self, command, options, on_output):
        if "-r" in options:
            self.result = None
        if options == ("-m",):
            code, stdout, stderr = self.range_report()
            return self._result(command, code, stdout, stderr, on_output)
        if options == ("-e",):
            self._stop_mode()
            self.mode = "repeater"
            self.state.update(rx="1", pa="0")
            return self._result(command, 0, "", "Starting repeater\n", on_output)
        if options == ("-n",):
            self._stop_mode()
            repeating = any(
                board.mode == "repeater" and board.state["channel"] == self.state["channel"]
                for serial, board in self.air.boards.items() if serial != self.serial)
            if not repeating or self.air.result is None:
                self.mode = "waiting"  # listens for a reply that never comes
            else:
                self.result = (("mismatch", self.air.mismatch)
                               if self.air.mismatch is not None else self.air.result)
                self.mode = "range"
                self._stop_mode()  # the test ends by itself: idle reset
                self.state["pa"] = "7"
            return self._result(command, 0, "", "Starting range test\n", on_output)
        return super()._util(command, options, on_output)


class PairBench:
    """Routes each command to its board: by the last -U, or for the result
    poll (sh -c RANGE_POLL ubertooth-util <serial> ...) by its serial."""

    def __init__(self, *boards):
        self.boards = {board.serial: board for board in boards}
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command[:3] == ("sh", "-c", util_cases.RANGE_POLL):
            board = self.boards[command[4]]
            board.commands.append(command)
            # The loop's first read; the fake takes no time, so a result that
            # is not there now never arrives within the deadline either.
            if board.deaf():
                read = (1, NOT_ANSWERING.format(serial=board.serial))
            else:
                code, stdout, stderr = board.range_report()
                read = (code, stdout + stderr)
            line = f"read 1: exit {read[0]}: {' '.join(read[1].split())}\n"
            done = "request PA level" in read[1] or "mismatch on byte" in read[1]
            if done:
                return board._result(command, 0, line, "", on_output)
            return board._result(command, 1,
                                 line + "no range test result within 10 s (40 reads)\n",
                                 "", on_output)
        selections = [command[i + 1] for i, item in enumerate(command) if item == "-U"]
        return self.boards[selections[-1]].run(
            argv, timeout_s=timeout_s, cancellation=cancellation, on_output=on_output)


def invocation(*, maximum_pa=None, sender=A, responder=B, guards=()):
    raw = {"host": "Host_Linux", "sender": "UT_0", "responder": "UT_1",
           "allow RF transmission": True}
    if maximum_pa is not None:
        raw["maximum PA level"] = maximum_pa
    resources = {"sender": "UT_0", "responder": "UT_1"}
    identities = {"sender": {"serial": sender}, "responder": {"serial": responder}}
    for index, serial in enumerate(guards, 1):
        resources[f"guard Ubertooth {index}"] = f"UT_{index + 1}"
        identities[f"guard Ubertooth {index}"] = {"serial": serial}
    return TestInvocation(
        f"001-{NAME}", NAME, "Host_Linux",
        builtin_catalog().get(NAME).normalize_arguments(raw), resources, identities,
    )


def pair(air=None, **responder_knobs):
    air = air or Air()
    sender = RangeBoard(A, air)
    responder = RangeBoard(B, air, **responder_knobs)
    return air, sender, responder


def originals(board):
    return {key: board.state[key] for key in ("channel", "pa", "squelch", "usr", "rx", "tx", "crc")}


def failed_steps(events):
    return [e.details["step_id"] for e in events
            if e.event_type == "step_finished" and e.status == "failed"]


class RangeCatalogTests(unittest.TestCase):

    def test_definition_is_implemented_with_sender_and_responder_roles(self):
        definition = builtin_catalog().get(NAME)
        self.assertTrue(definition.implemented)
        self.assertEqual({"sender": "Ubertooth", "responder": "Ubertooth"},
                         dict(definition.hardware_roles))
        self.assertTrue(definition.guards_other_ubertooths)
        self.assertIsNotNone(definition.estimates)

    def test_rf_transmission_must_be_allowed(self):
        definition = builtin_catalog().get(NAME)
        base = {"host": "Host_Linux", "sender": "UT_0", "responder": "UT_1"}
        with self.assertRaisesRegex(CatalogError, "allow RF transmission"):
            definition.normalize_arguments(base)
        with self.assertRaisesRegex(CatalogError, "must be true"):
            definition.normalize_arguments({**base, "allow RF transmission": False})

    def test_maximum_pa_level_is_0_to_7_and_defaults_to_7(self):
        definition = builtin_catalog().get(NAME)
        base = {"host": "Host_Linux", "sender": "UT_0", "responder": "UT_1",
                "allow RF transmission": True}
        self.assertEqual(7, definition.normalize_arguments(base)["maximum PA level"])
        self.assertEqual(0, definition.normalize_arguments(
            {**base, "maximum PA level": 0})["maximum PA level"])
        for bad in (8, -1, "3", True):
            with self.subTest(bad), self.assertRaisesRegex(CatalogError, "PA level 0-7"):
                definition.normalize_arguments({**base, "maximum PA level": bad})

    def test_builder_needs_two_distinct_full_serials(self):
        definition = builtin_catalog().get(NAME)
        arguments = invocation().arguments
        cases = {
            "same board": ({"serial": A}, {"serial": A}, "two different boards"),
            "short serial": ({"serial": A}, {"serial": "b" * 8}, "full 32-hex-digit responder"),
            "all digits": ({"serial": "1" * 32}, {"serial": B}, "full 32-hex-digit sender"),
            "no responder": ({"serial": A}, None, "registered responder serial"),
        }
        for label, (sender, responder, message) in cases.items():
            identities = {"sender": sender}
            if responder is not None:
                identities["responder"] = responder
            with self.subTest(label), self.assertRaisesRegex(CatalogError, message):
                definition.command_cases(arguments, {}, identities)

    def test_one_state_case_with_responder_cleanup_first(self):
        (case,) = builtin_catalog().get(NAME).command_cases(
            invocation().arguments, {}, invocation().resource_identities)
        self.assertIsInstance(case, StateCase)
        cleanup = [step.step_id for step in case.cleanup]
        self.assertEqual("responder-cleanup-stop", cleanup[0])
        self.assertLess(cleanup.index("responder-restored-device-identity"),
                        cleanup.index("sender-cleanup-stop"))
        ids = [step.step_id for step in case.steps]
        self.assertLess(ids.index("responder-start-repeater"),
                        ids.index("sender-start-range-test"))
        poll = case.steps[ids.index("sender-wait-for-result")]
        self.assertEqual(util_cases.range_poll_argv(A), poll.argv)
        self.assertEqual(util_cases.RANGE_POLL_TIMEOUT_S, poll.timeout_s)


class RangeResultCheckTests(unittest.TestCase):

    RESULT = ("request PA level : {}\nrequest number   : {}\n"
              "reply PA level   : {}\nreply number     : {}\n")

    def check(self, values, maximum=7):
        return util_cases.range_result_check(maximum)(self.RESULT.format(*values))

    def test_levels_and_copies_in_range_pass(self):
        self.assertEqual("", self.check((0, 0, 0, 0)))
        self.assertEqual("", self.check((7, 15, 7, 15)))
        self.assertEqual("", self.check((2, 3, 2, 0), maximum=2))

    def test_level_above_the_maximum_fails_naming_the_direction(self):
        self.assertIn("reply got through only at PA level 3, above the plan's maximum 2",
                      self.check((1, 0, 3, 0), maximum=2))
        self.assertIn("request got through only at PA level 5",
                      self.check((5, 0, 0, 0), maximum=2))

    def test_copy_number_past_15_fails(self):
        self.assertIn("request copy number 16 is not 0-15", self.check((0, 16, 0, 0)))

    def test_no_result_fails(self):
        self.assertIn("no range test result",
                      util_cases.range_result_check(7)("no range test result yet\n"))
        self.assertIn("nothing", util_cases.range_result_check(7)(""))


class RangePollScriptTests(unittest.TestCase):
    """The real RANGE_POLL shell loop against a stand-in ubertooth-util."""

    # The default deadline is generous: the loop exits as soon as the stand-in
    # reports a result, so a long deadline costs nothing, while a short one
    # ends the poll before the reads a test is counting on when the machine is
    # busy (it ran out at 2 s under a parallel suite, issue #184). The test
    # that waits the deadline out sets its own.
    def run_poll(self, script, deadline="20", interval="0.05"):
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / "ubertooth-util"
            count = Path(directory) / "count"
            count.write_text("0")
            tool.write_text("#!/bin/sh\n"
                            f'n=$(cat "{count}"); n=$((n + 1)); echo $n > "{count}"\n'
                            + script)
            tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
            return subprocess.run(
                ("sh", "-c", util_cases.RANGE_POLL, str(tool), A, deadline, interval),
                capture_output=True, text=True, timeout=40, check=False,
                env={**os.environ, "LC_ALL": "C"})

    def test_reads_until_a_result_and_logs_each_change_once(self):
        result = self.run_poll(
            'if [ "$n" -lt 3 ]; then echo "no Ubertooth found" >&2; exit 1; fi\n'
            'if [ "$n" -lt 5 ]; then echo "no range test result yet"; exit 0; fi\n'
            'printf "request PA level : 0\\nrequest number   : 1\\n"\n')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["read 1: exit 1: no Ubertooth found",
             "read 3: exit 0: no range test result yet",
             "read 5: exit 0: request PA level : 0 request number   : 1"],
            result.stdout.splitlines())

    def test_a_mismatch_ends_the_wait(self):
        result = self.run_poll(
            'echo "error: invalid range test: mismatch on byte 17" >&2; exit 1\n')
        self.assertEqual(0, result.returncode)
        self.assertIn("read 1: exit 1: error: invalid range test: mismatch on byte 17",
                      result.stdout)

    def test_no_result_by_the_deadline_exits_1(self):
        result = self.run_poll('echo "no range test result yet"\n', deadline="1")
        self.assertEqual(1, result.returncode)
        lines = result.stdout.splitlines()
        self.assertEqual("read 1: exit 0: no range test result yet", lines[0])
        self.assertRegex(lines[1], r"^no range test result within 1 s \(\d+ reads\)$")
        self.assertEqual(2, len(lines))
        self.assertEqual("", result.stderr)
        self.assertEqual(
            "no range test result within 1 s ("
            + lines[1].split("(")[1] + "; last read: exit 0: no range test result yet",
            util_cases.range_poll_check(result.stdout))


class RangePollCheckTests(unittest.TestCase):

    def test_a_result_passes(self):
        self.assertEqual("", util_cases.range_poll_check(
            "read 1: exit 0: no range test result yet\n"
            "read 4: exit 0: request PA level : 0 request number   : 1\n"))

    def test_a_mismatch_names_the_field(self):
        cases = {0: "byte 0 (length)", 1: "byte 1 (packet type)",
                 2: "byte 2 (sender serial number byte 0)",
                 17: "byte 17 (sender serial number byte 15)"}
        for byte, expected in cases.items():
            with self.subTest(byte):
                reason = util_cases.range_poll_check(
                    f"read 1: exit 1: error: invalid range test: mismatch on byte {byte}\n")
                self.assertEqual(f"the reply differs from the request at {expected}", reason)

    def test_no_result_names_the_last_read(self):
        reason = util_cases.range_poll_check(
            "read 1: exit 0: no range test result yet\n"
            "read 2: exit 1: no Ubertooth found with serial matching 'x'\n"
            "no range test result within 10 s (40 reads)\n")
        self.assertEqual("no range test result within 10 s (40 reads); last read: "
                         "exit 1: no Ubertooth found with serial matching 'x'", reason)
        self.assertIn("ended without a result", util_cases.range_poll_check(""))


class RangeExecutionTests(unittest.TestCase):

    def run_pair(self, *boards, **options):
        summary, events, report = run_guarded(invocation(**options), PairBench(*boards))
        return summary, events, report

    def test_round_trip_passes_and_restores_both_boards(self):
        air, sender, responder = pair()
        before = (originals(sender), originals(responder))
        summary, events, _report = self.run_pair(sender, responder)
        self.assertEqual([], failed_steps(events))
        self.assertTrue(summary.passed)
        self.assertEqual(before, (originals(sender), originals(responder)))
        self.assertIsNone(responder.mode)
        result = steps(events, step_id="sender-range-result")
        self.assertEqual("passed", result[0].status)
        # The repeater starts before the sender transmits and -S stops it.
        self.assertIn(("ubertooth-util", "-U", B, "-e"), responder.commands)
        self.assertIn(("ubertooth-util", "-U", B, "-S"), responder.commands)

    def test_the_repeater_starts_before_the_range_test(self):
        _air, sender, responder = pair()
        bench = PairBench(sender, responder)
        summary, _events, _report = run_guarded(invocation(), bench)
        self.assertTrue(summary.passed)
        self.assertLess(bench.commands.index(("ubertooth-util", "-U", B, "-e")),
                        bench.commands.index(("ubertooth-util", "-U", A, "-n")))

    def test_both_boards_run_on_the_test_channel(self):
        _air, sender, responder = pair()
        bench = PairBench(sender, responder)
        summary, events, _report = run_guarded(invocation(), bench)
        self.assertTrue(summary.passed, failed_steps(events))
        start = bench.commands.index(("ubertooth-util", "-U", B, "-e"))
        for serial in (A, B):
            self.assertLess(bench.commands.index(("ubertooth-util", "-U", serial, "-c2410")),
                            start)
        (during,) = steps(events, step_id="responder-channel-while-repeating")
        self.assertEqual("passed", during.status)

    def test_an_idle_reset_that_keeps_the_channel_fails(self):
        # The channel checks after the test are real: 2410 left there fails.
        air = Air()
        sender = RangeBoard(A, air, idle_reset_keeps_channel=True)
        responder = RangeBoard(B, air, idle_reset_keeps_channel=True)
        summary, events, _report = self.run_pair(sender, responder)
        self.assertFalse(summary.passed)
        failed = failed_steps(events)
        self.assertEqual("sender-channel-after-test", failed[0])
        (step,) = steps(events, step_id="sender-channel-after-test")
        self.assertIn("observed 2410", step.details["reason"])

    def test_repeater_that_stops_answering_usb_fails_before_the_range_test(self):
        # Fork #26 before 0684c11: the responder answers nothing once -e
        # started.
        # The first check runs before -n, so the sender never transmits.
        _air, sender, responder = pair(repeater_answers_usb=False)
        summary, events, _report = self.run_pair(sender, responder)
        self.assertFalse(summary.passed)
        failed = failed_steps(events)
        self.assertEqual("responder-repeating", failed[0])
        self.assertIn("responder-cleanup-stop", failed)
        self.assertNotIn(("ubertooth-util", "-U", A, "-n"), sender.commands)

    def test_a_wedged_sender_fails_at_the_wait_after_the_repeater_answered(self):
        _air, sender, responder = pair(Air(result=None))
        sender.sender_answers_usb = False
        summary, events, _report = self.run_pair(sender, responder)
        self.assertFalse(summary.passed)
        (before,) = steps(events, step_id="responder-repeating")
        self.assertEqual("passed", before.status)
        self.assertEqual("sender-wait-for-result", failed_steps(events)[0])
        (wait,) = steps(events, step_id="sender-wait-for-result")
        self.assertIn("last read: exit 1: no Ubertooth found", wait.details["reason"])

    def test_no_reply_fails_at_the_wait_and_still_cleans_up(self):
        _air, sender, responder = pair(Air(result=None))
        summary, events, _report = self.run_pair(sender, responder)
        self.assertFalse(summary.passed)
        self.assertEqual(["sender-wait-for-result"], failed_steps(events))
        (wait,) = steps(events, step_id="sender-wait-for-result")
        self.assertIn("no range test result within 10 s (40 reads); last read: "
                      "exit 0: no range test result yet", wait.details["reason"])
        self.assertIsNone(responder.mode)
        self.assertIsNone(sender.mode)

    def test_a_mismatch_fails_naming_the_field(self):
        _air, sender, responder = pair(Air(mismatch=17))
        summary, events, _report = self.run_pair(sender, responder)
        self.assertFalse(summary.passed)
        self.assertEqual(["sender-wait-for-result"], failed_steps(events))
        (wait,) = steps(events, step_id="sender-wait-for-result")
        self.assertIn("differs from the request at byte 17 (sender serial number byte 15)",
                      wait.details["reason"])

    def test_level_above_the_plan_maximum_fails(self):
        _air, sender, responder = pair(Air(result=(0, 0, 4, 2)))
        summary, events, _report = self.run_pair(sender, responder, maximum_pa=3)
        self.assertFalse(summary.passed)
        (result,) = steps(events, step_id="sender-range-result")
        self.assertIn("reply got through only at PA level 4", result.details["reason"])

    def test_old_result_is_cleared_before_the_test(self):
        # A result left from an earlier run must not pass for this one: the
        # reset clears it, so a sender with no reply fails.
        _air, sender, responder = pair(Air(result=None))
        sender.result = (0, 0, 0, 0)
        summary, events, _report = self.run_pair(sender, responder)
        self.assertFalse(summary.passed)
        self.assertEqual(["sender-wait-for-result"], failed_steps(events))

    def test_other_boards_are_guarded(self):
        air, sender, responder = pair()
        guard = RangeBoard(C, air)
        summary, events, _report = self.run_pair(sender, responder, guard, guards=(C,))
        self.assertTrue(summary.passed, failed_steps(events))
        self.assertTrue(guard_checks(events))
        self.assertTrue(all(e.status == "passed" for e in guard_checks(events)))
        # Guards are only read.
        self.assertFalse([c for c in guard.commands if "-S" in c or "-r" in c])


class RangePlanningTests(unittest.TestCase):

    def compile(self, plan_text, serials):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hardware.yaml").write_text(hardware(serials))
            (root / "plan.yaml").write_text(plan_text)
            registry = FileResourceRegistry.load(root / "hardware.yaml")
            return PlanCompiler(builtin_catalog(), registry).compile(
                load_plan(root / "plan.yaml"), "range")

    PLAN = """plan: range
suites:
  range:
    - Two-device range test:
        host: Host_Linux
        sender: default
        responder: {responder}
        allow RF transmission: true
"""

    def test_a_third_board_is_bound_as_a_guard(self):
        (item,) = self.compile(self.PLAN.format(responder="UT_1"), (A, B, C))
        self.assertEqual({"sender": "UT_0", "responder": "UT_1",
                          "guard Ubertooth 1": "UT_2"}, dict(item.resources))

    def test_the_same_board_in_both_roles_is_refused(self):
        with self.assertRaisesRegex(PlanningError, "distinct"):
            self.compile(self.PLAN.format(responder="UT_0"), (A, B))

    def test_the_committed_range_plan_compiles_on_the_example_registry(self):
        project = Path(__file__).resolve().parents[1]
        registry = FileResourceRegistry.load(project / "hardware.example.yaml")
        plan = load_plan(project / "testplans" / "range.yaml")
        (item,) = PlanCompiler(builtin_catalog(), registry).compile(plan, "range")
        self.assertEqual("Host_Linux", item.host)
        # The example bench's third board is the attenuated one, which this
        # suite does not use, so it is bound as a guard and read only.
        self.assertEqual(
            {"sender": "UT_Linux0", "responder": "UT_Linux1",
             "guard Ubertooth 1": "UT_Linux2"},
            dict(item.resources),
        )

    def test_the_weak_link_range_plan_takes_the_attenuated_responder(self):
        project = Path(__file__).resolve().parents[1]
        registry = FileResourceRegistry.load(project / "hardware.example.yaml")
        plan = load_plan(project / "testplans" / "range.yaml")

        (item,) = PlanCompiler(builtin_catalog(), registry).compile(
            plan, "range-weak-link"
        )

        # The attenuated board answers, an unattenuated one sends, and the
        # third is read as a guard: the two roles must not collide.
        self.assertEqual(
            {"sender": "UT_Linux0", "responder": "UT_Linux2",
             "guard Ubertooth 1": "UT_Linux1"},
            dict(item.resources),
        )
        self.assertEqual(
            {"antenna": True, "attenuation_db": 20.0, "distance_m": 1.0},
            dict(item.resource_placements["responder"]),
        )
        self.assertEqual(
            {"min_attenuation_db": 10.0, "max_attenuation_db": 20.0},
            dict(item.placement_requirements["responder"]),
        )
        self.assertEqual(
            {"antenna": True, "max_attenuation_db": 0.0},
            dict(item.placement_requirements["sender"]),
        )
        self.assertEqual(2, item.arguments["maximum PA level"])

    def test_the_weak_link_suite_needs_a_board_placed_for_it(self):
        # The suite names one host, so a bench with no attenuated board
        # fails planning rather than being recorded not applicable.
        project = Path(__file__).resolve().parents[1]
        text = (project / "hardware.example.yaml").read_text()
        plain = re.sub(
            r"\n    rf:\n      antenna: true\n      attenuation_db: 20\n"
            r"      distance_m: 1",
            "",
            text,
        )
        self.assertNotIn("attenuation_db", plain)
        plan = load_plan(project / "testplans" / "range.yaml")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.yaml"
            path.write_text(plain)
            compiler = PlanCompiler(
                builtin_catalog(), FileResourceRegistry.load(path)
            )

            with self.assertRaisesRegex(
                PlanningError,
                "no Ubertooth on this host has attenuation of at least 10 dB "
                "and attenuation of at most 20 dB for the responder",
            ):
                compiler.compile(plan, "range-weak-link")

    def test_the_weak_link_suite_keeps_the_roles_apart_on_an_attenuated_default(self):
        # Two `default` selectors would both resolve to the attenuated board
        # and planning would fail; the sender's own requirement prevents it.
        project = Path(__file__).resolve().parents[1]
        text = (project / "hardware.example.yaml").read_text()
        shifted = text.replace("Ubertooth: UT_Linux0", "Ubertooth: UT_Linux2")
        plan = load_plan(project / "testplans" / "range.yaml")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.yaml"
            path.write_text(shifted)
            (item,) = PlanCompiler(
                builtin_catalog(), FileResourceRegistry.load(path)
            ).compile(plan, "range-weak-link")

            self.assertEqual("UT_Linux2", item.resources["responder"])
            self.assertEqual("UT_Linux0", item.resources["sender"])


if __name__ == "__main__":
    unittest.main()
