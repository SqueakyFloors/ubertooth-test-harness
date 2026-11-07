"""A cleanup comparison against a reading only some runs of a case take.

A cleanup step may have to compare two answers to one question, read either
side of something that must not have changed it, where the first reading can
only be taken after a mutating step: the case has to put the board in that
state before there is anything to read. A case that stopped, or was canceled,
before that step therefore has one reading and not two.

``Observation.compare_with`` is what such a step says instead of an expected
value: compare with the recorded reading where the case took it, and compare
with nothing where it did not (``stateful.NothingToCompare``). The step runs
either way, so the restoration it performs is still checked by its own
patterns, and the cleanup is still verified -- neither of the other two
absences would do, because each of them stops the test
(``sequencer.NamedTestSequencer._run_state_case``). Nothing is invented: a
comparison is made against a reading the board gave, or not at all.

The synthetic case below is the shape that needs it (harness #366, from the
`Classic btbr console` review in #359): write an alternate image, read
something the write must not have touched, and in cleanup write the first
image back and read it again.
"""

import io
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.catalog import TestCatalog, TestDefinition
from ubertooth_harness.events import RunEvent, TerminalEventSink
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import (
    Observation,
    StateCase,
    StateCaseError,
    Step,
    require,
)

from tests.test_sequencer import CollectSink, run_contract


SERIAL = "abc123"
IDENTITIES = {"Ubertooth": {"serial": SERIAL}}
BOOTLOADER = "2.00 git-test, CRC32:0x11112222"
# What the cleanup reads back and compares with the reading taken under the
# alternate image, and the keys the two readings are recorded under.
BOOTLOADER_LINE = r"(?m)^Bootloader: (?P<value>.+)$"
UNDER_ALTERNATE = "bootloader under the alternate image"
AFTER_RESTORE = "bootloader after the restore"


class FakeBoard:
    """The three commands the synthetic case sends, and the host version.

    ``alternate_write_fails`` makes the alternate-image write exit 1, which
    is the case that stops before the baseline is read; the restore still
    works, as it would on a board a failed write left alone. ``bootloader_after`` is the fault
    the comparison is for: a write that reached the bootloader region, so the
    two readings differ. ``cancel_on`` cancels the run's token from inside a
    command, as an operator stop does.
    """

    def __init__(self, *, alternate_write_fails=False, bootloader_after=None,
                 quiet_reads=(), cancel_on=None):
        self.alternate_write_fails = alternate_write_fails
        self.bootloader_after = bootloader_after
        # Ordinals of the bootloader reads that report nothing, counting
        # from 1: how a case gets past a write and still records no reading.
        self.quiet_reads = frozenset(quiet_reads)
        self.cancel_on = cancel_on
        self.bootloader = BOOTLOADER
        self.image = "application"
        self.reads = 0
        self.canceled = False
        self.answered_version = False

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if command == ("ubertooth-util", "-V") and not self.answered_version:
            # The mandatory host-version preflight, before any case runs.
            self.answered_version = True
            return self._result(command, 0, "Host tools version: libubertooth "
                                            "1.1 (git-test), libbtbb 1.0 "
                                            "(git-btbb)\n", on_output)
        if (self.cancel_on is not None and not self.canceled
                and self.cancel_on(command)):
            # Once, as an operator stop is: the cleanup's own token is a
            # fresh one, and its commands run.
            self.canceled = True
            cancellation.cancel("operator stop")
            return CommandResult(command, 130, "", "canceled\n", 0.0, False, True)
        if command[1] == "-d":
            if self.alternate_write_fails and command[2] == "alternate":
                return self._result(command, 1, "", on_output,
                                    stderr="error: write failed\n")
            self.image = command[2]
            if self.bootloader_after is not None and self.image == "application":
                # A write that reached the bootloader region, which is what
                # reading it either side of the images is for.
                self.bootloader = self.bootloader_after
            return self._result(
                command, 0, f"Wrote {self.image}\n", on_output)
        assert command[1] == "-B", command
        self.reads += 1
        if self.reads in self.quiet_reads:
            return self._result(command, 0, "nothing to report\n", on_output)
        return self._result(command, 0, f"Bootloader: {self.bootloader}\n",
                            on_output)

    @staticmethod
    def _result(command, returncode, stdout, on_output, stderr=""):
        if on_output and stdout:
            on_output("stdout", stdout)
        if on_output and stderr:
            on_output("stderr", stderr)
        return CommandResult(command, returncode, stdout, stderr, 0.01, False)


