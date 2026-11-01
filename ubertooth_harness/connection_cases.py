"""Two nRF52840 DKs connect and reconnect while the Ubertooth follows them.

The DKs run the harness's role firmware (firmware/nrf52840dk), one as the
central and one as the peripheral, driven by nrfdk.py; the capture is checked
by conncapture.py. The Ubertooth only receives.

The central drops and remakes the link on a schedule (``conn cycle``), so a
capture started at any moment holds a whole connection: the CONNECT_IND, the
data length exchange, the connection parameter update the central asks for,
and the data. That is what makes this testable with one command at a time:
nothing has to be timed against the capture.

Each capture case runs ``ubertooth-btle -f`` with a capture file while the
pair runs, then checks:

- exactly the connection parameters the central was told to ask for in the
  CONNECT_IND (interval, latency, supervision timeout), a legal hop
  increment, and every data channel in the channel map;
- packets on the connection's access address, with correct CRCs, including
  packets longer than the 27 bytes a link without Data Length Extension
  would be capped at;
- the DKs' own view afterwards: connections made, parameter updates
  accepted, the negotiated data length and PHY.

How much of a followed connection arrives intact varies from capture to
capture, so a capture case that misses it takes another capture, up to
CAPTURE_ATTEMPTS in all, and the run records which attempt passed.

One case checks the pcapng connection-information option libbtbb records
for a CONNECT_IND: the parameters a reader takes from the file to follow the
connection in it. Until SqueakyFloors/libbtbb#21 was fixed they were copied
6 bytes early, so every one of them belonged to another field.

`Follow a BLE connection across its end` takes the window those cases avoid:
one capture spanning several of the central's reconnections. It asks what a
capture of one connection cannot: that the follow ends when the connection
does, that the next connection in the file is one the file also announced,
and that the board is idle and answering as soon as the capture stops.

Every case's cleanup leaves both DKs idle in the advertiser role with their
boot settings, and restores the Ubertooth's channel, squelch, and LEDs.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Mapping

from ubertooth_harness import conncapture, nrfdk, watchdog
from ubertooth_harness.advertiser_cases import (
    DK_TIMEOUT_S,
    ENSURE_TIMEOUT_S,
    FIRMWARE_LABEL,
    _dk,
    _dk_idle,
)
from ubertooth_harness.outputfiles import search_lines
from ubertooth_harness.patterns import serial_pattern
from ubertooth_harness.receive_cases import seconds_arg
from ubertooth_harness.stateful import (
    ATTEMPT_KEY,
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    StateCaseError,
    Step,
    artifact_key,
    require,
)
from ubertooth_harness.transitions import (
    CHANNEL,
    MODE_RESET_SETTINGS,
    _btle,
    _cleanup,
    _read_originals,
    _serial,
    _stop,
    _util,
)

TEST_NAME = "Capture known BLE connection"
CENTRAL_ROLE = "central"
PERIPHERAL_ROLE = "peripheral"
# How long each connection lasts before the central makes a new one.
#
# The capture window has to span exactly one of those boundaries: one, so it
# holds a CONNECT_IND and the connection that follows it; not two, because a
# window spanning several reconnections has been seen to die partway through
# with `error: the Ubertooth did not answer in time` and exit 1
# (SqueakyFloors/ubertooth#505; whether the end of the followed connection
# is what triggers it is not established). With the central started just
# before the capture, that means CYCLE_MS < duration < 2 * CYCLE_MS;
# `check_arguments` enforces it. One window gives the receiver one chance
# at the CONNECT_IND, which is why a failed capture is taken again
# (CAPTURE_ATTEMPTS).
CYCLE_MS = 5000
# Bytes per write and how often: 244 bytes of ATT payload fill a 251-byte
# link-layer packet, which only Data Length Extension allows.
WRITE_BYTES = 244
WRITE_PERIOD_MS = 50
# The advertising channel the peripheral uses and the Ubertooth listens on.
# The central connects on the channel it hears the peripheral on, so pinning
# both to one channel is what puts the CONNECT_IND in the capture.
ADV_CHANNEL = 37
# Without Data Length Extension the payload stops at 27 bytes, so a packet
# longer than that proves the extension took effect on air.
LONG_PACKET_BYTES = 28
# What a capture must hold. The Ubertooth catches part of a connection and
# only a fraction of what it catches passes the CRC: of the 18 packets of one
# followed connection on the bench (run 20261028T163546Z) two were intact,
# one of them a full 251 bytes. These are floors a working follow clears, not
# expected counts; the central writes every WRITE_PERIOD_MS to give the
# receiver enough chances.
MIN_DATA_PACKETS = 5
# ...of which at least this many must decode with a correct CRC.
MIN_VALID_PACKETS = 1
MIN_LONG_PACKETS = 1
# How many times a capture is taken before the case fails (#149). One
# capture window holds one of the central's reconnections, and how much of
# that connection the Ubertooth catches intact varies: three runs on the
# bench on 2026-10-30 cleared the floors above twice and missed them once,
# with 13 to 17 packets on the access address each time and none of them
# intact in two of the three. The pair keeps making and remaking the link,
# so another try costs only another capture window, and each try writes its
# own capture file.
CAPTURE_ATTEMPTS = 3
CHECK_TIMEOUT_S = 30.0
# Settling and status reads after the capture.
STATUS_TIMEOUT_S = 30.0
# `-w` is two control reads and a board that has gone costs a libusb timeout
# or two before it says so.
WATCHDOG_TIMEOUT_S = 15.0

# ---------------------------------------------------------------------------
# Follow a BLE connection across its end

FOLLOW_END_TEST_NAME = "Follow a BLE connection across its end"
# How many of the central's connections the capture window must span. Three,
# so the window holds the end of one connection, a whole one and the start of
# another: exactly what the single-connection cases above leave out.
MIN_CYCLES = 3
# How many of those connections the capture itself must hold a CONNECT_IND
# for. The Ubertooth hunts on one advertising channel and catches part of
# what is there, so it is not expected to see every reconnection; two is what
# shows a follow that ended and then found the next connection.
MIN_CONNECTIONS = 2
# Packets each of those connections must carry on its own access address.
# A floor, like MIN_DATA_PACKETS: it says the capture followed each
# connection rather than only hearing it start.
MIN_CONNECTION_PACKETS = 3
# How long after a CONNECT_IND a packet has to arrive for the central's
# parameter update to have taken effect. The central asks 1200 ms into each
# connection (the firmware's AUTOUPDATE_DELAY) and the update's instant is a
# few connection events after that, so 2 s is past it either way. A packet
# that late with a correct CRC is a follow that survived the update.
AFTER_UPDATE_MS = 2000
# How long before the end of the window the capture's last good packet may
# arrive. The last connection in a capture cannot be asked for a packet past
# the update, because the window is what ends it, so something has to say
# that the window is what ended the capture's traffic too, and not a follow
# that died. One of the central's cycles is generous: during a connection the
# central writes every WRITE_PERIOD_MS, and a reconnection costs a fraction
# of a second.
MAX_QUIET_MS = CYCLE_MS
# What the board must read right after the capture: back in idle mode, so
# nothing of the follow is left running.
IDLE_AFTER_CAPTURE = r"(?m)^Now in idle mode\b"
WATCHDOG_RESETS = "watchdog resets"


def _resets_observation(key: str, expected=None) -> Observation:
    """The board's unrequested self-reset count, from `ubertooth-util -w`.

    The pattern is watchdog.py's own, so the run-level monitor and this case
    read the same line through one regex.
    """
    return Observation(key, watchdog.RESETS.pattern, expected,
                       convert=lambda match: match["resets"])

_CONNCAPTURE = str(Path(conncapture.__file__).resolve())


def _attempt_capture_path(name: str):
    """The capture file of the attempt that is running.

    Every attempt keeps its own file (``capture-1.pcapng``,
    ``capture-2.pcapng``), so the run directory holds the capture each
    try checked, not only the last one.
    """
    stem, _, extension = name.rpartition(".")

    def path(context):
        attempt = context.get(ATTEMPT_KEY, "1")
        directory = Path(require(context, CAPTURE_DIR_KEY))
        return str(directory / f"{stem}-{attempt}.{extension}")

    return path


def _pair(resources: Mapping, resource_identities: Mapping,
          test_name: str = TEST_NAME) -> tuple[dict, dict]:
    """The central and the peripheral, from the registry's identities."""
    pair = []
    for role in (CENTRAL_ROLE, PERIPHERAL_ROLE):
        identity = resource_identities.get(role)
        if not identity:
            raise StateCaseError(f"{test_name} requires a {role} (nRF52840 DK)")
        try:
            serial = nrfdk.jlink_serial(identity.get("jlink serial", ""))
            address = nrfdk.normalize_address(identity.get("BLE address", ""))
        except nrfdk.NrfDkError as error:
            raise StateCaseError(f"{test_name}: {role} {error}") from error
        if not nrfdk.is_static_random(address):
            raise StateCaseError(
                f"{test_name}: {role} BLE address {address} is not a static random "
                "address (the two top bits must be set)")
        pair.append({"serial": str(serial), "address": address,
                     "port": identity.get("serial device") or None,
                     "alias": resources.get(role, "")})
    central, peripheral = pair
    if central["serial"] == peripheral["serial"]:
        raise StateCaseError(
            f"{test_name} needs two DKs; the central and the peripheral are both "
            f"J-Link {central['serial']}")
    return central, peripheral


