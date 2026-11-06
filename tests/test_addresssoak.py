"""addresssoak.py: the two readers, the schedule, the restarts, the verdict.

Nothing here touches hardware. The advertiser is the fake DK shell every
nrfdk.py test uses, injected into the driver; the capture tool is a real
child process -- a fake `ubertooth-btle` on PATH that writes real PCAP
records and prints the text libbtbb prints -- because what the driver has to
get right about it is signals, exits and a file growing under it, and a model
of a process would not test any of that. The records and the text come from
tests/fake_advertiser.py, built from the spec encoding, so no captured
traffic is used.

The runs here are seconds long, with the same figures a plan carries, so the
whole loop is exercised: the schedule, both readers, the windows, the gaps,
the liveness timeout, the restart ladder, the probe, the file rotation, and
the summary a long run is reviewed through.
"""

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ubertooth_harness import addresssoak, nrfdk
from tests import fake_advertiser
from tests.executables import write_executable
from tests.fake_dk import FakeFirmware, FakePort
from tests import watchdog_samples

ROOT = Path(__file__).resolve().parents[1]
SERIAL = "0123456789abcdef0123456789abcdef"
ADDRESS = "C3:11:22:33:44:55"

# A fake `ubertooth-btle`. It writes a PCAP exactly as the real tool's -q
# does and prints the text block libbtbb prints for the same packet, so both
# of the driver's readers have real work to do. The environment shapes what
# it does; every variable is set by one test below.
FAKE_BTLE = f"""#!/usr/bin/env python3
import os, signal, struct, sys, time
sys.path.insert(0, {str(ROOT)!r})
from tests import fake_advertiser

address = os.environ.get("FAKE_BTLE_ADDRESS", {ADDRESS!r})
rate = float(os.environ.get("FAKE_BTLE_RATE", "20"))
mode = os.environ.get("FAKE_BTLE_MODE", "both")
state = os.environ.get("FAKE_BTLE_STATE", "")
exit_after = float(os.environ.get("FAKE_BTLE_EXIT_AFTER", "0"))
exit_code = int(os.environ.get("FAKE_BTLE_EXIT_CODE", "0"))
pad = int(os.environ.get("FAKE_BTLE_PAD_BYTES", "0"))
# Stamp the records this far from the host clock, which is what the board's
# own clock drifting (or losing a wrap) does over a long capture.
offset = float(os.environ.get("FAKE_BTLE_TIME_OFFSET", "0"))
# Write one record header claiming an impossible length this many seconds in,
# which is a capture file that cannot be read past that point.
bad_record_after = float(os.environ.get("FAKE_BTLE_BAD_RECORD_AFTER", "0"))
wrote_bad_record = False
# Write half a record header and stop, which is a capture file whose last
# record never arrived.
partial_tail = bool(os.environ.get("FAKE_BTLE_PARTIAL_TAIL"))
if partial_tail:
    def _leave_partial(*_a):
        handle.write(b"\x01\x02\x03\x04\x05\x06")
        handle.flush()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _leave_partial)
if os.environ.get("FAKE_BTLE_IGNORE_SIGTERM"):
    signal.signal(signal.SIGTERM, lambda *a: None)

# With FAKE_BTLE_ONCE, only the first run of this tool produces anything:
# every later one opens its capture file and then stays quiet, which is a
# restart that did not bring the capture back.
once = os.environ.get("FAKE_BTLE_ONCE", "")
silent = False
if once:
    silent = os.path.exists(once)
    open(once, "a").close()

path = sys.argv[sys.argv.index("-q") + 1]
handle = open(path, "wb", buffering=0)
handle.write(fake_advertiser.PCAP_HEADER)
started = time.time()
counter = 0
while True:
    if exit_after and time.time() - started >= exit_after:
        if exit_code:
            print("error: the capture stopped: libusb returned -1", flush=True)
        sys.exit(exit_code)
    advertising = not silent
    if state and advertising:
        try:
            advertising = open(state).read().strip() == "on"
        except OSError:
            advertising = False
    if advertising:
        counter += 1
        now = time.time()
        if mode in ("both", "records", "badcrc"):
            data = fake_advertiser.record(
                address, counter=counter, valid_crc=mode != "badcrc", pad=pad)
            # After the first couple of seconds, which is a clock that
            # stepped rather than one that was always offset: an anchor
            # absorbs a constant offset, and a step is the fault.
            if (bad_record_after and not wrote_bad_record
                    and now - started >= bad_record_after):
                wrote_bad_record = True
                handle.write(struct.pack("<IIII", int(now), 0, 1 << 30, 1 << 30))
            stamp = now + (offset if now - started > 2 else 0)
            handle.write(fake_advertiser.pcap_record(
                data, int(stamp), int((stamp % 1) * 1e9)))
        if mode in ("both", "text", "badcrc"):
            sys.stdout.write(fake_advertiser.text_block(address))
            sys.stdout.flush()
    time.sleep(1.0 / rate)
"""

# A fake `ubertooth-util`: the reads the driver makes, and the watchdog
# report, with the real tools' wording.
FAKE_UTIL = f"""#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_UTIL_BUSY"):
    sys.stderr.write({watchdog_samples.BOARD_BUSY.format(serial=SERIAL)!r})
    sys.exit(1)
options = sys.argv[1:]
if "-w" in options:
    sys.stdout.write({watchdog_samples.FULL!r})
elif "-v" in options:
    sys.stdout.write("Firmware version: git-abc1234 (API 1.54)\\n")
elif "-s" in options:
    sys.stdout.write("Serial No: " + {SERIAL!r} + "\\n")
elif "-C" in options:
    sys.stdout.write("Channel: 2441 MHz\\n")
elif "-z" in options:
    sys.stdout.write("Squelch: -120\\n")
elif "-d" in options:
    sys.stdout.write("USR LED: 0\\nRX LED: 0\\nTX LED: 0\\n")
elif "-r" in options:
    sys.stderr.write("Resetting ubertooth device\\n")
sys.exit(0)
"""


class SilentCapture:
    """A capture that has produced nothing, for the recovery ladder.

    ``exited`` stands for a tool that has already gone, which is the other
    way the ladder is reached.
    """

    def __init__(self, records=0):
        self.tail = type("Tail", (), {"records": records, "error": ""})()
        self.attempt = addresssoak.Attempt(1, (), "capture-0001.pcap",
                                           "btle-0001.log")
        self.attempt.records = records
        self.attempt.returncode = 1
        self.exited = False

    def running(self):
        return not self.exited

    def finish(self):
        return self.attempt

    def poll_records(self):
        return []


class ScheduledFirmware(FakeFirmware):
    """The fake DK, with its advertising state written where the tool sees it.

    The capture tool is a separate process, so the only way it can emit
    packets while the advertiser is on and nothing while it is off -- which
    is what the detection and gap figures are made of -- is through something
    both can read.
    """

    def __init__(self, state_path: Path, **options):
        self.state_path = Path(state_path)
        super().__init__(**options)
        self.state_path.write_text("off", encoding="utf-8")

    def handle(self, line: str):
        answer = super().handle(line)
        if line == "adv start" and not answer[0].startswith("ERR"):
            self.state_path.write_text("on", encoding="utf-8")
        elif line in ("adv stop", "adv defaults"):
            self.state_path.write_text("off", encoding="utf-8")
        return answer


