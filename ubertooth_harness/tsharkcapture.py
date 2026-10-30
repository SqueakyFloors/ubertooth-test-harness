"""Capture through the BLE extcap plugin with real tshark, and read the fields.

Run as a script by the BLE extcap in tshark cases::

    python tsharkcapture.py capture --interface ubertooth<serial> --seconds N
        --address ADDR [--channel 37|38|39] [--min-packets N]
        [--filter EXPR] [--interrupt-after S] [--program PATH]
    python tsharkcapture.py read FILE --address ADDR [--min-packets N]

The other extcap cases (``extcapcapture.py``) play Wireshark's part
themselves: they run ``btle-extcap.py`` with the arguments Wireshark sends and
check the PCAP stream with the harness's own parser. That gives exact control
and needs no Wireshark, so it stays the main coverage. What it cannot show is
that real Wireshark agrees: the calls a given Wireshark version actually
makes, and that Wireshark's own dissector reads the records the plugin sends
(link type 256 and libbtbb's 10-byte LE pseudo-header). The harness's parser
could share a misreading with the script; a dissector written by someone else
cannot (harness issue #148).

``capture`` installs the plugin in a Wireshark configuration of its own (a
temporary HOME, and the extcap directory tshark itself reports for it), so
nothing is written to the real user's configuration and no other extcap
program on the host can answer. Then it runs::

    tshark -i ubertooth<serial> -a duration:N -l -T fields -E header=n
           -e btle.advertising_address -e btle.advertising_header.pdu_type

and checks the rows. tshark starts in a session of its own, so a signal this
script sends reaches tshark alone: SIGINT to the whole process group would
also hit the ``dumpcap`` that reads the plugin's FIFO, and tshark would then
report a child that died rather than stopping its capture.

Checks (each failure is a ``FAIL:`` line and exit status 1):

- tshark lists the interface the plugin offers, and exits 0;
- at least ``--min-packets`` rows carry the advertiser's address with the
  expected PDU type (ADV_NONCONN_IND by default), which is Wireshark's own
  reading of what the plugin sent;
- ``--filter EXPR``: tshark passes it to the plugin as
  ``--extcap-capture-filter``, which cannot apply it; the plugin's warning
  must reach tshark (it relays the plugin's stderr) and the capture must run
  anyway;
- ``--interrupt-after S``: SIGINT after S seconds must end the capture
  cleanly, exit 0, with the rows decoded so far kept;
- no process the capture started is left behind.

Only counts and the advertiser's own address are printed, never another
device's address or any packet data, so no nearby traffic reaches the run
record. Exit status 2 is a usage or setup error; ``pytools.SKIP_EXIT`` with a
``SKIP: <reason>`` line means tshark is not installed on this host, or the run
has no extcap script to install.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

if __package__:
    from ubertooth_harness import pytools
else:  # run as a script: this directory is sys.path[0]
    import pytools

TSHARK = "tshark"
# What the plugin's records decode to: Wireshark's own field names.
ADDRESS_FIELD = "btle.advertising_address"
PDU_TYPE_FIELD = "btle.advertising_header.pdu_type"
PDU_TYPES = {"conn": 0x0, "nonconn": 0x2}
PDU_NAMES = {0x0: "ADV_IND", 0x2: "ADV_NONCONN_IND"}
# tshark's own limits: how long it may take to start, and to stop after the
# capture window or a SIGINT.
START_WAIT_S = 20.0
STOP_WAIT_S = 30.0
GONE_WAIT_S = 5.0
POLL_S = 0.05
VERSION_TIMEOUT_S = 20.0
# The program is installed under this name, so tshark's interface listing and
# any message about it name the plugin.
PROGRAM_NAME = "btle-extcap"
# Where tshark reports it looks for a personal extcap program; the fallback is
# the layout Wireshark 4.x uses when `-G folders` cannot be read.
EXTCAP_FOLDER_LINE = re.compile(r"(?mi)^Personal Extcap path:\s*(.+?)\s*$")
FALLBACK_EXTCAP_DIR = Path(".local") / "lib" / "wireshark" / "extcap"


def version(run=subprocess.run) -> str | None:
    """The installed tshark's version ("4.6.9"), or None when there is none."""
    if shutil.which(TSHARK) is None:
        return None
    try:
        result = run((TSHARK, "--version"), capture_output=True, text=True,
                     timeout=VERSION_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", (result.stdout or "").splitlines()[0]
                      if result.stdout else "")
    return found[1] if found else None


