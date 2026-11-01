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
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ubertooth_harness import esp32peer
from ubertooth_harness.pytools import PYTHON_TOOLS_ENV, SKIP_EXIT

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

# Startup: the board is opened, given its address and that response, and the
# thread reading it started before the prompt appears. A board carrying the
# wrong image times the first write out instead, in libusb's own time, so this
# is generous.
START_TIMEOUT_S = 30.0
# One prompt command that needs no radio: the answer is printed as the line is
# read.
REPLY_TIMEOUT_S = 10.0
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
# Paging, in the order the firmware reports it.
PAGING_STARTED = "Paging started"
PAGING_CONNECTED = "Central started"
PAGING_READY = re.compile(r"(?m)^Connection ready, lt_addr=(?P<lt_addr>\d+)$")
LMP_COMPLETE = "LMP setup complete"
PAGING_TIMEOUT = "paging timeout"
PAGING_DONE = "Paging done"
INQUIRY_STARTED = "Starting BTCtlInquiryCmd"
INQUIRY_DONE = "Inquiry done"
# An LMP request with no handler: one line per opcode, not per packet.
LMP_UNHANDLED = re.compile(
    r"(?m)^warning: LMP opcode (?P<opcode>0x[0-9a-f]+) is not handled "
    r"in state \d+, refused$")

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
# Lines that belong on one stream only.
CAPTURE_LINE = re.compile(r"(?m)^\d\d:\d\d:\d\d [A-Z0-9]")
PROGRESS_LINE = re.compile(
    r"(?m)^(?:bt> |State |Starting BTCtl|Send idle|Paging |Inquiry |"
    r"Connection |Central started|Rx thread)")

# A word that is no command and no address, and three addresses that are not
# ones. Nothing on the bench answers to any of them and none reaches the
# radio. An address shorter than six bytes is deliberately absent: the tool
# takes one today and acts on a different address (fork issue 1075), so a case
# here would pin that rather than check it.
BAD_WORD = "wibble"
BAD_ADDRESSES = ("", "zzz", "11:22:33:44:55:66:77")
# The address to set the board to, and the one to page for the no-answer
# check: neither belongs to anything on the bench.
TEST_ADDRESS = "22:33:44:55:66:77"
NO_DEVICE_ADDRESS = "11:22:33:44:55:66"


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

    def __init__(self, name: str, source, sink, prefix: str = ""):
        self.name = name
        self._source = source
        self._sink = sink
        self._prefix = prefix
        self._lock = threading.Lock()
        self._chunks: list[str] = []
        self._size = 0
        # The part of the last chunk with no newline yet, kept only so the
        # echo below writes whole lines. It is already in ``_chunks``, so it
        # is never added to ``text`` a second time: an offset into ``text``
        # has to mean the same thing before and after a chunk arrives, or
        # every wait that starts from one looks past its answer.
        self._unechoed = ""
        self.overflowed = False
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self) -> None:
        while True:
            chunk = self._source.read1(4096)
            if not chunk:
                break
            self._append(chunk.decode("utf-8", errors="replace"))
        with self._lock:
            tail, self._unechoed = self._unechoed, ""
        if tail:
            self._echo([tail])

    def _append(self, text: str) -> None:
        with self._lock:
            if self._size + len(text) > MAX_STREAM_CHARS:
                self.overflowed = True
            else:
                self._chunks.append(text)
                self._size += len(text)
            lines = (self._unechoed + text).split("\n")
            self._unechoed = lines.pop()
        self._echo(lines)

    def _echo(self, lines: Sequence[str]) -> None:
        # The tool's standard output is passed through unchanged; its standard
        # error is marked, so the report lines cannot be read as the tool's.
        if not lines:
            return
        self._sink.write("".join(self._prefix + line + "\n" for line in lines))
        self._sink.flush()

    @property
    def text(self) -> str:
        with self._lock:
            return "".join(self._chunks)


class Console:
    """A running ubertooth-btbr session: send a line, wait for an answer."""

    def __init__(self, argv: Sequence[str], out, err, environment=None):
        self.argv = tuple(argv)
        self._out = out
        self._err = err
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
            "stdout": Stream("stdout", self.process.stdout, self._out),
            "stderr": Stream("stderr", self.process.stderr, self._err, "btbr| "),
        }
        for stream in self.streams.values():
            stream.thread.start()

    def text(self, stream: str = "stderr") -> str:
        return self.streams[stream].text

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
    console = session.console
    mark = console.mark()
    console.send("?")
    heading, took = console.wait(HELP_HEADING, REPLY_TIMEOUT_S, since=mark)
    if heading is None:
        return f"? printed no {HELP_HEADING!r} within {REPLY_TIMEOUT_S:g}s"
    listed = console.text()[mark:]
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
    if match["address"] != wanted:
        return (f"bdaddr {TEST_ADDRESS} set {match['address']}, not {wanted}")
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
        heard = sorted({match["address"]
                        for match in FHS_BDADDR.finditer(console.text("stdout")[out_mark:])})
        return (f"the inquiry did not hear the peer {session.peer_bdaddr} in "
                f"{session.inquiry_seconds:g}s; it heard "
                + (", ".join(heard) if heard else "nothing"))
    # The response's Extended Inquiry Response follows it as fields of their
    # own, so the name arrives just after the address.
    named, _ = console.wait(
        re.compile(rf"COMPLETE_LOCAL_NAME: '{re.escape(session.peer_name)}'"),
        REPLY_TIMEOUT_S, stream="stdout", since=found.end())
    captured = console.text("stdout")[out_mark:]
    if named is None:
        names = [match["name"] for match in EIR_NAME.finditer(captured)]
        return (f"the inquiry heard the peer {session.peer_bdaddr} but no "
                f"Extended Inquiry Response named {session.peer_name!r}; it "
                "carried " + (", ".join(repr(name) for name in names)
                              if names else "no name"))
    responses = len(set(FHS_BDADDR.findall(captured)))
    session.report(
        f"check inquiry: peer {session.peer_bdaddr} answered after {took:.1f} s, "
        f"name {session.peer_name!r}, {responses} device(s) heard in all")
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
    session.opcodes = sorted(found)
    named = ", ".join(session.opcodes) if found else "none"
    session.report(f"check lmp-unhandled: {named} refused, each named once")
    return ""


