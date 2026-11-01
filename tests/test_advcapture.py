import contextlib
import io
from pathlib import Path
import struct
import tempfile
import unittest

from ubertooth_harness import advcapture

ADDRESS = "C3:11:22:33:44:55"
OTHER = "D0:00:00:00:00:02"


def payload(counter=5, chmap=0x01, tag=b"\xc0\xff\xee", magic=b"UTAD"):
    mfg = struct.pack("<H", 0xFFFF) + magic + bytes([1, chmap]) + struct.pack("<I", counter) + tag
    return bytes([2, 0x01, 0x04, len(mfg) + 1, 0xFF]) + mfg


def record(address=ADDRESS, rf_channel=0, pdu_type=0x2, tx_add=1, data=None,
           corrupt=False, access=advcapture.ADV_ACCESS_ADDRESS):
    data = payload() if data is None else data
    adva = bytes(reversed(bytes.fromhex(address.replace(":", ""))))
    pdu = adva + data
    header = bytes([pdu_type | tx_add << 6, len(pdu)])
    crc = advcapture.crc24(header + pdu).to_bytes(3, "little")
    if corrupt:
        pdu = pdu[:-1] + bytes([pdu[-1] ^ 1])
    phdr = bytes([rf_channel, 0xC0, 0x9C, 0]) + struct.pack("<I", access) + b"\x00\x00"
    return phdr + struct.pack("<I", access) + header + pdu + crc


