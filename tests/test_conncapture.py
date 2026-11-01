import contextlib
import io
from pathlib import Path
import struct
import tempfile
import unittest

from ubertooth_harness import advcapture, conncapture
from tests.test_advcapture import pcap, pcapng

CENTRAL = "C0:7D:CD:95:52:86"
PERIPHERAL = "C1:92:EC:E7:71:BC"
OTHER = "D0:00:00:00:00:02"
ACCESS = 0x50655DAB
CRC_INIT = 0x6A2B3C
INTERVAL_UNITS = 24   # 30 ms
TIMEOUT_UNITS = 400   # 4 s
ALL_CHANNELS = (1 << 37) - 1


def lldata(access=ACCESS, crc_init=CRC_INIT, window_size=3, window_offset=1,
           interval=INTERVAL_UNITS, latency=0, timeout=TIMEOUT_UNITS,
           channel_map=ALL_CHANNELS, hop=11, sca=5):
    return (struct.pack("<I", access)
            + crc_init.to_bytes(3, "little")
            + bytes([window_size])
            + struct.pack("<HHHH", window_offset, interval, latency, timeout)
            + channel_map.to_bytes(5, "little")
            + bytes([hop | sca << 5]))


def _address(text):
    return bytes(reversed(bytes.fromhex(text.replace(":", ""))))


def connect_ind(initiator=CENTRAL, advertiser=PERIPHERAL, data=None, corrupt=False):
    pdu = _address(initiator) + _address(advertiser) + (lldata() if data is None else data)
    header = bytes([0x5 | 1 << 6 | 1 << 7, len(pdu)])
    crc = advcapture.crc24(header + pdu).to_bytes(3, "little")
    if corrupt:
        pdu = pdu[:-1] + bytes([pdu[-1] ^ 1])
    phdr = bytes([4, 0xC0, 0x9C, 0]) + struct.pack("<I", advcapture.ADV_ACCESS_ADDRESS) + b"\x00\x00"
    return (phdr + struct.pack("<I", advcapture.ADV_ACCESS_ADDRESS) + header + pdu
            + crc)


def data_packet(length=251, access=ACCESS, crc_init=CRC_INIT, corrupt=False):
    header = bytes([0x02, length])
    pdu = bytes(range(length % 251)) if length < 251 else bytes(251)
    pdu = (pdu + bytes(length))[:length]
    crc = advcapture.crc24(header + pdu, crc_init).to_bytes(3, "little")
    if corrupt:
        crc = bytes([crc[0] ^ 1]) + crc[1:]
    phdr = bytes([9, 0xC0, 0x9C, 0]) + struct.pack("<I", access) + b"\x00\x00"
    return phdr + struct.pack("<I", access) + header + pdu + crc


def connect_ind_body(record):
    """The CONNECT_IND record from its access address on, as libbtbb sees it."""
    return record[advcapture.LE_PHDR_LEN:advcapture.LE_PHDR_LEN + 6 + 34]


def pcapng_with_option(records, *option_pdus, value_length=8 + 34,
                       code=conncapture.CONNECTION_INFO_OPTION):
    """PCAPNG whose interface block carries libbtbb's connection info options.

    The option length is the length of its value, as libbtbb writes it: the
    8-byte timestamp and the 34 PDU bytes.
    """
    option = b""
    for option_pdu in option_pdus:
        option += (struct.pack("<HH", code, value_length) + struct.pack("<Q", 7)
                   + option_pdu)
        option += b"\x00" * (-len(option) % 4)
    body = struct.pack("<HHI", 256, 0, 65535) + option
    body += b"\x00" * (-len(body) % 4)
    out = _block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    out += _block(1, body)
    for item in records:
        out += _block(6, struct.pack("<IIIII", 0, 0, 1, len(item), len(item)) + item)
    return out


def _block(block_type, body):
    body += b"\x00" * (-len(body) % 4)
    length = len(body) + 12
    return struct.pack("<II", block_type, length) + body + struct.pack("<I", length)


