"""esp32peer.py: the peer shell, the pair session, and the failures it reports."""

import io
import json
import subprocess
import sys
import tempfile
from unittest import mock
import unittest
from pathlib import Path

from ubertooth_harness import afhmap, esp32peer
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
        self.assertIn("initiator audio: on ev3, sent 200 received 200 frames",
                      report)
        self.assertIn("pair carried audio: ev3", report)
        # The acceptor's profile starts before the initiator pages it, and
        # both boards are told the type.
        self.assertEqual(["sco type ev3", "sco start"],
                         [line for line in acceptor.commands
                          if line.startswith("sco")])
        self.assertEqual(["sco type ev3", "sco start"],
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
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "sent 200 audio frames, fewer than the 500"):
            self._audio(min_frames=500)

    def test_a_capture_that_stopped_early_fails_the_run(self):
        with self.assertRaisesRegex(esp32peer.PeerError, "it stopped early"):
            self._audio(min_seconds=60.0, min_frames=10)

    def test_one_bridge_cannot_be_both_peers(self):
        acceptor_firmware, initiator_firmware = _audio_pair()
        acceptor, _initiator = _peers(acceptor_firmware, initiator_firmware)
        with self.assertRaisesRegex(esp32peer.PeerError, "the same bridge"):
            esp32peer.audio(acceptor, acceptor, TRUE, out=io.StringIO())


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
