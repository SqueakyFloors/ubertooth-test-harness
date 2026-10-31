"""Classic Bluetooth (BR/EDR) cases against a known piconet of two ESP32 peers.

Two ESP32 Classic peers (firmware/esp32-classic-peer, driven by esp32peer.py)
hold a Serial Port Profile link and stream a payload across it while the
Ubertooth listens. The Ubertooth only receives. That gives these cases
something a check can be written against: BR/EDR traffic from an address the
registry knows.

The initiator pages, so it is the piconet master, and every packet in the
piconet carries its LAP (ubertooth-rx(1), DISCOVERING UNDISCOVERABLE
DEVICES). The acceptor's LAP is on the air only during the page train, which
is over before a capture starts. Two things follow, and the cases check both:

- ``ubertooth-rx -z`` lists the master's LAP in its survey results, and
  ``-l <master LAP> -u <master UAP>`` reports packets for that piconet;
- the same run with the acceptor's LAP and UAP reports none.

A survey's UAP is inferred from a handful of packets and can come out wrong,
so the cases take the UAP from the registry and pass it with ``-u``; only the
LAP of a survey line is checked. Other piconets in range may be listed too
(the bench hears ambient traffic now and then), so an unfiltered run is not
asked to report this piconet alone; a ``-l`` run is, because libbtbb then
looks for that one access code.

Opening an ESP32's USB bridge resets the board, so a case cannot set the pair
up in one step and capture in the next: the capture step would reset both
boards and drop the link. Instead ``esp32peer.py stream`` holds both ports
for the whole session and runs ``ubertooth-rx`` itself, passing the tool's
output through unchanged on standard output. The tool's command line is
therefore visible in the step's argv after ``--``, and the pair's own report
(firmware versions, both BD_ADDRs, the master's LAP and UAP, writes and bytes
received) is on standard error, where the step's patterns check it and the
run record keeps it.

Every case's cleanup resets both peers, which leaves them in the firmware's
boot state, and restores the Ubertooth's channel, squelch, and LEDs.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Callable, Mapping

from ubertooth_harness import capturefile, esp32peer
from ubertooth_harness.hostneeds import Needs
from ubertooth_harness.receive_cases import (
    EARLY_EXIT_S,
    EXIT_SLACK_S,
    PACKET_LINE,
    SURVEY_LINE,
    packet_check,
    seconds_arg,
)
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

CAPTURE_TEST_NAME = "Capture known Classic device"
FOLLOW_TEST_NAME = "Follow Classic Bluetooth piconet"
ACCEPTOR_ROLE = "acceptor"
INITIATOR_ROLE = "initiator"

# What the pair streams: 200 bytes every 20 ms, about 10 kB/s, so the
# Ubertooth meets this piconet's packets on every channel it sweeps.
STREAM_PERIOD_MS = 20
STREAM_PAYLOAD = 200
# Packet lines a capture of the master's piconet must hold. The Ubertooth
# sweeps 79 channels and decodes only the access codes it happens to land on,
# so the count varies widely: eight 25 s runs on the bench gave 50 to 189
# lines of this piconet. This is a floor a working receive clears with room
# to spare, not an expected count.
MIN_PACKETS = 10
# Writes the initiator must report afterwards, so a case cannot pass on a
# link that came up and then sent nothing.
MIN_WRITES = 50
# Everything the driver does around the capture, taken from its own
# deadlines, so a slow boot or a page that goes unanswered ends in
# esp32peer.py's own message rather than in the harness calling the step
# hung: two bridges opened (each resets its board and waits for the boot
# notice), the settings written, the page and the SDP lookup, the settle,
# and then both boards reset again to release them.
PAIR_SETUP_S = (
    2 * esp32peer.BOOT_TIMEOUT_S                      # both boards boot
    + 8 * esp32peer.COMMAND_TIMEOUT_S                 # settings and statuses
    + esp32peer.CONNECT_TIMEOUT_S                     # page and SDP lookup
    + esp32peer.SETTLE_S
    + 2 * (esp32peer.BOOT_TIMEOUT_S                   # the two releases
           + 2 * esp32peer.COMMAND_TIMEOUT_S)
)
# One identify or reset: an open with its reset, then a status read.
PEER_TIMEOUT_S = esp32peer.BOOT_TIMEOUT_S + 4 * esp32peer.COMMAND_TIMEOUT_S

_ESP32PEER = str(Path(esp32peer.__file__).resolve())
_CAPTURE_CHECKER = str(Path(capturefile.__file__).resolve())
# Link type libbtbb writes for a BR/EDR capture.
LINKTYPE_BREDR = capturefile.LINKTYPE_BLUETOOTH_BREDR_BB
CHECK_TIMEOUT_S = 30.0
# The peers are reached through /dev/serial/by-id, which is Linux's.
LINUX_ONLY = Needs(platforms=("linux",))


# ---------------------------------------------------------------------------
# The pair, from the registry


def _peer(resource_identities: Mapping, role: str, test_name: str,
          resources: Mapping = MappingProxyType({})) -> dict:
    identity = resource_identities.get(role)
    if not identity:
        raise StateCaseError(
            f"{test_name} requires an {role} (ESP32 Classic peer)")
    serial = str(identity.get("bridge serial", "")).strip()
    try:
        address = esp32peer.normalize_address(identity.get("BD_ADDR", ""))
        device = identity.get("serial device") or esp32peer.default_device(serial)
    except esp32peer.PeerError as error:
        raise StateCaseError(f"{test_name}: {role} {error}") from error
    lap, uap = esp32peer.address_parts(address)
    # A registry may write the LAP and UAP down beside the address, for a
    # person reading it. A note that disagrees would send this test looking
    # for the wrong piconet, so it is refused rather than ignored.
    for key, derived in (("LAP", lap), ("UAP", uap)):
        written = identity.get(key)
        if written is not None and str(written).lower() != derived:
            raise StateCaseError(
                f"{test_name}: the {role}'s registered {key} is {written}, but "
                f"its BD_ADDR {address} gives {derived}")
    return {"serial": serial, "address": address, "device": device,
            "lap": lap, "uap": uap, "alias": resources.get(role, "")}


def _pair(resource_identities: Mapping, test_name: str,
          resources: Mapping = MappingProxyType({})) -> tuple[dict, dict]:
    acceptor = _peer(resource_identities, ACCEPTOR_ROLE, test_name, resources)
    initiator = _peer(resource_identities, INITIATOR_ROLE, test_name, resources)
    if acceptor["serial"] == initiator["serial"]:
        raise StateCaseError(
            f"{test_name} needs two ESP32 peers; the acceptor and the initiator "
            f"are both bridge {acceptor['serial']}")
    return acceptor, initiator


# ---------------------------------------------------------------------------
# Output checks


Check = Callable[[str], str]


def _all(*checks: Check) -> Check:
    def check(output: str) -> str:
        reasons = [reason for reason in (one(output) for one in checks) if reason]
        return "; ".join(reasons)
    return check


def _packet_laps(output: str) -> list[str]:
    return [match["lap"]
            for match in map(PACKET_LINE.match, output.splitlines()) if match]


def _survey_laps(output: str) -> set[str]:
    """The LAPs of the survey results, in the form -l takes."""
    lines = output.splitlines()
    if "Survey Results" not in lines:
        return set()
    found = set()
    for line in lines[lines.index("Survey Results") + 1:]:
        match = SURVEY_LINE.match(line)
        if match:
            found.add(match["lap"].replace(":", "").lower())
    return found


def piconet_packets(lap: str, minimum: int, *, only: bool) -> Check:
    """At least ``minimum`` packet lines with ``lap``.

    With ``only``, a line with another LAP is a failure: a ``-l`` run asks
    libbtbb for one access code, so nothing else should decode. An
    unfiltered run reports whatever is in range, so it is not held to that.
    """
    def check(output: str) -> str:
        laps = _packet_laps(output)
        mine = [found for found in laps if found == lap]
        problems = []
        if len(mine) < minimum:
            problems.append(f"{len(mine)} packet lines with LAP {lap}, expected "
                            f"at least {minimum}")
        others = sorted(set(laps) - {lap})
        if only and others:
            problems.append(f"packet lines with LAP {others[0]}, not {lap}")
        return "; ".join(problems)
    return check


def no_piconet_packets(lap: str) -> Check:
    """No packet line at all, for a ``-l`` run on a LAP that is not on the air.

    Exact absence is safe even though ``-e`` allows two bit errors by
    default: an access code is 64 bits, so ambient traffic matching this
    LAP's within two bits is not something a run will see.
    """
    def check(output: str) -> str:
        laps = _packet_laps(output)
        if laps:
            return (f"{len(laps)} packet lines (LAP {laps[0]}), expected none: "
                    f"{lap} is not the piconet master's LAP")
        return ""
    return check


def survey_lists(lap: str) -> Check:
    """The survey results name ``lap``."""
    def check(output: str) -> str:
        found = _survey_laps(output)
        if lap in found:
            return ""
        listed = ", ".join(sorted(found)) if found else "nothing"
        return f"the survey listed {listed}, not LAP {lap}"
    return check


def survey_omits(lap: str) -> Check:
    """The survey results do not name ``lap``."""
    def check(output: str) -> str:
        if lap in _survey_laps(output):
            return f"the survey listed LAP {lap}, which is not the master's"
        return ""
    return check


# ---------------------------------------------------------------------------
# Steps


def _peer_command(command: str, peer: Mapping, role: str) -> tuple[str, ...]:
    argv = (sys.executable, _ESP32PEER, command, "--device", peer["device"],
            "--address", peer["address"], "--role", role)
    # With the alias, esp32peer.py marks every line of its conversation with
    # the board's shell, so the run's transcript holds it (targets.shell_line).
    if peer.get("alias"):
        argv += ("--alias", peer["alias"])
    return argv


def _reset_step(step_id: str, peer: Mapping, role: str) -> Step:
    return Step(step_id, f"Reset the {role} to the firmware's boot state",
                _peer_command("reset", peer, role), timeout_s=PEER_TIMEOUT_S,
                output_patterns=(rf"(?m)^{role} role: {ACCEPTOR_ROLE} ",))


def _capture_path(name):
    def path(context):
        return str(Path(require(context, CAPTURE_DIR_KEY)) / name)
    return path


def _written_packets(minimum: int) -> Check:
    """The capture file holds packets, not just a header."""
    def check(output: str) -> str:
        match = re.search(r"(?m)^Capture format: .*, packets: (\d+)$", output)
        if match is None:
            return "the checker did not report a packet count"
        if int(match[1]) < minimum:
            return (f"the capture holds {match[1]} packets, expected at least "
                    f"{minimum}")
        return ""
    return check


def _capture_file_step(step_id, name, file_name, fmt, minimum):
    """Check the file's format, link type, and that the piconet is in it.

    `Classic Bluetooth receive` already checks what -r and -q write with no
    traffic at all; this is the same file with a known piconet on the air, so
    the packet count is the point.
    """
    return Step(
        step_id, name,
        lambda context: (sys.executable, _CAPTURE_CHECKER,
                         _capture_path(file_name)(context), "--packets"),
        timeout_s=CHECK_TIMEOUT_S,
        output_patterns=(rf"(?m)^Capture format: {fmt}, link type: "
                         rf"{LINKTYPE_BREDR}, packets: \d+$",),
        output_check=_written_packets(minimum),
    )


def _stream_argv(acceptor, initiator, serial, seconds, options, files):
    """esp32peer.py stream ... -- ubertooth-rx -U <serial> -t <seconds> ..."""
    def argv(context):
        capture = []
        for option, file_name in files:
            capture += [option, _capture_path(file_name)(context)]
        return (
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
            "--min-seconds", str(seconds - EARLY_EXIT_S),
            "--timeout", str(seconds + EXIT_SLACK_S),
            "--", "ubertooth-rx", "-U", serial, "-t", str(seconds),
        ) + tuple(options) + tuple(capture)
    return argv


def _pair_patterns(acceptor, initiator) -> tuple[str, ...]:
    """What the pair's report must say for the capture to mean anything."""
    return (
        rf"(?m)^acceptor address: {acceptor['address']}$",
        rf"(?m)^initiator address: {initiator['address']}$",
        rf"(?m)^master LAP: {initiator['lap']}$",
        rf"(?m)^master UAP: {initiator['uap']}$",
        r"(?m)^link: up to ",
        r"(?m)^command exit: 0 after \d+\.\d s$",
        r"(?m)^pair streamed: yes$",
    )