def connect_inds(records):
    """Every CONNECT_IND in the records, as check() reads them: only the
    packets whose CRC is right, so a corrupted one cannot pass for the
    connection the harness asked for."""
    timed = [advcapture.TimedRecord(None, record) for record in records]
    return [connection for _at, connection in conncapture.timed_connect_inds(timed)]


class ConnectIndTests(unittest.TestCase):
    def test_fields_are_read_from_the_pdu(self):
        found = connect_inds([connect_ind()])
        self.assertEqual(1, len(found))
        self.assertEqual(
            (CENTRAL, PERIPHERAL, ACCESS, CRC_INIT, INTERVAL_UNITS, 0, TIMEOUT_UNITS,
             ALL_CHANNELS, 11, 5),
            (found[0].initiator, found[0].advertiser, found[0].access_address,
             found[0].crc_init, found[0].interval, found[0].latency,
             found[0].timeout, found[0].channel_map, found[0].hop,
             found[0].sleep_clock_accuracy),
        )

    def test_other_packets_are_not_connect_inds(self):
        self.assertEqual([], connect_inds([data_packet()]))
        self.assertEqual([], connect_inds([b"short"]))

    def test_a_corrupted_connect_ind_is_not_read(self):
        # Its parameters are what the checks compare, so a flipped bit in
        # them must not pass for the connection that was asked for.
        self.assertEqual([], connect_inds([connect_ind(corrupt=True)]))


class CheckTests(unittest.TestCase):
    def check(self, data, **options):
        options.setdefault("central", CENTRAL)
        options.setdefault("peripheral", PERIPHERAL)
        options.setdefault("interval_units", INTERVAL_UNITS)
        options.setdefault("latency", 0)
        options.setdefault("timeout_units", TIMEOUT_UNITS)
        return conncapture.check(data, **options)

    def records(self):
        return [connect_ind()] + [data_packet(251)] * 3 + [data_packet(27)] * 2

    def test_a_followed_connection_passes_in_pcap_and_pcapng(self):
        for data in (pcap(self.records()), pcapng(self.records())):
            header, result = self.check(data, min_data_packets=5, long_packets=3)
            self.assertEqual([], result.problems, header)
            self.assertEqual((1, 5, 5, 3, 251),
                             (result.connect_inds, result.data_packets,
                              result.valid_packets, result.long_packets,
                              result.longest))

    def test_each_wrong_connect_ind_parameter_is_named(self):
        _header, result = self.check(
            pcap([connect_ind(data=lldata(interval=48, latency=2, timeout=200,
                                          hop=20, channel_map=0x1F))]))
        text = "\n".join(result.problems)
        for reason in ("connection interval 48, not 24", "peripheral latency 2, not 0",
                       "supervision timeout 200, not 400", "hop increment 20, not 5-16",
                       "channel map"):
            self.assertIn(reason, text)

    def test_a_connection_between_other_devices_is_not_ours(self):
        _header, result = self.check(pcap([connect_ind(initiator=OTHER)]))
        self.assertEqual([f"no CONNECT_IND from {CENTRAL} to {PERIPHERAL}"],
                         result.problems)

    def test_several_connections_in_one_capture_all_count(self):
        # The pair remakes the link, so each connection has its own access
        # address; packets from both are counted and both are checked.
        other = 0x11223344
        records = ([connect_ind()] + [data_packet(251)] * 2
                   + [connect_ind(data=lldata(access=other, crc_init=0x123456))]
                   + [data_packet(251, access=other, crc_init=0x123456)] * 2)
        _header, result = self.check(pcap(records), min_data_packets=4,
                                     min_valid_packets=4,
                                     long_packets=4)
        self.assertEqual([], result.problems)
        self.assertEqual((2, 4), (result.connect_inds, result.valid_packets))

    def test_one_connection_announced_twice_counts_its_packets_once(self):
        """A second CONNECT_IND for an access address already in the file
        brings no traffic with it, so counting its packets again would raise
        the floors without a packet arriving."""
        records = [connect_ind()] + [data_packet(251)] * 3 + [connect_ind()]
        _header, result = self.check(pcap(records), min_data_packets=3,
                                     min_valid_packets=3)
        self.assertEqual([], result.problems)
        self.assertEqual((2, 3), (result.connect_inds, result.data_packets))

    def test_a_wrong_parameter_in_a_later_connection_fails(self):
        records = [connect_ind(), connect_ind(data=lldata(interval=48))]
        _header, result = self.check(pcap(records))
        self.assertIn("connection interval 48, not 24", "\n".join(result.problems))

    def test_corrupt_data_packets_do_not_count_as_long(self):
        records = [connect_ind()] + [data_packet(251, corrupt=True)] * 3
        _header, result = self.check(pcap(records), min_data_packets=1,
                                     min_valid_packets=1, long_packets=1)
        self.assertEqual((3, 0), (result.data_packets, result.valid_packets))
        text = "\n".join(result.problems)
        self.assertIn("0 packet(s) with a correct CRC, fewer than 1 "
                      "(of 3 on the access address)", text)
        self.assertIn("0 packet(s) of 28 bytes or more, fewer than 1", text)

    def test_packets_of_another_connection_are_not_counted(self):
        records = [connect_ind(), data_packet(251, access=0x11223344)]
        _header, result = self.check(pcap(records), min_data_packets=1)
        self.assertIn("0 packet(s) on the connection's access address",
                      result.problems[0])

    def test_short_packets_alone_fail_the_long_packet_floor(self):
        records = [connect_ind()] + [data_packet(27)] * 5
        _header, result = self.check(pcap(records), min_data_packets=5, long_packets=1)
        self.assertEqual(["0 packet(s) of 28 bytes or more, fewer than 1 (longest 27)"],
                         result.problems)

    def test_bad_files(self):
        _header, result = self.check(pcap([connect_ind()], link_type=192))
        self.assertEqual(["link type 192, not 256"], result.problems)
        _header, result = self.check(b"")
        self.assertIn("empty", result.problems[0])