def pcap(records, link_type=256, times=None, magic=0xA1B2C3D4, ticks=1_000_000):
    """PCAP of these records, each at ``times[i]`` microseconds if given.

    ``magic`` and ``ticks`` go together: the original magic counts
    microseconds in a record's second field, 0xA1B23C4D nanoseconds, which
    is what libbtbb writes.
    """
    out = struct.pack("<IHHiIII", magic, 2, 4, 0, 0, 65535, link_type)
    for index, item in enumerate(records):
        if times is None:
            seconds, fraction = 1, 2
        else:
            micros = times[index]
            seconds, fraction = divmod(micros * ticks // 1_000_000, ticks)
        out += struct.pack("<IIII", seconds, fraction, len(item), len(item)) + item
    return out


def _block(block_type, body):
    body += b"\x00" * (-len(body) % 4)
    length = len(body) + 12
    return struct.pack("<II", block_type, length) + body + struct.pack("<I", length)


def pcapng(records, link_type=256, times=None, tsresol=None):
    """PCAPNG of these records, each at ``times[i]`` microseconds if given.

    ``tsresol`` writes the interface option that says how finely the records
    are timed (9 for nanoseconds, as libbtbb writes); without it a reader
    has to assume microseconds.
    """
    ticks = 1_000_000 if tsresol is None else 10 ** tsresol
    options = b""
    if tsresol is not None:
        options = struct.pack("<HHB", 9, 1, tsresol) + b"\x00" * 3
    out = _block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    out += _block(1, struct.pack("<HHI", link_type, 0, 65535) + options)
    for index, item in enumerate(records):
        count = 1 if times is None else times[index] * ticks // 1_000_000
        out += _block(6, struct.pack("<IIIII", 0, count >> 32, count & 0xFFFFFFFF,
                                     len(item), len(item)) + item)
    return out


class CrcTests(unittest.TestCase):
    def test_round_trip_and_corruption(self):
        good = advcapture.parse_advertising(record())
        self.assertEqual(ADDRESS, good.address)
        self.assertIsNone(advcapture.parse_advertising(record(corrupt=True)))
        self.assertIsNone(advcapture.parse_advertising(record(access=0x12345678)))


class TimedRecordTests(unittest.TestCase):
    """When each record was captured, which a check of a window spanning
    several connections measures distances in."""

    def times(self, data):
        _format, _link_type, timed = advcapture.read_timed_records(data)
        return [item.micros for item in timed]

    def test_pcap_microseconds_and_nanoseconds(self):
        wanted = [0, 1_500_000, 18_000_123]
        self.assertEqual(wanted, self.times(
            pcap([record()] * 3, times=wanted)))
        # The nanosecond magic libbtbb writes carries the same instants.
        self.assertEqual(wanted, self.times(
            pcap([record()] * 3, times=wanted, magic=0xA1B23C4D,
                 ticks=1_000_000_000)))

    def test_pcapng_default_microseconds_and_a_declared_resolution(self):
        wanted = [0, 1_500_000, 18_000_123]
        self.assertEqual(wanted, self.times(pcapng([record()] * 3, times=wanted)))
        self.assertEqual(wanted, self.times(
            pcapng([record()] * 3, times=wanted, tsresol=9)))

    def test_a_resolution_option_the_block_does_not_hold_is_not_read(self):
        # The bytes after a short option belong to the next option, so a
        # resolution read from them would be any number at all.
        item = record()
        options = struct.pack("<HH", 9, 200) + b"\x09"  # claims 200, carries 1
        data = (_block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
                + _block(1, struct.pack("<HHI", 256, 0, 65535) + options)
                + _block(6, struct.pack("<IIIII", 0, 0, 1_500_000, len(item),
                                        len(item)) + item))
        # Microseconds, the resolution a reader assumes without the option.
        self.assertEqual([1_500_000], self.times(data))

    def test_a_record_with_no_time_keeps_none(self):
        # A simple packet block carries no timestamp, so it cannot be read
        # as time zero.
        item = record()
        data = (_block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
                + _block(1, struct.pack("<HHI", 256, 0, 65535))
                + _block(3, struct.pack("<I", len(item)) + item))
        self.assertEqual([None], self.times(data))

    def test_read_records_still_hands_back_the_records_alone(self):
        _format, _link_type, records = advcapture.read_records(
            pcap([record()], times=[7]))
        self.assertEqual([record()], records)


class CheckTests(unittest.TestCase):
    def check(self, data, **options):
        options.setdefault("address", ADDRESS)
        options.setdefault("channel", 37)
        return advcapture.check(data, **options)

    def test_expected_packets_pass_in_pcap_and_pcapng(self):
        packets = [record(), record(data=payload(counter=6)), record(address=OTHER, rf_channel=12)]
        for data in (pcap(packets), pcapng(packets)):
            header, result = self.check(
                data, payload="c0ffee", chmap="37", adv_type="nonconn",
                counter_min=5, counter_max=6, min_packets=2, max_packets=2)
            self.assertEqual([], result.problems, header)
            self.assertEqual((3, 2, [5, 6]), (result.records, result.matches, result.counters))
        self.assertTrue(self.check(pcapng(packets))[0].startswith("pcapng, link type 256"))

    def test_each_wrong_field_is_named(self):
        packets = [
            record(rf_channel=12),
            record(pdu_type=0x0),
            record(data=payload(chmap=0x07)),
            record(data=payload(tag=b"\x01")),
            record(data=payload(counter=99)),
            record(data=payload(magic=b"NOPE")),
        ]
        _header, result = self.check(pcap(packets), payload="c0ffee", chmap="37",
                                     adv_type="nonconn", counter_min=5, counter_max=6)
        text = "\n".join(result.problems)
        for reason in ("on rf channel 12, not 0", "ADV_IND, not ADV_NONCONN_IND",
                       "channel map byte 0x07, not 0x01", "tag 01, not c0ffee",
                       "counter 99 above 6", "without the harness payload"):
            self.assertIn(reason, text)

    def test_public_address_or_corrupt_packets_do_not_count(self):
        _header, result = self.check(pcap([record(tx_add=0), record(corrupt=True)]))
        self.assertEqual(0, result.matches)
        self.assertIn("0 packet(s) from C3:11:22:33:44:55, fewer than 1", result.problems)

    def test_absent_fails_on_any_packet(self):
        _header, result = self.check(pcap([record(address=OTHER)]), absent=True)
        self.assertEqual([], result.problems)
        _header, result = self.check(pcap([record(rf_channel=0)]), channel=38, absent=True)
        self.assertEqual(["1 packet(s) from C3:11:22:33:44:55 while it advertises on another "
                          "channel than 38"], result.problems)

    def test_the_advertisers_scan_responses_are_counted_not_checked(self):
        # An ADV_IND advertiser answers any scanner in range, so SCAN_RSPs
        # from its own address arrive whatever the test does. They carry no
        # harness payload and are not the type the case asked for.
        packets = [record(pdu_type=0x0), record(pdu_type=0x0),
                   record(pdu_type=0x4, data=b"")]

        _header, result = self.check(pcap(packets), payload="c0ffee", chmap="37",
                                     adv_type="conn", counter_min=5, counter_max=5,
                                     min_packets=2, max_packets=2)

        self.assertEqual([], result.problems)
        self.assertEqual((2, 1), (result.matches, result.scan_responses))

    def test_a_scan_response_on_another_channel_is_still_reported(self):
        # The advertiser answers on the channel the SCAN_REQ came in on, so a
        # reply on another channel is a leak like any other packet.
        _header, result = self.check(pcap([record(pdu_type=0x4, rf_channel=12, data=b"")]),
                                     adv_type="conn", min_packets=0)

        self.assertEqual(["1 packet(s) from C3:11:22:33:44:55 on rf channel 12, not 0"],
                         result.problems)
        self.assertEqual((0, 1), (result.matches, result.scan_responses))

    def test_a_scan_response_from_a_nonconn_advertiser_is_a_fault(self):
        # ADV_NONCONN_IND is not scannable, so there is nothing to answer.
        _header, result = self.check(pcap([record(pdu_type=0x4, data=b"")]),
                                     adv_type="nonconn")

        self.assertEqual(0, result.scan_responses)
        self.assertIn("SCAN_RSP, not ADV_NONCONN_IND", "\n".join(result.problems))

    def test_a_scan_response_still_counts_as_a_leak_for_absent(self):
        # --absent asks whether any of the advertiser's packets reached a
        # channel it does not advertise on; a scan response is one of them.
        _header, result = self.check(pcap([record(pdu_type=0x4, data=b"")]),
                                     channel=38, absent=True, adv_type="conn")

        self.assertEqual((1, 0), (result.matches, result.scan_responses))
        self.assertIn("while it advertises on another channel than 38", result.problems[0])

    def test_packet_bounds(self):
        _header, result = self.check(pcap([record()] * 4), min_packets=5)
        self.assertIn("fewer than 5", result.problems[0])
        _header, result = self.check(pcap([record()] * 4), max_packets=3)
        self.assertIn("more than 3", result.problems[0])

    def test_bad_files(self):
        for data, reason in ((b"", "empty"), (b"nope" * 8, "neither PCAP nor PCAPNG"),
                             (pcap([record()])[:-2], "cut short")):
            _header, result = self.check(data)
            self.assertIn(reason, result.problems[0])
        _header, result = self.check(pcap([record()], link_type=192))
        self.assertEqual(["link type 192, not 256"], result.problems)

    def test_main_prints_counts_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.pcap"
            path.write_bytes(pcap([record(), record(address=OTHER)]))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = advcapture.main([str(path), "--address", ADDRESS.lower(),
                                        "--channel", "37", "--payload", "C0FFEE",
                                        "--chmap", "37", "--type", "nonconn"])
        self.assertEqual(0, code)
        self.assertEqual("capture: pcap, link type 256, 2 records\n"
                         "advertiser packets: 1 (rf channel 0 x1)\n"
                         "counters: 5-5\ncapture checks passed\n", out.getvalue())
        self.assertNotIn(OTHER, out.getvalue())


if __name__ == "__main__":
    unittest.main()
