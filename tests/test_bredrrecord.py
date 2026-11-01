"""The BR/EDR capture record decoder and its checks.

The header error check and the payload CRC are written here a second time,
from the Bluetooth Core specification, in rxreplay.py, which builds synthetic
packets rather than reading them. These tests run the decoder's reverse
computations against that independent forward pair, so a mistake in either
one shows up instead of agreeing with itself.
"""

import io
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ubertooth_harness import bredrrecord, capturefile, rxreplay

LAP = 0xE9C5BA
UAP = 0x0D
# Types with a one-byte and a two-byte payload header.
DH1, DH5, POLL = 4, 15, 1

GOOD_FLAGS = (bredrrecord.DEWHITENED | bredrrecord.SIGPOWER_VALID
              | bredrrecord.NOISEPOWER_VALID | bredrrecord.REFLAP_VALID
              | bredrrecord.REFUAP_VALID | bredrrecord.HEC_CHECKED
              | bredrrecord.HEC_VALID)


def bt_header(packet_type=POLL, *, lt_addr=1, flags=0, uap=UAP, hec=None):
    """The 18-bit packet header a record carries, packed as libbtbb packs it."""
    data = (lt_addr & 7) | ((packet_type & 0xF) << 3) | ((flags & 7) << 7)
    return data | ((rxreplay.hec(data, uap) if hec is None else hec) << 10)


def dh1_payload(body=b"harness", uap=UAP):
    """A DH1 payload as a record holds it: header byte, body, CRC."""
    header = 2 | (1 << 2) | (len(body) << 3)
    data = bytes([header]) + body
    bits = [(byte >> i) & 1 for byte in data for i in range(8)]
    return data + rxreplay.crc16(bits, uap).to_bytes(2, "little")


def record(payload=b"", *, channel=39, signal=-50, noise=-80, offenses=0,
           transport_rate=0, corrected_header=0, corrected_payload=0,
           lap=LAP, ref_lap=LAP, ref_uap=UAP, header=None, flags=None):
    if header is None:
        header = bt_header(DH1 if payload else POLL)
    if flags is None:
        flags = GOOD_FLAGS | (bredrrecord.PAYLOAD_PRESENT if payload else 0)
    return bredrrecord._HEADER.pack(
        channel, signal, noise, offenses, transport_rate, corrected_header,
        corrected_payload, lap, (ref_lap & 0xFFFFFF) | (ref_uap << 24),
        header, flags) + payload


def pcap(records, link_type=bredrrecord.LINKTYPE_BLUETOOTH_BREDR_BB,
         magic=0xA1B23C4D, first_ns=1_000_000_000):
    out = struct.pack("<IHHiIII", magic, 2, 4, 0, 0, 0xFFFF, link_type)
    for step, data in enumerate(records):
        when = first_ns + step * 1_000_000
        out += struct.pack("<IIII", when // 10 ** 9, when % 10 ** 9,
                           len(data), len(data)) + data
    return out


def pcapng(records, link_type=bredrrecord.LINKTYPE_BLUETOOTH_BREDR_BB,
           options=b"", first_ns=1_000_000_000):
    tsresol = struct.pack("<HHB", capturefile.IF_TSRESOL, 1, 9) + b"\0\0\0"
    interface = tsresol + options + struct.pack("<HH", 0, 0)
    idb = struct.pack("<IIHHI", 1, 20 + len(interface), link_type, 0, 0xFFFF)
    idb += interface + struct.pack("<I", 20 + len(interface))
    out = struct.pack("<IIIHHq", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1)
    out += struct.pack("<I", 28) + idb
    for step, data in enumerate(records):
        when = first_ns + step * 1_000_000
        pad = (-len(data)) % 4
        total = 36 + len(data) + pad
        out += struct.pack("<IIIIIII", 2, total, 0, when >> 32,
                           when & 0xFFFFFFFF, len(data), len(data))
        out += data + b"\0" * pad + struct.pack("<II", 0, total)
    return out


def bd_addr_option(lap=LAP, uap=UAP, nap=0, uap_mask=0xFF, nap_valid=0):
    address = lap | (uap << 24) | (nap << 32)
    return (struct.pack("<HH", bredrrecord.OPTION_BD_ADDR, 8)
            + address.to_bytes(6, "little") + bytes([uap_mask, nap_valid]))