SECOND_ACCESS = 0x11223344
SECOND_CRC_INIT = 0x123456


def second_connect_ind():
    return connect_ind(data=lldata(access=SECOND_ACCESS, crc_init=SECOND_CRC_INIT))


def advertising_record():
    """What a board records once it is hunting again: an advertising packet."""
    pdu = _address(OTHER) + bytes([2, 0x01, 0x06])
    header = bytes([0x0 | 1 << 6, len(pdu)])
    crc = advcapture.crc24(header + pdu).to_bytes(3, "little")
    phdr = (bytes([0, 0xC0, 0x9C, 0])
            + struct.pack("<I", advcapture.ADV_ACCESS_ADDRESS) + b"\x00\x00")
    return (phdr + struct.pack("<I", advcapture.ADV_ACCESS_ADDRESS) + header
            + pdu + crc)


class RepeatedConnectionTests(unittest.TestCase):
    """A capture across several of the pair's connections: each one's own
    packets, when they arrived, and nothing the file does not announce."""

    def check(self, data, **options):
        options.setdefault("central", CENTRAL)
        options.setdefault("peripheral", PERIPHERAL)
        options.setdefault("interval_units", INTERVAL_UNITS)
        options.setdefault("latency", 0)
        options.setdefault("timeout_units", TIMEOUT_UNITS)
        return conncapture.check(data, **options)

    def two_cycles(self):
        """Two connections, each with packets up to 4 s after its CONNECT_IND."""
        records = [connect_ind(), data_packet(251), data_packet(251),
                   second_connect_ind(),
                   data_packet(251, access=SECOND_ACCESS, crc_init=SECOND_CRC_INIT),
                   data_packet(251, access=SECOND_ACCESS, crc_init=SECOND_CRC_INIT)]
        times = [0, 1_000_000, 4_000_000,
                 5_200_000, 6_200_000, 9_200_000]
        return records, times

    def cycles(self, **options):
        records, times = self.two_cycles()
        return self.check(pcapng(records, times=times, tsresol=9), **options)

    def test_two_connections_with_late_packets_pass(self):
        _header, result = self.cycles(min_connections=2, min_connection_packets=2,
                                      after_update_ms=2000, require_announced=True)
        self.assertEqual([], result.problems)
        self.assertEqual([ACCESS, SECOND_ACCESS],
                         [item.access_address for item in result.connections])
        self.assertEqual([4000, 4000],
                         [item.last_valid_after_ms for item in result.connections])
        self.assertEqual(9200, result.span_ms)

    def test_one_connection_does_not_meet_the_floor(self):
        records = [connect_ind()] + [data_packet(251)] * 3
        _header, result = self.check(
            pcapng(records, times=[0, 1_000, 2_000, 3_000_000], tsresol=9),
            min_connections=2)
        self.assertEqual(
            [f"1 connection(s) from {CENTRAL} to {PERIPHERAL}, fewer than 2"],
            result.problems)

    def test_a_connection_with_too_few_packets_is_named(self):
        records = [connect_ind(), data_packet(251), second_connect_ind()]
        _header, result = self.check(
            pcapng(records, times=[0, 3_000_000, 5_200_000], tsresol=9),
            min_connections=2, min_connection_packets=2)
        # Each connection that falls short is named, not just the first.
        self.assertEqual(
            [f"connection {ACCESS:08x} carries 1 packet(s), fewer than 2",
             f"connection {SECOND_ACCESS:08x} carries 0 packet(s), fewer than 2"],
            result.problems)

    def test_packets_only_at_the_start_of_a_connection_fail_the_update_floor(self):
        """A follow that stopped at the parameter update looks like this."""
        records = [connect_ind()] + [data_packet(251)] * 2 + [second_connect_ind()]
        _header, result = self.check(
            pcapng(records, times=[0, 100_000, 900_000, 5_200_000], tsresol=9),
            after_update_ms=2000)
        self.assertEqual(1, len(result.problems))
        self.assertIn("surviving the parameter update", result.problems[0])
        self.assertIn("the latest was 900 ms", result.problems[0])

    def test_every_connection_that_ended_inside_the_capture_is_asked(self):
        """One connection's late packet does not excuse another's missing
        one: a follow that died on the second connection's update would
        otherwise pass on the first connection's evidence."""
        records = [connect_ind(), data_packet(251),
                   second_connect_ind(),
                   data_packet(251, access=SECOND_ACCESS, crc_init=SECOND_CRC_INIT),
                   connect_ind(data=lldata(access=0x77665544, crc_init=0x223344))]
        times = [0, 4_000_000, 5_200_000, 5_600_000, 10_400_000]
        _header, result = self.check(
            pcapng(records, times=times, tsresol=9), after_update_ms=2000)
        self.assertEqual(
            [f"connection {SECOND_ACCESS:08x} has no packet with a correct CRC "
             "2000 ms or more after its own CONNECT_IND, so the capture does "
             "not show the follow surviving the parameter update (the latest "
             "was 400 ms)"],
            result.problems)

    def test_the_last_connection_in_the_file_is_not_asked(self):
        """The capture window is what ends it, not the follow."""
        records = [connect_ind(), data_packet(251), second_connect_ind(),
                   data_packet(251, access=SECOND_ACCESS, crc_init=SECOND_CRC_INIT)]
        _header, result = self.check(
            pcapng(records, times=[0, 4_000_000, 5_200_000, 5_600_000],
                   tsresol=9),
            after_update_ms=2000)
        self.assertEqual([], result.problems)

    def test_a_follow_that_stopped_before_the_capture_did_fails(self):
        """What keeps the exemption above honest. A board whose follow died
        goes back to hunting and keeps recording advertising packets, so a
        long run of records after the pair's last packet is the follow
        stopping, not the window ending."""
        records = [connect_ind(), data_packet(251), second_connect_ind(),
                   data_packet(251, access=SECOND_ACCESS, crc_init=SECOND_CRC_INIT),
                   advertising_record(), advertising_record()]
        times = [0, 4_000_000, 5_200_000, 6_400_000, 12_000_000, 18_000_000]
        data = pcapng(records, times=times, tsresol=9)
        _header, result = self.check(data, after_update_ms=2000,
                                     capture_ms=18000, max_quiet_ms=5000)
        self.assertEqual(11600, result.quiet_ms)
        self.assertEqual(
            ["the capture went on recording for 11600 ms after the last packet "
             "with a correct CRC of the pair's connections, more than 5000 ms, "
             "so the follow stopped before the capture did"],
            result.problems)

    def test_a_capture_that_stopped_recording_early_fails(self):
        """The other half: a capture whose records stop long before its
        window does heard nothing at all, our connections included."""
        records, times = self.two_cycles()
        _header, result = self.check(
            pcapng(records, times=times, tsresol=9), min_connections=2,
            min_connection_packets=2, after_update_ms=2000,
            capture_ms=18000, max_quiet_ms=5000)
        self.assertEqual(0, result.quiet_ms)
        self.assertEqual(
            ["the capture holds 9200 ms of records of a 18000 ms window, so it "
             "stopped recording more than 5000 ms before the window ran out"],
            result.problems)

    def test_a_capture_recorded_to_the_end_of_its_window_passes(self):
        records, times = self.two_cycles()
        _header, result = self.check(
            pcapng(records, times=times, tsresol=9), min_connections=2,
            min_connection_packets=2, after_update_ms=2000,
            capture_ms=10000, max_quiet_ms=5000)
        self.assertEqual([], result.problems)
        self.assertEqual(0, result.quiet_ms)

    def test_a_late_first_record_does_not_fail_a_good_capture(self):
        """The window's start is not in the file: the first record arrives
        some unknown time after the command did, and that cannot be read as
        the capture having stopped early."""
        records, times = self.two_cycles()
        times = [when + 4_000_000 for when in times]
        _header, result = self.check(
            pcapng(records, times=times, tsresol=9), min_connections=2,
            min_connection_packets=2, after_update_ms=2000,
            capture_ms=14000, max_quiet_ms=5000)
        self.assertEqual([], result.problems)

    def test_one_connection_alone_shows_nothing_about_the_update(self):
        # Nothing in the file ended inside it, so there is no connection the
        # check can ask, and passing on that would be passing on no evidence.
        records = [connect_ind(), data_packet(251)]
        _header, result = self.check(
            pcapng(records, times=[0, 4_000_000], tsresol=9), after_update_ms=2000)
        self.assertEqual(
            ["the capture holds no connection that ended inside it, so nothing "
             "in it shows a follow surviving the parameter update 2000 ms in"],
            result.problems)

    def test_no_timed_packet_at_all_fails_the_update_floor(self):
        # A simple packet block carries no time, so no distance can be read
        # from it and the check must not pass on it.
        data = _block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
        data += _block(1, struct.pack("<HHI", 256, 0, 65535))
        for item in (connect_ind(), data_packet(251), second_connect_ind()):
            data += _block(3, struct.pack("<I", len(item)) + item)
        _header, result = self.check(data, after_update_ms=2000)
        self.assertIn("the latest was none", result.problems[0])

    def test_a_corrupt_late_packet_does_not_satisfy_the_update_floor(self):
        records = [connect_ind(), data_packet(251, corrupt=True),
                   second_connect_ind()]
        _header, result = self.check(
            pcapng(records, times=[0, 4_000_000, 5_200_000], tsresol=9),
            after_update_ms=2000)
        self.assertIn("the latest was none", result.problems[0])

    def test_a_connection_the_capture_never_announced_is_refused(self):
        """The board writes the address it synchronized on into each record,
        so a packet on an address no CONNECT_IND handed out is a connection
        the file cannot explain."""
        records = [connect_ind(), data_packet(251),
                   data_packet(27, access=0xDEADBEEF, crc_init=0x111111),
                   data_packet(27, access=0xDEADBEEF, crc_init=0x111111)]
        times = [0, 4_000_000, 4_100_000, 4_200_000]
        data = pcapng(records, times=times, tsresol=9)
        _header, result = self.check(data, require_announced=True)
        self.assertEqual({0xDEADBEEF: 2}, result.unannounced)
        self.assertEqual(
            ["packet(s) on an access address no CONNECT_IND in this capture "
             "handed out: deadbeef x2"],
            result.problems)
        # Without the option the packets are still counted and reported.
        _header, result = self.check(data)
        self.assertEqual([], result.problems)
        self.assertEqual({0xDEADBEEF: 2}, result.unannounced)

    def test_another_pair_s_announced_connection_is_explained(self):
        """A CONNECT_IND of another device in range explains its packets."""
        other = connect_ind(initiator=OTHER,
                            data=lldata(access=0xDEADBEEF, crc_init=0x111111))
        records = [connect_ind(), data_packet(251), other,
                   data_packet(27, access=0xDEADBEEF, crc_init=0x111111)]
        _header, result = self.check(
            pcapng(records, times=[0, 4_000_000, 4_100_000, 4_200_000], tsresol=9),
            min_connections=1, require_announced=True)
        self.assertEqual([], result.problems)
        self.assertEqual({}, result.unannounced)
        # Only our pair's connections are measured.
        self.assertEqual([ACCESS], [item.access_address
                                    for item in result.connections])

    def test_main_reports_each_connection_and_the_span(self):
        records, times = self.two_cycles()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.pcapng"
            path.write_bytes(pcapng(records, times=times, tsresol=9))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = conncapture.main([
                    str(path), "--central", CENTRAL, "--peripheral", PERIPHERAL,
                    "--interval-units", str(INTERVAL_UNITS), "--latency", "0",
                    "--timeout-units", str(TIMEOUT_UNITS),
                    "--min-connections", "2", "--min-connection-packets", "2",
                    "--after-update-ms", "2000", "--require-announced"])
        text = out.getvalue()
        self.assertEqual(0, status, text)
        self.assertIn("span 9.2s", text)
        self.assertIn("connections: 2", text)
        self.assertIn(f"connection 2: access address {SECOND_ACCESS:08x} "
                      "5200 ms into the capture, 2 packet(s), 2 with a "
                      "correct CRC, last correct CRC 4000 ms after its "
                      "CONNECT_IND", text)
        self.assertIn("unannounced access addresses: none", text)


