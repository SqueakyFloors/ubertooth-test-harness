"""esp32peer.py: the peer shell, the pair session, and the failures it reports."""

import io
import json
import re
import subprocess
import sys
import tempfile
import time
from unittest import mock
import unittest
import unittest.mock
from pathlib import Path

from ubertooth_harness import afhmap, esp32peer
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
                def boot(self, *why):
                    lines = super().boot(*why)
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

    def test_a_link_of_another_type_than_the_one_asked_for_fails(self):
        # A test asks for ev3 because it wants the payload on the air at the
        # basic rate; being handed the enhanced-rate type instead must fail
        # rather than be reported as a pass.
        class Stubborn(FakePeerFirmware):
            def _sco_start(self):
                answer = super()._sco_start()
                for board in (self, self.peer_firmware):
                    if board is not None and board.sco_pkt_type != "none":
                        board.sco_pkt_type = "2-ev3"
                        board.sco_tx_len = 60
                return answer

        acceptor = Stubborn(ACCEPTOR_ADDRESS, version=esp32peer.AUDIO_FIRMWARE)
        initiator = Stubborn(INITIATOR_ADDRESS, version=esp32peer.AUDIO_FIRMWARE)
        acceptor.link(initiator)
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "reports an audio link using 2-ev3, not the ev3 both boards were "
            "told to ask for"
        ):
            self._audio(firmwares=(acceptor, initiator), packet_type="ev3")

    def test_a_link_that_is_not_esco_fails(self):
        # Both EV3 and 2-EV3 are eSCO types, and everything this session is
        # called says eSCO, so a plain SCO link is not what it carries.
        class Plain(FakePeerFirmware):
            def _sco_start(self):
                answer = super()._sco_start()
                self.sco_link = "sco"
                return answer

        acceptor = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.AUDIO_FIRMWARE)
        initiator = Plain(INITIATOR_ADDRESS, version=esp32peer.AUDIO_FIRMWARE)
        acceptor.link(initiator)
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "the initiator reports the link as sco, not esco"
        ):
            self._audio(firmwares=(acceptor, initiator))

    def test_a_board_that_cannot_name_its_link_fails(self):
        # `default` promises no particular type, so a board reporting one it
        # could not work out is all that is left to check, and it proves
        # nothing about what is on the air.
        class Vague(FakePeerFirmware):
            def status(self):
                state = super().status()
                if state["sco"] == esp32peer.AUDIO_ON:
                    state["sco_pkt_type"] = "unknown"
                    state["sco_tx_len"] = 0
                return state

        acceptor = Vague(ACCEPTOR_ADDRESS, version=esp32peer.AUDIO_FIRMWARE)
        initiator = FakePeerFirmware(INITIATOR_ADDRESS,
                                     version=esp32peer.AUDIO_FIRMWARE)
        acceptor.link(initiator)
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            r"the acceptor cannot say what its audio link carries \(link esco, "
            r"packet type unknown, 0 bytes a packet\)"
        ):
            self._audio(firmwares=(acceptor, initiator), packet_type="default")

    def test_a_negative_frame_floor_is_refused(self):
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "the frame floor cannot be negative"):
            self._audio(min_frames=-1)

    def test_a_link_renegotiated_while_the_command_ran_fails(self):
        # The link the capture was taken against has to be the one the
        # session checked at the start. A board that came up on ev3 and
        # finished on 2-ev3 carried something else on the air for part of
        # the window.
        acceptor, initiator = _audio_pair()

        def renegotiate():
            for board in (acceptor, initiator):
                board.sco_pkt_type = "2-ev3"
                board.sco_tx_len = 60

        initiator.during_the_command(renegotiate)
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "after the command ran, the initiator's audio link reads esco "
            "2-ev3, not the esco ev3 it carried when the command started"
        ):
            self._audio(min_frames=10, firmwares=(acceptor, initiator))

    def test_a_renegotiated_link_fails_even_with_no_type_asked_for(self):
        # `default` pins no type at bring-up, so the reading taken then is
        # the only thing the end of the session can be held to.
        acceptor, initiator = _audio_pair()

        # With no type asked for the stack settles this link on 2-EV3, so
        # the move under test is the other way.
        def renegotiate():
            for board in (acceptor, initiator):
                board.sco_pkt_type = "ev3"
                board.sco_tx_len = 30

        initiator.during_the_command(renegotiate)
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "after the command ran, the initiator's audio link reads esco "
            "ev3, not the esco 2-ev3 it carried when the command started"
        ):
            self._audio(min_frames=10, packet_type="default",
                        firmwares=(acceptor, initiator))

    def test_the_role_change_is_sent_with_the_deadline_it_needs(self):
        # `role` stops the audio link, then the SPP side, then starts the SPP
        # server again, which the firmware's README allows about 18 seconds
        # for whichever of those it has to do. So that command carries the
        # longer deadline in both sessions, not the ordinary five seconds.
        self.assertGreaterEqual(esp32peer.ROLE_TIMEOUT_S, 18.0)
        original = esp32peer.Shell.command
        seen = []

        def record(shell, text, timeout=esp32peer.COMMAND_TIMEOUT_S,
                   parse=None):
            seen.append((text, timeout))
            return original(shell, text, timeout, parse)

        with unittest.mock.patch.object(esp32peer.Shell, "command", record):
            self._audio(min_frames=10)
        self.assertIn((esp32peer.ROLE_INITIATOR, esp32peer.ROLE_TIMEOUT_S),
                      seen)

        # The firmware allows that long for `role` whatever the board was
        # doing, so the stream session sends it with the same deadline.
        seen.clear()
        acceptor, initiator = _peers(*_pair())
        with unittest.mock.patch.object(esp32peer.Shell, "command", record):
            esp32peer.stream(acceptor, initiator, TRUE, out=io.StringIO(),
                             settle_s=0.0, min_writes=1)
        self.assertIn((esp32peer.ROLE_INITIATOR, esp32peer.ROLE_TIMEOUT_S),
                      seen)

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

            def boot(self, *why):
                lines = super().boot(*why)
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

        def silent_boot(*why):
            lines = plain_boot(*why)
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

        def interrupt_after_the_initiator(*why):
            lines = plain_reset(*why)
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


