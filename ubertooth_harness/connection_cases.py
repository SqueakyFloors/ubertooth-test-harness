"""Capture known BLE connection: two nRF52840 DKs connect, the Ubertooth follows.

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

The pcapng connection-information option libbtbb writes for a CONNECT_IND is
known to be wrong (SqueakyFloors/libbtbb#21: it is copied 6 bytes early, so
every parameter in it belongs to another field). One case pins that, so it
fails when libbtbb is fixed and the case can be turned into a real check.

Every case's cleanup leaves both DKs idle in the advertiser role with their
boot settings, and restores the Ubertooth's channel, squelch, and LEDs.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Mapping

from ubertooth_harness import conncapture, nrfdk
from ubertooth_harness.advertiser_cases import (
    DK_TIMEOUT_S,
    ENSURE_TIMEOUT_S,
    FIRMWARE_LABEL,
    _capture_path,
    _dk,
    _dk_idle,
)
from ubertooth_harness.outputfiles import search_lines
from ubertooth_harness.receive_cases import seconds_arg
from ubertooth_harness.stateful import (
    StateCase,
    StateCaseError,
    Step,
    artifact_key,
)
from ubertooth_harness.transitions import (
    MODE_RESET_SETTINGS,
    _btle,
    _cleanup,
    _read_originals,
    _serial,
    _stop,
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
# `check_arguments` enforces it. Catching the one CONNECT_IND is not yet
# reliable: issue #149.
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
CHECK_TIMEOUT_S = 30.0
# Settling and status reads after the capture.
STATUS_TIMEOUT_S = 30.0

_CONNCAPTURE = str(Path(conncapture.__file__).resolve())


def _pair(resources: Mapping, resource_identities: Mapping) -> tuple[dict, dict]:
    """The central and the peripheral, from the registry's identities."""
    pair = []
    for role in (CENTRAL_ROLE, PERIPHERAL_ROLE):
        identity = resource_identities.get(role)
        if not identity:
            raise StateCaseError(f"{TEST_NAME} requires a {role} (nRF52840 DK)")
        try:
            serial = nrfdk.jlink_serial(identity.get("jlink serial", ""))
            address = nrfdk.normalize_address(identity.get("BLE address", ""))
        except nrfdk.NrfDkError as error:
            raise StateCaseError(f"{TEST_NAME}: {role} {error}") from error
        if not nrfdk.is_static_random(address):
            raise StateCaseError(
                f"{TEST_NAME}: {role} BLE address {address} is not a static random "
                "address (the two top bits must be set)")
        pair.append({"serial": str(serial), "address": address,
                     "port": identity.get("serial device") or None,
                     "alias": resources.get(role, "")})
    central, peripheral = pair
    if central["serial"] == peripheral["serial"]:
        raise StateCaseError(
            f"{TEST_NAME} needs two DKs; the central and the peripheral are both "
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
    r"updates (?P<updates>\d+) connects (?P<connects>\d+)$")
_UPDATE = re.compile(r"(?m)^update: asked (?P<ms>\d+) ms \(\d+ units\)$")


def _central_did_the_work(update_ms: int):
    """The central must have connected, written, and updated to ``update_ms``.

    Without this the case would pass on a DK that connected and sent nothing,
    or that never asked for the connection parameter update, or that had a
    different interval accepted: the capture checks the CONNECT_IND, which is
    written before any update.
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


def _capture_case(serial, central, peripheral, seconds, *, case_id, name,
                  interval_ms, update_ms, capture=("-r", "capture.pcapng"),
                  option_check=False):
    """One followed connection: the pair runs, ubertooth-btle -f captures it."""
    option, file_name = capture
    interval_units = nrfdk.conn_units(interval_ms)

    def check(context):
        argv = [sys.executable, _CONNCAPTURE, _capture_path(file_name)(context),
                "--central", central["address"],
                "--peripheral", peripheral["address"],
                "--interval-units", str(interval_units),
                "--latency", "0",
                "--timeout-units", str(conncapture.DEFAULT_TIMEOUT_UNITS),
                "--min-data-packets", str(MIN_DATA_PACKETS),
                "--min-valid-packets", str(MIN_VALID_PACKETS),
                "--min-data-length", str(LONG_PACKET_BYTES),
                "--long-packets", str(MIN_LONG_PACKETS)]
        if option_check:
            argv.append("--connection-info-option")
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
                                   _capture_path(file_name)(context)),
             timeout_s=float(seconds), mutates=True, continuous=True),
        _stop(serial),
        Step("central-report", "What the central did while the capture ran",
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
            "packets on its access address; both DKs idle; channel, squelch, and "
            "LEDs restored"),
        capture_dir=True,
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
            case_id="pcapng-connection-option-libbtbb-21",
            name="PCAPNG connection information is still copied 6 bytes early "
                 "(expected failure, libbtbb#21)",
            interval_ms=interval, update_ms=update, option_check=True))
    return tuple(cases)
