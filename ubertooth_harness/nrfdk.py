"""Drive an nRF52840 DK (PCA10056) running the harness's role firmware.

The firmware is in ``firmware/nrf52840dk`` (see its README for the build and
the shell protocol). This module flashes it by J-Link serial with a pinned
``nrfutil device`` and talks to its shell on the J-Link's first virtual COM
port (VCOM0, the DK's uart0). Run as a script by the advertiser cases::

    python nrfdk.py identify   --jlink SERIAL [--port PATH]
    python nrfdk.py ensure     --jlink SERIAL [--port PATH] [--hex IMAGE]
                               [--address ADDR] [--role ROLE]
    python nrfdk.py role       --jlink SERIAL --role advertiser|central|peripheral
    python nrfdk.py advertise  --jlink SERIAL [--port PATH] --chmap 37|38|39|all
                               --interval MS --payload HEX|none [--type nonconn|conn]
    python nrfdk.py peripheral --jlink SERIAL [--dle on|off]
                               [--chmap 37|38|39|all]
    python nrfdk.py central    --jlink SERIAL --peer ADDR --interval MS
                               [--dle on|off] [--payload BYTES] [--period MS]
                               [--cycle MS] [--autoupdate MS]
    python nrfdk.py update     --jlink SERIAL --interval MS
    python nrfdk.py datamap    --jlink SERIAL --channels all|0,12,36
    python nrfdk.py stop       --jlink SERIAL [--port PATH]
    python nrfdk.py idle       --jlink SERIAL [--port PATH]
    python nrfdk.py status     --jlink SERIAL [--port PATH]
    python nrfdk.py check-image IMAGE

``check-image`` is the one command that touches no board: it makes the check
``ensure`` makes before it flashes, that an image carries FIRMWARE_VERSION, so
a build can be checked where it is made (scripts/ci.sh firmware-dk).

``idle`` is the release command: it leaves the board in the advertiser role
with every setting back to its boot value, whatever it was doing.

Every command also takes ``--alias NAME``, the board's registry alias. With
it, each line sent to the shell and each line the board answers is written to
stderr as ``[NAME >] adv start`` and ``[NAME <] OK``, so a run records the
whole conversation beside the Ubertooth tool output.

Every command fails closed: a wrong tool version, a probe that is not
attached, a shell that does not answer within its timeout, an ``ERR`` reply,
or a status that does not show what was asked prints ``error: <why>`` on
stderr and exits 1. Results are ``key: value`` lines on stdout, for the
cases' patterns. Exit status 2 is a usage error.

``ensure`` flashes ``--hex`` only when the board does not already report
FIRMWARE_VERSION, and refuses an image that does not contain that version
string, so a run never writes an image it cannot then verify.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

# The firmware version the harness expects (firmware/nrf52840dk/src/roles.h
# UTDK_FW_VERSION; tests/test_nrfdk.py keeps them in step).
FIRMWARE_VERSION = targets.DK_FIRMWARE
# nrfutil and its device command, as installed and validated on the lab host.
NRFUTIL_VERSION = "8.2.1"
NRFUTIL_DEVICE_VERSION = "2.23.0"
BAUD = 115200
# Payload format (firmware README, "Advertising payload").
COMPANY_ID = 0xFFFF
MAGIC = b"UTAD"
PAYLOAD_FORMAT = 1
TAG_MAX = 12
CHMAP_BITS = {"37": 0x01, "38": 0x02, "39": 0x04, "all": 0x07}
INTERVAL_MIN_MS = 20
INTERVAL_MAX_MS = 10240

COMMAND_TIMEOUT_S = 3.0
# `role <name>` and `conn defaults` wait for the link to come down (up to 6 s
# in the firmware) and may wait out the second the controller needs before it
# will take the restored channel map.
LEAVE_TIMEOUT_S = 12.0
# After a reset (or a flash) the firmware needs a moment to start Bluetooth
# and the shell.
BOOT_TIMEOUT_S = 15.0
FLASH_TIMEOUT_S = 120.0
TOOL_TIMEOUT_S = 30.0
POLL_S = 0.05

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ADDRESS = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
_SHELL_ERROR = re.compile(r": (?:command not found|wrong parameter count)$")
_STATUS_KEYS = {
    "role": str, "fw": str, "bt": bool, "address": str, "adv": bool,
    "running": bool, "type": str, "chmap": str, "interval_ms": int,
    "payload": str, "counter": int,
}
# The connection roles' fields (firmware README, "Shell protocol"). Firmware
# older than the connection roles has none of them, and `ensure` has to be
# able to read its version to flash it, so they are required only when the
# status carries them at all.
_CONNECTION_KEYS = {
    "conn_on": bool, "connected": bool, "peer": str, "conn_interval_ms": int,
    "dle": bool, "interval_units": int, "latency": int, "timeout_units": int,
    "tx_octets": int, "rx_octets": int, "mtu": int, "phy": str,
    "conn_chmap": str, "cycle_ms": int, "autoupdate_ms": int,
    "writes": int, "received": int, "updates": int, "connects": int,
}
# The connection's data channel map, which only the central can change
# (firmware utdk-1.3.0 and later). Kept out of _CONNECTION_KEYS so that a
# board still running an earlier image reports a status `ensure` can read,
# and so gets flashed instead of refused.
_DATA_MAP_KEYS = {"data_map": str, "data_map_used": int, "data_map_sets": int}
ROLES = ("advertiser", "central", "peripheral")
# The data channels a connection hops over, 0 to 36, of which a channel map
# has to leave at least two used (Bluetooth Core, Vol 6 Part B, 4.5.8.1: the
# minimum number of used channels is 2; the controller on the bench refuses
# one channel).
DATA_CHANNELS = 37
DATA_CHANNELS_MIN = 2
DATA_MAP_BYTES = 5
DATA_MAP_ALL = "ffffffff1f"
# Connection interval, in the firmware's range (Bluetooth Core 7.5 ms-4 s).
CONN_INTERVAL_MIN_MS = 8
CONN_INTERVAL_MAX_MS = 4000

def conn_units(interval_ms: int) -> int:
    """``interval_ms`` as the 1.25 ms units a CONNECT_IND carries.

    The firmware computes this with integer arithmetic
    (``interval_ms * 4 / 5`` in connection.c), so this truncates too: for
    11 ms both give 8 units, and rounding here would expect 9 and fail every
    interval that is not a multiple of 5 ms.
    """
    return int(interval_ms) * 4 // 5
# The data length the pair asks for with `conn dle on`.
DATA_LENGTH_MAX = 251
# Time for the central to connect, write, and for status to settle.
CONNECT_TIMEOUT_S = 10.0


class NrfDkError(RuntimeError):
    """The DK, its probe, or the tools did not do what was asked."""


Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess]


def run_tool(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as error:
        raise NrfDkError(f"{argv[0]} not found on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise NrfDkError(f"{' '.join(argv[:3])} timed out after {timeout:g}s") from error


def jlink_serial(value: str) -> int:
    """A J-Link serial as registered (digits; nrfutil pads it to 12)."""
    text = str(value).strip()
    if not text.isdigit() or not 6 <= len(text.lstrip("0") or "0") <= 12:
        raise NrfDkError(f"J-Link serial must be 6-12 digits, got {value!r}")
    return int(text)


def normalize_address(value: str) -> str:
    text = str(value).strip().upper()
    if not _ADDRESS.fullmatch(text):
        raise NrfDkError(f"BLE address must look like C0:11:22:33:44:55, got {value!r}")
    return text


def is_static_random(address: str) -> bool:
    """A static random address has the two most significant bits set."""
    return int(normalize_address(address)[:2], 16) >> 6 == 0b11


def tag_hex(value: str) -> str:
    """The payload tag as the firmware prints it: lowercase hex, '' for none."""
    text = str(value).strip().lower()
    if text == "none":
        return ""
    if not re.fullmatch(r"(?:[0-9a-f]{2}){1,%d}" % TAG_MAX, text):
        raise NrfDkError(f"payload must be 1-{TAG_MAX} bytes of hex, or none; got {value!r}")
    return text


def default_port(serial: int, platform: str = sys.platform) -> str | None:
    """The stable VCOM0 path on Linux; None elsewhere (ask nrfutil)."""
    if platform.startswith("linux"):
        return targets.DK_BY_ID.format(serial=serial)
    return None


# --------------------------------------------------------------------------
# nrfutil


def _version(output: str, tool: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(tool)} (\S+)", output)
    return match[1] if match else None


def check_nrfutil(run: Runner = run_tool) -> None:
    """nrfutil and nrfutil device must be the pinned versions."""
    for argv, tool, expected in (
        (("nrfutil", "--version"), "nrfutil", NRFUTIL_VERSION),
        (("nrfutil", "device", "--version"), "nrfutil-device", NRFUTIL_DEVICE_VERSION),
    ):
        result = run(argv, TOOL_TIMEOUT_S)
        found = _version(result.stdout, tool)
        if result.returncode != 0 or found is None:
            raise NrfDkError(
                f"{' '.join(argv)} exited {result.returncode} without a version; "
                f"install nrfutil {NRFUTIL_VERSION} with device {NRFUTIL_DEVICE_VERSION}"
            )
        if found != expected:
            raise NrfDkError(
                f"{tool} is {found}, the harness is validated with {expected}; "
                f"install that version (nrfutil install device={NRFUTIL_DEVICE_VERSION} --force)"
                if tool == "nrfutil-device"
                else f"{tool} is {found}, the harness is validated with {expected}"
            )


def _json_lines(text: str):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def find_probe(serial: int, run: Runner = run_tool) -> dict:
    """nrfutil's record of the probe with this serial; fails if it is not attached."""
    result = run(("nrfutil", "device", "list", "--json"), TOOL_TIMEOUT_S)
    if result.returncode != 0:
        raise NrfDkError(f"nrfutil device list exited {result.returncode}: "
                         f"{_last_line(result.stderr or result.stdout)}")
    for message in _json_lines(result.stdout):
        devices = ((message.get("data") or {}).get("data") or {}).get("devices")
        for device in devices or ():
            try:
                found = int(str(device.get("serialNumber", "")))
            except ValueError:
                continue
            if found == serial:
                return device
    raise NrfDkError(f"no J-Link probe with serial {serial} is attached (nrfutil device list)")


