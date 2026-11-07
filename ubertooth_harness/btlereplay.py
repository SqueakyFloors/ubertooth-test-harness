"""Synthetic low-energy dump for ``ubertooth-btle -R``, and replay checks.

``ubertooth-btle -d FILE`` saves the records the board sent and ``-R FILE``
decodes them again with no board and no connection, taking the major mode the
dump was recorded with (fork SqueakyFloors/ubertooth#1129). The records are
the format ``ubertooth-rx -d`` writes, which [`rxreplay.py`](rxreplay.py) also
builds: a big-endian 32-bit host time, then one 64-byte usb_pkt_rx
(ubertooth_interface.h). A low-energy record carries the packet's own bytes
rather than symbols -- the access address, the PDU header, the payload and the
CRC -- so a fixture is written from the Bluetooth Core specification's own
encoding and the decode is known in advance.

This module builds two such files. Nothing in either was received over the
air: every address, identifier and parameter below is made up here.

``connection`` is a promiscuous capture of one connection. It holds the five
parameter records promiscuous mode sends as it works out which connection it
has found (``LE_PROMISC``, one per state byte), the ``CONNECT_IND`` that
started the connection on an advertising channel, and two data packets on the
connection's own access address. The parameters and the CONNECT_IND are the
pair a replay has to put back: nothing else in the file says which connection
the data packets belong to.

``advertising`` is one ``ADV_IND`` whose advertising data holds three
Manufacturer Specific Data fields: an identifier the Bluetooth SIG registered
recently, one it has had since the beginning, and one it has never assigned.
libbtbb reads the identifier out of the field and names the company
(``btbb_company_name()``, the ``Company:`` line of ``lell_print()``), so the
three pin both the table and the wording for an identifier that is not in it
(SqueakyFloors/libbtbb#75 and SqueakyFloors/libbtbb#128).

A transition step runs ``python <this file> ...`` as an ordinary logged
command. It imports only the standard library, so it runs from any working
directory, and prints summaries only:

    python btlereplay.py fixture PATH [--variant connection|advertising]
    python btlereplay.py dump-check DUMP FIXTURE [--variant V]
    python btlereplay.py files DIR
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import time
from pathlib import Path

# --- the records -------------------------------------------------------------

RECORD = 4 + 64          # host time + usb_pkt_rx
DATA_LEN = 50            # usb_pkt_rx.data
LE_PACKET = 1            # a demodulated low-energy packet
LE_PROMISC = 5           # a parameter promiscuous mode recovered
# The record's channel byte counts 1 MHz steps from 2402 MHz, which is what
# libbtbb turns back into a channel index: 0 is advertising index 37, and 22
# (2424 MHz) is data index 10.
ADVERTISING_CHANNEL = 0
ADVERTISING_INDEX = 37
DATA_CHANNEL = 22
DATA_INDEX = 10
# 0.1 s of device clock between records, so each packet line reports a
# delta_t of its own.
CLK100NS_STEP = 1_000_000
FIXTURE_AGE_S = 3600     # record times are this long before generation

ADVERTISING_AA = 0x8E89BED6     # the advertising access address (Core spec)

# --- the connection the `connection` fixture carries -------------------------
#
# The access address satisfies the Core specification's rules for one on a
# data channel (Vol 6, Part B, 2.1.2): no run of more than six equal bits, no
# more than 24 transitions, at least two in the top six bits, not all four
# octets equal, and neither the advertising address nor one bit from it. So
# libbtbb counts no offenses against it and the default -x allows it.
CONNECTION_AA = 0x5A3C96D2
CRC_INIT = 0x9B4F27
HOP_INTERVAL = 24               # 1.25 ms units, so 30 ms
HOP_INCREMENT = 7
EVENT_COUNTER = 1234            # the algorithm 2 counter the follow starts on
PERIPHERAL_LATENCY = 4
SUPERVISION_TIMEOUT = 200       # 10 ms units
WINDOW_SIZE = 3
WINDOW_OFFSET = 2
CHANNEL_MAP = bytes((0xFF, 0x0F, 0xC3, 0xF0, 0x1F))
SLEEP_CLOCK_ACCURACY = 5        # 31 ppm to 50 ppm

# The two addresses the CONNECT_IND names: the central's, marked random, and
# the advertiser's, marked public, so the record pins both type bits.
INITIATOR_ADDRESS = bytes.fromhex("1a2b3c4d5e6f")
ADVERTISER_ADDRESS = bytes.fromhex("112233445566")

# One LL Control PDU and one L2CAP start, so the two data records differ in
# their LLID, sequence number and length.
VERSION_IND = bytes((0x0C, 0x08, 0x01, 0x02, 0x03, 0x04, 0x05))
L2CAP_START = bytes((0x04, 0x00, 0x05, 0x00))

# --- the company identifiers the `advertising` fixture carries ---------------
#
# Each name is the one the pinned libbtbb's table gives. A regenerated table
# that renames a company fails the case, which is a one-line update here.
RECENT_COMPANY = (0x1040, "Raspberry Pi")      # registered since 2020
ORIGINAL_COMPANY = (0x0002, "Intel Corp.")     # in the table from the start
# Far above every identifier the SIG has assigned, and not the 0xffff it
# reserves for internal use, so this one stays unassigned.
UNASSIGNED_COMPANY = (0xF0F0, "not assigned")

VARIANTS = ("connection", "advertising")


def record(host_time: int, clk100ns: int, pkt_type: int, data: bytes,
           channel: int) -> bytes:
    """One dump record: the host time it arrived, then the usb_pkt_rx."""
    if len(data) > DATA_LEN:
        raise ValueError(f"{len(data)} bytes of data, at most {DATA_LEN}")
    header = struct.pack("<BBBBIbbbB2x", pkt_type, 0, channel, 0, clk100ns,
                         -40, -90, -70, 1)
    return (struct.pack(">I", host_time) + header + data
            + bytes(DATA_LEN - len(data)))


def connect_ind() -> bytes:
    """The CONNECT_IND that starts the connection, as the board hands it over.

    An advertising PDU: the advertising access address, the header (type 5
    with the initiator's address marked random), and the payload the Core
    specification lays out -- InitA, AdvA, the connection's access address,
    the CRC seed, the transmit window, the interval, latency and timeout, the
    channel map, and the hop increment beside the clock accuracy.
    """
    payload = (
        INITIATOR_ADDRESS
        + ADVERTISER_ADDRESS
        + struct.pack("<I", CONNECTION_AA)
        + CRC_INIT.to_bytes(3, "little")
        + bytes((WINDOW_SIZE,))
        + struct.pack("<HHHH", WINDOW_OFFSET, HOP_INTERVAL,
                      PERIPHERAL_LATENCY, SUPERVISION_TIMEOUT)
        + CHANNEL_MAP
        + bytes((HOP_INCREMENT | (SLEEP_CLOCK_ACCURACY << 5),))
    )
    # Type 5 (CONNECT_IND) with TxAdd set: the initiator's address is random.
    pdu = bytes((0x05 | 0x40, len(payload))) + payload
    return struct.pack("<I", ADVERTISING_AA) + pdu + _crc()


def data_pdu(llid: int, body: bytes, *, sequence: int) -> bytes:
    """One data channel PDU on the connection's access address."""
    pdu = bytes((llid | (sequence << 3), len(body))) + body
    return struct.pack("<I", CONNECTION_AA) + pdu + _crc()


def adv_ind() -> bytes:
    """The ADV_IND whose advertising data names three company identifiers."""
    fields = [bytes((2, 0x01, 0x06))]       # Flags: general discoverable
    for (identifier, _name), data in (
        (RECENT_COMPANY, bytes((0xAB, 0xCD))),
        (ORIGINAL_COMPANY, bytes((0x01,))),
        (UNASSIGNED_COMPANY, bytes((0x7F,))),
    ):
        value = struct.pack("<H", identifier) + data
        fields.append(bytes((1 + len(value), 0xFF)) + value)
    payload = ADVERTISER_ADDRESS + b"".join(fields)
    pdu = bytes((0x00, len(payload))) + payload   # type 0: ADV_IND
    return struct.pack("<I", ADVERTISING_AA) + pdu + _crc()


def _crc() -> bytes:
    """The three CRC bytes of a packet the board has already checked.

    A replay opens no board, so nothing verifies them: the firmware is what
    checks a CRC and it sets the record's flag, which is why these are zero
    rather than computed.
    """
    return bytes(3)


# The parameters promiscuous mode recovers, as the state byte it sends each
# one with, the value in the record, and the line the tool prints for it.
PARAMETERS = (
    (0, struct.pack("<I", CONNECTION_AA),
     f"LE Promisc - Access Address: {CONNECTION_AA:08x}"),
    (1, struct.pack("<I", CRC_INIT), f"LE Promisc - CRC Init: {CRC_INIT:06x}"),
    (2, struct.pack("<H", HOP_INTERVAL),
     f"LE Promisc - Hop interval: {HOP_INTERVAL * 1.25:g} ms"),
    (3, struct.pack("<B", HOP_INCREMENT),
     f"LE Promisc - Hop increment: {HOP_INCREMENT}"),
    (4, struct.pack("<H", EVENT_COUNTER),
     f"LE Promisc - Algorithm 2 event counter: {EVENT_COUNTER}"),
)


def _connection_records(base_time: int) -> list[bytes]:
    data = [bytes((state,)) + value for state, value, _line in PARAMETERS]
    channels = [DATA_CHANNEL] * len(data)
    types = [LE_PROMISC] * len(data)
    for packet, channel in (
        (connect_ind(), ADVERTISING_CHANNEL),
        (data_pdu(3, VERSION_IND, sequence=0), DATA_CHANNEL),
        (data_pdu(2, L2CAP_START, sequence=1), DATA_CHANNEL),
    ):
        data.append(packet)
        channels.append(channel)
        types.append(LE_PACKET)
    return [
        record(base_time + index, CLK100NS_STEP * (index + 1), types[index],
               data[index], channels[index])
        for index in range(len(data))
    ]


def _advertising_records(base_time: int) -> list[bytes]:
    return [record(base_time, CLK100NS_STEP, LE_PACKET, adv_ind(),
                   ADVERTISING_CHANNEL)]


def fixture_bytes(base_time: int, variant: str = "connection") -> bytes:
    if variant == "connection":
        return b"".join(_connection_records(base_time))
    if variant == "advertising":
        return b"".join(_advertising_records(base_time))
    raise ValueError(variant)


def fixture_base_time(now=None) -> int:
    """The record host time a fixture written now should carry.

    FIXTURE_AGE_S before the fixture is written, so a packet line reporting
    the replay's own clock instead of the recorded one is obvious.
    """
    return int(time.time() if now is None else now) - FIXTURE_AGE_S


def expected_packets(base_time: int,
                     variant: str = "connection") -> list[tuple[int, int, int]]:
    """(systime, frequency, access address) of each packet line, in order.

    The parameter records carry no packet, so they appear in the report
    without a packet line of their own; the systime of each packet is the
    host time of its own record (fork ubertooth#1129).
    """
    if variant == "advertising":
        return [(base_time, 2402 + ADVERTISING_CHANNEL, ADVERTISING_AA)]
    if variant != "connection":
        raise ValueError(variant)
    first = base_time + len(PARAMETERS)
    return [
        (first, 2402 + ADVERTISING_CHANNEL, ADVERTISING_AA),
        (first + 1, 2402 + DATA_CHANNEL, CONNECTION_AA),
        (first + 2, 2402 + DATA_CHANNEL, CONNECTION_AA),
    ]


# --- what a replay prints ----------------------------------------------------

# The per-packet line, before the packet's own bytes and lell_print()'s dump.
# The level is a figure in every line of a replay of these records, and that
# is pinned rather than allowed for: the column reads `none` only where the
# board's own reading is at the top of the radio's scale (fork
# SqueakyFloors/ubertooth#1192), and these records carry a fixed strongest
# reading far below it.
PACKET_LINE = re.compile(
    r"^systime=(?P<systime>\d+) freq=(?P<freq>\d+) "
    r"addr=(?P<addr>[0-9a-f]{8}) delta_t=-?\d+\.\d{3} ms rssi=-?\d+$"
)


def connection_lines() -> list[str]:
    """Every line a ``-p`` replay of the connection fixture must print.

    The five parameter lines in the order the records hold them, then the
    CONNECT_IND decoded field by field: which connection it opened, on what
    schedule, over which channels. Written as libbtbb writes them
    (lell_print(), bluetooth_le_packet.c), so a change to the wording or to
    the value of any field fails the case.
    """
    advertiser = ":".join(f"{byte:02x}" for byte in ADVERTISER_ADDRESS[::-1])
    initiator = ":".join(f"{byte:02x}" for byte in INITIATOR_ADDRESS[::-1])
    return [line for _state, _value, line in PARAMETERS] + [
        f"Advertising / AA {ADVERTISING_AA:08x} (valid)/ "
        f"{len(connect_ind()) - 4 - 2 - 3:2d} bytes",
        f"    Channel Index: {ADVERTISING_INDEX}",
        "    Type:  CONNECT_REQ",
        f"    InitA: {initiator} (random)",
        f"    AdvA:  {advertiser} (public)",
        f"    AA:    {CONNECTION_AA:08x}",
        f"    CRCInit: {CRC_INIT:06x}",
        f"    WinSize: {WINDOW_SIZE:02x} ({WINDOW_SIZE})",
        f"    WinOffset: {WINDOW_OFFSET:04x} ({WINDOW_OFFSET})",
        f"    Interval: {HOP_INTERVAL:04x} ({HOP_INTERVAL})",
        f"    Latency: {PERIPHERAL_LATENCY:04x} ({PERIPHERAL_LATENCY})",
        f"    Timeout: {SUPERVISION_TIMEOUT:04x} ({SUPERVISION_TIMEOUT})",
        "    ChM: " + " ".join(f"{byte:02x}" for byte in CHANNEL_MAP),
        f"    Hop: {HOP_INCREMENT}",
        f"    SCA: {SLEEP_CLOCK_ACCURACY}, 31 ppm to 50 ppm",
        f"Data / AA {CONNECTION_AA:08x} (valid) / {len(VERSION_IND):2d} bytes",
        f"    Channel Index: {DATA_INDEX}",
        "    LLID: 3 / LL Control PDU",
        "    Opcode: 12 / LL_VERSION_IND",
        f"Data / AA {CONNECTION_AA:08x} (valid) / {len(L2CAP_START):2d} bytes",
        "    LLID: 2 / LL Data PDU / L2CAP start",
    ]


def company_lines() -> list[str]:
    """Every line an ``-n`` replay of the advertising fixture must print.

    The advertisement's own heading, then one Manufacturer Specific Data
    field per company identifier: the name for each that the SIG has
    registered, and libbtbb's wording for the one it has not.
    """
    advertiser = ":".join(f"{byte:02x}" for byte in ADVERTISER_ADDRESS[::-1])
    lines = [
        f"Advertising / AA {ADVERTISING_AA:08x} (valid)/ "
        f"{len(adv_ind()) - 4 - 2 - 3:2d} bytes",
        "    Type:  ADV_IND",
        f"    AdvA:  {advertiser} (public)",
    ]
    for _identifier, name in (RECENT_COMPANY, ORIGINAL_COMPANY,
                              UNASSIGNED_COMPANY):
        lines += ["        Type ff (Manufacturer Specific Data)",
                  f"           Company: {name}"]
    return lines


def lines_check(expected: list[str]):
    """A Step.output_check: these lines, in this order, among the output.

    Other lines may come between them -- the packet lines, the bytes of each
    packet, and the rest of lell_print()'s dump -- so this says what the
    decode must contain and in what order, and names the first line missing.
    """

    def check(output) -> str:
        lines = [line.rstrip() for line in text_lines(output)]
        position = 0
        for wanted in expected:
            while position < len(lines) and lines[position] != wanted:
                position += 1
            if position == len(lines):
                return f"no {wanted!r} line, or it came out of order"
            position += 1
        return ""

    return check


def packets_check(expected: list[tuple[int, int, int]]):
    """A Step.output_check: exactly these packet lines, in this order."""

    def check(output) -> str:
        seen = [
            (int(m["systime"]), int(m["freq"]), int(m["addr"], 16))
            for m in map(PACKET_LINE.match, text_lines(output)) if m
        ]
        if seen == expected:
            return ""
        if len(seen) != len(expected):
            return f"{len(seen)} packet lines, expected {len(expected)}"
        for index, (got, want) in enumerate(zip(seen, expected)):
            if got != want:
                return (f"packet line {index + 1}: systime {got[0]} freq "
                        f"{got[1]} addr {got[2]:08x}, expected systime "
                        f"{want[0]} freq {want[1]} addr {want[2]:08x}")
        return "packet lines differ"  # pragma: no cover

    return check


def all_checks(*checks):
    """One Step.output_check that applies each of ``checks`` in turn.

    The first problem is what the step reports, so one replay can be judged
    on both its packet lines and its decoded fields without running twice.
    """

    def check(output) -> str:
        lines = text_lines(output)
        for one in checks:
            problem = one(lines)
            if problem:
                return problem
        return ""

    return check


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


# --- command line ------------------------------------------------------------


def dump_check(dump: Path, fixture: Path) -> str:
    """'' when a ``-d`` file holds the replayed records, byte for byte.

    A dump written during a replay rewrites every record it read, the
    parameter records included: they are the only note of which connection a
    promiscuous capture found, so a dump that dropped them would lose what
    the capture was for.
    """
    data = dump.read_bytes()
    want = fixture.read_bytes()
    if data == want:
        return ""
    got, trailing = divmod(len(data), RECORD)
    if trailing:
        return f"{trailing} trailing bytes after {got} records"
    if len(data) != len(want):
        return f"{got} records, expected {len(want) // RECORD}"
    for index in range(got):
        start = index * RECORD
        if data[start:start + RECORD] != want[start:start + RECORD]:
            return f"record {index} differs from the fixture record it came from"
    return "records differ"  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="btlereplay.py")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("fixture", help="write the synthetic dump")
    make.add_argument("path", type=Path)
    make.add_argument("--variant", choices=VARIANTS, default="connection")
    make.add_argument("--base-time", type=int, default=None)
    dump = sub.add_parser("dump-check", help="compare a -d file with the dump")
    dump.add_argument("dump", type=Path)
    dump.add_argument("fixture", type=Path)
    files = sub.add_parser("files", help="list a directory's files and sizes")
    files.add_argument("directory", type=Path)
    args = parser.parse_args(argv)

    if args.command == "fixture":
        base = args.base_time
        if base is None:
            base = fixture_base_time()
        data = fixture_bytes(base, args.variant)
        args.path.write_bytes(data)
        print(f"BLE replay fixture: {args.variant}, {len(data) // RECORD} "
              f"records, {len(data)} bytes, first systime={base}")
        return 0
    if args.command == "dump-check":
        for path in (args.dump, args.fixture):
            if not path.exists():
                print(f"BLE replay dump problem: {path} does not exist")
                return 1
        problem = dump_check(args.dump, args.fixture)
        if problem:
            print(f"BLE replay dump problem: {problem}")
            return 1
        print(f"BLE replay dump: {len(args.dump.read_bytes()) // RECORD} "
              "records match the dump replayed")
        return 0
    entries = sorted(args.directory.iterdir()) if args.directory.is_dir() else []
    for entry in entries:
        size = entry.stat().st_size if entry.is_file() else "dir"
        print(f"{entry.name}: {size}")
    print(f"BLE replay files: {len(entries)} in {args.directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