class SoakTestCase(unittest.TestCase):
    """One temporary bench: a PATH with the fakes on it and an output dir."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.out = self.root / "out"
        self.bin = self.root / "bin"
        self.state = self.root / "advertising"
        write_executable(self.bin / "ubertooth-btle", FAKE_BTLE)
        write_executable(self.bin / "ubertooth-util", FAKE_UTIL)
        self.environment = dict(os.environ)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        self.addCleanup(self._restore_environment)
        self.firmware = ScheduledFirmware(self.state, address=ADDRESS)

    def _restore_environment(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def set_tool(self, **values):
        for key, value in values.items():
            os.environ[f"FAKE_BTLE_{key}"] = str(value)
            self.addCleanup(os.environ.pop, f"FAKE_BTLE_{key}", None)

    def dk(self):
        port = FakePort(self.firmware)
        return nrfdk.Dk(1050000001, str(self.root / "dk"), opener=lambda _p:
                        nrfdk.Shell(port), alias="DK_Linux0")

    def argv(self, **overrides):
        values = {
            "--out": str(self.out), "--serial": SERIAL, "--address": ADDRESS,
            "--channel": "37", "--duration": "4", "--jlink": "1050000001",
            "--interval": "100", "--payload": "c0ffee",
            "--advertiser-on": "1", "--advertiser-off": "1",
            "--sample-every": "1", "--presence-window": "0.5",
            "--presence-windows": "2", "--liveness-timeout": "2",
            "--detect-within": "0.75", "--min-detection-share": "0.9",
            "--max-gap": "0.9", "--min-packets-per-minute": "1",
            "--max-disagreement-share": "0.5", "--max-restarts": "0",
            "--restart-min-interval": "0.2", "--term-grace": "1",
        }
        flags = []
        for key, value in overrides.items():
            option = "--" + key.replace("_", "-")
            if value is True:
                flags.append(option)
            elif value is None:
                values.pop(option, None)
            else:
                values[option] = str(value)
        argv = []
        for key, value in values.items():
            argv += [key, value]
        return argv + flags

    def run_soak(self, **overrides):
        """(exit status, stdout, stderr) of one driver run."""
        self.set_tool(STATE=str(self.state))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = addresssoak.main(self.argv(**overrides), dk=self.dk())
        return code, out.getvalue(), err.getvalue()

    def summary(self):
        return json.loads((self.out / "summary.json").read_text(encoding="utf-8"))

    def progress(self):
        return [json.loads(line) for line
                in (self.out / "progress.jsonl").read_text(encoding="utf-8").splitlines()]


class HealthyRunTests(SoakTestCase):

    def test_a_healthy_run_passes_and_both_readers_see_the_advertiser(self):
        code, out, err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_PASS, code, out + err)
        self.assertIn("SOAK VERDICT: pass", out)
        self.assertNotIn("FAIL:", out)
        summary = self.summary()
        self.assertGreater(summary["text"]["advertiser_packets"], 0)
        self.assertGreater(summary["structural"]["advertiser_packets"], 0)
        self.assertEqual(ADDRESS, summary["address"])
        # Every captured packet carried the harness payload tag, which is
        # what says the address is this advertiser's.
        self.assertEqual(summary["structural"]["advertiser_packets"],
                         summary["structural"]["payload_matches"])

    def test_detection_is_judged_per_on_period_for_both_readers(self):
        code, out, _err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_PASS, code, out)
        detection = self.summary()["detection"]
        self.assertEqual(sorted(detection), ["structural", "text"])
        for name, found in detection.items():
            with self.subTest(reader=name):
                self.assertGreaterEqual(found["periods"], 2)
                self.assertEqual(found["periods"], found["detected"])
                self.assertEqual(found["periods"], found["sustained"])
                self.assertIsNotNone(found["first_detection_s"])

    def test_the_run_is_reviewable_from_the_files_it_wrote(self):
        self.run_soak()
        records = self.progress()
        kinds = {record["record"] for record in records}
        for kind in ("capture_started", "period_started", "period", "sample",
                     "watchdog", "attempt", "board_after"):
            self.assertIn(kind, kinds)
        # Every record carries when it was written, so a reader of a run that
        # is still going knows how far it has got.
        for record in records:
            self.assertIn("at_s", record)
        self.assertTrue((self.out / "capture-0001.pcap").is_file())
        self.assertTrue((self.out / "btle-0001.log").is_file())
        # The transcript is the tool's own output and nothing else.
        transcript = (self.out / "btle-0001.log").read_text(encoding="utf-8")
        self.assertIn("AdvA:", transcript)
        self.assertNotIn("SOAK VERDICT", transcript)

    def test_the_schedule_is_applied_to_the_advertiser(self):
        self.run_soak()
        commands = self.firmware.commands
        self.assertIn("adv chmap 37", commands)
        self.assertIn("adv interval 100", commands)
        self.assertIn("adv payload c0ffee", commands)
        self.assertGreaterEqual(commands.count("adv start"), 2)
        self.assertGreaterEqual(commands.count("adv stop"), 2)
        # It is left quiet, and the counter is read at both ends.
        self.assertEqual("off", self.state.read_text(encoding="utf-8"))
        counter = self.summary()["advertiser_counter"]
        self.assertIsNotNone(counter["before"])
        self.assertIsNotNone(counter["after"])

    def test_memory_and_rate_are_reported(self):
        self.run_soak()
        summary = self.summary()
        self.assertGreaterEqual(summary["memory"]["samples"], 2)
        rate = summary["rate_per_minute"]
        self.assertGreater(rate["periods"], 0)
        self.assertGreater(rate["on_seconds"], 0)
        self.assertGreater(rate["mean"], 0)
        # Whether `ps` answers at all is the host's business (a sandbox may
        # refuse to run it), so what is asserted here is that the sampling
        # happened and the figures agree with each other. What `resident_kb`
        # makes of an answer is MemoryTests below.
        memory = summary["memory"]
        if memory["first_kb"] is not None:
            self.assertIsNotNone(memory["growth_kb"])
            self.assertEqual(memory["growth_kb"],
                             memory["last_kb"] - memory["first_kb"])
            self.assertGreaterEqual(memory["max_kb"], memory["last_kb"])
        else:
            self.assertIsNone(memory["growth_kb"])
        samples = [record for record in self.progress()
                   if record["record"] == "sample"]
        self.assertTrue(samples)
        for sample in samples:
            self.assertIn("rss_kb", sample)

    def test_the_watchdog_record_is_read_and_reported(self):
        self.run_soak()
        watchdog = self.summary()["watchdog"]
        # Read before the run and after it; the fake board reports the same
        # record both times, so nothing new is counted.
        self.assertGreaterEqual(watchdog["reads"], 2)
        self.assertEqual(0, watchdog["unrequested_resets"])

    def test_the_final_stop_is_a_clean_one(self):
        self.run_soak()
        attempts = self.summary()["attempts"]
        self.assertEqual(1, len(attempts))
        self.assertEqual("SIGTERM", attempts[-1]["signalled"])
        self.assertFalse(attempts[-1]["forced"])
        self.assertEqual(0, self.summary()["process"]["forced_stops"])


class TranscriptTests(SoakTestCase):
    """The tool's own output: kept, bounded, and still read past the bound."""

    def test_the_transcript_is_the_tools_output_and_nothing_else(self):
        self.run_soak()
        transcript = (self.out / "btle-0001.log").read_text(encoding="utf-8")
        self.assertIn("AdvA:", transcript)
        self.assertIn("Advertising / AA 8e89bed6", transcript)
        self.assertNotIn("SOAK VERDICT", transcript)
        kept = self.summary()["transcript"]
        self.assertGreater(kept["kept_bytes"], 0)
        self.assertEqual(0, kept["dropped_lines"])

    def test_the_budget_is_the_whole_run_s_and_lines_past_it_are_still_read(self):
        # A crowded channel prints megabytes a minute, and restarts and file
        # rotations would multiply a per-attempt cap, so the budget belongs
        # to the run. What a run loses past it is the per-packet text: the
        # lines are still parsed, so the figures do not change.
        self.set_tool(PAD_BYTES="4096")
        addresssoak.MAX_LOG_BYTES, held = 2048, addresssoak.MAX_LOG_BYTES
        self.addCleanup(setattr, addresssoak, "MAX_LOG_BYTES", held)
        code, out, _err = self.run_soak(max_capture_bytes=2048)
        self.assertEqual(addresssoak.EXIT_PASS, code, out)
        summary = self.summary()
        kept = summary["transcript"]
        self.assertLessEqual(kept["kept_bytes"], 2048)
        self.assertGreater(kept["dropped_lines"], 0)
        self.assertGreaterEqual(summary["process"]["rotations"], 1)
        # The second attempt's transcript shares the one budget, so it is
        # almost nothing, and the packets were counted all the same.
        self.assertGreater(summary["structural"]["advertiser_packets"], 0)
        self.assertGreater(summary["text"]["advertiser_packets"], 0)


class ParityTests(SoakTestCase):

    def test_a_text_only_capture_fails_the_parity_comparison(self):
        # The tool prints the address and records nothing: exactly the
        # regression this test is for, a reader that would see a device the
        # capture cannot show.
        self.set_tool(MODE="text")
        code, out, _err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        self.assertIn("FAIL: the readers disagreed about", out)
        self.assertIn("text only", out)
        summary = self.summary()
        self.assertEqual(0, summary["structural"]["advertiser_packets"])
        self.assertGreater(summary["parity"]["windows_text_only"], 0)

    def test_a_record_only_capture_fails_the_other_way(self):
        self.set_tool(MODE="records")
        code, out, _err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        self.assertGreater(self.summary()["parity"]["windows_structural_only"], 0)
        self.assertIn("FAIL: the readers disagreed about", out)

    def test_a_wrong_crc_is_dropped_by_the_structural_reader_alone(self):
        # libbtbb prints a decoded packet whether or not its CRC checks, so
        # this is the documented asymmetry rather than a fault: the text
        # reader reports the address and the record reader drops the packet.
        self.set_tool(MODE="badcrc")
        code, out, _err = self.run_soak()
        summary = self.summary()
        self.assertGreater(summary["text"]["advertiser_packets"], 0)
        self.assertEqual(0, summary["structural"]["advertiser_packets"])
        self.assertGreater(summary["structural"]["records_not_advertiser_crc_valid"], 0)
        self.assertEqual(addresssoak.EXIT_FAIL, code, out)

    def test_another_address_is_counted_and_never_recorded(self):
        other = "C3:99:88:77:66:55"
        self.set_tool(ADDRESS=other)
        code, out, _err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        summary = self.summary()
        self.assertEqual(0, summary["text"]["advertiser_packets"])
        self.assertGreater(summary["text"]["other_packets"], 0)
        self.assertEqual(1, summary["text"]["other_addresses"])
        # The other device's address is in neither the report nor the summary.
        self.assertNotIn(other, out)
        self.assertNotIn(other.lower(), json.dumps(summary))
        self.assertIn("FAIL: the capture held no packet of the advertiser at all", out)

    def test_the_allowance_is_a_share_of_the_windows_either_reader_saw(self):
        self.set_tool(MODE="text")
        self.run_soak(max_disagreement_share="1")
        parity = self.summary()["parity"]
        self.assertEqual(parity["windows_seen"], parity["allowance"])


