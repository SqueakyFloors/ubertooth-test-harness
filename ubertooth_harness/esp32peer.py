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
                                 -- COMMAND [ARGUMENT ...]
    python esp32peer.py audio    --acceptor PATH --initiator PATH
                                 [--type ev3|2ev3|default]
                                 [--min-frames N] [--min-packets N]
                                 [--timeout S]
                                 -- COMMAND [ARGUMENT ...]

**Opening the bridge resets the board.** DTR and RTS are wired to the chip's
EN and IO0 pins on these dev boards, and the kernel asserts both when the
first process opens the port, whatever pyserial then asks for. So anything
the board was told in an earlier invocation is gone, and a two-board session
cannot be built out of several commands the way the DK's is.

``stream`` and ``audio`` are the answer to that: each holds both ports open
for one whole session, brings a link up between the two boards, runs
``COMMAND`` while it carries traffic, takes it down, and reports what the two
boards did. The command they run is the thing under test
(``ubertooth-rx ...``), so its standard output is passed straight through;
this module's own report goes on standard error, as ``key: value`` lines for
the cases' patterns.

``stream`` carries data over the Serial Port Profile, in the polled slots an
ACL link uses. ``audio`` carries voice over a synchronous (eSCO) link, in
reserved slots, which is a different thing on the air and in a decoder.
``--type ev3`` asks for a link whose packets carry their payload at the basic
rate, which a basic-rate decoder can read; the stack's own choice, 2-EV3,
carries it as EDR.

The initiator pages, so it is the piconet central and its LAP is the one on
the air. ``central LAP`` and ``central UAP`` name it, which is what
``ubertooth-rx -l`` and ``-u`` take.

Every command fails closed: a port that cannot be opened, a board that does
not answer, an ``ERR`` reply, firmware of another version, an address that is
not the registered one, a link that did not come up or did not stay up, a
link of a packet type other than the one asked for, or a pair that sent
nothing prints ``error: <why>`` on stderr and exits 1. Exit status 2 is a
usage error. Otherwise ``stream`` and ``audio`` exit with the status of the
command they ran.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

if __package__ in (None, ""):
    # Run as a script: make the package importable for the shell-line format.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness import targets  # noqa: E402

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
# The audio link's commands and its status fields came later than the rest of
# the shell, and the packet-type choice later still, so an audio session asks
# for a newer image than the other commands do. A board on an older 1.x image
# answers every other command as before.
MINIMUM_AUDIO_FIRMWARE = "utesp-1.4.0"
BAUD = 115200
ROLES = ("acceptor", "initiator")
BOOT_ROLE = "acceptor"
# The commands that hold both boards for one session and run a command inside
# it; the rest take one board and no command.
SESSIONS = ("stream", "audio")
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

# The synchronous packet type the next audio link asks for. `default` leaves
# the stack's own table to choose, which picks 2-EV3.
SCO_TYPES = ("default", "ev3", "2ev3")
DEFAULT_SCO_TYPE = "default"
# What `sco_pkt_type` reads once a link of each asked-for type is up, so a
# link that settled on something else is caught. `default` is left out: two
# types could carry its packet length, so there is nothing to insist on.
SCO_TYPE_ON_LINK = {"ev3": "ev3", "2ev3": "2-ev3"}
# What `status` reports while an audio link is up, and the kind of link this
# session carries voice over: both EV3 and 2-EV3 are eSCO packet types.
SCO_UP = "on"
ESCO = "esco"
# The notice a board sends the moment an audio link goes down.
SCO_DISCONNECTED = "sco disconnected"
# One Bluetooth slot, which is what sco_interval and sco_retrans_window count.
SLOT_US = 625

COMMAND_TIMEOUT_S = 5.0
# The audio commands block the shell while the controller works, and the
# firmware's README gives the worst case of each: about 32 s for `sco start`
# on the initiator, which starts the profile, pages, brings the service level
# connection up, opens the link, and tidies up after a failure; about 10 s for
# `sco stop`; about 18 s for `role`, which stops the audio link, then the SPP
# side, then starts the server again. Each is rounded up so a slow worst case
# reads the firmware's own `ERR` rather than timing out here.
SCO_START_TIMEOUT_S = 40.0
SCO_STOP_TIMEOUT_S = 15.0
ROLE_TIMEOUT_S = 25.0
# How long the accepting board is given to report the link its controller has
# already opened: `sco start` answers on the board that pages, and the other
# board learns of the link from a callback of its own a moment later.
LINK_STATE_TIMEOUT_S = 5.0
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
# Time given to reading what the board said while nothing was being asked.
# The lines are already on their way, so this is a read, not a wait.
NOTICE_DRAIN_S = 0.3
# Time for the first writes to reach the air before the command starts.
SETTLE_S = 1.0
# Time an overrunning command gets to stop itself after SIGINT.
INTERRUPT_TIMEOUT_S = 10.0

_ADDRESS = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
# What the board says by itself: its own notices, ESP-IDF's warnings and
# errors, and the ROM's boot log, which appears after every reset.
_NOTICE = re.compile(r"^(?:utesp: |[WE] \(\d+\))")
_BOOT_NOISE = re.compile(
    r"^(?:ets |rst:0x|configsip:|clk_drv:|mode:|load:|entry |"
    r"[\x00-\x08\x0b-\x1f]|$)")
