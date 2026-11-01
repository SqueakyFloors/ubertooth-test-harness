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
from dataclasses import dataclass
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
# The oldest image whose shell can choose the audio link's packet type
# (`sco type`); the audio link itself arrived in utesp-1.1.0.
AUDIO_FIRMWARE = "utesp-1.4.0"
# What `status` must report for the link to be up and carrying audio.
AUDIO_ON = "on"
# `sco start` on the initiator blocks the shell while the controller starts
# the hands-free profile (3 s), pages and brings the service level connection
# up (15 s), opens the audio link (10 s), and, if that failed, puts the
# profile back (3.5 s). The firmware's README asks for 40 seconds, so that a
# bring-up that failed answers with the firmware's own reason rather than
# timing out here, and a slow one is not cut off mid-command. A link to a
# board that is awake takes four or five seconds.
AUDIO_CONNECT_TIMEOUT_S = 40.0

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


class PeerError(RuntimeError):
    """The peer, its bridge, or the pair did not do what was asked."""


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

    def command(self, text: str, timeout: float = COMMAND_TIMEOUT_S) -> list[str]:
        """Send one command; return its data lines. ERR or silence raises."""
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
                raise PeerError(
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
    tool stops its board and closes its files on it. Nothing here raises,
    because every caller is already reporting something that went wrong.
    """
    if child.poll() is not None:
        return
    try:
        child.send_signal(signal.SIGINT)
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
    except OSError:
        pass


def _run_command_watched(command: Sequence[str], *, timeout: float,
                         interrupt_after: float | None = None,
                         output_path: str | None = None,
                         sample_at: float | None = None,
                         sample: Callable[[float], None] | None = None,
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
                             sample_at=sample_at, sample=sample)
        except BaseException:
            # A sample that raised, or a cancellation: the command goes
            # first, so nothing is left holding the board.
            _stop_child(child)
            drained()
            raise
        if overran:
            _stop_child(child)
            drained()
            raise PeerError(f"{command[0]} did not finish within {timeout:g}s")
        status = child.wait()
        drained()
        return status, time.monotonic() - started
    finally:
        if saved is not None:
            saved.close()


def _watch(child, out, *, started: float, timeout: float,
           interrupt_after: float | None, sample_at: float | None,
           sample: Callable[[float], None] | None) -> bool:
    """Wait for the command, sampling and interrupting on time.

    True when it was still running at its deadline, which is the caller's to
    report; False when it ended by itself or at the interrupt.
    """
    deadline = started + timeout
    stop_at = None if interrupt_after is None else started + interrupt_after
    due = None if sample_at is None or sample is None else started + sample_at
    while child.poll() is None:
        now = time.monotonic()
        if due is not None and now >= due:
            due = None
            sample(now - started)
            continue
        if stop_at is not None and now >= stop_at:
            stop_at = None
            print(f"command interrupted after {now - started:.1f} s", file=out)
            out.flush()
            child.send_signal(signal.SIGINT)
            # From the interrupt, the command gets the same grace as an
            # overrunning one: long enough to stop its board and close its
            # files, not long enough to hold the boards for a run that is
            # already over.
            deadline = min(deadline, time.monotonic() + INTERRUPT_TIMEOUT_S)
            continue
        if now >= deadline:
            return True
        time.sleep(POLL_S)
    return False


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


def _release(shell: Shell) -> None:
    """Leave a board in its boot state, whatever the session did to it.

    Failures here are swallowed: the caller is already reporting what went
    wrong, and the case's cleanup resets both boards in any event.
    """
    try:
        shell.reset()
    except PeerError:
        pass


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


def _stream(acceptor, acceptor_shell, initiator, initiator_shell, command, *,
            period, payload, min_writes, min_seconds, timeout, out,
            settle_s, wanted_map=None, map_report=None, command_output=None,
            interrupt_after=None) -> int:
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
    print(f"stream: {payload} bytes every {period} ms", file=out)
    samples: list[dict] = []
    try:
        initiator_shell.command("role initiator")
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
        print(f"link: up to {linked['remote']} at "
              f"{time.strftime('%H:%M:%SZ', time.gmtime())}", file=out)
        time.sleep(settle_s)
        if wanted_map is not None:
            samples.append(read_link_map(initiator_shell, initiator.label,
                                         SAMPLE_POINTS[0], 0.0))
        print("command: " + " ".join(command), file=out)
        out.flush()
        if wanted_map is None and command_output is None and interrupt_after is None:
            status, seconds = _run_command(command, timeout)
        else:
            def middle(elapsed: float) -> None:
                samples.append(read_link_map(initiator_shell, initiator.label,
                                             SAMPLE_POINTS[1], elapsed))
            status, seconds = _run_command_watched(
                command, timeout=timeout, interrupt_after=interrupt_after,
                output_path=command_output,
                sample_at=None if wanted_map is None else _middle_of(
                    interrupt_after, timeout),
                sample=None if wanted_map is None else middle, out=out)
        print(f"command exit: {status} after {seconds:.1f} s", file=out)
        if wanted_map is not None:
            samples.append(read_link_map(initiator_shell, initiator.label,
                                         SAMPLE_POINTS[2], seconds))
        initiator_shell.command("stream off")
        after_initiator = initiator_shell.status()
        after_acceptor = acceptor_shell.status()
    finally:
        _release(initiator_shell)
        _release(acceptor_shell)
    if wanted_map is not None:
        requested = requested_map(wanted_map)
        _report_map(out, requested, samples)
        if map_report:
            _write_map_report(map_report, requested, samples, command, seconds)
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
    if len({sample["link"] for sample in samples}) > 1:
        # The leader's controller may classify channels again while the link
        # is up. There is then no one map the capture ran on, so the session
        # says so rather than leaving a comparison to pick a sample.
        problems.append("the link's channel map changed under the command: "
                        + "; ".join(f"{sample['at']} {sample['link']}"
                                    for sample in samples))
    if problems:
        raise PeerError("the pair did not stream: " + "; ".join(problems))
    print("pair streamed: yes", file=out)
    return status


def audio(acceptor: Peer, initiator: Peer, command: Sequence[str], *,
          packet_type: str = DEFAULT_AUDIO_TYPE, min_frames: int = 1,
          min_seconds: float = 0.0, timeout: float = 300.0, out=None,
          settle_s: float = SETTLE_S) -> int:
    """Hold a synchronous audio link up between the pair while ``command`` runs.

    The air then carries reserved-slot voice packets as well as the data the
    SPP stream puts there, and an ``ev3`` link's payload is at the basic
    rate, so a receive-only capture can decode it. As with ``stream``, both
    ports are opened first, which resets both boards.
    """
    out = out or sys.stderr
    if packet_type not in AUDIO_TYPES:
        raise PeerError("the audio packet type must be one of "
                        + ", ".join(AUDIO_TYPES))
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


def _audio_field(status: dict, key: str, kind, label: str):
    """One of the audio link's own status fields, which older images lack."""
    value = status.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise PeerError(f"{label}: status has no valid '{key}'; the audio link "
                        f"needs {AUDIO_FIRMWARE} or a later 1.x image")
    return value


def _require_audio_up(status: dict, peer: Peer, address: str) -> None:
    if _audio_field(status, "sco", str, peer.label) != AUDIO_ON:
        raise PeerError(f"the {peer.label}'s audio link to {address} is not up "
                        f"after 'sco start' (sco {status['sco']})")


def _audio(acceptor, acceptor_shell, initiator, initiator_shell, command, *,
           packet_type, min_frames, min_seconds, timeout, out, settle_s) -> int:
    acceptor_shell.wait_ready()
    initiator_shell.wait_ready()
    acceptor_status = check_status(acceptor_shell.status(), acceptor.label,
                                  acceptor.address)
    initiator_status = check_status(initiator_shell.status(), initiator.label,
                                    initiator.address)
    if acceptor_status["address"] == initiator_status["address"]:
        raise PeerError("both bridges lead to one board: they report the same "
                        f"BD_ADDR {acceptor_status['address']}")
    for peer_status, peer in ((acceptor_status, acceptor),
                              (initiator_status, initiator)):
        check_firmware(peer_status["fw"], peer.label, AUDIO_FIRMWARE)
    _report(out, acceptor.label, acceptor_status)
    _report(out, initiator.label, initiator_status)
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
        if packet_type != "default":
            acceptor_shell.command(f"sco type {packet_type}")
        acceptor_shell.command("sco start", AUDIO_CONNECT_TIMEOUT_S)
        initiator_shell.command("role initiator")
        initiator_shell.command(f"spp peer {acceptor_status['address']}")
        if packet_type != "default":
            initiator_shell.command(f"sco type {packet_type}")
        initiator_shell.command("sco start", AUDIO_CONNECT_TIMEOUT_S)
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
        print(f"audio link: {_audio_field(linked, 'sco_link', str, initiator.label)} "
              f"{_audio_field(linked, 'sco_pkt_type', str, initiator.label)}, "
              f"{_audio_field(linked, 'sco_tx_len', int, initiator.label)} bytes "
              f"every {_audio_field(linked, 'sco_interval', int, initiator.label)} "
              f"slots, up to {acceptor_status['address']} at "
              f"{time.strftime('%H:%M:%SZ', time.gmtime())}", file=out)
        print("command: " + " ".join(command), file=out)
        out.flush()
        status, seconds = _run_command(command, timeout)
        print(f"command exit: {status} after {seconds:.1f} s", file=out)
        after_initiator = initiator_shell.status()
        after_acceptor = acceptor_shell.status()
    finally:
        _release(initiator_shell)
        _release(acceptor_shell)
    problems = []
    for peer_status, peer in ((after_initiator, initiator),
                              (after_acceptor, acceptor)):
        sent = _audio_field(peer_status, "sco_tx_frames", int, peer.label)
        received = _audio_field(peer_status, "sco_rx_frames", int, peer.label)
        print(f"{peer.label} audio: {peer_status['sco']} "
              f"{peer_status['sco_pkt_type']}, sent {sent} received "
              f"{received} frames", file=out)
        if peer_status["sco"] != AUDIO_ON:
            problems.append(f"the {peer.label}'s audio link read "
                            f"{peer_status['sco']} afterwards, not {AUDIO_ON}")
        if sent < min_frames:
            problems.append(f"the {peer.label} sent {sent} audio frames, fewer "
                            f"than the {min_frames} this test needs")
        # This counter is the Serial Port Profile's, and an audio session
        # brings no SPP link up, so it is normally zero either way; what
        # says the audio link held is both ends reading `on` afterwards and
        # the frames they counted.
        if peer_status["disconnects"]:
            problems.append(f"the {peer.label}'s link went down "
                            f"{peer_status['disconnects']} time(s) while the "
                            "command ran")
    if seconds < min_seconds:
        problems.append(f"{command[0]} ran {seconds:.1f} s, less than the "
                        f"{min_seconds:g} s this test needs: it stopped early")
    if problems:
        raise PeerError("the pair did not carry audio: " + "; ".join(problems))
    print(f"pair carried audio: {after_initiator['sco_pkt_type']}", file=out)
    return status


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