class LivenessTests(SoakTestCase):

    def test_a_capture_that_exits_by_itself_is_restarted_and_reported(self):
        self.set_tool(EXIT_AFTER="1.5", EXIT_CODE="1")
        code, out, _err = self.run_soak(max_restarts=2)
        summary = self.summary()
        self.assertGreaterEqual(summary["process"]["restarts"], 1)
        self.assertGreaterEqual(summary["process"]["unexplained_exits"], 1)
        self.assertEqual(1, summary["attempts"][0]["returncode"])
        self.assertIn("libusb returned -1", summary["attempts"][0]["last_error"])
        self.assertIn("exited 1 by itself", out)
        # Each restart is preceded by a watchdog read, while nothing is
        # capturing.
        self.assertGreaterEqual(summary["watchdog"]["reads"], 3)
        self.assertIn(code, (addresssoak.EXIT_PASS, addresssoak.EXIT_FAIL))

    def test_more_restarts_than_the_budget_fail_the_run(self):
        self.set_tool(EXIT_AFTER="1", EXIT_CODE="1")
        code, out, _err = self.run_soak(max_restarts=0)
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        self.assertIn("unplanned restart(s), more than 0", out)

    def test_a_silent_capture_is_restarted_on_the_liveness_timeout(self):
        # Nothing arrives at all, which is the production rule: no packet of
        # any address for the timeout means the capture is not working.
        self.set_tool(MODE="records", RATE="0.01")
        code, out, _err = self.run_soak(max_restarts=5, duration=8,
                                        liveness_timeout=2)
        self.assertGreaterEqual(self.summary()["process"]["restarts"], 1)
        self.assertIn("no packet for", out)
        self.assertEqual(addresssoak.EXIT_FAIL, code)

    def test_the_second_silent_restart_asks_for_a_board_reset(self):
        """The ladder's second rung, decided rather than timed.

        A run that reaches it needs two liveness timeouts in a row and the
        restart interval between them, which is seconds of real waiting and
        fits or does not fit depending on how loaded the machine is -- a CI
        runner three modules deep could not. So the condition is driven
        directly: an attempt that heard nothing, after a restart that had
        already happened, is what asks for the reset, and `restart` is what
        runs it.
        """
        soak = self.silent_soak()
        soak.restarts = 1  # the clean restart has already been tried
        self.quietly(soak.check_liveness)
        self.assertTrue(soak.reset_next_restart)

    def test_the_restart_that_follows_resets_the_board(self):
        # The rung itself: `ubertooth-util -r`, counted, recorded, and
        # followed by a watchdog read, which is where the firmware says
        # where the board had been stuck.
        soak = self.silent_soak(stub_restart=False)
        soak.reset_next_restart = True
        stopped = addresssoak.Attempt(1, (), "capture-0001.pcap", "btle-0001.log")
        soak.stop_capture = lambda *a, **k: stopped
        soak.start_capture = lambda *a, **k: None
        self.quietly(soak.restart, "no packet for 3.0s")
        self.assertEqual(1, soak.board_resets)
        self.assertFalse(soak.reset_next_restart)
        # Read before the reset and after it.
        self.assertEqual(2, soak.watchdog_reads)

    def test_a_capture_that_dies_at_once_reaches_the_reset_rung_too(self):
        # The death #703 names first. The rung used to be reachable only
        # through the silence branch, so a board whose tool exited at once
        # restarted every few seconds for the rest of the window and the
        # reset the ladder specifies never ran.
        soak = self.silent_soak()
        soak.restarts = 1
        soak.capture.exited = True
        self.quietly(soak.check_liveness)
        self.assertTrue(soak.reset_next_restart)

    def test_a_capture_that_dies_after_hearing_something_asks_for_no_reset(self):
        soak = self.silent_soak(records=5)
        soak.restarts = 1
        soak.capture.exited = True
        self.quietly(soak.check_liveness)
        self.assertFalse(soak.reset_next_restart)

    def test_the_first_silent_capture_is_restarted_without_a_reset(self):
        # The first rung is a clean restart: the board is not reset until a
        # restart has been tried and the capture still heard nothing.
        soak = self.silent_soak()
        self.quietly(soak.check_liveness)
        self.assertFalse(soak.reset_next_restart)

    def test_a_capture_that_heard_something_never_asks_for_a_reset(self):
        soak = self.silent_soak(records=5)
        soak.restarts = 1
        self.quietly(soak.check_liveness)
        self.assertFalse(soak.reset_next_restart)

    def quietly(self, call, *arguments):
        """Run it with the driver's own progress lines out of the way."""
        out = io.StringIO()
        with redirect_stdout(out):
            return call(*arguments)

    def silent_soak(self, records=0, stub_restart=True):
        """A run whose capture is running and has heard nothing for a while."""
        self.out.mkdir(parents=True, exist_ok=True)
        options = addresssoak.options_from(addresssoak.build_parser().parse_args(
            self.argv()))
        soak = addresssoak.Soak(options, None)
        soak.capture = SilentCapture(records)
        # Longer ago than the liveness timeout the arguments carry.
        soak.last_packet_at = soak.elapsed - options.liveness_timeout_s - 1
        if stub_restart:
            # The decision is what these tests are about, not the restart it
            # leads to, which has its own test below.
            soak.restart = lambda *args, **options: None
        soak.board_after = {}
        return soak

    def test_a_tool_that_ignores_sigterm_is_killed_and_the_run_fails(self):
        self.set_tool(IGNORE_SIGTERM="1")
        code, out, _err = self.run_soak(term_grace=0.5)
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        self.assertIn("did not stop on SIGTERM", out)
        self.assertTrue(self.summary()["attempts"][-1]["forced"])
        self.assertEqual("SIGTERM then SIGKILL",
                         self.summary()["attempts"][-1]["signalled"])


class ProbeTests(SoakTestCase):

    def test_the_probe_kills_the_capture_once_and_requires_recovery(self):
        # A liveness timeout well clear of what starting a capture costs, so
        # a loaded machine cannot turn the probe's own gap into a second,
        # unplanned restart and make this test's subject disappear.
        code, out, _err = self.run_soak(duration=8, restart_probe=True,
                                        liveness_timeout=4)
        summary = self.summary()
        self.assertEqual(1, summary["process"]["probe_restarts"])
        self.assertEqual(0, summary["process"]["restarts"])
        self.assertIs(True, summary["process"]["probe_recovered"])
        self.assertIn("restart probe: recovered", out)
        self.assertEqual(addresssoak.EXIT_PASS, code, out)
        killed = summary["attempts"][0]
        self.assertEqual("SIGKILL", killed["signalled"])
        self.assertEqual("the restart probe", killed["stop_reason"])
        # The capture that followed it is a file of its own, because the tool
        # refuses to write over one that is there.
        self.assertTrue((self.out / "capture-0002.pcap").is_file())

    def test_a_probe_whose_capture_never_comes_back_never_claims_recovery(self):
        # Only the first run of the tool produces anything, so the capture
        # the probe starts hears nothing. Whether that reads as a probe that
        # did not recover or as too little of the schedule to judge depends
        # on where in it the probe landed, which a run cannot promise; what
        # it must never do is report a recovery. The verdict's own arithmetic
        # for both outcomes is VerdictTests below.
        self.set_tool(ONCE=str(self.root / "ran-once"))
        code, out, _err = self.run_soak(duration=6, restart_probe=True,
                                        max_restarts=5)
        summary = self.summary()
        self.assertEqual(1, summary["process"]["probe_restarts"])
        self.assertIsNot(True, summary["process"]["probe_recovered"])
        self.assertIn("restart probe: ", out)
        self.assertNotIn("restart probe: recovered", out)
        # Either way it fails: the advertiser was not heard again, or no
        # judged period followed the probe and the plan asked for evidence
        # that cannot then be given.
        self.assertEqual(addresssoak.EXIT_FAIL, code, out)

    def test_a_capture_that_keeps_dying_is_restarted_and_recorded_each_time(self):
        self.set_tool(ONCE=str(self.root / "ran-once"), EXIT_AFTER="2",
                      EXIT_CODE="1")
        code, out, _err = self.run_soak(duration=8, max_restarts=10)
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        summary = self.summary()
        self.assertGreater(summary["process"]["restarts"], 1)
        # Every restart here is a death the run did not ask for, though a
        # capture that dies and one that goes silent can both fire in the
        # same attempt, so the counts need not be equal.
        self.assertGreater(summary["process"]["unexplained_exits"], 0)
        self.assertGreaterEqual(summary["process"]["restarts"],
                                summary["process"]["unexplained_exits"] - 1)
        # Every attempt kept its own exit status, its last error line, and
        # its own capture file and transcript. An attempt either died of its
        # own accord -- exit 1 and nothing asked it to -- or the run stopped
        # it: a liveness timeout can fire in the moment before the tool
        # exits, and then the stop is what ended it.
        died = 0
        for attempt in summary["attempts"]:
            self.assertTrue((self.out / attempt["capture"]).is_file())
            self.assertTrue((self.out / attempt["log"]).is_file())
            if attempt["signalled"]:
                continue
            self.assertEqual(1, attempt["returncode"])
            self.assertIn("libusb returned -1", attempt["last_error"])
            died += 1
        self.assertGreater(died, 0)