_READY = re.compile(r"^utesp: (?P<version>utesp-\S+) ready$")
_VERSION = re.compile(r"^utesp-(\d+)\.(\d+)\.(\d+)$")
_START_FAILED = re.compile(r"^utesp: \S+ start failed: (?P<reason>.+)$")

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

# The audio link's status fields, which only an image with the audio link
# reports, so they are checked in the audio session rather than in every
# command (firmware README, "What `status` reports").
_AUDIO_STATUS_KEYS = {
    "sco": str, "sco_profile": str, "sco_codec": str, "sco_handle": int,
    "sco_frame_len": int, "sco_tx_frames": int, "sco_tx_errors": int,
    "sco_rx_frames": int, "sco_rx_bad": int, "sco_type": str,
    "sco_link": str, "sco_pkt_type": str, "sco_tx_len": int,
    "sco_rx_len": int, "sco_interval": int, "sco_retrans_window": int,
    "sco_types_supported": str,
}

# What those fields read after a reset. A board whose audio profile is still
# running refuses the next case's `role` and `defaults`, so a release has to
# leave the profile away, not merely the link down.
AUDIO_BOOT_STATE = {
    "sco": "off", "sco_profile": "none", "sco_codec": "none",
    "sco_handle": 0, "sco_frame_len": 0, "sco_tx_frames": 0,
    "sco_tx_errors": 0, "sco_rx_frames": 0, "sco_rx_bad": 0,
    "sco_type": DEFAULT_SCO_TYPE, "sco_link": "none", "sco_pkt_type": "none",
    "sco_tx_len": 0, "sco_rx_len": 0, "sco_interval": 0,
    "sco_retrans_window": 0,
}

# The controller's own packet counters, which `sco stats` answers with.
_SCO_STATS_KEYS = {
    "rx_total": int, "rx_correct": int, "rx_err": int, "rx_none": int,
    "rx_lost": int, "tx_total": int, "tx_discarded": int,
}


class PeerError(RuntimeError):
    """The peer, its bridge, or the pair did not do what was asked."""


class PeerRefused(PeerError):
    """The board answered a command with ``ERR``, and why.

    Its own answer, as against a silence, a timeout, or something it said
    that could not be read: a caller that can carry on without what it asked
    for can tell the board's refusal from a bridge or a driver that is lost.
    """


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


def packet_type(value) -> str:
    name = str(value).strip().lower()
    if name not in SCO_TYPES:
        raise PeerError("the audio packet type must be one of "
                        + ", ".join(SCO_TYPES) + f", got {value!r}")
    return name


# --------------------------------------------------------------------------
# Shell


class Shell:
    """Line protocol: one command line out; data lines, then OK or ERR <why>.

    The board also sends notices of its own at any time (``utesp: ...``), and
    the ROM prints a boot log after every reset. Both are skipped here, so a
    caller sees only the answer to what it asked.
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
                yield raw.decode("utf-8", "replace").strip("\r \t\x00")
            if self.clock() >= deadline:
                return
            chunk = self.port.read(256)
            if chunk:
                self._buffer += chunk

    def _note(self, line: str) -> None:
        self.notices.append(line)

    def _keep_notices(self) -> None:
        """File any notice already read but not yet looked at.

        A board's notice can arrive in the same chunk as the answer to the
        last command, after its `OK`, and nothing has looked at it. Clearing
        the buffer without this would throw away the board's own news, which
        for an audio link is how a link that went down says so.
        """
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").strip("\r \t\x00")
            if _NOTICE.match(line):
                self._note(line)

    def command(self, text: str, timeout: float = COMMAND_TIMEOUT_S) -> list[str]:
        """Send one command; return its data lines. ERR or silence raises."""
        self._keep_notices()
        self.port.reset_input_buffer()
        self._buffer = b""
        self.port.write(text.encode("ascii") + b"\n")
        self.port.flush()
        if self.log is not None:
            self.log(targets.SENT, text)
        data = []
        for line in self._lines(self.clock() + timeout):
            if line and self.log is not None:
                self.log(targets.RECEIVED, line)
            if _NOTICE.match(line):
                self._note(line)
                continue
            if _BOOT_NOISE.match(line):
                continue
            if line == "OK":
                return data
            if line.startswith("ERR"):
                raise PeerRefused(
                    f"{self.label}: '{text}' refused: {line[3:].strip() or 'ERR'}")
            data.append(line)
        raise PeerError(f"{self.label}: no answer to '{text}' within {timeout:g}s"
                        + (f" (last line {data[-1]!r})" if data else ""))

    def status(self, timeout: float = COMMAND_TIMEOUT_S) -> dict:
        lines = self.command("status", timeout)
        for line in reversed(lines):
            if line.startswith("{"):
                return parse_status(line, self.label)
        raise PeerError(f"{self.label}: status printed no JSON line")

    def _pending_lines(self, deadline: float):
        """Lines already on their way, stopping as soon as there are no more.

        Unlike _lines, this does not sit out its deadline waiting for a board
        that has nothing to say: it is reading what has arrived, and every
        moment it spends is counted inside somebody's measurement.
        """
        while True:
            while b"\n" in self._buffer:
                raw, self._buffer = self._buffer.split(b"\n", 1)
                yield raw.decode("utf-8", "replace").strip("\r \t\x00")
            chunk = self.port.read(256) if self.clock() < deadline else b""
            if not chunk:
                return
            self._buffer += chunk

    def drain_notices(self, timeout: float = NOTICE_DRAIN_S) -> None:
        """Read what the board said by itself, into ``notices``.

        A command clears the input buffer before it writes, so a notice that
        arrived while nothing was being asked would be thrown away. The one
        that matters here is `utesp: sco disconnected`, which the board sends
        the moment a link goes down: during a capture, which is the whole
        point of the session, no command is in flight to carry it.

        The line is in the kernel's buffer, not this driver's, so the port is
        read for it; the timeout only bounds a board that keeps talking,
        because this returns as soon as there is nothing more to read.
        """
        for line in self._pending_lines(self.clock() + timeout):
            if line and self.log is not None:
                self.log(targets.RECEIVED, line)
            if _NOTICE.match(line):
                self._note(line)

    def sco_stats(self, timeout: float = COMMAND_TIMEOUT_S) -> dict:
        """The controller's own (e)SCO packet counters, as one JSON line."""
        lines = self.command("sco stats", timeout)
        for line in reversed(lines):
            if line.startswith("{"):
                return parse_sco_stats(line, self.label)
        raise PeerError(f"{self.label}: 'sco stats' printed no JSON line")

    def wait_ready(self, timeout: float = BOOT_TIMEOUT_S) -> None:
        """Wait for the boot notice the firmware sends once Bluetooth is up."""
        for line in self._lines(self.clock() + timeout):
            if line and self.log is not None:
                self.log(targets.RECEIVED, line)
            if _BOOT_NOISE.match(line):
                continue
            if _NOTICE.match(line):
                self._note(line)
                failed = _START_FAILED.match(line)
                if failed:
                    raise PeerError(
                        f"{self.label}: Bluetooth did not start: {failed['reason']}")
                if _READY.match(line):
                    # Which version it is, is checked with its status, where
                    # the whole state is read at once.
                    return
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
        self.port.dtr = False
        self.port.rts = True
        time.sleep(RESET_PULSE_S)
        self.port.rts = False
        self.wait_ready()


