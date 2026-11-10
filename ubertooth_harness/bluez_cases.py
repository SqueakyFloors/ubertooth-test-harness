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

**Measured.** Every case here has run against a real adapter, a USB
controller on a Linux bench, with the two ESP32 peers holding their link and
one Ubertooth sweeping: the inquiry reports both peers while they stream, the
page for the clock offset succeeds against the peer that is already leading
its own piconet, and the followed capture carries that piconet's packets.
What the runs settled, and what they left open, is in ``docs/tests.md`` under
each test -- including the two cases the bench argued out of existence, and
what the capture ``-q`` writes does and does not decode.
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
# allows two such devices with margin.
#
# It is the one number here that is not derived, and the bench shows why it
# needs watching: the `-s` command took 78 s against its 111 s window in one
# run and 55 s in another, the difference being how many of other people's
# devices answered the inquiry and had to be paged. Two silent neighbors
# would cost 50 s of this on their own. A busier room falls back on the
# driver's deadline, which reads like a hung tool; what tells the two apart
# is the tool's own `note: reading the name of <address> failed` lines, which
# are in the run record on standard error. docs/tests.md records that under
# this test's known limits.
NAME_READS_S = 60.0
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
# The follow cases page the target for its clock offset, which is an HCI
# Create Connection and so needs CAP_NET_RAW (hciprivilege.py, #401, which
# records the bench measurement that settled which capability). The
# scan's cases do not: its name reads are Remote Name Requests, which the
# kernel's filter on a raw HCI socket lets an unprivileged opener send, and
# the only paging path it has is `-x`, which no case passes.
_FOLLOW_NEEDS = Needs(platforms=("linux",), bluez_tools=("ubertooth-follow",),
                      host_tools=("hciconfig", "hcitool"), hci_connect=True)

# One device of a BlueZ inquiry, as ubertooth-scan prints it: the address the
# inquiry reported and the name read back from the device, separated by a tab
# (print_name_and_class() in ubertooth-scan.c). A device that would not answer
# the name read is printed as "[unknown]", the reason going to standard error.
UNREAD_NAME = "[unknown]"
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
# An established link in `hcitool con`: one line per link, naming its type and
# its state. `state 1` is BlueZ's connected state; `state 2` is a page still
# in flight, which is what the scan's own name reads leave behind and what the
# kernel times out by itself. Measured on the bench: a scan left five such
# pending links, one of them to the `00:00:<UAP>:<LAP>` address it assembles
# from a survey result, and all of them were gone shortly after. So only an
# established link is held against a run -- that is a peer this host is
# keeping, which is what this check is for.
# `\b` rather than `\s` after the number, so the pattern does not depend on
# a link mode following it: `state 11` still fails to match, and a line that
# ends at the state matches as it should.
ESTABLISHED_LINK = r"(?m)^\s*[<>]\s+(?:ACL|SCO|eSCO|LE)\s.*\sstate 1\b"


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