class RotationTests(SoakTestCase):

    def test_a_large_capture_is_rolled_over_at_an_off_period(self):
        # The rotation is a planned stop and start, not a restart, and it
        # happens where the advertiser is quiet anyway, so a judged period
        # never holds the changeover.
        self.set_tool(PAD_BYTES="4096")
        code, out, _err = self.run_soak(max_capture_bytes=2048)
        summary = self.summary()
        self.assertGreaterEqual(summary["process"]["rotations"], 1)
        self.assertEqual(0, summary["process"]["restarts"])
        self.assertEqual(0, summary["process"]["unexplained_exits"])
        self.assertIn("starting another file", out)
        self.assertTrue((self.out / "capture-0002.pcap").is_file())
        self.assertEqual(addresssoak.EXIT_PASS, code, out)

    def test_no_limit_rolls_over_nothing(self):
        self.set_tool(PAD_BYTES="4096")
        self.run_soak(max_capture_bytes=0)
        self.assertEqual(0, self.summary()["process"]["rotations"])


class BudgetTests(SoakTestCase):
    """The run's own disk budget: newest captures kept, removals recorded."""

    def test_the_oldest_captures_are_removed_to_stay_inside_the_budget(self):
        # Each attempt leaves a capture and a transcript, and a long run has
        # many; the figures are counted as the records arrive, so what a run
        # gives up past its budget is the oldest bytes to read by hand.
        self.set_tool(PAD_BYTES="8192")
        code, out, _err = self.run_soak(max_capture_bytes=2048,
                                        max_total_capture_bytes=4096)
        self.assertEqual(addresssoak.EXIT_PASS, code, out)
        summary = self.summary()
        self.assertGreater(summary["captures"]["removed"], 0)
        self.assertGreater(summary["captures"]["removed_bytes"], 0)
        self.assertIn("removed to stay inside", out)
        # The oldest attempt's files are gone and the newest are there.
        attempts = summary["attempts"]
        self.assertTrue(attempts[0]["files_removed"])
        self.assertFalse((self.out / attempts[0]["capture"]).exists())
        self.assertFalse(attempts[-1]["files_removed"])
        self.assertTrue((self.out / attempts[-1]["capture"]).is_file())
        # And the removals are in the progress file, with what they cost.
        removals = [record for record in self.progress()
                    if record["record"] == "capture_removed"]
        self.assertTrue(removals)
        self.assertIn("budget", removals[0]["reason"])

    def test_the_newest_attempt_is_never_pruned(self):
        # "Newest kept" is what the budget promises, and the newest capture
        # is the one a reader of a failed run wants: a budget smaller than
        # one attempt must not leave an empty directory.
        self.set_tool(PAD_BYTES="8192")
        code, out, _err = self.run_soak(max_capture_bytes=2048,
                                        max_total_capture_bytes=1)
        self.assertEqual(addresssoak.EXIT_PASS, code, out)
        attempts = self.summary()["attempts"]
        self.assertFalse(attempts[-1]["files_removed"])
        self.assertTrue((self.out / attempts[-1]["capture"]).is_file())

    def test_an_attempt_whose_files_are_empty_is_still_pruned(self):
        # The state a failing run reaches: the transcript budget is spent, so
        # later logs are empty, and an attempt whose tool never got the board
        # leaves no capture either. An earlier version unlinked those files
        # and then put the attempt back because nothing had been freed, which
        # re-targeted the same attempt for ever and left the budget disabled
        # for the rest of the run.
        soak = self.built_soak(budget=1)
        # The oldest attempt's files are empty -- the transcript budget was
        # spent and its tool never got the board -- while a later one holds
        # the bytes that put the run over its budget.
        first = self.fake_attempt(1, size=0)
        second = self.fake_attempt(2, size=4096)
        soak.attempts += [first, second]
        soak.prune_captures()
        self.assertTrue(first.files_removed)
        self.assertFalse(second.files_removed)
        self.assertEqual(1, soak.captures_removed)
        self.assertEqual(0, soak.capture_bytes_removed)
        self.assertFalse((self.out / first.capture).exists())
        self.assertTrue((self.out / second.capture).is_file())

    def test_an_attempt_whose_files_cannot_be_removed_is_kept(self):
        soak = self.built_soak(budget=1)
        first, second = self.fake_attempt(1), self.fake_attempt(2)
        soak.attempts += [first, second]
        (self.out / first.capture).chmod(0o444)
        (self.out / first.log).chmod(0o444)
        self.out.chmod(0o555)
        self.addCleanup(self.out.chmod, 0o755)
        soak.prune_captures()
        self.assertFalse(first.files_removed)
        self.assertEqual(0, soak.captures_removed)
        self.assertTrue((self.out / first.capture).is_file())
        # And the run says so and fails, rather than carrying on writing
        # captures past the budget for however long it had left.
        self.assertTrue(soak.capture_budget_stuck)
        problems = soak.judge()
        self.assertTrue(any("capture budget could not be kept" in problem
                            for problem in problems), problems)

    def test_a_stuck_budget_stops_the_rollovers_too(self):
        soak = self.built_soak(budget=1)
        soak.capture_budget_stuck = ["capture-0001.pcap"]
        soak.capture = SilentCapture()
        before = soak.rotations
        soak.rotate_if_large()
        self.assertEqual(before, soak.rotations)

    def test_progress_records_stop_at_their_budget(self):
        """The file a long run is followed with has a bound of its own.

        A plan may ask for a sample a second over three days, which is a
        quarter of a million records; nothing else here stops that file from
        growing with the run.
        """
        soak = self.built_soak(budget=0)
        soak.progress = (self.out / "progress.jsonl").open("w", encoding="utf-8")
        self.addCleanup(soak.progress.close)
        addresssoak.MAX_PROGRESS_BYTES, saved = 200, addresssoak.MAX_PROGRESS_BYTES
        self.addCleanup(setattr, addresssoak, "MAX_PROGRESS_BYTES", saved)
        for index in range(40):
            soak.record({"record": "sample", "index": index})
        self.assertTrue(soak.progress_records_dropped)
        self.assertLessEqual(soak.progress_bytes, 200)
        summary = {"progress": {"records_not_written": soak.progress_records_dropped}}
        self.assertEqual(soak.progress_records_dropped,
                         summary["progress"]["records_not_written"])

    def built_soak(self, budget):
        """A Soak with an output directory and no run behind it."""
        self.out.mkdir(parents=True, exist_ok=True)
        argv = self.argv(max_total_capture_bytes=budget)
        options = addresssoak.options_from(
            addresssoak.build_parser().parse_args(argv))
        return addresssoak.Soak(options, None)

    def fake_attempt(self, index, size=16):
        """An attempt whose files are on disk, as a finished one leaves them."""
        attempt = addresssoak.Attempt(
            index, (), f"capture-{index:04d}.pcap", f"btle-{index:04d}.log")
        for name in (attempt.capture, attempt.log):
            (self.out / name).write_bytes(b"x" * size)
        attempt.bytes_kept = 2 * size
        return attempt

    def test_no_budget_removes_nothing(self):
        self.set_tool(PAD_BYTES="8192")
        self.run_soak(max_capture_bytes=2048, max_total_capture_bytes=0)
        self.assertEqual(0, self.summary()["captures"]["removed"])


class OffPeriodTests(SoakTestCase):
    """The other half of the schedule: silence when the advertiser is off."""

    def test_an_advertiser_that_ignores_the_stop_fails_the_run(self):
        # The hole this closes: a DK that accepts `adv stop` and keeps
        # advertising met every detection, gap and rate objective, and the
        # run reported a controlled schedule it never had. The fake firmware
        # answers OK and leaves the state file alone, which is exactly that.
        class Deaf(ScheduledFirmware):
            def handle(self, line):
                if line == "adv stop":
                    FakeFirmware.handle(self, line)
                    return ["OK"]  # the state file is left saying "on"
                return super().handle(line)

        self.firmware = Deaf(self.state, address=ADDRESS)
        # Long enough for several off periods: one noisy period is inside the
        # allowance, which is there for a delayed flush rather than for an
        # advertiser that never stopped.
        code, out, _err = self.run_soak(duration=8)
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        self.assertIn("did not stop when it was told to", out)
        quiet = self.summary()["off_quiet"]
        self.assertGreater(quiet["noisy"], 0)
        self.assertEqual(quiet["periods"], quiet["noisy"])

    def test_a_healthy_run_has_quiet_off_periods(self):
        self.run_soak()
        quiet = self.summary()["off_quiet"]
        self.assertGreater(quiet["periods"], 0)
        self.assertEqual(0, quiet["noisy"])


class UnreadableCaptureTests(SoakTestCase):
    """A capture the structural reader cannot read is named, not guessed at."""

    def test_a_half_written_record_after_a_clean_stop_is_an_error(self):
        # A tool that was asked to stop flushes and closes its capture, so a
        # partial record after a clean stop is a file that cannot be read to
        # its end. After a kill it is ordinary, which the probe relies on.
        self.set_tool(PARTIAL_TAIL="1")
        code, out, _err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        self.assertIn("bytes of an incomplete record", out)
        self.assertIn("capture file(s) could not be read to the end", out)
        attempts = self.summary()["attempts"]
        self.assertGreater(attempts[-1]["partial_tail_bytes"], 0)

    def test_a_corrupt_record_header_fails_the_run_with_its_own_reason(self):
        # Without this the run fails later for a symptom -- the readers
        # disagreed, or no advertiser packet at all -- on the very fault this
        # test exists to find.
        self.set_tool(BAD_RECORD_AFTER="1")
        code, out, _err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        self.assertIn("capture file(s) could not be read to the end", out)
        errors = self.summary()["structural"]["unreadable_captures"]
        self.assertTrue(errors)
        self.assertIn("more than", errors[0])
        records = [record for record in self.progress()
                   if record["record"] == "capture_unreadable"]
        self.assertTrue(records)


