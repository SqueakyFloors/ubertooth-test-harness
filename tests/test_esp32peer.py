"""esp32peer.py: the peer shell, the pair session, and the failures it reports."""

import io
import json
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

from ubertooth_harness import esp32peer
from tests import fake_esp32
from tests.fake_esp32 import FakePeerFirmware, FakePeerPort, opener, shell

ACCEPTOR = "/dev/serial/by-id/usb-fake-acceptor-if00-port0"
INITIATOR = "/dev/serial/by-id/usb-fake-initiator-if00-port0"
ACCEPTOR_ADDRESS = "C8:85:41:2A:1B:2A"
INITIATOR_ADDRESS = "20:50:0D:E9:C5:BA"
# A command that always succeeds, with no shell involved.
TRUE = (sys.executable, "-c", "pass")


def _pair(**options):
    acceptor = FakePeerFirmware(ACCEPTOR_ADDRESS)
    initiator = FakePeerFirmware(INITIATOR_ADDRESS, **options)
    acceptor.link(initiator)
    return acceptor, initiator


def _peers(acceptor, initiator):
    open_one = opener({ACCEPTOR: acceptor, INITIATOR: initiator})
    return (esp32peer.Peer(ACCEPTOR, "acceptor", ACCEPTOR_ADDRESS,
                           opener=open_one),
            esp32peer.Peer(INITIATOR, "initiator", INITIATOR_ADDRESS,
                           opener=open_one))


class AddressTests(unittest.TestCase):

    def test_the_lap_is_the_low_three_bytes_and_the_uap_the_next(self):
        self.assertEqual(("2a1b2a", "41"), esp32peer.address_parts(ACCEPTOR_ADDRESS))
        self.assertEqual(("e9c5ba", "0d"), esp32peer.address_parts(INITIATOR_ADDRESS))

    def test_the_device_name_follows_from_the_address(self):
        # main/bt_peer.c builds it as UT-ESP32- and the LAP in upper case, and
        # the shell cannot set it, so a case derives it rather than asking a
        # registry for it.
        self.assertEqual("UT-ESP32-2A1B2A", esp32peer.device_name(ACCEPTOR_ADDRESS))
        self.assertEqual("UT-ESP32-E9C5BA", esp32peer.device_name(INITIATOR_ADDRESS))
        with self.assertRaisesRegex(esp32peer.PeerError, "BD_ADDR must look like"):
            esp32peer.device_name("not an address")

    def test_a_malformed_address_is_refused(self):
        for value in ("", "1:2:3:4:5:6", "C8:85:41:2A:1B", "zz:85:41:2a:1b:2a"):
            with self.subTest(value), self.assertRaisesRegex(
                esp32peer.PeerError, "BD_ADDR must look like"
            ):
                esp32peer.normalize_address(value)

    def test_an_address_is_normalized_to_upper_case(self):
        self.assertEqual(ACCEPTOR_ADDRESS,
                         esp32peer.normalize_address(" c8:85:41:2a:1b:2a "))

    def test_the_by_id_path_is_derived_from_the_bridge_serial(self):
        path = esp32peer.default_device("abc123")
        self.assertEqual("/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_"
                         "Bridge_Controller_abc123-if00-port0", path)

    def test_a_bridge_serial_is_required_to_derive_a_path(self):
        with self.assertRaisesRegex(esp32peer.PeerError, "bridge serial is needed"):
            esp32peer.default_device("  ")

    def test_the_stream_bounds_are_the_firmwares(self):
        self.assertEqual(5, esp32peer.period_ms(5))
        self.assertEqual(512, esp32peer.payload_bytes(512))
        with self.assertRaisesRegex(esp32peer.PeerError, "period must be 5-10000"):
            esp32peer.period_ms(4)
        with self.assertRaisesRegex(esp32peer.PeerError, "payload must be 8-512"):
            esp32peer.payload_bytes(513)