class ConnectionInfoOptionTests(unittest.TestCase):
    """libbtbb records one option per CONNECT_IND it decodes, and the option
    a reader follows the connection with must be one of this capture's."""

    def setUp(self):
        self.record = connect_ind()
        self.pdu = connect_ind_body(self.record)[6:6 + 34]
        self.records = [self.record] + [data_packet(251)] * 3

    def check(self, data):
        return conncapture.check(
            data, central=CENTRAL, peripheral=PERIPHERAL,
            interval_units=INTERVAL_UNITS, latency=0,
            timeout_units=TIMEOUT_UNITS, check_connection_info=True)

    def test_the_recorded_pdu_passes(self):
        _header, result = self.check(
            pcapng_with_option(self.records, self.pdu))
        self.assertEqual([], result.problems)
        self.assertEqual(1, result.connection_info_options)
        self.assertIn("as the capture decoded it", result.connection_info)

    def test_pcap_and_pcapng_without_the_option_report_none(self):
        for data in (pcap([self.record]), pcapng([self.record])):
            self.assertEqual([], conncapture.connection_info_options(data))
        _header, result = self.check(pcapng(self.records))
        self.assertEqual(conncapture.NO_OPTION, result.connection_info)
        self.assertIn("carries no connection information option",
                      result.problems[0])

    def test_every_recorded_option_is_read_not_only_the_first(self):
        # A capture with other devices in range holds their CONNECT_INDs
        # too, and the pair's own may be any of the options.
        other = connect_ind_body(connect_ind(initiator=OTHER))[6:6 + 34]
        data = pcapng_with_option(self.records, other, other, self.pdu)
        self.assertEqual(3, len(conncapture.connection_info_options(data)))
        _header, result = self.check(data)
        self.assertEqual([], result.problems)
        self.assertEqual(3, result.connection_info_options)

    def test_an_option_whose_length_does_not_cover_the_pdu_is_not_read(self):
        # The bytes after a short option belong to the next option, not to
        # this one, so a reader would find no PDU there either.
        data = pcapng_with_option(self.records, self.pdu, value_length=0)
        self.assertEqual([], conncapture.connection_info_options(data))

    def test_only_another_device_s_option_fails(self):
        other = connect_ind_body(connect_ind(initiator=OTHER))[6:6 + 34]
        _header, result = self.check(pcapng_with_option(self.records, other))
        self.assertEqual(
            [f"none of the 1 recorded connection information option(s) is a "
             f"CONNECT_IND this capture decoded from {CENTRAL} to {PERIPHERAL}"],
            result.problems)
        # Never another device's address, only that there was one.
        self.assertNotIn(OTHER, "\n".join(result.problems))

    def test_the_six_byte_shift_fails(self):
        # What libbtbb#21 recorded: the 34 bytes from the access address on,
        # which read as a CONNECT_IND between two addresses nothing has.
        _header, result = self.check(
            pcapng_with_option(self.records, connect_ind_body(self.record)[:34]))
        self.assertIn("is a CONNECT_IND this capture decoded", result.problems[0])

    def test_wrong_parameters_in_the_pair_s_option_are_named(self):
        # The addresses are the pair's and the parameters are not: the
        # option would send a reader to the wrong interval.
        pdu = connect_ind(data=lldata(interval=48))[-37:-3]
        _header, result = self.check(pcapng_with_option(self.records, pdu))
        self.assertIn("connection interval 48, not 24", result.problems[0])

    def test_an_option_the_capture_never_decoded_is_named_field_by_field(self):
        # Every parameter the case expects is right and the addresses are
        # the pair's, but the access address, CRC init and hop belong to no
        # CONNECT_IND in the file: a reader would follow nothing.
        pdu = connect_ind(data=lldata(access=0x11223344, crc_init=0x123456,
                                      hop=9))[-37:-3]
        _header, result = self.check(pcapng_with_option(self.records, pdu))
        text = result.problems[0]
        self.assertIn("access address 287454020 belongs to no CONNECT_IND", text)
        self.assertIn("CRC init 1193046 belongs to no CONNECT_IND", text)
        self.assertIn("hop increment 9 belongs to no CONNECT_IND", text)


