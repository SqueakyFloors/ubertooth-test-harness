"""Find a known advertiser's packets in an ``ubertooth-btle`` capture file.

Run as a script by the Capture known BLE advertiser cases::

    python advcapture.py FILE --address ADDR --channel 37|38|39
        [--payload HEX|none --chmap 37|38|39|all --type nonconn|conn
         --counter-min N --counter-max N --min-packets N --max-packets N]
    python advcapture.py FILE --address ADDR --channel C --absent

FILE is PCAP or PCAPNG with LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR (256), as
``ubertooth-btle -q`` and ``-r`` write it. Each record is a 10-byte LE
pseudo-header (RF channel first), the access address, the PDU header, the
PDU and its CRC. A record is the advertiser's when it is an advertising
packet (access address 8e89bed6) whose CRC is right (computed here, so a
corrupted packet never counts either way) and whose AdvA is ADDR with TxAdd
set (random address).

Checks (each failure is a ``FAIL:`` line and exit status 1):

- the file is PCAP or PCAPNG with link type 256 and whole records;
- at least ``--min-packets`` (and at most ``--max-packets``) of the
  advertiser's packets, every one on ``--channel``'s RF channel (0, 12, 39),
  of the PDU type ``--type`` (ADV_NONCONN_IND or ADV_IND), carrying the
  harness payload (company 0xffff, magic UTAD, format 1) with the expected
  channel map byte and tag, and a counter from ``--counter-min`` to
  ``--counter-max``;
- ``--absent``: none of the advertiser's packets at all.

With ``--type conn``, a SCAN_RSP from the advertiser's own address is counted
and reported on its own line, and only its channel is checked: an ADV_IND
advertiser must answer a scanner in range, and the test does not control who
scans. ADV_NONCONN_IND is not scannable, so with ``--type nonconn`` a
SCAN_RSP is checked like any other packet.

Only counts and the advertiser's own fields are printed, never another
device's address or data, so no nearby traffic reaches the run record.
Exit status 2 is a usage error.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import struct
import sys

LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR = 256
ADV_ACCESS_ADDRESS = 0x8E89BED6
ADV_CRC_INIT = 0x555555
ADV_RF_CHANNEL = {37: 0, 38: 12, 39: 39}
PDU_TYPES = {"conn": 0x0, "nonconn": 0x2}
SCAN_RSP = 0x4
PDU_NAMES = {0x0: "ADV_IND", 0x1: "ADV_DIRECT_IND", 0x2: "ADV_NONCONN_IND",
             0x3: "SCAN_REQ", SCAN_RSP: "SCAN_RSP", 0x5: "CONNECT_IND",
             0x6: "ADV_SCAN_IND"}
LE_PHDR_LEN = 10

# The low-energy pseudo-header, in the order libbtbb packs it: the RF
# channel, the signal and noise powers in dBm, the access-address errors,
# the reference access address, and the flag word
# (``pcap_bluetooth_le_ll_header``, its ``lib/src/pcap-common.h``).
_LE_PHDR = struct.Struct("<BbbBIH")

# Flag word bits (the same header).
LE_DEWHITENED = 0x0001
LE_SIGPOWER_VALID = 0x0002
LE_NOISEPOWER_VALID = 0x0004
LE_PACKET_DECRYPTED = 0x0008
LE_REF_AA_VALID = 0x0010
LE_AA_OFFENSES_VALID = 0x0020
LE_CHANNEL_ALIASED = 0x0040
LE_CRC_CHECKED = 0x0400
LE_CRC_VALID = 0x0800
LE_MIC_CHECKED = 0x1000
LE_MIC_VALID = 0x2000

# A dBm figure outside this range is not a reading of this receiver: the
# host's piece-wise model of the CC2400's curve bottoms out at -120 and tops
# out at 0 (``cc2400_rssi_to_dbm()`` in the fork's callback). The record has
# no way to say a level was not measured, so a packet whose reading was at
# the top of the scale carries 0 dBm here while the tool's own line reads
# ``rssi=none`` (fork SqueakyFloors/ubertooth#1192, #1198).
MIN_DBM, MAX_DBM = -120, 0
COMPANY_ID = 0xFFFF
MAGIC = b"UTAD"
PAYLOAD_FORMAT = 1
CHMAP_BITS = {"37": 0x01, "38": 0x02, "39": 0x04, "all": 0x07}
AD_MANUFACTURER = 0xFF

# Byte order and timestamp ticks per second of each PCAP magic: the original
# magic counts microseconds in a record's second field, the 2013 one
# nanoseconds. libbtbb writes the nanosecond magic. Public, because
# addresssoak.py reads a growing capture record by record and needs the same
# table to make sense of its header.
PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", 1_000_000), b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
    b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000), b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000),
}
_PCAPNG_SHB = 0x0A0D0D0A
_PCAPNG_IDB = 0x00000001
_PCAPNG_EPB = 0x00000006
_PCAPNG_SPB = 0x00000003
# A PCAPNG interface option: one byte saying how finely its records are
# timed. Without it a reader is to assume microseconds (pcapng, Section
# 4.2); libbtbb writes 9, nanoseconds.
_PCAPNG_IF_TSRESOL = 9
_PCAPNG_IDB_FIXED = 8  # link type, reserved, snaplen, before the options
DEFAULT_TICKS_PER_SECOND = 1_000_000


class CaptureError(ValueError):
    """The file is not a whole PCAP/PCAPNG capture of link type 256."""


def _reverse24(value: int) -> int:
    return int(f"{value & 0xFFFFFF:024b}"[::-1], 2)


def crc24(data: bytes, init: int = ADV_CRC_INIT) -> int:
    """The LE link-layer CRC (Core Vol 6 Part B 3.1.1) over PDU header and PDU.

    A right-shifting register holding the init with all 24 bits reversed (the
    Ubertooth firmware's `rbit`), fed least significant bit first. The
    result's three bytes, least significant first, are the CRC bytes as a
    capture stores them.

    Reversing all 24 bits and reversing each byte give the same register for
    the advertising init 0x555555, so only a connection's own CRC init tells
    them apart: of the 18 data packets of a followed connection on the bench
    (run 20261028T163546Z), the whole-24-bit reversal validated some and the
    per-byte reversal none.
    """
    state = _reverse24(init)
    for byte in data:
        for _bit in range(8):
            feedback = (state ^ byte) & 1
            byte >>= 1
            state >>= 1
            if feedback:
                state |= 1 << 23
                state ^= 0x5A6000
    return state


def _crc_bytes(header_and_pdu: bytes) -> bytes:
    return crc24(header_and_pdu).to_bytes(3, "little")


@dataclass(frozen=True)
class TimedRecord:
    """One record and when the capture tool wrote it.

    ``micros`` is microseconds on the file's own clock, so only differences
    between records mean anything. It is None for a record the file carries
    no time for (a PCAPNG simple packet block), which a check that needs
    times has to leave out rather than treat as time zero.
    """

    micros: int | None
    data: bytes


def read_records(data: bytes) -> tuple[str, int, list[bytes]]:
    """(format, link type, records) of a PCAP or PCAPNG file."""
    file_format, link_type, timed = read_timed_records(data)
    return file_format, link_type, [record.data for record in timed]


def read_timed_records(data: bytes) -> tuple[str, int, list[TimedRecord]]:
    """(format, link type, records with their times) of a PCAP or PCAPNG file."""
    if len(data) >= 24 and data[:4] in PCAP_MAGIC:
        return _read_pcap(data)
    if len(data) >= 12 and struct.unpack("<I", data[:4])[0] == _PCAPNG_SHB:
        return _read_pcapng(data)
    if not data:
        raise CaptureError("the capture file is empty")
    raise CaptureError(f"magic {data[:4].hex()} is neither PCAP nor PCAPNG")


def _micros(ticks: int, ticks_per_second: int) -> int:
    return ticks * 1_000_000 // ticks_per_second


def _read_pcap(data: bytes):
    endian, ticks_per_second = PCAP_MAGIC[data[:4]]
    link_type = struct.unpack(endian + "I", data[20:24])[0]
    records, offset = [], 24
    while offset < len(data):
        if offset + 16 > len(data):
            raise CaptureError(f"record header cut short at byte {offset}")
        sec, frac, caplen, _length = struct.unpack(endian + "IIII", data[offset:offset + 16])
        end = offset + 16 + caplen
        if end > len(data):
            raise CaptureError(f"record at byte {offset} is cut short")
        records.append(TimedRecord(
            sec * 1_000_000 + _micros(frac, ticks_per_second),
            data[offset + 16:end]))
        offset = end
    return "pcap", link_type, records


def _interface_ticks_per_second(body: bytes, endian: str) -> int:
    """How finely an interface description block says its records are timed."""
    at = _PCAPNG_IDB_FIXED
    while at + 4 <= len(body):
        code, option_length = struct.unpack(endian + "HH", body[at:at + 4])
        if code == 0:  # end of options
            break
        if at + 4 + option_length > len(body):
            break  # the option claims more than the block holds
        value = body[at + 4:at + 4 + option_length]
        if code == _PCAPNG_IF_TSRESOL and option_length >= 1:
            # The top bit chooses the base: set means 2^-n, clear means 10^-n.
            power = value[0] & 0x7F
            return 2 ** power if value[0] & 0x80 else 10 ** power
        at += 4 + 4 * ((option_length + 3) // 4)
    return DEFAULT_TICKS_PER_SECOND


def _read_pcapng(data: bytes):
    endian = "<"
    magic = data[8:12]
    if magic == b"\x4d\x3c\x2b\x1a":
        endian = "<"
    elif magic == b"\x1a\x2b\x3c\x4d":
        endian = ">"
    else:
        raise CaptureError("PCAPNG section header has no byte-order magic")
    link_types: list[int] = []
    ticks: list[int] = []
    records, offset = [], 0
    while offset < len(data):
        if offset + 12 > len(data):
            raise CaptureError(f"block cut short at byte {offset}")
        block_type, length = struct.unpack(endian + "II", data[offset:offset + 8])
        if length < 12 or length % 4 or offset + length > len(data):
            raise CaptureError(f"block at byte {offset} has a bad length {length}")
        body = data[offset + 8:offset + length - 4]
        if block_type == _PCAPNG_SHB:
            link_types = []
            ticks = []
        elif block_type == _PCAPNG_IDB:
            link_types.append(struct.unpack(endian + "H", body[:2])[0])
            ticks.append(_interface_ticks_per_second(body, endian))
        elif block_type == _PCAPNG_EPB:
            interface, high, low, caplen, _orig = struct.unpack(endian + "IIIII", body[:20])
            if interface >= len(link_types):
                raise CaptureError(f"packet block at byte {offset} names no interface")
            records.append(TimedRecord(
                _micros(high << 32 | low, ticks[interface]), body[20:20 + caplen]))
        elif block_type == _PCAPNG_SPB:
            # A simple packet block carries no timestamp at all.
            records.append(TimedRecord(None, body[4:]))
        offset += length
    if not link_types:
        raise CaptureError("PCAPNG has no interface description block")
    if len(set(link_types)) != 1:
        raise CaptureError(f"PCAPNG interfaces have several link types {sorted(set(link_types))}")
    return "pcapng", link_types[0], records


def format_address(adva: bytes) -> str:
    return ":".join(f"{byte:02X}" for byte in reversed(adva))


@dataclass
class Packet:
    rf_channel: int
    pdu_type: int
    tx_add: int
    address: str
    adv_data: bytes


def parse_advertising(record: bytes) -> Packet | None:
    """An advertising packet with a correct CRC and an AdvA, else None."""
    if len(record) < LE_PHDR_LEN + 4 + 2 + 6 + 3:
        return None
    rf_channel = record[0]
    body = record[LE_PHDR_LEN:]
    if struct.unpack("<I", body[:4])[0] != ADV_ACCESS_ADDRESS:
        return None
    header = body[4:6]
    length = header[1] & 0x3F
    if len(body) < 6 + length + 3 or length < 6:
        return None
    pdu = body[6:6 + length]
    if body[6 + length:9 + length] != _crc_bytes(header + pdu):
        return None
    pdu_type = header[0] & 0x0F
    if pdu_type not in (0x0, 0x2, 0x4, 0x6):  # PDUs that start with AdvA + AdvData
        return None
    return Packet(rf_channel, pdu_type, header[0] >> 6 & 1, format_address(pdu[:6]), pdu[6:])


def harness_payload(adv_data: bytes) -> tuple[int, int, bytes] | None:
    """(channel map byte, counter, tag) from the harness's manufacturer data."""
    offset = 0
    while offset < len(adv_data):
        length = adv_data[offset]
        if length == 0 or offset + 1 + length > len(adv_data):
            return None
        kind, value = adv_data[offset + 1], adv_data[offset + 2:offset + 1 + length]
        if (kind == AD_MANUFACTURER and len(value) >= 12
                and struct.unpack("<H", value[:2])[0] == COMPANY_ID
                and value[2:6] == MAGIC and value[6] == PAYLOAD_FORMAT):
            return value[7], struct.unpack("<I", value[8:12])[0], bytes(value[12:])
        offset += 1 + length
    return None


@dataclass(frozen=True)
class Levels:
    """The power fields of one low-energy record, and its flag word."""

    index: int
    rf_channel: int
    signal_power: int
    noise_power: int
    access_address_offenses: int
    ref_access_address: int
    flags: int

    def has(self, flag: int) -> bool:
        return bool(self.flags & flag)

    @property
    def is_data_channel_packet(self) -> bool:
        """Whether the capture recorded no reference access address.

        A tool listening to the advertising channels records 0x8e89bed6; one
        following a connection records 0 for the connection's own packets,
        because the field is defined as the address configured into the tool
        (libbtbb's ``LE_REF_AA_FLAG``). So a record with no reference address
        is a packet of a followed connection.
        """
        return self.ref_access_address == 0


def levels(records) -> list[Levels]:
    """The power fields of each record, in file order."""
    found = []
    for index, record in enumerate(records):
        if len(record) < LE_PHDR_LEN:
            continue
        found.append(Levels(index, *_LE_PHDR.unpack(record[:LE_PHDR_LEN])))
    return found


def level_problems(found: list[Levels]) -> list[str]:
    """What is wrong with the power fields of these records.

    Rules the writers hold to whatever was on the air, so a live capture is
    judged by them as a replay is. Each is one line of libbtbb's
    ``lell_pcap_append_packet()`` and ``lell_pcapng_append_packet()``, or of
    the figures the fork hands them (``determine_signal_and_noise()``):

    - every record is dewhitened, carries a signal figure, and carries a
      count of its access-address errors, because the writers set those
      three flags on every record they write;
    - the noise flag is set exactly when the noise figure is below the
      signal, which is how the writers decide it: a board with no background
      estimate for the channel is given a figure no signal can reach, so the
      flag comes out clear and a reader does not take the byte for a
      measurement;
    - each figure the flags call a measurement is inside the range the host's
      model of the radio can produce;
    - the reference access address flag is set exactly when that field holds
      an address, which is what the flag is defined to say.
    """
    problems = []
    for record in found:
        where = f"record {record.index}"
        for flag, name in ((LE_DEWHITENED, "dewhitened"),
                           (LE_SIGPOWER_VALID, "signal power"),
                           (LE_AA_OFFENSES_VALID, "access-address error")):
            if not record.has(flag):
                problems.append(f"{where}: the {name} flag is clear, and the "
                                "writers set it on every record")
        if not MIN_DBM <= record.signal_power <= MAX_DBM:
            problems.append(f"{where}: signal power {record.signal_power} dBm")
        noisy = record.noise_power < record.signal_power
        if record.has(LE_NOISEPOWER_VALID) != noisy:
            problems.append(
                f"{where}: the noise power flag is "
                f"{'set' if record.has(LE_NOISEPOWER_VALID) else 'clear'} with "
                f"noise {record.noise_power} dBm and signal "
                f"{record.signal_power} dBm")
        if record.has(LE_NOISEPOWER_VALID) and not MIN_DBM <= record.noise_power <= MAX_DBM:
            problems.append(f"{where}: noise power {record.noise_power} dBm")
        if record.has(LE_REF_AA_VALID) != (record.ref_access_address != 0):
            problems.append(
                f"{where}: the reference access address flag is "
                f"{'set' if record.has(LE_REF_AA_VALID) else 'clear'} with "
                f"reference access address {record.ref_access_address:08x}")
    return problems


def level_summary(found: list[Levels]) -> list[str]:
    """The lines describing these records' power fields."""
    if not found:
        return ["levels: no record carries a pseudo-header"]
    signals = [record.signal_power for record in found]
    measured = [record.noise_power for record in found
                if record.has(LE_NOISEPOWER_VALID)]
    lines = [f"levels: signal {min(signals)} to {max(signals)} dBm, "
             f"{len(set(signals))} distinct in {len(found)} records"]
    lines.append(
        "levels: noise "
        + (f"{min(measured)} to {max(measured)} dBm in {len(measured)} records, "
           f"{len(set(measured))} distinct" if measured
           else "no record reports a measured figure"))
    return lines


@dataclass
class Result:
    records: int = 0
    matches: int = 0
    scan_responses: int = 0
    counters: list = field(default_factory=list)
    channels: dict = field(default_factory=dict)
    levels: list = field(default_factory=list)
    problems: list = field(default_factory=list)


def check(data: bytes, *, address: str, channel: int, absent: bool = False,
          payload: str | None = None, chmap: str | None = None, adv_type: str | None = None,
          counter_min: int | None = None, counter_max: int | None = None,
          min_packets: int = 1, max_packets: int | None = None) -> tuple[str, Result]:
    result = Result()
    try:
        file_format, link_type, records = read_records(data)
    except CaptureError as error:
        result.problems.append(str(error))
        return "unreadable", result
    header = f"{file_format}, link type {link_type}"
    if link_type != LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR:
        result.problems.append(f"link type {link_type}, not {LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR}")
        return header, result
    address = address.upper()
    result.records = len(records)
    result.levels = levels(records)
    # The power fields of every record, judged by the rules the writers hold
    # to whatever was on the air. They are not about the advertiser, so they
    # are checked over the whole file rather than over the packets this case
    # was looking for.
    result.problems.extend(level_problems(result.levels))
    wrong = {}
    expected_rf = ADV_RF_CHANNEL[channel]
    for record in records:
        packet = parse_advertising(record)
        if packet is None or packet.address != address or not packet.tx_add:
            continue
        reasons = []
        if packet.rf_channel != expected_rf:
            reasons.append(f"on rf channel {packet.rf_channel}, not {expected_rf}")
        if not absent and adv_type == "conn" and packet.pdu_type == SCAN_RSP:
            # An ADV_IND advertiser is scannable, and must answer a SCAN_REQ
            # from any scanner in range with a SCAN_RSP from its own address
            # (Core spec 6.0, Vol 6 Part B 4.4.2.3). The bench has scanners
            # the test does not control, so those replies arrive at random.
            # They are not advertising events the case asked for and carry no
            # harness payload, so only the channel they arrived on is checked:
            # a SCAN_RSP goes out on the channel its SCAN_REQ came in on, so
            # one on another channel is still a leak. ADV_NONCONN_IND is not
            # scannable, so a SCAN_RSP in one of those cases is a real fault
            # and is checked like any other packet.
            result.scan_responses += 1
            for reason in reasons:
                wrong[reason] = wrong.get(reason, 0) + 1
            continue
        result.matches += 1
        result.channels[packet.rf_channel] = result.channels.get(packet.rf_channel, 0) + 1
        found = harness_payload(packet.adv_data)
        if adv_type is not None and packet.pdu_type != PDU_TYPES[adv_type]:
            reasons.append(f"{PDU_NAMES.get(packet.pdu_type, packet.pdu_type)}, not "
                           f"{PDU_NAMES[PDU_TYPES[adv_type]]}")
        if found is None:
            reasons.append("without the harness payload")
        else:
            map_byte, counter, tag = found
            result.counters.append(counter)
            if chmap is not None and map_byte != CHMAP_BITS[chmap]:
                reasons.append(f"channel map byte {map_byte:#04x}, not {CHMAP_BITS[chmap]:#04x}")
            if payload is not None and tag.hex() != payload:
                reasons.append(f"tag {tag.hex() or 'none'}, not {payload or 'none'}")
            if counter_min is not None and counter < counter_min:
                reasons.append(f"counter {counter} below {counter_min}")
            if counter_max is not None and counter > counter_max:
                reasons.append(f"counter {counter} above {counter_max}")
        for reason in reasons:
            wrong[reason] = wrong.get(reason, 0) + 1
    if absent:
        if result.matches:
            result.problems.append(
                f"{result.matches} packet(s) from {address} while it advertises on "
                f"another channel than {channel}")
        return header, result
    for reason, count in sorted(wrong.items()):
        result.problems.append(f"{count} packet(s) from {address} {reason}")
    if result.matches < min_packets:
        result.problems.append(f"{result.matches} packet(s) from {address}, fewer than "
                               f"{min_packets}")
    if max_packets is not None and result.matches > max_packets:
        result.problems.append(f"{result.matches} packet(s) from {address}, more than "
                               f"{max_packets}")
    return header, result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="advcapture.py", description=__doc__.splitlines()[0])
    parser.add_argument("file")
    parser.add_argument("--address", required=True)
    parser.add_argument("--channel", required=True, type=int, choices=sorted(ADV_RF_CHANNEL))
    parser.add_argument("--absent", action="store_true")
    parser.add_argument("--payload")
    parser.add_argument("--chmap", choices=sorted(CHMAP_BITS))
    parser.add_argument("--type", dest="adv_type", choices=sorted(PDU_TYPES))
    parser.add_argument("--counter-min", type=int)
    parser.add_argument("--counter-max", type=int)
    parser.add_argument("--min-packets", type=int, default=1)
    parser.add_argument("--max-packets", type=int)
    args = parser.parse_args(argv)
    payload = args.payload
    if payload is not None:
        payload = "" if payload.lower() == "none" else payload.lower()
    try:
        with open(args.file, "rb") as stream:
            data = stream.read()
    except OSError as error:
        print(f"FAIL: cannot read {args.file}: {error.strerror or error}")
        return 1
    header, result = check(
        data, address=args.address, channel=args.channel, absent=args.absent,
        payload=payload, chmap=args.chmap, adv_type=args.adv_type,
        counter_min=args.counter_min, counter_max=args.counter_max,
        min_packets=args.min_packets, max_packets=args.max_packets,
    )
    print(f"capture: {header}, {result.records} records")
    channels = ", ".join(f"rf channel {rf} x{count}" for rf, count in sorted(result.channels.items()))
    print(f"advertiser packets: {result.matches}" + (f" ({channels})" if channels else ""))
    if result.scan_responses:
        print(f"advertiser scan responses, not checked: {result.scan_responses}")
    if result.counters:
        print(f"counters: {min(result.counters)}-{max(result.counters)}")
    for line in level_summary(result.levels):
        print(line)
    for problem in result.problems:
        print(f"FAIL: {problem}")
    if result.problems:
        return 1
    print("capture checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
