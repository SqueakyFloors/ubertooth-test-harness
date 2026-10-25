import io
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ubertooth_harness import extcapcapture, pytools, python_cases
from ubertooth_harness.catalog import builtin_catalog
import tests.test_transitions as tt
from tests.test_python_cases import git
from tests.test_transitions import SERIAL

TEST = "BLE extcap capture"

# btle-extcap.py --capture as at fork 71e1a8a, trimmed to the capture path.
EXTCAP = '''#!/usr/bin/env python3
import getopt, re, signal, sys
from subprocess import Popen

child = None

def sig_handler(sig, frame):
    if child is not None:
        child.terminate()

def main():
    signal.signal(signal.SIGTERM, sig_handler)
    opts, args = getopt.getopt(sys.argv[1:], "h", ["capture", "extcap-interface=",
                                                   "fifo=", "channel="])
    interface = ''
    fifo = None
    channel = "37"
    do_capture = False
    for o, a in opts:
        if o == "--capture":
            do_capture = True
        elif o == "--extcap-interface":
            interface = a
        elif o == "--fifo":
            fifo = a
        elif o == "--channel":
            channel = a
    m = re.match(r'ubertooth([0-9a-fA-F]+)$', interface)
    if not m:
        print("error: --extcap-interface must be ubertooth<serial>, as "
              "--extcap-interfaces lists it", file=sys.stderr)
        exit(1)
    interface = m.group(1)
    if do_capture:
        if fifo is None:
            print("error: --capture needs --fifo=PATH", file=sys.stderr)
            exit(1)
        capture(interface, fifo, channel)

def capture(interface, fifo, channel):
    global child
    p = Popen(["ubertooth-btle", "-f", "-U", interface, "-q", fifo, "-A", channel])
    child = p
    p.wait()

if __name__ == "__main__":
    main()
'''
# The fix SqueakyFloors/ubertooth#334 asks for.
FIXED_EXTCAP = EXTCAP.replace("    p.wait()\n", "    sys.exit(p.wait())\n")

SERIAL_A = "1ff0ff1967102caf8334fa5ac52000f5"
SERIAL_B = "0b700017490c2cafa039ee5ac52000f5"
NO_BOARD = "0badc0de" * 4

# ubertooth-btle -f -U <serial> -q <fifo> -A <channel> as at fork 71e1a8a: -A
# is checked before the FIFO is opened; the FIFO is opened (header written)
# before the board, so an unknown serial ends after the header with exit 1;
# SIGTERM ends the capture with exit 0. FAKE_BTLE switches on a bug;
# FAKE_RECORDS caps the records sent.
FAKE_BTLE = r'''#!PYTHON
import os, signal, struct, sys, time
stop = False
def quit(*_args):
    global stop
    stop = True
signal.signal(signal.SIGTERM, quit)
args = sys.argv[1:]
bug = os.environ.get("FAKE_BTLE", "")
limit = int(os.environ.get("FAKE_RECORDS", "0"))
channel = int(args[args.index("-A") + 1])
if channel not in (37, 38, 39):
    print(f"error: advertising channel index must be 37-39 (got {channel})", file=sys.stderr)
    sys.exit(1)
board = args[args.index("-U") + 1]
out = open(args[args.index("-q") + 1], "wb")
out.write(struct.pack("<IHHiIII", 0xa1b23c4d, 2, 4, 0, 0, 400,
                      192 if bug == "link-type" else 256))
if board not in os.environ.get("FAKE_BOARDS", "").split(","):
    print(f"error: no Ubertooth with serial {board}", file=sys.stderr)
    out.close()
    sys.exit(1)
rf = {37: 0, 38: 12, 39: 39}[channel]
if bug == "wrong-channel":
    rf = 0 if rf else 12
sent = 0
while not stop:
    if limit and sent >= limit:
        time.sleep(0.05)
        continue
    body = bytes([rf, 0xc4, 0x80, 0]) + struct.pack("<IH", 0x8e89bed6, 0x0817)
    body += struct.pack("<I", 0x8e89bed6) + bytes(9)
    if bug == "truncated" and sent == 3:
        out.write(struct.pack("<IIII", 1, 2, len(body), len(body)) + body[:5])
        out.flush()
        break
    out.write(struct.pack("<IIII", 1, 2, len(body), len(body)) + body)
    out.flush()
    sent += 1
    if bug == "exits" and sent == 3:
        sys.exit(1)
    time.sleep(0.05)
if bug == "ignores-term":
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    time.sleep(60)
out.close()
'''