class ChannelMapTests(unittest.TestCase):
    """--map: the piconet is held to a map, and both maps are reported."""

    HALF_BAND = afhmap.hex_from_channels(range(0, 40))
    # What the fake controller leaves of it, as the bench's do: it drops
    # channels 0, 24, and 78 of its own accord.
    IN_FORCE = afhmap.hex_from_channels(set(range(0, 40)) - {0, 24})

    # A command that prints one map line and waits to be interrupted, as
    # ubertooth-afh -r does; it exits 0 on the interrupt, as that does.
    TOOL = (sys.executable, "-c",
            "import signal, sys, time\n"
            "signal.signal(signal.SIGINT, lambda *_a: sys.exit(0))\n"
            "print('1000 ' + '1' * 39 + '0' * 41)\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n")

    def _stream(self, directory, *, wanted=None, command=None, **options):
        """One session on a map; the command's own output is captured too."""
        if wanted is None:
            wanted = self.HALF_BAND
        acceptor_firmware, initiator_firmware = _pair(
            version=esp32peer.AFH_MINIMUM_FIRMWARE)
        acceptor_firmware.version = esp32peer.AFH_MINIMUM_FIRMWARE
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        report = io.StringIO()
        paths = {"map_report": str(Path(directory) / "peer.json"),
                 "command_output": str(Path(directory) / "maps.txt")}
        options.setdefault("timeout", 20.0)
        with _captured_stdout() as captured:
            status = esp32peer.stream(
                acceptor, initiator, command or self.TOOL, out=report,
                settle_s=0.0, min_writes=10, wanted_map=wanted,
                interrupt_after=0.4, **paths, **options)
            written = captured()
        return status, report.getvalue(), initiator_firmware, paths, written

    def test_a_map_is_set_on_the_initiator_before_the_link_comes_up(self):
        with tempfile.TemporaryDirectory() as directory:
            status, report, initiator, _paths, _out = self._stream(directory)
        self.assertEqual(0, status)
        # Before `spp start`, so the leader announces it as the link opens.
        self.assertLess(initiator.commands.index(f"afh {self.HALF_BAND}"),
                        initiator.commands.index("spp start"))
        self.assertIn(f"channel map requested: {self.HALF_BAND} used 40", report)
        self.assertIn("channel map in force: stable", report)
        self.assertIn("pair streamed: yes", report)

    def test_the_link_map_is_read_at_the_start_the_middle_and_the_end(self):
        with tempfile.TemporaryDirectory() as directory:
            _status, report, _initiator, paths, _out = self._stream(directory)
            written = json.loads(Path(paths["map_report"]).read_text())
        for at in esp32peer.SAMPLE_POINTS:
            self.assertIn(f"channel map in force at the {at} ", report)
        self.assertEqual(list(esp32peer.SAMPLE_POINTS),
                         [sample["at"] for sample in written["samples"]])
        self.assertEqual([self.IN_FORCE] * 3,
                         [sample["link"] for sample in written["samples"]])
        self.assertEqual([38] * 3, [sample["used"] for sample in written["samples"]])
        self.assertEqual(self.HALF_BAND, written["requested"])
        self.assertEqual(40, written["requested_used"])
        # The middle reading is taken while the command is running.
        self.assertGreater(written["samples"][1]["seconds"], 0.0)
        self.assertLess(written["samples"][1]["seconds"],
                        written["samples"][2]["seconds"])

    def test_a_link_map_that_changes_under_the_command_fails_the_session(self):
        other = afhmap.hex_from_channels(range(0, 30))
        acceptor_firmware, initiator_firmware = _pair(
            version=esp32peer.AFH_MINIMUM_FIRMWARE)
        acceptor_firmware.version = esp32peer.AFH_MINIMUM_FIRMWARE
        # The link's map is read once per sample, and the reads in between
        # (the link check and the traffic reads) take the earlier entries.
        initiator_firmware.link_maps = [self.IN_FORCE] * 2 + [other] * 8
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        with tempfile.TemporaryDirectory() as directory, _captured_stdout(), \
                self.assertRaisesRegex(
            esp32peer.PeerError, "channel map changed under the command"
        ):
            esp32peer.stream(acceptor, initiator, self.TOOL,
                             out=io.StringIO(), settle_s=0.0, min_writes=10,
                             wanted_map=self.HALF_BAND, interrupt_after=0.4,
                             timeout=20.0,
                             map_report=str(Path(directory) / "peer.json"),
                             command_output=str(Path(directory) / "maps.txt"))

    def test_a_controller_that_will_not_report_the_links_map_is_reported(self):
        acceptor_firmware, initiator_firmware = _pair(
            version=esp32peer.AFH_MINIMUM_FIRMWARE)
        acceptor_firmware.version = esp32peer.AFH_MINIMUM_FIRMWARE
        initiator_firmware.link_error = "the controller does not know this link"
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        with _captured_stdout(), self.assertRaisesRegex(
            esp32peer.PeerError,
            "could not be read at the start of the command: the controller does"
        ):
            esp32peer.stream(acceptor, initiator, self.TOOL, out=io.StringIO(),
                             settle_s=0.0, min_writes=10,
                             wanted_map=self.HALF_BAND, interrupt_after=0.4,
                             timeout=20.0)

    def test_firmware_without_the_channel_map_commands_is_refused(self):
        acceptor_firmware, initiator_firmware = _pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            f"initiator runs {esp32peer.MINIMUM_FIRMWARE}, and the harness needs "
            f"{esp32peer.AFH_MINIMUM_FIRMWARE}"
        ):
            esp32peer.stream(acceptor, initiator, TRUE, out=io.StringIO(),
                             settle_s=0.0, wanted_map=self.HALF_BAND)
        # Nothing was asked of the controller.
        self.assertNotIn(f"afh {self.HALF_BAND}", initiator_firmware.commands)

    def test_a_map_no_piconet_may_use_is_refused_before_a_board_is_opened(self):
        for value, reason in (
            ("ffff", "must be 20 hex digits"),
            ("ff" * 9 + "80", "reserved 80th bit"),
            (afhmap.hex_from_channels(range(19)), "needs at least 20 channels"),
        ):
            with self.subTest(value), self.assertRaisesRegex(
                esp32peer.PeerError, reason
            ):
                esp32peer.channel_map(value)

    def test_off_is_every_channel_and_is_reported_as_the_map_it_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            _status, report, initiator, paths, _out = self._stream(
                directory, wanted=esp32peer.AFH_OFF)
            written = json.loads(Path(paths["map_report"]).read_text())
        self.assertIn(f"afh {esp32peer.AFH_OFF}", initiator.commands)
        self.assertEqual(79, written["requested_used"])
        self.assertIn("channel map requested: ffffffffffffffffff7f used 79", report)

    def test_the_commands_output_is_saved_and_still_passed_through(self):
        with tempfile.TemporaryDirectory() as directory:
            _status, _report, _initiator, paths, written = self._stream(directory)
            saved = Path(paths["command_output"]).read_text()
        expected = "1000 " + "1" * 39 + "0" * 41 + "\n"
        self.assertEqual(expected, saved)
        self.assertEqual(expected, written)

    def test_a_command_that_runs_until_it_is_signalled_is_interrupted_on_time(self):
        with tempfile.TemporaryDirectory() as directory:
            status, report, _initiator, _paths, _out = self._stream(directory)
        self.assertEqual(0, status)
        self.assertRegex(report, r"(?m)^command interrupted after [0-9.]+ s$")
        self.assertRegex(report, r"(?m)^command exit: 0 after [0-9.]+ s$")

    def test_a_command_that_ignores_the_interrupt_is_not_left_running(self):
        # The interrupt is the planned end of the capture, so the command
        # gets its own grace from there; one that will not stop is killed and
        # the reason says it was the stopping that failed, not the window.
        deaf = (sys.executable, "-c",
                "import signal, time\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                "time.sleep(60)\n")
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            esp32peer.PeerError,
            f"did not stop within {esp32peer.INTERRUPT_TIMEOUT_S:g}s of the "
            "interrupt"
        ):
            self._stream(directory, command=deaf, timeout=120.0)

    def test_a_command_that_overruns_its_own_deadline_says_so(self):
        # No interrupt asked for: the deadline is the whole command's, and
        # the reason names it.
        quiet = (sys.executable, "-c", "import time; time.sleep(60)")
        report = io.StringIO()
        with _captured_stdout(), self.assertRaisesRegex(
            esp32peer.PeerError, "did not finish within 1s"
        ):
            esp32peer._run_command_watched(quiet, timeout=1.0, out=report)

    def test_a_sample_that_fails_mid_capture_does_not_leave_the_tool_running(self):
        # A link that drops under the capture makes the middle read fail. A
        # capture tool left running would hold the board past the step and
        # into the retry, so the command is stopped before the error goes up.
        marker = Path(tempfile.mkdtemp()) / "still-running"
        child = (sys.executable, "-c",
                 "import signal, sys, time\n"
                 "signal.signal(signal.SIGINT, lambda *_a: sys.exit(0))\n"
                 f"open({str(marker)!r}, 'w').close()\n"
                 "time.sleep(60)\n")

        def fails(_elapsed):
            # Wait for the child to be up before failing the sample: a loaded
            # machine can take longer to start an interpreter than the sample
            # interval, and the point of the test is what happens to a
            # command that is running.
            deadline = time.monotonic() + 10.0
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            raise esp32peer.PeerError("the controller does not know this link")

        started = []
        real_popen = subprocess.Popen

        def record(*args, **options):
            child_process = real_popen(*args, **options)
            started.append(child_process)
            return child_process

        report = io.StringIO()
        with mock.patch.object(esp32peer.subprocess, "Popen", record), \
                _captured_stdout(), self.assertRaisesRegex(
            esp32peer.PeerError, "does not know this link"
        ):
            esp32peer._run_command_watched(
                child, timeout=30.0, interrupt_after=10.0, sample_at=0.3,
                sample=fails, out=report)
        self.assertTrue(marker.exists())  # it really started
        # The command was stopped and reaped before the error went up.
        (ran,) = started
        self.assertIsNotNone(ran.poll())

    def test_a_map_report_without_a_map_is_a_usage_error(self):
        acceptor_firmware, initiator_firmware = _pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        with self.assertRaisesRegex(esp32peer.PeerError, "--map-report needs --map"):
            esp32peer.stream(acceptor, initiator, TRUE, out=io.StringIO(),
                             settle_s=0.0, map_report="/dev/null")