class ShellTests(unittest.TestCase):

    def test_the_boot_notice_is_waited_for_and_the_rom_log_skipped(self):
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        peer_shell = shell(firmware)
        peer_shell.wait_ready()
        self.assertEqual(ACCEPTOR_ADDRESS, peer_shell.status()["address"])

    def test_older_firmware_is_refused_and_a_later_1_x_accepted(self):
        # Nothing flashes a peer, so a later image of the same major, which
        # has these commands and more, has to be usable. The versions are
        # built around the minimum rather than written out, so that moving the
        # pin does not turn this into a test of last month's pin.
        major, minor, patch = (int(part) for part
                              in esp32peer.MINIMUM_FIRMWARE.split("-")[1].split("."))
        name = "utesp-%d.%d.%d".__mod__
        cases = [(name((major, minor, patch)), True),
                 (name((major, minor, patch + 1)), True),
                 (name((major, minor + 1, 0)), True),
                 (name((major - 1, 9, 0)), False),
                 (name((major + 1, 0, 0)), False)]
        if minor:
            cases.append((name((major, minor - 1, 9)), False))
        if patch:
            cases.append((name((major, minor, patch - 1)), False))
        for version, accepted in cases:
            firmware = FakePeerFirmware(ACCEPTOR_ADDRESS, version=version)
            peer_shell = shell(firmware)
            peer_shell.wait_ready()
            with self.subTest(version):
                if accepted:
                    self.assertEqual(version,
                                     esp32peer.check_status(peer_shell.status(),
                                                            "acceptor")["fw"])
                else:
                    with self.assertRaisesRegex(
                        esp32peer.PeerError,
                        f"acceptor runs {version}, and the harness needs "
                        f"{esp32peer.MINIMUM_FIRMWARE} or a later 1.x image"
                    ):
                        esp32peer.check_status(peer_shell.status(), "acceptor")

    def test_a_version_that_is_not_a_version_is_reported(self):
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "'utesp-dev' is not a firmware version"):
            esp32peer.check_firmware("utesp-dev", "acceptor")

    def test_a_board_that_never_says_ready_is_reported(self):
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        port = FakePeerPort(firmware)
        port.pending.clear()  # the boot notice was lost
        peer_shell = esp32peer.Shell(port, "acceptor", _CountingClock())
        with self.assertRaisesRegex(esp32peer.PeerError, "no 'ready' notice within"):
            peer_shell.wait_ready(2.0)

    def test_a_board_whose_bluetooth_did_not_start_says_why(self):
        peer_shell = shell(FakePeerFirmware(ACCEPTOR_ADDRESS,
                                            start_failure="no memory"))
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "Bluetooth did not start: no memory"):
            peer_shell.wait_ready()

    def test_a_board_that_says_nothing_is_reported_not_waited_for_forever(self):
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        firmware.silent = True
        peer_shell = esp32peer.Shell(FakePeerPort(firmware), "acceptor",
                                     _CountingClock())
        peer_shell.wait_ready()  # the boot notice still arrives
        with self.assertRaisesRegex(esp32peer.PeerError, "no answer to 'status'"):
            peer_shell.status()

    def test_an_err_reply_is_raised_with_its_reason(self):
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        peer_shell = shell(firmware)
        peer_shell.wait_ready()
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "'stream on' refused: no connection to stream on"):
            peer_shell.command("stream on")

    def test_notices_are_not_mistaken_for_an_answer(self):
        acceptor, initiator = _pair()
        peer_shell = shell(initiator, "initiator")
        peer_shell.wait_ready()
        peer_shell.command("role initiator")
        peer_shell.command(f"spp peer {ACCEPTOR_ADDRESS}")
        # The firmware's "spp connected" notice comes before the OK.
        self.assertEqual([], peer_shell.command("spp start"))
        self.assertIn(f"utesp: spp connected {ACCEPTOR_ADDRESS}", peer_shell.notices)

    def test_the_shell_writes_its_conversation_for_the_transcript(self):
        from ubertooth_harness import targets
        from tests.fake_esp32 import FakePeerPort, FakeClock

        lines = []
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        peer_shell = esp32peer.Shell(
            FakePeerPort(firmware), "acceptor", FakeClock(),
            log=lambda direction, text: lines.append((direction, text)))
        peer_shell.wait_ready()
        peer_shell.command("stream payload 200")
        self.assertIn((targets.SENT, "stream payload 200"), lines)
        self.assertIn((targets.RECEIVED, "OK"), lines)
        # The boot notice is in there too, so the transcript shows the reset.
        self.assertIn((targets.RECEIVED,
                       f"utesp: {esp32peer.MINIMUM_FIRMWARE} ready"), lines)

    def test_a_logger_marks_each_line_with_the_boards_alias(self):
        from ubertooth_harness import targets

        written = io.StringIO()
        esp32peer.shell_logger("ESP32_Linux1", written)(targets.SENT, "spp start")
        self.assertEqual("[ESP32_Linux1 >] spp start\n", written.getvalue())

    def test_a_status_line_that_is_not_json_is_reported(self):
        with self.assertRaisesRegex(esp32peer.PeerError, "status is not JSON"):
            esp32peer.parse_status("{truncated")

    def test_a_status_missing_a_field_names_it(self):
        status = FakePeerFirmware(ACCEPTOR_ADDRESS).status()
        del status["lap"]
        with self.assertRaisesRegex(esp32peer.PeerError, "no valid 'lap'"):
            esp32peer.parse_status(json.dumps(status))

    def test_a_status_whose_lap_does_not_match_its_address_is_refused(self):
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        status = firmware.status()
        status["lap"] = "000000"
        with self.assertRaisesRegex(esp32peer.PeerError, "do not belong to"):
            esp32peer.check_status(status, "acceptor")

    def test_the_registered_address_must_be_the_one_reported(self):
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "reports BD_ADDR C8:85:41:2A:1B:2A, the "
                                    "registry says 20:50:0D:E9:C5:BA"):
            esp32peer.check_status(firmware.status(), "acceptor", INITIATOR_ADDRESS)


class _CountingClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.5
        return self.now


class CommandTests(unittest.TestCase):

    def test_identify_reports_the_version_address_lap_and_uap(self):
        acceptor, initiator = _pair()
        peer, _ = _peers(acceptor, initiator)
        out = io.StringIO()
        esp32peer.identify(peer, out)
        text = out.getvalue()
        self.assertIn(f"acceptor firmware: {esp32peer.MINIMUM_FIRMWARE}", text)
        self.assertIn(f"acceptor address: {ACCEPTOR_ADDRESS}", text)
        self.assertIn("acceptor LAP: 2a1b2a UAP: 41", text)

    def test_status_prints_the_json_line_and_the_traffic(self):
        acceptor, initiator = _pair()
        peer, _ = _peers(acceptor, initiator)
        out = io.StringIO()
        esp32peer.status_command(peer, out)
        self.assertIn("acceptor traffic: writes 0 errors 0 received 0 bytes",
                      out.getvalue())

    def test_reset_pulses_the_board_and_checks_its_boot_state(self):
        acceptor, initiator = _pair()
        acceptor.role = "initiator"
        acceptor.writes = 500
        peer, _ = _peers(acceptor, initiator)
        out = io.StringIO()
        esp32peer.reset(peer, out)
        # One reset for the open, one for the pulse.
        self.assertEqual(2, acceptor.resets)
        self.assertEqual(esp32peer.BOOT_ROLE, acceptor.role)
        self.assertIn("acceptor role: acceptor", out.getvalue())

    def test_a_board_that_does_not_come_back_in_its_boot_state_fails(self):
        # Every field of the status has to be a boot value: a release that
        # left any of them behind would hand the next case a dirty board.
        for field, value, reason in (
            ("stream", True, r"stream True, not False"),
            ("role", "initiator", r"role 'initiator', not 'acceptor'"),
            ("spp_server", False, r"spp_server False, not True"),
            ("discoverable", False, r"discoverable False, not True"),
            ("peer", INITIATOR_ADDRESS, r"peer '20:50:0D:E9:C5:BA', not ''"),
            ("payload", 300, r"payload_len 300, not 100"),
            ("period", 10, r"period_ms 10, not 100"),
            ("writes", 7, r"counter 7, not 0, writes 7, not 0"),
            ("disconnects", 2, r"disconnects 2, not 0"),
        ):
            class Stale(FakePeerFirmware):
                def boot(self):
                    lines = super().boot()
                    setattr(self, field, value)  # a reset that did not take
                    return lines

            peer, _ = _peers(Stale(ACCEPTOR_ADDRESS),
                             FakePeerFirmware(INITIATOR_ADDRESS))
            with self.subTest(field), self.assertRaisesRegex(
                esp32peer.PeerError,
                "not in its boot state after a reset: " + reason
            ):
                esp32peer.reset(peer, io.StringIO())

    def test_a_missing_device_is_reported_without_a_traceback(self):
        peer = esp32peer.Peer("/dev/serial/by-id/usb-absent", "acceptor",
                              opener=opener({}))
        with self.assertRaisesRegex(esp32peer.PeerError, "does not exist"):
            esp32peer.identify(peer, io.StringIO())