def phdr_record(rf=0, access_address=0x8E89BED6, length=None, fraction=2):
    body = bytes([rf, 0xC4, 0x80, 0]) + struct.pack("<IH", 0x8E89BED6, 0x0817)
    body += struct.pack("<I", access_address) + bytes(9)
    incl = len(body) if length is None else length
    return struct.pack("<IIII", 1, fraction, incl, incl) + body


def header(link_type=256, magic=0xA1B23C4D):
    return struct.pack("<IHHiIII", magic, 2, 4, 0, 0, 400, link_type)


class ParseStreamTests(unittest.TestCase):

    def test_records_by_rf_channel(self):
        stream = extcapcapture.parse_stream(
            header() + phdr_record(12) * 3 + phdr_record(5, access_address=0x50654321))
        self.assertEqual([], stream.problems)
        self.assertEqual(256, stream.link_type)
        self.assertIn("(nanosecond), version 2.4, snaplen 400, link type 256", stream.header)
        self.assertEqual((4, {12: 3}, 1),
                         (stream.records, stream.advertising, stream.data_records))
        self.assertEqual("records: 4 (advertising 3: rf channel 12 x3; data 1)",
                         stream.summary())

    def test_empty_stream_has_no_header_and_no_problem(self):
        stream = extcapcapture.parse_stream(b"")
        self.assertEqual(("", None, []), (stream.header, stream.link_type, stream.problems))

    def test_bad_streams(self):
        cases = {
            "truncated PCAP global header": header()[:10],
            "not a PCAP magic": b"\x0a\x0d\x0d\x0a" + bytes(20),
            "link type 192, not 256": header(192),
            "truncated record header": header() + phdr_record()[:9],
            "truncated": header() + phdr_record()[:-3],
            "too short for the LE pseudo-header": header() + phdr_record(length=8),
            "RF channel 40 is not 0-39": header() + phdr_record(40),
            "sub-second field": header() + phdr_record(fraction=1_000_000_000),
            "more than the snaplen": header() + struct.pack("<IIII", 1, 2, 401, 401)
            + bytes(401),
        }
        for problem, data in cases.items():
            with self.subTest(problem):
                problems = extcapcapture.parse_stream(data).problems
                self.assertTrue(problems)
                self.assertIn(problem, problems[0])

    def test_microsecond_records_must_stay_below_a_second(self):
        data = header(magic=0xA1B2C3D4) + phdr_record(fraction=1_000_000)
        self.assertIn("not below 1000000", extcapcapture.parse_stream(data).problems[0])


class Fixture:
    """A fake ubertooth-btle on PATH and the extcap script in a temp dir."""

    def __init__(self, directory, script=EXTCAP):
        self.root = Path(directory)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        btle = bin_dir / "ubertooth-btle"
        btle.write_text(FAKE_BTLE.replace("PYTHON", sys.executable))
        btle.chmod(0o755)
        self.script = self.root / "btle-extcap.py"
        self.script.write_text(script)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.fifo = str(self.root / "extcap.fifo")

    def capture(self, extcap_args, *, bug="", boards=SERIAL_A, records=0, **options):
        out = io.StringIO()
        env = {"PATH": self.path, "FAKE_BTLE": bug, "FAKE_BOARDS": boards,
               "FAKE_RECORDS": str(records)}
        with mock.patch.dict(os.environ, env), redirect_stdout(out), \
                mock.patch("sys.stderr", io.StringIO()):
            code = extcapcapture.capture(
                (sys.executable, str(self.script), "--capture", f"--fifo={self.fifo}",
                 *extcap_args),
                self.fifo, options.pop("seconds", 0.5), out=print, **options)
        self.assertGone()
        return code, out.getvalue()

    def assertGone(self):
        assert not os.path.lexists(self.fifo), "the FIFO was left behind"


