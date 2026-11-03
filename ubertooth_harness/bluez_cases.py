"""The two BlueZ tools, against a host Bluetooth adapter and the ESP32 pair.

``ubertooth-scan`` and ``ubertooth-follow`` are the only host tools the fork
builds just when libbluetooth is found, and the only ones that drive a second
radio: a Bluetooth controller of the host's, which BlueZ names ``hciN``. An
Ubertooth cannot stand in for it. ``ubertooth-scan`` runs that controller's
inquiry and reads the name of what it finds; ``ubertooth-follow`` pages a
target from it to learn the target's clock offset, and with ``-a`` reads the
channel map of the link it made. Both then capture with the Ubertooth.

So these cases bind three things: the Ubertooth, the adapter (the
``Bluetooth adapter`` resource), and the two ESP32 Classic peers, which hold
a Serial Port Profile link and stream across it as they do for the cases in
``classic_cases``. The pair is what gives the adapter something known to
find: both boards are discoverable and connectable from boot and stay so
through a session, since switching a board's role does not touch its scan
mode (firmware/esp32-classic-peer, ``disc``), and the initiator pages, so it
is the piconet central and its LAP is the one on the air.

Each case therefore has three parts:

- a read of the adapter itself, before and after, with ``hciconfig`` and
  ``hcitool``: it is up, it is the controller the registry names, and the
  session left no link to a peer behind. The HCI device name is the order the
  kernel attached controllers in, not a property of the adapter, so the
  address is what identifies it; an adapter that came back as another
  ``hciN`` fails here, naming what was found, rather than driving whatever
  took the name;
- one ``esp32peer.py stream`` session, which holds both bridges, brings the
  link up, runs the tool, and reports what the two boards did. Opening an
  ESP32's bridge resets it, so the pair cannot be set up in one step and used
  in the next; ``stream`` is the answer to that, and the tool's command line
  is visible in the step's argv after ``--``;
- the checks over the tool's own standard output, which is passed through
  unchanged.

``ubertooth-scan`` bounds its own capture with ``-t``. ``ubertooth-follow``
has no such option -- its ``-t`` is the target address -- and captures until
it is signalled, so those cases give the driver ``--interrupt-after``, which
is what ends the capture, and ``--min-seconds``, so a run that stopped early
fails instead of passing on a shorter capture.

**What these cases do not claim.** Nothing here has run against a real
adapter: the bench has no Bluetooth controller
([#88](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/88)).
The checks are written to what the two tools document and print, and the
plan that names them is not in the release gate. ``docs/tests.md`` records,
per case, which outcome is the tool's documented one and which is a reading
of its source that the first real run has to confirm.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Mapping

from ubertooth_harness import afhmap, capturefile, esp32peer
from ubertooth_harness.classic_cases import (
    ACCEPTOR_ROLE,
    INITIATOR_ROLE,
    MIN_PACKETS,
    MIN_WRITES,
    PAIR_SETUP_S,
    STREAM_PAYLOAD,
    STREAM_PERIOD_MS,
    Check,
    _all,
    _capture_file_step,
    _capture_path,
    _pair,
    _pair_patterns,
    _record_checks,
    _record_step,
    _reset_step,
    piconet_packets,
)
from ubertooth_harness.hostneeds import Needs
from ubertooth_harness.outputfiles import text_lines
from ubertooth_harness.receive_cases import EARLY_EXIT_S, EXIT_SLACK_S, seconds_arg
from ubertooth_harness.resources import BLUETOOTH_ADAPTER
from ubertooth_harness.stateful import (
    StateCase,
    StateCaseError,
    Step,
)
from ubertooth_harness.transitions import (
    MODE_RESET_SETTINGS,
    _cleanup,
    _read_originals,
    _serial,
    _stop,
)

INQUIRY_TEST_NAME = "Bluetooth inquiry scan"
FOLLOW_TEST_NAME = "Follow Classic piconet over HCI"
# The role name is the resource type: one host has one controller these tools
# can use, so there is nothing to tell apart by role.
ADAPTER_ROLE = BLUETOOTH_ADAPTER

# Reading the controller's own state costs one ioctl; the deadline is for a
# host under load, not for the work.
ADAPTER_READ_S = 15.0
# Link type libbtbb writes for a BR/EDR capture.
LINKTYPE_BREDR = capturefile.LINKTYPE_BLUETOOTH_BREDR_BB
_ESP32PEER = str(Path(esp32peer.__file__).resolve())

# What `ubertooth-scan` spends outside the window `-t` bounds, which has to be
# allowed for on top of it.
#
# `-t` arms an alarm, and the tool arms it only after the BlueZ inquiry has
# run (`main()` in ubertooth-scan.c: the `if (scan)` block comes before
# `ubertooth_set_timeout()`). So `-s` adds a whole inquiry to the run: BlueZ
# is asked for 8 units of 1.28 s, which is 10.24 s.
INQUIRY_S = 11.0
# The inquiry runs its full 8 units whatever it finds, so this is also the
# least a -s run can take on top of its window, which is what makes the floor
# below bound the sweep rather than being satisfied by the inquiry alone.
INQUIRY_MIN_S = 8 * 1.28
# And each device either half found is paged for its name, once by the inquiry
# half and once per survey result. Every one of those reads is bounded at 25 s
# (`REMOTE_NAME_TIMEOUT_MS` in ubertooth-scan.c, fork #1091): until that
# landed, the survey's read passed a zero timeout, which BlueZ takes as no
# deadline at all, and one silent device could hold the tool forever. This
# allows two such devices with margin; a bench with more falls back on the
# driver's own deadline, which fails with its message.
NAME_READS_S = 60.0
# What -x adds, which is the one allowance here that is not a worst case.
# `extra_info()` is called for every device the inquiry answered AND for
# every survey result whose UAP the sweep recovered, which the pair's own
# piconet is on any working run -- those go to the `00:00:<UAP>:<LAP>`
# address the tool assembles, so each costs a page as well. Per
# device, connects (25 s), sleeps a second, and reads the name (25 s), the
# version (20 s) and up to two feature pages (20 s each): about 113 s if a
# device fails every read. The number of devices is whatever is in range, so
# no figure bounds it. This covers two or three devices behaving normally with
# a wide margin; a run that needs more fails on the driver's deadline, with
# its message, rather than being left to the harness's own.
EXTENDED_REPORT_S = 240.0

# What `ubertooth-follow` spends before its capture starts, all of it inside
# the window the driver interrupts at, so the window has to be the capture
# plus this or the capture is shorter than the plan asked for -- and with a
# slow page there is no capture at all.
#
# It is an allowance rather than a bound. Most of it is the tool's own
# deadlines, which it cannot exceed without failing instead: BlueZ is given
# 25 s for the page (`hci_create_connection(..., 25000)`), 1 s each for the
# clock and the clock offset (`HCI_TIMEOUT_MS`), and 10 s to drop the link
# again, which `follow_release_adapter()` does immediately before the receive
# loop. The rest has no deadline at all -- opening the board, checking its
# firmware API, creating the capture files, setting the target address and
# zeroing the clock, and starting the transfer thread -- so BOARD_SETUP_S is
# a margin for it. If the setup ever outruns the whole allowance the capture
# is shorter than the plan asked for and nothing here notices, because the
# tool prints no mark for the moment its capture begins; `docs/tests.md`
# records that under this test's known limits.
PAGE_S = 25.0
HCI_READ_S = 1.0
TEARDOWN_S = 10.0
# Opening the board, checking its firmware API, creating the capture files,
# and starting the transfer thread. No deadline of their own, and outside
# both tools' own windows, so both allow for it.
BOARD_SETUP_S = 15.0
FOLLOW_SETUP_S = PAGE_S + 2 * HCI_READ_S + TEARDOWN_S + BOARD_SETUP_S
# -a reads the channel map first, up to three times with a wait of a fifth of
# a second between attempts (`FOLLOW_AFH_READ_ATTEMPTS`,
# `FOLLOW_AFH_RETRY_DELAY_US`), so three reads and two waits.
AFH_READ_S = 3 * HCI_READ_S + 2 * 0.2

# Both tools and both ESP32 bridges are Linux's: libbluetooth is BlueZ, and
# the peers are reached through /dev/serial/by-id.
_SCAN_NEEDS = Needs(platforms=("linux",), bluez_tools=("ubertooth-scan",),
                    host_tools=("hciconfig", "hcitool"))
_FOLLOW_NEEDS = Needs(platforms=("linux",), bluez_tools=("ubertooth-follow",),
                      host_tools=("hciconfig", "hcitool"))

# One device of a BlueZ inquiry, as ubertooth-scan prints it: the address the
# inquiry reported and the name read back from the device, separated by a tab
# (print_name_and_class() in ubertooth-scan.c). A device that would not answer
# the name read is printed as "[unknown]".
INQUIRY_LINE = re.compile(
    r"^(?P<address>[0-9A-F]{2}(?::[0-9A-F]{2}){5})\t(?P<name>.*)$")
# One piconet of the Ubertooth half's survey. Ordinary piconet traffic carries
# no NAP, so the two most significant bytes are printed as "??"; the UAP
# follows, as "??" until a packet's CRC settles it, and then the LAP. A
# piconet whose UAP is known is paged for its name, which is appended after a
# tab; one whose UAP is not known cannot be paged and is printed alone.
SCAN_RESULT_LINE = re.compile(
    r"^\?\?:\?\?:(?P<uap>\?\?|[0-9A-F]{2}):"
    r"(?P<lap>[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2})(?:\t(?P<name>.*))?$")
# The lines of the extended report of -x, which are indented under the device
# they belong to (extra_info() in ubertooth-scan.c). Only the three a
# reachable device always answers are checked: the address it was reached at,
# its name, and its LMP version. The features, the clock offset and the
# channel map depend on the controller.
_EXTENDED_ADDRESS = re.compile(r"^\tBD Address:  (?P<address>\S+)$")
_EXTENDED_NAME = re.compile(r"^\tDevice Name: (?P<name>.*)$")
_EXTENDED_LMP = re.compile(
    r"^\tLMP Version: .+ \(0x[0-9a-f]+\) LMP Subversion: 0x[0-9a-f]+$")

# An established link in `hcitool con`: one line per link, naming its type and
# its state. `state 1` is BlueZ's connected state; `state 2` is a page still
# in flight, which is what the scan's own name reads leave behind and what the
# kernel times out by itself. Measured on the bench: a scan left three such
# pending links, one of them to the `00:00:<UAP>:<LAP>` address it assembles
# from a survey result, and all three were gone shortly after. So only an
# established link is held against a run -- that is a peer this host is
# keeping, which is what this check is for.
ESTABLISHED_LINK = r"(?m)^\s*[<>]\s+(?:ACL|SCO|eSCO|LE)\s.*\sstate 1\s"

# ---------------------------------------------------------------------------
# The adapter, from the registry


def adapter(resource_identities: Mapping, test_name: str,
            resources: Mapping = MappingProxyType({})) -> dict:
    """The host Bluetooth adapter a case drives, from its registry identity."""
    identity = resource_identities.get(ADAPTER_ROLE)
    if not identity:
        raise StateCaseError(
            f"{test_name} requires a {BLUETOOTH_ADAPTER} on the host: the two "
            "BlueZ tools read a controller of the host's, and an Ubertooth "
            "cannot stand in for one")
    device = str(identity.get("hci device", "")).strip()
    if not device:
        raise StateCaseError(
            f"{test_name}: the {BLUETOOTH_ADAPTER} has no 'hci device' identity")
    try:
        address = esp32peer.normalize_address(identity.get("BD_ADDR", ""))
    except esp32peer.PeerError as error:
        raise StateCaseError(
            f"{test_name}: the {BLUETOOTH_ADAPTER} {error}") from error
    return {"device": device, "address": address,
            "alias": resources.get(ADAPTER_ROLE, "")}


def _distinct_address(adapter_at: dict, acceptor: dict, initiator: dict,
                      test_name: str) -> None:
    """The adapter is not one of the peers.

    Every check below tells the piconet from the adapter by address, so an
    adapter registered with a peer's address would make a case that cannot
    fail. It would also mean the registry describes one radio twice.
    """
    for peer, role in ((acceptor, ACCEPTOR_ROLE), (initiator, INITIATOR_ROLE)):
        if adapter_at["address"] == peer["address"]:
            raise StateCaseError(
                f"{test_name}: the {BLUETOOTH_ADAPTER} and the {role} are both "
                f"registered at {adapter_at['address']}")


# ---------------------------------------------------------------------------
# Reading the adapter


def _adapter_up_step(step_id: str, hci: dict) -> Step:
    """The adapter is present, up, and the one the registry names."""
    device = re.escape(hci["device"])
    address = re.escape(hci["address"])
    return Step(
        step_id,
        f"{hci['device']} is up and is the registered adapter {hci['address']}",
        ("hciconfig", hci["device"]),
        timeout_s=ADAPTER_READ_S,
        output_patterns=(
            rf"(?m)^{device}:\s",
            rf"(?m)^\s+BD Address: {address}\b",
            # `UP` alone, not `UP RUNNING`: BlueZ builds this line from the
            # flags it finds, and a controller caught part way through its
            # initialization reads `UP INIT RUNNING`, which is up.
            r"(?m)^\s+UP\b",
        ),
        # A controller that is down answers hciconfig and would otherwise
        # read as present: the tool under test then fails on its own, later,
        # with no record of why.
        forbidden_patterns=(r"(?m)^\s+DOWN\b",),
    )


def _adapter_free_step(step_id: str, name: str, hci: dict) -> Step:
    """The adapter holds no established link.

    Any address, not the peers' alone, for two reasons. The scan pages the
    address it assembles from a survey result, which is
    ``00:00:<UAP>:<LAP>`` and so is not a registered address at all -- the
    bench showed exactly that one left behind. And a link to either peer
    cannot survive the step anyway:
    ``esp32peer.py stream`` resets both boards on its way out, whatever
    happened, and a reset drops every link a board holds.

    So what this catches afterwards is a link to something else that the tool
    opened and did not close, which in practice means a device ``-x``
    reached: ``ubertooth-follow`` makes one link, to its target, and drops it
    before the capture starts, so in that test the cleanup read has nothing
    left to find and the one before the tool is the whole of it. Running it
    before is the stronger check either way: a bench whose adapter starts
    free is one whose state afterwards means something.
    """
    return Step(
        step_id, name,
        ("hcitool", "-i", hci["device"], "con"),
        timeout_s=ADAPTER_READ_S,
        output_patterns=(r"(?m)^Connections:",),
        forbidden_patterns=(ESTABLISHED_LINK,),
    )


# ---------------------------------------------------------------------------
# Output checks


def inquiry_found(address: str, name: str) -> Check:
    """The BlueZ inquiry reported this device, under this name."""
    def check(output) -> str:
        found = {}
        for line in text_lines(output):
            match = INQUIRY_LINE.match(line)
            if match:
                found[match["address"]] = match["name"]
        if address not in found:
            listed = ", ".join(sorted(found)) if found else "nothing"
            return (f"the inquiry reported {listed}, not {address}: the peer "
                    "is discoverable from boot, so an inquiry that missed it "
                    "did not work")
        if found[address] != name:
            return (f"the inquiry reported {address} as {found[address]!r}, "
                    f"and the peer firmware names it {name!r}")
        return ""
    return check


def scan_results_list(lap: str, *, with_map: bool = False) -> Check:
    """The Ubertooth half's survey reported this piconet.

    Named apart from ``classic_cases.survey_lists``, which reads
    ``ubertooth-rx -z``: that tool heads its results with a ``Survey
    Results`` line and prints no channel map, where this one has no heading
    and a map line after each result, so the two parsers are not
    interchangeable.

    With ``with_map``, the map line that must follow this piconet's result is
    read as part of it. The scan prints one map per result, so a map line
    found anywhere in the output would otherwise be another piconet's.

    The LAP is compared in the form ``-l`` takes, so the colons the tool
    prints between the three bytes are removed first.
    """
    def check(output) -> str:
        found: dict[str, bool] = {}
        current = None
        for line in text_lines(output):
            match = SCAN_RESULT_LINE.match(line)
            if match:
                current = match["lap"].replace(":", "").lower()
                found.setdefault(current, False)
                continue
            if current is not None and afhmap.MAP_LINE.match(line):
                found[current] = True
            current = None
        if lap not in found:
            listed = ", ".join(sorted(found)) if found else "nothing"
            return f"the scan results listed {listed}, not LAP {lap}"
        if with_map and not found[lap]:
            return (f"the result for LAP {lap} has no 'AFH map:' line after "
                    "it; the scan prints one per piconet it reports")
        return ""
    return check


def extended_report(address: str, name: str) -> Check:
    """``-x``'s report of this device holds its address, name and version.

    The three are checked together, inside the one device's block, and not as
    three patterns over the whole output: the scan reports every device the
    inquiry answered, each with a report of its own, so another device's
    ``LMP Version:`` line would otherwise stand in for a missing one here.

    A block is the device's own line (``<address>\t<name>``, or a survey
    result) followed by the indented lines of its report.
    """
    def check(output) -> str:
        blocks: dict[str, list[str]] = {}
        current: list[str] | None = None
        for line in text_lines(output):
            if line.startswith("\t"):
                if current is not None:
                    current.append(line)
                continue
            match = INQUIRY_LINE.match(line) or SCAN_RESULT_LINE.match(line)
            current = blocks.setdefault(line, []) if match else None
        mine = next(
            (lines for device, lines in blocks.items()
             if device.split("\t")[0] == address), None)
        if mine is None:
            return (f"no device line for {address}, so it has no extended "
                    "report either")
        reported = next(
            (found["address"] for found in map(_EXTENDED_ADDRESS.match, mine)
             if found), None)
        if reported != address:
            return (f"the extended report under {address} gives its address as "
                    f"{reported or 'nothing'}")
        found_name = next(
            (found["name"] for found in map(_EXTENDED_NAME.match, mine)
             if found), None)
        if found_name is None:
            return (f"the extended report of {address} has no 'Device Name:' "
                    f"line; the peer firmware names it {name!r}")
        if found_name != name:
            return (f"the extended report of {address} names it "
                    f"{found_name!r}, and the peer firmware names it {name!r}")
        if not any(_EXTENDED_LMP.match(line) for line in mine):
            return f"the extended report of {address} holds no LMP version"
        return ""
    return check


# ---------------------------------------------------------------------------
# The session that runs a tool while the pair streams


def _session_argv(acceptor, initiator, command, *, timeout, min_seconds,
                  interrupt_after=None, files=()):
    """esp32peer.py stream ... -- <tool> ... [<capture option> <path>]"""
    def argv(context):
        capture = []
        for option, file_name in files:
            capture += [option, _capture_path(file_name)(context)]
        base = (
            sys.executable, _ESP32PEER, "stream",
            "--acceptor", acceptor["device"],
            "--acceptor-address", acceptor["address"],
            "--acceptor-alias", acceptor["alias"],
            "--initiator", initiator["device"],
            "--initiator-address", initiator["address"],
            "--initiator-alias", initiator["alias"],
            "--period", str(STREAM_PERIOD_MS),
            "--payload", str(STREAM_PAYLOAD),
            "--min-writes", str(MIN_WRITES),
            "--min-seconds", str(min_seconds),
            "--timeout", str(timeout),
        )
        if interrupt_after is not None:
            base += ("--interrupt-after", str(interrupt_after))
        return base + ("--",) + tuple(command) + tuple(capture)
    return argv


def _tool_case(*, case_id, name, serial, hci, acceptor, initiator, command,
               seconds, needs, expected_state, check=None, patterns=(),
               returncodes=(0,), forbidden=(), interrupt=False, extra_s=0.0,
               floor_s=0.0, files=(), verify=()):
    """One tool run against the adapter while the ESP32 pair streams.

    ``seconds`` is the capture window: the tool's own ``-t`` where it has
    one, otherwise the point at which the driver interrupts it.
    ``extra_s`` is what the tool spends outside that window and which the
    deadline has to allow for as well; ``floor_s`` is the part of that which
    it spends however the run goes, which the minimum duration adds, so that
    the floor bounds the capture rather than being satisfied without one.
    """
    # The driver's deadline covers the window, what the tool spends outside
    # it, and slack for a host under load.
    timeout = seconds + extra_s + EXIT_SLACK_S
    if interrupt:
        # The driver decides when the command ends, so the window it is given
        # is the capture plus the setup that happens inside it, and the floor
        # is that whole length: a command that returned sooner stopped for a
        # reason of its own.
        interrupt_after = float(seconds) + extra_s
        min_seconds = interrupt_after - EARLY_EXIT_S
    else:
        # The tool bounds its own capture. The floor is the window plus
        # whatever the tool must spend outside it whatever it finds, so that
        # the floor bounds the capture and not only the run: for -s that is a
        # whole inquiry, which runs its 8 units however many devices answer.
        interrupt_after = None
        min_seconds = seconds + floor_s - EARLY_EXIT_S
    argv = _session_argv(acceptor, initiator, command, timeout=timeout,
                         min_seconds=min_seconds,
                         interrupt_after=interrupt_after, files=files)
    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        _adapter_up_step("adapter-up", hci),
        _adapter_free_step(
            "adapter-idle",
            f"{hci['device']} starts with no established link", hci),
        Step("pair-and-run",
             f"The pair streams while {' '.join(command)} runs, capturing for "
             f"{seconds} s inside a {seconds + extra_s:g} s window",
             argv,
             timeout_s=PAIR_SETUP_S + timeout,
             mutates=True,
             expected_returncodes=tuple(returncodes),
             output_patterns=tuple(patterns),
             forbidden_patterns=tuple(forbidden),
             output_check=check),
        _stop(serial),
        *verify,
    )
    return StateCase(
        case_id, name, steps,
        # The free check first: the two resets below drop every link a peer
        # holds, so after them a link to one could never show up. The
        # driver's own release has already reset them, so what is left to
        # catch here is a link to anything else.
        (_adapter_free_step(
            "adapter-free",
            f"{hci['device']} holds no established link", hci),
         # And is still the adapter it was. `hcitool con` lists links only for
         # a controller that is up, so without this an adapter that went down
         # during the case, or came back as another hciN, would read as free.
         _adapter_up_step("adapter-still-up", hci),
         _reset_step("reset-initiator", initiator, INITIATOR_ROLE),
         _reset_step("reset-acceptor", acceptor, ACCEPTOR_ROLE))
        + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=(expected_state + "; no established link left on "
                        f"{hci['device']}, "
                        "which is still up at its registered address; "
                        "both peers reset to their boot state; "
                        "channel, squelch, and LEDs restored"),
        capture_dir=bool(files),
        needs=needs,
    )


def _pair_report(acceptor, initiator) -> tuple[str, ...]:
    """The driver's report of the session, which pins a zero exit."""
    return _pair_patterns(acceptor, initiator)