class CancelDuringRestartTests(SoakTestCase):
    """A cancellation that lands between two captures still leaves a report."""

    def test_a_run_canceled_with_nothing_running_still_reports(self):
        # The window is real: a restart stops its capture, reads the
        # watchdog, may reset the board, and waits before the next attempt.
        # A cancellation anywhere in there used to reach the teardown with no
        # capture and die of it -- no summary, no verdict, a traceback.
        soak = self.built_soak(budget=0)
        soak.cancel("SIGTERM")
        self.assertIsNone(soak.stop_capture("the run was stopped"))
        out = io.StringIO()
        with redirect_stdout(out):
            code = soak.report(None)
        self.assertIn("nothing was running when the run ended", out.getvalue())
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        summary = json.loads(
            (self.out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual("fail", summary["verdict"])
        self.assertEqual("SIGTERM", summary["canceled"])
        self.assertTrue(any("stopped by SIGTERM" in problem
                            for problem in summary["problems"]))

    def test_a_cancellation_stops_the_board_work_a_restart_would_do(self):
        soak = self.built_soak(budget=0)
        soak.attempts.append(self.fake_attempt(1))
        soak.cancel("SIGTERM")
        # No capture, canceled: restart must return without reading the
        # watchdog or resetting the board, each of which is bounded in tens
        # of seconds while the harness allows about two.
        soak.restart("no packet for 2.0s")
        self.assertEqual(0, soak.watchdog_reads)
        self.assertEqual(0, soak.board_resets)
        self.assertIsNone(soak.capture)

    def built_soak(self, budget):
        self.out.mkdir(parents=True, exist_ok=True)
        options = addresssoak.options_from(addresssoak.build_parser().parse_args(
            self.argv(max_total_capture_bytes=budget)))
        soak = addresssoak.Soak(options, None)
        soak.board_after = {}
        return soak

    def fake_attempt(self, index):
        return addresssoak.Attempt(index, (), f"capture-{index:04d}.pcap",
                                   f"btle-{index:04d}.log")


class RecordClockTests(SoakTestCase):
    """The capture clock is anchored to the host's, and says when it drifted."""

    def test_a_healthy_run_needs_no_re_anchor(self):
        self.run_soak()
        clock = self.summary()["record_clock"]
        self.assertEqual(0, clock["reanchors"])
        self.assertEqual(0.0, clock["largest_drift_s"])

    def test_a_record_clock_that_runs_away_is_re_anchored_and_reported(self):
        # The record clock is the board's, anchored once per attempt and
        # advanced by the board's own counter, so it drifts from the host's
        # over hours and jumps a whole period if a quiet gap loses a wrap.
        # Here the fake tool steps its records forward by the 327.68 s the
        # fork's own warning names as one lost wrap, partway through the run.
        self.set_tool(TIME_OFFSET="327.68")
        code, out, _err = self.run_soak()
        clock = self.summary()["record_clock"]
        self.assertGreater(clock["reanchors"], 0)
        self.assertGreater(clock["largest_drift_s"], 1.0)
        self.assertIn("record clock:", out)
        # And the sightings still land in the periods they belong to, so the
        # run still passes: what the step costs is the window the record it
        # landed on was re-anchored into, which is inside the allowance, and
        # a note saying it happened. Without the re-anchor every record after
        # the step would have been five minutes out of its period.
        self.assertEqual(addresssoak.EXIT_PASS, code, out)
        parity = self.summary()["parity"]
        self.assertLessEqual(parity["disagreements"], parity["allowance"])
        detection = self.summary()["detection"]
        self.assertEqual(detection["structural"]["periods"],
                         detection["structural"]["detected"])


class ConfigurationTests(SoakTestCase):

    def test_a_busy_board_is_a_configuration_failure_and_is_not_reset(self):
        os.environ["FAKE_UTIL_BUSY"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_UTIL_BUSY", None)
        code, out, err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_CONFIGURATION, code)
        self.assertIn("does not answer", err)
        self.assertIn("in use by another program", err)
        self.assertIn("SOAK VERDICT: configuration", out)
        self.assertIn("FAIL: the run could not finish", out)
        # Nothing was captured, and nothing was reset: the board belongs to
        # whatever has it. The summary still says what happened, because the
        # verdict of a run is read from there.
        self.assertFalse((self.out / "capture-0001.pcap").exists())
        self.assertEqual("configuration", self.summary()["verdict"])

    def test_an_advertiser_at_another_address_is_a_configuration_failure(self):
        self.firmware.address = "C3:00:00:00:00:09"
        code, _out, err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_CONFIGURATION, code)
        self.assertIn("the advertiser reports C3:00:00:00:00:09", err)

    def test_a_schedule_the_advertiser_refuses_is_recorded_and_fails(self):
        self.firmware.refuse = {"adv start": "the radio is busy"}
        code, out, _err = self.run_soak()
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        self.assertIn("schedule command(s) failed", out)
        self.assertGreater(self.summary()["schedule"]["errors"], 0)
        # A period whose schedule did not reach the board is left out of the
        # verdict rather than counted as a missed detection.
        self.assertIn("FAIL: no advertiser on period ran in full", out)


class VerdictTests(unittest.TestCase):
    """The arithmetic of the verdict, with the run itself built by hand.

    No process, no board, no clock: the periods and the counters are set to
    exactly the run being asked about, so each objective is tested on its own
    and the answer cannot move with the machine's load. The scenario tests
    above cover the parts a model could not: signals, exits, and a file
    growing under the reader.
    """

    ON = 30.0
    OFF = 30.0

    def soak(self, **overrides):
        values = {
            "--out": "/tmp/soak", "--serial": SERIAL, "--address": ADDRESS,
            "--channel": "37", "--duration": "600", "--jlink": "1050000001",
            "--interval": "100", "--payload": "c0ffee",
            "--advertiser-on": str(self.ON), "--advertiser-off": str(self.OFF),
            "--sample-every": "30", "--presence-window": "2",
            "--presence-windows": "5", "--liveness-timeout": "60",
            "--detect-within": "10", "--min-detection-share": "0.9",
            "--max-gap": "15", "--min-packets-per-minute": "10",
            "--max-disagreement-share": "0.02", "--max-restarts": "0",
        }
        values.update({key: str(value) for key, value in overrides.items()})
        argv = [item for pair in values.items() for item in pair]
        options = addresssoak.options_from(addresssoak.build_parser().parse_args(argv))
        soak = addresssoak.Soak(options, None)
        # A healthy run, which each test below then spoils in one way.
        soak.structural_packets = 600
        soak.text_packets = 600
        soak.payload_matches = 600
        soak.board_after = {key: {"returncode": 0, "answered": True}
                            for key in ("firmware", "serial", "channel")}
        return soak

    def add_period(self, soak, state="on", *, index=None, text=(), structural=(),
                   seconds=None, **flags):
        """One period with the sightings each reader had inside it."""
        index = len(soak.periods) if index is None else index
        length = self.ON if seconds is None else seconds
        started = index * (self.ON + self.OFF)
        period = addresssoak.Period(index, state, started, **flags)
        for name, times in (("text", text), ("structural", structural)):
            side = getattr(period, name)
            for at in times:
                side.see(started + at, started, soak.options.window_s)
        period.ended = started + length
        if state == "on":
            period.text.close(period.ended, started)
            period.structural.close(period.ended, started)
        soak.periods.append(period)
        return period

    def seen_throughout(self):
        """Sighting times that detect at once and hold the whole period."""
        return [0.5 + step * 0.5 for step in range(int(self.ON * 2) - 1)]

    def healthy(self, periods=4, **overrides):
        soak = self.soak(**overrides)
        seen = self.seen_throughout()
        for index in range(periods):
            self.add_period(soak, "on", index=index, text=seen, structural=seen)
        return soak

    def test_a_healthy_run_has_nothing_to_report(self):
        self.assertEqual([], self.healthy().judge())

    def test_a_late_first_sighting_is_a_missed_detection(self):
        soak = self.healthy()
        # One period's first packet arrives after the deadline, and nothing
        # else changes: three of four detected, which is below the share.
        late = [20.0, 20.5, 21.0, 21.5, 22.0]
        soak.periods[0] = addresssoak.Period(0, "on", 0.0)
        self.add_period(soak, "on", index=0, text=late, structural=late)
        soak.periods[0] = soak.periods.pop()
        problems = soak.judge()
        self.assertTrue(any("detected 3 of 4 on periods within 10s" in problem
                            for problem in problems), problems)

    def test_presence_that_is_not_sustained_is_reported_apart_from_detection(self):
        soak = self.soak()
        # Seen at once in every period, but never in five windows in a row.
        for index in range(4):
            self.add_period(soak, "on", index=index, text=[0.5, 11.0],
                            structural=[0.5, 11.0])
        problems = soak.judge()
        self.assertTrue(any("sustained presence (5 x 2s windows) in 0 of 4"
                            in problem for problem in problems), problems)
        self.assertFalse(any("detected" in problem and "within" in problem
                             for problem in problems), problems)

    def test_the_coverage_floor_catches_a_capture_that_kept_dying(self):
        soak = self.soak()
        seen = self.seen_throughout()
        self.add_period(soak, "on", index=0, text=seen, structural=seen)
        for index in (1, 2, 3):
            self.add_period(soak, "on", index=index, unplanned_gaps=1)
        problems = soak.judge()
        self.assertTrue(any("only 1 of 4 advertiser on periods could be judged"
                            in problem for problem in problems), problems)
        # And it is not reported as a detection failure: nothing was
        # listening, so there was nothing to detect.
        self.assertFalse(any("detected 1 of" in problem for problem in problems),
                         problems)

    def test_the_gaps_the_run_chose_are_allowed_against_that_floor(self):
        soak = self.soak()
        seen = self.seen_throughout()
        for index in (0, 1, 2):
            self.add_period(soak, "on", index=index, text=seen, structural=seen)
        # The probe's own period, and the one the duration ended.
        self.add_period(soak, "on", index=3, planned_gaps=1)
        self.add_period(soak, "on", index=4, cut_by_duration=True, seconds=3.0)
        self.assertEqual([], soak.judge())
        self.assertEqual({"ran": 5, "ended_by_the_run": 1, "cut_short": 0,
                          "schedule_error": 0, "capture_gap": 1,
                          "planned_capture_gap": 1}, soak.on_period_counts())

    def test_a_period_whose_schedule_failed_is_left_out_and_reported(self):
        soak = self.soak()
        seen = self.seen_throughout()
        for index in (0, 1, 2, 3):
            self.add_period(soak, "on", index=index, text=seen, structural=seen)
        self.add_period(soak, "on", index=4, schedule_error="'adv start' failed twice")
        soak.schedule_errors = 1
        problems = soak.judge()
        # Reported twice, and both are true: the schedule is not known, and
        # the run could not judge enough of what it ran because of it. A
        # failed transition is the bench's doing, not the run's own, so it is
        # not allowed against the coverage floor the way the probe is.
        self.assertEqual(
            ["only 4 of 5 advertiser on periods could be judged (0 cut short, 1 "
             "with a schedule failure, 0 with a capture gap, of which 0 were the "
             "run's own doing), below 0.90",
             "1 advertiser schedule command(s) failed, so the schedule is not known"],
            problems)
        self.assertEqual(4, len(soak.on_periods()))

    def test_window_disagreements_beyond_the_allowance_fail(self):
        soak = self.healthy()
        # One window per period that the other reader did not have and that
        # is nowhere near any window it did have. The indices are the run's,
        # so each has to be its own.
        for index, period in enumerate(soak.periods):
            period.text.windows.add(90000 + 10 * index)
        parity = soak.parity()
        self.assertEqual(4, parity["windows_text_only"])
        problems = soak.judge()
        self.assertTrue(any("the readers disagreed about 4 of" in problem
                            for problem in problems), problems)

    def test_a_window_beside_one_the_other_reader_had_is_not_a_disagreement(self):
        # Both readers see their packets after the fact, through buffers that
        # do not fill together, so a packet near a window's edge lands either
        # side of it. What the comparison is for is a reader that missed the
        # advertiser, which is a run of windows away from the other's.
        soak = self.healthy()
        period = soak.periods[0]
        self.assertEqual(0, period.disagreements())
        # One window past the end of what both readers had: beside theirs.
        edge = max(period.structural.windows) + 1
        period.text.windows.add(edge)
        self.assertEqual(0, period.disagreements())
        self.assertEqual(0, soak.parity()["disagreements"])
        self.assertEqual(1, soak.parity()["windows_text_only"])
        # One nowhere near any window either reader had -- the windows are
        # the run's, so "nowhere near" means nowhere in the run.
        period.text.windows.add(edge + 10000)
        self.assertEqual(1, period.disagreements())
        self.assertEqual(1, soak.parity()["disagreements"])

    def test_readers_that_never_share_a_window_cannot_pass(self):
        # The degenerate case the allowance base has to cover: if only the
        # shared and the lonely windows counted, two readers that alternated
        # windows and never agreed on one would have an allowance of one and
        # nothing to compare.
        soak = self.soak()
        period = self.add_period(soak, "on", index=0)
        period.text.windows.update(range(0, 30, 2))
        period.structural.windows.update(range(1, 30, 2))
        parity = soak.parity()
        self.assertEqual(0, parity["windows_both"])
        self.assertEqual(30, parity["windows_seen"])
        self.assertEqual(0, parity["disagreements"])  # each is beside one
        self.assertEqual(1, parity["allowance"])
        # The adjacency tolerance lets that through, and the detection
        # objective is what catches it: a reader seeing every other window
        # has no run of windows, so neither reader has sustained presence.
        problems = soak.judge()
        self.assertTrue(any("sustained presence" in problem
                            for problem in problems), problems)

    def test_an_off_period_the_advertiser_was_heard_in_is_allowed_one(self):
        # One late sighting among several judged periods is the stdio flush
        # the allowance is for. It takes more than one judged period for the
        # allowance to exist at all: see the test below.
        soak = self.soak()
        seen = self.seen_throughout()
        for index in range(4):
            self.add_period(soak, "on", index=index, text=seen, structural=seen)
        for index in (4, 5, 6):
            self.add_period(soak, "off", index=index, seconds=self.OFF)
        noisy = soak.periods[-1]
        noisy.structural.see(noisy.started + 20.0, noisy.started, 2.0)
        quiet = soak.off_quiet()
        self.assertEqual(3, quiet["periods"])
        self.assertEqual(1, quiet["noisy"])
        self.assertEqual(1, quiet["allowance"])
        self.assertEqual([], soak.judge())

    def test_the_only_judged_off_period_has_no_allowance(self):
        """The allowance is never the whole population.

        The floor of one used to be unconditional, so a run with one judged
        off period had an allowance of one and the check could not fail
        however the advertiser behaved -- which is what the short suite has,
        and the suite a driver change is tried on.
        """
        soak = self.soak()
        seen = self.seen_throughout()
        for index in range(4):
            self.add_period(soak, "on", index=index, text=seen, structural=seen)
        noisy = self.add_period(soak, "off", index=4, seconds=self.OFF)
        noisy.structural.see(noisy.started + 20.0, noisy.started, 2.0)
        quiet = soak.off_quiet()
        self.assertEqual(1, quiet["periods"])
        self.assertEqual(1, quiet["noisy"])
        self.assertEqual(0, quiet["allowance"])
        problems = soak.judge()
        self.assertTrue(any("did not stop when it was told to" in problem
                            for problem in problems), problems)

    def test_rotations_that_empty_the_off_periods_fail_the_run(self):
        """A file rolled over in every off period leaves nothing judged.

        The rollover is the run's own doing, so crediting it against a share
        floor the way the on periods do would let an advertiser that takes
        `adv stop` and keeps advertising pass: nothing would be judged and
        nothing would complain. So an empty judged population fails on its
        own, whatever emptied it.
        """
        soak = self.soak()
        seen = self.seen_throughout()
        for index in range(4):
            self.add_period(soak, "on", index=index, text=seen, structural=seen)
        for index in (4, 5, 6):
            period = self.add_period(soak, "off", index=index, seconds=self.OFF)
            # A rollover whose capture never came back inside the period:
            # there is no stretch anything was listening to.
            period.planned_gaps = 1
            period.listening_from = None
        self.assertEqual(0, soak.off_quiet()["periods"])
        self.assertEqual(3, soak.off_period_counts()["rollover_left_nothing"])
        problems = soak.judge()
        self.assertTrue(any("never measured" in problem for problem in problems),
                        problems)

    def test_a_rollover_leaves_the_rest_of_its_off_period_judged(self):
        """The rollover itself does not cost the period.

        It happens at the start of an off period by design, so throwing the
        period away would mean a busy channel with a small capture file
        limit judged no off period at all. What is judged is the stretch
        from when something was listening again.
        """
        soak = self.soak()
        seen = self.seen_throughout()
        for index in range(4):
            self.add_period(soak, "on", index=index, text=seen, structural=seen)
        period = self.add_period(soak, "off", index=4, seconds=self.OFF)
        period.planned_gaps = 1
        period.listening_from = period.started + 5.0
        self.assertEqual([period], soak.off_periods())
        # A sighting inside the gap is not held against it; one after the
        # capture came back is.
        period.structural.see(period.started + 1.0, period.started, 2.0)
        self.assertEqual(0, soak.off_quiet()["noisy"])
        period.structural.see(period.started + 20.0, period.started, 2.0)
        self.assertEqual(1, soak.off_quiet()["noisy"])

    def test_several_noisy_off_periods_fail(self):
        soak = self.soak()
        seen = self.seen_throughout()
        for index in range(4):
            self.add_period(soak, "on", index=index, text=seen, structural=seen)
        for index in (4, 5, 6):
            noisy = self.add_period(soak, "off", index=index, seconds=self.OFF)
            noisy.structural.see(noisy.started + 20.0, noisy.started, 2.0)
        problems = soak.judge()
        self.assertTrue(any("did not stop when it was told to" in problem
                            for problem in problems), problems)

    def test_a_reader_that_missed_a_whole_period_is_still_caught(self):
        soak = self.healthy()
        soak.periods[0].structural = addresssoak.Side()
        parity = soak.parity()
        self.assertGreater(parity["disagreements"], parity["allowance"])
        self.assertTrue(any("the readers disagreed about" in problem
                            for problem in soak.judge()), soak.judge())

    def test_the_allowance_is_a_share_of_the_windows_either_reader_saw(self):
        soak = self.healthy()
        parity = soak.parity()
        # Four periods of fifteen two-second windows each, seen by both.
        self.assertEqual(60, parity["windows_both"])
        self.assertEqual(60, parity["windows_seen"])
        self.assertEqual(2, parity["allowance"])  # ceil(60 x 0.02)
        # It never rounds down to nothing, however short the run.
        short = self.soak()
        self.add_period(short, "on", index=0, text=[0.5], structural=[0.5])
        self.assertEqual(1, short.parity()["allowance"])

    def test_a_gap_longer_than_the_limit_fails_with_the_longest_one(self):
        soak = self.soak(**{"--max-gap": "10"})
        seen = self.seen_throughout()
        for index in (0, 1, 2):
            self.add_period(soak, "on", index=index, text=seen, structural=seen)
        # Heard at the start of two periods and then not again: one over the
        # limit is inside the allowance, which is there for an outlier.
        self.add_period(soak, "on", index=3, text=[0.5], structural=[0.5])
        self.add_period(soak, "on", index=4, text=[0.5], structural=[0.5])
        problems = soak.judge()
        self.assertEqual(29.5, soak.gaps()["structural_max_s"])
        self.assertTrue(any("gap with no advertiser packet longer than 10s"
                            in problem for problem in problems), problems)
        self.assertEqual(2, soak.gaps()["periods_over_limit"])
        self.assertEqual(1, soak.gaps()["allowance"])

    def test_the_rate_is_per_second_the_advertiser_was_really_on(self):
        # Not per wall-clock minute an on period happened to touch: a
        # schedule whose legs cross minute boundaries, or are shorter than a
        # minute, would otherwise pass or fail on where the minutes fell.
        soak = self.healthy()
        rate = soak.rate()
        self.assertEqual(4, rate["periods"])
        self.assertEqual(120.0, rate["on_seconds"])  # four 30 s periods
        # 59 sightings in each of four 30 s periods: 118 a minute.
        self.assertEqual(118.0, rate["mean"])
        self.assertEqual(118.0, rate["min"])
        self.assertEqual([], soak.judge())

    def test_a_rate_below_the_floor_fails(self):
        soak = self.soak(**{"--min-packets-per-minute": "60"})
        for index in range(4):
            # One packet every ten seconds: six a minute.
            self.add_period(soak, "on", index=index,
                            text=[1.0, 11.0, 21.0], structural=[1.0, 11.0, 21.0])
        problems = soak.judge()
        self.assertEqual(6.0, soak.rate()["mean"])
        self.assertTrue(any("packets a minute, below 60" in problem
                            for problem in problems), problems)

    def test_a_capture_with_no_payload_tag_may_be_another_device(self):
        soak = self.healthy()
        soak.payload_matches = 0
        self.assertIn("no captured packet carried the harness payload tag, so the "
                      "address may not be this advertiser's", soak.judge())

    def test_a_capture_with_no_advertiser_packet_at_all_fails(self):
        soak = self.healthy()
        soak.structural_packets = 0
        self.assertIn("the capture held no packet of the advertiser at all",
                      soak.judge())

    def test_restarts_forced_stops_and_board_resets(self):
        soak = self.healthy()
        soak.restarts = 1
        self.assertIn("1 unplanned restart(s), more than 0", soak.judge())
        soak = self.healthy(**{"--max-restarts": "2"})
        soak.restarts = 2
        self.assertEqual([], soak.judge())
        soak.forced_stops = 1
        self.assertTrue(any("had to be killed" in problem
                            for problem in soak.judge()))

    def test_a_board_that_did_not_answer_afterwards_is_named(self):
        soak = self.healthy()
        soak.board_after["channel"] = {"returncode": 1, "answered": False}
        self.assertIn("the board did not answer after the run: channel",
                      soak.judge())

    def test_memory_growth_fails_only_when_a_limit_was_set(self):
        soak = self.healthy()
        soak.rss_first, soak.rss_last, soak.rss_max = 1000, 9000, 9000
        soak.rss_growth_max = 8000
        self.assertEqual([], soak.judge())
        self.assertEqual(8000, soak.memory()["growth_kb"])
        limited = self.healthy(**{"--max-memory-growth-kb": "4096"})
        limited.rss_first, limited.rss_last = 1000, 9000
        limited.rss_growth_max = 8000
        self.assertIn("the capture process grew by 8000 KiB, more than 4096 KiB",
                      limited.judge())

    def test_a_probe_that_was_not_heard_again_fails(self):
        soak = self.healthy()
        soak.options.restart_probe = True
        soak.probe_period_index = 1
        soak.periods[2].structural = addresssoak.Side()
        soak.periods[2].structural.close(soak.periods[2].ended,
                                         soak.periods[2].started)
        self.assertIs(False, soak.probe_outcome())
        self.assertIn("the advertiser was not heard again in the first judged on "
                      "period after the restart probe", soak.judge())

    def test_a_probe_with_no_judged_period_after_it_is_a_failure(self):
        # The plan asked for evidence that the restart path recovers, so a
        # run that could not produce it has not met what it was asked for.
        soak = self.healthy()
        soak.options.restart_probe = True
        soak.probe_period_index = 10  # after every period the run had
        self.assertIsNone(soak.probe_outcome())
        self.assertIn("no judged advertiser on period followed the restart probe, "
                      "so whether the capture recovered was never measured",
                      soak.judge())

    def test_a_cancellation_is_its_own_reason(self):
        soak = self.healthy()
        soak.canceled = "SIGTERM"
        self.assertIn("the run was stopped by SIGTERM", soak.judge())

    def test_a_run_with_no_judgeable_period_says_so_once(self):
        soak = self.soak()
        self.add_period(soak, "on", index=0, unplanned_gaps=1)
        self.assertIn("no advertiser on period ran in full, so nothing could be "
                      "judged", soak.judge())


class TimingTests(unittest.TestCase):
    """The relationships the catalog refuses before any board is leased."""

    def timings(self, **overrides):
        values = {
            "duration_s": 600.0, "on_s": 30.0, "off_s": 30.0, "window_s": 2.0,
            "windows_needed": 5, "liveness_timeout_s": 60.0,
            "detect_within_s": 10.0, "max_gap_s": 15.0, "sample_every_s": 30.0,
            "interval_ms": 100, "min_packets_per_minute": 10,
        }
        values.update(overrides)
        return addresssoak.Timings(**values)

    def test_a_consistent_set_is_accepted(self):
        addresssoak.check_timings(self.timings())

    def test_each_relationship_is_refused_with_its_reason(self):
        for overrides, reason in (
            ({"detect_within_s": 40.0}, "'detect within' (40s) is longer"),
            ({"windows_needed": 40}, "sustained presence"),
            ({"liveness_timeout_s": 30.0}, "'liveness timeout' (30s) is not longer"),
            ({"liveness_timeout_s": 35.0},
             "an off period and the wait for the next detection (30s + 10s)"),
            ({"max_gap_s": 5.0}, "'maximum gap' (5s) is shorter than 'detect "
                                 "within'"),
            ({"max_gap_s": 30.0}, "'maximum gap' (30s) is not shorter than the "
                                  "30s"),
            ({"window_s": 0.05}, "'presence window' (0.05s) is shorter"),
            ({"sample_every_s": 0.5}, "'sample every' (0.5s) is shorter"),
            ({"on_s": 0.5}, "'advertiser on' (0.5s) is shorter than 1s"),
            ({"off_s": 0.5}, "'advertiser off' (0.5s) is shorter than 1s"),
            ({"duration_s": 86400.0 * 3, "on_s": 1.0, "off_s": 1.0},
             "more than 20000"),
            ({"interval_ms": 3000}, "fewer than 3 advertising events in a 2s"),
            # One event per window is not enough either: the advertiser
            # delays each event a little and the air loses some.
            ({"interval_ms": 1000}, "fewer than 3 advertising events in a 2s"),
            ({"interval_ms": 10000, "window_s": 30.0, "windows_needed": 2,
              "on_s": 120.0, "min_packets_per_minute": 30},
             "about 6 packets a minute, fewer than the 30"),
            ({"duration_s": 60.0}, "less than two 60s schedule cycles"),
            ({"sample_every_s": 300.0}, "'sample every' (300s) leaves fewer"),
        ):
            with self.subTest(**overrides):
                with self.assertRaises(addresssoak.SoakError) as caught:
                    addresssoak.check_timings(self.timings(**overrides))
                self.assertIn(reason, str(caught.exception))

    def test_the_options_a_command_line_builds_carry_the_same_figures(self):
        parser = addresssoak.build_parser()
        args = parser.parse_args([
            "--out", "/tmp/x", "--serial", SERIAL, "--address", ADDRESS.lower(),
            "--channel", "38", "--duration", "600", "--jlink", "1050000001",
            "--interval", "100", "--payload", "NONE", "--advertiser-on", "30",
            "--advertiser-off", "30", "--sample-every", "30",
            "--presence-window", "2", "--presence-windows", "5",
            "--liveness-timeout", "60", "--detect-within", "10",
            "--min-detection-share", "0.9", "--max-gap", "15",
            "--min-packets-per-minute", "10", "--max-disagreement-share", "0.02",
            "--max-restarts", "0",
        ])
        options = addresssoak.options_from(args)
        self.assertEqual(ADDRESS, options.address)  # normalized
        self.assertEqual("", options.payload)  # "none"
        self.assertEqual(self.timings(), options.timings())


class AdvertiserShellTests(unittest.TestCase):
    """A dropped serial port is the failure the retry is written for."""

    def dk(self, opener):
        return nrfdk.Dk(1050000001, "/nonexistent", opener=opener)

    def test_a_shell_that_cannot_be_reopened_raises_a_soak_error(self):
        # Not an AttributeError on the next transition thirty seconds later:
        # every caller handles a SoakError, and a run that died of the other
        # would end in a traceback with no report.
        def opener(_path):
            raise nrfdk.NrfDkError("cannot open /nonexistent")

        advertiser = addresssoak.Advertiser(self.dk(opener))
        advertiser.shell = None
        with self.assertRaises(addresssoak.SoakError):
            advertiser.command("adv start")
        with self.assertRaises(addresssoak.SoakError):
            advertiser.status()

    def test_a_transition_is_retried_once_on_a_reopened_shell(self):
        opened = []

        class Dead:
            def command(self, _text, timeout=None):
                raise nrfdk.NrfDkError("the port went away")

            def status(self, timeout=None):
                raise nrfdk.NrfDkError("the port went away")

            def close(self):
                pass

        firmware = FakeFirmware(address=ADDRESS)

        def opener(path):
            opened.append(path)
            return nrfdk.Shell(FakePort(firmware))

        advertiser = addresssoak.Advertiser(self.dk(opener))
        advertiser.shell = Dead()
        self.assertEqual([], advertiser.command("adv start"))
        self.assertEqual(1, advertiser.retries)
        self.assertEqual(0, advertiser.errors)
        self.assertEqual(1, len(opened))


class MemoryTests(unittest.TestCase):
    """What the resident-memory read makes of what `ps` answers."""

    def answer(self, stdout="", returncode=0, raises=None):
        def run(argv, **_options):
            self.argv = argv
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout, "")
        return run

    def test_the_figure_is_the_first_field_of_the_line(self):
        self.assertEqual(12345, addresssoak.resident_kb(7, self.answer(" 12345\n")))
        self.assertEqual(["ps", "-o", "rss=", "-p", "7"], self.argv)

    def test_anything_else_costs_the_figure_and_nothing_else(self):
        for description, run in (
            ("no answer", self.answer("")),
            ("a process that has gone", self.answer("", returncode=1)),
            ("a line that is not a number", self.answer("what?\n")),
            ("a host that will not run it", self.answer(raises=PermissionError(1, "no"))),
            ("one that has not got it", self.answer(raises=FileNotFoundError())),
            ("one that does not answer", self.answer(
                raises=subprocess.TimeoutExpired(["ps"], 5.0))),
        ):
            with self.subTest(description):
                self.assertIsNone(addresssoak.resident_kb(7, run))