def inquiry_found(names: Mapping[str, str]) -> Check:
    """The BlueZ inquiry reported every one of these devices, and read back
    the name of at least one of them.

    Both halves matter, and they are asked for differently because the bench
    showed they are worth different confidence.

    Every address must be listed. The two peers are discoverable from boot,
    so an inquiry through the adapter that missed one did not do its work,
    and that part held in all 19 runs of this case on the bench.

    The names did not. A name is read over a page of that device, and two
    things happened across those 19 runs:

    - a peer's own read failed and it was printed as ``[unknown]``, in two
      runs, once for each board;
    - a neighbor's name landed on a peer's line, in one run: an inquiry that
      found two televisions besides the peers printed the acceptor's address
      against a television's name, while the television, whose own read had
      just failed with EIO, was printed as ``[unknown]``.

    The tool reads each name into a buffer of its own, one device at a time
    (``print_name_and_class()`` in ubertooth-scan.c), so the swap is below
    it, in the reply the library matched to the read -- fork
    [#1172](https://github.com/SqueakyFloors/ubertooth/issues/1172). Nothing
    the harness can check tells a swapped name from a wrongly reported one,
    and the devices whose failing reads set it off are not the bench's.

    So the check is that a name read worked, of either peer: 16 of the 19
    runs had both peers under their own names and all 19 had at least one,
    where naming a particular board would have failed three of them. One
    peer under its own name proves the adapter paged a device the harness
    owns and got its name back; both peers wrong fails.

    What is also checked, and what the "at least one" floor on its own let
    through, is a peer reported under a name that is not its own. A failed
    read prints ``[unknown]``, so a peer's line carries either its own name
    or that word; anything else is a name that came from somewhere -- which
    is what the one swapped run showed, and what fork
    [#1175](https://github.com/SqueakyFloors/ubertooth/pull/1175) fixed. That
    run passed the floor, because the other peer was under its own name. This
    rule fails it while leaving the two ``[unknown]`` runs alone, so it costs
    no reliability: of the 19 runs, it fails the one that carried the fault.

    Requiring *both* peers under their own names is a separate question and a
    maintainer's: it would have failed 3 of those 19 runs, and the two
    ``[unknown]`` ones are a page that went unanswered rather than a
    misattribution
    ([harness#384](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/384)).

    Retrying the case to hold on to the stronger check (``attempts``, as
    `Recover AFH map` uses for what the air gives it) was considered and
    rejected: a retry costs another whole inquiry and name sweep -- 78 s of
    command measured -- and what it would catch is a regression that
    misattributes exactly one of two names, where every mechanism found so
    far misattributes by device rather than by peer.
    """
    def check(output) -> str:
        found = {}
        for line in text_lines(output):
            match = INQUIRY_LINE.match(line)
            if match:
                found[match["address"]] = match["name"]
        missing = [address for address in names if address not in found]
        if missing:
            listed = ", ".join(sorted(found)) if found else "nothing"
            return (f"the inquiry reported {listed}, and not "
                    f"{', '.join(missing)}: the peers are discoverable from "
                    "boot, so an inquiry that missed one did not work")
        problems = []
        wrong = [(address, found[address]) for address, name in names.items()
                 if found[address] not in (name, UNREAD_NAME)]
        if wrong:
            problems.append(
                "; ".join(
                    f"{address} came back as {reported!r}, and the peer "
                    f"firmware names it {names[address]!r}"
                    for address, reported in wrong)
                + f": a peer's own name read either works or prints "
                  f"{UNREAD_NAME!r}, so another name on its line is one that "
                  "came from somewhere else")
        if not any(found[address] == name for address, name in names.items()):
            reported = ", ".join(f"{address} as {found[address]!r}"
                                 for address in names)
            wanted = ", ".join(f"{address} is {name!r}"
                               for address, name in names.items())
            problems.append(
                f"the inquiry reported {reported}, and the peer firmware "
                f"names them {wanted}: no peer came back under its own "
                "name, so no name read worked")
        return "; ".join(problems)
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
    """The driver's report of the session, with the zero exit every case pins."""
    return tuple(
        r"(?m)^command exit: 0 after \d+\.\d s$"
        if pattern.startswith(r"(?m)^command exit:") else pattern
        for pattern in _pair_patterns(acceptor, initiator)
    )


# ---------------------------------------------------------------------------
# Bluetooth inquiry scan