class CaptureTests(unittest.TestCase):

    def fixture(self, script=EXTCAP):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Fixture(directory.name, script)

    def test_capture_on_each_channel_passes(self):
        fixture = self.fixture()
        for channel, rf in ((37, 0), (38, 12), (39, 39)):
            with self.subTest(channel):
                code, out = fixture.capture(
                    (f"--extcap-interface=ubertooth{SERIAL_B}", f"--channel={channel}"),
                    boards=f"{SERIAL_A},{SERIAL_B}", board=SERIAL_B, channel=channel)
                self.assertEqual(0, code, out)
                self.assertRegex(out, rf"(?m)^ubertooth-btle command: \S*ubertooth-btle -f "
                                      rf"-U {SERIAL_B} -q \S+ -A {channel}$")
                self.assertIn("link type 256", out)
                self.assertRegex(out, rf"advertising \d+: rf channel {rf} x\d+")
                self.assertIn("extcap exit: 0 (after SIGTERM)", out)
                self.assertIn("processes left: none", out)
                self.assertTrue(out.endswith("capture checks passed\n"))

    def test_default_channel_is_37(self):
        code, out = self.fixture().capture(
            (f"--extcap-interface=ubertooth{SERIAL_A}",), board=SERIAL_A, channel=37)
        self.assertEqual(0, code, out)
        self.assertIn("-A 37", out)

    def test_a_stream_longer_than_max_bytes_fails(self):
        # 24-byte header plus 39-byte records: 10 records are 414 bytes.
        with mock.patch.object(extcapcapture, "MAX_BYTES", 200):
            code, out = self.fixture().capture(
                (f"--extcap-interface=ubertooth{SERIAL_A}",), records=10,
                board=SERIAL_A, channel=37, seconds=1.5)
        self.assertEqual(1, code, out)
        self.assertIn("FAIL: the FIFO carried 414 bytes, more than the 200 checked", out)
        self.assertNotIn("truncated", out)
        self.assertNotIn("capture checks passed", out)
        # The same stream exactly at the limit passes.
        with mock.patch.object(extcapcapture, "MAX_BYTES", 414):
            code, out = self.fixture().capture(
                (f"--extcap-interface=ubertooth{SERIAL_A}",), records=10,
                board=SERIAL_A, channel=37, seconds=1.5)
        self.assertEqual(0, code, out)
        self.assertIn("records: 10 ", out)

    def test_failures(self):
        fixture = self.fixture()
        cases = {
            "wrong-channel": "advertising records on RF channel(s) [0], not only 12",
            "link-type": "link type 192, not 256",
            "truncated": "truncated",
            "exits": "btle-extcap exited 0 before the stop",
        }
        for bug, failure in cases.items():
            with self.subTest(bug):
                code, out = fixture.capture(
                    (f"--extcap-interface=ubertooth{SERIAL_A}", "--channel=38"),
                    bug=bug, board=SERIAL_A, channel=38)
                self.assertEqual(1, code, out)
                self.assertRegex(out, rf"(?m)^FAIL: .*{re.escape(failure)}")
                self.assertNotIn("capture checks passed", out)

    def test_the_board_and_channel_must_reach_ubertooth_btle(self):
        fixture = self.fixture()
        code, out = fixture.capture((f"--extcap-interface=ubertooth{SERIAL_A}",),
                                    board=SERIAL_B, channel=38)
        self.assertEqual(1, code)
        self.assertIn("FAIL: ubertooth-btle was not given -A 38", out)
        self.assertIn(f"FAIL: ubertooth-btle was not given -U {SERIAL_B}", out)

    def test_a_child_that_ignores_sigterm_is_reported_and_stopped_with_sigterm(self):
        with mock.patch.object(extcapcapture, "STOP_WAIT_S", 1.0), \
                mock.patch.object(extcapcapture, "GONE_WAIT_S", 0.5), \
                mock.patch.object(extcapcapture, "DRAIN_S", 0.5):
            code, out = self.fixture().capture(
                (f"--extcap-interface=ubertooth{SERIAL_A}",), bug="ignores-term",
                board=SERIAL_A, channel=37)
        self.assertEqual(1, code)
        # The script waits on its child; the second SIGTERM (default action
        # now) ends the child, and then the script.
        self.assertIn("FAIL: btle-extcap did not exit within 1s of SIGTERM", out)
        self.assertIn("processes left: none", out)

    def test_refused_capture_at_fork_main_fails_strictly(self):
        fixture = self.fixture()
        for args in ((f"--extcap-interface=ubertooth{NO_BOARD}",),
                     (f"--extcap-interface=ubertooth{SERIAL_A}", "--channel=40")):
            with self.subTest(args):
                code, out = fixture.capture(args, refused=True)
                self.assertEqual(1, code, out)
                self.assertIn("extcap exit: 0 (before the stop)", out)
                self.assertIn("FAIL: btle-extcap exited 0 although its capture failed", out)

    def test_refused_capture_passes_once_the_exit_status_is_kept(self):
        fixture = self.fixture(FIXED_EXTCAP)
        for args in ((f"--extcap-interface=ubertooth{NO_BOARD}",),
                     (f"--extcap-interface=ubertooth{SERIAL_A}", "--channel=40")):
            with self.subTest(args):
                code, out = fixture.capture(args, refused=True)
                self.assertEqual(0, code, out)
                self.assertIn("extcap exit: 1 (before the stop)", out)
        code, out = fixture.capture((f"--extcap-interface=ubertooth{SERIAL_A}",),
                                    board=SERIAL_A, channel=37)
        self.assertEqual(0, code, out)

    def test_refused_mode_fails_a_capture_that_keeps_running(self):
        code, out = self.fixture().capture(
            (f"--extcap-interface=ubertooth{SERIAL_A}",), refused=True)
        self.assertEqual(1, code)
        self.assertIn("FAIL: btle-extcap was still capturing at the stop", out)
        self.assertIn("packet records were written", out)


