"""Decode and check the BR/EDR capture records ubertooth-rx writes.

A transition step runs ``python <this file> PATH [checks]`` as an ordinary
logged command. It imports only the standard library and
``ubertooth_harness.capturefile``, which walks the pcap and pcapng record
structure, so the only thing decoded here is what a BR/EDR record means.

It prints a few summary lines and then either nothing, and exits 0, or one
``BR/EDR record problem:`` line per problem, and exits 1. Numbers, flags and
packet type names are reported; captured payload bytes never are, so no
traffic of the piconet reaches the run record.

    BR/EDR records: 137 in follow.pcap
    BR/EDR LAPs: e9c5ba (137)
    BR/EDR types: NULL (70), POLL (13), DH1/2-DH1 (54)
    ...

The record is the LINKTYPE_BLUETOOTH_BREDR_BB pseudo-header both of
libbtbb's BR/EDR writers build (``pcap_bluetooth_bredr_bb_header`` in its
``lib/src/pcap-common.h``), followed by the packet's payload when the
decoder produced one. Every field below is read from those bytes; nothing is
taken from the tool's printed lines, which carry only some of this and in a
form meant for a person.

Three checks tie a record's fields to each other, so a record that mixes one
packet's header with another's metadata does not pass:

- the payload's own length field, with the packet type's header size and
  CRC, must come to exactly as many payload bytes as the record holds;
- the header's HEC, reversed, must give the reference upper address part
  (UAP) the record carries, whenever the record says the HEC passed and
  carries a reference UAP;
- the payload's CRC, recomputed with that UAP, must match the one in the
  payload, whenever the record says the CRC passed.

The last two need a reference UAP in the record. libbtbb records what it
checked whether or not the capture had an address to record, so a record can
report a passing HEC with no UAP beside it; those checks skip it rather than
guess, and a caller asks for a floor of records that carry both
(``--min-hec-valid``).

The last two need the link to be unencrypted, which the bench's ESP32 peers
are (they pair with nobody). A capture of an encrypted piconet would decode
neither payload lengths nor CRCs, so ``--payload-checks`` can be turned off
for one.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import struct
import sys

if __package__ in (None, ""):
    # Run as a script: make the package importable for the capture reader.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness.capturefile import (  # noqa: E402
    LINKTYPE_BLUETOOTH_BREDR_BB,
    CaptureFileError,
    interface_options,
    packet_records,
    read_header,
)

# The pseudo-header, in the order libbtbb packs it: the RF channel, the
# signal and noise powers in dBm, the access-code errors, the transport and
# rate nibbles, the corrected header and payload bit counts, this packet's
# LAP, the reference LAP and UAP of the capture, the packet's own 18-bit
# header, and the flag word.
_HEADER = struct.Struct("<BbbBBBhIIIH")
PSEUDO_HEADER_LEN = _HEADER.size  # 22

# Flag word bits (libbtbb, lib/src/pcap-common.h).
DEWHITENED = 0x0001
SIGPOWER_VALID = 0x0002
NOISEPOWER_VALID = 0x0004
PAYLOAD_DECRYPTED = 0x0008
REFLAP_VALID = 0x0010
PAYLOAD_PRESENT = 0x0020
CHANNEL_ALIASED = 0x0040
REFUAP_VALID = 0x0080
HEC_CHECKED = 0x0100
HEC_VALID = 0x0200
CRC_CHECKED = 0x0400
CRC_VALID = 0x0800
MIC_CHECKED = 0x1000
MIC_VALID = 0x2000

FLAG_NAMES = {
    DEWHITENED: "dewhitened", SIGPOWER_VALID: "signal",
    NOISEPOWER_VALID: "noise", PAYLOAD_DECRYPTED: "decrypted",
    REFLAP_VALID: "ref-lap", PAYLOAD_PRESENT: "payload",
    CHANNEL_ALIASED: "aliased", REFUAP_VALID: "ref-uap",
    HEC_CHECKED: "hec-checked", HEC_VALID: "hec-valid",
    CRC_CHECKED: "crc-checked", CRC_VALID: "crc-valid",
    MIC_CHECKED: "mic-checked", MIC_VALID: "mic-valid",
}

# Packet type names, as libbtbb prints them (TYPE_NAMES in its
# lib/src/bluetooth_packet.c). One name covers several types because the
# four header bits do: what a basic-rate receiver hears as an HV3 is an EV3
# or a 3-EV3 on an enhanced-rate link.
TYPE_NAMES = (
    "NULL", "POLL", "FHS", "DM1", "DH1/2-DH1", "HV1", "HV2/2-EV3",
    "HV3/EV3/3-EV3", "DV/3-DH1", "AUX1", "DM3/2-DH3", "DH3/3-DH3",
    "EV4/2-EV5", "EV5/3-EV5", "DM5/2-DH5", "DH5/3-DH5",
)
TYPE_NUMBERS = {name: number for number, name in enumerate(TYPE_NAMES)}

# The types that carry a payload header, with its size in bytes and the
# longest payload the type can hold, both as libbtbb's
# decode_payload_header() has them. A type absent from this table either
# carries no payload header (NULL, POLL, FHS, the voice types) or carries no
# payload at all.
_PAYLOAD_HEADER = {
    3: (1, 20),    # DM1
    4: (1, 30),    # DH1
    8: (1, 12),    # DV, whose voice field is not in the record's payload
    9: (1, 30),    # AUX1
    10: (2, 125),  # DM3
    11: (2, 187),  # DH3
    14: (2, 228),  # DM5
    15: (2, 343),  # DH5
}
# AUX1 is the one type with a payload header and no CRC after the body
# (Bluetooth Core, Vol 2, Part B, Section 6.5.4.7).
AUX1 = 9
NUM_BREDR_CHANNELS = 79
# The longest payload a record can hold (BREDR_MAX_PAYLOAD).
MAX_PAYLOAD = 400
# A LAP field of every bit set is libbtbb's LAP_ANY, and a UAP field of 0xff
# its UAP_ANY: the capture had no address to record.
LAP_ANY = 0xFFFFFF
UAP_ANY = 0xFF
# A dBm figure outside this range is not a reading of this receiver.
MIN_DBM, MAX_DBM = -128, 0
# The header's 18 bits are each sent three times, and the decoder takes the
# majority of each three, counting every triple that disagreed. It accepts
# the header only when fewer than a quarter of the 18 disagreed, so a header
# that decoded was mended in at most 3 places (libbtbb's unfec13()); four
# bench captures gave 0 to 3. The rate 2/3 code over a payload corrects one
# bit per 15-symbol block, and the longest payload it carries is a DM5's 228
# bytes, which is 183 blocks.
MAX_CORRECTED_HEADER_BITS = 3
MAX_CORRECTED_PAYLOAD_BITS = 183
# Transport and rate nibbles of payload_transport_rate: ubertooth-rx tells
# libbtbb the transport is unknown (BTBB_TRANSPORT_ANY) and the modulation
# basic rate (BTBB_MOD_GFSK), which is all a receive-only capture knows.
TRANSPORT_ANY = 0
MOD_GFSK = 0
# libbtbb's interface option carrying the address a capture was following
# (PCAPNG_BREDR_OPTION_BD_ADDR), and the length of its value.
OPTION_BD_ADDR = 0xD340
_BD_ADDR_LEN = 8
# More records than any test of ours captures; a file with this many is not
# read into memory.
_MAX_RECORDS = 200_000
# How many problems one run reports before it stops listing them.
_MAX_PROBLEMS = 8


class RecordError(ValueError):
    """The file is not a BR/EDR capture this module can read."""


def _reverse_bits(byte: int) -> int:
    return int(f"{byte & 0xFF:08b}"[::-1], 2)


def uap_from_hec(header_bits: int, hec: int) -> int:
    """The UAP that would make ``hec`` the right HEC for ``header_bits``.

    The header error check is an 8-bit remainder whose register starts at
    the piconet's UAP, so running it backwards recovers that UAP. This is
    libbtbb's uap_from_hec(), which is how the decoder decides whether a
    header belongs to the piconet it is following.
    """
    for bit in range(9, -1, -1):
        if hec & 0x80:
            hec ^= 0x65
        hec = ((hec << 1) & 0xFF) | (((hec >> 7) ^ (header_bits >> bit)) & 0x01)
    return _reverse_bits(hec)


def payload_crc(payload: bytes, uap: int) -> int:
    """The CRC-16 of ``payload``, with the register started at ``uap``.

    libbtbb's crcgen(), over bytes rather than one char per symbol: a
    record's payload bytes hold the air bits least significant first, which
    is the order the register takes them in.
    """
    register = (_reverse_bits(uap) << 8) & 0xFF00
    for byte in payload:
        for shift in range(8):
            register = ((register >> 1)
                        | (((register & 0x0001) ^ ((byte >> shift) & 0x01)) << 15))
            register ^= (register & 0x8000) >> 5
            register ^= (register & 0x8000) >> 12
    return register


@dataclass(frozen=True)
class Record:
    """One LINKTYPE_BLUETOOTH_BREDR_BB record."""

    index: int
    timestamp_ns: int | None
    rf_channel: int
    signal_power: int
    noise_power: int
    access_code_offenses: int
    payload_transport_rate: int
    corrected_header_bits: int
    corrected_payload_bits: int
    lap: int
    ref_lap_uap: int
    bt_header: int
    flags: int
    payload: bytes

    # --- the packet's own 18-bit header, unpacked

    @property
    def lt_addr(self) -> int:
        return self.bt_header & 0x07

    @property
    def packet_type(self) -> int:
        return (self.bt_header >> 3) & 0x0F

    @property
    def type_name(self) -> str:
        return TYPE_NAMES[self.packet_type]

    @property
    def flow(self) -> int:
        return (self.bt_header >> 7) & 0x01

    @property
    def arqn(self) -> int:
        return (self.bt_header >> 8) & 0x01

    @property
    def seqn(self) -> int:
        return (self.bt_header >> 9) & 0x01

    @property
    def hec(self) -> int:
        return (self.bt_header >> 10) & 0xFF

    @property
    def header_bits(self) -> int:
        """The ten header bits the HEC covers."""
        return self.bt_header & 0x3FF

    # --- the rest

    @property
    def transport(self) -> int:
        return (self.payload_transport_rate >> 4) & 0x0F

    @property
    def rate(self) -> int:
        return self.payload_transport_rate & 0x0F

    @property
    def ref_lap(self) -> int:
        return self.ref_lap_uap & 0xFFFFFF

    @property
    def ref_uap(self) -> int:
        return (self.ref_lap_uap >> 24) & 0xFF

    def has(self, flag: int) -> bool:
        return bool(self.flags & flag)

    @property
    def flag_names(self) -> tuple[str, ...]:
        return tuple(name for flag, name in sorted(FLAG_NAMES.items())
                     if self.flags & flag)

    @property
    def payload_header_length(self) -> int:
        """The payload's own length field plus the bytes around it, or -1.

        -1 for a type whose payload has no length field to read, and for a
        record holding too few bytes to hold one.
        """
        sizes = _PAYLOAD_HEADER.get(self.packet_type)
        if sizes is None:
            return -1
        header_bytes, longest = sizes
        if len(self.payload) < header_bytes:
            return -1
        if header_bytes == 1:
            body = (self.payload[0] >> 3) & 0x1F
        else:
            body = ((self.payload[0] >> 3) | (self.payload[1] << 5)) & 0x3FF
        crc = 0 if self.packet_type == AUX1 else 2
        return min(body + header_bytes + crc, longest)


def records(path: Path | str) -> list[Record]:
    """Every BR/EDR record of ``path``, in file order.

    The whole list is held at once, payloads and all, because the checks
    count and compare across it. A capture of the length these tests take is
    a few hundred records of a few hundred bytes; a file with more than
    ``_MAX_RECORDS`` is refused rather than read.
    """
    fmt, link_type = read_header(path)
    if link_type != LINKTYPE_BLUETOOTH_BREDR_BB:
        raise RecordError(f"{path}: {fmt} link type {link_type}, not a BR/EDR "
                          f"capture (link type {LINKTYPE_BLUETOOTH_BREDR_BB})")
    found = []
    for index, (when, data) in enumerate(packet_records(path)):
        if len(data) < PSEUDO_HEADER_LEN:
            raise RecordError(f"{path}: record {index} is {len(data)} bytes, "
                              f"shorter than the {PSEUDO_HEADER_LEN}-byte "
                              "BR/EDR pseudo-header")
        fields = _HEADER.unpack(data[:PSEUDO_HEADER_LEN])
        found.append(Record(index, when, *fields, data[PSEUDO_HEADER_LEN:]))
        if len(found) > _MAX_RECORDS:
            raise RecordError(f"{path}: more than {_MAX_RECORDS} records")
    return found


def following_address(path: Path | str) -> dict | None:
    """The address a pcapng capture recorded that it was following, or None.

    ``ubertooth-rx -l`` writes libbtbb's BD_ADDR interface option once, so a
    reader of the capture knows whose packets are in it. The option holds
    the address least significant byte first, a mask saying which bits of
    the UAP were known, and whether the non-significant address part (NAP)
    is meaningful.
    """
    for code, value in interface_options(path):
        if code != OPTION_BD_ADDR:
            continue
        if len(value) != _BD_ADDR_LEN:
            raise RecordError(f"{path}: the BD_ADDR interface option is "
                              f"{len(value)} bytes, not {_BD_ADDR_LEN}")
        address = int.from_bytes(value[:6], "little")
        return {"lap": address & 0xFFFFFF, "uap": (address >> 24) & 0xFF,
                "nap": (address >> 32) & 0xFFFF,
                "uap_mask": value[6], "nap_valid": value[7]}
    return None


# ---------------------------------------------------------------------------
# Checks


def _consistency(record: Record, *, payload_checks: bool) -> list[str]:
    """What is wrong inside one record, with nothing else to compare it to."""
    problems = []
    if record.rf_channel >= NUM_BREDR_CHANNELS:
        problems.append(f"RF channel {record.rf_channel}, and there are only "
                        f"{NUM_BREDR_CHANNELS}")
    if not record.has(DEWHITENED):
        problems.append("the dewhitened flag is clear, and the decoder "
                        "dewhitens every packet it records")
    if not record.has(SIGPOWER_VALID):
        problems.append("the signal power flag is clear")
    elif not MIN_DBM <= record.signal_power <= MAX_DBM:
        problems.append(f"signal power {record.signal_power} dBm")
    # The writers set this flag exactly when the noise figure is below the
    # signal, so a record claiming otherwise has the two out of step.
    noisy = record.noise_power < record.signal_power
    if record.has(NOISEPOWER_VALID) != noisy:
        problems.append(
            f"the noise power flag is {'set' if record.has(NOISEPOWER_VALID) else 'clear'} "
            f"with noise {record.noise_power} dBm and signal {record.signal_power} dBm")
    if record.has(NOISEPOWER_VALID) and not MIN_DBM <= record.noise_power <= MAX_DBM:
        problems.append(f"noise power {record.noise_power} dBm")
    if record.has(PAYLOAD_PRESENT) != bool(record.payload):
        problems.append(
            f"the payload flag is {'set' if record.has(PAYLOAD_PRESENT) else 'clear'} "
            f"with {len(record.payload)} payload bytes")
    if len(record.payload) > MAX_PAYLOAD:
        problems.append(f"{len(record.payload)} payload bytes, more than the "
                        f"{MAX_PAYLOAD} a record holds")
    if record.has(REFLAP_VALID) != (record.ref_lap != LAP_ANY):
        problems.append(
            f"the reference LAP flag is {'set' if record.has(REFLAP_VALID) else 'clear'} "
            f"with reference LAP {record.ref_lap:06x}")
    if record.has(REFUAP_VALID) != (record.ref_uap != UAP_ANY):
        problems.append(
            f"the reference UAP flag is {'set' if record.has(REFUAP_VALID) else 'clear'} "
            f"with reference UAP {record.ref_uap:02x}")
    if record.has(HEC_VALID) and not record.has(HEC_CHECKED):
        problems.append("the HEC is valid but was not checked")
    if record.has(CRC_VALID) and not record.has(CRC_CHECKED):
        problems.append("the CRC is valid but was not checked")
    if record.has(MIC_VALID) and not record.has(MIC_CHECKED):
        problems.append("the message integrity check is valid but was not made")
    if record.has(CRC_CHECKED) and len(record.payload) < 3:
        problems.append(f"a CRC was checked over {len(record.payload)} payload "
                        "bytes, which cannot hold a CRC and a byte for it to cover")
    if record.transport != TRANSPORT_ANY or record.rate != MOD_GFSK:
        problems.append(f"transport {record.transport} rate {record.rate}, and a "
                        f"receive-only basic-rate capture records "
                        f"{TRANSPORT_ANY}/{MOD_GFSK}")
    if not 0 <= record.corrected_header_bits <= MAX_CORRECTED_HEADER_BITS:
        problems.append(f"{record.corrected_header_bits} corrected header bits")
    if not 0 <= record.corrected_payload_bits <= MAX_CORRECTED_PAYLOAD_BITS:
        problems.append(f"{record.corrected_payload_bits} corrected payload bits")
    if record.corrected_payload_bits and not record.payload:
        problems.append(f"{record.corrected_payload_bits} corrected payload bits "
                        "with no payload in the record")

    # The three cross-field checks. Each needs the record to claim the thing
    # it checks, so a packet the decoder only guessed at is not held to it.
    if record.has(HEC_VALID) and record.has(REFUAP_VALID):
        recovered = uap_from_hec(record.header_bits, record.hec)
        if recovered != record.ref_uap:
            problems.append(
                f"the header's HEC gives UAP {recovered:02x}, but the record's "
                f"reference UAP is {record.ref_uap:02x}, and the record says the "
                "HEC passed")
    if payload_checks and record.payload:
        expected = record.payload_header_length
        if expected >= 0 and expected != len(record.payload):
            problems.append(
                f"a {record.type_name} payload whose length field says "
                f"{expected} bytes, in a record holding {len(record.payload)}")
        if record.has(CRC_VALID) and record.has(REFUAP_VALID) \
                and len(record.payload) >= 3:
            computed = payload_crc(record.payload[:-2], record.ref_uap)
            carried = int.from_bytes(record.payload[-2:], "little")
            if computed != carried:
                problems.append(
                    f"a payload CRC of {carried:04x} where UAP "
                    f"{record.ref_uap:02x} gives {computed:04x}, and the record "
                    "says the CRC passed")
    return problems


def problems(found: list[Record], *, lap: int | None = None,
             reference_lap: int | None = None, reference_uap: int | None = None,
             min_packets: int = 0, min_reference_uap: int = 0,
             min_with_payload: int = 0, min_hec_valid: int = 0,
             min_crc_valid: int = 0, min_channels: int = 0,
             types: tuple[str, ...] = (), require_types: tuple[str, ...] = (),
             payload_checks: bool = True) -> list[str]:
    """Everything wrong with these records, worst first; empty when they pass."""
    found = list(found)
    failures = []
    if len(found) < min_packets:
        failures.append(f"{len(found)} records, expected at least {min_packets}")
    if lap is not None:
        wrong = [record for record in found if record.lap != lap]
        if wrong:
            failures.append(f"{len(wrong)} records with another LAP, the first "
                            f"{wrong[0].lap:06x} in record {wrong[0].index}, "
                            f"expected {lap:06x}")
    if reference_lap is not None:
        wrong = [record for record in found if record.ref_lap != reference_lap]
        if wrong:
            failures.append(f"{len(wrong)} records whose reference LAP is not "
                            f"{reference_lap:06x}, the first "
                            f"{wrong[0].ref_lap:06x} in record {wrong[0].index}")
    if reference_uap is not None:
        wrong = [record for record in found if record.ref_uap != reference_uap]
        if wrong:
            failures.append(f"{len(wrong)} records whose reference UAP is not "
                            f"{reference_uap:02x}, the first "
                            f"{wrong[0].ref_uap:02x} in record {wrong[0].index}")
    with_uap = [record for record in found if record.has(REFUAP_VALID)]
    if len(with_uap) < min_reference_uap:
        failures.append(f"{len(with_uap)} records carry a reference UAP, "
                        f"expected at least {min_reference_uap}")
    with_payload = [record for record in found if record.payload]
    if len(with_payload) < min_with_payload:
        failures.append(f"{len(with_payload)} records carry a payload, expected "
                        f"at least {min_with_payload}")
    hec_valid = [record for record in found if record.has(HEC_VALID)]
    if len(hec_valid) < min_hec_valid:
        failures.append(f"{len(hec_valid)} records passed a header HEC, "
                        f"expected at least {min_hec_valid}")
    crc_valid = [record for record in found if record.has(CRC_VALID)]
    if len(crc_valid) < min_crc_valid:
        failures.append(f"{len(crc_valid)} records passed a payload CRC, expected "
                        f"at least {min_crc_valid}")
    channels = {record.rf_channel for record in found}
    if len(channels) < min_channels:
        failures.append(f"records on {len(channels)} channels, expected at least "
                        f"{min_channels}")
    if types:
        allowed = {TYPE_NUMBERS[name] for name in types}
        wrong = [record for record in found if record.packet_type not in allowed]
        if wrong:
            failures.append(f"{len(wrong)} records of another packet type, the "
                            f"first {wrong[0].type_name} in record "
                            f"{wrong[0].index}")
    for name in require_types:
        if not any(record.packet_type == TYPE_NUMBERS[name] for record in found):
            failures.append(f"no {name} record")
    # The per-record failures come last, because there can be one per record
    # and the caller shows only the first few: the counts and addresses above
    # are what a reader needs first.
    for record in found:
        for problem in _consistency(record, payload_checks=payload_checks):
            failures.append(f"record {record.index}: {problem}")
    return failures


def summary(found: list[Record], label: str) -> list[str]:
    """The lines describing these records. No payload bytes are in them."""
    lines = [f"BR/EDR records: {len(found)} in {label}"]
    if not found:
        return lines
    laps = Counter(f"{record.lap:06x}" for record in found)
    references = Counter(f"{record.ref_lap:06x}/{record.ref_uap:02x}"
                         for record in found)
    types = Counter(record.type_name for record in found)
    channels = sorted({record.rf_channel for record in found})
    payloads = [len(record.payload) for record in found if record.payload]
    signals = [record.signal_power for record in found]
    noises = [record.noise_power for record in found if record.has(NOISEPOWER_VALID)]
    flags = Counter(name for record in found for name in record.flag_names)
    lines.append("BR/EDR LAPs: " + _counted(laps))
    lines.append("BR/EDR reference LAP/UAP: " + _counted(references))
    lines.append("BR/EDR types: " + _counted(types))
    lines.append(f"BR/EDR channels: {len(channels)} distinct, "
                 f"{channels[0]} to {channels[-1]}")
    lines.append(
        f"BR/EDR payloads: {len(payloads)} records carry one"
        + (f", {min(payloads)} to {max(payloads)} bytes" if payloads else ""))
    lines.append(f"BR/EDR signal: {min(signals)} to {max(signals)} dBm")
    lines.append("BR/EDR noise: "
                 + (f"{min(noises)} to {max(noises)} dBm in {len(noises)} records"
                    if noises else "no record reports a measured figure"))
    lines.append("BR/EDR flags: " + _counted(flags))
    lines.append("BR/EDR times: " + _times(found))
    lines.append("BR/EDR header fields: "
                 + _counted(Counter(f"lt_addr {record.lt_addr}"
                                    for record in found))
                 + "; " + _counted(Counter(
                     f"flow {record.flow} arqn {record.arqn} seqn {record.seqn}"
                     for record in found)))
    return lines


def _times(found: list[Record]) -> str:
    """What the record timestamps did, reported and not asserted.

    Nothing here fails on them. Each one is built from the capture clock the
    board reports, and the fork notes that the firmware latches that clock
    and its period count with separate reads, so a packet caught as the
    count ticks over carries a time a whole period out, in either direction.
    Any bound tight enough to mean something would therefore fail a good
    capture now and then. Small steps backwards are ordinary too: two
    packets decoded out of one 400 microsecond bank land in either order,
    and a bench capture of 285 records had one step of 225 us.
    """
    stamped = [record.timestamp_ns for record in found
               if record.timestamp_ns is not None]
    if not stamped:
        return "no record carries one"
    steps = [after - before for before, after in zip(stamped, stamped[1:])]
    back = [-step for step in steps if step < 0]
    return (f"{len(stamped)} stamped, spanning "
            f"{(max(stamped) - min(stamped)) / 1e9:.1f} s, "
            + (f"{len(back)} stepping back, the largest by "
               f"{max(back) / 1000:.0f} us" if back else "none stepping back"))


def _counted(counter: Counter) -> str:
    return ", ".join(f"{name} ({count})"
                     for name, count in sorted(counter.items()))


def _address(parts: dict) -> str:
    """The recorded address, with the parts it does not claim to know as ``??``.

    ``ubertooth-rx -l`` knows a LAP and, with ``-u``, a UAP; it has no idea
    of the non-significant address part, so it records a zero NAP and says
    so in the option's own fields. This prints what the option claims, in
    the form ``ubertooth-rx -z`` prints a surveyed address.
    """
    nap = (f"{parts['nap'] >> 8:02X}:{parts['nap'] & 0xFF:02X}"
           if parts["nap_valid"] else "??:??")
    uap = f"{parts['uap']:02X}" if parts["uap_mask"] == 0xFF else "??"
    return (f"{nap}:{uap}:{parts['lap'] >> 16:02X}:"
            f"{(parts['lap'] >> 8) & 0xFF:02X}:{parts['lap'] & 0xFF:02X}")


def _lap(value: str) -> int:
    number = int(value, 16)
    if not 0 <= number <= 0xFFFFFF:
        raise ValueError(value)
    return number


def _uap(value: str) -> int:
    number = int(value, 16)
    if not 0 <= number <= 0xFF:
        raise ValueError(value)
    return number


def _type_name(value: str) -> str:
    if value not in TYPE_NUMBERS:
        raise ValueError(value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bredrrecord.py")
    parser.add_argument("path", type=Path)
    parser.add_argument("--lap", type=_lap,
                        help="the LAP every record must carry")
    parser.add_argument("--reference-lap", type=_lap,
                        help="the reference LAP every record must carry")
    parser.add_argument("--reference-uap", type=_uap,
                        help="the reference UAP every record must carry")
    parser.add_argument("--following-lap", type=_lap,
                        help="the LAP the pcapng interface option must name "
                             "as the address this capture was following")
    parser.add_argument("--following-uap", type=_uap,
                        help="the UAP that option must name")
    parser.add_argument("--following-uap-mask", type=_uap,
                        help="the UAP mask that option must carry")
    parser.add_argument("--min-packets", type=int, default=0)
    parser.add_argument(
        "--min-reference-uap", type=int, default=0,
        help="records that must carry a reference UAP, for a capture that "
             "recovers one as it goes rather than being given one")
    parser.add_argument("--min-with-payload", type=int, default=0)
    parser.add_argument(
        "--min-hec-valid", type=int, default=0,
        help="records whose header HEC must have passed, so a record "
             "carrying another packet's header, or none, cannot pass")
    parser.add_argument("--min-crc-valid", type=int, default=0)
    parser.add_argument("--min-channels", type=int, default=0)
    parser.add_argument("--types", default="",
                        help="comma list of the only packet type names allowed")
    parser.add_argument("--require-types", default="",
                        help="comma list of packet type names that must appear")
    parser.add_argument(
        "--no-payload-checks", action="store_true",
        help="skip the payload length and CRC checks, which need a link "
             "whose payloads are not encrypted")
    args = parser.parse_args(argv)
    try:
        types = tuple(_type_name(name)
                      for name in args.types.split(",") if name)
        require_types = tuple(_type_name(name)
                              for name in args.require_types.split(",") if name)
    except ValueError as error:
        parser.error(f"unknown packet type {error}")
    if not args.path.exists():
        print(f"BR/EDR record problem: {args.path} does not exist")
        return 1
    try:
        found = records(args.path)
        following = following_address(args.path)
    except (OSError, CaptureFileError, RecordError, struct.error) as error:
        print(f"BR/EDR record problem: {error}")
        return 1
    for line in summary(found, args.path.name):
        print(line)
    if following is not None:
        print(f"BR/EDR following: {_address(following)}, UAP mask "
              f"{following['uap_mask']:02x}, NAP "
              f"{'valid' if following['nap_valid'] else 'invalid'}")
    failures = problems(
        found, lap=args.lap, reference_lap=args.reference_lap,
        reference_uap=args.reference_uap, min_packets=args.min_packets,
        min_reference_uap=args.min_reference_uap,
        min_with_payload=args.min_with_payload,
        min_hec_valid=args.min_hec_valid,
        min_crc_valid=args.min_crc_valid, min_channels=args.min_channels,
        types=types, require_types=require_types,
        payload_checks=not args.no_payload_checks)
    wanted = (("LAP", "lap", args.following_lap),
              ("UAP", "uap", args.following_uap),
              ("UAP mask", "uap_mask", args.following_uap_mask))
    for label, key, value in wanted:
        if value is None:
            continue
        if following is None:
            failures.append("the capture records no address it was following, "
                            f"so it names no {label}")
        elif following[key] != value:
            failures.append(f"the capture was following {label} "
                            f"{following[key]:0{2 if key != 'lap' else 6}x}, "
                            f"not {value:0{2 if key != 'lap' else 6}x}")
    for failure in failures[:_MAX_PROBLEMS]:
        print(f"BR/EDR record problem: {failure}")
    if len(failures) > _MAX_PROBLEMS:
        print(f"BR/EDR record problem: and {len(failures) - _MAX_PROBLEMS} more")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
