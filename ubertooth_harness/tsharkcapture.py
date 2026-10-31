"""Capture through the BLE extcap plugin with real tshark, and read the fields.

Run as a script by the BLE extcap in tshark cases::

    python tsharkcapture.py capture --interface ubertooth<serial> --seconds N
        --address ADDR [--min-packets N] [--filter EXPR]
        [--interrupt-after S] [--program PATH]
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

``capture`` installs the plugin in a Wireshark configuration of its own: a
temporary HOME with every directory Wireshark takes from the environment
pointed inside it, and the extcap directory tshark itself reports for that
environment, which the versions differ on. Nothing is written to the host
user's own configuration, and if tshark reports a directory outside it there
is no way to install the plugin for this capture alone, so the command fails
(exit 2) instead. The installed program records each call it is given, so the
checks can tell that it, and not another extcap program on the host, is what
tshark captured with. Then it runs::

    tshark -i ubertooth<serial> -a duration:N -l -T fields -E header=n
           -e btle.advertising_address -e btle.advertising_header.pdu_type

and checks the rows. tshark starts in a session of its own, so a signal this
script sends reaches tshark alone: SIGINT to the whole process group would
also hit the ``dumpcap`` that reads the plugin's FIFO, and tshark would then
report a child that died rather than stopping its capture. For the same
reason the SIGINT goes in only once a row has been decoded, which is the proof
that dumpcap is capturing; with nothing decoded in the window there is nothing
to stop a capture after, and the run reports a skip rather than a guess.

Checks (each failure is a ``FAIL:`` line and exit status 1):

- tshark lists the interface the plugin offers, and exits 0;
- at least ``--min-packets`` rows carry the advertiser's address with the
  expected PDU type (ADV_NONCONN_IND by default), which is Wireshark's own
  reading of what the plugin sent;
- ``--filter EXPR``: tshark passes it to the plugin as
  ``--extcap-capture-filter``, which cannot apply it; the plugin's warning
  must reach tshark (it relays the plugin's stderr) and the capture must run
  anyway;
- ``--interrupt-after S``: SIGINT, no sooner than S seconds in and not before
  a row has been decoded, must end the capture cleanly, exit 0, with the rows
  decoded so far kept;
- the program installed here is the one tshark captured with;
- no process the capture started is left behind, the extcap program included:
  it is dumpcap's child, not tshark's, and is checked by the process id it
  recorded itself.

Only counts and the advertiser's own address are printed, never another
device's address or any packet data, so no nearby traffic reaches the run
record. Exit status 2 is a usage or setup error; ``pytools.SKIP_EXIT`` with a
``SKIP: <reason>`` line means tshark is not installed on this host, the run
has no extcap script to install, or (``--interrupt-after``) nothing was
decoded to stop the capture after.
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
# tshark's own limits. The whole command must finish inside the step's
# timeout (tshark_cases.CAPTURE_SLACK_S, which test_tshark_capture checks
# against these): tshark runs in a session of its own, so a command killed
# from outside would leave it behind untracked.
START_WAIT_S = 20.0
# How long past the end of the capture window, or past a SIGINT, tshark may
# take to exit before it is stopped.
EXIT_WAIT_S = 10.0
# How long after the SIGTERM before it counts as left running.
STOP_WAIT_S = 10.0
# Reading a capture file opens no device and starts no capture.
READ_TIMEOUT_S = 60.0
GONE_WAIT_S = 5.0
# One pass of the process-tree scan: pgrep per level, and the loop checks its
# deadline between passes, so a pass can run past it by this much.
SCAN_WAIT_S = 5.0
SCAN_DEPTH = 4
SCAN_BUDGET_S = SCAN_WAIT_S * SCAN_DEPTH
POLL_S = 0.05
VERSION_TIMEOUT_S = 20.0
# The program is installed under this name, so tshark's interface listing and
# any message about it name the plugin.
PROGRAM_NAME = "btle-extcap"
# Where tshark reports it looks for a personal extcap program; the fallback is
# the layout Wireshark 4.x uses when `-G folders` cannot be read.
EXTCAP_FOLDER_LINE = re.compile(r"(?mi)^Personal Extcap path:\s*(.+?)\s*$")
FALLBACK_EXTCAP_DIR = Path(".local") / "lib" / "wireshark" / "extcap"
NO_ROWS = "no packet reached tshark in the capture window"


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


def _extcap_dir(home: Path, env) -> Path | None:
    """Where tshark looks for a personal extcap program with this
    environment, or None when that is not inside home (so installing the
    plugin would touch the host user's own configuration).

    tshark is asked rather than told: which directory it is has changed
    between Wireshark versions.
    """
    try:
        result = subprocess.run((TSHARK, "-G", "folders"), capture_output=True,
                                text=True, timeout=VERSION_TIMEOUT_S, env=env)
        found = EXTCAP_FOLDER_LINE.search(result.stdout or "")
    except (OSError, subprocess.SubprocessError):
        found = None
    reported = Path(found[1]) if found else home / FALLBACK_EXTCAP_DIR
    return reported if home in reported.parents else None


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


def _install(directory: Path, program: Program, marker: Path) -> Path:
    """Put a program at directory/PROGRAM_NAME that runs it with tshark's
    arguments appended. Wireshark runs an extcap program directly, so this
    wrapper is what carries the interpreter; it records its own process id and
    arguments in marker first, so the checks can tell that the program this
    installed is the one that answered, and that it is gone afterwards."""
    directory.mkdir(parents=True, exist_ok=True)
    argv = program.argv(directory)
    installed = directory / PROGRAM_NAME
    installed.write_text(
        "#!/bin/sh\n"
        f"printf '%s %s\\n' \"$$\" \"$*\" >> {shlex.quote(str(marker))}\n"
        "exec " + " ".join(shlex.quote(str(word)) for word in argv) + ' "$@"\n')
    installed.chmod(0o755)
    return installed


def _marker_calls(marker: Path) -> list:
    """(pid, arguments) of each run of the installed program."""
    if not marker.exists():
        return []
    calls = []
    for line in marker.read_text(errors="replace").splitlines():
        pid, _, arguments = line.partition(" ")
        if pid.isdigit():
            calls.append((int(pid), arguments))
    return calls


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
                                text=True, timeout=SCAN_WAIT_S)
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if parts and parts[0].isdigit():
            found[int(parts[0])] = (parts[1] if len(parts) > 1 else "").strip()
    return found


def _descendants(pid: int, depth: int = SCAN_DEPTH) -> dict:
    """pid -> command line of everything below pid: tshark starts dumpcap,
    which is what starts the extcap program, so children alone miss it."""
    found = {}
    frontier = [pid]
    for _level in range(depth):
        next_frontier = []
        for parent in frontier:
            for child, command in _children(parent).items():
                if child not in found:
                    found[child] = command
                    next_frontier.append(child)
        if not next_frontier:
            break
        frontier = next_frontier
    return found


def _alive(pid: int) -> bool:
    """True while that process id is one of ours and still there.

    Every process id checked here was started by this command, so one that
    now belongs to another user is a reused number, not a process left
    behind.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
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


def _run_tshark(argv, env, seconds, interrupt_after, failures, out, directory):
    """Run tshark to the end of its capture window, or to a SIGINT.

    Returns (exit status, stdout, stderr, descendants seen, quiet): quiet when
    a SIGINT was asked for and no packet ever reached tshark, so it was not
    sent and the capture ran its window out.

    The SIGINT goes to tshark once the capture is under way, which a decoded
    row is the proof of: tshark hands its own capture over to a dumpcap child,
    and a SIGINT that arrives before that child is capturing ends the run as a
    child that died (exit 255) instead of a capture that was stopped.
    """
    out("tshark command: " + " ".join(argv))
    stdout_path = directory / "tshark.out"
    with open(stdout_path, "w+b") as stdout, tempfile.TemporaryFile() as stderr:
        # A session of its own: the signal reaches tshark alone, never the
        # dumpcap that reads the plugin's FIFO (which a signal to the process
        # group would also hit).
        process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, env=env,
                                   start_new_session=True)
        seen = {}
        deadline = time.monotonic() + seconds + EXIT_WAIT_S
        not_before = (time.monotonic() + interrupt_after
                      if interrupt_after is not None else None)
        interrupted = quiet = False
        while process.poll() is None and time.monotonic() < deadline:
            seen.update(_descendants(process.pid))
            if not_before is not None and time.monotonic() >= not_before:
                rows = len(_read(stdout_path).splitlines())
                if rows:
                    out(f"stopping tshark with SIGINT, {rows} row(s) decoded")
                    process.send_signal(signal.SIGINT)
                    not_before = None
                    interrupted = True
                    deadline = time.monotonic() + EXIT_WAIT_S
            time.sleep(POLL_S)
        if interrupt_after is not None and not interrupted:
            # Nothing was decoded in time to interrupt after, so the capture
            # ran its window out and the SIGINT check was not made.
            quiet = True
        if process.poll() is None:
            # The window and EXIT_WAIT_S are gone: stop it now rather than
            # wait again, so the whole command stays inside the step's
            # timeout and nothing is left for it to kill.
            failures.append(
                f"tshark did not exit within {EXIT_WAIT_S:g}s of the end of its "
                "capture; it was sent SIGTERM")
            process.terminate()
            try:
                process.wait(STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            # It is in a session of its own, so nothing outside this command
            # will reap it: name it among what was left running, which fails
            # the case. Nothing is ever stopped with SIGKILL
            # (SqueakyFloors/ubertooth#239), here or in extcapcapture.py.
            seen[process.pid] = " ".join(argv)
        stderr.seek(0)
        return (process.poll(), _read(stdout_path),
                stderr.read().decode(errors="replace"), seen, quiet)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _isolated_env(home: Path) -> dict:
    """An environment whose Wireshark configuration is all inside home.

    Every place Wireshark 3.x and 4.x take their personal directories from, so
    the host user's own configuration is neither read nor written and an
    extcap program installed here is the only personal one.
    """
    env = dict(os.environ, HOME=str(home))
    env["XDG_DATA_HOME"] = str(home / "data")
    env["XDG_CONFIG_HOME"] = str(home / "config")
    env["XDG_CACHE_HOME"] = str(home / "cache")
    env["WIRESHARK_CONFIG_DIR"] = str(home / "wireshark")
    # No locale surprises in the field output.
    env["LC_ALL"] = "C"
    return env


def capture(interface, seconds, address, *, program: Program,
            pdu_type=PDU_TYPES["nonconn"], min_packets=1, capture_filter=None,
            interrupt_after=None, out=print) -> int:
    """Capture through the plugin with tshark and check the decoded rows."""
    failures = []
    with tempfile.TemporaryDirectory(prefix="tshark-home-") as directory:
        home = Path(directory)
        env = _isolated_env(home)
        extcap = _extcap_dir(home, env)
        if extcap is None:
            print("error: this tshark does not take its personal extcap directory "
                  "from the environment, so the plugin cannot be installed for it "
                  "alone; its own configuration must not be used", file=sys.stderr)
            return 2
        marker = home / "extcap-calls"
        installed = _install(extcap, program, marker)
        out(f"extcap program: {installed.name} in {extcap}")
        # Listing the interfaces runs the program too, so a tshark that does
        # not answer is reported like any other failure rather than ending
        # the command with a traceback; what the program started is caught by
        # the process ids it recorded, below.
        try:
            listing = subprocess.run((TSHARK, "-D"), capture_output=True, text=True,
                                     timeout=START_WAIT_S, env=env).stdout
        except subprocess.TimeoutExpired:
            listing = ""
            failures.append(f"tshark -D did not answer within {START_WAIT_S:g}s")
        if interface not in listing:
            failures.append(f"tshark -D does not list {interface}")
        argv = tshark_argv(interface, seconds, capture_filter)
        status, stdout, stderr, seen, quiet = _run_tshark(
            argv, env, seconds, interrupt_after, failures, out, home)
        calls = _marker_calls(marker)
    for pid, arguments in calls:
        out(f"the installed program ran: {arguments}")
    if not any("--capture" in arguments for _pid, arguments in calls):
        failures.append(
            "the program installed here was not the one tshark captured with")
    # The extcap program is dumpcap's child, not tshark's, and may outlive
    # both: it is checked by the process id it recorded itself.
    seen.update({pid: f"{PROGRAM_NAME} {arguments}" for pid, arguments in calls})
    return _report(status, stdout, stderr, seen, address, pdu_type, min_packets,
                   capture_filter, failures, out, quiet=quiet,
                   interrupted=interrupt_after is not None and not quiet)


def read_file(path, address, *, pdu_type=PDU_TYPES["nonconn"], min_packets=1,
              out=print) -> int:
    """Check what tshark's dissector reads from a capture file."""
    failures = []
    argv = tshark_argv("", 0, read=str(path))
    out("tshark command: " + " ".join(argv))
    env = dict(os.environ, LC_ALL="C")
    result = subprocess.run(argv, capture_output=True, text=True,
                            timeout=READ_TIMEOUT_S, env=env)
    return _report(result.returncode, result.stdout, result.stderr, {}, address,
                   pdu_type, min_packets, None, failures, out)


def _report(status, stdout, stderr, children, address, pdu_type, min_packets,
            capture_filter, failures, out, *, quiet=False, interrupted=False) -> int:
    rows = Rows(address, pdu_type)
    rows.read(stdout)
    for line in stderr.splitlines():
        # tshark's own notes (what it captured, what the plugin said) are
        # part of the record; a row of fields never is.
        out("tshark: " + line.strip())
    out(rows.summary())
    out(f"tshark exit: {status}" + (" (after SIGINT)" if interrupted else ""))
    if status != 0:
        failures.append(f"tshark exited {status}, not 0")
    if quiet:
        # Nothing was decoded at all, so there was nothing to stop the capture
        # after and nothing to count: that is the skip below, not a shortfall.
        pass
    elif rows.advertiser < min_packets:
        name = PDU_NAMES.get(pdu_type, f"PDU type {pdu_type:#04x}")
        failures.append(
            f"Wireshark decoded {rows.advertiser} {name} packet(s) from "
            f"{rows.address}, fewer than {min_packets}")
    if capture_filter is not None:
        if "applies no capture filter" not in stderr:
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
    if quiet:
        # There was nothing decoded to stop the capture after, so what a
        # SIGINT does was not shown (the other checks still passed).
        out(f"SKIP: {NO_ROWS}; the capture ran its window out instead")
        return pytools.SKIP_EXIT
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
    # Every argument is checked before the host is looked at, so a command
    # line that is wrong is a usage error on any host, with or without tshark.
    if options.min_packets < 0:
        parser.error("--min-packets is a count of packets")
    if options.command == "capture" and options.interrupt_after is not None and (
            options.interrupt_after >= options.seconds):
        parser.error("--interrupt-after must be inside the capture window")
    installed = version()
    if installed is None:
        print("SKIP: tshark is not installed")
        return pytools.SKIP_EXIT
    print(f"tshark version: {installed}")
    pdu_type = PDU_TYPES[options.type]
    if options.command == "read":
        return read_file(options.file, options.address, pdu_type=pdu_type,
                         min_packets=options.min_packets)
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