def parse_status(line: str, label: str = "peer") -> dict:
    try:
        status = json.loads(line)
    except ValueError as error:
        raise PeerError(f"{label}: status is not JSON: {line[:80]!r}") from error
    if not isinstance(status, dict):
        raise PeerError(f"{label}: status is not a JSON object")
    for key, kind in _STATUS_KEYS.items():
        value = status.get(key)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise PeerError(f"{label}: status has no valid '{key}'")
    return status


def parse_sco_stats(line: str, label: str = "peer") -> dict:
    try:
        stats = json.loads(line)
    except ValueError as error:
        raise PeerError(f"{label}: 'sco stats' is not JSON: "
                        f"{line[:80]!r}") from error
    if not isinstance(stats, dict):
        raise PeerError(f"{label}: 'sco stats' is not a JSON object")
    for key, kind in _SCO_STATS_KEYS.items():
        value = stats.get(key)
        if not isinstance(value, kind) or isinstance(value, bool):
            raise PeerError(f"{label}: 'sco stats' has no valid '{key}'")
    return stats


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


def boot_state_problems(status: dict) -> list[str]:
    """Every field of ``status`` that is not what a reset leaves behind.

    An image that has the audio link reports its state too, and that has to
    be back at boot as well: a profile left running makes the next case's
    `role` and `defaults` refuse. An older image reports none of those fields
    and is checked on the rest.
    """
    expected = dict(BOOT_STATE)
    expected.update({key: value for key, value in AUDIO_BOOT_STATE.items()
                     if key in status})
    return [f"{key} {status[key]!r}, not {value!r}"
            for key, value in expected.items() if status.get(key) != value]


def check_audio_status(status: dict, label: str) -> dict:
    """The board's image can drive an audio link and reports what it is doing.

    The version is the first thing checked, so a board on an older 1.x image
    is told what it needs rather than being reported field by field.
    """
    check_firmware(status["fw"], label, MINIMUM_AUDIO_FIRMWARE)
    for key, kind in _AUDIO_STATUS_KEYS.items():
        value = status.get(key)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise PeerError(f"{label}: status has no valid '{key}', so this image "
                            "cannot be driven through an audio link")
    return status


# --------------------------------------------------------------------------
# Reporting


def _report(out, label: str, status: dict) -> None:
    print(f"{label} firmware: {status['fw']}", file=out)
    print(f"{label} address: {status['address']}", file=out)
    print(f"{label} LAP: {status['lap']} UAP: {status['uap']}", file=out)
    print(f"{label} role: {status['role']} name: {status['name']}", file=out)


def _report_traffic(out, label: str, status: dict) -> None:
    print(f"{label} traffic: writes {status['writes']} errors "
          f"{status['write_errors']} received {status['rx_bytes']} bytes "
          f"connects {status['connects']} disconnects {status['disconnects']}",
          file=out)


