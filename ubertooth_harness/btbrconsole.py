"""Drive the ubertooth-btbr console against a known Classic peer.

`ubertooth-btbr` is the fork's one interactive tool. It opens a prompt, `bt> `,
and each word typed there starts one operation on a board carrying the
separate `btbr` firmware image; `bluetooth_rxtx`, which every other tool
needs, does not run it (host/doc/ubertooth-btbr.md, FIRMWARE). Nothing about
that fits a single command line, so the harness drives the prompt from here
and the case checks this driver's report.

Run as a script by a case step::

    python btbrconsole.py session --serial <serial> --peer <BD_ADDR> \
        --peer-name <name>

One invocation is one console session. The tool is started once, every check
runs in order down the same prompt, and the session ends with `exit`, because
the board has to be flashed with `btbr` before it and back to the application
image after it: a second session would cost two more flashes.

What the session checks, and why each answer is knowable in advance:

- **start**: the board was opened, the `btbr` image answered the requests that
  set a session up, and the prompt arrived. A board carrying `bluetooth_rxtx`
  fails here on a write timeout, which is what the reports of this tool
  hanging turned out to be.
- **help**, **help-one**: `?` lists the commands and `? <command>` describes
  one. The tool is describing itself, so no board state is involved.
- **no-such-command**, **no-help**, **bad-address**, **page-no-address**: the
  safe half of malformed input, and the only half a shared bench allows. A
  word that is no command, `?` on a word that is no command, an address that
  is empty, not hex, or too long, and `page` with no address at all: each is
  one `error:` line, the prompt stays up, and no radio is touched.
- **set-address**: `bdaddr` takes an address and echoes the sync word it
  derived.
- **inquiry**: the registered ESP32 Classic peer is discoverable and
  connectable from boot (docs/hardware.md), so an inquiry has to hear it. Its
  BD_ADDR is in the registry and its name carries its own LAP, so the address
  in the response and the name in the Extended Inquiry Response are both
  known before the run.
- **page**: paging that peer brings a connection up and runs the LMP exchange
  that opens one. The peer answers the name request with its own name, so the
  `LMP_NAME_RES` on the air says which device replied.
- **lmp-unhandled**: a request this link layer has no handler for is refused
  and named in one `warning:` line per opcode for the life of the connection,
  not one per packet. These controllers ask for power control several times a
  second, so a regression to one line per packet is loud.
- **idle**: `idle` stops the running command, the firmware reports it, and
  the prompt comes back. It runs after the inquiry and again after the page.
- **streams**: the capture is on standard output and the progress on standard
  error, with nothing of either on the other.
- **page-no-answer** (comprehensive): paging an address nothing answers ends
  in `paging timeout` and no connection, which is the one missing-response
  path a bench can produce safely.

Standard output carries the tool's own standard output unchanged, which is the
packets it decoded; the report, and the tool's standard error marked with
``btbr| ``, are on standard error, where the case's patterns read them. Exit
status is 0 when every check passed, 1 when one did not or the session could
not be driven, and ``SKIP_EXIT`` with a ``SKIP: <reason>`` line when the run
built no Python tools to test.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

if __package__ in (None, ""):
    # Run as a script, which is how a case step runs it: make the package
    # importable for the two modules below.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness import esp32peer  # noqa: E402
from ubertooth_harness.pytools import PYTHON_TOOLS_ENV, SKIP_EXIT  # noqa: E402

TOOL = "ubertooth-btbr"
# The prompt has no newline of its own, so it is matched in the stream's text
# rather than in a line (host/doc/ubertooth-btbr.md, OUTPUT).
PROMPT = "bt> "
INTRO = "Type ? to list commands"
# The two startup lines that say the board answered: the session claimed it
# over USB, and the Extended Inquiry Response an inquiring device will see was
# written to it.
USB_CONNECTED = "USB connected"
EIR_UPDATED = "EIR updated"

# The first startup line, `USB connected`. A board carrying the wrong image
# times the first write out instead, in libusb's own time, so this is
# generous; the lines after it follow at once and get REPLY_TIMEOUT_S.
START_TIMEOUT_S = 30.0
# One prompt command that needs no radio. The answer is printed as the line is
# read: every one of them arrived inside 0.1 s on the bench, and a session
# makes about twenty of these waits, so a generous window here is what would
# push the whole session past its step deadline.
REPLY_TIMEOUT_S = 5.0
# `exit` with nothing running.
EXIT_TIMEOUT_S = 15.0
# How long after `idle` the firmware has to stop the running command and the
# prompt come back.
IDLE_TIMEOUT_S = 20.0
# Windows the radio checks get, each ended as soon as its answer arrives. The
# bench numbers behind them are in docs/tests.md: an inquiry heard the peer in
# about 2 s and a page connected in about 7 s.
DEFAULT_INQUIRY_S = 25.0
DEFAULT_PAGE_S = 30.0
# How long the connection is held once it is up, so the LMP exchange and the
# peer's own requests reach the capture.
DEFAULT_HOLD_S = 10.0
# A page nothing answers: the firmware allows one attempt of about 45 s and
# then reports the timeout (host/doc/ubertooth-btbr.md, PROMPT COMMANDS).
DEFAULT_NO_ANSWER_S = 75.0
POLL_S = 0.05
# A session's whole output, for the matching below. A console session of this
# length prints a few hundred lines; this is a backstop against a board that
# floods the stream, so the driver reports instead of filling the run record.
MAX_STREAM_CHARS = 2_000_000

# The one reason for not starting that the run cannot do anything about and
# the bench is not at fault for: the interpreter the tools were built for has
# no libusb1 package, so no session can open a board at all
# (host/doc/ubertooth-btbr.md, DIAGNOSTICS).
NO_LIBUSB1 = "No module named 'usb1'"

# What the tool prints, measured at fork b2cd6c68 and documented in
# host/doc/ubertooth-btbr.md.
NO_SUCH_COMMAND = "error: no such command '{word}'"
BAD_ADDRESS = "error: BD_ADDR must be xx:xx:xx:xx:xx:xx (got {word})"
NO_HELP = "error: no help on '{word}'"
HELP_HEADING = "Documented commands"
# The commands `?` must name. Its answer is a table several to a line, so
# these are searched for as words, not as lines.
HELP_COMMANDS = ("bdaddr", "discoverable", "exit", "idle", "inquiry",
                 "inquiry_scan", "l2ping", "monitor", "page", "page_scan")
HELP_ONE = ("inquiry", "inquiry: Discover nearby devices")
# `bdaddr <address>` echoes the address it took and the sync word it derived.
# Not anchored at the start of a line: the prompt has no newline of its own,
# so the first answer after it lands on the end of the prompt.
SET_BDADDR = r"(?m)Set bdaddr (?P<address>[0-9a-f]+) -> sw [0-9a-f]+$"
# The firmware prints that address with %llx, so an address with a leading
# zero byte comes back shorter than twelve digits: it is compared as a number.
# Paging, in the order the firmware reports it.
PAGING_STARTED = "Paging started"
PAGING_CONNECTED = "Central started"
PAGING_READY = re.compile(r"(?m)^Connection ready, lt_addr=(?P<lt_addr>\d+)$")
LMP_COMPLETE = "LMP setup complete"
PAGING_TIMEOUT = "paging timeout"
PAGING_DONE = "Paging done"
INQUIRY_STARTED = "Starting BTCtlInquiryCmd"
INQUIRY_DONE = "Inquiry done"
# An LMP request with no handler: one line per opcode, not per packet. The
# opcode is `0x<hex>`, or `<opcode>/<extended opcode>` for an escape opcode,
# and the line ends in `, refused` only where there was something to refuse: a
# PDU that expects no answer is just reported (host/python/ubtbr/ubtbr/lmp.py).
LMP_UNHANDLED = re.compile(
    r"(?m)^warning: LMP opcode (?P<opcode>\S+) is not handled in state \d+")

# An inquiry response is an FHS packet carrying the responder's address, and
# its Extended Inquiry Response follows as fields of its own.
FHS_BDADDR = re.compile(r"\bbdaddr=(?P<address>[0-9a-f]+)\b")
EIR_NAME = re.compile(r"COMPLETE_LOCAL_NAME: '(?P<name>[^']*)'")
# The peer's answer to the central's name request, on the air.
LMP_NAME_RES = "LMP_NAME_RES"
# How much of the peer's name the printed LMP_NAME_RES line is checked for.
# The line shows the PDU's first seventeen bytes, three of which are the
# opcode, the fragment offset, and the length, so a fourteen-character name
# fits whole and a longer one is cut.
NAME_BYTES_CHECKED = 10
# Records and progress lines, each of which belongs on one stream only. The
# capture pattern is not anchored to the start of a line: the prompt carries no
# newline of its own, so a record printed right after one does not start a
# line, and that is the likeliest place for a stray record to appear.
CAPTURE_LINE = re.compile(r"(?m)\d\d:\d\d:\d\d [A-Z0-9]")
# Every line of the capture stream must be a record, so anything else there is
# named whatever it is: an error, a warning, a note, the prompt, the intro, or
# a line of what `?` printed.
NOT_A_RECORD = re.compile(r"(?m)^(?!\d\d:\d\d:\d\d [A-Z0-9])(?P<line>.+)$")

# A word that is no command and no address, and three addresses that are not
# ones. Nothing on the bench answers to any of them and none reaches the
# radio. An address shorter than six bytes is deliberately absent: the tool
# takes one today and acts on a different address (fork issue 1075), so a case
# here would pin that rather than check it.
BAD_WORD = "wibble"
BAD_ADDRESSES = ("", "zzz", "11:22:33:44:55:66:77")
# The address to set the board to. It is not `11:22:33:44:55:66`, which the
# tool gives the board itself at startup (host/doc/ubertooth-btbr.md,
# DESCRIPTION), so the check is a change rather than a rewrite of the same
# value.
TEST_ADDRESS = "22:33:44:55:66:77"
# What the no-answer check pages is derived from the registered peer instead
# of being a constant: a page is addressed by the access code the LAP gives,
# and a constant would sooner or later be some bench's peer. The low byte of
# the peer's own address is inverted, which changes the LAP and so the access
# code, and leaves an address nothing is listening on.
_NO_DEVICE_MASK = 0xFF


class ConsoleError(RuntimeError):
    """The console session could not be driven to the point of a verdict."""


def _now() -> float:
    return time.monotonic()


class Stream:
    """One of the tool's output streams, kept whole so the prompt can be seen.

    A reader thread appends everything that arrives to the stream's text and
    passes it on to ``sink``. The prompt carries no newline, so a line-based
    reader would block on it; the matching below therefore runs over the text.
    """

    def __init__(self, name: str, source, sink=None, prefix: str = "",
                 keep: str = ""):
        self.name = name
        self._source = source
        self._sink = sink
        self._prefix = prefix
        # The one address this stream may name. The tool renders a decoded
        # packet inside a warning or two, so a third-party address could
        # reach the run record by the stream the capture was kept out of.
        self._keep = keep.lower()
        self._lock = threading.Lock()
        self._chunks: list[str] = []
        self._size = 0
        # The part of the last chunk with no newline yet, kept only so the
        # echo below writes whole lines. It is already in ``_chunks``, so it
        # is never added to ``text`` a second time: an offset into ``text``
        # has to mean the same thing before and after a chunk arrives, or
        # every wait that starts from one looks past its answer.
        self._unechoed = ""
        # A carriage return at the end of a chunk, held back until the next
        # chunk says whether a newline follows it (see _append).
        self._pending_cr = ""
        self.overflowed = False
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self) -> None:
        while True:
            chunk = self._source.read1(4096)
            if not chunk:
                break
            self._append(chunk.decode("utf-8", errors="replace"))
        # Whatever was held back waiting for a newline that never came.
        self._append("")
        with self._lock:
            tail, self._unechoed = self._unechoed, ""
            if self._pending_cr:
                tail, self._pending_cr = tail + self._pending_cr, ""
        if tail:
            self._echo([tail])

    def _append(self, text: str) -> None:
        with self._lock:
            # The tool ends some of its lines with a carriage return and a
            # newline. The carriage return goes here, because every pattern
            # the checks wait for anchors on the end of a line and one left
            # in would stop it matching: that is how a `bdaddr` answer the
            # tool did print read as one it had not. A chunk that ends on the
            # carriage return keeps it back until the next chunk says whether
            # a newline follows, so a read split between the two cannot leave
            # one in. A carriage return on its own stays: the tool redraws a
            # line with it.
            text = self._pending_cr + text
            self._pending_cr = ""
            if text.endswith("\r"):
                text, self._pending_cr = text[:-1], "\r"
            text = text.replace("\r\n", "\n")
            if not text:
                return
            if self._size + len(text) > MAX_STREAM_CHARS:
                self.overflowed = True
            else:
                self._chunks.append(text)
                self._size += len(text)
            lines = (self._unechoed + text).split("\n")
            self._unechoed = lines.pop()
        self._echo(lines)

    def _echo(self, lines: Sequence[str]) -> None:
        """Write the lines on, where this stream has somewhere to write them.

        The tool's standard error is marked, so the report lines cannot be
        read as the tool's. Its standard output has no sink at all: it is the
        capture, and an inquiry hears every discoverable device in range, so
        passing it on would put third-party addresses and device names into
        the run record. The checks below read it in memory and report only
        what they matched, which is the registered peer's own.
        """
        if not lines or self._sink is None:
            return
        self._sink.write("".join(self._prefix + self._redact(line) + "\n"
                                 for line in lines))
        self._sink.flush()

    def _redact(self, line: str) -> str:
        """Any address but the peer's, replaced before the line is written."""
        if self._keep and "bdaddr=" not in line:
            return line
        return FHS_BDADDR.sub(
            lambda found: ("bdaddr=" + found["address"]
                           if found["address"].lower() == self._keep
                           else "bdaddr=<another device>"),
            line)

    @property
    def text(self) -> str:
        with self._lock:
            return "".join(self._chunks)


