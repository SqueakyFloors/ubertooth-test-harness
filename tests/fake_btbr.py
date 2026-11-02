"""A stand-in for `ubertooth-btbr`'s console, for btbrconsole.py tests.

The real tool opens a prompt on a board carrying the `btbr` firmware image.
This writes a script that answers the same lines on the same streams: capture
records on standard output, the prompt and the progress on standard error.
The wording of each line is the tool's, read from its sources and its man
page; every address and device name here is made up, including the second
device an inquiry hears, which stands in for whatever else is in range of a
bench. ``faults`` makes the fake misbehave one way at a time, so each check in
btbrconsole.py can be shown to fail for its own reason.
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

_SCRIPT = '''#!/usr/bin/env python3
"""A fake ubertooth-btbr console. See tests/fake_btbr.py."""
import json, os, sys, time

CONFIG = json.loads(os.environ["FAKE_BTBR_CONFIG"])
FAULTS = set(CONFIG["faults"])
PEER = CONFIG["peer"].replace(":", "").lower()
NAME = CONFIG["name"]
OTHER = CONFIG["other"]
OTHER_NAME = CONFIG["other_name"]

def err(text):
    # Carriage return and newline, as the tool itself ends these lines: a
    # driver that anchors a pattern on the end of a line has to cope.
    sys.stderr.write(text + "\\r\\n")
    sys.stderr.flush()

def out(text):
    sys.stdout.write("09:11:00 " + text + "\\n")
    sys.stdout.flush()

def prompt():
    if "no-prompt" not in FAULTS:
        sys.stderr.write("bt> ")
        sys.stderr.flush()

def response(address, name):
    out("RX FHS: FHS: bdaddr=%s parity=2c43c0d1b cls=1f00 ltaddr=0 clk=4706"
        % address)
    out("ACL(llid=L2CAP_START, flow=0, len=23): 10 09 "
        + " ".join("%02x" % byte for byte in name.encode()))
    out("COMPLETE_LOCAL_NAME: '%s'" % name)

HELP = ["EOF", "discoverable", "help", "inquiry", "l2ping", "page",
        "bdaddr", "exit", "idle", "inquiry_scan", "monitor", "page_scan"]

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
while True:
    # One line at a time: `for line in sys.stdin` reads in blocks, which
    # would hold every answer back until the driver closed the pipe.
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    word, _, rest = line.partition(" ")
    rest = rest.strip()
    if word in ("exit", "q", "x"):
        if "never-exits" in FAULTS:
            while True:
                time.sleep(0.2)
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
                err("State CONNECTED -> STANDBY")
                err("Paging done")
            else:
                err("State STANDBY -> STANDBY")
        running = None
        prompt()
        continue
    if word in ("bdaddr", "page", "monitor", "l2ping"):
        digits = rest.replace(":", "")
        good = len(digits) == 12 and all(c in "0123456789abcdefABCDEF"
                                         for c in digits)
        if not good and "takes-bad-address" not in FAULTS:
            err("error: BD_ADDR must be xx:xx:xx:xx:xx:xx (got %s)" % rest)
            if word == "page" and "pages-without-address" in FAULTS:
                err("Paging started")
            prompt()
            continue
        if word == "bdaddr":
            err("Set bdaddr %s -> sw b0a86caac43c0d1b" % digits.lower())
            prompt()
            continue
        err("Starting BTCtlPagingCmd")
        prompt()
        err("State STANDBY -> PAGE")
        err("Paging started")
        running = "page"
        if digits.lower() != PEER or "deaf-peer" in FAULTS:
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