class MainTests(unittest.TestCase):

    def test_main_runs_the_script_from_the_run_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            found = (sys.executable, EXTCAP.encode())
            out = io.StringIO()
            with mock.patch.object(pytools, "extcap_script", return_value=found), \
                    mock.patch.dict(os.environ, {"PATH": fixture.path,
                                                 "FAKE_BOARDS": SERIAL_A}), \
                    redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()):
                code = extcapcapture.main([
                    "--seconds", "1", "--fifo", fixture.fifo, "--board", SERIAL_A,
                    "--channel", "37", "--", "--capture",
                    f"--extcap-interface=ubertooth{SERIAL_A}", f"--fifo={fixture.fifo}"])
            self.assertEqual(0, code, out.getvalue())
            fixture.assertGone()

    def test_an_existing_fifo_path_is_refused(self):
        with tempfile.NamedTemporaryFile() as existing, \
                mock.patch("sys.stderr", io.StringIO()) as err:
            code = extcapcapture.main(["--seconds", "1", "--fifo", existing.name,
                                       "--", "--capture"])
        self.assertEqual(2, code)
        self.assertIn("already exists", err.getvalue())

    def test_runs_as_a_script(self):
        result = subprocess.run(
            (sys.executable, python_cases._EXTCAP_CAPTURE, "--seconds", "1", "--fifo",
             "/nonexistent/x", "--", "--capture"),
            capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, timeout=30)
        self.assertEqual(2, result.returncode)
        self.assertIn("are not set; run the case with run-suite", result.stderr)


class CaseTests(unittest.TestCase):

    def cases(self, coverage, guards=0):
        definition = builtin_catalog().get(TEST)
        self.assertTrue(definition.guards_other_ubertooths)
        identities = {"Ubertooth": {"serial": SERIAL}}
        identities.update({f"guard Ubertooth {i}": {"serial": f"{i:032x}"}
                           for i in range(1, guards + 1)})
        arguments = definition.normalize_arguments(
            {"coverage": coverage, "duration": "3s"})
        return {case.case_id: case for case in definition.command_cases(
            arguments, {}, identities)}

    def test_case_sets(self):
        self.assertEqual(["extcap-capture"], list(self.cases("smoke")))
        self.assertEqual(
            ["extcap-capture", "extcap-capture-37", "extcap-capture-38",
             "extcap-capture-39", "extcap-capture-no-board", "extcap-capture-bad-channel"],
            list(self.cases("comprehensive")))

    def test_capture_commands(self):
        cases = self.cases("comprehensive", guards=1)
        context = {"capture dir": "/run/c"}

        def capture_argv(case_id):
            step = {s.step_id: s for s in cases[case_id].steps}["capture"]
            self.assertTrue(step.mutates)
            self.assertGreater(step.timeout_s, 3)
            return step.resolve_argv(context)[2:]

        self.assertEqual(
            ("--seconds", "3", "--fifo", "/run/c/extcap.fifo", "--board", SERIAL,
             "--channel", "38", "--", "--capture", f"--extcap-interface=ubertooth{SERIAL}",
             "--fifo=/run/c/extcap.fifo", "--channel=38"),
            capture_argv("extcap-capture-38"))
        self.assertNotIn("--channel=37", capture_argv("extcap-capture"))
        self.assertIn(f"--extcap-interface=ubertooth{NO_BOARD}",
                      capture_argv("extcap-capture-no-board"))
        self.assertIn("--refused", capture_argv("extcap-capture-no-board"))
        self.assertIn("--channel=40", capture_argv("extcap-capture-bad-channel"))
        for case in cases.values():
            self.assertTrue(case.capture_dir)
            self.assertEqual("cleanup-stop", case.cleanup[0].step_id)

    def test_steps_and_the_no_board_serial(self):
        steps = [s.step_id for s in self.cases("smoke")["extcap-capture"].steps]
        self.assertNotIn("board-index", steps)
        self.assertRegex(NO_BOARD, "[a-f]")
        definition = builtin_catalog().get(TEST)
        arguments = definition.normalize_arguments({"coverage": "smoke", "duration": "3s"})
        with self.assertRaisesRegex(Exception, "is registered"):
            definition.command_cases(arguments, {}, {
                "Ubertooth": {"serial": SERIAL},
                "guard Ubertooth 1": {"serial": NO_BOARD.upper()}})


