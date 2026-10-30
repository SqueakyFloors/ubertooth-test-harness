"""The firmware watchdog monitor: reading `-w`, comparing, and reporting."""

import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from ubertooth_harness.catalog import (
    HOSTS,
    UBERTOOTH,
    TestCatalog,
    TestDefinition,
    builtin_catalog,
)
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.report import render_run
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness import watchdog
from ubertooth_harness.watchdog import (
    AFTER_TEST,
    BASELINE,
    MonitoredBoard,
    WatchdogMonitor,
    compare,
    find_duplicates,
    fork_issue,
    notes_for,
    parse_report,
    search_argv,
    warnings_for,
)

from tests import watchdog_samples as samples
from tests.test_catalog_resources import HARDWARE
from tests.test_sequencer import CollectSink, run_contract


SERIAL = "aaaa0000aaaa0000aaaa0000aaaa0000"
BOARD = MonitoredBoard("UT_Linux0", SERIAL)


class ParseTests(unittest.TestCase):

    def test_every_line_of_a_full_report_read_from_a_board(self):
        record = parse_report(samples.FULL)

        self.assertEqual("watchdog", record.boot_cause)
        self.assertEqual(("watchdog",), record.causes)
        self.assertTrue(record.armed)
        self.assertEqual(3000, record.timeout_ms)
        self.assertEqual((3, 56), (record.resets, record.boots))
        self.assertEqual("BLE follow", record.last_reset_mode)
        self.assertEqual("running a control request", record.last_reset_wait)
        self.assertEqual("idle", record.mode)
        self.assertEqual("not in a wait", record.wait)
        self.assertEqual("", record.fault)
        self.assertEqual(0, record.transfer_errors)
        self.assertEqual(
            {"fs_lock": 0, "fs_unlock": 0, "oscillator": 0, "state_machine": 0,
             "packet_bytes": 0},
            dict(record.radio_waits),
        )

    def test_older_host_tools_leave_the_last_two_lines_out(self):
        # The same board read with a build from before those lines: the rest
        # is still read, and the two counts are not reported.
        record = parse_report(samples.OLDER_TOOLS)

        self.assertEqual((3, 55), (record.resets, record.boots))
        self.assertEqual("in a wait this version does not know", record.last_reset_wait)
        self.assertIsNone(record.transfer_errors)
        self.assertIsNone(record.radio_waits)

    def test_a_board_that_has_not_reset_itself_names_no_mode(self):
        record = parse_report(samples.report())

        self.assertEqual("power-on", record.boot_cause)
        self.assertEqual((0, 1), (record.resets, record.boots))
        self.assertFalse(record.last_reset_reported)
        self.assertEqual("", record.last_reset_wait)

    def test_the_other_line_forms(self):
        unarmed = parse_report(samples.report(armed=False, cause="not recorded"))
        self.assertFalse(unarmed.armed)
        self.assertIsNone(unarmed.timeout_ms)
        self.assertEqual((), unarmed.causes)

        several = parse_report(samples.report(cause="power-on+watchdog"))
        self.assertEqual(("power-on", "watchdog"), several.causes)

        # The btbr image runs its own state machine, so it records no mode.
        unnamed = parse_report(samples.report(
            mode=None, wait="not in a wait", resets=1,
            last=f"in {watchdog._UNNAMED_MODE}, in the hard fault handler",
        ))
        self.assertIsNone(unnamed.mode)
        self.assertIsNone(unnamed.last_reset_mode)
        self.assertEqual("in the hard fault handler", unnamed.last_reset_wait)
        self.assertEqual(
            "in a mode the board does not report, in the hard fault handler",
            unnamed.last_reset_where,
        )

        faulted = parse_report(samples.report(
            fault="status 0x00000082, the access was at 0x4000c008"))
        self.assertEqual("0x00000082", faulted.fault_status)
        self.assertEqual("0x4000c008", faulted.fault_address)
        self.assertEqual(
            "status 0x00000082, the access was at 0x4000c008", faulted.fault)

        no_address = parse_report(samples.report(
            fault="status 0x40000000, no address recorded"))
        self.assertIsNone(no_address.fault_address)
        self.assertEqual("status 0x40000000, no address recorded", no_address.fault)

    def test_output_with_no_report_is_not_a_record(self):
        self.assertIsNone(parse_report(""))
        self.assertIsNone(parse_report(samples.FIRMWARE_TOO_OLD))
        self.assertIsNone(parse_report(samples.BOARD_GONE.format(serial=SERIAL)))

    def test_a_board_the_read_could_not_reach_at_all(self):
        for text in (
            samples.BOARD_GONE.format(serial=SERIAL),
            samples.BOARD_LEFT,
            "error: no Ubertooth is attached\n",
            # On the bus and refusing to be opened, which is how a board looks
            # for a moment after the watchdog has reset it.
            samples.BOARD_BUSY.format(serial=SERIAL),
            samples.CLAIM_FAILED.format(serial=SERIAL),
            samples.READ_TIMED_OUT,
        ):
            self.assertTrue(watchdog.board_unreachable(text), text)
        # Firmware that answered and has no such request is not that.
        self.assertFalse(watchdog.board_unreachable(samples.FIRMWARE_TOO_OLD))
        self.assertFalse(watchdog.board_unreachable(samples.FULL))
        # The phrase must be a line of its own, not quoted anywhere.
        self.assertFalse(watchdog.board_unreachable(
            "Now in idle mode, the board is no longer attached was the reason\n"))


