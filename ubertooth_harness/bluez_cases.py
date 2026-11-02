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
    DEFAULT_MAX_AC_ERRORS,
    INITIATOR_ROLE,
    MIN_HEC_VALID_RECORDS,
    MIN_PACKETS,
    MIN_REFERENCE_UAP_RECORDS,
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
# And each device either half found is paged for its name, once by the inquiry
# half and once per survey result. The read the survey results go through
# passes a zero timeout, which BlueZ takes as "no deadline", so a device that
# answers the page and then says nothing can hold the tool for as long as its
# controller allows (fork #1090). This is an allowance, not a bound: the
# driver's own deadline is the backstop, and a run that overruns it fails with
# the driver's message rather than hanging.
NAME_READS_S = 30.0
# What -x adds, which is the one allowance here that is not a worst case.
# `extra_info()` is called for every device the inquiry answered and, per
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
# Every figure is the tool's own deadline, so the setup cannot take longer
# than this without failing instead: the page BlueZ is given 25 s
# (`hci_create_connection(..., 25000)` in ubertooth-follow.c), and the clock
# and the clock offset 1 s each (`HCI_TIMEOUT_MS`).
PAGE_S = 25.0
HCI_READ_S = 1.0
FOLLOW_SETUP_S = PAGE_S + 2 * HCI_READ_S
# -a reads the channel map first, up to three times 1 s apart
# (`FOLLOW_AFH_READ_ATTEMPTS`, `FOLLOW_AFH_RETRY_DELAY_US`).
AFH_READ_S = 3 * (HCI_READ_S + 0.2)

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


def _adapter_free_step(step_id: str, name: str, hci: dict, peers) -> Step:
    """No link to a peer is on the adapter.

    Run before the tool, so a case does not start with a link an earlier run
    left up, and again at the start of the cleanup. At the start, because the
    two peer resets that follow drop every link a peer holds: a free check
    after them would always pass and so would say nothing.

    ``ubertooth-follow`` drops the link to its target before the capture
    starts, and ``ubertooth-scan -x`` drops each link it opened, so a link
    still up at the end is one this host is holding, and the peer stays held
    until its controller times the link out.
    """
    return Step(
        step_id, name,
        ("hcitool", "-i", hci["device"], "con"),
        timeout_s=ADAPTER_READ_S,
        output_patterns=(r"(?m)^Connections:",),
        forbidden_patterns=tuple(
            re.escape(peer["address"]) for peer in peers),
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
                    "is discoverable from boot, so a run that does not find "
                    "it found neither")
        if found[address] != name:
            return (f"the inquiry reported {address} as {found[address]!r}, "
                    f"and the peer firmware names it {name!r}")
        return ""
    return check


def scan_results_list(lap: str) -> Check:
    """The Ubertooth half's survey reported this piconet.

    Named apart from ``classic_cases.survey_lists``, which reads
    ``ubertooth-rx -z``: that tool heads its results with a ``Survey
    Results`` line and prints no channel map, where this one has no heading
    and a map line after each result, so the two parsers are not
    interchangeable.

    The LAP is compared in the form ``-l`` takes, so the colons the tool
    prints between the three bytes are removed first.
    """
    def check(output) -> str:
        found = {
            match["lap"].replace(":", "").lower()
            for match in map(SCAN_RESULT_LINE.match, text_lines(output))
            if match
        }
        if lap in found:
            return ""
        listed = ", ".join(sorted(found)) if found else "nothing"
        return f"the scan results listed {listed}, not LAP {lap}"
    return check


