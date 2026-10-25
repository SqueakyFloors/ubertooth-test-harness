"""Count packets left from the previous mode in alternating BR/LE captures.

A transition step runs ``python <this file> --dumps D1 ... --captures P1 ...
[--channel K]`` as an ordinary logged command, after a loop that ran, for
each iteration i, a bounded ``ubertooth-dump -d Di`` straight after a
bounded ``ubertooth-btle -n``, then a bounded ``ubertooth-btle -n -A37 -q
Pi`` straight after that dump. It imports only the standard library (and
its sibling checkers), so it runs from any working directory, and prints
one summary line, never packet contents:

    Stale packets: BR dump after BLE: 3, 2, 2, 2, 2 records (LE: 3, 0, 0, 0, 0);
    BLE capture after BR dump: 0, 0, 0, 0, 0 packets; stale iterations:
    BR dump 5/5, BLE capture 0/5

(one line). It exits 0 when every file could be read, whatever the counts;
the step compares the "stale iterations" field, so the per-iteration counts
are in the record either way. It exits 1 if a file is missing or malformed.

Stale records in a dump are found by rxdump.stale_records() (the clock
restarts at mode entry; LE_PACKET records are never basic-rate symbols).
Stale packets in a -q capture are packets whose RF channel is not the one
the capture ran on (``--channel``, default 0 for advertising index 37): a
basic-rate record from the dump carries the dump's channel, 2441 MHz after
a stop, which libbtbb files as RF channel 19.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from ubertooth_harness import capturefile, rxdump
except ImportError:  # run by path: the sibling modules are on sys.path[0]
    import capturefile  # type: ignore[no-redef]
    import rxdump  # type: ignore[no-redef]


def summary(dumps, captures, channel=0) -> str:
    """The summary line; raises ValueError or OSError for an unreadable file."""
    dump_counts, le_counts, capture_counts = [], [], []
    for path in dumps:
        _records, stale, le = rxdump.stale_records(Path(path))
        dump_counts.append(stale)
        le_counts.append(le)
    for path in captures:
        channels = capturefile.le_rf_channels(path)
        capture_counts.append(sum(item != channel for item in channels))

    def joined(counts):
        return ", ".join(map(str, counts)) or "none"

    def stale(counts):
        return f"{sum(count > 0 for count in counts)}/{len(counts)}"

    return (
        f"Stale packets: BR dump after BLE: {joined(dump_counts)} records "
        f"(LE: {joined(le_counts)}); BLE capture after BR dump: "
        f"{joined(capture_counts)} packets; stale iterations: "
        f"BR dump {stale(dump_counts)}, BLE capture {stale(capture_counts)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stalecheck.py")
    parser.add_argument("--dumps", nargs="*", type=Path, default=[])
    parser.add_argument("--captures", nargs="*", type=Path, default=[])
    parser.add_argument("--channel", type=int, default=0,
                        help="RF channel the -q captures ran on (0-39)")
    args = parser.parse_args(argv)
    try:
        for path in args.dumps + args.captures:
            if not path.exists():
                raise ValueError(f"{path} does not exist")
        line = summary(args.dumps, args.captures, args.channel)
    except (OSError, ValueError) as error:
        print(f"Stale check problem: {error}")
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
