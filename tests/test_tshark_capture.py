"""tsharkcapture.py: real tshark reads what the extcap plugin sends (#148).

The checks that need no tshark (counting rows, the command line, the usage
refusals) always run. The rest drive the real tshark that is installed, with
``tests/fake_extcap.py`` in place of the fork's plugin, so the whole path runs
with no Ubertooth and no DK: tshark installs the program, calls it as it calls
the plugin, reads its FIFO, and its own dissector decodes the records. CI
installs tshark on the Linux job for exactly this; where it is missing these
are skipped, as the cases are.

What a run on the bench adds is the radio and the fork's own plugin; what runs
here is the agreement between Wireshark's dissector and the record format the
harness expects, which is what the harness's own parser cannot show.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
import warnings
from unittest import mock

from tests import fake_extcap
from ubertooth_harness import advcapture, pytools, tshark_cases, tsharkcapture

ADDRESS = "C3:11:22:33:44:55"
OTHER = "D0:00:00:00:00:02"
SERIAL = "deadbeef"
INTERFACE = f"ubertooth{SERIAL}"
FAKE = str(Path(fake_extcap.__file__).resolve())
HAVE_TSHARK = tsharkcapture.version() is not None
# Long enough for dumpcap to hand tshark a packet, short enough for a suite.
SECONDS = 6.0


def pcap(records) -> bytes:
    out = fake_extcap.pcap_header()
    for index, body in enumerate(records):
        out += fake_extcap.pcap_record(body, 1_700_000_000 + index)
    return out


def capture_file(directory, records) -> str:
    path = Path(directory) / "capture.pcap"
    path.write_bytes(pcap(records))
    return str(path)


class RowTests(unittest.TestCase):
    def test_rows_are_counted_by_address_and_pdu_type(self):
        rows = tsharkcapture.Rows(ADDRESS, tsharkcapture.PDU_TYPES["nonconn"])
        rows.read(
            f"{ADDRESS.lower()}\t0x02\n"
            f"{ADDRESS.upper()}\t0x02\n"      # the same board, as tshark may print it
            f"{ADDRESS.lower()}\t0x00\n"      # the advertiser, another PDU type
            f"{OTHER.lower()}\t0x02\n"
            "\t\n"                            # a data packet: no advertising address
            "\n"                              # a packet with neither field
        )

        self.assertEqual(6, rows.rows)
        self.assertEqual(2, rows.advertiser)
        self.assertEqual(1, rows.wrong_pdu_type)
        self.assertEqual(1, rows.other_addresses)
        self.assertEqual(2, rows.no_address)
        self.assertEqual(
            rows.rows,
            rows.advertiser + rows.wrong_pdu_type + rows.other_addresses
            + rows.no_address)

    def test_the_summary_names_the_advertiser_only(self):
        rows = tsharkcapture.Rows(ADDRESS, tsharkcapture.PDU_TYPES["nonconn"])
        rows.read(f"{OTHER.lower()}\t0x02\n{ADDRESS.lower()}\t0x02\n")

        summary = rows.summary()

        # Another device's address must never reach the run record.
        self.assertNotIn(OTHER.lower(), summary)
        self.assertIn(ADDRESS.lower(), summary)
        self.assertIn("ADV_NONCONN_IND", summary)

    def test_the_command_asks_for_the_two_fields_and_a_duration(self):
        argv = tsharkcapture.tshark_argv(INTERFACE, 15)

        self.assertEqual(
            ("tshark", "-i", INTERFACE, "-a", "duration:15", "-l", "-T", "fields",
             "-E", "header=n", "-e", tsharkcapture.ADDRESS_FIELD,
             "-e", tsharkcapture.PDU_TYPE_FIELD), argv)
        self.assertIn("-f", tsharkcapture.tshark_argv(INTERFACE, 15, "btle.length > 20"))
        read = tsharkcapture.tshark_argv("", 0, read="/tmp/x.pcap")
        self.assertEqual(("tshark", "-r", "/tmp/x.pcap", "-T"), read[:4])
        self.assertNotIn("-i", read)


class RefusalTests(unittest.TestCase):
    def main(self, *argv):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            try:
                code = tsharkcapture.main(list(argv))
            except SystemExit as exit_code:  # argparse usage errors
                code = exit_code.code
        return code, out.getvalue()

    def test_without_tshark_the_case_is_skipped_not_failed(self):
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.dict(os.environ, {"PATH": empty}):
            code, out = self.main("capture", "--interface", INTERFACE,
                                  "--seconds", "1", "--address", ADDRESS)

        self.assertEqual(pytools.SKIP_EXIT, code)
        self.assertIn("SKIP: tshark is not installed", out)

    def usage_errors(self):
        for name, argv in (
            ("--interrupt-after at the end of the window",
             ("capture", "--interface", INTERFACE, "--seconds", "5", "--address",
              ADDRESS, "--interrupt-after", "5")),
            ("a negative packet count",
             ("read", "x.pcap", "--address", ADDRESS, "--min-packets", "-1")),
        ):
            with self.subTest(name):
                code, _out = self.main(*argv)
                self.assertEqual(2, code)

    def test_a_wrong_command_line_is_a_usage_error(self):
        self.usage_errors()

    def test_a_wrong_command_line_is_a_usage_error_without_tshark_too(self):
        # The arguments are checked before the host is looked at: a host with
        # no tshark must not turn a wrong command line into a skip.
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.dict(os.environ, {"PATH": empty}):
            self.usage_errors()


class BudgetTests(unittest.TestCase):
    def test_the_case_allows_more_time_than_the_driver_takes(self):
        # The driver must get to its own stop before the step's timeout kills
        # it: tshark runs in a session of its own, so a command killed from
        # outside leaves it behind with nothing to reap it.
        driver = (tsharkcapture.VERSION_TIMEOUT_S      # tshark --version
                  + tsharkcapture.VERSION_TIMEOUT_S    # tshark -G folders
                  + tsharkcapture.START_WAIT_S         # tshark -D
                  + tsharkcapture.EXIT_WAIT_S          # past the window
                  + tsharkcapture.STOP_WAIT_S          # past the SIGTERM
                  + tsharkcapture.GONE_WAIT_S)         # the leftover scan
        self.assertLess(driver, tshark_cases.CAPTURE_SLACK_S)

    def test_a_tshark_that_ignores_the_stop_is_reported_not_left_quietly(self):
        failures = []
        lines = []
        with mock.patch.multiple(tsharkcapture, EXIT_WAIT_S=0.5, STOP_WAIT_S=0.5), \
                tempfile.TemporaryDirectory() as directory, \
                warnings.catch_warnings():
            # The one left running on purpose is killed below; its Popen is
            # dropped first, which would warn about it.
            warnings.simplefilter("ignore", ResourceWarning)
            # Deaf to both signals, as a wedged tshark would be.
            argv = ("sh", "-c", "trap '' TERM INT; sleep 30")
            status, _out, _err, seen, _quiet = tsharkcapture._run_tshark(
                argv, dict(os.environ), 0.2, None, failures, lines.append,
                Path(directory))

        self.assertIsNone(status)
        self.assertTrue(any("was sent SIGTERM" in failure for failure in failures),
                        failures)
        # Its own process id is among what was left running, so the case fails
        # instead of the command ending with a tshark still going.
        self.assertTrue(any("sleep 30" in command for command in seen.values()), seen)
        for pid in seen:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


class IsolationTests(unittest.TestCase):
    def test_the_environment_moves_every_wireshark_directory_inside_the_home(self):
        home = Path("/tmp/tshark-home-test")
        env = tsharkcapture._isolated_env(home)

        for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                     "WIRESHARK_CONFIG_DIR"):
            self.assertTrue(env[name].startswith(str(home)), f"{name}={env[name]}")

    def test_a_directory_outside_the_home_is_refused_not_written_to(self):
        # The host user's own extcap directory must never have the plugin
        # written into it: when that is where tshark would look, there is no
        # way to install it for this capture alone.
        home = Path("/tmp/tshark-home-test")
        folders = "Personal Extcap path:\t/home/someone/.local/lib/wireshark/extcap\n"
        with mock.patch.object(tsharkcapture.subprocess, "run",
                               return_value=mock.Mock(stdout=folders)):
            self.assertIsNone(tsharkcapture._extcap_dir(home, {}))

        inside = f"Personal Extcap path:\t{home}/data/lib/wireshark/extcap\n"
        with mock.patch.object(tsharkcapture.subprocess, "run",
                               return_value=mock.Mock(stdout=inside)):
            self.assertEqual(Path(f"{home}/data/lib/wireshark/extcap"),
                             tsharkcapture._extcap_dir(home, {}))

    def test_a_tshark_that_cannot_be_asked_falls_back_inside_the_home(self):
        home = Path("/tmp/tshark-home-test")
        with mock.patch.object(tsharkcapture.subprocess, "run", side_effect=OSError):
            self.assertEqual(home / tsharkcapture.FALLBACK_EXTCAP_DIR,
                             tsharkcapture._extcap_dir(home, {}))


class FixtureTests(unittest.TestCase):
    """The fake plugin's records are the ones the harness itself expects."""

    def test_the_harness_parser_reads_the_fake_records(self):
        # parse_advertising returns nothing for a record whose CRC or shape is
        # wrong, so this also checks the fake's CRC against the harness's.
        packet = advcapture.parse_advertising(fake_extcap.record(ADDRESS, 7))

        self.assertIsNotNone(packet, "the harness parser rejected the fake's record")
        self.assertEqual(ADDRESS, packet.address)
        self.assertEqual(advcapture.PDU_TYPES["nonconn"], packet.pdu_type)
        self.assertEqual(0, packet.rf_channel)
        self.assertEqual((0x01, 7, fake_extcap.TAG),
                         advcapture.harness_payload(packet.adv_data))

    def test_the_fake_answers_the_calls_wireshark_makes(self):
        for argv, expected in (
            (["--extcap-interfaces"], f"interface {{value={INTERFACE}}}"),
            ([f"--extcap-interface={INTERFACE}", "--extcap-dlts"], "dlt {number=256}"),
            ([f"--extcap-interface={INTERFACE}", "--extcap-config"], "{value=37}"),
        ):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"FAKE_EXTCAP_SERIAL": SERIAL}), \
                    mock.patch("sys.stdout", out):
                code = fake_extcap.main(argv)
            self.assertEqual(0, code, argv)
            self.assertIn(expected, out.getvalue())


