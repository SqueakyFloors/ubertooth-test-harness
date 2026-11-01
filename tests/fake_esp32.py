"""A stand-in for the ESP32 Classic peer firmware's shell, for esp32peer.py tests.

FakePeerFirmware answers the line protocol of firmware/esp32-classic-peer
(README, "Shell protocol"); FakePeerPort feeds it from pyserial-like calls.
Two firmwares can be wired to each other with ``link``, so a whole pair
session can be exercised without hardware.
"""

import json

from ubertooth_harness import esp32peer

# What the ROM prints after every reset, which opening the bridge causes.
BOOT_LOG = (
    "ets Jul 29 2019 12:21:46",
    "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
    "configsip: 0, SPIWP:0xee",
    "load:0x3fff0030,len:5220",
    "entry 0x400805c8",
)


# What a link of each packet type settles on, as both were measured on the
# bench (firmware README, "What the link settled on"). An EV3 packet carries
# half the payload of a 2-EV3, so the interval halves to keep the same rate.
LINKS = {
    "ev3": {"sco_pkt_type": "ev3", "sco_tx_len": 30, "sco_rx_len": 30,
            "sco_interval": 6, "sco_retrans_window": 4, "sco_frame_len": 60},
    "2ev3": {"sco_pkt_type": "2-ev3", "sco_tx_len": 60, "sco_rx_len": 60,
             "sco_interval": 12, "sco_retrans_window": 2,
             "sco_frame_len": 120},
}


def negotiated_type(mine, theirs):
    """The type a link between two boards uses, or None if there is none.

    Each board states the types it allows and the link uses one both allowed,
    so two boards naming different types have nothing in common. `default`
    allows what the stack's table does, which is 2-EV3.
    """
    allowed = [{esp32peer.DEFAULT_SCO_TYPE: "2ev3"}.get(asked, asked)
               for asked in (mine, theirs)]
    return allowed[0] if allowed[0] == allowed[1] else None