def _extcap_dir(home: Path, env) -> Path:
    """The extcap directory tshark reports for this HOME."""
    try:
        result = subprocess.run((TSHARK, "-G", "folders"), capture_output=True,
                                text=True, timeout=VERSION_TIMEOUT_S, env=env)
        found = EXTCAP_FOLDER_LINE.search(result.stdout or "")
    except (OSError, subprocess.SubprocessError):
        found = None
    if found:
        reported = Path(found[1])
        # Only a path inside this run's HOME: a tshark that reports a shared
        # directory must not have the plugin written into it.
        if home in reported.parents:
            return reported
    return home / FALLBACK_EXTCAP_DIR


@dataclass(frozen=True)
class Program:
    """The extcap program to install: one already runnable, or a script and
    the interpreter to run it with (the fork's btle-extcap.py, which CMake
    never installs, comes out of the run's commit as bytes)."""

    path: str | None = None
    interpreter: str | None = None
    script: bytes | None = None

    def argv(self, directory: Path) -> tuple:
        if self.path:
            return (self.path,)
        target = directory / "btle-extcap.py"
        target.write_bytes(self.script or b"")
        return (self.interpreter, str(target))


def _install(directory: Path, program: Program) -> Path:
    """Put a program at directory/PROGRAM_NAME that runs it with tshark's
    arguments appended. Wireshark runs an extcap program directly, so this
    wrapper is what carries the interpreter."""
    directory.mkdir(parents=True, exist_ok=True)
    argv = program.argv(directory)
    installed = directory / PROGRAM_NAME
    installed.write_text(
        "#!/bin/sh\nexec " + " ".join(shlex.quote(str(word)) for word in argv) + ' "$@"\n')
    installed.chmod(0o755)
    return installed


class Rows:
    """The field rows tshark printed, counted by address."""

    def __init__(self, address: str, pdu_type: int):
        self.address = address.lower()
        self.pdu_type = pdu_type
        self.rows = 0
        self.advertiser = 0
        self.wrong_pdu_type = 0
        self.other_addresses = 0
        self.no_address = 0

    def read(self, output: str) -> None:
        # One line per packet the dissector read, so a packet that is not
        # advertising (both fields empty) still counts as a row: the counts
        # then add up to what tshark says it captured.
        for line in output.splitlines():
            self.rows += 1
            fields = line.split("\t")
            found = fields[0].strip().lower()
            pdu = fields[1].strip() if len(fields) > 1 else ""
            if not found:
                self.no_address += 1
            elif found != self.address:
                self.other_addresses += 1
            elif _as_int(pdu) == self.pdu_type:
                self.advertiser += 1
            else:
                self.wrong_pdu_type += 1

    def summary(self) -> str:
        name = PDU_NAMES.get(self.pdu_type, f"PDU type {self.pdu_type:#04x}")
        return (
            f"rows: {self.rows} (from {self.address} as {name}: {self.advertiser}; "
            f"from it with another PDU type: {self.wrong_pdu_type}; from other "
            f"addresses: {self.other_addresses}; with no advertising address: "
            f"{self.no_address})"
        )


def _as_int(text: str) -> int | None:
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return None