# ---------------------------------------------------------------------------
# Bluetooth inquiry scan


def inquiry_scan_cases(arguments, resources, resource_identities):
    """Bluetooth inquiry scan: ubertooth-scan's two halves, and -x."""
    serial = _serial(resource_identities, INQUIRY_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, INQUIRY_TEST_NAME, resources)
    hci = adapter(resource_identities, INQUIRY_TEST_NAME, resources)
    _distinct_address(hci, acceptor, initiator, INQUIRY_TEST_NAME)
    seconds = seconds_arg(arguments)
    device = hci["device"]
    central = initiator["lap"]
    acceptor_name = esp32peer.device_name(acceptor["address"])
    report = _pair_report(acceptor, initiator)

    def scan(*options, **case):
        case.setdefault("patterns", report)
        # The Ubertooth half's survey results are each paged for a name, in
        # every case; -s adds a whole inquiry before the window opens, and a
        # name read per device it found.
        inquiry = INQUIRY_S if "-s" in options else 0.0
        case.setdefault("extra_s", inquiry + NAME_READS_S + BOARD_SETUP_S)
        case.setdefault("floor_s", INQUIRY_MIN_S if "-s" in options else 0.0)
        return _tool_case(
            serial=serial, hci=hci, acceptor=acceptor, initiator=initiator,
            command=("ubertooth-scan", "-U", serial, "-b", device,
                     "-t", str(seconds)) + options,
            seconds=seconds, needs=_SCAN_NEEDS, **case)

    cases = [
        # -s adds the adapter's own half: an inquiry, and the name of each
        # device it answers. The acceptor is discoverable from boot, so it is
        # what the inquiry must report. Only that is checked here, so a bench
        # whose sweep happened to hear no piconet still tests the adapter.
        # Other discoverable devices in range are reported too and are not
        # held against the run.
        scan("-s", case_id="inquiry",
             name=f"ubertooth-scan -s reports the acceptor {acceptor['address']} "
                  f"as {acceptor_name}",
             check=inquiry_found(acceptor["address"], acceptor_name),
             expected_state=f"the inquiry reported {acceptor['address']} as "
                            f"{acceptor_name}"),
        # Without -s only the Ubertooth half runs: the board sweeps the band
        # and the survey reports the piconet the pair is holding, by the
        # central's LAP, with the channel map the scan built for it.
        scan(case_id="survey",
             name=f"ubertooth-scan sweeps and reports the piconet LAP {central} "
                  "with a channel map",
             check=scan_results_list(central, with_map=True),
             expected_state=f"the scan results listed LAP {central} with the "
                            "'AFH map:' line the scan built for it"),
    ]
    if arguments["coverage"] == "comprehensive":
        # -x asks each device found for its name, version and features over a
        # connection the scan makes itself. It is comprehensive-only because
        # it is the one part of this tool that transmits to whatever the
        # inquiry answered, rather than only listening.
        #
        # A zero exit, like every other case. The tool's status is one bit
        # for several things: a part of any device's report that could not be
        # fetched, a capture stream that died, or a board that would not give
        # its mode up. Accepting 1 to allow for the first would accept the
        # other two as well -- the survey results show only that the sweep
        # decoded the piconet at some point, and a board answers a stop
        # normally after a failed stream, so no later step would report one --
        # and the lines libubertooth prints are not all pinnable, since the
        # refused-stop path reports through a libusb error that names no
        # request. So the status is kept, and the cost is stated: a device in
        # range that answers the inquiry and then refuses a read fails this
        # case for a reason that is not the bench's. That is one more reason
        # -x is comprehensive-only and wants a quiet room.
        cases.append(scan(
            "-s", "-x", case_id="extended-report",
            name=f"ubertooth-scan -s -x reports the acceptor's name and LMP "
                 f"version over a connection, and the piconet LAP {central}",
            check=_all(inquiry_found(acceptor["address"], acceptor_name),
                       extended_report(acceptor["address"], acceptor_name),
                       scan_results_list(central)),
            extra_s=(INQUIRY_S + NAME_READS_S + BOARD_SETUP_S
                     + EXTENDED_REPORT_S),
            patterns=report,
            expected_state=(f"the extended report of {acceptor['address']} held "
                            f"{acceptor_name} and an LMP version, and the scan "
                            f"results listed LAP {central}")))
    return tuple(cases)