class FakePeerFirmware:
    def __init__(self, address="C8:85:41:2A:1B:2A",
                 version=esp32peer.MINIMUM_FIRMWARE, start_failure=""):
        self.address = address
        self.version = version
        self.start_failure = start_failure
        self.commands = []
        self.silent = False
        self.refuse = {}
        self.resets = 0
        # Bytes the far end receives for each write this board makes, and
        # writes this board reports per `stream on`; the bench numbers are
        # much larger, so a test asks for small floors.
        self.writes_per_stream = 100
        # Frames, and controller packets, one audio link stands for; the
        # bench numbers are in the thousands.
        self.frames_per_link = 50
        # The packet types these controllers support, as the firmware reports
        # them: HV1, HV3, EV3, EV4, and EV5, and no bit forbidding an EDR type.
        self.sco_types_supported = "003d"
        self.peer_firmware = None
        self.defaults()

    def defaults(self):
        self.role = esp32peer.BOOT_ROLE
        self.discoverable = True
        self.spp_server = True
        self.connecting = False
        self.connected = False
        self.peer = ""
        self.remote = ""
        self.stream = False
        self.period = 100
        self.payload = 100
        self.writes = 0
        self.write_errors = 0
        self.rx_bytes = 0
        self.connects = 0
        self.disconnects = 0
        self.sco_profile = "none"
        self.sco_type = esp32peer.DEFAULT_SCO_TYPE
        self.sco_tx_frames = 0
        self.sco_tx_errors = 0
        self.sco_rx_frames = 0
        self.sco_rx_bad = 0
        self.sco_packets = 0
        self._audio_link_down()

    def _audio_link_down(self):
        """Everything the audio link itself sets, with the counters left alone."""
        self.sco = "ready" if self.sco_profile != "none" else "off"
        self.sco_link = "none"
        self.sco_pkt_type = "none"
        self.sco_codec = "none"
        self.sco_handle = 0
        self.sco_frame_len = 0
        self.sco_tx_len = 0
        self.sco_rx_len = 0
        self.sco_interval = 0
        self.sco_retrans_window = 0

    def has_audio(self) -> bool:
        """Whether this image has the audio link at all, as its version says."""
        return (esp32peer.version_parts(self.version)
                >= esp32peer.version_parts(esp32peer.MINIMUM_AUDIO_FIRMWARE))

    def link(self, other: "FakePeerFirmware") -> None:
        """Let `spp start` on this board connect to ``other``."""
        self.peer_firmware = other
        other.peer_firmware = self

    # -- the board's own output ---------------------------------------------

    def boot(self) -> list[str]:
        """The lines a reset produces: the ROM's log, then the boot notice."""
        self.resets += 1
        self.defaults()
        if self.start_failure:
            return [*BOOT_LOG, f"utesp: {self.version} start failed: "
                               f"{self.start_failure}"]
        return [*BOOT_LOG, f"utesp: {self.version} ready"]

    def sco_stats(self) -> dict:
        """The controller's own packet counters, as `sco stats` answers."""
        return {"rx_total": self.sco_packets, "rx_correct": self.sco_packets,
                "rx_err": 0, "rx_none": 0, "rx_lost": 0,
                "tx_total": self.sco_packets, "tx_discarded": 0}

    def status(self) -> dict:
        lap, uap = esp32peer.address_parts(self.address)
        nap = "".join(self.address.split(":")[:2]).lower()
        if self.has_audio():
            return {**self._spp_status(lap, uap, nap), **self._audio_status()}
        return self._spp_status(lap, uap, nap)

    def _audio_status(self) -> dict:
        return {
            "sco": self.sco, "sco_profile": self.sco_profile,
            "sco_codec": self.sco_codec, "sco_handle": self.sco_handle,
            "sco_frame_len": self.sco_frame_len,
            "sco_tx_frames": self.sco_tx_frames,
            "sco_tx_errors": self.sco_tx_errors,
            "sco_rx_frames": self.sco_rx_frames, "sco_rx_bad": self.sco_rx_bad,
            "sco_type": self.sco_type, "sco_link": self.sco_link,
            "sco_pkt_type": self.sco_pkt_type, "sco_tx_len": self.sco_tx_len,
            "sco_rx_len": self.sco_rx_len, "sco_interval": self.sco_interval,
            "sco_retrans_window": self.sco_retrans_window,
            "sco_types_supported": self.sco_types_supported,
        }

    def _spp_status(self, lap, uap, nap) -> dict:
        return {
            "role": self.role, "fw": self.version, "bt": not self.start_failure,
            "address": self.address, "lap": lap, "uap": uap, "nap": nap,
            "name": f"UT-ESP32-{lap.upper()}", "discoverable": self.discoverable,
            "spp_server": self.spp_server, "connecting": self.connecting,
            "connected": self.connected, "peer": self.peer,
            "remote": self.remote, "stream": self.stream,
            "period_ms": self.period, "payload_len": self.payload,
            "counter": self.writes, "writes": self.writes,
            "write_errors": self.write_errors, "rx_bytes": self.rx_bytes,
            "connects": self.connects, "disconnects": self.disconnects,
            "last_err": 0,
        }

    # -- the shell ----------------------------------------------------------

    def handle(self, line: str) -> list[str]:
        self.commands.append(line)
        if self.silent:
            return []
        if line in self.refuse:
            return [f"ERR {self.refuse[line]}"]
        words = line.split()
        if words == ["status"]:
            return [json.dumps(self.status(), separators=(",", ":")), "OK"]
        if words == ["version"]:
            return [self.version, "OK"]
        if words[:1] == ["role"] and len(words) == 2:
            return self._role(words[1])
        if words[:1] == ["spp"]:
            return self._spp(words[1:])
        if words[:1] == ["stream"]:
            return self._stream(words[1:])
        if words[:1] == ["sco"]:
            if not self.has_audio():
                return ["ERR unknown command 'sco'"]
            return self._sco(words[1:])
        if words == ["defaults"]:
            self.defaults()
            return ["OK"]
        return [f"ERR unknown command '{words[0] if words else ''}'"]

    def _role(self, wanted):
        if wanted not in esp32peer.ROLES:
            return ["ERR role must be acceptor or initiator"]
        self.stream = False
        self._drop()
        # The audio link belongs to a role, so a role change takes it and the
        # profile down and puts the packet type back.
        self._sco_stop()
        self.sco_type = esp32peer.DEFAULT_SCO_TYPE
        self.role = wanted
        self.spp_server = wanted == esp32peer.BOOT_ROLE
        return ["OK"]

    def _spp(self, words):
        if not words:
            return ["ERR usage: spp <peer|start|stop>"]
        command, args = words[0], words[1:]
        if command == "peer" and args:
            try:
                self.peer = esp32peer.normalize_address(args[0])
            except esp32peer.PeerError:
                return ["ERR address must be aa:bb:cc:dd:ee:ff"]
            return ["OK"]
        if command == "start":
            return self._start()
        if command == "stop":
            self.stream = False
            self._drop()
            self.spp_server = self.role == esp32peer.BOOT_ROLE
            return ["OK"]
        return [f"ERR unknown spp command '{command}'"]

    def _start(self):
        if self.role == esp32peer.BOOT_ROLE:
            self.spp_server = True
            return ["OK"]
        if not self.peer:
            return ["ERR no peer address set"]
        far = self.peer_firmware
        if far is None or far.address != self.peer:
            return ["ERR connect timed out"]
        self._connect(far.address)
        far._connect(self.address)
        return [f"utesp: spp connected {far.address}", "OK"]

    def _connect(self, remote):
        self.connected = True
        self.remote = remote
        self.connects += 1

    def _drop(self):
        if self.connected:
            self.connected = False
            self.remote = ""
            far = self.peer_firmware
            if far is not None and far.connected:
                far.connected = False
                far.remote = ""

    def _stream(self, words):
        if not words:
            return ["ERR usage: stream <on|off|period|payload>"]
        command, args = words[0], words[1:]
        if command == "on":
            if not self.connected:
                return ["ERR no connection to stream on"]
            self.stream = True
            # One `stream on` stands for the whole run's traffic.
            self.writes += self.writes_per_stream
            far = self.peer_firmware
            if far is not None:
                far.rx_bytes += self.writes_per_stream * self.payload
            return ["OK"]
        if command == "off":
            self.stream = False
            return ["OK"]
        if command == "period" and args:
            self.period = int(args[0])
            return ["OK"]
        if command == "payload" and args:
            self.payload = int(args[0])
            return ["OK"]
        return [f"ERR unknown stream command '{command}'"]

    # -- the audio link -----------------------------------------------------

    def _sco(self, words):
        if not words:
            return ["ERR usage: sco <start|stop|stats|type>"]
        command, args = words[0], words[1:]
        if command == "type" and args:
            if args[0] not in esp32peer.SCO_TYPES:
                return ["ERR packet type must be ev3, 2ev3 or default"]
            if self.sco_profile != "none":
                return ['ERR stop the audio profile with "sco stop" before '
                        "choosing a packet type"]
            self.sco_type = args[0]
            return ["OK"]
        if command == "start":
            return self._sco_start()
        if command == "stop":
            self._sco_stop()
            return ["OK"]
        if command == "stats":
            return [json.dumps(self.sco_stats(), separators=(",", ":")), "OK"]
        return [f"ERR unknown sco command '{command}'"]

    def _sco_start(self):
        if self.role == esp32peer.BOOT_ROLE:
            # The hands-free unit starts its profile and waits to be paged.
            self.sco_profile = "hf"
            if self.sco == "off":
                self.sco = "ready"
            return ["OK"]
        self.sco_profile = "ag"
        far = self.peer_firmware
        if not self.peer or far is None or far.address != self.peer:
            self.sco_profile = "none"
            self._audio_link_down()
            return ["ERR service level connect timed out"]
        if far.sco_profile != "hf":
            # Nothing is listening for an audio link on the far board.
            self.sco_profile = "none"
            self._audio_link_down()
            return ["ERR audio connect timed out"]
        kind = negotiated_type(self.sco_type, far.sco_type)
        if kind is None:
            self.sco_profile = "none"
            self._audio_link_down()
            return ["ERR the controller did not open a synchronous link"]
        self._sco_up(kind)
        far._sco_up(kind)
        return [f"utesp: sco connected, {LINKS[kind]['sco_pkt_type']}", "OK"]

    def _sco_up(self, kind):
        self.sco = "on"
        self.sco_link = "esco"
        self.sco_codec = "cvsd"
        self.sco_handle = 129
        for field, value in LINKS[kind].items():
            setattr(self, field, value)
        # One link stands for the whole run's audio: each side answers every
        # frame it receives with one of its own, so both counters move.
        self.sco_tx_frames += self.frames_per_link
        self.sco_rx_frames += self.frames_per_link
        self.sco_packets += self.frames_per_link

    def _sco_stop(self):
        far = self.peer_firmware
        if self.sco == "on" and far is not None and far.sco == "on":
            far._audio_link_down()
        self.sco_profile = "none"
        self._audio_link_down()