def _write(step_id, image):
    return Step(step_id, f"Write the {image} image",
                ("ubertooth-dfu", "-d", image, "-r"), mutates=True,
                output_patterns=(rf"(?m)^Wrote {image}$",))


def _read(step_id, name, key, **observation):
    return Step(step_id, name, ("ubertooth-util", "-B"),
                observe=Observation(key, BOOTLOADER_LINE, **observation))


def alternate_image_case(case_id="alternate-image"):
    """Write an alternate image, read the bootloader, write the first back.

    The cleanup's second reading is compared with the one taken under the
    alternate image, which only a case that got that far recorded.
    """
    return StateCase(
        case_id,
        "An alternate image, and the bootloader either side of it",
        (_write("write-alternate", "alternate"),
         _read("read-baseline", "Read the bootloader under the alternate "
                                "image", UNDER_ALTERNATE)),
        (_write("restore-application", "application"),
         _read("verify-bootloader",
               "The bootloader is the one the alternate image reported",
               AFTER_RESTORE, compare_with=UNDER_ALTERNATE)),
    )


def run_case(*cases, board, cancellation=None, continue_on_case_failure=False):
    """Run ``cases`` as one catalog test, and return (summary, events, dir)."""
    catalog = TestCatalog([
        TestDefinition(
            "Custom",
            "custom",
            MappingProxyType({}),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=lambda *_args: cases,
            continue_on_case_failure=continue_on_case_failure,
        )
    ])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = TestInvocation(
            "001-Custom", "Custom", "Host_Linux", {},
            {"Ubertooth": "UT_Linux0"}, IDENTITIES,
        )
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(
            board, catalog=catalog, event_sink=sink
        ).run(
            (invocation,), root, run_id="named-run",
            manifest=manifest, preflight_checks=checks,
            cancellation=cancellation,
        )
        return summary, sink.events, (root / "report.md").read_text()


def steps(events, **match):
    return [
        event for event in events
        if event.event_type == "step_finished"
        and all(event.details.get(key) == value for key, value in match.items())
    ]


class Recorder:
    """The publisher one step needs, for a step run on its own."""

    def __init__(self, emitted):
        self.emitted = emitted

    def emit(self, event_type, status, details):
        self.emitted.append((status, dict(details)))


def cases(events):
    return [
        event for event in events
        if event.event_type == "check_finished"
        and event.details.get("phase") == "test"
    ]


def one_step(events, step_id, case_id=None):
    match = {"step_id": step_id}
    if case_id is not None:
        match["case_id"] = case_id
    found = steps(events, **match)
    assert len(found) == 1, (step_id, [e.details for e in found])
    return found[0]