def _report_link(out, status: dict) -> None:
    """The audio link's parameters, as the controllers settled on them.

    `sco_interval` and `sco_retrans_window` count Bluetooth slots, so the
    interval is given in milliseconds too. One packet per interval is the
    floor of what a capture sees, not the rate: a retransmission window puts
    more on the air when a packet has to be sent again.
    """
    print(f"audio link: {status['sco_link']} {status['sco_pkt_type']} "
          f"codec {status['sco_codec']}", file=out)
    print(f"audio packets: {status['sco_tx_len']} bytes every "
          f"{status['sco_interval']} slots "
          f"({status['sco_interval'] * SLOT_US / 1000:.2f} ms), "
          f"retransmission window {status['sco_retrans_window']} slots",
          file=out)
    print(f"audio frames: {status['sco_frame_len']} bytes over HCI", file=out)


def _report_audio_traffic(out, label: str, before: "_Counts",
                          after: "_Counts") -> None:
    """What this board counted while the command ran, not since the link came up.

    The frames are the firmware's own, counted on each side of the link; the
    packets are its controller's, which is the number a capture of the air is
    compared with.
    """
    print(f"{label} audio frames while the command ran: sent "
          f"{_grew(before, after, 'sco_tx_frames')} refused "
          f"{_grew(before, after, 'sco_tx_errors')} received "
          f"{_grew(before, after, 'sco_rx_frames')} damaged "
          f"{_grew(before, after, 'sco_rx_bad')}", file=out)
    if before.stats is None or after.stats is None:
        print(f"{label} audio packets: not counted "
              f"({after.refused or before.refused})", file=out)
        return
    moved = {key: after.stats[key] - before.stats[key] for key in _SCO_STATS_KEYS}
    print(f"{label} audio packets while the command ran: sent "
          f"{moved['tx_total']} discarded {moved['tx_discarded']} received "
          f"{moved['rx_total']} good {moved['rx_correct']} errors "
          f"{moved['rx_err']} missing {moved['rx_none']} lost "
          f"{moved['rx_lost']}", file=out)


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
        status = check_status(shell.status(), peer.label, peer.address)
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
        status = check_status(shell.status(), peer.label, peer.address)
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
        status = check_status(shell.status(), peer.label, peer.address)
    finally:
        shell.close()
    wrong = boot_state_problems(status)
    if wrong:
        raise PeerError(f"{peer.label} is not in its boot state after a reset: "
                        + ", ".join(wrong))
    _report(out, peer.label, status)
    return status


def _run_command(command: Sequence[str], timeout: float) -> tuple[int, float]:
    """Run the thing under test, inheriting our streams, with a deadline.

    The child stays in this process's group, so the harness's own deadline
    reaches it; this deadline is the backstop for a command with none of its
    own.
    """
    started = time.monotonic()
    try:
        child = subprocess.Popen(list(command))
    except OSError as error:
        raise PeerError(f"cannot run {command[0]}: {error}") from error
    try:
        return child.wait(timeout=timeout), time.monotonic() - started
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
           settle_s: float = SETTLE_S) -> int:
    """Hold the pair's link up and streaming while ``command`` runs.

    Both ports are opened first, which resets both boards, so the pair starts
    from the firmware's boot state however an earlier case left it.
    """
    out = out or sys.stderr
    period = period_ms(period)
    payload = payload_bytes(payload)
    min_writes = whole(min_writes, "the write floor")
    min_seconds = seconds(min_seconds, "the seconds the command must run for")
    timeout = seconds(timeout, "the seconds the command may take")
    return _session(
        acceptor, initiator,
        lambda acceptor_shell, initiator_shell: _stream(
            acceptor, acceptor_shell, initiator, initiator_shell, command,
            period=period, payload=payload, min_writes=min_writes,
            min_seconds=min_seconds, timeout=timeout, out=out,
            settle_s=settle_s))


def _session(acceptor: Peer, initiator: Peer, run):
    """Hold both bridges open for one whole session, which resets both boards.

    Opening a bridge resets its board, so a session cannot be built out of
    several invocations: everything it does happens between these two opens.
    """
    if acceptor.device == initiator.device:
        raise PeerError("the acceptor and the initiator are the same bridge; a "
                        "Classic link needs two boards")
    acceptor_shell = acceptor.open()
    try:
        initiator_shell = initiator.open()
        try:
            return run(acceptor_shell, initiator_shell)
        finally:
            initiator_shell.close()
    finally:
        acceptor_shell.close()


def _introduce(acceptor, acceptor_shell, initiator, initiator_shell,
               out) -> tuple[dict, dict]:
    """Read both boards, report who they are, and name the piconet's central."""
    acceptor_shell.wait_ready()
    initiator_shell.wait_ready()
    acceptor_status = check_status(acceptor_shell.status(), acceptor.label,
                                  acceptor.address)
    initiator_status = check_status(initiator_shell.status(), initiator.label,
                                   initiator.address)
    if acceptor_status["address"] == initiator_status["address"]:
        raise PeerError("both bridges lead to one board: they report the same "
                        f"BD_ADDR {acceptor_status['address']}")
    _report(out, acceptor.label, acceptor_status)
    _report(out, initiator.label, initiator_status)
    # The initiator pages, so it is the central and its LAP is on the air.
    print(f"central LAP: {initiator_status['lap']}", file=out)
    print(f"central UAP: {initiator_status['uap']}", file=out)
    return acceptor_status, initiator_status


