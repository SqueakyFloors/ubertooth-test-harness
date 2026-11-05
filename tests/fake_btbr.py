"""A stand-in for `ubertooth-btbr`'s console, for btbrconsole.py tests.

The real tool opens a prompt on a board carrying the `btbr` firmware image.
This writes a script that answers the same lines on the same streams: capture
records on standard output, the prompt and the progress on standard error.
The wording of each line is the tool's, read from its sources and its man
page; every address and device name here is made up, including the second
device an inquiry hears, which stands in for whatever else is in range of a
bench. ``faults`` makes the fake misbehave one way at a time, so each check in
btbrconsole.py can be shown to fail for its own reason.

The transmit formats are written out here a second time, from the same man
page the driver's own table comes from. That is deliberate: the point of the
transmit check is that the two agree, so a single table shared between them
would check nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.executables import write_executable

# The peer the fake answers for: a made-up address in the range the ESP32
# boards use, and the name the peer firmware would build from it.
PEER_ADDRESS = "C8:85:41:2A:1B:2A"
PEER_NAME = "UT-ESP32-2A1B2A"
# A second device an inquiry hears, so a test can show that the driver counts
# the others without naming them. Made up: a locally administered address and
# a name that is nobody's product.
OTHER_ADDRESS = "020000000002"
OTHER_NAME = "Example Device"

# One fault per run, named in the ``faults`` list:
#   no-usb              the startup line the btbr image answers never comes
#   no-prompt           the intro line arrives and the prompt does not
#   no-help             `?` prints no command list
#   short-help          the list leaves `page_scan` out
#   takes-bad-address   a malformed address is accepted instead of refused
#   drops-leading-zeros  an address echo loses its leading zero byte
#   pages-without-address  `page` with no address starts paging anyway
#   silent-inquiry      the inquiry hears another device and not the peer
#   unnamed-peer        the peer answers without its name in the response
#   deaf-peer           the page is never answered
#   no-lmp-name         the connection carries no LMP_NAME_RES for the peer
#   repeat-unhandled    an unhandled LMP opcode is reported twice
#   capture-on-stderr   a capture record goes to the progress stream
#   stuck-idle          `idle` never reports the command stopped
#   bad-exit            the session exits non-zero
#   never-exits         the session ignores `exit`
#   quiet-lmp           the connection brings no unhandled LMP request
#   says-why            it cannot open the board and says so in one error line
#   no-libusb1          the interpreter has no libusb1 package
#   late-eir            the firmware's EIR line lands after the intro
#   stray-packet        a warning on the progress stream renders a packet
#   takes-bad-tx        a length the format will not carry is transmitted
#   wrong-tx-payload    the payload printed is not the one the format gives
#   slot-not-sent       the board reports a transmit it did not put on the air
#   scheduler-storm     it reports that transmit over and over
#   tx-already-waiting  it refuses a tx this session sent
#   dead-link           the connection carries nothing after the transmit runs
#   second-page-fails   a page after the first is never answered
#   false-connection    a connection is reported and then the page resumes
#   stuck-scan          a scan mode never reports itself ready
#   hangs-up            this end detaches from a peer that took the connection
#   takes-bad-option    an option value outside the radio's field is accepted
#   opens-board-first   the board is looked for before the value is checked

_SCRIPT = '''#!/usr/bin/env python3
"""A fake ubertooth-btbr console. See tests/fake_btbr.py."""
import json, os, re, sys, threading, time

CONFIG = json.loads(os.environ["FAKE_BTBR_CONFIG"])
FAULTS = set(CONFIG["faults"])
PEER = CONFIG["peer"].replace(":", "").lower()
NAME = CONFIG["name"]
OTHER = CONFIG["other"]
OTHER_NAME = CONFIG["other_name"]

WRITE = threading.Lock()

def err(text):
    # Carriage return and newline, as the tool itself ends these lines: a
    # driver that anchors a pattern on the end of a line has to cope.
    with WRITE:
        sys.stderr.write(text + "\\r\\n")
        sys.stderr.flush()

def out(text):
    with WRITE:
        sys.stdout.write("09:11:00 " + text + "\\n")
        sys.stdout.flush()

def prompt():
    if "no-prompt" not in FAULTS:
        with WRITE:
            sys.stderr.write("bt> ")
            sys.stderr.flush()

# ---- the command line, which answers before any board is looked for -------

# The fields the two option values are written to, six bits and two.
HIGHEST = {"-f": 63, "-b": 3}

def board_stage(selector, leaving=True):
    """The refusal a selector naming no board gives, at the device stage."""
    err("error: no Ubertooth at index %s (6 attached)" % selector)
    err("note: indexes change when boards are added or removed; select a "
        "board by serial with -U<serial>")
    if leaving:
        sys.exit(1)

def read_command_line():
    """Refuse an option value outside its field, before the board stage."""
    words = sys.argv[1:]
    selector = None
    while words:
        word = words.pop(0)
        value = words.pop(0) if words else ""
        if word == "-U":
            selector = value
            continue
        if word in HIGHEST and "takes-bad-option" not in FAULTS:
            if "opens-board-first" in FAULTS and selector is not None:
                # A tool that went looking for the board before it read the
                # value: the refusal is still printed, but after a line that
                # says a board was already looked for.
                board_stage(selector, leaving=False)
            try:
                number = int(value)
            except ValueError:
                number = None
            if number is None or number < 0 or number > HIGHEST[word]:
                err("error: %s must be 0-%d (got %s)"
                    % (word, HIGHEST[word], value))
                sys.exit(1)
    # An all-digits selector is an index, and these runs give one no bench has
    # a board at, so the board stage is where such a run ends. A serial
    # selector is the session's own, and goes on to the prompt below.
    if selector is not None and selector.isdigit():
        board_stage(selector)

read_command_line()

# ---- the transmit formats, as the man page gives them ---------------------

# name: (least, most, payload header bytes, voice bytes, air slots)
TX_FORMATS = {
    "dm1": (0, 17, 1, 0, 1), "dh1": (0, 27, 1, 0, 1),
    "dm3": (0, 121, 2, 0, 3), "dh3": (0, 183, 2, 0, 3),
    "dm5": (0, 224, 2, 0, 5), "dh5": (0, 339, 2, 0, 5),
    "aux1": (0, 29, 1, 0, 1), "dv": (0, 9, 1, 10, 1),
    "hv1": (10, 10, 0, 0, 1), "hv2": (20, 20, 0, 0, 1),
    "hv3": (30, 30, 0, 0, 1), "ev3": (1, 30, 0, 0, 1),
    "ev4": (1, 120, 0, 0, 3), "ev5": (1, 180, 0, 0, 3),
}
MAX_SLOTS = 400
FORMAT_LIST = ", ".join(sorted(TX_FORMATS))

def fill(count, base=0):
    return bytes((base + index) & 0xff for index in range(count))

def payload_of(name, data):
    least, most, hlen, voice, slots = TX_FORMATS[name]
    payload = fill(voice, 0xa0)
    if hlen:
        word = (len(data) << 3) | (1 << 2) | 2
        payload += word.to_bytes(hlen, "little")
    return payload + data

def byte_count(n):
    return "%d byte%s" % (n, "" if n == 1 else "s")

def do_tx(rest, connected):
    """Answer one `tx` command line, in the order the tool checks it."""
    words = rest.split()
    if not words:
        return err("error: tx needs a format: %s" % FORMAT_LIST)
    if len(words) > 4:
        return err("error: tx takes a format, a length, a count and a "
                   "payload (got %d words)" % len(words))
    name = words[0].lower()
    if name not in TX_FORMATS:
        return err("error: no such format '%s'; one of %s"
                   % (words[0], FORMAT_LIST))
    least, most, hlen, voice, slots = TX_FORMATS[name]
    length = most
    if len(words) > 1:
        try:
            length = int(words[1], 0)
        except ValueError:
            return err("error: length must be a number (got %s)" % words[1])
        if (length < least or length > most) and "takes-bad-tx" not in FAULTS:
            return err("error: %s carries %d to %d data bytes, not %d"
                       % (name, least, most, length))
    count = 1
    if len(words) > 2:
        try:
            count = int(words[2], 0)
        except ValueError:
            return err("error: count must be a number (got %s)" % words[2])
        allowed = MAX_SLOTS // slots
        if count < 1 or count > allowed:
            return err("error: count must be 1 to %d for %s (got %d)"
                       % (allowed, name, count))
    if len(words) > 3:
        try:
            data = bytes.fromhex(words[3])
        except ValueError:
            return err("error: payload must be hex digits, an even number of "
                       "them (got %s)" % words[3])
        if len(data) != length:
            return err("error: payload of %s where the length says %d"
                       % (byte_count(len(data)), length))
    else:
        data = fill(length)
    if not connected:
        return err("error: tx needs a connection: page a device, or "
                   "page_scan and let one page this board")
    payload = payload_of(name, data)
    if "wrong-tx-payload" in FAULTS:
        payload = payload[:-1] + bytes([payload[-1] ^ 0xff]) if payload else b"\\x00"
    if "slot-not-sent" in FAULTS:
        err("TX: cannot schedule transmit, packet not sent")
    if "scheduler-storm" in FAULTS:
        for _ in range(5):
            err("TX: cannot schedule transmit, packet not sent")
    if "tx-already-waiting" in FAULTS:
        err("Tx packet already waiting, send this one after it")
    out("TX pkt: %s len=%d count=%d data=%s payload=%s"
        % (name, len(data), count, data.hex(), payload.hex()))

# ---- the session ---------------------------------------------------------

def response(address, name):
    out("RX FHS: FHS: bdaddr=%s parity=2c43c0d1b cls=1f00 ltaddr=0 clk=4706"
        % address)
    out("ACL(llid=L2CAP_START, flow=0, len=23): 10 09 "
        + " ".join("%02x" % byte for byte in name.encode()))
    out("COMPLETE_LOCAL_NAME: '%s'" % name)

HELP = ["EOF", "discoverable", "help", "inquiry", "l2ping", "page",
        "bdaddr", "exit", "idle", "inquiry_scan", "monitor", "page_scan"]

# The prompt takes six bytes written with the five colons or with none, and
# nothing else: the tool's own pattern.
BDADDR = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\\Z"
                    r"|[0-9A-Fa-f]{12}\\Z")

# The two scan modes: the line the tool prints as each starts, the state the
# board reports, the ready line, and what `idle` ends with.
SCANS = {
    "page_scan": ("BTCtlPageScanCmd", "PAGE_SCAN", "Page Scan state Ready",
                  "Page scan done", "page scan canceled"),
    "inquiry_scan": ("BTCtlInquiryScanCmd", "INQUIRY_SCAN",
                     "Inquiry Scan state Ready", "Inquiry scan done",
                     "inq scan canceled"),
}

if "says-why" in FAULTS:
    err("error: Ubertooth 0123 is in use by another program (libusb -6)")
    sys.exit(1)
if "no-libusb1" in FAULTS:
    err("error: could not start the session: No module named 'usb1'")
    err('note: a session needs the Python libusb1 package, which ships the '
        'usb1 module; install it with "pip install libusb1"')
    sys.exit(1)
err("Rx thread started")
if "no-usb" not in FAULTS:
    err("USB connected")
err("Send idle")
err("State STANDBY -> STANDBY")
err("Set bdaddr 112233445566 -> sw b11155996c38a7bd")
if "late-eir" not in FAULTS:
    err("EIR updated")
err("Type ? to list commands")
if "late-eir" in FAULTS:
    # The firmware line the receive thread ferries up, after the intro.
    err("EIR updated")
prompt()

running = None
status = 0
pages = 0
link = None
# Lines the board prints on its own while a command runs, delivered when the
# next one arrives.
pending = []

def carry_the_link(stop):
    """While the connection is up it carries packets, as a real one does.

    The peer asks for power control several times a second and this link
    layer refuses each one, so the capture stream keeps filling for as long
    as the link lasts.
    """
    while not stop.wait(0.2):
        out("RX LMP: BBHdr(lt_addr=1, type=DM1, flags=FA) | (tid 1) "
            "LMP_DECR_POWER_REQ | 41 01")

def start_the_link():
    global link
    stop = threading.Event()
    thread = threading.Thread(target=carry_the_link, args=(stop,), daemon=True)
    link = stop
    if "dead-link" not in FAULTS:
        thread.start()

def stop_the_link():
    global link
    if link is not None:
        link.set()
        link = None

while True:
    # One line at a time: `for line in sys.stdin` reads in blocks, which
    # would hold every answer back until the driver closed the pipe.
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    while pending:
        err(pending.pop(0))
    word, _, rest = line.partition(" ")
    rest = rest.strip()
    if word in ("exit", "q", "x"):
        if "never-exits" in FAULTS:
            while True:
                time.sleep(0.2)
        stop_the_link()
        err("Send idle")
        break
    if word == "?":
        if rest:
            if rest == "inquiry":
                err("inquiry: Discover nearby devices")
            else:
                err("error: no help on '%s'" % rest)
        elif "no-help" not in FAULTS:
            err("Documented commands (type help <topic>):")
            names = [n for n in HELP
                     if not ("short-help" in FAULTS and n == "page_scan")]
            err("========================================")
            err("  ".join(names))
        prompt()
        continue
    if word == "idle":
        if "stuck-idle" not in FAULTS:
            err("Send idle")
            if running == "inquiry":
                err("State INQUIRY -> STANDBY")
                err("Inquiry done")
            elif running == "page":
                stop_the_link()
                err("State CONNECTED -> STANDBY")
                err("Paging done")
            elif running in SCANS:
                _cmd, state, _ready, done, canceled = SCANS[running]
                err(canceled)
                err("State %s -> STANDBY" % state)
                err(done)
            else:
                err("State STANDBY -> STANDBY")
        running = None
        prompt()
        continue
    if word == "tx":
        do_tx(rest, running == "page")
        prompt()
        continue
    if word in SCANS:
        command, state, ready, _done, _canceled = SCANS[word]
        err("Starting %s" % command)
        prompt()
        err("State STANDBY -> %s" % state)
        if "stuck-scan" not in FAULTS:
            err(ready)
        running = word
        continue
    if word in ("bdaddr", "page", "monitor", "l2ping"):
        digits = rest.replace(":", "")
        good = BDADDR.match(rest) is not None
        if not good and "takes-bad-address" not in FAULTS:
            err("error: BD_ADDR must be xx:xx:xx:xx:xx:xx (got %s)" % rest)
            if word == "page" and "pages-without-address" in FAULTS:
                err("Paging started")
            prompt()
            continue
        if word == "bdaddr":
            echoed = digits.lower()
            if "drops-leading-zeros" in FAULTS:
                echoed = echoed.lstrip("0") or "0"
            err("Set bdaddr %s -> sw b0a86caac43c0d1b" % echoed)
            prompt()
            continue
        pages += 1
        err("Starting BTCtlPagingCmd")
        prompt()
        err("State STANDBY -> PAGE")
        err("Paging started")
        running = "page"
        deaf = ("deaf-peer" in FAULTS
                or ("second-page-fails" in FAULTS and pages > 1))
        if digits.lower() != PEER or deaf:
            err("paging timeout")
            err("State PAGE -> STANDBY")
            err("Paging done")
            running = None
            continue
        err("Central started")
        err("State PAGE -> CONNECTED")
        err("Connection ready, lt_addr=1")
        err("LMP setup complete")
        if "no-lmp-name" not in FAULTS:
            name = " ".join("%02x" % byte for byte in NAME.encode())
            out("RX LMP: BBHdr(lt_addr=1, type=DM1, flags=FA) | (tid 0) "
                "LMP_NAME_RES | 04 00 0f " + name)
        warning = ("warning: LMP opcode 0x20 is not handled in state 1, "
                   "refused")
        if "quiet-lmp" not in FAULTS:
            err(warning)
        if "repeat-unhandled" in FAULTS:
            err(warning)
            err(warning)
        if "capture-on-stderr" in FAULTS:
            err("09:11:00 RX LMP: BBHdr(lt_addr=1, type=DM1, flags=F) | x")
        if "false-connection" in FAULTS:
            # The connection carried nothing, so link supervision ends it and
            # the pager goes back to its page train -- a second later on a
            # board, which here is when the next line arrives, so that it
            # lands while the connection is being held rather than inside the
            # answers the page itself printed.
            pending.append("Paging resumed")
        if "hangs-up" in FAULTS:
            err("warning: the peer refused LMP_HOST_CONNECTION_REQ: "
                "unsupported LMP feature; closing the link")
            err("0.42 sec | >>> lmp_tx (state=1): (tid 0) LMP_DETACH")
        start_the_link()
        continue
    if word == "inquiry":
        if "stray-packet" in FAULTS:
            # One of the tool's own warnings renders a decoded packet.
            err("warning: a packet arrived before BTCtlInquiryCmd was ready: "
                "RX(FHS: bdaddr=%s parity=1 cls=1f00 ltaddr=0 clk=1)" % OTHER)
        err("Starting BTCtlInquiryCmd")
        prompt()
        err("State STANDBY -> INQUIRY")
        running = "inquiry"
        if "silent-inquiry" in FAULTS:
            response(OTHER, OTHER_NAME)
        elif "unnamed-peer" in FAULTS:
            out("RX FHS: FHS: bdaddr=%s parity=2c43c0d1b cls=1f00 "
                "ltaddr=0 clk=4706" % PEER)
        else:
            response(PEER, NAME)
            response(OTHER, OTHER_NAME)
        err("Inquiry state Ready")
        continue
    err("error: no such command '%s'" % word)
    prompt()

if "bad-exit" in FAULTS:
    status = 1
sys.exit(status)
'''


def write_fake_btbr(directory, *, faults=(), peer=PEER_ADDRESS,
                    name=PEER_NAME) -> dict:
    """Put a fake ubertooth-btbr in ``directory``; returns its environment."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_executable(directory / "ubertooth-btbr", _SCRIPT)
    return {
        "FAKE_BTBR_CONFIG": json.dumps(
            {"faults": list(faults), "peer": peer, "name": name,
             "other": OTHER_ADDRESS, "other_name": OTHER_NAME}),
    }
