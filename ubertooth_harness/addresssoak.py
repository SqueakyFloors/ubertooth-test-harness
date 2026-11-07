"""Run a long BLE advertising capture and read one known address two ways.

Run as a script by the `BLE address parity soak` case::

    python addresssoak.py --out DIR --serial HEX --address ADDR
        --channel 37|38|39 --duration S --jlink SERIAL [--dk-port PATH]
        [--dk-alias NAME] --interval MS --payload HEX|none
        --advertiser-on S --advertiser-off S --sample-every S
        --presence-window S --presence-windows N --liveness-timeout S
        --detect-within S --min-detection-share F --max-gap S
        --min-packets-per-minute N --max-disagreement-share F
        --max-restarts N [--max-memory-growth-kb N] [--restart-probe]
        [--max-capture-bytes N] [--max-total-capture-bytes N]
        [--term-grace S] [--restart-min-interval S]

One Ubertooth, pinned by serial, runs ``ubertooth-btle -n -U <serial>
-A<channel> -q <file>``: advertising only, never following a connection, with
its standard error merged into its standard output, which is how a
production-style advertising-detection service runs it. One registered
nRF52840 DK advertises its registered address on that channel to a schedule
this driver applies (``--advertiser-on`` seconds advertising, then
``--advertiser-off`` seconds quiet), so every detection has a known deadline
and every silence a known cause.

**The two readers.** Each capture is read twice, from the one capture:

- the *text* reader takes the documented ``AdvA:  xx:xx:xx:xx:xx:xx
  (random)`` line libbtbb prints for every packet it decodes, which is what a
  service that watches the tool's output parses, and timestamps it when the
  host read the line;
- the *structural* reader tails the capture file as the tool writes it,
  decodes each ``LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR`` record with
  advcapture.py, recomputes the link-layer CRC, and takes the AdvA field of
  the packets whose CRC is right, timestamped with the time the capture
  recorded.

Both addresses are normalized the same way (upper case, colon separated)
before anything is compared, since one source prints lower case and the other
is raw bytes. The readers are then compared over the same windows:
``--presence-window`` seconds wide, and a device counts as sustained once it
has been seen in ``--presence-windows`` of them in a row.

They are not expected to agree packet for packet, and the test does not ask
them to. libbtbb prints a decoded packet whether or not its CRC checks, so
the text reader can report an address from a corrupted packet that the
structural reader drops; and the ``AdvA:`` of a SCAN_REQ or a CONNECT_REQ is
the *target's* address, so a scanner that addressed our advertiser would
appear in the text reader alone. What is compared is therefore which windows
each reader found the address in, with an allowance of
``--max-disagreement-share`` of the windows either reader saw it in.

**Liveness, death and recovery.** A capture counts as dead when the process
exits; its exit status and its last ``error:`` line are recorded. A capture is
restarted when the process exits, or when no packet of any address arrives
for ``--liveness-timeout`` seconds, and no more often than
``--restart-min-interval``. The stop is SIGTERM first, waiting
``--term-grace`` seconds, and SIGKILL only if the tool is still there. After
each restart the board's watchdog record (``ubertooth-util -w``) is read while
no capture is running, so a self-reset is reported with the mode and wait the
firmware recorded. A restart whose capture hears nothing either gets one
``ubertooth-util -r`` before the next try, which is the recovery ladder
proposed in SqueakyFloors/ubertooth#703. ``--restart-probe`` kills the tool
once, at the middle of the run, with SIGKILL and no stop sent to the board --
the way a lost terminal ends a capture -- so a run carries evidence that the
restart path recovers rather than only evidence that it was never needed.

A board that does not answer before the capture starts is a **configuration
failure** (exit 3) and is never reset to make it work: something else is
using it, and taking it would spoil that run as well as this one.

**What it writes.** In ``--out``: one ``capture-NNNN.pcap`` and one
``btle-NNNN.log`` (the tool's own transcript, capped) per capture attempt,
``progress.jsonl``, appended and flushed as the run goes, and
``summary.json`` at the end. The progress file is what makes a long run
reviewable while it runs and after the client that started it has gone: the
run directory, not this process, holds the record. A capture file that
reaches ``--max-capture-bytes`` is rolled over to the next attempt, at the
start of an off period so the changeover falls in a silence the schedule
already has; the off period is still judged, from the moment something was
listening again. ``--max-total-capture-bytes`` bounds the rest, removing the
oldest complete capture and keeping the newest, and ``progress.jsonl`` and
each transcript have budgets of their own, so no file here grows with the
length of the run. A file the budget needs gone and cannot remove ends the
run: writing captures past the budget for the hours it had left is the one
thing the budget is there to prevent.

Only counts, the known advertiser's own address, and the harness's own
figures are printed or recorded. Another device's address never leaves the
tool's transcript.

Exit status 0 when every configured objective is met, 1 when one is not (each
one a ``FAIL:`` line) or the run was signalled, 2 for a usage error, and 3 for
a configuration failure. Imports the harness package, so it runs under the
harness interpreter from any working directory.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import statistics
import struct
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script: make the package importable, as nrfdk.py does.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness import advcapture, nrfdk, watchdog  # noqa: E402

TOOL = "ubertooth-btle"
UTIL = "ubertooth-util"

# The documented line a production text reader parses, as libbtbb prints it
# (lib/src/bluetooth_le_packet.c, _dump_addr): four spaces, the label, the
# address most significant byte first in lower case, and the address type.
ADVA_LINE = re.compile(
    r"^\s*AdvA:\s+((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\s*\((random|public)\)")
# The tool's own error reporting: the stable signal for a dead capture is the
# exit status plus one of these lines (SqueakyFloors/ubertooth#703).
ERROR_LINE = re.compile(r"^\s*error:\s*(.+?)\s*$")
# What an older service treated as the capture dying, before the fork's error
# wording settled. Counted, never acted on, so a run says whether the old
# signal would have fired at all.
LEGACY_DEATH_LINE = re.compile(r"libUSB Error|could not open Ubertooth device")
# The warning libubertooth prints when it finds the board still in a mode a
# killed capture left it in (fork e657807): evidence that the restart path
# met that state and cleared it.
MODE_WARNING = "is still in mode"

POLL_S = 0.1
# A board read is two control transfers; a board that has gone takes the
# whole of this and no longer.
UTIL_TIMEOUT_S = 10.0
RESET_TIMEOUT_S = 20.0
# Between tries of the watchdog read after a reset, as the harness's own
# monitor pauses (watchdog.RECOVERY_PAUSE_S).
WATCHDOG_RETRY_PAUSE_S = watchdog.RECOVERY_PAUSE_S
# SIGTERM first, then this long before SIGKILL. #703's planned pattern says
# three seconds; the harness's own measured bound for `ubertooth-btle` ending
# after a signal is five (interrupt_cases.EXIT_DEADLINE_S), and a tool that
# takes four would otherwise be killed here and fail a three-day run at its
# last step while passing `Interrupting a capture`.
DEFAULT_TERM_GRACE_S = 5.0
# A restart no more often than this, so a board that cannot capture at all
# is not hammered.
DEFAULT_RESTART_MIN_INTERVAL_S = 5.0
# How much of the tool's own transcript the whole run keeps, shared across
# every attempt. A crowded advertising channel is the case that decides this:
# on the lab bench one 30-second capture printed 14 MB of transcript, which
# over three days and the restarts and rotations of a long run would be
# hundreds of gigabytes. Every line is still read, whether or not it is kept,
# so the figures the verdict rests on do not change; what a run loses past
# the budget is the per-packet text, and it is recorded how much.
MAX_LOG_BYTES = 64 * 1024 * 1024
# How much of a growing capture file is read per poll, so one tick's work is
# bounded however far behind the reader is.
TAIL_CHUNK = 4 * 1024 * 1024
# Distinct other addresses counted before the count becomes a floor. They are
# never printed; this only bounds the memory a crowded bench costs.
MAX_OTHER_ADDRESSES = 10000
# How many of the tool's lines may wait for the main loop at once. The loop
# drains every poll, so this is only reached when something holds it for
# seconds (a board reset) on a channel printing thousands of lines a second.
MAX_PENDING_LINES = 200000
# How many chunks the end of an attempt reads before it stops: TAIL_CHUNK each,
# so this is a bounded drain of a file the reader had fallen behind on.
MAX_FINAL_POLLS = 64
# How many attempts a run holds in memory. Past it the middle ones are
# forgotten, oldest and newest kept; every attempt is in progress.jsonl.
MAX_HELD_ATTEMPTS = 2000
# How large one capture file may grow before the run starts another. A
# capture on a crowded advertising channel records every advertiser in range,
# so a run of days would otherwise leave a file of gigabytes in the run
# directory; 0 means no limit.
DEFAULT_CAPTURE_LIMIT_BYTES = 256 * 1024 * 1024
# How much of the captures the whole run keeps. The per-file limit above
# bounds one file; this bounds the run, because what the captures cost is the
# channel's traffic and not the advertiser's: the lab bench measured 1.3 MB
# of records in 30 seconds on channel 37, which is tens of gigabytes over
# three days. Past the budget the oldest complete capture and its transcript
# are removed, newest kept, and what was removed is recorded -- every figure
# the verdict rests on is already counted as the records arrive, so what a
# run loses is the oldest bytes to look at by hand, not its result. 0 means
# no budget.
DEFAULT_CAPTURE_BUDGET_BYTES = 4 * 1024 * 1024 * 1024
# And a cap on how many attempts' files are kept, for the other way a run
# fills a filesystem: a board that stops capturing in the first hour of a
# three-day run restarts every minute or so, and each attempt would leave a
# pair of files behind.
MAX_KEPT_ATTEMPTS = 64
# How many attempts the summary carries in full; progress.jsonl holds them all.
MAX_SUMMARY_ATTEMPTS = 40
# How many of the places a board reset itself the summary names; every read is
# in progress.jsonl.
MAX_WATCHDOG_WHERE = 20
# How many distinct unreadable-capture reasons the summary names; the count
# is kept whole.
MAX_CAPTURE_ERRORS = 20
# What progress.jsonl may grow to. A sample a second over three days is a
# quarter of a million records, and the plan chooses both figures, so the
# file needs a bound of its own: past it the records are counted and the run
# reports what it kept, as the transcript does. summary.json holds the
# figures whatever happened to this file.
MAX_PROGRESS_BYTES = 64 * 1024 * 1024
# How long a capture is given to open its file before the schedule starts
# against it. Bounded by the liveness timeout as well, so this never delays
# the rule that notices a capture which never starts at all.
CAPTURE_READY_TIMEOUT_S = 10.0
# The narrowest presence window and the shortest sampling interval a run may
# be asked for: a window is held per reader per period for the whole run, and
# a sample is a record written and synced.
MIN_WINDOW_S = 0.1
MIN_SAMPLE_EVERY_S = 1.0
# The shortest leg of the schedule, and the most periods a run may hold: each
# period is kept for the whole run, for the detection and parity figures, and
# each one writes two synced progress records.
MIN_SCHEDULE_LEG_S = 1.0
# How many advertising events a presence window must have room for. One is not
# enough: the advertiser adds a random delay to each event and the air loses
# some, so a window holding one event is empty whenever that event is lost or
# late, and sustained presence would fail with nothing wrong.
MIN_EVENTS_PER_WINDOW = 3
MAX_PERIODS = 20000
# How long into an off period the advertiser may still be heard. The stop is
# a shell command and the packets already on the air, and already in the
# tool's own buffers, arrive after it; what is judged is the rest of the
# period.
OFF_SETTLE_S = 2.0
# How long after a probe or a restart the advertiser must be heard again,
# beyond the configured detection deadline: one whole off period can fall in
# between, and the schedule is not re-aligned by a restart.
RECOVERY_SLACK_S = 2.0

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFIGURATION = 3


def say(*text, file=None) -> None:
    """Print one line and flush it.

    A soak's own progress lines are read while it runs, and the harness kills
    this process about two seconds after it cancels a step, so an unflushed
    line is a line nobody sees.
    """
    print(*text, flush=True, file=file or sys.stdout)


class SoakError(RuntimeError):
    """The run cannot be judged: a configuration or bench failure."""


# ---------------------------------------------------------------------------
# Reading the capture file as it grows


class PcapTail:
    """Complete records of a PCAP file that is still being written.

    The tool writes LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR records with libbtbb's
    nanosecond magic, and a reader that waits for the file to be closed could
    not report anything during a run that lasts days. So the header is read
    once, each poll reads what has arrived since the last one, and a record
    that is only partly there is left in the buffer for the next poll. The
    file is never loaded whole, so a capture of any length costs the same
    memory.
    """

    HEADER_LEN = 24
    RECORD_HEADER_LEN = 16
    # The longest record this will wait for. A corrupt record header claiming
    # a huge length would otherwise make the reader hold every later read
    # until that many bytes arrived, which on a run of days is the whole
    # capture in memory. The file's own snaplen is 262144 and an LE record is
    # a few hundred bytes, so anything past this is a broken file, not a
    # record that has not finished arriving.
    MAX_RECORD_LEN = 262144

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None
        self.endian = None
        self.ticks_per_second = advcapture.DEFAULT_TICKS_PER_SECOND
        self.link_type = None
        self.error = ""
        self.records = 0
        self._pending = bytearray()

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def _open(self) -> bool:
        if self.handle is not None:
            return True
        if self.error:
            return False
        try:
            handle = self.path.open("rb")
        except OSError:
            return False  # the tool has not created it yet
        header = handle.read(self.HEADER_LEN)
        if len(header) < self.HEADER_LEN:
            # The header is written in one call, so this is only the moment
            # between create and write; try again next poll.
            handle.close()
            return False
        magic = bytes(header[:4])
        if magic not in advcapture.PCAP_MAGIC:
            handle.close()
            self.error = f"{self.path.name} has magic {magic.hex()}, which is not PCAP"
            return False
        self.endian, self.ticks_per_second = advcapture.PCAP_MAGIC[magic]
        self.link_type = struct.unpack(self.endian + "I", header[20:24])[0]
        if self.link_type != advcapture.LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR:
            handle.close()
            self.error = (f"{self.path.name} has link type {self.link_type}, not "
                          f"{advcapture.LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR}")
            return False
        self.handle = handle
        return True

    def open_header(self) -> bool:
        """Has the tool written the file's header yet?

        Separate from ``poll`` on purpose: the readiness wait asks this, and a
        poll would read the records behind the header and throw them away.
        """
        return self._open()

    @property
    def pending_bytes(self) -> int:
        """Bytes of a record that has not finished arriving."""
        return len(self._pending)

    def poll(self) -> list[tuple[float, bytes]]:
        """(capture time in epoch seconds, record) for every whole record."""
        if not self._open():
            return []
        chunk = self.handle.read(TAIL_CHUNK)
        if chunk:
            self._pending += chunk
        found = []
        while len(self._pending) >= self.RECORD_HEADER_LEN:
            seconds, fraction, caplen, _length = struct.unpack(
                self.endian + "IIII", self._pending[:self.RECORD_HEADER_LEN])
            if caplen > self.MAX_RECORD_LEN:
                self.error = (f"{self.path.name} has a record claiming {caplen} "
                              f"bytes, more than {self.MAX_RECORD_LEN}; the file "
                              "is not readable from here on")
                self._pending.clear()
                self.close()
                return found
            end = self.RECORD_HEADER_LEN + caplen
            if len(self._pending) < end:
                break  # the rest of this record has not arrived yet
            record = bytes(self._pending[self.RECORD_HEADER_LEN:end])
            del self._pending[:end]
            found.append((seconds + fraction / self.ticks_per_second, record))
            self.records += 1
        return found


# ---------------------------------------------------------------------------
# Reading the tool's own output


class LineSink:
    """What the reader thread hands the main loop, under one lock.

    Bounded, because the two run at different rates: the reader takes every
    line the tool prints, while the main loop drains between a capture poll,
    the schedule's shell commands, a watchdog read and a board reset, any of
    which can hold it for seconds. On a crowded channel that is tens of
    thousands of lines, and over three days an unbounded list is a leak. Past
    the bound the oldest are dropped and counted, so the run says it happened
    rather than growing until something stops it.
    """

    def __init__(self, limit: int = MAX_PENDING_LINES):
        self._lock = threading.Lock()
        self._items: deque = deque()
        self.limit = limit
        self.dropped = 0

    def add(self, item) -> None:
        with self._lock:
            if len(self._items) >= self.limit:
                # The oldest goes: a deque, so the drop costs nothing in the
                # case the bound is for.
                self._items.popleft()
                self.dropped += 1
            self._items.append(item)

    def drain(self) -> list:
        with self._lock:
            items, self._items = list(self._items), deque()
        return items


class TranscriptReader(threading.Thread):
    """Read the tool's merged output: keep it, and parse the AdvA lines.

    The thread does the reading because the tool's output has to be consumed
    whatever the main loop is doing: a pipe nobody reads fills up and stops
    the capture. Every line is written to the attempt's own log file until the
    cap, and every line is parsed whether or not it was kept.
    """

    def __init__(self, stream, log_path: Path, sink: LineSink, clock=time.monotonic,
                 max_bytes: int = MAX_LOG_BYTES):
        super().__init__(daemon=True)
        self.stream = stream
        self.log_path = Path(log_path)
        self.sink = sink
        self.clock = clock
        self.max_bytes = max_bytes
        self.lines = 0
        self.written_bytes = 0
        self.dropped_lines = 0
        self.error_lines = 0
        self.last_error = ""
        self.legacy_death_lines = 0
        self.mode_warning = False

    def run(self) -> None:
        with self.log_path.open("wb") as log:
            for raw in self.stream:
                self.lines += 1
                if self.written_bytes + len(raw) <= self.max_bytes:
                    log.write(raw)
                    self.written_bytes += len(raw)
                    log.flush()
                else:
                    self.dropped_lines += 1
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                self._parse(text)

    def _parse(self, text: str) -> None:
        found = ADVA_LINE.search(text)
        if found:
            self.sink.add(("adva", self.clock(), found.group(1).upper()))
            return
        error = ERROR_LINE.search(text)
        if error:
            self.error_lines += 1
            self.last_error = error.group(1)
        if LEGACY_DEATH_LINE.search(text):
            self.legacy_death_lines += 1
        if MODE_WARNING in text:
            self.mode_warning = True


# ---------------------------------------------------------------------------
# What each reader saw, per period


@dataclass
class Side:
    """One reader's sightings of the known address inside one period."""

    packets: int = 0
    windows: set = field(default_factory=set)
    first: float | None = None
    last: float | None = None
    max_gap: float = 0.0
    _gap_from: float | None = None

    def see(self, at: float, period_start: float, window_s: float) -> None:
        self.packets += 1
        # The window is the run's, not the period's: the one place the two
        # readers' buffers really do split a packet is a period boundary, and
        # a period-relative index would make the window before it and the
        # window after it unrelated numbers in different sets.
        self.windows.add(int(at / window_s))
        if self.first is None:
            self.first = at
        self.last = at
        reference = period_start if self._gap_from is None else self._gap_from
        self.max_gap = max(self.max_gap, at - reference)
        self._gap_from = at

    def close(self, period_end: float, period_start: float) -> None:
        """The gap that runs to the end of the period counts too.

        A reader that heard the advertiser for ten seconds and then nothing
        for the rest of its on period has a gap, and it is the one a service
        would have noticed.
        """
        reference = period_start if self._gap_from is None else self._gap_from
        self.max_gap = max(self.max_gap, period_end - reference)

    def sustained(self, needed: int) -> bool:
        """Was it seen in `needed` windows in a row?"""
        if needed <= 0:
            return True
        run = 0
        previous = None
        for index in sorted(self.windows):
            run = run + 1 if previous is not None and index == previous + 1 else 1
            previous = index
            if run >= needed:
                return True
        return False

    def to_dict(self) -> dict:
        return {
            "packets": self.packets,
            "windows": len(self.windows),
            "first_s": None if self.first is None else round(self.first, 3),
            "last_s": None if self.last is None else round(self.last, 3),
            "max_gap_s": round(self.max_gap, 3),
        }