@unittest.skipUnless(HAVE_TSHARK, "tshark is not installed")
class ReadTests(unittest.TestCase):
    """What Wireshark's own dissector reads from the records."""

    def check(self, records, **options):
        lines = []
        with tempfile.TemporaryDirectory() as directory:
            code = tsharkcapture.read_file(
                capture_file(directory, records), ADDRESS, out=lines.append,
                **options)
        return code, "\n".join(lines)

    def test_wireshark_decodes_the_advertisers_address_and_pdu_type(self):
        records = [fake_extcap.record(ADDRESS, counter) for counter in (1, 2, 3)]

        code, out = self.check(records, min_packets=3)

        self.assertEqual(0, code, out)
        self.assertIn("tshark checks passed", out)
        self.assertIn(f"from {ADDRESS.lower()} as ADV_NONCONN_IND: 3", out)

    def test_another_devices_packets_do_not_count_and_are_not_printed(self):
        records = [fake_extcap.record(OTHER, 1), fake_extcap.record(ADDRESS, 2)]

        code, out = self.check(records, min_packets=2)

        self.assertEqual(1, code, out)
        self.assertIn(f"decoded 1 ADV_NONCONN_IND packet(s) from {ADDRESS.lower()}, "
                      "fewer than 2", out)
        self.assertNotIn(OTHER.lower(), out)
        self.assertIn("from other addresses: 1", out)