class ComparisonDefinitionTests(unittest.TestCase):
    """What a case may ask to compare with, judged where it is built."""

    def test_expected_and_compare_with_are_two_ways_to_say_one_thing(self):
        with self.assertRaisesRegex(StateCaseError, "give one"):
            Observation(AFTER_RESTORE, BOOTLOADER_LINE, "2.00",
                        compare_with=UNDER_ALTERNATE)

    def test_an_observation_cannot_compare_with_itself(self):
        with self.assertRaisesRegex(StateCaseError, "compare with itself"):
            Observation(AFTER_RESTORE, BOOTLOADER_LINE,
                        compare_with=AFTER_RESTORE)

    def test_a_comparison_needs_a_reading_some_step_records(self):
        # A comparison with a reading this case never takes would be left
        # out of every run of it, and nothing would say so: leaving it out
        # is the ordinary outcome of a case that stopped early.
        with self.assertRaisesRegex(
            StateCaseError, "compares with 'bootloader nobody reads', which "
                            "no step of this case records"
        ):
            StateCase(
                "no-baseline", "No baseline",
                (_write("write-alternate", "alternate"),),
                (_read("verify-bootloader", "Read it back", AFTER_RESTORE,
                       compare_with="bootloader nobody reads"),),
            )

    def test_a_comparison_cannot_name_a_reading_taken_later(self):
        with self.assertRaisesRegex(
            StateCaseError, "which a later step records, not an earlier one"
        ):
            StateCase(
                "late-baseline", "Late baseline",
                (_write("write-alternate", "alternate"),
                 _read("verify-bootloader", "Read it back", AFTER_RESTORE,
                       compare_with=UNDER_ALTERNATE)),
                (_read("read-baseline", "Read the baseline", UNDER_ALTERNATE),),
            )

    def test_the_case_this_is_for_is_accepted(self):
        case = alternate_image_case()

        self.assertEqual(
            UNDER_ALTERNATE, case.cleanup[-1].observe.compare_with)

    def test_a_reading_that_is_there_is_compared(self):
        step = _read("verify-bootloader", "Read it back", AFTER_RESTORE,
                     compare_with=UNDER_ALTERNATE)
        result = CommandResult(("ubertooth-util", "-B"), 0,
                               f"Bootloader: {BOOTLOADER}\n", "", 0.01, False)

        same = step.evaluate(result, {UNDER_ALTERNATE: BOOTLOADER})
        differs = step.evaluate(result, {UNDER_ALTERNATE: "1.00 git-old"})

        self.assertEqual("", same.failure)
        self.assertEqual(BOOTLOADER, same.expected)
        self.assertEqual(BOOTLOADER, same.observed)
        self.assertEqual("", same.uncompared)
        self.assertIn(f"expected {AFTER_RESTORE}=1.00 git-old",
                      differs.failure)

    def test_a_reading_that_is_absent_is_named_and_not_compared(self):
        step = _read("verify-bootloader", "Read it back", AFTER_RESTORE,
                     compare_with=UNDER_ALTERNATE)
        result = CommandResult(("ubertooth-util", "-B"), 0,
                               f"Bootloader: {BOOTLOADER}\n", "", 0.01, False)

        outcome = step.evaluate(result, {})

        self.assertEqual("", outcome.failure)
        self.assertEqual("", outcome.skipped)
        self.assertIsNone(outcome.expected)
        self.assertEqual(BOOTLOADER, outcome.observed)
        self.assertEqual(UNDER_ALTERNATE, outcome.uncompared)

    def test_nothing_to_compare_with_excuses_nothing_else(self):
        step = Step("verify-bootloader", "Read it back",
                    ("ubertooth-util", "-B"),
                    output_patterns=(r"(?m)^Bootloader: ",),
                    observe=Observation(AFTER_RESTORE, BOOTLOADER_LINE,
                                        compare_with=UNDER_ALTERNATE))
        quiet = CommandResult(("ubertooth-util", "-B"), 0, "", "", 0.01, False)
        failed = CommandResult(("ubertooth-util", "-B"), 1,
                               f"Bootloader: {BOOTLOADER}\n", "", 0.01, False)

        no_line = step.evaluate(quiet, {})
        exited = step.evaluate(failed, {})

        self.assertIn("was not found", no_line.failure)
        self.assertEqual(UNDER_ALTERNATE, no_line.uncompared)
        self.assertIn("command exited 1", exited.failure)
        self.assertEqual(UNDER_ALTERNATE, exited.uncompared)

    def test_a_missing_expected_value_still_ends_the_step(self):
        """``compare_with`` changes nothing for a value that is always there.

        A step whose expected value needs something it must have been given
        still fails on its absence (``MissingContext``), so a required value
        cannot be lost quietly.
        """
        step = _read("verify-bootloader", "Read it back", AFTER_RESTORE,
                     expected=lambda context: require(context, "the run's "
                                                               "bootloader"))
        result = CommandResult(("ubertooth-util", "-B"), 0,
                               f"Bootloader: {BOOTLOADER}\n", "", 0.01, False)

        outcome = step.evaluate(result, {})

        self.assertEqual(
            "Read it back: missing recorded the run's bootloader",
            outcome.failure)
        self.assertEqual("", outcome.uncompared)