class BridgeLossTests(unittest.TestCase):
    """Bytes lost between the board and this process (#310).

    What the bench measured is a contiguous run of bytes gone with the
    newlines either side intact, so the line arrives looking whole: `OK` as
    `K`, and a status line with a hole in the middle of it. Holding a
    partial line until its newline, which the shell already did, cannot
    catch that, and four of about seventy pair sessions hit it on a host
    that was also capturing over USB.
    """

    FRAMED = esp32peer.FRAME_MINIMUM_FIRMWARE

    def _shell(self, *losses, version=FRAMED, chunk=None, step=0.05):
        bridge = fake_esp32.LossyBridge(*losses)
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS, version=version)
        board = shell(firmware, "acceptor", bridge, chunk, step)
        board.wait_ready()
        board.ask_for_framing(board.version)
        return board, firmware, bridge

    def test_the_crc_is_the_one_the_firmware_computes(self):
        # CRC-16/CCITT-FALSE: 0xffff in, polynomial 0x1021, no reflection
        # and no final xor. The check vectors the standard is usually
        # quoted with, so a change of polynomial or seed on either side
        # fails here rather than on the bench.
        self.assertEqual(0x29B1, esp32peer.crc16(b"123456789"))
        self.assertEqual(0xFFFF, esp32peer.crc16(b""))
        self.assertEqual(0xF952, esp32peer.crc16(b"OK"))

    def test_the_frame_measures_the_line_the_board_wrote(self):
        """Blanks at the end of a line belong to it, and the length counts them.

        Nothing the firmware writes ends in one today. Taking the blanks off
        before the check would make the first line that does fail every read,
        so they come off after it.
        """
        board, firmware, _bridge = self._shell()
        firmware.refuse["status"] = "no  "

        with self.assertRaisesRegex(esp32peer.PeerError,
                                    r"'status' refused: no"):
            board.status()
        self.assertEqual(0, board.lost)

    def test_a_line_that_arrives_a_few_bytes_at_a_time_is_read_whole(self):
        # The status line is longer than one read either way, and under
        # three bytes a read it takes hundreds. A chunk boundary mid-line
        # was never the fault: this is here so that stays true now that
        # every line goes through the frame check as well.
        board, _firmware, _bridge = self._shell(chunk=3, step=0.0005)

        self.assertEqual(ACCEPTOR_ADDRESS, board.status()["address"])
        self.assertEqual(0, board.lost)
        self.assertEqual(0, board.retried)

    def test_a_status_line_with_a_hole_in_it_is_read_again(self):
        # 20 bytes out of the middle, which takes the JSON with them.
        board, _firmware, bridge = self._shell(('"role"', 60, 20))

        self.assertEqual(ACCEPTOR_ADDRESS, board.status()["address"])
        self.assertEqual(1, len(bridge.damaged))
        self.assertEqual(1, board.lost)
        self.assertEqual(1, board.retried)
        self.assertEqual("acceptor bridge: 1 line(s) lost, 1 command(s) sent "
                         "again", board.serial_report())

    def test_a_hole_that_leaves_the_status_readable_is_caught_by_the_frame(self):
        """The case the firmware's framing is for.

        Three bytes out of the channel map leave a shorter map, which is
        still a string in still-valid JSON, so nothing about the line says
        it is wrong and every field this driver type-checks is there. The
        length and CRC the firmware puts around the line are what catch it.
        """
        board, _firmware, bridge = self._shell(('"afh"', 40, 3))

        status = board.status()

        self.assertEqual(1, len(bridge.damaged))
        self.assertEqual(1, board.lost)
        self.assertEqual(1, board.retried)
        self.assertEqual(fake_esp32.ALL_CHANNELS, status["afh"])

    def test_without_the_frame_that_same_hole_is_taken_at_face_value(self):
        """Why the firmware had to change, stated as a test.

        On an image with no framing the same loss passes: the line is JSON,
        every field is of the right type, and the map it reports is three
        bytes shorter than the one the controller has. A test comparing a
        recovered map against it would be comparing against a lie.
        """
        board, _firmware, bridge = self._shell(('"afh"', 12, 3),
                                               version="utesp-1.5.0")

        status = board.status()

        self.assertFalse(board.framed)
        self.assertEqual(1, len(bridge.damaged))
        self.assertEqual(0, board.lost)
        self.assertEqual(0, board.retried)
        self.assertNotEqual(fake_esp32.ALL_CHANNELS, status["afh"])

    def test_an_ok_that_lost_its_first_byte_costs_a_second_attempt(self):
        # The bench's other sighting: 'K', which is `OK` a byte short, so
        # the command never saw its ending and timed out.
        board, firmware, bridge = self._shell(("OK", 0, 1),
                                              version="utesp-1.5.0")

        self.assertEqual(ACCEPTOR_ADDRESS, board.status()["address"])
        self.assertEqual(1, len(bridge.damaged))
        self.assertEqual(1, board.retried)
        self.assertEqual(["status", "status"], firmware.commands)

    def test_a_status_that_stays_unreadable_is_still_refused(self):
        """Every attempt damaged, so nothing readable ever arrives.

        A half-read status is worse than none, so the read fails rather
        than the session carrying on with whatever happened to parse. On a
        framed board the frame is what refuses it; on an older image it is
        the JSON. Both are refusals, and neither is a retry forever.

        A framed board costs one send more than the retries, because the
        last of them is the question _ask_unframed has to put to tell a
        board that stopped framing from a bridge that is losing lines.
        """
        for version, reason, sends in (
            (self.FRAMED, "a line from the board lost bytes on its bridge",
             esp32peer.COMMAND_RETRIES + 2),
            ("utesp-1.5.0", "status is not JSON",
             esp32peer.COMMAND_RETRIES + 1),
        ):
            board, firmware, _bridge = self._shell(
                *[('"role"', 60, 20)] * sends, version=version)
            with self.subTest(version), self.assertRaisesRegex(
                esp32peer.SerialLoss, reason
            ):
                board.status()
            self.assertEqual(sends, firmware.commands.count("status"))

    def test_a_bridge_that_only_loses_lines_is_not_called_a_restart(self):
        """The last resort for a restart must not fire for a lossy bridge.

        A board that has stopped framing has restarted, and asking once
        without the framing is how that is settled. An answer that comes
        back framed says the board never stopped, so the attempts before it
        lost their lines and that is all.
        """
        board, _firmware, _bridge = self._shell(
            *[('"role"', 60, 20)] * (esp32peer.COMMAND_RETRIES + 2))

        with self.assertRaises(esp32peer.SerialLoss):
            board.status()
        self.assertEqual([], board.restarts)
        self.assertTrue(board.framed)

    def test_a_json_status_missing_a_field_is_refused_rather_than_patched(self):
        for line, reason in (
            ('{"role":"acceptor"}', "status has no valid 'fw'"),
            ('["role"]', "status is not a JSON object"),
            ('{"role":"acceptor"', "status is not JSON"),
        ):
            with self.subTest(line), self.assertRaisesRegex(
                esp32peer.SerialLoss, reason
            ):
                esp32peer.parse_status(line, "acceptor")

    def test_a_command_that_cannot_be_repeated_is_not(self):
        """A lost answer to `role initiator` fails; the role is not set twice.

        `spp start`, `spp stop`, `role`, `sco start`, `sco stop`,
        `defaults` and `reset` each move the board or the link on, so
        sending one again on a lost answer would act on a board that had
        already done it.
        """
        board, firmware, bridge = self._shell(("OK", 0, 1),
                                              version="utesp-1.5.0")

        with self.assertRaisesRegex(esp32peer.SerialLoss,
                                    "no answer to 'role initiator'"):
            board.command(esp32peer.ROLE_INITIATOR)
        self.assertEqual(1, len(bridge.damaged))
        self.assertEqual(1, firmware.commands.count(esp32peer.ROLE_INITIATOR))
        self.assertEqual(0, board.retried)

    def test_which_commands_may_be_sent_again(self):
        for text in ("status", "version", "sco stats", "frame on",
                     f"spp peer {ACCEPTOR_ADDRESS}", "stream period 20",
                     "stream payload 200", "stream on", "stream off",
                     "disc off", "sco type ev3", "afh off"):
            with self.subTest(text):
                self.assertTrue(esp32peer.Shell.repeatable(text))
        for text in ("spp start", "spp stop", "role initiator", "sco start",
                     "sco stop", "defaults", "reset"):
            with self.subTest(text):
                self.assertFalse(esp32peer.Shell.repeatable(text))

    def test_a_refusal_is_a_refusal_and_is_not_sent_again(self):
        board, firmware, _bridge = self._shell()
        firmware.refuse["status"] = "bluetooth is not up"

        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "'status' refused: bluetooth is not up"):
            board.status()
        self.assertEqual(1, firmware.commands.count("status"))
        self.assertEqual(0, board.retried)