def maps_reported(minimum: int = 1) -> Check:
    """The scan printed a channel map for the piconets it found.

    ``ubertooth-scan`` writes one ``AFH map:`` line after each survey result,
    so beside ``scan_results_list`` this is a weak check: it catches a result
    whose map came out empty, where ``afh_map_line()`` writes a blank line
    rather than a map. It carries the ``-a`` case of
    ``Follow Classic piconet over HCI``, where the map is the point.
    """
    def check(output) -> str:
        maps = [line for line in text_lines(output) if afhmap.MAP_LINE.match(line)]
        if len(maps) < minimum:
            return (f"{len(maps)} 'AFH map:' lines, expected at least "
                    f"{minimum}: the scan prints one per piconet it reports")
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
               returncodes=(0,), interrupt=False, extra_s=0.0, files=(),
               verify=()):
    """One tool run against the adapter while the ESP32 pair streams.

    ``seconds`` is the capture window: the tool's own ``-t`` where it has
    one, otherwise the point at which the driver interrupts it.
    ``extra_s`` is what the tool spends outside that window and which the
    deadline has to allow for as well.
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
        # The tool bounds its own capture, and what it does outside that
        # window only makes the run longer, so the window is the floor.
        interrupt_after = None
        min_seconds = seconds - EARLY_EXIT_S
    argv = _session_argv(acceptor, initiator, command, timeout=timeout,
                         min_seconds=min_seconds,
                         interrupt_after=interrupt_after, files=files)
    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        _adapter_up_step("adapter-up", hci),
        _adapter_free_step(
            "adapter-idle",
            f"{hci['device']} starts with no link to either peer",
            hci, (acceptor, initiator)),
        Step("pair-and-run",
             f"The pair streams while {' '.join(command)} runs for {seconds} s",
             argv,
             timeout_s=PAIR_SETUP_S + timeout,
             mutates=True,
             expected_returncodes=tuple(returncodes),
             output_patterns=tuple(patterns),
             output_check=check),
        _stop(serial),
        *verify,
    )
    return StateCase(
        case_id, name, steps,
        # The free check first: the two resets below drop every link a peer
        # holds, so after them it could only ever pass.
        (_adapter_free_step(
            "adapter-free", f"{hci['device']} holds no link to either peer",
            hci, (acceptor, initiator)),
         _reset_step("reset-initiator", initiator, INITIATOR_ROLE),
         _reset_step("reset-acceptor", acceptor, ACCEPTOR_ROLE))
        + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=(expected_state + f"; no link left on {hci['device']}; "
                        "both peers reset to their boot state; "
                        "channel, squelch, and LEDs restored"),
        capture_dir=bool(files),
        needs=needs,
    )


def _pair_report(acceptor, initiator, *, exit_status=r"0") -> tuple[str, ...]:
    """The driver's report, with the tool's exit status the case accepts.

    ``_pair_patterns`` pins a zero exit, which is right for every case but
    ``-x``: there the tool's status also reflects the devices it reached,
    which includes devices this bench does not own, so the case takes either
    and the checks over its output are what decide it.
    """
    return tuple(
        rf"(?m)^command exit: {exit_status} after \d+\.\d s$"
        if pattern.startswith(r"(?m)^command exit:") else pattern
        for pattern in _pair_patterns(acceptor, initiator)
    )


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
        case.setdefault("extra_s", inquiry + NAME_READS_S)
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
             check=_all(scan_results_list(central), maps_reported()),
             expected_state=f"the scan results listed LAP {central} and one "
                            "'AFH map:' line for it"),
    ]
    if arguments["coverage"] == "comprehensive":
        # -x asks each device found for its name, version and features over a
        # connection the scan makes itself. It is comprehensive-only because
        # it is the one part of this tool that transmits to whatever the
        # inquiry answered, rather than only listening.
        #
        # Either exit status is accepted, because the tool exits 1 when any
        # part of any device's report could not be fetched and the devices it
        # asks include whatever else in range answered the inquiry. But it
        # also exits 1 when its own capture stream died or the board refused
        # the teardown, so this case requires both halves of the scan to have
        # worked -- the acceptor's report and the piconet in the survey
        # results -- rather than the report alone.
        cases.append(scan(
            "-s", "-x", case_id="extended-report",
            name=f"ubertooth-scan -s -x reports the acceptor's name and LMP "
                 f"version over a connection, and the piconet LAP {central}",
            check=_all(inquiry_found(acceptor["address"], acceptor_name),
                       extended_report(acceptor["address"], acceptor_name),
                       scan_results_list(central), maps_reported()),
            returncodes=(0, 1),
            extra_s=INQUIRY_S + NAME_READS_S + EXTENDED_REPORT_S,
            patterns=_pair_report(acceptor, initiator, exit_status="[01]"),
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
        # -a is the only host path that follows with AFH on: it reads the map
        # of the link the page made, over HCI, and hands it to the board. The
        # map read is of that link rather than of the pair's own piconet, so
        # the case checks that a map was read and applied, not which channels
        # it holds.
        follow("-a", case_id="follow-target-afh",
               name=f"ubertooth-follow -t {target} -a reads a channel map over "
                    "HCI and follows with AFH on",
               patterns=report + targeted + (
                   r"(?m)^AFH map read: \d+ of 79 channels in use$",),
               check=_all(piconet_packets(central, MIN_PACKETS, only=True),
                          maps_reported()),
               expected_state=(f"{MIN_PACKETS}+ packet lines with LAP {central}, "
                               "and one 'AFH map:' line read from the link's "
                               "map and handed to the board")),
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
                _record_step(
                    "verify-records",
                    f"Every record in the -q file carries LAP {central}",
                    "follow.pcap",
                    # -t gives the tool the UAP as well as the LAP, so the
                    # reference address is knowable in full, and a header
                    # only passes its HEC when it decoded at this piconet's
                    # clock and gave this UAP.
                    ("--lap", central,
                     "--reference-lap", central,
                     "--reference-uap", central_uap,
                     "--min-reference-uap", str(MIN_REFERENCE_UAP_RECORDS),
                     "--min-hec-valid", str(MIN_HEC_VALID_RECORDS),
                     "--min-packets", str(MIN_PACKETS),
                     "--max-access-code-errors", str(DEFAULT_MAX_AC_ERRORS))),
            )))
    return tuple(cases)