# ---------------------------------------------------------------------------
# Follow Classic piconet over HCI


def follow_over_hci_cases(arguments, resources, resource_identities):
    """Follow Classic piconet over HCI: ubertooth-follow -t, and -a."""
    serial = _serial(resource_identities, FOLLOW_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, FOLLOW_TEST_NAME, resources)
    hci = adapter(resource_identities, FOLLOW_TEST_NAME, resources)
    _distinct_address(hci, acceptor, initiator, FOLLOW_TEST_NAME)
    seconds = seconds_arg(arguments)
    device = hci["device"]
    # The initiator pages the acceptor, so it leads the piconet and its LAP
    # is the access code on the air: it is the target to follow.
    central, central_uap = initiator["lap"], initiator["uap"]
    target = initiator["address"]
    report = _pair_report(acceptor, initiator)
    # What the tool says it is following, on standard error. The LAP and UAP
    # it prints are derived from -t, so this is what proves the address the
    # case gave it was read as the piconet the checks look for.
    targeted = (rf"(?m)^Target {re.escape(target)}: LAP={central} "
                rf"UAP={central_uap}$",)

    def follow(*options, **case):
        # Everything the tool does before it captures -- the page, the clock
        # reads, and with -a the channel map read -- happens inside the window
        # the driver interrupts at, so the window is the capture plus that.
        case.setdefault(
            "extra_s",
            FOLLOW_SETUP_S + (AFH_READ_S if "-a" in options else 0.0))
        return _tool_case(
            serial=serial, hci=hci, acceptor=acceptor, initiator=initiator,
            command=("ubertooth-follow", "-U", serial, "-b", device,
                     "-t", target) + options,
            seconds=seconds, needs=_FOLLOW_NEEDS, interrupt=True, **case)

    cases = [
        follow(case_id="follow-target",
               name=f"ubertooth-follow -t {target} follows the piconet on the "
                    "clock it read over HCI",
               patterns=report + targeted + (r"(?m)^Not use AFH$",),
               check=piconet_packets(central, MIN_PACKETS, only=True),
               expected_state=f"{MIN_PACKETS}+ packet lines, all with LAP "
                              f"{central}"),
        # -a is the only host path that follows with AFH on: it asks the
        # target, over the link the page made, for that link's channel map.
        # On this bench the answer is that there is none -- the peer reports
        # AFH off on that link -- so the tool says so, clears -a, and follows
        # without it. That is FOLLOW_AFH_ACTION_DISABLE, a documented outcome
        # with a zero exit, and it is what the bench produced in both runs
        # that reached it (2026-11-03).
        #
        # So that is what this case pins: the tool reached the read, was told
        # there was no map, said it was following without one, and still
        # reported the piconet. It fails if the read errors instead
        # (FOLLOW_AFH_ACTION_FAIL, which is an `error:` line and exit 1), if
        # the tool claims a map, or if the follow then finds nothing. What
        # stays unexercised is the branch that applies a map, because no peer
        # here offers one; docs/tests.md records that and why.
        follow("-a", case_id="follow-target-afh",
               name=f"ubertooth-follow -t {target} -a asks the link for its "
                    "channel map, is told there is none, and follows anyway",
               patterns=report + targeted + (
                   r"(?m)^the remote device is not using AFH; following "
                   r"without it$",),
               check=piconet_packets(central, MIN_PACKETS, only=True),
               forbidden=(r"(?m)^AFH map read: ", r"(?m)^AFH map: 0x",
                          r"(?m)^error: "),
               expected_state=(f"{MIN_PACKETS}+ packet lines with LAP {central} "
                               "after the tool reported the remote is not "
                               "using AFH")),
    ]
    if arguments["coverage"] == "comprehensive":
        # The file is decoded as well as counted: a BR/EDR capture holding
        # the right number of records says nothing about whose packets they
        # are, and a followed capture's records all belong to the piconet it
        # was given.
        cases.append(follow(
            case_id="follow-target-pcap",
            name=f"ubertooth-follow -t {target} -q writes the piconet's packets "
                 "to a PCAP file",
            patterns=report + targeted,
            check=piconet_packets(central, MIN_PACKETS, only=True),
            expected_state=(f"{MIN_PACKETS}+ packet lines with LAP {central}; "
                            f"-q wrote a pcap file with link type "
                            f"{LINKTYPE_BREDR} whose records all carry LAP "
                            f"{central}"),
            files=(("-q", "follow.pcap"),),
            verify=(
                _capture_file_step(
                    "verify-capture",
                    "The -q file is a BR/EDR pcap and holds the packets",
                    "follow.pcap", "pcap", MIN_PACKETS),
                # The same checks the Classic record test makes of a
                # followed capture, from the one place that spells them out:
                # -t gives this tool the UAP as well as the LAP, so the
                # reference address is knowable in full. Not `following=`,
                # which reads an option only a PCAPNG carries.
                _record_step(
                    "verify-records",
                    f"Every record in the -q file carries LAP {central}",
                    "follow.pcap", _record_checks(initiator)),
            )))
    return tuple(cases)