class FramingTests(unittest.TestCase):
    """`frame on`, and what a session does on an image that has not got it."""

    def test_the_frame_is_asked_for_and_the_answers_carry_it(self):
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        board = shell(firmware, "acceptor")
        board.wait_ready()

        self.assertTrue(board.ask_for_framing(board.version))
        self.assertTrue(board.framed)
        self.assertIn(esp32peer.FRAME_ON, firmware.commands)
        self.assertTrue(firmware.framing)
        # A framed line still reads as the line it is.
        self.assertEqual([esp32peer.FRAME_MINIMUM_FIRMWARE],
                         board.command("version"))

    def test_the_answer_to_frame_on_is_in_the_form_that_was_in_force(self):
        """So no line's form is unpredictable at the switch.

        The firmware answers `frame` and then changes, which means the OK
        to `frame on` is bare and the line after it is framed. Were it the
        other way round, a driver would have to accept either form for one
        line in each direction.
        """
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        port = FakePeerPort(firmware)
        board = esp32peer.Shell(port, "acceptor", fake_esp32.FakeClock())
        board.wait_ready()

        port.write(b"frame on\n")
        self.assertEqual("OK", port.pending.decode().strip())
        port.pending.clear()
        port.write(b"version\n")
        self.assertTrue(port.pending.decode().startswith("#11,"))

    def test_an_older_image_is_driven_without_the_frame(self):
        # The command arrived in utesp-1.6.0 and nothing in the harness
        # flashes a peer, so a board on an older 1.x image has to keep
        # working: the retry is what saves its runs.
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS, version="utesp-1.5.0")
        board = shell(firmware, "acceptor")
        board.wait_ready()

        self.assertFalse(board.ask_for_framing(board.version))
        self.assertFalse(board.framed)
        self.assertEqual(ACCEPTOR_ADDRESS, board.status()["address"])

    def test_an_image_that_should_frame_and_will_not_fails_the_session(self):
        """Carrying on unframed there would give up the check silently.

        An older image has no such command and is driven as it always was.
        One whose version says it has the command and refuses it is a board
        whose lines cannot be checked, so that is the session's failure
        rather than a quiet fallback.
        """
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        firmware.refuse[esp32peer.FRAME_ON] = "unknown command, try \"help\""
        board = shell(firmware, "acceptor")
        board.wait_ready()

        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "'frame on' refused"):
            board.ask_for_framing(board.version)

    def test_a_boot_notice_that_named_an_older_image_than_the_status_fails(self):
        """The hole a corrupted boot notice would otherwise leave.

        The framing is asked for on the version in the boot notice, which is
        read before anything is checking the lines. A notice that lost bytes
        could name an image too old to frame, and the session would then run
        unchecked against a board that does. The status line is the version
        check proper, so the two have to agree.
        """
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        board = shell(firmware, "acceptor")
        board.wait_ready()

        self.assertFalse(board.ask_for_framing("utesp-1.5.0"))
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            f"acceptor runs {esp32peer.FRAME_MINIMUM_FIRMWARE}, which "
            "check-sums the lines it sends, but this session is not reading "
            "framed lines: its boot notice named "
            f"{esp32peer.FRAME_MINIMUM_FIRMWARE}"
        ):
            board.confirm_framing(esp32peer.FRAME_MINIMUM_FIRMWARE)

    def test_an_older_image_passes_the_same_check(self):
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS, version="utesp-1.5.0")
        board = shell(firmware, "acceptor")
        board.wait_ready()
        board.ask_for_framing(board.version)

        board.confirm_framing("utesp-1.5.0")  # no complaint
        self.assertFalse(board.framed)

    def test_a_session_says_whether_each_bridge_is_framed(self):
        acceptor, initiator = _pair(version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        acceptor.version = "utesp-1.5.0"
        report = io.StringIO()

        esp32peer.stream(*_peers(acceptor, initiator), TRUE, out=report,
                         settle_s=0.0, min_writes=1)

        self.assertIn("initiator bridge: framed", report.getvalue())
        self.assertIn("acceptor bridge: not framed, utesp-1.5.0 is older than "
                      f"{esp32peer.FRAME_MINIMUM_FIRMWARE}", report.getvalue())

    def test_a_pair_session_survives_a_lost_line_and_says_so(self):
        acceptor, initiator = _pair(version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        acceptor.version = esp32peer.FRAME_MINIMUM_FIRMWARE
        bridges = {INITIATOR: fake_esp32.LossyBridge(('"role"', 60, 20))}
        open_one = opener({ACCEPTOR: acceptor, INITIATOR: initiator}, bridges)
        peers = (esp32peer.Peer(ACCEPTOR, "acceptor", ACCEPTOR_ADDRESS,
                                opener=open_one),
                 esp32peer.Peer(INITIATOR, "initiator", INITIATOR_ADDRESS,
                                opener=open_one))
        report = io.StringIO()

        status = esp32peer.stream(*peers, TRUE, out=report, settle_s=0.0,
                                  min_writes=1)

        self.assertEqual(0, status)
        self.assertIn("pair streamed: yes", report.getvalue())
        self.assertIn("initiator bridge: 1 line(s) lost, 1 command(s) sent "
                      "again", report.getvalue())

    def test_the_settings_the_board_took_are_read_back(self):
        """A command line can lose bytes too, and still be a command.

        `stream payload 200` arriving as `stream payload 20` is another
        valid setting, and its OK says nothing about which. So the session
        reads the values back off the board rather than trusting the OK.
        """
        for field, value, reason in (
            ("payload", 20, r"payload_len 20 where this session set 200"),
            ("period", 2000, r"period_ms 2000 where this session set 20"),
            ("peer", INITIATOR_ADDRESS, r"peer '20:50:0D:E9:C5:BA' where this "
                                        r"session set 'C8:85:41:2A:1B:2A'"),
        ):
            acceptor, initiator = _pair()
            plain = initiator.handle

            def handle(line, field=field, value=value, initiator=initiator,
                       plain=plain):
                answer = plain(line)
                if line == "stream on":
                    # A setting that arrived as another valid one, which
                    # is what a command line short of bytes can be.
                    setattr(initiator, field, value)
                return answer
            initiator.handle = handle
            with self.subTest(field), self.assertRaisesRegex(
                esp32peer.PeerError,
                "the initiator did not take this session's stream settings: "
                "the initiator reports " + reason
            ):
                esp32peer.stream(*_peers(acceptor, initiator), TRUE,
                                 out=io.StringIO(), settle_s=0.0,
                                 period=20, payload=200, min_writes=1)


class RestartTests(unittest.TestCase):
    """A peer that goes away under a run, and is reported as that (#311).

    One follow case in eight ended with the initiator back at boot, and
    every counter reading zero was reported as a pair that never streamed.
    The board says plenty on its way down and on its way back; the shell
    was clearing the port before each command, so none of it was read.
    """

    def _crash_during(self, which=("initiator", "acceptor"), **options):
        """Run a stream session whose named boards restart under the command."""
        acceptor, initiator = _pair(version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        acceptor.version = esp32peer.FRAME_MINIMUM_FIRMWARE
        shells = {}
        open_one = opener({ACCEPTOR: acceptor, INITIATOR: initiator})

        def remember(device, label):
            shells[label] = open_one(device, label)
            return shells[label]

        peers = (esp32peer.Peer(ACCEPTOR, "acceptor", ACCEPTOR_ADDRESS,
                                opener=remember),
                 esp32peer.Peer(INITIATOR, "initiator", INITIATOR_ADDRESS,
                                opener=remember))
        plain = esp32peer._run_command

        def crash_then_run(command, timeout, drain=None):
            for label in which:
                shells[label].port.crash()
            return plain(command, timeout, drain)

        report = io.StringIO()
        with mock.patch.object(esp32peer, "_run_command", crash_then_run):
            try:
                esp32peer.stream(*peers, TRUE, out=report, settle_s=0.0,
                                 min_writes=10, **options)
            except esp32peer.PeerError as error:
                return str(error), report.getvalue(), acceptor, initiator
        self.fail("a session whose peer restarted must not pass")

    def test_the_failure_names_the_restart_and_not_a_pair_that_never_streamed(self):
        error, report, _acceptor, _initiator = self._crash_during(("initiator",))

        self.assertIn("a peer restarted while the session ran", error)
        self.assertIn("the initiator restarted while the session ran", error)
        self.assertNotIn("the pair did not stream", error)
        self.assertIn("initiator restarted:", report)

    def test_the_reason_the_chip_gives_and_the_version_it_came_back_on(self):
        error, report, _acceptor, _initiator = self._crash_during(("initiator",))

        self.assertIn(fake_esp32.PANIC_REASON, error)
        self.assertIn(f"back on {esp32peer.FRAME_MINIMUM_FIRMWARE}", error)
        # And the lines themselves, so a reader can see what the board
        # said rather than only this driver's reading of it.
        self.assertIn(f"initiator restart line: {fake_esp32.PANIC_RST}", report)
        self.assertIn("initiator restart line: Guru Meditation Error", report)

    def test_both_peers_are_reset_after_a_restart(self):
        _error, _report, acceptor, initiator = self._crash_during(("initiator",))

        # The release reset at the end of the session, on top of the one
        # opening the bridge caused and the restart itself.
        self.assertEqual(2, acceptor.resets)
        self.assertEqual(3, initiator.resets)
        self.assertEqual(esp32peer.BOOT_ROLE, initiator.role)
        self.assertFalse(initiator.connected)
        self.assertFalse(acceptor.connected)

    def test_a_restart_on_either_board_is_named_for_that_board(self):
        error, _report, _acceptor, _initiator = self._crash_during(
            ("initiator", "acceptor"))

        self.assertIn("the initiator restarted while the session ran", error)
        self.assertIn("the acceptor restarted while the session ran", error)

    def test_a_restart_the_bridge_did_not_show_is_caught_by_the_role(self):
        """The belt to the frame's braces.

        The operating system's buffer holds what the board said while the
        command ran, and a long enough capture with a chatty board can
        overflow it. The board's own state still says what happened: this
        session set `role initiator`, and a board is in the boot role with
        its server listening only when it has just booted.
        """
        acceptor, initiator = _pair()
        plain = initiator.handle

        def handle(line):
            answer = plain(line)
            if line == "stream off":
                # Back at boot, with nothing of the restart read.
                initiator.defaults()
            return answer
        initiator.handle = handle

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "a peer restarted while the session ran: the initiator reports "
            "role acceptor with its SPP server on, where this session set "
            "initiator: the board is back in its boot state"
        ):
            esp32peer.stream(*_peers(acceptor, initiator), TRUE,
                             out=io.StringIO(), settle_s=0.0, min_writes=1)

    def test_the_resets_this_driver_asks_for_are_not_restarts(self):
        # Opening the bridge resets the board, and so does the release at
        # the end of a session. Neither is #311, and a session that
        # reported them as one would fail every run.
        acceptor, initiator = _pair()
        report = io.StringIO()

        esp32peer.stream(*_peers(acceptor, initiator), TRUE, out=report,
                         settle_s=0.0, min_writes=1)

        self.assertIn("pair streamed: yes", report.getvalue())
        self.assertNotIn("restarted", report.getvalue())

    def test_a_link_notice_while_nothing_is_asking_is_kept_not_mistaken(self):
        """A link coming up or going down is news, not a restart.

        Those notices arrive at the same moment a boot banner would, with
        no command in flight, so the one has to be told from the other.
        """
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        board = shell(firmware, "acceptor")
        board.wait_ready()
        board.port.pending += (
            f"utesp: spp connected {INITIATOR_ADDRESS}\r\n"
            "utesp: spp disconnected\r\n"
            "W (12345) BT_APPL: something the stack says\r\n"
        ).encode()

        board.drain()

        self.assertEqual([], board.restarts)
        self.assertIn(f"utesp: spp connected {INITIATOR_ADDRESS}", board.notices)
        self.assertIn("utesp: spp disconnected", board.notices)
        self.assertEqual(ACCEPTOR_ADDRESS, board.status()["address"])

    def test_a_restart_found_after_the_command_is_still_reported(self):
        """A restart the post-command reads turned up, not the command.

        The session used to take its restart report once, straight after the
        command. A board that went away while `stream off` or the status
        reads were running was then reported as whatever those reads made of
        it, which is the symptom and not the cause.
        """
        acceptor, initiator = _pair()
        shells = {}
        open_one = opener({ACCEPTOR: acceptor, INITIATOR: initiator})

        def remember(device, label):
            shells[label] = open_one(device, label)
            return shells[label]

        plain = initiator.handle

        def handle(line):
            answer = plain(line)
            if line == "stream off":
                shells["initiator"].port.crash()
            return answer
        initiator.handle = handle
        peers = (esp32peer.Peer(ACCEPTOR, "acceptor", ACCEPTOR_ADDRESS,
                                opener=remember),
                 esp32peer.Peer(INITIATOR, "initiator", INITIATOR_ADDRESS,
                                opener=remember))

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "a peer restarted while the session ran: the initiator restarted "
            f"while the session ran, .*{fake_esp32.PANIC_REASON}"
        ):
            esp32peer.stream(*peers, TRUE, out=io.StringIO(), settle_s=0.0,
                             min_writes=1)

    def test_a_command_that_overran_says_the_peer_restarted_too(self):
        """The path the session cannot reach by itself.

        A command that overruns its deadline, a sample that fails, a bridge
        that goes away: each leaves the session by raising, and a board that
        went away is the likeliest reason for most of them. The evidence
        goes out with the failure instead of being dropped on the way past.
        """
        acceptor, initiator = _pair()
        shells = {}
        open_one = opener({ACCEPTOR: acceptor, INITIATOR: initiator})

        def remember(device, label):
            shells[label] = open_one(device, label)
            return shells[label]

        peers = (esp32peer.Peer(ACCEPTOR, "acceptor", ACCEPTOR_ADDRESS,
                                opener=remember),
                 esp32peer.Peer(INITIATOR, "initiator", INITIATOR_ADDRESS,
                                opener=remember))
        plain_run = esp32peer._run_command

        def crash_then_overrun(command, timeout, drain=None):
            shells["initiator"].port.crash()
            return plain_run(command, 0.2, drain)

        with mock.patch.object(esp32peer, "_run_command", crash_then_overrun), \
                self.assertRaisesRegex(
                    esp32peer.PeerError,
                    "a peer restarted while the session ran: the initiator "
                    "restarted while the session ran, .*; and then: .*did not "
                    "finish within"):
            esp32peer.stream(*peers,
                             (sys.executable, "-c", "import time; time.sleep(5)"),
                             out=io.StringIO(), settle_s=0.0, min_writes=1)

    def test_a_restart_just_before_the_release_is_not_thrown_away(self):
        """`reset` used to clear the port before anything read it.

        The release reset is the last thing a session does, and it cleared
        the bridge before pulsing the board. A restart whose lines were
        still waiting there went with them.
        """
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        board = shell(firmware, "acceptor")
        board.wait_ready()
        board.port.crash()  # nothing has read this yet

        board.reset()

        self.assertEqual(1, len(board.restarts))
        self.assertEqual(fake_esp32.PANIC_REASON, board.restarts[0].reason)

    def test_an_audio_session_reports_a_restart_the_same_way(self):
        acceptor, initiator = _audio_pair()
        shells = {}
        open_one = opener({ACCEPTOR: acceptor, INITIATOR: initiator})

        def remember(device, label):
            shells[label] = open_one(device, label)
            return shells[label]

        peers = (esp32peer.Peer(ACCEPTOR, "acceptor", ACCEPTOR_ADDRESS,
                                opener=remember),
                 esp32peer.Peer(INITIATOR, "initiator", INITIATOR_ADDRESS,
                                opener=remember))
        plain = esp32peer._run_command

        def crash_then_run(command, timeout, drain=None):
            shells["initiator"].port.crash()
            return plain(command, timeout, drain)

        with mock.patch.object(esp32peer, "_run_command", crash_then_run), \
                self.assertRaisesRegex(
                    esp32peer.PeerError,
                    "a peer restarted while the session ran: the initiator "
                    "restarted while the session ran"):
            esp32peer.audio(*peers, TRUE, out=io.StringIO(), settle_s=0.0,
                            min_frames=1)


    def test_a_restart_is_seen_when_its_rom_line_lost_bytes(self):
        """The hole the second review round found.

        A board that has restarted is not framing, so its boot notices
        arrive bare. The ROM's reset line is what normally tells the shell
        the board rebooted; damage that line and, before the fix, every
        notice after it was refused as a line that had lost its frame, so
        the restart left nothing at all behind.
        """
        bridge = fake_esp32.LossyBridge((fake_esp32.PANIC_RST, 4, 12))
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        board = shell(firmware, "acceptor", bridge)
        board.wait_ready()
        board.ask_for_framing(board.version)
        board.port.crash()

        board.drain()

        self.assertEqual(1, len(board.restarts))
        self.assertEqual(fake_esp32.PANIC_REASON, board.restarts[0].reason)
        self.assertEqual(esp32peer.FRAME_MINIMUM_FIRMWARE,
                         board.restarts[0].version)
        self.assertFalse(board.framed)
        # And the board is readable again afterwards, because the boot
        # notice cleared the framing the board no longer has.
        self.assertEqual(ACCEPTOR_ADDRESS, board.status()["address"])

    def test_a_read_that_failed_after_the_command_is_not_called_a_restart(self):
        """Only a restart makes a session report one.

        The reads after the command can fail for their own reasons - a
        bridge that went away, a line lost for the last time - and a session
        that called those a restart would send a reader looking for a board
        that never left.
        """
        acceptor, initiator = _pair()
        plain = initiator.handle

        def handle(line):
            if line == "status" and "stream off" in initiator.commands:
                initiator.commands.append(line)
                return []  # silent, so the read runs out of attempts
            return plain(line)
        initiator.handle = handle

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            r"the pair did not stream: initiator: no answer to 'status'"
        ):
            esp32peer.stream(*_peers(acceptor, initiator), TRUE,
                             out=io.StringIO(), settle_s=0.0, min_writes=1)

    def test_an_audio_counter_line_that_lost_bytes_is_read_again(self):
        """`sco stats` is repeatable, so its answer has to be parsed inside it.

        On an image with no framing a damaged counter line is caught by
        failing to parse, and that has to happen where the retry can see
        it: parsing after the command returned made the one command whose
        answer is JSON the one command that was never sent twice.
        """
        bridge = fake_esp32.LossyBridge(('"rx_total"', 8, 12))
        acceptor = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.AUDIO_FIRMWARE)
        initiator = FakePeerFirmware(INITIATOR_ADDRESS,
                                     version=esp32peer.AUDIO_FIRMWARE)
        acceptor.link(initiator)
        open_one = opener({ACCEPTOR: acceptor, INITIATOR: initiator},
                          {INITIATOR: bridge})
        peers = (esp32peer.Peer(ACCEPTOR, "acceptor", ACCEPTOR_ADDRESS,
                                opener=open_one),
                 esp32peer.Peer(INITIATOR, "initiator", INITIATOR_ADDRESS,
                                opener=open_one))
        report = io.StringIO()

        status = esp32peer.audio(*peers, TRUE, out=report, settle_s=0.0,
                                 min_frames=1)

        self.assertEqual(0, status)
        self.assertIn("pair carried audio", report.getvalue())
        self.assertEqual(1, len(bridge.damaged))
        self.assertIn("initiator bridge: 0 line(s) lost, 1 command(s) sent "
                      "again", report.getvalue())

    def test_a_framed_board_that_stops_framing_restarted(self):
        """The last resort when no boot line at all arrived.

        The bridge's buffer can overflow while the capture runs, and a
        boot's lines are the ones with no command waiting on them, so a
        restart can leave nothing behind. The board still gives it away:
        framing is cleared by a reset and by nothing else, so a board that
        was framing and answers bare has been away.
        """
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        board = shell(firmware, "acceptor")
        board.wait_ready()
        board.ask_for_framing(board.version)
        # A restart whose every line went missing: the board is back at
        # boot and not framing, and the host heard none of it.
        board.port.crash()
        board.port.pending.clear()

        status = board.status()

        self.assertEqual(ACCEPTOR_ADDRESS, status["address"])
        self.assertEqual(1, len(board.restarts))
        self.assertIn("stopped check-summing its lines",
                      board.restarts[0].reason)
        self.assertFalse(board.framed)

    def test_an_acceptor_that_forgot_the_link_restarted(self):
        """The acceptor's own test, since no session sets its role.

        The session read the pair's link up before the command, so the
        acceptor answered a page. A board that afterwards counts no link at
        all has been back to boot since.
        """
        acceptor, initiator = _pair()
        plain = acceptor.handle

        def handle(line):
            if line == "status" and "stream off" in initiator.commands:
                acceptor.defaults()  # back at boot, nothing of it heard
            return plain(line)
        acceptor.handle = handle

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "a peer restarted while the session ran: the acceptor counts no "
            "link at all, where this session saw the pair's link come up to "
            "it"
        ):
            esp32peer.stream(*_peers(acceptor, initiator), TRUE,
                             out=io.StringIO(), settle_s=0.0, min_writes=1)

    def test_an_audio_initiator_back_at_boot_restarted(self):
        acceptor, initiator = _audio_pair()
        plain = initiator.handle

        del plain
        # Back at boot while the command ran, with nothing of it heard: the
        # session's reading after the command is what has to catch it.
        initiator.during_the_command(initiator.defaults)

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "a peer restarted while the session ran: .*the initiator reports "
            "role acceptor with its SPP server on"
        ):
            esp32peer.audio(*_peers(acceptor, initiator), TRUE,
                            out=io.StringIO(), settle_s=0.0, min_frames=1)

    def test_frame_off_puts_the_shell_back_in_step_with_the_board(self):
        """The documented transition, driven through `Shell.command()`.

        Nothing in the harness turns the framing off, but the command is
        there for a person reading a board by hand, and a shell left
        expecting frames after it would refuse every line that followed.
        """
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.FRAME_MINIMUM_FIRMWARE)
        board = shell(firmware, "acceptor")
        board.wait_ready()
        board.ask_for_framing(board.version)
        self.assertTrue(board.framed)

        board.command(esp32peer.FRAME_OFF)

        self.assertFalse(board.framed)
        self.assertFalse(firmware.framing)
        self.assertEqual(ACCEPTOR_ADDRESS, board.status()["address"])
        self.assertEqual(0, board.lost)
        # And it is not repeatable: its answer is in the old form and the
        # next line is in the new one, so a second attempt would be read
        # for the wrong form.
        self.assertFalse(esp32peer.Shell.repeatable(esp32peer.FRAME_OFF))

