"""btbrconsole.py drives the console and decides each check, against a fake.

tests/fake_btbr.py answers the lines a real session prints. Every fault it can
be given is a check in btbrconsole.py failing for its own reason, so a reader
can see what each check is for without a board.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import unittest.mock

from tests.fake_btbr import PEER_ADDRESS, PEER_NAME, write_fake_btbr
from ubertooth_harness import btbrconsole
from ubertooth_harness.pytools import PYTHON_TOOLS_ENV, SKIP_EXIT


def arguments(**overrides) -> SimpleNamespace:
    values = {
        "serial": "05f00014e380df95f4436562c32000f5",
        "peer": PEER_ADDRESS,
        "peer_name": PEER_NAME,
        # Short windows: the fake answers at once, so a wait that does not
        # end is a failing check rather than a slow test.
        "inquiry_seconds": 3.0,
        "page_seconds": 3.0,
        "hold_seconds": 0.05,
        "no_answer_seconds": 3.0,
        "no_answer": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SessionTests(unittest.TestCase):
    def setUp(self):
        # The fake answers at once, so every wait the driver may sit out is
        # shortened: a failing check then costs a moment, not its real window.
        for name, value in (("START_TIMEOUT_S", 5.0), ("REPLY_TIMEOUT_S", 0.6),
                            ("IDLE_TIMEOUT_S", 0.6), ("EXIT_TIMEOUT_S", 3.0)):
            patch = unittest.mock.patch.object(btbrconsole, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_session(self, *, faults=(), name=PEER_NAME, peer_name=None,
                    tools=True, **overrides):
        """Run one session; ``name`` is what the fake board answers with and
        ``peer_name`` what the registry says, which differ only in the
        mismatch test."""
        with tempfile.TemporaryDirectory() as directory:
            environment = {"PATH": "/usr/bin:/bin"}
            if tools:
                environment.update(
                    write_fake_btbr(Path(directory) / "bin", faults=faults,
                                    name=name))
                environment[PYTHON_TOOLS_ENV] = str(Path(directory) / "bin")
            out, err = io.StringIO(), io.StringIO()
            status = btbrconsole.run_session(
                arguments(peer_name=peer_name or name, **overrides),
                out, err, environment)
            return status, out.getvalue(), err.getvalue()

    def test_a_good_session_passes_every_check_and_reports_each_one(self):
        status, captured, report = self.run_session()
        self.assertEqual(0, status, report)
        for line in (
            f"peer address: {PEER_ADDRESS}",
            "check help: 10 commands listed",
            "check help-one: ? inquiry described it",
            "check no-such-command: 'wibble' refused",
            "check no-help: '? wibble' refused",
            "check bad-address: 'bdaddr zzz' refused",
            "check page-no-address: 'page' refused",
            "check set-address: 22:33:44:55:66:77 set",
            f"check inquiry: peer {PEER_ADDRESS.replace(':', '').lower()} answered",
            "check inquiry-idle: 'Inquiry done'",
            f"check page: connected to {PEER_ADDRESS}",
            f"check page-peer-name: LMP_NAME_RES carried '{PEER_NAME}'",
            "check page-idle: 'Paging done'",
            "check lmp-unhandled: 0x20 refused, each named once",
            "check streams: ",
            "session exit: 0 after ",
        ):
            self.assertIn(line, report)
        self.assertIn("checks: 11 passed of 11, 0 failed, 0 not run", report)
        # The capture never leaves the driver: an inquiry hears every
        # discoverable device in range, and those addresses and names must not
        # reach the run record. Nothing is written to standard output at all.
        self.assertEqual("", captured)
        self.assertNotIn("[TV] UN75J6300", report)
        self.assertNotIn("f877b844d1b9", report)
        # What it does report of the capture is the record it matched for the
        # registered peer, which is the bench's own device.
        self.assertIn(f"  record: 09:11:00 RX FHS: FHS: bdaddr="
                      f"{PEER_ADDRESS.replace(':', '').lower()}", report)
        self.assertIn("  record: 09:11:00 RX LMP: ", report)
        # The tool's own progress is marked, so a report line cannot be
        # mistaken for it.
        self.assertIn("btbr| Type ? to list commands", report)

    def test_the_comprehensive_profile_adds_the_page_nothing_answers(self):
        status, _captured, report = self.run_session(no_answer=True)
        self.assertEqual(0, status, report)
        self.assertIn("check page-no-answer: 'paging timeout' after ", report)
        self.assertIn("checks: 12 passed of 12, 0 failed, 0 not run", report)

    def test_without_a_python_tool_build_the_case_skips(self):
        status, captured, _report = self.run_session(tools=False)
        self.assertEqual(SKIP_EXIT, status)
        self.assertIn("SKIP: ubertooth-btbr is installed only by", captured)

    def test_a_board_that_does_not_answer_the_startup_fails_the_session(self):
        status, _captured, report = self.run_session(faults=("no-usb",))
        self.assertEqual(1, status)
        self.assertIn("printed no 'USB connected' line", report)
        self.assertIn("a board carrying bluetooth_rxtx fails here", report)

    def test_a_missing_prompt_fails_the_session(self):
        status, _captured, report = self.run_session(faults=("no-prompt",))
        self.assertEqual(1, status)
        self.assertIn("the prompt did not", report)

    def test_each_fault_fails_its_own_check(self):
        for fault, expected in (
            ("no-help", "? printed no 'Documented commands'"),
            ("short-help", "? did not list page_scan"),
            ("takes-bad-address", "'bdaddr' was not refused"),
            ("pages-without-address",
             "page with no address started paging anyway"),
            ("silent-inquiry", "the inquiry did not hear the peer"),
            ("unnamed-peer", "no Extended Inquiry Response named"),
            ("deaf-peer", "did not answer the page"),
            ("no-lmp-name", "no LMP_NAME_RES on the air carried the peer's name"),
            ("repeat-unhandled",
             "an unhandled LMP opcode was reported more than once: 0x20 3 times"),
            ("quiet-lmp",
             "no LMP request with no handler was reported at all"),
            ("capture-on-stderr", "a capture line is on stderr"),
            ("stuck-idle", "idle did not reach"),
            ("bad-exit", "the session exited 1 after"),
        ):
            with self.subTest(fault):
                status, _captured, report = self.run_session(faults=(fault,))
                self.assertEqual(1, status, report)
                self.assertIn(expected, report)
                self.assertIn("session failed: ", report)

    def test_a_session_that_will_not_exit_is_killed_and_reported(self):
        status, _captured, report = self.run_session(faults=("never-exits",))
        self.assertEqual(1, status)
        self.assertIn("the session did not exit within", report)

    def test_a_board_answering_with_another_name_fails_the_inquiry_check(self):
        # The name follows from the peer's address, so a board answering with
        # another one is a board the registry does not describe. The fake
        # answers with the name it is given; the driver is told the one the
        # registry derived.
        status, _captured, report = self.run_session(name="UT-ESP32-OTHER",
                                                     peer_name=PEER_NAME)
        self.assertEqual(1, status)
        self.assertIn("no Extended Inquiry Response named 'UT-ESP32-2A1B2A'",
                      report)
        self.assertIn("response(s) carried a name", report)
        # The names it did carry are counted, never reported.
        self.assertNotIn("UT-ESP32-OTHER", report)

    def test_a_session_that_stops_early_says_which_checks_never_ran(self):
        status, _captured, report = self.run_session(faults=("no-help",))
        self.assertEqual(1, status)
        # The help check failed and the rest still ran.
        self.assertIn("checks: 10 passed of 11, 1 failed, 0 not run", report)
        status, _captured, report = self.run_session(faults=("no-usb",))
        self.assertEqual(1, status)
        self.assertIn("checks: 0 passed of 11, 0 failed, 11 not run", report)


class CheckListTests(unittest.TestCase):
    def test_the_no_answer_check_runs_after_the_page_and_before_the_streams(self):
        smoke = [name for name, _check in btbrconsole.checks(False)]
        comprehensive = [name for name, _check in btbrconsole.checks(True)]
        self.assertNotIn("page-no-answer", smoke)
        self.assertEqual(len(smoke) + 1, len(comprehensive))
        self.assertGreater(comprehensive.index("page-no-answer"),
                           comprehensive.index("page"))
        self.assertLess(comprehensive.index("page-no-answer"),
                        comprehensive.index("streams"))

    def test_no_check_sends_an_address_shorter_than_six_bytes(self):
        # The tool takes one today and acts on a different address (fork
        # issue 1075); a case here would pin that rather than check it.
        for word in btbrconsole.BAD_ADDRESSES:
            digits = word.replace(":", "")
            with self.subTest(word):
                self.assertFalse(
                    len(digits) < 12 and all(c in "0123456789abcdefABCDEF"
                                             for c in digits) and digits)


class CommandLineTests(unittest.TestCase):
    def test_a_peer_that_is_not_an_address_is_refused(self):
        self.assertEqual(2, btbrconsole.main(
            ["session", "--serial", "x", "--peer", "nonsense",
             "--peer-name", PEER_NAME]))

    def test_an_empty_peer_name_is_refused(self):
        self.assertEqual(2, btbrconsole.main(
            ["session", "--serial", "x", "--peer", PEER_ADDRESS,
             "--peer-name", ""]))


if __name__ == "__main__":
    unittest.main()