def _ensure_step(step_id: str, dk: Mapping, role: str) -> Step:
    def argv(context):
        options = ["--address", dk["address"], "--role", role]
        image = context.get(artifact_key(FIRMWARE_LABEL))
        if image:
            options += ["--hex", image]
        return _dk(dk, "ensure", *options)

    return Step(step_id, f"Flash and check the {role} (J-Link {dk['serial']})",
                argv, timeout_s=ENSURE_TIMEOUT_S, mutates=True,
                output_patterns=(
                    rf"(?m)^firmware: {nrfdk.FIRMWARE_VERSION}$",
                    rf"(?m)^role: {role}$",
                    rf"(?m)^address: {dk['address']}$",
                ))


def _ready_case(central: Mapping, peripheral: Mapping) -> StateCase:
    return StateCase(
        "pair-ready",
        f"Both DKs run {nrfdk.FIRMWARE_VERSION} at their registered addresses",
        (
            Step("identify-central", "Find the central's J-Link probe and port",
                 _dk(central, "identify"), timeout_s=DK_TIMEOUT_S,
                 output_patterns=(rf"(?m)^J-Link serial: {central['serial']}$",)),
            Step("identify-peripheral", "Find the peripheral's J-Link probe and port",
                 _dk(peripheral, "identify"), timeout_s=DK_TIMEOUT_S,
                 output_patterns=(rf"(?m)^J-Link serial: {peripheral['serial']}$",)),
            _ensure_step("ensure-peripheral", peripheral, PERIPHERAL_ROLE),
            _ensure_step("ensure-central", central, CENTRAL_ROLE),
        ),
        (_dk_idle(peripheral, "peripheral-idle"),
         _dk_idle(central, "central-idle")),
        expected_state=(f"both DKs run {nrfdk.FIRMWARE_VERSION}, idle, in the "
                        "advertiser role"),
        optional_artifacts=(FIRMWARE_LABEL,),
    )


