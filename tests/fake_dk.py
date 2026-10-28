"""A stand-in for the nRF52840 DK role firmware's shell, for nrfdk.py tests.

FakeFirmware answers the line protocol of firmware/nrf52840dk (README,
"Shell protocol"); FakePort feeds it from pyserial-like write/read calls.
"""

import json
import subprocess

from ubertooth_harness import nrfdk


class FakeFirmware:
    def __init__(self, version=nrfdk.FIRMWARE_VERSION, address="C3:11:22:33:44:55"):
        self.version = version
        self.address = address
        self.commands = []
        self.silent = False
        self.refuse = {}
        self.defaults()
        self.counter = 7

    def defaults(self):
        self.adv = False
        self.type = "nonconn"
        self.chmap = "all"
        self.interval = 100
        self.payload = ""

    def status(self):
        return {
            "role": "advertiser", "fw": self.version, "bt": True,
            "address": self.address, "address_type": "random static",
            "adv": self.adv, "running": self.adv, "type": self.type,
            "chmap": self.chmap, "interval_ms": self.interval,
            "payload": self.payload, "counter": self.counter, "starts": 0,
            "connections": 0, "last_err": 0,
        }

    def handle(self, line: str) -> list[str]:
        self.commands.append(line)
        if self.silent:
            return []
        if line in self.refuse:
            return [f"ERR {self.refuse[line]}"]
        words = line.split()
        if words == ["status"]:
            return [json.dumps(self.status(), separators=(",", ":")), "OK"]
        if words[:1] != ["adv"] or len(words) < 2:
            return [f"{line}: command not found"]
        command, args = words[1], words[2:]
        if command == "start":
            self.adv = True
        elif command == "stop":
            self.adv = False
            self.counter += 2  # time passed while it advertised
        elif command == "defaults":
            self.defaults()
        elif command == "chmap" and args:
            self.chmap = args[0]
        elif command == "interval" and args:
            self.interval = int(args[0])
        elif command == "payload" and args:
            self.payload = "" if args[0] == "none" else args[0].lower()
        elif command == "type" and args:
            self.type = args[0]
        else:
            return ["ERR bad command"]
        return ["OK"]


class FakePort:
    """pyserial-like: write a command line, read the firmware's answer."""

    def __init__(self, firmware: FakeFirmware, noise: bytes = b""):
        self.firmware = firmware
        self.pending = bytearray(noise)
        self.closed = False
        self._line = b""

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, data: bytes):
        self._line += data
        while b"\r" in self._line:
            raw, self._line = self._line.split(b"\r", 1)
            # The shell echoes nothing; the prompt is empty; lines end CRLF.
            for answer in self.firmware.handle(raw.decode()):
                self.pending += answer.encode() + b"\r\n"
        return len(data)

    def flush(self):
        pass

    def read(self, size: int) -> bytes:
        chunk = bytes(self.pending[:size])
        del self.pending[:size]
        return chunk

    def close(self):
        self.closed = True


class FakeClock:
    """Advances on every read of time, so timeouts end without sleeping."""

    def __init__(self, step=0.05):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


def nrfutil_runner(version=nrfdk.NRFUTIL_VERSION, device=nrfdk.NRFUTIL_DEVICE_VERSION,
                   serials=("001050000001",), calls=None, program_returncode=0,
                   on_program=None):
    """A fake run_tool for nrfutil: version, device list, program, reset."""
    def run(argv, timeout):
        argv = list(argv)
        if calls is not None:
            calls.append(argv)
        out, code = "", 0
        if argv == ["nrfutil", "--version"]:
            out = f"nrfutil {version} (350d1fd 2026-08-11)\ncommit-hash: x\n"
        elif argv == ["nrfutil", "device", "--version"]:
            out = f"nrfutil-device {device} (522aec1 2026-10-19)\n"
        elif argv[:3] == ["nrfutil", "device", "list"]:
            devices = [{
                "serialNumber": serial,
                "devkit": {"boardVersion": "PCA10056"},
                "serialPorts": [
                    {"path": f"/dev/fake-{serial}-0", "vcom": 0},
                    {"path": f"/dev/fake-{serial}-1", "vcom": 1},
                ],
            } for serial in serials]
            out = "\n".join([
                json.dumps({"type": "task_begin", "data": {}}),
                json.dumps({"type": "task_end", "data": {"data": {"devices": devices}}}),
            ]) + "\n"
        elif argv[:3] == ["nrfutil", "device", "program"]:
            code = program_returncode
            if on_program is not None:
                on_program(argv)
            if code:
                out = "Error: failed to program\n"
        elif argv[:3] == ["nrfutil", "device", "reset"]:
            pass
        else:
            code = 2
        return subprocess.CompletedProcess(argv, code, out, "")
    return run