class ExtcapUbertooth(tt.FakeUbertooth):
    """FakeUbertooth plus the capture step, which runs
    extcapcapture.py for real against the fake ubertooth-btle; the capture
    puts the board in a mode, as ubertooth-btle -f does."""

    def __init__(self, fixture, fork, **kwargs):
        super().__init__(**kwargs)
        self.fixture = fixture
        self.fork = fork

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if command[:2] == (sys.executable, python_cases._EXTCAP_CAPTURE):
            self.commands.append(command)
            env = {"PATH": self.fixture.path, "FAKE_BOARDS": self.serial,
                   pytools.SOURCE_ENV: self.fork[0], pytools.COMMIT_ENV: self.fork[1]}
            result = subprocess.run(command, env=env, capture_output=True, text=True,
                                    timeout=timeout_s)
            self.mode = "btle"
            self.state.update(channel="2402")
            return self._result(command, result.returncode, result.stdout,
                                result.stderr, on_output)
        return super().run(argv, timeout_s=timeout_s, cancellation=cancellation,
                           on_output=on_output)


class SequencedCaptureTests(unittest.TestCase):

    def run_test(self, coverage):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            source = Path(directory) / "src"
            script = source / pytools.EXTCAP_PATH
            script.parent.mkdir(parents=True)
            script.write_text(EXTCAP)
            git("init", "-q", cwd=source)
            git("add", ".", cwd=source)
            git("commit", "-q", "-m", "fixture", cwd=source)
            fork = (str(source), git("rev-parse", "HEAD", cwd=source))
            device = ExtcapUbertooth(fixture, fork)
            original = tt.cases_for

            def with_duration(name, cov="smoke"):
                if name != TEST:
                    return original(name, cov)
                definition = builtin_catalog().get(name)
                arguments = definition.normalize_arguments({
                    "hosts": ["Host_Linux"], "Ubertooth": "default",
                    "coverage": cov, "duration": "1s"})
                return arguments, definition.command_cases(
                    arguments, {"Ubertooth": "UT_Linux0"}, tt.IDENTITIES)

            with mock.patch.object(tt, "cases_for", with_duration):
                summary, events = tt.run_test(TEST, device, coverage=coverage)
            return summary, events, device

    def test_smoke_passes_and_restores_the_board(self):
        summary, events, device = self.run_test("smoke")
        results = tt.case_results(events)
        self.assertEqual(["passed"], [event.status for event in results],
                         [event.details for event in results])
        self.assertEqual("2441", device.state["channel"])
        self.assertIsNone(device.mode)
        capture = [c for c in device.commands if c[:2] == (
            sys.executable, python_cases._EXTCAP_CAPTURE)]
        self.assertEqual(1, len(capture))
        self.assertIn(f"--extcap-interface=ubertooth{SERIAL}", capture[0])

    def test_comprehensive_fails_only_the_strict_cases(self):
        _summary, events, _device = self.run_test("comprehensive")
        failed = {event.details["case_id"].removeprefix(f"001-{TEST}-")
                  for event in tt.case_results(events) if event.status == "failed"}
        self.assertEqual({"extcap-capture-no-board", "extcap-capture-bad-channel"}, failed)


if __name__ == "__main__":
    unittest.main()
