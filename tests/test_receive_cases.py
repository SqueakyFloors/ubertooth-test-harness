import io
import os
import struct
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ubertooth_harness import capturefile, receive_cases, rxdump, stalecheck
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.stateful import StateCase
from tests.test_tool_cases import ToolUbertooth, failed_names
import tests.test_transitions as tt
from tests.test_transitions import SERIAL

FULL = rxdump.FULL_RECORD
BITS = rxdump.BITS_RECORD


def full_record(pkt_type=0, channel=39, host_time=None, clock=0):
    """One 68-byte record: host time, then usb_pkt_rx with clk100ns ``clock``."""
    host_time = int(time.time()) if host_time is None else host_time
    return (struct.pack(">I", host_time) + bytes([pkt_type, 0, channel, 0])
            + struct.pack("<I", clock) + bytes(56))


def le_pcap(*channels):
    """An LE PHDR pcap (link type 256) with one packet per RF channel."""
    data = tt.FakeUbertooth.PCAP_HEADER(capturefile.LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR)
    for channel in channels:
        body = bytes([channel]) + bytes(9) + bytes.fromhex("d6be898e") + bytes(11)
        data += struct.pack("<IIII", 0, 0, len(body), len(body)) + body
    return data


class ReceiveUbertooth(ToolUbertooth):
    """ToolUbertooth plus ubertooth-rx, -afh, -dump, -ego, and debug MDMCTRL.

    Each run follows the fork source; ``bugs`` switches on a misbehavior so
    each case can be shown to fail:

    rx-ignores-timeout, rx-exits-early, rx-ignores-channel, rx-ignores-errors,
    rx-link-type, rx-writes-extra, rx-dump-trailing, rx-sweep-notice (-s
    prints the notice it printed before 5ac22a8), rx-sweep-overrides-channel
    (-s sweeps although -c names a channel),
    rx-no-survey, afh-zero-map (#187), afh-no-message, afh-exits-0, afh-exits-early,
    afh-r-silent, afh-segfault (#185),
    dump-trailing, dump-bits-garbage, dump-ignores-l, dump-first-wins,
    dump-stalls, ego-exits, ego-ignores-channel, ego-ignores-length,
    ego-accepts-bad-ac, reject:<tool>:<option>; fork #318: stale-le-into-dump
    (a dump starts with two LE records), stale-br-into-dump (with a BR record
    and a keep-alive from the previous dump), stale-into-ble (a btle -q
    capture holds a basic-rate packet, RF channel 19).
    """

    def __init__(self, *args, bugs=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.bugs = set(bugs)
        self.mdmctrl = 0x0029

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if command[1:2] == (receive_cases._DUMP_CHECKER,):
            out = io.StringIO()
            with redirect_stdout(out):
                code = rxdump.main(list(command[2:]))
            return self._result(command, code, out.getvalue(), "", on_output)
        if command[1:2] == (receive_cases._STALE_CHECKER,):
            out = io.StringIO()
            with redirect_stdout(out):
                code = stalecheck.main(list(command[2:]))
            return self._result(command, code, out.getvalue(), "", on_output)
        if command[1:2] == (receive_cases._CAPTURE_CHECKER,):
            command = (command[0], tt.transitions._CAPTURE_CHECKER) + command[2:]
            return super().run(command, timeout_s=timeout_s, cancellation=cancellation,
                               on_output=on_output)
        tool = command[0]
        handler = {
            "ubertooth-rx": self._rx, "ubertooth-afh": self._afh,
            "ubertooth-dump": self._dump, "ubertooth-ego": self._ego,
        }.get(tool)
        if tool == "ubertooth-debug" and "0x03" in command:
            self.commands.append(command)
            return self._result(command, 0, f"%MDMCTRL = 0x{self.mdmctrl:04X}\n", "", on_output)
        if handler is None:
            return super().run(argv, timeout_s=timeout_s, cancellation=cancellation,
                               on_output=on_output)
        self.commands.append(command)
        serial = command[command.index("-U") + 1]
        options = list(command[3:])
        rejected = [o for o in options if f"reject:{tool}:{o}" in self.bugs]
        if rejected:
            return self._result(command, 1, "usage\n", "", on_output)
        return handler(command, serial, options, timeout_s, on_output)

    def _btle(self, command, options, on_output):
        result = super()._btle(command, options, on_output)
        if "-n" in options and "-q" in options:
            # Live advertising packets on RF channel 0 (index 37).
            channels = (0, 19, 0) if "stale-into-ble" in self.bugs else (0, 0)
            Path(self._value(list(options), "-q")).write_bytes(le_pcap(*channels))
        return result

    def _timed(self, command, code, stdout, stderr, duration, on_output, timed_out=False):
        if on_output and stdout:
            on_output("stdout", stdout)
        return CommandResult(command, code, stdout, stderr, duration, timed_out)

    def _value(self, options, flag, default=None):
        return options[options.index(flag) + 1] if flag in options else default

    def _run_mode(self, channel=None):
        self.mode = "rx"
        if channel is not None:
            self.state["channel"] = channel
        self._stop_mode()  # the tool's ubertooth_stop()

    # ubertooth-rx --------------------------------------------------------
    def _rx(self, command, serial, options, timeout_s, on_output):
        seconds = int(self._value(options, "-t", "0"))
        channel = self._value(options, "-c")
        errors = int(self._value(options, "-e", "1"))
        if "rx-ignores-errors" in self.bugs:
            errors = 4
        lines = []
        if "-s" in options and "rx-sweep-overrides-channel" in self.bugs:
            channel = None
        for index, ch in enumerate((10, 40, 70)):
            if channel is not None and "rx-ignores-channel" not in self.bugs:
                ch = int(channel)
            lines.append(f"systime=1 ch={ch:2d} LAP=9e8b33 err={min(index, errors)} clkn=1 "
                         "clk_offset=1 s=-40 n=-55 snr=15")
        lines.append("UAP = 0x47 found after 3 total packets.")
        if "-z" in options and "rx-no-survey" not in self.bugs:
            lines += ["Survey Results", "??:??:??:9E:8B:33", "??:??:47:9E:8B:33"]
        captures = {}
        for index, option in enumerate(options):
            if option in ("-r", "-q", "-d"):
                path = options[index + 1]
                handle = option
                if handle in captures and "rx-writes-extra" not in self.bugs:
                    lines.insert(0, f"Ignoring extra capture file: {path}")
                    continue
                captures[handle] = path
                link = 1 if "rx-link-type" in self.bugs else 255
                if option == "-r":
                    data = self._pcapng_header(link)
                elif option == "-q":
                    data = self.PCAP_HEADER(link)
                else:
                    data = full_record(channel=10) * 3
                    if "rx-dump-trailing" in self.bugs:
                        data += b"\0\0\0"
                Path(path).write_bytes(data)
        stderr = ""
        if "-s" in options and "rx-sweep-notice" in self.bugs:
            stderr = "sweep mode is now the default and the -s argument is deprecated\n"
        self._run_mode("2402" if channel is None else str(2402 + int(channel)))
        stdout = "\n".join(lines) + "\n"
        if "rx-ignores-timeout" in self.bugs or seconds == 0:
            return self._timed(command, 124, stdout, stderr + "HARD TIMEOUT\n", timeout_s,
                               on_output, timed_out=True)
        duration = 0.2 if "rx-exits-early" in self.bugs else seconds + 0.3
        return self._timed(command, 0, stdout, stderr, duration, on_output)

    # ubertooth-afh -------------------------------------------------------
    def _afh(self, command, serial, options, timeout_s, on_output):
        """fork 3b22c95: -t N with no packet from the piconet (the bench has
        none) says so on stderr and exits 2 after N s; -r prints a line per
        second until signalled."""
        if serial != self.serial:
            if "afh-segfault" in self.bugs:
                return self._result(command, -11, "",
                                    f"no Ubertooth found with serial matching '{serial}'\n",
                                    on_output)
            return self._result(command, 1, "",
                                f"no Ubertooth found with serial matching '{serial}'\n",
                                on_output)
        self._run_mode("2402")
        if "-r" in options:
            count = 0 if "afh-r-silent" in self.bugs else int(timeout_s)
            out = "".join(f"{1792838727 + i} {'0' * 80}\n" for i in range(count))
            return self._timed(command, 124, out, "HARD TIMEOUT\n", timeout_s, on_output,
                               timed_out=True)
        seconds = int(self._value(options, "-t"))
        if "afh-zero-map" in self.bugs:
            # #187, fork builds before 3b22c95: an all-zero map, then monitoring.
            return self._timed(command, 124, "AFH map: 0x00000000000000000000\n",
                               "HARD TIMEOUT\n", timeout_s, on_output, timed_out=True)
        stderr = ("error: no packets from ??:??:22:44:66:88 within "
                  f"{seconds} s; AFH map unknown, not monitoring\n")
        if "afh-no-message" in self.bugs:
            stderr = ""
        code = 0 if "afh-exits-0" in self.bugs else 2
        duration = 0.2 if "afh-exits-early" in self.bugs else seconds + 0.3
        return self._timed(command, code, "", stderr, duration, on_output)

    # ubertooth-dump ------------------------------------------------------
    def _dump(self, command, serial, options, timeout_s, on_output):
        modulations = [o for o in options if o in ("-c", "-l")]
        if "dump-first-wins" in self.bugs:
            modulations = modulations[:1]
        if "dump-ignores-l" in self.bugs:
            modulations = [m for m in modulations if m != "-l"]
        le = bool(modulations) and modulations[-1] == "-l"
        self.mdmctrl = 0x0040 if le else 0x0029
        records = 5 if "dump-stalls" in self.bugs else 400 * int(timeout_s)
        if "-b" in options:
            record = b"0110" * 100 + b"\n"
            if "dump-bits-garbage" in self.bugs:
                record = b"2" * 400 + b"\n"
            data = record * records
        else:
            # One host time for the whole file: reading the clock per record
            # let the prepended records below land in a later second than
            # the ones after them, which rxdump.py rightly reports as a host
            # time going backwards.
            now = int(time.time())
            data = b"".join(full_record(host_time=now, clock=5000 + 4000 * i)
                            for i in range(records - 1))
            data = full_record(pkt_type=3, host_time=now) + data
            if "stale-le-into-dump" in self.bugs:
                data = (full_record(pkt_type=1, channel=0, host_time=now, clock=209427)
                        + full_record(pkt_type=1, channel=0, host_time=now, clock=19287773)
                        + data)
            if "stale-br-into-dump" in self.bugs:
                data = (full_record(host_time=now, clock=19107111)
                        + full_record(pkt_type=3, host_time=now) + data)
        if "dump-trailing" in self.bugs:
            data += b"\0"
        Path(self._value(options, "-d")).write_bytes(data)
        self._run_mode()
        return self._timed(command, 124, "", "rx block timestamp 1 * 100 nanoseconds\n"
                           "HARD TIMEOUT\n", timeout_s, on_output, timed_out=True)

    # ubertooth-ego -------------------------------------------------------
    def _ego(self, command, serial, options, timeout_s, on_output):
        code = self._value(options, "-a")
        if code is not None and not all(c in "0123456789abcdef" for c in code[:2].lower()) \
                and "ego-accepts-bad-ac" not in self.bugs:
            return self._result(command, 1, "error: access code must be 2, 4, 6 or 8 hex digits (got zz)\n", "", on_output)
        if "ego-exits" in self.bugs:
            return self._result(command, 1, "Error: E-GO not supported by this firmware\n", "",
                                on_output)
        frequency = int(self._value(options, "-c", "2418"))
        if "ego-ignores-channel" in self.bugs:
            frequency = 2418
        length = int(self._value(options, "-l", "18"))
        if "ego-ignores-length" in self.bugs:
            length = 18
        self._run_mode(str(frequency))
        out = (f"time=100 delta_t=0.010000 ms freq={frequency} \n"
               + " ".join(["ab"] * length) + " \n\n")
        return self._timed(command, 124, out, "HARD TIMEOUT\n", timeout_s, on_output,
                           timed_out=True)


def cases_for(test_name, coverage, duration="2s"):
    definition = builtin_catalog().get(test_name)
    arguments = definition.normalize_arguments(
        {"hosts": ["Host_Linux"], "Ubertooth": "default", "coverage": coverage,
         "duration": duration})
    return arguments, definition.command_cases(
        arguments, {"Ubertooth": "UT_Linux0"}, {"Ubertooth": {"serial": SERIAL}})


def run(test_name, device, coverage="comprehensive"):
    original = tt.cases_for

    def with_duration(name, cov="smoke"):
        if name == test_name:
            return cases_for(name, cov)
        return original(name, cov)

    with mock.patch.object(tt, "cases_for", with_duration):
        return tt.run_test(test_name, device, coverage=coverage)


CLASSIC = "Classic Bluetooth receive"
RAW = "Raw receive"


class ReceiveCaseGenerationTests(unittest.TestCase):

    def test_smoke_and_comprehensive_case_sets(self):
        _a, smoke = cases_for(CLASSIC, "smoke")
        self.assertEqual(["rx-survey-timeout", "rx-capture-pcapng", "afh-timeout-no-packets"],
                         [c.case_id for c in smoke])
        _a, full = cases_for(CLASSIC, "comprehensive")
        self.assertEqual(15, len(full))
        _a, smoke = cases_for(RAW, "smoke")
        self.assertEqual(["dump-full", "dump-bitstream", "ego-rx"], [c.case_id for c in smoke])
        _a, full = cases_for(RAW, "comprehensive")
        self.assertEqual(11, len(full))
        self.assertEqual("stale-packets-across-mode-change", full[-1].case_id)
        self.assertTrue(all(isinstance(c, StateCase) for c in full))

    def test_every_case_restores_the_mode_reset_settings_and_selects_the_board(self):
        context = {"original channel": "2441", "original squelch": "-90",
                   "original leds": "0/0/0", "capture dir": "/x", "original crc": "1"}
        for test in (CLASSIC, RAW):
            _a, full = cases_for(test, "comprehensive")
            for case in full:
                ids = [s.step_id for s in case.cleanup]
                with self.subTest(case.case_id):
                    self.assertIn("verify-restored-channel", ids)
                    self.assertIn("verify-restored-squelch", ids)
                    self.assertIn("verify-restored-leds", ids)
                for step in case.steps + case.cleanup:
                    argv = step.resolve_argv(context)
                    if "-U" in argv and case.case_id != "afh-board-not-found":
                        self.assertEqual(SERIAL, argv[argv.index("-U") + 1])

    def test_files_stay_in_the_capture_directory(self):
        for test in (CLASSIC, RAW):
            _a, full = cases_for(test, "comprehensive")
            for case in full:
                for step in case.steps:
                    argv = step.resolve_argv({"original channel": "2441",
                                              "original squelch": "-90",
                                              "original leds": "0/0/0",
                                              "capture dir": "/run/captures/x"})
                    paths = [a for a in argv if a.startswith("/") and a != sys.executable
                             and not a.endswith(".py")]
                    for path in paths:
                        self.assertTrue(path.startswith("/run/captures/x/"), (case.case_id, path))
                        self.assertTrue(case.capture_dir, case.case_id)

    def test_rx_timeout_bounds(self):
        _a, smoke = cases_for(CLASSIC, "smoke", duration="1500ms")
        step = smoke[0].steps[-1]
        self.assertIn("-t", step.resolve_argv({}))
        self.assertEqual("2", step.resolve_argv({})[step.resolve_argv({}).index("-t") + 1])
        self.assertEqual(1.5, step.min_duration_s)
        self.assertEqual(7.0, step.timeout_s)


class ClassicReceiveTests(unittest.TestCase):

    def test_passes_on_a_correct_tool(self):
        device = ReceiveUbertooth(crc="1")
        summary, events = run(CLASSIC, device)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual("2441", device.state["channel"])
        self.assertEqual("-90", device.state["squelch"])
        smoke, _e = run(CLASSIC, ReceiveUbertooth(crc="1", bugs={"afh-segfault"}),
                        coverage="smoke")
        self.assertTrue(smoke.passed, smoke.failure_reason)

    def test_each_case_can_fail(self):
        rx = ["Survey (-z -t) ends by itself", "PCAPNG capture (-r)",
              "Sniff all LAPs (-t) ends by itself", "Fixed channel (-c 39)",
              "Explicit sweep (-s)", "-s -c 39: the channel wins",
              "No access-code errors (-e 0)", "Four access-code errors (-e 4)",
              "PCAP capture (-q)",
              "Repeated -r: the first file is written, the extra ignored",
              "Packet dump (-d)"]
        expectations = {
            # Every ubertooth-rx case checks that -t ends the run on time.
            "rx-ignores-timeout": rx,
            "rx-exits-early": rx,
            "rx-no-survey": ["Survey (-z -t) ends by itself"],
            "rx-link-type": ["PCAPNG capture (-r)", "PCAP capture (-q)",
                             "Repeated -r: the first file is written, the extra ignored"],
            "rx-writes-extra": ["Repeated -r: the first file is written, the extra ignored"],
            "rx-dump-trailing": ["Packet dump (-d)"],
            "rx-ignores-channel": ["Fixed channel (-c 39)", "-s -c 39: the channel wins"],
            "rx-sweep-notice": ["Explicit sweep (-s)", "-s -c 39: the channel wins"],
            "rx-sweep-overrides-channel": ["-s -c 39: the channel wins"],
            "reject:ubertooth-rx:-s": ["Explicit sweep (-s)", "-s -c 39: the channel wins"],
            "rx-ignores-errors": ["No access-code errors (-e 0)"],
            "reject:ubertooth-rx:-e": ["No access-code errors (-e 0)",
                                       "Four access-code errors (-e 4)"],
            # #187 (fork builds before 3b22c95) and regressions of its fix.
            "afh-zero-map": ["AFH -t with no piconet in range", "AFH with -e 2 accepted"],
            "afh-no-message": ["AFH -t with no piconet in range", "AFH with -e 2 accepted"],
            "afh-exits-0": ["AFH -t with no piconet in range", "AFH with -e 2 accepted"],
            "afh-exits-early": ["AFH -t with no piconet in range", "AFH with -e 2 accepted"],
            "reject:ubertooth-afh:-e": ["AFH with -e 2 accepted"],
            "afh-r-silent": ["AFH map every second (-r)"],
            "afh-segfault": ["AFH with an unknown -U exits 1"],
        }
        for bug, names in expectations.items():
            with self.subTest(bug):
                summary, events = run(CLASSIC, ReceiveUbertooth(crc="1", bugs={bug}))
                self.assertFalse(summary.passed)
                self.assertEqual(sorted(names), failed_names(events))


class RawReceiveTests(unittest.TestCase):

    def test_passes_on_a_correct_tool(self):
        device = ReceiveUbertooth(crc="1")
        summary, _events = run(RAW, device)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual("2441", device.state["channel"])

    def test_each_case_can_fail(self):
        dumps = ["Full records to a file (-d)", "LE modulation (-l)",
                 "-l -c: the last (-c) wins", "-c -l: the last (-l) wins"]
        egos = ["E-GO continuous receive (-r -c 2440)", "E-GO capture length -l 1",
                "E-GO capture length -l 48", "E-GO access code -a 630f9ffe"]
        stale = "No packets from the previous mode, BLE and BR alternated 5 times"
        expectations = {
            # a partial last bitstream record is ignored (no fflush)
            "dump-trailing": dumps + [stale],
            # fork #318: LE records also fail every type-checked dump case;
            # basic-rate ones and a stale capture only the stale case.
            "stale-le-into-dump": dumps + [stale],
            "stale-br-into-dump": [stale],
            "stale-into-ble": [stale],
            "dump-stalls": dumps + ["Bitstream to a file (-b -d)"],
            "dump-bits-garbage": ["Bitstream to a file (-b -d)"],
            "dump-ignores-l": ["LE modulation (-l)", "-c -l: the last (-l) wins"],
            "dump-first-wins": ["-l -c: the last (-c) wins", "-c -l: the last (-l) wins"],
            "ego-exits": egos,
            "ego-ignores-channel": egos,
            "ego-ignores-length": ["E-GO capture length -l 1", "E-GO capture length -l 48"],
            "reject:ubertooth-ego:-a": ["E-GO access code -a 630f9ffe", "E-GO rejects -a zz"],
            "ego-accepts-bad-ac": ["E-GO rejects -a zz"],
        }
        for bug, names in expectations.items():
            with self.subTest(bug):
                summary, events = run(RAW, ReceiveUbertooth(crc="1", bugs={bug}))
                self.assertFalse(summary.passed)
                self.assertEqual(sorted(names), failed_names(events))

    def test_dump_files_survive_a_second_boundary(self):
        # A fake dump read the wall clock per record, so a second
        # boundary between records put a later host time before an earlier
        # one and "Full records to a file (-d)" failed at random. A clock
        # that ticks 1 ms per call crosses several boundaries in every dump.
        start = float(int(time.time()))
        ticks = iter(range(10 ** 9))
        with mock.patch.object(time, "time", lambda: start + next(ticks) / 1000):
            summary, _events = run(RAW, ReceiveUbertooth(crc="1"))
        self.assertTrue(summary.passed, summary.failure_reason)


class StaleCaseTests(unittest.TestCase):

    def test_case_alternates_bounded_streams(self):
        _a, full = cases_for(RAW, "comprehensive")
        case = full[-1]
        context = {"capture dir": "/c", "original crc": "1"}
        streams = [s for s in case.steps if s.continuous]
        self.assertEqual(1 + 2 * receive_cases.STALE_ITERATIONS, len(streams))
        tools = [s.resolve_argv(context)[0] for s in streams]
        self.assertEqual(["ubertooth-btle"] + ["ubertooth-dump", "ubertooth-btle"] * 5, tools)
        for step in streams:
            self.assertEqual(2.0, step.timeout_s)
        argv = streams[2].resolve_argv(context)
        self.assertEqual(("ubertooth-btle", "-U", SERIAL, "-n", "-A37", "-q",
                          "/c/stale-1.pcap"), argv)
        ids = [s.step_id for s in case.steps]
        # Every stream is followed by a stop before the next mode starts.
        for index, step in enumerate(case.steps):
            if step.continuous:
                self.assertTrue(ids[index + 1].startswith("stop-"), ids[index + 1])
        self.assertIn("read-original-crc", ids)
        self.assertIn("verify-restored-crc", [s.step_id for s in case.cleanup])

    def test_failure_reports_the_per_iteration_counts(self):
        summary, events = run(RAW, ReceiveUbertooth(crc="1", bugs={"stale-br-into-dump"}))
        self.assertFalse(summary.passed)
        [event] = tt.steps(events, step_id="verify-no-stale")
        self.assertEqual("BR dump 0/5, BLE capture 0/5", event.details["expected"])
        self.assertEqual("BR dump 5/5, BLE capture 0/5", event.details["observed"])
        summary, events = run(RAW, ReceiveUbertooth(crc="1", bugs={"stale-into-ble"}))
        [event] = tt.steps(events, step_id="verify-no-stale")
        self.assertEqual("BR dump 0/5, BLE capture 5/5", event.details["observed"])


class CheckerTests(unittest.TestCase):

    def test_stale_records(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "dump.bin"
            path.write_bytes(full_record(pkt_type=3) + full_record(clock=5)
                             + full_record(clock=9) + full_record(pkt_type=3, clock=1))
            self.assertEqual((4, 0, 0), rxdump.stale_records(path))
            path.write_bytes(full_record(pkt_type=1, clock=900) + full_record(pkt_type=1, clock=99)
                             + full_record(clock=5) + full_record(clock=9))
            self.assertEqual((4, 2, 2), rxdump.stale_records(path))
            path.write_bytes(full_record(clock=900) + full_record(pkt_type=3)
                             + full_record(clock=5) + full_record(pkt_type=1, clock=9))
            self.assertEqual((4, 3, 1), rxdump.stale_records(path))
            path.write_bytes(b"")
            self.assertEqual((0, 0, 0), rxdump.stale_records(path))
            path.write_bytes(full_record() + b"x")
            with self.assertRaises(ValueError):
                rxdump.stale_records(path)

    def test_le_rf_channels(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.pcap"
            path.write_bytes(le_pcap(0, 19, 0))
            self.assertEqual([0, 19, 0], capturefile.le_rf_channels(path))
            path.write_bytes(le_pcap())
            self.assertEqual([], capturefile.le_rf_channels(path))
            path.write_bytes(le_pcap(0)[:-3])
            with self.assertRaises(capturefile.CaptureFileError):
                capturefile.le_rf_channels(path)
            path.write_bytes(tt.FakeUbertooth.PCAP_HEADER(192))
            with self.assertRaises(capturefile.CaptureFileError):
                capturefile.le_rf_channels(path)

    def test_stalecheck(self):
        with tempfile.TemporaryDirectory() as d:
            clean, stale = Path(d) / "a.bin", Path(d) / "b.bin"
            clean.write_bytes(full_record(clock=5) + full_record(clock=9))
            stale.write_bytes(full_record(pkt_type=1, clock=99) + full_record(clock=5))
            good, bad = Path(d) / "a.pcap", Path(d) / "b.pcap"
            good.write_bytes(le_pcap(0, 0))
            bad.write_bytes(le_pcap(19, 0))

            def check(*args):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = stalecheck.main([str(a) for a in args])
                return code, out.getvalue()

            code, out = check("--dumps", clean, stale, "--captures", good, bad)
            self.assertEqual(0, code)
            self.assertEqual(
                "Stale packets: BR dump after BLE: 0, 1 records (LE: 0, 1); BLE capture "
                "after BR dump: 0, 1 packets; stale iterations: BR dump 1/2, "
                "BLE capture 1/2\n", out)
            other = Path(d) / "c.pcap"
            other.write_bytes(le_pcap(19, 19))
            code, out = check("--dumps", clean, "--captures", other, "--channel", "19")
            self.assertIn("BR dump 0/1, BLE capture 0/1", out)
            code, out = check("--dumps", Path(d) / "missing.bin")
            self.assertEqual((1, True), (code, "does not exist" in out))

    def check(self, data, fmt, *args, mtime=None):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "dump.bin"
            path.write_bytes(data)
            if mtime is not None:
                os.utime(path, (mtime, mtime))
            out = io.StringIO()
            with redirect_stdout(out):
                code = rxdump.main([str(path), fmt, *args])
        return code, out.getvalue()

    def test_full_records(self):
        now = int(time.time())
        self.assertEqual((0, "Receive dump: 2 full records\n"),
                         self.check(full_record(host_time=now) * 2, "full"))
        self.assertEqual(0, self.check(b"", "full")[0])
        code, out = self.check(full_record() + b"x", "full")
        self.assertEqual(1, code)
        self.assertIn("1 trailing bytes", out)
        self.assertIn("packet type 3", self.check(full_record(pkt_type=3), "full",
                                                  "--types", "br")[1])
        self.assertIn("channel index 79", self.check(full_record(channel=79), "full")[1])
        self.assertIn("not near the file time", self.check(full_record(host_time=5), "full")[1])
        self.assertIn("goes backwards", self.check(
            full_record(host_time=now) + full_record(host_time=now - 1), "full")[1])
        self.assertIn("at least 3", self.check(full_record(), "full", "--min-records", "3")[1])

    def test_bit_records(self):
        self.assertEqual(0, self.check((b"01" * 200 + b"\n") * 3, "bits")[0])
        self.assertIn("not 400 symbols", self.check(b"2" * 400 + b"\n", "bits")[1])
        self.assertIn("not 400 symbols", self.check(b"0" * 401, "bits")[1])

    def test_missing_file(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(1, rxdump.main(["/nonexistent/dump.bin", "full"]))
        self.assertIn("does not exist", out.getvalue())

    def test_packet_check(self):
        line = "systime=1 ch={ch:2d} LAP=9e8b33 err={err} clkn=1 clk_offset=1 s=-4 n=-5 snr=1"
        out = line.format(ch=39, err=0) + "\n" + line.format(ch=5, err=2) + "\n"
        self.assertIn("channel 5", receive_cases.packet_check(channel=39)(out))
        self.assertIn("2 access-code errors", receive_cases.packet_check(max_errors=0)(out))
        self.assertEqual("", receive_cases.packet_check(max_errors=2)(out))
        survey = receive_cases.packet_check(survey=True)
        self.assertEqual("", survey("Survey Results\n??:??:47:9E:8B:33\n"))
        self.assertEqual("", survey("Survey Results\n"))
        self.assertIn("not piconet addresses", survey("Survey Results\n9e8b33\n"))
        self.assertIn("no 'Survey Results'", survey(""))

    def test_afh_no_packets_message(self):
        # The fork's own message for -l 446688 -u 22 -t 3 (ubertooth-afh.c).
        line = ("error: no packets from ??:??:22:44:66:88 within 3 s; AFH map unknown, "
                "not monitoring")
        self.assertRegex(line, receive_cases.afh_no_packets_pattern(3))
        self.assertNotRegex(line, receive_cases.afh_no_packets_pattern(2))
        _a, smoke = cases_for(CLASSIC, "smoke", duration="3s")
        step = next(c for c in smoke if c.case_id == "afh-timeout-no-packets").steps[-2]
        self.assertEqual((2,), step.expected_returncodes)
        self.assertFalse(step.continuous)
        self.assertEqual(2.5, step.min_duration_s)
        self.assertEqual(8.0, step.timeout_s)

    def test_afh_r_and_ego_checks(self):
        check = receive_cases.afh_r_check(2)
        self.assertEqual("", check(f"1 {'0' * 80}\n2 {'1' * 80}\n"))
        self.assertIn("expected at least 2", check(f"1 {'0' * 80}\n"))
        self.assertIn("not '<time> <map>'", check(f"1 {'0' * 80}\n2 {'0' * 80}\nAFH map: 0x00\n"))
        ego = receive_cases.ego_check(2440, 2)
        self.assertEqual("", ego(""))
        self.assertEqual("", ego("time=1 delta_t=0.100000 ms freq=2440 \nab cd \n\n"))
        self.assertIn("2418 MHz", ego("time=1 delta_t=0.100000 ms freq=2418 \nab cd \n"))
        self.assertIn("3 bytes", ego("time=1 delta_t=0.100000 ms freq=2440 \nab cd ef \n"))

    def test_capture_header_rejects_the_le_link_type(self):
        _a, smoke = cases_for(CLASSIC, "smoke")
        verify = smoke[1].steps[-1]
        result = CommandResult((), 0, "Capture format: pcapng, link type: 256\n", "", 0.1, False)
        self.assertIn("expected", verify.evaluate(result, {}).failure)


if __name__ == "__main__":
    unittest.main()