class DrainTests(unittest.TestCase):
    """The boards are read while the command under test runs (#311).

    Nothing asks them anything then, which on the bench was the whole of
    the capture, so this is the only moment a restart can be seen as it
    happens rather than deduced afterwards.
    """

    SLEEP = (sys.executable, "-c", "import time; time.sleep(0.6)")

    def test_the_plain_run_drains_on_its_cadence(self):
        drains = []
        with mock.patch.object(esp32peer, "DRAIN_S", 0.05):
            status, seconds = esp32peer._run_command(
                self.SLEEP, 30.0, lambda: drains.append(time.monotonic()))

        self.assertEqual(0, status)
        self.assertGreater(seconds, 0.5)
        self.assertGreater(len(drains), 2)

    def test_the_watched_run_drains_on_its_cadence(self):
        drains = []
        with mock.patch.object(esp32peer, "DRAIN_S", 0.05):
            status, _seconds = esp32peer._run_command_watched(
                self.SLEEP, timeout=30.0, out=io.StringIO(),
                pass_through=io.StringIO(),
                drain=lambda: drains.append(time.monotonic()))

        self.assertEqual(0, status)
        self.assertGreater(len(drains), 2)

    def test_a_command_with_no_drain_still_runs_to_its_deadline(self):
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "did not finish within 0.2s"):
            esp32peer._run_command(self.SLEEP, 0.2)

    def test_a_command_being_drained_still_runs_to_its_deadline(self):
        with mock.patch.object(esp32peer, "DRAIN_S", 0.05), \
                self.assertRaisesRegex(esp32peer.PeerError,
                                       "did not finish within 0.2s"):
            esp32peer._run_command(self.SLEEP, 0.2, lambda: None)

    def test_a_drain_keeps_a_line_that_has_not_finished_arriving(self):
        # A boot log that spans two drains is still read as one boot, so
        # the reason line is not lost to a read that caught half of it.
        firmware = FakePeerFirmware(ACCEPTOR_ADDRESS)
        board = shell(firmware, "acceptor")
        board.wait_ready()
        board.port.crash()
        whole = bytes(board.port.pending)
        half = whole.index(fake_esp32.PANIC_RST.encode()) + 10
        board.port.pending = bytearray(whole[:half])

        board.drain()
        self.assertEqual([], board.restarts)  # still arriving
        board.port.pending += whole[half:]
        board.drain()

        self.assertEqual(1, len(board.restarts))
        self.assertIn(fake_esp32.PANIC_RST, board.restarts[0].lines)
        self.assertEqual(fake_esp32.PANIC_REASON, board.restarts[0].reason)


