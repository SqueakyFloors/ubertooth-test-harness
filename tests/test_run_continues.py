import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from tests.test_sequencer import CollectSink, run_contract
from tests.test_tool_cases import ToolUbertooth
from tests.test_transitions import IDENTITIES, HostVersionPassthrough, cases_for


def invocation(index, test_name, coverage):
    arguments, _cases = cases_for(test_name, coverage)
    return TestInvocation(
        f"{index:03d}-{test_name}", test_name, "Host_Linux", arguments,
        {"Ubertooth": "UT_Linux0"}, IDENTITIES,
    )


def run(device, invocations):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifest, checks = run_contract(root, tuple(invocations))
        sink = CollectSink()
        summary = NamedTestSequencer(HostVersionPassthrough(device), event_sink=sink).run(
            tuple(invocations), root, run_id="named-run", manifest=manifest,
            preflight_checks=checks,
        )
        return summary, sink.events


def finished(events):
    return [(e.details["invocation_id"], e.status, e.details.get("stops_run"))
            for e in events if e.event_type == "test_finished"]


class RunContinuesTests(unittest.TestCase):

    def test_a_cleanly_failed_test_does_not_stop_later_tests(self):
        # Comprehensive Debug registers fails its exit-status cases (pre-#164
        # behavior) but every case is verified restored.
        device = ToolUbertooth(crc="1", debug_exit_is_value=True)
        invocations = [
            invocation(1, "Debug registers", "comprehensive"),
            invocation(2, "Debug registers", "smoke"),
            invocation(3, "BLE settings", "smoke"),
        ]

        summary, events = run(device, invocations)

        self.assertFalse(summary.passed)
        self.assertEqual("failed", summary.status)
        self.assertEqual(2, summary.completed_tests)
        self.assertEqual(3, summary.requested_tests)
        self.assertEqual([
            ("001-Debug registers", "failed", False),
            ("002-Debug registers", "passed", None),
            ("003-BLE settings", "passed", None),
        ], finished(events))
        self.assertTrue(summary.failure_reason.startswith("Debug registers:"))
        self.assertNotIn("more failed", summary.failure_reason)

    def test_the_summary_counts_every_failed_test(self):
        device = ToolUbertooth(crc="1", debug_exit_is_value=True, btle_aa_bugs=True)
        invocations = [
            invocation(1, "Debug registers", "comprehensive"),
            invocation(2, "BLE settings", "comprehensive"),
            invocation(3, "Debug registers", "smoke"),
        ]

        summary, events = run(device, invocations)

        self.assertEqual(1, summary.completed_tests)
        self.assertIn("(and 1 more failed test)", summary.failure_reason)
        self.assertEqual(["failed", "failed", "passed"], [s for _i, s, _x in finished(events)])

    def test_an_unrestored_device_still_ends_the_run(self):
        # Every restore of the channel fails, so the first case is not
        # verified restored and the device state is unknown.
        def fail(argv):
            return argv[0] == "ubertooth-util" and any(a.startswith("-c2") for a in argv[3:])

        device = ToolUbertooth(crc="1", debug_exit_is_value=True, fail=fail)
        invocations = [
            invocation(1, "Debug registers", "comprehensive"),
            invocation(2, "Debug registers", "smoke"),
        ]

        summary, events = run(device, invocations)

        self.assertFalse(summary.passed)
        self.assertEqual(0, summary.completed_tests)
        self.assertEqual([("001-Debug registers", "failed", True)], finished(events))
        self.assertFalse(any(e.event_type == "test_started"
                             and e.details["invocation_id"].startswith("002")
                             for e in events))

    def test_a_failed_original_read_ends_the_run(self):
        # The CRC read fails, so its restore is skipped: nothing reported a
        # cleanup failure, but the state was never verified.
        def fail(argv):
            return tuple(argv[3:]) == ("-v",) and argv[0] == "ubertooth-btle"

        device = ToolUbertooth(crc="1", fail=fail)
        invocations = [
            invocation(1, "BLE CRC transitions", "smoke"),
            invocation(2, "Debug registers", "smoke"),
        ]

        summary, events = run(device, invocations)

        self.assertFalse(summary.passed)
        self.assertEqual([("001-BLE CRC transitions", "failed", True)], finished(events))

    def test_cancellation_after_a_clean_failure_keeps_the_failure(self):
        from ubertooth_harness.cancellation import CancellationToken
        token = CancellationToken()
        device = ToolUbertooth(crc="1", debug_exit_is_value=True)
        original = device.run

        seen = []

        def run_and_cancel(argv, **options):
            result = original(argv, **options)
            if tuple(argv[3:5]) == ("-r", "0x02,0x01,0x00"):
                seen.append(argv)
                if len(seen) == 2:  # the list read of the second test
                    token.cancel("operator stop")
            return result

        device.run = run_and_cancel
        invocations = [
            invocation(1, "Debug registers", "comprehensive"),
            invocation(2, "Debug registers", "smoke"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, checks = run_contract(root, tuple(invocations))
            summary = NamedTestSequencer(HostVersionPassthrough(device), event_sink=CollectSink()).run(
                tuple(invocations), root, run_id="named-run", manifest=manifest,
                preflight_checks=checks, cancellation=token,
            )

        self.assertEqual("failed", summary.status)
        self.assertTrue(summary.failure_reason.startswith("Debug registers:"))
        self.assertIn("; then Debug registers:", summary.failure_reason)
        self.assertIn("operator stop", summary.failure_reason)


if __name__ == "__main__":
    unittest.main()
