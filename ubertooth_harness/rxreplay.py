"""Synthetic raw receive fixture for ``ubertooth-rx -i``, and replay checks.

``ubertooth-rx -i FILE`` decodes a saved capture without opening a device.
stream_rx_file() in libubertooth (ubertooth.c) reads the "full" record format
that ``ubertooth-rx -d`` and ``ubertooth-dump`` write: a big-endian 32-bit
host time, then one 64-byte usb_pkt_rx (ubertooth_interface.h), until a short
read. Each record goes to cb_rx() (ubertooth_callback.c), which skips records
that are not BR_PACKET, carry the DISCARD status, or name a channel index
above 78, then unpacks the 50 data bytes into 400 symbols (most significant
bit first) and searches them for one access code with libbtbb.

This module builds such a file from scratch. Nothing in it was received over
the air: the access codes, headers, clocks, and times are computed here from
the Bluetooth Core specification (Vol 2, Part B: sync word 6.3.3, HEC 7.1.1,
whitening 7.2, FEC 1/3 7.4), in the bit order libbtbb decodes. The fixture
holds a piconet that shows LAP_A and, over several POLL packets at different
clocks, lets libbtbb derive UAP_A; ID packets (no header) for LAP_B, one with
a single access-code bit error; and decoy records with a valid LAP_DECOY
access code that cb_rx must skip. Each record's timestamp also puts its
access code a chosen distance past a CLKN edge, so every packet line's
clock offset is a figure computed here rather than merely a number.

A transition step runs ``python <this file> ...`` as an ordinary logged
command. It imports only the standard library, so it runs from any working
directory, and prints summaries only:

    python rxreplay.py fixture PATH [--variant full|truncated|empty|partial]
    python rxreplay.py dump-check DUMP FIXTURE [--lap HEX] [--variant V]
    python rxreplay.py files DIR
    python rxreplay.py only-fixture DIR NAME --base-time T [--variant V]

The ``partial`` variant is one record cut short and nothing else, so a replay
of it reads no whole record at all, and ``skipped`` is one whole record the
decoder skips followed by one cut short, so a replay of it read a record and
made no packet of it. The Failed capture files cases use both: a failed replay
keeps its capture files from the first whole record, whatever the decoder made
of it, and takes them back before that.

``only-fixture`` passes when DIR holds NAME and nothing else, and NAME still
holds the fixture written with that base time and variant: the directory as
it was before the command under test ran.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

# --- Bluetooth baseband encoding ---------------------------------------------

# The 64-bit PN sequence p0..p63 (bit i is p_i) that the sync word is
# scrambled with, and the (64,30) expurgated BCH generator, octal 260534236651.
PN = 0x83848D96BBCC54FC
BCH_GENERATOR = 0o260534236651
# Barker sequences a24..a29 appended to the LAP, chosen by a23 (bit i is a_(24+i)).
BARKER = {0: 0b101100, 1: 0b010011}


def sync_word(lap: int) -> int:
    """The 64-bit sync word for a LAP; bit i is the i-th symbol on air."""
    lap &= 0xFFFFFF
    info = lap | (BARKER[lap >> 23] << 24)
    scrambled = info ^ (PN >> 34)
    remainder = scrambled << 34
    for bit in range(63, 33, -1):
        if remainder >> bit & 1:
            remainder ^= BCH_GENERATOR << (bit - 34)
    return ((scrambled << 34) | remainder) ^ PN


def hec(header10: int, uap: int) -> int:
    """The 8-bit header error check of 10 header bits (LSB first) for a UAP.

    The LFSR D^8+D^7+D^5+D^2+D+1 starts with UAP0 in the leftmost cell; bit k
    of the result is the k-th HEC symbol sent.
    """
    register = [(uap >> i) & 1 for i in range(8)]
    for i in range(10):
        feedback = ((header10 >> i) & 1) ^ register[7]
        register = [feedback] + register[:7]
        for cell in (1, 2, 5, 7):
            register[cell] ^= feedback
    return sum(bit << k for k, bit in enumerate(reversed(register)))


def whitening(clock: int, length: int) -> list[int]:
    """Whitening bits for a packet sent at central clock CLK (CLK1-6 used).

    The LFSR D^7+D^4+1 is loaded with CLK1..CLK6 and a one in the top cell.
    """
    register = [(clock >> (k + 1)) & 1 for k in range(6)] + [1]
    out = []
    for _ in range(length):
        bit = register[6]
        out.append(bit)
        register = [bit] + register[:6]
        register[4] ^= bit
    return out


def header_bits(lt_addr: int, packet_type: int, flags: int, uap: int,
                clock: int) -> list[int]:
    """The 54 header symbols: 18 bits, whitened with CLK1-6, each sent 3 times."""
    data = (lt_addr & 7) | ((packet_type & 0xF) << 3) | ((flags & 7) << 7)
    bits = [(data >> i) & 1 for i in range(10)]
    bits += [(hec(data, uap) >> i) & 1 for i in range(8)]
    white = whitening(clock, 18)
    return [bit ^ w for bit, w in zip(bits, white) for _ in range(3)]


def access_code(lap: int, *, trailer: bool) -> list[int]:
    """Preamble, sync word, and (for packets with a header) the trailer."""
    sync = sync_word(lap)
    bits = [(sync >> i) & 1 for i in range(64)]
    preamble = [1, 0, 1, 0] if bits[0] else [0, 1, 0, 1]
    tail = [bits[63] ^ 1, bits[63], bits[63] ^ 1, bits[63]] if trailer else []
    return preamble + bits + tail


def crc16(bits: list[int], uap: int) -> int:
    """The payload CRC (D^16+D^12+D^5+1) over air-order bits, the register
    preset from the UAP; bit k of the result is the k-th CRC symbol sent."""
    register = int(f"{uap:08b}"[::-1], 2) << 8
    for bit in bits:
        register = (register >> 1) | (((register & 1) ^ bit) << 15)
        if register & 0x8000:
            register ^= 0x0408
    return register


# How far into the whitening sequence a packet's payload starts: the sequence
# runs once over the header and the payload with no reload between them
# (Bluetooth Core, Vol 2, Part B, Section 7.2), and the header is 18 bits
# before the repetition code triples them (libbtbb's HEADER_WHITENING_BITS).
HEADER_WHITENING_BITS = 18
# The DV packet's voice field, which comes before its data field and is
# whitened with it, so the data field starts this much further in again
# (Vol 2, Part B, Section 6.5.4.4; libbtbb's DV_VOICE_BITS).
DV_VOICE_BITS = 80
# The rate 2/3 FEC code of Vol 2, Part B, Section 7.4: a (15,10) shortened
# Hamming code, as the generator rows libbtbb encodes with (fec23_gen_matrix).
FEC23_GENERATOR = (0x2C01, 0x5802, 0x1C04, 0x3808, 0x7010,
                   0x4C20, 0x3440, 0x6880, 0x7D00, 0x5600)


def _whitened(bits: list[int], clock: int, skip: int) -> list[int]:
    """``bits`` whitened from ``skip`` bits into the packet's sequence."""
    white = whitening(clock, skip + len(bits))[skip:]
    return [bit ^ w for bit, w in zip(bits, white)]