def _release(shell: Shell, label: str = "peer", out=None) -> str:
    """Leave a board in its boot state, whatever the session did to it.

    Returns why it could not, or "". It does not raise: the caller may
    already be reporting what went wrong, and it decides what a board left
    behind costs. Either way it is said aloud, because a board whose audio
    profile is still running refuses the next session's `role` and
    `defaults`, and a reader would otherwise have no sign of it.
    """
    out = out or sys.stderr
    try:
        shell.reset()
        status = shell.status()
    except Exception as error:  # a bridge can fail here as well as a board
        print(f"note: the {label} was not released: {error}", file=out)
        return str(error)
    wrong = boot_state_problems(status)
    if not wrong:
        return ""
    print(f"note: the {label} is not in its boot state after its reset: "
          + ", ".join(wrong), file=out)
    return "it is not in its boot state after its reset: " + ", ".join(wrong)


def whole(value, what: str) -> int:
    """A floor cannot be negative: a negative one would check nothing."""
    number = int(value)
    if number < 0:
        raise PeerError(f"{what} cannot be negative, got {value!r}")
    return number


def seconds(value, what: str) -> float:
    number = float(value)
    if number < 0:
        raise PeerError(f"{what} cannot be negative, got {value!r}")
    return number


def _stream(acceptor, acceptor_shell, initiator, initiator_shell, command, *,
            period, payload, min_writes, min_seconds, timeout, out,
            settle_s) -> int:
    acceptor_status, _ = _introduce(acceptor, acceptor_shell, initiator,
                                    initiator_shell, out)
    print(f"stream: {payload} bytes every {period} ms", file=out)
    try:
        # `role` takes an audio link and its profile down before it changes
        # anything, which the firmware allows about 18 s for, so it gets the
        # same deadline here as in an audio session.
        initiator_shell.command("role initiator", ROLE_TIMEOUT_S)
        initiator_shell.command(f"spp peer {acceptor_status['address']}")
        initiator_shell.command(f"stream period {period}")
        initiator_shell.command(f"stream payload {payload}")
        initiator_shell.command("spp start", CONNECT_TIMEOUT_S)
        initiator_shell.command("stream on")
        linked = initiator_shell.status()
        if not linked["connected"] or linked["remote"] != acceptor_status["address"]:
            raise PeerError(
                f"the link is not up to {acceptor_status['address']} after 'spp "
                f"start' (remote {linked['remote'] or 'none'})")
        print(f"link: up to {linked['remote']} at "
              f"{time.strftime('%H:%M:%SZ', time.gmtime())}", file=out)
        time.sleep(settle_s)
        print("command: " + " ".join(command), file=out)
        out.flush()
        status, seconds = _run_command(command, timeout)
        print(f"command exit: {status} after {seconds:.1f} s", file=out)
        initiator_shell.command("stream off")
        after_initiator = initiator_shell.status()
        after_acceptor = acceptor_shell.status()
    finally:
        _release(initiator_shell, initiator.label, out)
        _release(acceptor_shell, acceptor.label, out)
    _report_traffic(out, initiator.label, after_initiator)
    _report_traffic(out, acceptor.label, after_acceptor)
    problems = []
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
    if problems:
        raise PeerError("the pair did not stream: " + "; ".join(problems))
    print("pair streamed: yes", file=out)
    return status


def audio(acceptor: Peer, initiator: Peer, command: Sequence[str], *,
          sco_type: str = DEFAULT_SCO_TYPE, min_frames: int = 1,
          min_packets: int = 1, min_seconds: float = 0.0,
          timeout: float = 300.0, out=None,
          settle_s: float = SETTLE_S,
          link_timeout: float = LINK_STATE_TIMEOUT_S) -> int:
    """Hold a synchronous (eSCO) audio link up between the pair while ``command``
    runs.

    A synchronous link carries voice in reserved slots rather than the polled
    slots an ACL link uses, so it is a different thing on the air from what
    ``stream`` makes. ``sco_type`` asks for the packet type: ``ev3`` for a
    link a basic-rate decoder can read, ``2ev3`` for the EDR type the stack
    picks by itself, or ``default`` to leave the choice to the stack.

    Both ports are opened first, which resets both boards, so the pair starts
    from the firmware's boot state however an earlier case left it.
    """
    out = out or sys.stderr
    wanted = packet_type(sco_type)
    min_frames = whole(min_frames, "the frame floor")
    min_packets = whole(min_packets, "the packet floor")
    min_seconds = seconds(min_seconds, "the seconds the command must run for")
    timeout = seconds(timeout, "the seconds the command may take")
    return _session(
        acceptor, initiator,
        lambda acceptor_shell, initiator_shell: _audio(
            acceptor, acceptor_shell, initiator, initiator_shell, command,
            sco_type=wanted, min_frames=min_frames, min_packets=min_packets,
            min_seconds=min_seconds, timeout=timeout, out=out,
            settle_s=settle_s, link_timeout=link_timeout))


