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
from typing import Callable, Mapping

from ubertooth_harness import esp32peer
from ubertooth_harness.hostneeds import Needs
from ubertooth_harness.receive_cases import (
    EARLY_EXIT_S,
    EXIT_SLACK_S,
    _capture_header,
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
# so the yield is low and varies: 21 to 28 packet lines per 25 s run on the
# bench. This is a floor a working receive clears, not an expected count.
MIN_PACKETS = 3
# Writes the initiator must report afterwards, so a case cannot pass on a
# link that came up and then sent nothing.
MIN_WRITES = 50
# Opening both bridges resets both boards; they boot, start Bluetooth, and
# then the initiator pages the acceptor and looks its channel up over SDP.
# The step's deadline adds this to the capture's own duration.
PAIR_SETUP_S = 60.0
PEER_TIMEOUT_S = 40.0

_ESP32PEER = str(Path(esp32peer.__file__).resolve())
# The peers are reached through /dev/serial/by-id, which is Linux's.
LINUX_ONLY = Needs(platforms=("linux",))


# ---------------------------------------------------------------------------
# The pair, from the registry


def _peer(resource_identities: Mapping, role: str, test_name: str) -> dict:
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
    return {"serial": serial, "address": address, "device": device,
            "lap": lap, "uap": uap}


def _pair(resource_identities: Mapping, test_name: str) -> tuple[dict, dict]:
    acceptor = _peer(resource_identities, ACCEPTOR_ROLE, test_name)
    initiator = _peer(resource_identities, INITIATOR_ROLE, test_name)
    if acceptor["serial"] == initiator["serial"]:
        raise StateCaseError(
            f"{test_name} needs two ESP32 peers; the acceptor and the initiator "
            f"are both bridge {acceptor['serial']}")
    return acceptor, initiator


# ---------------------------------------------------------------------------
# Output checks


# The packet line ubertooth-rx prints for every access code it decodes
# (the fork's ubertooth_callback.c, cb_br_rx), with its LAP picked out.
_PACKET_LAP = re.compile(
    r"^systime=\d+ ch=\s*\d+ LAP=(?P<lap>[0-9a-f]{6}) err=\d+ clkn=\d+ "
    r"clk_offset=\d+ s=-?\d+ n=-?\d+ snr=-?\d+$")
# A survey result line: ??:??:<UAP or ??>:<the three LAP bytes>.
_SURVEY_LAP = re.compile(
    r"^\?\?:\?\?:(?:\?\?|[0-9A-F]{2}):(?P<lap>[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2})$")

Check = Callable[[str], str]


def _all(*checks: Check) -> Check:
    def check(output: str) -> str:
        reasons = [reason for reason in (one(output) for one in checks) if reason]
        return "; ".join(reasons)
    return check


def _packet_laps(output: str) -> list[str]:
    return [match["lap"]
            for match in map(_PACKET_LAP.match, output.splitlines()) if match]


def _survey_laps(output: str) -> set[str]:
    """The LAPs of the survey results, in the form -l takes."""
    lines = output.splitlines()
    if "Survey Results" not in lines:
        return set()
    found = set()
    for line in lines[lines.index("Survey Results") + 1:]:
        match = _SURVEY_LAP.match(line)
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
    """No packet line at all, for a ``-l`` run on a LAP that is not on the air."""
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
    return (sys.executable, _ESP32PEER, command, "--device", peer["device"],
            "--address", peer["address"], "--role", role)


def _identify_step(step_id: str, peer: Mapping, role: str) -> Step:
    return Step(step_id, f"The {role} answers at its registered BD_ADDR",
                _peer_command("identify", peer, role), timeout_s=PEER_TIMEOUT_S,
                mutates=True,  # opening the bridge resets the board
                output_patterns=(
                    # The exact version goes in the run record; the driver
                    # refuses one too old to drive.
                    rf"(?m)^{role} firmware: utesp-\d+\.\d+\.\d+$",
                    rf"(?m)^{role} address: {peer['address']}$",
                    rf"(?m)^{role} LAP: {peer['lap']} UAP: {peer['uap']}$",
                ))


def _reset_step(step_id: str, peer: Mapping, role: str) -> Step:
    return Step(step_id, f"Reset the {role} to the firmware's boot state",
                _peer_command("reset", peer, role), timeout_s=PEER_TIMEOUT_S,
                output_patterns=(rf"(?m)^{role} role: {ACCEPTOR_ROLE} ",))


def _ready_case(acceptor: Mapping, initiator: Mapping) -> StateCase:
    """Both peers run usable firmware at their registered addresses."""
    return StateCase(
        "peers-ready",
        f"Both ESP32 peers run {esp32peer.MINIMUM_FIRMWARE} or later at their "
        "registered BD_ADDRs",
        (_identify_step("identify-acceptor", acceptor, ACCEPTOR_ROLE),
         _identify_step("identify-initiator", initiator, INITIATOR_ROLE)),
        (_reset_step("reset-acceptor", acceptor, ACCEPTOR_ROLE),
         _reset_step("reset-initiator", initiator, INITIATOR_ROLE)),
        expected_state=(f"both peers run {esp32peer.MINIMUM_FIRMWARE} or later, "
                        f"in the {ACCEPTOR_ROLE} role, with nothing streamed"),
        needs=LINUX_ONLY,
    )


def _capture_path(name):
    def path(context):
        return str(Path(require(context, CAPTURE_DIR_KEY)) / name)
    return path


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
            "--initiator", initiator["device"],
            "--initiator-address", initiator["address"],
            "--period", str(STREAM_PERIOD_MS),
            "--payload", str(STREAM_PAYLOAD),
            "--min-writes", str(MIN_WRITES),
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
        r"(?m)^command exit: 0$",
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
             min_duration_s=seconds - EARLY_EXIT_S,
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


def known_classic_cases(arguments, _resources, resource_identities):
    """Capture known Classic device: a survey and a sweep find the master."""
    serial = _serial(resource_identities, CAPTURE_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, CAPTURE_TEST_NAME)
    seconds = seconds_arg(arguments)
    master = initiator["lap"]

    def capture(**options):
        return _capture_case(serial, acceptor, initiator, seconds, **options)

    cases = [
        _ready_case(acceptor, initiator),
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
            expected_state=f"the survey results list LAP {master}; -r wrote a "
                           "pcapng file with link type 255",
            files=(("-r", "survey.pcapng"),),
            verify=(_capture_header("verify-capture", "Verify the -r file header",
                                    "survey.pcapng", "pcapng"),)))
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


def follow_classic_cases(arguments, _resources, resource_identities):
    """Follow Classic Bluetooth piconet: -l and -u follow the master only."""
    serial = _serial(resource_identities, FOLLOW_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, FOLLOW_TEST_NAME)
    seconds = seconds_arg(arguments)
    master, master_uap = initiator["lap"], initiator["uap"]

    def capture(**options):
        return _capture_case(serial, acceptor, initiator, seconds, **options)

    cases = [
        _ready_case(acceptor, initiator),
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
            expected_state=f"{MIN_PACKETS}+ packet lines with LAP {master}; -q "
                           "wrote a pcap file with link type 255",
            files=(("-q", "follow.pcap"),),
            verify=(_capture_header("verify-capture", "Verify the -q file header",
                                    "follow.pcap", "pcap"),)))
    return tuple(cases)