class MainTests(unittest.TestCase):
    def run_main(self, records, *options):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.pcapng"
            path.write_bytes(pcapng(records))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = conncapture.main([
                    str(path), "--central", CENTRAL.lower(),
                    "--peripheral", PERIPHERAL.lower(),
                    "--interval-units", str(INTERVAL_UNITS), "--latency", "0",
                    "--timeout-units", str(TIMEOUT_UNITS), *options])
        return code, out.getvalue()

    def test_it_prints_counts_and_the_connection_only(self):
        code, output = self.run_main(
            [connect_ind()] + [data_packet(251)] * 2 + [data_packet(31, access=0x99)],
            "--min-data-packets", "2", "--long-packets", "2")
        self.assertEqual(0, code, output)
        self.assertIn("connect_ind: 1\n", output)
        self.assertIn("parameters: interval 24 units (30 ms) latency 0 timeout 400 "
                      "units hop 11 window 3/1\n", output)
        self.assertIn(f"access address: {ACCESS:08x} crc init {CRC_INIT:06x}\n", output)
        self.assertIn("data packets: 2 (2 with a correct CRC, 2 of 28 bytes or more, "
                      "longest 251)\n", output)
        self.assertIn("connection info option: none recorded\n", output)
        self.assertTrue(output.endswith("capture checks passed\n"))
        self.assertNotIn(OTHER, output)

    def test_a_missing_file_is_reported_without_a_traceback(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = conncapture.main(["/nonexistent/capture.pcapng", "--central",
                                     CENTRAL, "--peripheral", PERIPHERAL,
                                     "--interval-units", "24", "--latency", "0",
                                     "--timeout-units", "400"])
        self.assertEqual(1, code)
        self.assertIn("FAIL: cannot read", out.getvalue())


if __name__ == "__main__":
    unittest.main()