class CompareTests(unittest.TestCase):

    def test_a_self_reset_since_the_baseline_is_counted_and_warned_about(self):
        baseline = parse_report(samples.report(resets=0, boots=1))
        current = parse_report(samples.report(
            resets=2, boots=3, cause="watchdog",
            last="in BLE promiscuous mode, waiting for a BLE packet or the next hop",
        ))

        change = compare(baseline, current)

        self.assertEqual(2, change.new_resets)
        self.assertTrue(change.reset_itself)
        self.assertFalse(change.restarted)
        (warning,) = warnings_for("UT (serial s)", current, change, "during the test")
        self.assertIn("UT (serial s) reset itself 2 times during the test", warning)
        self.assertIn(
            "in BLE promiscuous mode, waiting for a BLE packet or the next hop",
            warning,
        )
        self.assertIn("worth an issue on the fork", warning)

    def test_a_requested_reset_is_a_note_not_a_warning(self):
        # `-r` and entering the bootloader are watchdog resets to the chip, so
        # the boot cause changes while the unrequested count does not.
        baseline = parse_report(samples.report(resets=0, boots=1, cause="power-on"))
        current = parse_report(samples.report(resets=0, boots=2, cause="watchdog"))

        change = compare(baseline, current)

        self.assertEqual(0, change.new_resets)
        self.assertTrue(change.gained_watchdog_cause)
        self.assertEqual([], warnings_for("UT", current, change, "during the test"))
        (note,) = notes_for("UT", current, change, "during the test")
        self.assertIn("a reset the run asked for", note)

    def test_a_power_change_starts_the_counters_again(self):
        # Cutting power to the board (its hub port, or a re-plug) zeroes both
        # counters, which reads as a count that went backwards.
        baseline = parse_report(samples.report(resets=4, boots=9, cause="watchdog"))
        current = parse_report(samples.report(resets=0, boots=1, cause="power-on"))

        change = compare(baseline, current)

        self.assertTrue(change.restarted)
        self.assertEqual(0, change.new_resets)
        self.assertEqual([], warnings_for("UT", current, change, "during the test"))
        (note,) = notes_for("UT", current, change, "during the test")
        self.assertIn("counters started again", note)
        self.assertIn("powered on again", note)

    def test_a_power_cycle_that_leaves_the_counts_alone_is_still_a_restart(self):
        # A board at 0 resets and 1 boot reads the same after a power cycle:
        # the boot cause gaining power-on is the only sign.
        baseline = parse_report(samples.report(resets=0, boots=1, cause="watchdog"))
        current = parse_report(samples.report(resets=0, boots=1, cause="power-on"))

        change = compare(baseline, current)

        self.assertTrue(change.restarted)
        self.assertEqual([], warnings_for("UT", current, change, "during the test"))

    def test_firmware_flashed_over_the_record_reads_as_a_restart_too(self):
        baseline = parse_report(samples.report(resets=4, boots=9, cause="watchdog"))
        current = parse_report(samples.report(resets=0, boots=1, cause="watchdog"))

        change = compare(baseline, current)

        self.assertTrue(change.restarted)
        (note,) = notes_for("UT", current, change, "during the test")
        self.assertIn("lost power, or it was flashed with firmware", note)

    def test_a_reset_after_a_power_change_counts_from_the_restart(self):
        baseline = parse_report(samples.report(resets=4, boots=9))
        current = parse_report(samples.report(resets=1, boots=2, cause="watchdog"))

        change = compare(baseline, current)

        self.assertTrue(change.restarted)
        self.assertEqual(1, change.new_resets)
        self.assertTrue(any(
            "reset itself once" in line
            for line in warnings_for("UT", current, change, "during the test")
        ))

    def test_a_counter_already_at_its_limit_cannot_rise(self):
        # Nothing to warn about at a later read: the count is pinned, so the
        # baseline is where it is worth saying (MonitorTests).
        current = parse_report(samples.report(resets=255, boots=300, cause="watchdog"))

        change = compare(parse_report(samples.report(resets=255, boots=299)), current)

        self.assertEqual(0, change.new_resets)
        self.assertEqual([], warnings_for("UT", current, change, "during the test"))

    def test_a_boot_count_at_its_limit_hides_a_reboot_from_the_radio_waits(self):
        # Both counters stop at 255, so a reboot of a board pinned there is
        # invisible. The counts are still differenced, because taking every
        # read for a fresh boot would re-report the whole count each time;
        # the baseline notes the pinned counter instead (MonitorTests).
        baseline = parse_report(samples.report(boots=255, radio_waits=(7, 0, 0, 0, 0)))
        current = parse_report(samples.report(boots=255, radio_waits=(9, 0, 0, 0, 0)))

        change = compare(baseline, current)

        self.assertEqual({"fs_lock": 2}, dict(change.new_radio_waits))

    def test_a_new_processor_fault_warns_and_the_same_one_does_not(self):
        fault = "status 0x00000082, the access was at 0x4000c008"
        clean = parse_report(samples.report())
        faulted = parse_report(samples.report(fault=fault))

        first = compare(clean, faulted)
        self.assertEqual(fault, first.new_fault)
        self.assertTrue(any(
            "recorded a processor fault" in line
            for line in warnings_for("UT", faulted, first, "during the test")
        ))

        again = compare(faulted, faulted)
        self.assertEqual("", again.new_fault)
        self.assertEqual([], warnings_for("UT", faulted, again, "during the test"))

    def test_a_brown_out_is_a_restart_note_not_a_warning(self):
        # The part sets the brown-out flag on an ordinary power-up too, so a
        # deliberate power cycle (a test's isolation, a re-plug) must not warn
        # about a supply dip. Either way the board was powered on again, which
        # started its counters again.
        current = parse_report(samples.report(cause="power-on+brown-out", boots=1))

        change = compare(parse_report(samples.report(boots=4, resets=1)), current)

        self.assertTrue(change.restarted)
        self.assertTrue(change.brown_out)
        self.assertEqual([], warnings_for("UT", current, change, "during the test"))
        (note,) = notes_for("UT", current, change, "during the test")
        self.assertIn("powered on again", note)

    def test_radio_waits_start_again_at_every_boot_not_only_a_power_cycle(self):
        # They live in ordinary RAM, so any reset clears them; the boot count
        # rising is how the read sees that.
        baseline = parse_report(samples.report(boots=1, radio_waits=(7, 0, 0, 0, 0)))
        current = parse_report(samples.report(boots=2, radio_waits=(2, 0, 0, 0, 0)))

        change = compare(baseline, current)

        self.assertFalse(change.restarted)
        self.assertEqual({"fs_lock": 2}, dict(change.new_radio_waits))

    def test_transfer_errors_and_radio_waits_are_notes_only(self):
        baseline = parse_report(samples.report(transfer_errors=0, radio_waits=(0,) * 5))
        current = parse_report(samples.report(
            transfer_errors=3, radio_waits=(2, 0, 0, 0, 1)))

        change = compare(baseline, current)

        self.assertEqual(3, change.new_transfer_errors)
        self.assertEqual({"fs_lock": 2, "packet_bytes": 1}, dict(change.new_radio_waits))
        self.assertEqual([], warnings_for("UT", current, change, "during the test"))
        notes = notes_for("UT", current, change, "during the test")
        self.assertIn("3 capture transfer error(s)", notes[0])
        # The words and order the tool prints them in.
        self.assertIn("synthesizer lock 2, packet bytes 1", notes[1])

    def test_with_no_baseline_everything_counted_is_new(self):
        current = parse_report(samples.report(resets=2, boots=3, cause="watchdog"))

        change = compare(None, current)

        self.assertEqual(2, change.new_resets)
        self.assertFalse(change.restarted)