def _lonely_windows(mine: set, theirs: set) -> int:
    """How many of ``mine`` have no window of ``theirs`` beside them."""
    return sum(1 for window in mine
               if not {window - 1, window, window + 1} & theirs)


@dataclass
class Period:
    """One leg of the advertiser's schedule, as it was really applied."""

    index: int
    state: str  # "on" or "off"
    started: float
    ended: float | None = None
    text: Side = field(default_factory=Side)
    structural: Side = field(default_factory=Side)
    payload_matches: int = 0
    schedule_error: str = ""
    # A capture was stopped or started while this period was running, so for
    # part of it nothing was listening. Counted apart by whether the run
    # meant to do it: the probe and a file rotation are planned, a restart
    # after a death or a silence is not. Either way the period is not judged
    # -- nothing was listening for part of it -- but only the unplanned ones
    # count against the share of its periods a run has to be able to judge.
    planned_gaps: int = 0
    unplanned_gaps: int = 0
    # The duration ended while this period was running, so the run itself cut
    # it short. Every run ends in one, and it is the run's own doing, so it
    # is not counted against the share of its periods a run has to be able
    # to judge.
    cut_by_duration: bool = False
    # When something was listening again, for a period a capture came back
    # inside. The off-period check judges only the stretch past its settle
    # time, so a gap that ended before that stretch began left the part
    # being judged fully captured -- which is what a file rolled over at the
    # start of an off period is, by design.
    listening_from: float | None = None

    @property
    def capture_gap(self) -> bool:
        return bool(self.planned_gaps or self.unplanned_gaps)

    @property
    def seconds(self) -> float:
        return 0.0 if self.ended is None else self.ended - self.started

    def disagreements(self) -> int:
        """Windows one reader found the address in and the other did not.

        A window next to one the other reader did have is not counted. Both
        readers see their packets later than the packets themselves -- the
        tool's own output goes through a stdio buffer on its way to the pipe,
        and its capture file through another on its way to the disk -- and
        the two buffers do not fill together, so a packet near a window's
        edge lands either side of it depending on which buffer flushed
        first. The bench showed exactly that: two windows the record reader
        had alone in a one-minute run of 238 packets, with nothing wrong.
        What the comparison is for is a reader that misses the advertiser
        while the other sees it, which is a run of windows and not an edge.
        """
        return (_lonely_windows(self.text.windows, self.structural.windows)
                + _lonely_windows(self.structural.windows, self.text.windows))

    def to_dict(self) -> dict:
        return {
            "record": "period",
            "index": self.index,
            "state": self.state,
            "started_s": round(self.started, 3),
            "ended_s": None if self.ended is None else round(self.ended, 3),
            "text": self.text.to_dict(),
            "structural": self.structural.to_dict(),
            "payload_matches": self.payload_matches,
            "window_disagreements": self.disagreements(),
            "schedule_error": self.schedule_error,
            "capture_gap": self.capture_gap,
            "planned_capture_gaps": self.planned_gaps,
            "unplanned_capture_gaps": self.unplanned_gaps,
            "cut_by_duration": self.cut_by_duration,
        }


# ---------------------------------------------------------------------------
# Options


@dataclass(frozen=True)
class Timings:
    """The figures whose relationships decide whether a run can be judged.

    Apart on purpose: the same relationships are checked when a plan loads,
    where there is no board, no output directory and no advertiser, and
    checking them twice from two copies of the rules is how they would come
    to differ (catalog.`_check_address_soak`).
    """

    duration_s: float
    on_s: float
    off_s: float
    window_s: float
    windows_needed: int
    liveness_timeout_s: float
    detect_within_s: float
    max_gap_s: float
    sample_every_s: float
    # How often the advertiser sends, and the rate floor the run is judged
    # against: both decide verdicts, so both are checked here rather than on
    # the bench.
    interval_ms: int = 100
    min_packets_per_minute: int = 0
    # Whether the run kills its own capture once, which the duration has to
    # leave room for: the probe is only evidence if a period is judged after
    # it (`probe_outcome`).
    restart_probe: bool = False


@dataclass
class Options:
    out: Path
    serial: str
    address: str
    channel: int
    duration_s: float
    jlink: str
    dk_port: str | None
    dk_alias: str
    interval_ms: int
    payload: str
    on_s: float
    off_s: float
    sample_every_s: float
    window_s: float
    windows_needed: int
    liveness_timeout_s: float
    detect_within_s: float
    min_detection_share: float
    max_gap_s: float
    min_packets_per_minute: int
    max_disagreement_share: float
    max_restarts: int
    max_memory_growth_kb: int | None
    restart_probe: bool
    capture_limit_bytes: int
    capture_budget_bytes: int
    term_grace_s: float
    restart_min_interval_s: float


    def timings(self) -> Timings:
        return Timings(
            restart_probe=self.restart_probe,
            interval_ms=self.interval_ms,
            min_packets_per_minute=self.min_packets_per_minute,
            duration_s=self.duration_s, on_s=self.on_s, off_s=self.off_s,
            window_s=self.window_s, windows_needed=self.windows_needed,
            liveness_timeout_s=self.liveness_timeout_s,
            detect_within_s=self.detect_within_s, max_gap_s=self.max_gap_s,
            sample_every_s=self.sample_every_s,
        )


