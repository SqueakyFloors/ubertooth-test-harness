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
            "--sample-every": "1", "--presence-window": "0.25",
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
        self.assertGreater(summary["rate_per_minute"]["minutes"], 0)
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

    def test_the_second_silent_restart_resets_the_board(self):
        # The recovery ladder: a clean restart that still hears nothing is
        # followed by one board reset before the next attempt.
        self.set_tool(MODE="records", RATE="0.01")
        # Long enough for three liveness restarts even on a loaded machine:
        # the reset is the second rung, so it needs the first restart to have
        # happened and the next capture to hear nothing either.
        self.run_soak(max_restarts=9, duration=14, liveness_timeout=2)
        self.assertGreaterEqual(self.summary()["process"]["board_resets"], 1)
        resets = [record for record in self.progress()
                  if record["record"] == "board_reset"]
        self.assertTrue(resets)

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
        self.assertIn(code, (addresssoak.EXIT_PASS, addresssoak.EXIT_FAIL))

    def test_a_capture_that_keeps_dying_is_restarted_and_recorded_each_time(self):
        self.set_tool(ONCE=str(self.root / "ran-once"), EXIT_AFTER="2",
                      EXIT_CODE="1")
        code, out, _err = self.run_soak(duration=8, max_restarts=10)
        self.assertEqual(addresssoak.EXIT_FAIL, code)
        summary = self.summary()
        self.assertGreater(summary["process"]["restarts"], 1)
        self.assertEqual(summary["process"]["restarts"],
                         summary["process"]["unexplained_exits"])
        # Every attempt kept its own exit status, its last error line, and
        # its own capture file and transcript.
        for attempt in summary["attempts"][:-1]:
            self.assertEqual(1, attempt["returncode"])
            self.assertIn("libusb returned -1", attempt["last_error"])
            self.assertTrue((self.out / attempt["capture"]).is_file())
            self.assertTrue((self.out / attempt["log"]).is_file())


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
        code, out, _err = self.run_soak()
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
        # Nothing was captured, and nothing was reset: the board belongs to
        # whatever has it.
        self.assertFalse((self.out / "capture-0001.pcap").exists())
        self.assertFalse((self.out / "summary.json").exists())

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
        soak.minute_packets = {minute: 60 for minute in range(0, 200)}
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
        soak.minute_packets = {minute: 60 for minute in range(200)}
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
        soak.minute_packets = {minute: 60 for minute in range(200)}
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
        soak.minute_packets = {minute: 60 for minute in range(200)}
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
        # One reader saw a window the other did not, nowhere near one of
        # theirs, in every period.
        for period in soak.periods:
            period.text.windows.add(99)
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
        # One nowhere near it is a disagreement, and is judged.
        period.text.windows.add(edge + 50)
        self.assertEqual(1, period.disagreements())
        self.assertEqual(1, soak.parity()["disagreements"])

    def test_a_reader_that_missed_a_whole_period_is_still_caught(self):
        soak = self.healthy()
        soak.periods[0].structural = addresssoak.Side()
        parity = soak.parity()
        self.assertGreater(parity["disagreements"], parity["allowance"])
        self.assertTrue(any("the readers disagreed about" in problem
                            for problem in soak.judge()), soak.judge())

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
        # One nowhere near it is a disagreement, and is judged.
        period.text.windows.add(edge + 50)
        self.assertEqual(1, period.disagreements())
        self.assertEqual(1, soak.parity()["disagreements"])

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
        # Heard at the start of the period and then not again.
        self.add_period(soak, "on", index=3, text=[0.5], structural=[0.5])
        soak.minute_packets = {minute: 60 for minute in range(200)}
        problems = soak.judge()
        self.assertEqual(29.5, soak.gaps()["structural_max_s"])
        self.assertTrue(any("gap with no advertiser packet longer than 10s"
                            in problem for problem in problems), problems)
        self.assertEqual(1, soak.gaps()["periods_over_limit"])

    def test_the_rate_floor_is_measured_over_the_minutes_the_advertiser_was_on(self):
        soak = self.healthy()
        soak.minute_packets = {0: 60, 1: 1}
        rate = soak.rate()
        # Four on periods, one a minute: minutes 0 to 3, and the minutes the
        # advertiser was off are not counted at all.
        self.assertEqual(4, rate["minutes"])
        self.assertEqual(15.2, rate["mean"])
        self.assertEqual([], soak.judge())
        soak.minute_packets = {0: 1}
        self.assertTrue(any("packets a minute, below 10" in problem
                            for problem in soak.judge()), soak.judge())

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
        self.assertEqual([], soak.judge())
        self.assertEqual(8000, soak.memory()["growth_kb"])
        limited = self.healthy(**{"--max-memory-growth-kb": "4096"})
        limited.rss_first, limited.rss_last = 1000, 9000
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

    def test_a_probe_with_no_judged_period_after_it_claims_nothing(self):
        soak = self.healthy()
        soak.options.restart_probe = True
        soak.probe_period_index = 10  # after every period the run had
        self.assertIsNone(soak.probe_outcome())
        self.assertEqual([], soak.judge())

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

    def test_windows_come_from_the_time_inside_the_period(self):
        side = self.side([0.1, 0.9, 1.2, 5.5])
        self.assertEqual({0, 1, 5}, side.windows)
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
