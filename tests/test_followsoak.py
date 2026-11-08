"""followsoak.py: the cycle, the follow, the checkpoints, the verdict.

Nothing here touches hardware. The two DKs are the fake shell every nrfdk.py
test uses, injected into the driver; the capture tool is a real child process
-- a fake `ubertooth-btle` on PATH that writes real PCAP records and prints
the text libbtbb prints -- because what the driver has to get right about it
is signals, exits, a file growing under it, and being stopped and started
again at a checkpoint, and a model of a process would not test any of that.
The records come from tests/fake_connection.py, built from the spec encoding,
so no captured traffic is used.

The scenario runs here are seconds long, with the same figures a plan
carries, so the whole loop is exercised: the three-phase cycle, the fixture's
own reporting, the readers, the windows, the planned watchdog checkpoints,
the liveness timeout, and the summary a long run is reviewed through. The
verdict tests after them drive the model directly, so each objective is
tested on its own and the answer cannot move with the machine's load.

`FAKE_UTIL` and `SERIAL` come from tests/test_addresssoak.py: the board reads
this driver makes are the same reads, in the same wording, and a second copy
of that fake would be a second place for it to drift.
"""

import io
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ubertooth_harness import addresssoak, followsoak, nrfdk, watchdog
from tests.executables import write_executable
from tests.fake_dk import FakeFirmware, FakePort
from tests.test_addresssoak import FAKE_UTIL, SERIAL

ROOT = Path(__file__).resolve().parents[1]
CENTRAL = "C0:00:00:00:00:02"
PERIPHERAL = "C0:00:00:00:00:03"
CENTRAL_JLINK = "1050000002"
PERIPHERAL_JLINK = "1050000003"

# A fake `ubertooth-btle -f`. It writes a PCAP exactly as the real tool's -q
# does and prints the text block libbtbb prints for an advertisement, so the
# driver's readers have real work to do. What it emits follows the phase the
# fixture is in, which it reads from a file the fake DKs write (PhaseFirmware
# below): the tool is a separate process, so that is the only way it can
# advertise, carry a connection, and fall silent in step with the cycle.
FAKE_BTLE = f"""#!/usr/bin/env python3
import os, signal, sys, time
sys.path.insert(0, {str(ROOT)!r})
from tests import fake_connection

central = os.environ.get("FAKE_BTLE_CENTRAL", {CENTRAL!r})
peripheral = os.environ.get("FAKE_BTLE_PERIPHERAL", {PERIPHERAL!r})
rate = float(os.environ.get("FAKE_BTLE_RATE", "20"))
state_path = os.environ.get("FAKE_BTLE_STATE", "")
# both: the advertiser and the connection. advertising: the advertiser only,
# which is a board that never followed the link. deaf: nothing at all.
mode = os.environ.get("FAKE_BTLE_MODE", "both")
exit_after = float(os.environ.get("FAKE_BTLE_EXIT_AFTER", "0"))
exit_code = int(os.environ.get("FAKE_BTLE_EXIT_CODE", "0"))
# Keep reporting the advertiser and the connection this long into an absent
# phase: a fixture whose going away the board did not notice.
linger = float(os.environ.get("FAKE_BTLE_LINGER", "0"))
if os.environ.get("FAKE_BTLE_IGNORE_SIGTERM"):
    signal.signal(signal.SIGTERM, lambda *a: None)

path = sys.argv[sys.argv.index("-q") + 1]
handle = open(path, "wb", buffering=0)
handle.write(fake_connection.PCAP_HEADER)


def put(record, when):
    handle.write(fake_connection.pcap_record(
        record, int(when), int((when % 1) * 1e9)))


started = time.time()
# Seeded from this process, so a capture started again at a checkpoint
# announces connections nobody has seen before, which is what a board
# following fresh links reports.
access_address = 0x11000000 | ((os.getpid() & 0xFFFF) << 4)
crc_init = 0x500000 | (os.getpid() & 0xFFF)
counter = 0
previous = ""
left_at = None
while True:
    now = time.time()
    if exit_after and now - started >= exit_after:
        if exit_code:
            print("error: the capture stopped: libusb returned -1", flush=True)
        sys.exit(exit_code)
    try:
        state = open(state_path).read().strip() if state_path else "advertising"
    except OSError:
        state = ""
    if state != previous:
        if state == "connected" and mode == "both":
            # The CONNECT_IND the central sends: one per connection, which is
            # what the driver measures its follow acquisition from.
            access_address += 1
            crc_init += 1
            put(fake_connection.connect_ind(
                central, peripheral, access_address=access_address,
                crc_init=crc_init), now)
        left_at = now if state == "absent" else None
        previous = state
    lingering = (state == "absent" and linger and left_at is not None
                 and now - left_at < linger)
    if mode != "deaf" and (state == "advertising" or lingering):
        put(fake_connection.adv_ind(peripheral), now)
        sys.stdout.write(fake_connection.text_block(peripheral))
        sys.stdout.flush()
    if mode == "both" and (state == "connected" or lingering):
        counter += 1
        put(fake_connection.data_packet(access_address, crc_init,
                                        counter=counter), now)
    time.sleep(1.0 / rate)
"""