class Console:
    """A running ubertooth-btbr session: send a line, wait for an answer."""

    def __init__(self, argv: Sequence[str], err, environment=None, keep=""):
        self.argv = tuple(argv)
        self._err = err
        # The only device address the echoed progress may name.
        self._keep = keep
        self._environment = environment
        self.process: subprocess.Popen | None = None
        self.streams: dict[str, Stream] = {}
        self.started = 0.0

    def start(self) -> None:
        try:
            self.process = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self._environment,
            )
        except OSError as error:
            raise ConsoleError(f"could not run {self.argv[0]}: {error}") from error
        self.started = _now()
        self.streams = {
            "stdout": Stream("stdout", self.process.stdout),
            "stderr": Stream("stderr", self.process.stderr, self._err, "btbr| ",
                             keep=self._keep),
        }
        for stream in self.streams.values():
            stream.thread.start()

    def text(self, stream: str = "stderr") -> str:
        """What the stream has printed, or "" before the tool was started.

        A tool that could not be run leaves no streams at all, and the one
        path that reports that reads this first.
        """
        found = self.streams.get(stream)
        return found.text if found is not None else ""

    def mark(self, stream: str = "stderr") -> int:
        """Where the stream's text has reached, so a wait starts from here."""
        return len(self.text(stream))

    def send(self, line: str) -> None:
        if self.process is None or self.process.stdin is None:
            raise ConsoleError("the session is not running")
        try:
            self.process.stdin.write((line + "\n").encode())
            self.process.stdin.flush()
        except OSError as error:
            raise ConsoleError(
                f"could not send {line!r}: the session has gone ({error})") from error

    def wait(self, needle, timeout: float, *, stream: str = "stderr",
             since: int = 0) -> tuple[re.Match | None, float]:
        """Wait for ``needle`` in ``stream`` past offset ``since``.

        ``needle`` is a compiled pattern or a literal string. Returns the
        match and how long the wait took, or (None, elapsed) at the timeout. A
        session that exits while this waits is given one last look at the text
        it left, so an answer is not lost to that race.
        """
        if self.streams[stream].overflowed:
            raise ConsoleError(
                f"the session's {stream} passed {MAX_STREAM_CHARS} characters; "
                "what it printed after that is lost, so no check can be decided")
        pattern = (needle if isinstance(needle, re.Pattern)
                   else re.compile(re.escape(needle)))
        start = _now()
        deadline = start + timeout
        while True:
            match = pattern.search(self.text(stream), since)
            if match is not None:
                return match, _now() - start
            exited = self.process is not None and self.process.poll() is not None
            if exited or _now() >= deadline:
                return pattern.search(self.text(stream), since), _now() - start
            time.sleep(POLL_S)

    def end(self, timeout: float | None = None) -> int:
        """Ask the session to exit, and never leave the process behind."""
        timeout = EXIT_TIMEOUT_S if timeout is None else timeout
        if self.process is None:
            return 0
        if self.process.poll() is None:
            try:
                self.send("exit")
            except ConsoleError:
                pass
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            status = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # The session would not end. It is killed so the board is
            # released and the step reports rather than hanging.
            self.process.kill()
            status = self.process.wait(timeout=timeout)
            self._join(timeout)
            raise ConsoleError(f"the session did not exit within {timeout:g}s")
        self._join(timeout)
        return status

    def _join(self, timeout: float) -> None:
        """Wait for both readers, then close the pipes they were reading."""
        for stream in self.streams.values():
            stream.thread.join(timeout=timeout)
        for pipe in (self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# What the checks share


def no_device_address(peer: str) -> str:
    """An address nothing answers, from the one that does.

    The peer's low byte inverted: that is a different LAP, so a page for it
    carries a different access code, and no device on a bench registered
    around the peer can answer it.
    """
    parts = peer.split(":")
    low = int(parts[-1], 16) ^ _NO_DEVICE_MASK
    return ":".join(parts[:-1] + [f"{low:02X}"])


def _record(text: str, match: re.Match) -> str:
    """The whole capture line a match landed on, trimmed for a report line.

    Only ever called on a record the check matched for the registered peer, so
    what it reports is the bench's own device. The lines of an inquiry it did
    not match are counted, never reported: they belong to whatever else is in
    range.
    """
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    line = text[start:end if end >= 0 else len(text)].strip()
    return line[:_MAX_RECORD_CHARS - 3] + "..." if len(line) > _MAX_RECORD_CHARS else line


# A reported record is one line of a report; a capture line is well inside it.
_MAX_RECORD_CHARS = 200


@dataclass
class Session:
    """The console, what the peer should be, and how long each wait may take."""

    console: Console
    peer_address: str
    peer_name: str
    inquiry_seconds: float
    page_seconds: float
    hold_seconds: float
    no_answer_seconds: float
    report: Callable[[str], None]
    opcodes: list[str] = field(default_factory=list)

    @property
    def peer_bdaddr(self) -> str:
        """The peer's address as an FHS line writes it: hex, no separators."""
        return self.peer_address.replace(":", "").lower()

    def refused(self, label: str, line: str, expected: str) -> str:
        """Send a line that must be refused, with the prompt left up."""
        mark = self.console.mark()
        self.console.send(line)
        match, took = self.console.wait(expected, REPLY_TIMEOUT_S, since=mark)
        if match is None:
            return (f"{line!r} was not refused with {expected!r} within "
                    f"{REPLY_TIMEOUT_S:g}s")
        prompt, _ = self.console.wait(PROMPT, REPLY_TIMEOUT_S, since=match.end())
        if prompt is None:
            return f"{line!r} was refused but the prompt did not come back"
        self.report(f"check {label}: {line!r} refused in {took:.1f} s, prompt up")
        return ""

    def idle(self, label: str, done: str) -> str:
        """`idle` stops the running command; the firmware says so."""
        mark = self.console.mark()
        self.console.send("idle")
        ended, took = self.console.wait(done, IDLE_TIMEOUT_S, since=mark)
        if ended is None:
            return f"idle did not reach {done!r} within {IDLE_TIMEOUT_S:g}s"
        prompt, _ = self.console.wait(PROMPT, REPLY_TIMEOUT_S, since=ended.end())
        if prompt is None:
            return f"idle reached {done!r} but the prompt did not come back"
        self.report(f"check {label}: {done!r} in {took:.1f} s, prompt up")
        return ""


# ---------------------------------------------------------------------------
# The checks. Each reports its own line and returns a failure reason, or "".


def check_help(session: Session) -> str:
    """`?` lists the commands. The list is the tool describing itself."""
    console = session.console
    mark = console.mark()
    console.send("?")
    heading, took = console.wait(HELP_HEADING, REPLY_TIMEOUT_S, since=mark)
    if heading is None:
        return f"? printed no {HELP_HEADING!r} within {REPLY_TIMEOUT_S:g}s"
    # The names come in writes of their own, after the heading and the rule
    # under it, so the prompt that follows them is what says the list is
    # whole: reading the text as soon as the heading arrives could catch it
    # half written and report a command that was on its way.
    prompt, _ = console.wait(PROMPT, REPLY_TIMEOUT_S, since=heading.end())
    if prompt is None:
        return f"? printed {HELP_HEADING!r} but the prompt did not come back"
    listed = console.text()[mark:prompt.start()]
    missing = [name for name in HELP_COMMANDS
               if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", listed)]
    if missing:
        return "? did not list " + ", ".join(missing)
    session.report(f"check help: {len(HELP_COMMANDS)} commands listed in {took:.1f} s")
    return ""


def check_help_one(session: Session) -> str:
    command, description = HELP_ONE
    console = session.console
    mark = console.mark()
    console.send(f"? {command}")
    match, took = console.wait(description, REPLY_TIMEOUT_S, since=mark)
    if match is None:
        return f"? {command} did not print {description!r}"
    session.report(f"check help-one: ? {command} described it in {took:.1f} s")
    return ""


def check_no_such_command(session: Session) -> str:
    return session.refused("no-such-command", BAD_WORD,
                           NO_SUCH_COMMAND.format(word=BAD_WORD))


def check_no_help(session: Session) -> str:
    return session.refused("no-help", f"? {BAD_WORD}",
                           NO_HELP.format(word=BAD_WORD))


def check_bad_address(session: Session) -> str:
    for word in BAD_ADDRESSES:
        line = f"bdaddr {word}".rstrip()
        reason = session.refused("bad-address", line,
                                 BAD_ADDRESS.format(word=word))
        if reason:
            return reason
    return ""


def check_page_no_address(session: Session) -> str:
    """`page` with no address is refused before the radio is asked for."""
    console = session.console
    mark = console.mark()
    reason = session.refused("page-no-address", "page", BAD_ADDRESS.format(word=""))
    if reason:
        return reason
    started, _ = console.wait(PAGING_STARTED, 0.0, since=mark)
    if started is not None:
        return "page with no address started paging anyway"
    return ""


def check_set_address(session: Session) -> str:
    """`bdaddr <address>` takes it and echoes the sync word it derived."""
    console = session.console
    wanted = TEST_ADDRESS.replace(":", "").lower()
    mark = console.mark()
    console.send(f"bdaddr {TEST_ADDRESS}")
    match, took = console.wait(re.compile(SET_BDADDR), REPLY_TIMEOUT_S, since=mark)
    if match is None:
        return f"bdaddr {TEST_ADDRESS} did not report the address it set"
    if int(match["address"], 16) != int(wanted, 16):
        return f"bdaddr {TEST_ADDRESS} set {match['address']}, not {wanted}"
    session.report(f"check set-address: {TEST_ADDRESS} set in {took:.1f} s, "
                   f"sync word derived")
    return ""


def check_inquiry(session: Session) -> str:
    """An inquiry hears the registered peer, by address and then by name."""
    console = session.console
    out_mark, err_mark = console.mark("stdout"), console.mark()
    console.send("inquiry")
    started, _ = console.wait(INQUIRY_STARTED, REPLY_TIMEOUT_S, since=err_mark)
    if started is None:
        return "inquiry did not start"
    found, took = console.wait(
        re.compile(rf"\bbdaddr={session.peer_bdaddr}\b"),
        session.inquiry_seconds, stream="stdout", since=out_mark)
    if found is None:
        # Counted, not named: the other devices are whatever is in range.
        heard = len({match["address"] for match
                     in FHS_BDADDR.finditer(console.text("stdout")[out_mark:])})
        return (f"the inquiry did not hear the peer {session.peer_bdaddr} in "
                f"{session.inquiry_seconds:g}s; it heard {heard} other "
                "device(s)")
    # The response's Extended Inquiry Response follows it as fields of their
    # own, so the name usually arrives just after the address. What is left of
    # the inquiry window is given to it all the same: the inquiry is still
    # running, the peer answers again every few seconds, and one lost packet
    # should not cost a case two firmware writes.
    left = max(REPLY_TIMEOUT_S, session.inquiry_seconds - took)
    named, _ = console.wait(
        re.compile(rf"COMPLETE_LOCAL_NAME: '{re.escape(session.peer_name)}'"),
        left, stream="stdout", since=found.end())
    captured = console.text("stdout")[out_mark:]
    if named is None:
        names = len(EIR_NAME.findall(captured))
        return (f"the inquiry heard the peer {session.peer_bdaddr} but no "
                f"Extended Inquiry Response named {session.peer_name!r}; "
                f"{names} response(s) carried a name")
    responses = len(set(FHS_BDADDR.findall(captured)))
    session.report(
        f"check inquiry: peer {session.peer_bdaddr} answered after {took:.1f} s, "
        f"name {session.peer_name!r}, {responses} device(s) heard in all")
    session.report(f"  record: {_record(captured, found)}")
    return session.idle("inquiry-idle", INQUIRY_DONE)


def check_page(session: Session) -> str:
    """Paging the peer brings a connection up and opens it with LMP."""
    console = session.console
    out_mark, err_mark = console.mark("stdout"), console.mark()
    console.send(f"page {session.peer_address}")
    started, _ = console.wait(PAGING_STARTED, REPLY_TIMEOUT_S, since=err_mark)
    if started is None:
        return f"page {session.peer_address} did not start paging"
    central, took = console.wait(PAGING_CONNECTED, session.page_seconds,
                                 since=started.end())
    if central is None:
        return (f"the peer {session.peer_address} did not answer the page in "
                f"{session.page_seconds:g}s")
    ready, _ = console.wait(PAGING_READY, REPLY_TIMEOUT_S, since=central.end())
    if ready is None:
        return "the page was answered but no connection became ready"
    done, _ = console.wait(LMP_COMPLETE, REPLY_TIMEOUT_S, since=ready.end())
    if done is None:
        return (f"the connection came up but the LMP exchange did not reach "
                f"{LMP_COMPLETE!r} in {REPLY_TIMEOUT_S:g}s")
    session.report(
        f"check page: connected to {session.peer_address} in {took:.1f} s, "
        f"lt_addr={ready['lt_addr']}, LMP setup complete")
    reason = _check_peer_named(session, out_mark)
    if reason:
        return reason
    # The link is held so the peer's own requests reach the capture, which is
    # what lmp-unhandled below reads.
    time.sleep(session.hold_seconds)
    return session.idle("page-idle", PAGING_DONE)


def _check_peer_named(session: Session, out_mark: int) -> str:
    """The peer answered the name request with its own name, on the air."""
    wanted = " ".join(f"{byte:02x}" for byte in
                      session.peer_name.encode()[:NAME_BYTES_CHECKED])
    match, _ = session.console.wait(
        re.compile(rf"{LMP_NAME_RES}.*\b{re.escape(wanted)}\b"),
        REPLY_TIMEOUT_S, stream="stdout", since=out_mark)
    if match is None:
        return (f"no {LMP_NAME_RES} on the air carried the peer's name "
                f"{session.peer_name!r} (first {NAME_BYTES_CHECKED} bytes "
                f"{wanted})")
    session.report(f"check page-peer-name: {LMP_NAME_RES} carried "
                   f"{session.peer_name!r}")
    session.report(f"  record: {_record(session.console.text('stdout'), match)}")
    return ""


def check_lmp_unhandled(session: Session) -> str:
    """An LMP request with no handler is named once per opcode, not per packet."""
    found: dict[str, int] = {}
    for match in LMP_UNHANDLED.finditer(session.console.text()):
        found[match["opcode"]] = found.get(match["opcode"], 0) + 1
    repeated = sorted(opcode for opcode, count in found.items() if count > 1)
    if repeated:
        return ("an unhandled LMP opcode was reported more than once: "
                + ", ".join(f"{opcode} {found[opcode]} times"
                            for opcode in repeated))
    if not found:
        # Nothing arrived, so nothing was decided. These controllers ask for
        # power control about four times a second for the life of the link,
        # and this link layer has no handler for it, so a connection held for
        # the hold above always brings one: none at all is either a peer that
        # no longer asks or a tool that stopped saying so, and both are worth
        # a look rather than a quiet pass.
        return ("no LMP request with no handler was reported at all, so "
                "nothing was checked: the peer asks for power control several "
                "times a second, and this link layer refuses it")
    session.opcodes = sorted(found)
    session.report("check lmp-unhandled: " + ", ".join(session.opcodes)
                   + " refused, each named once")
    return ""


def check_page_no_answer(session: Session) -> str:
    """A page nothing answers ends in a timeout, with no connection."""
    console = session.console
    mark = console.mark()
    address = no_device_address(session.peer_address)
    console.send(f"page {address}")
    started, _ = console.wait(PAGING_STARTED, REPLY_TIMEOUT_S, since=mark)
    if started is None:
        return f"page {address} did not start paging"
    timeout, took = console.wait(PAGING_TIMEOUT, session.no_answer_seconds,
                                 since=started.end())
    if timeout is None:
        return (f"paging {address}, which nothing answers, did not report "
                f"{PAGING_TIMEOUT!r} in {session.no_answer_seconds:g}s")
    connected, _ = console.wait(PAGING_CONNECTED, 0.0, since=started.end())
    if connected is not None:
        return (f"paging {address} reported {PAGING_CONNECTED!r}: something "
                "answered for an address nothing has")
    ended, _ = console.wait(PAGING_DONE, REPLY_TIMEOUT_S, since=timeout.end())
    if ended is None:
        return f"the page timed out but did not report {PAGING_DONE!r}"
    session.report(f"check page-no-answer: {PAGING_TIMEOUT!r} after {took:.1f} s, "
                   "no connection")
    return ""


def check_streams(session: Session) -> str:
    """The capture is on standard output and the progress on standard error.

    This is the last check, so it is also where an overflowed stream is
    caught: the two checks before it read the text rather than waiting on it,
    and a stream that overflowed during the connection hold would have left
    them deciding on text that stopped part way.
    """
    for stream in session.console.streams.values():
        if stream.overflowed:
            return (f"the session's {stream.name} passed {MAX_STREAM_CHARS} "
                    "characters, so what it printed after that is lost")
    stray_capture = CAPTURE_LINE.search(session.console.text())
    if stray_capture:
        return f"a capture record is on stderr: {stray_capture[0]!r}"
    # Not an allowlist of what may not be there: anything that is not a
    # record is named, whatever it is.
    stray = NOT_A_RECORD.search(session.console.text("stdout"))
    if stray:
        return (f"a line that is not a capture record is on stdout: "
                f"{stray['line'][:120]!r}")
    captured = len(CAPTURE_LINE.findall(session.console.text("stdout")))
    session.report(f"check streams: {captured} capture records on stdout, "
                   "nothing else, and none on stderr")
    return ""


# In order down one prompt: the refusals first, because they need no radio and
# a board that cannot answer them is not worth a radio window; then the
# address, the inquiry and the page it is all about, and the streams check
# last, over everything that was printed.
SMOKE_CHECKS = (
    ("help", check_help),
    ("help-one", check_help_one),
    ("no-such-command", check_no_such_command),
    ("no-help", check_no_help),
    ("bad-address", check_bad_address),
    ("page-no-address", check_page_no_address),
    ("set-address", check_set_address),
    ("inquiry", check_inquiry),
    ("page", check_page),
    ("lmp-unhandled", check_lmp_unhandled),
    ("streams", check_streams),
)
# The missing-response path, which costs the firmware's whole page attempt.
NO_ANSWER_CHECK = ("page-no-answer", check_page_no_answer)


# What each check may wait out, in the module's windows above. A case sets its
# step deadline from worst_case_seconds() below, so a check that fails ends in
# this module's own reason rather than in the harness killing the step: the
# difference between a diagnosis and a timeout. Each count is the waits in the
# check's own code. tests/test_btbrconsole.py checks that every check has an
# entry and that no entry is left behind by one that went away, which is the
# drift that would otherwise leave a new check outside the budget.
def _check_seconds(label: str, *, inquiry: float, page: float, hold: float,
                   no_answer: float) -> float:
    reply, idle = REPLY_TIMEOUT_S, IDLE_TIMEOUT_S
    return {
        # The heading, then the prompt that says the list is whole.
        "help": 2 * reply,
        "help-one": reply,
        # The refusal, then the prompt after it.
        "no-such-command": 2 * reply,
        "no-help": 2 * reply,
        # Three addresses, each a refusal and a prompt.
        "bad-address": 3 * 2 * reply,
        # The refusal, the prompt, and the look for a paging line.
        "page-no-address": 2 * reply,
        "set-address": reply,
        # The start, the peer's response, then what is left of the inquiry
        # window for its name, then idle: the stop line and the prompt.
        "inquiry": reply + 2 * inquiry + idle + reply,
        # The start, the page, ready, LMP complete, the name on the air, the
        # hold, then idle.
        "page": 5 * reply + page + hold + idle + reply,
        # Both read what is already there.
        "lmp-unhandled": 0.0,
        "streams": 0.0,
        # The start, the timeout, the look for a connection, and the done line.
        "page-no-answer": 3 * reply + no_answer,
    }[label]


def worst_case_seconds(no_answer: bool, *, inquiry: float = DEFAULT_INQUIRY_S,
                       page: float = DEFAULT_PAGE_S,
                       hold: float = DEFAULT_HOLD_S,
                       no_answer_seconds: float = DEFAULT_NO_ANSWER_S) -> float:
    """Every wait a session may sit out, with its startup and its exit.

    The startup waits for the first line, then for the two after it and the
    prompt.
    """
    return (
        START_TIMEOUT_S + 3 * REPLY_TIMEOUT_S
        + sum(_check_seconds(label, inquiry=inquiry, page=page, hold=hold,
                             no_answer=no_answer_seconds)
              for label, _check in checks(no_answer))
        + EXIT_TIMEOUT_S
    )


def checks(no_answer: bool):
    """The checks to run, in order."""
    if not no_answer:
        return SMOKE_CHECKS
    # After the page that works and before the streams check, which reads
    # everything printed.
    checks_list = list(SMOKE_CHECKS)
    checks_list.insert(checks_list.index(("streams", check_streams)),
                       NO_ANSWER_CHECK)
    return tuple(checks_list)


# ---------------------------------------------------------------------------
# The session


def tool_path(environment) -> str | None:
    """The ubertooth-btbr this run built, and only that one.

    There is deliberately no fall back to whatever is on ``PATH``: the point
    of the test is the tool of the commit under test, and an installed copy
    from another commit answering in its place would be reported as a pass.
    """
    tools = environment.get(PYTHON_TOOLS_ENV)
    if not tools:
        return None
    candidate = os.path.join(tools, TOOL)
    return candidate if os.path.isfile(candidate) else None


def run_session(arguments, out, err, environment) -> int:
    tool = tool_path(environment)
    if tool is None:
        out.write(f"SKIP: {TOOL} is installed only by "
                  "run-suite --build --with-python\n")
        out.flush()
        return SKIP_EXIT
    argv = (tool, "-U", arguments.serial)
    console = Console(argv, err, environment,
                      keep=arguments.peer.replace(":", "").lower())

    def report(line: str) -> None:
        err.write(line + "\n")
        err.flush()

    report("btbr console: " + " ".join(argv))
    report(f"board serial: {arguments.serial}")
    report(f"peer address: {arguments.peer}")
    report(f"peer name: {arguments.peer_name}")
    session = Session(
        console, arguments.peer, arguments.peer_name,
        arguments.inquiry_seconds, arguments.page_seconds,
        arguments.hold_seconds, arguments.no_answer_seconds, report,
    )
    planned = checks(arguments.no_answer)
    failures: list[str] = []
    failed_checks: list[str] = []
    decided: list[str] = []
    try:
        # start() inside the try: a tool that cannot be run is reported in the
        # same line as everything else, not as a traceback.
        console.start()
        _start(console, report)
        for label, check in planned:
            reason = check(session)
            decided.append(label)
            if reason:
                failed_checks.append(label)
                failures.append(f"{label}: {reason}")
                report(f"check {label}: FAILED: {reason}")
    except ConsoleError as error:
        if NO_LIBUSB1 in console.text():
            # Nothing here can decide the session, and nothing on the bench is
            # wrong: the interpreter the tools were built for cannot open a
            # board. The case is skipped with that as its reason.
            report(f"session: not run: {error}")
            _end(console, report, [])
            out.write(f"SKIP: the interpreter {TOOL} runs has no libusb1 "
                      "package, so no session can open a board; install it "
                      'with "pip install libusb1"\n')
            out.flush()
            return SKIP_EXIT
        failures.append(str(error))
        report(f"session: FAILED: {error}")
    status = _end(console, report, failures)
    # Checks, not failures: a session-level failure (the startup, the exit, a
    # stream that overflowed) is not a check, and a session that stopped early
    # leaves checks that never ran. Reporting them apart is what keeps the
    # count honest.
    passed = len(decided) - len(failed_checks)
    report(f"checks: {passed} passed of {len(planned)}, "
           f"{len(failed_checks)} failed, {len(planned) - len(decided)} not run")
    if failures:
        report("session failed: " + "; ".join(failures))
        return 1
    return 0 if status == 0 else 1


def _start(console: Console, report) -> None:
    """Wait for the startup the `btbr` image answers, then for the prompt.

    The lines are not required in order. Two of them come from the host tool
    and `EIR updated` is a firmware line the receive thread ferries up, with
    only milliseconds between it and the intro, so which lands first is a
    race and not something to fail a board over. Only the first wait gets the
    long window: by the time one line has arrived the others are there or
    coming.
    """
    reached = 0
    window = START_TIMEOUT_S
    for needle in (USB_CONNECTED, EIR_UPDATED, INTRO):
        match, _ = console.wait(needle, window)
        if match is None:
            raise ConsoleError(
                f"{TOOL} printed no {needle!r} line within {window:g}s"
                + _why_not_started(console))
        reached = max(reached, match.end())
        window = REPLY_TIMEOUT_S
    prompt, _ = console.wait(PROMPT, REPLY_TIMEOUT_S, since=reached)
    if prompt is None:
        raise ConsoleError(f"the {INTRO!r} line arrived but the prompt did not")
    report(f"prompt: up after {_now() - console.started:.1f} s")


def _why_not_started(console: Console) -> str:
    """The tool's own reason for not starting, where it gave one.

    A session that never reaches its prompt usually said why in one `error: `
    line, and that line is worth far more than a guess: a board carrying
    `bluetooth_rxtx` times the first write out and says so, a board in use by
    another program says that, and a `--with-python` interpreter without the
    `libusb1` package says that. Without a line, the firmware is the likeliest
    cause and is named as likely rather than as fact.
    """
    said = re.findall(r"(?m)^(?:error|note): .*$", console.text())
    if said:
        return "; the tool said: " + "; ".join(said[:3])
    return "; a board carrying bluetooth_rxtx fails this way"


def _end(console: Console, report, failures: list[str]) -> int:
    """End the session and report how it went; never raise."""
    if console.process is None:
        # It never started, so there is no exit status and no duration.
        report("session exit: not started")
        return 1
    try:
        status = console.end()
    except ConsoleError as error:
        failures.append(str(error))
        report(f"session: FAILED: {error}")
        return 1
    elapsed = _now() - console.started
    report(f"session exit: {status} after {elapsed:.1f} s")
    if status != 0:
        failures.append(f"the session exited {status} after {elapsed:.1f} s; "
                        "a clean exit is 0")
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive the ubertooth-btbr console against a known peer")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("session")
    one.add_argument("--serial", required=True, help="the Ubertooth to open")
    one.add_argument("--peer", required=True, help="the peer's registered BD_ADDR")
    one.add_argument("--peer-name", required=True,
                     help="the name the peer's Extended Inquiry Response carries")
    one.add_argument("--inquiry-seconds", type=float, default=DEFAULT_INQUIRY_S,
                     help="how long the inquiry may take to hear the peer")
    one.add_argument("--page-seconds", type=float, default=DEFAULT_PAGE_S,
                     help="how long the peer may take to answer the page")
    one.add_argument("--hold-seconds", type=float, default=DEFAULT_HOLD_S,
                     help="how long the connection is held once it is up")
    one.add_argument("--no-answer-seconds", type=float,
                    default=DEFAULT_NO_ANSWER_S,
                     help="how long a page nothing answers may take to time out")
    one.add_argument("--no-answer", action="store_true",
                     help="also page an address nothing answers")
    arguments = parser.parse_args(argv)
    try:
        arguments.peer = esp32peer.normalize_address(arguments.peer)
    except esp32peer.PeerError as error:
        print(f"error: --peer {error}", file=sys.stderr)
        return 2
    if not arguments.peer_name:
        print("error: --peer-name must not be empty", file=sys.stderr)
        return 2
    out = open(sys.stdout.fileno(), "w", closefd=False)
    try:
        return run_session(arguments, out, sys.stderr, dict(os.environ))
    finally:
        out.flush()


if __name__ == "__main__":
    sys.exit(main())
