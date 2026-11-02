"""Classic Bluetooth (BR/EDR) cases against a known piconet of two ESP32 peers.

Two ESP32 Classic peers (firmware/esp32-classic-peer, driven by esp32peer.py)
hold a Serial Port Profile link and stream a payload across it while the
Ubertooth listens. The Ubertooth only receives. That gives these cases
something a check can be written against: BR/EDR traffic from an address the
registry knows.

The initiator pages, so it is the piconet central, and every packet in the
piconet carries its LAP (ubertooth-rx(1), DISCOVERING UNDISCOVERABLE
DEVICES). The acceptor's LAP is on the air only during the page train, which
is over before a capture starts. Two things follow, and the cases check both:

- ``ubertooth-rx -z`` lists the central's LAP in its survey results, and
  ``-l <central LAP> -u <central UAP>`` reports packets for that piconet;
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
(firmware versions, both BD_ADDRs, the central's LAP and UAP, writes and bytes
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
from typing import Callable, Iterable, Mapping

from ubertooth_harness import bredrrecord, capturefile, esp32peer
from ubertooth_harness.hostneeds import Needs
from ubertooth_harness.outputfiles import text_lines
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
RECORD_TEST_NAME = "Classic capture record fields"
ACCEPTOR_ROLE = "acceptor"
INITIATOR_ROLE = "initiator"

# What the pair streams: 200 bytes every 20 ms, about 10 kB/s, so the
# Ubertooth meets this piconet's packets on every channel it sweeps.
STREAM_PERIOD_MS = 20
STREAM_PAYLOAD = 200
# Packet lines a capture of the central's piconet must hold. The Ubertooth
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
# The audio bring-up sends four more commands than the stream's and reads
# one more status, and `sco start` on the initiator pages, starts the
# hands-free profile, and opens the audio link, which the firmware allows
# much longer than an ordinary command. The session then reads each board
# twice either side of the command - a status and the controller's packet
# counters each time - and takes the link down on both before releasing
# them, which is nine more commands again.
AUDIO_SETUP_S = (PAIR_SETUP_S + 12 * esp32peer.COMMAND_TIMEOUT_S
                 + 2 * esp32peer.STOP_TIMEOUT_S
                 + esp32peer.AUDIO_CONNECT_TIMEOUT_S)
# One identify or reset: an open with its reset, then a status read.
PEER_TIMEOUT_S = esp32peer.BOOT_TIMEOUT_S + 4 * esp32peer.COMMAND_TIMEOUT_S

# The audio link's packet type the record cases ask for. An EV3 link carries
# its payload on the air at the basic rate, which is the rate the Ubertooth
# receives, so its packets decode; the 2-EV3 link the stack would choose
# carries the same payload as enhanced rate.
AUDIO_TYPE = "ev3"
# How the boards name each type back, which is the specification's spelling
# rather than the command line's. One source for it, in the driver that
# talks to the boards (firmware/esp32-classic-peer/README.md,
# `sco_pkt_type`).
AUDIO_PACKET_TYPE = esp32peer.AUDIO_TYPE_ON_LINK[AUDIO_TYPE]
# What libbtbb names an EV3 packet. The four header bits cannot tell an EV3
# from an HV3 or a 3-EV3, so this one name covers all three
# (bredrrecord.TYPE_NAMES).
AUDIO_TYPE_NAME = "HV3/EV3/3-EV3"
# Audio frames each board must report sending. The bench measured about 268
# packets a second each way on an EV3 link, so a capture window of a few
# seconds clears this with room to spare; it is here so a link that came up
# and carried nothing cannot pass.
MIN_AUDIO_FRAMES = 100
# Records of a survey that must carry a UAP the survey recovered. A survey
# is given no address: the decoder keeps a piconet per LAP it hears and
# fills the UAP in once a packet's CRC settles it, so the records before
# that carry none, and how many that is depends on how much of the piconet
# the sweep happened to hear. A 25-second survey on the bench recorded 235
# packets and 182 of them carried a recovered UAP.
MIN_SURVEY_UAP_RECORDS = 10
# Records of a followed capture that must carry the piconet's UAP as their
# reference address. Every record of the bench's three followed captures
# carried it (285, 109 and 394 of them), so this has room for a capture that
# loses the clock partway and records none for a while.
MIN_REFERENCE_UAP_RECORDS = 10
# Access-code bit errors ubertooth-rx allows when no -e says otherwise
# (ubertooth-rx(1)), and so the most any record of these captures may
# report. The bench's captures reported 0, 1 and 2.
DEFAULT_MAX_AC_ERRORS = 2
# Records whose header HEC must have passed. A header only passes when it
# decoded at the piconet's clock and its HEC gave the piconet's UAP, which
# is what makes the header and reference-UAP checks bite; how many of a
# capture's records manage it depends on what the sweep heard, so this is a
# floor rather than a share. The bench's three followed captures gave 59 of
# 285, 26 of 109, and 112 of 394; its survey, which is not held to this,
# gave 1 of 235.
MIN_HEC_VALID_RECORDS = 5

_ESP32PEER = str(Path(esp32peer.__file__).resolve())
_CAPTURE_CHECKER = str(Path(capturefile.__file__).resolve())
_RECORD_CHECKER = str(Path(bredrrecord.__file__).resolve())
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


# A Step.output_check: it is given the command's stdout lines and returns a
# failure reason, or "" when the output is acceptable.
Check = Callable[[Iterable[str]], str]


def _all(*checks: Check) -> Check:
    def check(output) -> str:
        reasons = [reason for reason in (one(output) for one in checks) if reason]
        return "; ".join(reasons)
    return check


def _packet_laps(output) -> list[str]:
    return [match["lap"]
            for match in map(PACKET_LINE.match, text_lines(output)) if match]


def _survey_laps(output) -> set[str]:
    """The LAPs of the survey results, in the form -l takes."""
    found = set()
    results = False
    for line in text_lines(output):
        if not results:
            results = line == "Survey Results"
            continue
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
    def check(output) -> str:
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
    def check(output) -> str:
        laps = _packet_laps(output)
        if laps:
            return (f"{len(laps)} packet lines (LAP {laps[0]}), expected none: "
                    f"{lap} is not the piconet central's LAP")
        return ""
    return check


def survey_lists(lap: str) -> Check:
    """The survey results name ``lap``."""
    def check(output) -> str:
        found = _survey_laps(output)
        if lap in found:
            return ""
        listed = ", ".join(sorted(found)) if found else "nothing"
        return f"the survey listed {listed}, not LAP {lap}"
    return check


def survey_omits(lap: str) -> Check:
    """The survey results do not name ``lap``."""
    def check(output) -> str:
        if lap in _survey_laps(output):
            return f"the survey listed LAP {lap}, which is not the central's"
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
    def check(output) -> str:
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


def _pair_argv(mode, acceptor, initiator, serial, seconds, options, files,
               extra):
    """esp32peer.py <mode> ... -- ubertooth-rx -U <serial> -t <seconds> ..."""
    def argv(context):
        capture = []
        for option, file_name in files:
            capture += [option, _capture_path(file_name)(context)]
        return (
            sys.executable, _ESP32PEER, mode,
            "--acceptor", acceptor["device"],
            "--acceptor-address", acceptor["address"],
            "--acceptor-alias", acceptor["alias"],
            "--initiator", initiator["device"],
            "--initiator-address", initiator["address"],
            "--initiator-alias", initiator["alias"],
            "--min-seconds", str(seconds - EARLY_EXIT_S),
            "--timeout", str(seconds + EXIT_SLACK_S),
        ) + tuple(extra) + (
            "--", "ubertooth-rx", "-U", serial, "-t", str(seconds),
        ) + tuple(options) + tuple(capture)
    return argv


def _stream_argv(acceptor, initiator, serial, seconds, options, files):
    return _pair_argv("stream", acceptor, initiator, serial, seconds, options,
                      files, ("--period", str(STREAM_PERIOD_MS),
                              "--payload", str(STREAM_PAYLOAD),
                              "--min-writes", str(MIN_WRITES)))


def _audio_argv(acceptor, initiator, serial, seconds, options, files):
    return _pair_argv("audio", acceptor, initiator, serial, seconds, options,
                      files, ("--type", AUDIO_TYPE,
                              "--min-frames", str(MIN_AUDIO_FRAMES)))


def _pair_patterns(acceptor, initiator, *, audio=False) -> tuple[str, ...]:
    """What the pair's report must say for the capture to mean anything."""
    common = (
        rf"(?m)^acceptor address: {acceptor['address']}$",
        rf"(?m)^initiator address: {initiator['address']}$",
        rf"(?m)^central LAP: {initiator['lap']}$",
        rf"(?m)^central UAP: {initiator['uap']}$",
    )
    exited = (r"(?m)^command exit: 0 after \d+\.\d s$",)
    if audio:
        # The type the link settled on, which is the one the records on the
        # air must carry: a 2-EV3 link would put its payload on the air as
        # enhanced rate, where a basic-rate capture reads a different type.
        # The boards report it with the hyphen the specification uses
        # (`2-ev3`), which is not how the command line spells it.
        settled = re.escape(AUDIO_PACKET_TYPE)
        # Each board's figures are the movement across the command's own
        # window, and `link 0 opened 0 dropped` is what says the capture
        # was of one link rather than of two with a gap in the middle
        # (#264). The controller's packet counters are the air; the frames
        # beside them are what the two applications made of it.
        measured = tuple(
            rf"(?m)^{role} audio: on {settled}, sent \d+ received \d+ frames, "
            rf"\d+ send errors, \d+ damaged, link 0 opened 0 dropped$"
            for role in ("initiator", "acceptor")
        ) + tuple(
            rf"(?m)^{role} packets: controller sent \d+ received \d+, \d+ good, "
            rf"\d+ bad, \d+ missing, \d+ lost, \d+ discarded$"
            for role in ("initiator", "acceptor")
        )
        return common + (
            rf"(?m)^audio link: esco {settled}, \d+ bytes every \d+ slots, ",
        ) + measured + exited + (rf"(?m)^pair carried audio: {settled}$",)
    return common + (r"(?m)^link: up to ",) + exited + (
        r"(?m)^pair streamed: yes$",)