class PhaseFirmware(FakeFirmware):
    """A fake DK that writes the phase the fixture is in where the tool sees it.

    The driver's transitions are shell commands, and the capture tool is
    another process: this is what puts the two in step. The peripheral
    starting is the advertising phase, the central starting is the connected
    phase, and either one stopping is the fixture off the air -- which is
    exactly the order followsoak.`open_phase` sends them in.

    The central also counts a write on every status read while it is
    connected, which is what the real firmware does as it writes to the link:
    the driver checks that the central wrote something over the phase, so a
    stand-in whose counter never moved would report a fixture that made the
    link and sent nothing.
    """

    def __init__(self, state_path: Path, role: str, **options):
        self.state_path = Path(state_path)
        super().__init__(**options)
        # Left in the `advertiser` role, which is where `fixture-ready`'s own
        # cleanup leaves both boards (`nrfdk.py idle` sends `role
        # advertiser`). So the driver has to put each board in its role
        # itself, and a stand-in built already in that role would hide it.
        self.role = "advertiser"
        if role == "peripheral":
            self.state_path.write_text("absent", encoding="utf-8")

    def status(self):
        if self.role == "central" and self.connected:
            self.writes += 1
        return super().status()

    def handle(self, line: str):
        answer = super().handle(line)
        if answer and answer[-1].startswith("ERR"):
            return answer
        if line == "conn start":
            self.state_path.write_text(
                "connected" if self.role == "central" else "advertising",
                encoding="utf-8")
        elif line in ("conn stop", "conn defaults"):
            self.state_path.write_text("absent", encoding="utf-8")
        return answer