def check_page_no_answer(session: Session) -> str:
    """A page nothing answers ends in a timeout, with no connection."""
    console = session.console
    mark = console.mark()
    console.send(f"page {NO_DEVICE_ADDRESS}")
    started, _ = console.wait(PAGING_STARTED, REPLY_TIMEOUT_S, since=mark)
    if started is None:
        return f"page {NO_DEVICE_ADDRESS} did not start paging"
    timeout, took = console.wait(PAGING_TIMEOUT, session.no_answer_seconds,
                                 since=started.end())
    if timeout is None:
        return (f"paging {NO_DEVICE_ADDRESS}, which nothing answers, did not "
                f"report {PAGING_TIMEOUT!r} in {session.no_answer_seconds:g}s")
    connected, _ = console.wait(PAGING_CONNECTED, 0.0, since=started.end())
    if connected is not None:
        return (f"paging {NO_DEVICE_ADDRESS} reported {PAGING_CONNECTED!r}: "
                "something answered for an address nothing has")
    ended, _ = console.wait(PAGING_DONE, REPLY_TIMEOUT_S, since=timeout.end())
    if ended is None:
        return f"the page timed out but did not report {PAGING_DONE!r}"
    session.report(f"check page-no-answer: {PAGING_TIMEOUT!r} after {took:.1f} s, "
                   "no connection")
    return ""


def check_streams(session: Session) -> str:
    """The capture is on standard output and the progress on standard error."""
    stray_capture = CAPTURE_LINE.search(session.console.text())
    if stray_capture:
        return f"a capture line is on stderr: {stray_capture[0]!r}"
    stray_progress = PROGRESS_LINE.search(session.console.text("stdout"))
    if stray_progress:
        return f"a progress line is on stdout: {stray_progress[0]!r}"
    captured = len(CAPTURE_LINE.findall(session.console.text("stdout")))
    session.report(f"check streams: {captured} capture lines on stdout, "
                   "no progress line among them")
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
    """The ubertooth-btbr this run built, or one on PATH."""
    tools = environment.get(PYTHON_TOOLS_ENV)
    if tools:
        candidate = os.path.join(tools, TOOL)
        return candidate if os.path.isfile(candidate) else None
    return shutil.which(TOOL, path=environment.get("PATH"))


def run_session(arguments, out, err, environment) -> int:
    tool = tool_path(environment)
    if tool is None:
        out.write(f"SKIP: {TOOL} is installed only by "
                  "run-suite --build --with-python\n")
        out.flush()
        return SKIP_EXIT
    argv = (tool, "-U", arguments.serial)
    console = Console(argv, out, err, environment)

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
    console.start()
    try:
        _start(console, report)
        for label, check in planned:
            reason = check(session)
            if reason:
                failures.append(f"{label}: {reason}")
                report(f"check {label}: FAILED: {reason}")
    except ConsoleError as error:
        failures.append(str(error))
        report(f"session: FAILED: {error}")
    status = _end(console, report, failures)
    for stream in console.streams.values():
        if stream.overflowed:
            failures.append(f"the session's {stream.name} passed "
                            f"{MAX_STREAM_CHARS} characters")
            report(f"session: FAILED: {failures[-1]}")
    report(f"checks: {len(planned) - len(failures)} passed of {len(planned)}, "
           f"{len(failures)} failed")
    if failures:
        report("session failed: " + "; ".join(failures))
        return 1
    return 0 if status == 0 else 1


def _start(console: Console, report) -> None:
    """Wait for the startup the `btbr` image answers, then for the prompt."""
    reached = 0
    for needle in (USB_CONNECTED, EIR_UPDATED, INTRO):
        match, _ = console.wait(needle, START_TIMEOUT_S, since=reached)
        if match is None:
            raise ConsoleError(
                f"{TOOL} printed no {needle!r} line within {START_TIMEOUT_S:g}s; "
                "a board carrying bluetooth_rxtx fails here")
        reached = match.end()
    prompt, _ = console.wait(PROMPT, REPLY_TIMEOUT_S, since=reached)
    if prompt is None:
        raise ConsoleError(f"the {INTRO!r} line arrived but the prompt did not")
    report(f"prompt: up after {_now() - console.started:.1f} s")


def _end(console: Console, report, failures: list[str]) -> int:
    """End the session and report how it went; never raise."""
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
