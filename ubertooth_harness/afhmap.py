"""Compare an AFH channel map `ubertooth-afh` recovered with the one in force.

A Classic Bluetooth piconet using Adaptive Frequency Hopping hops over the
channels its leader kept, and `ubertooth-afh` works that set out from the air
alone: it listens for the piconet's access code and marks every channel it
hears one on. The ESP32 Classic peers can be held to a chosen map
(firmware/esp32-classic-peer, "The channel map"), so a test can put a known
map on the air and ask what the tool made of it.

Run as a script by the AFH cases::

    python afhmap.py --peer-report PATH --maps PATH
                     [--min-found-share S] [--max-stray-share S]
                     [--min-dropped-channels N]

``--peer-report`` is the JSON esp32peer.py wrote for the session: the map the
peer was asked for, and the map the link was really hopping over, read from
the leader's controller at the start, the middle, and the end of the capture.
``--maps`` is the tool's own standard output, saved by the same session.

Two maps, two names, because the difference matters. The **requested** map is
what the peer's host asked its controller to classify; the **in-force** map
is what the controller reports the link hopping over, and it is a subset:
these controllers drop channels 0, 24, and 78 of their own accord. The
in-force map is therefore what a recovered map is judged against, and the
requested map is only checked for being the superset it must be.

What is judged, and the tolerance each has:

- **found:** the share of in-force channels the recovered map marks used.
  The tool has to hear a packet on a channel to find it, and it hears one
  only when its sweep and the piconet's hop land together, so this is the
  measure of the recovery working at all. The default demands all of them;
  a plan may allow a share below 1 for a short capture.
- **stray:** the share of the channels NOT in force that the recovered map
  marks used. A receiver close to the piconet reads some energy from the
  channels next to the kept block, and those neighbors decode often enough
  to be marked used, so this cannot be held to zero on this bench; an
  attenuated or distant receiver brings it down. The ceiling is a documented
  tolerance, not a tight bound.

The stray share is left unjudged, and said to be, when fewer than
``--min-dropped-channels`` channels are out of force: a map that keeps nearly
the whole band leaves too few channels for the share to mean anything.

The tool's output comes in two forms and both are read:

- ``-r`` prints ``<unix time> <80 characters of 0 and 1>`` once a second, so
  the capture carries the whole series. The last line is the recovered map,
  and the series is reported a minute at a time, which is how a map that
  grew all run looks different from one that settled.
- without ``-r`` it prints ``AFH map: 0x<20 hex digits>`` when the map it has
  worked out changes, so the last line is again the recovered map. These
  lines carry no time, so there is no per-minute series for them.

Every failure is one line on standard error beginning ``error: `` and exit 1;
the measurements are printed on standard output either way, because a run
that missed the tolerance is still the measurement of what the tool did.
Exit status 2 is a usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

# The 79 Bluetooth basic rate channels, and the 80th bit of a ten-byte map,
# which the spec reserves and a map must leave clear.
CHANNELS = 79
MAP_BITS = 80
MAP_DIGITS = MAP_BITS // 4

# What the tool prints, in its two forms.
R_LINE = re.compile(r"^(?P<time>\d+) (?P<bits>[01]{80})$")
MAP_LINE = re.compile(r"^AFH map: 0x(?P<hex>[0-9a-f]{20})$")

# Defaults the bench measured, and why each is where it is, are in
# docs/tests.md under "Recover AFH map". They are defaults rather than fixed
# bounds so a plan can tighten them once a run has a harder channel edge.
DEFAULT_MIN_FOUND_SHARE = 1.0
DEFAULT_MAX_STRAY_SHARE = 0.70
DEFAULT_MIN_DROPPED_CHANNELS = 10
# Seconds a bucket of the per-minute series covers.
MINUTE_S = 60
# The fewest channels a piconet hops over (Core 6.0 Vol 4 Part E 7.3.46, and
# N_min in Vol 2 Part B 2.6.3), which the peer firmware enforces too. A link
# map below it is not a map this comparison can mean anything about.
MIN_PICONET_CHANNELS = 20


class AfhMapError(RuntimeError):
    """The maps, the report, or the recovery were not what the test needs."""


def channels_from_hex(text: str, label: str = "map") -> frozenset[int]:
    """The channels a twenty-hex-digit map marks used.

    Bit ``b`` of byte ``i`` is channel ``8i + b``, which is the order the
    spec's ``Set_AFH_Host_Channel_Classification`` uses and the order libbtbb
    prints a recovered map in, so the two compare as they stand.
    """
    digits = str(text).strip().lower()
    if len(digits) != MAP_DIGITS or not re.fullmatch(r"[0-9a-f]+", digits):
        raise AfhMapError(f"{label} must be {MAP_DIGITS} hex digits, got {text!r}")
    channels = set()
    for index in range(MAP_BITS // 8):
        byte = int(digits[index * 2:index * 2 + 2], 16)
        for bit in range(8):
            if byte & (1 << bit):
                channels.add(index * 8 + bit)
    if CHANNELS in channels:
        raise AfhMapError(f"{label} sets the reserved 80th bit: {digits}")
    return frozenset(channels)


def channels_from_bits(bits: str, label: str = "map") -> frozenset[int]:
    """The channels an eighty-character ``-r`` map marks used.

    Character ``k`` is channel ``k``: the tool walks the same bytes and bits
    as ``channels_from_hex`` and prints one character for each.
    """
    text = str(bits).strip()
    if len(text) != MAP_BITS or set(text) - set("01"):
        raise AfhMapError(f"{label} must be {MAP_BITS} characters of 0 and 1")
    channels = frozenset(index for index, value in enumerate(text) if value == "1")
    if CHANNELS in channels:
        raise AfhMapError(f"{label} sets the reserved 80th bit")
    return channels


def hex_from_channels(channels: Iterable[int]) -> str:
    """``channels`` as the twenty hex digits every map here is written in."""
    used = set(channels)
    unknown = sorted(channel for channel in used if not 0 <= channel < CHANNELS)
    if unknown:
        raise AfhMapError(f"channel {unknown[0]} is not one of the {CHANNELS}")
    digits = []
    for index in range(MAP_BITS // 8):
        byte = 0
        for bit in range(8):
            if index * 8 + bit in used:
                byte |= 1 << bit
        digits.append(f"{byte:02x}")
    return "".join(digits)


def runs(channels: Iterable[int]) -> str:
    """``channels`` as ranges, for a line a person reads: ``0-39, 42, 44``."""
    ordered = sorted(set(channels))
    if not ordered:
        return "none"
    parts = []
    start = previous = ordered[0]
    for channel in ordered[1:] + [None]:
        if channel == previous + 1:
            previous = channel
            continue
        parts.append(f"{start}-{previous}" if previous > start else f"{start}")
        if channel is None:
            break
        start = previous = channel
    return ", ".join(parts)


def _share(part: int, whole: int) -> float:
    """``part`` of ``whole``; a whole of nothing is complete, not undefined."""
    return 1.0 if whole == 0 else part / whole


def _percent(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{round(100 * part / whole)}%"


# ---------------------------------------------------------------------------
# The session's maps


@dataclass(frozen=True)
class Sample:
    """One reading of the map the live link is hopping over."""

    at: str
    seconds: float
    link: str
    used: int
    mode: str
    error: str


@dataclass(frozen=True)
class PeerReport:
    """What esp32peer.py recorded about the piconet's own maps."""

    requested: str
    requested_channels: frozenset[int]
    link_channels: frozenset[int]
    samples: tuple[Sample, ...]

    @property
    def link(self) -> str:
        return hex_from_channels(self.link_channels)