def dh1_payload_bits(body: bytes, uap: int, clock: int) -> list[int]:
    """A DH1 payload: 1-byte payload header (LLID 2, flow 1, length), body,
    CRC; no FEC; whitened on from where the packet header stopped."""
    header = 2 | (1 << 2) | (len(body) << 3)
    bits = [(byte >> i) & 1 for byte in bytes([header]) + body for i in range(8)]
    crc = crc16(bits, uap)
    bits += [(crc >> k) & 1 for k in range(16)]
    return _whitened(bits, clock, HEADER_WHITENING_BITS)


def fec23(bits: list[int]) -> list[int]:
    """``bits`` as the rate 2/3 FEC code carries them: 15 symbols per 10 bits.

    The code sends each block of ten bits as it stands followed by five check
    symbols, and the last block is padded out with zeros, which is the length
    libbtbb's decoder reads back (unfec23 rounds a length up to a whole
    block).
    """
    padded = list(bits) + [0] * (-len(bits) % 10)
    out: list[int] = []
    for start in range(0, len(padded), 10):
        block = padded[start:start + 10]
        data = sum(bit << index for index, bit in enumerate(block))
        codeword = 0
        for index in range(10):
            if data >> index & 1:
                codeword ^= FEC23_GENERATOR[index]
        check = codeword >> 10
        out += block + [(check >> k) & 1 for k in range(5)]
    return out


def aux1_payload_bits(body: bytes, clock: int) -> list[int]:
    """An AUX1 payload: 1-byte payload header and body, and no CRC at all.

    AUX1 is the one type with a payload header and nothing after the body
    (Vol 2, Part B, Section 6.5.4.7), so a reader that expects every payload
    header to be followed by a CRC counts two bytes too many here. No FEC,
    like the DH types it is decoded with.
    """
    header = 2 | (1 << 2) | (len(body) << 3)
    bits = [(byte >> i) & 1 for byte in bytes([header]) + body for i in range(8)]
    return _whitened(bits, clock, HEADER_WHITENING_BITS)


