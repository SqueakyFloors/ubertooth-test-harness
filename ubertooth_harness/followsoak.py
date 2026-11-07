"""Run a long BLE capture across a known advertiser and connection going away.

Run as a script by the `Intermittent follow soak` case::

    python followsoak.py --out DIR --serial HEX --channel 37|38|39
        --duration S --peripheral-jlink SERIAL --peripheral-address ADDR
        --central-jlink SERIAL --central-address ADDR
        [--peripheral-port PATH --peripheral-alias NAME]
        [--central-port PATH --central-alias NAME]
        --advertising S --connected S --absent S
        --checkpoint-every S --sample-every S
        --conn-interval MS --write-bytes N --write-period MS
        --presence-window S --presence-windows N
        --liveness-timeout S --detect-within S --connect-within S
        --acquire-within S --quiet-after S --max-gap S
        --min-detection-share F --min-follow-share F
        --min-connection-packets N --max-restarts N
        [--max-memory-growth-kb N]
        [--max-capture-bytes N] [--max-total-capture-bytes N]
        [--term-grace S] [--restart-min-interval S]

One Ubertooth, pinned by serial, runs ``ubertooth-btle -f -U <serial>
-A<channel> -q <file>``: following connections, which is how a
production-style service watches a device it expects to connect and
disconnect. Two registered nRF52840 DKs are the fixture, one in the
``peripheral`` role and one in the ``central`` role, driven by this driver
over their own shells.

**What makes this different from one long capture.** `BLE address parity
soak` (addresssoak.py) holds one state for its whole window: an advertiser
that goes on and off while the board does nothing but listen. This one
changes the *radio state* the board has to keep up with, three times every
cycle:

1. **advertising** -- the peripheral advertises connectable and nothing is
   connected, so the board is hunting and must find the advertiser;
2. **connected** -- the central connects to the peripheral and writes to it
   for the whole phase, so the board must acquire the connection from its
   CONNECT_IND and record its data packets;
3. **absent** -- the central disconnects and the peripheral stops
   advertising, so the pair is off the air altogether and the board must stop
   reporting it.

Then the cycle begins again, which is where reacquisition is measured: every
advertising phase after the first follows an absence, so its first-sighting
time is the time to find a device that went away and came back.

**The fixture is driven, not watched.** Each transition is a shell command to
a DK, and the central's own counters say whether the link it was told to make
really came up: a phase whose ``conn start`` did not reach the board, or
whose central never reported itself connected, is recorded as a schedule
failure and left out of the verdict rather than counted against the board.
That is what keeps a follow that found nothing distinguishable from a
connection that never happened.

**Watchdog checkpoints.** ``ubertooth-util -w`` is a control read on the same
board the capture holds, so it is never asked for beside a running capture.
Every ``--checkpoint-every`` seconds the run takes a *planned checkpoint* at
the start of an absent phase, where the fixture is off the air anyway:

- the capture is ended cleanly (SIGTERM, then the term grace);
- the run waits for the process to go and confirms the board answers a read,
  so it is idle and nothing else has it;
- ``ubertooth-util -U <serial> -w`` is read;
- the counters, and what rose since the last read, are recorded against the
  interval that just ended;
- the next capture is started, in a new file.

A checkpoint is classified as planned: it is not a restart, not a recovery,
and not a failure. What *is* a failure is a checkpoint that could not do its
job -- a capture that had to be killed, a board that did not answer
afterwards, or a board the next capture found still in a mode.

The record is read before the run, at every checkpoint, after every unplanned
restart, and at the end, and each read closes an **interval**: the stretch of
the run since the previous read, with the phases and cycles it covered and
every counter that rose inside it -- unrequested resets, capture transfer
errors, and the radio waits that gave up. So a counter that moves is
timestamped and attributed to the stretch it moved in rather than to the run
as a whole.

**Leases and parallel operation.** Every command this driver runs names the
board by serial, so the test binds the Ubertooth and the two DKs and nothing
else: no other Ubertooth on the host is guarded, and the test writes no
firmware and switches no power, so the run takes no host lock. Host-only work
and a hardware run on other devices can go on beside it, which matters when
the longest suite holds its own devices for three days. The DKs must already
run the pinned role firmware: the case checks it and refuses rather than
flashing, because flashing is what would take the host lock for the whole
window.

**What it writes.** In ``--out``: one ``capture-NNNN.pcap`` and one
``btle-NNNN.log`` per capture attempt, ``progress.jsonl``, appended and
flushed as the run goes, and ``summary.json`` at the end, so a run of days is
reviewable while it runs and after the client that started it has gone. The
transcript, the progress file, the per-file capture size and the run's whole
capture budget are all bounded, so no file here grows with the length of the
run; what the budget removes is the oldest complete capture, newest kept.

Only counts, the two DKs' own registered addresses, the access addresses of
connections this fixture announced, and the harness's own figures are printed
or recorded. Another device's address never leaves the tool's transcript, and
a connection the fixture did not make is counted and never described.

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
from pathlib import Path
import signal
import statistics
import sys
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field

if __package__ in (None, ""):
    # Run as a script: make the package importable, as nrfdk.py does.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness import advcapture, conncapture, nrfdk, watchdog  # noqa: E402
from ubertooth_harness.addresssoak import (  # noqa: E402
    CAPTURE_READY_TIMEOUT_S,
    DEFAULT_CAPTURE_BUDGET_BYTES,
    DEFAULT_CAPTURE_LIMIT_BYTES,
    DEFAULT_RESTART_MIN_INTERVAL_S,
    DEFAULT_TERM_GRACE_S,
    EXIT_CONFIGURATION,
    EXIT_FAIL,
    EXIT_PASS,
    MAX_CAPTURE_ERRORS,
    MAX_FINAL_POLLS,
    MAX_HELD_ATTEMPTS,
    MAX_LOG_BYTES,
    MAX_OTHER_ADDRESSES,
    MAX_PROGRESS_BYTES,
    MAX_SUMMARY_ATTEMPTS,
    MAX_WATCHDOG_WHERE,
    MIN_EVENTS_PER_WINDOW,
    MIN_SAMPLE_EVERY_S,
    MIN_SCHEDULE_LEG_S,
    MIN_WINDOW_S,
    POLL_S,
    RESET_TIMEOUT_S,
    WATCHDOG_BOARD_SILENT,
    Attempt,
    Capture,
    DkShell,
    LineSink,
    RecordClock,
    Side,
    SoakError,
    board_answers,
    prune_capture_files,
    read_watchdog,
    resident_kb,
    run_util,
    say,
)

# The tool's mode: following connections, not advertising only. That is the
# whole reason this soak exists beside the address one, so it is named here
# rather than passed in.
TOOL_MODE = "-f"

ADVERTISING = "advertising"
CONNECTED = "connected"
ABSENT = "absent"
# One cycle, in the order the fixture goes through it.
PHASES = (ADVERTISING, CONNECTED, ABSENT)

# How much of its scheduled length a phase must have had to be judged. The
# same figure the address soak uses: a phase the run cut short was not known
# to be in that state throughout.
PHASE_COMPLETE_SHARE = 0.9
# How many phases a run may hold. Each one is kept for the whole run, for the
# detection and follow figures, and writes a synced progress record; three
# days of 80-second cycles is about ten thousand.
MAX_PHASES = 30000
# How many connections the attribution map holds. A connection's packets
# arrive within seconds of its own CONNECT_IND, so the map only has to reach
# back over the capture reader's lag; the phase keeps its own figures, so a
# connection falling out of the map loses nothing that was counted.
MAX_LINKS_TRACKED = 64
# How many access addresses of connections this fixture did not announce are
# counted before the count becomes a floor. They are never printed; this only
# bounds what a busy bench costs in memory.
MAX_OTHER_LINKS = 10000
# How many intervals and checkpoints the summary carries in full.
# progress.jsonl holds every one of them.
MAX_INTERVALS_SUMMARIZED = 200
# How many intervals a run holds in memory. A board that died in the first
# hour of a three-day run reads its record after every restart, so past this
# the middle ones are forgotten, oldest and newest kept; each one was
# recorded as it closed.
MAX_HELD_INTERVALS = 2000
# How long after the capture has gone the board is given to answer a read at
# a checkpoint, which is what says it is idle.
IDLE_TRIES = 3
IDLE_PAUSE_S = 1.0
# How often the central's own status is read while the run waits for the link
# it was told to make.
CONNECT_POLL_S = 0.5
# The connectable advertising interval the peripheral role uses, in
# milliseconds. It is the firmware's own and not a figure a plan can set:
# `start_peripheral_locked` advertises with BT_GAP_ADV_FAST_INT_MIN_2 to
# BT_GAP_ADV_FAST_INT_MAX_2 (firmware/nrf52840dk/src/connection.c), and the
# `adv interval` command belongs to the advertiser role, which this soak does
# not use. The presence-window check uses the slower end, because that is the
# worst case a window has to hold an advertising event in.
PERIPHERAL_ADV_INTERVAL_MIN_MS = 100
PERIPHERAL_ADV_INTERVAL_MAX_MS = 150
# How short a stretch of an absent phase is worth judging: long enough for a
# peripheral that was still advertising to have sent several events into it,
# which is the same reasoning a presence window is sized by. Below this, a
# phase whose checkpoint ran nearly to its end would join the judged
# population -- raising the allowance -- with no room for the fixture to have
# been heard in it at all.
MIN_ABSENT_STRETCH_S = (PERIPHERAL_ADV_INTERVAL_MAX_MS / 1000.0
                        * MIN_EVENTS_PER_WINDOW)


# ---------------------------------------------------------------------------
# The fixture


class Peer(DkShell):
    """One of the two DKs, its shell held open for the whole run.

    The retry and the counters are `DkShell`'s (addresssoak.py), which is the
    part a run of days depends on; what is here is what this fixture is asked
    to do, and the checks that say the board in front of the run is the board
    the registry describes.
    """

    def __init__(self, dk: nrfdk.Dk, role: str, address: str):
        super().__init__(dk, label=role)
        self.role = role
        self.address = address

    def check(self) -> dict:
        """The board runs the pinned firmware, in its role, at its address."""
        status = nrfdk.require_connection_fields(self.status())
        found = nrfdk.normalize_address(status["address"])
        if found != self.address:
            raise SoakError(
                f"the {self.role} reports {found}, the registry says {self.address}")
        if status["role"] != self.role:
            raise SoakError(
                f"the {self.role} reports role {status['role']}, not {self.role}")
        if status["fw"] != nrfdk.FIRMWARE_VERSION:
            raise SoakError(
                f"the {self.role} runs {status['fw']}, not {nrfdk.FIRMWARE_VERSION}")
        return status

    def quiet(self) -> dict:
        """Disconnect and stop, which is what every settings change needs.

        The firmware refuses a settings change while the connection role is
        started, so this comes before `configure` and at the end of every
        cycle.
        """
        self.command("conn stop")
        return self.status()


class Peripheral(Peer):
    """The DK that advertises connectable and waits to be connected."""

    def configure(self, options: "Options") -> dict:
        self.check()
        self.quiet()
        for command in (f"conn chmap {options.channel}", "conn dle on"):
            self.command(command)
        status = nrfdk.require_connection_fields(self.status())
        if status["conn_chmap"] != str(options.channel):
            raise SoakError(
                f"the peripheral advertises on channel(s) {status['conn_chmap']}, "
                f"not {options.channel}")
        return status

    def advertise(self) -> None:
        """Start advertising connectable. It does not wait for a link.

        The firmware's own `conn start` returns as soon as advertising is up
        for a peripheral, and waits for the link only for a central
        (firmware/nrf52840dk/src/connection.c, `cmd_start`), so this is an
        ordinary shell command with the ordinary timeout.
        """
        self.command("conn start")


class Central(Peer):
    """The DK that makes the link and writes to it for the whole phase."""

    def configure(self, options: "Options") -> dict:
        self.check()
        self.quiet()
        for command in (f"conn peer {options.peripheral_address}",
                        f"conn interval {options.conn_interval_ms}",
                        "conn dle on",
                        f"conn payload {options.write_bytes}",
                        f"conn period {options.write_period_ms}",
                        # Exactly one connection per phase, with no parameter
                        # update: this driver is what makes and breaks the
                        # link, so the firmware's own cycling would put
                        # connections in the capture nothing scheduled.
                        "conn cycle off", "conn autoupdate off"):
            self.command(command)
        status = nrfdk.require_connection_fields(self.status())
        if status["peer"] != options.peripheral_address:
            raise SoakError(
                f"the central is set to connect to {status['peer'] or 'nothing'}, "
                f"not the registered peripheral {options.peripheral_address}")
        if status["cycle_ms"] or status["autoupdate_ms"]:
            raise SoakError(
                "the central still has its own connection cycling or automatic "
                "parameter update switched on")
        return status

    def connect(self) -> None:
        """Make the link, which this command does not return until it has.

        The firmware waits on the connection for up to its own five seconds
        and answers with an error if none came
        (firmware/nrf52840dk/src/connection.c, `cmd_start`:
        `CONNECT_TIMEOUT_MS`). So the shell's ordinary three-second timeout
        would time out a link that was merely slow, and the retry behind it
        would then send `conn start` to a board that is already starting.
        `nrfdk.central` passes the same longer timeout for the same command.
        """
        self.command("conn start", timeout=nrfdk.CONNECT_TIMEOUT_S)


# ---------------------------------------------------------------------------
# What happened in one phase, and in one connection


@dataclass
class Link:
    """One connection the fixture announced, as the capture carries it.

    Kept by the connected phase that made it, so a connection falling out of
    the attribution map (MAX_LINKS_TRACKED) loses nothing that was counted.
    ``late`` is what says the follow ended when the connection did: packets
    on this access address after the phase that owned it was over and the
    settle time had passed.
    """

    access_address: int
    crc_init: int
    announced_at: float
    phase_index: int
    packets: int = 0
    valid_packets: int = 0
    long_packets: int = 0
    first_at: float | None = None
    last_at: float | None = None
    late: int = 0

    def to_dict(self) -> dict:
        return {
            # The fixture's own connection, so its access address is the
            # harness's to print; a connection this run did not announce is
            # counted and never described.
            "access_address": f"0x{self.access_address:08x}",
            "announced_at_s": round(self.announced_at, 3),
            "phase": self.phase_index,
            "packets": self.packets,
            "valid_packets": self.valid_packets,
            "long_packets": self.long_packets,
            "first_at_s": None if self.first_at is None else round(self.first_at, 3),
            "last_at_s": None if self.last_at is None else round(self.last_at, 3),
            "late_packets": self.late,
        }


@dataclass
class Phase:
    """One leg of the cycle, as it was really applied."""

    index: int
    state: str
    cycle: int
    started: float
    ended: float | None = None
    # Whether an absent phase came before this one, which is what makes an
    # advertising phase a reacquisition rather than a first acquisition.
    after_absence: bool = False
    # The peripheral's advertising, read two ways: the capture's own records
    # (what the verdict rests on) and the tool's AdvA line (reported beside
    # it, since the parity question itself belongs to the address soak).
    structural: Side = field(default_factory=Side)
    text: Side = field(default_factory=Side)
    # The connection this phase made, for a connected phase.
    link: Link | None = None
    connect_inds: int = 0
    acquired_at: float | None = None
    # When the central reported itself connected, and what its own counters
    # did over the phase, which is the fixture saying the link existed.
    linked_at: float | None = None
    connects: int = 0
    writes: int = 0
    schedule_error: str = ""
    # A capture was stopped or started while this phase was running, so for
    # part of it nothing was listening. Planned ones are the run's own doing
    # -- a checkpoint, or a capture file rolled over -- and only the
    # unplanned ones count against the share of its phases a run must judge.
    planned_gaps: int = 0
    unplanned_gaps: int = 0
    checkpoints: int = 0
    # The run itself ended this phase: its duration ran out, or it stopped
    # early because its capture budget could not be kept. Every run ends in
    # one, and it is the run's own doing, so it is not counted against the
    # share of its phases a run has to be able to judge -- and it is not
    # called `cut_by_duration`, because the budget can end a run too.
    ended_by_the_run: bool = False
    listening_from: float | None = None

    @property
    def capture_gap(self) -> bool:
        return bool(self.planned_gaps or self.unplanned_gaps)

    @property
    def seconds(self) -> float:
        return 0.0 if self.ended is None else self.ended - self.started

    def to_dict(self) -> dict:
        return {
            "record": "phase",
            "index": self.index,
            "cycle": self.cycle,
            "state": self.state,
            "started_s": round(self.started, 3),
            "ended_s": None if self.ended is None else round(self.ended, 3),
            "after_absence": self.after_absence,
            "structural": self.structural.to_dict(),
            "text": self.text.to_dict(),
            "connect_inds": self.connect_inds,
            "acquired_at_s": (None if self.acquired_at is None
                              else round(self.acquired_at, 3)),
            "linked_at_s": (None if self.linked_at is None
                            else round(self.linked_at, 3)),
            "central_connects": self.connects,
            "central_writes": self.writes,
            "link": None if self.link is None else self.link.to_dict(),
            "schedule_error": self.schedule_error,
            "capture_gap": self.capture_gap,
            "planned_capture_gaps": self.planned_gaps,
            "unplanned_capture_gaps": self.unplanned_gaps,
            "checkpoints": self.checkpoints,
            "ended_by_the_run": self.ended_by_the_run,
        }


@dataclass
class Interval:
    """The stretch between two watchdog reads, and what rose inside it.

    SqueakyFloors/ubertooth#703 asks for the board's record at the start, at
    each checkpoint, after each unplanned restart, and at the end. Each read
    closes one of these, so a counter that moved is attributed to the stretch
    it moved in rather than to a run of three days as a whole.
    """

    index: int
    started: float
    ended: float | None = None
    # Why this interval ended: the checkpoint, a restart, or the run.
    reason: str = ""
    phases: int = 0
    cycles: int = 0
    advertising_packets: int = 0
    connection_packets: int = 0
    records: int = 0
    restarts: int = 0
    # From watchdog.compare, against the board's running baseline.
    watchdog_read: bool = False
    watchdog_reason: str = ""
    new_resets: int = 0
    new_transfer_errors: int = 0
    new_radio_waits: dict = field(default_factory=dict)
    new_fault: str = ""
    counters_restarted: bool = False
    rss_kb: int | None = None

    @property
    def seconds(self) -> float:
        return 0.0 if self.ended is None else self.ended - self.started

    @property
    def counters_moved(self) -> bool:
        return bool(self.new_resets or self.new_transfer_errors
                    or self.new_radio_waits or self.new_fault)

    def to_dict(self) -> dict:
        return {
            "record": "interval",
            "index": self.index,
            "started_s": round(self.started, 3),
            "ended_s": None if self.ended is None else round(self.ended, 3),
            "seconds": round(self.seconds, 3),
            "ended_by": self.reason,
            "phases": self.phases,
            "cycles": self.cycles,
            "advertising_packets": self.advertising_packets,
            "connection_packets": self.connection_packets,
            "records": self.records,
            "restarts": self.restarts,
            "watchdog_read": self.watchdog_read,
            "watchdog_reason": self.watchdog_reason,
            "new_unrequested_resets": self.new_resets,
            "new_transfer_errors": self.new_transfer_errors,
            "new_radio_waits": dict(self.new_radio_waits),
            "new_fault": self.new_fault,
            "counters_restarted": self.counters_restarted,
            "rss_kb": self.rss_kb,
        }


@dataclass
class Checkpoint:
    """One planned stop, read and restart, and whether it did its job."""

    index: int
    at: float
    phase_index: int
    stopped_cleanly: bool = False
    board_idle: bool = False
    board_reason: str = ""
    watchdog_read: bool = False
    watchdog_reason: str = ""
    capture_restarted: bool = False
    seconds: float = 0.0
    # The run was stopped while this checkpoint was running, so what it did
    # not get to is the cancellation's doing and not the checkpoint's.
    canceled: bool = False

    @property
    def problem(self) -> str:
        """Why this checkpoint did not do its job, if it did not."""
        if self.canceled:
            return ""
        if not self.stopped_cleanly:
            return "the capture did not stop on SIGTERM and had to be killed"
        if not self.board_idle:
            return f"the board did not answer afterwards: {self.board_reason}"
        if not self.capture_restarted:
            return "the next capture was not started"
        return ""

    def to_dict(self) -> dict:
        return {
            "record": "checkpoint",
            "index": self.index,
            "at_s": round(self.at, 3),
            "phase": self.phase_index,
            "planned": True,
            "stopped_cleanly": self.stopped_cleanly,
            "board_idle": self.board_idle,
            "board_reason": self.board_reason,
            "watchdog_read": self.watchdog_read,
            "watchdog_reason": self.watchdog_reason,
            "capture_restarted": self.capture_restarted,
            "canceled": self.canceled,
            "seconds": round(self.seconds, 3),
            "problem": self.problem,
        }


# ---------------------------------------------------------------------------
# Options


@dataclass(frozen=True)
class Timings:
    """The figures whose relationships decide whether a run can be judged.

    Apart on purpose: the same relationships are checked when a plan loads,
    where there is no board, no output directory and no fixture, and checking
    them twice from two copies of the rules is how they would come to differ
    (catalog.`_check_follow_soak`).
    """

    duration_s: float
    advertising_s: float
    connected_s: float
    absent_s: float
    checkpoint_every_s: float
    sample_every_s: float
    window_s: float
    windows_needed: int
    liveness_timeout_s: float
    detect_within_s: float
    connect_within_s: float
    acquire_within_s: float
    quiet_after_s: float
    max_gap_s: float
    write_period_ms: int = 50
    min_connection_packets: int = 1

    @property
    def cycle_s(self) -> float:
        return self.advertising_s + self.connected_s + self.absent_s


@dataclass
class Options:
    out: Path
    serial: str
    channel: int
    duration_s: float
    peripheral_jlink: str
    peripheral_address: str
    peripheral_port: str | None
    peripheral_alias: str
    central_jlink: str
    central_address: str
    central_port: str | None
    central_alias: str
    advertising_s: float
    connected_s: float
    absent_s: float
    checkpoint_every_s: float
    sample_every_s: float
    conn_interval_ms: int
    write_bytes: int
    write_period_ms: int
    window_s: float
    windows_needed: int
    liveness_timeout_s: float
    detect_within_s: float
    connect_within_s: float
    acquire_within_s: float
    quiet_after_s: float
    max_gap_s: float
    min_detection_share: float
    min_follow_share: float
    min_connection_packets: int
    max_restarts: int
    max_memory_growth_kb: int | None
    capture_limit_bytes: int
    capture_budget_bytes: int
    term_grace_s: float
    restart_min_interval_s: float

    def timings(self) -> Timings:
        return Timings(
            duration_s=self.duration_s, advertising_s=self.advertising_s,
            connected_s=self.connected_s, absent_s=self.absent_s,
            checkpoint_every_s=self.checkpoint_every_s,
            sample_every_s=self.sample_every_s, window_s=self.window_s,
            windows_needed=self.windows_needed,
            liveness_timeout_s=self.liveness_timeout_s,
            detect_within_s=self.detect_within_s,
            connect_within_s=self.connect_within_s,
            acquire_within_s=self.acquire_within_s,
            quiet_after_s=self.quiet_after_s, max_gap_s=self.max_gap_s,
            write_period_ms=self.write_period_ms,
            min_connection_packets=self.min_connection_packets,
        )


def check_timings(options: Timings | Options) -> None:
    """Refuse a set of figures that could not judge what it asks for.

    The catalog makes the same checks on the plan's arguments before any
    board is leased (catalog.py, `_check_follow_soak`); these are here so the
    driver is sound when it is run by hand too. Each reason names the figure
    as a plan writes it ('detect within') rather than as this command line
    spells it, because a plan is where these are usually wrong and the
    catalog raises these same words.
    """
    if isinstance(options, Options):
        options = options.timings()
    for name, seconds in (("advertising", options.advertising_s),
                          ("connected", options.connected_s),
                          ("absent", options.absent_s)):
        if seconds < MIN_SCHEDULE_LEG_S:
            raise SoakError(
                f"'{name}' ({seconds:g}s) is shorter than {MIN_SCHEDULE_LEG_S:g}s; "
                "every leg of the cycle is a phase the run holds")
    cycle = options.cycle_s
    phases = len(PHASES) * options.duration_s / cycle
    if phases > MAX_PHASES:
        raise SoakError(
            f"{options.duration_s:g}s of {cycle:g}s cycles is about {phases:.0f} "
            f"phases, more than {MAX_PHASES}; the run holds every one of them")
    if options.duration_s < 3 * cycle:
        # Three, not two: the first advertising phase is not a reacquisition,
        # so a run of two cycles would measure one and could not compare.
        raise SoakError(
            f"the duration ({options.duration_s:g}s) is less than three {cycle:g}s "
            f"cycles, so there is nothing to compare")
    if options.detect_within_s > options.advertising_s:
        raise SoakError(
            f"'detect within' ({options.detect_within_s:g}s) is longer than the "
            f"{options.advertising_s:g}s the peripheral advertises, so a phase "
            "could end before its own detection deadline")
    if options.window_s * options.windows_needed > options.advertising_s:
        raise SoakError(
            f"sustained presence ({options.windows_needed} x "
            f"{options.window_s:g}s windows) does not fit in the "
            f"{options.advertising_s:g}s the peripheral advertises")
    if options.connect_within_s > options.connected_s:
        raise SoakError(
            f"'connect within' ({options.connect_within_s:g}s) is longer than the "
            f"{options.connected_s:g}s the link is held, so the phase could end "
            "before the central was asked to have connected")
    if options.acquire_within_s > options.connected_s:
        raise SoakError(
            f"'acquire within' ({options.acquire_within_s:g}s) is longer than the "
            f"{options.connected_s:g}s the link is held, so a phase could end "
            "before its own follow deadline")
    if options.connect_within_s > options.acquire_within_s:
        # The follow deadline is measured from the start of the phase, so a
        # link allowed to come up after it would fail the follow for the
        # fixture's own slowness.
        raise SoakError(
            f"'connect within' ({options.connect_within_s:g}s) is longer than "
            f"'acquire within' ({options.acquire_within_s:g}s), so a link that "
            "came up in time would miss the follow deadline measured from the "
            "same moment")
    if options.absent_s - options.quiet_after_s < MIN_ABSENT_STRETCH_S:
        # Not merely shorter: the stretch past the settle point has to be
        # long enough for a peripheral that was still advertising to have
        # been heard in it, or every absent phase would join the judged
        # population and none of them could ever fail.
        raise SoakError(
            f"'quiet after' ({options.quiet_after_s:g}s) leaves less than "
            f"{MIN_ABSENT_STRETCH_S:g}s of the {options.absent_s:g}s the fixture "
            "is off the air, which is too short a stretch to judge it on")
    # The longest silence a healthy run really has: the connected phase, where
    # only the link is on the air and a sniffer may catch none of it, then the
    # absent phase, then the wait for the next advertising phase's first
    # packet. A timeout below that restarts the capture every cycle.
    quiet = options.connected_s + options.absent_s + options.detect_within_s
    if options.liveness_timeout_s <= quiet:
        raise SoakError(
            f"'liveness timeout' ({options.liveness_timeout_s:g}s) is not longer "
            f"than a connected phase, an absent phase and the wait for the next "
            f"detection ({options.connected_s:g}s + {options.absent_s:g}s + "
            f"{options.detect_within_s:g}s), so the cycle's own silence would "
            "restart the capture")
    # A gap is measured inside one advertising phase, so the limit has to sit
    # between the detection deadline -- every phase's leading gap is at least
    # the time to its first sighting -- and the phase's own length, past
    # which no gap could ever reach it.
    if options.max_gap_s < options.detect_within_s:
        raise SoakError(
            f"'maximum gap' ({options.max_gap_s:g}s) is shorter than 'detect "
            f"within' ({options.detect_within_s:g}s), so the wait for the first "
            "packet of a phase would fail it")
    if options.max_gap_s >= options.advertising_s:
        raise SoakError(
            f"'maximum gap' ({options.max_gap_s:g}s) is not shorter than the "
            f"{options.advertising_s:g}s the peripheral advertises, so no gap "
            "inside a phase could ever reach it")
    if options.checkpoint_every_s < 2 * cycle:
        # Two, not one. A checkpoint stops the capture at the start of an
        # absent phase, so that phase has a stretch with nothing listening
        # and is not judged; an interval of one cycle would checkpoint every
        # absent phase there was, and the run would fail saying the
        # fixture's going away was never measured. With two, every other
        # absent phase is untouched.
        raise SoakError(
            f"'checkpoint every' ({options.checkpoint_every_s:g}s) is shorter "
            f"than two {cycle:g}s cycles, so a checkpoint would interrupt every "
            "absent phase and leave the fixture's going away unmeasured")
    if options.checkpoint_every_s > options.duration_s / 2:
        raise SoakError(
            f"'checkpoint every' ({options.checkpoint_every_s:g}s) leaves fewer "
            f"than two checkpoints in {options.duration_s:g}s, so the watchdog "
            "record would not be read inside the window and compared")
    if options.sample_every_s > options.duration_s / 3:
        raise SoakError(
            f"'sample every' ({options.sample_every_s:g}s) leaves fewer than "
            f"three samples in {options.duration_s:g}s")
    # Both of these are bounded below as well, because the run holds a window
    # per reader per phase and writes a record per sample.
    if options.window_s < MIN_WINDOW_S:
        raise SoakError(
            f"'presence window' ({options.window_s:g}s) is shorter than "
            f"{MIN_WINDOW_S:g}s; a run holds a window per reader per phase")
    if options.sample_every_s < MIN_SAMPLE_EVERY_S:
        raise SoakError(
            f"'sample every' ({options.sample_every_s:g}s) is shorter than "
            f"{MIN_SAMPLE_EVERY_S:g}s; each sample is a synced record")
    interval_s = PERIPHERAL_ADV_INTERVAL_MAX_MS / 1000.0
    if interval_s * MIN_EVENTS_PER_WINDOW > options.window_s:
        raise SoakError(
            f"the peripheral role advertises every {interval_s:g}s at its "
            f"slowest, which leaves fewer than {MIN_EVENTS_PER_WINDOW} "
            f"advertising events in a {options.window_s:g}s presence window, so a "
            "window would be empty whenever one event is lost or late")
    # The floor on a connection's packets cannot be above what the central
    # could send in the phase: the sniffer catches a fraction of a followed
    # connection, so a floor at or above the writes themselves can never be
    # met however well the follow works.
    writes = options.connected_s * 1000.0 / options.write_period_ms
    if options.min_connection_packets > writes:
        raise SoakError(
            f"'minimum connection packets' ({options.min_connection_packets}) is "
            f"more than the about {writes:.0f} writes the central makes in a "
            f"{options.connected_s:g}s connected phase, so no phase could meet it")


# ---------------------------------------------------------------------------
# The run


class Soak:
    """The whole run: the cycle, the capture, the checkpoints, the verdict."""

    def __init__(self, options: Options, peripheral_dk: nrfdk.Dk,
                 central_dk: nrfdk.Dk, clock=time.monotonic, wall=time.time):
        self.options = options
        self.clock = clock
        self.wall = wall
        self.peripheral = Peripheral(peripheral_dk, "peripheral",
                                     options.peripheral_address)
        self.central = Central(central_dk, "central", options.central_address)
        self.sink = LineSink()
        self.phases: list[Phase] = []
        self.attempts: list[Attempt] = []
        self.intervals: list[Interval] = []
        self.intervals_forgotten = 0
        self.intervals_opened = 0
        self.checkpoints: list[Checkpoint] = []
        # Connections the fixture announced, newest last, bounded: a phase
        # keeps its own link, so what falls out here is only the lookup.
        self.links: "OrderedDict[int, Link]" = OrderedDict()
        self.other_link_addresses: set = set()
        self.other_link_packets = 0
        self.unannounced_packets = 0
        self.connect_inds = 0
        self.other_connect_inds = 0
        self.connection_packets = 0
        self.valid_connection_packets = 0
        self.long_connection_packets = 0
        self.advertising_packets = 0
        self.text_packets = 0
        self.attempt_text_packets = 0
        self.other_addresses: set = set()
        self.other_packets = 0
        self.records_seen = 0
        self.unattributed_packets = 0
        # Half a window: a record placed further from the host's reading
        # than that could fall in the wrong window.
        self.record_clock = RecordClock(max(0.25, options.window_s / 2))
        self.watchdog_reads = 0
        self.watchdog_baseline = None
        self.watchdog_resets = 0
        self.watchdog_transfer_errors = 0
        self.watchdog_where: list = []
        self.watchdog_unreadable = 0
        self.watchdog_not_reported = 0
        self.watchdog_last_reason = ""
        self.samples = 0
        self.rss_first = None
        self.attempt_rss_first = None
        self.attempt_rss_last = None
        self.rss_growth_max = 0
        self.rss_last = None
        self.rss_max = None
        self.capture: Capture | None = None
        self.capture_index = 0
        self.capture_errors: list = []
        self.capture_error_count = 0
        self._seen_capture_errors: set = set()
        self.captures_removed = 0
        self.capture_bytes_removed = 0
        self.capture_budget_stuck: list = []
        self.attempts_forgotten = 0
        self.transcript_budget = MAX_LOG_BYTES
        self.transcript_bytes = 0
        self.transcript_dropped_lines = 0
        self.progress = None
        self.progress_bytes = 0
        self.progress_records_dropped = 0
        self.restarts = 0
        self.rotations = 0
        self.unexplained_exits = 0
        self.forced_stops = 0
        self.board_resets = 0
        self.schedule_errors = 0
        self.closing_error = None
        self.canceled = ""
        self.window_start = 0.0
        self.start_mono = clock()
        self.started_wall = wall()
        self.last_packet_at = 0.0
        self.last_restart_at = -options.restart_min_interval_s
        self.reset_next_restart = False
        # Attempts a checkpoint started, so a board the next capture found
        # still in a mode is attributed to the checkpoint that left it.
        self.checkpoint_attempts: set = set()
        self.cycles = 0
        self.problems: list[str] = []
        self.extra_problems: list = []
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
        flushed and synced as it is written. Bounded like the transcript and
        for the same reason: past the budget the records are counted rather
        than written, and summary.json carries the figures either way.
        """
        if self.progress is None:
            return
        entry = {"at_s": round(self.clock() - self.start_mono, 3), **entry}
        line = json.dumps(entry, sort_keys=True) + "\n"
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
        """How long the cycle has been running, which the duration bounds."""
        return self.elapsed - self.window_start

    def record_time(self, epoch: float) -> float:
        return self.record_clock.at(epoch, self.elapsed)

    # -- the cycle --------------------------------------------------------

    def phase(self) -> Phase | None:
        return self.phases[-1] if self.phases else None

    def drive(self, phase: Phase, state: str) -> None:
        """Tell the fixture to be in ``state``, recording a failure on ``phase``.

        A transition that failed twice is recorded against the phase it
        belongs to, which leaves that phase out of the verdict rather than
        counting the fixture's trouble against the board.
        """
        try:
            if state == ADVERTISING:
                self.peripheral.advertise()
            elif state == CONNECTED:
                self.central.connect()
            else:
                # The central disconnects first: a peripheral that stopped
                # advertising while still connected would leave the link up
                # with nothing announcing it.
                self.central.command("conn stop")
                self.peripheral.command("conn stop")
        except SoakError as error:
            phase.schedule_error = str(error)
            self.schedule_errors += 1

    def open_phase(self, state: str) -> Phase:
        """Start a phase, and tell the fixture, in the order that phase needs.

        For an advertising or an absent phase the fixture is told first and
        the phase begins afterwards, so the shell command's own latency is
        charged to neither the detection deadline nor the settle time: a
        phase runs for the length it was given, measured from when the
        fixture was told.

        A connected phase is the other way round, and it has to be. The
        central's `conn start` does not return until the link is up
        (firmware/nrf52840dk/src/connection.c, `cmd_start` waits on
        `connected_sem`), so the CONNECT_IND is already on the air while the
        command is still running. A phase that began afterwards would not
        contain its own acquisition: the record would fall between two
        phases, `acquired_at` would stay unset, and every connected phase
        would read as a follow that never happened. So the phase is counting
        before the command goes out, and the connect latency is inside the
        follow deadline by construction -- which is why the catalog requires
        `acquire within` to be at least `connect within`.
        """
        previous = self.phase()
        if state == ADVERTISING:
            self.cycles += 1
        phase = Phase(len(self.phases), state, self.cycles, self.elapsed,
                      after_absence=(previous is not None
                                     and previous.state == ABSENT))
        if state == CONNECTED:
            self.phases.append(phase)
            self.drive(phase, state)
            if not phase.schedule_error:
                self.wait_for_link(phase)
        else:
            self.drive(phase, state)
            phase.started = self.elapsed
            self.phases.append(phase)
        self.record({"record": "phase_started", "index": phase.index,
                     "cycle": phase.cycle, "state": state,
                     "after_absence": phase.after_absence,
                     "schedule_error": phase.schedule_error})
        return phase

    def wait_for_link(self, phase: Phase) -> None:
        """Wait, bounded, for the central to report the link it was told to make.

        This is the fixture reporting on itself, and it is what keeps a
        follow that found nothing apart from a connection that never
        happened. A link that does not come up inside 'connect within' is a
        schedule failure for this phase, so the phase is left out of the
        follow verdict instead of counted against the board.

        The readers are drained while it waits: the tool keeps printing and
        its capture keeps growing, and a wait that read neither would charge
        this stretch's packets to the wrong phase.
        """
        deadline = self.elapsed + self.options.connect_within_s
        while self.elapsed < deadline and not self.canceled:
            self.read_text()
            self.read_structural()
            try:
                status = nrfdk.require_connection_fields(self.central.status())
            except (SoakError, nrfdk.NrfDkError) as error:
                phase.schedule_error = f"the central's status failed: {error}"
                self.schedule_errors += 1
                return
            if status["connected"] and status["peer"] == self.options.peripheral_address:
                phase.linked_at = self.elapsed
                phase.connects = status["connects"]
                phase.writes = status["writes"]
                return
            time.sleep(CONNECT_POLL_S)
        if self.canceled:
            return
        phase.schedule_error = (
            f"the central did not report itself connected within "
            f"{self.options.connect_within_s:g}s")
        self.schedule_errors += 1

    def close_phase(self, phase: Phase) -> None:
        phase.ended = self.elapsed
        if phase.state == ADVERTISING:
            phase.structural.close(phase.ended, phase.started)
            phase.text.close(phase.ended, phase.started)
        if phase.state == CONNECTED and not phase.schedule_error and not self.canceled:
            # The central's own counters over the phase, read before the
            # disconnect the next phase sends: a phase whose link the
            # fixture did not make is a schedule failure, not a follow one.
            # Skipped on a cancellation, where the run has about two seconds
            # left and the summary is what must not be lost: this is a shell
            # command with a reopen-and-retry behind it, and the phase it
            # would judge is the one the cancellation ended.
            try:
                status = nrfdk.require_connection_fields(self.central.status())
            except (SoakError, nrfdk.NrfDkError) as error:
                phase.schedule_error = f"the central's status failed: {error}"
                self.schedule_errors += 1
            else:
                if status["writes"] <= phase.writes:
                    phase.schedule_error = (
                        f"the central wrote nothing over the phase (still "
                        f"{status['writes']} write(s))")
                    self.schedule_errors += 1
                phase.writes = status["writes"] - phase.writes
                phase.connects = status["connects"] - phase.connects
        self.record(phase.to_dict())

    def phase_at(self, at: float) -> Phase | None:
        """The phase a sighting belongs to, by its own time.

        Not the phase running now: both readers hand their sightings over
        later than the sighting itself -- the tool's output arrives in blocks
        and the capture's records are read on the next poll -- so attributing
        them to whatever phase the loop is in would charge a packet from the
        end of one phase to the start of the next. Searched from the newest
        phase, where almost every sighting belongs.
        """
        for phase in reversed(self.phases):
            if at >= phase.started and (phase.ended is None or at < phase.ended):
                return phase
        return None

    # -- the readers ------------------------------------------------------

    def read_text(self) -> None:
        """The tool's own AdvA lines, which is how a text service sees it.

        Reported rather than judged. The connection half of this soak is not
        visible in that line at all -- libbtbb prints no AdvA for a data
        packet -- and whether the text line agrees with the records about an
        advertiser is `BLE address parity soak`'s question, measured there
        over a capture that holds one state. What it is here for is the
        liveness rule, the error lines, and a figure beside the record
        reader's for the same phases.
        """
        for kind, at, address in self.sink.drain():
            if kind != "adva":
                continue
            seen = at - self.start_mono
            self.last_packet_at = max(self.last_packet_at, seen)
            if address != self.options.peripheral_address:
                self.other_packets += 1
                if len(self.other_addresses) < MAX_OTHER_ADDRESSES:
                    self.other_addresses.add(address)
                continue
            self.text_packets += 1
            self.attempt_text_packets += 1
            phase = self.phase_at(seen)
            if phase is not None:
                phase.text.see(seen, phase.started, self.options.window_s)

    def read_structural(self) -> None:
        """The capture's own records: the advertiser, and the connection."""
        if self.capture is None:
            return
        records = self.capture.poll_records()
        error = self.capture.tail.error
        if error and error not in self._seen_capture_errors:
            # One corrupt record header, a file that is not a capture, or the
            # wrong link type stops the reader for the rest of the attempt.
            # Reported and judged: without this the run would fail later for
            # a reason that is a symptom of the very fault it is diagnosing.
            self._note_capture_error(error)
            say(f"capture {self.capture_index}: {error}")
            self.record({"record": "capture_unreadable",
                         "index": self.capture_index, "reason": error})
        if records:
            # Any record at all is the capture working, whether or not it is
            # this fixture's: the liveness rule is about a capture that has
            # stopped producing.
            self.last_packet_at = max(self.last_packet_at, self.elapsed)
        for epoch, data in records:
            self.records_seen += 1
            self.read_record(data, self.record_time(epoch))

    def read_record(self, data: bytes, at: float) -> None:
        """One capture record: an advertisement, a CONNECT_IND, or data.

        In that order, because each test is cheaper and more specific than
        the next: `parse_advertising` takes the packets that carry an AdvA
        and a correct advertising CRC, a CONNECT_IND is the advertising-
        channel packet it leaves out, and anything else is on a connection's
        own access address.
        """
        packet = advcapture.parse_advertising(data)
        if packet is not None:
            if packet.address != self.options.peripheral_address:
                return
            self.advertising_packets += 1
            phase = self.phase_at(at)
            if phase is not None:
                phase.structural.see(at, phase.started, self.options.window_s)
            else:
                self.unattributed_packets += 1
            return
        found = conncapture.connect_ind_in(data, self.options.central_address,
                                           self.options.peripheral_address)
        if found is not None:
            self.note_connect_ind(found, at)
            return
        if conncapture.connect_ind_in(data) is not None:
            # A CONNECT_IND between two other devices: counted, never
            # described, and the reason a judged connected phase can fail
            # its follow objective on a busy bench.
            self.other_connect_inds += 1
            return
        self.note_data_packet(data, at)

    def note_connect_ind(self, found: conncapture.ConnectInd, at: float) -> None:
        """The fixture's own CONNECT_IND: the phase has acquired its link."""
        self.connect_inds += 1
        phase = self.phase_at(at)
        link = Link(found.access_address, found.crc_init, at,
                    -1 if phase is None else phase.index)
        self.links[found.access_address] = link
        while len(self.links) > MAX_LINKS_TRACKED:
            self.links.popitem(last=False)
        if phase is None:
            self.unattributed_packets += 1
            return
        phase.connect_inds += 1
        if phase.acquired_at is None:
            phase.acquired_at = at
            phase.link = link
        self.record({"record": "connect_ind", "phase": phase.index,
                     "at_s": round(at, 3),
                     "access_address": f"0x{found.access_address:08x}",
                     "interval_units": found.interval,
                     "latency": found.latency,
                     "timeout_units": found.timeout,
                     "hop": found.hop,
                     "channels": bin(found.channel_map).count("1")})

    def note_data_packet(self, data: bytes, at: float) -> None:
        """A packet on a connection's access address, ours or another's."""
        address = conncapture.record_access_address(data)
        if address is None or address == advcapture.ADV_ACCESS_ADDRESS:
            # An advertising-channel packet `parse_advertising` would not
            # take: a corrupted one, or a PDU with no AdvA. Nothing here
            # measures those; the address soak reports the asymmetry.
            return
        link = self.links.get(address)
        if link is None:
            self.unannounced_packets += 1
            self.other_link_packets += 1
            if len(self.other_link_addresses) < MAX_OTHER_LINKS:
                self.other_link_addresses.add(address)
            return
        item = conncapture.data_packet_in(data, link.access_address, link.crc_init)
        if item is None:
            return
        length, valid = item
        self.connection_packets += 1
        link.packets += 1
        if link.first_at is None:
            link.first_at = at
        link.last_at = at
        if valid:
            self.valid_connection_packets += 1
            link.valid_packets += 1
            if length >= conncapture.DEFAULT_DATA_LENGTH + 1:
                # Longer than a link without Data Length Extension could
                # carry, which is what the fixture asks for.
                self.long_connection_packets += 1
                link.long_packets += 1
        owner = self.phases[link.phase_index] if 0 <= link.phase_index < len(self.phases) else None
        if owner is not None and owner.ended is not None:
            settled = owner.ended + self.options.quiet_after_s
            if at > settled:
                # The connection was told to end and this packet says the
                # board was still following it. Counted on the link, so the
                # phase that made it is the one judged.
                link.late += 1

    # -- the capture ------------------------------------------------------

    def note_capture_gap(self, planned: bool) -> None:
        """The phase running now had a stretch with nothing listening."""
        phase = self.phase()
        if phase is None or phase.ended is not None:
            return
        if planned:
            phase.planned_gaps += 1
        else:
            phase.unplanned_gaps += 1

    def start_capture(self, reason: str, *, planned: bool = True,
                      checkpoint: bool = False) -> bool:
        self.note_capture_gap(planned)
        self.capture_index += 1
        self.record_clock.restart()
        self.attempt_text_packets = 0
        self.attempt_rss_first = None
        self.attempt_rss_last = None
        self.capture = Capture(self.options, self.capture_index, self.sink,
                               clock=self.clock,
                               max_log_bytes=max(0, self.transcript_budget),
                               mode=TOOL_MODE)
        self.capture.start()
        if checkpoint:
            self.checkpoint_attempts.add(self.capture_index)
        ready = self.wait_until_capturing()
        self.last_packet_at = self.elapsed
        phase = self.phase()
        if phase is not None and phase.ended is None:
            # The moment something was listening again, which is what says
            # whether the stretch an absent phase is judged on was captured.
            phase.listening_from = self.elapsed
        self.record({"record": "capture_started", "index": self.capture_index,
                     "pid": self.capture.attempt.pid, "reason": reason,
                     "planned": planned, "checkpoint": checkpoint,
                     "ready_s": None if ready is None else round(ready, 3),
                     "argv": list(self.capture.argv)})
        return ready is not None

    def wait_until_capturing(self) -> float | None:
        """Wait, briefly, until the tool has opened its capture file.

        The tool writes the file's header once it has the board, so the
        header arriving is the first moment anything is listening. Waiting
        for it keeps the cycle from starting against a capture that has not
        begun -- which would charge the tool's own start-up to the
        detection deadline -- and the wait is bounded, so a tool that never
        gets that far is left to the liveness rule instead of holding the run
        here.
        """
        deadline = self.clock() + min(self.options.liveness_timeout_s,
                                      CAPTURE_READY_TIMEOUT_S)
        started = self.clock()
        while self.clock() < deadline and not self.canceled:
            if self.capture is None or not self.capture.running():
                return None
            if self.capture.tail.error:
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
        # A canceled run waits for nothing: the tool was asked to stop in the
        # signal handler, this process has about two seconds left, and the
        # group SIGKILL that follows reaches the tool as well.
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
            # the run was doing at the time. Counted here, where every
            # attempt is closed, so no death is reported nowhere.
            self.unexplained_exits += 1
        if attempt.partial_tail_bytes and not attempt.forced and not kill:
            # A tool that was asked to stop flushes and closes its capture,
            # so a half-written record after a clean stop is a file that
            # cannot be read to its end. After a kill it is ordinary.
            why = (f"{attempt.capture} ends in {attempt.partial_tail_bytes} "
                   "bytes of an incomplete record")
            self._note_capture_error(why)
            say(f"capture {attempt.index}: {why}")
            self.record({"record": "capture_unreadable", "index": attempt.index,
                         "reason": why})
        self.record(attempt.to_dict())
        self.capture = None
        self.prune_captures()
        return attempt

    def _note_capture_error(self, reason: str) -> None:
        """Record a capture that could not be read, bounded like the rest."""
        self.capture_error_count += 1
        self._seen_capture_errors.add(reason)
        if len(self.capture_errors) < MAX_CAPTURE_ERRORS:
            self.capture_errors.append(reason)

    def prune_captures(self) -> None:
        removed, removed_bytes, stuck = prune_capture_files(
            self.attempts, self.options.out, self.options.capture_budget_bytes,
            self.record, stuck_already=bool(self.capture_budget_stuck))
        self.captures_removed += removed
        self.capture_bytes_removed += removed_bytes
        if stuck:
            self.capture_budget_stuck = stuck

    def rotate_if_large(self) -> None:
        """Start a new capture file once this one is big enough.

        Called at the start of an absent phase, where the fixture is off the
        air anyway, so the second or so the changeover costs falls in a
        silence the cycle already has instead of in a phase being judged.
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

    def restart(self, reason: str) -> None:
        """An unplanned restart: the capture died or brought nothing back."""
        attempt = self.stop_capture(reason, planned=False)
        if attempt is None:
            return
        self.restarts += 1
        if self.canceled:
            # Everything below is board work bounded in tens of seconds, and
            # the harness allows about two seconds after its SIGTERM.
            # Reaching the teardown is what matters now.
            return
        self.read_watchdog_now("after a restart", reason=reason)
        if self.reset_next_restart:
            # The second rung of SqueakyFloors/ubertooth#703's ladder: a
            # clean restart did not bring the capture back, so the board is
            # reset before the next try and the record is read again.
            result = run_util(self.options.serial, "-r", timeout=RESET_TIMEOUT_S)
            self.board_resets += 1
            self.record({"record": "board_reset", "returncode": result.returncode})
            self.reset_next_restart = False
            if self.canceled:
                return
            self.read_watchdog_now("after a board reset",
                                   tries=watchdog.RECOVERY_TRIES,
                                   reason="a board reset")
        if self.canceled:
            return
        if self.elapsed - self.last_restart_at < self.options.restart_min_interval_s:
            time.sleep(max(0.0, self.options.restart_min_interval_s
                           - (self.elapsed - self.last_restart_at)))
        self.last_restart_at = self.elapsed
        if self.canceled:
            # Starting a tool the teardown is about to stop would cost the
            # board a mode change for nothing.
            return
        self.start_capture(reason, planned=False)

    def needs_a_reset(self) -> bool:
        """Has a clean restart failed to bring this capture back?

        This attempt's own packets, not the run's: a run whose first capture
        worked would otherwise never reach the rung, however many later
        captures heard nothing.
        """
        if self.capture is None:
            return False
        heard = (self.capture.tail.records > 0 or self.attempt_text_packets > 0)
        return not heard and self.restarts > 0

    def check_liveness(self) -> None:
        """Restart when the process has gone, or when nothing is arriving."""
        if self.capture is None or self.canceled:
            # A cancellation reaches the tool as well as this process (they
            # are in one group), so a tool that has just exited on its way
            # out is not a death to recover from.
            return
        if not self.capture.running():
            self.capture.finish()
            attempt = self.capture.attempt
            say(f"capture {attempt.index} exited {attempt.returncode} by itself"
                + (f"; last error: {attempt.last_error}" if attempt.last_error else ""))
            # After `finish`, so the records written between the last poll
            # and the exit are counted: an attempt that did bring something
            # back has not earned a board reset.
            self.reset_next_restart = self.needs_a_reset()
            self.restart(f"the process exited {attempt.returncode}")
            return
        quiet = self.elapsed - self.last_packet_at
        if quiet >= self.options.liveness_timeout_s:
            if self.elapsed - self.last_restart_at < self.options.restart_min_interval_s:
                return
            self.reset_next_restart = self.needs_a_reset()
            say(f"capture {self.capture_index}: no packet for {quiet:.1f}s, restarting")
            self.restart(f"no packet for {quiet:.1f}s")

    # -- the watchdog record, and the intervals it closes -----------------

    def open_interval(self) -> Interval:
        # Its own counter, not the list's length: the list is pruned in a
        # long restarting run (MAX_HELD_INTERVALS), and an index that came
        # back would stop identifying the records already written.
        self.intervals_opened += 1
        interval = Interval(self.intervals_opened - 1, self.elapsed)
        interval.phases = len(self.phases)
        interval.cycles = self.cycles
        interval.advertising_packets = self.advertising_packets
        interval.connection_packets = self.connection_packets
        interval.records = self.records_seen
        interval.restarts = self.restarts
        self.intervals.append(interval)
        return interval

    def close_interval(self, reason: str, change, record, watchdog_reason: str) -> None:
        """Attribute what rose to the stretch it rose in.

        The counters the board keeps are since power-on, so what a read says
        on its own is a total; what a reader of a three-day run needs is
        which stretch of it a counter moved in. Each read closes the
        interval it ends and opens the next, with the rise this read saw
        recorded against the one that ended.
        """
        if not self.intervals:
            self.open_interval()
        interval = self.intervals[-1]
        interval.ended = self.elapsed
        interval.reason = reason
        interval.phases = len(self.phases) - interval.phases
        interval.cycles = self.cycles - interval.cycles
        interval.advertising_packets = (self.advertising_packets
                                        - interval.advertising_packets)
        interval.connection_packets = (self.connection_packets
                                       - interval.connection_packets)
        interval.records = self.records_seen - interval.records
        interval.restarts = self.restarts - interval.restarts
        interval.watchdog_read = record is not None
        interval.watchdog_reason = watchdog_reason
        interval.rss_kb = self.rss_last
        if change is not None:
            interval.new_resets = change.new_resets
            interval.new_transfer_errors = change.new_transfer_errors
            interval.new_radio_waits = dict(change.new_radio_waits)
            interval.new_fault = change.new_fault
            interval.counters_restarted = change.restarted
        self.record(interval.to_dict())
        if interval.counters_moved:
            say(f"interval {interval.index} ({interval.seconds:.1f}s, ended by "
                f"{reason}): {interval.new_resets} unrequested reset(s), "
                f"{interval.new_transfer_errors} transfer error(s)"
                + (f", radio waits {watchdog.wait_counts(interval.new_radio_waits)}"
                   if interval.new_radio_waits else ""))
        if len(self.intervals) > MAX_HELD_INTERVALS:
            # A board that died in the first hour of a three-day run reads
            # its record after every restart. Each one was recorded as it
            # closed, so what is held here is only what the summary reads:
            # the middle ones go, oldest and newest kept.
            self.intervals_forgotten += 1
            del self.intervals[MAX_HELD_INTERVALS // 2]
        self.open_interval()

    def read_watchdog_now(self, when: str, tries: int = 1,
                          reason: str = "") -> tuple[object, str]:
        """Read the record while no capture is running, and close an interval."""
        record, why = read_watchdog(self.options.serial, tries)
        entry = {"record": "watchdog", "when": when, "reason": why}
        change = None
        if record is not None:
            change = watchdog.compare(self.watchdog_baseline, record)
            if self.watchdog_baseline is None or change.restarted:
                # The board was power-cycled or reflashed under the run: its
                # counters start again, so it is re-baselined there.
                self.watchdog_baseline = record
            else:
                if change.reset_itself:
                    self.watchdog_resets += change.new_resets
                    if len(self.watchdog_where) < MAX_WATCHDOG_WHERE:
                        self.watchdog_where.append(record.last_reset_where)
                self.watchdog_transfer_errors += change.new_transfer_errors
                self.watchdog_baseline = record
            entry["watchdog"] = record.to_dict()
        else:
            # Two different things, and only one of them is a failure: a
            # board that will not answer is one the run cannot use again,
            # while firmware without the record is reported and judged by a
            # person (watchdog.py).
            if why == WATCHDOG_BOARD_SILENT:
                self.watchdog_unreadable += 1
            else:
                self.watchdog_not_reported += 1
            self.watchdog_last_reason = why
        self.watchdog_reads += 1
        self.record(entry)
        self.close_interval(reason or when, change, record, why)
        return record, why

    def checkpoint(self) -> Checkpoint:
        """A planned stop, read and restart: what #703 asks for by name.

        `ubertooth-util -w` is a control read on the board the capture holds,
        so it is never asked for beside a running capture. This is the
        planned way to get one: end the capture cleanly, wait for it to go,
        confirm the board answers, read the record, and start the next
        capture. None of it is a restart or a recovery, and the phase it
        happens in is marked with a planned gap, which is the same thing a
        capture file rollover leaves.
        """
        phase = self.phase()
        mark = Checkpoint(len(self.checkpoints), self.elapsed,
                          -1 if phase is None else phase.index)
        self.checkpoints.append(mark)
        if phase is not None:
            phase.checkpoints += 1
        say(f"checkpoint {mark.index} at {mark.at:.1f}s: stopping the capture to "
            "read the board's watchdog record")
        attempt = self.stop_capture(f"checkpoint {mark.index}")
        mark.stopped_cleanly = attempt is None or not attempt.forced
        if self.canceled:
            mark.canceled = True
            mark.seconds = self.elapsed - mark.at
            self.record(mark.to_dict())
            return mark
        # The board answering a read is what says the capture has let go of
        # it: a tool that is still there holds the device, and a read tried
        # once would call a board that is simply slow to re-enumerate dead.
        for attempt_number in range(IDLE_TRIES):
            if attempt_number:
                time.sleep(IDLE_PAUSE_S)
            answers, why = board_answers(self.options.serial)
            if answers:
                mark.board_idle = True
                mark.board_reason = ""
                break
            mark.board_reason = why
            if self.canceled:
                break
        if mark.board_idle:
            record, why = self.read_watchdog_now(f"checkpoint {mark.index}",
                                                 reason=f"checkpoint {mark.index}")
            mark.watchdog_read = record is not None
            mark.watchdog_reason = why
        else:
            # Not read: a board that will not answer a version read will not
            # answer two control transfers either, and the interval still has
            # to be closed so the next one starts here.
            mark.watchdog_reason = mark.board_reason
            self.close_interval(f"checkpoint {mark.index}", None, None,
                                mark.board_reason)
        if not self.canceled:
            mark.capture_restarted = self.start_capture(
                f"after checkpoint {mark.index}", checkpoint=True)
        # Either it never got that far, or the cancel arrived inside the
        # readiness wait and left it looking like a checkpoint that could not
        # restart the capture.
        if self.canceled:
            mark.canceled = True
        mark.seconds = self.elapsed - mark.at
        self.record(mark.to_dict())
        if mark.problem:
            say(f"checkpoint {mark.index}: {mark.problem}")
        return mark

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
        phase = self.phase()
        self.record({
            "record": "sample",
            "state": None if phase is None else phase.state,
            "cycle": self.cycles,
            "capture": self.capture_index,
            "running": self.capture is not None and self.capture.running(),
            "advertising_packets": self.advertising_packets,
            "connection_packets": self.connection_packets,
            "connect_inds": self.connect_inds,
            "records": self.records_seen,
            "since_last_packet_s": round(self.elapsed - self.last_packet_at, 3),
            "rss_kb": rss,
            "restarts": self.restarts,
            "checkpoints": len(self.checkpoints),
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
            # However the run ends, the tool does not outlive it holding the
            # board, and neither shell is left open.
            if self.capture is not None:
                try:
                    self.stop_capture("the run ended unexpectedly")
                except Exception:  # noqa: BLE001 - the teardown comes first
                    self.capture.signal_tool(signal.SIGKILL)
            self.peripheral.close()
            self.central.close()
            if self.progress is not None:
                self.progress.close()

    def _run(self) -> int:
        options = self.options
        say(f"follow soak: {options.duration_s:g}s, channel {options.channel}, "
            f"cycle {options.advertising_s:g}s advertising / "
            f"{options.connected_s:g}s connected / {options.absent_s:g}s absent, "
            f"checkpoint every {options.checkpoint_every_s:g}s")
        answers, reason = board_answers(options.serial)
        if not answers:
            # Never reset to make it work: something else is using it, and
            # taking it would spoil that run as well as this one.
            raise SoakError(f"the Ubertooth does not answer: {reason}")
        self.open_interval()
        self.read_watchdog_now("before the run", reason="the first read")
        self.peripheral.open()
        self.central.open()
        peripheral = self.peripheral.configure(options)
        central = self.central.configure(options)
        say(f"peripheral: {peripheral['fw']} at {options.peripheral_address}, "
            f"advertising channel {peripheral['conn_chmap']}")
        say(f"central: {central['fw']} at {options.central_address}, peer "
            f"{central['peer']}, interval {options.conn_interval_ms} ms, "
            f"{options.write_bytes} bytes every {options.write_period_ms} ms")

        self.start_capture("the run started")
        # The duration the plan asked for is the window the cycle runs over,
        # so it is measured from here: the board read, the watchdog read, the
        # fixture's settings and the capture's own start-up come before it
        # and are not part of it.
        self.window_start = self.elapsed
        phase = self.open_phase(ADVERTISING)
        next_sample = self.elapsed + options.sample_every_s
        next_checkpoint = self.window_elapsed + options.checkpoint_every_s
        next_switch = phase.started + options.advertising_s

        while self.window_elapsed < options.duration_s and not self.canceled:
            time.sleep(POLL_S)
            self.read_text()
            self.read_structural()
            now = self.elapsed
            if now >= next_switch:
                current = self.phase()
                self.close_phase(current)
                state = PHASES[(PHASES.index(current.state) + 1) % len(PHASES)]
                opened = self.open_phase(state)
                # From the new phase's own start, so a slow transition
                # shortens nothing: a phase runs for the length it was
                # given, measured from when the fixture was told.
                next_switch = opened.started + {
                    ADVERTISING: options.advertising_s,
                    CONNECTED: options.connected_s,
                    ABSENT: options.absent_s,
                }[state]
                if state == ABSENT:
                    # Both the file rollover and the checkpoint happen here,
                    # where the fixture is off the air anyway. The checkpoint
                    # first: it starts a new file of its own, so a rollover
                    # straight after it would stop a capture a second old.
                    if self.window_elapsed >= next_checkpoint:
                        self.checkpoint()
                        next_checkpoint = (self.window_elapsed
                                           + options.checkpoint_every_s)
                    else:
                        self.rotate_if_large()
            if now >= next_sample:
                self.sample()
                next_sample = now + options.sample_every_s
            self.check_liveness()
            if self.capture_budget_stuck:
                # Nothing it could do next stays inside the budget it was
                # given, so the window ends here and the verdict says why.
                break

        if self.phase() is not None and self.phase().ended is None:
            self.phase().ended_by_the_run = True
            self.close_phase(self.phase())
        final = self.stop_capture("the run was stopped" if self.canceled
                                  else "the run finished")
        # A cancellation leaves about two seconds before the harness kills
        # this process (executor.py), so the teardown after it is the part
        # that cannot be left out: the capture is stopped above and the
        # summary is written from what the run has. The board reads and the
        # closing watchdog read are skipped -- each can take ten seconds,
        # and the case's own cleanup reads and restores the board anyway.
        if self.canceled:
            return self.report(final)
        try:
            self.central.quiet()
            self.peripheral.quiet()
        except SoakError as error:
            # Counted apart from the cycle's own failures, and not a failure:
            # this pair of commands runs after the last phase, which the
            # duration ended and which nothing judged. A transition inside
            # the window is the one that means the cycle is not known.
            self.closing_error = str(error)
            self.record({"record": "closing_error", "when": "the run finished",
                         "reason": str(error)})
        self.read_watchdog_now("after the run", reason="the run finished")
        self.read_board_after()
        return self.report(final)

    def read_board_after(self) -> None:
        """The board answers, and what it is set to, once the capture is done."""
        reads = {}
        for key, option in (("firmware", "-v"), ("serial", "-s"), ("channel", "-C"),
                            ("squelch", "-z"), ("leds", "-d")):
            result = run_util(self.options.serial, option)
            reads[key] = {"returncode": result.returncode,
                          "answered": result.returncode == 0}
        self.board_after = reads
        self.record({"record": "board_after", "reads": reads})

    # -- the verdict ------------------------------------------------------

    def judgeable(self, state: str, length_s: float) -> list[Phase]:
        """Phases of one state the run really completed.

        Four kinds are left out, and the reason is the same each time: the
        phase says nothing about the board, so counting it as a miss would be
        reporting the harness's own doing as the board's. A phase the
        duration cut short was not known to be in that state throughout; one
        whose shell command did not reach a DK, or whose link the central
        never reported, was not known to be in it at all; and one with a
        capture gap had a stretch with nothing listening, whether that was a
        checkpoint, a file rollover, or a restart. All are recorded in
        progress.jsonl and counted in the summary, so a run that judged fewer
        phases than it ran says so.
        """
        return [phase for phase in self.phases
                if phase.state == state and phase.ended is not None
                and not phase.schedule_error and not phase.capture_gap
                and not phase.ended_by_the_run
                and phase.seconds >= length_s * PHASE_COMPLETE_SHARE]

    def phase_counts(self, state: str, length_s: float) -> dict:
        """How many phases of one state ran, and why each one left out was."""
        ran = [phase for phase in self.phases if phase.state == state]
        return {
            "ran": len(ran),
            # The one the run's own duration ended, which every run has.
            "ended_by_the_run": sum(1 for phase in ran if phase.ended_by_the_run),
            "cut_short": sum(1 for phase in ran if not phase.ended_by_the_run
                             and (phase.ended is None
                                  or phase.seconds < length_s * PHASE_COMPLETE_SHARE)),
            "schedule_error": sum(1 for phase in ran if phase.schedule_error),
            "capture_gap": sum(1 for phase in ran if phase.capture_gap),
            "checkpoints": sum(phase.checkpoints for phase in ran),
            # Credited against the coverage floor, so it is only the phases
            # whose one trouble was the run's own doing: a phase that was
            # also cut short, or whose transition failed, was not judgeable
            # for a reason the run did not choose. A planned gap always
            # takes a phase of these two states out of the judged
            # population, so every one of them is credited.
            "planned_capture_gap": sum(
                1 for phase in ran
                if phase.planned_gaps and not phase.unplanned_gaps
                and not phase.ended_by_the_run and not phase.schedule_error
                and phase.seconds >= length_s * PHASE_COMPLETE_SHARE),
            "judged": len(self.judgeable(state, length_s)),
        }

    def allowance(self, population: int, share: float | None = None) -> int:
        """How many of `population` comparisons may go the wrong way.

        A share of the population, because each figure rests on a time the
        host read something and one late flush should not end three days of
        work -- with a floor of one, so a short run has that much slack too,
        and never the whole population, so a comparison resting on one or two
        phases can still fail.
        """
        if population <= 0:
            return 0
        wanted = self.options.min_detection_share if share is None else share
        allowed = math.ceil((1.0 - wanted) * population)
        return min(max(1, allowed), population - 1)

    def detection(self) -> dict:
        """Per reader, how many advertising phases were found and sustained.

        ``reacquired`` is the same measurement over the phases that followed
        an absence, which in this cycle is every advertising phase but the
        first: the time to find a device that went away and came back.
        """
        options = self.options
        phases = self.judgeable(ADVERTISING, options.advertising_s)
        found = {}
        for name in ("structural", "text"):
            detected = sustained = 0
            first_times: list = []
            reacquired: list = []
            for phase in phases:
                side = getattr(phase, name)
                late = (side.first is None
                        or side.first - phase.started > options.detect_within_s)
                if not late:
                    detected += 1
                    first_times.append(side.first - phase.started)
                    if phase.after_absence:
                        reacquired.append(side.first - phase.started)
                if side.sustained(options.windows_needed):
                    sustained += 1
            found[name] = {
                "phases": len(phases),
                "detected": detected,
                "sustained": sustained,
                "detected_share": detected / len(phases) if phases else 0.0,
                "sustained_share": sustained / len(phases) if phases else 0.0,
                "first_detection_s": round(min(first_times), 3) if first_times else None,
                "median_detection_s": (round(statistics.median(first_times), 3)
                                       if first_times else None),
                "slowest_detection_s": (round(max(first_times), 3)
                                        if first_times else None),
                "reacquisitions": len(reacquired),
                "median_reacquisition_s": (round(statistics.median(reacquired), 3)
                                           if reacquired else None),
                "slowest_reacquisition_s": (round(max(reacquired), 3)
                                            if reacquired else None),
            }
        return found

    def gaps(self) -> dict:
        phases = self.judgeable(ADVERTISING, self.options.advertising_s)
        structural = [phase.structural.max_gap for phase in phases]
        over = sum(1 for phase in phases
                   if phase.structural.max_gap > self.options.max_gap_s)
        return {
            "phases": len(phases),
            "allowance": self.allowance(len(phases)),
            "max_s": round(max(structural), 3) if structural else None,
            "median_s": (round(statistics.median(structural), 3)
                         if structural else None),
            "phases_over_limit": over,
        }

    def follow(self) -> dict:
        """Per connected phase: was the link acquired, and how much arrived?"""
        options = self.options
        phases = self.judgeable(CONNECTED, options.connected_s)
        acquired: list = []
        in_time = enough = 0
        packets: list = []
        for phase in phases:
            if (phase.acquired_at is not None
                    and phase.acquired_at - phase.started <= options.acquire_within_s):
                in_time += 1
                acquired.append(phase.acquired_at - phase.started)
            held = 0 if phase.link is None else phase.link.packets
            packets.append(held)
            if held >= options.min_connection_packets:
                enough += 1
        return {
            "phases": len(phases),
            "acquired": in_time,
            "acquired_share": in_time / len(phases) if phases else 0.0,
            "with_enough_packets": enough,
            "packets_share": enough / len(phases) if phases else 0.0,
            "fastest_acquisition_s": round(min(acquired), 3) if acquired else None,
            "median_acquisition_s": (round(statistics.median(acquired), 3)
                                     if acquired else None),
            "slowest_acquisition_s": round(max(acquired), 3) if acquired else None,
            "median_packets": (round(statistics.median(packets), 1)
                               if packets else None),
            "max_packets": max(packets) if packets else 0,
        }

    def absent_judged_from(self, phase: Phase) -> float | None:
        """When an absent phase's judged stretch begins, or None if it has none.

        Judged on the time past its settle point, because the packets already
        on the air and already in the tool's buffers arrive just after the
        fixture is told to stop. A capture that came back inside the phase
        moves that start later: the stretch has to be one something was
        listening to.

        This is what keeps the check from being emptied. A checkpoint and a
        capture file rollover both happen at the start of an absent phase on
        purpose, so discarding every phase they touch would leave nothing
        judged, and a fixture that took `conn stop` and carried on would pass
        with half the cycle checked. An unplanned gap still disqualifies the
        phase, because then the capture's own health is in question.
        """
        if (phase.state != ABSENT or phase.ended is None
                or phase.schedule_error or phase.ended_by_the_run
                or phase.seconds < self.options.absent_s * PHASE_COMPLETE_SHARE):
            return None
        if phase.unplanned_gaps:
            return None
        start = phase.started + self.options.quiet_after_s
        if phase.planned_gaps:
            if phase.listening_from is None:
                return None
            start = max(start, phase.listening_from)
        # A stretch of nothing is not a quiet phase, it is an unmeasured
        # one, and nor is a stretch too short to have held an advertising
        # event (MIN_ABSENT_STRETCH_S).
        return start if phase.ended - start >= MIN_ABSENT_STRETCH_S else None

    def absent_phases(self) -> list[Phase]:
        return [phase for phase in self.phases
                if self.absent_judged_from(phase) is not None]

    def absent_counts(self) -> dict:
        ran = [phase for phase in self.phases if phase.state == ABSENT]
        return {
            "ran": len(ran),
            "ended_by_the_run": sum(1 for phase in ran if phase.ended_by_the_run),
            "cut_short": sum(
                1 for phase in ran if not phase.ended_by_the_run
                and (phase.ended is None
                     or phase.seconds < self.options.absent_s * PHASE_COMPLETE_SHARE)),
            "schedule_error": sum(1 for phase in ran if phase.schedule_error),
            "capture_gap": sum(1 for phase in ran if phase.capture_gap),
            "checkpoints": sum(phase.checkpoints for phase in ran),
            "planned_capture_gap": sum(
                1 for phase in ran
                if phase.planned_gaps and not phase.unplanned_gaps
                and not phase.ended_by_the_run and not phase.schedule_error
                and phase.seconds >= self.options.absent_s * PHASE_COMPLETE_SHARE),
            # A checkpoint or rollover that left no stretch to judge: the
            # phase is not in the judged population and the run says so.
            # This, and not `planned_capture_gap`, is what the coverage
            # floor credits for this state: an absent phase is judged from
            # the moment something was listening again, so one the run
            # interrupted and could still judge is in `judged` already, and
            # crediting it twice would loosen the floor by the number of
            # checkpoints.
            "nothing_left_to_judge": sum(
                1 for phase in ran
                if phase.planned_gaps and not phase.unplanned_gaps
                and self.absent_judged_from(phase) is None),
            "judged": len(self.absent_phases()),
        }

    def absence(self) -> dict:
        """Did the fixture really go away when it was told to?

        Two things have to stop, and they are different faults. The
        peripheral must stop advertising, which is the same check the address
        soak makes of its own advertiser; and the followed connection must
        end, which only a soak that follows one can ask. A board that kept
        reporting a connection the central had dropped would otherwise meet
        every acquisition objective while telling a service a device was
        still there.
        """
        phases = self.absent_phases()
        still_advertising: list = []
        still_connected: list = []
        for phase in phases:
            settled = self.absent_judged_from(phase)
            if any(side.last is not None and side.last > settled
                   for side in (phase.structural, phase.text)):
                still_advertising.append(phase.index)
            # The connection the cycle before this phase made, judged by its
            # own late packets: the link object is the connected phase's, so
            # a run that forgot the access address still has the count.
            previous = self.phases[phase.index - 1] if phase.index else None
            if (previous is not None and previous.state == CONNECTED
                    and previous.link is not None and previous.link.late):
                still_connected.append(phase.index)
        return {
            "phases": len(phases),
            "still_advertising": len(still_advertising),
            "still_connected": len(still_connected),
            "settle_s": self.options.quiet_after_s,
            "allowance": self.allowance(len(phases)),
        }

    def memory(self) -> dict:
        """What the capture process's memory did, per process and over the run.

        ``growth_kb`` is the largest growth inside one capture process, which
        is the figure a limit is about: a run that restarted has a new process
        each time, and the difference between two of them is not growth.
        """
        return {"first_kb": self.rss_first, "last_kb": self.rss_last,
                "max_kb": self.rss_max,
                "growth_kb": None if self.rss_first is None else self.rss_growth_max,
                "samples": self.samples}

    def checkpoint_problems(self) -> list[str]:
        """Checkpoints that could not do their job, bounded for the report."""
        problems = []
        for mark in self.checkpoints:
            if mark.problem:
                problems.append(f"checkpoint {mark.index}: {mark.problem}")
        for attempt in self.attempts:
            if attempt.index in self.checkpoint_attempts and attempt.mode_warning:
                # libubertooth warns when it finds the board still in a mode a
                # previous capture left it in (fork e657807). After a planned
                # clean stop there is nothing to find, so this says the stop
                # the checkpoint reported as clean did not leave the board idle.
                problems.append(
                    f"the capture started after a checkpoint (attempt "
                    f"{attempt.index}) found the board still in a mode")
        return problems

    def final_exit_problem(self) -> str:
        """Why the capture's last exit was not a clean stop, if it was not."""
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
        problems: list[str] = []
        problems += self.judge_coverage()
        problems += self.judge_detection()
        problems += self.judge_follow()
        problems += self.judge_absence()
        gaps = self.gaps()
        if gaps["phases_over_limit"] > gaps["allowance"]:
            problems.append(
                f"{gaps['phases_over_limit']} judged advertising phase(s) held a "
                f"gap with no packet from the peripheral longer than "
                f"{options.max_gap_s:g}s, more than the allowance of "
                f"{gaps['allowance']} (longest {gaps['max_s']}s)")
        if not self.advertising_packets:
            problems.append("the capture held no advertising packet of the "
                            "peripheral at all")
        if not self.connect_inds:
            problems.append("the capture held no CONNECT_IND between the two "
                            "registered DKs, so no connection was ever followed")
        elif not self.connection_packets:
            problems.append(
                f"the capture held {self.connect_inds} CONNECT_IND(s) of this "
                "fixture and not one packet on the connections they handed out, "
                "so no connection was followed past its own start")
        elif not self.valid_connection_packets:
            problems.append(
                f"none of the {self.connection_packets} packet(s) on the "
                "fixture's own connections decoded with a correct CRC, so the "
                "access address and CRC init the capture recorded do not decode "
                "the connection")
        if self.capture_error_count:
            problems.append(
                f"{self.capture_error_count} capture file(s) could not be read to "
                "the end: " + "; ".join(self.capture_errors[:3]))
        problems += self.checkpoint_problems()
        if not self.checkpoints and not self.canceled:
            # The plan asked for them, and the figures a checkpoint produces
            # are half of what this soak reports; a run that took none has
            # not done what it was asked. A canceled run is its own reason:
            # the cancellation is already among the problems, and a stop in
            # the first interval has taken none through no fault of the board.
            problems.append("the run took no watchdog checkpoint")
        if self.watchdog_unreadable:
            problems.append(
                f"{self.watchdog_unreadable} watchdog read(s) found a board that "
                f"would not answer: {self.watchdog_last_reason}")
        if self.restarts > options.max_restarts:
            problems.append(
                f"{self.restarts} unplanned restart(s), more than "
                f"{options.max_restarts}")
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
        transitions = self.allowance(len(self.phases))
        if self.schedule_errors > transitions:
            # Over the allowance, not over zero. Each failed transition
            # already takes its own phase out of the verdict, and the
            # coverage floors are what catch a fixture that is failing
            # systematically; a run of three days turns the fixture over
            # about eleven thousand times, and ending it for one DK that
            # hiccuped would fail the board for the bench's trouble. The
            # count is reported either way.
            problems.append(
                f"{self.schedule_errors} fixture transition(s) failed, more than "
                f"the allowance of {transitions}, so the cycle is not known for "
                "too many phases to judge the run")
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
        if self.sink.dropped:
            # The harness's own loss, not the board's: those are lines the
            # text reader never saw, so its figures are not the whole picture.
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

    def judge_coverage(self) -> list[str]:
        """A run has to be able to judge most of what it ran.

        Without this a capture that kept dying, or a fixture that kept
        refusing its transitions, would pass on the few phases it did cover.
        The phases the run meant to interrupt -- a checkpoint, and a capture
        file rolled over -- are allowed, because the run chose them; the rest
        count against this floor. Each state is counted on its own, because
        each one carries objectives the others do not.
        """
        options = self.options
        problems = []
        for state, counts, credited in (
                (ADVERTISING, self.phase_counts(ADVERTISING, options.advertising_s),
                 "planned_capture_gap"),
                (CONNECTED, self.phase_counts(CONNECTED, options.connected_s),
                 "planned_capture_gap"),
                # Not the same key: an absent phase the run interrupted and
                # could still judge is in `judged` already (absent_counts).
                (ABSENT, self.absent_counts(), "nothing_left_to_judge")):
            meant = counts["ran"] - counts["ended_by_the_run"]
            judged = counts["judged"]
            if not meant:
                continue
            if not judged:
                # Nothing credited here: whatever emptied the population,
                # including the run's own checkpoints, that part of the cycle
                # went unmeasured, and a soak that cannot say what happened
                # in it has not checked the cycle it reports.
                problems.append(
                    f"none of the {meant} {state} phase(s) meant to complete "
                    f"could be judged ({counts['cut_short']} cut short, "
                    f"{counts['schedule_error']} with a failed transition, "
                    f"{counts['capture_gap']} with a capture gap), so what "
                    f"happened while the fixture was {state} was never measured")
                continue
            usable = judged + counts[credited]
            if usable < options.min_detection_share * meant:
                problems.append(
                    f"only {judged} of {meant} {state} phases could be judged "
                    f"({counts['cut_short']} cut short, "
                    f"{counts['schedule_error']} with a failed transition, "
                    f"{counts['capture_gap']} with a capture gap, of which "
                    f"{counts[credited]} were the run's own doing), "
                    f"below {options.min_detection_share:.2f}")
        return problems

    def judge_detection(self) -> list[str]:
        """The record reader's detection objectives.

        The text reader is reported beside it and not judged: libbtbb prints
        no AdvA line for a data packet, so it can say nothing about the
        connection half of this soak, and whether its advertiser line agrees
        with the records is `BLE address parity soak`'s question, measured
        there over a capture that holds one state.
        """
        options = self.options
        problems = []
        found = self.detection()["structural"]
        if not found["phases"]:
            return problems
        if found["detected_share"] < options.min_detection_share:
            problems.append(
                f"the peripheral was found within {options.detect_within_s:g}s in "
                f"{found['detected']} of {found['phases']} advertising phases "
                f"({found['detected_share']:.2f}), below "
                f"{options.min_detection_share:.2f}")
        if found["sustained_share"] < options.min_detection_share:
            problems.append(
                f"sustained presence ({options.windows_needed} x "
                f"{options.window_s:g}s windows) was reached in "
                f"{found['sustained']} of {found['phases']} advertising phases "
                f"({found['sustained_share']:.2f}), below "
                f"{options.min_detection_share:.2f}")
        return problems

    def judge_follow(self) -> list[str]:
        options = self.options
        problems = []
        follow = self.follow()
        if not follow["phases"]:
            return problems
        if follow["acquired_share"] < options.min_follow_share:
            problems.append(
                f"the connection was acquired within {options.acquire_within_s:g}s "
                f"in {follow['acquired']} of {follow['phases']} connected phases "
                f"({follow['acquired_share']:.2f}), below "
                f"{options.min_follow_share:.2f}")
        if follow["packets_share"] < options.min_follow_share:
            problems.append(
                f"at least {options.min_connection_packets} packet(s) of the "
                f"connection arrived in {follow['with_enough_packets']} of "
                f"{follow['phases']} connected phases "
                f"({follow['packets_share']:.2f}), below "
                f"{options.min_follow_share:.2f} (most in one phase "
                f"{follow['max_packets']})")
        return problems

    def judge_absence(self) -> list[str]:
        absence = self.absence()
        problems = []
        if not absence["phases"]:
            return problems
        if absence["still_advertising"] > absence["allowance"]:
            problems.append(
                f"the peripheral was still heard more than "
                f"{absence['settle_s']:g}s into {absence['still_advertising']} of "
                f"{absence['phases']} absent phase(s), more than the allowance of "
                f"{absence['allowance']}, so it did not stop when it was told to")
        if absence["still_connected"] > absence["allowance"]:
            problems.append(
                f"packets of a connection the central had dropped arrived more "
                f"than {absence['settle_s']:g}s into "
                f"{absence['still_connected']} of {absence['phases']} absent "
                f"phase(s), more than the allowance of {absence['allowance']}, so "
                "the follow did not end when the connection did")
        return problems

    # -- the record of the run --------------------------------------------

    def summary(self) -> dict:
        options = self.options
        return {
            "duration_s": round(self.elapsed, 3),
            "window_s": round(self.window_elapsed, 3),
            "channel": options.channel,
            "fixture": {
                "peripheral": options.peripheral_address,
                "central": options.central_address,
                # The firmware's, not a plan's: the peripheral role has no
                # interval command (PERIPHERAL_ADV_INTERVAL_MAX_MS).
                "advertising_interval_ms": [PERIPHERAL_ADV_INTERVAL_MIN_MS,
                                            PERIPHERAL_ADV_INTERVAL_MAX_MS],
                "connection_interval_ms": options.conn_interval_ms,
                "write_bytes": options.write_bytes,
                "write_period_ms": options.write_period_ms,
                "transitions": (self.peripheral.transitions
                                + self.central.transitions),
                "retries": self.peripheral.retries + self.central.retries,
                "errors": self.schedule_errors,
                "closing_error": self.closing_error,
            },
            "cycle": {
                "advertising_s": options.advertising_s,
                "connected_s": options.connected_s,
                "absent_s": options.absent_s,
                "cycles": self.cycles,
                "phases": len(self.phases),
                "advertising": self.phase_counts(ADVERTISING, options.advertising_s),
                "connected": self.phase_counts(CONNECTED, options.connected_s),
                "absent": self.absent_counts(),
            },
            "detection": self.detection(),
            "gaps": self.gaps(),
            "follow": self.follow(),
            "absence": self.absence(),
            "advertising": {
                "structural_packets": self.advertising_packets,
                "text_packets": self.text_packets,
                "other_text_packets": self.other_packets,
                "other_addresses": len(self.other_addresses),
            },
            "connections": {
                "connect_inds": self.connect_inds,
                "packets": self.connection_packets,
                "valid_packets": self.valid_connection_packets,
                "long_packets": self.long_connection_packets,
                # Connections this fixture did not make. Counted, never
                # described: on a bench with other BLE traffic the tool can
                # follow someone else's link, which is why a judged connected
                # phase can miss its own follow deadline.
                "other_connect_inds": self.other_connect_inds,
                "packets_of_connections_not_announced": self.unannounced_packets,
                "other_access_addresses": len(self.other_link_addresses),
            },
            "checkpoints": {
                "taken": len(self.checkpoints),
                "every_s": options.checkpoint_every_s,
                "problems": self.checkpoint_problems(),
                "records": [mark.to_dict() for mark in self.checkpoints
                            [:MAX_INTERVALS_SUMMARIZED]],
            },
            "intervals": {
                "count": len(self.intervals),
                "forgotten": self.intervals_forgotten,
                "with_counter_increases": sum(
                    1 for item in self.intervals if item.counters_moved),
                # Every interval is in progress.jsonl. Here: the ones that
                # moved a counter, which is what a reader of a long run
                # looks for, and then the first few whatever they did.
                "counter_increases": [item.to_dict() for item in self.intervals
                                      if item.counters_moved
                                      ][:MAX_INTERVALS_SUMMARIZED],
                "records": [item.to_dict() for item in
                            self.intervals[:MAX_INTERVALS_SUMMARIZED]],
            },
            "watchdog": {
                "reads": self.watchdog_reads,
                "board_would_not_answer": self.watchdog_unreadable,
                "not_reported_by_this_firmware": self.watchdog_not_reported,
                "unrequested_resets": self.watchdog_resets,
                "transfer_errors": self.watchdog_transfer_errors,
                "where": self.watchdog_where,
            },
            "record_clock": self.record_clock.to_dict(),
            "memory": self.memory(),
            "process": {
                "attempts": len(self.attempts),
                "attempts_forgotten": self.attempts_forgotten,
                "restarts": self.restarts,
                "rotations": self.rotations,
                "unexplained_exits": self.unexplained_exits,
                "forced_stops": self.forced_stops,
                "board_resets": self.board_resets,
            },
            "captures": {
                "removed": self.captures_removed,
                "removed_bytes": self.capture_bytes_removed,
                "budget_bytes": options.capture_budget_bytes,
                "file_limit_bytes": options.capture_limit_bytes,
                "unreadable": list(self.capture_errors),
                "unreadable_count": self.capture_error_count,
                "budget_stuck_files": list(self.capture_budget_stuck),
            },
            "transcript": {"kept_bytes": self.transcript_bytes,
                           "budget_bytes": MAX_LOG_BYTES,
                           "dropped_lines": self.transcript_dropped_lines,
                           "unread_lines_dropped": self.sink.dropped},
            "progress": {"kept_bytes": self.progress_bytes,
                         "budget_bytes": MAX_PROGRESS_BYTES,
                         "records_not_written": self.progress_records_dropped},
            "between_phases": self.unattributed_packets,
            "board_after": self.board_after,
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
        options = self.options
        summary = self.summary()
        cycle = summary["cycle"]
        say(f"cycle: {cycle['cycles']} cycle(s), {cycle['phases']} phases")
        for state, counts in (("advertising", cycle["advertising"]),
                              ("connected", cycle["connected"]),
                              ("absent", cycle["absent"])):
            say(f"{state} phases: {counts['ran']} ran, {counts['judged']} judged "
                f"({counts['ended_by_the_run']} ended by the run, "
                f"{counts['cut_short']} cut short, {counts['schedule_error']} with "
                f"a failed transition, {counts['capture_gap']} with a capture gap, "
                f"{counts['checkpoints']} holding a checkpoint)")
        for name in ("structural", "text"):
            found = summary["detection"][name]
            say(f"{name} reader: {found['detected']}/{found['phases']} advertising "
                f"phases found within {options.detect_within_s:g}s, "
                f"{found['sustained']} sustained, median "
                f"{found['median_detection_s']}s, slowest "
                f"{found['slowest_detection_s']}s")
        found = summary["detection"]["structural"]
        say(f"reacquisition: {found['reacquisitions']} phase(s) after an absence, "
            f"median {found['median_reacquisition_s']}s, slowest "
            f"{found['slowest_reacquisition_s']}s")
        follow = summary["follow"]
        say(f"follow: {follow['acquired']}/{follow['phases']} connected phases "
            f"acquired within {options.acquire_within_s:g}s (median "
            f"{follow['median_acquisition_s']}s, slowest "
            f"{follow['slowest_acquisition_s']}s), "
            f"{follow['with_enough_packets']} with at least "
            f"{options.min_connection_packets} packet(s) (median "
            f"{follow['median_packets']}, most {follow['max_packets']})")
        connections = summary["connections"]
        say(f"connections: {connections['connect_inds']} CONNECT_IND(s) of this "
            f"fixture, {connections['packets']} packet(s) on them, "
            f"{connections['valid_packets']} with a correct CRC, "
            f"{connections['long_packets']} longer than "
            f"{conncapture.DEFAULT_DATA_LENGTH} bytes")
        say(f"other traffic: {connections['other_connect_inds']} CONNECT_IND(s) and "
            f"{connections['packets_of_connections_not_announced']} packet(s) of "
            f"{connections['other_access_addresses']} connection(s) this run did "
            f"not announce, plus {summary['advertising']['other_text_packets']} "
            f"advertising packet(s) from "
            f"{summary['advertising']['other_addresses']} other address(es); none "
            "recorded")
        absence = summary["absence"]
        say(f"absence: {absence['phases']} judged, {absence['still_advertising']} "
            f"still advertising and {absence['still_connected']} still carrying a "
            f"dropped connection more than {absence['settle_s']:g}s in (allowance "
            f"{absence['allowance']})")
        gaps = summary["gaps"]
        say(f"gaps: longest {gaps['max_s']}s, median {gaps['median_s']}s, limit "
            f"{options.max_gap_s:g}s, {gaps['phases_over_limit']} phase(s) over it "
            f"(allowance {gaps['allowance']})")
        checkpoints = summary["checkpoints"]
        say(f"checkpoints: {checkpoints['taken']} taken every "
            f"{options.checkpoint_every_s:g}s, {len(checkpoints['problems'])} that "
            "could not do their job")
        intervals = summary["intervals"]
        say(f"intervals: {intervals['count']} between watchdog reads, "
            f"{intervals['with_counter_increases']} in which a board counter rose")
        board = summary["watchdog"]
        say(f"watchdog: {board['unrequested_resets']} unrequested reset(s) and "
            f"{board['transfer_errors']} transfer error(s) over {board['reads']} "
            f"read(s), {board['board_would_not_answer']} read(s) the board would "
            f"not answer, {board['not_reported_by_this_firmware']} this firmware "
            "does not report"
            + ("; " + "; ".join(board["where"]) if board["where"] else ""))
        memory = summary["memory"]
        say(f"memory: first {memory['first_kb']} KiB, last {memory['last_kb']} KiB, "
            f"growth {memory['growth_kb']} KiB over {memory['samples']} sample(s)")
        process = summary["process"]
        say(f"process: {process['attempts']} attempt(s), {process['rotations']} "
            f"planned file rotation(s), {process['restarts']} restart(s), "
            f"{process['unexplained_exits']} unexplained exit(s), "
            f"{process['forced_stops']} forced stop(s), {process['board_resets']} "
            "board reset(s)")
        say(f"captures: {self.captures_removed} attempt(s) removed to stay inside "
            f"the run's budget ({self.capture_bytes_removed} bytes)")
        say(f"transcript: {self.transcript_bytes} of {MAX_LOG_BYTES} bytes kept, "
            f"{self.transcript_dropped_lines} line(s) past the budget read but "
            "not kept")
        say(f"progress: {self.progress_bytes} of {MAX_PROGRESS_BYTES} bytes kept, "
            f"{self.progress_records_dropped} record(s) past the budget not "
            "written (every figure is in summary.json)")
        say(f"record clock: {self.record_clock.reanchors} re-anchor(s), largest "
            f"drift {self.record_clock.drift_max_s:.3f}s (re-anchored past "
            f"{self.record_clock.reanchor_s:g}s)")
        if final is None:
            # A cancellation that arrived between a capture stopping and the
            # next one starting leaves nothing to report about, and the
            # report is the part that must not be lost.
            say("final stop: nothing was running when the run ended")
        else:
            say(f"final stop: {final.signalled or 'none'} exit {final.returncode}"
                + (f", last error: {final.last_error}" if final.last_error else ""))
        say(f"compatibility: {sum(a.error_lines for a in self.attempts)} error "
            f"line(s), {sum(a.legacy_death_lines for a in self.attempts)} line(s) "
            "matching the older capture-died wording")
        self.problems = self.judge()
        if verdict is None:
            verdict = "fail" if self.problems else "pass"
        # Written after the verdict and carrying it: the one machine-readable
        # artifact of a run of days has to say how it came out.
        summary["verdict"] = verdict
        summary["problems"] = list(self.problems)
        # Written beside and renamed into place: a canceled run has about two
        # seconds before the harness kills it, and a truncate-then-write
        # interrupted in the middle would leave an empty file where the one
        # machine-readable artifact of a run of days should be.
        final_path = options.out / "summary.json"
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
    parser = argparse.ArgumentParser(prog="followsoak.py",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--channel", required=True, type=int, choices=(37, 38, 39))
    parser.add_argument("--duration", required=True, type=float)
    parser.add_argument("--peripheral-jlink", required=True)
    parser.add_argument("--peripheral-address", required=True)
    parser.add_argument("--peripheral-port")
    parser.add_argument("--peripheral-alias", default="")
    parser.add_argument("--central-jlink", required=True)
    parser.add_argument("--central-address", required=True)
    parser.add_argument("--central-port")
    parser.add_argument("--central-alias", default="")
    parser.add_argument("--advertising", required=True, type=float)
    parser.add_argument("--connected", required=True, type=float)
    parser.add_argument("--absent", required=True, type=float)
    parser.add_argument("--checkpoint-every", required=True, type=float)
    parser.add_argument("--sample-every", required=True, type=float)
    parser.add_argument("--conn-interval", required=True, type=int)
    parser.add_argument("--write-bytes", required=True, type=int)
    parser.add_argument("--write-period", required=True, type=int)
    parser.add_argument("--presence-window", required=True, type=float)
    parser.add_argument("--presence-windows", required=True, type=int)
    parser.add_argument("--liveness-timeout", required=True, type=float)
    parser.add_argument("--detect-within", required=True, type=float)
    parser.add_argument("--connect-within", required=True, type=float)
    parser.add_argument("--acquire-within", required=True, type=float)
    parser.add_argument("--quiet-after", required=True, type=float)
    parser.add_argument("--max-gap", required=True, type=float)
    parser.add_argument("--min-detection-share", required=True, type=float)
    parser.add_argument("--min-follow-share", required=True, type=float)
    parser.add_argument("--min-connection-packets", required=True, type=int)
    parser.add_argument("--max-restarts", required=True, type=int)
    parser.add_argument("--max-memory-growth-kb", type=int)
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
                        ("--min-connection-packets", args.min_connection_packets),
                        ("--max-restarts", args.max_restarts),
                        ("--max-capture-bytes", args.max_capture_bytes),
                        ("--max-total-capture-bytes", args.max_total_capture_bytes),
                        ("--max-memory-growth-kb", args.max_memory_growth_kb),
                        ("--write-bytes", args.write_bytes)):
        if value is not None and value < 0:
            raise SoakError(f"{name} cannot be negative (got {value})")
    if args.presence_windows < 1:
        raise SoakError("--presence-windows must be at least 1")
    if args.write_period < 1:
        raise SoakError("--write-period must be at least 1 ms")
    if args.min_connection_packets < 1:
        # A floor of zero would pass a phase in which nothing of the
        # connection arrived, which is the thing this soak is measuring.
        raise SoakError("--min-connection-packets must be at least 1")
    for name, value in (("--min-detection-share", args.min_detection_share),
                        ("--min-follow-share", args.min_follow_share)):
        if not 0 < value <= 1:
            raise SoakError(f"{name} must be a fraction above 0 and at most 1 "
                            f"(got {value})")
    for name, value in (("--duration", args.duration),
                        ("--advertising", args.advertising),
                        ("--connected", args.connected),
                        ("--absent", args.absent),
                        ("--checkpoint-every", args.checkpoint_every),
                        ("--sample-every", args.sample_every),
                        ("--presence-window", args.presence_window),
                        ("--liveness-timeout", args.liveness_timeout),
                        ("--detect-within", args.detect_within),
                        ("--connect-within", args.connect_within),
                        ("--acquire-within", args.acquire_within),
                        ("--quiet-after", args.quiet_after),
                        ("--max-gap", args.max_gap),
                        ("--term-grace", args.term_grace),
                        ("--restart-min-interval", args.restart_min_interval)):
        if value < 0:
            raise SoakError(f"{name} cannot be negative (got {value})")
    if args.max_capture_bytes > args.max_total_capture_bytes > 0:
        raise SoakError(
            f"--max-capture-bytes ({args.max_capture_bytes}) is larger than "
            f"--max-total-capture-bytes ({args.max_total_capture_bytes}), which "
            "is what the whole run's captures may cost")


def options_from(args) -> Options:
    _check_values(args)
    return Options(
        out=Path(args.out),
        serial=args.serial.lower(),
        channel=args.channel,
        duration_s=args.duration,
        peripheral_jlink=args.peripheral_jlink,
        peripheral_address=nrfdk.normalize_address(args.peripheral_address),
        peripheral_port=args.peripheral_port,
        peripheral_alias=args.peripheral_alias,
        central_jlink=args.central_jlink,
        central_address=nrfdk.normalize_address(args.central_address),
        central_port=args.central_port,
        central_alias=args.central_alias,
        advertising_s=args.advertising,
        connected_s=args.connected,
        absent_s=args.absent,
        checkpoint_every_s=args.checkpoint_every,
        sample_every_s=args.sample_every,
        conn_interval_ms=args.conn_interval,
        write_bytes=args.write_bytes,
        write_period_ms=args.write_period,
        window_s=args.presence_window,
        windows_needed=args.presence_windows,
        liveness_timeout_s=args.liveness_timeout,
        detect_within_s=args.detect_within,
        connect_within_s=args.connect_within,
        acquire_within_s=args.acquire_within,
        quiet_after_s=args.quiet_after,
        max_gap_s=args.max_gap,
        min_detection_share=args.min_detection_share,
        min_follow_share=args.min_follow_share,
        min_connection_packets=args.min_connection_packets,
        max_restarts=args.max_restarts,
        max_memory_growth_kb=args.max_memory_growth_kb,
        capture_limit_bytes=args.max_capture_bytes,
        capture_budget_bytes=args.max_total_capture_bytes,
        term_grace_s=args.term_grace,
        restart_min_interval_s=args.restart_min_interval,
    )


def main(argv=None, peripheral=None, central=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        options = options_from(args)
        check_timings(options)
        if options.peripheral_address == options.central_address:
            raise SoakError(
                "the central and the peripheral are registered at the same BLE "
                f"address ({options.central_address}); this needs two DKs")
        if options.peripheral_jlink == options.central_jlink:
            raise SoakError(
                f"the central and the peripheral are both J-Link "
                f"{options.central_jlink}; this needs two DKs")
    except (nrfdk.NrfDkError, SoakError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    try:
        if peripheral is None:
            peripheral = nrfdk.resolve(options.peripheral_jlink,
                                       options.peripheral_port,
                                       alias=options.peripheral_alias)
        if central is None:
            central = nrfdk.resolve(options.central_jlink, options.central_port,
                                    alias=options.central_alias)
    except nrfdk.NrfDkError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    soak = Soak(options, peripheral, central)

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
        print(f"error: the fixture: {error}", file=sys.stderr)
        say("SOAK VERDICT: configuration")
        return EXIT_CONFIGURATION


if __name__ == "__main__":
    sys.exit(main())
