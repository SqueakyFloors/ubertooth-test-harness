"""btbrconsole.py drives the console and decides each check, against a fake.

tests/fake_btbr.py answers the lines a real session prints. Every fault it can
be given is a check in btbrconsole.py failing for its own reason, so a reader
can see what each check is for without a board.
"""

from __future__ import annotations

import io
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import unittest.mock

from tests import fake_btbr
from tests.fake_btbr import (
    PEER_ADDRESS,
    PEER_NAME,
    write_fake_btbr,
    write_fake_peer_reset,
)
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
        "comprehensive": False,
        # Filled in per run: the session resets the peer between pages.
        "peer_reset": "",
        "peer_device": "/dev/serial/by-id/fake",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SessionTests(unittest.TestCase):
    def setUp(self):
        # The fake answers at once, so every wait the driver may sit out is
        # shortened: a failing check then costs a moment, not its real window.
        for name, value in (("START_TIMEOUT_S", 5.0), ("REPLY_TIMEOUT_S", 0.6),
                            ("IDLE_TIMEOUT_S", 0.6), ("EXIT_TIMEOUT_S", 3.0),
                            # What the driver sits out on purpose: the slots
                            # of a transmit run, the moment a scan is left
                            # running, and the window a record may take.
                            ("TX_SETTLE_S", 0.05), ("SCAN_HOLD_S", 0.05),
                            ("TX_LINK_S", 2.0)):
            patch = unittest.mock.patch.object(btbrconsole, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_session(self, *, faults=(), name=PEER_NAME, peer_name=None,
                    tools=True, peer_reset_fails=False, **overrides):
        """Run one session; ``name`` is what the fake board answers with and
        ``peer_name`` what the registry says, which differ only in the
        mismatch test."""
        with tempfile.TemporaryDirectory() as directory:
            environment = {"PATH": "/usr/bin:/bin"}
            reset, reset_environment = write_fake_peer_reset(
                Path(directory) / "bin", name=name, fails=peer_reset_fails)
            environment.update(reset_environment)
            if tools:
                environment.update(
                    write_fake_btbr(Path(directory) / "bin", faults=faults,
                                    name=name))
                environment[PYTHON_TOOLS_ENV] = str(Path(directory) / "bin")
            out, err = io.StringIO(), io.StringIO()
            status = btbrconsole.run_session(
                arguments(peer_name=peer_name or name, peer_reset=reset,
                          **overrides),
                out, err, environment)
            return status, out.getvalue(), err.getvalue()

    def test_a_good_session_passes_every_check_and_reports_each_one(self):
        status, captured, report = self.run_session()
        self.assertEqual(0, status, report)
        checks = len(btbrconsole.checks(False))
        for line in (
            f"peer address: {PEER_ADDRESS}",
            "check help: 10 commands listed",
            "check help-one: ? inquiry described it",
            "check no-such-command: 'wibble' refused",
            "check no-help: '? wibble' refused",
            "check bad-address: 'bdaddr zzz' refused",
            f"check bad-address: {len(btbrconsole.BAD_ADDRESSES)} addresses "
            "the prompt will not take refused",
            "check page-no-address: 'page' refused",
            "check tx-refusals: 'tx dm1 18' refused",
            f"check tx-refusals: {len(btbrconsole.TX_REFUSALS)} tx command "
            "lines refused, prompt up, no packet printed",
            "check set-address: 22:33:44:55:66:77 set",
            "check zero-address: 00:11:22:33:44:55 echoed as 001122334455",
            f"check inquiry: peer {PEER_ADDRESS.replace(':', '').lower()} answered",
            "check inquiry-idle: 'Inquiry done'",
            f"check page: connected to {PEER_ADDRESS}",
            f"check page-peer-name: LMP_NAME_RES carried '{PEER_NAME}'",
            f"check tx-formats: {len(btbrconsole.TX_RUNS)} run(s) transmitted",
            "check tx-link: the link carried a packet ",
            "check page-idle: 'Paging done'",
            "check lmp-unhandled: 0x20 refused, each named once",
            f"check page-repeat: {btbrconsole.PAGE_REPEATS} further page(s) "
            "completed and named the peer",
            "check scan-modes: page_scan, inquiry_scan each started, held, "
            "and returned to standby",
            "check scheduler-reports: none of the ",
            "check no-detach: the peer took the connection and this end did "
            "not hang up",
            "check streams: ",
            "session exit: 0 after ",
        ):
            self.assertIn(line, report)
        self.assertIn(f"checks: {checks} passed of {checks}, 0 failed, "
                      "0 not run", report)
        # The capture never leaves the driver: an inquiry hears every
        # discoverable device in range, and those addresses and names must not
        # reach the run record. Nothing is written to standard output at all.
        self.assertEqual("", captured)
        self.assertNotIn(fake_btbr.OTHER_NAME, report)
        self.assertNotIn(fake_btbr.OTHER_ADDRESS, report)
        # What it does report of the capture is the record it matched for the
        # registered peer, which is the bench's own device.
        self.assertIn(f"  record: 09:11:00 RX FHS: FHS: bdaddr="
                      f"{PEER_ADDRESS.replace(':', '').lower()}", report)
        self.assertIn("  record: 09:11:00 RX LMP: ", report)
        # The tool's own progress is marked, so a report line cannot be
        # mistaken for it.
        self.assertIn("btbr| Type ? to list commands", report)

    def test_the_comprehensive_profile_adds_more_of_each(self):
        status, _captured, report = self.run_session(comprehensive=True)
        self.assertEqual(0, status, report)
        checks = len(btbrconsole.checks(True))
        runs = len(btbrconsole.TX_RUNS) + len(btbrconsole.TX_RUNS_COMPREHENSIVE)
        self.assertIn("check page-no-answer: 'paging timeout' after ", report)
        self.assertIn(f"check tx-formats: {runs} run(s) transmitted", report)
        self.assertIn(
            f"check page-repeat: {btbrconsole.PAGE_REPEATS_COMPREHENSIVE} "
            "further page(s) completed and named the peer", report)
        self.assertIn(f"checks: {checks} passed of {checks}, 0 failed, "
                      "0 not run", report)

    def test_without_a_python_tool_build_the_case_skips(self):
        status, captured, _report = self.run_session(tools=False)
        self.assertEqual(SKIP_EXIT, status)
        self.assertIn("SKIP: ubertooth-btbr is installed only by", captured)

    def test_a_board_that_does_not_answer_the_startup_fails_the_session(self):
        status, _captured, report = self.run_session(faults=("no-usb",))
        self.assertEqual(1, status)
        self.assertIn("printed no 'USB connected' line", report)
        # With nothing said, the firmware is named as the likely cause.
        self.assertIn("a board carrying bluetooth_rxtx fails this way", report)

    def test_the_tools_own_reason_for_not_starting_is_reported(self):
        status, _captured, report = self.run_session(faults=("says-why",))
        self.assertEqual(1, status)
        self.assertIn("the tool said: error: Ubertooth 0123 is in use by "
                      "another program (libusb -6)", report)
        self.assertNotIn("a board carrying bluetooth_rxtx fails this way",
                         report)

    def test_an_interpreter_without_libusb1_skips_rather_than_failing(self):
        # Nothing on the bench is wrong and the run cannot decide the case.
        status, captured, report = self.run_session(faults=("no-libusb1",))
        self.assertEqual(SKIP_EXIT, status)
        self.assertIn("SKIP: the interpreter ubertooth-btbr runs has no "
                      "libusb1 package", captured)
        self.assertIn("session: not run: ", report)

    def test_an_address_that_is_not_the_peers_is_not_named_in_the_report(self):
        # One of the tool's own warnings renders a decoded packet, so the
        # progress stream could name a device the capture stream does not.
        status, _captured, report = self.run_session(faults=("stray-packet",))
        self.assertEqual(0, status, report)
        self.assertNotIn(fake_btbr.OTHER_ADDRESS, report)
        self.assertIn("bdaddr=<another device>", report)
        # The peer's own is still shown.
        self.assertIn(f"bdaddr={PEER_ADDRESS.replace(':', '').lower()}", report)

    def test_the_startup_lines_may_arrive_in_any_order(self):
        # `EIR updated` is a firmware line the receive thread ferries up, and
        # the tool prints the intro milliseconds later, so which lands first
        # is a race rather than a fault.
        status, _captured, report = self.run_session(faults=("late-eir",))
        self.assertEqual(0, status, report)
        checks = len(btbrconsole.checks(False))
        self.assertIn(f"checks: {checks} passed of {checks}, 0 failed, "
                      "0 not run", report)

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
            ("capture-on-stderr", "a capture record is on stderr"),
            ("stuck-idle", "idle did not reach"),
            ("bad-exit", "the session exited 1 after"),
            ("drops-leading-zeros",
             "echoed 1122334455, not 001122334455"),
            ("takes-bad-tx", "'tx dm1 18' was not refused"),
            ("wrong-tx-payload", "the packet the board was asked for is not "
                                 "the one the format gives"),
            ("tx-already-waiting",
             "the board refused a transmit this session asked for"),
            ("scheduler-storm", "the board could not schedule its work"),
            ("dead-link", "the connection did not carry on"),
            ("second-page-fails", "page 1 of 1 after the first: the peer"),
            ("false-connection", "link supervision ended it and the page "
                                 "train started again"),
            ("stuck-scan", "the board was never reported in that state"),
            ("hangs-up", "this end hung up on the peer"),
        ):
            with self.subTest(fault):
                status, _captured, report = self.run_session(faults=(fault,))
                self.assertEqual(1, status, report)
                self.assertIn(expected, report)
                self.assertIn("session failed: ", report)

    def test_one_refusal_the_board_met_is_reported_and_allowed(self):
        # Each of those lines is an exceptional condition the firmware is
        # built to survive, so one of them is not a fault: it is reported, and
        # the checks around it say whether the session came through it. The
        # same fault repeated fails, which the fault matrix above covers.
        status, _captured, report = self.run_session(faults=("slot-not-sent",))
        self.assertEqual(0, status, report)
        self.assertIn("check scheduler-reports: the board met an exceptional "
                      "condition and the session came through it: "
                      "'TX: cannot schedule transmit, packet not sent' 1",
                      report)
        # The transmit check names it too, and passes on the packet that
        # followed.
        self.assertIn("check tx-link: the link carried a packet ", report)
        self.assertIn("the board had reported 'TX: cannot schedule transmit",
                      report)

    def test_a_peer_that_cannot_be_reset_fails_the_repeat_page(self):
        # The repeat page is only a page of the peer if the peer has forgotten
        # the last one, so a reset that did not happen is not a page it can
        # decide.
        status, _captured, report = self.run_session(peer_reset_fails=True)
        self.assertEqual(1, status)
        self.assertIn("check page-repeat: FAILED: the peer was not reset "
                      "(exit 1): error: could not open the peer's bridge",
                      report)

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
        checks = len(btbrconsole.checks(False))
        status, _captured, report = self.run_session(faults=("no-help",))
        self.assertEqual(1, status)
        # The help check failed and the rest still ran.
        self.assertIn(f"checks: {checks - 1} passed of {checks}, 1 failed, "
                      "0 not run", report)
        status, _captured, report = self.run_session(faults=("no-usb",))
        self.assertEqual(1, status)
        self.assertIn(f"checks: 0 passed of {checks}, 0 failed, "
                      f"{checks} not run", report)