_TRAFFIC = re.compile(
    r"(?m)^traffic: writes (?P<writes>\d+) received \d+ bytes "
    r"updates (?P<updates>\d+) connects (?P<connects>\d+) "
    r"disconnects (?P<disconnects>\d+)$")
_UPDATE = re.compile(r"(?m)^update: asked (?P<ms>\d+) ms \(\d+ units\)$")


def _central_did_the_work(update_ms: int):
    """The central must have connected, written, and updated to ``update_ms``.

    Without this the case would pass on a DK that connected and sent nothing,
    or that never asked for the connection parameter update, or that had a
    different interval accepted: the capture checks the CONNECT_IND, which is
    written before any update.

    The counters it reads run from ``conn start``, so on a second capture
    attempt they cover the whole run rather than that one capture window.
    That is the floor this check is for: a DK that is connecting, writing,
    and having its updates accepted while the case runs.
    """
    def check(stdout) -> str:
        match = search_lines(stdout, _TRAFFIC)
        if match is None:
            return "the central reported no traffic line"
        counts = {name: int(value) for name, value in match.groupdict().items()}
        if counts["connects"] < 1:
            return "the central made no connection"
        if counts["writes"] < 1:
            return "the central wrote nothing on the connection"
        if counts["updates"] < 1:
            return (f"the central's {update_ms} ms connection parameter update "
                    "was never accepted")
        asked = search_lines(stdout, _UPDATE)
        if asked is None:
            return "the central reported no update line"
        if int(asked["ms"]) != update_ms:
            return (f"the central's accepted update was {asked['ms']} ms, not the "
                    f"{update_ms} ms it was told to ask for")
        return ""
    return check


