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
access code that cb_rx must skip.

A transition step runs ``python <this file> ...`` as an ordinary logged
command. It imports only the standard library, so it runs from any working
directory, and prints summaries only:

    python rxreplay.py fixture PATH [--variant full|truncated|empty]
    python rxreplay.py dump-check DUMP FIXTURE [--lap HEX]
    python rxreplay.py files DIR
    python rxreplay.py only-fixture DIR NAME --base-time T [--variant V]

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
from dataclasses import dataclass
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
    """Whitening bits for a packet sent at master clock CLK (CLK1-6 used).

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


def dh1_payload_bits(body: bytes, uap: int, clock: int) -> list[int]:
    """A DH1 payload: 1-byte payload header (LLID 2, flow 1, length), body,
    CRC; no FEC; whitened on from where the packet header stopped."""
    header = 2 | (1 << 2) | (len(body) << 3)
    bits = [(byte >> i) & 1 for byte in bytes([header]) + body for i in range(8)]
    crc = crc16(bits, uap)
    bits += [(crc >> k) & 1 for k in range(16)]
    white = whitening(clock, 18 + len(bits))[18:]
    return [bit ^ w for bit, w in zip(bits, white)]


# --- usb_pkt_rx records --------------------------------------------------------

SYMBOLS = 400            # 50 data bytes, unpacked MSB first
RECORD = 4 + 64          # host time + usb_pkt_rx
# The truncated variant cuts this many bytes off its last record.
TRUNCATED_BYTES = 10
PREAMBLE_AT = 16         # where the access code starts in the 400 symbols
SYNC_AT = PREAMBLE_AT + 4
BR_PACKET, LE_PACKET = 0, 1
DISCARD = 0x20
POLL, DH1 = 1, 4
DH1_BODY = bytes(range(4))      # a made-up 4-byte body

LAP_A, UAP_A = 0x5A3C96, 0x6B   # piconet with headers: LAP and UAP decode
LAP_B = 0x0F1E2D                # ID packets only: LAP decodes, no UAP
LAP_DECOY = 0x3E1D07            # only in records cb_rx must skip
FIXTURE_AGE_S = 3600            # record times are this long before generation


@dataclass(frozen=True)
class Record:
    lap: int | None             # None: no access code at all
    channel: int
    clock: int                  # master CLK (312.5 us units)
    kind: str = "poll"          # poll, dh1 (header and CRC-checked payload), id
    flip: int | None = None     # sync-word bit to invert (an access-code error)
    pkt_type: int = BR_PACKET
    status: int = 0
    decoy: bool = False         # cb_rx must skip it (or it holds no packet)


def _records() -> tuple[Record, ...]:
    # Master slots (CLK1-0 = 0); the clock steps vary. Headers alone leave
    # CLK6 ambiguous, so the DH1 CRC is what pins the UAP.
    a = [
        (3, 0x2468, "poll"), (17, 0x246C, "poll"), (40, 0x2478, "poll"),
        (62, 0x2480, "dh1"), (5, 0x2494, "poll"), (78, 0x2498, "poll"),
        (22, 0x24A8, "poll"), (51, 0x24C4, "poll"),
    ]
    A = [Record(LAP_A, ch, clk, kind) for ch, clk, kind in a]
    B = [Record(LAP_B, 9, 0x3000 + 4 * i, "id", flip=5 if i == 2 else None)
         for i in range(3)]
    return (
        A[0], A[1], A[2], A[3], B[0],
        Record(LAP_DECOY, 12, 0x4000, pkt_type=LE_PACKET, decoy=True),
        A[4], B[1],
        Record(LAP_DECOY, 12, 0x4004, status=DISCARD, decoy=True),
        A[5],
        Record(LAP_DECOY, 79, 0x4008, decoy=True),
        B[2], A[6],
        Record(None, 30, 0x5000, decoy=True),
        A[7],
    )


RECORDS = _records()


def _symbols(record: Record) -> list[int]:
    symbols = [0] * PREAMBLE_AT
    if record.lap is not None:
        bits = access_code(record.lap, trailer=record.kind != "id")
        if record.flip is not None:
            bits[4 + record.flip] ^= 1
        symbols += bits
        seqn = (record.clock >> 2) & 1
        if record.kind == "id":
            # After an ID packet: symbols that are not a repetition-coded header.
            symbols += [0, 1] * 30
        elif record.kind == "dh1":
            symbols += header_bits(1, DH1, 0b001 | (seqn << 2), UAP_A, record.clock)
            symbols += dh1_payload_bits(DH1_BODY, UAP_A, record.clock)
        else:
            symbols += header_bits(1, POLL, 0b001 | (seqn << 2), UAP_A, record.clock)
    symbols += [0] * (SYMBOLS - len(symbols))
    return symbols[:SYMBOLS]


def record_bytes(record: Record, host_time: int) -> bytes:
    symbols = _symbols(record)
    data = bytes(
        sum(bit << (7 - j) for j, bit in enumerate(symbols[i:i + 8]))
        for i in range(0, SYMBOLS, 8)
    )
    # cb_rx: clkn = (clkn_high << 20) + (clk100ns + offset*10 - 4000) / 3125,
    # offset being the sync word's first symbol. Leave 1000 (100 ns) of slack.
    clk100ns = record.clock * 3125 + 4000 - SYNC_AT * 10 + 1000
    header = struct.pack(
        "<BBBBIbbbB2x", record.pkt_type, record.status, record.channel, 0,
        clk100ns, -40, -90, -70, 1,
    )
    return struct.pack(">I", host_time) + header + data


def fixture_bytes(base_time: int, variant: str = "full") -> bytes:
    if variant == "empty":
        return b""
    data = b"".join(
        record_bytes(record, base_time + index) for index, record in enumerate(RECORDS)
    )
    if variant == "truncated":
        return data[:-TRUNCATED_BYTES]  # the last record loses its last bytes
    return data


def expected_packets(variant: str = "full", lap: int | None = None,
                     max_errors: int = 2) -> list[tuple[int, int, int]]:
    """(LAP, access-code errors, channel) of each packet line, in order."""
    records = list(RECORDS)
    if variant == "empty":
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
        out.append((record.lap, errors, record.channel))
    return out


def packet_record_indices(lap: int | None = None) -> list[int]:
    return [
        index for index, record in enumerate(RECORDS)
        if not record.decoy and (lap is None or record.lap == lap)
    ]


# --- output checks ------------------------------------------------------------

# The noise figure and the signal-to-noise ratio beside it are either both
# figures or both the word `none`: one comes from the other, so a line with one
# of each is malformed and does not count as a packet. The fixture's records
# set neither of the status bits that say the board measured this packet's
# power or estimated its channel's background, so a current ubertooth-rx prints
# `none` for both (fork 532adc19); a record from a board that does set them
# prints figures.
_NOISE_AND_SNR = r"(?:n=-?\d+ snr=-?\d+|n=none snr=none)"

PACKET_LINE = re.compile(
    rf"^systime=(?P<systime>\d+) ch=\s*(?P<ch>\d+) LAP=(?P<lap>[0-9a-f]{{6}}) "
    rf"err=(?P<err>\d+) clkn=\d+ clk_offset=\d+ s=-?\d+ {_NOISE_AND_SNR}$"
)
UAP_LINE = re.compile(r"UAP = 0x(?P<uap>[0-9a-f]+) found after \d+ total packets")
# libbtbb works the UAP out from the packets' CRCs and says so. Since libbtbb
# bf878df2 it says it on standard error, like every note a library makes, so
# the line cannot land in a capture written to standard output.
ANY_UAP_NOTE = UAP_LINE.pattern


def uap_note(uap: int) -> str:
    """libbtbb's note that it worked this UAP out, as the library writes it."""
    return rf"UAP = 0x{uap:x} found after \d+ total packets"


