"""Check the record structure of an ubertooth-dump or ubertooth-rx -d file.

A transition step runs ``python <this file> PATH FORMAT [options]`` as an
ordinary logged command. It imports only the standard library, so it runs
from any working directory. It prints one summary line, never record
contents, so no received symbols reach the run record:

    Receive dump: 6751 full records
    Receive dump problem: 3 trailing bytes

and exits 0 when the file is well formed, 1 otherwise. With ``--max-stale N``
the line also carries how many records were left from an earlier mode
(stale_records() below), and more than N of them is a problem:

    Receive dump: 6751 full records, 0 stale (0 LE)

Formats, from libubertooth (ubertooth.c cb_dump_full, cb_dump_bitstream;
ubertooth_callback.c cb_rx):

- ``full``: 68-byte records, a big-endian 32-bit host time followed by one
  64-byte usb_pkt_rx (pkt_type, status, channel index, ...). Written by
  ubertooth-dump without -b, and by ubertooth-rx -d for each packet found.
- ``bits``: 401-byte records, 400 ASCII '0'/'1' symbols and a newline.
  Written by ubertooth-dump -b.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

FULL_RECORD = 4 + 64
BITS_RECORD = 400 + 1
# pkt_type values (ubertooth_interface.h): BR_PACKET 0, LE_PACKET 1, KEEP_ALIVE 3.
PACKET_TYPES = {"br": 0, "le": 1, "keep-alive": 3}
NUM_BREDR_CHANNELS = 79
# Host times must lie within this many seconds of the file's modification time.
TIME_WINDOW_S = 24 * 3600


def check(path: Path, fmt: str, *, min_records: int = 0, types=("br", "keep-alive")):
    """(records, problem); problem is "" for a well-formed file."""
    data = path.read_bytes()
    size = FULL_RECORD if fmt == "full" else BITS_RECORD
    records, trailing = divmod(len(data), size)
    # cb_dump_bitstream never flushes, so a run the executor had to SIGKILL
    # after the SIGTERM grace can end mid-record at a stdio buffer boundary;
    # the last partial bitstream record is ignored. Full dumps flush each
    # record, so any trailing bytes there are a real problem.
    if trailing and fmt != "bits":
        return records, f"{trailing} trailing bytes after {records} records"
    data = data[:records * size]
    if records < min_records:
        return records, f"{records} records, expected at least {min_records}"
    if fmt == "bits":
        for index in range(records):
            record = data[index * size:(index + 1) * size]
            if record[-1:] != b"\n" or record[:-1].strip(b"01"):
                return records, f"record {index} is not 400 symbols and a newline"
        return records, ""
    allowed = {PACKET_TYPES[name] for name in types}
    mtime = int(path.stat().st_mtime)
    previous = 0
    for index in range(records):
        record = data[index * size:(index + 1) * size]
        host_time = struct.unpack(">I", record[:4])[0]
        pkt_type, channel = record[4], record[6]
        if pkt_type not in allowed:
            return records, f"record {index} has packet type {pkt_type}"
        if channel >= NUM_BREDR_CHANNELS:
            return records, f"record {index} has channel index {channel}"
        if abs(host_time - mtime) > TIME_WINDOW_S:
            return records, f"record {index} host time is not near the file time"
        if host_time < previous:
            return records, f"record {index} host time goes backwards"
        previous = host_time
    return records, ""


def stale_records(path: Path) -> tuple[int, int, int]:
    """(records, stale, le) for a ``full`` dump of one basic-rate stream.

    A mode change runs cc2400_idle(), whose clkn_stop() sets the clock to 0,
    so a stream's own records carry clk100ns (usb_pkt_rx bytes 4-7, little
    endian) counting up from about 0; it wraps only after 3125 * 2^20 x 100
    ns (about 327 s), far longer than a bounded run. Records before the last
    place the clock goes backwards are left from an earlier mode
    (SqueakyFloors/ubertooth#318). Keep-alive records are skipped in that
    comparison: the firmware sends one byte, and the rest of the record is
    whatever the host's buffer held. ``stale`` counts those leading records
    plus any LE_PACKET record after them; ``le`` counts LE_PACKET records
    anywhere. Raises ValueError for a file that is not whole full records.
    """
    data = path.read_bytes()
    records, trailing = divmod(len(data), FULL_RECORD)
    if trailing:
        raise ValueError(f"{trailing} trailing bytes after {records} records")
    types = [data[i * FULL_RECORD + 4] for i in range(records)]
    start, previous = 0, None
    for index in range(records):
        if types[index] == PACKET_TYPES["keep-alive"]:
            continue
        offset = index * FULL_RECORD + 8
        clock = struct.unpack("<I", data[offset:offset + 4])[0]
        if previous is not None and clock < previous:
            start = index
        previous = clock
    le = sum(kind == PACKET_TYPES["le"] for kind in types)
    later_le = sum(kind == PACKET_TYPES["le"] for kind in types[start:])
    return records, start + later_le, le


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rxdump.py")
    parser.add_argument("path", type=Path)
    parser.add_argument("format", choices=("full", "bits"))
    parser.add_argument("--min-records", type=int, default=0)
    parser.add_argument(
        "--types", default="br,keep-alive",
        help="comma list of allowed packet types (full format): br, le, keep-alive",
    )
    parser.add_argument(
        "--max-stale", type=int, default=None,
        help="fail when more than this many records are left from an earlier "
             "mode (full format); the count is added to the summary line. "
             "stale_records() finds a basic-rate leftover by its clock, so "
             "one whose clock is not ahead of the new stream's first record "
             "is not counted; an LE record is found whatever its clock",
    )
    args = parser.parse_args(argv)
    types = tuple(item for item in args.types.split(",") if item)
    if not set(types) <= set(PACKET_TYPES):
        parser.error("unknown packet type")
    if not args.path.exists():
        print(f"Receive dump problem: {args.path} does not exist")
        return 1
    try:
        records, problem = check(args.path, args.format, min_records=args.min_records,
                                 types=types)
    except OSError as error:
        print(f"Receive dump problem: {error}")
        return 1
    if problem:
        print(f"Receive dump problem: {problem}")
        return 1
    summary = f"Receive dump: {records} {args.format} records"
    if args.max_stale is None:
        print(summary)
        return 0
    if args.format != "full":
        print("Receive dump problem: --max-stale needs the full format")
        return 1
    try:
        _records, stale, le = stale_records(args.path)
    except (OSError, ValueError) as error:
        print(f"Receive dump problem: {error}")
        return 1
    # The count goes in the line whether or not it is within the limit, so
    # the run record carries the number either way.
    print(f"{summary}, {stale} stale ({le} LE)")
    if stale > args.max_stale:
        print(f"Receive dump problem: {stale} records left from an earlier "
              f"mode, more than {args.max_stale}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
