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
    _pair,
    _pair_patterns,
    _reset_step,
    piconet_packets,
)
from ubertooth_harness.hostneeds import Needs
from ubertooth_harness.outputfiles import text_lines
from ubertooth_harness.receive_cases import EARLY_EXIT_S, EXIT_SLACK_S, seconds_arg
from ubertooth_harness.resources import BLUETOOTH_ADAPTER
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    StateCase,
    StateCaseError,
    Step,
    require,
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
CHECK_TIMEOUT_S = 30.0
# Link type libbtbb writes for a BR/EDR capture.
LINKTYPE_BREDR = capturefile.LINKTYPE_BLUETOOTH_BREDR_BB
_ESP32PEER = str(Path(esp32peer.__file__).resolve())
_CAPTURE_CHECKER = str(Path(capturefile.__file__).resolve())

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
# The extended report of -x, which is indented under the device it belongs to
# (extra_info() in ubertooth-scan.c). Only the three lines a reachable device
# always answers are checked: the address it was reached at, its name, and
# its LMP version. The features, the clock offset and the channel map depend
# on the controller.
EXTENDED_ADDRESS = r"(?m)^\tBD Address:  {address}$"
EXTENDED_NAME = r"(?m)^\tDevice Name: {name}$"
EXTENDED_LMP = r"(?m)^\tLMP Version: .+ \(0x[0-9a-f]+\) LMP Subversion: 0x[0-9a-f]+$"


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


def pages_from_lap_uap(address: str) -> bool:
    """Whether -l and -u alone can page the device at ``address``.

    ``ubertooth-follow`` given ``-l`` and ``-u`` has four of the six bytes
    and sends the page to ``00:00:<UAP>:<LAP>``. A page carries only the LAP
    and the UAP (Bluetooth Core, Vol 2, Part B 1.3, 6.4.6), so the two bytes
    it guessed do not decide whether the page arrives -- but BlueZ is asked
    to connect to the whole guessed address, and the fork's own message says
    to give ``-t`` instead
    ([fork #316](https://github.com/SqueakyFloors/ubertooth/issues/316)). A
    device whose address really begins 00:00 is the one case where the guess
    is right, and a case that expects the page to be refused must not be
    built for it.
    """
    return esp32peer.normalize_address(address).startswith("00:00:")


# ---------------------------------------------------------------------------
# Reading the adapter


def _adapter_up_step(step_id: str, adapter_at: dict) -> Step:
    """The adapter is present, up, and the one the registry names."""
    device = re.escape(adapter_at["device"])
    address = re.escape(adapter_at["address"])
    return Step(
        step_id,
        f"{adapter_at['device']} is up and is the registered adapter "
        f"{adapter_at['address']}",
        ("hciconfig", adapter_at["device"]),
        timeout_s=ADAPTER_READ_S,
        output_patterns=(
            rf"(?m)^{device}:\s",
            rf"(?m)^\s+BD Address: {address}\b",
            r"(?m)^\s+UP RUNNING\b",
        ),
        # A controller that is down answers hciconfig and would otherwise
        # read as present: the tool under test then fails on its own, later,
        # with no record of why.
        forbidden_patterns=(r"(?m)^\s+DOWN\b",),
    )