class SideTests(unittest.TestCase):
    """What one reader's sightings inside one period add up to."""

    def side(self, times, start=0.0, end=10.0, window=1.0):
        side = addresssoak.Side()
        for at in times:
            side.see(at, start, window)
        side.close(end, start)
        return side

    def test_windows_come_from_the_run_s_own_clock(self):
        # The index is the run's, not the period's, which is what makes a
        # window's neighbour its neighbour across a period boundary
        # (VerdictTests covers the comparison that rests on it).
        side = self.side([0.1, 0.9, 1.2, 5.5])
        self.assertEqual({0, 1, 5}, side.windows)
        later = self.side([10.1, 11.2], start=10.0, end=12.0)
        self.assertEqual({10, 11}, later.windows)
        self.assertEqual(4, side.packets)
        self.assertEqual(0.1, side.first)
        self.assertEqual(5.5, side.last)

    def test_the_gap_runs_from_the_period_start_and_to_its_end(self):
        side = self.side([4.0], start=0.0, end=10.0)
        # The leading gap is 4 s and the trailing one 6 s: the longer wins,
        # because a reader that stopped hearing it has the gap a service
        # would have noticed.
        self.assertEqual(6.0, side.max_gap)
        self.assertEqual(3.0, self.side([3.0, 4.0, 7.0], end=7.0).max_gap)

    def test_a_period_with_no_sighting_is_one_whole_gap(self):
        side = self.side([], start=2.0, end=12.0)
        self.assertEqual(10.0, side.max_gap)
        self.assertIsNone(side.first)

    def test_sustained_presence_needs_consecutive_windows(self):
        self.assertTrue(self.side([0.1, 1.1, 2.1]).sustained(3))
        self.assertFalse(self.side([0.1, 1.1, 3.1]).sustained(3))
        self.assertTrue(self.side([0.1, 1.1, 3.1, 4.1, 5.1]).sustained(3))
        self.assertTrue(self.side([]).sustained(0))
        self.assertFalse(self.side([]).sustained(1))