class SoakTestCase(unittest.TestCase):
    """One temporary bench: a PATH with the fakes on it and an output dir."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.out = self.root / "out"
        self.bin = self.root / "bin"
        self.state = self.root / "phase"
        write_executable(self.bin / "ubertooth-btle", FAKE_BTLE)
        write_executable(self.bin / "ubertooth-util", FAKE_UTIL)
        self.environment = dict(os.environ)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        self.addCleanup(self._restore_environment)
        self.peripheral_firmware = PhaseFirmware(self.state, "peripheral",
                                                 address=PERIPHERAL)
        self.central_firmware = PhaseFirmware(self.state, "central",
                                              address=CENTRAL)

    def _restore_environment(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def set_tool(self, **values):
        for key, value in values.items():
            os.environ[f"FAKE_BTLE_{key}"] = str(value)
            self.addCleanup(os.environ.pop, f"FAKE_BTLE_{key}", None)

    def _dk(self, firmware, serial, alias):
        port = FakePort(firmware)
        return nrfdk.Dk(int(serial), str(self.root / alias),
                        opener=lambda _p: nrfdk.Shell(port), alias=alias)

    def argv(self, **overrides):
        values = {
            "--out": str(self.out), "--serial": SERIAL, "--channel": "37",
            "--duration": "14",
            "--peripheral-jlink": PERIPHERAL_JLINK,
            "--peripheral-address": PERIPHERAL,
            "--central-jlink": CENTRAL_JLINK, "--central-address": CENTRAL,
            "--advertising": "1.5", "--connected": "1", "--absent": "1",
            "--checkpoint-every": "7", "--sample-every": "1",
            "--conn-interval": "30",
            "--write-bytes": "244", "--write-period": "50",
            "--presence-window": "0.5", "--presence-windows": "2",
            "--liveness-timeout": "4", "--detect-within": "0.5",
            "--connect-within": "0.5", "--acquire-within": "0.75",
            "--quiet-after": "0.3", "--max-gap": "1",
            "--min-detection-share": "0.9", "--min-follow-share": "0.8",
            "--min-connection-packets": "3", "--max-restarts": "0",
            "--restart-min-interval": "0.2", "--term-grace": "1",
        }
        for key, value in overrides.items():
            option = "--" + key.replace("_", "-")
            if value is None:
                values.pop(option, None)
            else:
                values[option] = str(value)
        return [item for pair in values.items() for item in pair]

    def run_soak(self, **overrides):
        """(exit status, stdout, stderr) of one driver run."""
        self.set_tool(STATE=str(self.state))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = followsoak.main(
                self.argv(**overrides),
                peripheral=self._dk(self.peripheral_firmware,
                                    PERIPHERAL_JLINK, "DK_Linux2"),
                central=self._dk(self.central_firmware, CENTRAL_JLINK,
                                 "DK_Linux1"))
        return code, out.getvalue(), err.getvalue()

    def summary(self) -> dict:
        return json.loads((self.out / "summary.json").read_text(encoding="utf-8"))

    def progress(self) -> list:
        lines = (self.out / "progress.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in lines.splitlines() if line.strip()]


class ScenarioTests(SoakTestCase):
    """Short real runs: the loop, the fixture, the tool, and the files."""

    def test_a_healthy_run_passes_and_reports_every_part_of_the_cycle(self):
        code, out, err = self.run_soak()
        self.assertEqual(0, code, out + err)
        self.assertIn("SOAK VERDICT: pass", out)
        summary = self.summary()
        self.assertEqual("pass", summary["verdict"])
        self.assertEqual([], summary["problems"])
        # Every phase of the cycle ran and some of each was judged.
        for state in ("advertising", "connected", "absent"):
            with self.subTest(state):
                self.assertGreater(summary["cycle"][state]["ran"], 1)
                self.assertGreater(summary["cycle"][state]["judged"], 0)
        # The advertiser was found by both readers; the connection was
        # acquired from its own CONNECT_IND and carried packets.
        self.assertGreater(summary["detection"]["structural"]["detected"], 0)
        self.assertGreater(summary["detection"]["text"]["detected"], 0)
        self.assertGreater(summary["follow"]["acquired"], 0)
        self.assertGreater(summary["connections"]["connect_inds"], 0)
        self.assertGreater(summary["connections"]["valid_packets"], 0)
        # Data Length Extension: the fixture writes 244-byte payloads, which
        # a link without it could not carry.
        self.assertGreater(summary["connections"]["long_packets"], 0)
        # And nothing was attributed to a connection the run did not announce.
        self.assertEqual(0, summary["connections"]["other_connect_inds"])
        self.assertEqual(
            0, summary["connections"]["packets_of_connections_not_announced"])

    def test_each_dk_is_driven_into_its_role_before_it_is_configured(self):
        """`fixture-ready` releases both boards into the `advertiser` role.

        Its cleanup is `nrfdk.py idle`, which sends `role advertiser`, so
        neither board is in the role this soak needs by the time the driver
        runs. The driver sends the role itself, before every connection
        setting, because `role` restores both roles' boot settings.
        """
        self.assertEqual("advertiser", self.peripheral_firmware.role)
        self.assertEqual("advertiser", self.central_firmware.role)
        code, out, err = self.run_soak()
        self.assertEqual(0, code, out + err)
        for firmware, role in ((self.peripheral_firmware, "peripheral"),
                               (self.central_firmware, "central")):
            with self.subTest(role):
                self.assertIn(f"role {role}", firmware.commands)
                self.assertLess(firmware.commands.index(f"role {role}"),
                                firmware.commands.index("conn dle on"))
                self.assertEqual(role, firmware.role)

    def test_a_reacquisition_is_measured_for_every_phase_after_an_absence(self):
        code, out, err = self.run_soak()
        self.assertEqual(0, code, out + err)
        found = self.summary()["detection"]["structural"]
        # The first advertising phase is not a reacquisition; every later one
        # is, so a run of three cycles measures at least one.
        self.assertGreater(found["reacquisitions"], 0)
        self.assertLess(found["reacquisitions"], found["phases"] + 1)
        self.assertIsNotNone(found["median_reacquisition_s"])
        self.assertIn("reacquisition:", out)

    def test_a_checkpoint_stops_the_capture_reads_the_board_and_starts_another(self):
        code, out, err = self.run_soak()
        self.assertEqual(0, code, out + err)
        summary = self.summary()
        checkpoints = summary["checkpoints"]
        self.assertGreater(checkpoints["taken"], 0)
        self.assertEqual([], checkpoints["problems"])
        for mark in checkpoints["records"]:
            with self.subTest(mark["index"]):
                self.assertTrue(mark["planned"])
                self.assertTrue(mark["stopped_cleanly"])
                self.assertTrue(mark["board_idle"])
                self.assertTrue(mark["watchdog_read"])
                self.assertTrue(mark["capture_restarted"])
        # A checkpoint is not a restart and not a recovery.
        self.assertEqual(0, summary["process"]["restarts"])
        self.assertEqual(0, summary["process"]["board_resets"])
        # Each one leaves its own capture file behind, so the run has one
        # more attempt than it had checkpoints.
        self.assertGreaterEqual(summary["process"]["attempts"],
                                checkpoints["taken"] + 1)
        files = sorted(path.name for path in self.out.glob("capture-*.pcap"))
        self.assertGreaterEqual(len(files), checkpoints["taken"] + 1)
        # The phase a checkpoint happened in records it as a planned gap, not
        # an unplanned one.
        phases = [entry for entry in self.progress()
                  if entry.get("record") == "phase" and entry["checkpoints"]]
        self.assertTrue(phases)
        for entry in phases:
            with self.subTest(entry["index"]):
                self.assertEqual("absent", entry["state"])
                self.assertGreater(entry["planned_capture_gaps"], 0)
                self.assertEqual(0, entry["unplanned_capture_gaps"])

    def test_every_watchdog_read_closes_an_interval_the_counters_belong_to(self):
        code, out, err = self.run_soak()
        self.assertEqual(0, code, out + err)
        summary = self.summary()
        # One read before the run, one per checkpoint, one at the end.
        self.assertEqual(summary["watchdog"]["reads"],
                         summary["checkpoints"]["taken"] + 2)
        self.assertEqual(0, summary["watchdog"]["board_would_not_answer"])
        self.assertEqual(0, summary["watchdog"]["not_reported_by_this_firmware"])
        intervals = [entry for entry in self.progress()
                     if entry.get("record") == "interval"]
        self.assertEqual(summary["watchdog"]["reads"], len(intervals))
        for entry in intervals:
            with self.subTest(entry["index"]):
                self.assertTrue(entry["watchdog_read"])
                self.assertIsNotNone(entry["ended_s"])
                self.assertTrue(entry["ended_by"])
        # The intervals cover the run end to end, in order.
        for before, after in zip(intervals, intervals[1:]):
            self.assertLessEqual(before["ended_s"], after["started_s"] + 0.01)

    def test_a_fixture_the_board_keeps_reporting_fails_the_absence_check(self):
        # The tool goes on reporting the advertiser and the connection for the
        # whole absent phase, which is a board that did not notice either
        # going away.
        self.set_tool(LINGER="5")
        code, out, err = self.run_soak()
        self.assertEqual(1, code, out + err)
        summary = self.summary()
        problems = summary["problems"]
        self.assertTrue(any("did not stop when it was told to" in problem
                            for problem in problems), problems)
        # The follow outliving its connection is measured end to end here --
        # a short run judges too few absent phases for it to outvote its own
        # allowance, which VerdictTests covers with a population that can.
        self.assertGreater(summary["absence"]["still_connected"], 0)

    def test_a_capture_that_never_followed_the_link_fails_the_follow(self):
        # Advertising only: the board found the peripheral every cycle and
        # never acquired the connection, which the detection objective would
        # not have caught.
        self.set_tool(MODE="advertising")
        code, out, err = self.run_soak()
        self.assertEqual(1, code, out + err)
        problems = self.summary()["problems"]
        self.assertTrue(any("no CONNECT_IND between the two registered DKs"
                            in problem for problem in problems), problems)

    def test_a_capture_that_exits_by_itself_is_restarted_and_counted(self):
        self.set_tool(EXIT_AFTER="2", EXIT_CODE="1")
        code, out, err = self.run_soak()
        self.assertEqual(1, code, out + err)
        summary = self.summary()
        self.assertGreater(summary["process"]["restarts"], 0)
        problems = summary["problems"]
        self.assertTrue(any("unplanned restart" in problem
                            for problem in problems), problems)
        # A restart reads the board's record too, so it closes an interval of
        # its own: more reads than the checkpoints and the two ends.
        self.assertGreater(summary["watchdog"]["reads"],
                           summary["checkpoints"]["taken"] + 2)

    def test_a_cancelled_run_still_writes_its_summary(self):
        def interrupt():
            time.sleep(3.0)
            os.kill(os.getpid(), signal.SIGINT)

        thread = threading.Thread(target=interrupt, daemon=True)
        thread.start()
        code, out, err = self.run_soak(duration=60, checkpoint_every=20)
        thread.join(timeout=5)
        self.assertEqual(1, code, out + err)
        summary = self.summary()
        self.assertEqual("SIGINT", summary["canceled"])
        self.assertTrue(any("stopped by SIGINT" in problem
                            for problem in summary["problems"]),
                        summary["problems"])


class ConfigurationTests(SoakTestCase):
    """What the driver refuses before it touches a board."""

    def test_two_dks_at_one_address_are_refused(self):
        code, _out, err = self.run_soak(central_address=PERIPHERAL)
        self.assertEqual(followsoak.EXIT_CONFIGURATION, code)
        self.assertIn("registered at the same BLE address", err)

    def test_one_dk_in_both_roles_is_refused(self):
        code, _out, err = self.run_soak(central_jlink=PERIPHERAL_JLINK)
        self.assertEqual(followsoak.EXIT_CONFIGURATION, code)
        self.assertIn("this needs two DKs", err)

    def test_a_board_that_does_not_answer_is_a_configuration_failure(self):
        os.environ["FAKE_UTIL_BUSY"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_UTIL_BUSY", None)
        code, out, err = self.run_soak()
        self.assertEqual(followsoak.EXIT_CONFIGURATION, code)
        self.assertIn("the Ubertooth does not answer", out + err)
        # Never reset to make it work, and the run still leaves its record.
        self.assertEqual("configuration", self.summary()["verdict"])

    def test_a_capture_file_limit_above_the_budget_is_refused(self):
        code, _out, err = self.run_soak(max_capture_bytes=2000,
                                        max_total_capture_bytes=1000)
        self.assertEqual(followsoak.EXIT_CONFIGURATION, code)
        self.assertIn("is what the whole run's captures may cost", err)

    def test_a_floor_of_no_connection_packets_is_refused(self):
        code, _out, err = self.run_soak(min_connection_packets=0)
        self.assertEqual(followsoak.EXIT_CONFIGURATION, code)
        self.assertIn("--min-connection-packets must be at least 1", err)


class TimingTests(unittest.TestCase):
    """The relationships a plan has to satisfy, checked with no bench at all.

    The catalog raises these same words when a plan loads
    (catalog._check_follow_soak), so a plan whose figures contradict each
    other fails before any board is leased.
    """

    def timings(self, **overrides) -> followsoak.Timings:
        values = {
            "duration_s": 14400.0, "advertising_s": 20.0, "connected_s": 30.0,
            "absent_s": 20.0, "checkpoint_every_s": 1800.0,
            "sample_every_s": 60.0, "window_s": 2.0, "windows_needed": 5,
            "liveness_timeout_s": 120.0, "detect_within_s": 10.0,
            "connect_within_s": 10.0, "acquire_within_s": 15.0,
            "quiet_after_s": 3.0, "max_gap_s": 15.0,
            "write_period_ms": 50, "min_connection_packets": 3,
        }
        values.update(overrides)
        return followsoak.Timings(**values)

    def refuses(self, **overrides) -> str:
        with self.assertRaises(followsoak.SoakError) as caught:
            followsoak.check_timings(self.timings(**overrides))
        return str(caught.exception)

    def test_the_committed_figures_are_accepted(self):
        followsoak.check_timings(self.timings())

    def test_a_leg_shorter_than_a_second_is_refused(self):
        self.assertIn("every leg of the cycle is a phase the run holds",
                      self.refuses(advertising_s=0.5))

    def test_a_detection_deadline_past_its_own_phase_is_refused(self):
        self.assertIn("could end before its own detection deadline",
                      self.refuses(detect_within_s=25.0))

    def test_sustained_presence_that_does_not_fit_is_refused(self):
        self.assertIn("does not fit in the 20s the peripheral advertises",
                      self.refuses(windows_needed=20))

    def test_a_connect_deadline_past_the_connected_phase_is_refused(self):
        self.assertIn("could end before the central was asked to have connected",
                      self.refuses(connect_within_s=40.0, acquire_within_s=40.0))

    def test_a_follow_deadline_past_the_connected_phase_is_refused(self):
        self.assertIn("could end before its own follow deadline",
                      self.refuses(acquire_within_s=40.0))

    def test_a_link_allowed_up_after_the_follow_deadline_is_refused(self):
        self.assertIn("would miss the follow deadline measured from the same "
                      "moment",
                      self.refuses(connect_within_s=20.0, acquire_within_s=15.0))

    def test_a_settle_time_that_leaves_no_stretch_to_judge_is_refused(self):
        self.assertIn("too short a stretch to judge it on",
                      self.refuses(quiet_after_s=20.0))
        # And one that leaves a stretch too short to have held an
        # advertising event, which would make every absent phase judged and
        # none of them failable.
        self.assertIn("too short a stretch to judge it on",
                      self.refuses(quiet_after_s=19.8))

    def test_a_liveness_timeout_the_cycle_itself_would_trip_is_refused(self):
        # The difference from the address soak: this fixture is deliberately
        # off the air for a connected phase and an absent phase together.
        self.assertIn("so the cycle's own silence would restart the capture",
                      self.refuses(liveness_timeout_s=45.0))

    def test_a_gap_limit_below_the_detection_deadline_is_refused(self):
        self.assertIn("the wait for the first packet of a phase would fail it",
                      self.refuses(max_gap_s=5.0))

    def test_a_gap_limit_no_phase_could_reach_is_refused(self):
        self.assertIn("no gap inside a phase could ever reach it",
                      self.refuses(max_gap_s=25.0))

    def test_a_checkpoint_in_every_absent_phase_is_refused(self):
        self.assertIn("would interrupt every absent phase and leave the "
                      "fixture's going away unmeasured",
                      self.refuses(checkpoint_every_s=100.0))

    def test_a_checkpoint_interval_longer_than_half_the_run_is_refused(self):
        self.assertIn("so the watchdog record would not be read inside the "
                      "window and compared",
                      self.refuses(checkpoint_every_s=10000.0))

    def test_a_run_too_short_to_compare_is_refused(self):
        self.assertIn("less than three 70s cycles",
                      self.refuses(duration_s=140.0, checkpoint_every_s=70.0,
                                   sample_every_s=10.0))

    def test_a_window_too_narrow_for_the_firmwares_own_interval_is_refused(self):
        # The peripheral role has no interval command, so this is a check on
        # the window alone against what the firmware really sends.
        self.assertIn("advertising events in a 0.4s presence window",
                      self.refuses(window_s=0.4, windows_needed=2))

    def test_a_packet_floor_above_what_the_central_sends_is_refused(self):
        self.assertIn("more than the about 600 writes the central makes",
                      self.refuses(min_connection_packets=900))

    def test_too_many_phases_to_hold_is_refused(self):
        self.assertIn("phases, more than", self.refuses(
            duration_s=72 * 3600.0, advertising_s=1.0, connected_s=1.0,
            absent_s=1.0, detect_within_s=1.0, max_gap_s=1.0,
            acquire_within_s=1.0, connect_within_s=1.0, quiet_after_s=0.5,
            window_s=0.5, windows_needed=1, liveness_timeout_s=10.0,
            checkpoint_every_s=3600.0))


class VerdictTests(unittest.TestCase):
    """Each objective on its own, from a model of a finished run.

    No process, no board, no clock: the phases and the counters are set to
    exactly the run being asked about, so each objective is tested on its own
    and the answer cannot move with the machine's load. The scenario tests
    above cover the parts a model could not: signals, exits, a file growing
    under the reader, and a capture stopped and started again.
    """

    ADVERTISING = 20.0
    CONNECTED = 30.0
    ABSENT = 20.0

    def soak(self, **overrides):
        values = {
            "--out": "/tmp/follow-soak", "--serial": SERIAL, "--channel": "37",
            "--duration": "14400",
            "--peripheral-jlink": PERIPHERAL_JLINK,
            "--peripheral-address": PERIPHERAL,
            "--central-jlink": CENTRAL_JLINK, "--central-address": CENTRAL,
            "--advertising": str(self.ADVERTISING),
            "--connected": str(self.CONNECTED), "--absent": str(self.ABSENT),
            "--checkpoint-every": "1800", "--sample-every": "60",
            "--conn-interval": "30",
            "--write-bytes": "244", "--write-period": "50",
            "--presence-window": "2", "--presence-windows": "5",
            "--liveness-timeout": "120", "--detect-within": "10",
            "--connect-within": "10", "--acquire-within": "15",
            "--quiet-after": "3", "--max-gap": "15",
            "--min-detection-share": "0.9", "--min-follow-share": "0.8",
            "--min-connection-packets": "3", "--max-restarts": "0",
        }
        values.update({key: str(value) for key, value in overrides.items()})
        argv = [item for pair in values.items() for item in pair]
        options = followsoak.options_from(
            followsoak.build_parser().parse_args(argv))
        soak = followsoak.Soak(options, None, None)
        # A healthy run, which each test below then spoils in one way.
        soak.advertising_packets = 600
        soak.text_packets = 600
        soak.connect_inds = 10
        soak.connection_packets = 200
        soak.valid_connection_packets = 50
        soak.long_connection_packets = 20
        soak.checkpoints = [followsoak.Checkpoint(
            0, 1800.0, 3, stopped_cleanly=True, board_idle=True,
            watchdog_read=True, capture_restarted=True)]
        soak.board_after = {key: {"returncode": 0, "answered": True}
                            for key in ("firmware", "serial", "channel")}
        return soak

    def length(self, state: str) -> float:
        return {followsoak.ADVERTISING: self.ADVERTISING,
                followsoak.CONNECTED: self.CONNECTED,
                followsoak.ABSENT: self.ABSENT}[state]

    def add_phase(self, soak, state, *, seen=(), text=None, seconds=None,
                  **flags):
        """One phase, with the sightings the record reader had inside it."""
        index = len(soak.phases)
        started = sum(self.length(phase.state) for phase in soak.phases)
        length = self.length(state) if seconds is None else seconds
        previous = soak.phases[-1] if soak.phases else None
        phase = followsoak.Phase(
            index, state, index // len(followsoak.PHASES), started,
            after_absence=previous is not None
            and previous.state == followsoak.ABSENT,
            **flags)
        for name, times in (("structural", seen),
                            ("text", seen if text is None else text)):
            side = getattr(phase, name)
            for at in times:
                side.see(started + at, started, soak.options.window_s)
        phase.ended = started + length
        if state == followsoak.ADVERTISING:
            phase.structural.close(phase.ended, started)
            phase.text.close(phase.ended, started)
        soak.phases.append(phase)
        return phase

    def seen_throughout(self):
        """Sighting times that detect at once and hold the whole phase."""
        return [0.5 + step * 0.5 for step in range(int(self.ADVERTISING * 2) - 1)]

    def add_cycle(self, soak, *, acquire_at=2.0, packets=10, late=0,
                  seen=None, **absent_flags):
        """One whole cycle of a healthy run, with the link it carried."""
        self.add_phase(soak, followsoak.ADVERTISING,
                       seen=self.seen_throughout() if seen is None else seen)
        connected = self.add_phase(soak, followsoak.CONNECTED)
        if acquire_at is not None:
            link = followsoak.Link(0x11110000 + connected.index, 0x555000,
                                   connected.started + acquire_at,
                                   connected.index, packets=packets,
                                   valid_packets=packets, late=late)
            connected.acquired_at = connected.started + acquire_at
            connected.connect_inds = 1
            connected.link = link
        self.add_phase(soak, followsoak.ABSENT, **absent_flags)

    def healthy(self, cycles=4, **overrides):
        soak = self.soak(**overrides)
        for _ in range(cycles):
            self.add_cycle(soak)
        return soak

    def test_a_healthy_run_has_nothing_to_report(self):
        self.assertEqual([], self.healthy().judge())

    def test_a_late_first_sighting_is_a_missed_detection(self):
        soak = self.soak()
        self.add_cycle(soak, seen=[12.0, 12.5, 13.0, 13.5, 14.0, 14.5])
        for _ in range(3):
            self.add_cycle(soak)
        problems = soak.judge()
        self.assertTrue(any("found within 10s in 3 of 4 advertising phases"
                            in problem for problem in problems), problems)

    def test_presence_that_is_not_sustained_is_reported_apart_from_detection(self):
        soak = self.soak()
        for _ in range(4):
            # Seen at once in every phase, but never in five windows in a row.
            self.add_cycle(soak, seen=[0.5, 11.0])
        problems = soak.judge()
        self.assertTrue(any("sustained presence (5 x 2s windows) was reached in "
                            "0 of 4" in problem for problem in problems),
                        problems)
        self.assertFalse(any("found within" in problem for problem in problems),
                         problems)

    def test_a_connection_the_board_did_not_acquire_in_time_fails_the_follow(self):
        soak = self.soak()
        # Acquired, but after the deadline, in three of the four cycles.
        for _ in range(3):
            self.add_cycle(soak, acquire_at=25.0)
        self.add_cycle(soak)
        problems = soak.judge()
        self.assertTrue(any("acquired within 15s in 1 of 4 connected phases"
                            in problem for problem in problems), problems)

    def test_a_connection_that_brought_too_few_packets_fails_the_follow(self):
        soak = self.soak()
        for _ in range(3):
            self.add_cycle(soak, packets=1)
        self.add_cycle(soak)
        problems = soak.judge()
        self.assertTrue(any("at least 3 packet(s) of the connection arrived in "
                            "1 of 4" in problem for problem in problems),
                        problems)

    def test_a_follow_that_outlived_its_connection_fails_the_absence(self):
        soak = self.soak()
        for _ in range(3):
            self.add_cycle(soak, late=5)
        self.add_cycle(soak)
        problems = soak.judge()
        self.assertTrue(any("the follow did not end when the connection did"
                            in problem for problem in problems), problems)

    def test_an_advertiser_that_did_not_stop_fails_the_absence(self):
        soak = self.soak()
        for _ in range(4):
            self.add_cycle(soak)
        # Heard well past the settle point in three of the four absent phases.
        for phase in soak.phases:
            if phase.state == followsoak.ABSENT and phase.index < 10:
                phase.structural.see(phase.started + 10.0, phase.started,
                                     soak.options.window_s)
        problems = soak.judge()
        self.assertTrue(any("did not stop when it was told to" in problem
                            for problem in problems), problems)

    def test_the_coverage_floor_catches_a_capture_that_kept_dying(self):
        soak = self.soak()
        self.add_cycle(soak)
        for _ in range(3):
            # Every phase of these cycles had a stretch with nothing
            # listening, and none of them was the run's own doing.
            self.add_phase(soak, followsoak.ADVERTISING, unplanned_gaps=1)
            self.add_phase(soak, followsoak.CONNECTED, unplanned_gaps=1)
            self.add_phase(soak, followsoak.ABSENT, unplanned_gaps=1)
        problems = soak.judge()
        self.assertTrue(any("only 1 of 4 advertising phases could be judged"
                            in problem for problem in problems), problems)

    def test_a_checkpoint_does_not_count_against_the_coverage_floor(self):
        soak = self.soak()
        # Half the absent phases hold a checkpoint, which is what a legal
        # checkpoint interval produces (one every two cycles). A phase with a
        # planned gap is not judged, and the run is not blamed for it either:
        # the coverage floor credits the ones it chose to interrupt.
        for index in range(4):
            if index % 2:
                self.add_cycle(soak, planned_gaps=1, checkpoints=1)
            else:
                self.add_cycle(soak)
        counts = soak.absent_counts()
        self.assertEqual(2, counts["judged"])
        self.assertEqual(2, counts["planned_capture_gap"])
        problems = soak.judge()
        self.assertFalse(any("absent phase" in problem for problem in problems),
                         problems)

    def test_an_absence_nothing_could_be_judged_from_is_not_a_pass(self):
        soak = self.soak()
        for _ in range(4):
            # Cut short rather than interrupted on purpose: nothing says the
            # fixture went away, and the run has to say so.
            self.add_cycle(soak)
        for phase in soak.phases:
            if phase.state == followsoak.ABSENT:
                phase.ended = phase.started + 1.0
        problems = soak.judge()
        self.assertTrue(any("was never measured" in problem
                            for problem in problems), problems)

    def test_a_run_that_took_no_checkpoint_fails(self):
        soak = self.healthy()
        soak.checkpoints = []
        problems = soak.judge()
        self.assertIn("the run took no watchdog checkpoint", problems)

    def test_a_checkpoint_that_could_not_read_the_board_fails(self):
        soak = self.healthy()
        soak.checkpoints = [followsoak.Checkpoint(
            0, 1800.0, 3, stopped_cleanly=True, board_idle=False,
            board_reason="error: no Ubertooth found")]
        problems = soak.judge()
        self.assertTrue(any("checkpoint 0: the board did not answer afterwards"
                            in problem for problem in problems), problems)

    def test_a_checkpoint_whose_capture_had_to_be_killed_fails(self):
        soak = self.healthy()
        soak.checkpoints = [followsoak.Checkpoint(0, 1800.0, 3)]
        problems = soak.judge()
        self.assertTrue(any("did not stop on SIGTERM and had to be killed"
                            in problem for problem in problems), problems)

    def test_a_checkpoint_that_left_the_board_in_a_mode_fails(self):
        soak = self.healthy()
        attempt = followsoak.Attempt(2, (), "capture-0002.pcap", "btle-0002.log")
        attempt.mode_warning = True
        soak.attempts.append(attempt)
        soak.checkpoint_attempts.add(2)
        problems = soak.judge()
        self.assertTrue(any("found the board still in a mode" in problem
                            for problem in problems), problems)

    def test_a_watchdog_read_the_board_would_not_answer_fails(self):
        soak = self.healthy()
        soak.watchdog_unreadable = 2
        soak.watchdog_last_reason = addresssoak.WATCHDOG_BOARD_SILENT
        problems = soak.judge()
        self.assertTrue(any("2 watchdog read(s) found a board that would not "
                            "answer" in problem for problem in problems),
                        problems)

    def test_firmware_that_does_not_report_the_record_is_not_a_failure(self):
        # watchdog.py's own doctrine: a board that will not answer is one the
        # run cannot use again, and firmware without the record is reported
        # and judged by a person.
        soak = self.healthy()
        soak.watchdog_not_reported = 3
        soak.watchdog_last_reason = addresssoak.WATCHDOG_NOT_REPORTED
        self.assertEqual([], soak.judge())

    def test_a_checkpoint_the_cancellation_cut_short_is_not_its_own_failure(self):
        soak = self.healthy()
        soak.canceled = "SIGINT"
        soak.checkpoints = [followsoak.Checkpoint(
            0, 1800.0, 3, stopped_cleanly=True, board_idle=True,
            watchdog_read=True, canceled=True)]
        problems = soak.judge()
        self.assertEqual(["the run was stopped by SIGINT"], problems)

    def test_a_canceled_run_is_not_blamed_for_taking_no_checkpoint(self):
        soak = self.healthy()
        soak.canceled = "SIGTERM"
        soak.checkpoints = []
        self.assertEqual(["the run was stopped by SIGTERM"], soak.judge())

    def test_a_capture_with_no_connection_packet_that_decoded_fails(self):
        soak = self.healthy()
        soak.valid_connection_packets = 0
        problems = soak.judge()
        self.assertTrue(any("do not decode the connection" in problem
                            for problem in problems), problems)

    def test_a_run_whose_fixture_refused_a_transition_says_so(self):
        soak = self.healthy()
        soak.schedule_errors = 3
        problems = soak.judge()
        self.assertTrue(any("3 fixture transition(s) failed" in problem
                            for problem in problems), problems)

    def test_memory_growth_is_only_judged_when_a_plan_asks(self):
        soak = self.healthy()
        soak.rss_first = 1000
        soak.rss_growth_max = 5000
        self.assertEqual([], soak.judge())
        limited = self.healthy(**{"--max-memory-growth-kb": 1000})
        limited.rss_first = 1000
        limited.rss_growth_max = 5000
        self.assertTrue(any("grew by 5000 KiB" in problem
                            for problem in limited.judge()), limited.judge())

    def test_an_allowance_is_never_the_whole_population(self):
        # One phase judged, so a single disagreement still fails: without the
        # cap the floor of one would make the check unfailable on a short run.
        soak = self.soak()
        self.assertEqual(0, soak.allowance(1))
        self.assertEqual(1, soak.allowance(2))
        self.assertEqual(1, soak.allowance(10))


class IntervalTests(unittest.TestCase):
    """What rose, and which stretch of the run it rose in."""

    def soak(self):
        argv = ["--out", "/tmp/follow-soak", "--serial", SERIAL,
                "--channel", "37", "--duration", "14400",
                "--peripheral-jlink", PERIPHERAL_JLINK,
                "--peripheral-address", PERIPHERAL,
                "--central-jlink", CENTRAL_JLINK, "--central-address", CENTRAL,
                "--advertising", "20", "--connected", "30", "--absent", "20",
                "--checkpoint-every", "1800", "--sample-every", "60",
                "--conn-interval", "30",
                "--write-bytes", "244", "--write-period", "50",
                "--presence-window", "2", "--presence-windows", "5",
                "--liveness-timeout", "120", "--detect-within", "10",
                "--connect-within", "10", "--acquire-within", "15",
                "--quiet-after", "3", "--max-gap", "15",
                "--min-detection-share", "0.9", "--min-follow-share", "0.8",
                "--min-connection-packets", "3", "--max-restarts", "0"]
        options = followsoak.options_from(
            followsoak.build_parser().parse_args(argv))
        return followsoak.Soak(options, None, None)

    def record(self, **values) -> watchdog.WatchdogRecord:
        fields = {"boot_cause": "watchdog", "resets": 0, "boots": 1,
                  "transfer_errors": 0, "radio_waits": {}}
        fields.update(values)
        return watchdog.WatchdogRecord(**fields)

    def test_a_counter_that_rose_is_attributed_to_the_interval_it_rose_in(self):
        soak = self.soak()
        soak.open_interval()
        base = self.record()
        soak.watchdog_baseline = base
        # The second read finds two more resets and a transfer error: the
        # interval that just ended is the one they belong to.
        current = self.record(resets=2, transfer_errors=1,
                              last_reset_mode="LE", last_reset_wait="rx")
        change = watchdog.compare(base, current)
        soak.close_interval("checkpoint 0", change, current, "")
        first = soak.intervals[0]
        self.assertEqual(2, first.new_resets)
        self.assertEqual(1, first.new_transfer_errors)
        self.assertTrue(first.counters_moved)
        self.assertEqual("checkpoint 0", first.reason)
        # And a new interval is open, carrying none of it.
        self.assertEqual(2, len(soak.intervals))
        self.assertFalse(soak.intervals[1].counters_moved)

    def test_an_unrequested_reset_records_where_the_firmware_was(self):
        """The `where` line of each reset, which nothing else reads.

        `WatchdogRecord.last_reset_where` is a property, and it was being
        called as a function in the address soak, so the first unrequested
        reset in a soak ended the run in a `TypeError`. A reset is not
        something a healthy run produces, so no scenario test reaches this.
        """
        soak = self.soak()
        soak.open_interval()
        soak.watchdog_baseline = self.record()
        current = self.record(resets=1, last_reset_mode="LE",
                              last_reset_wait="rx")
        change = watchdog.compare(soak.watchdog_baseline, current)
        self.assertTrue(change.reset_itself)
        # What read_watchdog_now does with a record that moved.
        soak.watchdog_resets += change.new_resets
        soak.watchdog_where.append(current.last_reset_where)
        self.assertEqual(1, soak.watchdog_resets)
        self.assertEqual([current.last_reset_where], soak.watchdog_where)
        self.assertIsInstance(soak.watchdog_where[0], str)
        self.assertIn("rx", soak.watchdog_where[0])

    def test_an_interval_counts_the_phases_and_packets_inside_it(self):
        soak = self.soak()
        soak.open_interval()
        soak.phases.append(followsoak.Phase(0, followsoak.ADVERTISING, 0, 0.0))
        soak.cycles = 1
        soak.advertising_packets = 120
        soak.connection_packets = 40
        soak.records_seen = 300
        soak.close_interval("checkpoint 0", None, self.record(), "")
        first = soak.intervals[0]
        self.assertEqual(1, first.phases)
        self.assertEqual(1, first.cycles)
        self.assertEqual(120, first.advertising_packets)
        self.assertEqual(40, first.connection_packets)
        self.assertEqual(300, first.records)
        self.assertFalse(first.counters_moved)

    def test_a_read_with_no_interval_open_opens_one_first(self):
        # The teardown path can reach a read before the first interval was
        # opened; it must not lose the read.
        soak = self.soak()
        soak.close_interval("the run finished", None, self.record(), "")
        self.assertEqual(2, len(soak.intervals))
        self.assertEqual("the run finished", soak.intervals[0].reason)


class ReaderTests(unittest.TestCase):
    """One record at a time: what the driver makes of each kind."""

    def soak(self):
        return IntervalTests.soak(IntervalTests())

    def phase(self, soak, state, started=0.0, length=20.0):
        phase = followsoak.Phase(len(soak.phases), state, 0, started)
        phase.ended = started + length
        soak.phases.append(phase)
        return phase

    def test_the_peripherals_advertisement_is_seen_and_another_is_not(self):
        from tests import fake_connection

        soak = self.soak()
        phase = self.phase(soak, followsoak.ADVERTISING)
        soak.read_record(fake_connection.adv_ind(PERIPHERAL), 1.0)
        soak.read_record(fake_connection.adv_ind("C0:00:00:00:00:09"), 2.0)
        self.assertEqual(1, soak.advertising_packets)
        self.assertEqual(1, phase.structural.packets)

    def test_the_fixtures_connect_ind_acquires_the_phase_and_anothers_does_not(self):
        from tests import fake_connection

        soak = self.soak()
        phase = self.phase(soak, followsoak.CONNECTED)
        soak.read_record(
            fake_connection.connect_ind(CENTRAL, PERIPHERAL,
                                        access_address=0x11110001,
                                        crc_init=0x555001), 4.0)
        self.assertEqual(1, soak.connect_inds)
        self.assertEqual(4.0, phase.acquired_at)
        self.assertIsNotNone(phase.link)
        soak.read_record(
            fake_connection.connect_ind("C0:00:00:00:00:07",
                                        "C0:00:00:00:00:08",
                                        access_address=0x22220001,
                                        crc_init=0x666001), 5.0)
        self.assertEqual(1, soak.connect_inds)
        self.assertEqual(1, soak.other_connect_inds)

    def test_packets_of_a_connection_the_run_announced_are_counted(self):
        from tests import fake_connection

        soak = self.soak()
        phase = self.phase(soak, followsoak.CONNECTED)
        soak.read_record(
            fake_connection.connect_ind(CENTRAL, PERIPHERAL,
                                        access_address=0x11110001,
                                        crc_init=0x555001), 1.0)
        soak.read_record(fake_connection.data_packet(0x11110001, 0x555001), 2.0)
        soak.read_record(
            fake_connection.data_packet(0x11110001, 0x555001, valid_crc=False),
            3.0)
        self.assertEqual(2, soak.connection_packets)
        self.assertEqual(1, soak.valid_connection_packets)
        self.assertEqual(1, soak.long_connection_packets)
        self.assertEqual(2, phase.link.packets)
        # And a packet of a connection nobody announced is counted apart and
        # never described.
        soak.read_record(fake_connection.data_packet(0x99990001, 0x777001), 4.0)
        self.assertEqual(1, soak.unannounced_packets)
        self.assertEqual(2, soak.connection_packets)

    def test_a_packet_after_the_connection_ended_is_counted_as_late(self):
        from tests import fake_connection

        soak = self.soak()
        connected = self.phase(soak, followsoak.CONNECTED, 0.0, 30.0)
        soak.read_record(
            fake_connection.connect_ind(CENTRAL, PERIPHERAL,
                                        access_address=0x11110001,
                                        crc_init=0x555001), 1.0)
        self.phase(soak, followsoak.ABSENT, 30.0, 20.0)
        # Inside the settle time, which the packets already on the air need.
        soak.read_record(fake_connection.data_packet(0x11110001, 0x555001), 31.0)
        self.assertEqual(0, connected.link.late)
        # And past it, which is a follow that did not end.
        soak.read_record(fake_connection.data_packet(0x11110001, 0x555001), 40.0)
        self.assertEqual(1, connected.link.late)


if __name__ == "__main__":
    unittest.main()