def _adapter_free_step(step_id: str, adapter_at: dict, peers) -> Step:
    """No link to a peer is left on the adapter.

    ``ubertooth-follow`` pages its target and drops the link before the
    capture starts, and ``ubertooth-scan -x`` drops each link it opened. A
    link still up afterwards is a peer held by this host until its controller
    times the link out, which is the next case's problem, so it is found here
    where it was made.
    """
    return Step(
        step_id, f"{adapter_at['device']} holds no link to either peer",
        ("hcitool", "-i", adapter_at["device"], "con"),
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


def survey_lists(lap: str) -> Check:
    """The Ubertooth half's survey reported this piconet.

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

    One ``AFH map:`` line follows each survey result, so the floor is the
    number of piconets a case asks to have been found.
    """
    def check(output) -> str:
        maps = [line for line in text_lines(output) if afhmap.MAP_LINE.match(line)]
        if len(maps) < minimum:
            return (f"{len(maps)} 'AFH map:' lines, expected at least "
                    f"{minimum}: the scan prints one per piconet it reports")
        return ""
    return check


def _written_packets(minimum: int) -> Check:
    """The capture file holds packets, not just a header."""
    def check(output) -> str:
        match = re.search(r"(?m)^Capture format: .*, packets: (\d+)$", output)
        if match is None:
            return "the checker did not report a packet count"
        if int(match[1]) < minimum:
            return (f"the capture holds {match[1]} packets, expected at least "
                    f"{minimum}")
        return ""
    return check


# ---------------------------------------------------------------------------
# The session that runs a tool while the pair streams


def _capture_path(name):
    def path(context):
        return str(Path(require(context, CAPTURE_DIR_KEY)) / name)
    return path


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


def _capture_file_step(step_id, name, file_name, fmt, minimum):
    return Step(
        step_id, name,
        lambda context: (sys.executable, _CAPTURE_CHECKER,
                         _capture_path(file_name)(context), "--packets"),
        timeout_s=CHECK_TIMEOUT_S,
        output_patterns=(rf"(?m)^Capture format: {fmt}, link type: "
                         rf"{LINKTYPE_BREDR}, packets: \d+$",),
        output_check=_written_packets(minimum),
    )


def _tool_case(*, case_id, name, serial, hci, acceptor, initiator, command,
               seconds, needs, expected_state, check=None, patterns=(),
               returncodes=(0,), interrupt=False, files=(), verify=()):
    """One tool run against the adapter while the ESP32 pair streams."""
    # A tool that bounds its own capture is given its window plus slack; one
    # that runs until it is signalled is stopped by the driver at the window,
    # and either way it may not exit before the window is nearly up.
    timeout = seconds + EXIT_SLACK_S
    interrupt_after = float(seconds) if interrupt else None
    min_seconds = seconds - EARLY_EXIT_S
    argv = _session_argv(acceptor, initiator, command, timeout=timeout,
                         min_seconds=min_seconds,
                         interrupt_after=interrupt_after, files=files)
    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        _adapter_up_step("adapter-up", hci),
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
        (_reset_step("reset-initiator", initiator, INITIATOR_ROLE),
         _reset_step("reset-acceptor", acceptor, ACCEPTOR_ROLE),
         _adapter_free_step("adapter-free", hci, (acceptor, initiator)))
        + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=(expected_state + "; both peers reset to their boot "
                        f"state; no link left on {hci['device']}; "
                        "channel, squelch, and LEDs restored"),
        capture_dir=bool(files),
        needs=needs,
    )