def _central_cycled(update_ms: int, cycles: int):
    """...and made and dropped the link at least ``cycles`` times.

    The capture says what reached the air; only the DK can say how many
    times the link really went down and came back, so a capture window that
    the central spent on one long connection is told apart from one it spent
    cycling. One fewer disconnect than connection is allowed, because the
    last connection is usually still up when the status is read.

    ``cycles`` is the floor the duration rule guarantees, not what the
    duration works out to: each reconnection costs a moment on top of the
    cycle, so a longer window does not reach a proportionally higher count.

    The firmware counts connects and disconnects from the board's boot, and
    nothing in a case resets them, so on a second capture attempt they cover
    the whole case rather than that one window. That is what this floor is
    for: a central that is making and dropping links while the case runs.
    What binds per window is the capture check, which wants two of the
    pair's connections in the file the attempt just wrote.
    """
    did_the_work = _central_did_the_work(update_ms)

    def check(stdout) -> str:
        # did_the_work has found the traffic line when it reports nothing
        # wrong, so the search below has a match.
        reason = did_the_work(stdout)
        if reason:
            return reason
        counts = {name: int(value) for name, value
                  in search_lines(stdout, _TRAFFIC).groupdict().items()}
        if counts["connects"] < cycles:
            return (f"the central made {counts['connects']} connection(s), fewer "
                    f"than the {cycles} the capture window spans")
        if counts["disconnects"] < cycles - 1:
            return (f"the central dropped {counts['disconnects']} connection(s), "
                    f"fewer than the {cycles - 1} its cycling must have ended")
        return ""
    return check


def _capture_case(serial, central, peripheral, seconds, *, case_id, name,
                  interval_ms, update_ms, capture=("-r", "capture.pcapng"),
                  connection_info=False):
    """One followed connection: the pair runs, ubertooth-btle -f captures it."""
    option, file_name = capture
    interval_units = nrfdk.conn_units(interval_ms)

    capture_file = _attempt_capture_path(file_name)

    def check(context):
        argv = [sys.executable, _CONNCAPTURE, capture_file(context),
                "--central", central["address"],
                "--peripheral", peripheral["address"],
                "--interval-units", str(interval_units),
                "--latency", "0",
                "--timeout-units", str(conncapture.DEFAULT_TIMEOUT_UNITS),
                "--min-data-packets", str(MIN_DATA_PACKETS),
                "--min-valid-packets", str(MIN_VALID_PACKETS),
                "--min-data-length", str(LONG_PACKET_BYTES),
                "--long-packets", str(MIN_LONG_PACKETS)]
        if connection_info:
            argv.append("--check-connection-info")
        return argv

    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step("peripheral-start",
             f"Peripheral advertises connectable on channel {ADV_CHANNEL}",
             _dk(peripheral, "peripheral", "--dle", "on", "--chmap",
                 str(ADV_CHANNEL)),
             timeout_s=DK_TIMEOUT_S,
             mutates=True, output_patterns=(r"(?m)^role: peripheral$",)),
        Step("central-start",
             f"Central connects at {interval_ms} ms, then asks for {update_ms} ms",
             _dk(central, "central", "--peer", peripheral["address"],
                 "--interval", str(interval_ms), "--dle", "on",
                 "--payload", str(WRITE_BYTES), "--period", str(WRITE_PERIOD_MS),
                 "--cycle", str(CYCLE_MS), "--autoupdate", str(update_ms)),
             timeout_s=STATUS_TIMEOUT_S, mutates=True,
             output_patterns=(
                 r"(?m)^role: central$",
                 rf"(?m)^data length: tx {nrfdk.DATA_LENGTH_MAX} rx "
                 rf"{nrfdk.DATA_LENGTH_MAX} octets mtu \d+ phy 1M$",
             )),
        Step("capture", f"ubertooth-btle -f -A{ADV_CHANNEL} {option} for {seconds}s",
             lambda context: _btle(serial, "-f", f"-A{ADV_CHANNEL}", option,
                                   capture_file(context)),
             timeout_s=float(seconds), mutates=True, continuous=True),
        _stop(serial),
        Step("central-report", "What the central did while the case ran",
             _dk(central, "status"), timeout_s=DK_TIMEOUT_S,
             output_patterns=(r"(?m)^role: central$",),
             output_check=_central_did_the_work(update_ms)),
        Step("check-capture", "The capture holds the connection the DKs made",
             check, timeout_s=CHECK_TIMEOUT_S,
             output_patterns=(r"(?m)^capture checks passed$",)),
    )
    return StateCase(
        case_id, name, steps,
        (_dk_idle(central, "central-idle"),
         _dk_idle(peripheral, "peripheral-idle"))
        + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=(
            f"a CONNECT_IND at {interval_units} units with {MIN_DATA_PACKETS}+ "
            f"packets on its access address, within {CAPTURE_ATTEMPTS} captures; "
            "both DKs idle; channel, squelch, and LEDs restored"),
        capture_dir=True,
        # A capture that misses the connection is taken again: the capture
        # itself, the stop that ends it, the central's report of what it did
        # while it ran, and the check of the file it wrote.
        retry_steps=("capture", "stop", "central-report", "check-capture"),
        attempts=CAPTURE_ATTEMPTS,
    )