class StreamReaderTests(unittest.TestCase):
    """How the reader treats carriage returns, which decide whether a check
    sees an answer the tool printed: every pattern anchors on the end of a
    line, and the tool ends some of its lines with a carriage return and a
    newline."""

    def stream(self):
        return btbrconsole.Stream("stderr", io.BytesIO(), None, "")

    def test_a_carriage_return_before_a_newline_goes(self):
        stream = self.stream()
        stream._append("Set bdaddr 223344556677 -> sw b155\r\n")
        self.assertEqual("Set bdaddr 223344556677 -> sw b155\n",
                         stream.text)

    def test_a_carriage_return_split_across_two_reads_goes(self):
        # A read can end between the two bytes, which is the case no single
        # replacement over one chunk would catch.
        stream = self.stream()
        stream._append("first line\r")
        # Held back, so nothing yet says the line ended.
        self.assertEqual("first line", stream.text)
        stream._append("\nsecond line\r\n")
        self.assertEqual("first line\nsecond line\n", stream.text)

    def test_a_redraw_before_a_line_leaves_no_carriage_return(self):
        # A progress redraw immediately before a CRLF-terminated line gives
        # `...\r\r\n`, where replacing the pair once would leave one in.
        stream = self.stream()
        stream._append("progress\r\r\n")
        self.assertEqual("progress\n", stream.text)

    def test_a_carriage_return_with_no_newline_after_it_stays(self):
        # The tool redraws a line with one, so it is text like any other.
        stream = self.stream()
        stream._append("counting 1\rcounting 2\r")
        stream._append("counting 3\n")
        self.assertEqual("counting 1\rcounting 2\rcounting 3\n",
                         stream.text)

    def test_a_carriage_return_at_the_end_of_the_stream_is_kept(self):
        source = io.BytesIO(b"last\r")
        stream = btbrconsole.Stream("stderr", source, None, "")
        stream.thread.start()
        stream.thread.join(5.0)
        self.assertEqual("last\r", stream.text)

    def test_nothing_more_is_kept_once_the_stream_has_overflowed(self):
        # A chunk appended after one was dropped would leave a hole in the
        # middle of the text, and every offset a wait started from would
        # point past its answer.
        stream = self.stream()
        stream._append("a" * (btbrconsole.MAX_STREAM_CHARS + 1))
        self.assertTrue(stream.overflowed)
        stream._append("small\n")
        self.assertEqual("", stream.text)


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

    def test_the_transmit_checks_run_inside_the_connection(self):
        # The transmit runs need the connection the page brought up, and the
        # hold and the `idle` that end it come after them; the refusals, which
        # need no connection, come before the page.
        order = [name for name, _check in btbrconsole.checks(True)]
        for label in ("tx-formats", "tx-link"):
            with self.subTest(label):
                self.assertGreater(order.index(label), order.index("page"))
                self.assertLess(order.index(label), order.index("page-idle"))
        self.assertLess(order.index("tx-refusals"), order.index("page"))
        # The two that read the whole session read it whole: nothing that
        # drives the board comes after them.
        for label in ("scheduler-reports", "no-detach"):
            with self.subTest(label):
                self.assertGreater(order.index(label),
                                   order.index("scan-modes"))

    def test_every_check_is_in_the_budget(self):
        # A check with no entry would be left outside the step's deadline, so
        # the budget is asked for each one by name; an unknown name raises,
        # which is what keeps a stale entry from passing for a real one.
        windows = {"inquiry": 1.0, "page": 1.0, "hold": 1.0, "no_answer": 1.0}
        for comprehensive in (False, True):
            for label, _check in btbrconsole.checks(comprehensive):
                with self.subTest(label, comprehensive=comprehensive):
                    self.assertGreaterEqual(
                        btbrconsole._check_seconds(
                            label, comprehensive=comprehensive, **windows),
                        0.0)
        with self.assertRaises(KeyError):
            btbrconsole._check_seconds("not-a-check", comprehensive=False,
                                       **windows)

    def test_the_comprehensive_budget_is_the_larger(self):
        self.assertGreater(btbrconsole.worst_case_seconds(True),
                           btbrconsole.worst_case_seconds(False))

    def test_the_fake_names_no_real_device(self):
        # This repository must not hold captured device traffic, so the
        # second device an inquiry hears is made up too.
        self.assertTrue(fake_btbr.OTHER_ADDRESS.startswith("02"),
                        "a locally administered address belongs to nobody")
        self.assertEqual("Example Device", fake_btbr.OTHER_NAME)

    def test_no_address_the_prompt_takes_is_sent_as_a_bad_one(self):
        # The prompt takes six bytes written with the five colons or with
        # none, and nothing else. Every word in the list must be outside
        # that, or the check would be asking the tool to refuse an address it
        # is right to accept.
        taken = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z"
                           r"|[0-9A-Fa-f]{12}\Z")
        for word in btbrconsole.BAD_ADDRESSES:
            with self.subTest(word):
                self.assertIsNone(taken.match(word))
        # The shapes the list has to cover, each of which named a different
        # address from the one that was typed before the tool refused them
        # (fork #1075).
        for word in ("11:22:33", "11:2233:4455:66", "11-22-33-44-55-66",
                     "0x112233445566", "11_2233445566"):
            self.assertIn(word, btbrconsole.BAD_ADDRESSES)

    def test_the_transmit_runs_name_formats_the_table_describes(self):
        for line in btbrconsole.TX_RUNS + btbrconsole.TX_RUNS_COMPREHENSIVE:
            with self.subTest(line):
                self.assertIn(line.split()[0], btbrconsole.TX_FORMATS)
                # Every run has to be one the driver can work an expectation
                # out for, which is what the check compares.
                btbrconsole.tx_expectation(line)

    def test_the_counted_fills_cannot_be_mistaken_for_each_other(self):
        # A `dv` carries ten voice bytes and at most nine data bytes, and the
        # two fills must not overlap: that is what lets a decoder see where
        # the voice field ends from the bytes alone.
        most = btbrconsole.TX_FORMATS["dv"].most
        data = set(btbrconsole.counted_fill(most))
        voice = set(btbrconsole.counted_fill(
            btbrconsole.TX_FORMATS["dv"].voice, btbrconsole.TX_VOICE_BASE))
        self.assertEqual(set(), data & voice)