def _capture_case(serial, acceptor, initiator, seconds, *, case_id, name,
                  options, check, expected_state, files=(), verify=()):
    """One bounded ubertooth-rx run while the pair streams."""
    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step("stream-and-receive",
             f"The pair streams while ubertooth-rx -t {seconds} "
             + " ".join(tuple(options) + tuple(option for option, _f in files)),
             _stream_argv(acceptor, initiator, serial, seconds, options, files),
             timeout_s=PAIR_SETUP_S + seconds + EXIT_SLACK_S,
             # A floor on the capture's own run belongs to the driver, which
             # times the child; this step's duration is the whole session.
             mutates=True,
             output_patterns=_pair_patterns(acceptor, initiator),
             output_check=check),
        _stop(serial),
        *verify,
    )
    return StateCase(
        case_id, name, steps,
        (_reset_step("reset-initiator", initiator, INITIATOR_ROLE),
         _reset_step("reset-acceptor", acceptor, ACCEPTOR_ROLE))
        + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=(expected_state + "; both peers reset to their boot state; "
                        "channel, squelch, and LEDs restored"),
        capture_dir=bool(files),
        needs=LINUX_ONLY,
    )


# ---------------------------------------------------------------------------
# The two tests


def known_classic_cases(arguments, resources, resource_identities):
    """Capture known Classic device: a survey and a sweep find the master."""
    serial = _serial(resource_identities, CAPTURE_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, CAPTURE_TEST_NAME, resources)
    seconds = seconds_arg(arguments)
    master = initiator["lap"]

    def capture(**options):
        return _capture_case(serial, acceptor, initiator, seconds, **options)

    cases = [
        capture(case_id="survey",
                name=f"ubertooth-rx -z lists the pair's master LAP {master}",
                options=("-z",),
                check=_all(packet_check(survey=True), survey_lists(master)),
                expected_state=f"the survey results list LAP {master}"),
        capture(case_id="sweep",
                name="ubertooth-rx with no target reports the pair's packets",
                options=(),
                check=piconet_packets(master, MIN_PACKETS, only=False),
                expected_state=f"{MIN_PACKETS}+ packet lines with LAP {master}"),
    ]
    if arguments["coverage"] == "comprehensive":
        cases.append(capture(
            case_id="survey-pcapng",
            name=f"ubertooth-rx -z -r (PCAPNG) lists LAP {master} and writes a file",
            options=("-z",),
            check=_all(packet_check(survey=True), survey_lists(master)),
            expected_state=(f"the survey results list LAP {master}; -r wrote a "
                            f"pcapng file with link type {LINKTYPE_BREDR} and "
                            f"{MIN_PACKETS}+ packets in it"),
            files=(("-r", "survey.pcapng"),),
            verify=(_capture_file_step(
                "verify-capture",
                "The -r file is a BR/EDR pcapng and holds the packets",
                "survey.pcapng", "pcapng", MIN_PACKETS),)))
        # Which board is master is decided by which one pages, so binding the
        # pair the other way round moves the LAP on the air to the other board.
        cases.append(_capture_case(
            serial, initiator, acceptor, seconds, case_id="survey-roles-swapped",
            name=f"With the roles swapped the survey lists LAP {acceptor['lap']}, "
                 f"not {master}",
            options=("-z",),
            check=_all(packet_check(survey=True), survey_lists(acceptor["lap"]),
                       survey_omits(master)),
            expected_state=f"the survey results list LAP {acceptor['lap']} and "
                           f"not {master}"))
    return tuple(cases)