def _children(pid: int) -> dict:
    """pid -> command line of the live children of pid."""
    try:
        result = subprocess.run(("pgrep", "-lf", "-P", str(pid)), capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if parts and parts[0].isdigit():
            found[int(parts[0])] = (parts[1] if len(parts) > 1 else "").strip()
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tshark_argv(interface: str, seconds: float, capture_filter: str | None = None,
                read: str | None = None) -> tuple:
    """The tshark command the cases run: the fields, nothing resolved."""
    argv = [TSHARK]
    if read is not None:
        argv += ["-r", read]
    else:
        argv += ["-i", interface, "-a", f"duration:{seconds:g}", "-l"]
        if capture_filter is not None:
            argv += ["-f", capture_filter]
    return tuple(argv + ["-T", "fields", "-E", "header=n",
                         "-e", ADDRESS_FIELD, "-e", PDU_TYPE_FIELD])


def _run_tshark(argv, env, seconds, interrupt_after, failures, out):
    """Run tshark to its end or to a SIGINT; (exit status, stdout, stderr)."""
    out("tshark command: " + " ".join(argv))
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        # A session of its own: a signal sent here reaches tshark alone, not
        # the dumpcap that reads the plugin's FIFO.
        process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, env=env,
                                   start_new_session=True)
        children = {}
        deadline = time.monotonic() + seconds + STOP_WAIT_S
        interrupt_at = (time.monotonic() + interrupt_after
                        if interrupt_after is not None else None)
        interrupted = False
        while process.poll() is None and time.monotonic() < deadline:
            children.update(_children(process.pid))
            if interrupt_at is not None and time.monotonic() >= interrupt_at:
                out(f"stopping tshark with SIGINT after {interrupt_after:g}s")
                process.send_signal(signal.SIGINT)
                interrupt_at = None
                interrupted = True
                deadline = time.monotonic() + STOP_WAIT_S
            time.sleep(POLL_S)
        if process.poll() is None:
            failures.append(
                f"tshark did not exit within {STOP_WAIT_S:g}s of the end of its "
                "capture; it was sent SIGTERM")
            process.terminate()
            try:
                process.wait(STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
        if interrupt_after is not None and not interrupted:
            failures.append("tshark exited before the SIGINT")
        stdout.seek(0)
        stderr.seek(0)
        return (process.poll(), stdout.read().decode(errors="replace"),
                stderr.read().decode(errors="replace"), children)


def capture(interface, seconds, address, *, program: Program,
            pdu_type=PDU_TYPES["nonconn"], min_packets=1, capture_filter=None,
            interrupt_after=None, out=print) -> int:
    """Capture through the plugin with tshark and check the decoded rows."""
    failures = []
    with tempfile.TemporaryDirectory(prefix="tshark-home-") as directory:
        home = Path(directory)
        env = dict(os.environ, HOME=str(home))
        # No locale surprises in the field output.
        env["LC_ALL"] = "C"
        extcap = _extcap_dir(home, env)
        installed = _install(extcap, program)
        out(f"extcap program: {installed.name} in {extcap}")
        listing = subprocess.run((TSHARK, "-D"), capture_output=True, text=True,
                                 timeout=START_WAIT_S, env=env)
        if interface not in listing.stdout:
            failures.append(f"tshark -D does not list {interface}")
        argv = tshark_argv(interface, seconds, capture_filter)
        status, stdout, stderr, children = _run_tshark(
            argv, env, seconds, interrupt_after, failures, out)
    return _report(status, stdout, stderr, children, address, pdu_type, min_packets,
                   capture_filter, failures, out)


def read_file(path, address, *, pdu_type=PDU_TYPES["nonconn"], min_packets=1,
              out=print) -> int:
    """Check what tshark's dissector reads from a capture file."""
    failures = []
    argv = tshark_argv("", 0, read=str(path))
    out("tshark command: " + " ".join(argv))
    env = dict(os.environ, LC_ALL="C")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=STOP_WAIT_S,
                            env=env)
    return _report(result.returncode, result.stdout, result.stderr, {}, address,
                   pdu_type, min_packets, None, failures, out)