class StreamTests(unittest.TestCase):

    def _stream(self, command=TRUE, **options):
        acceptor_firmware, initiator_firmware = _pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        report = io.StringIO()
        status = esp32peer.stream(acceptor, initiator, command, out=report,
                                 settle_s=0.0, **options)
        return status, report.getvalue(), acceptor_firmware, initiator_firmware

    def test_the_pair_links_up_streams_and_the_command_runs(self):
        status, report, acceptor, initiator = self._stream(min_writes=10)
        self.assertEqual(0, status)
        self.assertIn(f"central LAP: {esp32peer.address_parts(INITIATOR_ADDRESS)[0]}",
                      report)
        self.assertIn("central UAP: 0d", report)
        self.assertIn(f"link: up to {ACCEPTOR_ADDRESS}", report)
        self.assertIn("command exit: 0", report)
        self.assertIn("pair streamed: yes", report)
        self.assertIn("initiator traffic: writes 100 errors 0", report)
        self.assertIn("acceptor traffic: writes 0 errors 0 received 20000 bytes",
                      report)
        # The initiator is the one that pages, and both end up reset.
        self.assertIn("role initiator", initiator.commands)
        self.assertIn(f"spp peer {ACCEPTOR_ADDRESS}", initiator.commands)
        self.assertEqual(esp32peer.BOOT_ROLE, initiator.role)
        self.assertEqual(2, acceptor.resets)

    def test_the_period_and_payload_reach_the_initiator(self):
        _status, report, _acceptor, initiator = self._stream(
            period=10, payload=300, min_writes=10)
        self.assertIn("stream period 10", initiator.commands)
        self.assertIn("stream payload 300", initiator.commands)
        self.assertIn("stream: 300 bytes every 10 ms", report)

    def test_the_commands_exit_status_is_this_scripts_own(self):
        status, report, _acceptor, _initiator = self._stream(
            command=(sys.executable, "-c", "raise SystemExit(3)"), min_writes=10)
        self.assertEqual(3, status)
        self.assertIn("command exit: 3", report)

    def test_the_commands_output_is_not_touched(self):
        # The command writes to the real stdout, which it inherits; nothing
        # of the pair's report is on that stream.
        script = "import sys; sys.stdout.write('systime=1 ch= 1\\n')"
        acceptor_firmware, initiator_firmware = _pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        report = io.StringIO()
        with _captured_stdout() as captured:
            esp32peer.stream(acceptor, initiator, (sys.executable, "-c", script),
                             min_writes=10, out=report, settle_s=0.0)
            written = captured()
        self.assertEqual("systime=1 ch= 1\n", written)
        # ... and none of the pair's report went to that stream.
        self.assertNotIn("central LAP", written)
        self.assertIn("central LAP: e9c5ba", report.getvalue())

    def test_a_pair_that_sent_too_little_fails_the_run(self):
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "reported 100 writes, fewer than the 1000"):
            self._stream(min_writes=1000)

    def test_a_link_that_did_not_come_up_is_reported(self):
        acceptor_firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        initiator_firmware = FakePeerFirmware(INITIATOR_ADDRESS)
        # Not linked to each other: the page gets no answer.
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "'spp start' refused: connect timed out"):
            esp32peer.stream(acceptor, initiator, TRUE, out=io.StringIO(),
                             settle_s=0.0)
        # Both boards are left in their boot state even so.
        self.assertEqual(esp32peer.BOOT_ROLE, initiator_firmware.role)
        self.assertEqual(2, initiator_firmware.resets)

    def test_one_bridge_cannot_be_both_peers(self):
        acceptor_firmware, initiator_firmware = _pair()
        acceptor, _initiator = _peers(acceptor_firmware, initiator_firmware)
        with self.assertRaisesRegex(esp32peer.PeerError, "the same bridge"):
            esp32peer.stream(acceptor, acceptor, TRUE, out=io.StringIO())

    def test_two_bridges_that_lead_to_one_board_are_caught(self):
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        open_one = opener({ACCEPTOR: firmware, INITIATOR: firmware})
        acceptor = esp32peer.Peer(ACCEPTOR, "acceptor", None, opener=open_one)
        initiator = esp32peer.Peer(INITIATOR, "initiator", None, opener=open_one)
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "both bridges lead to one board"):
            esp32peer.stream(acceptor, initiator, TRUE, out=io.StringIO())

    def test_a_command_that_overruns_is_interrupted_and_reported(self):
        acceptor_firmware, initiator_firmware = _pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        # It stops itself on SIGINT, quietly: a Python child killed by a
        # signal puts a crash report dialog on a Mac.
        sleeper = (sys.executable, "-c",
                   "import time\ntry:\n    time.sleep(30)\n"
                   "except KeyboardInterrupt:\n    pass\n")
        with self.assertRaisesRegex(esp32peer.PeerError, "did not finish within"):
            esp32peer.stream(acceptor, initiator, sleeper, out=io.StringIO(),
                             timeout=0.5, settle_s=0.0, min_writes=1)