def _pair_report(acceptor, initiator, *, exit_status=0) -> tuple[str, ...]:
    """The driver's report, with the tool's exit status the case expects.

    ``_pair_patterns`` pins a zero exit, which is right for every case but
    the one that asks the tool to refuse: ``stream`` reports the command's
    status and exits with it, so there the line says 1 and the pair's own
    report still has to be whole.
    """
    patterns = _pair_patterns(acceptor, initiator)
    if exit_status == 0:
        return patterns
    return tuple(
        rf"(?m)^command exit: {exit_status} after \d+\.\d s$"
        if pattern.startswith(r"(?m)^command exit:") else pattern
        for pattern in patterns
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
        return _tool_case(
            serial=serial, hci=hci, acceptor=acceptor, initiator=initiator,
            command=("ubertooth-scan", "-U", serial, "-b", device,
                     "-t", str(seconds)) + options,
            seconds=seconds, needs=_SCAN_NEEDS, **case)

    cases = [
        # -s is the adapter's own half: an inquiry, and the name of each
        # device it answers. The acceptor is discoverable from boot, so it is
        # what the inquiry must report. Other discoverable devices in range
        # are reported too and are not held against the run.
        scan("-s", case_id="inquiry",
             name=f"ubertooth-scan -s reports the acceptor {acceptor['address']} "
                  f"as {acceptor_name}",
             check=inquiry_found(acceptor["address"], acceptor_name),
             expected_state=f"the inquiry reported {acceptor['address']} as "
                            f"{acceptor_name}"),
        # The Ubertooth half, with no -s: the board sweeps the band and the
        # survey reports the piconet the pair is holding, by the central's
        # LAP, with the channel map the scan built for it.
        scan(case_id="survey",
             name=f"ubertooth-scan sweeps and reports the piconet LAP {central} "
                  "with a channel map",
             check=_all(survey_lists(central), maps_reported()),
             expected_state=f"the scan results listed LAP {central} and one "
                            "'AFH map:' line for it"),
    ]
    if arguments["coverage"] == "comprehensive":
        # Both halves in one run, which is the usage the tool's own help
        # leads with.
        cases.append(scan(
            "-s", case_id="inquiry-and-survey",
            name=f"ubertooth-scan -s reports the acceptor and the piconet LAP "
                 f"{central}",
            check=_all(inquiry_found(acceptor["address"], acceptor_name),
                       survey_lists(central), maps_reported()),
            expected_state=(f"the inquiry reported {acceptor['address']} and "
                            f"the scan results listed LAP {central}")))
        # -x asks each device found for its name, version and features over a
        # connection the scan makes itself. It is comprehensive-only because
        # it is the one part of this tool that transmits to whatever the
        # inquiry answered, rather than only listening.
        cases.append(scan(
            "-s", "-x", case_id="extended-report",
            name=f"ubertooth-scan -s -x reports the acceptor's name and LMP "
                 "version over a connection",
            check=inquiry_found(acceptor["address"], acceptor_name),
            patterns=report + (
                EXTENDED_ADDRESS.format(address=re.escape(acceptor["address"])),
                EXTENDED_NAME.format(name=re.escape(acceptor_name)),
                EXTENDED_LMP,
            ),
            expected_state=(f"the extended report held {acceptor['address']}, "
                            f"{acceptor_name}, and an LMP version")))
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
                               "and one 'AFH map:' line from the link's map")),
    ]
    if arguments["coverage"] == "comprehensive":
        cases.append(follow(
            case_id="follow-target-pcap",
            name=f"ubertooth-follow -t {target} -q writes the piconet's packets "
                 "to a PCAP file",
            patterns=report + targeted,
            check=piconet_packets(central, MIN_PACKETS, only=True),
            expected_state=(f"{MIN_PACKETS}+ packet lines with LAP {central}; "
                            f"-q wrote a pcap file with link type "
                            f"{LINKTYPE_BREDR} and {MIN_PACKETS}+ packets"),
            files=(("-q", "follow.pcap"),),
            verify=(_capture_file_step(
                "verify-capture",
                "The -q file is a BR/EDR pcap and holds the packets",
                "follow.pcap", "pcap", MIN_PACKETS),)))
        if not pages_from_lap_uap(target):
            # -l and -u leave the tool guessing the two most significant
            # bytes as 00:00 and asking BlueZ to connect there. This peer's
            # address has a NAP, so the page is refused and the tool says to
            # give the whole address with -t. The capture never starts, so
            # the pair's report is all there is to check (fork #316).
            cases.append(_tool_case(
                case_id="follow-lap-uap-needs-the-whole-address",
                name=f"ubertooth-follow -l {central} -u {central_uap} is refused: "
                     "the page needs the whole target address",
                serial=serial, hci=hci, acceptor=acceptor, initiator=initiator,
                command=("ubertooth-follow", "-U", serial, "-b", device,
                         "-l", central, "-u", central_uap),
                seconds=seconds, needs=_FOLLOW_NEEDS,
                returncodes=(1,),
                patterns=_pair_report(acceptor, initiator, exit_status=1) + (
                    r"(?m)^error: connecting to 00:00:"
                    + re.escape(central_uap.upper())
                    + r":[0-9A-F:]+ failed.*-t xx:xx:xx:xx:xx:xx$",),
                expected_state=("the tool refused the page and named -t; no "
                                "capture ran")))
    return tuple(cases)
