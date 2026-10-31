"""Capture known BLE advertiser: an nRF52840 DK advertises, the Ubertooth listens.

The DK runs the harness's role firmware (firmware/nrf52840dk) and is driven
by nrfdk.py; capture files are checked by advcapture.py. Both run by path,
like the other helpers, with the harness's own interpreter.

The first case makes sure the DK runs nrfdk.FIRMWARE_VERSION, flashing the
run's ``nRF52840 advertiser`` artifact when it runs anything else, and that it
advertises from its registered address. Each capture case then sets the DK to
one advertising channel, runs ``ubertooth-btle -n`` (or ``-f``) on one
channel for the plan's duration with a capture file, stops both, and checks
the file:

- listening where the DK advertises: at least a minimum share of its
  advertising events, every one on that channel's RF channel, with the
  harness payload (magic, channel map, tag) and a counter from the DK's
  counter before the capture to its counter after, and no more packets than
  the DK can have sent at its interval;
- listening on another channel: none of its packets at all (the bands are
  24 MHz or more apart, so a leak is a real fault, not noise).

Every case's cleanup stops the DK (boot settings, advertising off) and
restores the Ubertooth's channel, squelch, and LEDs.
"""

from __future__ import annotations

import math
from pathlib import Path
import sys
from typing import Mapping

from ubertooth_harness import advcapture, nrfdk
from ubertooth_harness.receive_cases import seconds_arg
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    StateCaseError,
    Step,
    artifact_key,
    require,
)
from ubertooth_harness.transitions import (
    MODE_RESET_SETTINGS,
    _btle,
    _cleanup,
    _read_originals,
    _serial,
    _stop,
)

TEST_NAME = "Capture known BLE advertiser"
# --artifact "nRF52840 advertiser=PATH" (plan.image_label of the name).
FIRMWARE_LABEL = "nrf52840_advertiser"
ADVERTISER_ROLE = "advertiser"
CHANNELS = (37, 38, 39)
# A slow interval for the interval case: a DK advertising faster than asked
# would exceed the packet bound.
SLOW_INTERVAL_MS = 1000
# Share of the DK's advertising events the Ubertooth must catch on its
# channel. On the bench it catches most; well under half means a fault.
MIN_SHARE = 0.2
MIN_PACKETS = 3
# Seconds the capture may run beyond its duration (start-up and stop), for
# the upper packet bound.
CAPTURE_SLACK_S = 3.0
# nrfdk.py: a setting run is up to seven shell commands; ensure may flash.
DK_TIMEOUT_S = 30.0
ENSURE_TIMEOUT_S = nrfdk.FLASH_TIMEOUT_S + nrfdk.BOOT_TIMEOUT_S + 30.0
CHECK_TIMEOUT_S = 30.0

_NRFDK = str(Path(nrfdk.__file__).resolve())
_ADVCAPTURE = str(Path(advcapture.__file__).resolve())
_COUNTER = r"(?m)^counter: (?P<value>\d+)$"


def _advertiser(resources: Mapping, resource_identities: Mapping) -> dict:
    identity = resource_identities.get(ADVERTISER_ROLE)
    if not identity:
        raise StateCaseError(f"{TEST_NAME} requires an advertiser (nRF52840 DK)")
    try:
        serial = nrfdk.jlink_serial(identity.get("jlink serial", ""))
        address = nrfdk.normalize_address(identity.get("BLE address", ""))
    except nrfdk.NrfDkError as error:
        raise StateCaseError(f"{TEST_NAME}: advertiser {error}") from error
    if not nrfdk.is_static_random(address):
        raise StateCaseError(
            f"{TEST_NAME}: advertiser BLE address {address} is not a static random "
            "address (the two top bits must be set)")
    port = identity.get("serial device") or None
    return {"serial": str(serial), "address": address, "port": port,
            "alias": resources.get(ADVERTISER_ROLE, "")}


def _dk(dk: Mapping, command: str, *options: str) -> tuple[str, ...]:
    argv = (sys.executable, _NRFDK, command, "--jlink", dk["serial"])
    # With the alias, nrfdk.py marks every line of its conversation with the
    # board's shell, so the run's transcript holds it (targets.shell_line).
    if dk.get("alias"):
        argv += ("--alias", dk["alias"])
    if dk["port"]:
        argv += ("--port", dk["port"])
    return argv + options


def _ensure_case(dk: Mapping) -> StateCase:
    def argv(context):
        options = ["--address", dk["address"]]
        image = context.get(artifact_key(FIRMWARE_LABEL))
        if image:
            options += ["--hex", image]
        return _dk(dk, "ensure", *options)

    return StateCase(
        "advertiser-ready",
        f"Advertiser runs {nrfdk.FIRMWARE_VERSION} at {dk['address']}",
        (
            Step("identify", "Find the advertiser's J-Link probe and port",
                 _dk(dk, "identify"), timeout_s=DK_TIMEOUT_S,
                 output_patterns=(rf"(?m)^J-Link serial: {dk['serial']}$",)),
            Step("ensure", "Flash the advertiser firmware if the board runs another",
                 argv, timeout_s=ENSURE_TIMEOUT_S, mutates=True,
                 output_patterns=(
                     rf"(?m)^firmware: {nrfdk.FIRMWARE_VERSION}$",
                     rf"(?m)^address: {dk['address']}$",
                     r"(?m)^advertising: off ",
                 )),
        ),
        (_dk_idle(dk),),
        expected_state=f"the DK runs {nrfdk.FIRMWARE_VERSION}, idle, at {dk['address']}",
        optional_artifacts=(FIRMWARE_LABEL,),
    )


