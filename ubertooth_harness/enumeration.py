"""Read-only discovery of attached Ubertooths, used to confirm power isolation.

Power controllers such as the StarTech managed hub cannot measure VBUS, so a
port that reads "off" is not proof the board lost power. Isolation is instead
confirmed by the board disappearing from (and reappearing in) USB enumeration.
Discovery reads sysfs only; it never opens a device, so it cannot disturb a
running capture.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

UBERTOOTH_VENDOR = "1d50"
# Every Ubertooth USB identity (firmware/bootloader/bootloader.cpp and
# host/libubertooth): Zero (6000) and its bootloader (6001), One (6002) and its
# DFU bootloader (6003). A board in a bootloader is still powered.
UBERTOOTH_PRODUCTS = frozenset({"6000", "6001", "6002", "6003"})
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"
# The DFU bootloader reports no serial number (iSerialNumber = 0), so such a
# device cannot be identified. It is reported under this prefix, which can
# never match a registered serial, so isolation checks fail closed on it.
UNIDENTIFIED_PREFIX = "unidentified:"

# Measured on the lab host (StarTech hub): a board leaves USB about
# 0.1-0.3 s after power-off and re-enumerates about 0.7-0.8 s after power-on
# (about 1.2 s including restoration). On the Acroname USBHub3+ a board
# leaves 0.06-0.22 s after power-off and re-enumerates 0.54-0.59 s after
# power-on, answering ubertooth-util -v about 0.02 s later. Defaults leave
# generous headroom for both.
DEFAULT_REMOVAL_DEADLINE_S = 5.0
DEFAULT_RETURN_DEADLINE_S = 10.0
DEFAULT_POLL_INTERVAL_S = 0.1
# A state must hold this long to count, so a board that is momentarily gone
# because it is re-enumerating is not mistaken for a powered-off board.
DEFAULT_SETTLE_S = 1.0


class EnumerationError(RuntimeError):
    """USB enumeration did not reach the expected state in time."""


class EnumerationAborted(RuntimeError):
    """An enumeration wait was aborted (for example, the run was canceled)."""


class UbertoothEnumerator(Protocol):
    def serials(self) -> frozenset[str]:
        """Return lowercase serials of attached Ubertooths.

        Devices without a serial are returned as UNIDENTIFIED_PREFIX + name.
        """


def is_unidentified(entry: str) -> bool:
    return entry.startswith(UNIDENTIFIED_PREFIX)


class SysfsUbertoothEnumerator:
    """Linux sysfs enumerator; the root is injectable for tests."""

    def __init__(self, root: str | Path = SYSFS_USB_DEVICES):
        self.root = Path(root)

    def serials(self) -> frozenset[str]:
        found = set()
        try:
            entries = list(self.root.iterdir())
        except OSError as error:
            raise EnumerationError(
                f"cannot read USB devices from {self.root}: {error}"
            ) from error
        for device in entries:
            try:
                vendor = (device / "idVendor").read_text().strip().lower()
                product = (device / "idProduct").read_text().strip().lower()
            except (OSError, ValueError):
                # Interfaces have no idVendor; devices can vanish mid-scan.
                continue
            if vendor != UBERTOOTH_VENDOR or product not in UBERTOOTH_PRODUCTS:
                continue
            try:
                serial = (device / "serial").read_text().strip().lower()
            except (OSError, ValueError):
                if not device.exists():
                    continue  # removed mid-scan, not a serial-less device
                serial = ""
            found.add(serial or f"{UNIDENTIFIED_PREFIX}{device.name}:{product}")
        return frozenset(found)


@dataclass(frozen=True)
class EnumerationExpectation:
    """Required enumeration state.

    absent: serials that must be gone. present: serials that must be there.
    exactly: if set, the enumerated set must equal it (anything else,
    including unidentified devices, is unexpected).
    """

    absent: frozenset[str]
    present: frozenset[str]
    exactly: frozenset[str] | None = None

    @classmethod
    def of(cls, absent: Iterable[str] = (), present: Iterable[str] = (),
           exactly: Iterable[str] | None = None):
        absent = frozenset(s.lower() for s in absent)
        present = frozenset(s.lower() for s in present)
        exact = None if exactly is None else frozenset(s.lower() for s in exactly)
        if absent & present:
            raise EnumerationError(
                "a serial cannot be both expected absent and present"
            )
        if exact is not None and (absent & exact or not present <= exact):
            raise EnumerationError("exact set conflicts with absent/present")
        return cls(absent, present, exact)

    def problems(self, seen: frozenset[str]) -> list[str]:
        problems = []
        still = self.absent & seen
        if still:
            problems.append("still enumerated: " + ", ".join(sorted(still)))
        if self.exactly is not None:
            unexpected = seen - self.exactly - self.absent
            if unexpected:
                problems.append(
                    "unexpected Ubertooth enumerated: " + ", ".join(sorted(unexpected))
                )
        missing = self.present - seen
        if missing:
            problems.append("not enumerated: " + ", ".join(sorted(missing)))
        return problems


def wait_for_enumeration(
    enumerator: UbertoothEnumerator,
    expectation: EnumerationExpectation,
    deadline_s: float,
    *,
    settle_s: float = 0.0,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    abort: Callable[[], bool] | None = None,
) -> float:
    """Poll until the expectation holds continuously for settle_s.

    Returns seconds waited; raises EnumerationError at the deadline.
    `abort`, if given, is checked before every scan; when it returns true,
    EnumerationAborted is raised.
    """
    if deadline_s <= 0 or settle_s < 0 or poll_interval_s <= 0:
        raise EnumerationError(
            "deadline and poll interval must be positive; settle must not be negative"
        )
    start = clock()
    held_since = None
    while True:
        if abort is not None and abort():
            raise EnumerationAborted("canceled during the enumeration wait")
        now = clock()
        problems = expectation.problems(enumerator.serials())
        if problems:
            held_since = None
        else:
            held_since = now if held_since is None else held_since
            if now - held_since >= settle_s:
                return clock() - start
        remaining = deadline_s - (clock() - start)
        if remaining <= 0:
            raise EnumerationError(
                f"USB enumeration not reached within {deadline_s:g}s ("
                + "; ".join(problems or ["state did not hold for the settle time"])
                + ")"
            )
        sleep(min(poll_interval_s, remaining))


def stable_serials(
    enumerator: UbertoothEnumerator,
    settle_s: float,
    deadline_s: float,
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    abort: Callable[[], bool] | None = None,
) -> frozenset[str]:
    """Return the enumerated set once it has been unchanged for settle_s.

    `abort`, if given, is checked before every scan; when it returns true,
    EnumerationAborted is raised.
    """
    if deadline_s <= 0 or settle_s < 0 or poll_interval_s <= 0:
        raise EnumerationError(
            "deadline and poll interval must be positive; settle must not be negative"
        )

    def check_abort():
        if abort is not None and abort():
            raise EnumerationAborted("canceled during the enumeration wait")

    start = clock()
    check_abort()
    current = enumerator.serials()
    since = clock()
    while clock() - since < settle_s:
        if clock() - start >= deadline_s:
            raise EnumerationError(
                f"USB enumeration did not settle within {deadline_s:g}s"
            )
        sleep(min(poll_interval_s, settle_s))
        check_abort()
        seen = enumerator.serials()
        if seen != current:
            current, since = seen, clock()
    return current