class ForkIssueTests(unittest.TestCase):

    def issue(self):
        return fork_issue(
            serial=SERIAL,
            resource="UT_Linux0",
            record=parse_report(samples.FULL),
            resets=1,
            build="git-d8e8c54 (commit d8e8c549" + "0" * 32 + ")",
            test_name="Capture known BLE connection",
            run_id="named-run",
            when="2026-10-29T20:33:38",
            report=samples.FULL,
        )

    def test_the_issue_names_the_board_the_mode_and_the_wait(self):
        issue = self.issue()

        self.assertEqual(
            "Firmware watchdog reset in BLE follow mode, running a control request",
            issue.title,
        )
        self.assertIn("reset itself once during a test harness run", issue.body)
        self.assertIn(f"- Board serial: `{SERIAL}` (UT_Linux0)", issue.body)
        self.assertIn("- Firmware and host tools: git-d8e8c54 (commit d8e8c549",
                      issue.body)
        self.assertIn("- Mode: BLE follow", issue.body)
        self.assertIn("- Wait: running a control request", issue.body)
        self.assertIn("- Test: Capture known BLE connection", issue.body)
        self.assertIn("- Harness run: named-run", issue.body)
        self.assertIn("- Time (UTC): 2026-10-29T20:33:38", issue.body)
        # The record it was read from, so the issue stands on its own.
        self.assertIn("Unrequested watchdog resets since power-on: 3", issue.body)
        self.assertEqual("SqueakyFloors/ubertooth", issue.to_dict()["repository"])

    def test_the_duplicate_search_asks_gh_about_the_same_mode_and_wait(self):
        asked = []

        def gh(argv, timeout_s):
            asked.append((tuple(argv), timeout_s))
            return 0, json.dumps([
                {"number": 505, "title": "btle capture dies after a few minutes",
                 "url": "https://github.com/SqueakyFloors/ubertooth/issues/505"},
                {"number": 0, "title": "not an issue", "url": ""},
            ]), ""

        found = find_duplicates(self.issue(), run=gh)

        self.assertEqual(
            'watchdog "BLE follow" "running a control request"', found.terms)
        (argv, timeout_s), = asked
        self.assertEqual(
            tuple(search_argv('watchdog "BLE follow" "running a control request"')),
            argv,
        )
        self.assertEqual(watchdog.SEARCH_TIMEOUT_S, timeout_s)
        self.assertIn("-R", argv)
        self.assertEqual("SqueakyFloors/ubertooth", argv[argv.index("-R") + 1])
        self.assertEqual("--state", argv[argv.index("--state")])
        # Only real issues; an item with no number is not one.
        self.assertEqual((505,), tuple(match["number"] for match in found.matches))
        self.assertEqual("", found.error)

    def test_a_libusb_error_with_no_wording_of_its_own_is_an_unreachable_board(self):
        # libubertooth prints libusb's own description and the code; the
        # refused request carries its code inside "(stalled, libusb -9)" and
        # is not this.
        self.assertTrue(watchdog.board_unreachable(samples.READ_FAILED))
        self.assertTrue(watchdog.board_unreachable(
            "error: Input/Output Error (libusb -1)\n"))
        self.assertFalse(watchdog.board_unreachable(samples.FIRMWARE_TOO_OLD))

    def test_a_search_that_cannot_run_still_offers_the_issue(self):
        def missing(argv, timeout_s):
            raise OSError("No such file or directory: 'gh'")

        found = find_duplicates(self.issue(), run=missing)
        self.assertEqual((), found.matches)
        self.assertIn("could not run gh", found.error)

        def refused(argv, timeout_s):
            return 1, "", "gh: To get started with GitHub CLI, please run: gh auth login\n"

        self.assertIn("gh issue list failed", find_duplicates(self.issue(), run=refused).error)

        def nonsense(argv, timeout_s):
            return 0, "not json", ""

        self.assertIn("no readable list", find_duplicates(self.issue(), run=nonsense).error)


class FakeCommands:
    """Answers `-w` per board from a list of outputs, in order."""

    def __init__(self, outputs):
        self.outputs = {serial: list(texts) for serial, texts in outputs.items()}
        self.argv = []
        self.ids = []

    def __call__(self, *, command_id, argv, timeout_s, owner):
        self.argv.append(tuple(argv))
        self.ids.append(command_id)
        serial = argv[argv.index("-U") + 1]
        texts = self.outputs[serial]
        text = texts.pop(0) if len(texts) > 1 else texts[0]
        # `-w` exits non-zero when either of its two reads failed, so the
        # record alone does not say the command succeeded.
        code = 0 if parse_report(text) and "error: " not in text else 1
        return CommandResult(tuple(argv), code, text, "", 0.01, False)


