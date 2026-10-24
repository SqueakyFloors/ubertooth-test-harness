"""Summarize an ubertooth-specan -d file: 3-byte records, frequency in MHz
(big-endian uint16) then RSSI (int8).

Run as ``python <this file> PATH LOW HIGH`` from a transition step. It prints
one line, ``Specan dump: N samples, M frequencies in LOW-HIGH, K outside``,
and exits 0 when the file has whole records, at least one sample, every
frequency of the range, none outside it, and no partial record; 1 otherwise. Imports only the
standard library, so it runs from any working directory.
"""

from __future__ import annotations

import sys
from pathlib import Path


def summarize(path: Path | str, low: int, high: int) -> tuple[int, set[int], int, int]:
    """(samples, frequencies in range, samples outside, trailing bytes)."""
    data = Path(path).read_bytes()
    whole = len(data) - len(data) % 3
    seen: set[int] = set()
    outside = 0
    for offset in range(0, whole, 3):
        frequency = (data[offset] << 8) | data[offset + 1]
        if low <= frequency <= high:
            seen.add(frequency)
        else:
            outside += 1
    return whole // 3, seen, outside, len(data) % 3


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("usage: specandump.py PATH LOW HIGH")
        return 2
    path, low, high = args[0], int(args[1]), int(args[2])
    try:
        samples, seen, outside, trailing = summarize(path, low, high)
    except OSError as error:
        print(f"Specan dump unreadable: {error}")
        return 1
    print(
        f"Specan dump: {samples} samples, {len(seen)} frequencies in {low}-{high}, "
        f"{outside} outside" + (f", {trailing} trailing bytes" if trailing else "")
    )
    expected = high - low + 1
    return 0 if samples and len(seen) == expected and not outside and not trailing else 1


if __name__ == "__main__":
    sys.exit(main())