def check_timings(options: Timings | Options) -> None:
    """Refuse a set of figures that could not judge what it asks for.

    The catalog makes the same checks on the plan's arguments before any
    board is leased (catalog.py, `_check_address_soak`); these are here so
    the driver is sound when it is run by hand too.
    """
    if isinstance(options, Options):
        options = options.timings()
    # A schedule in milliseconds is legal as a duration and would leave a
    # three-day run holding a million periods and writing a synced record for
    # each of them, so both the leg and the number of legs are bounded.
    for name, seconds in (("advertiser on", options.on_s),
                          ("advertiser off", options.off_s)):
        if seconds < MIN_SCHEDULE_LEG_S:
            raise SoakError(
                f"'{name}' ({seconds:g}s) is shorter than {MIN_SCHEDULE_LEG_S:g}s; "
                "every leg of the schedule is a period the run holds")
    periods = 2 * options.duration_s / (options.on_s + options.off_s)
    if periods > MAX_PERIODS:
        raise SoakError(
            f"{options.duration_s:g}s of {options.on_s:g}s on and "
            f"{options.off_s:g}s off is about {periods:.0f} periods, more than "
            f"{MAX_PERIODS}; the run holds every one of them")
    # Each reason names the figure as a plan writes it ('detect within') and
    # not as this command line spells it, because a plan is where these are
    # usually wrong and the catalog raises these same words (the driver's
    # docstring pairs each one with its option).
    if options.detect_within_s > options.on_s:
        raise SoakError(
            f"'detect within' ({options.detect_within_s:g}s) is longer than the "
            f"{options.on_s:g}s the advertiser is on, so a period could end "
            "before its own detection deadline")
    if options.window_s * options.windows_needed > options.on_s:
        raise SoakError(
            f"sustained presence ({options.windows_needed} x "
            f"{options.window_s:g}s windows) does not fit in the "
            f"{options.on_s:g}s the advertiser is on")
    # The quiet stretch a healthy run really has is an off period and then
    # the wait for the next period's first packet, so the timeout has to
    # clear both; clearing the off period alone would restart the capture
    # every cycle on a bench where nothing else is advertising.
    if options.liveness_timeout_s <= options.off_s + options.detect_within_s:
        raise SoakError(
            f"'liveness timeout' ({options.liveness_timeout_s:g}s) is not "
            f"longer than an off period and the wait for the next detection "
            f"({options.off_s:g}s + {options.detect_within_s:g}s), so the "
            "schedule's own silence would restart the capture")
    # A gap is measured inside one on period, from its start to its end, so
    # the limit has to sit between the detection deadline -- every period's
    # leading gap is at least the time to its first sighting -- and the
    # period's own length, past which no gap could ever reach it.
    if options.max_gap_s < options.detect_within_s:
        raise SoakError(
            f"'maximum gap' ({options.max_gap_s:g}s) is shorter than 'detect "
            f"within' ({options.detect_within_s:g}s), so the wait for the "
            "first packet of a period would fail it")
    if options.max_gap_s >= options.on_s:
        raise SoakError(
            f"'maximum gap' ({options.max_gap_s:g}s) is not shorter than the "
            f"{options.on_s:g}s the advertiser is on, so no gap inside a period "
            "could ever reach it")
    cycle = options.on_s + options.off_s
    if options.restart_probe and options.duration_s < 3 * cycle:
        # The probe fires at the middle of the run and the period it lands in
        # is not judged, so a whole cycle has to follow it for the recovery
        # to be measured at all.
        raise SoakError(
            f"a restart probe needs a duration of at least three {cycle:g}s "
            f"schedule cycles, so a whole period follows the probe; this is "
            f"{options.duration_s:g}s")
    if options.duration_s < 2 * cycle:
        raise SoakError(
            f"the duration ({options.duration_s:g}s) is less than two "
            f"{cycle:g}s schedule cycles, so there is nothing to compare")
    if options.sample_every_s > options.duration_s / 3:
        raise SoakError(
            f"'sample every' ({options.sample_every_s:g}s) leaves fewer than "
            f"three samples in {options.duration_s:g}s")
    # Both of these are bounded below as well, because the run holds a window
    # per reader per period and writes a record per sample: a millisecond
    # either way is legal as a duration and would cost a long run hundreds of
    # millions of windows, or a synced write every poll.
    if options.window_s < MIN_WINDOW_S:
        raise SoakError(
            f"'presence window' ({options.window_s:g}s) is shorter than "
            f"{MIN_WINDOW_S:g}s; a run holds a window per reader per period")
    if options.sample_every_s < MIN_SAMPLE_EVERY_S:
        raise SoakError(
            f"'sample every' ({options.sample_every_s:g}s) is shorter than "
            f"{MIN_SAMPLE_EVERY_S:g}s; each sample is a synced record")
    # The advertising interval decides two verdicts, so it is checked with
    # the figures they are judged by: an advertiser sending less often than a
    # window is wide cannot fill the windows sustained presence needs, and one
    # whose own interval sends fewer packets a minute than the floor asks for
    # cannot clear it however well the capture works.
    interval_s = options.interval_ms / 1000.0
    if interval_s * MIN_EVENTS_PER_WINDOW > options.window_s:
        raise SoakError(
            f"the advertiser's {interval_s:g}s interval leaves fewer than "
            f"{MIN_EVENTS_PER_WINDOW} advertising events in a "
            f"{options.window_s:g}s presence window, so a window would be empty "
            "whenever one event is lost or late")
    at_best = 60.0 / interval_s
    if at_best < options.min_packets_per_minute:
        raise SoakError(
            f"an advertiser sending every {interval_s:g}s sends about "
            f"{at_best:.0f} packets a minute, fewer than the "
            f"{options.min_packets_per_minute} 'minimum packets per minute' "
            "asks for")


# ---------------------------------------------------------------------------
# The advertiser's schedule


class DkShell:
    """One DK, held open for the whole run, driven a command at a time.

    One shell for the run rather than one command per transition: a 72-hour
    run turns a fixture on and off thousands of times, and the DK's
    virtual COM port does not reset the board when it is opened, so there is
    nothing to be gained by closing it. A transition that fails is tried once
    more on a freshly opened shell, because a dropped port is the failure
    this is likely to meet, and a transition that fails twice raises, so the
    caller records it against the period or phase it belongs to.

    Apart from `Advertiser` so another soak can hold a DK the same way: the
    retry and the counters are the part a long run depends on, and two copies
    of them would be two places for a fix to land (followsoak.py).
    """

    def __init__(self, dk: nrfdk.Dk):
        self.dk = dk
        self.shell = None
        self.transitions = 0
        self.retries = 0
        self.errors = 0

    def open(self) -> None:
        self.shell = nrfdk._open(self.dk)

    def close(self) -> None:
        if self.shell is not None:
            try:
                self.shell.close()
            except OSError:
                pass
            self.shell = None

    def _reopen(self) -> None:
        self.close()
        self.open()

    def _shell(self):
        if self.shell is None:
            # A reopen that failed leaves none: every caller handles a
            # SoakError, and an attribute error on the next transition would
            # end the run in a traceback with no report.
            raise SoakError("the advertiser's shell is not open")
        return self.shell

    def command(self, text: str) -> list[str]:
        """One shell command, retried once on a reopened shell."""
        self.transitions += 1
        try:
            return self._shell().command(text)
        except (nrfdk.NrfDkError, OSError) as first:
            self.retries += 1
            try:
                self._reopen()
                return self._shell().command(text)
            except (nrfdk.NrfDkError, OSError) as second:
                self.errors += 1
                raise SoakError(f"'{text}' failed twice: {first}; then {second}") from second

    def status(self) -> dict:
        try:
            return self._shell().status()
        except (nrfdk.NrfDkError, OSError) as first:
            self.retries += 1
            try:
                self._reopen()
                return self._shell().status()
            except (nrfdk.NrfDkError, OSError) as second:
                self.errors += 1
                raise SoakError(f"status failed twice: {first}; then {second}") from second


class Advertiser(DkShell):
    """The advertiser DK, turned on and off by this soak's own schedule."""

    def configure(self, options: Options) -> dict:
        """Settings for the whole run, then the address and counter it reports."""
        for command in ("adv stop", "adv type nonconn", f"adv chmap {options.channel}",
                        f"adv interval {options.interval_ms}",
                        f"adv payload {options.payload or 'none'}"):
            self.command(command)
        status = self.status()
        address = nrfdk.normalize_address(status["address"])
        if address != options.address:
            raise SoakError(
                f"the advertiser reports {address}, the registry says {options.address}")
        if status["adv"] or status["running"]:
            raise SoakError("the advertiser is still advertising after 'adv stop'")
        return status

    def start(self) -> None:
        self.command("adv start")

    def stop(self) -> None:
        self.command("adv stop")


# ---------------------------------------------------------------------------
# The board


def run_util(serial: str, *options: str, timeout: float = UTIL_TIMEOUT_S):
    argv = [UTIL, "-U", serial, *options]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout:g}s")
    except OSError as error:
        return subprocess.CompletedProcess(argv, 127, "", str(error))


def board_answers(serial: str) -> tuple[bool, str]:
    """Does the board answer a read? The reason when it does not.

    A board another program holds, or one that has gone, is a configuration
    failure rather than something to reset: resetting it would spoil whatever
    is using it as well as this run.
    """
    result = run_util(serial, "-v")
    text = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return True, ""
    reason = ""
    for line in text.splitlines():
        if ERROR_LINE.search(line):
            reason = line.strip()
            break
    return False, reason or f"{UTIL} -v exited {result.returncode}"


def read_watchdog(serial: str, tries: int = 1,
                  pause_s: float = WATCHDOG_RETRY_PAUSE_S
                  ) -> tuple[watchdog.WatchdogRecord | None, str]:
    """The firmware's watchdog record, or why there is none.

    ``tries`` is for the read after a reset, which is the one
    SqueakyFloors/ubertooth#703 asks for by name: `ubertooth-util -r` waits
    about two seconds for the board to come back and the board refuses every
    request until the host has noticed it re-enumerate, so a single read
    there is the one most likely to find nothing and record "the board did
    not answer" for a board that is perfectly well.
    """
    text = ""
    for attempt in range(max(1, tries)):
        if attempt:
            time.sleep(pause_s)
        result = run_util(serial, "-w")
        text = (result.stdout or "") + (result.stderr or "")
        record = watchdog.parse_report(text)
        if record is not None:
            return record, ""
    if watchdog.board_unreachable(text):
        return None, "the board did not answer the watchdog read"
    return None, "this firmware does not report a watchdog record"


def resident_kb(pid: int, run=subprocess.run) -> int | None:
    """The capture process's resident set size in KiB, or None.

    `ps` rather than a library: it is on every host the harness runs on, it
    needs no privilege, and nothing has to be installed for a soak to report
    memory. A host that will not run it at all, or that answers something
    this cannot read, costs the memory figures and nothing else, so every
    failure here is None rather than an error.
    """
    try:
        result = run(["ps", "-o", "rss=", "-p", str(pid)],
                     capture_output=True, text=True, timeout=5.0, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or "").strip().split()
    if result.returncode != 0 or not value:
        return None
    try:
        return int(value[0])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# One capture attempt


@dataclass
class Attempt:
    """One run of the capture tool, from start to however it ended."""

    index: int
    argv: tuple[str, ...]
    capture: str
    log: str
    pid: int = 0
    started: float = 0.0
    ended: float | None = None
    returncode: int | None = None
    signalled: str = ""
    stop_reason: str = ""
    forced: bool = False
    last_error: str = ""
    error_lines: int = 0
    legacy_death_lines: int = 0
    transcript_lines: int = 0
    transcript_dropped: int = 0
    records: int = 0
    mode_warning: bool = False
    # The capture and the transcript this attempt left, and whether the run's
    # budget has since taken them away again.
    bytes_kept: int = 0
    files_removed: bool = False
    partial_tail_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "record": "attempt",
            "index": self.index,
            "pid": self.pid,
            "capture": self.capture,
            "log": self.log,
            "started_s": round(self.started, 3),
            "ended_s": None if self.ended is None else round(self.ended, 3),
            "returncode": self.returncode,
            "signalled": self.signalled,
            "stop_reason": self.stop_reason,
            "forced": self.forced,
            "last_error": self.last_error,
            "error_lines": self.error_lines,
            "legacy_death_lines": self.legacy_death_lines,
            "transcript_lines": self.transcript_lines,
            "transcript_dropped_lines": self.transcript_dropped,
            "records": self.records,
            "found_board_in_a_mode": self.mode_warning,
            "bytes_kept": self.bytes_kept,
            "files_removed": self.files_removed,
            "partial_tail_bytes": self.partial_tail_bytes,
        }


class RecordClock:
    """A capture record's time on the run's own timeline.

    The time in a record is the board's clock, anchored once when the
    capture's first packet arrived and advanced by the board's own counter
    (libubertooth's ``now_ns_from_clk100ns``). Over hours that walks away
    from the host clock -- the crystal's drift, and a whole clock period if a
    quiet gap loses a wrap, which the fork itself warns about -- and the
    windows a soak compares in are seconds wide. So the record clock is used
    for the resolution between records, anchored to the host time the record
    was read, and re-anchored whenever the two have drifted more than
    ``reanchor_s`` apart. Every re-anchor is counted and the largest drift is
    kept, so a run says what its capture clock did rather than hiding it.

    What this does not do is make two readers independent in their clocks:
    both end up on the host's timeline, which is the honest position, because
    what a reader comparison is about is the decode -- the printed line
    against the record's own bytes and CRC -- and never was about whose clock
    was right.
    """

    def __init__(self, reanchor_s: float):
        self.reanchor_s = reanchor_s
        self.reanchors = 0
        self.drift_max_s = 0.0
        self._anchor = None

    def restart(self) -> None:
        """Forget the anchor: a new capture has its own clock."""
        self._anchor = None

    def at(self, epoch: float, host: float) -> float:
        if self._anchor is None:
            self._anchor = (epoch, host)
            return host
        anchor_epoch, anchor_host = self._anchor
        at = anchor_host + (epoch - anchor_epoch)
        drift = host - at
        if abs(drift) > self.reanchor_s:
            self.reanchors += 1
            self.drift_max_s = max(self.drift_max_s, abs(drift))
            self._anchor = (epoch, host)
            return host
        return at

    def to_dict(self) -> dict:
        return {"reanchors": self.reanchors,
                "largest_drift_s": round(self.drift_max_s, 3),
                "reanchor_at_s": self.reanchor_s}