class Publisher:
    run_id = "named-run"

    def __init__(self):
        self.events = []

    def emit(self, event_type, status, details, **_):
        self.events.append((event_type, status, details))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def invocation(limit=None):
    arguments = {"hosts": "each"}
    if limit is not None:
        arguments["max_watchdog_resets"] = limit
    return TestInvocation(
        "001-Soak", "Soak", "Host_Linux", arguments,
        {"Ubertooth": "UT_Linux0"}, {"Ubertooth": {"serial": SERIAL}},
    )


class MonitorTests(unittest.TestCase):

    def monitor(self, outputs, boards=(BOARD,), **options):
        commands = FakeCommands(outputs)
        publisher = Publisher()
        clock = Clock()
        monitor = WatchdogMonitor(
            commands, publisher, boards, clock=clock, sleep=clock.sleep,
            build="git-test (commit " + "d" * 40 + ")",
            utc_now=lambda: "2026-10-29T20:00:00",
            **{
                "search": lambda issue: watchdog.DuplicateSearch(issue.search_terms),
                **options,
            },
        )
        return monitor, commands, publisher, clock

    def test_the_baseline_reads_every_board_and_warns_about_nothing(self):
        monitor, commands, publisher, _clock = self.monitor(
            {SERIAL: [samples.report(resets=2, boots=5, cause="watchdog")]})

        outcome = monitor.baseline()

        self.assertEqual(("ubertooth-util", "-U", SERIAL, "-w"), commands.argv[0])
        self.assertEqual((), outcome.warnings)
        self.assertEqual("", outcome.failure)
        # Resets from before the run are the board's, not this run's.
        self.assertEqual(0, monitor.total_resets)
        (note,) = outcome.notes
        self.assertIn("had already reset itself 2 time(s)", note)
        (event,) = [e for e in publisher.events if e[0] == "watchdog"]
        self.assertEqual("passed", event[1])
        self.assertEqual(BASELINE, event[2]["stage"])
        self.assertEqual(SERIAL, event[2]["serial"])
        self.assertEqual(2, event[2]["resets"])
        self.assertEqual(0, event[2]["new_resets"])

    def test_a_reset_during_a_test_warns_and_the_test_still_passes(self):
        monitor, _commands, publisher, _clock = self.monitor({SERIAL: [
            samples.report(resets=0, boots=1),
            samples.report(resets=1, boots=2, cause="watchdog",
                           last="in BLE follow mode, waiting for a BLE packet or the next hop"),
        ]})
        monitor.baseline()

        outcome = monitor.after_test(invocation())

        self.assertEqual("", outcome.failure)
        self.assertFalse(outcome.board_lost)
        self.assertEqual(1, outcome.resets)
        (warning,) = outcome.warnings
        self.assertIn("reset itself once during the test", warning)
        after = [e for e in publisher.events if e[0] == "watchdog"][-1]
        self.assertEqual("warning", after[1])
        self.assertEqual(AFTER_TEST, after[2]["stage"])
        self.assertEqual([warning], after[2]["warnings"])
        self.assertEqual("BLE follow", after[2]["last_reset_mode"])

    def test_one_reset_is_reported_once(self):
        monitor, _commands, _publisher, _clock = self.monitor({SERIAL: [
            samples.report(resets=0, boots=1),
            samples.report(resets=1, boots=2, cause="watchdog"),
            samples.report(resets=1, boots=2, cause="watchdog"),
        ]})
        monitor.baseline()

        first = monitor.after_test(invocation())
        second = monitor.after_test(invocation())

        self.assertEqual(1, first.resets)
        self.assertEqual(0, second.resets)
        self.assertEqual((), second.warnings)
        self.assertEqual(1, monitor.total_resets)

    def test_the_soak_limit_is_the_only_reset_that_fails(self):
        outputs = {SERIAL: [
            samples.report(resets=0, boots=1),
            samples.report(resets=3, boots=4, cause="watchdog"),
        ]}
        monitor, _commands, _publisher, _clock = self.monitor(outputs)
        monitor.baseline()

        self.assertEqual("", monitor.after_test(invocation(), 3).failure)

        monitor, _commands, _publisher, _clock = self.monitor(outputs)
        monitor.baseline()
        outcome = monitor.after_test(invocation(), 2)

        self.assertIn("reset themselves 3 times", outcome.failure)
        self.assertIn("max_watchdog_resets (2)", outcome.failure)
        # The boards are still usable, so the run goes on.
        self.assertFalse(outcome.board_lost)

    def test_firmware_without_the_request_reads_as_not_reported(self):
        monitor, _commands, publisher, _clock = self.monitor(
            {SERIAL: [samples.FIRMWARE_TOO_OLD]})

        outcome = monitor.baseline()

        self.assertEqual("", outcome.failure)
        (note,) = outcome.notes
        self.assertIn("watchdog not reported: the board's firmware does not "
                      "support it", note)
        (event,) = [e for e in publisher.events if e[0] == "watchdog"]
        self.assertEqual("passed", event[1])
        self.assertEqual("not reported", event[2]["state"])

    def test_a_board_that_does_not_come_back_fails_the_test(self):
        monitor, commands, publisher, clock = self.monitor(
            {SERIAL: [samples.BOARD_GONE.format(serial=SERIAL)]})

        outcome = monitor.after_test(invocation())

        self.assertTrue(outcome.board_lost)
        self.assertIn("did not answer on USB", outcome.failure)
        self.assertEqual(watchdog.RECOVERY_TRIES, len(commands.argv))
        self.assertEqual(
            (watchdog.RECOVERY_TRIES - 1) * watchdog.RECOVERY_PAUSE_S, clock.now)
        self.assertEqual("failed", publisher.events[-1][1])

    def test_a_board_absent_at_the_baseline_fails_before_any_test(self):
        monitor, _commands, _publisher, _clock = self.monitor(
            {SERIAL: [samples.BOARD_GONE.format(serial=SERIAL)]})

        outcome = monitor.baseline()

        self.assertTrue(outcome.board_lost)
        self.assertIn("did not answer on USB", outcome.failure)

    def test_a_failed_read_of_a_board_that_answered_before_is_not_old_firmware(self):
        # A libusb error with no wording of its own: the board is not refusing
        # the request, and printed no record, so it is retried and then fails.
        for output in (samples.READ_FAILED,):
            monitor, commands, publisher, _clock = self.monitor(
                {SERIAL: [samples.report(), output]})
            monitor.baseline()

            outcome = monitor.after_test(invocation())

            self.assertTrue(outcome.board_lost, output)
            self.assertIn("did not answer on USB", outcome.failure)
            self.assertEqual(
                watchdog.RECOVERY_TRIES + 1, len(commands.argv), output)
            self.assertEqual("failed", publisher.events[-1][1])

    def test_firmware_flashed_without_the_request_reads_as_not_reported(self):
        # A flash test writes the 2020-12-R1 image on purpose, and it has no
        # such request. The board answers every try and refuses the request
        # every time, which is how that is told from a board re-enumerating.
        monitor, commands, publisher, _clock = self.monitor({SERIAL: [
            samples.report(), samples.FIRMWARE_TOO_OLD,
        ]})
        monitor.baseline()

        outcome = monitor.after_test(invocation())

        self.assertEqual("", outcome.failure)
        self.assertFalse(outcome.board_lost)
        self.assertEqual("not reported", publisher.events[-1][2]["state"])
        self.assertEqual(watchdog.RECOVERY_TRIES + 1, len(commands.argv))

        # And it is not waited for again, having shown what its firmware is.
        monitor.at_run_end()
        self.assertEqual(watchdog.RECOVERY_TRIES + 2, len(commands.argv))

    def test_a_record_that_printed_is_the_read_whatever_else_failed(self):
        # The command reads the record and then the radio waits. A board that
        # printed the record answered, so the record counts and what became of
        # the rest of the command is a note on it.
        monitor, commands, publisher, _clock = self.monitor({SERIAL: [
            samples.report(resets=0, boots=1),
            samples.RECORD_THEN_FAILED,
        ]})
        monitor.baseline()

        outcome = monitor.after_test(invocation())

        self.assertEqual("", outcome.failure)
        self.assertFalse(outcome.board_lost)
        # One read: nothing to retry, the record is there.
        self.assertEqual(2, len(commands.argv))
        self.assertEqual(3, outcome.resets)
        self.assertTrue(any("read its watchdog record, but its read did not "
                            "finish" in note for note in outcome.notes), outcome.notes)
        self.assertEqual("read", publisher.events[-1][2]["state"])

    def test_firmware_with_the_record_and_not_the_radio_waits_still_reads(self):
        # A fork commit has the watchdog record and not the radio waits: the
        # record prints, the second read is refused, and the command exits 1.
        output = samples.FULL.replace(
            "Radio waits that gave up since boot: synthesizer lock 0, unlock 0, "
            "oscillator 0, state machine 0, packet bytes 0\n",
            "error: could not read the radio wait timeouts: the board's firmware "
            "does not support it (stalled, libusb -9)\n",
        )
        monitor, commands, publisher, _clock = self.monitor({SERIAL: [output]})

        outcome = monitor.baseline()

        self.assertEqual("", outcome.failure)
        self.assertEqual(1, len(commands.argv))
        read = publisher.events[-1][2]
        self.assertEqual("read", read["state"])
        self.assertEqual(3, read["resets"])
        self.assertIsNone(read["radio_waits"])

    def test_a_board_without_the_request_still_gets_the_window_after_a_reflash(self):
        # A flash test leaves 2020-12-R1 on the board, so it is known to have
        # no such request; the next test reflashes it, and the read that
        # follows must wait for it to come back rather than fail at once.
        monitor, commands, _publisher, _clock = self.monitor({SERIAL: [
            # The baseline tries the whole window before it concludes that
            # the firmware has not got the request.
            *[samples.FIRMWARE_TOO_OLD] * watchdog.RECOVERY_TRIES,
            samples.BOARD_GONE.format(serial=SERIAL),
            samples.report(resets=0, boots=1),
        ]})
        monitor.baseline()
        reads = len(commands.argv)
        self.assertEqual(watchdog.RECOVERY_TRIES, reads)

        outcome = monitor.after_test(invocation())

        self.assertEqual("", outcome.failure)
        self.assertFalse(outcome.board_lost)
        self.assertEqual(reads + 2, len(commands.argv))

    def test_a_board_lost_during_a_long_test_still_fails_after_it(self):
        # A mid-test read reports nothing, so it must not give the board up:
        # the read after the test is what gives it its window and fails.
        monitor, commands, _publisher, clock = self.monitor(
            {SERIAL: [samples.report(), samples.BOARD_GONE.format(serial=SERIAL)]},
            interval_s=10.0,
        )
        monitor.baseline()
        clock.now += 11.0

        during = monitor.poll_if_due(invocation())
        reads = len(commands.argv)
        after = monitor.after_test(invocation())

        self.assertEqual("", during.failure)
        self.assertIn("did not answer on USB", after.failure)
        self.assertTrue(after.board_lost)
        # The window again, not one try: the poll gave the board up for nobody.
        self.assertEqual(reads + watchdog.RECOVERY_TRIES, len(commands.argv))

    def test_a_board_that_refuses_to_be_opened_is_retried_not_called_old(self):
        # The board is on the bus and refusing requests, which is how it looks
        # for a moment after the watchdog has reset it.
        monitor, commands, _publisher, _clock = self.monitor({SERIAL: [
            samples.report(resets=0, boots=1),
            samples.BOARD_BUSY.format(serial=SERIAL),
            samples.CLAIM_FAILED.format(serial=SERIAL),
            samples.report(resets=1, boots=2, cause="watchdog"),
        ]})
        monitor.baseline()

        outcome = monitor.after_test(invocation())

        self.assertEqual("", outcome.failure)
        self.assertEqual(4, len(commands.argv))
        self.assertEqual(1, outcome.resets)

    def test_a_cancelled_run_neither_waits_for_a_lost_board_nor_fails_on_it(self):
        # Stopping the run is what was asked for, and a board may have gone as
        # part of it, so the read is recorded and the cancellation stands.
        monitor, commands, publisher, clock = self.monitor(
            {SERIAL: [samples.BOARD_GONE.format(serial=SERIAL)]},
            cancelled=lambda: True,
        )

        outcome = monitor.after_test(invocation())

        self.assertEqual("", outcome.failure)
        self.assertFalse(outcome.board_lost)
        self.assertEqual(1, len(commands.argv))
        self.assertEqual(0.0, clock.now)
        # Recorded as a warning, not a failure: nothing acts on it.
        self.assertEqual("warning", publisher.events[-1][1])
        self.assertIn("while the run was being canceled",
                      publisher.events[-1][2]["reason"])

    def test_a_board_already_given_up_on_is_not_waited_for_or_named_again(self):
        monitor, commands, _publisher, _clock = self.monitor(
            {SERIAL: [samples.BOARD_GONE.format(serial=SERIAL)]})

        first = monitor.after_test(invocation())
        reads = len(commands.argv)
        again = monitor.at_run_end()

        self.assertIn("did not answer on USB", first.failure)
        self.assertEqual("", again.failure)
        self.assertEqual(reads + 1, len(commands.argv))

    def test_a_board_that_never_reports_says_so_at_every_read(self):
        # Firmware without the request, which no read of the run can change.
        monitor, _commands, publisher, _clock = self.monitor(
            {SERIAL: [samples.FIRMWARE_TOO_OLD]})
        monitor.baseline()

        outcome = monitor.after_test(invocation())

        self.assertEqual("", outcome.failure)
        (note,) = outcome.notes
        self.assertIn("watchdog not reported: the board's firmware does not "
                      "support it", note)
        self.assertEqual("not reported", publisher.events[-1][2]["state"])

    def test_the_baseline_says_which_counters_have_stopped(self):
        monitor, _commands, _publisher, _clock = self.monitor(
            {SERIAL: [samples.report(resets=255, boots=255, cause="watchdog")]})

        outcome = monitor.baseline()

        self.assertTrue(any(
            "at the firmware's limit of 255 recorded resets" in note
            for note in outcome.notes
        ))
        self.assertTrue(any(
            "at the firmware's limit of 255 recorded boots" in note
            for note in outcome.notes
        ))

    def test_a_round_during_a_test_reads_only_that_test_s_boards(self):
        # Another test's board may be powered off by this one's isolation, and
        # its resets are not this test's to count.
        other = MonitoredBoard("UT_Linux1", "b" * 32)
        monitor, commands, _publisher, _clock = self.monitor(
            {SERIAL: [samples.report()], other.serial: [samples.report()]},
            boards=(BOARD, other),
        )
        monitor.baseline()
        self.assertEqual(2, len(commands.argv))

        monitor.after_test(invocation())

        self.assertEqual(
            [SERIAL], [argv[2] for argv in commands.argv[2:]])

    def test_each_mid_test_read_gets_a_command_id_of_its_own(self):
        monitor, commands, _publisher, clock = self.monitor(
            {SERIAL: [samples.report()]}, interval_s=10.0)
        monitor.baseline()
        for _ in range(3):
            clock.now += 11.0
            monitor.poll_if_due(invocation())

        self.assertEqual(
            ["watchdog-baseline", "001-Soak-watchdog-during-1",
             "001-Soak-watchdog-during-2", "001-Soak-watchdog-during-3"],
            [command_id.rsplit(f"-{SERIAL}-", 1)[0] for command_id in commands.ids],
        )

    def test_a_board_that_comes_back_inside_the_window_is_only_read_again(self):
        monitor, commands, _publisher, _clock = self.monitor({SERIAL: [
            samples.report(resets=0, boots=1),
            samples.BOARD_GONE.format(serial=SERIAL),
            samples.report(resets=1, boots=2, cause="watchdog"),
        ]})
        monitor.baseline()

        outcome = monitor.after_test(invocation())

        self.assertEqual("", outcome.failure)
        self.assertEqual(3, len(commands.argv))
        self.assertEqual(1, outcome.resets)

    def test_a_long_test_is_read_again_between_cases(self):
        monitor, _commands, publisher, clock = self.monitor({SERIAL: [
            samples.report(resets=0, boots=1),
            samples.report(resets=1, boots=2, cause="watchdog"),
            samples.report(resets=1, boots=2, cause="watchdog"),
        ]}, interval_s=60.0)
        monitor.baseline()

        self.assertEqual((), monitor.poll_if_due(invocation()).warnings)
        clock.now += 61.0
        during = monitor.poll_if_due(invocation())

        self.assertEqual(1, during.resets)
        self.assertIn("reset itself once during the test", during.warnings[0])
        self.assertEqual(
            watchdog.DURING_TEST,
            [e for e in publisher.events if e[0] == "watchdog"][-1][2]["stage"],
        )
        # What the mid-test read found is reported with the test, once: the
        # read after the test carries it, and the same reset is not counted
        # again.
        after = monitor.after_test(invocation())
        self.assertEqual(during.warnings, after.warnings)
        self.assertEqual(1, len(after.warnings))
        self.assertEqual(1, after.resets)
        self.assertEqual(1, monitor.total_resets)

    def test_the_run_summary_offers_an_issue_with_its_duplicate_search(self):
        searched = []

        def search(issue):
            searched.append(issue)
            return watchdog.DuplicateSearch(
                issue.search_terms,
                matches=({"number": 505, "title": "capture dies", "url": "u"},),
            )

        monitor, _commands, publisher, _clock = self.monitor({SERIAL: [
            samples.report(resets=0, boots=1),
            samples.FULL,
        ]})
        monitor.search = search
        monitor.baseline()
        monitor.after_test(invocation())

        monitor.at_run_end()

        (summary,) = [e for e in publisher.events if e[0] == "watchdog_summary"]
        self.assertEqual("warning", summary[1])
        self.assertEqual(3, summary[2]["resets"])
        (board,) = summary[2]["boards"]
        self.assertEqual(("UT_Linux0", SERIAL, 3), (board["resource"], board["serial"],
                                                    board["resets"]))
        (offer,) = summary[2]["issues"]
        self.assertEqual(
            "Firmware watchdog reset in BLE follow mode, running a control request",
            offer["issue"]["title"],
        )
        self.assertEqual([505], [m["number"] for m in offer["duplicates"]["matches"]])
        self.assertEqual(
            'watchdog "BLE follow" "running a control request"',
            offer["duplicates"]["terms"],
        )
        # The issue names the test the reset happened in.
        self.assertIn("- Test: Soak", offer["issue"]["body"])
        self.assertEqual(1, len(searched))

    def test_each_mode_gets_its_own_issue_counting_its_own_resets(self):
        follow = "in BLE follow mode, running a control request"
        promisc = "in BLE promiscuous mode, waiting for a BLE packet or the next hop"
        monitor, _commands, publisher, _clock = self.monitor({SERIAL: [
            samples.report(resets=0, boots=1),
            samples.report(resets=2, boots=3, cause="watchdog", last=follow),
            samples.report(resets=3, boots=4, cause="watchdog", last=promisc),
        ]})
        monitor.baseline()
        monitor.after_test(invocation())
        monitor.after_test(invocation())

        monitor.at_run_end()

        (summary,) = [e for e in publisher.events if e[0] == "watchdog_summary"]
        offers = summary[2]["issues"]
        self.assertEqual(
            [("BLE follow", 2), ("BLE promiscuous", 1)],
            [(offer["issue"]["mode"], offer["resets"]) for offer in offers],
        )
        self.assertEqual(3, summary[2]["resets"])

    def test_only_the_first_few_resets_of_a_run_are_searched_for(self):
        searched = []

        def search(issue):
            searched.append(issue.search_terms)
            return watchdog.DuplicateSearch(issue.search_terms)

        reports = [samples.report(resets=0, boots=1)] + [
            samples.report(resets=n, boots=n + 1, cause="watchdog",
                           last=f"in mode {n} mode, waiting for something")
            for n in range(1, watchdog.MAX_SEARCHES + 2)
        ]
        monitor, _commands, publisher, _clock = self.monitor(
            {SERIAL: reports}, search=search)
        monitor.baseline()
        for _ in range(watchdog.MAX_SEARCHES + 1):
            monitor.after_test(invocation())

        monitor.at_run_end()

        (summary,) = [e for e in publisher.events if e[0] == "watchdog_summary"]
        offers = summary[2]["issues"]
        self.assertEqual(watchdog.MAX_SEARCHES + 1, len(offers))
        self.assertEqual(watchdog.MAX_SEARCHES, len(searched))
        # The issue is still offered; only its search was left out.
        self.assertIn(f"only the first {watchdog.MAX_SEARCHES}",
                      offers[-1]["duplicates"]["error"])

    def test_a_run_with_no_reset_offers_no_issue_and_searches_for_nothing(self):
        def refuse(issue):
            raise AssertionError("searched with no reset to report")

        monitor, _commands, publisher, _clock = self.monitor(
            {SERIAL: [samples.report()]}, search=refuse)
        monitor.baseline()
        monitor.after_test(invocation())
        monitor.at_run_end()

        (summary,) = [e for e in publisher.events if e[0] == "watchdog_summary"]
        self.assertEqual("passed", summary[1])
        self.assertEqual([], summary[2]["issues"])
        self.assertEqual(0, summary[2]["resets"])