def _audio_pair(version=esp32peer.AUDIO_FIRMWARE):
    """A pair whose firmware is new enough to choose the audio packet type."""
    acceptor = FakePeerFirmware(ACCEPTOR_ADDRESS, version=version)
    initiator = FakePeerFirmware(INITIATOR_ADDRESS, version=version)
    acceptor.link(initiator)
    return acceptor, initiator


class AudioTests(unittest.TestCase):
    """The synchronous audio link: the same session, voice packets on the air."""

    def _audio(self, command=TRUE, firmwares=None, **options):
        acceptor_firmware, initiator_firmware = firmwares or _audio_pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        report = io.StringIO()
        status = esp32peer.audio(acceptor, initiator, command, out=report,
                                 settle_s=0.0, **options)
        return status, report.getvalue(), acceptor_firmware, initiator_firmware

    def test_the_link_comes_up_at_the_type_asked_for_and_the_command_runs(self):
        status, report, acceptor, initiator = self._audio(min_frames=10)
        self.assertEqual(0, status)
        self.assertIn("central LAP: e9c5ba", report)
        self.assertIn("audio: ev3 packets", report)
        self.assertIn("audio link: esco ev3, 30 bytes every 6 slots", report)
        self.assertIn("command exit: 0", report)
        # Every figure is the command's own window: two readings each side
        # of it, each worth one tick of the fixture's counters.
        self.assertIn("initiator audio: on ev3, sent 100 received 100 frames, "
                      "0 send errors, 0 damaged, link 0 opened 0 dropped",
                      report)
        self.assertIn("initiator packets: controller sent 110 received 110, "
                      "110 good, 0 bad, 0 missing, 0 lost, 0 discarded", report)
        self.assertIn("acceptor audio: on ev3, sent 100 received 100 frames, ",
                      report)
        self.assertIn("pair carried audio: ev3", report)
        # The acceptor's profile starts before the initiator pages it, both
        # boards are told the type, both are read before and after the
        # command, and both have their link taken down afterwards.
        self.assertEqual(["sco type ev3", "sco start", "sco stats",
                          "sco stats", "sco stop"],
                         [line for line in acceptor.commands
                          if line.startswith("sco")])
        self.assertEqual(["sco type ev3", "sco start", "sco stats",
                          "sco stats", "sco stop"],
                         [line for line in initiator.commands
                          if line.startswith("sco")])
        self.assertIn(f"spp peer {ACCEPTOR_ADDRESS}", initiator.commands)
        # The initiator's type is set after `role`, which is the order the
        # bench uses. `role` takes an audio link down and keeps the chosen
        # type, so either order works and this one also survives a firmware
        # that put the type back.
        self.assertLess(initiator.commands.index("role initiator"),
                        initiator.commands.index("sco type ev3"))
        # Both end up reset, as the stream session leaves them.
        self.assertEqual(esp32peer.BOOT_ROLE, initiator.role)
        self.assertEqual("off", initiator.sco)
        self.assertEqual(2, acceptor.resets)

    def test_the_enhanced_rate_type_is_reported_as_what_it_is(self):
        _status, report, _acceptor, _initiator = self._audio(
            packet_type="2ev3", min_frames=10)
        self.assertIn("audio link: esco 2-ev3, 60 bytes every 12 slots", report)
        self.assertIn("pair carried audio: 2-ev3", report)

    def test_the_default_type_sets_nothing_on_the_boards(self):
        _status, report, acceptor, initiator = self._audio(
            packet_type="default", min_frames=10)
        self.assertNotIn("sco type default", acceptor.commands)
        self.assertNotIn("sco type default", initiator.commands)
        self.assertIn("audio: default packets", report)

    def test_an_unknown_type_is_refused_before_a_board_is_opened(self):
        acceptor_firmware, initiator_firmware = _audio_pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "audio packet type must be one of"):
            esp32peer.audio(acceptor, initiator, TRUE, packet_type="hv3",
                            out=io.StringIO())
        self.assertEqual([], acceptor_firmware.commands)

    def test_firmware_without_the_type_command_is_refused(self):
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    f"the harness needs {esp32peer.AUDIO_FIRMWARE}"):
            self._audio(firmwares=_audio_pair(esp32peer.MINIMUM_FIRMWARE))

    def test_a_link_that_did_not_come_up_is_reported(self):
        acceptor_firmware = FakePeerFirmware(
            ACCEPTOR_ADDRESS, version=esp32peer.AUDIO_FIRMWARE)
        initiator_firmware = FakePeerFirmware(
            INITIATOR_ADDRESS, version=esp32peer.AUDIO_FIRMWARE)
        # Not linked to each other: the page gets no answer.
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "'sco start' refused: no peer address set"):
            esp32peer.audio(acceptor, initiator, TRUE, out=io.StringIO(),
                            settle_s=0.0)
        self.assertEqual(esp32peer.BOOT_ROLE, initiator_firmware.role)
        self.assertEqual("off", initiator_firmware.sco)

    def test_a_pair_that_carried_too_few_frames_fails_the_run(self):
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "sent 100 audio frames while the command ran, fewer than the 500"):
            self._audio(min_frames=500)

    def test_a_capture_that_stopped_early_fails_the_run(self):
        with self.assertRaisesRegex(esp32peer.PeerError, "it stopped early"):
            self._audio(min_seconds=60.0, min_frames=10)

    def test_one_bridge_cannot_be_both_peers(self):
        acceptor_firmware, initiator_firmware = _audio_pair()
        acceptor, _initiator = _peers(acceptor_firmware, initiator_firmware)
        with self.assertRaisesRegex(esp32peer.PeerError, "the same bridge"):
            esp32peer.audio(acceptor, acceptor, TRUE, out=io.StringIO())