def packets_check(expected: list[tuple[int, int, int]]):
    """A Step.output_check: exactly these packet lines, in this order."""

    def check(output: str) -> str:
        seen = [
            (int(m["lap"], 16), int(m["err"]), int(m["ch"]))
            for m in map(PACKET_LINE.match, output.splitlines()) if m
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
                        f"ch {got[2]}, expected LAP {want[0]:06x} err {want[1]} "
                        f"ch {want[2]}")
        return "packet lines differ"  # pragma: no cover

    return check


def uap_check(expected: list[tuple[int, int, int]]):
    """Packet lines as expected, and no UAP note among them.

    The note is libbtbb's, and standard output holds the packet lines (or a
    capture), so the note belongs on standard error. Which UAP the library
    found, or that it found none, is checked by the step's patterns, which
    read standard error too.
    """
    packets = packets_check(expected)

    def check(output: str) -> str:
        problem = packets(output)
        if problem:
            return problem
        found = sorted({m["uap"] for m in UAP_LINE.finditer(output)})
        if found:
            return "libbtbb's note about UAP " + ", ".join(found) + " is on stdout"
        return ""

    return check


def survey_check(expected_lines: set[str]):
    def check(output: str) -> str:
        lines = output.splitlines()
        if "Survey Results" not in lines:
            return "no 'Survey Results' line"
        results = {line for line in lines[lines.index("Survey Results") + 1:] if line}
        if results != expected_lines:
            return ("survey listed " + ", ".join(sorted(results)) + "; expected "
                    + ", ".join(sorted(expected_lines)))
        return ""

    return check


def systime_check(output: str, recorded: list[int]) -> str:
    times = [int(m["systime"]) for m in map(PACKET_LINE.match, output.splitlines()) if m]
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


def dump_check(dump: Path, fixture: Path, lap: int | None = None) -> str:
    """'' when the -d output is the fixture's packet records, byte for byte."""
    data = dump.read_bytes()
    source = fixture.read_bytes()
    want = b"".join(
        source[i * RECORD:(i + 1) * RECORD] for i in packet_record_indices(lap)
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
    make.add_argument("--variant", choices=("full", "truncated", "empty"), default="full")
    make.add_argument("--base-time", type=int, default=None)
    dump = sub.add_parser("dump-check", help="compare a -d file with the fixture")
    dump.add_argument("dump", type=Path)
    dump.add_argument("fixture", type=Path)
    dump.add_argument("--lap", type=lambda text: int(text, 16), default=None)
    files = sub.add_parser("files", help="list a directory's files and sizes")
    files.add_argument("directory", type=Path)
    only = sub.add_parser("only-fixture", help="the directory holds only the fixture")
    only.add_argument("directory", type=Path)
    only.add_argument("name")
    only.add_argument("--base-time", type=int, required=True)
    only.add_argument("--variant", choices=("full", "truncated", "empty"), default="full")
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
        problem = dump_check(args.dump, args.fixture, args.lap)
        if problem:
            print(f"Replay dump problem: {problem}")
            return 1
        print(f"Replay dump: {len(packet_record_indices(args.lap))} records match the fixture")
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