@unittest.skipUnless(HAVE_TSHARK, "tshark is not installed")
class CaptureTests(unittest.TestCase):
    """tshark drives the extcap program itself, as it drives the plugin."""

    def capture(self, **options):
        lines = []
        environment = {
            "FAKE_EXTCAP_SERIAL": SERIAL,
            "FAKE_EXTCAP_ADDRESS": ADDRESS,
            "FAKE_EXTCAP_PERIOD_S": "0.02",
        }
        environment.update(options.pop("environment", {}))
        with mock.patch.dict(os.environ, environment):
            code = tsharkcapture.capture(
                INTERFACE, options.pop("seconds", SECONDS), ADDRESS,
                program=tsharkcapture.Program(interpreter=sys.executable,
                                              script=Path(FAKE).read_bytes()),
                out=lines.append, **options)
        return code, "\n".join(lines)

    def test_tshark_captures_through_the_program_and_decodes_the_advertiser(self):
        code, out = self.capture()

        self.assertEqual(0, code, out)
        self.assertIn("tshark checks passed", out)
        self.assertIn(f"extcap program: {tsharkcapture.PROGRAM_NAME}", out)
        self.assertIn("processes left: none", out)

    def test_a_capture_filter_is_passed_on_and_its_warning_reaches_tshark(self):
        code, out = self.capture(capture_filter="btle.length > 20")

        self.assertEqual(0, code, out)
        self.assertIn("applies no capture filter", out)
        self.assertIn("tshark checks passed", out)

    def test_a_plugin_that_says_nothing_about_the_filter_fails_the_check(self):
        # The fake answers --capture without the warning when the plugin is
        # not told the filter: the check must notice.
        code, out = self.capture(capture_filter="btle.length > 20",
                                 environment={"FAKE_EXTCAP_QUIET_FILTER": "1"})

        self.assertEqual(1, code, out)
        self.assertIn("did not reach tshark", out)

    def test_sigint_partway_ends_the_capture_cleanly(self):
        code, out = self.capture(interrupt_after=SECONDS / 2)

        self.assertEqual(0, code, out)
        self.assertIn("stopping tshark with SIGINT", out)
        self.assertIn("tshark exit: 0", out)
        self.assertIn("tshark checks passed", out)

    def test_a_sigint_case_asking_for_a_packet_is_skipped_when_none_arrives(self):
        # The bench cases ask for at least one packet, so the skip has to
        # win over the count: a capture window with nothing in it says
        # nothing about what a SIGINT does, and is not a shortfall.
        code, out = self.capture(seconds=4.0, interrupt_after=1.0, min_packets=1,
                                 environment={"FAKE_EXTCAP_SILENT": "1"})

        self.assertEqual(pytools.SKIP_EXIT, code, out)
        self.assertNotIn("fewer than", out)

    def test_a_sigint_with_nothing_decoded_to_stop_after_is_skipped(self):
        # The SIGINT goes in once a row has arrived, which is what makes the
        # check deterministic: tshark hands the capture to a dumpcap child,
        # and a signal that lands before that child is capturing ends the run
        # as a child that died. With no traffic there is nothing to stop
        # after, so the check is not made rather than guessed at.
        code, out = self.capture(seconds=4.0, interrupt_after=1.0,
                                 min_packets=0,
                                 environment={"FAKE_EXTCAP_SILENT": "1"})

        self.assertEqual(pytools.SKIP_EXIT, code, out)
        self.assertIn(f"SKIP: {tsharkcapture.NO_ROWS}", out)
        self.assertNotIn("stopping tshark with SIGINT", out)

    def test_the_program_this_installed_is_the_one_that_captured(self):
        code, out = self.capture()

        self.assertEqual(0, code, out)
        # Recorded by the installed program itself, so another extcap program
        # on the host answering for the interface could not pass this.
        capture_calls = [line for line in out.splitlines()
                         if line.startswith("the installed program ran: --capture")]
        self.assertEqual(1, len(capture_calls), out)
        # Which form the arguments take is Wireshark's business (4.6 sends
        # --extcap-interface and its value as two words), so only the
        # interface and the FIFO are checked.
        self.assertIn(INTERFACE, capture_calls[0])
        self.assertIn("--fifo", capture_calls[0])

    def test_a_plugin_that_refuses_the_capture_fails_the_check(self):
        code, out = self.capture(
            seconds=3.0,
            environment={"FAKE_EXTCAP_REFUSE": "no Ubertooth found with serial "
                                                "matching 'deadbeef'"})

        self.assertEqual(1, code, out)
        self.assertIn("FAIL:", out)

    def test_an_interface_the_program_does_not_offer_is_reported(self):
        lines = []
        with mock.patch.dict(os.environ, {"FAKE_EXTCAP_SERIAL": "0badc0de"}):
            code = tsharkcapture.capture(
                INTERFACE, 3.0, ADDRESS,
                program=tsharkcapture.Program(interpreter=sys.executable,
                                              script=Path(FAKE).read_bytes()),
                out=lines.append)

        out = "\n".join(lines)
        self.assertEqual(1, code, out)
        self.assertIn(f"tshark -D does not list {INTERFACE}", out)