def _catalog():
    """One read-only command test, so a run reaches the watchdog reads."""
    return TestCatalog([
        TestDefinition(
            "Soak", "Read the tool's help",
            MappingProxyType({"hosts": HOSTS, "Ubertooth": UBERTOOTH}),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            argv=("ubertooth-util", "-h"),
            output_pattern="usage",
        ),
    ])


class RunExecutor:
    """Host version, the test's help, and `-w` per read, in order."""

    def __init__(self, reports):
        self.reports = list(reports)
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command[-1] == "-w":
            output = self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]
        elif command[-1] == "-h":
            output = "usage\n"
        else:
            output = (
                "Host tools version: libubertooth 1.1 (git-test), "
                "libbtbb 1.0 (git-btbb)\n"
            )
        if on_output:
            on_output("stdout", output)
        return CommandResult(command, 0, output, "", 0.01, False)


class RunRecordTests(unittest.TestCase):
    """What a whole run records: run.jsonl, report.md, and the GUI's view."""

    def run_suite(self, reports, limit=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hw.yaml").write_text(HARDWARE.replace("serial: linux-0", f"serial: {SERIAL}"))
            registry = FileResourceRegistry.load(root / "hw.yaml")
            arguments = {"hosts": "each", "Ubertooth": "default"}
            if limit is not None:
                arguments["max_watchdog_resets"] = limit
            item = TestInvocation(
                "001-Soak", "Soak", "Host_Linux", arguments,
                {"Ubertooth": "UT_Linux0"}, {"Ubertooth": {"serial": SERIAL}},
            )
            manifest, checks = run_contract(root, item)
            executor = RunExecutor(reports)
            sink = CollectSink()
            summary = NamedTestSequencer(
                executor, catalog=_catalog(), event_sink=sink, registry=registry,
                # Nothing in the tests may reach GitHub.
                watchdog_search=lambda issue: watchdog.DuplicateSearch(
                    issue.search_terms,
                    matches=({"number": 505, "title": "capture dies",
                              "url": "https://example.invalid/505"},),
                ),
            ).run((item,), root, run_id="named-run", manifest=manifest,
                  preflight_checks=checks)
            events = [json.loads(line) for line in
                      (root / "run.jsonl").read_text().splitlines()]
            report = (root / "report.md").read_text()
        return summary, events, report, executor

    def test_a_reset_is_recorded_and_reported_without_failing_the_run(self):
        summary, events, report, executor = self.run_suite([
            samples.report(resets=0, boots=1),
            samples.FULL,
            samples.FULL,
        ])

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(3, summary.watchdog_resets)
        # Read at the baseline, after the test, and at the end of the run.
        self.assertEqual(3, sum(1 for c in executor.commands if c[-1] == "-w"))
        stages = [e["details"]["stage"] for e in events if e["event_type"] == "watchdog"]
        self.assertEqual(["baseline", "after the test", "run end"], stages)
        finished = next(e for e in events if e["event_type"] == "test_finished")
        self.assertEqual("passed", finished["status"])
        self.assertEqual(1, finished["details"]["watchdog_warnings"])
        self.assertTrue(any(
            "reset itself" in line
            for e in events if e["event_type"] == "watchdog"
            for line in e["details"]["warnings"]
        ))
        (offer,) = next(
            e for e in events if e["event_type"] == "watchdog_summary"
        )["details"]["issues"]
        self.assertEqual("UT_Linux0", offer["resource"])

        # report.md: the result line, the test's reason, and the section.
        self.assertIn("3 board self-resets (see Watchdog)", report)
        self.assertIn("## Watchdog", report)
        self.assertIn("| UT_Linux0 |", report)
        self.assertIn("Firmware watchdog reset in BLE follow mode", report)
        self.assertIn("warning: UT_Linux0", report)

        # The per-board count the GUI adds up as the records arrive.
        reads = [e["details"] for e in events if e["event_type"] == "watchdog"]
        self.assertEqual(3, sum(read["new_resets"] for read in reads))
        self.assertEqual({"UT_Linux0"}, {read["resource"] for read in reads})
        self.assertEqual({"read"}, {read["state"] for read in reads})

    def test_a_run_with_no_reset_records_the_reads_and_says_nothing_more(self):
        summary, events, report, _executor = self.run_suite([samples.report()])

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(0, summary.watchdog_resets)
        self.assertEqual(
            ["passed", "passed", "passed"],
            [e["status"] for e in events if e["event_type"] == "watchdog"],
        )
        self.assertIn("## Watchdog", report)
        self.assertNotIn("board self-resets", report)
        self.assertNotIn("Issue to file", report)
        self.assertEqual(
            0,
            sum(e["details"]["new_resets"] for e in events
                if e["event_type"] == "watchdog"),
        )

    def test_a_mid_test_reset_is_in_the_test_record_once(self):
        summary, events, report, _executor = self.run_suite(
            [samples.report(resets=0, boots=1), samples.FULL, samples.FULL],
        )

        finished = next(e for e in events if e["event_type"] == "test_finished")
        self.assertEqual(1, finished["details"]["watchdog_warnings"])
        # Once in the Watchdog section's warning list. The results table says
        # how many there were and leaves the text to that section.
        section = report.split("## Watchdog", 1)[1].split("## Transcript", 1)[0]
        self.assertEqual(
            1, section.count("reset itself 3 times during the test; the last was"))
        self.assertIn("warning: 1 watchdog warning (see Watchdog)", report)
        self.assertEqual(3, summary.watchdog_resets)

    def test_a_board_lost_at_the_run_end_fails_the_run_as_itself(self):
        summary, events, report, _executor = self.run_suite(
            [samples.report(), samples.report(), samples.BOARD_GONE.format(serial=SERIAL)],
        )

        self.assertFalse(summary.passed)
        self.assertEqual("failed", summary.status)
        self.assertIn("did not answer on USB", summary.failure_reason)
        # Not reported as a failed test: no test was running.
        self.assertNotIn("more failed test", summary.failure_reason)
        self.assertEqual(
            "passed",
            next(e for e in events if e["event_type"] == "test_finished")["status"],
        )
        self.assertEqual(
            "failed",
            next(e for e in events if e["event_type"] == "watchdog_summary")["status"],
        )
        self.assertIn("did not answer on USB", report)

    def test_a_test_over_its_limit_fails_and_names_the_limit(self):
        summary, events, report, _executor = self.run_suite(
            [samples.report(resets=0, boots=1), samples.FULL, samples.FULL], limit=1,
        )

        self.assertFalse(summary.passed)
        self.assertIn("max_watchdog_resets (1)", summary.failure_reason)
        finished = next(e for e in events if e["event_type"] == "test_finished")
        self.assertEqual("failed", finished["status"])
        self.assertFalse(finished["details"]["stops_run"])
        self.assertIn("max_watchdog_resets (1)", report)


class CatalogTests(unittest.TestCase):

    def test_every_test_accepts_the_limit_and_refuses_a_bad_one(self):
        catalog = builtin_catalog()
        for name in catalog.names:
            definition = catalog.get(name)
            self.assertIn("max_watchdog_resets", definition.arguments, name)
        definition = catalog.get("Host CLI surface")

        self.assertEqual(
            0, definition.normalize_arguments(
                {"hosts": "each", "max_watchdog_resets": 0})["max_watchdog_resets"],
        )
        # Left out when the plan does not set it: no limit.
        self.assertNotIn(
            "max_watchdog_resets", definition.normalize_arguments({"hosts": "each"}))
        with self.assertRaisesRegex(Exception, "nonnegative integer"):
            definition.normalize_arguments({"hosts": "each", "max_watchdog_resets": -1})
        with self.assertRaisesRegex(Exception, "nonnegative integer"):
            definition.normalize_arguments(
                {"hosts": "each", "max_watchdog_resets": "many"})


if __name__ == "__main__":
    unittest.main()
