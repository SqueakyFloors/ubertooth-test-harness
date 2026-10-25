"""Run ``btle-extcap.py --capture`` as Wireshark does and check the FIFO.

Run as a script by the BLE extcap capture cases::

    python extcapcapture.py --seconds N --fifo PATH [--board SERIAL] [--channel C]
                            [--refused] -- EXTCAP-ARGS...

It creates the FIFO, opens it for reading, starts the fork's
``host/python/extcap/btle-extcap.py`` (from the run's commit, as pytools.py
finds it) with EXTCAP-ARGS, reads the FIFO for N seconds, then stops the
script with SIGTERM, which it forwards to the ``ubertooth-btle`` it runs.
Nothing is ever stopped with SIGKILL (SqueakyFloors/ubertooth#239).

The script runs ``ubertooth-btle -f -U <serial> -q <fifo> -A <channel>`` at
fork 71e1a8a: ``-q`` writes PCAP with LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR (256)
through libbtbb, whose records carry a 10-byte LE pseudo-header whose first
byte is the RF channel (0 for advertising channel 37, 12 for 38, 39 for 39).

Checks (each failure is a ``FAIL:`` line and exit status 1):

- the stream is a PCAP global header for link type 256 followed by whole,
  well-formed records, and no more than MAX_BYTES of it (a longer stream
  fails rather than being checked only in part);
- ``--channel C``: the ``ubertooth-btle`` command has ``-A C`` and every
  advertising record (access address 8e89bed6) is on C's RF channel;
- ``--board SERIAL``: the ``ubertooth-btle`` command selects ``-U SERIAL``;
- the script is still capturing at the stop and exits 0 after SIGTERM, and no
  process it started is left running;
- ``--refused``: instead, the script must end by itself before the stop with
  a non-zero status and write no packet records.

The script's stdout (and so ``ubertooth-btle``'s decoded packets) is
discarded, and only counts are printed, so no captured traffic reaches the
run record. Exit status 2 is a usage or setup error; pytools.SKIP_EXIT means
there is no interpreter to run the script.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time

if __package__:
    from ubertooth_harness import pytools
else:  # run as a script: this directory is sys.path[0]
    import pytools

LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR = 256
# Advertising channel index -> RF channel in the LE pseudo-header.
ADV_RF_CHANNEL = {37: 0, 38: 12, 39: 39}
ADV_ACCESS_ADDRESS = 0x8E89BED6
LE_PHDR_LEN = 10
# Pseudo-header, then at least the access address.
MIN_RECORD_LEN = LE_PHDR_LEN + 4
MAX_BYTES = 32 * 1024 * 1024
STOP_WAIT_S = 10.0
DRAIN_S = 5.0
GONE_WAIT_S = 5.0
POLL_S = 0.05
CHILD_SCAN_S = 0.25

_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", "microsecond"), b"\xa1\xb2\xc3\xd4": (">", "microsecond"),
    b"\x4d\x3c\xb2\xa1": ("<", "nanosecond"), b"\xa1\xb2\x3c\x4d": (">", "nanosecond"),
}


@dataclass
class Stream:
    header: str = ""
    link_type: int | None = None
    records: int = 0
    data_records: int = 0
    # RF channel -> advertising records on it.
    advertising: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)

    def summary(self) -> str:
        channels = ", ".join(
            f"rf channel {channel} x{count}"
            for channel, count in sorted(self.advertising.items())
        )
        advertising = sum(self.advertising.values())
        return (
            f"records: {self.records} (advertising {advertising}"
            + (f": {channels}" if channels else "")
            + f"; data {self.data_records})"
        )


def parse_stream(data: bytes) -> Stream:
    """Walk a PCAP stream from libbtbb by its length fields."""
    stream = Stream()
    if not data:
        return stream
    if len(data) < 24:
        stream.problems.append(f"{len(data)} bytes, a truncated PCAP global header")
        return stream
    found = _MAGIC.get(data[:4])
    if found is None:
        stream.problems.append(f"magic {data[:4].hex()} is not a PCAP magic number")
        return stream
    endian, resolution = found
    major, minor, _zone, _sigfigs, snaplen, link_type = struct.unpack(
        endian + "HHiIII", data[4:24])
    stream.link_type = link_type
    stream.header = (
        f"magic {data[:4].hex()} ({resolution}), version {major}.{minor}, "
        f"snaplen {snaplen}, link type {link_type}"
    )
    if (major, minor) != (2, 4):
        stream.problems.append(f"PCAP version {major}.{minor}, not 2.4")
    if link_type != LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR:
        stream.problems.append(
            f"link type {link_type}, not {LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR}")
    fraction_limit = 1_000_000_000 if resolution == "nanosecond" else 1_000_000
    offset = 24
    while offset < len(data):
        index = stream.records + 1
        if len(data) - offset < 16:
            stream.problems.append(f"record {index}: truncated record header")
            break
        _seconds, fraction, incl_len, orig_len = struct.unpack(
            endian + "IIII", data[offset:offset + 16])
        body = data[offset + 16:offset + 16 + incl_len]
        if len(body) < incl_len:
            stream.problems.append(
                f"record {index}: {len(body)} of {incl_len} bytes, truncated")
            break
        problem = ""
        if fraction >= fraction_limit:
            problem = f"sub-second field {fraction} is not below {fraction_limit}"
        elif snaplen and incl_len > snaplen:
            problem = f"{incl_len} bytes, more than the snaplen {snaplen}"
        elif orig_len < incl_len:
            problem = f"original length {orig_len} is below the captured {incl_len}"
        elif incl_len < MIN_RECORD_LEN:
            problem = (f"{incl_len} bytes, too short for the LE pseudo-header and "
                       "access address")
        elif body[0] > 39:
            problem = f"RF channel {body[0]} is not 0-39"
        if problem:
            stream.problems.append(f"record {index}: {problem}")
            break
        stream.records += 1
        access_address = struct.unpack("<I", body[LE_PHDR_LEN:LE_PHDR_LEN + 4])[0]
        if access_address == ADV_ACCESS_ADDRESS:
            stream.advertising[body[0]] = stream.advertising.get(body[0], 0) + 1
        else:
            stream.data_records += 1
        offset += 16 + incl_len
    return stream


def _children(pid: int) -> dict:
    """pid -> command line of the live children of pid: pgrep -P for the
    pids, then /proc (Linux) or pgrep -lf (macOS) for the command lines."""
    try:
        result = subprocess.run(
            ("pgrep", "-lf", "-P", str(pid)), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        child = int(parts[0])
        try:
            with open(f"/proc/{child}/cmdline", "rb") as file:
                command = file.read().rstrip(b"\0").replace(b"\0", b" ").decode(
                    errors="replace")
        except OSError:
            command = parts[1] if len(parts) > 1 else ""
        if command:
            found[child] = command.strip()
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _btle_tokens(command: str) -> list:
    """The ubertooth-btle command line in a ps command (an interpreter may
    come first for a script), or []."""
    tokens = command.split()
    for index, token in enumerate(tokens):
        if os.path.basename(token) == "ubertooth-btle":
            return tokens[index:]
    return []


def _selects(tokens, option, value) -> bool:
    """tokens hold OPTION VALUE or OPTIONVALUE."""
    for index, token in enumerate(tokens):
        if token == option + value:
            return True
        if token == option and index + 1 < len(tokens) and tokens[index + 1] == value:
            return True
    return False


class _Reader:
    """Reads the FIFO into data, up to MAX_BYTES. Past that it keeps reading
    (so the writer never blocks) but drops the bytes and counts them in
    overflow, which fails the capture."""

    def __init__(self, fd):
        self.fd = fd
        self.data = bytearray()
        self.overflow = 0
        self.eof = False

    def read(self) -> bool:
        """Read what is there; True when something was read."""
        try:
            chunk = os.read(self.fd, 65536)
        except BlockingIOError:
            # A writer has the FIFO open but sent nothing yet.
            self.eof = False
            return False
        # With no writer (none yet, or it has closed) a read returns nothing.
        self.eof = not chunk
        if not chunk:
            return False
        room = MAX_BYTES - len(self.data)
        self.data += chunk[:room]
        self.overflow += max(0, len(chunk) - room)
        return True


def capture(argv, fifo, seconds, *, board=None, channel=None, refused=False,
            out=print) -> int:
    """Run argv (the extcap script) against fifo; print the checks; 0 or 1."""
    failures = []
    os.mkfifo(fifo, 0o600)
    try:
        # A non-blocking read end opens at once and lets the script's write
        # end open (it blocks until a reader exists).
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        reader = _Reader(fd)
        try:
            with tempfile.TemporaryFile() as errors:
                returncode, stopped, children = _run(
                    argv, reader, seconds, errors, failures, refused, out)
                errors.seek(0)
                diagnostics = errors.read().decode(errors="replace")
        finally:
            os.close(fd)
    finally:
        if stat.S_ISFIFO(os.lstat(fifo).st_mode):
            os.unlink(fifo)
    if diagnostics:
        sys.stderr.write(diagnostics)

    btle = {pid: tokens for pid, tokens in (
        (pid, _btle_tokens(command)) for pid, command in children.items()) if tokens}
    for pid, command in sorted(children.items()):
        if pid in btle:
            out("ubertooth-btle command: " + " ".join(btle[pid]))
        else:
            out(f"child command: {command}")
    stream = parse_stream(bytes(reader.data))
    out(f"PCAP header: {stream.header or 'none'}")
    out(stream.summary())
    when = "after SIGTERM" if stopped else "before the stop"
    out(f"extcap exit: {returncode} ({when})" if returncode is not None
        else "extcap exit: none, still running")
    problems = list(stream.problems)
    if reader.overflow and problems and "truncated" in problems[-1]:
        # The cut at MAX_BYTES, not the writer's: the overflow failure says so.
        problems.pop()
    failures += problems
    if reader.overflow:
        failures.append(
            f"the FIFO carried {len(reader.data) + reader.overflow} bytes, more than "
            f"the {MAX_BYTES} checked; the stream past that was not checked")

    if refused:
        if stopped:
            failures.append("btle-extcap was still capturing at the stop")
        elif returncode == 0:
            failures.append("btle-extcap exited 0 although its capture failed")
        if stream.records:
            failures.append(f"{stream.records} packet records were written")
    else:
        if not stopped:
            failures.append(f"btle-extcap exited {returncode} before the stop")
        elif returncode != 0:
            failures.append(f"btle-extcap exited {returncode} after SIGTERM, not 0")
        if stream.link_type is None and not stream.problems:
            failures.append("no PCAP header reached the FIFO")
        if not btle:
            failures.append("no ubertooth-btle process was seen")
        for tokens in btle.values():
            if channel is not None and not _selects(tokens, "-A", str(channel)):
                failures.append(f"ubertooth-btle was not given -A {channel}")
            if board is not None and not _selects(tokens, "-U", str(board)):
                failures.append(f"ubertooth-btle was not given -U {board}")
        if channel is not None:
            expected = ADV_RF_CHANNEL[channel]
            other = sorted(rf for rf in stream.advertising if rf != expected)
            if other:
                failures.append(
                    f"advertising records on RF channel(s) {other}, not only "
                    f"{expected} (advertising channel {channel})")

    deadline = time.monotonic() + GONE_WAIT_S
    left = [pid for pid in children if _alive(pid)]
    while left and time.monotonic() < deadline:
        time.sleep(POLL_S)
        left = [pid for pid in left if _alive(pid)]
    out("processes left: " + (", ".join(
        f"{pid} {children[pid]}" for pid in left) if left else "none"))
    if left:
        failures.append(f"{len(left)} process(es) left running")
    if returncode is None:
        failures.append("btle-extcap did not exit after SIGTERM")

    for failure in failures:
        out(f"FAIL: {failure}")
    if not failures:
        out("capture checks passed")
    return 1 if failures else 0


def _run(argv, reader, seconds, errors, failures, refused, out):
    out("extcap command: " + " ".join(argv))
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=errors)
    children = {}
    deadline = time.monotonic() + seconds
    next_scan = 0.0
    while time.monotonic() < deadline and process.poll() is None:
        if time.monotonic() >= next_scan:
            children.update(_children(process.pid))
            next_scan = time.monotonic() + CHILD_SCAN_S
        if not reader.read():
            time.sleep(POLL_S)
    stopped = process.poll() is None
    if stopped:
        children.update(_children(process.pid))
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            # The script did not forward the stop: stop its children the
            # same way, never with SIGKILL.
            failures.append(
                f"btle-extcap did not exit within {STOP_WAIT_S:g}s of SIGTERM; "
                "its children were sent SIGTERM")
            for pid in children:
                if _alive(pid):
                    os.kill(pid, signal.SIGTERM)
            try:
                process.wait(STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
    # Drain what the writer left once it has closed the FIFO.
    drain = time.monotonic() + DRAIN_S
    while not reader.eof and time.monotonic() < drain:
        if not reader.read():
            time.sleep(POLL_S)
    if not reader.eof and not refused:
        failures.append("the FIFO was still open for writing after the stop")
    return process.poll(), stopped, children


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="extcapcapture.py")
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--fifo", required=True)
    parser.add_argument("--board")
    parser.add_argument("--channel", type=int, choices=sorted(ADV_RF_CHANNEL))
    parser.add_argument("--refused", action="store_true")
    parser.add_argument("extcap_args", nargs="+", metavar="EXTCAP-ARGS")
    options = parser.parse_args(argv)
    if os.path.lexists(options.fifo):
        print(f"error: {options.fifo} already exists", file=sys.stderr)
        return 2
    found = pytools.extcap_script()
    if isinstance(found, int):
        return found
    python, script = found
    with tempfile.TemporaryDirectory(prefix="btle-extcap-") as directory:
        path = os.path.join(directory, "btle-extcap.py")
        with open(path, "wb") as file:
            file.write(script)
        return capture(
            (python, path, *options.extcap_args), options.fifo, options.seconds,
            board=options.board, channel=options.channel, refused=options.refused,
        )


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