class TransmitPayloadTests(unittest.TestCase):
    """The payload the driver expects, against the man page's worked examples.

    ubertooth-btbr.md, TRANSMITTING CHOSEN PACKET TYPES, prints both of these
    as the lines a `tx` gives.
    """

    def test_an_enhanced_synchronous_payload_is_the_data_alone(self):
        name, length, count, data, payload = btbrconsole.tx_expectation(
            "ev3 3 20")
        self.assertEqual(("ev3", 3, 20), (name, length, count))
        self.assertEqual("000102", data.hex())
        self.assertEqual("000102", payload.hex())

    def test_a_dv_payload_is_the_voice_field_then_the_header_then_the_data(self):
        _name, _length, _count, data, payload = btbrconsole.tx_expectation(
            "dv 3 20")
        self.assertEqual("000102", data.hex())
        # The voice field counting from 0xa0, then 0x1e -- three data bytes,
        # the flow bit, and an L2CAP first fragment -- then the data.
        self.assertEqual("a0a1a2a3a4a5a6a7a8a91e000102", payload.hex())

    def test_a_length_not_given_is_the_formats_maximum(self):
        _name, length, count, data, _payload = btbrconsole.tx_expectation("dv")
        self.assertEqual((9, 1), (length, count))
        self.assertEqual("000102030405060708", data.hex())

    def test_a_payload_given_by_hand_is_not_counted(self):
        _name, length, _count, data, payload = btbrconsole.tx_expectation(
            "dm1 4 1 deadbeef")
        self.assertEqual(4, length)
        self.assertEqual("deadbeef", data.hex())
        self.assertEqual("26deadbeef", payload.hex())