def ev3_payload_bits(body: bytes, uap: int, clock: int) -> list[int]:
    """An EV3 payload: the body and its CRC, with no payload header.

    An eSCO payload's length is agreed when the link is set up and is not in
    the packet (Vol 2, Part B, Section 6.5.3), so the decoder unwhitens a
    byte at a time and takes the length whose CRC agrees. No FEC: an EV3
    carries its payload as it stands.
    """
    bits = [(byte >> i) & 1 for byte in body for i in range(8)]
    crc = crc16(bits, uap)
    bits += [(crc >> k) & 1 for k in range(16)]
    return _whitened(bits, clock, HEADER_WHITENING_BITS)


def dv_payload_bits(voice: bytes, body: bytes, uap: int,
                    clock: int) -> list[int]:
    """A DV payload: the voice field, then the data field under rate 2/3 FEC.

    The voice field is whitened and sent as it stands; the data field that
    follows it is a DM1-style payload (1-byte payload header, body, CRC)
    whitened from after the voice field and then carried by the FEC code. A
    record of this packet holds the data field only, which is what the
    voice/data boundary comes down to.
    """
    voice_bits = [(byte >> i) & 1 for byte in voice for i in range(8)]
    if len(voice_bits) != DV_VOICE_BITS:
        raise ValueError(f"{len(voice_bits)} voice bits, expected {DV_VOICE_BITS}")
    header = 2 | (1 << 2) | (len(body) << 3)
    data = [(byte >> i) & 1 for byte in bytes([header]) + body for i in range(8)]
    crc = crc16(data, uap)
    data += [(crc >> k) & 1 for k in range(16)]
    return (_whitened(voice_bits, clock, HEADER_WHITENING_BITS)
            + fec23(_whitened(data, clock,
                              HEADER_WHITENING_BITS + DV_VOICE_BITS)))


def long_payload_bits(declared: int, clock: int) -> list[int]:
    """A DH3 payload header declaring more bytes than a record can hold.

    Only the two-byte payload header is here: the point is a payload the
    decoder is told about and has no symbols for. A record carries one
    400-symbol bank, so a DH3 claiming this many bytes cannot have arrived
    in it, and the decoder must report no payload rather than decode one out
    of whatever follows the packet in the bank (fork
    SqueakyFloors/ubertooth#754).
    """
    header = 2 | (1 << 2) | (declared << 3)
    bits = [(header >> i) & 1 for i in range(16)]
    return _whitened(bits, clock, HEADER_WHITENING_BITS)


# --- usb_pkt_rx records --------------------------------------------------------

SYMBOLS = 400            # 50 data bytes, unpacked MSB first
RECORD = 4 + 64          # host time + usb_pkt_rx
# The truncated variant cuts this many bytes off its last record.
TRUNCATED_BYTES = 10
PREAMBLE_AT = 16         # where the access code starts in the 400 symbols
SYNC_AT = PREAMBLE_AT + 4
BR_PACKET, LE_PACKET = 0, 1
DISCARD = 0x20
# The packet types the fixture carries, as the header's four type bits
# (Vol 2, Part B, Table 6.2, at the basic rate).
POLL, DH1 = 1, 4
EV3, DV, AUX1, DH3 = 7, 8, 9, 11
DH1_BODY = bytes(range(4))      # a made-up 4-byte body
# The bodies of the three rarer payloads, each a different length so that a
# record carrying one cannot be mistaken for a record carrying another.
AUX1_BODY = bytes(range(0x11, 0x16))        # 5 bytes, and no CRC after them
DV_VOICE = bytes(range(0x21, 0x2B))         # 10 bytes: the voice field
DV_BODY = bytes(range(0x31, 0x35))          # 4 bytes: the data field's body
EV3_BODY = bytes(range(0x41, 0x49))         # 8 bytes, then the CRC
# What the long record's DH3 payload header declares. A record carries one
# 400-symbol bank, which holds about 32 payload bytes at this rate, so a
# payload this long cannot have arrived in it.
LONG_DECLARED_BYTES = 100

# libubertooth's clock arithmetic (ubertooth_callback.c): a CLKN tick is
# 312.5 us, a slot is two of them, and a record's timestamp is taken at the
# end of its 400-symbol bank, so the bank's first symbol is BANK_100NS
# earlier.
CLK100NS_PER_CLKN = 3125
CLK100NS_PER_SLOT = 2 * CLK100NS_PER_CLKN
BANK_100NS = SYMBOLS * 10
# How far past a CLKN edge each record's access code sits, one step further
# per record. Every value stays under one CLKN tick, so each record's CLKN
# still comes out as the central clock it was built for, while the clock
# offset the packet line reports differs from record to record: a fixture
# with one offset throughout would pass against a decoder that printed a
# constant (harness#357). The step times the number of records has to stay
# under CLK100NS_PER_CLKN, which _records() checks.
CLK_SLACK_STEP = 125