def _report(status, stdout, stderr, children, address, pdu_type, min_packets,
            capture_filter, failures, out) -> int:
    rows = Rows(address, pdu_type)
    rows.read(stdout)
    for line in stderr.splitlines():
        # tshark's own notes (what it captured, what the plugin said) are
        # part of the record; a row of fields never is.
        out("tshark: " + line.strip())
    out(rows.summary())
    out(f"tshark exit: {status}")
    if status != 0:
        failures.append(f"tshark exited {status}, not 0")
    if rows.advertiser < min_packets:
        name = PDU_NAMES.get(pdu_type, f"PDU type {pdu_type:#04x}")
        failures.append(
            f"Wireshark decoded {rows.advertiser} {name} packet(s) from "
            f"{rows.address}, fewer than {min_packets}")
    if capture_filter is not None:
        warning = f"applies no capture filter" in stderr
        if not warning:
            failures.append(
                "the plugin's warning that it applies no capture filter did not "
                "reach tshark")
        if f"(got {capture_filter})" not in stderr:
            failures.append(
                f"the warning does not name the filter tshark passed on "
                f"({capture_filter})")
    deadline = time.monotonic() + GONE_WAIT_S
    left = [pid for pid in children if _alive(pid)]
    while left and time.monotonic() < deadline:
        time.sleep(POLL_S)
        left = [pid for pid in left if _alive(pid)]
    out("processes left: " + (", ".join(f"{pid} {children[pid]}" for pid in left)
                              if left else "none"))
    if left:
        failures.append(f"{len(left)} process(es) left running")
    for failure in failures:
        out(f"FAIL: {failure}")
    if failures:
        return 1
    out("tshark checks passed")
    return 0


def _program(given: str | None):
    """The extcap program to install: a given one, or the fork's
    btle-extcap.py from the run's commit; or the exit status to return."""
    if given:
        return Program(path=str(Path(given).resolve()))
    found = pytools.extcap_script()
    if isinstance(found, int):
        return found
    python, script = found
    return Program(interpreter=python, script=script)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tsharkcapture.py")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "read"):
        command = sub.add_parser(name)
        command.add_argument("--address", required=True,
                             help="the advertiser's BLE address")
        command.add_argument("--type", default="nonconn", choices=sorted(PDU_TYPES))
        command.add_argument("--min-packets", type=int, default=1)
    live = sub.choices["capture"]
    live.add_argument("--interface", required=True, help="ubertooth<serial>")
    live.add_argument("--seconds", type=float, required=True)
    live.add_argument("--filter", help="capture filter to pass to the plugin")
    live.add_argument("--interrupt-after", type=float,
                      help="send SIGINT this many seconds in")
    live.add_argument("--program", help="extcap program to install (default: the "
                                       "fork's btle-extcap.py)")
    sub.choices["read"].add_argument("file", help="capture file tshark reads")
    options = parser.parse_args(argv)
    if options.min_packets < 0:
        parser.error("--min-packets is a count of packets")
    installed = version()
    if installed is None:
        print("SKIP: tshark is not installed")
        return pytools.SKIP_EXIT
    print(f"tshark version: {installed}")
    pdu_type = PDU_TYPES[options.type]
    if options.command == "read":
        return read_file(options.file, options.address, pdu_type=pdu_type,
                         min_packets=options.min_packets)
    if options.interrupt_after is not None and options.interrupt_after >= options.seconds:
        parser.error("--interrupt-after must be inside the capture window")
    program = _program(options.program)
    if isinstance(program, int):
        return program
    return capture(options.interface, options.seconds, options.address,
                   program=program, pdu_type=pdu_type,
                   min_packets=options.min_packets, capture_filter=options.filter,
                   interrupt_after=options.interrupt_after)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