class SpecComputations(unittest.TestCase):
    """The reverse HEC and the CRC against rxreplay's forward pair."""

    def test_the_hec_reversal_recovers_every_uap(self):
        for uap in range(256):
            for header in (0, 1, 0x155, 0x2AA, 0x3FF):
                with self.subTest(uap=uap, header=header):
                    self.assertEqual(
                        uap,
                        bredrrecord.uap_from_hec(header,
                                                 rxreplay.hec(header, uap)))

    def test_the_payload_crc_matches_the_specs_crc(self):
        for body in (b"", b"\x00", b"harness", bytes(range(40))):
            for uap in (0x00, 0x0D, 0xA5, 0xFF):
                with self.subTest(body=len(body), uap=uap):
                    bits = [(byte >> i) & 1 for byte in body for i in range(8)]
                    self.assertEqual(rxreplay.crc16(bits, uap),
                                     bredrrecord.payload_crc(body, uap))

    def test_a_record_crc_checks_out(self):
        payload = dh1_payload()
        self.assertEqual(
            int.from_bytes(payload[-2:], "little"),
            bredrrecord.payload_crc(payload[:-2], UAP))


class Decoding(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_a_pcap_record_decodes_field_by_field(self):
        payload = dh1_payload()
        path = self.write("one.pcap", pcap([record(
            payload, channel=60, signal=-43, noise=-81, offenses=1,
            corrected_header=2, corrected_payload=3,
            header=bt_header(DH1, lt_addr=5, flags=0b101))]))
        found, = bredrrecord.records(path)
        self.assertEqual(60, found.rf_channel)
        self.assertEqual((-43, -81), (found.signal_power, found.noise_power))
        self.assertEqual(1, found.access_code_offenses)
        self.assertEqual((2, 3), (found.corrected_header_bits,
                                  found.corrected_payload_bits))
        self.assertEqual((LAP, LAP, UAP), (found.lap, found.ref_lap, found.ref_uap))
        self.assertEqual((5, DH1, "DH1/2-DH1"),
                         (found.lt_addr, found.packet_type, found.type_name))
        # flags 0b101: FLOW set, ARQN clear, SEQN set.
        self.assertEqual((1, 0, 1), (found.flow, found.arqn, found.seqn))
        self.assertEqual((0, 0), (found.transport, found.rate))
        self.assertEqual(len(payload), len(found.payload))
        self.assertEqual(len(payload), found.payload_header_length)
        self.assertEqual(1_000_000_000, found.timestamp_ns)
        self.assertIn("payload", found.flag_names)

    def test_a_pcapng_record_decodes_the_same_way(self):
        data = record(dh1_payload())
        one, = bredrrecord.records(self.write("one.pcapng", pcapng([data])))
        two, = bredrrecord.records(self.write("one.pcap", pcap([data])))
        self.assertEqual(
            (one.lap, one.bt_header, one.flags, one.payload, one.timestamp_ns),
            (two.lap, two.bt_header, two.flags, two.payload, two.timestamp_ns))

    def test_a_two_byte_payload_header_gives_the_longer_length(self):
        body = bytes(100)
        header = 2 | (1 << 2) | (len(body) << 3)
        payload = header.to_bytes(2, "little") + body + b"\0\0"
        found, = bredrrecord.records(self.write("dh5.pcap", pcap([record(
            payload, header=bt_header(DH5))])))
        self.assertEqual(len(body) + 4, found.payload_header_length)
        self.assertEqual(len(payload), found.payload_header_length)

    def test_a_type_with_no_payload_header_has_no_length_to_read(self):
        found, = bredrrecord.records(self.write("poll.pcap", pcap([record()])))
        self.assertEqual(-1, found.payload_header_length)

    def test_another_link_type_is_refused(self):
        path = self.write("le.pcap", pcap([b"\0" * 30], link_type=256))
        with self.assertRaises(bredrrecord.RecordError):
            bredrrecord.records(path)

    def test_a_record_shorter_than_the_pseudo_header_is_refused(self):
        path = self.write("short.pcap", pcap([b"\0" * 10]))
        with self.assertRaises(bredrrecord.RecordError):
            bredrrecord.records(path)

    def test_the_followed_address_comes_from_the_interface_option(self):
        path = self.write("follow.pcapng",
                          pcapng([record()], options=bd_addr_option()))
        self.assertEqual({"lap": LAP, "uap": UAP, "nap": 0, "uap_mask": 0xFF,
                          "nap_valid": 0},
                         bredrrecord.following_address(path))
        self.assertIsNone(bredrrecord.following_address(
            self.write("plain.pcapng", pcapng([record()]))))
        self.assertIsNone(bredrrecord.following_address(
            self.write("plain.pcap", pcap([record()]))))

    def test_a_short_bd_addr_option_is_refused(self):
        option = struct.pack("<HH", bredrrecord.OPTION_BD_ADDR, 4) + b"\1\2\3\4"
        path = self.write("odd.pcapng", pcapng([record()], options=option))
        with self.assertRaises(bredrrecord.RecordError):
            bredrrecord.following_address(path)


class Consistency(unittest.TestCase):
    """Each check names the record and what is out of step in it."""

    def check(self, data, **options):
        return bredrrecord.problems(
            [bredrrecord.Record(0, 0, *bredrrecord._HEADER.unpack(data[:22]),
                                data[22:])],
            **options)

    def assertProblem(self, text, data, **options):
        found = self.check(data, **options)
        self.assertTrue(found, "expected a problem")
        self.assertTrue(any(text in problem for problem in found),
                        f"{text!r} not in {found}")

    def test_a_well_formed_record_has_nothing_wrong(self):
        self.assertEqual([], self.check(record()))
        self.assertEqual([], self.check(record(dh1_payload())))

    def test_a_channel_above_the_band_is_a_problem(self):
        self.assertProblem("RF channel 79", record(channel=79))

    def test_the_noise_flag_must_follow_the_two_figures(self):
        self.assertProblem("the noise power flag is set",
                           record(signal=-80, noise=-50))
        self.assertProblem("the noise power flag is clear",
                           record(flags=GOOD_FLAGS & ~bredrrecord.NOISEPOWER_VALID))

    def test_the_payload_flag_must_follow_the_bytes(self):
        self.assertProblem("the payload flag is clear",
                           record(dh1_payload(), flags=GOOD_FLAGS))
        self.assertProblem(
            "the payload flag is set",
            record(flags=GOOD_FLAGS | bredrrecord.PAYLOAD_PRESENT))

    def test_the_reference_flags_must_follow_the_reference_field(self):
        self.assertProblem("the reference LAP flag is set",
                           record(ref_lap=bredrrecord.LAP_ANY))
        self.assertProblem("the reference UAP flag is set",
                           record(ref_uap=bredrrecord.UAP_ANY))
        self.assertProblem(
            "the reference UAP flag is clear",
            record(flags=GOOD_FLAGS & ~bredrrecord.REFUAP_VALID))

    def test_a_valid_check_that_was_not_made_is_a_problem(self):
        self.assertProblem("the HEC is valid but was not checked",
                           record(flags=GOOD_FLAGS & ~bredrrecord.HEC_CHECKED))
        self.assertProblem(
            "the CRC is valid but was not checked",
            record(dh1_payload(),
                   flags=GOOD_FLAGS | bredrrecord.PAYLOAD_PRESENT
                   | bredrrecord.CRC_VALID))

    def test_a_crc_checked_over_too_few_bytes_is_a_problem(self):
        self.assertProblem(
            "a CRC was checked over 2 payload bytes",
            record(b"\x01\x02",
                   flags=GOOD_FLAGS | bredrrecord.PAYLOAD_PRESENT
                   | bredrrecord.CRC_CHECKED,
                   header=bt_header(POLL)))

    def test_a_header_whose_hec_names_another_uap_is_a_problem(self):
        self.assertProblem("the header's HEC gives UAP",
                           record(header=bt_header(POLL, uap=UAP ^ 0x10)))

    def test_a_payload_whose_length_field_disagrees_is_a_problem(self):
        self.assertProblem("length field says",
                           record(dh1_payload() + b"\0", header=bt_header(DH1)))

    def test_a_payload_whose_crc_disagrees_is_a_problem(self):
        payload = bytearray(dh1_payload())
        payload[-1] ^= 0xFF
        self.assertProblem(
            "says the CRC passed",
            record(bytes(payload),
                   flags=GOOD_FLAGS | bredrrecord.PAYLOAD_PRESENT
                   | bredrrecord.CRC_CHECKED | bredrrecord.CRC_VALID,
                   header=bt_header(DH1)))

    def test_the_payload_checks_can_be_left_out_for_an_encrypted_link(self):
        broken = record(dh1_payload() + b"\0", header=bt_header(DH1))
        self.assertEqual([], self.check(broken, payload_checks=False))

    def test_a_transport_or_rate_a_receive_capture_cannot_know_is_a_problem(self):
        self.assertProblem("transport 3 rate 0", record(transport_rate=0x30))
        self.assertProblem("transport 0 rate 1", record(transport_rate=0x01))

    def test_corrected_bit_counts_outside_the_codes_are_problems(self):
        self.assertProblem("11 corrected header bits", record(corrected_header=11))
        self.assertProblem("184 corrected payload bits",
                           record(dh1_payload(), corrected_payload=184))
        self.assertProblem("corrected payload bits with no payload",
                           record(corrected_payload=1))

    def test_the_counts_and_addresses_asked_for_are_checked(self):
        one = [bredrrecord.Record(0, 0, 39, -50, -80, 0, 0, 0, 0, LAP,
                                  LAP | (UAP << 24), bt_header(POLL),
                                  GOOD_FLAGS, b"")]
        self.assertEqual([], bredrrecord.problems(one, lap=LAP, min_packets=1,
                                                  reference_uap=UAP))
        self.assertIn("expected at least 2",
                      " ".join(bredrrecord.problems(one, min_packets=2)))
        self.assertIn("another LAP",
                      " ".join(bredrrecord.problems(one, lap=0x123456)))
        self.assertIn("reference UAP is not",
                      " ".join(bredrrecord.problems(one, reference_uap=0x11)))
        # A record that carries no reference UAP is not held to one: a
        # capture that gives a clock up records none until it has another.
        none = [bredrrecord.Record(0, 0, 39, -50, -80, 0, 0, 0, 0, LAP,
                                   LAP | (bredrrecord.UAP_ANY << 24),
                                   bt_header(POLL),
                                   GOOD_FLAGS & ~bredrrecord.REFUAP_VALID, b"")]
        self.assertEqual([], bredrrecord.problems(none, reference_uap=UAP))
        self.assertIn("carry a reference UAP",
                      " ".join(bredrrecord.problems(none, reference_uap=UAP,
                                                    min_reference_uap=1)))
        self.assertIn("another packet type",
                      " ".join(bredrrecord.problems(one, types=("DH1/2-DH1",))))
        self.assertIn("no DH1/2-DH1 record",
                      " ".join(bredrrecord.problems(one,
                                                    require_types=("DH1/2-DH1",))))
        self.assertIn("records carry a payload",
                      " ".join(bredrrecord.problems(one, min_with_payload=1)))
        self.assertIn("passed a payload CRC",
                      " ".join(bredrrecord.problems(one, min_crc_valid=1)))
        # The one record does carry a valid HEC, so a floor of one passes and
        # a floor of two does not.
        self.assertEqual([], bredrrecord.problems(one, min_hec_valid=1))
        self.assertIn("passed a header HEC",
                      " ".join(bredrrecord.problems(one, min_hec_valid=2)))
        self.assertEqual([], bredrrecord.problems(one, min_reference_uap=1))
        self.assertIn("carry a reference UAP",
                      " ".join(bredrrecord.problems(one, min_reference_uap=2)))
        self.assertIn("records on 1 channels",
                      " ".join(bredrrecord.problems(one, min_channels=2)))

    def stamped(self, *times):
        return [bredrrecord.Record(index, when, 39, -50, -80, 0, 0, 0, 0, LAP,
                                   LAP | (UAP << 24), bt_header(POLL),
                                   GOOD_FLAGS, b"")
                for index, when in enumerate(times)]

    def test_the_timestamps_are_reported_and_not_judged(self):
        # A packet caught as the capture clock's period count ticks over can
        # carry a time a whole period out in either direction, so a bound on
        # these would fail a good capture; the summary says what they did.
        records = self.stamped(2_000_000_000, 1_000_000_000, 3_000_000_000)
        self.assertEqual([], bredrrecord.problems(records))
        line, = [line for line in bredrrecord.summary(records, "x.pcap")
                 if line.startswith("BR/EDR times:")]
        self.assertEqual("BR/EDR times: 3 stamped, spanning 2.0 s, 1 stepping "
                         "back, the largest by 1000000 us", line)

    def test_the_counts_come_before_the_records_in_a_failure(self):
        # main() shows only the first few, and the counts are what a reader
        # needs first.
        bad = bredrrecord._HEADER.unpack(record(channel=79)[:22])
        many = [bredrrecord.Record(index, 0, *bad, b"") for index in range(20)]
        failures = bredrrecord.problems(many, min_packets=50)
        self.assertIn("expected at least 50", failures[0])


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = bredrrecord.main([str(item) for item in argv])
        return code, out.getvalue()

    def test_a_good_capture_is_summarized_and_passes(self):
        path = self.write("follow.pcapng",
                          pcapng([record(), record(dh1_payload(), channel=7)],
                                 options=bd_addr_option()))
        code, output = self.run_main(path, "--lap", "e9c5ba", "--reference-uap",
                                     "0d", "--min-packets", "2",
                                     "--min-with-payload", "1",
                                     "--min-channels", "2",
                                     "--require-types", "DH1/2-DH1",
                                     "--following-lap", "e9c5ba",
                                     "--following-uap", "0d",
                                     "--following-uap-mask", "ff")
        self.assertEqual(0, code, output)
        self.assertIn("BR/EDR records: 2 in follow.pcapng", output)
        self.assertIn("BR/EDR LAPs: e9c5ba (2)", output)
        self.assertIn("DH1/2-DH1 (1)", output)
        self.assertIn("BR/EDR channels: 2 distinct, 7 to 39", output)
        # ubertooth-rx knows the LAP and the UAP and never the NAP, so the
        # parts the option does not claim are printed as ??.
        self.assertIn("BR/EDR following: ??:??:0D:E9:C5:BA, UAP mask ff, "
                      "NAP invalid", output)
        self.assertNotIn("problem", output)

    def test_the_summary_prints_no_payload_bytes(self):
        payload = dh1_payload(b"secrets")
        path = self.write("one.pcap", pcap([record(payload)]))
        code, output = self.run_main(path)
        self.assertEqual(0, code, output)
        self.assertNotIn("secrets", output)
        self.assertNotIn(payload.hex(), output)

    def test_a_wrong_lap_or_address_fails(self):
        path = self.write("one.pcapng", pcapng([record()],
                                               options=bd_addr_option()))
        code, output = self.run_main(path, "--lap", "123456")
        self.assertEqual(1, code)
        self.assertIn("BR/EDR record problem:", output)
        code, output = self.run_main(path, "--following-lap", "112233")
        self.assertEqual(1, code)
        self.assertIn("was following LAP e9c5ba, not 112233", output)
        code, output = self.run_main(path, "--following-uap", "11")
        self.assertEqual(1, code)
        self.assertIn("was following UAP 0d, not 11", output)
        code, output = self.run_main(path, "--following-uap-mask", "00")
        self.assertEqual(1, code)
        self.assertIn("was following UAP mask ff, not 00", output)
        plain = self.write("plain.pcapng", pcapng([record()]))
        code, output = self.run_main(plain, "--following-lap", "e9c5ba")
        self.assertEqual(1, code)
        self.assertIn("records no address it was following", output)

    def test_a_missing_or_foreign_file_fails(self):
        code, output = self.run_main(self.dir / "absent.pcap")
        self.assertEqual(1, code)
        self.assertIn("does not exist", output)
        code, output = self.run_main(self.write("junk.pcap", b"junk"))
        self.assertEqual(1, code)
        self.assertIn("BR/EDR record problem:", output)

    def test_an_unknown_packet_type_name_is_refused(self):
        path = self.write("one.pcap", pcap([record()]))
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.run_main(path, "--types", "DH9")

    def test_it_runs_as_a_script_from_any_directory(self):
        path = self.write("one.pcap", pcap([record()]))
        result = subprocess.run(
            [sys.executable, str(Path(bredrrecord.__file__).resolve()),
             str(path), "--min-packets", "1"],
            cwd=tempfile.gettempdir(), capture_output=True, text=True,
            timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("BR/EDR records: 1", result.stdout)


if __name__ == "__main__":
    unittest.main()