LAP_A, UAP_A = 0x5A3C96, 0x6B   # piconet with headers: LAP and UAP decode
LAP_B = 0x0F1E2D                # ID packets only: LAP decodes, no UAP
# A third piconet, whose address part is 0x00. That is an address like any
# other, and a piconet given it on the command line has to start the clock
# search like any other; one that read the part as an absence did not
# (libbtbb SqueakyFloors/libbtbb#135, which the fork took on in
# SqueakyFloors/ubertooth#1125). Every other case states UAP_A, which took
# the same branch before and after.
LAP_C, UAP_C = 0x274E85, 0x00
# A fourth piconet, carrying the payload types a bench capture does not
# reliably produce: an AUX1, whose payload header is followed by no CRC; a
# DV, whose record holds its data field and not its voice field; an EV3,
# whose length is in no field and is found by its CRC; and a DH3 claiming a
# payload no 400-symbol bank can hold.
#
# Their own piconet, and a short one, because a header is read only while
# the library holds the piconet's CLK1-6: the hop-reversal search that
# follows winnows its candidates on the channels it sees, and the fixture's
# channels are made up rather than a real hop sequence, so a piconet of
# enough packets runs the candidates out and the search starts again --
# leaving the packets in between with no clock to read a header at. Five
# packets stay well inside that, with a POLL first because the very first
# packet of any piconet arrives before its CLK1-6 is settled.
LAP_D, UAP_D = 0x6D4B29, 0x3C
LAP_DECOY = 0x3E1D07            # only in records cb_rx must skip
FIXTURE_AGE_S = 3600            # record times are this long before generation


@dataclass(frozen=True)
class Record:
    lap: int | None             # None: no access code at all
    channel: int
    clock: int                  # central CLK (312.5 us units)
    # poll; id (no header); dh1, aux1, dv, ev3 (a payload the decoder reads);
    # long (a payload header claiming more symbols than a bank holds)
    kind: str = "poll"
    flip: int | None = None     # sync-word bit to invert (an access-code error)
    pkt_type: int = BR_PACKET
    status: int = 0
    decoy: bool = False         # cb_rx must skip it (or it holds no packet)
    slack: int = CLK_SLACK_STEP  # 100 ns from the CLKN edge to the access code
    uap: int = UAP_A            # the piconet's address part, for the HEC and CRC


def _records() -> tuple[Record, ...]:
    # Central slots (CLK1-0 = 0); the clock steps vary. Headers alone leave
    # CLK6 ambiguous, so the DH1 CRC is what pins the UAP.
    a = [
        (3, 0x2468, "poll"), (17, 0x246C, "poll"), (40, 0x2478, "poll"),
        (62, 0x2480, "dh1"), (5, 0x2494, "poll"), (78, 0x2498, "poll"),
        (22, 0x24A8, "poll"), (51, 0x24C4, "poll"),
    ]
    A = [Record(LAP_A, ch, clk, kind) for ch, clk, kind in a]
    B = [Record(LAP_B, 9, 0x3000 + 4 * i, "id", flip=5 if i == 2 else None)
         for i in range(3)]
    # The piconet whose address part is 0x00, with a DH1 among its POLLs so
    # that a run given only its LAP can settle the part for itself.
    C = [Record(LAP_C, ch, clk, kind, uap=UAP_C) for ch, clk, kind in (
        (11, 0x1200, "poll"), (29, 0x1208, "dh1"), (57, 0x1218, "poll"),
    )]
    # The piconet of rarer payloads. The POLL comes first because the first
    # packet of a piconet is read before its clock is settled.
    D = [Record(LAP_D, ch, clk, kind, uap=UAP_D) for ch, clk, kind in (
        (34, 0x0840, "poll"), (31, 0x0848, "aux1"), (44, 0x0854, "dv"),
        (7, 0x0860, "ev3"), (66, 0x0874, "long"),
    )]
    records = (
        A[0], A[1], A[2], A[3], B[0],
        Record(LAP_DECOY, 12, 0x4000, pkt_type=LE_PACKET, decoy=True),
        A[4], B[1],
        Record(LAP_DECOY, 12, 0x4004, status=DISCARD, decoy=True),
        A[5],
        Record(LAP_DECOY, 79, 0x4008, decoy=True),
        B[2], A[6],
        Record(None, 30, 0x5000, decoy=True),
        A[7], C[0], C[1], C[2], D[0], D[1], D[2], D[3], D[4],
    )
    # One slack step further per record, so no two packet lines report the
    # same clock offset -- and every one still inside a CLKN tick, so each
    # record's CLKN is the central clock it was built for.
    if CLK_SLACK_STEP * len(records) >= CLK100NS_PER_CLKN:
        raise ValueError(
            f"{len(records)} records at {CLK_SLACK_STEP} x 100 ns reach past a "
            f"CLKN tick ({CLK100NS_PER_CLKN} x 100 ns)")
    return tuple(replace(record, slack=CLK_SLACK_STEP * (index + 1))
                 for index, record in enumerate(records))


