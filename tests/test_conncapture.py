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


def pcapng_with_option(records, option_pdu, code=conncapture.CONNECTION_INFO_OPTION):
    """PCAPNG whose interface block carries libbtbb's connection info option."""
    option = struct.pack("<HH", code, 4 + 8 + 34) + struct.pack("<Q", 7) + option_pdu
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


class ConnectIndTests(unittest.TestCase):
    def test_fields_are_read_from_the_pdu(self):
        found = conncapture.connect_inds([connect_ind()])
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
        self.assertEqual([], conncapture.connect_inds([data_packet()]))
        self.assertEqual([], conncapture.connect_inds([b"short"]))

    def test_a_corrupted_connect_ind_is_not_read(self):
        # Its parameters are what the checks compare, so a flipped bit in
        # them must not pass for the connection that was asked for.
        self.assertEqual([], conncapture.connect_inds([connect_ind(corrupt=True)]))


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


class ConnectionInfoOptionTests(unittest.TestCase):
    """libbtbb records the option 6 bytes early (SqueakyFloors/libbtbb#21)."""

    def setUp(self):
        self.record = connect_ind()
        self.body = connect_ind_body(self.record)

    def classify(self, option_pdu):
        data = pcapng_with_option([self.record], option_pdu)
        return conncapture.classify_connection_info(
            conncapture.connection_info_option(data), self.body)

    def test_the_shift_is_recognised(self):
        self.assertEqual(conncapture.SHIFTED, self.classify(self.body[:34]))

    def test_the_fixed_option_is_recognised(self):
        self.assertEqual(conncapture.CORRECT, self.classify(self.body[6:6 + 34]))

    def test_anything_else_is_named(self):
        self.assertEqual("neither the PDU nor the 6-byte shift",
                         self.classify(bytes(34)))

    def test_pcap_and_pcapng_without_the_option_report_absent(self):
        for data in (pcap([self.record]), pcapng([self.record])):
            self.assertIsNone(conncapture.connection_info_option(data))
        self.assertEqual("absent", conncapture.classify_connection_info(None, self.body))

    def test_a_missing_option_fails_the_pin_as_a_regression(self):
        _header, result = conncapture.check(
            pcapng([self.record]), central=CENTRAL, peripheral=PERIPHERAL,
            interval_units=INTERVAL_UNITS, latency=0, timeout_units=TIMEOUT_UNITS,
            connection_info_wrong=True)
        self.assertEqual(conncapture.ABSENT, result.connection_info)
        self.assertIn("no connection information option at all", result.problems[0])

    def test_the_check_passes_while_the_option_is_not_the_connect_ind(self):
        # Whatever libbtbb records today, it is not the CONNECT_IND's bytes.
        for option in (self.body[:34], bytes(34)):
            data = pcapng_with_option([self.record] + [data_packet(251)] * 3,
                                      option)
            _header, result = conncapture.check(
                data, central=CENTRAL, peripheral=PERIPHERAL,
                interval_units=INTERVAL_UNITS, latency=0,
                timeout_units=TIMEOUT_UNITS, min_data_packets=1,
                long_packets=1, connection_info_wrong=True)
            self.assertEqual([], result.problems)

    def test_the_check_fails_once_libbtbb_is_fixed(self):
        data = pcapng_with_option([self.record], self.body[6:6 + 34])
        _header, result = conncapture.check(
            data, central=CENTRAL, peripheral=PERIPHERAL,
            interval_units=INTERVAL_UNITS, latency=0, timeout_units=TIMEOUT_UNITS,
            connection_info_wrong=True)
        self.assertIn("libbtbb#21 is fixed", result.problems[0])


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
        self.assertIn("connection info option: absent\n", output)
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