def _follow_end_case(serial, central, peripheral, seconds, *, interval_ms,
                     update_ms) -> StateCase:
    """One capture across several of the pair's connections."""
    interval_units = nrfdk.conn_units(interval_ms)
    capture_file = _attempt_capture_path("capture.pcapng")

    def check(context):
        return [sys.executable, _CONNCAPTURE, capture_file(context),
                "--central", central["address"],
                "--peripheral", peripheral["address"],
                "--interval-units", str(interval_units),
                "--latency", "0",
                "--timeout-units", str(conncapture.DEFAULT_TIMEOUT_UNITS),
                "--min-data-packets", str(MIN_DATA_PACKETS),
                "--min-valid-packets", str(MIN_VALID_PACKETS),
                "--min-connections", str(MIN_CONNECTIONS),
                "--min-connection-packets", str(MIN_CONNECTION_PACKETS),
                "--after-update-ms", str(AFTER_UPDATE_MS),
                "--capture-ms", str(int(seconds) * 1000),
                "--max-quiet-ms", str(MAX_QUIET_MS),
                "--require-announced"]

    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step("read-original-watchdog", "Read the board's self-reset count",
             _util(serial, "-w"), timeout_s=WATCHDOG_TIMEOUT_S,
             observe=_resets_observation(WATCHDOG_RESETS)),
        Step("peripheral-start",
             f"Peripheral advertises connectable on channel {ADV_CHANNEL}",
             _dk(peripheral, "peripheral", "--dle", "on", "--chmap",
                 str(ADV_CHANNEL)),
             timeout_s=DK_TIMEOUT_S, mutates=True,
             output_patterns=(r"(?m)^role: peripheral$",)),
        Step("central-start",
             f"Central connects at {interval_ms} ms and remakes the link every "
             f"{CYCLE_MS / 1000:g}s",
             _dk(central, "central", "--peer", peripheral["address"],
                 "--interval", str(interval_ms), "--dle", "on",
                 "--payload", str(WRITE_BYTES), "--period", str(WRITE_PERIOD_MS),
                 "--cycle", str(CYCLE_MS), "--autoupdate", str(update_ms)),
             timeout_s=STATUS_TIMEOUT_S, mutates=True,
             output_patterns=(
                 r"(?m)^role: central$",
                 rf"(?m)^data length: tx {nrfdk.DATA_LENGTH_MAX} rx "
                 rf"{nrfdk.DATA_LENGTH_MAX} octets mtu \d+ phy 1M$",
             )),
        Step("capture",
             f"ubertooth-btle -f -A{ADV_CHANNEL} -r for {seconds}s, across "
             f"{MIN_CYCLES}+ of the central's connections",
             lambda context: _btle(serial, "-f", f"-A{ADV_CHANNEL}", "-r",
                                   capture_file(context)),
             timeout_s=float(seconds), mutates=True, continuous=True),
        _stop(serial),
        Step("central-report", "What the central did while the case ran",
             _dk(central, "status"), timeout_s=DK_TIMEOUT_S,
             output_patterns=(r"(?m)^role: central$",),
             output_check=_central_cycled(update_ms, MIN_CYCLES)),
        Step("check-capture",
             "The capture holds several of the pair's connections and nothing else",
             check, timeout_s=CHECK_TIMEOUT_S,
             output_patterns=(r"(?m)^capture checks passed$",)),
        Step("read-back", "The board answers identity and settings reads at once",
             _util(serial, "-s", "-C"),
             output_patterns=(serial_pattern(serial), CHANNEL.pattern)),
        Step("idle-after-capture",
             "The board is back in idle mode, with no new self-reset",
             _util(serial, "-w"), timeout_s=WATCHDOG_TIMEOUT_S,
             output_patterns=(IDLE_AFTER_CAPTURE,),
             observe=_resets_observation(
                 f"{WATCHDOG_RESETS} after the capture",
                 lambda context: require(context, WATCHDOG_RESETS))),
    )
    return StateCase(
        "follow-across-end",
        f"ubertooth-btle -f follows {MIN_CONNECTIONS}+ of the pair's "
        "connections in one capture and leaves the board ready",
        steps,
        (_dk_idle(central, "central-idle"),
         _dk_idle(peripheral, "peripheral-idle"))
        + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=(
            f"{MIN_CONNECTIONS}+ CONNECT_INDs at {interval_units} units, each "
            f"with {MIN_CONNECTION_PACKETS}+ packets on its access address and "
            f"none on an unannounced one; the board idle with no new self-reset; "
            "both DKs idle; channel, squelch, and LEDs restored"),
        capture_dir=True,
        # Only the capture and the checks of it are taken again: the reads
        # after them must see the board as the last capture left it.
        retry_steps=("capture", "stop", "central-report", "check-capture"),
        attempts=CAPTURE_ATTEMPTS,
    )