def _audio(acceptor, acceptor_shell, initiator, initiator_shell, command, *,
           sco_type, min_frames, min_packets, min_seconds, timeout, out,
           settle_s, link_timeout=LINK_STATE_TIMEOUT_S) -> int:
    acceptor_status, initiator_status = _introduce(
        acceptor, acceptor_shell, initiator, initiator_shell, out)
    # Both images are checked before anything is started, so a board on an
    # older 1.x image is reported rather than refusing a command halfway
    # through bringing a link up.
    check_audio_status(acceptor_status, acceptor.label)
    check_audio_status(initiator_status, initiator.label)
    print(f"audio type asked for: {sco_type}", file=out)
    # The mask each controller supports, in the firmware's own bit order. The
    # controller masks a request against it, so a board whose controller lacks
    # the type asked for refuses the link at `sco start`: the mask is reported
    # here so that refusal can be read rather than puzzled over.
    print(f"audio types supported: {initiator.label} "
          f"{initiator_status['sco_types_supported']}, {acceptor.label} "
          f"{acceptor_status['sco_types_supported']}", file=out)
    try:
        # The initiator runs the audio gateway, which pages, so it is the
        # central here as it is for a stream. `role` stops the audio profile,
        # and `sco type` is refused while a profile is running, so the role
        # goes first and the choice after it.
        initiator_shell.command("role initiator", ROLE_TIMEOUT_S)
        if sco_type != DEFAULT_SCO_TYPE:
            # Each board states the types it allows and the link uses one
            # both allowed, so both are told. The accepting board's choice
            # reaches its controller when the far end pages, with no command
            # in flight, so it has to be set before either profile starts.
            acceptor_shell.command(f"sco type {sco_type}")
            initiator_shell.command(f"sco type {sco_type}")
        # The acceptor's hands-free profile has to be listening before the
        # gateway pages it.
        _sco_start(acceptor_shell, acceptor.label)
        initiator_shell.command(f"spp peer {acceptor_status['address']}")
        _sco_start(initiator_shell, initiator.label)
        # The initiator's `sco start` answered once the link was open, so its
        # own view is ready; the acceptor's lags that by a callback.
        linked = check_audio_status(initiator_shell.status(), initiator.label)
        far = _wait_for_link(acceptor_shell, acceptor.label, link_timeout)
        _check_link(linked, far, acceptor.label, initiator.label, sco_type)
        # The address is the one the initiator was told to page: a
        # synchronous link carries no remote address over HCI, so unlike a
        # stream's, this far end is not read back from the board.
        print(f"audio link: up at {time.strftime('%H:%M:%SZ', time.gmtime())}, "
              f"paged {acceptor_status['address']}", file=out)
        _report_link(out, linked)
        time.sleep(settle_s)
        # Every counter here counts from the moment the link came up, so the
        # run's own numbers are the difference across the command: bringing
        # the link up, waiting for the far board, and settling have already
        # put frames on the air, about 270 of them on an EV3 link.
        before_initiator = _read_counts(initiator_shell, initiator.label)
        before_acceptor = _read_counts(acceptor_shell, acceptor.label)
        print("command: " + " ".join(command), file=out)
        out.flush()
        # Everything either board says from here on belongs to the command's
        # own window. Anything said before it is read first, so a notice
        # still sitting in a buffer is not laid at the command's door.
        initiator_shell.drain_notices()
        acceptor_shell.drain_notices()
        marks = {initiator.label: len(initiator_shell.notices),
                 acceptor.label: len(acceptor_shell.notices)}
        status, seconds = _run_command(command, timeout)
        print(f"command exit: {status} after {seconds:.1f} s", file=out)
        # Read what they said before the next command clears the buffer:
        # `utesp: sco disconnected` is how a link that went down announces
        # itself, and during a capture nothing is in flight to carry it.
        initiator_shell.drain_notices()
        acceptor_shell.drain_notices()
        # Read the counters again while the link is still up: the
        # controller's own belong to a live link, and `sco stop` ends it.
        after_initiator = _read_counts(initiator_shell, initiator.label)
        after_acceptor = _read_counts(acceptor_shell, acceptor.label)
        # Taken after those reads, so a notice that arrived as they were
        # made counts too: the window ends with them, not before them.
        said = {label: shell.notices[marks[label]:]
                for label, shell in ((initiator.label, initiator_shell),
                                     (acceptor.label, acceptor_shell))}
        stopped = ""
        if after_initiator.status["sco"] == SCO_UP:
            try:
                initiator_shell.command("sco stop", SCO_STOP_TIMEOUT_S)
            except PeerError as error:
                # Reported with everything else rather than thrown here: the
                # counters are already in hand and are the run's result.
                stopped = str(error)
    finally:
        # A board left with its audio profile running cannot run the next
        # session, so failing to release one fails this one.
        left = {initiator.label: _release(initiator_shell, initiator.label, out),
                acceptor.label: _release(acceptor_shell, acceptor.label, out)}
    sides = ((initiator.label, before_initiator, after_initiator),
             (acceptor.label, before_acceptor, after_acceptor))
    for label, before, after in sides:
        _report_audio_traffic(out, label, before, after)
        for notice in said.get(label, ()):
            print(f"{label} said: {notice}", file=out)
    problems, notes = [], []
    if stopped:
        problems.append(f"the audio link would not stop: {stopped}")
    for label, reason in left.items():
        if reason:
            problems.append(f"the {label} was not left ready for the next "
                            f"session: {reason}")
    for label, before, after in sides:
        # A link that is down is that, not a link that came back: the
        # firmware zeroes the handle when it goes, so the flap check below
        # would describe the same failure wrongly.
        if after.status["sco"] != SCO_UP:
            problems.append(f"the {label} reports the audio link "
                            f"{after.status['sco']}, not up, after the command "
                            "ran")
            continue
        # A link that went down and came back carries its frame counters
        # over, so the floors below would pass on the traffic of two links.
        # The board's own notice is the reliable sign of it; a new handle, or
        # a controller's count that started again, catch what it misses.
        dropped = [notice for notice in said.get(label, ())
                   if SCO_DISCONNECTED in notice]
        if dropped:
            problems.append(f"the {label} said the audio link went down while "
                            "the command ran, so what it carried is not one "
                            "link's traffic")
        elif after.status["sco_handle"] != before.status["sco_handle"]:
            problems.append(
                f"the {label}'s audio link went down and came back while the "
                f"command ran (handle {before.status['sco_handle']}, then "
                f"{after.status['sco_handle']})")
        elif _grew(before, after, "sco_rx_frames") < 0 or (
                before.stats is not None and after.stats is not None
                and after.stats["tx_total"] < before.stats["tx_total"]):
            problems.append(f"the {label}'s counters went backwards while the "
                            "command ran: either this is not the link they "
                            "were first read from, or this controller's "
                            "counters do not add up across reads, and a "
                            "difference means nothing on it")
        # Each side answers every frame it receives with one of its own, so
        # both ends receiving frames is what shows the link carried audio
        # while the command ran, rather than only before it.
        received = _grew(before, after, "sco_rx_frames")
        if received < min_frames:
            problems.append(f"the {label} received {received} frames while the "
                            f"command ran, fewer than the {min_frames} this "
                            "test needs")
        # The controller's packet count is the number a capture of the air is
        # compared with, so a run whose capture found far fewer has something
        # to answer for.
        if before.stats is None or after.stats is None:
            notes.append(f"note: the {label}'s controller packets were not "
                         f"counted, so the {min_packets}-packet floor was not "
                         "checked")
        else:
            sent = after.stats["tx_total"] - before.stats["tx_total"]
            if sent < min_packets:
                problems.append(f"the {label}'s controller sent {sent} packets "
                                f"while the command ran, fewer than the "
                                f"{min_packets} this test needs")
    if all(after.status["sco"] == SCO_UP for _label, _before, after in sides):
        # The link the command ran against has to be the link it was given: a
        # link that came back as another type, or as plain SCO, part way
        # through would otherwise pass on having been right at the start.
        try:
            _check_link(after_initiator.status, after_acceptor.status,
                        acceptor.label, initiator.label, sco_type)
        except PeerError as error:
            problems.append(f"after the command ran, {error}")
    if seconds < min_seconds:
        problems.append(f"{command[0]} ran {seconds:.1f} s, less than the "
                        f"{min_seconds:g} s this test needs: it stopped early")
    for note in notes:
        print(note, file=out)
    if problems:
        raise PeerError("the pair did not carry audio: " + "; ".join(problems))
    print("pair carried audio: yes", file=out)
    return status


