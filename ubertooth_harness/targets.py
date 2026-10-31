"""Known target devices: what a test ran against, and its serial shell lines.

A *known target* is a device the harness drives itself, so the run knows
exactly what was on the air: an nRF52840 DK in one of its roles
(firmware/nrf52840dk, driven by nrfdk.py), or an ESP32 Classic peer
(firmware/esp32-classic-peer). Every other test listens to whatever traffic
happens to be there, which is why a report has to say which is which.

This module holds, for each kind of target, the registry identity keys that
name it and the firmware version its driver requires, and turns a test's
bound resources into:

- the ``known_target`` record the run writes to run.jsonl, one per test;
- the one-line summary report.md and the GUI both show, so neither has to
  know how a target is described;
- the format of the serial-shell lines a driver writes for the transcript,
  ``[DK_Linux1 >] adv start`` out and ``[DK_Linux1 <] OK`` back.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

_HEX_BYTE = re.compile(r"[0-9A-Fa-f]{2}")

NRF52840_DK = "nRF52840 DK"
ESP32_CLASSIC_PEER = "ESP32 Classic peer"

# Stable device paths on Linux: udev's by-id name carries the probe's or the
# bridge's serial, so the path can be derived from the registry's identity.
# nrfutil pads a J-Link serial to 12 digits in the name it reports.
DK_BY_ID = "/dev/serial/by-id/usb-SEGGER_J-Link_{serial:012d}-if00"
PEER_BY_ID = ("/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_"
              "Controller_{serial}-if00-port0")

# The firmware version each driver requires of its target, which is what a
# record says: not a version read from the board, which the run has not asked
# for when the record is written. nrfdk.py flashes a DK that reports anything
# else, so its ready case is what proves the board runs this
# (firmware/nrf52840dk/src/roles.h and firmware/esp32-classic-peer/main/peer.h;
# tests/test_targets.py keeps them in step with those sources).
DK_FIRMWARE = "utdk-1.2.1"
PEER_FIRMWARE = "utesp-1.2.0"


@dataclass(frozen=True)
class TargetKind:
    """How one kind of known target is named and identified."""

    serial_key: str  # registry identity key that names the device
    serial_label: str  # how that serial is introduced in the summary
    address_key: str  # registry identity key of its known address
    firmware: str  # the version its driver requires, not one read from it
    by_id: str
    # A Classic (BR/EDR) address also gives the LAP and UAP that
    # `ubertooth-rx -l` and `-u` take, so the summary spells them out.
    classic: bool = False


KINDS: Mapping[str, TargetKind] = {
    NRF52840_DK: TargetKind(
        serial_key="jlink serial", serial_label="J-Link",
        address_key="BLE address", firmware=DK_FIRMWARE, by_id=DK_BY_ID,
    ),
    ESP32_CLASSIC_PEER: TargetKind(
        serial_key="bridge serial", serial_label="bridge",
        address_key="BD_ADDR", firmware=PEER_FIRMWARE, by_id=PEER_BY_ID,
        classic=True,
    ),
}

NO_TARGET = "No known target: the harness drove no device for this test"


def device_path(kind: TargetKind, serial: str, platform: str | None) -> str:
    """The target's serial port path, where this host has a stable one."""
    if platform != "linux" or not serial:
        return ""
    if "{serial:012d}" in kind.by_id:
        if not serial.isdigit():
            return ""
        return kind.by_id.format(serial=int(serial))
    return kind.by_id.format(serial=serial)


def lap_uap(address: str) -> tuple[str, str]:
    """The LAP and UAP of a Classic address, as the host tools take them.

    Empty for anything that is not a 48-bit address written in hex, so a
    registry entry nobody has validated cannot put a confident LAP in a
    report.
    """
    parts = address.split(":")
    if len(parts) != 6 or not all(_HEX_BYTE.fullmatch(part) for part in parts):
        return "", ""
    return "".join(parts[3:]).lower(), parts[2].lower()