class CleanupComparisonRunTests(unittest.TestCase):
    """What a whole run makes of the two readings, on each path to cleanup."""

    def test_a_case_that_took_both_readings_compares_them(self):
        board = FakeBoard()

        summary, events, report = run_case(
            alternate_image_case(), board=board)

        self.assertTrue(summary.passed, summary.failure_reason)
        verify = one_step(events, "verify-bootloader")
        self.assertEqual("passed", verify.status)
        self.assertEqual(BOOTLOADER, verify.details["expected"])
        self.assertEqual(BOOTLOADER, verify.details["observed"])
        self.assertNotIn("uncompared", verify.details)
        self.assertEqual("passed", cases(events)[0].details["cleanup_status"])
        self.assertEqual("application", board.image)
        self.assertNotIn("so it was not compared", report)

    def test_two_readings_that_differ_fail_the_cleanup(self):
        # The fault the comparison is for: a write reached the bootloader
        # region, so the board reports another one once the image is back.
        board = FakeBoard(bootloader_after="1.00 git-other")

        summary, events, _report = run_case(
            alternate_image_case("first"), alternate_image_case("second"),
            board=board, continue_on_case_failure=True)

        self.assertFalse(summary.passed)
        self.assertIn("cleanup failed", summary.failure_reason)
        self.assertIn(f"expected {AFTER_RESTORE}={BOOTLOADER}, "
                      "observed 1.00 git-other", summary.failure_reason)
        self.assertEqual("failed", cases(events)[0].details["cleanup_status"])
        # An unverified cleanup stops the test, however many cases follow.
        self.assertEqual(1, len(cases(events)))

    def test_a_write_that_failed_leaves_the_comparison_out_and_restores(self):
        board = FakeBoard(alternate_write_fails=True)

        summary, events, report = run_case(
            alternate_image_case("first"), alternate_image_case("second"),
            board=board, continue_on_case_failure=True)

        self.assertFalse(summary.passed)
        self.assertIn("Write the alternate image: command exited 1",
                      summary.failure_reason)
        self.assertNotIn("cleanup failed", summary.failure_reason)
        # The cleanup read ran, recorded what the board answered, and said
        # which reading it had nothing to compare with.
        verify = one_step(events, "verify-bootloader", case_id="001-Custom-first")
        self.assertEqual("passed", verify.status)
        self.assertIsNone(verify.details["expected"])
        self.assertEqual(BOOTLOADER, verify.details["observed"])
        self.assertEqual(UNDER_ALTERNATE, verify.details["uncompared"])
        self.assertIn(
            f"(recorded {BOOTLOADER}) (nothing recorded for {UNDER_ALTERNATE}, "
            "so it was not compared)", report)
        # Verified restored, so the run goes on to the next case rather than
        # reporting a board left on the alternate image.
        first = cases(events)[0]
        self.assertEqual("failed", first.status)
        self.assertEqual("passed", first.details["cleanup_status"])
        self.assertEqual(2, len(cases(events)))

    def test_a_baseline_read_that_reported_nothing_leaves_it_out_too(self):
        # The write went through, so the case changed the board, but the
        # reading it was setting up for never came. The cleanup's own read
        # answers, so the cleanup is judged on that and nothing else.
        board = FakeBoard(quiet_reads=(1,))

        summary, events, _report = run_case(
            alternate_image_case(), board=board)

        self.assertFalse(summary.passed)
        self.assertIn(f"{UNDER_ALTERNATE} was not reported",
                      summary.failure_reason)
        self.assertNotIn("cleanup failed", summary.failure_reason)
        verify = one_step(events, "verify-bootloader")
        self.assertEqual("passed", verify.status)
        self.assertEqual(BOOTLOADER, verify.details["observed"])
        self.assertEqual(UNDER_ALTERNATE, verify.details["uncompared"])
        case = cases(events)[0]
        self.assertEqual("failed", case.status)
        self.assertEqual("passed", case.details["cleanup_status"])
        self.assertEqual("application", board.image)

    def test_a_cancellation_before_the_baseline_still_verifies_the_cleanup(self):
        token = CancellationToken()
        board = FakeBoard(cancel_on=lambda command: command[1] == "-d")

        summary, events, _report = run_case(
            alternate_image_case("first"), alternate_image_case("second"),
            board=board, cancellation=token, continue_on_case_failure=True)

        self.assertFalse(summary.passed)
        case = cases(events)[0]
        self.assertEqual("canceled", case.status)
        # Cleanup runs on its own token, so the restore and its read still
        # happened; the comparison had nothing to compare with.
        self.assertEqual("passed", case.details["cleanup_status"])
        verify = one_step(events, "verify-bootloader")
        self.assertEqual("passed", verify.status)
        self.assertEqual(UNDER_ALTERNATE, verify.details["uncompared"])
        self.assertEqual("application", board.image)
        # A cancellation ends the test whatever the cleanup found, as it
        # always has: the record says the board was put back, not that the
        # run may carry on.
        self.assertEqual(1, len(cases(events)))

    def test_a_step_no_case_owns_fails_on_the_absence_instead(self):
        """The tolerance is for a case's own steps, which StateCase checked.

        The guard reads are built outside any case (transitions.py), so no
        construction-time check refuses a comparison with a reading nothing
        records. There the absence fails the step, before its command runs,
        rather than being left out. The tolerated stages are what the run
        tests above cover.
        """
        emitted = []
        step = _read("read-guard", "Read a guard board", AFTER_RESTORE,
                     compare_with=UNDER_ALTERNATE)
        sequencer = NamedTestSequencer(FakeBoard())

        with tempfile.TemporaryDirectory() as directory:
            failure, ran, canceled, skipped = sequencer._run_state_step(
                Recorder(emitted), CancellationToken(), Path(directory),
                step, "guard", {}, {"phase": "test"}, "001-Custom-case",
            )

        self.assertEqual(
            f"Read a guard board: missing recorded {UNDER_ALTERNATE}", failure)
        self.assertFalse(ran)
        self.assertFalse(canceled)
        self.assertEqual("", skipped)
        self.assertEqual([("failed", failure, None)],
                         [(status, details["reason"], details["argv"])
                          for status, details in emitted])

    def test_the_live_console_line_says_a_comparison_was_left_out(self):
        # What someone watching the run sees, which is not report.md.
        stream = io.StringIO()
        sink = TerminalEventSink(stream)
        details = {"stage": "cleanup", "step_name": "Read it back",
                   "expected": None, "observed": BOOTLOADER,
                   "uncompared": UNDER_ALTERNATE, "reason": ""}

        sink.emit(RunEvent(1, "run", 1, "2026-10-22T01:00:00.000",
                           "step_finished", "passed", details))

        self.assertEqual(
            f"[cleanup passed] Read it back (recorded {BOOTLOADER}) "
            f"(nothing recorded for {UNDER_ALTERNATE}, so it was not "
            "compared)\n", stream.getvalue())