def probe_vcom0(device: dict) -> str | None:
    for port in device.get("serialPorts") or ():
        if port.get("vcom") == 0:
            return port.get("path") or port.get("comName")
    return None


def flash(serial: int, image: Path, run: Runner = run_tool) -> None:
    """Erase, program, verify by read-back, and reset."""
    result = run(
        ("nrfutil", "device", "program", "--serial-number", str(serial),
         "--firmware", str(image),
         "--options", "chip_erase_mode=ERASE_ALL,verify=VERIFY_READ,reset=RESET_SYSTEM"),
        FLASH_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise NrfDkError(
            f"nrfutil device program exited {result.returncode}: "
            f"{_last_line(result.stderr or result.stdout)}")


def reset(serial: int, run: Runner = run_tool) -> None:
    result = run(("nrfutil", "device", "reset", "--serial-number", str(serial)),
                 TOOL_TIMEOUT_S)
    if result.returncode != 0:
        raise NrfDkError(f"nrfutil device reset exited {result.returncode}: "
                         f"{_last_line(result.stderr or result.stdout)}")


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def hex_bytes(path: Path) -> bytes:
    """The data bytes of an Intel HEX file, in file order (addresses ignored)."""
    data = bytearray()
    try:
        text = Path(path).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise NrfDkError(f"cannot read firmware image {path}: {error}") from error
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            if not line.startswith(":"):
                raise ValueError
            record = bytes.fromhex(line[1:])
        except ValueError:
            raise NrfDkError(f"{path}:{number}: not an Intel HEX record") from None
        if len(record) < 5 or len(record) != record[0] + 5 or sum(record) & 0xFF:
            raise NrfDkError(f"{path}:{number}: bad Intel HEX record")
        if record[3] == 0x00:
            data += record[4:-1]
    if not data:
        raise NrfDkError(f"{path}: no data records")
    return bytes(data)


def check_image(path: Path, version: str = FIRMWARE_VERSION) -> None:
    if version.encode() not in hex_bytes(path):
        raise NrfDkError(f"{path} does not contain firmware version {version}; "
                         "build it from firmware/nrf52840dk at this harness commit")


# --------------------------------------------------------------------------
# Shell


class Shell:
    """Line protocol: one command line out; data lines, then OK or ERR <why>.

    ``log(direction, text)``, when given, is called with every line sent and
    every line received, so a run can record the whole conversation.
    """

    def __init__(self, port, clock=time.monotonic, log=None):
        self.port = port
        self.clock = clock
        self.log = log
        self._buffer = b""

    @classmethod
    def open(cls, path: str, log=None) -> "Shell":
        try:
            import serial  # pyserial, imported lazily
        except ImportError as error:
            raise NrfDkError("pyserial is not installed in this interpreter") from error
        try:
            port = serial.Serial(path, BAUD, timeout=POLL_S, write_timeout=1.0,
                                 exclusive=True)
        except (OSError, ValueError, serial.SerialException) as error:
            raise NrfDkError(f"cannot open {path}: {error}") from error
        return cls(port, log=log)

    def close(self) -> None:
        self.port.close()

    def _lines(self, deadline: float):
        while True:
            while b"\n" in self._buffer:
                raw, self._buffer = self._buffer.split(b"\n", 1)
                yield _ANSI.sub("", raw.decode("utf-8", "replace")).strip("\r \t\x00")
            if self.clock() >= deadline:
                return
            chunk = self.port.read(256)
            if chunk:
                self._buffer += chunk

    def command(self, text: str, timeout: float = COMMAND_TIMEOUT_S) -> list[str]:
        """Send one command; its data lines. ERR or no answer raises."""
        self.port.reset_input_buffer()
        self._buffer = b""
        self.port.write(text.encode("ascii") + b"\r")
        self.port.flush()
        if self.log is not None:
            self.log(targets.SENT, text)
        data = []
        for line in self._lines(self.clock() + timeout):
            if line and self.log is not None:
                self.log(targets.RECEIVED, line)
            if line == "OK":
                return data
            if line.startswith("ERR"):
                raise NrfDkError(f"'{text}' refused: {line[3:].strip() or 'ERR'}")
            if _SHELL_ERROR.search(line):
                # Zephyr's shell answers these itself, with no OK or ERR.
                raise NrfDkError(f"'{text}' refused by the shell: {line}")
            if line:
                data.append(line)
        raise NrfDkError(f"no answer to '{text}' within {timeout:g}s"
                         + (f" (last line {data[-1]!r})" if data else ""))

    def status(self, timeout: float = COMMAND_TIMEOUT_S) -> dict:
        lines = self.command("status", timeout)
        for line in reversed(lines):
            if line.startswith("{"):
                return parse_status(line)
        raise NrfDkError("status printed no JSON line")

    def wait_ready(self, timeout: float = BOOT_TIMEOUT_S) -> dict:
        """Status, retried until the firmware answers or the deadline passes."""
        deadline = self.clock() + timeout
        last = None
        while True:
            try:
                return self.status(min(COMMAND_TIMEOUT_S, max(0.2, deadline - self.clock())))
            except NrfDkError as error:
                last = error
            if self.clock() >= deadline:
                raise NrfDkError(f"the DK did not answer status within {timeout:g}s: {last}")


def parse_status(line: str) -> dict:
    try:
        status = json.loads(line)
    except ValueError as error:
        raise NrfDkError(f"status is not JSON: {line[:80]!r}") from error
    if not isinstance(status, dict):
        raise NrfDkError("status is not a JSON object")
    required = dict(_STATUS_KEYS)
    if any(key in status for key in _CONNECTION_KEYS):
        required.update(_CONNECTION_KEYS)
    for key, kind in required.items():
        value = status.get(key)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise NrfDkError(f"status has no valid '{key}'")
    return status


def require_connection_fields(status: dict) -> dict:
    """The status must come from firmware that has the connection roles."""
    if not all(key in status for key in _CONNECTION_KEYS):
        raise NrfDkError(
            f"this firmware's status has no connection fields; the connection "
            f"roles need {FIRMWARE_VERSION} (it reports {status.get('fw', '?')})")
    return status


def data_map(channels: str) -> str:
    """A channel list ("all", or "0,12,36") as the firmware reports the map.

    Ten hex digits, the five bytes a CONNECT_IND and an LL_CHANNEL_MAP_IND
    carry, least significant byte first: bit b of byte i is channel 8i + b.
    Refuses a list of fewer than two channels, a channel the air does not
    have, and anything that is not a list.
    """
    if channels == "all":
        return DATA_MAP_ALL
    numbers = []
    for word in channels.split(","):
        # isdecimal(), not isdigit(): the latter is true of characters int()
        # refuses, and the firmware's parser takes ASCII digits only.
        if not word.isdecimal() or not word.isascii():
            raise NrfDkError(
                f"channel map must be 'all' or a list of data channels "
                f"0-{DATA_CHANNELS - 1}, for example 0,12,36 (got {channels!r})")
        numbers.append(int(word))
    for number in numbers:
        if number >= DATA_CHANNELS:
            raise NrfDkError(f"channel {number} is not a data channel "
                             f"(0-{DATA_CHANNELS - 1})")
    if len(set(numbers)) < DATA_CHANNELS_MIN:
        raise NrfDkError(
            f"a channel map has to leave at least {DATA_CHANNELS_MIN} data "
            f"channels used (got {channels!r})")
    bits = 0
    for number in numbers:
        bits |= 1 << number
    return bits.to_bytes(DATA_MAP_BYTES, "little").hex()


# --------------------------------------------------------------------------
# Operations


@dataclass
class Dk:
    serial: int
    port: str
    run: Runner = run_tool
    # Opens the shell on ``port``; tests pass a fake.
    opener: Callable[[str], Shell] | None = None
    # The board's registry alias. With one, every line to and from its shell
    # is written to stderr marked with it, so the run records the whole
    # conversation; without one nothing is written.
    alias: str = ""


def resolve(jlink: str, port: str | None, run: Runner = run_tool,
            alias: str = "") -> Dk:
    serial = jlink_serial(jlink)
    path = port or default_port(serial)
    if path is None:
        path = probe_vcom0(find_probe(serial, run))
        if path is None:
            raise NrfDkError(f"J-Link {serial} reports no VCOM0 port; register 'serial device'")
    return Dk(serial, path, run, alias=alias)


def shell_logger(alias: str, stream=None):
    """Writes each shell line, marked with ``alias``, for the run's transcript."""
    def log(direction: str, text: str) -> None:
        print(targets.shell_line(alias, direction, text),
              file=stream or sys.stderr, flush=True)
    return log


def _open(dk: Dk) -> Shell:
    log = shell_logger(dk.alias) if dk.alias else None
    if dk.opener is not None:
        shell = dk.opener(dk.port)
        shell.log = log
        return shell
    if not os.path.exists(dk.port):
        raise NrfDkError(f"{dk.port} does not exist: is J-Link {dk.serial} attached?")
    return Shell.open(dk.port, log=log)


def _report(status: dict, out) -> None:
    out = out or sys.stdout
    print(f"firmware: {status['fw']}", file=out)
    print(f"role: {status['role']}", file=out)
    print(f"address: {status['address']}", file=out)
    state = "on" if status["adv"] and status["running"] else "off"
    print(f"advertising: {state} type {status['type']} chmap {status['chmap']} "
          f"interval {status['interval_ms']} ms payload {status['payload'] or 'none'}",
          file=out)
    print(f"counter: {status['counter']}", file=out)


def _report_connection(status: dict, out) -> None:
    out = out or sys.stdout
    print(f"connection: {'up' if status['connected'] else 'down'} peer "
          f"{status['peer'] or 'none'} interval {status['interval_units']} units "
          f"({status['interval_units'] * 1.25:g} ms) latency {status['latency']} "
          f"timeout {status['timeout_units']} units", file=out)
    print(f"data length: tx {status['tx_octets']} rx {status['rx_octets']} "
          f"octets mtu {status['mtu']} phy {status['phy']}", file=out)
    print(f"update: asked {status['update_ms']} ms "
          f"({conn_units(status['update_ms'])} units)", file=out)
    if all(key in status for key in _DATA_MAP_KEYS):
        # Firmware older than utdk-1.3.0 reports none of these.
        print(f"channel map: {status['data_map']} "
              f"({status['data_map_used']} of {DATA_CHANNELS} channels, set "
              f"{status['data_map_sets']} time(s))", file=out)
    print(f"traffic: writes {status['writes']} received {status['received']} bytes "
          f"updates {status['updates']} connects {status['connects']}", file=out)


def identify(dk: Dk, out=None) -> None:
    check_nrfutil(dk.run)
    device = find_probe(dk.serial, dk.run)
    print(f"J-Link serial: {dk.serial}", file=out)
    board = (device.get("devkit") or {}).get("boardVersion")
    if board:
        print(f"board: {board}", file=out)
    if dk.opener is None and not os.path.exists(dk.port):
        raise NrfDkError(f"{dk.port} does not exist for J-Link {dk.serial}")
    print(f"port: {dk.port}", file=out)


def ensure(dk: Dk, image: Path | None, address: str | None, out=None,
           version: str = FIRMWARE_VERSION, wanted_role: str = "advertiser") -> dict:
    """The board runs ``version`` (flashing ``image`` if not), idle, at ``address``.

    It is left in ``wanted_role``, idle: an image is flashed only when the
    board reports another firmware version, so a run never writes an image it
    cannot then verify.
    """
    if wanted_role not in ROLES:
        raise NrfDkError("role must be " + ", ".join(ROLES))
    status = None
    shell = _open(dk)
    try:
        try:
            status = shell.wait_ready(COMMAND_TIMEOUT_S * 2)
        except NrfDkError as error:
            reason = str(error)
        else:
            reason = f"it runs {status['fw']}"
        if status is None or status["fw"] != version:
            if image is None:
                raise NrfDkError(
                    f"J-Link {dk.serial} does not run {version} ({reason}); pass "
                    '--artifact "nRF52840 advertiser=PATH" with the image to '
                    "flash (one image holds every role, so the label is the "
                    "same whichever role is wanted)")
            check_image(image, version)
            check_nrfutil(dk.run)
            find_probe(dk.serial, dk.run)
            shell.close()
            flash(dk.serial, image, dk.run)
            print(f"flashed: {image.name} ({reason} before)", file=out)
            shell = _open(dk)
            status = shell.wait_ready()
            if status["fw"] != version:
                raise NrfDkError(f"after flashing, J-Link {dk.serial} reports "
                                 f"{status['fw']}, not {version}")
        shell.command(f"role {wanted_role}")
        shell.command("adv defaults")
        status = shell.status()
    finally:
        shell.close()
    if status["role"] != wanted_role:
        raise NrfDkError(f"J-Link {dk.serial} role is {status['role']}, "
                         f"not {wanted_role}")
    if address is not None and status["address"] != normalize_address(address):
        raise NrfDkError(f"J-Link {dk.serial} advertises from {status['address']}, the "
                         f"registry says {normalize_address(address)}")
    if status["adv"] or status["running"]:
        raise NrfDkError("advertising did not stop")
    _report(status, out)
    return status


def advertise(dk: Dk, chmap: str, interval_ms: int, payload: str,
              adv_type: str = "nonconn", out=None) -> dict:
    if chmap not in CHMAP_BITS:
        raise NrfDkError("channel map must be 37, 38, 39 or all")
    if not INTERVAL_MIN_MS <= interval_ms <= INTERVAL_MAX_MS:
        raise NrfDkError(f"interval must be {INTERVAL_MIN_MS}-{INTERVAL_MAX_MS} ms")
    if adv_type not in ("nonconn", "conn"):
        raise NrfDkError("type must be nonconn or conn")
    tag = tag_hex(payload)
    shell = _open(dk)
    try:
        for command in ("adv stop", f"adv type {adv_type}", f"adv chmap {chmap}",
                        f"adv interval {interval_ms}", f"adv payload {tag or 'none'}",
                        "adv start"):
            shell.command(command)
        status = shell.status()
    finally:
        shell.close()
    expected = {"adv": True, "running": True, "type": adv_type, "chmap": chmap,
                "interval_ms": interval_ms, "payload": tag}
    wrong = [f"{key} {status[key]!r} (asked {value!r})"
             for key, value in expected.items() if status[key] != value]
    if wrong:
        raise NrfDkError("status does not show the advertising asked for: " + ", ".join(wrong))
    _report(status, out)
    return status


def stop(dk: Dk, out=None, defaults: bool = False) -> dict:
    shell = _open(dk)
    try:
        shell.command("adv defaults" if defaults else "adv stop")
        status = shell.status()
    finally:
        shell.close()
    if status["adv"] or status["running"]:
        raise NrfDkError("advertising did not stop")
    _report(status, out)
    return status


def idle(dk: Dk, out=None) -> dict:
    """Leave the board idle whatever role it is in: the release command.

    ``role advertiser`` leaves both the advertiser and the connection role
    idle and restores their boot settings, so one command releases a DK
    without the caller having to know what it was doing.
    """
    shell = _open(dk)
    try:
        shell.command("role advertiser", LEAVE_TIMEOUT_S)
        shell.command("adv defaults")
        status = require_connection_fields(shell.status())
    finally:
        shell.close()
    wrong = []
    if status["role"] != "advertiser":
        wrong.append(f"role {status['role']}")
    if status["adv"] or status["running"]:
        wrong.append("still advertising")
    if status["conn_on"] or status["connected"]:
        wrong.append("still connected")
    wrong += _data_map_restored(status)
    if wrong:
        raise NrfDkError("the DK did not go idle: " + ", ".join(wrong))
    _report(status, out)
    return status


def _data_map_restored(status: dict) -> list:
    """Why the board is not fit to hand on, by its channel map.

    The channel classification belongs to the controller and outlives the
    link, so a board left with a narrowed one would narrow the next test's
    CONNECT_IND. Leaving the role puts all 37 channels back; if the
    controller refused, the board says so here and the release fails rather
    than passing the narrow map on.
    """
    found = status.get("data_map")
    if found is None:  # firmware older than utdk-1.3.0
        return []
    if found != DATA_MAP_ALL:
        return [f"channel map {found}, not all {DATA_CHANNELS} channels "
                f"({DATA_MAP_ALL})"]
    return []


def role(dk: Dk, wanted: str, out=None) -> dict:
    """Switch the board to ``wanted``, leaving the previous role idle."""
    if wanted not in ROLES:
        raise NrfDkError("role must be " + ", ".join(ROLES))
    shell = _open(dk)
    try:
        shell.command(f"role {wanted}", LEAVE_TIMEOUT_S)
        status = shell.status()
    finally:
        shell.close()
    if status["role"] != wanted:
        raise NrfDkError(f"J-Link {dk.serial} reports role {status['role']}, not {wanted}")
    wrong = _data_map_restored(status)
    if wrong:
        raise NrfDkError(f"J-Link {dk.serial} left the role with " + ", ".join(wrong))
    _report(status, out)
    return status


def _conn_interval(interval_ms: int) -> int:
    if not CONN_INTERVAL_MIN_MS <= interval_ms <= CONN_INTERVAL_MAX_MS:
        raise NrfDkError(f"connection interval must be {CONN_INTERVAL_MIN_MS}-"
                         f"{CONN_INTERVAL_MAX_MS} ms")
    return interval_ms


def peripheral(dk: Dk, dle: bool = True, chmap: str = "37", out=None) -> dict:
    """Take the peripheral role and advertise connectable, waiting to be connected.

    ``chmap`` is the advertising channel (or channels) it uses. The central
    connects on the channel it hears the peripheral on, so one channel puts
    the CONNECT_IND where a receiver is listening.
    """
    if chmap not in CHMAP_BITS:
        raise NrfDkError("channel map must be 37, 38, 39 or all")
    shell = _open(dk)
    try:
        # `role` already leaves both roles idle with their boot settings.
        for command in ("role peripheral", f"conn chmap {chmap}",
                        f"conn dle {'on' if dle else 'off'}", "conn start"):
            shell.command(command)
        status = require_connection_fields(shell.status())
    finally:
        shell.close()
    if (status["role"] != "peripheral" or not status["conn_on"]
            or status["conn_chmap"] != chmap):
        raise NrfDkError(f"J-Link {dk.serial} is not advertising as a peripheral "
                         f"(role {status['role']}, started {status['conn_on']}, "
                         f"channels {status['conn_chmap']})")
    _report(status, out)
    _report_connection(status, out)
    return status


def central(dk: Dk, peer: str, interval_ms: int, dle: bool = True,
            payload: int | None = None, period_ms: int | None = None,
            cycle_ms: int | None = None, autoupdate_ms: int | None = None,
            out=None) -> dict:
    """Take the central role and connect to ``peer`` at ``interval_ms``.

    With ``cycle_ms`` the link is dropped and made again that often, so a
    capture started at any moment sees a whole connection; ``autoupdate_ms``
    asks for a connection parameter update once each link is up.
    """
    address = normalize_address(peer)
    interval = _conn_interval(interval_ms)
    commands = ["role central", f"conn peer {address}",
                f"conn interval {interval}",
                f"conn dle {'on' if dle else 'off'}"]
    if payload is not None:
        commands.append(f"conn payload {payload}")
    if period_ms is not None:
        commands.append(f"conn period {period_ms}")
    if cycle_ms is not None:
        commands.append(f"conn cycle {cycle_ms if cycle_ms else 'off'}")
    if autoupdate_ms is not None:
        commands.append(
            f"conn autoupdate {_conn_interval(autoupdate_ms) if autoupdate_ms else 'off'}")
    commands.append("conn start")
    # The connection interval the CONNECT_IND asks for, and the one an
    # automatic update moves to; either is right once the link is up.
    intervals = {conn_units(interval)}
    if autoupdate_ms:
        intervals.add(conn_units(_conn_interval(autoupdate_ms)))
    shell = _open(dk)
    try:
        for command in commands:
            shell.command(command, CONNECT_TIMEOUT_S if command == "conn start"
                          else COMMAND_TIMEOUT_S)
        # The data length, PHY and MTU are settled a few connection events
        # after the link comes up, so wait for the whole state, not just the
        # link. With `conn cycle` a reconnection may be in progress, which
        # is one of the states this waits out.
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while True:
            status = require_connection_fields(shell.status())
            wrong = []
            if status["role"] != "central":
                wrong.append(f"role {status['role']}")
            if not status["connected"]:
                wrong.append("not connected")
            if status["peer"] != address:
                wrong.append(f"peer {status['peer'] or 'none'}")
            if status["interval_units"] not in intervals:
                wrong.append(f"interval {status['interval_units']} units, asked "
                             + " or ".join(str(units) for units in sorted(intervals)))
            if status["phy"] != "1M":
                wrong.append(f"phy {status['phy']}")
            if dle and status["tx_octets"] != DATA_LENGTH_MAX:
                wrong.append(f"tx {status['tx_octets']} octets, asked "
                             f"{DATA_LENGTH_MAX}")
            if not wrong:
                break
            if time.monotonic() >= deadline:
                raise NrfDkError(f"the connection is not what was asked for "
                                 f"{CONNECT_TIMEOUT_S:g}s after starting: "
                                 + ", ".join(wrong))
            time.sleep(0.2)
    finally:
        shell.close()
    _report(status, out)
    _report_connection(status, out)
    return status


def update(dk: Dk, interval_ms: int, out=None) -> dict:
    """Ask for a connection parameter update to ``interval_ms``."""
    interval = _conn_interval(interval_ms)
    expected_units = conn_units(interval)
    shell = _open(dk)
    try:
        shell.command(f"conn update {interval}")
        # The update takes effect a few connection events later.
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while True:
            status = require_connection_fields(shell.status())
            if status["interval_units"] == expected_units:
                break
            if time.monotonic() >= deadline:
                raise NrfDkError(
                    f"the connection interval is still {status['interval_units']} "
                    f"units {CONNECT_TIMEOUT_S:g}s after asking for {expected_units}")
            time.sleep(0.2)
    finally:
        shell.close()
    if not status["connected"]:
        raise NrfDkError("the connection dropped during the parameter update")
    _report(status, out)
    _report_connection(status, out)
    return status


def datamap(dk: Dk, channels: str, out=None) -> dict:
    """Change the live connection's data channel map to ``channels``.

    The central has the controller classify those channels, and the
    controller carries the new map to the link as an LL_CHANNEL_MAP_IND, so
    a sniffer following the connection has to follow the map change. The
    link must be up, and the map has to leave at least two channels used.
    """
    wanted = data_map(channels)
    shell = _open(dk)
    try:
        # The count before, so a board that answered OK and changed nothing
        # cannot pass by echoing the map back.
        before = shell.status().get("data_map_sets")
        shell.command(f"conn datamap {channels}")
        status = require_connection_fields(shell.status())
    finally:
        shell.close()
    for key, kind in _DATA_MAP_KEYS.items():
        value = status.get(key)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise NrfDkError(
                f"this firmware does not report a valid '{key}' for the "
                f"connection's channel map; it needs {FIRMWARE_VERSION} (it "
                f"reports {status.get('fw', '?')})")
    if status["data_map"] != wanted:
        raise NrfDkError(f"the connection's channel map is {status['data_map']}, "
                         f"not the {wanted} that was asked for")
    used = bin(int(wanted, 16)).count("1")
    if status["data_map_used"] != used:
        raise NrfDkError(f"the board reports {status['data_map_used']} channels in "
                         f"use, not the {used} its own map {wanted} leaves")
    if isinstance(before, int) and status["data_map_sets"] <= before:
        raise NrfDkError(f"the board still reports {status['data_map_sets']} channel "
                         "map(s) taken by its controller, so this one was not")
    if not status["connected"]:
        raise NrfDkError("the connection dropped during the channel map update")
    _report(status, out)
    _report_connection(status, out)
    return status


def status_command(dk: Dk, out=None) -> dict:
    shell = _open(dk)
    try:
        status = shell.status()
    finally:
        shell.close()
    print(json.dumps(status, sort_keys=True), file=out)
    _report(status, out)
    if all(key in status for key in _CONNECTION_KEYS):
        # Firmware with the connection roles: report the link as well, so a
        # caller can check what the pair did without parsing the JSON.
        _report_connection(status, out)
    return status


def main(argv: Sequence[str] | None = None, run: Runner = run_tool) -> int:
    parser = argparse.ArgumentParser(prog="nrfdk.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {}
    for name in ("identify", "ensure", "advertise", "stop", "idle", "status", "role",
                 "peripheral", "central", "update", "datamap"):
        command = sub.add_parser(name)
        command.add_argument("--jlink", required=True, help="J-Link serial")
        command.add_argument("--port", help="VCOM0 path (default: by J-Link serial)")
        command.add_argument(
            "--alias", default="",
            help="the board's registry alias; with it, every line to and from "
                 "its shell is written to stderr marked with that alias")
        commands[name] = command
    # The only command that touches no board: it reads an image, as `ensure`
    # does before it flashes one (scripts/ci.sh firmware-dk).
    image_parser = sub.add_parser("check-image")
    image_parser.add_argument("hex", type=Path, help="Intel HEX image to check")
    commands["ensure"].add_argument("--hex", type=Path, help="image to flash if needed")
    commands["ensure"].add_argument("--address", help="expected BLE address")
    commands["ensure"].add_argument("--role", default="advertiser", choices=ROLES)
    advertise_parser = commands["advertise"]
    advertise_parser.add_argument("--chmap", required=True, choices=sorted(CHMAP_BITS))
    advertise_parser.add_argument("--interval", required=True, type=int, help="ms")
    advertise_parser.add_argument("--payload", required=True, help="tag hex, or none")
    advertise_parser.add_argument("--type", default="nonconn", choices=("nonconn", "conn"))
    commands["role"].add_argument("--role", required=True, choices=ROLES)
    for name in ("peripheral", "central"):
        commands[name].add_argument("--dle", default="on", choices=("on", "off"))
    commands["peripheral"].add_argument("--chmap", default="37",
                                        choices=sorted(CHMAP_BITS))
    central_parser = commands["central"]
    central_parser.add_argument("--peer", required=True, help="the peripheral's address")
    central_parser.add_argument("--interval", required=True, type=int, help="ms")
    central_parser.add_argument("--payload", type=int, help="bytes per write")
    central_parser.add_argument("--period", type=int, help="ms between writes")
    central_parser.add_argument("--cycle", type=int,
                                help="ms a connection lasts before the next (0: one)")
    central_parser.add_argument("--autoupdate", type=int,
                                help="ms interval to ask for once connected (0: none)")
    commands["update"].add_argument("--interval", required=True, type=int, help="ms")
    commands["datamap"].add_argument(
        "--channels", required=True,
        help="data channels the connection may use: all, or a list such as 0,12,36")
    args = parser.parse_args(argv)
    try:
        if args.command == "check-image":
            check_image(args.hex)
            print(f"image: {args.hex}")
            print(f"firmware version: {FIRMWARE_VERSION}")
            return 0
        dk = resolve(args.jlink, args.port, run, args.alias)
        if args.command == "identify":
            identify(dk)
        elif args.command == "ensure":
            ensure(dk, args.hex, args.address, wanted_role=args.role)
        elif args.command == "advertise":
            advertise(dk, args.chmap, args.interval, args.payload, args.type)
        elif args.command == "stop":
            stop(dk)
        elif args.command == "idle":
            idle(dk)
        elif args.command == "role":
            role(dk, args.role)
        elif args.command == "peripheral":
            peripheral(dk, args.dle == "on", args.chmap)
        elif args.command == "central":
            central(dk, args.peer, args.interval, args.dle == "on", args.payload,
                    args.period, args.cycle, args.autoupdate)
        elif args.command == "update":
            update(dk, args.interval)
        elif args.command == "datamap":
            datamap(dk, args.channels)
        else:
            status_command(dk)
    except NrfDkError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
