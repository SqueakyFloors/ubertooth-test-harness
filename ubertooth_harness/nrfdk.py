"""Drive an nRF52840 DK (PCA10056) running the harness's role firmware.

The firmware is in ``firmware/nrf52840dk`` (see its README for the build and
the shell protocol). This module flashes it by J-Link serial with a pinned
``nrfutil device`` and talks to its shell on the J-Link's first virtual COM
port (VCOM0, the DK's uart0). Run as a script by the advertiser cases::

    python nrfdk.py identify  --jlink SERIAL [--port PATH]
    python nrfdk.py ensure    --jlink SERIAL [--port PATH] [--hex IMAGE] [--address ADDR]
    python nrfdk.py advertise --jlink SERIAL [--port PATH] --chmap 37|38|39|all
                              --interval MS --payload HEX|none [--type nonconn|conn]
    python nrfdk.py stop      --jlink SERIAL [--port PATH]
    python nrfdk.py idle      --jlink SERIAL [--port PATH]
    python nrfdk.py status    --jlink SERIAL [--port PATH]

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

# The firmware version the harness expects (firmware/nrf52840dk/src/roles.h
# UTDK_FW_VERSION; tests/test_nrfdk.py keeps them in step).
FIRMWARE_VERSION = "utdk-1.0.0"
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
# After a reset (or a flash) the firmware needs a moment to start Bluetooth
# and the shell.
BOOT_TIMEOUT_S = 15.0
FLASH_TIMEOUT_S = 120.0
TOOL_TIMEOUT_S = 30.0
POLL_S = 0.05

_LINUX_BY_ID = "/dev/serial/by-id/usb-SEGGER_J-Link_{serial:012d}-if00"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ADDRESS = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
_SHELL_ERROR = re.compile(r": (?:command not found|wrong parameter count)$")
_STATUS_KEYS = {
    "role": str, "fw": str, "bt": bool, "address": str, "adv": bool,
    "running": bool, "type": str, "chmap": str, "interval_ms": int,
    "payload": str, "counter": int,
}


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
        return _LINUX_BY_ID.format(serial=serial)
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
    """Line protocol: one command line out; data lines, then OK or ERR <why>."""

    def __init__(self, port, clock=time.monotonic):
        self.port = port
        self.clock = clock
        self._buffer = b""

    @classmethod
    def open(cls, path: str) -> "Shell":
        try:
            import serial  # pyserial, imported lazily
        except ImportError as error:
            raise NrfDkError("pyserial is not installed in this interpreter") from error
        try:
            port = serial.Serial(path, BAUD, timeout=POLL_S, write_timeout=1.0,
                                 exclusive=True)
        except (OSError, ValueError, serial.SerialException) as error:
            raise NrfDkError(f"cannot open {path}: {error}") from error
        return cls(port)

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
        data = []
        for line in self._lines(self.clock() + timeout):
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
    for key, kind in _STATUS_KEYS.items():
        value = status.get(key)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise NrfDkError(f"status has no valid '{key}'")
    return status


# --------------------------------------------------------------------------
# Operations


@dataclass
class Dk:
    serial: int
    port: str
    run: Runner = run_tool
    # Opens the shell on ``port``; tests pass a fake.
    opener: Callable[[str], Shell] | None = None


def resolve(jlink: str, port: str | None, run: Runner = run_tool) -> Dk:
    serial = jlink_serial(jlink)
    path = port or default_port(serial)
    if path is None:
        path = probe_vcom0(find_probe(serial, run))
        if path is None:
            raise NrfDkError(f"J-Link {serial} reports no VCOM0 port; register 'serial device'")
    return Dk(serial, path, run)


def _open(dk: Dk) -> Shell:
    if dk.opener is not None:
        return dk.opener(dk.port)
    if not os.path.exists(dk.port):
        raise NrfDkError(f"{dk.port} does not exist: is J-Link {dk.serial} attached?")
    return Shell.open(dk.port)


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
           version: str = FIRMWARE_VERSION) -> dict:
    """The board runs ``version`` (flashing ``image`` if not), idle, at ``address``."""
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
                    "--artifact \"nRF52840 advertiser=PATH\" with the image to flash")
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
        shell.command("adv defaults")
        status = shell.status()
    finally:
        shell.close()
    if status["role"] != "advertiser":
        raise NrfDkError(f"J-Link {dk.serial} role is {status['role']}, not advertiser")
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


def status_command(dk: Dk, out=None) -> dict:
    shell = _open(dk)
    try:
        status = shell.status()
    finally:
        shell.close()
    print(json.dumps(status, sort_keys=True), file=out)
    _report(status, out)
    return status


def main(argv: Sequence[str] | None = None, run: Runner = run_tool) -> int:
    parser = argparse.ArgumentParser(prog="nrfdk.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {}
    for name in ("identify", "ensure", "advertise", "stop", "idle", "status"):
        command = sub.add_parser(name)
        command.add_argument("--jlink", required=True, help="J-Link serial")
        command.add_argument("--port", help="VCOM0 path (default: by J-Link serial)")
        commands[name] = command
    commands["ensure"].add_argument("--hex", type=Path, help="image to flash if needed")
    commands["ensure"].add_argument("--address", help="expected BLE address")
    advertise_parser = commands["advertise"]
    advertise_parser.add_argument("--chmap", required=True, choices=sorted(CHMAP_BITS))
    advertise_parser.add_argument("--interval", required=True, type=int, help="ms")
    advertise_parser.add_argument("--payload", required=True, help="tag hex, or none")
    advertise_parser.add_argument("--type", default="nonconn", choices=("nonconn", "conn"))
    args = parser.parse_args(argv)
    try:
        dk = resolve(args.jlink, args.port, run)
        if args.command == "identify":
            identify(dk)
        elif args.command == "ensure":
            ensure(dk, args.hex, args.address)
        elif args.command == "advertise":
            advertise(dk, args.chmap, args.interval, args.payload, args.type)
        elif args.command in ("stop", "idle"):
            stop(dk, defaults=args.command == "idle")
        else:
            status_command(dk)
    except NrfDkError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