RECORDS = _records()


def record_clk100ns(record: Record) -> int:
    """The timestamp a record carries: the end of its bank, in 100 ns ticks.

    Built backwards from where the access code is to sit. cb_rx reads
    ``clkn = (clkn_high << 20) + (clk100ns + offset*10 - BANK_100NS) /
    CLK100NS_PER_CLKN``, with ``offset`` the sync word's first symbol, so a
    slack below one CLKN tick leaves CLKN at the record's own central clock.
    """
    return (record.clock * CLK100NS_PER_CLKN + BANK_100NS - SYNC_AT * 10
            + record.slack)


def clk_offset(record: Record) -> int:
    """The clock offset a packet line reports for this record.

    libubertooth's own arithmetic (cb_rx): the 100 ns from the rising edge of
    CLKN to the first bit of the access code, within one slot. Not the CLK1-6
    slot offset btbb_piconet_get_clk_offset() reports, which is a different
    quantity in different units and which the fork reads only on a live
    follow.
    """
    return ((record_clk100ns(record) + SYNC_AT * 10 + CLK100NS_PER_SLOT
             - BANK_100NS) % CLK100NS_PER_SLOT)


def _symbols(record: Record) -> list[int]:
    symbols = [0] * PREAMBLE_AT
    if record.lap is not None:
        bits = access_code(record.lap, trailer=record.kind != "id")
        if record.flip is not None:
            bits[4 + record.flip] ^= 1
        symbols += bits
        seqn = (record.clock >> 2) & 1
        flags = 0b001 | (seqn << 2)
        uap, clock = record.uap, record.clock
        if record.kind == "id":
            # After an ID packet: symbols that are not a repetition-coded header.
            symbols += [0, 1] * 30
        elif record.kind == "dh1":
            symbols += header_bits(1, DH1, flags, uap, clock)
            symbols += dh1_payload_bits(DH1_BODY, uap, clock)
        elif record.kind == "aux1":
            symbols += header_bits(1, AUX1, flags, uap, clock)
            symbols += aux1_payload_bits(AUX1_BODY, clock)
        elif record.kind == "dv":
            symbols += header_bits(1, DV, flags, uap, clock)
            symbols += dv_payload_bits(DV_VOICE, DV_BODY, uap, clock)
        elif record.kind == "ev3":
            symbols += header_bits(1, EV3, flags, uap, clock)
            symbols += ev3_payload_bits(EV3_BODY, uap, clock)
        elif record.kind == "long":
            symbols += header_bits(1, DH3, flags, uap, clock)
            symbols += long_payload_bits(LONG_DECLARED_BYTES, clock)
        else:
            symbols += header_bits(1, POLL, flags, uap, clock)
    symbols += [0] * (SYMBOLS - len(symbols))
    return symbols[:SYMBOLS]


def record_bytes(record: Record, host_time: int) -> bytes:
    symbols = _symbols(record)
    data = bytes(
        sum(bit << (7 - j) for j, bit in enumerate(symbols[i:i + 8]))
        for i in range(0, SYMBOLS, 8)
    )
    # cb_rx: clkn = (clkn_high << 20) + (clk100ns + offset*10 - 4000) / 3125,
    # offset being the sync word's first symbol. The record's own slack is
    # what the packet line reports as its clock offset (clk_offset()).
    header = struct.pack(
        "<BBBBIbbbB2x", record.pkt_type, record.status, record.channel, 0,
        record_clk100ns(record), -40, -90, -70, 1,
    )
    return struct.pack(">I", host_time) + header + data


VARIANTS = ("full", "truncated", "empty", "partial", "skipped")
# The first record of RECORDS that cb_rx skips, for the "skipped" variant.
SKIPPED_RECORD = next(index for index, record in enumerate(RECORDS) if record.decoy)


