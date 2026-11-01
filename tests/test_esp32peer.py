"""esp32peer.py: the peer shell, the pair session, and the failures it reports."""

import io
import json
import sys
import unittest

from ubertooth_harness import esp32peer
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


def _audio_pair(acceptor_class=FakePeerFirmware, initiator_class=FakePeerFirmware,
                **options):
    """A pair whose images have the audio link, which a later image added."""
    version = esp32peer.MINIMUM_AUDIO_FIRMWARE
    acceptor = acceptor_class(ACCEPTOR_ADDRESS, version=version, **options)
    initiator = initiator_class(INITIATOR_ADDRESS, version=version, **options)
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


class AudioTests(unittest.TestCase):
    """The eSCO session: a voice link of a chosen packet type, and its counters."""

    def _audio(self, command=TRUE, pair=None, **options):
        acceptor_firmware, initiator_firmware = pair or _audio_pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        report = io.StringIO()
        status = esp32peer.audio(acceptor, initiator, command, out=report,
                                settle_s=0.0, **options)
        return status, report.getvalue(), acceptor_firmware, initiator_firmware

    def test_the_link_comes_up_the_command_runs_and_both_boards_are_released(self):
        status, report, acceptor, initiator = self._audio(min_frames=10,
                                                          min_packets=10)
        self.assertEqual(0, status)
        self.assertIn("central LAP: e9c5ba", report)
        self.assertIn("central UAP: 0d", report)
        self.assertIn(f"audio link: up to {ACCEPTOR_ADDRESS}", report)
        self.assertIn("command exit: 0", report)
        self.assertIn("pair carried audio: yes", report)
        self.assertIn("initiator audio frames: sent 50 refused 0 received 50 "
                      "damaged 0", report)
        self.assertIn("acceptor audio packets: sent 50 discarded 0 received 50 "
                      "good 50 errors 0 missing 0 lost 0", report)
        # The mask each controller supports, so a refused type can be read
        # rather than puzzled over.
        self.assertIn("audio types supported: initiator 003d, acceptor 003d",
                      report)
        # The initiator runs the gateway, which pages, so it is the central.
        self.assertIn("role initiator", initiator.commands)
        self.assertIn(f"spp peer {ACCEPTOR_ADDRESS}", initiator.commands)
        self.assertIn("sco start", acceptor.commands)
        # Both left in the firmware's boot state: one reset for the open, one
        # for the release.
        self.assertEqual(2, acceptor.resets)
        self.assertEqual(2, initiator.resets)
        self.assertEqual("off", acceptor.sco)
        self.assertEqual("none", initiator.sco_profile)

    def test_the_stacks_own_choice_is_a_2_ev3_link(self):
        _status, report, _acceptor, initiator = self._audio()
        self.assertIn("audio type asked for: default", report)
        self.assertIn("audio link: esco 2-ev3 codec cvsd", report)
        self.assertIn("audio packets: 60 bytes every 12 slots (7.50 ms), "
                      "retransmission window 2 slots", report)
        self.assertIn("audio frames: 120 bytes over HCI", report)
        # Nothing was asked for, so nothing was set.
        self.assertNotIn("sco type default", initiator.commands)

    def test_ev3_is_asked_for_on_both_boards_before_either_profile_starts(self):
        _status, report, acceptor, initiator = self._audio(sco_type="ev3")
        self.assertIn("audio type asked for: ev3", report)
        self.assertIn("audio link: esco ev3 codec cvsd", report)
        self.assertIn("audio packets: 30 bytes every 6 slots (3.75 ms), "
                      "retransmission window 4 slots", report)
        # The accepting board's choice reaches its controller when the far end
        # pages, so it has to be set before any profile is running.
        for firmware in (acceptor, initiator):
            self.assertLess(firmware.commands.index("sco type ev3"),
                            firmware.commands.index("sco start"))
        # `sco type` is refused while a profile is running, and `role` stops
        # the profile, so the role goes before the choice.
        self.assertLess(initiator.commands.index("role initiator"),
                        initiator.commands.index("sco type ev3"))

    def test_a_type_only_one_board_asked_for_does_not_open_a_link(self):
        # The link uses a type both boards allowed, and `default` does not
        # allow plain EV3, so this is the failure a one-sided choice gives.
        acceptor, initiator = _audio_pair()
        acceptor.refuse["sco type ev3"] = "test: this board kept the default"
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "'sco type ev3' refused"):
            self._audio(pair=(acceptor, initiator), sco_type="ev3")

    def test_a_link_of_another_type_than_the_one_asked_for_fails(self):
        class Stubborn(FakePeerFirmware):
            def _sco_up(self, kind):  # the choice did not reach the air
                super()._sco_up("2ev3")

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            r"the initiator reports an audio link using 2-ev3, not the ev3 "
            r"both boards asked for \(60 bytes every 12 slots\)"
        ):
            self._audio(pair=_audio_pair(initiator_class=Stubborn),
                        sco_type="ev3")

    def test_the_type_is_checked_on_the_board_that_answered_the_page_too(self):
        # Each board works the type out from its own allowed list and the
        # packet length its controller reported, so both are read.
        class Confused(FakePeerFirmware):
            def _sco_up(self, kind):
                super()._sco_up(kind)
                self.sco_pkt_type = "2-ev3"

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "the acceptor reports an audio link using 2-ev3, not the ev3"
        ):
            self._audio(pair=_audio_pair(acceptor_class=Confused),
                        sco_type="ev3")

    def test_a_link_that_is_not_esco_fails(self):
        # An EV3 or 2-EV3 packet type is an eSCO one, and everything this
        # session is called says eSCO, so a plain SCO link is not a pass.
        class Plain(FakePeerFirmware):
            def _sco_up(self, kind):
                super()._sco_up(kind)
                self.sco_link = "sco"

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "the initiator reports a sco link, not esco"
        ):
            self._audio(pair=_audio_pair(initiator_class=Plain))

    def test_a_link_that_dropped_during_the_command_is_reported_with_the_counts(self):
        # The firmware refuses `sco stats` with no link up, so a run that
        # lost its link must not end on that refusal: the frame counters and
        # the message written for a dropped link are what the reader needs.
        class Dropping(FakePeerFirmware):
            reads = 0

            def _audio_status(self):
                self.reads += 1
                if self.reads > 2:  # after the introduction and the link check
                    self._sco_stop()
                return super()._audio_status()

        acceptor_firmware, initiator_firmware = _audio_pair(
            initiator_class=Dropping)
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        report = io.StringIO()
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "the initiator reports the audio link off, not up, after the "
            "command ran"
        ):
            esp32peer.audio(acceptor, initiator, TRUE, out=report, settle_s=0.0)
        written = report.getvalue()
        self.assertIn("initiator audio frames: sent 50", written)
        self.assertIn("initiator audio packets: not counted", written)
        self.assertIn("no audio link to count packets on", written)

    def test_counters_refused_for_the_rest_of_the_boot_do_not_end_the_session(self):
        # The firmware refuses them for the rest of a boot once a request has
        # gone unanswered. That is worth saying, not worth abandoning a run
        # whose frames both boards counted.
        class Deaf(FakePeerFirmware):
            def boot(self):
                lines = super().boot()
                self.stats_dead = True
                return lines

        status, report, _acceptor, _initiator = self._audio(
            pair=_audio_pair(initiator_class=Deaf), min_frames=10,
            min_packets=0)
        self.assertEqual(0, status)
        self.assertIn("initiator audio packets: not counted", report)
        self.assertIn("went unanswered", report)
        self.assertIn("pair carried audio: yes", report)

    def test_a_packet_type_that_is_not_one_of_the_three_is_refused(self):
        for value in ("", "EV4", "3ev3", "ev3 2ev3"):
            with self.subTest(value), self.assertRaisesRegex(
                esp32peer.PeerError, "audio packet type must be one of"
            ):
                esp32peer.audio(*_peers(*_audio_pair()), TRUE, sco_type=value,
                                out=io.StringIO())

    def test_an_image_without_the_audio_link_is_told_what_it_needs(self):
        acceptor, initiator = _audio_pair()
        initiator.version = "utesp-1.3.0"
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "initiator runs utesp-1.3.0, and the harness needs utesp-1.4.0"
        ):
            self._audio(pair=(acceptor, initiator))
        # It failed before anything was started on either board.
        self.assertNotIn("sco start", acceptor.commands)

    def test_an_image_whose_status_lacks_an_audio_field_names_it(self):
        class Partial(FakePeerFirmware):
            def _audio_status(self):
                status = super()._audio_status()
                del status["sco_interval"]
                return status

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "acceptor: status has no valid 'sco_interval', so this image "
            "cannot be driven through an audio link"
        ):
            self._audio(pair=_audio_pair(acceptor_class=Partial))

    def test_an_acceptor_that_is_not_listening_is_reported(self):
        acceptor, initiator = _audio_pair()
        acceptor.refuse["sco start"] = "audio link unavailable: test"
        with self.assertRaisesRegex(esp32peer.PeerError, "'sco start' refused"):
            self._audio(pair=(acceptor, initiator))
        # The initiator is still left in its boot state.
        self.assertEqual(esp32peer.BOOT_ROLE, initiator.role)
        self.assertEqual(2, initiator.resets)

    def test_a_link_that_did_not_come_up_is_reported(self):
        acceptor = FakePeerFirmware(ACCEPTOR_ADDRESS,
                                    version=esp32peer.MINIMUM_AUDIO_FIRMWARE)
        initiator = FakePeerFirmware(INITIATOR_ADDRESS,
                                     version=esp32peer.MINIMUM_AUDIO_FIRMWARE)
        # Not linked to each other, so the page gets no answer.
        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "refused: service level connect timed out"):
            self._audio(pair=(acceptor, initiator))

    def test_an_acceptor_whose_stack_is_a_moment_behind_is_waited_for(self):
        # `sco start` answers on the board that pages; the other board learns
        # of the link from a callback of its own, which can land later.
        class Late(FakePeerFirmware):
            lagging = True

            def _audio_status(self):
                status = super()._audio_status()
                if status["sco"] == "on" and self.lagging:
                    self.lagging = False
                    status["sco"] = "slc"  # the link, not reported yet
                return status

        status, report, _acceptor, _initiator = self._audio(
            pair=_audio_pair(acceptor_class=Late), min_frames=10)
        self.assertEqual(0, status)
        self.assertIn("pair carried audio: yes", report)

    def test_an_acceptor_that_never_reports_the_link_is_named(self):
        class Deaf(FakePeerFirmware):
            def _audio_status(self):
                status = super()._audio_status()
                status["sco"] = "slc"  # the link is never reported
                return status

        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "the acceptor reports sco slc"
        ):
            self._audio(pair=_audio_pair(acceptor_class=Deaf),
                        link_timeout=0.0)

    def test_a_link_that_reports_up_with_no_handle_is_caught(self):
        class Handleless(FakePeerFirmware):
            def _sco_up(self, kind):
                super()._sco_up(kind)
                self.sco_handle = 0

        with self.assertRaisesRegex(esp32peer.PeerError,
                                    "audio link up with no connection handle"):
            self._audio(pair=_audio_pair(initiator_class=Handleless))

    def test_a_link_that_carried_too_few_frames_fails(self):
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "the acceptor received 50 frames, fewer than the 500 this test needs"
        ):
            self._audio(min_frames=500)

    def test_a_controller_that_sent_too_few_packets_fails(self):
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "controller sent 50 packets, fewer than the 5000 this test needs"
        ):
            self._audio(min_packets=5000)

    def test_a_command_that_stopped_early_fails(self):
        with self.assertRaisesRegex(esp32peer.PeerError, "it stopped early"):
            self._audio(min_seconds=30)

    def test_the_counters_are_read_while_the_link_is_still_up(self):
        # The controller's counters belong to a live link, and `sco stop` ends
        # it, so a run whose numbers were read afterwards would read zero.
        _status, _report, _acceptor, initiator = self._audio(min_frames=10)
        self.assertLess(initiator.commands.index("sco stats"),
                        initiator.commands.index("sco stop"))

    def test_counters_that_are_not_json_are_reported(self):
        for line, reason in (("not json", "'sco stats' is not JSON"),
                             ("[1, 2]", "'sco stats' is not a JSON object"),
                             ('{"tx_total": 4}',
                              "'sco stats' has no valid 'rx_total'")):
            with self.subTest(line), self.assertRaisesRegex(
                esp32peer.PeerError, reason
            ):
                esp32peer.parse_sco_stats(line, "acceptor")

    def test_a_reset_that_left_the_audio_profile_running_is_reported(self):
        # A board whose profile is still running refuses the next case's
        # `role` and `defaults`, so a release has to leave it away.
        class Stale(FakePeerFirmware):
            def boot(self):
                lines = super().boot()
                self.sco_profile = "ag"
                self.sco = "ready"
                return lines

        acceptor, initiator = _audio_pair(acceptor_class=Stale)
        peer, _ = _peers(acceptor, initiator)
        with self.assertRaisesRegex(
            esp32peer.PeerError,
            "not in its boot state after a reset: sco 'ready', not 'off', "
            "sco_profile 'ag', not 'none'"
        ):
            esp32peer.reset(peer, io.StringIO())

    def test_the_commands_output_is_not_touched(self):
        script = "import sys; sys.stdout.write('systime=1 ch= 1\\n')"
        acceptor_firmware, initiator_firmware = _audio_pair()
        acceptor, initiator = _peers(acceptor_firmware, initiator_firmware)
        report = io.StringIO()
        with _captured_stdout() as captured:
            esp32peer.audio(acceptor, initiator, (sys.executable, "-c", script),
                            out=report, settle_s=0.0)
            written = captured()
        self.assertEqual("systime=1 ch= 1\n", written)
        self.assertNotIn("audio link", written)
        self.assertIn("audio link: esco", report.getvalue())


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

    def test_audio_without_a_command_is_a_usage_error(self):
        with _captured_stderr() as captured:
            with self.assertRaises(SystemExit) as raised:
                esp32peer.main(["audio", "--acceptor", ACCEPTOR,
                                "--initiator", INITIATOR])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("audio needs the command to run after --", captured())

    def test_a_packet_type_the_firmware_has_no_name_for_is_a_usage_error(self):
        with _captured_stderr() as captured:
            with self.assertRaises(SystemExit) as raised:
                esp32peer.main(["audio", "--acceptor", ACCEPTOR,
                                "--initiator", INITIATOR, "--type", "ev4",
                                "--", "true"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--type", captured())

    def test_audio_reaches_the_boards_with_the_commands_own_options_intact(self):
        # -U and -t belong to ubertooth-rx; a missing bridge then ends it with
        # an error, not a usage error.
        with _captured_stderr() as captured:
            code = esp32peer.main([
                "audio", "--acceptor", "/dev/absent-a",
                "--initiator", "/dev/absent-b", "--type", "ev3",
                "--min-frames", "100", "--min-packets", "100", "--",
                "ubertooth-rx", "-U", "f" * 32, "-t", "5"])
        self.assertEqual(1, code)
        self.assertIn("error: /dev/absent-a does not exist", captured())

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