@dataclass
class _Counts:
    """One board's counters at a moment, with why the controller's are missing.

    ``stats`` is None when the board refused the packet counters, whatever its
    reason: no link up, a request of its own that went unanswered, or a
    controller that would not give them. That is a fact about the run, not a
    reason to abandon it: these counters say nothing about the link that the
    frame counters do not, and the likeliest cause, a link that dropped while
    the command ran, has a message of its own.
    """

    status: dict
    stats: dict | None
    refused: str = ""


def _read_counts(shell: Shell, label: str) -> _Counts:
    status = check_audio_status(shell.status(), label)
    try:
        return _Counts(status, shell.sco_stats())
    except PeerRefused as error:
        # Only the board's own refusal is carried on from, whichever of its
        # reasons it gives. A silence, a timeout, or an answer that cannot be
        # read is the bridge or the driver being lost, which no floor should
        # be waived for.
        return _Counts(status, None, str(error))


def _grew(before: _Counts, after: _Counts, key: str) -> int:
    return after.status[key] - before.status[key]


def _sco_start(shell: Shell, label: str) -> None:
    """Start this board's side of the audio link, with what it said if it fails.

    A start that fails tries to put the profile away, and says so in a notice
    of its own if it could not: the answer is why the start failed, and the
    notice is the reason the next `role` or `defaults` will refuse. Both
    belong in the error, because nothing later in the session reports it.
    """
    seen = len(shell.notices)
    try:
        shell.command("sco start", SCO_START_TIMEOUT_S)
    except PeerError as error:
        # The notice arrives beside the refusal, so it may still be unread.
        shell.drain_notices()
        said = shell.notices[seen:]
        if said:
            raise PeerError(f"{error}; the {label} said: "
                            + "; ".join(said)) from None
        raise


def _wait_for_link(shell: Shell, label: str,
                   timeout: float = LINK_STATE_TIMEOUT_S) -> dict:
    """This board's view of the audio link, once its own stack has caught up.

    Read again rather than failing the first time: the board that answered the
    page learns the link is up from a callback, which can land after the
    paging board's command has already answered.
    """
    deadline = time.monotonic() + timeout
    while True:
        status = check_audio_status(shell.status(), label)
        if status["sco"] == SCO_UP or time.monotonic() >= deadline:
            return status
        time.sleep(POLL_S)