def fixture_bytes(base_time: int, variant: str = "full") -> bytes:
    if variant == "empty":
        return b""
    if variant == "partial":
        # One record, short of its last bytes, and nothing before it: a file
        # a replay reads no whole record from.
        return record_bytes(RECORDS[0], base_time)[:RECORD - TRUNCATED_BYTES]
    if variant == "skipped":
        # One whole record the decoder skips, then a record cut short: a
        # replay that read a record and made no packet of it.
        skipped = RECORDS[SKIPPED_RECORD]
        return (record_bytes(skipped, base_time)
                + record_bytes(skipped, base_time + 1)[:RECORD - TRUNCATED_BYTES])
    data = b"".join(
        record_bytes(record, base_time + index) for index, record in enumerate(RECORDS)
    )
    if variant == "truncated":
        return data[:-TRUNCATED_BYTES]  # the last record loses its last bytes
    return data


# What a replay of each variant says about the bytes left over after the last
# whole record (``ubertooth-rx``'s own wording, on standard error).
PARTIAL_RECORD_BYTES = {
    "truncated": RECORD - TRUNCATED_BYTES,
    "partial": RECORD - TRUNCATED_BYTES,
    "skipped": RECORD - TRUNCATED_BYTES,
}


def fixture_base_time(now=None) -> int:
    """The record host time a fixture written now should carry.

    FIXTURE_AGE_S before the fixture is written, which is inside rxdump.py's
    window around the file time and far enough from the replay's own clock to
    tell the two apart in a packet line.
    """
    return int(time.time() if now is None else now) - FIXTURE_AGE_S


def expected_packets(variant: str = "full", lap: int | None = None,
                     max_errors: int = 2) -> list[tuple[int, int, int, int]]:
    """(LAP, access-code errors, channel, clock offset) of each packet line.

    In the order the lines appear. The offset is the figure libubertooth
    computes from the record's own timestamp, so a packet line is checked
    against a number rather than against ``clk_offset=\\d+``.
    """
    records = list(RECORDS)
    if variant in ("empty", "partial", "skipped"):
        records = []
    elif variant == "truncated":
        records = records[:-1]
    out = []
    for record in records:
        if record.decoy:
            continue
        errors = 0 if record.flip is None else 1
        if lap is not None and record.lap != lap:
            continue
        if errors > max_errors:
            continue
        out.append((record.lap, errors, record.channel, clk_offset(record)))
    return out


def fixture_records(variant: str = "full") -> list:
    """The records a fixture of ``variant`` holds whole, in its own order."""
    if variant == "truncated":
        return list(RECORDS[:-1])      # the last record is cut short
    if variant == "skipped":
        return [RECORDS[SKIPPED_RECORD]]
    if variant in ("empty", "partial"):
        return []
    return list(RECORDS)


def packet_record_indices(lap: int | None = None,
                          variant: str = "full") -> list[int]:
    """Which of the fixture's own whole records the decoder makes a packet of.

    The positions are into the fixture of that variant, so they are what a
    ``-d`` dump of it should hold, in order.
    """
    return [
        index for index, record in enumerate(fixture_records(variant))
        if not record.decoy and (lap is None or record.lap == lap)
    ]


# The header type bits each kind of record carries. An ID packet has no
# header at all, so it is absent.
PACKET_TYPES = {"poll": POLL, "dh1": DH1, "aux1": AUX1, "dv": DV, "ev3": EV3,
                "long": DH3}


def packet_type(record: Record) -> int | None:
    """The type this record's header states, or None where it has no header."""
    return PACKET_TYPES.get(record.kind)


def payload_bytes(record: Record) -> bytes:
    """The payload bytes a decoder recovers from this record.

    Empty where there is none to recover: a POLL and an ID packet carry no
    payload, and the long record's payload header claims more symbols than
    one bank holds, so a decoder bounded to the bank it was given reports
    none.
    """
    if record.kind == "aux1":
        header = 2 | (1 << 2) | (len(AUX1_BODY) << 3)
        return bytes([header]) + AUX1_BODY
    if record.kind in ("dh1", "dv", "ev3"):
        if record.kind == "ev3":
            body = EV3_BODY               # no payload header of its own
        else:
            inner = DH1_BODY if record.kind == "dh1" else DV_BODY
            body = bytes([2 | (1 << 2) | (len(inner) << 3)]) + inner
        bits = [(byte >> i) & 1 for byte in body for i in range(8)]
        return body + crc16(bits, record.uap).to_bytes(2, "little")
    return b""


# --- output checks ------------------------------------------------------------

