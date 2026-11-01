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
        self.role = "advertiser"
        # Statuses to report a short data length before the real one, so a
        # caller that waits for the connection to settle can be exercised.
        self.slow_data_length = 0
        # The channel classification belongs to the controller: it outlives
        # the link and the role, so it is not part of conn_defaults(). Set
        # data_map_restore_fails to stand for a controller that refuses the
        # restore, which leaves the narrow map in place.
        self.data_map = nrfdk.DATA_MAP_ALL
        self.data_map_sets = 0
        # True until a command has reached the controller, as the firmware's
        # data_map_at starts at 0. A test that wants another map sets it
        # again, standing for the second having passed.
        self.data_map_gap_passed = True
        self.data_map_restore_fails = False
        self.defaults()
        self.conn_defaults()
        self.counter = 7

    def defaults(self):
        self.adv = False
        self.type = "nonconn"
        self.chmap = "all"
        self.interval = 100
        self.payload = ""

    def conn_defaults(self):
        self.conn_on = False
        self.connected = False
        self.peer = ""
        self.conn_interval = 30
        self.update_ms = 0
        self.dle = True
        self.interval_units = 0
        self.latency = 0
        self.timeout_units = 0
        self.tx_octets = 0
        self.rx_octets = 0
        self.mtu = 0
        self.phy = "none"
        self.payload_len = 244
        self.period_ms = 100
        self.conn_chmap = "37"
        self.cycle_ms = 0
        self.autoupdate_ms = 0
        self.writes = 0
        self.received = 0
        self.updates = 0
        self.connects = 0
        self.disconnects = 0

    def status(self):
        tx = self.tx_octets
        if self.connected and self.slow_data_length > 0:
            # The data length update has not landed yet.
            self.slow_data_length -= 1
            tx = 27
        return {
            "role": self.role, "fw": self.version, "bt": True,
            "address": self.address, "address_type": "random static",
            "adv": self.adv, "running": self.adv, "type": self.type,
            "chmap": self.chmap, "interval_ms": self.interval,
            "payload": self.payload, "counter": self.counter, "starts": 0,
            "connections": 0, "last_err": 0,
            "conn_on": self.conn_on, "connected": self.connected,
            "peer": self.peer, "conn_interval_ms": self.conn_interval,
            "update_ms": self.update_ms, "dle": self.dle,
            "interval_units": self.interval_units, "latency": self.latency,
            "timeout_units": self.timeout_units, "tx_octets": tx,
            "rx_octets": self.rx_octets, "mtu": self.mtu, "phy": self.phy,
            "payload_len": self.payload_len, "period_ms": self.period_ms,
            "conn_chmap": self.conn_chmap, "data_map": self.data_map,
            "data_map_used": bin(int(self.data_map, 16)).count("1"),
            "data_map_sets": self.data_map_sets,
            "cycle_ms": self.cycle_ms,
            "autoupdate_ms": self.autoupdate_ms,
            "writes": self.writes, "write_errors": 0, "received": self.received,
            "updates": self.updates, "connects": self.connects,
            "disconnects": self.disconnects, "conn_err": 0,
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
        if words[:1] == ["role"] and len(words) == 2:
            return self._role(words[1])
        if words[:1] == ["conn"]:
            return self._conn(words[1:])
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

    def _role(self, wanted):
        if wanted not in nrfdk.ROLES:
            return ["ERR role must be advertiser, central or peripheral"]
        self.conn_defaults()
        kept = self._restore_data_map()
        self.defaults()
        self.role = wanted
        if kept:
            return ["ERR the controller kept the connection's channel map "
                    "(-22); the board is not idle"]
        return ["OK"]

    def _restore_data_map(self):
        """What the firmware's restore_data_map() does on leaving the role.

        The count and the one-second gap survive it. Returns whether the
        controller kept the narrow map, which the firmware reports as an
        ERR from `role` and `conn defaults`.
        """
        if self.data_map == nrfdk.DATA_MAP_ALL:
            return False
        self.data_map_gap_passed = False
        if self.data_map_restore_fails:
            return True
        self.data_map = nrfdk.DATA_MAP_ALL
        return False

    def _conn(self, words):
        if self.role == "advertiser":
            return [f"ERR role is advertiser; run 'role central' or 'role peripheral'"]
        if not words:
            return ["ERR usage: conn <peer|interval|dle|payload|period|chmap|"
                    "datamap|cycle|autoupdate|start|stop|update|defaults>"]
        command, args = words[0], words[1:]
        if command == "start":
            self.conn_on = True
            if self.role == "central":
                self._connect()
            return ["OK"]
        if command == "stop":
            self.conn_on = False
            self.connected = False
            return ["OK"]
        if command == "defaults":
            self.conn_defaults()
            if self._restore_data_map():
                return ["ERR the controller kept the connection's channel map "
                        "(-22); the board is not idle"]
            return ["OK"]
        if command == "datamap" and args:
            if self.role != "central":
                return ["ERR only the central can change the connection's "
                        "channel map"]
            try:
                wanted = nrfdk.data_map(args[0])
            except nrfdk.NrfDkError:
                return ["ERR channel map must be 'all' or a list of at least "
                        "2 data channels 0-36, for example 0,12,36"]
            if not self.connected:
                return ["ERR no connection to update"]
            if not self.data_map_gap_passed:
                return ["ERR channel map updates must be 1000 ms apart; the "
                        "last one was 0 ms ago"]
            self.data_map = wanted
            self.data_map_sets += 1
            self.data_map_gap_passed = False
            return ["OK"]
        if command == "update" and args:
            if not self.connected:
                return ["ERR no connection to update"]
            self.update_ms = int(args[0])
            self.interval_units = nrfdk.conn_units(int(args[0]))
            self.updates += 1
            return ["OK"]
        if self.conn_on:
            return ["ERR stop the connection before changing settings"]
        if command == "peer" and args:
            self.peer = args[0]
        elif command == "interval" and args:
            self.conn_interval = int(args[0])
        elif command == "dle" and args:
            self.dle = args[0] == "on"
        elif command == "payload" and args:
            self.payload_len = int(args[0])
        elif command == "period" and args:
            self.period_ms = int(args[0])
        elif command == "chmap" and args:
            self.conn_chmap = args[0]
        elif command == "cycle" and args:
            self.cycle_ms = 0 if args[0] == "off" else int(args[0])
        elif command == "autoupdate" and args:
            self.autoupdate_ms = 0 if args[0] == "off" else int(args[0])
        else:
            return [f"ERR unknown conn command '{command}'"]
        return ["OK"]

    def _connect(self):
        self.connected = True
        self.connects += 1
        self.phy = "1M"
        self.mtu = 247
        self.latency = 0
        self.timeout_units = 400
        self.tx_octets = 251 if self.dle else 27
        self.rx_octets = self.tx_octets
        interval = self.autoupdate_ms or self.conn_interval
        # The firmware truncates; so does nrfdk.conn_units().
        self.interval_units = nrfdk.conn_units(interval)
        if self.autoupdate_ms:
            self.update_ms = self.autoupdate_ms
            self.updates += 1
        self.writes += 10
        self.received += 10 * self.payload_len


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
