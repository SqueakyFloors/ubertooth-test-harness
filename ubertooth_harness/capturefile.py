"""Read the format and link type of a capture file written by ubertooth-btle
or ubertooth-rx.

A transition step runs ``python <this file> PATH`` as an ordinary logged
command. It imports only the standard library, so it runs from any working
directory. It prints one line:

    Capture format: pcap, link type: 256
    Capture format: pcapng, link type: 256
    Capture file absent: PATH

With ``--packets`` it also walks the packet records (pcap record headers,
pcapng enhanced/simple/obsolete packet blocks) by their length fields and
adds ``, packets: N`` to the format line; packet bytes are still not
printed.

``packet_records()`` is the one walker over those records, and the other
readers here and in bredrrecord.py are built on it, so a capture format is
parsed in one place.

It exits 0 when it could read a header (or, with ``--absent``, when the file
does not exist), 1 otherwise. Only the file header is read; packet contents
are never printed, so no captured traffic reaches the run record.

Link types written by ubertooth-btle through libbtbb:

- ``-r`` PCAPNG: one interface, LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR (256)
- ``-q`` PCAP: LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR (256)
- ``-c`` PCAP: LINKTYPE_PPI (192), wrapping LINKTYPE_BLUETOOTH_LE_LL

and by ubertooth-rx (BR/EDR):

- ``-r`` PCAPNG and ``-q`` PCAP: LINKTYPE_BLUETOOTH_BREDR_BB (255)
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

LINKTYPE_PPI = 192
LINKTYPE_BLUETOOTH_BREDR_BB = 255
LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR = 256

_PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",  # microsecond
    b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">",  # nanosecond
}
# The two magic numbers above whose record timestamps count nanoseconds, not
# microseconds. libbtbb writes one of these.
_PCAP_NANOSECOND = frozenset({b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"})
_PCAPNG_SHB = 0x0A0D0D0A
_PCAPNG_IDB = 0x00000001
_PCAPNG_BYTE_ORDER = 0x1A2B3C4D
# Interface block option codes (pcapng, Section 4.2): the timestamp
# resolution, and the end of the option list.
IF_TSRESOL = 9
_OPTION_END = 0
# An interface block holds its 8-byte block header, the 2-byte link type, a
# reserved field and the 4-byte snapshot length, and then its options.
_IDB_OPTIONS_OFFSET = 16
# The resolution a file that names none uses: microseconds.
_DEFAULT_TSRESOL = 6


class CaptureFileError(ValueError):
    pass


def read_header(path: Path | str) -> tuple[str, int]:
    """(format, link type) of a pcap or pcapng file; the first IDB for pcapng."""
    with Path(path).open("rb") as stream:
        head = stream.read(24)
        if len(head) < 4:
            raise CaptureFileError(f"{path}: {len(head)} bytes, no capture header")
        endian = _PCAP_MAGIC.get(head[:4])
        if endian is not None:
            if len(head) < 24:
                raise CaptureFileError(f"{path}: truncated pcap header")
            return "pcap", struct.unpack(endian + "I", head[20:24])[0]
        if struct.unpack("<I", head[:4])[0] == _PCAPNG_SHB:
            return "pcapng", _pcapng_link_type(path, stream, head)
    raise CaptureFileError(f"{path}: not a pcap or pcapng file")


# Blocks are walked by their length fields, never read whole: libbtbb sizes
# its section header block in pages (32 KiB on the tested macOS host).
_MAX_BLOCKS = 1024


def _pcapng_endian(path, head: bytes) -> str:
    if len(head) < 12:
        raise CaptureFileError(f"{path}: truncated pcapng section header")
    for endian in ("<", ">"):
        if struct.unpack(endian + "I", head[8:12])[0] == _PCAPNG_BYTE_ORDER:
            return endian
    raise CaptureFileError(f"{path}: bad pcapng byte-order magic")


def _interface_block(path, stream, endian: str) -> bytes:
    """The file's first interface description block, whole."""
    offset = 0
    for _ in range(_MAX_BLOCKS):
        stream.seek(offset)
        header = stream.read(8)
        if len(header) < 8:
            break
        block_type, length = struct.unpack(endian + "II", header)
        if length < 12 or length % 4:
            raise CaptureFileError(f"{path}: bad pcapng block length {length}")
        if block_type == _PCAPNG_IDB:
            # The whole block must be present and end with its own length.
            stream.seek(offset)
            block = stream.read(length)
            if length < 20 or len(block) < length:
                raise CaptureFileError(f"{path}: truncated pcapng interface block")
            if struct.unpack(endian + "I", block[-4:])[0] != length:
                raise CaptureFileError(f"{path}: pcapng interface block trailer mismatch")
            return block
        offset += length
    raise CaptureFileError(f"{path}: no pcapng interface description block")