# The noise figure and the ratio beside it are either both figures or both
# the word `none`: the ratio comes from the noise, so a line with one of each
# is malformed and does not count as a packet. The fixture's records set no
# NOISE_ESTIMATE status bit, so there is no background for this channel and a
# current ubertooth-rx prints `none` for both (fork 532adc19).
#
# The level column is a figure in every line of a replay of this fixture, and
# that is pinned rather than allowed for: the column reads `none` only where
# the board's own reading is at the top of the radio's scale (fork
# SqueakyFloors/ubertooth#1192), and these records carry a fixed strongest
# reading far below it. A live capture can print `none` there, which is why
# receive_cases' pattern accepts it and this one does not.
_NOISE_AND_SNR = r"(?:n=-?\d+ snr=-?\d+|n=none snr=none)"

PACKET_LINE = re.compile(
    rf"^systime=(?P<systime>\d+) ch=\s*(?P<ch>\d+) LAP=(?P<lap>[0-9a-f]{{6}}) "
    rf"err=(?P<err>\d+) clkn=\d+ clk_offset=(?P<clk_offset>\d+) s=-?\d+ "
    rf"{_NOISE_AND_SNR}$"
)
UAP_LINE = re.compile(r"UAP = 0x(?P<uap>[0-9a-f]+) found after \d+ total packets")
# libbtbb works the UAP out from the packets' CRCs and says so. Since libbtbb
# bf878df2 it says it on standard error, like every note a library makes, so
# the line cannot land in a capture written to standard output.
def text_lines(source):
    """``source``'s lines: a line source as given, or a string split the way
    a saved output file is read (outputfiles.split_lines).

    Spelled out here rather than imported, because this module also runs as a
    script by path, where the package is not importable.
    """
    if not isinstance(source, str):
        return source
    lines = [line.rstrip("\r") for line in source.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


ANY_UAP_NOTE = UAP_LINE.pattern

# The heading `-z` lists its results under, on standard error (see
# survey_check). Spelled out here as PACKET_LINE is, because this module also
# runs as a script by path; receive_cases.SURVEY_HEADING is the same word.
SURVEY_HEADING = "Survey Results"


def uap_note(uap: int) -> str:
    """libbtbb's note that it worked this UAP out, as the library writes it."""
    return rf"UAP = 0x{uap:x} found after \d+ total packets"


def packets_check(expected: list[tuple[int, int, int, int]]):
    """A Step.output_check: exactly these packet lines, in this order."""

    def check(output) -> str:
        seen = [
            (int(m["lap"], 16), int(m["err"]), int(m["ch"]),
             int(m["clk_offset"]))
            for m in map(PACKET_LINE.match, text_lines(output)) if m
        ]
        if seen == expected:
            return ""
        decoys = [p for p in seen if p[0] == LAP_DECOY]
        if decoys:
            return f"decoded {len(decoys)} decoy records cb_rx must skip"
        if len(seen) != len(expected):
            return f"{len(seen)} packet lines, expected {len(expected)}"
        for index, (got, want) in enumerate(zip(seen, expected)):
            if got != want:
                return (f"packet line {index + 1}: LAP {got[0]:06x} err {got[1]} "
                        f"ch {got[2]} clk_offset {got[3]}, expected LAP "
                        f"{want[0]:06x} err {want[1]} ch {want[2]} clk_offset "
                        f"{want[3]}")
        return "packet lines differ"  # pragma: no cover

    return check


def uap_check(expected: list[tuple[int, int, int, int]]):
    """Packet lines as expected, and no UAP note among them.

    The note is libbtbb's, and standard output holds the packet lines (or a
    capture), so the note belongs on standard error. Which UAP the library
    found, or that it found none, is checked by the step's patterns, which
    read standard error too.
    """
    packets = packets_check(expected)

    def check(output) -> str:
        problem = packets(output)
        if problem:
            return problem
        found = sorted({
            match["uap"]
            for line in text_lines(output)
            for match in UAP_LINE.finditer(line)
        })
        if found:
            return "libbtbb's note about UAP " + ", ".join(found) + " is on stdout"
        return ""

    return check


def survey_check(expected_lines: set[str]):
    """Exactly ``expected_lines`` beside the packet lines on standard output.

    The results are no longer introduced on this stream: the heading goes to
    standard error so that standard output holds only the packet lines and
    the addresses a script parses (fork 1c1b41b7). So every line here that
    is not a packet line is an address the survey listed, which also makes a
    heading that arrived on this stream a listed "address" and a failure.
    The step's patterns are what read the heading on standard error.
    """
    def check(output) -> str:
        results = set()
        for line in text_lines(output):
            if not line.strip() or PACKET_LINE.match(line):
                continue
            results.add(line.strip())
        problems = []
        if SURVEY_HEADING in results:
            problems.append(
                f"the {SURVEY_HEADING!r} heading is on stdout, among the "
                "addresses a script parses")
            results.discard(SURVEY_HEADING)
        if results != expected_lines:
            problems.append("survey listed " + (", ".join(sorted(results)) or "nothing")
                            + "; expected " + ", ".join(sorted(expected_lines)))
        return "; ".join(problems)

    return check


def systime_check(output, recorded: list[int]) -> str:
    times = [
        int(m["systime"]) for m in map(PACKET_LINE.match, text_lines(output)) if m
    ]
    if times != recorded:
        first = times[0] if times else "none"
        return (f"packet lines report systime {first}, the records hold "
                f"{recorded[0] if recorded else 'none'}")
    return ""


# --- command line ---------------------------------------------------------------


def _write_fixture(path: Path, variant: str, base_time: int) -> str:
    data = fixture_bytes(base_time, variant)
    path.write_bytes(data)
    complete = len(data) // RECORD
    return (f"Replay fixture: {variant}, {complete} whole records, "
            f"{len(data)} bytes, first systime={base_time}")


def dump_check(dump: Path, fixture: Path, lap: int | None = None,
               variant: str = "full") -> str:
    """'' when the -d output is the fixture's packet records, byte for byte."""
    data = dump.read_bytes()
    source = fixture.read_bytes()
    want = b"".join(
        source[i * RECORD:(i + 1) * RECORD]
        for i in packet_record_indices(lap, variant)
    )
    if data == want:
        return ""
    got, trailing = divmod(len(data), RECORD)
    if trailing:
        return f"{trailing} trailing bytes after {got} records"
    if got != len(want) // RECORD:
        return f"{got} records, expected {len(want) // RECORD}"
    for index in range(got):
        if data[index * RECORD:(index + 1) * RECORD] != want[index * RECORD:(index + 1) * RECORD]:
            return f"record {index} differs from the fixture record it came from"
    return "records differ"  # pragma: no cover


def only_fixture(directory: Path, name: str, base_time: int, variant: str) -> str:
    """'' when ``directory`` is exactly the fixture step's output, else why not."""
    if not directory.is_dir():
        return f"{directory} is not a directory"
    extra = sorted(entry.name for entry in directory.iterdir() if entry.name != name)
    if extra:
        return "unexpected file(s) left behind: " + ", ".join(extra)
    path = directory / name
    if not path.is_file():
        return f"{name} is gone"
    if path.read_bytes() != fixture_bytes(base_time, variant):
        return f"{name} changed ({path.stat().st_size} bytes)"
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rxreplay.py")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("fixture", help="write the synthetic fixture")
    make.add_argument("path", type=Path)
    make.add_argument("--variant", choices=VARIANTS, default="full")
    make.add_argument("--base-time", type=int, default=None)
    dump = sub.add_parser("dump-check", help="compare a -d file with the fixture")
    dump.add_argument("dump", type=Path)
    dump.add_argument("fixture", type=Path)
    dump.add_argument("--lap", type=lambda text: int(text, 16), default=None)
    dump.add_argument("--variant", choices=VARIANTS, default="full")
    files = sub.add_parser("files", help="list a directory's files and sizes")
    files.add_argument("directory", type=Path)
    only = sub.add_parser("only-fixture", help="the directory holds only the fixture")
    only.add_argument("directory", type=Path)
    only.add_argument("name")
    only.add_argument("--base-time", type=int, required=True)
    only.add_argument("--variant", choices=VARIANTS, default="full")
    args = parser.parse_args(argv)

    if args.command == "fixture":
        base = args.base_time
        if base is None:
            base = int(time.time()) - FIXTURE_AGE_S
        print(_write_fixture(args.path, args.variant, base))
        return 0
    if args.command == "dump-check":
        for path in (args.dump, args.fixture):
            if not path.exists():
                print(f"Replay dump problem: {path} does not exist")
                return 1
        problem = dump_check(args.dump, args.fixture, args.lap, args.variant)
        if problem:
            print(f"Replay dump problem: {problem}")
            return 1
        print(f"Replay dump: {len(packet_record_indices(args.lap, args.variant))} "
              "records match the fixture")
        return 0
    if args.command == "only-fixture":
        problem = only_fixture(args.directory, args.name, args.base_time, args.variant)
        if problem:
            print(f"Replay directory problem: {problem}")
            return 1
        print(f"Replay directory: only {args.name}, unchanged")
        return 0
    entries = sorted(args.directory.iterdir()) if args.directory.is_dir() else []
    for entry in entries:
        size = entry.stat().st_size if entry.is_file() else "dir"
        print(f"{entry.name}: {size}")
    print(f"Replay files: {len(entries)} in {args.directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