class AudioWindowTests(unittest.TestCase):
    """What the command's own window carried, not the board's whole life.

    A session used to report the totals a board happened to be holding when
    it was read. Those include the bring-up, the settle, and on a board an
    earlier session left running, that session too, so a floor on them says
    nothing about the capture that was just taken (#264).
    """

    def _audio(self, command=TRUE, firmwares=None, **options):
        acceptor_firmware, initiator_firmware = firmwares or _audio_pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        report = io.StringIO()
        status = esp32peer.audio(acceptor, initiator, command, out=report,
                                 settle_s=0.0, **options)
        return status, report.getvalue(), acceptor_firmware, initiator_firmware

    def test_frames_counted_before_the_command_are_not_credited_to_it(self):
        acceptor, initiator = _audio_pair()
        # Both boards arrive carrying a large count from somewhere else.
        for board in (acceptor, initiator):
            board.sco_tx_frames = 50_000
            board.sco_rx_frames = 50_000
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "the initiator sent 100 audio frames while the command ran, "
                "fewer than the 500"):
            self._audio(firmwares=(acceptor, initiator), min_frames=500)

    def test_a_link_that_dropped_and_came_back_is_not_one_link(self):
        acceptor, initiator = _audio_pair()
        acceptor.during_the_command(acceptor.flap)
        with self.assertRaises(esp32peer.PeerError) as raised:
            self._audio(firmwares=(acceptor, initiator), min_frames=10)

        # Both ends of the link saw it, and both say so.
        for label in ("initiator", "acceptor"):
            self.assertIn(f"the {label}'s audio link did not stay up while the "
                          "command ran: 1 opened, 1 went down",
                          str(raised.exception))

    def test_a_link_replaced_without_a_drop_being_counted_is_caught(self):
        """A renegotiated link keeps both boards reading `on` throughout.

        The handle is the second thing that says which link it is, so it is
        compared as well as the two counters.
        """
        self.assertEqual(
            ["the initiator's audio link handle changed from 1 to 2: the "
             "command did not run against one link"],
            esp32peer._audio_checks(
                esp32peer.Peer("/dev/null", "initiator"),
                {"sco": esp32peer.AUDIO_ON},
                {"sco_tx_frames": 10, "sco_tx_errors": 0, "sco_rx_frames": 10,
                 "sco_rx_bad": 0, "sco_connects": 0, "sco_disconnects": 0,
                 "disconnects": 0},
                dict.fromkeys(esp32peer.AUDIO_PKT_COUNTERS, 10),
                (1, 2), min_frames=10))

    def test_both_controllers_packet_counters_are_read_and_compared(self):
        _status, report, acceptor, initiator = self._audio(min_frames=10)
        for label in ("initiator", "acceptor"):
            self.assertIn(f"{label} packets: controller sent 110 received 110, "
                          "110 good, 0 bad, 0 missing, 0 lost, 0 discarded",
                          report)
        # Read from both boards, before the command and after it.
        for board in (acceptor, initiator):
            self.assertEqual(2, board.commands.count("sco stats"))

    def test_a_controller_that_counted_no_packets_fails_the_run(self):
        """Frames the application handled over a link the air never saw."""
        acceptor, initiator = _audio_pair()
        for board in (acceptor, initiator):
            board.packets_per_read = 0
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "the initiator's controller sent no synchronous packets while "
                "the command ran"):
            self._audio(firmwares=(acceptor, initiator), min_frames=10)

    def test_a_controller_whose_packets_were_all_bad_fails_the_run(self):
        acceptor, initiator = _audio_pair()
        for board in (acceptor, initiator):
            def goes_bad(board=board):
                """Every packet still arrives, and none of them is correct."""
                board.packets_advance = ("rx_total", "rx_err", "tx_total")
            board.during_the_command(goes_bad)
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "the initiator's controller received 110 synchronous packets "
                "while the command ran and not one of them correct"):
            self._audio(firmwares=(acceptor, initiator), min_frames=10)

    def test_counters_zeroed_part_way_through_are_named_not_subtracted(self):
        acceptor, initiator = _audio_pair()

        def zero_them():
            initiator.sco_tx_frames = 0
        initiator.during_the_command(zero_them)
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "the initiator's status sco_tx_frames went backwards, 200 to "
                "100: its counters were zeroed while the command ran"):
            self._audio(firmwares=(acceptor, initiator), min_frames=10)

    def test_a_counter_that_is_not_a_number_is_refused(self):
        acceptor, initiator = _audio_pair()
        plain_status = initiator.status

        def with_a_text_counter():
            status = plain_status()
            status["sco_tx_frames"] = "many"
            return status
        initiator.status = with_a_text_counter
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "initiator: status has no valid 'sco_tx_frames'"):
            self._audio(firmwares=(acceptor, initiator), min_frames=10)

    def test_a_negative_counter_is_refused(self):
        acceptor, initiator = _audio_pair()
        plain_status = initiator.status

        def with_a_negative_counter():
            status = plain_status()
            status["sco_rx_bad"] = -1
            return status
        initiator.status = with_a_negative_counter
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "initiator: status reports sco_rx_bad -1, and a counter cannot "
                "be negative"):
            self._audio(firmwares=(acceptor, initiator), min_frames=10)

    def test_counters_that_a_board_will_not_give_are_reported(self):
        acceptor, initiator = _audio_pair()
        initiator.refuse = {"sco stats": "no audio link to count packets on"}
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "initiator: 'sco stats' refused: no audio link to count packets"):
            self._audio(firmwares=(acceptor, initiator), min_frames=10)

    def test_a_link_that_dropped_and_stayed_down_still_reports_what_it_carried(self):
        """The failure the whole check exists for keeps its measurements.

        A board with no link refuses the controller's counters, so asking
        for them is not allowed to lose the frame counters and the drop
        that the same reading already holds.
        """
        acceptor, initiator = _audio_pair()
        acceptor.during_the_command(acceptor.drop)
        report = io.StringIO()
        acceptor_peer, initiator_peer = _peers(acceptor, initiator)
        with self.assertRaises(esp32peer.PeerError) as raised:
            esp32peer.audio(acceptor_peer, initiator_peer, TRUE, out=report,
                            settle_s=0.0, min_frames=10)
        error, text = str(raised.exception), report.getvalue()

        # The drop is named, on both boards ...
        for label in ("initiator", "acceptor"):
            self.assertIn(f"the {label}'s audio link did not stay up while the "
                          "command ran: 0 opened, 1 went down", error)
            self.assertIn(f"the {label}'s audio link read ready afterwards, "
                          "not on", error)
            self.assertIn(f"{label}: 'sco stats' refused: no audio link to "
                          "count packets on", error)
        # ... and what the capture's own window carried is still reported,
        # without the controller's counters, which no longer exist.
        self.assertIn("initiator audio: ready none, sent 0 received 0 "
                      "frames, 0 send errors, 0 damaged, link 0 opened 1 "
                      "dropped", text)
        self.assertNotIn("initiator packets:", text)

    def test_a_window_that_carried_no_frames_fails_the_run(self):
        acceptor, initiator = _audio_pair()
        for board in (acceptor, initiator):
            board.frames_per_read = 0
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "the initiator sent 0 audio frames while the command ran, "
                "fewer than the 10"):
            self._audio(firmwares=(acceptor, initiator), min_frames=10)

    def test_a_window_whose_frames_only_went_one_way_fails_the_run(self):
        acceptor, initiator = _audio_pair()

        # The far end never answers: frames go out, none come back.
        for board in (acceptor, initiator):
            board.frames_advance = ("tx",)
        with self.assertRaisesRegex(
                esp32peer.PeerError,
                "the initiator counted no audio frames from the far end while "
                "the command ran"):
            self._audio(firmwares=(acceptor, initiator), min_frames=0)