def follow_end_cases(arguments, resources, resource_identities):
    serial = _serial(resource_identities, FOLLOW_END_TEST_NAME)
    central, peripheral = _pair(resources, resource_identities,
                                FOLLOW_END_TEST_NAME)
    return (
        _ready_case(central, peripheral),
        _follow_end_case(serial, central, peripheral, seconds_arg(arguments),
                         interval_ms=arguments["interval"],
                         update_ms=arguments["update"]),
    )


def known_connection_cases(arguments, resources, resource_identities):
    serial = _serial(resource_identities, TEST_NAME)
    central, peripheral = _pair(resources, resource_identities)
    seconds = seconds_arg(arguments)
    interval = arguments["interval"]
    update = arguments["update"]
    cases = [_ready_case(central, peripheral)]
    cases.append(_capture_case(
        serial, central, peripheral, seconds, case_id="follow-pcapng",
        name="ubertooth-btle -f -r (PCAPNG) holds the pair's CONNECT_IND and "
             "long packets",
        interval_ms=interval, update_ms=update))
    if arguments["coverage"] == "comprehensive":
        cases.append(_capture_case(
            serial, central, peripheral, seconds, case_id="follow-pcap",
            name="ubertooth-btle -f -q (PCAP) holds the pair's CONNECT_IND and "
                 "long packets",
            interval_ms=interval, update_ms=update,
            capture=("-q", "capture.pcap")))
        cases.append(_capture_case(
            serial, central, peripheral, seconds,
            case_id="pcapng-connection-option",
            name="PCAPNG connection information holds the pair's CONNECT_IND "
                 "and its parameters",
            interval_ms=interval, update_ms=update, connection_info=True))
    return tuple(cases)
