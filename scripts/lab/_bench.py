"""What the bench helpers in this directory share: how a device is locked.

One rule, in one place, so `hold` and `hub-port` can never disagree about
which file names a device's lock. A disagreement would silently stop them
excluding each other, which is the whole point of the locks.
"""

from __future__ import annotations

from ubertooth_harness import leases
from ubertooth_harness.resources import RegistryError

PEER_TYPE = leases.PEER_TYPE
HELD_TYPES = tuple(leases.LEASED_TYPES)

# The shortest prefix of a serial a name may be. Bench notes name a board by
# its first four hex digits, and an ambiguous prefix is refused, so a short
# name can never quietly select the wrong device.
MIN_PREFIX = 4


def lock_key(resource) -> str:
    """The lease key of a device: the harness's own, for every type it leases."""
    if resource.type in leases.LEASED_TYPES:
        return leases.resource_key(resource)
    raise RegistryError(
        f"{resource.alias} is a {resource.type}, which these helpers do not "
        "hold (they hold " + ", ".join(HELD_TYPES) + ")"
    )


def _identity_values(resource) -> list[str]:
    """The serials of a resource, as a bench note or a tool would print them.

    Serials only. A radio address is also in the identity, and it is not a
    device name: matching it would accept a BD_ADDR as a device, which no
    document promises and which reads as a mistake.
    """
    values = []
    for key, value in resource.identity.items():
        text = str(value or "").strip()
        if not text or "serial" not in key or key == "serial device":
            continue
        values.append(text)
        if text.isdigit():
            values.append(text.lstrip("0"))  # nrfutil's form of a J-Link serial
    return values


def _and(resources) -> str:
    aliases = sorted(resource.alias for resource in resources)
    return ", ".join(aliases[:-1]) + " and " + aliases[-1]


def resolve(registry, name: str):
    """The one resource a name selects, or an error naming what it matched.

    A registry alias, a full serial, or an unambiguous prefix of one; for an
    nRF52840 DK, its J-Link serial with or without the leading zeros, which is
    how nrfutil and sysfs each print it.
    """
    if not name:
        raise RegistryError("empty device name")
    if name in registry.resources:
        return registry.resources[name]
    wanted = name.strip().lower()
    exact = [
        resource for resource in registry.resources.values()
        if any(value.lower() == wanted for value in _identity_values(resource))
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RegistryError(
            f"'{name}' is the serial of " + _and(exact) + "; name one alias"
        )
    if len(wanted) < MIN_PREFIX:
        raise RegistryError(
            f"'{name}' is no alias or serial in the registry, and is too short "
            f"to be a prefix of one (at least {MIN_PREFIX} characters)"
        )
    matches = [
        resource for resource in registry.resources.values()
        if any(value.lower().startswith(wanted) for value in _identity_values(resource))
    ]
    if not matches:
        raise RegistryError(
            f"'{name}' is no alias, serial, or serial prefix in the registry"
        )
    if len(matches) > 1:
        raise RegistryError(
            f"'{name}' is the start of the serial of " + _and(matches)
            + "; give more of the serial, or an alias"
        )
    return matches[0]


def plain_serial(resource) -> str:
    """The serial a bench's own scripts may name a device's lock file after.

    An Ubertooth's lease key is its serial, so there is no second name and
    this is empty. A DK's and a peer's keys are not their serials, so both
    names exist and both are taken.
    """
    if resource.type not in (leases.DK_TYPE, PEER_TYPE):
        return ""
    for key in ("jlink serial", "bridge serial"):
        value = str(resource.identity.get(key, "") or "").strip().lower()
        if value:
            return value
    return ""


def board(resource) -> leases.Board:
    """The device's own lock, for `leases.acquire`."""
    return leases.Board(resource.alias, lock_key(resource))


def bench_lock_board(resource) -> leases.Board | None:
    """The bench's older lock name for this device, when it has a second one."""
    serial = plain_serial(resource)
    if not serial or serial == lock_key(resource):
        return None
    return leases.Board(f"{resource.alias} (bench lock)", serial)
