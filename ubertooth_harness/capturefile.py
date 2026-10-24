"""Read the format and link type of a capture file written by ubertooth-btle.

Run as ``python <this file> PATH`` (or ``-m ubertooth_harness.capturefile``)
from a transition step; it imports only the standard library, so it runs from
any working directory,
so the check is an ordinary logged command. It prints one line:

    Capture format: pcap, link type: 256
    Capture format: pcapng, link type: 256
    Capture file absent: PATH

and exits 0 when it could read a header (or, with ``--absent``, when the file
does not exist), 1 otherwise. Only the file header is read; packet contents
are never printed, so no captured traffic reaches the run record.

Link types written by ubertooth-btle through libbtbb:

- ``-r`` PCAPNG: one interface, LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR (256)
- ``-q`` PCAP: LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR (256)
- ``-c`` PCAP: LINKTYPE_PPI (192), wrapping LINKTYPE_BLUETOOTH_LE_LL
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

LINKTYPE_PPI = 192
LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR = 256

_PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",  # microsecond
    b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">",  # nanosecond
}
_PCAPNG_SHB = 0x0A0D0D0A
_PCAPNG_IDB = 0x00000001
_PCAPNG_BYTE_ORDER = 0x1A2B3C4D


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


def _pcapng_link_type(path, stream, head: bytes) -> int:
    if len(head) < 12:
        raise CaptureFileError(f"{path}: truncated pcapng section header")
    for endian in ("<", ">"):
        if struct.unpack(endian + "I", head[8:12])[0] == _PCAPNG_BYTE_ORDER:
            break
    else:
        raise CaptureFileError(f"{path}: bad pcapng byte-order magic")
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
            return struct.unpack(endian + "H", block[8:10])[0]
        offset += length
    raise CaptureFileError(f"{path}: no pcapng interface description block")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ubertooth_harness.capturefile")
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--absent", action="store_true", help="succeed only if the file does not exist"
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
    except (OSError, CaptureFileError) as error:
        print(f"Capture file unreadable: {error}")
        return 1
    print(f"Capture format: {fmt}, link type: {link_type}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
