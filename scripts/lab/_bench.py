"""What the bench helpers in this directory share: how a device is locked.

One rule, in one place, so `hold` and `hub-port` can never disagree about
which file names a device's lock. A disagreement would silently stop them
excluding each other, which is the whole point of the locks.
"""

from __future__ import annotations

from ubertooth_harness import leases
from ubertooth_harness.resources import RegistryError

# The Classic Bluetooth peers. The harness does not lease this type yet; when
# it does, `leases.LEASED_TYPES` covers it and the fallback below retires.
PEER_TYPE = "ESP32 Classic peer"
HELD_TYPES = tuple(leases.LEASED_TYPES) + (PEER_TYPE,)


def lock_key(resource) -> str:
    """The lease key of a device, the harness's own wherever it has one."""
    if resource.type in leases.LEASED_TYPES:
        return leases.resource_key(resource)
    if resource.type == PEER_TYPE:
        serial = str(resource.identity.get("bridge serial", "") or "").strip().lower()
        if not serial:
            raise RegistryError(
                f"{resource.alias} needs a 'bridge serial' in the hardware "
                "registry to be held"
            )
        return f"esp32-{serial}"
    raise RegistryError(
        f"{resource.alias} is a {resource.type}, which these helpers do not "
        "hold (they hold " + ", ".join(HELD_TYPES) + ")"
    )


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