def _pcapng_link_type(path, stream, head: bytes) -> int:
    endian = _pcapng_endian(path, head)
    block = _interface_block(path, stream, endian)
    return struct.unpack(endian + "H", block[8:10])[0]


def interface_options(path: Path | str) -> list[tuple[int, bytes]]:
    """``[(code, value)]`` of the first pcapng interface block's options.

    An empty list for a pcap file, which has no options at all, and for an
    interface block that carries none. The option codes are pcapng's own
    (``IF_TSRESOL``) and the ones libbtbb defines for a Bluetooth capture
    (bredrrecord.OPTION_BD_ADDR).
    """
    fmt, _link_type = read_header(path)
    if fmt != "pcapng":
        return []
    with Path(path).open("rb") as stream:
        endian = _pcapng_endian(path, stream.read(24))
        block = _interface_block(path, stream, endian)
    options = []
    offset = _IDB_OPTIONS_OFFSET
    # The last four bytes of the block are its trailing total length, so an
    # option header has to start before them.
    while offset + 4 <= len(block) - 4:
        code, length = struct.unpack(endian + "HH", block[offset:offset + 4])
        if code == _OPTION_END:
            break
        value = block[offset + 4:offset + 4 + length]
        if len(value) < length:
            raise CaptureFileError(f"{path}: truncated pcapng interface option")
        options.append((code, value))
        # Each option's value is padded out to a 4-byte boundary.
        offset += 4 + 4 * ((length + 3) // 4)
    return options


def timestamp_resolution(options: Iterable[tuple[int, bytes]]) -> int:
    """The ``if_tsresol`` value of an interface's options, or pcapng's default."""
    for code, value in options:
        if code == IF_TSRESOL and len(value) == 1:
            return value[0]
    return _DEFAULT_TSRESOL


def _nanoseconds(ticks: int, resolution: int) -> int:
    """A pcapng timestamp in the unit ``if_tsresol`` names, in nanoseconds.

    A resolution with its high bit set counts negative powers of two, and
    any other counts negative powers of ten (pcapng, Section 4.2).
    """
    if resolution & 0x80:
        return (ticks * 1_000_000_000) >> (resolution & 0x7F)
    return ticks * 1_000_000_000 // 10 ** resolution


_PCAPNG_PACKET_BLOCKS = {0x00000002, 0x00000003, 0x00000006}
_MAX_PACKETS = 1_000_000


def _capped(path, packets: int) -> int:
    if packets >= _MAX_PACKETS:
        raise CaptureFileError(f"{path}: {_MAX_PACKETS} or more packets; not counted")
    return packets


def packet_records(path: Path | str) -> Iterator[tuple[int | None, bytes]]:
    """``(timestamp_ns, data)`` for every packet record of a capture file.

    The records come in file order. ``data`` is the captured frame as it
    stands in the file, pseudo-header and all; nothing here prints it, and a
    caller that reports anything about it must not print it either, so no
    captured traffic reaches a run record. The timestamp is None for a
    record whose block carries none (a pcapng simple packet block).

    The file is walked a record at a time by the length fields, never read
    whole, so a long capture costs no more memory than its longest record.
    """
    fmt, _link_type = read_header(path)
    resolution = timestamp_resolution(interface_options(path))
    with Path(path).open("rb") as stream:
        head = stream.read(24)
        size = stream.seek(0, 2)
        if fmt == "pcap":
            endian = _PCAP_MAGIC[head[:4]]
            scale = 1 if head[:4] in _PCAP_NANOSECOND else 1000
            offset = 24
            while offset < size:
                stream.seek(offset)
                record = stream.read(16)
                if len(record) < 16:
                    raise CaptureFileError(f"{path}: truncated pcap record header")
                seconds, fraction, incl_len = struct.unpack(
                    endian + "III", record[:12])
                if offset + 16 + incl_len > size:
                    raise CaptureFileError(f"{path}: truncated pcap record")
                yield seconds * 1_000_000_000 + fraction * scale, stream.read(incl_len)
                offset += 16 + incl_len
            return
        endian = _pcapng_endian(path, head)
        offset = 0
        while offset < size:
            stream.seek(offset)
            header = stream.read(8)
            if len(header) < 8:
                raise CaptureFileError(f"{path}: truncated pcapng block header")
            block_type, length = struct.unpack(endian + "II", header)
            if length < 12 or length % 4 or offset + length > size:
                raise CaptureFileError(f"{path}: bad pcapng block length {length}")
            if block_type in _PCAPNG_PACKET_BLOCKS:
                yield _pcapng_packet(path, stream, endian, block_type, offset,
                                     length, resolution)
            offset += length


# Enhanced and obsolete packet blocks hold the interface, a 64-bit
# timestamp, and the captured and original lengths before the data; a simple
# packet block holds only the original length, and no timestamp.
_PCAPNG_SIMPLE_PACKET = 0x00000003


def _pcapng_packet(path, stream, endian, block_type, offset, length,
                   resolution) -> tuple[int | None, bytes]:
    stream.seek(offset)
    block = stream.read(length)
    if len(block) < length:
        raise CaptureFileError(f"{path}: truncated pcapng packet block")
    # Every block ends with its own total length again, so a block whose
    # trailer disagrees with its header is not a block, as the interface
    # block reader already insists.
    if struct.unpack(endian + "I", block[-4:])[0] != length:
        raise CaptureFileError(f"{path}: pcapng packet block trailer mismatch")
    if block_type == _PCAPNG_SIMPLE_PACKET:
        if length < 16:
            raise CaptureFileError(f"{path}: short pcapng simple packet block")
        original = struct.unpack(endian + "I", block[8:12])[0]
        return None, block[12:12 + min(original, length - 16)]
    if length < 32:
        raise CaptureFileError(f"{path}: short pcapng packet block")
    high, low, captured = struct.unpack(endian + "III", block[12:24])
    if 28 + captured > length:
        raise CaptureFileError(f"{path}: pcapng packet longer than its block")
    return (_nanoseconds((high << 32) | low, resolution),
            block[28:28 + captured])


def count_packets(path: Path | str) -> int:
    """Packet records in a pcap or pcapng file whose header read_header accepts.

    Counting walks each record whole, so a pcapng block whose captured
    length or trailing total length disagrees with its header is reported
    rather than counted, where counting the block types alone accepted it.
    """
    packets = 0
    for _record in packet_records(path):
        packets += 1
        if packets >= _MAX_PACKETS:
            break
    return _capped(path, packets)


def le_rf_channels(path: Path | str) -> list[int]:
    """The RF channel (0-39, (MHz - 2402) / 2) of each packet in a pcap file
    with LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR (ubertooth-btle -q). The
    pseudo-header's first byte is the RF channel libbtbb's
    lell_pcap_append_packet() writes from lell_packet channel_k."""
    fmt, link_type = read_header(path)
    if (fmt, link_type) != ("pcap", LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR):
        raise CaptureFileError(f"{path}: {fmt} link type {link_type}, not an LE PHDR pcap")
    channels = []
    for _when, data in packet_records(path):
        if not data:
            raise CaptureFileError(f"{path}: a record with no pseudo-header")
        channels.append(data[0])
    return channels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capturefile.py")
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--absent", action="store_true", help="succeed only if the file does not exist"
    )
    parser.add_argument(
        "--packets", action="store_true", help="also count the packet records"
    )
    args = parser.parse_args(argv)
    if not args.path.exists():
        print(f"Capture file absent: {args.path}")
        return 0 if args.absent else 1
    if args.absent:
        print(f"Capture file present: {args.path}")
        return 1
    try:
        fmt, link_type = read_header(args.path)
        packets = count_packets(args.path) if args.packets else None
    except (OSError, CaptureFileError) as error:
        print(f"Capture file unreadable: {error}")
        return 1
    suffix = "" if packets is None else f", packets: {packets}"
    print(f"Capture format: {fmt}, link type: {link_type}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