def inquiry_scan_cases(arguments, resources, resource_identities):
    """Bluetooth inquiry scan: ubertooth-scan's two halves.

    Both profiles run the same two cases. `-s -x` is not exercised, and the
    bench has narrowed what one would need. It was recorded here that `-x`
    fetches its report over a connection it makes to each device, that the
    acceptor is in the pair's link throughout so the connection does not
    happen, and that the case wanted a third Classic device. Measured again
    on 2026-11-10 with the peers on `utesp-1.6.0`, the connection does
    happen: a peer in the pair's link answers its address, name, LMP version
    and features, and fails the extended-features, clock-offset and AFH-map
    reads; an idle peer answers all of it. So no third device is wanted (#88).

    What a case still has to settle is the status. Every one of those failed
    reads prints a line these cases forbid and makes the tool exit 1, the
    bench's own peers included, and `-x` pages every piconet the sweep heard,
    so devices the bench does not own fail too (#429). docs/tests.md records
    both measurements.
    """
    serial = _serial(resource_identities, INQUIRY_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, INQUIRY_TEST_NAME, resources)
    hci = adapter(resource_identities, INQUIRY_TEST_NAME, resources)
    _distinct_address(hci, acceptor, initiator, INQUIRY_TEST_NAME)
    seconds = seconds_arg(arguments)
    device = hci["device"]
    central = initiator["lap"]
    # The firmware names a board after its own address, so the names the
    # inquiry must read back follow from the registry.
    peer_names = {
        acceptor["address"]: esp32peer.device_name(acceptor["address"]),
        initiator["address"]: esp32peer.device_name(initiator["address"]),
    }
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
        # device it answers. Both peers are discoverable from boot, so both
        # are what the inquiry must report, and one of them must come back
        # under its own name -- see inquiry_found() for why the names are
        # asked of the pair rather than of each board. Only the inquiry is
        # checked here, so a bench whose sweep happened to hear no piconet
        # still tests the adapter. Other discoverable devices in range are
        # reported too and are not held against the run.
        scan("-s", case_id="inquiry",
             name="ubertooth-scan -s reports both peers and names one of them",
             check=inquiry_found(peer_names),
             expected_state=f"the inquiry reported {acceptor['address']} and "
                            f"{initiator['address']}, and named one of them"),
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
        # stays unexercised is the branch that applies a map, because the
        # target a follow must use is in the pair's link and does not answer
        # a map read on a second one; docs/tests.md records that and why.
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
        # was given. Two of the decoder's checks are lowered for what the
        # bench measured, and only those two: see the step below.
        cases.append(follow(
            case_id="follow-target-pcap",
            name=f"ubertooth-follow -t {target} -q writes the piconet's packets "
                 "to a PCAP file",
            patterns=report + targeted,
            check=piconet_packets(central, MIN_PACKETS, only=True),
            expected_state=(f"{MIN_PACKETS}+ packet lines with LAP {central}; "
                            f"-q wrote a pcap file with link type "
                            f"{LINKTYPE_BREDR} holding {MIN_PACKETS}+ "
                            "packets"),
            files=(("-q", "follow.pcap"),),
            verify=(
                _capture_file_step(
                    "verify-capture",
                    "The -q file is a BR/EDR pcap and holds the packets",
                    "follow.pcap", "pcap", MIN_PACKETS),
                # Every record must carry this piconet's LAP, and that LAP
                # and UAP as its reference address, which is what the bench
                # measured: a -q run wrote 258 records and every one of them
                # did. Two of the decoder's checks are lowered, and only
                # those two, because the same run showed
                #
                #   none of the 258 passing a header HEC, where
                #   `ubertooth-rx -l -u` on this same pair passes a fifth to
                #   a third of them, and
                #
                #   35 of them carrying `transport 3`, ACL.
                #
                # Both observations are about the same thing: the access code
                # is found and the header does not decode, which is about the
                # clock `ubertooth-follow` hops on. That is what fork #518 is
                # open about, and it is a fault of the capture rather than of
                # a record, so the HEC floor is 0 here and the transport
                # nibble is let be. A transport is checked against the packet
                # type, and a header that did not decode leaves no type to
                # check it against.
                #
                # What those 35 records would carry today is **not
                # established**: that sweep ran against a different libbtbb
                # than the one now pinned, and at the pinned commit an
                # abandoned trial clock clears the transport and the payload
                # together. The figures are kept as measured rather than
                # re-explained. The
                # header fields of a capture whose headers do decode are
                # `Classic capture record fields`' business. docs/tests.md
                # carries the figures.
                #
                # The level floors are left out for the same reason the HEC
                # floor is: they were measured on captures of the ESP32
                # piconet, and how much a level varies across a capture of a
                # page train is **not established**.
                _record_step(
                    "verify-records",
                    "Every record of the -q file belongs to this piconet",
                    "follow.pcap",
                    _record_checks(initiator, hec_valid=0, levels=False,
                                   extra=("--any-transport",))),
            )))
    return tuple(cases)