class OptionValueTests(unittest.TestCase):
    """`options`: the values the command line refuses before the board stage."""

    def run_options(self, *, faults=(), tools=True):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"PATH": "/usr/bin:/bin"}
            if tools:
                environment.update(
                    write_fake_btbr(Path(directory) / "bin", faults=faults))
                environment[PYTHON_TOOLS_ENV] = str(Path(directory) / "bin")
            out, err = io.StringIO(), io.StringIO()
            status = btbrconsole.run_options(out, err, environment)
            return status, out.getvalue(), err.getvalue()

    def test_every_value_is_refused_and_the_control_reaches_the_board(self):
        status, _captured, report = self.run_options()
        self.assertEqual(0, status, report)
        for line in (
            "check option -U 99 -f 64: 'error: -f must be 0-63 (got 64)', "
            "and no board looked for",
            "check option -U 99 -f bad: 'error: -f must be 0-63 (got bad)', "
            "and no board looked for",
            "check option -U 99 -b 4: 'error: -b must be 0-3 (got 4)', and "
            "no board looked for",
            "check option -U 99 -b -1: 'error: -b must be 0-3 (got -1)', and "
            "no board looked for",
            "check option control: -U 99 reached the device stage: error: no "
            "Ubertooth at index 99",
        ):
            self.assertIn(line, report)
        decided = len(btbrconsole.OPTION_REFUSALS) + 1
        self.assertIn(f"options: {decided} passed of {decided}, 0 failed",
                      report)

    def test_a_value_outside_the_field_that_is_taken_fails(self):
        status, _captured, report = self.run_options(
            faults=("takes-bad-option",))
        self.assertEqual(1, status)
        self.assertIn("check option: FAILED: -U 99 -f 64: said", report)

    def test_a_tool_that_looks_for_the_board_first_fails(self):
        # The order is the point: a refusal that came after the board was
        # looked for is not a refusal before it.
        status, _captured, report = self.run_options(
            faults=("opens-board-first",))
        self.assertEqual(1, status)
        self.assertIn("refused the value and went looking for the board as "
                      "well", report)

    def test_a_host_that_cannot_show_the_order_skips(self):
        # The tool looks for the board after it imports the package a session
        # needs, so on such a host the control run shows nothing about the
        # order the values are refused in. Nothing on the bench is wrong, so
        # the step skips rather than passing on a control that proved nothing.
        status, captured, report = self.run_options(faults=("no-libusb1",))
        self.assertEqual(SKIP_EXIT, status)
        self.assertIn("SKIP: the interpreter ubertooth-btbr runs has no "
                      "libusb1 package", captured)
        self.assertIn("the order the option values are refused in cannot be "
                      "shown", captured)
        self.assertIn("stopped at the import a session needs", report)

    def test_without_a_python_tool_build_the_step_skips(self):
        status, captured, _report = self.run_options(tools=False)
        self.assertEqual(SKIP_EXIT, status)
        self.assertIn("SKIP: ubertooth-btbr is installed only by", captured)


class CommandLineTests(unittest.TestCase):
    REST = ["--peer-reset", "reset.py", "--peer-device", "/dev/fake"]

    def test_a_peer_that_is_not_an_address_is_refused(self):
        self.assertEqual(2, btbrconsole.main(
            ["session", "--serial", "x", "--peer", "nonsense",
             "--peer-name", PEER_NAME] + self.REST))

    def test_an_empty_peer_name_is_refused(self):
        self.assertEqual(2, btbrconsole.main(
            ["session", "--serial", "x", "--peer", PEER_ADDRESS,
             "--peer-name", ""] + self.REST))


if __name__ == "__main__":
    unittest.main()