class Capture:
    """The capture process, its transcript reader, and its capture tail."""

    def __init__(self, options: Options, index: int, sink: LineSink,
                 clock=time.monotonic, max_log_bytes: int = MAX_LOG_BYTES,
                 mode: str = "-n"):
        self.options = options
        self.clock = clock
        self.max_log_bytes = max_log_bytes
        self.capture_path = options.out / f"capture-{index:04d}.pcap"
        self.log_path = options.out / f"btle-{index:04d}.log"
        # ``mode`` is the tool's own mode option: `-n` for advertising only,
        # which is what this soak runs, and `-f` for a soak that has to
        # follow a connection (followsoak.py). Everything else about an
        # attempt -- the capture file, the transcript, the signals, the
        # exit -- is the same whichever mode is running, so the mode is an
        # argument rather than a second copy of this class.
        self.mode = mode
        self.argv = (TOOL, mode, "-U", options.serial, f"-A{options.channel}",
                     "-q", str(self.capture_path))
        self.attempt = Attempt(index, self.argv, self.capture_path.name,
                               self.log_path.name)
        self.process = None
        self.reader = None
        self.tail = PcapTail(self.capture_path)
        self.sink = sink
        self._mode_warning = False

    def start(self) -> None:
        self.attempt.started = self.clock()
        try:
            # In this process's own process group, not a session of its own.
            # Two things follow, and both are wanted. The harness signals the
            # group when it cancels a step or reaches its deadline, so the
            # tool is asked to stop -- and stops the board -- even if this
            # process never runs again; and a SIGKILL that ends this process
            # ends the capture with it rather than leaving a tool holding the
            # board. Signals this driver sends on purpose go to the pid
            # alone, so the probe's kill and the measured SIGTERM grace are
            # still exactly this tool's.
            self.process = subprocess.Popen(
                self.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
        except OSError as error:
            raise SoakError(f"cannot run {TOOL}: {error}") from error
        self.attempt.pid = self.process.pid
        self.reader = TranscriptReader(self.process.stdout, self.log_path, self.sink,
                                       clock=self.clock,
                                       max_bytes=self.max_log_bytes)
        self.reader.start()

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def poll_records(self) -> list[tuple[float, bytes]]:
        return self.tail.poll()

    def stop(self, reason: str, *, kill: bool = False,
             grace_s: float | None = None) -> Attempt:
        """End this attempt: SIGTERM and wait, or SIGKILL when asked.

        ``kill`` is the probe's path and the one a lost terminal takes: the
        tool never runs its exit path, so the board is left in its mode and
        the next attempt has to clear it. ``grace_s`` overrides how long the
        tool is given, which the cancellation path needs: the harness allows
        about two seconds between its SIGTERM and its SIGKILL, so a teardown
        that waited the ordinary grace would be killed before it wrote the
        run's report.
        """
        self.attempt.stop_reason = reason
        grace = self.options.term_grace_s if grace_s is None else grace_s
        process = self.process
        if process is not None and process.poll() is None:
            if kill:
                self.attempt.forced = True
                self.attempt.signalled = "SIGKILL"
                self.signal_tool(signal.SIGKILL)
            else:
                self.attempt.signalled = "SIGTERM"
                self.signal_tool(signal.SIGTERM)
                if grace > 0 and not self._wait(grace):
                    self.attempt.forced = True
                    self.attempt.signalled = "SIGTERM then SIGKILL"
                    self.signal_tool(signal.SIGKILL)
        if process is not None:
            try:
                self.attempt.returncode = process.wait(
                    timeout=1.0 if grace <= 0 else 30)
            except subprocess.TimeoutExpired:
                self.attempt.returncode = None
        self.finish()
        return self.attempt

    def signal_tool(self, number) -> None:
        """Signal the tool itself, never the group this process is in.

        The signal is recorded on the attempt here rather than only where
        the stop is decided, because a cancellation signals the tool from the
        signal handler: without this the record of a canceled run would say
        the tool was never asked to stop.
        """
        process = self.process
        if process is None:
            return
        if not self.attempt.signalled:
            self.attempt.signalled = signal.Signals(number).name
        try:
            process.send_signal(number)
        except (ProcessLookupError, PermissionError, OSError):
            pass


    def _wait(self, seconds: float) -> bool:
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            if self.process.poll() is not None:
                return True
            time.sleep(min(POLL_S, max(0.01, deadline - self.clock())))
        return self.process.poll() is not None

    def finish(self) -> Attempt:
        """Collect what the attempt left, once the process is gone."""
        if self.attempt.ended is None:
            self.attempt.ended = self.clock()
        if self.process is not None and self.attempt.returncode is None:
            self.attempt.returncode = self.process.poll()
        if self.reader is not None:
            self.reader.join(timeout=1 if self.attempt.returncode is None else 30)
            self.attempt.last_error = self.reader.last_error
            self.attempt.error_lines = self.reader.error_lines
            self.attempt.legacy_death_lines = self.reader.legacy_death_lines
            self.attempt.transcript_lines = self.reader.lines
            self.attempt.transcript_dropped = self.reader.dropped_lines
            # Read from the lines themselves, not from the log file: the
            # transcript budget belongs to the whole run, so a later
            # attempt's log can be empty while its warning was printed.
            self.attempt.mode_warning = self.reader.mode_warning
        if (self.process is not None and self.process.stdout is not None
                and (self.reader is None or not self.reader.is_alive())):
            # Only once the reader has finished with it: closing a stream the
            # thread is still iterating raises in the thread and prints a
            # traceback into the step's own output.
            try:
                self.process.stdout.close()
            except OSError:
                pass
        self.attempt.records = self.tail.records
        kept = 0
        for path in (self.capture_path, self.log_path):
            try:
                kept += path.stat().st_size
            except OSError:
                continue
        self.attempt.bytes_kept = kept
        return self.attempt

    def close(self) -> None:
        self.tail.close()


def prune_capture_files(attempts, out: Path, budget_bytes: int, record,
                        *, max_kept: int = MAX_KEPT_ATTEMPTS,
                        stuck_already: bool = False):
    """Keep the newest captures inside a run's budget: (removed, bytes, stuck).

    Only complete attempts are considered, so the capture being written is
    never touched. Each removal is recorded through ``record`` with the
    file's size, so the run says what it had and what it let go.

    ``stuck`` is non-empty when the budget cannot be kept at all: over it,
    with nothing left that can be removed. Recording that and carrying on
    would leave the run writing captures past the budget for however long it
    had left, which is the one thing the budget is there to prevent, so the
    caller stops the run and fails instead. ``stuck_already`` says the caller
    has already latched that, so it is reported once per run.

    Apart from `Soak` because two soaks keep their captures the same way and
    this is where the mistakes were: an attempt whose files would not go away
    has to stay in the accounting, or a single undeletable file disables the
    budget for the rest of a run of days (followsoak.py).
    """
    kept = [attempt for attempt in attempts if not attempt.files_removed]
    # Attempts whose files would not go away: passed over rather than
    # returned on, so one of them cannot disable the budget for the rest
    # of a run of days.
    skipped: list = []
    over_budget = False
    removed_attempts = 0
    removed_bytes = 0
    # Never the last one standing: the newest capture is the one a reader
    # of a failed run wants, and "newest kept" is what this promises.
    while len(kept) > 1:
        total = sum(attempt.bytes_kept for attempt in kept + skipped)
        over_budget = budget_bytes > 0 and total > budget_bytes
        if not over_budget and len(kept) <= max_kept:
            break
        oldest = kept.pop(0)
        removed = 0
        left = []
        for name in (oldest.capture, oldest.log):
            path = out / name
            try:
                size = path.stat().st_size
            except OSError:
                continue  # already gone, or never written
            try:
                path.unlink()
                removed += size
            except OSError:
                left.append(name)
        if left:
            skipped.append(oldest)
            # A file that is still there has not been given back to the
            # filesystem, so this attempt stays in the accounting and the
            # run says nothing it did not do. Earlier versions put it back
            # after unlinking, which deleted the files and then re-targeted
            # the same attempt for ever, leaving the budget disabled for the
            # rest of the run.
            record({"record": "capture_not_removed",
                    "index": oldest.index, "files": left})
            continue
        # Marked whether or not there were bytes to free: an attempt whose
        # files were empty, or never written, is still one fewer attempt's
        # worth of files in the directory.
        oldest.files_removed = True
        removed_attempts += 1
        removed_bytes += removed
        record({"record": "capture_removed", "index": oldest.index,
                "capture": oldest.capture, "log": oldest.log,
                "bytes": removed,
                "reason": ("the run's capture budget" if over_budget
                           else f"more than {max_kept} attempts")})
    total = sum(attempt.bytes_kept for attempt in kept + skipped)
    stuck: list = []
    if budget_bytes > 0 and total > budget_bytes and skipped and not stuck_already:
        stuck = sorted(name for attempt in skipped
                       for name in (attempt.capture, attempt.log))
        record({"record": "capture_budget_stuck", "bytes_kept": total,
                "budget": budget_bytes, "files": stuck})
        say(f"the capture budget cannot be kept: {total} bytes on disk against "
            f"{budget_bytes}, and {len(stuck)} file(s) could not be removed; "
            "stopping the run")
    return removed_attempts, removed_bytes, stuck


# ---------------------------------------------------------------------------
# The run


class Soak:
    """The whole run: the schedule, the capture, the readers, the verdict."""

    def __init__(self, options: Options, dk: nrfdk.Dk, clock=time.monotonic,
                 wall=time.time):
        self.options = options
        self.clock = clock
        self.wall = wall
        self.advertiser = Advertiser(dk)
        self.sink = LineSink()
        self.periods: list[Period] = []
        self.attempts: list[Attempt] = []
        self.watchdog_reads = 0
        self.watchdog_where: list = []
        self.watchdog_baseline = None
        self.watchdog_resets = 0
        self.samples = 0
        self.rss_first = None
        self.attempt_rss_first = None
        self.attempt_rss_last = None
        self.rss_growth_max = 0
        self.rss_last = None
        self.rss_max = None
        self.other_packets = 0
        self.attempt_text_packets = 0
        self.unattributed_packets = 0
        # Problems a fault outside judge() has to add (report_error).
        self.extra_problems: list = []
        self.capture_errors: list = []
        self.capture_error_count = 0
        self._seen_capture_errors: set = set()
        self.attempts_forgotten = 0
        self.captures_removed = 0
        self.capture_bytes_removed = 0
        # Files the budget needs gone and cannot remove: the run stops.
        self.capture_budget_stuck: list = []
        self.progress_bytes = 0
        self.progress_records_dropped = 0
        # The closing stop-and-read, which no period is judged on.
        self.closing_error = None
        self.other_addresses: set = set()
        self.structural_packets = 0
        self.text_packets = 0
        self.payload_matches = 0
        self.crc_dropped = 0
        self.records_seen = 0
        self.restarts = 0
        self.rotations = 0
        self.probe_restarts = 0
        self.board_resets = 0
        self.unexplained_exits = 0
        self.forced_stops = 0
        self.schedule_errors = 0
        self.canceled = ""
        # When the schedule started, which is what the duration measures.
        self.window_start = 0.0
        # The capture record clock, anchored to the host's (RecordClock).
        # Half a window: a record placed further from the host's reading than
        # that could fall in the wrong window, which is what the comparison
        # counts.
        self.record_clock = RecordClock(max(0.25, options.window_s / 2))
        # Shared by every attempt, so restarts and file rotations cannot
        # multiply what the transcript costs (MAX_LOG_BYTES).
        self.transcript_budget = MAX_LOG_BYTES
        self.transcript_bytes = 0
        self.transcript_dropped_lines = 0
        self.start_mono = clock()
        self.started_wall = wall()
        self.counter_before = None
        self.counter_after = None
        self.capture: Capture | None = None
        self.capture_index = 0
        self.probe_done = not options.restart_probe
        self.probe_at = options.duration_s / 2 if options.restart_probe else None
        self.probe_recovered = None
        self.probe_period_index = None
        self.last_packet_at = 0.0
        self.last_restart_at = -options.restart_min_interval_s
        self.reset_next_restart = False
        self.progress = None
        self.problems: list[str] = []
        self.board_after: dict = {}

    def cancel(self, name: str) -> None:
        """Stop as soon as possible, from a signal handler.

        The capture is asked to stop here rather than after the loop has
        noticed, because the harness allows about two seconds between its
        SIGTERM and its SIGKILL: by the time the loop runs again this process
        may be gone. Asking the tool first means the board is told to stop in
        every case, and one syscall is all this does.
        """
        self.canceled = name
        if self.capture is not None:
            self.capture.signal_tool(signal.SIGTERM)

    # -- bookkeeping ------------------------------------------------------

    def record(self, entry: dict) -> None:
        """Append one record to progress.jsonl and get it onto the disk.

        A long run is reviewed from the run directory while it is still
        going, and after whatever started it has gone, so each record is
        flushed and synced as it is written. They are rare -- one per sample
        and one per schedule period -- so the cost is not in the way.
        """
        if self.progress is None:
            return
        entry = {"at_s": round(self.clock() - self.start_mono, 3), **entry}
        line = json.dumps(entry, sort_keys=True) + "\n"
        # Bounded like the transcript, and for the same reason: a plan may
        # ask for a sample a second over three days, and nothing else here
        # stops the file that holds them from growing with the run. Past the
        # budget the records are counted rather than written, and the run
        # reports what it kept; summary.json carries the figures either way.
        if self.progress_bytes + len(line) > MAX_PROGRESS_BYTES:
            self.progress_records_dropped += 1
            return
        self.progress_bytes += len(line)
        self.progress.write(line)
        self.progress.flush()
        try:
            os.fsync(self.progress.fileno())
        except OSError:
            pass

    @property
    def elapsed(self) -> float:
        return self.clock() - self.start_mono

    @property
    def window_elapsed(self) -> float:
        """How long the schedule has been running, which the duration bounds."""
        return self.elapsed - self.window_start

    def period(self) -> Period | None:
        return self.periods[-1] if self.periods else None

    # -- the schedule -----------------------------------------------------

    def open_period(self, state: str) -> Period:
        """Tell the advertiser, then start counting the period.

        In that order, so the time the shell command itself takes is not
        charged to the detection deadline: a period begins when the
        advertiser has been told to start, not when the run decided to tell
        it. The command is bounded by the shell's own timeout, and a
        transition that failed is recorded against the period it belongs to,
        which leaves that period out of the verdict.
        """
        failure = ""
        try:
            if state == "on":
                self.advertiser.start()
            else:
                self.advertiser.stop()
        except SoakError as error:
            failure = str(error)
            self.schedule_errors += 1
        period = Period(len(self.periods), state, self.elapsed,
                        schedule_error=failure)
        self.periods.append(period)
        self.record({"record": "period_started", "index": period.index,
                     "state": state, "schedule_error": period.schedule_error})
        return period

    def close_period(self, period: Period) -> None:
        period.ended = self.elapsed
        if period.state == "on":
            period.text.close(period.ended, period.started)
            period.structural.close(period.ended, period.started)
        self.record(period.to_dict())

    # -- the readers ------------------------------------------------------

    def record_time(self, epoch: float) -> float:
        """A capture record's time on this run's own timeline (RecordClock)."""
        return self.record_clock.at(epoch, self.clock() - self.start_mono)

    def period_at(self, at: float) -> Period | None:
        """The period a sighting belongs to, by its own time.

        Not the period running now: both readers hand their sightings over
        later than the sighting itself -- the tool's output arrives in blocks
        and the capture's records are read on the next poll -- so attributing
        them to whatever period the loop is in would charge a packet from the
        end of one period to the start of the next. Searched from the newest
        period, where almost every sighting belongs.
        """
        for period in reversed(self.periods):
            if at >= period.started and (period.ended is None or at < period.ended):
                return period
        return None

    def read_text(self) -> None:
        for kind, at, address in self.sink.drain():
            if kind != "adva":
                continue
            seen = at - self.start_mono
            self.last_packet_at = max(self.last_packet_at, seen)
            if address != self.options.address:
                self.other_packets += 1
                if len(self.other_addresses) < MAX_OTHER_ADDRESSES:
                    self.other_addresses.add(address)
                continue
            self.text_packets += 1
            self.attempt_text_packets += 1
            period = self.period_at(seen)
            if period is not None:
                period.text.see(seen, period.started, self.options.window_s)
            else:
                # Between two periods: a period ends before the next one's
                # schedule command and the next begins after it, so the
                # command's own latency belongs to neither. Counted, so the
                # per-period totals and the run's can be reconciled.
                self.unattributed_packets += 1

    def read_structural(self) -> None:
        if self.capture is None:
            return
        records = self.capture.poll_records()
        error = self.capture.tail.error
        if error and error not in self._seen_capture_errors:
            # One corrupt record header, a file that is not a capture, or the
            # wrong link type stops the structural reader for the rest of the
            # attempt. Reported and judged: without this the run would fail
            # later for a reason that is a symptom (the readers disagreed, or
            # no advertiser packet at all) on the very fault this test is
            # here to diagnose.
            self._note_capture_error(error)
            say(f"capture {self.capture_index}: {error}")
            self.record({"record": "capture_unreadable",
                          "index": self.capture_index, "reason": error})
        if records:
            # Any record at all is the capture working, whether or not
            # libbtbb printed an AdvA line for it: the liveness rule is about
            # a capture that has stopped producing, and a run that watched
            # only the text would restart a healthy capture of traffic that
            # decodes to something else.
            self.last_packet_at = max(self.last_packet_at,
                                      self.clock() - self.start_mono)
        for epoch, data in records:
            self.records_seen += 1
            at = self.record_time(epoch)
            packet = advcapture.parse_advertising(data)
            if packet is None:
                # Either not an advertising packet or its CRC is wrong; the
                # text reader prints both, which is the asymmetry this test
                # reports rather than demands away.
                self.crc_dropped += 1
                continue
            if packet.address != self.options.address:
                continue
            self.structural_packets += 1
            found = advcapture.harness_payload(packet.adv_data)
            matched = found is not None and found[2].hex() == self.options.payload
            if matched:
                self.payload_matches += 1
            period = self.period_at(at)
            if period is not None:
                period.structural.see(at, period.started, self.options.window_s)
                if matched:
                    period.payload_matches += 1
            else:
                self.unattributed_packets += 1

    # -- the capture ------------------------------------------------------

    def note_capture_gap(self, planned: bool) -> None:
        """The period running now had a stretch with nothing listening."""
        period = self.period()
        if period is None or period.ended is not None:
            return
        if planned:
            period.planned_gaps += 1
        else:
            period.unplanned_gaps += 1

    def start_capture(self, reason: str, *, planned: bool = True) -> None:
        self.note_capture_gap(planned)
        self.capture_index += 1
        self.record_clock.restart()
        self.attempt_text_packets = 0
        self.attempt_rss_first = None
        self.attempt_rss_last = None
        self.capture = Capture(self.options, self.capture_index, self.sink,
                               clock=self.clock,
                               max_log_bytes=max(0, self.transcript_budget))
        self.capture.start()
        ready = self.wait_until_capturing()
        self.last_packet_at = self.elapsed
        period = self.period()
        if period is not None and period.ended is None:
            # The moment something was listening again, which is what says
            # whether the stretch an off period is judged on was captured.
            period.listening_from = self.elapsed
        self.record({"record": "capture_started", "index": self.capture_index,
                     "pid": self.capture.attempt.pid, "reason": reason,
                     "ready_s": None if ready is None else round(ready, 3),
                     "argv": list(self.capture.argv)})

    def wait_until_capturing(self) -> float | None:
        """Wait, briefly, until the tool has opened its capture file.

        The tool writes the file's header once it has the board, so the
        header arriving is the first moment anything is listening. Waiting
        for it keeps the schedule from starting against a capture that has
        not begun -- which would charge the tool's own start-up to the
        advertiser's detection deadline -- and the wait is bounded, so a tool
        that never gets that far is left to the liveness rule instead of
        holding the run here. The seconds it took are recorded.
        """
        deadline = self.clock() + min(self.options.liveness_timeout_s,
                                      CAPTURE_READY_TIMEOUT_S)
        started = self.clock()
        while self.clock() < deadline and not self.canceled:
            if self.capture is None or not self.capture.running():
                return None
            if self.capture.tail.error:
                # Not readable at all; read_structural records and judges it.
                return None
            if self.capture.tail.open_header():
                return self.clock() - started
            time.sleep(POLL_S)
        return None

    def stop_capture(self, reason: str, *, kill: bool = False,
                     planned: bool = True) -> Attempt | None:
        if self.capture is None:
            # Nothing is running: a cancellation that arrived inside a
            # restart, after the old capture was stopped and before the new
            # one started, reaches the teardown here.
            return None
        self.note_capture_gap(planned)
        # A canceled run waits for nothing: the tool was asked to stop in
        # the signal handler, this process has about two seconds left, and
        # the group SIGKILL that follows reaches the tool as well.
        attempt = self.capture.stop(reason, kill=kill,
                                    grace_s=0.0 if self.canceled else None)
        # Whatever the tool wrote before it went is still the capture's, and
        # a poll reads one chunk, so the rest of the file is read until it
        # gives nothing back.
        for _ in range(MAX_FINAL_POLLS):
            before = self.capture.tail.records
            self.read_structural()
            if self.capture.tail.records == before:
                break
        attempt.records = self.capture.tail.records
        # What the tool left half-written, measured once the file has been
        # read to its end: ordinary for a capture that was killed, judged
        # for one that was asked to stop (below).
        attempt.partial_tail_bytes = self.capture.tail.pending_bytes
        if self.capture.reader is not None:
            self.transcript_budget -= self.capture.reader.written_bytes
            self.transcript_bytes += self.capture.reader.written_bytes
            self.transcript_dropped_lines += self.capture.reader.dropped_lines
        self.capture.close()
        self.read_text()
        self.attempts.append(attempt)
        if len(self.attempts) > MAX_HELD_ATTEMPTS:
            # A board that died in the first hour of a three-day run is
            # restarted every few seconds for the rest of it. The oldest
            # records are in progress.jsonl; what is held here is what the
            # summary and the pruning read.
            self.attempts_forgotten += 1
            del self.attempts[MAX_HELD_ATTEMPTS // 2]
        if attempt.forced and not kill:
            self.forced_stops += 1
        if attempt.returncode is not None and not attempt.signalled:
            # It had already gone before anything was asked of it, whatever
            # the run was doing at the time: a restart, a file rotation, or
            # the end of the run. Counted here, where every attempt is
            # closed, so no death is reported nowhere.
            self.unexplained_exits += 1
        if attempt.partial_tail_bytes and not attempt.forced and not kill:
            # A tool that was asked to stop flushes and closes its capture,
            # so a half-written record after a clean stop is a file that
            # cannot be read to its end. After a kill it is ordinary.
            reason = (f"{attempt.capture} ends in {attempt.partial_tail_bytes} "
                      "bytes of an incomplete record")
            self._note_capture_error(reason)
            say(f"capture {attempt.index}: {reason}")
            self.record({"record": "capture_unreadable", "index": attempt.index,
                          "reason": reason})
        self.record(attempt.to_dict())
        self.capture = None
        self.prune_captures()
        return attempt

    def _note_capture_error(self, reason: str) -> None:
        """Record a capture that could not be read, bounded like the rest.

        A run that keeps restarting would otherwise add one of these per
        attempt and copy all of them into the summary; what a reader needs is
        that it happened, how often, and the first few reasons.
        """
        self.capture_error_count += 1
        self._seen_capture_errors.add(reason)
        if len(self.capture_errors) < MAX_CAPTURE_ERRORS:
            self.capture_errors.append(reason)

    def prune_captures(self) -> None:
        """Keep the newest captures inside the run's budget."""
        removed, removed_bytes, stuck = prune_capture_files(
            self.attempts, self.options.out, self.options.capture_budget_bytes,
            self.record, stuck_already=bool(self.capture_budget_stuck))
        self.captures_removed += removed
        self.capture_bytes_removed += removed_bytes
        if stuck:
            self.capture_budget_stuck = stuck

    def rotate_if_large(self) -> None:
        """Start a new capture file once this one is big enough.

        A run of days keeps the whole capture, and on a crowded channel that
        is a file nobody wants in a run directory: the tool records every
        advertiser on the channel, not only ours. So the capture is rolled
        over at a size, and only at the start of an off period, where the
        advertiser is quiet anyway -- the second or so the changeover costs
        then falls in a silence the test already expects, instead of
        appearing as a gap in a period being judged.
        """
        if (self.capture is None or self.options.capture_limit_bytes <= 0
                or self.capture_budget_stuck):
            # Another file is another file nothing can remove.
            return
        try:
            size = self.capture.capture_path.stat().st_size
        except OSError:
            return
        if size < self.options.capture_limit_bytes:
            return
        reason = f"the capture file reached {size} bytes"
        say(f"capture {self.capture_index}: {reason}, starting another file")
        self.stop_capture(reason)
        self.rotations += 1
        self.start_capture(reason)

    def restart(self, reason: str, *, kill: bool = False, probe: bool = False) -> None:
        # The probe is the run's own doing; every other restart is not.
        attempt = self.stop_capture(reason, kill=kill, planned=probe)
        if attempt is None:
            return
        if not probe:
            self.restarts += 1
        else:
            self.probe_restarts += 1
        if self.canceled:
            # Everything below is board work bounded in tens of seconds -- a
            # watchdog read, perhaps a reset and another read, and the pause
            # before the next attempt -- and the harness allows about two
            # seconds after its SIGTERM. Reaching the teardown is what
            # matters now.
            return
        self.read_watchdog_now("after a restart")
        if self.reset_next_restart:
            # The second rung of #703's ladder: a clean restart did not bring
            # the capture back, so the board is reset before the next try and
            # the watchdog record is read again afterwards.
            result = run_util(self.options.serial, "-r", timeout=RESET_TIMEOUT_S)
            self.board_resets += 1
            self.record({"record": "board_reset", "returncode": result.returncode})
            self.reset_next_restart = False
            if self.canceled:
                return
            # The board is re-enumerating, so this read is tried more than
            # once (watchdog.RECOVERY_TRIES is the monitor's own number).
            self.read_watchdog_now("after a board reset",
                                    tries=watchdog.RECOVERY_TRIES)
        if self.canceled:
            return
        if self.elapsed - self.last_restart_at < self.options.restart_min_interval_s:
            time.sleep(max(0.0, self.options.restart_min_interval_s
                           - (self.elapsed - self.last_restart_at)))
        self.last_restart_at = self.elapsed
        if self.canceled:
            # Starting a tool the teardown is about to stop would cost the
            # board a mode change for nothing, and the harness kills this
            # process about two seconds after it cancels a step.
            return
        self.start_capture(reason, planned=probe)
        if probe:
            self.probe_period_index = len(self.periods) - 1

    def read_watchdog_now(self, when: str, tries: int = 1) -> None:
        """Read the watchdog record while no capture is running."""
        record, reason = read_watchdog(self.options.serial, tries)
        entry = {"record": "watchdog", "when": when, "reason": reason}
        if record is not None:
            change = watchdog.compare(self.watchdog_baseline, record)
            if self.watchdog_baseline is None:
                self.watchdog_baseline = record
            elif change.restarted:
                # The board was power-cycled or reflashed under the run: its
                # counters start again, so it is re-baselined there.
                self.watchdog_baseline = record
            elif change.reset_itself:
                self.watchdog_resets += change.new_resets
                if len(self.watchdog_where) < MAX_WATCHDOG_WHERE:
                    self.watchdog_where.append(record.last_reset_where)
            entry["watchdog"] = record.to_dict()
        # Counted, not kept: a run that restarts for three days reads the
        # record thousands of times, and progress.jsonl holds every one.
        self.watchdog_reads += 1
        self.record(entry)

    # -- sampling ---------------------------------------------------------

    def sample(self) -> None:
        self.samples += 1
        rss = None
        if self.capture is not None and self.capture.attempt.pid:
            rss = resident_kb(self.capture.attempt.pid)
        if rss is not None:
            # Per attempt: a run that restarted would otherwise subtract one
            # capture process's memory from another's and report the
            # difference as growth.
            if self.attempt_rss_first is None:
                self.attempt_rss_first = rss
            self.attempt_rss_last = rss
            self.rss_first = rss if self.rss_first is None else self.rss_first
            self.rss_last = rss
            self.rss_max = rss if self.rss_max is None else max(self.rss_max, rss)
            self.rss_growth_max = max(
                self.rss_growth_max, self.attempt_rss_last - self.attempt_rss_first)
        period = self.period()
        self.record({
            "record": "sample",
            "state": None if period is None else period.state,
            "capture": self.capture_index,
            "running": self.capture is not None and self.capture.running(),
            "text_packets": self.text_packets,
            "structural_packets": self.structural_packets,
            "records": self.records_seen,
            "since_last_packet_s": round(self.elapsed - self.last_packet_at, 3),
            "rss_kb": rss,
            "restarts": self.restarts,
        })

    # -- the loop ---------------------------------------------------------

    def run(self) -> int:
        options = self.options
        self.start_mono = self.clock()
        self.started_wall = self.wall()
        options.out.mkdir(parents=True, exist_ok=True)
        self.progress = (options.out / "progress.jsonl").open("w", encoding="utf-8")
        try:
            return self._run()
        except (SoakError, nrfdk.NrfDkError) as error:
            # A bench failure: the run cannot be judged, and it says so with
            # the report it has rather than only on standard error.
            return self.report_error(f"the run could not finish: {error}",
                                      EXIT_CONFIGURATION)
        except Exception as error:  # noqa: BLE001 - the report comes first
            # Anything else -- a full disk under the progress file, a fault
            # nobody expected -- still leaves the one machine-readable
            # artifact of a run of days, with the reason in it.
            traceback.print_exc()
            return self.report_error(f"the run ended with an error: {error!r}",
                                      EXIT_FAIL)
        finally:
            # However the run ends -- an error nobody expected, a disk that
            # filled under the progress file -- the tool does not outlive it
            # holding the board.
            if self.capture is not None:
                try:
                    self.stop_capture("the run ended unexpectedly")
                except Exception:  # noqa: BLE001 - the teardown comes first
                    self.capture.signal_tool(signal.SIGKILL)
            self.advertiser.close()
            if self.progress is not None:
                self.progress.close()

    def _run(self) -> int:
        options = self.options
        say(f"soak: {options.duration_s:g}s, channel {options.channel}, advertiser "
              f"{options.address} every {options.interval_ms} ms, schedule "
              f"{options.on_s:g}s on / {options.off_s:g}s off")
        answers, reason = board_answers(options.serial)
        if not answers:
            raise SoakError(f"the Ubertooth does not answer: {reason}")
        self.read_watchdog_now("before the run")
        self.advertiser.open()
        status = self.advertiser.configure(options)
        self.counter_before = status["counter"]
        say(f"advertiser: {status['fw']} at {options.address}, counter "
              f"{self.counter_before}")

        self.start_capture("the run started")
        # The duration the plan asked for is the window the schedule runs
        # over, so it is measured from here: the board read, the watchdog
        # read, the advertiser's settings and the capture's own start-up
        # come before it and are not part of it.
        self.window_start = self.elapsed
        period = self.open_period("on")
        next_sample = self.elapsed + options.sample_every_s
        next_switch = period.started + options.on_s

        while self.window_elapsed < options.duration_s and not self.canceled:
            time.sleep(POLL_S)
            self.read_text()
            self.read_structural()
            now = self.elapsed
            if now >= next_switch:
                period = self.period()
                self.close_period(period)
                state = "off" if period.state == "on" else "on"
                opened = self.open_period(state)
                # From the new period's own start, so a slow transition
                # shortens nothing: a period runs for the length it was
                # given, measured from when the advertiser was told.
                next_switch = opened.started + (
                    options.on_s if state == "on" else options.off_s)
                if state == "off":
                    self.rotate_if_large()
            if now >= next_sample:
                self.sample()
                next_sample = now + options.sample_every_s
            if (self.probe_at is not None and not self.probe_done
                    and self.window_elapsed >= self.probe_at):
                self.probe_done = True
                say(f"probe: SIGKILL at {now:.1f}s, with no stop sent to the board")
                self.restart("the restart probe", kill=True, probe=True)
                continue
            self.check_liveness()
            if self.capture_budget_stuck:
                # Nothing it could do next stays inside the budget it was
                # given, so the window ends here and the verdict says why.
                break

        if self.period() is not None and self.period().ended is None:
            self.period().cut_by_duration = True
            self.close_period(self.period())
        final = self.stop_capture("the run was stopped" if self.canceled
                                  else "the run finished")
        # A cancellation leaves about two seconds before the harness kills
        # this process (executor.py), so the teardown after it is the part
        # that cannot be left out: the capture is stopped above, the
        # advertiser is quieted, and the summary is written from what the run
        # has. The board reads and the closing watchdog read are skipped --
        # each can take ten seconds, and the case's own cleanup steps read
        # and restore the board anyway.
        if self.canceled:
            # The report first: quieting the advertiser is a shell command
            # with a retry behind it, and the harness allows about two
            # seconds after its SIGTERM. The run's own `finally` closes the
            # shell, and the case's cleanup leaves the DK idle.
            return self.report(final)
        try:
            self.advertiser.stop()
            self.counter_after = self.advertiser.status()["counter"]
        except SoakError as error:
            # Counted apart from the schedule's own failures, and not a
            # failure: this pair of commands runs after the last period,
            # which the duration ended and which nothing judged, so a DK
            # that hiccups in the final second of a three-day run affected
            # no measurement. A transition inside the window is the one that
            # means the schedule is not known.
            self.closing_error = str(error)
            self.record({"record": "closing_error", "when": "the run finished",
                         "reason": str(error)})
        self.read_watchdog_now("after the run")
        self.read_board_after()
        return self.report(final)

    def check_liveness(self) -> None:
        """Restart when the process has gone, or when nothing is arriving."""
        if self.capture is None or self.canceled:
            # A cancellation reaches the tool as well as this process (they
            # are in one group), so a tool that has just exited on its way
            # out is not a death to recover from.
            return
        options = self.options
        if not self.capture.running():
            self.capture.finish()
            attempt = self.capture.attempt
            say(f"capture {attempt.index} exited {attempt.returncode} by itself"
                  + (f"; last error: {attempt.last_error}" if attempt.last_error else ""))
            # The ladder is about a capture that brings nothing back,
            # however it fails: a tool that exits at once reaches the reset
            # rung as a silent one does.
            # After `finish`, so the records written between the last poll
            # and the exit are counted: an attempt that did bring something
            # back has not earned a board reset.
            self.reset_next_restart = self.needs_a_reset()
            self.restart(f"the process exited {attempt.returncode}")
            return
        quiet = self.elapsed - self.last_packet_at
        if quiet >= options.liveness_timeout_s:
            if self.elapsed - self.last_restart_at < options.restart_min_interval_s:
                return
            self.reset_next_restart = self.needs_a_reset()
            say(f"capture {self.capture_index}: no packet for {quiet:.1f}s, restarting")
            self.restart(f"no packet for {quiet:.1f}s")

    def needs_a_reset(self) -> bool:
        """Has a clean restart failed to bring this capture back?

        The ladder's second rung: this attempt heard nothing at all, and a
        restart has already been tried, so the next one is preceded by
        ``ubertooth-util -r``. This attempt's own packets, not the run's: a
        run whose first capture worked would otherwise never reach the rung,
        however many later captures heard nothing.
        """
        if self.capture is None:
            return False
        heard = (self.capture.tail.records > 0 or self.attempt_text_packets > 0)
        return not heard and self.restarts > 0

    def read_board_after(self) -> None:
        """The board answers, and what it is set to, once the capture is done."""
        reads = {}
        for key, option in (("firmware", "-v"), ("serial", "-s"), ("channel", "-C"),
                            ("squelch", "-z"), ("leds", "-d")):
            result = run_util(self.options.serial, option)
            reads[key] = {
                "returncode": result.returncode,
                "answered": result.returncode == 0,
            }
        self.board_after = reads
        self.record({"record": "board_after", "reads": reads})

    # -- the verdict ------------------------------------------------------

    def on_periods(self) -> list[Period]:
        """On periods the run really completed, which are the judgeable ones.

        Three kinds are left out, and the reason is the same each time: the
        period says nothing about detection, so counting it as a miss would
        be reporting the harness's own doing as the board's. A period the
        duration cut short was not known to be on throughout; one whose
        schedule command did not reach the DK was not known to be on at all;
        and one with a capture gap had a stretch with nothing listening,
        whether that was a restart, the probe, or a file rotation. All three
        are recorded in progress.jsonl and counted in the summary, so a run
        that judged fewer periods than it ran says so.
        """
        return [period for period in self.periods
                if period.state == "on" and period.ended is not None
                and not period.schedule_error and not period.capture_gap
                and not period.cut_by_duration
                and period.seconds >= self.options.on_s * 0.9]

    def on_period_counts(self) -> dict:
        """How many on periods ran, and why each one left out was left out."""
        ran = [period for period in self.periods if period.state == "on"]
        return {
            "ran": len(ran),
            # The one the run's own duration ended, which every run has.
            "ended_by_the_run": sum(1 for period in ran if period.cut_by_duration),
            "cut_short": sum(1 for period in ran if not period.cut_by_duration
                             and (period.ended is None
                                  or period.seconds < self.options.on_s * 0.9)),
            "schedule_error": sum(1 for period in ran if period.schedule_error),
            "capture_gap": sum(1 for period in ran if period.capture_gap),
            # Credited against the coverage floor, so it is only the periods
            # whose one trouble was the run's own doing: a period that was
            # also cut short, or whose schedule command failed, was not
            # judgeable for a reason the run did not choose.
            "planned_capture_gap": sum(
                1 for period in ran
                if period.planned_gaps and not period.unplanned_gaps
                and not period.cut_by_duration and not period.schedule_error
                and period.seconds >= self.options.on_s * 0.9),
        }

    def detection(self) -> dict:
        """Per reader, how many on periods were detected and sustained."""
        periods = self.on_periods()
        found = {}
        for name in ("text", "structural"):
            detected = sustained = 0
            first_times = []
            for period in periods:
                side = getattr(period, name)
                if (side.first is not None
                        and side.first - period.started <= self.options.detect_within_s):
                    detected += 1
                    first_times.append(side.first - period.started)
                if side.sustained(self.options.windows_needed):
                    sustained += 1
            found[name] = {
                "periods": len(periods),
                "detected": detected,
                "sustained": sustained,
                "detected_share": detected / len(periods) if periods else 0.0,
                "sustained_share": sustained / len(periods) if periods else 0.0,
                "first_detection_s": round(min(first_times), 3) if first_times else None,
                "median_detection_s": (round(statistics.median(first_times), 3)
                                       if first_times else None),
            }
        return found

    def allowance(self, periods: int) -> int:
        """How many of `periods` comparisons may go the wrong way.

        A share of the population, because the text reader's times are when
        the host read a stdio block and one late flush should not end three
        days of work -- with a floor of one, so a short run has that much
        slack too, and never the whole population, so a comparison with one
        or two judged periods can still fail. Without that cap the floor made
        the off-period and gap checks structurally unfailable on the short
        suite: one judged period, allowance one.
        """
        if periods <= 0:
            return 0
        share = math.ceil((1.0 - self.options.min_detection_share) * periods)
        return min(max(1, share), periods - 1)

    def off_settle_s(self) -> float:
        # Never more than half the period, so a short schedule still has a
        # stretch that is judged: a settle time longer than the off period
        # would leave nothing to look at and read as quiet whatever happened.
        return min(OFF_SETTLE_S, self.options.off_s / 2)

    def off_judged_from(self, period: Period) -> float | None:
        """When this off period's judged stretch begins, or None if it has none.

        An off period is judged on the time past its settle point, because
        the packets already on the air and already in the tool's buffers
        arrive just after the stop. A capture that came back inside the
        period moves that start later: the stretch has to be one something
        was listening to.

        This is what keeps the check from being emptied. A capture file is
        rolled over at the start of an off period on purpose, so on a busy
        channel with a small `capture file limit` every off period holds a
        gap; discarding those periods left nothing judged, and an advertiser
        that took `adv stop` and kept advertising would have passed with the
        schedule half checked. An unplanned gap still disqualifies the
        period, because then the capture's own health is in question.
        """
        if (period.state != "off" or period.ended is None
                or period.schedule_error or period.cut_by_duration
                or period.seconds < self.options.off_s * 0.9):
            return None
        start = period.started + self.off_settle_s()
        if period.unplanned_gaps:
            return None
        if period.planned_gaps:
            if period.listening_from is None:
                return None
            start = max(start, period.listening_from)
        # A stretch of nothing is not a quiet period, it is an unmeasured one.
        return start if start < period.ended else None

    def off_periods(self) -> list[Period]:
        """Off periods with a stretch this run can judge them on."""
        return [period for period in self.periods
                if self.off_judged_from(period) is not None]

    def off_period_counts(self) -> dict:
        """How many off periods ran, and why each one left out was left out.

        The off periods are judged in their own right (`off_quiet`), so what
        was left out of that judgement has to be reported and held to a floor
        as the on periods are. A capture rolled over at the start of every
        off period, which a small `capture file limit` on a busy channel
        does, would otherwise leave nothing judged and the schedule only half
        checked -- an advertiser that takes `adv stop` and keeps advertising
        would pass.
        """
        ran = [period for period in self.periods if period.state == "off"]
        return {
            "ran": len(ran),
            "ended_by_the_run": sum(1 for period in ran if period.cut_by_duration),
            "cut_short": sum(1 for period in ran if not period.cut_by_duration
                             and (period.ended is None
                                  or period.seconds < self.options.off_s * 0.9)),
            "schedule_error": sum(1 for period in ran if period.schedule_error),
            "capture_gap": sum(1 for period in ran if period.capture_gap),
            "planned_capture_gap": sum(
                1 for period in ran
                if period.planned_gaps and not period.unplanned_gaps
                and not period.cut_by_duration and not period.schedule_error
                and period.seconds >= self.options.off_s * 0.9),
            # A rollover that left no stretch to judge: the period is not in
            # the judged population and the run says which of them it was.
            "rollover_left_nothing": sum(
                1 for period in ran
                if period.planned_gaps and not period.unplanned_gaps
                and self.off_judged_from(period) is None),
        }

    def off_quiet(self) -> dict:
        """Was the advertiser really quiet when it was told to stop?

        Without this the schedule is only half checked: a DK that accepts
        `adv stop` and keeps advertising would meet every detection, gap and
        rate objective, and the run would report a controlled schedule it
        never had. Packets already on the air or already in the capture's
        buffers arrive just after the stop, so what is judged is the rest of
        the period: a sighting more than OFF_SETTLE_S into it, by either
        reader.
        """
        periods = self.off_periods()
        noisy = []
        for period in periods:
            # Per period, because a capture that came back inside one moves
            # the start of the stretch that can be judged (`off_judged_from`).
            settled = self.off_judged_from(period)
            for name in ("text", "structural"):
                side = getattr(period, name)
                if side.last is not None and side.last > settled:
                    noisy.append(period.index)
                    break
        return {"periods": len(periods), "noisy": len(noisy),
                "settle_s": round(self.off_settle_s(), 3),
                "allowance": self.allowance(len(periods))}

    def gaps(self) -> dict:
        periods = self.on_periods()
        text = [period.text.max_gap for period in periods]
        structural = [period.structural.max_gap for period in periods]
        # Either reader: a tool that stopped printing its AdvA line partway
        # through an on period is as much a fault as a capture that stopped
        # recording, and judging the records alone would leave the text
        # reader's gap reported and unjudgeable.
        over = sum(1 for period in periods
                   if max(period.text.max_gap, period.structural.max_gap)
                   > self.options.max_gap_s)
        return {
            "allowance": self.allowance(len(periods)),
            "text_max_s": round(max(text), 3) if text else None,
            "structural_max_s": round(max(structural), 3) if structural else None,
            "structural_median_s": (round(statistics.median(structural), 3)
                                    if structural else None),
            "periods_over_limit": over,
        }

    def parity(self) -> dict:
        # Over the whole run, not period by period: the windows are the
        # run's own, so a window at a period boundary has its neighbour in
        # the set either way, which is where the readers' buffers really do
        # split a packet.
        text: set = set()
        structural: set = set()
        for period in self.periods:
            text |= period.text.windows
            structural |= period.structural.windows
        both = len(text & structural)
        text_only = len(text - structural)
        structural_only = len(structural - text)
        # Not text_only + structural_only: a window beside one the other
        # reader had is the two buffers flushing at different moments, not a
        # reader that missed the advertiser.
        disagreements = (_lonely_windows(text, structural)
                         + _lonely_windows(structural, text))
        # Every window either reader saw it in, which is what the share is a
        # share of: counting only the shared and the lonely ones would leave
        # a run where the readers never agreed on a single window with an
        # allowance of one and nothing to compare.
        seen = len(text | structural)
        allowance = max(1, math.ceil(seen * self.options.max_disagreement_share))
        return {
            "windows_both": both,
            "windows_text_only": text_only,
            "windows_structural_only": structural_only,
            # Of the windows above, the ones with no window of the other
            # reader's beside them, which is what the allowance is against.
            "disagreements": disagreements,
            "windows_seen": seen,
            "allowance": allowance,
        }

    def rate(self) -> dict:
        """Packets a minute of the time the advertiser was really on.

        Per judged on period and then over all of them, rather than per
        wall-clock minute the periods happened to touch: a schedule whose
        legs cross minute boundaries, or are shorter than a minute, would
        otherwise pass or fail on where the minutes fell.
        """
        periods = self.on_periods()
        seconds = sum(period.seconds for period in periods)
        rates = [period.structural.packets / period.seconds * 60.0
                 for period in periods if period.seconds > 0]
        packets = sum(period.structural.packets for period in periods)
        if not rates or seconds <= 0:
            return {"periods": 0, "on_seconds": 0.0, "packets": 0, "mean": 0.0,
                    "min": 0.0, "max": 0.0, "median": 0.0}
        return {
            "periods": len(periods),
            "on_seconds": round(seconds, 3),
            "packets": packets,
            "mean": round(packets / seconds * 60.0, 1),
            "min": round(min(rates), 1),
            "max": round(max(rates), 1),
            "median": round(statistics.median(rates), 1),
        }

    def memory(self) -> dict:
        """What the capture process's memory did, per process and over the run.

        ``growth_kb`` is the largest growth inside one capture process, which
        is the figure a limit is about: a run that restarted has a new process
        each time, and the difference between two of them is not growth.
        """
        growth = (None if self.rss_first is None
                  else self.rss_growth_max)
        return {"first_kb": self.rss_first, "last_kb": self.rss_last,
                "max_kb": self.rss_max, "growth_kb": growth,
                "samples": self.samples}

    def probe_outcome(self) -> bool | None:
        """Did the advertiser come back after the probe killed the capture?

        Judged on the first judged on period after the probe: the probe can
        land in the middle of one, and a period with a capture gap in it says
        nothing either way. ``None`` when no probe was asked for, or when no
        judged period followed it, and the report says so rather than
        claiming a recovery nobody measured.
        """
        if not self.options.restart_probe or self.probe_period_index is None:
            return None
        for period in self.on_periods():
            if period.index <= self.probe_period_index:
                continue
            side = period.structural
            deadline = self.options.detect_within_s + RECOVERY_SLACK_S
            return side.first is not None and side.first - period.started <= deadline
        return None

    def final_exit_problem(self) -> str:
        """Why the capture's last exit was not a clean stop, if it was not.

        A tool asked to stop is expected to exit 0: a positive exit status
        after a SIGTERM is the tool reporting a fault on its way out, and
        nothing else here would notice it, since the restart counters are
        about attempts that were restarted and this one ended the run.
        """
        if not self.attempts:
            return ""
        final = self.attempts[-1]
        if final.returncode is None or final.returncode <= 0:
            return ""  # it is still gone, or it died of a signal
        if final.stop_reason.startswith("the run was stopped"):
            return ""  # a canceled run is its own reason
        return (f"the capture exited {final.returncode} when it was stopped"
                + (f": {final.last_error}" if final.last_error else ""))

    def judge(self) -> list[str]:
        """Every configured objective this run did not meet."""
        options = self.options
        problems = []
        detection = self.detection()
        counts = self.on_period_counts()
        judged = len(self.on_periods())
        if not judged:
            problems.append("no advertiser on period ran in full, so nothing could "
                            "be judged")
        elif counts["ran"] - counts["ended_by_the_run"] > 0:
            # A run has to be able to judge most of what it ran, or a capture
            # that keeps dying would pass on the few periods it did cover.
            # The periods the run meant to interrupt -- the probe, and a
            # capture file rolled over -- are allowed, because the run chose
            # them; the rest count against this floor.
            meant_to_complete = counts["ran"] - counts["ended_by_the_run"]
            usable = judged + counts["planned_capture_gap"]
            if usable < options.min_detection_share * meant_to_complete:
                problems.append(
                    f"only {judged} of {meant_to_complete} advertiser on periods "
                    f"could be judged ({counts['cut_short']} cut short, "
                    f"{counts['schedule_error']} with a schedule failure, "
                    f"{counts['capture_gap']} with a capture gap, of which "
                    f"{counts['planned_capture_gap']} were the run's own doing), "
                    f"below {options.min_detection_share:.2f}")
        for name, found in detection.items():
            if found["periods"] and found["detected_share"] < options.min_detection_share:
                problems.append(
                    f"the {name} reader detected {found['detected']} of "
                    f"{found['periods']} on periods within {options.detect_within_s:g}s "
                    f"({found['detected_share']:.2f}), below "
                    f"{options.min_detection_share:.2f}")
            if found["periods"] and found["sustained_share"] < options.min_detection_share:
                problems.append(
                    f"the {name} reader saw sustained presence "
                    f"({options.windows_needed} x {options.window_s:g}s windows) in "
                    f"{found['sustained']} of {found['periods']} on periods "
                    f"({found['sustained_share']:.2f}), below "
                    f"{options.min_detection_share:.2f}")
        quiet = self.off_quiet()
        off_counts = self.off_period_counts()
        off_meant = off_counts["ran"] - off_counts["ended_by_the_run"]
        if off_meant > 0 and not quiet["periods"]:
            # Nothing credited here, unlike the share floor below: whatever
            # emptied the population, including the run's own file rollovers,
            # the schedule's other half went unmeasured, and a soak that
            # cannot say the advertiser stopped when it was told has not
            # checked the schedule it reports.
            problems.append(
                f"none of the {off_meant} advertiser off period(s) meant to "
                f"complete could be judged ({off_counts['cut_short']} cut short, "
                f"{off_counts['schedule_error']} with a schedule failure, "
                f"{off_counts['capture_gap']} with a capture gap), so whether the "
                "advertiser stopped when it was told was never measured")
        elif off_meant > 0:
            usable = quiet["periods"] + off_counts["planned_capture_gap"]
            if usable < options.min_detection_share * off_meant:
                problems.append(
                    f"only {quiet['periods']} of {off_meant} advertiser off periods "
                    f"could be judged ({off_counts['cut_short']} cut short, "
                    f"{off_counts['schedule_error']} with a schedule failure, "
                    f"{off_counts['capture_gap']} with a capture gap, of which "
                    f"{off_counts['planned_capture_gap']} were the run's own doing), "
                    f"below {options.min_detection_share:.2f}")
        if quiet["periods"] and quiet["noisy"] > quiet["allowance"]:
            problems.append(
                f"the advertiser was still heard more than {quiet['settle_s']:g}s "
                f"into {quiet['noisy']} of {quiet['periods']} off period(s), more "
                f"than the allowance of {quiet['allowance']}, so it did not stop "
                "when it was told to")
        parity = self.parity()
        if parity["disagreements"] > parity["allowance"]:
            problems.append(
                f"the readers disagreed about {parity['disagreements']} of "
                f"{parity['windows_seen']} windows ({parity['windows_text_only']} text "
                f"only, {parity['windows_structural_only']} structural only), more than "
                f"the allowance of {parity['allowance']}")
        gaps = self.gaps()
        if gaps["periods_over_limit"] > gaps["allowance"]:
            problems.append(
                f"{gaps['periods_over_limit']} judged period(s) held a gap with "
                f"no advertiser packet longer than {options.max_gap_s:g}s, more "
                f"than the allowance of {gaps['allowance']} "
                f"(longest {gaps['structural_max_s']}s structural, "
                f"{gaps['text_max_s']}s text)")
        rate = self.rate()
        if rate["periods"] and rate["mean"] < options.min_packets_per_minute:
            problems.append(
                f"{rate['mean']} advertiser packets a minute, below "
                f"{options.min_packets_per_minute}")
        if self.structural_packets and not self.payload_matches:
            problems.append(
                "no captured packet carried the harness payload tag, so the address "
                "may not be this advertiser's")
        if self.capture_error_count:
            problems.append(
                f"{self.capture_error_count} capture file(s) could not be read to "
                "the end: " + "; ".join(self.capture_errors[:3]))
        if not self.structural_packets:
            problems.append("the capture held no packet of the advertiser at all")
        if self.restarts > options.max_restarts:
            problems.append(
                f"{self.restarts} unplanned restart(s), more than {options.max_restarts}")
        if self.unexplained_exits > options.max_restarts:
            # Judged in its own right, because the last attempt's death is
            # not a restart: the run ends, and nothing else would notice that
            # the capture had already gone.
            problems.append(
                f"the capture process died {self.unexplained_exits} time(s) with "
                f"nothing asking it to, more than {options.max_restarts}")
        if (final_exit := self.final_exit_problem()):
            problems.append(final_exit)
        if self.forced_stops:
            problems.append(
                f"{self.forced_stops} capture(s) did not stop on SIGTERM within "
                f"{options.term_grace_s:g}s and had to be killed")
        if self.schedule_errors:
            problems.append(
                f"{self.schedule_errors} advertiser schedule command(s) failed, so "
                "the schedule is not known")
        unanswered = sorted(key for key, read in self.board_after.items()
                            if not read["answered"])
        if unanswered:
            problems.append("the board did not answer after the run: "
                            + ", ".join(unanswered))
        growth = self.memory()["growth_kb"]
        if (options.max_memory_growth_kb is not None and growth is not None
                and growth > options.max_memory_growth_kb):
            problems.append(
                f"the capture process grew by {growth} KiB, more than "
                f"{options.max_memory_growth_kb} KiB")
        probe = self.probe_outcome()
        if probe is False:
            problems.append("the advertiser was not heard again in the first judged "
                            "on period after the restart probe")
        elif probe is None and options.restart_probe:
            # The plan asked for the evidence, so a run that could not
            # produce it has not met what it was asked for: passing while
            # reporting "not established" would be a soak that never showed
            # the restart path recovering.
            problems.append(
                "no judged advertiser on period followed the restart probe, so "
                "whether the capture recovered was never measured")
        if self.sink.dropped:
            # The harness's own loss, not the board's: those are sightings
            # the text reader never saw, so the figures above are not the
            # whole picture and the run says so rather than judging them.
            problems.append(
                f"{self.sink.dropped} line(s) of the tool's output were dropped "
                "before they were read, so the text reader's figures are "
                "incomplete")
        if self.capture_budget_stuck:
            problems.append(
                f"the run's capture budget could not be kept: "
                f"{len(self.capture_budget_stuck)} file(s) could not be removed "
                f"({', '.join(self.capture_budget_stuck[:3])}), so the run stopped "
                "rather than write captures past the budget it was given")
        if self.canceled:
            problems.append(f"the run was stopped by {self.canceled}")
        return problems + list(self.extra_problems)

    def summary(self) -> dict:
        return {
            "duration_s": round(self.elapsed, 3),
            "address": self.options.address,
            "channel": self.options.channel,
            "schedule": {"on_s": self.options.on_s, "off_s": self.options.off_s,
                          "interval_ms": self.options.interval_ms,
                          "periods": len(self.periods),
                          "on_periods": self.on_period_counts()["ran"],
                          "on_periods_judged": len(self.on_periods()),
                          "on_periods_left_out": self.on_period_counts(),
                          "off_periods_left_out": self.off_period_counts(),
                          "errors": self.schedule_errors,
                          "closing_error": self.closing_error,
                          "transitions": self.advertiser.transitions,
                          "retries": self.advertiser.retries},
            "advertiser_counter": {"before": self.counter_before,
                                    "after": self.counter_after},
            "text": {"advertiser_packets": self.text_packets,
                      "other_packets": self.other_packets,
                      "other_addresses": len(self.other_addresses),
                      # A crowded channel passes this in minutes, and then
                      # the count is a floor rather than a figure.
                      "other_addresses_counted_to_the_limit":
                          len(self.other_addresses) >= MAX_OTHER_ADDRESSES},
            "captures": {"removed": self.captures_removed,
                          "removed_bytes": self.capture_bytes_removed,
                          "budget_bytes": self.options.capture_budget_bytes,
                          "file_limit_bytes": self.options.capture_limit_bytes,
                          "kept_attempts": MAX_KEPT_ATTEMPTS,
                          "budget_stuck_files": list(self.capture_budget_stuck)},
            "progress": {"kept_bytes": self.progress_bytes,
                          "budget_bytes": MAX_PROGRESS_BYTES,
                          "records_not_written": self.progress_records_dropped},
            "transcript": {"kept_bytes": self.transcript_bytes,
                            "budget_bytes": MAX_LOG_BYTES,
                            "dropped_lines": self.transcript_dropped_lines,
                            "unread_lines_dropped": self.sink.dropped},
            "structural": {"advertiser_packets": self.structural_packets,
                            "records": self.records_seen,
                            "unreadable_captures": list(self.capture_errors),
                            "unreadable_capture_count": self.capture_error_count,
                            "records_not_advertiser_crc_valid": self.crc_dropped,
                            "payload_matches": self.payload_matches},
            "detection": self.detection(),
            "off_quiet": self.off_quiet(),
            "record_clock": self.record_clock.to_dict(),
            "parity": self.parity(),
            # Sightings that fell between two periods, which belong to
            # neither: the schedule command's own latency sits there.
            "between_periods": self.unattributed_packets,
            "gaps": self.gaps(),
            "rate_per_minute": self.rate(),
            "memory": self.memory(),
            "process": {"attempts": len(self.attempts),
                         "attempts_forgotten": self.attempts_forgotten,
                         "restarts": self.restarts,
                         "probe_restarts": self.probe_restarts,
                         "rotations": self.rotations,
                         "unexplained_exits": self.unexplained_exits,
                         "forced_stops": self.forced_stops,
                         "board_resets": self.board_resets,
                         "probe_recovered": self.probe_outcome()},
            "watchdog": {"reads": self.watchdog_reads,
                          "unrequested_resets": self.watchdog_resets,
                          "where": self.watchdog_where},
            "board_after": self.board_after,
            # Bounded: a board that stopped capturing early in a long run
            # leaves thousands of attempts, and the first and the last of
            # them are what a reader needs. progress.jsonl holds every one.
            "attempts": self.attempt_summaries(),
            "attempt_count": len(self.attempts),
            "canceled": self.canceled,
        }

    def attempt_summaries(self) -> list:
        """Every attempt, or the first and last of them when there are many."""
        attempts = [attempt.to_dict() for attempt in self.attempts]
        if len(attempts) <= MAX_SUMMARY_ATTEMPTS:
            return attempts
        keep = MAX_SUMMARY_ATTEMPTS // 2
        left_out = len(attempts) - 2 * keep
        return (attempts[:keep]
                + [{"record": "attempts_left_out", "count": left_out}]
                + attempts[-keep:])

    VERDICTS = {EXIT_CONFIGURATION: "configuration"}

    def report_error(self, problem: str, status: int) -> int:
        """Report a run that could not finish, with what it had.

        The verdict of a soak is read from its summary, so an error has to
        leave one: the figures up to the fault, the problems the run already
        had, and this one.
        """
        self.extra_problems.append(problem)
        try:
            self.report(self.attempts[-1] if self.attempts else None,
                        verdict=self.VERDICTS.get(status, "fail"))
        except Exception:  # noqa: BLE001 - nothing may hide the first fault
            traceback.print_exc()
        say(f"error: {problem}", file=sys.stderr)
        return status

    def report(self, final: Attempt | None, verdict: str | None = None) -> int:
        summary = self.summary()
        detection = summary["detection"]
        parity = summary["parity"]
        gaps = summary["gaps"]
        rate = summary["rate_per_minute"]
        memory = summary["memory"]
        left_out = summary["schedule"]["on_periods_left_out"]
        say(f"schedule: {summary['schedule']['periods']} periods, "
              f"{left_out['ran']} of them on, "
              f"{summary['schedule']['on_periods_judged']} judged "
              f"({left_out['ended_by_the_run']} ended by the run, "
              f"{left_out['cut_short']} cut short, {left_out['schedule_error']} with a "
              f"schedule failure, {left_out['capture_gap']} with a capture gap)")
        for name in ("text", "structural"):
            found = detection[name]
            say(f"{name} reader: {summary[name]['advertiser_packets']} advertiser "
                  f"packets, {found['detected']}/{found['periods']} periods detected "
                  f"within {self.options.detect_within_s:g}s, {found['sustained']} "
                  f"sustained, first detection {found['first_detection_s']}s")
        say(f"other traffic: {self.other_packets} packets from "
              f"{len(self.other_addresses)} other address(es), never recorded")
        say(f"captures: {self.captures_removed} attempt(s) removed to stay inside "
            f"the run's budget ({self.capture_bytes_removed} bytes)")
        if self.sink.dropped:
            say(f"output: {self.sink.dropped} line(s) dropped before they were "
                "read, so the text reader's figures are incomplete")
        say(f"transcript: {self.transcript_bytes} of {MAX_LOG_BYTES} bytes kept, "
              f"{self.transcript_dropped_lines} line(s) past the budget read but "
              "not kept")
        say(f"progress: {self.progress_bytes} of {MAX_PROGRESS_BYTES} bytes kept, "
            f"{self.progress_records_dropped} record(s) past the budget not "
            "written (every figure is in summary.json)")
        # The judged figure first and named as such: the text-only and
        # structural-only counts include every window a buffer split at a
        # boundary, which is exactly what the allowance does not apply to, so
        # a line without `disagreements` reads as a healthy run sitting on
        # its limit.
        say(f"parity: {parity['disagreements']} of {parity['windows_seen']} windows "
            f"disagree (allowance {parity['allowance']}); "
            f"{parity['windows_both']} windows both, "
            f"{parity['windows_text_only']} text only, "
            f"{parity['windows_structural_only']} structural only")
        quiet = summary["off_quiet"]
        say(f"off periods: {quiet['periods']} judged, {quiet['noisy']} still heard "
            f"more than {quiet['settle_s']:g}s in (allowance {quiet['allowance']})")
        say(f"record clock: {self.record_clock.reanchors} re-anchor(s), largest "
            f"drift {self.record_clock.drift_max_s:.3f}s (re-anchored past "
            f"{self.record_clock.reanchor_s:g}s)")
        say(f"gaps: longest {gaps['structural_max_s']}s structural, "
              f"{gaps['text_max_s']}s text, limit {self.options.max_gap_s:g}s, "
              f"{gaps['periods_over_limit']} period(s) over it (allowance "
              f"{gaps['allowance']})")
        say(f"rate: {rate['mean']} packets a minute over {rate['on_seconds']}s of "
            f"advertising in {rate['periods']} period(s) (slowest {rate['min']}, "
            f"fastest {rate['max']})")
        say(f"memory: first {memory['first_kb']} KiB, last {memory['last_kb']} KiB, "
              f"growth {memory['growth_kb']} KiB over {memory['samples']} sample(s)")
        say(f"process: {len(self.attempts)} attempt(s), {self.rotations} planned "
              f"file rotation(s), {self.restarts} restart(s), "
              f"{self.probe_restarts} probe, {self.unexplained_exits} unexplained "
              f"exit(s), {self.forced_stops} forced stop(s), {self.board_resets} "
              f"board reset(s)")
        if final is None:
            # A cancellation that arrived between a capture stopping and
            # the next one starting leaves nothing to report about, and
            # the report is the part that must not be lost.
            say("final stop: nothing was running when the run ended")
        else:
            say(f"final stop: {final.signalled or 'none'} exit "
                f"{final.returncode}"
                + (f", last error: {final.last_error}" if final.last_error
                   else ""))
        say(f"compatibility: {sum(a.error_lines for a in self.attempts)} error line(s), "
              f"{sum(a.legacy_death_lines for a in self.attempts)} line(s) matching the "
              "older capture-died wording")
        say(f"watchdog: {self.watchdog_resets} unrequested reset(s) over "
              f"{self.watchdog_reads} read(s)"
              + ("; " + "; ".join(self.watchdog_where) if self.watchdog_where else ""))
        if self.options.restart_probe:
            probe = summary["process"]["probe_recovered"]
            say("restart probe: " + (
                "recovered" if probe
                else "did not recover" if probe is False
                else "no whole advertiser on period followed it, so whether it "
                     "recovered is not established"))
        self.problems = self.judge()
        if verdict is None:
            verdict = "fail" if self.problems else "pass"
        # Written after the verdict and carrying it: the one machine-readable
        # artifact of a run of days has to say how it came out.
        summary["verdict"] = verdict
        summary["problems"] = list(self.problems)
        # Written beside and renamed into place: a canceled run has about
        # two seconds before the harness kills it, and a truncate-then-write
        # interrupted in the middle would leave an empty file where the one
        # machine-readable artifact of a run of days should be.
        final_path = self.options.out / "summary.json"
        beside = final_path.with_suffix(".json.part")
        beside.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        os.replace(beside, final_path)
        self.record({"record": "finished", "verdict": verdict,
                      "problems": list(self.problems)})
        for problem in self.problems:
            say(f"FAIL: {problem}")
        say(f"SOAK VERDICT: {verdict}")
        return EXIT_FAIL if self.problems else EXIT_PASS


# ---------------------------------------------------------------------------
# Command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="addresssoak.py",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--channel", required=True, type=int, choices=(37, 38, 39))
    parser.add_argument("--duration", required=True, type=float)
    parser.add_argument("--jlink", required=True)
    parser.add_argument("--dk-port")
    parser.add_argument("--dk-alias", default="")
    parser.add_argument("--interval", required=True, type=int)
    parser.add_argument("--payload", required=True)
    parser.add_argument("--advertiser-on", required=True, type=float)
    parser.add_argument("--advertiser-off", required=True, type=float)
    parser.add_argument("--sample-every", required=True, type=float)
    parser.add_argument("--presence-window", required=True, type=float)
    parser.add_argument("--presence-windows", required=True, type=int)
    parser.add_argument("--liveness-timeout", required=True, type=float)
    parser.add_argument("--detect-within", required=True, type=float)
    parser.add_argument("--min-detection-share", required=True, type=float)
    parser.add_argument("--max-gap", required=True, type=float)
    parser.add_argument("--min-packets-per-minute", required=True, type=int)
    parser.add_argument("--max-disagreement-share", required=True, type=float)
    parser.add_argument("--max-restarts", required=True, type=int)
    parser.add_argument("--max-memory-growth-kb", type=int)
    parser.add_argument("--restart-probe", action="store_true")
    parser.add_argument("--max-capture-bytes", type=int,
                        default=DEFAULT_CAPTURE_LIMIT_BYTES)
    parser.add_argument("--max-total-capture-bytes", type=int,
                        default=DEFAULT_CAPTURE_BUDGET_BYTES)
    parser.add_argument("--term-grace", type=float, default=DEFAULT_TERM_GRACE_S)
    parser.add_argument("--restart-min-interval", type=float,
                        default=DEFAULT_RESTART_MIN_INTERVAL_S)
    return parser


def _check_values(args) -> None:
    """Refuse a figure the catalog would refuse, for a run started by hand.

    The driver is documented as runnable on its own, so the contract cannot
    live only in the catalog: a negative budget read as "no budget" would
    quietly turn the disk bound off.
    """
    for name, value in (("--presence-windows", args.presence_windows),
                        ("--min-packets-per-minute", args.min_packets_per_minute),
                        ("--max-restarts", args.max_restarts),
                        ("--max-capture-bytes", args.max_capture_bytes),
                        ("--max-total-capture-bytes", args.max_total_capture_bytes),
                        ("--max-memory-growth-kb", args.max_memory_growth_kb)):
        if value is not None and value < 0:
            raise SoakError(f"{name} cannot be negative (got {value})")
    if args.presence_windows < 1:
        raise SoakError("--presence-windows must be at least 1")
    for name, value in (("--min-detection-share", args.min_detection_share),
                        ("--max-disagreement-share", args.max_disagreement_share)):
        if not 0 < value <= 1:
            raise SoakError(f"{name} must be a fraction above 0 and at most 1 "
                            f"(got {value})")
    for name, value in (("--duration", args.duration),
                        ("--advertiser-on", args.advertiser_on),
                        ("--advertiser-off", args.advertiser_off),
                        ("--sample-every", args.sample_every),
                        ("--presence-window", args.presence_window),
                        ("--liveness-timeout", args.liveness_timeout),
                        ("--detect-within", args.detect_within),
                        ("--max-gap", args.max_gap),
                        ("--term-grace", args.term_grace),
                        ("--restart-min-interval", args.restart_min_interval)):
        if value < 0:
            raise SoakError(f"{name} cannot be negative (got {value})")


def options_from(args) -> Options:
    _check_values(args)
    payload = "" if args.payload.lower() == "none" else args.payload.lower()
    return Options(
        out=Path(args.out),
        serial=args.serial.lower(),
        address=nrfdk.normalize_address(args.address),
        channel=args.channel,
        duration_s=args.duration,
        jlink=args.jlink,
        dk_port=args.dk_port,
        dk_alias=args.dk_alias,
        interval_ms=args.interval,
        payload=payload,
        on_s=args.advertiser_on,
        off_s=args.advertiser_off,
        sample_every_s=args.sample_every,
        window_s=args.presence_window,
        windows_needed=args.presence_windows,
        liveness_timeout_s=args.liveness_timeout,
        detect_within_s=args.detect_within,
        min_detection_share=args.min_detection_share,
        max_gap_s=args.max_gap,
        min_packets_per_minute=args.min_packets_per_minute,
        max_disagreement_share=args.max_disagreement_share,
        max_restarts=args.max_restarts,
        max_memory_growth_kb=args.max_memory_growth_kb,
        restart_probe=args.restart_probe,
        capture_limit_bytes=args.max_capture_bytes,
        capture_budget_bytes=args.max_total_capture_bytes,
        term_grace_s=args.term_grace,
        restart_min_interval_s=args.restart_min_interval,
    )


def main(argv=None, dk=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        options = options_from(args)
        check_timings(options)
    except (nrfdk.NrfDkError, SoakError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    if dk is None:
        try:
            dk = nrfdk.resolve(options.jlink, options.dk_port, alias=options.dk_alias)
        except nrfdk.NrfDkError as error:
            print(f"error: {error}", file=sys.stderr)
            return EXIT_CONFIGURATION
    soak = Soak(options, dk)

    def stop(number, _frame):
        soak.cancel(signal.Signals(number).name)

    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(number, stop)
        except (ValueError, OSError):  # not the main thread, or not supported
            pass
    try:
        return soak.run()
    except SoakError as error:
        print(f"error: {error}", file=sys.stderr)
        say("SOAK VERDICT: configuration")
        return EXIT_CONFIGURATION
    except nrfdk.NrfDkError as error:
        print(f"error: advertiser: {error}", file=sys.stderr)
        say("SOAK VERDICT: configuration")
        return EXIT_CONFIGURATION


if __name__ == "__main__":
    sys.exit(main())