class FakePeerPort:
    """pyserial-like: the open and the reset pulse both reboot the board."""

    def __init__(self, firmware: FakePeerFirmware):
        self.firmware = firmware
        self.pending = bytearray()
        self.closed = False
        self.dtr = False
        self._rts = False
        self._line = b""
        self._boot()

    def _boot(self):
        for line in self.firmware.boot():
            self.pending += line.encode() + b"\r\n"

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        # EN is pulled low while RTS is asserted; the board boots when it
        # is released.
        released = self._rts and not value
        self._rts = value
        if released:
            self.pending.clear()
            self._boot()

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, data: bytes):
        self._line += data
        while b"\n" in self._line:
            raw, self._line = self._line.split(b"\n", 1)
            for answer in self.firmware.handle(raw.decode().strip()):
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
    """Advances on every reading, so timeouts end without sleeping."""

    def __init__(self, step=0.05):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


def shell(firmware: FakePeerFirmware, label="peer") -> esp32peer.Shell:
    return esp32peer.Shell(FakePeerPort(firmware), label, FakeClock())


def opener(devices):
    """An esp32peer.Peer.opener: a shell per registered device path."""
    def open_one(device, label):
        try:
            firmware = devices[device]
        except KeyError:
            raise esp32peer.PeerError(f"{device} does not exist") from None
        return shell(firmware, label)
    return open_one