class AudioCleanupTests(unittest.TestCase):
    """A session that measured something reports it, then says what failed.

    Losing a 30-second capture's measurements because the board would not
    stop its link afterwards is the worst of both: the capture is spent and
    the run cannot say what it held (#264).
    """

    def _audio(self, firmwares, **options):
        acceptor_firmware, initiator_firmware = firmwares
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        report = io.StringIO()
        with self.assertRaises(esp32peer.PeerError) as raised:
            esp32peer.audio(acceptor, initiator, TRUE, out=report,
                            settle_s=0.0, min_frames=10, **options)
        return str(raised.exception), report.getvalue()

    def test_a_refused_stop_keeps_the_whole_session_report(self):
        acceptor, initiator = _audio_pair()
        initiator.refuse = {"sco stop": "the audio link did not stop"}
        error, report = self._audio((acceptor, initiator))

        self.assertIn("the initiator did not stop its audio link: initiator: "
                      "'sco stop' refused: the audio link did not stop", error)
        # Everything the capture was taken for is still in the report.
        self.assertIn("initiator audio: on ev3, sent 100 received 100 frames",
                      report)
        self.assertIn("initiator packets: controller sent 110 received 110",
                      report)
        self.assertIn("command exit: 0", report)

    def test_a_board_left_out_of_its_boot_state_fails_the_run(self):
        class Stale(FakePeerFirmware):
            """A board whose release reset leaves its profile running.

            Its first boot, the one that opening the bridge causes, is a
            proper one: it is the release at the end that does not take.
            """

            def boot(self):
                lines = super().boot()
                if self.resets > 1:
                    self.sco = "ready"
                    self.sco_profile = "hf"
                return lines

        acceptor = Stale(ACCEPTOR_ADDRESS, version=esp32peer.AUDIO_FIRMWARE)
        initiator = FakePeerFirmware(INITIATOR_ADDRESS,
                                     version=esp32peer.AUDIO_FIRMWARE)
        acceptor.link(initiator)
        error, report = self._audio((acceptor, initiator))

        self.assertIn("the acceptor is not in its boot state after its release "
                      "reset: sco 'ready', not 'off'", error)
        self.assertIn("sco_profile 'hf', not 'none'", error)
        # ... and the session still said what it measured.
        self.assertIn("acceptor audio: on ev3, sent 100 received 100 frames",
                      report)

    def test_a_board_that_cannot_be_reset_at_all_is_reported(self):
        acceptor, initiator = _audio_pair()
        # Silent from the release reset onwards: no boot notice arrives.
        plain_boot = acceptor.boot

        def silent_boot():
            lines = plain_boot()
            if acceptor.resets > 1:
                acceptor.silent = True
                return []
            return lines
        acceptor.boot = silent_boot
        error, _report = self._audio((acceptor, initiator))

        self.assertIn("the acceptor was not released to its boot state", error)

    def test_a_cancelled_session_still_releases_both_boards(self):
        """Cancellation does not raise PeerError, and must still clean up.

        The harness cancels a run by interrupting it, which arrives here as
        KeyboardInterrupt out of the command. A session that caught only
        PeerError would leave both boards with the link up and the
        initiator in the initiator role.
        """
        for failure in (KeyboardInterrupt, RuntimeError):
            with self.subTest(failure=failure.__name__):
                acceptor, initiator = _audio_pair()
                acceptor_peer, initiator_peer = _peers(acceptor, initiator)
                raiser = (sys.executable, "-c", "pass")
                with unittest.mock.patch.object(
                        esp32peer, "_run_command", side_effect=failure):
                    with self.assertRaises(failure):
                        esp32peer.audio(acceptor_peer, initiator_peer, raiser,
                                        out=io.StringIO(), settle_s=0.0,
                                        min_frames=10)
                for board in (acceptor, initiator):
                    self.assertEqual(2, board.resets)
                    self.assertEqual("off", board.sco)
                    self.assertEqual(esp32peer.BOOT_ROLE, board.role)

    def test_an_interruption_during_the_release_resets_each_board_once(self):
        """The board already released is not released a second time.

        The release happens after the measurements on the ordinary path, so
        an interruption can land between the two boards. Each must end up
        released, and neither twice.
        """
        acceptor, initiator = _audio_pair()
        acceptor_peer, initiator_peer = _peers(acceptor, initiator)
        plain_reset = initiator.boot

        def interrupt_after_the_initiator():
            lines = plain_reset()
            if initiator.resets > 1:
                raise KeyboardInterrupt
            return lines
        initiator.boot = interrupt_after_the_initiator
        with self.assertRaises(KeyboardInterrupt):
            esp32peer.audio(acceptor_peer, initiator_peer, TRUE,
                            out=io.StringIO(), settle_s=0.0, min_frames=10)

        # The initiator's release is where the interruption landed, so it
        # was tried once; the acceptor's happened in the finally, once.
        self.assertEqual(2, initiator.resets)
        self.assertEqual(2, acceptor.resets)
        self.assertEqual("off", acceptor.sco)
        self.assertEqual(esp32peer.BOOT_ROLE, acceptor.role)

    def test_a_board_is_not_reset_twice_on_the_ordinary_path(self):
        acceptor, initiator = _audio_pair()
        acceptor_peer, initiator_peer = _peers(acceptor, initiator)
        esp32peer.audio(acceptor_peer, initiator_peer, TRUE,
                        out=io.StringIO(), settle_s=0.0, min_frames=10)

        # The open's reset and the release's, and no third.
        for board in (acceptor, initiator):
            self.assertEqual(2, board.resets)

    def test_the_link_is_taken_down_before_the_boards_are_released(self):
        acceptor, initiator = _audio_pair()
        acceptor_peer, initiator_peer = _peers(acceptor, initiator)
        esp32peer.audio(acceptor_peer, initiator_peer, TRUE,
                        out=io.StringIO(), settle_s=0.0, min_frames=10)

        # Two resets: the one that opened the bridge and the release. The
        # stop comes between the measurements and that second reset.
        self.assertEqual(2, initiator.resets)
        self.assertEqual(["sco stats", "sco stats", "sco stop"],
                         [line for line in initiator.commands
                          if line in ("sco stats", "sco stop")])
        self.assertEqual("off", initiator.sco)
        self.assertEqual(esp32peer.BOOT_ROLE, initiator.role)


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "esp32-classic-peer"


