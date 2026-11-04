"""Drive an ESP32 Classic Bluetooth peer running the harness's peer firmware.

The firmware is in ``firmware/esp32-classic-peer`` (see its README for the
build and the shell protocol). A board is reached through its CP2102N USB to
UART bridge, named by the stable ``/dev/serial/by-id`` path of that bridge's
serial. Run as a script by the Classic cases::

    python esp32peer.py identify --device PATH [--address ADDR]
    python esp32peer.py status   --device PATH
    python esp32peer.py reset    --device PATH [--address ADDR]
    python esp32peer.py stream   --acceptor PATH --initiator PATH
                                 [--acceptor-address ADDR]
                                 [--initiator-address ADDR]
                                 [--period MS] [--payload BYTES]
                                 [--min-writes N] [--timeout S]
                                 [--map 20-HEX-DIGITS|off]
                                 [--map-report PATH] [--command-output PATH]
                                 [--interrupt-after S]
                                 -- COMMAND [ARGUMENT ...]
    python esp32peer.py audio    --acceptor PATH --initiator PATH
                                 [--acceptor-address ADDR]
                                 [--initiator-address ADDR]
                                 [--type ev3|2ev3|default]
                                 [--min-frames N] [--timeout S]
                                 -- COMMAND [ARGUMENT ...]

**Opening the bridge resets the board.** DTR and RTS are wired to the chip's
EN and IO0 pins on these dev boards, and the kernel asserts both when the
first process opens the port, whatever pyserial then asks for. So anything
the board was told in an earlier invocation is gone, and a two-board session
cannot be built out of several commands the way the DK's is.

``stream`` is the answer to that: it holds both ports open for one whole
session. It brings the Serial Port Profile link up, runs ``COMMAND`` while
the pair streams, stops the pair, and reports what the two boards did. The
command it runs is the thing under test (``ubertooth-rx ...``), so its
standard output is passed straight through; this module's own report goes on
standard error, as ``key: value`` lines for the cases' patterns.

``audio`` does the same with a synchronous (eSCO) link over the Hands-Free
Profile instead of the data stream, so the air carries reserved-slot voice
packets. With ``--type ev3`` the link's payload is on the air at the basic
rate, which is the one a receive-only capture can decode; the default the
stack would pick, 2-EV3, carries it as enhanced-rate.

``--map`` holds the piconet to part of the band: it is set on the initiator,
which leads the piconet, before the link comes up, so the whole session runs
on one map (firmware/esp32-classic-peer README, "The channel map").
``--map-report`` then writes the maps as JSON for afhmap.py: the map that was
asked for, and the map the link is really hopping over, read from the
leader's controller at the start, the middle, and the end of the command. A
link map that changes under the command is reported rather than averaged
over. ``--command-output`` saves the command's standard output, which is
passed through as always, so a later step can read it; ``--interrupt-after``
stops a command that runs until it is signalled, which is what
``ubertooth-afh`` does once it has a map.

The initiator pages, so it is the piconet central and its LAP is the one on
the air. ``central LAP`` and ``central UAP`` name it, which is what
``ubertooth-rx -l`` and ``-u`` take.

Every command fails closed: a port that cannot be opened, a board that does
not answer, an ``ERR`` reply, firmware of another version, an address that is
not the registered one, a link that did not come up or did not stay up, or a
pair that sent nothing prints ``error: <why>`` on stderr and exits 1. Exit
status 2 is a usage error. Otherwise ``stream`` and ``audio`` exit with the
status of the command they ran.

**The bridge is not reliable, and the board can go away.** Two things the
bench measured are handled here rather than left to the cases:

* a loaded host loses runs of bytes on the CP2102N at 115200, so a line can
  arrive complete-looking with a hole in the middle of it. An image from
  ``utesp-1.6.0`` on is asked to put a length and a CRC around every line
  it writes, which makes such a line known to be broken; a read, or a setter
  whose second application sets the same value, is then sent again rather
  than failing the run. A status line that is not readable is still refused,
  framed or not: a half-read status would be worse than none
  (`#310 <https://github.com/SqueakyFloors/ubertooth-test-harness/issues/310>`_).
* a peer can restart under the capture, and did, when a second link was
  paged to it. What the board says on its way down and on its way back is
  read and reported as a restart, with the reason the chip gave and the
  version it came back on, rather than leaving a board full of zeroed
  counters to be reported as a pair that never streamed
  (`#311 <https://github.com/SqueakyFloors/ubertooth-test-harness/issues/311>`_).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

if __package__ in (None, ""):
    # Run as a script: make the package importable for the shell-line format.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness import afhmap, targets  # noqa: E402

# The oldest firmware whose shell has everything this driver uses, and the
# stable path of its bridge: both from targets.py, which keeps every known
# target's identity in one place (tests/test_targets.py keeps the version in
# step with firmware/esp32-classic-peer/main/peer.h).
#
# A minimum rather than an exact version, because nothing in the harness
# flashes a peer: a board carrying a later 1.x image, which has these
# commands and more, must not fail a run. A different major version, or an
# older one, does.
# The oldest image whose shell has every command this driver sends. It is
# not targets.PEER_FIRMWARE, the version this tree builds: a firmware PR
# bumps that, and boards on an older 1.x image must stay usable.
MINIMUM_FIRMWARE = "utesp-1.0.0"
# The oldest image whose shell has `afh` and reports the live link's map, so
# only a session given --map asks for it; everything else still runs on
# MINIMUM_FIRMWARE.
AFH_MINIMUM_FIRMWARE = "utesp-1.3.0"
# The oldest image whose shell can check-sum the lines it sends, which is
# what makes a line that lost bytes on the bridge known to have lost them
# rather than taken at face value (#310). A board on an older 1.x image is
# driven exactly as before: the framing is asked for where it exists and
# nothing depends on it.
FRAME_MINIMUM_FIRMWARE = "utesp-1.6.0"
BAUD = 115200
ROLES = ("acceptor", "initiator")
BOOT_ROLE = "acceptor"
# What the firmware's status reports after a reset, from its README: the
# acceptor role with its server listening and discoverable, no link, nothing
# streaming, the default stream settings, and every counter zero. The
# address, LAP, UAP, name, and `bt` are the board's own and are checked
# separately, by check_status.
BOOT_STATE = {
    "role": BOOT_ROLE, "discoverable": True, "spp_server": True,
    "connecting": False, "connected": False, "peer": "", "remote": "",
    "stream": False, "period_ms": 100, "payload_len": 100, "counter": 0,
    "writes": 0, "write_errors": 0, "rx_bytes": 0, "connects": 0,
    "disconnects": 0, "last_err": 0,
}

# Payload bounds the firmware enforces (its README, "Shell protocol").
PERIOD_MIN_MS, PERIOD_MAX_MS = 5, 10000
PAYLOAD_MIN, PAYLOAD_MAX = 8, 512
DEFAULT_PERIOD_MS = 20
DEFAULT_PAYLOAD = 200

# The synchronous (eSCO) audio link's packet type a caller may ask for.
# `ev3` carries its payload on the air at the basic rate, which is what a
# receive-only basic-rate decoder can read; `2ev3`, what the stack asks for
# by default, carries the same payload as enhanced-rate. `default` leaves
# the choice alone.
AUDIO_TYPES = ("ev3", "2ev3", "default")
DEFAULT_AUDIO_TYPE = "ev3"
# The two subcommands that hold the pair's link up while another command
# runs, as opposed to the ones that read or reset one board.
PAIR_COMMANDS = ("stream", "audio")
# The oldest image whose shell has everything an audio session reads. The
# link itself arrived in utesp-1.1.0 and `sco type` in utesp-1.4.0;
# utesp-1.5.0 added the two counters that say how many links a profile has
# had, which is how a session tells one link that lasted from a second that
# replaced it (#264).
AUDIO_FIRMWARE = "utesp-1.5.0"
# What `status` must report for the link to be up and carrying audio.
AUDIO_ON = "on"
# The one value of AUDIO_TYPES that names no packet type, so it promises
# nothing about what is on the air. Spelled once rather than at each place
# that compares against it.
AUDIO_TYPE_STACK = "default"
# What `sco_pkt_type` reads for each type a caller can ask for by name,
# which is the specification's spelling rather than the command line's
# (firmware/esp32-classic-peer/README.md, `sco_pkt_type`). `default` is
# left out: the choice is the stack's, so no particular type is promised.
AUDIO_TYPE_ON_LINK = {"ev3": "ev3", "2ev3": "2-ev3"}
# The session's own shell commands. One source for them, so this module,
# its tests, and the cases that read its report cannot come to disagree
# with the firmware about what they are called.
SCO_START, SCO_STOP, SCO_STATS, SCO_TYPE = (
    "sco start", "sco stop", "sco stats", "sco type")
ROLE_INITIATOR = "role initiator"
# The audio counters a session reads before the command and again after it.
# What it reports is how far each one moved: a total would include the
# bring-up, and on a board an earlier session left running it would include
# that session too (#264).
AUDIO_FRAME_COUNTERS = ("sco_tx_frames", "sco_tx_errors", "sco_rx_frames",
                        "sco_rx_bad")
# How many synchronous links the profile has had, and how many went down
# again. One link that lasted the whole command leaves both unchanged; a
# link that dropped and came back, or was renegotiated, moves one of them,
# which is the difference between a capture of one link and a capture of
# two with a gap in the middle.
AUDIO_LINK_COUNTERS = ("sco_connects", "sco_disconnects")
# The Serial Port Profile's own link counter, read the same way. An audio
# session brings no SPP link up, so it should not move either.
SPP_LINK_COUNTERS = ("disconnects",)
# The controller's own synchronous-packet counters (`sco stats`): the
# packets that went on the air, as against the frames the application made
# of them. The firmware's README records a measured reading of all seven.
AUDIO_PKT_COUNTERS = ("rx_total", "rx_correct", "rx_err", "rx_none",
                      "rx_lost", "tx_total", "tx_discarded")
# The audio half of a board's boot state. BOOT_STATE is checked on every
# 1.x image, and the oldest of those have none of these fields, so an audio
# session checks both and the rest of the module only the first.
AUDIO_BOOT_STATE = {
    "sco": "off", "sco_profile": "none", "sco_codec": "none",
    "sco_handle": 0, "sco_frame_len": 0, "sco_type": AUDIO_TYPE_STACK,
    "sco_link": "none", "sco_pkt_type": "none", "sco_tx_frames": 0,
    "sco_tx_errors": 0, "sco_rx_frames": 0, "sco_rx_bad": 0,
    "sco_connects": 0, "sco_disconnects": 0,
}
AUDIO_RELEASE_STATE = {**BOOT_STATE, **AUDIO_BOOT_STATE}
# `sco start` on the initiator blocks the shell while the controller starts
# the hands-free profile (3 s), pages and brings the service level connection
# up (15 s), opens the audio link (10 s), and, if that failed, puts the
# profile back (3.5 s). The firmware's README asks for 40 seconds, so that a
# bring-up that failed answers with the firmware's own reason rather than
# timing out here, and a slow one is not cut off mid-command. A link to a
# board that is awake takes four or five seconds.
AUDIO_CONNECT_TIMEOUT_S = 40.0
# `role` takes an audio link and its profile down before it changes anything,
# which the firmware's README allows about 18 seconds for, so it cannot run
# on the ordinary command deadline once a session may have a link up.
ROLE_TIMEOUT_S = 25.0
# What the link has to be, and what a board reports for a link whose packet
# type it could not work out. Both EV3 and 2-EV3 are eSCO types.
AUDIO_ESCO = "esco"
AUDIO_UNNAMED = "unknown"

# `sco stop` closes the audio link (3 s), then the service level connection
# (3 s), waits for a frame already with the stack (0.5 s), and stops the
# profile (3 s), so the firmware's own worst case is 9.5 s. As with
# AUDIO_CONNECT_TIMEOUT_S, this has to outlast that, or a slow but healthy
# stop reads as a board that refused one.
AUDIO_STOP_TIMEOUT_S = 15.0

# The channel map: `off` puts every channel back, and the smallest map a
# piconet may hop over is the spec's twenty channels, which the firmware
# refuses to go below. Refusing the same maps here keeps a session that
# cannot work from touching the boards at all.
AFH_OFF = "off"
AFH_MIN_CHANNELS = 20
# Where the link's map is read, as the report and afhmap.py name the three.
SAMPLE_POINTS = ("start", "middle", "end")

COMMAND_TIMEOUT_S = 5.0
# Opening the port resets the board; it then has to boot and start Bluetooth
# before the shell answers. About 1.5 s on the bench, with room to spare.
BOOT_TIMEOUT_S = 20.0
# `spp start` on the initiator pages the acceptor, looks its channel up over
# SDP, and answers from the controller's event. The firmware gives up after
# 15 s, so this has to outlast that to read its ERR rather than time out.
CONNECT_TIMEOUT_S = 25.0
# `spp stop` waits for the link to close and the server to stop.
STOP_TIMEOUT_S = 10.0
# Seconds the reset pulse holds EN low, as esptool's hard reset does.
RESET_PULSE_S = 0.1
POLL_S = 0.05
# Time for the first writes to reach the air before the command starts.
SETTLE_S = 1.0
# Time an overrunning command gets to stop itself after SIGINT.
INTERRUPT_TIMEOUT_S = 10.0
# How often a session reads its boards' bridges while the command under test
# runs. Nothing is asking them anything then, so a board that reboots says
# so to a port nobody is reading, and the operating system's buffer is all
# that holds the evidence until the next command. Two seconds keeps it well
# inside that buffer and costs one non-blocking read per board (#311).
DRAIN_S = 2.0
# How many times a command whose answer did not arrive, or did not survive
# the bridge, is sent again. Only the commands in REPEATABLE are, and the
# bench's loss rate is a few lines in several thousand, so one spare attempt
# would do; two makes a run that hits the rare second loss slower rather
# than failed (#310).
COMMAND_RETRIES = 2
# A moment between a lost answer and asking again, so a board part way
# through writing a line is not interrupted by the next command.
RETRY_PAUSE_S = 0.2
# A bound on one drain, so a board stuck in a reboot loop cannot hold the
# session reading it. 256 bytes a read, so this is 64 KB, which is far more
# than the operating system's own buffer holds.
DRAIN_READS_MAX = 256

_ADDRESS = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
# What the board says by itself: its own notices, ESP-IDF's warnings and
# errors, and the ROM's boot log, which appears after every reset.
_NOTICE = re.compile(r"^(?:utesp: |[WE] \(\d+\))")
# ESP-IDF's own log lines. They do not come from the firmware's line writer,
# so they carry no frame even on an image that frames everything else.
_IDF_LOG = re.compile(r"^[WE] \(\d+\)")
_BOOT_NOISE = re.compile(
    r"^(?:ets |rst:0x|configsip:|clk_drv:|mode:|load:|entry |"
    r"[\x00-\x08\x0b-\x1f]|$)")
_READY = re.compile(r"^utesp: (?P<version>utesp-\S+) ready$")
_VERSION = re.compile(r"^utesp-(\d+)\.(\d+)\.(\d+)$")
_START_FAILED = re.compile(r"^utesp: (?P<version>\S+) start failed: "
                           r"(?P<reason>.+)$")
# The ROM's own first line after a restart, which names the reason the chip
# restarted. The ROM writes it whatever the firmware does, so it arrives even
# from a board that crashed before its own code ran (#311).
_ROM_RESET = re.compile(r"^rst:0x[0-9a-f]+ \(.*")
# The firmware's reading of the same thing, which names the reason rather
# than numbering it (utesp-1.6.0 and later).
_BOOT_REASON = re.compile(r"^utesp: boot reason (?P<reason>.+)$")
# What a crash prints on its way down, before the ROM line. Any of these is
# a restart in progress and says more about why than the ROM's number does.
_CRASH = re.compile(
    r"Guru Meditation Error|Backtrace:|abort\(\) was called|assert failed|"
    r"Brownout detector was triggered|Stack canary watchpoint|"
    r"Interrupt wdt timeout|Task watchdog got triggered|CORRUPT HEAP")
# The frame an image from FRAME_MINIMUM_FIRMWARE on puts around every line
# its own writer sends: the payload's length in bytes and its CRC-16, so a
# line that lost a run of bytes fails one or both (#310).
_FRAMED = re.compile(r"^#(?P<len>\d{1,5}),(?P<crc>[0-9a-f]{4}):(?P<payload>.*)$",
                     re.DOTALL)
# The command that turns that framing on, and what the shell calls it.
FRAME_ON = "frame on"

# Commands that may be sent again when their answer did not arrive or did
# not survive the bridge. Each is either a read, which reports state and
# changes none, or a setter whose second application sets the same value, so
# a repeat cannot move a board or a link on. Everything else - `spp start`,
# `spp stop`, `role`, `sco start`, `sco stop`, `defaults`, `reset` - does
# move one on, and a lost answer there is reported rather than guessed at.
#
# An entry ending in a space is the start of a command and its operands;
# every other entry is the whole command line. Nothing is matched loosely,
# so a command this list has not named cannot be repeated by accident.
REPEATABLE = ("status", "version", "help", "clock", "sco stats", FRAME_ON,
              "frame off", "stream on", "stream off", "spp peer ",
              "stream period ", "stream payload ", "disc ", "sco type ",
              "afh ")

# The status fields this driver reads, reports, or checks after a reset,
# with the type each must have. A later image of the same major may add
# fields, and does: nothing here minds. `nap` is left out because nothing
# reads it.
_STATUS_KEYS = {
    "role": str, "fw": str, "bt": bool, "address": str, "lap": str,
    "uap": str, "name": str, "discoverable": bool,
    "spp_server": bool, "connecting": bool, "connected": bool, "peer": str,
    "remote": str, "stream": bool, "period_ms": int, "payload_len": int,
    "counter": int, "writes": int, "write_errors": int, "rx_bytes": int,
    "connects": int, "disconnects": int, "last_err": int,
}


def _unframable(line: str) -> bool:
    """Whether a line may arrive without a frame from a framing board.

    Two kinds may. The first never went through the firmware's own line
    writer, so it was never framed at all: the ROM's boot log, ESP-IDF's log
    lines, and what a panic prints on its way down.

    The second is the board's own boot notices, and they are here because a
    board that has restarted is not framing any more - the setting is the
    wire's and a reset clears it. So a bare boot notice is not a line that
    lost its frame; it is the board saying it went away, which is the one
    thing the session most needs to hear (#311). Without this, a restart
    whose ROM line was damaged or lost would take every notice after it down
    with it and leave nothing to report.

    A damaged line that happened to read as a boot notice would be taken for
    one. That costs a reported restart, which is a loud failure rather than a
    quiet pass, and it needs a hole to fall exactly inside a notice.
    """
    return bool(_BOOT_NOISE.match(line) or _IDF_LOG.match(line)
                or _CRASH.search(line) or _READY.match(line)
                or _START_FAILED.match(line) or _BOOT_REASON.match(line))


class PeerError(RuntimeError):
    """The peer, its bridge, or the pair did not do what was asked."""


class SerialLoss(PeerError):
    """What the board sent did not reach this process whole.

    A loaded host loses runs of bytes on a CP2102N bridge at 115200, which
    is how a complete-looking line arrives with a hole in the middle of it
    (#310). This is the one failure a command may be sent again for, so it
    is a kind of its own; it is still a PeerError, because to everything
    that only reports a failure it is one.
    """


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE of ``data``, the check the firmware's frames carry.

    The same bit-by-bit polynomial the firmware computes, which is short
    enough to write twice and avoids a dependency on either side. The CRC
    catches a line whose bytes were replaced; the length beside it in the
    frame catches the far commoner case, a run of them simply gone.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass
class Restart:
    """One restart of a board that nothing asked for (#311).

    The driver resets a board on purpose twice in a session, when it opens
    the bridge and when it releases the board, and those are not this. This
    is a board that went away under a command or under the capture: the
    reason the chip gives, the version it came back on, and the lines it
    said on the way, so a run says what happened instead of reporting the
    zeroed counters of a board that is back at boot.
    """

    at: str
    reason: str = ""
    version: str = ""
    # Why Bluetooth did not come up on this boot, if it did not. The board's
    # shell answers either way, so this is the boot's own news rather than a
    # failure to boot.
    failed: str = ""
    lines: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"at {self.at}"]
        parts.append(self.reason if self.reason else "no reason line arrived")
        if not self.version:
            parts.append("and had not finished booting")
        elif self.failed:
            parts.append(f"back on {self.version} with Bluetooth not started: "
                         f"{self.failed}")
        else:
            parts.append(f"back on {self.version}")
        return ", ".join(parts)


def version_parts(version: str, label: str = "peer") -> tuple[int, int, int]:
    match = _VERSION.fullmatch(str(version).strip())
    if match is None:
        raise PeerError(f"{label}: '{version}' is not a firmware version of the "
                        "form utesp-<major>.<minor>.<patch>")
    return tuple(int(part) for part in match.groups())


def check_firmware(version: str, label: str = "peer",
                   minimum: str = MINIMUM_FIRMWARE) -> None:
    """The board's firmware must be ``minimum`` or a later image of its major.

    A later 1.x image has these commands and more, so it is accepted; the
    exact version is reported either way, and goes in the run record.
    """
    found = version_parts(version, label)
    wanted = version_parts(minimum)
    if found[0] != wanted[0] or found < wanted:
        raise PeerError(
            f"{label} runs {version}, and the harness needs {minimum} or a "
            f"later {wanted[0]}.x image; build and flash "
            "firmware/esp32-classic-peer at this harness commit")


def normalize_address(value: str) -> str:
    """A BD_ADDR as the firmware prints it: upper case, colon separated."""
    text = str(value).strip().upper()
    if not _ADDRESS.fullmatch(text):
        raise PeerError(f"BD_ADDR must look like C8:85:41:2A:1B:2A, got {value!r}")
    return text


def address_parts(address: str) -> tuple[str, str]:
    """The LAP and UAP of ``address``, as ubertooth-rx -l and -u take them.

    The LAP is the lowest three bytes and the UAP the one above them
    (ubertooth-rx(1), DISCOVERING UNDISCOVERABLE DEVICES), both lower case
    hex with no separators. Unlike targets.lap_uap, which it uses, this
    refuses an address it cannot read rather than returning empty strings:
    a case that cannot name the piconet must not run.
    """
    return targets.lap_uap(normalize_address(address))


def device_name(address: str) -> str:
    """The name the firmware gives a board with this address.

    `main/bt_peer.c` builds it as `UT-ESP32-` and the LAP in upper case, and
    the shell cannot set it, so it follows from the
    address and is unique per board. It is the name the board answers an
    inquiry and an LMP name request with, which is how a capture says which
    board replied. A registry may write it down beside the address for a
    person reading it; `reset` reports the board's own, so a run checks the
    two agree rather than trusting either.
    """
    lap, _uap = address_parts(address)
    return f"UT-ESP32-{lap.upper()}"


def default_device(bridge_serial: str) -> str:
    """The bridge's stable by-id path, from the CP2102N's serial.

    /dev/ttyUSB<n> is assignment order, and the two boards swap between
    boots, so the by-id path is the only safe way to name one. The path is
    Linux's, which is where the peers are: the cases that use them declare
    Linux, and a registry may name another path with `serial device`.
    """
    serial = str(bridge_serial).strip()
    if not serial:
        raise PeerError("a bridge serial is needed to find the board's port")
    return targets.PEER_BY_ID.format(serial=serial)


def period_ms(value) -> int:
    number = int(value)
    if not PERIOD_MIN_MS <= number <= PERIOD_MAX_MS:
        raise PeerError(f"stream period must be {PERIOD_MIN_MS}-{PERIOD_MAX_MS} ms")
    return number


def payload_bytes(value) -> int:
    number = int(value)
    if not PAYLOAD_MIN <= number <= PAYLOAD_MAX:
        raise PeerError(f"stream payload must be {PAYLOAD_MIN}-{PAYLOAD_MAX} bytes")
    return number


def channel_map(value) -> str:
    """``off``, or a channel map in the twenty hex digits the shell takes.

    The firmware refuses a map of fewer than twenty channels and one that
    sets the reserved 80th bit, and so does this: a map no piconet may use
    tells a test nothing, and refusing it here costs no board time.
    """
    text = str(value).strip().lower()
    if text == AFH_OFF:
        return AFH_OFF
    try:
        channels = afhmap.channels_from_hex(text, "the channel map")
    except afhmap.AfhMapError as error:
        raise PeerError(str(error)) from error
    if len(channels) < AFH_MIN_CHANNELS:
        raise PeerError(f"a piconet needs at least {AFH_MIN_CHANNELS} channels, "
                        f"and {text} keeps {len(channels)}")
    return text


def requested_map(wanted: str) -> str:
    """The map ``wanted`` asks the controller for, as its status reports it.

    ``off`` is every channel, which is what the board reports after it, so a
    report and a comparison can name one map either way.
    """
    if wanted == AFH_OFF:
        return afhmap.hex_from_channels(range(afhmap.CHANNELS))
    return wanted


# --------------------------------------------------------------------------
# Shell


class Shell:
    """Line protocol: one command line out; data lines, then OK or ERR <why>.

    The board also sends notices of its own at any time (``utesp: ...``), and
    the ROM prints a boot log after every reset. Both are skipped here, so a
    caller sees only the answer to what it asked.

    Two things about the bridge between here and the board are this class's
    business, because nothing above it can see them:

    **Bytes go missing.** On a host that is also capturing over USB, a run
    of bytes is lost on the CP2102N now and then, and a line that lost some
    from the middle still arrives with its newline (#310). Holding a partial
    line until the newline, which :meth:`_lines` does, cannot catch that.
    So an image that can is asked to check-sum what it sends
    (:meth:`ask_for_framing`), a line that fails the check is a
    :class:`SerialLoss` rather than data, and a command that may be sent
    again is.

    **The board can go away.** It talks when nothing is asking it anything,
    and it reboots without being told to. What it says then used to be
    dropped, so a peer that restarted under a capture read as a pair that
    never streamed (#311). :meth:`drain` keeps that evidence, and
    :attr:`restarts` is what a session reports it from.
    """

    def __init__(self, port, label: str = "peer", clock=time.monotonic,
                 log=None):
        self.port = port
        self.label = label
        self.clock = clock
        # With a logger, every line to and from this shell is written for the
        # run's transcript (targets.shell_line); None keeps it quiet.
        self.log = log
        self.notices: list[str] = []
        self._buffer = b""
        # The version of the last boot notice this shell read, which is the
        # image now running on the board.
        self.version = ""
        # Restarts nothing asked for, oldest first, and the boot now being
        # read, if one is. last_boot is the most recent complete boot of
        # either kind, which is how wait_ready reads the notice that ended
        # the boot it was waiting for.
        self.restarts: list[Restart] = []
        self.last_boot: Restart | None = None
        self._booting: Restart | None = None
        # True while a boot is this driver's own doing: opening the bridge
        # resets the board, and so does `reset`. Everything else is #311.
        self._expect_boot = True
        # Lines that did not survive the bridge, and commands sent again
        # because of one. A session reports both, so a run that was saved by
        # a retry is visible rather than silently slower (#310).
        self.lost = 0
        self.retried = 0
        # Whether the board is check-summing its lines. It is not after a
        # restart, whatever it was before, because the board that came back
        # has not been asked.
        self.framed = False

    @classmethod
    def open(cls, path: str, label: str = "peer", log=None) -> "Shell":
        try:
            import serial  # pyserial, imported lazily
        except ImportError as error:
            raise PeerError("pyserial is not installed in this interpreter") from error
        port = serial.Serial()
        port.port = path
        port.baudrate = BAUD
        port.timeout = POLL_S
        port.write_timeout = 1.0
        port.exclusive = True
        # IO0 must stay high so the chip boots its application rather than
        # the serial bootloader. The open resets the board either way.
        port.dtr = False
        port.rts = False
        try:
            port.open()
        except Exception as error:  # pyserial raises SerialException or OSError
            raise PeerError(f"cannot open {path}: {error}") from error
        return cls(port, label, log=log)

    def close(self) -> None:
        self.port.close()

    def _lines(self, deadline: float):
        while True:
            while b"\n" in self._buffer:
                raw, self._buffer = self._buffer.split(b"\n", 1)
                yield self._decode(raw)
            if self.clock() >= deadline:
                return
            chunk = self.port.read(256)
            if chunk:
                self._buffer += chunk

    def _note(self, line: str) -> None:
        self.notices.append(line)

    # -- what the board says by itself -------------------------------------

    def _unframe(self, line: str) -> tuple[str, bool]:
        """The payload of ``line``, and whether it reached us whole.

        A framed line carries its own length and CRC, so a run of bytes lost
        anywhere in it fails the check. An unframed line is taken as it
        stands, except on a board that has been asked to frame everything it
        writes: there anything bare that :func:`_unframable` does not account
        for has lost its frame.
        """
        found = _FRAMED.match(line)
        if found is None:
            if self.framed and not _unframable(line):
                return line, False
            return line, True
        payload = found["payload"]
        if (len(payload.encode("utf-8", "replace")) != int(found["len"])
                or crc16(payload.encode("utf-8", "replace")) != int(found["crc"], 16)):
            return payload, False
        return payload, True

    def _boot_evidence(self, line: str) -> bool:
        """Record ``line`` if it belongs to a boot; say whether it did.

        A boot is the ROM's log, anything a crash printed before it, and the
        firmware's own notice at the end of it. They are gathered into one
        :class:`Restart` so a session can report the reason and the version
        together rather than a scatter of lines.
        """
        crash = _CRASH.search(line)
        rom = _ROM_RESET.match(line)
        reason = _BOOT_REASON.match(line)
        ready = _READY.match(line) or _START_FAILED.match(line)
        noise = _BOOT_NOISE.match(line)
        if not (crash or rom or reason or ready or noise):
            return False
        if noise and not rom and self._booting is None:
            # The ROM's quieter lines on their own say nothing: a stray
            # control character or a blank line is not a boot.
            return True
        if self._booting is None:
            self._booting = Restart(at=time.strftime("%H:%M:%SZ", time.gmtime()))
            # Whatever it was framing, the board that came back has not been
            # asked to frame anything.
            self.framed = False
        self._booting.lines.append(line)
        if rom or (crash and not self._booting.reason):
            self._booting.reason = line
        if reason:
            self._booting.reason = reason["reason"]
        if ready:
            failed = _START_FAILED.match(line)
            self._booting.version = ready["version"]
            self._booting.failed = failed["reason"] if failed else ""
            self.version = ready["version"]
            if not self._expect_boot:
                self.restarts.append(self._booting)
            self.last_boot = self._booting
            self._booting = None
            self._expect_boot = False
        return True

    def _read(self, line: str) -> str | None:
        """One line from the board: log it, and give back a protocol line.

        None for a line that was the board talking by itself rather than an
        answer. Raises :class:`SerialLoss` for one that did not arrive whole.

        The frame is checked against the payload exactly as it arrived, and
        the surrounding blanks are taken off afterwards: the length in the
        frame counts the bytes the board wrote, so a line that ever ends in
        one must not be measured with it already gone.
        """
        if line and self.log is not None:
            self.log(targets.RECEIVED, line)
        payload, whole = self._unframe(line)
        text = payload.strip(" \t")
        if not whole:
            self.lost += 1
            raise SerialLoss(
                f"{self.label}: a line from the board lost bytes on its "
                f"bridge: {text[:80]!r}")
        if self._boot_evidence(text):
            return None
        if _NOTICE.match(text):
            self._note(text)
            return None
        return text

    def drain(self) -> None:
        """Read what is waiting on the bridge, keeping what the board said.

        Nothing is asking the board anything while the command under test
        runs, so a link dropping or a restart is written to a port no one is
        reading. This is how that reaches :attr:`restarts` and
        :attr:`notices` instead of being thrown away by the next command
        (#311). A partial line is kept for the next call, so a boot log that
        spans two drains is still read as one.

        Nothing here raises: a drained line that lost bytes is counted and
        dropped, because no command is waiting on it.
        """
        for _ in range(DRAIN_READS_MAX):
            chunk = self.port.read(256)
            if not chunk:
                break
            self._buffer += chunk
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            try:
                self._read(self._decode(raw))
            except SerialLoss:
                pass

    @staticmethod
    def _decode(raw: bytes) -> str:
        """One line's bytes as text, with the line ending taken off.

        Only the ending: a space or a tab at either end belongs to the line
        the board wrote, and the frame's length counts it. _read takes those
        off once the frame has been checked.
        """
        return raw.decode("utf-8", "replace").strip("\r\x00")

    # -- commands ----------------------------------------------------------

    @staticmethod
    def repeatable(text: str) -> bool:
        """Whether sending ``text`` a second time can change nothing."""
        return any(text.startswith(safe) if safe.endswith(" ") else text == safe
                   for safe in REPEATABLE)

    def command(self, text: str, timeout: float = COMMAND_TIMEOUT_S,
                parse: Callable[[list[str]], object] | None = None):
        """Send one command and read its answer. ERR or silence raises.

        Returns the data lines, or what ``parse`` makes of them. The parse
        belongs inside the attempt because a status line that lost bytes is
        caught by reading it, not by receiving it: a command whose answer
        can be checked is sent again on an answer that failed the check, the
        same as on one that never came (#310).
        """
        attempts = 1 + (COMMAND_RETRIES if self.repeatable(text) else 0)
        for attempt in range(1, attempts + 1):
            try:
                lines = self._send(text, timeout)
                return lines if parse is None else parse(lines)
            except SerialLoss as error:
                if attempt == attempts:
                    raise SerialLoss(
                        f"{error} (sent '{text}' {attempts} times)" if attempts > 1
                        else str(error)) from error
                self.retried += 1
                if self.log is not None:
                    self.log(targets.SENT, f"# retrying '{text}': {error}")
                time.sleep(RETRY_PAUSE_S)
        raise AssertionError("unreachable")  # pragma: no cover

    def _send(self, text: str, timeout: float) -> list[str]:
        """One attempt at one command."""
        # Read what the board has said since the last command before the
        # port is cleared, so a restart under the capture is not thrown away
        # with it (#311); then the leftovers go, because a part of an answer
        # to an earlier command must not be read as this one's.
        self.drain()
        self._buffer = b""
        self.port.write(text.encode("ascii") + b"\n")
        self.port.flush()
        if self.log is not None:
            self.log(targets.SENT, text)
        data: list[str] = []
        for raw in self._lines(self.clock() + timeout):
            line = self._read(raw)
            if line is None:
                continue
            if line == "OK":
                return data
            if line.startswith("ERR"):
                raise PeerError(
                    f"{self.label}: '{text}' refused: {line[3:].strip() or 'ERR'}")
            data.append(line)
        # Silence and a lost ending look the same from here, and both mean
        # the answer cannot be trusted, so both are the one failure a
        # repeatable command is sent again for.
        raise SerialLoss(
            f"{self.label}: no answer to '{text}' within {timeout:g}s"
            + (f" (last line {data[-1]!r})" if data else ""))

    def status(self, timeout: float = COMMAND_TIMEOUT_S) -> dict:
        return self.command("status", timeout, parse=self._pick_status)

    def _pick_status(self, lines: list[str]) -> dict:
        for line in reversed(lines):
            if line.startswith("{"):
                return parse_status(line, self.label)
        raise SerialLoss(f"{self.label}: status printed no JSON line")

    def ask_for_framing(self, version: str) -> bool:
        """Have the board check-sum every line it writes, if its image can.

        An image older than FRAME_MINIMUM_FIRMWARE has no such command, and
        nothing here depends on one: the loss is then caught where it always
        was, by a line that stopped being readable, and the retry is what
        saves the run. A version that did not arrive readably is treated the
        same way, and :meth:`confirm_framing` is what catches that.

        An image new enough must frame. A board that will not do what its
        version says it can is a board whose lines cannot be checked, and
        carrying on unchecked would quietly give up the whole point of this,
        so that failure is the session's. Returns whether the board is
        framing.
        """
        try:
            check_firmware(version, self.label, FRAME_MINIMUM_FIRMWARE)
        except PeerError:
            self.framed = False
            return False
        self.command(FRAME_ON)
        self.framed = True
        return True

    def confirm_framing(self, firmware: str) -> None:
        """The version in the board's status must agree about the framing.

        :meth:`ask_for_framing` goes on the version in the boot notice,
        which is read before anything is checking the lines, so a notice
        that lost bytes could name an older image than the board is really
        running and the session would run unchecked without saying so. The
        status line is the version check proper, so the two are compared
        once it has been read.
        """
        if self.framed:
            return
        try:
            check_firmware(firmware, self.label, FRAME_MINIMUM_FIRMWARE)
        except PeerError:
            return  # an older image, which frames nothing
        raise PeerError(
            f"{self.label} runs {firmware}, which check-sums the lines it "
            "sends, but this session is not reading framed lines: its boot "
            f"notice named {self.version or 'no version'}")

    def wait_ready(self, timeout: float = BOOT_TIMEOUT_S) -> None:
        """Wait for the boot notice the firmware sends once Bluetooth is up.

        This boot is the driver's own doing, so it is not one of
        :attr:`restarts`: opening the bridge resets the board, and so does
        :meth:`reset`.
        """
        self.last_boot = None
        self._expect_boot = True
        for line in self._lines(self.clock() + timeout):
            try:
                self._read(line)
            except SerialLoss:
                # A boot log is not an answer to anything, so a line of it
                # that lost bytes costs nothing: the notice at the end of it
                # is what this is waiting for.
                continue
            if self.last_boot is None:
                continue
            if self.last_boot.failed:
                raise PeerError(f"{self.label}: Bluetooth did not start: "
                                f"{self.last_boot.failed}")
            # Which version it is, is checked with the status, where the
            # whole state is read at once.
            return
        self._expect_boot = False
        raise PeerError(f"{self.label}: no 'ready' notice within {timeout:g}s of "
                        "opening the bridge")

    def reset(self) -> None:
        """Reset the board with the bridge's own lines, then wait for boot.

        RTS pulls EN low through the dev board's auto-reset transistors while
        DTR leaves IO0 high, which is esptool's hard reset: the chip restarts
        into its application. The board is then in its boot state, so this is
        also how a case releases one.
        """
        self.port.reset_input_buffer()
        self._buffer = b""
        self._booting = None
        self.framed = False
        self.port.dtr = False
        self.port.rts = True
        time.sleep(RESET_PULSE_S)
        self.port.rts = False
        self.wait_ready()

    def serial_report(self) -> str:
        """What the bridge cost this session, or "" when it cost nothing."""
        if not (self.lost or self.retried):
            return ""
        return (f"{self.label} bridge: {self.lost} line(s) lost, "
                f"{self.retried} command(s) sent again")


def parse_status(line: str, label: str = "peer") -> dict:
    """The board's status line, or a refusal.

    A line that is not JSON, or is JSON missing a field or carrying one of
    the wrong type, is refused: a status read is how a session learns what
    the pair did, and a half-read one would be worse than none. The refusal
    is a :class:`SerialLoss` because a line that lost bytes on the bridge is
    what it looks like in practice, so the read is worth one more try (#310);
    it is still refused if it comes back the same.
    """
    try:
        status = json.loads(line)
    except ValueError as error:
        raise SerialLoss(f"{label}: status is not JSON: {line[:80]!r}") from error
    if not isinstance(status, dict):
        raise SerialLoss(f"{label}: status is not a JSON object")
    for key, kind in _STATUS_KEYS.items():
        value = status.get(key)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise SerialLoss(f"{label}: status has no valid '{key}'")
    return status


def check_status(status: dict, label: str, address: str | None = None) -> dict:
    """The board runs usable firmware, Bluetooth is up, at ``address``."""
    check_firmware(status["fw"], label)
    if not status["bt"]:
        raise PeerError(f"{label}: Bluetooth is not up (status bt is false)")
    if address is not None and status["address"] != normalize_address(address):
        raise PeerError(f"{label} reports BD_ADDR {status['address']}, the registry "
                        f"says {normalize_address(address)}")
    lap, uap = address_parts(status["address"])
    if (status["lap"], status["uap"]) != (lap, uap):
        raise PeerError(f"{label} reports LAP {status['lap']} UAP {status['uap']}, "
                        f"which do not belong to {status['address']}")
    return status


# --------------------------------------------------------------------------
# Reporting


def _report(out, label: str, status: dict) -> None:
    print(f"{label} firmware: {status['fw']}", file=out)
    print(f"{label} address: {status['address']}", file=out)
    print(f"{label} LAP: {status['lap']} UAP: {status['uap']}", file=out)
    print(f"{label} role: {status['role']} name: {status['name']}", file=out)


def set_channel_map(shell: Shell, wanted: str, label: str,
                    firmware: str | None = None) -> dict:
    """Hold this board's piconet to ``wanted`` and check the controller took it.

    The shell answers once the controller has taken the map rather than when
    the command was sent, so the status read after it reports the new map.
    Reading it back is what makes the report's requested map a measurement
    and not a restatement of the command line.
    """
    check_firmware(firmware if firmware is not None else shell.status()["fw"],
                   label, AFH_MINIMUM_FIRMWARE)
    shell.command(f"afh {wanted}")
    status = shell.status()
    expected = requested_map(wanted)
    taken = str(status.get("afh") or "")
    if taken != expected:
        raise PeerError(f"{label} reports channel map {taken or 'none'} after "
                        f"'afh {wanted}', not {expected}")
    used = len(afhmap.channels_from_hex(expected, "the requested map"))
    if status.get("afh_used") != used:
        raise PeerError(f"{label} reports {status.get('afh_used')} channels used "
                        f"in {taken}, which has {used}")
    return status


def read_link_map(shell: Shell, label: str, at: str, seconds: float) -> dict:
    """One reading of the map the live link is hopping over.

    Every reason the read can fail is the controller's or the link's, and
    each one makes the map unknown rather than wrong, so it is refused here
    instead of being recorded as a map (the peer's README, "The two maps in
    `status`").
    """
    status = shell.status()
    error = str(status.get("afh_link_error") or "")
    if error:
        raise PeerError(f"{label}: the link's channel map could not be read at "
                        f"the {at} of the command: {error}")
    link = status.get("afh_link")
    if not link:
        raise PeerError(f"{label}: the controller reports no channel map for one "
                        f"link at the {at} of the command; a board with no link, "
                        "or more than one, has no piconet map")
    mode = str(status.get("afh_mode") or "")
    if mode != "on":
        raise PeerError(f"{label}: the controller reports AFH {mode or 'unknown'} "
                        f"at the {at} of the command, so the link is not hopping "
                        "over a map")
    channels = afhmap.channels_from_hex(str(link), "the in-force map")
    if status.get("afh_link_used") != len(channels):
        raise PeerError(f"{label}: the controller reports "
                        f"{status.get('afh_link_used')} channels used in {link}, "
                        f"which has {len(channels)}")
    return {"at": at, "seconds": round(seconds, 1), "link": str(link),
            "used": len(channels), "mode": mode, "error": ""}


def _report_map(out, requested: str, samples: Sequence[dict]) -> None:
    print(f"channel map requested: {requested} used "
          f"{len(afhmap.channels_from_hex(requested, 'the requested map'))}",
          file=out)
    for sample in samples:
        print(f"channel map in force at the {sample['at']} "
              f"({sample['seconds']:.1f} s): {sample['link']} used "
              f"{sample['used']}", file=out)
    maps = {sample["link"] for sample in samples}
    if len(maps) == 1:
        print("channel map in force: stable", file=out)
    else:
        print("channel map in force: changed under the command", file=out)


def _report_traffic(out, label: str, status: dict) -> None:
    print(f"{label} traffic: writes {status['writes']} errors "
          f"{status['write_errors']} received {status['rx_bytes']} bytes "
          f"connects {status['connects']} disconnects {status['disconnects']}",
          file=out)


# --------------------------------------------------------------------------
# Operations


def shell_logger(alias: str, stream=None):
    """Writes each shell line, marked with ``alias``, for the run's transcript."""
    def log(direction: str, text: str) -> None:
        print(targets.shell_line(alias, direction, text),
              file=stream or sys.stderr, flush=True)
    return log


@dataclass
class Peer:
    device: str
    label: str
    address: str | None = None
    # The board's registry alias. With one, every line to and from its shell
    # is written to stderr marked with that alias.
    alias: str = ""
    # Opens the shell on ``device``; tests pass a fake.
    opener: Callable[[str, str], Shell] | None = None

    def open(self) -> Shell:
        if self.opener is not None:
            return self.opener(self.device, self.label)
        if not os.path.exists(self.device):
            raise PeerError(f"{self.device} does not exist: is the {self.label}'s "
                            "USB bridge attached?")
        return Shell.open(self.device, self.label,
                          shell_logger(self.alias) if self.alias else None)


def identify(peer: Peer, out=None) -> dict:
    """Report the board behind this bridge: version, address, LAP, UAP, role."""
    out = out or sys.stdout
    shell = peer.open()
    try:
        shell.wait_ready()
        # Before the status read, because a status line is what loses bytes
        # (#310). These one-board commands say nothing about it: the shell
        # does the asking and the retrying, and their own report is what a
        # case reads.
        shell.ask_for_framing(shell.version)
        status = check_status(shell.status(), peer.label, peer.address)
        shell.confirm_framing(status["fw"])
    finally:
        shell.close()
    print(f"{peer.label} device: {peer.device}", file=out)
    _report(out, peer.label, status)
    return status


def status_command(peer: Peer, out=None) -> dict:
    out = out or sys.stdout
    shell = peer.open()
    try:
        shell.wait_ready()
        shell.ask_for_framing(shell.version)
        status = check_status(shell.status(), peer.label, peer.address)
        shell.confirm_framing(status["fw"])
    finally:
        shell.close()
    print(json.dumps(status, sort_keys=True), file=out)
    _report(out, peer.label, status)
    _report_traffic(out, peer.label, status)
    return status


def reset(peer: Peer, out=None) -> dict:
    """Reset the board and check every field of its status is a boot value.

    This is how a case releases a peer, so it has to leave nothing of the
    previous case behind: not a role, a half-open link, a stream setting, or
    a counter. Anything that differs from BOOT_STATE is named.
    """
    out = out or sys.stdout
    shell = peer.open()
    try:
        shell.reset()
        shell.ask_for_framing(shell.version)
        status = check_status(shell.status(), peer.label, peer.address)
        shell.confirm_framing(status["fw"])
    finally:
        shell.close()
    wrong = [f"{key} {status[key]!r}, not {value!r}"
             for key, value in BOOT_STATE.items() if status[key] != value]
    if wrong:
        raise PeerError(f"{peer.label} is not in its boot state after a reset: "
                        + ", ".join(wrong))
    _report(out, peer.label, status)
    return status


def _tee(stream, sinks) -> None:
    """Pass the command's output on, line by line, to every sink.

    One line at a time and flushed, because the sinks are the stream the
    step's record reads and the saved file a later step reads: a capture
    stopped partway must leave both complete up to the last line the command
    wrote.
    """
    for line in stream:
        for sink in sinks:
            sink.write(line)
            sink.flush()


def _stop_child(child, grace: float = INTERRUPT_TIMEOUT_S) -> None:
    """End the command, interrupt first, kill if it will not go.

    SIGINT rather than a kill, for the reason _run_command gives: a capture
    tool stops its board and closes its files on it. Every caller is already
    reporting something that went wrong, so this reports nothing of its own;
    a command that survives both signals leaves its own error for the caller
    rather than a second one from here.
    """
    if child.poll() is not None:
        return
    try:
        child.send_signal(signal.SIGINT)
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
            child.wait(timeout=grace)
        except (subprocess.TimeoutExpired, OSError):
            pass
    except OSError:
        pass


def _run_command_watched(command: Sequence[str], *, timeout: float,
                         interrupt_after: float | None = None,
                         output_path: str | None = None,
                         sample_at: float | None = None,
                         sample: Callable[[float], None] | None = None,
                         drain: Callable[[], None] | None = None,
                         out, pass_through=None) -> tuple[int, float]:
    """Run the thing under test, saving its output and stopping it on time.

    ``interrupt_after`` is for a command that runs until it is signalled:
    ``ubertooth-afh`` prints the map it has worked out and then keeps
    watching for changes, so the interrupt is the planned end of the capture
    and its exit status is the command's own. ``sample_at`` reads the peer
    while the command runs, which costs a round trip to the board's shell
    and nothing on the air.

    The command is never left running. A sample that fails, which is what a
    link going down mid-capture looks like, would otherwise leave a capture
    tool holding the board while the harness waited out the step's deadline,
    and the retry would start on a board that was still busy.
    """
    started = time.monotonic()
    saved = (open(output_path, "w", encoding="utf-8")  # noqa: SIM115 (closed below)
             if output_path else None)
    try:
        try:
            child = subprocess.Popen(list(command), stdout=subprocess.PIPE,
                                     text=True, errors="replace")
        except OSError as error:
            raise PeerError(f"cannot run {command[0]}: {error}") from error
        sinks = [pass_through if pass_through is not None else sys.stdout]
        if saved:
            sinks.append(saved)
        pump = threading.Thread(target=_tee, args=(child.stdout, sinks),
                                daemon=True)
        pump.start()

        def drained() -> None:
            """Wait for the reader, then let go of the pipe.

            The reader is given the same grace to pass on what the command
            wrote before it exited. One still blocked after that keeps the
            pipe open until this process ends, which is close enough behind
            it; closing the pipe underneath the thread would raise there
            instead of here, and the thread is a daemon either way.
            """
            pump.join(INTERRUPT_TIMEOUT_S)
            if not pump.is_alive():
                child.stdout.close()

        try:
            overran = _watch(child, out, started=started, timeout=timeout,
                             interrupt_after=interrupt_after,
                             sample_at=sample_at, sample=sample, drain=drain)
        except BaseException:
            # A sample that raised, or a cancellation: the command goes
            # first, so nothing is left holding the board.
            _stop_child(child)
            drained()
            raise
        if overran:
            interrupted = (interrupt_after is not None
                           and time.monotonic() - started >= interrupt_after)
            _stop_child(child)
            drained()
            raise PeerError(
                f"{command[0]} did not stop within {INTERRUPT_TIMEOUT_S:g}s of "
                "the interrupt that ended its capture" if interrupted else
                f"{command[0]} did not finish within {timeout:g}s")
        status = child.wait()
        drained()
        return status, time.monotonic() - started
    finally:
        if saved is not None:
            saved.close()


def _watch(child, out, *, started: float, timeout: float,
           interrupt_after: float | None, sample_at: float | None,
           sample: Callable[[float], None] | None,
           drain: Callable[[], None] | None = None) -> bool:
    """Wait for the command, sampling, draining, and interrupting on time.

    True when it was still running at its deadline, which is the caller's to
    report; False when it ended by itself or at the interrupt.

    ``drain`` reads whatever the boards have said since the last time it
    ran. Nothing is asking them anything while the command is running, so
    this is the only moment a restart under the capture can be seen (#311).
    """
    deadline = started + timeout
    stop_at = None if interrupt_after is None else started + interrupt_after
    due = None if sample_at is None or sample is None else started + sample_at
    drain_at = None if drain is None else started + DRAIN_S
    while child.poll() is None:
        now = time.monotonic()
        if drain_at is not None and now >= drain_at:
            drain_at = now + DRAIN_S
            drain()
            continue
        if due is not None and now >= due:
            due = None
            sample(now - started)
            continue
        if stop_at is not None and now >= stop_at:
            stop_at = None
            print(f"command interrupted after {now - started:.1f} s", file=out)
            out.flush()
            child.send_signal(signal.SIGINT)
            # The interrupt is the planned end of the capture, so from here
            # the command has a grace of its own: long enough to stop its
            # board and close its files, not long enough to hold the boards
            # for a run that is already over. It replaces the deadline rather
            # than being capped by it, because the whole-command deadline is
            # about the window and this is about stopping.
            deadline = time.monotonic() + INTERRUPT_TIMEOUT_S
            continue
        if now >= deadline:
            return True
        time.sleep(POLL_S)
    return False


def _run_command(command: Sequence[str], timeout: float,
                 drain: Callable[[], None] | None = None) -> tuple[int, float]:
    """Run the thing under test, inheriting our streams, with a deadline.

    The child stays in this process's group, so the harness's own deadline
    reaches it; this deadline is the backstop for a command with none of its
    own.

    ``drain`` reads the boards' bridges while the command runs, for the
    reason :func:`_watch` gives: it is the only moment a restart under the
    capture can be seen (#311). Waiting in short steps rather than one long
    one is what makes room for it.
    """
    started = time.monotonic()
    try:
        child = subprocess.Popen(list(command))
    except OSError as error:
        raise PeerError(f"cannot run {command[0]}: {error}") from error
    try:
        if drain is None:
            return child.wait(timeout=timeout), time.monotonic() - started
        deadline = started + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(command[0], timeout)
            try:
                status = child.wait(timeout=min(DRAIN_S, left))
            except subprocess.TimeoutExpired:
                drain()
                continue
            return status, time.monotonic() - started
    except subprocess.TimeoutExpired:
        # Interrupt first: a capture tool stops its board and closes its
        # files on SIGINT, which a kill would skip.
        child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=INTERRUPT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        raise PeerError(f"{command[0]} did not finish within {timeout:g}s")


def stream(acceptor: Peer, initiator: Peer, command: Sequence[str], *,
           period: int = DEFAULT_PERIOD_MS, payload: int = DEFAULT_PAYLOAD,
           min_writes: int = 1, min_seconds: float = 0.0,
           timeout: float = 300.0, out=None,
           settle_s: float = SETTLE_S, wanted_map: str | None = None,
           map_report: str | None = None, command_output: str | None = None,
           interrupt_after: float | None = None) -> int:
    """Hold the pair's link up and streaming while ``command`` runs.

    Both ports are opened first, which resets both boards, so the pair starts
    from the firmware's boot state however an earlier case left it.
    """
    out = out or sys.stderr
    period = period_ms(period)
    payload = payload_bytes(payload)
    wanted = None if wanted_map is None else channel_map(wanted_map)
    if map_report and wanted is None:
        raise PeerError("--map-report needs --map: with no map asked for there "
                        "is nothing to compare a recovered map against")
    if acceptor.device == initiator.device:
        raise PeerError("the acceptor and the initiator are the same bridge; a "
                        "Classic link needs two boards")
    acceptor_shell = acceptor.open()
    try:
        initiator_shell = initiator.open()
        try:
            return _stream(acceptor, acceptor_shell, initiator, initiator_shell,
                           command, period=period, payload=payload,
                           min_writes=min_writes, min_seconds=min_seconds,
                           timeout=timeout, out=out, settle_s=settle_s,
                           wanted_map=wanted, map_report=map_report,
                           command_output=command_output,
                           interrupt_after=interrupt_after)
        finally:
            initiator_shell.close()
    finally:
        acceptor_shell.close()


def _release(shell: Shell, peer: Peer, expected: dict) -> str:
    """Leave a board in its boot state; say so when it did not get there.

    Returns the reason it did not, or "". A board still carrying a role, a
    profile, a packet-type choice or a counter of this session's is where
    the next case starts from, so the release is checked rather than hoped
    for: this used to swallow every failure, which let a session that could
    not put a board back read as a pass (#264).
    """
    try:
        shell.reset()
        # The board that came back has not been asked to check-sum
        # anything, so the release's own status read asks again (#310).
        shell.ask_for_framing(shell.version)
        status = shell.status()
        shell.confirm_framing(status.get("fw", ""))
    except Exception as error:
        # Any failure, not only a PeerError: a bridge that went away raises
        # from pyserial, and letting that out of here would skip the other
        # board's release as well.
        return f"the {peer.label} was not released to its boot state: {error}"
    wrong = [f"{key} {status.get(key)!r}, not {value!r}"
             for key, value in expected.items() if status.get(key) != value]
    if wrong:
        return (f"the {peer.label} is not in its boot state after its release "
                "reset: " + ", ".join(wrong))
    return ""


def _middle_of(interrupt_after: float | None, timeout: float) -> float:
    """Halfway through the capture, which is where the middle sample is read.

    A command that is stopped on time has a known length, so half of it is
    the middle; one that stops itself has only the deadline, and half of
    that is the best the session can do.
    """
    return (timeout if interrupt_after is None else interrupt_after) / 2


def _write_map_report(path: str, requested: str, samples: Sequence[dict],
                      command: Sequence[str], seconds: float) -> None:
    """The session's maps as JSON, for afhmap.py to judge the recovery by."""
    report = {
        "requested": requested,
        "requested_used": len(afhmap.channels_from_hex(requested,
                                                       "the requested map")),
        "samples": list(samples),
        "command": list(command),
        "seconds": round(seconds, 1),
    }
    try:
        Path(path).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n",
                              encoding="utf-8")
    except OSError as error:
        raise PeerError(f"cannot write the map report {path}: {error}") from error


def _restart_problems(out, boards) -> list[str]:
    """Report every restart the boards' bridges showed, and name each one.

    A board that went away and came back under a capture has counters that
    all read zero, which used to be reported as a pair that never streamed
    (#311). This is what the session says instead, with the reason the chip
    gave and the lines it said on the way, so the run names its own failure.
    """
    problems = []
    for peer, shell in boards:
        for event in shell.restarts:
            print(f"{peer.label} restarted: {event.describe()}", file=out)
            for line in event.lines:
                print(f"{peer.label} restart line: {line}", file=out)
            problems.append(f"the {peer.label} restarted while the session ran, "
                            f"{event.describe()}, so its counters below are a "
                            "board that has just booted and this run measured "
                            "nothing")
    return problems


def _back_at_boot(label: str, status: dict, role: str) -> str:
    """Why this status says the board restarted, or "".

    The session set the board's role, and a board is only in the boot role
    with its SPP server listening when it has just booted. So a status that
    reports both is a restart even when nothing of the board's own account
    of it reached the host, which is the case where the bridge's buffer
    overflowed while the capture ran (#311).
    """
    if status["role"] == role:
        return ""
    return (f"the {label} reports role {status['role']} with its SPP server "
            f"{'on' if status['spp_server'] else 'off'}, where this session "
            f"set {role}: the board is back in its boot state, so it "
            "restarted while the session ran")


def _settings_taken(label: str, status: dict, address: str, period: int,
                    payload: int) -> list[str]:
    """What the board made of the settings the session sent it.

    Bytes go missing in both directions on a loaded host. The firmware
    refuses most command lines that lost some, but not all of them:
    ``stream payload 200`` that arrives as ``stream payload 20`` is another
    valid command, and nothing in the answer says so. So every setting that
    decides what goes on the air is read back (#310).
    """
    wrong = []
    for key, wanted in (("peer", address), ("period_ms", period),
                        ("payload_len", payload)):
        if status[key] != wanted:
            wrong.append(f"the {label} reports {key} {status[key]!r} where this "
                         f"session set {wanted!r}")
    return wrong


def _why(restarted: bool) -> str:
    """How a failed stream session names itself.

    A session whose peer went away did not fail to stream: it lost a board,
    and every zero it read afterwards follows from that. Saying so is the
    whole of #311's first half.
    """
    return ("a peer restarted while the session ran: " if restarted
            else "the pair did not stream: ")


def _report_serial(out, boards) -> None:
    """What the bridges cost, for a board that lost any lines at all."""
    for _peer, shell in boards:
        line = shell.serial_report()
        if line:
            print(line, file=out)


def _ask_for_framing(out, boards) -> None:
    """Have each board check-sum its lines, where its image can (#310).

    The version comes from the boot notice rather than from a status line,
    so the framing is on before the session's first status read, which is
    one of the reads #310 was reported from.
    """
    for peer, shell in boards:
        if shell.ask_for_framing(shell.version):
            print(f"{peer.label} bridge: framed", file=out)
            continue
        image = shell.version or "that image"
        print(f"{peer.label} bridge: not framed, {image} is older than "
              f"{FRAME_MINIMUM_FIRMWARE}", file=out)


def _stream(acceptor, acceptor_shell, initiator, initiator_shell, command, *,
            period, payload, min_writes, min_seconds, timeout, out,
            settle_s, wanted_map=None, map_report=None, command_output=None,
            interrupt_after=None) -> int:
    boards = ((initiator, initiator_shell), (acceptor, acceptor_shell))
    acceptor_shell.wait_ready()
    initiator_shell.wait_ready()
    _ask_for_framing(out, boards)
    acceptor_status = check_status(acceptor_shell.status(), acceptor.label,
                                   acceptor.address)
    initiator_status = check_status(initiator_shell.status(), initiator.label,
                                    initiator.address)
    initiator_shell.confirm_framing(initiator_status["fw"])
    acceptor_shell.confirm_framing(acceptor_status["fw"])
    if acceptor_status["address"] == initiator_status["address"]:
        raise PeerError("both bridges lead to one board: they report the same "
                        f"BD_ADDR {acceptor_status['address']}")
    _report(out, acceptor.label, acceptor_status)
    _report(out, initiator.label, initiator_status)
    # The initiator pages, so it is the central and its LAP is on the air.
    print(f"central LAP: {initiator_status['lap']}", file=out)
    print(f"central UAP: {initiator_status['uap']}", file=out)
    print(f"stream: {payload} bytes every {period} ms", file=out)
    samples: list[dict] = []
    problems: list[str] = []
    restarts: list[str] = []
    after_initiator = after_acceptor = None
    seconds = 0.0

    def drain() -> None:
        for _peer, shell in boards:
            shell.drain()

    try:
        initiator_shell.command(ROLE_INITIATOR, ROLE_TIMEOUT_S)
        initiator_shell.command(f"spp peer {acceptor_status['address']}")
        initiator_shell.command(f"stream period {period}")
        initiator_shell.command(f"stream payload {payload}")
        if wanted_map is not None:
            # Before the link: the leader announces the map it has when the
            # link comes up, so the whole capture then runs on one map.
            set_channel_map(initiator_shell, wanted_map, initiator.label,
                            initiator_status["fw"])
        initiator_shell.command("spp start", CONNECT_TIMEOUT_S)
        initiator_shell.command("stream on")
        linked = initiator_shell.status()
        if not linked["connected"] or linked["remote"] != acceptor_status["address"]:
            raise PeerError(
                f"the link is not up to {acceptor_status['address']} after 'spp "
                f"start' (remote {linked['remote'] or 'none'})")
        wrong = _settings_taken(initiator.label, linked,
                                acceptor_status["address"], period, payload)
        if wrong:
            raise PeerError("the initiator did not take this session's stream "
                            "settings: " + "; ".join(wrong))
        print(f"link: up to {linked['remote']} at "
              f"{time.strftime('%H:%M:%SZ', time.gmtime())}", file=out)
        time.sleep(settle_s)
        if wanted_map is not None:
            samples.append(read_link_map(initiator_shell, initiator.label,
                                         SAMPLE_POINTS[0], 0.0))
        print("command: " + " ".join(command), file=out)
        out.flush()
        if wanted_map is None and command_output is None and interrupt_after is None:
            status, seconds = _run_command(command, timeout, drain)
        else:
            def middle(elapsed: float) -> None:
                samples.append(read_link_map(initiator_shell, initiator.label,
                                             SAMPLE_POINTS[1], elapsed))
            status, seconds = _run_command_watched(
                command, timeout=timeout, interrupt_after=interrupt_after,
                output_path=command_output,
                sample_at=None if wanted_map is None else _middle_of(
                    interrupt_after, timeout),
                sample=None if wanted_map is None else middle, out=out,
                drain=drain)
        print(f"command exit: {status} after {seconds:.1f} s", file=out)
        # The last of the command's own window: whatever the boards said in
        # it that the drains inside the command have not read yet.
        drain()
        # Before anything else can fail: a restart is the one failure that
        # explains every other reading, and the reads below are the ones a
        # restarted board is least likely to answer as the session expects.
        restarts = _restart_problems(out, boards)
        problems += restarts
        try:
            if wanted_map is not None:
                samples.append(read_link_map(initiator_shell, initiator.label,
                                             SAMPLE_POINTS[2], seconds))
            initiator_shell.command("stream off")
            after_initiator = initiator_shell.status()
            after_acceptor = acceptor_shell.status()
        except PeerError as error:
            # A board that went away cannot answer for the capture, and
            # saying why it could not beats replacing the restart above
            # with a read that failed because of it.
            problems.append(str(error))
    finally:
        # On the way out of a session that failed, the failure is the whole
        # report and the case's own cleanup resets both boards again, so
        # these reasons are only collected for a session that got this far.
        released = [_release(initiator_shell, initiator, BOOT_STATE),
                    _release(acceptor_shell, acceptor, BOOT_STATE)]
    # How the failure reads: a run that lost a board did not fail to stream,
    # it lost a board (#311). Only the restarts themselves decide that. The
    # reads after the command can fail for their own reasons - a bridge that
    # went away, a line lost for the last time - and none of those is a
    # board that restarted.
    restarted = bool(restarts)
    _report_serial(out, boards)
    if wanted_map is not None and len(samples) == len(SAMPLE_POINTS):
        requested = requested_map(wanted_map)
        _report_map(out, requested, samples)
        if map_report:
            _write_map_report(map_report, requested, samples, command, seconds)
    problems += [reason for reason in released if reason]
    if after_initiator is None or after_acceptor is None:
        # One of the reads after the command failed, and said why; the
        # fallback is there so this can never raise an empty reason.
        raise PeerError(_why(restarted) + "; ".join(
            problems or ["neither board could be read after the command"]))
    _report_traffic(out, initiator.label, after_initiator)
    _report_traffic(out, acceptor.label, after_acceptor)
    if not restarted:
        # Only where the bridge showed nothing. A restart the bridge did
        # report has already been named with its reason, which is more than
        # this can say, so saying it twice would only lengthen the failure.
        boot_state = _back_at_boot(initiator.label, after_initiator, "initiator")
        if boot_state:
            problems.append(boot_state)
            restarted = True
    if after_initiator["disconnects"]:
        problems.append(f"the link went down {after_initiator['disconnects']} "
                        "time(s) while the command ran")
    if after_initiator["writes"] < min_writes:
        problems.append(f"the initiator reported {after_initiator['writes']} "
                        f"writes, fewer than the {min_writes} this test needs")
    if after_acceptor["rx_bytes"] < payload:
        problems.append(f"the acceptor received {after_acceptor['rx_bytes']} "
                        "bytes, less than one payload")
    if seconds < min_seconds:
        problems.append(f"{command[0]} ran {seconds:.1f} s, less than the "
                        f"{min_seconds:g} s this test needs: it stopped early")
    if len({sample["link"] for sample in samples}) > 1:
        # The leader's controller may classify channels again while the link
        # is up. There is then no one map the capture ran on, so the session
        # says so rather than leaving a comparison to pick a sample.
        problems.append("the link's channel map changed under the command: "
                        + "; ".join(f"{sample['at']} {sample['link']}"
                                    for sample in samples))
    if problems:
        raise PeerError(_why(restarted) + "; ".join(problems))
    print("pair streamed: yes", file=out)
    return status


def audio(acceptor: Peer, initiator: Peer, command: Sequence[str], *,
          packet_type: str = DEFAULT_AUDIO_TYPE, min_frames: int = 1,
          min_seconds: float = 0.0, timeout: float = 300.0, out=None,
          settle_s: float = SETTLE_S) -> int:
    """Hold a synchronous audio link up between the pair while ``command`` runs.

    The air then carries reserved-slot voice packets, and the Hands-Free
    service level connection beside them; this session starts no SPP stream.
    An ``ev3`` link's payload is at the basic rate, so a receive-only capture
    can decode it. As with ``stream``, both ports are opened first, which
    resets both boards.
    """
    out = out or sys.stderr
    if packet_type not in AUDIO_TYPES:
        raise PeerError("the audio packet type must be one of "
                        + ", ".join(AUDIO_TYPES))
    # A negative floor would check nothing at all.
    if min_frames < 0:
        raise PeerError(f"the frame floor cannot be negative, got {min_frames!r}")
    if acceptor.device == initiator.device:
        raise PeerError("the acceptor and the initiator are the same bridge; an "
                        "audio link needs two boards")
    acceptor_shell = acceptor.open()
    try:
        initiator_shell = initiator.open()
        try:
            return _audio(acceptor, acceptor_shell, initiator, initiator_shell,
                          command, packet_type=packet_type,
                          min_frames=min_frames, min_seconds=min_seconds,
                          timeout=timeout, out=out, settle_s=settle_s)
        finally:
            initiator_shell.close()
    finally:
        acceptor_shell.close()


def _audio_field(status: dict, key: str, kind, label: str,
                 where: str = "status"):
    """One of the audio link's own fields, which older images lack.

    A status field that is missing or of the wrong type is usually an image
    older than the one these fields arrived in, so that is what the reason
    says. A counter missing from the `sco stats` line is not: that command
    answers all seven or refuses, so a line short of one is a line that lost
    bytes, and saying so is what lets the read be tried again (#310).
    """
    value = status.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        if where != "status":
            raise SerialLoss(f"{label}: {where} has no valid '{key}'")
        raise PeerError(f"{label}: status has no valid '{key}'; the audio link "
                        f"needs {AUDIO_FIRMWARE} or a later 1.x image")
    return value


def _audio_count(values: dict, key: str, label: str,
                 where: str = "status") -> int:
    """One counter of the audio link: a whole number, and never negative.

    Every floor this session applies and every figure it reports is
    arithmetic on these, so a value that is not a number at all, or one
    below zero, is named here rather than carried into a subtraction that
    would make nonsense of it.
    """
    value = _audio_field(values, key, int, label, where)
    if value < 0:
        # A negative counter from `sco stats` is corruption for the same
        # reason a missing one is, so it is worth reading again.
        kind = SerialLoss if where != "status" else PeerError
        raise kind(f"{label}: {where} reports {key} {value}, and a counter "
                   "cannot be negative")
    return value


def _require_audio_up(status: dict, peer: Peer, address: str) -> None:
    if _audio_field(status, "sco", str, peer.label) != AUDIO_ON:
        raise PeerError(f"the {peer.label}'s audio link to {address} is not up "
                        f"after '{SCO_START}' (sco {status['sco']})")


def _pkt_counters(shell: Shell, peer: Peer) -> dict:
    """The controller's own synchronous-packet counters, from `sco stats`.

    These count the packets that went on the air, which is what a capture
    of the link is a capture of; the frame counters in `status` count what
    the application made of them. Both are read at the same moment, so a
    session can say what the air carried as well as what the two
    applications saw of it (#264).

    The command needs a live link, so it belongs inside the session; a
    board that refuses it says why, and that reason is the session's.

    The reading is done inside the command, through ``parse``, so a line
    that did not survive the bridge is read again rather than reported: this
    command is one of the repeatable ones, and an answer that cannot be
    parsed is what a lost run of bytes looks like on an image with no
    framing (#310).
    """
    def read(lines: list[str]) -> dict:
        for line in reversed(lines):
            if line.startswith("{"):
                try:
                    counters = json.loads(line)
                except ValueError as error:
                    raise SerialLoss(
                        f"{peer.label}: '{SCO_STATS}' answered {line[:80]!r}, "
                        "which is not JSON") from error
                if not isinstance(counters, dict):
                    raise SerialLoss(f"{peer.label}: '{SCO_STATS}' did not "
                                     "answer a JSON object")
                return {key: _audio_count(counters, key, peer.label, SCO_STATS)
                        for key in AUDIO_PKT_COUNTERS}
        raise SerialLoss(f"{peer.label}: '{SCO_STATS}' printed no JSON line")

    return shell.command(SCO_STATS, parse=read)


def _reading(shell: Shell, peer: Peer) -> tuple[dict, str]:
    """Everything the session measures on one board, at one moment.

    Taken once as late as possible before the command starts and once as
    soon as it has finished, so every figure the session reports belongs to
    the command's own window rather than to the whole life of the board.

    Returns the reading and, separately, the reason the controller's packet
    counters are not in it. They need a live link, so a link that went down
    during the capture and stayed down cannot give them - and that is
    exactly the session whose other figures matter most, so the status half
    of the reading is kept and the missing counters are reported as their
    own problem rather than losing everything (#264).
    """
    status = shell.status()
    reading = {
        "status": status,
        "handle": _audio_count(status, "sco_handle", peer.label),
        "counters": {
            key: _audio_count(status, key, peer.label)
            for key in AUDIO_FRAME_COUNTERS + AUDIO_LINK_COUNTERS
            + SPP_LINK_COUNTERS
        },
        "packets": None,
    }
    try:
        reading["packets"] = _pkt_counters(shell, peer)
    except PeerError as error:
        return reading, str(error)
    return reading, ""


def _moved(before: dict, after: dict, peer: Peer, where: str) -> dict:
    """How far each counter moved between two readings of one board.

    A counter that went backwards means the board zeroed it part way
    through, and no delta can be taken across that, so it is named rather
    than reported as a figure that would read like a quiet link.
    """
    moved = {}
    for key, start in before.items():
        end = after[key]
        if end < start:
            raise PeerError(f"the {peer.label}'s {where} {key} went backwards, "
                            f"{start} to {end}: its counters were zeroed while "
                            "the command ran")
        moved[key] = end - start
    return moved


def _audio_checks(peer: Peer, status: dict, counters: dict, packets: dict | None,
                  handles: tuple[int, int], *, min_frames: int) -> list[str]:
    """What the command's own window must show for its capture to mean something."""
    problems = []
    if status.get("sco") != AUDIO_ON:
        problems.append(f"the {peer.label}'s audio link read {status.get('sco')} "
                        f"afterwards, not {AUDIO_ON}")
    # One link, from the first reading to the last. A link that went down
    # and came back leaves both ends reading `on` either side of the
    # command and is still not the one link a capture of it would be of.
    if counters["sco_connects"] or counters["sco_disconnects"]:
        problems.append(f"the {peer.label}'s audio link did not stay up while "
                        f"the command ran: {counters['sco_connects']} opened, "
                        f"{counters['sco_disconnects']} went down")
    elif handles[0] != handles[1]:
        problems.append(f"the {peer.label}'s audio link handle changed from "
                        f"{handles[0]} to {handles[1]}: the command did not run "
                        "against one link")
    if counters["disconnects"]:
        # The Serial Port Profile's counter. An audio session brings no SPP
        # link up, so it moving means something else was going on.
        problems.append(f"the {peer.label}'s data link went down "
                        f"{counters['disconnects']} time(s) while the command ran")
    if counters["sco_tx_frames"] < min_frames:
        problems.append(f"the {peer.label} sent {counters['sco_tx_frames']} audio "
                        f"frames while the command ran, fewer than the "
                        f"{min_frames} this test needs")
    if not counters["sco_rx_frames"]:
        problems.append(f"the {peer.label} counted no audio frames from the far "
                        "end while the command ran")
    # The controller's counters are the air. Frames the application handled
    # with nothing counted under them would describe a link that was not
    # there, and a capture taken against it would be of nothing. None of
    # them where the board could not give them at all, which the reading
    # has already reported.
    if packets is None:
        return problems
    if not packets["tx_total"]:
        problems.append(f"the {peer.label}'s controller sent no synchronous "
                        "packets while the command ran")
    if not packets["rx_total"]:
        problems.append(f"the {peer.label}'s controller received no synchronous "
                        "packets while the command ran")
    elif not packets["rx_correct"]:
        problems.append(f"the {peer.label}'s controller received "
                        f"{packets['rx_total']} synchronous packets while the "
                        "command ran and not one of them correct")
    return problems


def _audio_measured(out, boards, before: dict, after: dict, *,
                    min_frames: int) -> list[str]:
    """Report what each board measured over the command, and what is wrong.

    Every figure is the movement across the command's own window. Printing
    comes before the link is taken down and the boards are released, so a
    stop or a release that fails afterwards adds to the report rather than
    replacing it (#264).
    """
    problems = []
    for peer, _shell in boards:
        start, end = before.get(peer.label), after.get(peer.label)
        if start is None or end is None:
            continue  # the reading itself failed, and said so
        status = end["status"]
        try:
            # Inside the try with the arithmetic: a board that did not
            # report one of these is this board's problem, and the other
            # board's figures, the stop, and the release still happen.
            counters = _moved(start["counters"], end["counters"], peer, "status")
            # None where the board could not give its controller's counters,
            # which the reading reported as its own problem.
            packets = None
            if start["packets"] is not None and end["packets"] is not None:
                packets = _moved(start["packets"], end["packets"], peer,
                                 SCO_STATS)
        except PeerError as error:
            problems.append(str(error))
            continue
        state = _audio_field(status, "sco", str, peer.label)
        settled = _audio_field(status, "sco_pkt_type", str, peer.label)
        print(f"{peer.label} audio: {state} {settled}, "
              f"sent {counters['sco_tx_frames']} received "
              f"{counters['sco_rx_frames']} frames, "
              f"{counters['sco_tx_errors']} send errors, "
              f"{counters['sco_rx_bad']} damaged, link "
              f"{counters['sco_connects']} opened "
              f"{counters['sco_disconnects']} dropped", file=out)
        if packets is not None:
            print(f"{peer.label} packets: controller sent {packets['tx_total']} "
                  f"received {packets['rx_total']}, {packets['rx_correct']} "
                  f"good, {packets['rx_err']} bad, {packets['rx_none']} "
                  f"missing, {packets['rx_lost']} lost, "
                  f"{packets['tx_discarded']} discarded", file=out)
        problems += _audio_checks(peer, status, counters, packets,
                                  (start["handle"], end["handle"]),
                                  min_frames=min_frames)
    return problems


def _require_audio_type(status: dict, peer: Peer, packet_type: str) -> None:
    """The link is an eSCO one, and of the type that was asked for.

    Both boards are checked, because each works out what its link carries
    from the types it allowed and the packet length its own controller
    reported. A test that asks for ``ev3`` because it wants a basic-rate
    payload on the air must not quietly be handed the enhanced-rate type, or
    a link the board cannot name at all.
    """
    link = _audio_field(status, "sco_link", str, peer.label)
    settled = _audio_field(status, "sco_pkt_type", str, peer.label)
    length = _audio_field(status, "sco_tx_len", int, peer.label)
    if AUDIO_UNNAMED in (link, settled) or length == 0:
        # The board could not read its link back, so it cannot say what is on
        # the air. With `default` that reading is all there is to go on.
        raise PeerError(f"the {peer.label} cannot say what its audio link "
                        f"carries (link {link}, packet type {settled}, "
                        f"{length} bytes a packet)")
    if link != AUDIO_ESCO:
        raise PeerError(f"the {peer.label} reports the link as {link}, not "
                        f"{AUDIO_ESCO}: an audio session carries voice over an "
                        f"{AUDIO_ESCO} link")
    wanted = AUDIO_TYPE_ON_LINK.get(packet_type)
    if wanted is not None and settled != wanted:
        raise PeerError(f"the {peer.label} reports an audio link using "
                        f"{settled}, not the {wanted} both boards were told to "
                        f"ask for ({length} bytes a packet)")


def _audio(acceptor, acceptor_shell, initiator, initiator_shell, command, *,
           packet_type, min_frames, min_seconds, timeout, out, settle_s) -> int:
    # The initiator first wherever both boards are worked through: it is
    # the board that paged, so it is the one whose end of the link goes
    # first when the session is taken apart.
    boards = ((initiator, initiator_shell), (acceptor, acceptor_shell))
    released: set[str] = set()
    # Both boards are released on every way out of this function,
    # including a cancellation or a serial error, which is not a
    # PeerError. Which boards that has been done for is tracked by name,
    # so the ordinary path - where the release happens after the
    # measurements - cannot release one twice, and an interruption part way
    # through that loop still releases the board it had not reached.
    acceptor_shell.wait_ready()
    initiator_shell.wait_ready()
    _ask_for_framing(out, boards)
    acceptor_status = check_status(acceptor_shell.status(), acceptor.label,
                                  acceptor.address)
    initiator_status = check_status(initiator_shell.status(), initiator.label,
                                    initiator.address)
    initiator_shell.confirm_framing(initiator_status["fw"])
    acceptor_shell.confirm_framing(acceptor_status["fw"])
    if acceptor_status["address"] == initiator_status["address"]:
        raise PeerError("both bridges lead to one board: they report the same "
                        f"BD_ADDR {acceptor_status['address']}")
    for peer_status, peer in ((acceptor_status, acceptor),
                              (initiator_status, initiator)):
        check_firmware(peer_status["fw"], peer.label, AUDIO_FIRMWARE)
    _report(out, acceptor.label, acceptor_status)
    _report(out, initiator.label, initiator_status)

    def drain() -> None:
        for _peer, shell in boards:
            shell.drain()
    # The initiator pages, so it is the central and its LAP is on the air.
    print(f"central LAP: {initiator_status['lap']}", file=out)
    print(f"central UAP: {initiator_status['uap']}", file=out)
    print(f"audio: {packet_type} packets", file=out)
    try:
        # Each board states the packet types it will allow and the link uses
        # one both allowed, so the type goes to both. The hands-free unit
        # has to be listening before the gateway pages it, so the acceptor
        # starts its profile first; `sco start` there only waits.
        #
        # The initiator is told its type after `role`, which is the order
        # the bench uses. `role` takes an audio link down but keeps the
        # chosen type (only `defaults` puts that back), so either order
        # works; this one also works if that ever changes.
        if packet_type != AUDIO_TYPE_STACK:
            acceptor_shell.command(f"{SCO_TYPE} {packet_type}")
        acceptor_shell.command(SCO_START, AUDIO_CONNECT_TIMEOUT_S)
        initiator_shell.command(ROLE_INITIATOR, ROLE_TIMEOUT_S)
        initiator_shell.command(f"spp peer {acceptor_status['address']}")
        if packet_type != AUDIO_TYPE_STACK:
            initiator_shell.command(f"{SCO_TYPE} {packet_type}")
        initiator_shell.command(SCO_START, AUDIO_CONNECT_TIMEOUT_S)
        # Both ends have to report the link, not just the one that paged.
        # The audio link is the Hands-Free Profile's, not the Serial Port
        # Profile's, so `remote` stays empty here: whose link it is, is
        # fixed by the address the initiator was given to page.
        #
        # The settle comes first: the far end learns of the link through a
        # callback of its own, so reading it the instant `sco start` returns
        # can catch it before it has caught up. It is read again after
        # another settle if it has not.
        time.sleep(settle_s)
        linked = initiator_shell.status()
        _require_audio_up(linked, initiator, acceptor_status["address"])
        # The far end is read once more if it has not caught up: it learns
        # of the link through a callback of its own.
        far = acceptor_shell.status()
        if far.get("sco") != AUDIO_ON:
            time.sleep(settle_s)
            far = acceptor_shell.status()
        _require_audio_up(far, acceptor, initiator_status["address"])
        # What each board says its link is, kept to compare with after the
        # command. The reading rather than the type that was asked for: with
        # `default` no particular type was asked for, and the link still has
        # to be the same one at the end as at the start.
        settled = {}
        for peer_status, peer in ((linked, initiator), (far, acceptor)):
            _require_audio_type(peer_status, peer, packet_type)
            settled[peer.label] = (peer_status["sco_link"],
                                   peer_status["sco_pkt_type"])
        print(f"audio link: {_audio_field(linked, 'sco_link', str, initiator.label)} "
              f"{_audio_field(linked, 'sco_pkt_type', str, initiator.label)}, "
              f"{_audio_field(linked, 'sco_tx_len', int, initiator.label)} bytes "
              f"every {_audio_field(linked, 'sco_interval', int, initiator.label)} "
              f"slots, up to {acceptor_status['address']} at "
              f"{time.strftime('%H:%M:%SZ', time.gmtime())}", file=out)
        # The readings the command is measured against, taken as late as
        # possible before it starts. Everything up to here - the bring-up,
        # the settle, the status reads - put frames on the air as well, and
        # those are not frames the command saw.
        before, problems = {}, []
        for peer, shell in boards:
            before[peer.label], missing = _reading(shell, peer)
            if missing:
                # Before the command there is no session to salvage: a
                # board that cannot count its own link's packets cannot be
                # measured over the capture either.
                raise PeerError(missing)
        print("command: " + " ".join(command), file=out)
        out.flush()
        status, seconds = _run_command(command, timeout, drain)
        print(f"command exit: {status} after {seconds:.1f} s", file=out)

        # A board that went away under the capture explains every figure
        # below it, so it is named before them (#311). The drain first, for
        # whatever the boards said that the ones inside the command have
        # not read yet.
        drain()
        restarts = _restart_problems(out, boards)
        problems += restarts

        # Nothing below raises until the end. A session that measured
        # something reports all of it, and a stop or a release that fails
        # afterwards is added to that report rather than taking its place
        # (#264).
        after = {}
        for peer, shell in boards:
            try:
                after[peer.label], missing = _reading(shell, peer)
                if missing:
                    problems.append(missing)
            except PeerError as error:
                problems.append(str(error))
        problems += _audio_measured(out, boards, before, after,
                                    min_frames=min_frames)
        # The link the command ran against has to still be the one it was
        # given: one renegotiated part way through would otherwise pass on
        # having been right when it came up. A link that is no longer up at
        # all was reported just above.
        for peer, _shell in boards:
            end = after.get(peer.label)
            if end is None or end["status"].get("sco") != AUDIO_ON:
                continue
            # settled holds both boards: it is filled before the command
            # runs, from the same two.
            was = settled[peer.label]
            now = (end["status"].get("sco_link"),
                   end["status"].get("sco_pkt_type"))
            if now == was:
                continue
            if AUDIO_UNNAMED in now or now[1] is None:
                # The board says the link is up but cannot read it back, so
                # it cannot say what is on the air either way.
                problems.append(
                    f"after the command ran, the {peer.label} cannot say what "
                    f"its audio link carries (link {now[0]}, packet type "
                    f"{now[1]}), where it carried {was[0]} {was[1]} when the "
                    "command started")
            else:
                problems.append(
                    f"after the command ran, the {peer.label}'s audio link "
                    f"reads {now[0]} {now[1]}, not the {was[0]} {was[1]} it "
                    "carried when the command started: the command did not "
                    "run against one link")
        # The link comes down before the boards are released, so a stop the
        # firmware refuses is reported as itself rather than hidden by the
        # reset that would have cleared it anyway.
        for peer, shell in boards:
            try:
                shell.command(SCO_STOP, AUDIO_STOP_TIMEOUT_S)
            except Exception as error:
                # Any failure, as in _release: letting a serial error out
                # of here would throw away the measurements and the reason
                # the session failed, which is the whole point of stopping
                # the link at this moment rather than earlier.
                problems.append(f"the {peer.label} did not stop its audio "
                                f"link: {error}")
        for peer, shell in boards:
            # Marked before the attempt, not after: each board gets one
            # release, so an interruption part way through this loop does
            # not send the board it was working on through a second one.
            released.add(peer.label)
            reason = _release(shell, peer, AUDIO_RELEASE_STATE)
            if reason:
                problems.append(reason)
        if seconds < min_seconds:
            problems.append(f"{command[0]} ran {seconds:.1f} s, less than the "
                            f"{min_seconds:g} s this test needs: it stopped "
                            "early")
        _report_serial(out, boards)
        if problems:
            raise PeerError(("a peer restarted while the session ran: "
                             if restarts else "the pair did not carry audio: ")
                            + "; ".join(problems))
        print("pair carried audio: " + _audio_field(
            after[initiator.label]["status"], "sco_pkt_type", str,
            initiator.label), file=out)
        return status
    finally:
        # Whatever went wrong - a refused command, a cancellation, a bridge
        # that went away - the boards go back as far as they will go. The
        # failure that brought us here is the report, and the case's own
        # cleanup resets them again.
        for peer, shell in boards:
            if peer.label not in released:
                _release(shell, peer, AUDIO_RELEASE_STATE)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="esp32peer.py",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("identify", "status", "reset"):
        one = sub.add_parser(name)
        one.add_argument("--device", required=True,
                         help="the bridge's /dev/serial/by-id path")
        one.add_argument("--address", help="the registered BD_ADDR")
        one.add_argument("--role", default="peer", choices=ROLES + ("peer",),
                         help="what to call this board in the report")
        one.add_argument("--alias", default="",
                         help="the board's registry alias; with it, every line "
                              "to and from its shell is written to stderr "
                              "marked with that alias")
    # `stream` and `audio` both hold the pair's link up while a command
    # runs, so they take the same two boards and the same deadlines; what
    # differs is what goes on the air.
    for name in PAIR_COMMANDS:
        pair = sub.add_parser(name)
        pair.add_argument("--acceptor", required=True,
                          help="the listening board's bridge")
        pair.add_argument("--initiator", required=True,
                          help="the paging board's bridge; it is the piconet central")
        pair.add_argument("--acceptor-address",
                          help="the acceptor's registered BD_ADDR")
        pair.add_argument("--initiator-address",
                          help="the initiator's registered BD_ADDR")
        pair.add_argument("--acceptor-alias", default="",
                          help="the acceptor's registry alias, for the transcript")
        pair.add_argument("--initiator-alias", default="",
                          help="the initiator's registry alias, for the transcript")
        pair.add_argument("--min-seconds", type=float, default=0.0,
                          help="seconds the command must run for, so a capture "
                               "that stopped early is not taken for a pass")
        pair.add_argument("--timeout", type=float, default=300.0,
                          help="seconds the command may take")
        if name == "stream":
            pair.add_argument("--period", type=int, default=DEFAULT_PERIOD_MS,
                              help="ms")
            pair.add_argument("--payload", type=int, default=DEFAULT_PAYLOAD,
                              help="bytes")
            pair.add_argument("--min-writes", type=int, default=1,
                              help="writes the initiator must report afterwards")
            pair.add_argument("--map", dest="channel_map",
                              help="hold the piconet to this channel map "
                                   "(twenty hex digits) or 'off' for every "
                                   "channel; set on the initiator before the "
                                   "link comes up")
            pair.add_argument("--map-report",
                              help="write the session's maps here as JSON, for "
                                   "afhmap.py")
            pair.add_argument("--command-output",
                              help="save the command's standard output here as "
                                   "well as passing it through")
            pair.add_argument("--interrupt-after", type=float,
                              help="seconds after which the command is "
                                   "interrupted, for one that runs until it is "
                                   "signalled")
        else:
            pair.add_argument("--type", dest="packet_type",
                              default=DEFAULT_AUDIO_TYPE, choices=AUDIO_TYPES,
                              help="the audio link's packet type")
            pair.add_argument("--min-frames", type=int, default=1,
                              help="audio frames each board must report sending")
    # Everything after the first -- is the command to run, untouched: its own
    # options (-U, -t, -z) must not be read as this script's.
    options = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in options:
        split = options.index("--")
        options, command = options[:split], options[split + 1:]
    args = parser.parse_args(options)
    if args.command in PAIR_COMMANDS and not command:
        parser.error(f"{args.command} needs the command to run after --")
    if args.command not in PAIR_COMMANDS and command:
        parser.error(f"{args.command} takes no command after --")
    try:
        if args.command in PAIR_COMMANDS:
            acceptor = Peer(args.acceptor, "acceptor", args.acceptor_address,
                            args.acceptor_alias)
            initiator = Peer(args.initiator, "initiator", args.initiator_address,
                             args.initiator_alias)
            if args.command == "audio":
                return audio(acceptor, initiator, command,
                             packet_type=args.packet_type,
                             min_frames=args.min_frames,
                             min_seconds=args.min_seconds, timeout=args.timeout)
            return stream(
                acceptor, initiator, command, period=args.period,
                payload=args.payload, min_writes=args.min_writes,
                min_seconds=args.min_seconds, timeout=args.timeout,
                wanted_map=args.channel_map, map_report=args.map_report,
                command_output=args.command_output,
                interrupt_after=args.interrupt_after)
        peer = Peer(args.device, args.role, args.address, args.alias)
        if args.command == "identify":
            identify(peer)
        elif args.command == "reset":
            reset(peer)
        else:
            status_command(peer)
    except PeerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