def _dk_idle(dk: Mapping, step_id: str = "advertiser-idle") -> Step:
    """Release a DK: back to the advertiser role, idle, with boot settings.

    ``step_id`` names it, because a case with two DKs has two of these.
    """
    return Step(step_id, f"Leave J-Link {dk['serial']} idle in the advertiser role",
                _dk(dk, "idle"), timeout_s=DK_TIMEOUT_S,
                output_patterns=(r"(?m)^advertising: off ", r"(?m)^role: advertiser$"))


def _capture_path(name):
    def path(context):
        return str(Path(require(context, CAPTURE_DIR_KEY)) / name)
    return path


def _capture_case(serial, dk, seconds, *, case_id, name, listen, chmap, interval_ms,
                  payload, mode="-n", capture=("-q", "capture.pcap"), adv_type="nonconn",
                  min_packets=None, max_packets=None):
    """One capture: the DK on ``chmap``, the Ubertooth on ``listen``.

    The advertiser's packets are expected when ``chmap`` covers ``listen``
    (``all`` or the same channel) and must be absent otherwise.
    """
    option, file_name = capture
    expected = chmap in ("all", str(listen))
    events = seconds * 1000.0 / interval_ms
    if min_packets is None:
        min_packets = max(MIN_PACKETS, math.floor(events * MIN_SHARE))
    if max_packets is None:
        max_packets = math.ceil((seconds + CAPTURE_SLACK_S) * 1000.0 / interval_ms) + 1

    def check(context):
        argv = [sys.executable, _ADVCAPTURE, _capture_path(file_name)(context),
                "--address", dk["address"], "--channel", str(listen)]
        if not expected:
            return argv + ["--absent"]
        return argv + [
            "--payload", payload or "none", "--chmap", chmap, "--type", adv_type,
            "--counter-min", require(context, "counter before"),
            "--counter-max", require(context, "counter after"),
            "--min-packets", str(min_packets), "--max-packets", str(max_packets),
        ]

    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step("advertise", f"Advertiser on channel {chmap}, {interval_ms} ms, {adv_type}",
             _dk(dk, "advertise", "--chmap", chmap, "--interval", str(interval_ms),
                 "--payload", payload or "none", "--type", adv_type),
             timeout_s=DK_TIMEOUT_S, mutates=True,
             output_patterns=(rf"(?m)^advertising: on type {adv_type} chmap {chmap} ",),
             observe=Observation("counter before", _COUNTER)),
        Step("capture", f"ubertooth-btle {mode} -A{listen} {option} for {seconds}s",
             lambda context: _btle(serial, mode, f"-A{listen}", option,
                                   _capture_path(file_name)(context)),
             timeout_s=float(seconds), mutates=True, continuous=True),
        _stop(serial),
        Step("advertiser-stop", "Stop the advertiser",
             _dk(dk, "stop"), timeout_s=DK_TIMEOUT_S,
             output_patterns=(r"(?m)^advertising: off ",),
             observe=Observation("counter after", _COUNTER)),
        Step("check-capture",
             (f"Capture holds the advertiser's packets on channel {listen}" if expected
              else f"Capture holds none of the advertiser's packets (it is on {chmap})"),
             check, timeout_s=CHECK_TIMEOUT_S,
             output_patterns=(r"(?m)^capture checks passed$",)),
    )
    state = (f"{min_packets}-{max_packets} advertiser packets on channel {listen}"
             if expected else f"no advertiser packets on channel {listen}")
    return StateCase(
        case_id, name, steps, (_dk_idle(dk),) + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=state + "; advertiser idle; channel, squelch, and LEDs restored",
        capture_dir=True,
    )


def known_advertiser_cases(arguments, resources, resource_identities):
    serial = _serial(resource_identities, TEST_NAME)
    dk = _advertiser(resources, resource_identities)
    seconds = seconds_arg(arguments)
    interval = arguments["interval"]
    payload = arguments["payload"]
    common = {"interval_ms": interval, "payload": payload}

    def capture(listen, chmap, **options):
        return _capture_case(serial, dk, seconds, listen=listen, chmap=chmap,
                             **{**common, **options})

    cases = [_ensure_case(dk)]
    cases += [
        capture(channel, str(channel), case_id=f"capture-{channel}",
                name=f"ubertooth-btle -n -A{channel} -q sees the advertiser on {channel}")
        for channel in CHANNELS
    ]
    comprehensive = arguments["coverage"] == "comprehensive"
    pairs = ([(listen, adv) for listen in CHANNELS for adv in CHANNELS if listen != adv]
             if comprehensive else [(37, 38)])
    cases += [
        capture(listen, str(adv), case_id=f"absent-{listen}-adv-{adv}",
                name=f"ubertooth-btle -n -A{listen} sees nothing of the advertiser on {adv}")
        for listen, adv in pairs
    ]
    follow = CHANNELS if comprehensive else (37,)
    cases += [
        capture(channel, str(channel), case_id=f"follow-{channel}-pcapng", mode="-f",
                capture=("-r", "capture.pcapng"),
                name=f"ubertooth-btle -f -A{channel} -r (PCAPNG) sees the advertiser")
        for channel in follow
    ]
    if comprehensive:
        cases += [
            capture(37, "37", case_id="adv-ind-37", adv_type="conn",
                    name="ubertooth-btle -n -A37 sees the advertiser's ADV_IND"),
            capture(38, "all", case_id="all-channels-38",
                    name="ubertooth-btle -n -A38 sees the advertiser on all channels"),
            capture(37, "37", case_id="slow-interval-37", interval_ms=SLOW_INTERVAL_MS,
                    name=f"Advertiser at {SLOW_INTERVAL_MS} ms sends no more than its "
                         "interval allows"),
            capture(39, "39", case_id="no-payload-39", payload="",
                    name="ubertooth-btle -n -A39 sees the advertiser with no tag"),
        ]
    return tuple(cases)