def describe(target: Mapping) -> str:
    """One target, as the report's Known target line and the GUI show it."""
    kind = KINDS.get(target.get("type", ""))
    where = ", ".join(
        part for part in (
            f"{kind.serial_label} {target['serial']}" if kind and target.get("serial")
            else target.get("serial", ""),
            target.get("port") or "",
        ) if part
    )
    text = f"{target.get('type', '')} {target.get('role', '')} {target.get('alias', '')}"
    if where:
        text += f" ({where})"
    if target.get("firmware_required"):
        text += f", requires firmware {target['firmware_required']}"
    identity = target.get("identity") or {}
    for key, value in identity.items():
        text += f", {key} {value}"
    # A Classic address also gives the LAP and UAP the host tools take.
    if target.get("lap"):
        text += f" (LAP {target['lap']}, UAP {target.get('uap', '')})"
    return text


def summary(targets: Sequence[Mapping]) -> str:
    """The line a test's results and transcript carry, with or without targets."""
    if not targets:
        return NO_TARGET
    plural = "s" if len(targets) > 1 else ""
    return (f"Run against known target{plural}: "
            + "; ".join(describe(target) for target in targets))


def column(targets: Sequence[Mapping]) -> str:
    """The summary table's cell: which known target, or none."""
    if not targets:
        return "none"
    # A stored run.jsonl is read as it was written, so a record missing a
    # field must not break the whole report.
    return ", ".join(
        f"{target.get('alias', '')} ({target.get('role', '')})" for target in targets
    )


def known_targets(
    hardware_roles: Mapping[str, str],
    resources: Mapping[str, str],
    resource_identities: Mapping[str, Mapping[str, str]],
    platform: str | None = None,
) -> tuple[dict, ...]:
    """Every known target a test invocation is bound to, in the test's order.

    ``hardware_roles`` is the catalog's role to resource type mapping, so the
    kind of each target comes from the test definition; the alias and the
    identity come from what the planner selected. Roles bound to anything
    else (an Ubertooth, a guard board) are not known targets.
    """
    found = []
    for role, resource_type in hardware_roles.items():
        kind = KINDS.get(resource_type)
        if kind is None:
            continue
        identity = resource_identities.get(role) or {}
        serial = identity.get(kind.serial_key, "")
        address = identity.get(kind.address_key, "")
        record = {
            "type": resource_type,
            "role": role,
            "alias": resources.get(role, ""),
            "serial": serial,
            "serial_label": kind.serial_label,
            "port": identity.get("serial device") or device_path(kind, serial, platform),
            "firmware_required": kind.firmware,
            # The identity as the registry holds it, so a reader of the
            # record gets the address itself, not a description of it.
            "identity": {kind.address_key: address} if address else {},
        }
        if address and kind.classic:
            lap, uap = lap_uap(address)
            if lap:
                record["lap"], record["uap"] = lap, uap
        found.append(record)
    return tuple(found)


def target_details(
    invocation_id: str,
    test_name: str,
    targets: Sequence[Mapping],
) -> dict:
    """The ``known_target`` record's details, with the summary both readers use."""
    return {
        "invocation_id": invocation_id,
        "test_name": test_name,
        "targets": [dict(target) for target in targets],
        "summary": summary(targets),
    }


# --------------------------------------------------------------------------
# Serial shell transcript

SENT = ">"
RECEIVED = "<"
# The alias as the registry accepts it (resources._ALIAS_RE; the same text,
# because a tag only works if it matches what an alias may be, and
# tests/test_targets.py keeps the two in step).
ALIAS = r"[A-Za-z][A-Za-z0-9_-]{0,63}"
_SHELL_LINE = re.compile(
    rf"^\[(?P<alias>{ALIAS}) (?P<direction>[<>])\] (?P<text>.*)$"
)


def shell_line(alias: str, direction: str, text: str) -> str:
    """One line to or from a target's serial shell, marked with its alias.

    A driver writes these on standard error as it talks to its target, so
    every command and answer reaches run.jsonl with the time it arrived and
    shows up in the transcript beside the Ubertooth tool output.
    """
    if direction not in (SENT, RECEIVED):
        raise ValueError(f"direction must be {SENT!r} or {RECEIVED!r}")
    return f"[{alias} {direction}] {text}"


def shell_tag(line: str) -> tuple[str, str] | None:
    """``("[DK_Linux1 >]", "adv start")`` for a shell line, else None."""
    match = _SHELL_LINE.fullmatch(line)
    if match is None:
        return None
    return f"[{match['alias']} {match['direction']}]", match["text"]