def _without_comments(source: str) -> str:
    """C source with its block comments taken out.

    What a comment says about argv is not what the compiler reads.
    """
    return re.sub(r"/\*.*?\*/", "", source, flags=re.S)


class PeerShellSourceTests(unittest.TestCase):
    """The firmware's own command parser, and the fixture that stands for it."""

    def _run_body(self) -> str:
        source = (FIRMWARE / "main" / "main.c").read_text()
        body = source.split("static void run(int argc, char **argv)", 1)[1]
        return body.split("\nstatic void handle_line(", 1)[0]

    def test_no_branch_reads_an_argument_the_line_did_not_carry(self):
        """Every operand comes through word(), which bounds it by argc.

        A branch that tested an operand before testing argc read an
        uninitialized pointer, which is what #262 was. One accessor rather
        than an argc test per branch is what keeps that from growing back,
        so the absence of any other argv index is the thing to pin.
        """
        indexed = re.findall(r"argv\[(\w+)\]", _without_comments(self._run_body()))

        self.assertEqual(["0"], indexed)

    def test_the_accessor_gives_an_empty_word_beyond_the_line(self):
        source = (FIRMWARE / "main" / "main.c").read_text()

        self.assertIn('return n < argc ? argv[n] : "";', source)

    def test_the_usage_lines_the_fixture_answers_are_the_firmwares(self):
        header = (FIRMWARE / "main" / "peer.h").read_text()
        names = re.search(r'#define SCO_TYPE_NAMES\s+"([^"]*)"', header)[1]
        type_usage = re.search(r'#define SCO_TYPE_USAGE\s+"([^"]*)"', header)[1]
        usage = re.search(r'#define SCO_USAGE\s+"([^"]*)"\s+SCO_TYPE_NAMES\s+'
                          r'"([^"]*)"', header)

        self.assertEqual(type_usage, fake_esp32.SCO_TYPE_USAGE)
        self.assertEqual(usage[1] + names + usage[2], fake_esp32.SCO_USAGE)
        # The names in that line are the ones the module may ask for.
        self.assertEqual(set(esp32peer.AUDIO_TYPES), set(names.split("|")))