class PeerFirmwareFramingSourceTests(unittest.TestCase):
    """The firmware's side of the frame, read out of its source."""

    def test_the_firmware_computes_the_same_crc(self):
        source = (FIRMWARE / "main" / "main.c").read_text()

        self.assertIn("crc = 0xffff;", source)
        self.assertIn("0x1021", source)
        self.assertIn('"#%d,%04x:"', source)

    def test_the_frame_command_is_in_the_help_text(self):
        source = (FIRMWARE / "main" / "main.c").read_text()

        self.assertIn('"frame <on|off>', source)

    def test_the_reasons_the_fixture_boots_with_are_the_firmwares(self):
        source = (FIRMWARE / "main" / "main.c").read_text()

        self.assertIn(f'return "{fake_esp32.PANIC_REASON}";', source)
        self.assertIn(f'return "{fake_esp32.POWER_ON_REASON}";', source)
        self.assertIn('shell_line("utesp: boot reason %s", reset_reason());',
                      source)

    def test_the_frame_fits_in_the_room_the_line_buffer_leaves(self):
        source = (FIRMWARE / "main" / "main.c").read_text()
        frame_max = int(re.search(r"#define SHELL_FRAME_MAX (\d+)", source)[1])
        out_max = int(re.search(r"#define SHELL_OUT_MAX\s+(\d+)", source)[1])

        # "#<len>,<crc>:" at its widest, which is the longest line the
        # buffer can hold followed by four hex digits.
        self.assertLessEqual(len(f"#{out_max - 1},ffff:"), frame_max)


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