def _capture_case(serial, acceptor, initiator, seconds, *, case_id, name,
                  options, check, expected_state, files=(), verify=(),
                  audio=False):
    """One bounded ubertooth-rx run while the pair streams, or carries audio."""
    argv = (_audio_argv if audio else _stream_argv)(
        acceptor, initiator, serial, seconds, options, files)
    setup_s = AUDIO_SETUP_S if audio else PAIR_SETUP_S
    doing = "carries an audio link" if audio else "streams"
    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step("stream-and-receive",
             f"The pair {doing} while ubertooth-rx -t {seconds} "
             + " ".join(tuple(options) + tuple(option for option, _f in files)),
             argv,
             timeout_s=setup_s + seconds + EXIT_SLACK_S,
             # A floor on the capture's own run belongs to the driver, which
             # times the child; this step's duration is the whole session.
             mutates=True,
             output_patterns=_pair_patterns(acceptor, initiator, audio=audio),
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
    """Capture known Classic device: a survey and a sweep find the central."""
    serial = _serial(resource_identities, CAPTURE_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, CAPTURE_TEST_NAME, resources)
    seconds = seconds_arg(arguments)
    central = initiator["lap"]

    def capture(**options):
        return _capture_case(serial, acceptor, initiator, seconds, **options)

    cases = [
        capture(case_id="survey",
                name=f"ubertooth-rx -z lists the pair's central LAP {central}",
                options=("-z",),
                check=_all(packet_check(survey=True), survey_lists(central)),
                expected_state=f"the survey results list LAP {central}"),
        capture(case_id="sweep",
                name="ubertooth-rx with no target reports the pair's packets",
                options=(),
                check=piconet_packets(central, MIN_PACKETS, only=False),
                expected_state=f"{MIN_PACKETS}+ packet lines with LAP {central}"),
    ]
    if arguments["coverage"] == "comprehensive":
        cases.append(capture(
            case_id="survey-pcapng",
            name=f"ubertooth-rx -z -r (PCAPNG) lists LAP {central} and writes a file",
            options=("-z",),
            check=_all(packet_check(survey=True), survey_lists(central)),
            expected_state=(f"the survey results list LAP {central}; -r wrote "
                            f"a pcapng file with link type {LINKTYPE_BREDR} "
                            f"and {MIN_PACKETS}+ packets in it"),
            files=(("-r", "survey.pcapng"),),
            verify=(_capture_file_step(
                "verify-capture",
                "The -r file is a BR/EDR pcapng and holds the packets",
                "survey.pcapng", "pcapng", MIN_PACKETS),)))
        # Which board is the central is decided by which one pages, so binding
        # the pair the other way round moves the LAP on the air to the other
        # board.
        cases.append(_capture_case(
            serial, initiator, acceptor, seconds, case_id="survey-roles-swapped",
            name=f"With the roles swapped the survey lists LAP {acceptor['lap']}, "
                 f"not {central}",
            options=("-z",),
            check=_all(packet_check(survey=True), survey_lists(acceptor["lap"]),
                       survey_omits(central)),
            expected_state=f"the survey results list LAP {acceptor['lap']} and "
                           f"not {central}"))
    return tuple(cases)


def follow_classic_cases(arguments, resources, resource_identities):
    """Follow Classic Bluetooth piconet: -l and -u follow the central only."""
    serial = _serial(resource_identities, FOLLOW_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, FOLLOW_TEST_NAME, resources)
    seconds = seconds_arg(arguments)
    central, central_uap = initiator["lap"], initiator["uap"]

    def capture(**options):
        return _capture_case(serial, acceptor, initiator, seconds, **options)

    cases = [
        capture(case_id="follow-lap-uap",
                name=f"ubertooth-rx -l {central} -u {central_uap} reports the "
                     "piconet's packets",
                options=("-l", central, "-u", central_uap),
                check=piconet_packets(central, MIN_PACKETS, only=True),
                expected_state=f"{MIN_PACKETS}+ packet lines, all with LAP {central}"),
        capture(case_id="follow-other-lap",
                name=f"ubertooth-rx -l {acceptor['lap']} -u {acceptor['uap']} "
                     "reports nothing: that board is not the central",
                options=("-l", acceptor["lap"], "-u", acceptor["uap"]),
                check=no_piconet_packets(acceptor["lap"]),
                expected_state=f"no packet line with LAP {acceptor['lap']}"),
    ]
    if arguments["coverage"] == "comprehensive":
        cases.append(capture(
            case_id="follow-lap-only",
            name=f"ubertooth-rx -l {central} recovers the UAP itself",
            options=("-l", central),
            check=piconet_packets(central, MIN_PACKETS, only=True),
            expected_state=f"{MIN_PACKETS}+ packet lines, all with LAP {central}"))
        cases.append(capture(
            case_id="follow-pcap",
            name=f"ubertooth-rx -l {central} -u {central_uap} -q (PCAP) writes the "
                 "piconet's packets",
            options=("-l", central, "-u", central_uap),
            check=piconet_packets(central, MIN_PACKETS, only=True),
            expected_state=(f"{MIN_PACKETS}+ packet lines with LAP {central}; -q "
                            f"wrote a pcap file with link type {LINKTYPE_BREDR} "
                            f"and {MIN_PACKETS}+ packets in it"),
            files=(("-q", "follow.pcap"),),
            verify=(_capture_file_step(
                "verify-capture",
                "The -q file is a BR/EDR pcap and holds the packets",
                "follow.pcap", "pcap", MIN_PACKETS),)))
    return tuple(cases)


# ---------------------------------------------------------------------------
# Classic capture record fields


def _record_step(step_id, name, file_name, checks) -> Step:
    """Run the record decoder over one capture file, with these checks."""
    return Step(
        step_id, name,
        lambda context: (sys.executable, _RECORD_CHECKER,
                         _capture_path(file_name)(context)) + tuple(checks),
        timeout_s=CHECK_TIMEOUT_S,
        output_patterns=(rf"(?m)^BR/EDR records: \d+ in {re.escape(file_name)}$",),
        # The decoder exits non-zero for any of these, so this only puts the
        # reason in the step's own failure rather than in its output alone.
        forbidden_patterns=(r"BR/EDR record problem:",),
    )


def _record_checks(central=None, *, minimum=MIN_PACKETS, following=False,
                   extra=()) -> tuple[str, ...]:  # noqa: C901
    """What the records of a capture must hold.

    With a ``central``, this is a capture that was given one piconet to
    follow, so every record belongs to it and carries it as the reference
    address. Without one, the capture is a survey: it records whatever is in
    range, and the caller says what to look for instead.
    """
    if following and central is None:
        raise StateCaseError("the address a capture was following is the "
                             "central's, so a check of it needs the central")
    # No case passes -e, so the tool allows its default of two bit errors in
    # an access code, and no record may report more.
    checks = ("--min-packets", str(minimum),
              "--max-access-code-errors", str(DEFAULT_MAX_AC_ERRORS))
    if central is not None:
        # A record whose header carries another packet's bits, or none,
        # cannot pass its HEC, so this is the floor that makes the header
        # checks bite. Only a followed capture gets it: the decoder reads a
        # header when it is working on one piconet's clock, and a survey,
        # which is working on every piconet it hears, decoded one header in
        # 235 records on the bench.
        checks += ("--min-hec-valid", str(MIN_HEC_VALID_RECORDS),
                   "--lap", central["lap"],
                   "--reference-lap", central["lap"],
                   # Every record that carries a reference UAP must carry
                   # this one, and this many must carry it: a capture that
                   # gives a clock up has no UAP to record until it derives
                   # one again, which is an honest record, not a wrong one.
                   "--reference-uap", central["uap"],
                   "--min-reference-uap", str(MIN_REFERENCE_UAP_RECORDS))
    if following:
        # ubertooth-rx -l records the address it was told to follow once, in
        # a PCAPNG interface option. It knows the LAP and, with -u, the UAP,
        # and says in the option's own mask that the whole UAP was known;
        # the peer's non-significant address part it never learns, so the
        # option carries a zero NAP marked invalid.
        checks += ("--following-lap", central["lap"],
                   "--following-uap", central["uap"],
                   "--following-uap-mask", "ff")
    return checks + tuple(extra)


def record_fields_cases(arguments, resources, resource_identities):
    """Classic capture record fields: decode what a BR/EDR capture holds."""
    serial = _serial(resource_identities, RECORD_TEST_NAME)
    acceptor, initiator = _pair(resource_identities, RECORD_TEST_NAME, resources)
    seconds = seconds_arg(arguments)
    central, central_uap = initiator["lap"], initiator["uap"]
    follow = ("-l", central, "-u", central_uap)

    def capture(**options):
        return _capture_case(serial, acceptor, initiator, seconds, **options)

    cases = [
        _capture_case(
            serial, acceptor, initiator, seconds, case_id="follow-fields",
            name=f"Every record of a -l {central} -u {central_uap} capture "
                 "holds this piconet's fields, in both formats",
            options=follow,
            check=piconet_packets(central, MIN_PACKETS, only=True),
            expected_state=(
                f"the pcap and the pcapng each hold {MIN_PACKETS}+ records of "
                f"LAP {central} with reference LAP {central} and reference UAP "
                f"{central_uap}, each record's payload length, HEC, and CRC "
                f"agreeing with its own header; the pcapng names "
                f"{initiator['address']} as the address followed"),
            files=(("-q", "records.pcap"), ("-r", "records.pcapng")),
            verify=(
                _record_step("verify-pcap",
                             "The PCAP's records hold this piconet's fields",
                             "records.pcap", _record_checks(initiator)),
                _record_step("verify-pcapng",
                             "The PCAPNG's records hold this piconet's fields, "
                             "and it names the address followed",
                             "records.pcapng",
                             _record_checks(initiator,
                                            following=True)),
            )),
        # Which board is the central is decided by which one pages, so the
        # LAP and the reference address in the records follow the roles
        # rather than being whatever the decoder had lying around.
        _capture_case(
            serial, initiator, acceptor, seconds, case_id="swapped-fields",
            name=f"With the roles swapped the records hold LAP "
                 f"{acceptor['lap']}, not {central}",
            options=("-l", acceptor["lap"], "-u", acceptor["uap"]),
            check=piconet_packets(acceptor["lap"], MIN_PACKETS, only=True),
            expected_state=(f"the pcapng holds {MIN_PACKETS}+ records, all of "
                            f"LAP {acceptor['lap']} and none of {central}"),
            files=(("-r", "swapped.pcapng"),),
            verify=(_record_step(
                "verify-pcapng",
                f"The records hold LAP {acceptor['lap']}",
                "swapped.pcapng",
                _record_checks(acceptor, following=True)),)),
    ]
    if arguments["coverage"] == "comprehensive":
        cases.append(capture(
            case_id="audio-fields",
            name=f"An {AUDIO_TYPE.upper()} audio link's records are named "
                 f"{AUDIO_TYPE_NAME} and hold this piconet's fields",
            options=follow,
            # A packet line is printed for every access code that matched,
            # before the decoder is asked for a clock, so an audio capture
            # prints them like any other; what it does not do is enter
            # follow mode, which none of these checks needs.
            check=piconet_packets(central, MIN_PACKETS, only=True),
            expected_state=(f"the pcapng holds {MIN_PACKETS}+ records of LAP "
                            f"{central}, at least one named {AUDIO_TYPE_NAME}, "
                            f"each record's fields agreeing with its own header"),
            files=(("-r", "audio.pcapng"),),
            audio=True,
            verify=(
                _record_step("verify-pcapng",
                             f"The records name {AUDIO_TYPE_NAME} packets",
                             "audio.pcapng",
                             _record_checks(
                                 initiator, following=True,
                                 extra=("--require-types", AUDIO_TYPE_NAME))),
            )))
        cases.append(capture(
            case_id="survey-fields",
            name="A survey's records carry the UAP it recovered as it went",
            options=("-z",),
            check=_all(packet_check(survey=True), survey_lists(central)),
            expected_state=(
                f"the pcapng holds {MIN_PACKETS}+ records, at least "
                f"{MIN_SURVEY_UAP_RECORDS} of them carrying a UAP the survey "
                "recovered, and every record's fields agreeing with its own "
                "header"),
            files=(("-r", "survey.pcapng"),),
            verify=(
                # A survey is given no piconet: it records whatever is in
                # range, under no reference LAP, and fills in each piconet's
                # UAP once it has settled it, so the records before that
                # carry none and the LAP is not this pair's alone.
                _record_step("verify-pcapng",
                             "The survey's records carry the UAP it recovered",
                             "survey.pcapng",
                             _record_checks(
                                 extra=("--min-reference-uap",
                                        str(MIN_SURVEY_UAP_RECORDS)))),
            )))
    return tuple(cases)