class PcapTailTests(unittest.TestCase):
    """The reader that follows a capture file while it is being written."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "capture.pcap"

    def tail(self):
        tail = addresssoak.PcapTail(self.path)
        self.addCleanup(tail.close)
        return tail

    def test_a_file_that_is_not_there_yet_is_no_error(self):
        tail = self.tail()
        self.assertEqual([], tail.poll())
        self.assertEqual("", tail.error)

    def test_records_arrive_as_they_are_written(self):
        tail = self.tail()
        self.path.write_bytes(fake_advertiser.PCAP_HEADER)
        self.assertEqual([], tail.poll())
        self.assertEqual(advcapture_link_type(), tail.link_type)
        data = fake_advertiser.record(ADDRESS, counter=1)
        with self.path.open("ab") as handle:
            handle.write(fake_advertiser.pcap_record(data, 7, 500_000_000))
        (found,) = tail.poll()
        self.assertEqual(7.5, found[0])
        self.assertEqual(data, found[1])
        self.assertEqual([], tail.poll())

    def test_half_a_record_is_kept_until_the_rest_arrives(self):
        tail = self.tail()
        data = fake_advertiser.record(ADDRESS)
        whole = fake_advertiser.pcap_record(data, 1, 0)
        self.path.write_bytes(fake_advertiser.PCAP_HEADER + whole[:20])
        self.assertEqual([], tail.poll())
        with self.path.open("ab") as handle:
            handle.write(whole[20:])
        self.assertEqual(1, len(tail.poll()))
        self.assertEqual(1, tail.records)

    def test_a_file_that_is_not_a_le_capture_is_reported_once(self):
        self.path.write_bytes(b"not a capture at all" + b"\x00" * 20)
        tail = self.tail()
        self.assertEqual([], tail.poll())
        self.assertIn("not PCAP", tail.error)
        self.assertEqual([], tail.poll())

    def test_a_record_claiming_more_than_a_capture_holds_is_refused(self):
        # One corrupt record header would otherwise make the reader hold
        # every later read until that many bytes arrived, which on a run of
        # days is the whole capture in memory.
        data = fake_advertiser.record(ADDRESS)
        bad = bytearray(fake_advertiser.pcap_record(data, 1, 0))
        bad[8:12] = (addresssoak.PcapTail.MAX_RECORD_LEN + 1).to_bytes(4, "little")
        self.path.write_bytes(fake_advertiser.PCAP_HEADER + bytes(bad))
        tail = self.tail()
        self.assertEqual([], tail.poll())
        self.assertIn("more than", tail.error)
        self.assertIn("262144", tail.error)
        # And it stops there rather than reading on past a file it cannot
        # make sense of.
        self.assertEqual([], tail.poll())

    def test_a_capture_of_another_link_type_is_refused(self):
        header = bytearray(fake_advertiser.PCAP_HEADER)
        header[20:24] = (255).to_bytes(4, "little")
        self.path.write_bytes(bytes(header))
        tail = self.tail()
        self.assertEqual([], tail.poll())
        self.assertIn("link type 255", tail.error)


def advcapture_link_type():
    from ubertooth_harness import advcapture

    return advcapture.LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR


class CancellationTests(SoakTestCase):
    """A signalled run still leaves a report, and leaves nothing running.

    The signal is a real one, delivered to this process, because what is
    under test is the handler the driver installs: the harness allows about
    two seconds between its SIGTERM and its SIGKILL, so the handler has to
    ask the capture to stop itself rather than wait for the loop to notice.
    """

    def setUp(self):
        super().setUp()
        held = {number: signal.getsignal(number) for number
                in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(number, handler)
                                 for number, handler in held.items()])

    def test_a_signal_ends_the_run_with_a_summary_and_no_capture_left(self):
        pids = []

        def signal_once():
            # Once the capture is really running, not before: a cancellation
            # that arrived before it started would test nothing.
            for _ in range(400):
                records = self._records()
                started = [record for record in records
                           if record["record"] == "capture_started" and record["pid"]]
                if started:
                    pids.append(started[0]["pid"])
                    time.sleep(0.5)
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=signal_once, daemon=True)
        thread.start()
        code, out, _err = self.run_soak(duration=120)
        thread.join(timeout=30)
        self.assertEqual(addresssoak.EXIT_FAIL, code, out)
        self.assertIn("SOAK VERDICT: fail", out)
        self.assertIn("FAIL: the run was stopped by SIGTERM", out)
        summary = self.summary()
        self.assertEqual("SIGTERM", summary["canceled"])
        # The capture was asked to stop and is gone, and the run stopped long
        # before the duration it was given.
        self.assertEqual("SIGTERM", summary["attempts"][-1]["signalled"])
        self.assertLess(summary["duration_s"], 60)
        self.assertEqual(1, len(pids))
        for _ in range(200):
            if _gone(pids[0]):
                break
            time.sleep(0.05)
        self.assertTrue(_gone(pids[0]), "the capture outlived the signalled run")

    def _records(self):
        try:
            return self.progress()
        except OSError:
            return []


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


if __name__ == "__main__":
    unittest.main()