def _check_link(linked: dict, far: dict, acceptor_label: str,
                initiator_label: str, sco_type: str) -> None:
    """Both ends have the link up, and it is of the type that was asked for."""
    for label, status in ((initiator_label, linked), (acceptor_label, far)):
        if status["sco"] != SCO_UP:
            raise PeerError(
                f"the audio link is not up after 'sco start': the {label} "
                f"reports sco {status['sco']} (profile {status['sco_profile']})")
    # Both ends are checked, not only the board that paged: each works its
    # link type, packet type, and frame size out from what its own controller
    # told it, and the accepting board's `sco start` answers before its link
    # exists, so its own view is the only sign it really has one.
    for label, status in ((initiator_label, linked), (acceptor_label, far)):
        if status["sco_type"] != sco_type:
            # What the board says it will ask for, against what it was told.
            # With `default` that is the only thing to check: the choice is
            # the stack's, so no particular type on the link is promised.
            raise PeerError(
                f"the {label} says it asks for {status['sco_type']} links, not "
                f"the {sco_type} it was told to")
        if status["sco_handle"] == 0 or status["sco_frame_len"] == 0:
            raise PeerError(
                f"the {label} reports the audio link up with no connection "
                f"handle or frame size (handle {status['sco_handle']}, frame "
                f"{status['sco_frame_len']} bytes)")
        if status["sco_link"] != ESCO:
            # Every name this session goes by says eSCO, and an EV3 or 2-EV3
            # packet type is an eSCO one. A plain SCO link reported as a pass
            # would put something else on the air than the test asked for.
            raise PeerError(
                f"the {label} reports a {status['sco_link']} link, not {ESCO}: "
                f"this session carries voice over an {ESCO} link")
        on_link = SCO_TYPE_ON_LINK.get(sco_type)
        if on_link is not None and status["sco_pkt_type"] != on_link:
            # A type both boards were told to ask for, and only that type, so
            # a link of another type means the request did not reach the air;
            # a decode test reading basic-rate payloads would get EDR.
            raise PeerError(
                f"the {label} reports an audio link using "
                f"{status['sco_pkt_type']}, not the {on_link} both boards "
                f"asked for ({status['sco_tx_len']} bytes every "
                f"{status['sco_interval']} slots)")


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
    # Both two-board sessions name the pair the same way; each adds the
    # options of the traffic it makes.
    both = argparse.ArgumentParser(add_help=False)
    both.add_argument("--acceptor", required=True, help="the listening board's bridge")
    both.add_argument("--initiator", required=True,
                      help="the paging board's bridge; it is the piconet central")
    both.add_argument("--acceptor-address", help="the acceptor's registered BD_ADDR")
    both.add_argument("--initiator-address", help="the initiator's registered BD_ADDR")
    both.add_argument("--acceptor-alias", default="",
                      help="the acceptor's registry alias, for the transcript")
    both.add_argument("--initiator-alias", default="",
                      help="the initiator's registry alias, for the transcript")
    both.add_argument("--min-seconds", type=float, default=0.0,
                      help="seconds the command must run for, so a capture "
                           "that stopped early is not taken for a pass")
    both.add_argument("--timeout", type=float, default=300.0,
                      help="seconds the command may take")
    pair = sub.add_parser("stream", parents=[both])
    pair.add_argument("--period", type=int, default=DEFAULT_PERIOD_MS, help="ms")
    pair.add_argument("--payload", type=int, default=DEFAULT_PAYLOAD, help="bytes")
    pair.add_argument("--min-writes", type=int, default=1,
                      help="writes the initiator must report afterwards")
    voice = sub.add_parser("audio", parents=[both])
    voice.add_argument("--type", dest="sco_type", default=DEFAULT_SCO_TYPE,
                       choices=SCO_TYPES,
                       help="the synchronous packet type the link asks for; "
                            "ev3 carries its payload at the basic rate")
    voice.add_argument("--min-frames", type=int, default=1,
                       help="frames each board must report receiving")
    voice.add_argument("--min-packets", type=int, default=1,
                       help="packets each controller must report sending")
    # Everything after the first -- is the command to run, untouched: its own
    # options (-U, -t, -z) must not be read as this script's.
    options = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in options:
        split = options.index("--")
        options, command = options[:split], options[split + 1:]
    args = parser.parse_args(options)
    if args.command in SESSIONS and not command:
        parser.error(f"{args.command} needs the command to run after --")
    if args.command not in SESSIONS and command:
        parser.error(f"{args.command} takes no command after --")
    try:
        if args.command in SESSIONS:
            acceptor = Peer(args.acceptor, "acceptor", args.acceptor_address,
                            args.acceptor_alias)
            initiator = Peer(args.initiator, "initiator",
                             args.initiator_address, args.initiator_alias)
            if args.command == "audio":
                return audio(acceptor, initiator, command,
                             sco_type=args.sco_type,
                             min_frames=args.min_frames,
                             min_packets=args.min_packets,
                             min_seconds=args.min_seconds,
                             timeout=args.timeout)
            return stream(acceptor, initiator, command, period=args.period,
                          payload=args.payload, min_writes=args.min_writes,
                          min_seconds=args.min_seconds, timeout=args.timeout)
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
    except KeyboardInterrupt:
        # The boards have been released by the session's own cleanup; this is
        # only to report it as a line rather than a traceback.
        print("error: interrupted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