def follow_classic_cases(arguments, resources, resource_identities):
    """Follow Classic Bluetooth piconet: -l and -u follow the master only."""
    serial = _serial(resource_identities, FOLLOW_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, FOLLOW_TEST_NAME, resources)
    seconds = seconds_arg(arguments)
    master, master_uap = initiator["lap"], initiator["uap"]

    def capture(**options):
        return _capture_case(serial, acceptor, initiator, seconds, **options)

    cases = [
        capture(case_id="follow-lap-uap",
                name=f"ubertooth-rx -l {master} -u {master_uap} reports the "
                     "piconet's packets",
                options=("-l", master, "-u", master_uap),
                check=piconet_packets(master, MIN_PACKETS, only=True),
                expected_state=f"{MIN_PACKETS}+ packet lines, all with LAP {master}"),
        capture(case_id="follow-other-lap",
                name=f"ubertooth-rx -l {acceptor['lap']} -u {acceptor['uap']} "
                     "reports nothing: that board is not the master",
                options=("-l", acceptor["lap"], "-u", acceptor["uap"]),
                check=no_piconet_packets(acceptor["lap"]),
                expected_state=f"no packet line with LAP {acceptor['lap']}"),
    ]
    if arguments["coverage"] == "comprehensive":
        cases.append(capture(
            case_id="follow-lap-only",
            name=f"ubertooth-rx -l {master} recovers the UAP itself",
            options=("-l", master),
            check=piconet_packets(master, MIN_PACKETS, only=True),
            expected_state=f"{MIN_PACKETS}+ packet lines, all with LAP {master}"))
        cases.append(capture(
            case_id="follow-pcap",
            name=f"ubertooth-rx -l {master} -u {master_uap} -q (PCAP) writes the "
                 "piconet's packets",
            options=("-l", master, "-u", master_uap),
            check=piconet_packets(master, MIN_PACKETS, only=True),
            expected_state=(f"{MIN_PACKETS}+ packet lines with LAP {master}; -q "
                            f"wrote a pcap file with link type {LINKTYPE_BREDR} "
                            f"and {MIN_PACKETS}+ packets in it"),
            files=(("-q", "follow.pcap"),),
            verify=(_capture_file_step(
                "verify-capture",
                "The -q file is a BR/EDR pcap and holds the packets",
                "follow.pcap", "pcap", MIN_PACKETS),)))
    return tuple(cases)