def load_peer_report(path: Path) -> PeerReport:
    """Read the session's JSON report and check it describes one stable map.

    A link map that changed under the capture, or a controller that would not
    answer, makes the comparison meaningless: there is then no one map the
    recovered one should match, which is exactly the case the peer's README
    says to report plainly rather than average over.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise AfhMapError(f"cannot read the peer report {path}: {error}") from error
    except ValueError as error:
        raise AfhMapError(f"the peer report {path} is not JSON: {error}") from error
    if not isinstance(raw, dict):
        raise AfhMapError(f"the peer report {path} is not a JSON object")
    requested = str(raw.get("requested", ""))
    requested_channels = channels_from_hex(requested, "the requested map")
    samples = []
    for entry in raw.get("samples", ()):
        if not isinstance(entry, dict):
            raise AfhMapError("a sample in the peer report is not a JSON object")
        at = str(entry.get("at", "?"))
        try:
            seconds = float(entry.get("seconds", 0.0))
            used = int(entry.get("used") or 0)
        except (TypeError, ValueError) as error:
            # A field that is not a number is a damaged report, which is one
            # of this command's failures rather than a traceback.
            raise AfhMapError(f"the {at} sample of {path} has a bad number: "
                              f"{error}") from error
        samples.append(Sample(
            at=at, seconds=seconds, used=used,
            link=str(entry.get("link") or ""),
            mode=str(entry.get("mode") or ""),
            error=str(entry.get("error") or ""),
        ))
    if not samples:
        raise AfhMapError(f"the peer report {path} holds no link map sample")
    maps = {sample.link for sample in samples}
    if len(maps) != 1:
        raise AfhMapError(
            "the link's channel map changed under the capture: "
            + "; ".join(f"{sample.at} {sample.link or 'none'}" for sample in samples))
    (link,) = maps
    link_channels = channels_from_hex(link, "the in-force map")
    if len(link_channels) < MIN_PICONET_CHANNELS:
        # Below the spec's floor there is no hopping piconet to compare a
        # recovered map with, and an empty map would make "every in-force
        # channel found" true of a tool that found nothing.
        raise AfhMapError(
            f"the link's map {link} keeps {len(link_channels)} channels, fewer "
            f"than the {MIN_PICONET_CHANNELS} a piconet hops over; there is no "
            "map here to compare a recovery with")
    beyond = link_channels - requested_channels
    if beyond:
        # The controller may drop channels the host asked to keep; it must
        # not add any. One that does is not the piconet this test set up.
        raise AfhMapError(
            f"the link is hopping over channel(s) {runs(beyond)}, which the "
            f"requested map {requested} does not keep")
    return PeerReport(requested, requested_channels, link_channels, tuple(samples))


@dataclass(frozen=True)
class Series:
    """The maps the tool printed, in order, with their times where it gave any."""

    channels: tuple[frozenset[int], ...]
    times: tuple[int, ...] = ()
    form: str = "map lines"

    @property
    def recovered(self) -> frozenset[int]:
        return self.channels[-1]


def read_series(path: Path) -> Series:
    """The tool's saved standard output as a series of maps.

    One form or the other, never a mixture: a file holding both would mean
    the capture ran two tools, and which one's last line is the recovered map
    would be a guess.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise AfhMapError(f"cannot read the map file {path}: {error}") from error
    timed: list[tuple[int, frozenset[int]]] = []
    printed: list[frozenset[int]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        match = R_LINE.match(line)
        if match:
            timed.append((int(match["time"]),
                          channels_from_bits(match["bits"], f"line {number}")))
            continue
        match = MAP_LINE.match(line)
        if match:
            printed.append(channels_from_hex(match["hex"], f"line {number}"))
    if timed and printed:
        raise AfhMapError(
            f"{path} holds both one-a-second maps and 'AFH map:' lines; one "
            "capture prints one form")
    if timed:
        return Series(tuple(channels for _time, channels in timed),
                      tuple(time for time, _channels in timed),
                      "one map a second (-r)")
    if printed:
        return Series(tuple(printed), (), "AFH map: lines")
    raise AfhMapError(
        f"{path} holds no AFH map: the tool printed none, so it recovered "
        "nothing from the piconet")


# ---------------------------------------------------------------------------
# The comparison


@dataclass(frozen=True)
class Comparison:
    """One map judged against the one in force."""

    recovered: frozenset[int]
    in_force: frozenset[int]

    @property
    def found(self) -> frozenset[int]:
        return self.recovered & self.in_force

    @property
    def missing(self) -> frozenset[int]:
        return self.in_force - self.recovered

    @property
    def dropped(self) -> frozenset[int]:
        """The channels the link is not hopping over, which should stay clear."""
        return frozenset(range(CHANNELS)) - self.in_force

    @property
    def stray(self) -> frozenset[int]:
        """Channels marked used that the link is not hopping over."""
        return self.recovered - self.in_force

    @property
    def found_share(self) -> float:
        return _share(len(self.found), len(self.in_force))

    @property
    def stray_share(self) -> float:
        return _share(len(self.stray), len(self.dropped))


def minute_buckets(series: Series) -> list[tuple[int, int, frozenset[int]]]:
    """``(minute, lines, map at the end of it)`` for each minute of a series.

    The map of a minute is the last one printed inside it, not a union over
    it: each line is already the tool's whole map at that second, so the last
    line of a minute is what it had worked out by then.
    """
    if not series.times:
        return []
    start = series.times[0]
    buckets: dict[int, list[frozenset[int]]] = {}
    for time, channels in zip(series.times, series.channels):
        minute = int((time - start) // MINUTE_S) + 1
        buckets.setdefault(minute, []).append(channels)
    return [(minute, len(maps), maps[-1])
            for minute, maps in sorted(buckets.items())]


def describe(report: PeerReport, series: Series,
             comparison: Comparison) -> list[str]:
    """The measurement, as the lines the case's patterns read."""
    lines = [
        f"requested map: {report.requested} used {len(report.requested_channels)}"
        f" ({runs(report.requested_channels)})",
        f"in-force map: {report.link} used {len(report.link_channels)}"
        f" ({runs(report.link_channels)})",
        f"in-force samples: {len(report.samples)}, all the same map",
        f"tool output: {len(series.channels)} {series.form}",
        f"recovered map: {hex_from_channels(comparison.recovered)} used "
        f"{len(comparison.recovered)} ({runs(comparison.recovered)})",
        f"in-force channels found: {len(comparison.found)} of "
        f"{len(comparison.in_force)} "
        f"({_percent(len(comparison.found), len(comparison.in_force))})",
        f"in-force channels missing: {runs(comparison.missing)}",
        f"stray channels marked used: {len(comparison.stray)} of "
        f"{len(comparison.dropped)} out of force "
        f"({_percent(len(comparison.stray), len(comparison.dropped))})",
        f"stray channels: {runs(comparison.stray)}",
    ]
    for minute, count, channels in minute_buckets(series):
        minute_view = Comparison(channels, comparison.in_force)
        lines.append(
            f"minute {minute}: {count} map lines, used {len(channels)}, "
            f"in force {len(minute_view.found)} of {len(minute_view.in_force)}, "
            f"stray {len(minute_view.stray)}")
    if not series.times:
        lines.append("minutes: none, this form of output carries no time")
    return lines


def judge(comparison: Comparison, *, min_found_share: float,
          max_stray_share: float, min_dropped_channels: int) -> list[str]:
    """What the tolerances make of a comparison: a line per verdict.

    The last line is ``afh recovery: ok`` when every judged measure is inside
    its tolerance, and the reasons are raised instead when one is not.
    """
    lines = []
    problems = []
    if comparison.found_share < min_found_share:
        problems.append(
            f"{len(comparison.found)} of {len(comparison.in_force)} in-force "
            f"channels found, below the {min_found_share:.0%} this test needs "
            f"(missing {runs(comparison.missing)})")
    if max_stray_share >= 1.0:
        # Every channel out of force allowed: the caller is measuring the
        # recovery and not its selectivity, which is what the detection
        # phase's map calls for (it fills gaps and removes nothing).
        lines.append("stray share: not judged, this test allows every channel "
                     "out of force")
    elif len(comparison.dropped) < max(min_dropped_channels, 1):
        lines.append(
            f"stray share: not judged, only {len(comparison.dropped)} channels "
            f"are out of force and this test judges the share from "
            f"{max(min_dropped_channels, 1)}")
    elif comparison.stray_share > max_stray_share:
        problems.append(
            f"{len(comparison.stray)} of {len(comparison.dropped)} channels out "
            f"of force are marked used, above the {max_stray_share:.0%} this "
            f"test allows ({runs(comparison.stray)})")
    if problems:
        raise AfhMapError("; ".join(problems))
    return lines + ["afh recovery: ok"]


def check(peer_report: Path, maps: Path, *,
          min_found_share: float = DEFAULT_MIN_FOUND_SHARE,
          max_stray_share: float = DEFAULT_MAX_STRAY_SHARE,
          min_dropped_channels: int = DEFAULT_MIN_DROPPED_CHANNELS,
          out=None) -> None:
    """Print the measurement, then raise when a tolerance was missed."""
    out = out or sys.stdout
    report = load_peer_report(peer_report)
    series = read_series(maps)
    comparison = Comparison(series.recovered, report.link_channels)
    for line in describe(report, series, comparison):
        print(line, file=out)
    try:
        verdict = judge(comparison, min_found_share=min_found_share,
                        max_stray_share=max_stray_share,
                        min_dropped_channels=min_dropped_channels)
    except AfhMapError:
        out.flush()
        raise
    for line in verdict:
        print(line, file=out)
    out.flush()


def _share_argument(value: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("a share is between 0 and 1")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="afhmap.py",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--peer-report", required=True, type=Path,
                        help="the JSON esp32peer.py wrote for the session")
    parser.add_argument("--maps", required=True, type=Path,
                        help="the tool's saved standard output")
    parser.add_argument("--min-found-share", type=_share_argument,
                        default=DEFAULT_MIN_FOUND_SHARE,
                        help="share of the in-force channels that must be found")
    parser.add_argument("--max-stray-share", type=_share_argument,
                        default=DEFAULT_MAX_STRAY_SHARE,
                        help="share of the channels out of force that may be "
                             "marked used")
    parser.add_argument("--min-dropped-channels", type=int,
                        default=DEFAULT_MIN_DROPPED_CHANNELS,
                        help="channels that must be out of force for the stray "
                             "share to be judged at all")
    args = parser.parse_args(argv)
    try:
        check(args.peer_report, args.maps,
              min_found_share=args.min_found_share,
              max_stray_share=args.max_stray_share,
              min_dropped_channels=args.min_dropped_channels)
    except AfhMapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