class PeerShellArityTests(unittest.TestCase):
    """`sco` lines with the wrong number of words: one ERR, nothing changed.

    Run against the host-side fixture of the firmware's shell, whose two
    usage lines PeerShellSourceTests pins to the firmware's own.
    """

    @staticmethod
    def _board(live: bool):
        """A board to send a malformed line to, idle or with a link up.

        Returns the board, its shell, and the far end of the link or None.
        Fresh boards each time, so one case cannot leave a profile running
        for the next.
        """
        firmware = FakePeerFirmware(version=esp32peer.AUDIO_FIRMWARE)
        shell = fake_esp32.shell(firmware)
        shell.wait_ready()
        if not live:
            return firmware, shell, None
        far = FakePeerFirmware("20:50:0D:E9:C5:BA",
                               version=esp32peer.AUDIO_FIRMWARE)
        far.link(firmware)
        far_shell = fake_esp32.shell(far)
        far_shell.wait_ready()
        shell.command(f"{esp32peer.SCO_TYPE} ev3")
        shell.command(esp32peer.SCO_START)
        far_shell.command(f"{esp32peer.SCO_TYPE} ev3")
        far_shell.command(esp32peer.ROLE_INITIATOR)
        far_shell.command(f"spp peer {firmware.address}")
        far_shell.command(esp32peer.SCO_START)
        return firmware, shell, far

    def _refused(self, line: str) -> str:
        """The line is refused, idle and with a link up, changing nothing.

        Every case runs both ways: idle, and with a synchronous link up,
        which is the only state where "no counter moved" and "the link is
        untouched" have any content at all. The same refusal has to come
        back either way, which is what "one stable usage line" means.
        """
        reasons = []
        for live in (False, True):
            firmware, shell, far = self._board(live)
            self.assertEqual(esp32peer.AUDIO_ON if live else "off",
                             firmware.status()["sco"])
            before = firmware.status()
            far_before = far.status() if far is not None else None
            with self.assertRaises(esp32peer.PeerError) as raised:
                shell.command(line)
            # Nothing of the board moved: not the role, the profile, the
            # link, the chosen packet type, or any counter. Nor the far
            # end's, which shares the link.
            self.assertEqual(before, firmware.status())
            if far is not None:
                self.assertEqual(far_before, far.status())
            reasons.append(str(raised.exception))
        self.assertEqual(reasons[0], reasons[1])
        return reasons[0]

    def test_a_bare_sco_line_is_refused_with_the_commands_usage(self):
        self.assertIn(fake_esp32.SCO_USAGE, self._refused("sco"))

    def test_a_sco_line_of_nothing_but_spaces_is_refused_the_same_way(self):
        self.assertIn(fake_esp32.SCO_USAGE, self._refused("sco   "))

    def test_an_unknown_subcommand_is_refused_with_the_commands_usage(self):
        self.assertIn(fake_esp32.SCO_USAGE, self._refused("sco bogus"))

    def test_an_extra_operand_is_refused_for_each_form_that_takes_none(self):
        for line in ("sco start extra", "sco stop extra", "sco stats extra"):
            with self.subTest(line=line):
                self.assertIn(fake_esp32.SCO_USAGE, self._refused(line))

    def test_sco_type_without_its_operand_is_refused_with_the_types_usage(self):
        self.assertIn(fake_esp32.SCO_TYPE_USAGE, self._refused("sco type"))

    def test_sco_type_with_an_extra_operand_is_refused(self):
        self.assertIn(fake_esp32.SCO_TYPE_USAGE,
                      self._refused("sco type ev3 extra"))

    def test_sco_type_with_a_name_it_does_not_know_is_refused(self):
        self.assertIn(fake_esp32.SCO_TYPE_USAGE, self._refused("sco type hv3"))

    def test_the_four_forms_it_does_know_are_not_refused(self):
        firmware, shell, _far = self._board(live=False)
        # `type` is the one of the four that works with no link up.
        self.assertEqual([], shell.command(f"{esp32peer.SCO_TYPE} ev3"))
        self.assertEqual("ev3", firmware.status()["sco_type"])
        for line in (esp32peer.SCO_START, esp32peer.SCO_STOP):
            with self.subTest(line=line):
                shell.command(line)
        # `stats` needs a live link, and says so rather than any usage line.
        with self.assertRaises(esp32peer.PeerError) as raised:
            shell.command(esp32peer.SCO_STATS)
        self.assertIn("no audio link to count packets on", str(raised.exception))


class MainTests(unittest.TestCase):
    """The command line: everything after -- belongs to the command."""

    def test_stream_without_a_command_is_a_usage_error(self):
        with _captured_stderr():
            with self.assertRaises(SystemExit) as raised:
                esp32peer.main(["stream", "--acceptor", ACCEPTOR,
                                "--initiator", INITIATOR])
        self.assertEqual(2, raised.exception.code)

    def test_the_commands_own_options_are_not_read_as_this_scripts(self):
        # -U, -z and -t belong to ubertooth-rx; a missing device then ends it
        # with an error, not a usage error.
        with _captured_stderr() as captured:
            code = esp32peer.main([
                "stream", "--acceptor", "/dev/absent-a",
                "--initiator", "/dev/absent-b", "--",
                "ubertooth-rx", "-U", "f" * 32, "-z", "-t", "5"])
        self.assertEqual(1, code)
        self.assertIn("error: /dev/absent-a does not exist", captured())

    def test_the_driver_runs_as_a_script_by_path(self):
        # The cases run it by path, from whatever directory the runner is in,
        # so the package has to be importable from there. Without that every
        # peer command dies in the import before it reaches a board.
        import subprocess
        import tempfile

        result = subprocess.run(
            [sys.executable, esp32peer.__file__, "identify",
             "--device", "/dev/absent-peer"],
            capture_output=True, text=True, cwd=tempfile.gettempdir())
        self.assertEqual(1, result.returncode)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("error: /dev/absent-peer does not exist", result.stderr)

    def test_a_command_after_an_identify_is_refused(self):
        with _captured_stderr():
            with self.assertRaises(SystemExit) as raised:
                esp32peer.main(["identify", "--device", ACCEPTOR, "--", "ls"])
        self.assertEqual(2, raised.exception.code)


class _captured:
    def __init__(self, name):
        self.name = name
        self.buffer = io.StringIO()

    def __enter__(self):
        self.saved = getattr(sys, self.name)
        setattr(sys, self.name, self.buffer)
        return self.buffer.getvalue

    def __exit__(self, *_exc):
        setattr(sys, self.name, self.saved)
        return False


def _captured_stderr():
    return _captured("stderr")


class _captured_stdout:
    """Replace the real file descriptor 1, which a child process inherits."""

    def __enter__(self):
        import os
        import tempfile
        self.file = tempfile.TemporaryFile(mode="w+")
        self.saved = os.dup(1)
        os.dup2(self.file.fileno(), 1)
        return self._read

    def _read(self):
        self.file.seek(0)
        return self.file.read()

    def __exit__(self, *_exc):
        import os
        os.dup2(self.saved, 1)
        os.close(self.saved)
        self.file.close()
        return False


if __name__ == "__main__":
    unittest.main()
