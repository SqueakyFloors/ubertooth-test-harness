"""A stand-in for the ESP32 Classic peer firmware's shell, for esp32peer.py tests.

FakePeerFirmware answers the line protocol of firmware/esp32-classic-peer
(README, "Shell protocol"); FakePeerPort feeds it from pyserial-like calls.
Two firmwares can be wired to each other with ``link``, so a whole pair
session can be exercised without hardware.
"""

import json

from ubertooth_harness import esp32peer

# The two lines the firmware refuses a malformed `sco` line with
# (SCO_USAGE and SCO_TYPE_USAGE in firmware/esp32-classic-peer/main/peer.h).
# A unit test reads them out of that header, so these cannot drift from it.
SCO_USAGE = "sco takes start, stop, stats, or type <ev3|2ev3|default>"
SCO_TYPE_USAGE = "sco type takes ev3, 2ev3, or default"

# What the ROM prints after every reset, which opening the bridge causes.
BOOT_LOG = (
    "ets Jul 29 2019 12:21:46",
    "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
    "configsip: 0, SPIWP:0xee",
    "load:0x3fff0030,len:5220",
    "entry 0x400805c8",
)


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
        # What the audio link carried since the last reading of this board:
        # a real board counts frames and packets while a session reads it,
        # so each reading here advances the counters once and a session's
        # before and after readings differ (see _tick). A session takes two
        # readings each side of its command, so its deltas are these.
        self.frames_per_read = 100
        self.packets_per_read = 110
        # Which of the controller's counters a reading advances. The
        # default is a link that is working: every packet sent, received,
        # and correct. A test moves the correct ones to the error count to
        # make a link that is not. Which way this board's frames go, the
        # same way: a test drops "rx" for a far end that never answers.
        # Both are the fixture's own settings, not board state, so neither
        # a reset nor `defaults` puts them back.
        self.packets_advance = ("rx_total", "rx_correct", "tx_total")
        self.frames_advance = ("tx", "rx")
        # Readings of the controller's counters this board has answered,
        # and a test's hook to run after the first of them, which is where
        # a session's command runs (`during_the_command`).
        self.stats_reads = 0
        self._hook = None
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
        self._sco_off()

    def _sco_off(self):
        """The audio half of the boot state (firmware README, `sco`)."""
        self.sco = "off"
        self.sco_profile = "none"
        self.sco_codec = "none"
        self.sco_type = esp32peer.AUDIO_TYPE_STACK
        self.sco_link = "none"
        self.sco_pkt_type = "none"
        self.sco_handle = 0
        self.sco_frame_len = 0
        self.sco_tx_frames = 0
        self.sco_tx_errors = 0
        self.sco_rx_frames = 0
        self.sco_rx_bad = 0
        # How many synchronous links this profile has had, and lost. The
        # firmware zeroes both when a profile starts, so they count this
        # session's links and nothing earlier.
        self.sco_connects = 0
        self.sco_disconnects = 0
        self.sco_tx_len = 0
        self.sco_interval = 0
        # The controller's own packet counters (`sco stats`), which keep
        # counting across a link going down and coming back as the real
        # ones do.
        self.sco_packets = dict.fromkeys(esp32peer.AUDIO_PKT_COUNTERS, 0)

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

    def status(self) -> dict:
        lap, uap = esp32peer.address_parts(self.address)
        nap = "".join(self.address.split(":")[:2]).lower()
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
            "sco": self.sco, "sco_profile": self.sco_profile,
            "sco_codec": self.sco_codec, "sco_type": self.sco_type,
            "sco_link": self.sco_link, "sco_pkt_type": self.sco_pkt_type,
            "sco_handle": self.sco_handle,
            "sco_frame_len": self.sco_frame_len,
            "sco_tx_frames": self.sco_tx_frames,
            "sco_tx_errors": self.sco_tx_errors,
            "sco_rx_frames": self.sco_rx_frames,
            "sco_rx_bad": self.sco_rx_bad,
            "sco_connects": self.sco_connects,
            "sco_disconnects": self.sco_disconnects,
            "sco_tx_len": self.sco_tx_len, "sco_interval": self.sco_interval,
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
            self._tick()
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
        # The audio link belongs to a role, so it goes down with the old
        # one; the chosen packet type stays, and only `defaults` puts that
        # back (bt_set_role() and bt_defaults() in the firmware).
        self._sco_down()
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

    # What each packet type costs on the air, as the bench measured it: an
    # EV3 link carries 30 bytes every 6 slots, a 2-EV3 one 60 every 12.
    _AUDIO_LINK = {"ev3": ("ev3", 30, 6), "2ev3": ("2-ev3", 60, 12)}

    def _tick(self):
        """What the link carried since this board was last read.

        A board counts while a session is reading it, so each reading
        advances the counters once - the status read, which is where a
        reading begins; that is what gives a session's before and after
        readings something to differ by. The controller's packet counters
        advance with the frames: on this link one packet carries one frame.

        Reads standing in for elapsed time is a deliberate simplification -
        there is no clock here to count against, and a session's command
        takes no measurable time in a test. A window that carried nothing
        is modelled by setting `frames_per_read` and `packets_per_read` to
        zero, which is what the tests for an empty window do.
        """
        if self.sco != esp32peer.AUDIO_ON:
            return
        if "tx" in self.frames_advance:
            self.sco_tx_frames += self.frames_per_read
        if "rx" in self.frames_advance:
            self.sco_rx_frames += self.frames_per_read
        for key in self.packets_advance:
            self.sco_packets[key] += self.packets_per_read

    def during_the_command(self, hook) -> None:
        """Run ``hook`` once, where a session's command runs.

        A session reads the controller's counters on both boards just
        before its command and again just after it, so the hook fires once
        both boards have answered the first of those. Whatever it does then
        happened while the command was running, whichever board of the pair
        it was registered on.
        """
        # From zero, so a fixture used for a second session in one test
        # does not fire the hook on the first board's baseline because the
        # far board still carries a count from the session before.
        for board in (self, self.peer_firmware):
            if board is not None:
                board.stats_reads = 0
        self._hook = hook

    def _fire_hook(self):
        far = self.peer_firmware
        if self._hook is None or self.stats_reads < 1:
            return
        if far is not None and far.stats_reads < 1:
            return
        hook, self._hook = self._hook, None
        hook()

    def flap(self):
        """The link goes down and comes straight back, counted as the
        firmware counts it: both link counters step and the handle changes.
        """
        for board in (self, self.peer_firmware):
            if board is not None and board.sco == esp32peer.AUDIO_ON:
                board.sco_connects += 1
                board.sco_disconnects += 1
                board.sco_handle += 1

    def drop(self):
        """The link goes down and stays down, on both boards.

        Each keeps the profile it started, so each reads `ready` and
        refuses `sco stats`, which needs a live link.
        """
        for board in (self, self.peer_firmware):
            if board is not None and board.sco == esp32peer.AUDIO_ON:
                board.sco = "ready"
                board.sco_disconnects += 1
                board.sco_codec = "none"
                board.sco_link = "none"
                board.sco_pkt_type = "none"
                board.sco_handle = 0

    def _sco(self, words):
        """The firmware's `sco` branch, with every arity it refuses.

        A line that is none of the four forms answers one usage line and
        changes nothing: no role, profile, link, packet type, or counter.
        The two usage lines are the firmware's own, and
        tests/test_esp32peer.py reads them out of `main/peer.h` so the two
        cannot drift apart (#262).
        """
        if words == ["start"]:
            return self._sco_start()
        if words == ["stop"]:
            self._sco_down()
            return ["OK"]
        if words == ["stats"]:
            return self._sco_stats()
        if words[:1] == ["type"]:
            if len(words) != 2:
                return [f"ERR {SCO_TYPE_USAGE}"]
            return self._sco_set_type(words[1])
        return [f"ERR {SCO_USAGE}"]

    def _sco_set_type(self, wanted):
        if wanted not in esp32peer.AUDIO_TYPES:
            return [f"ERR {SCO_TYPE_USAGE}"]
        if self.sco != "off":
            return ["ERR stop the audio profile with \"sco stop\" before "
                    "choosing a packet type"]
        self.sco_type = wanted
        return ["OK"]

    def _sco_stats(self):
        if self.sco != esp32peer.AUDIO_ON:
            return ["ERR no audio link to count packets on"]
        # No advance here: a reading is a status and then these counters,
        # and the two belong to the same moment. Asking twice in a row does
        # not make a board's counters jump.
        answer = [json.dumps(self.sco_packets, separators=(",", ":")), "OK"]
        self.stats_reads += 1
        # Either board's reading may be the one that completes the pair's,
        # so both are offered the chance to fire.
        self._fire_hook()
        if self.peer_firmware is not None:
            self.peer_firmware._fire_hook()
        return answer

    def _sco_start(self):
        if self.role == esp32peer.BOOT_ROLE:
            # The hands-free unit only starts its profile and waits.
            self.sco = "ready"
            self.sco_profile = "hf"
            return ["OK"]
        far = self.peer_firmware
        if far is None or far.address != self.peer:
            return ["ERR no peer address set"]
        if far.sco != "ready":
            return ["ERR the peer is not ready for an audio link"]
        # Each board states the types it allows and the link uses one both
        # allowed, so two boards asking for the same type get it and
        # anything else falls back to what the stack asks for.
        wanted = (self.sco_type if self.sco_type == far.sco_type
                  else esp32peer.AUDIO_TYPE_STACK)
        name, length, interval = self._AUDIO_LINK.get(wanted,
                                                      self._AUDIO_LINK["2ev3"])
        self.sco_profile = "ag"
        for board in (self, far):
            board.sco = esp32peer.AUDIO_ON
            board.sco_codec = "cvsd"
            board.sco_link = "esco"
            board.sco_pkt_type = name
            board.sco_handle = 1
            board.sco_frame_len = 120
            board.sco_tx_len = length
            board.sco_interval = interval
            board.sco_connects += 1
        return [f"utesp: sco connected, {name}", "OK"]

    def _sco_down(self):
        """Stop this board's audio profile, keeping its chosen packet type.

        The far end goes with it only when a link was really up between
        them: a board that has merely started its profile and is waiting is
        nobody else's business.
        """
        linked = self.sco == esp32peer.AUDIO_ON
        far = self.peer_firmware
        for board in (self, far if linked else None):
            if board is not None and board.sco != "off":
                wanted = board.sco_type
                board._sco_off()
                board.sco_type = wanted


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
