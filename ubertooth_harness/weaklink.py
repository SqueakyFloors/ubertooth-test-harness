"""The bench's weak-link receiver, and the checks parameterized by it.

One board on the bench sits away from the others with an inline attenuator
between it and its antenna. The registry records that by hand as its RF
placement (`rf.py`), and a plan can ask for it by placement. Writing the
placement out per role is exact but repetitive, and it hides the one number a
reader of the plan cares about:

```yaml
  - Capture known BLE advertiser:
      rf:
        Ubertooth: {min_attenuation_db: 20, max_attenuation_db: 20, antenna: true}
        advertiser: {max_attenuation_db: 0, antenna: true}
```

`weak link:` is that, said once:

```yaml
  - Capture known BLE advertiser:
      weak link: {Ubertooth: 20}
```

The roles it names must sit at exactly the attenuation given, with their
antennas attached; every other hardware role of the test must be an ordinary
one. "Exactly", not "at least", for the reason the range suite gives: a
figure measured behind 20 dB says nothing about a bench with 10 dB or 40 dB
in the path, so a bench with a different attenuator should be told what the
check needs rather than run against numbers taken elsewhere. Swapping the
attenuator is a one-line plan change, which is what makes the attenuation a
parameter rather than a constant.

The attenuation a run actually used is not re-derived from this argument: the
planner records the placement of every board it bound (`resource_placements`),
and the report quotes it beside the figures, so a report says what its
numbers were taken at even when the bench was rewired since.

## What the bench has measured

20 dB at about a meter is the only setting measured through the harness so
far, and what it establishes is narrow (see `docs/tests.md`, `Two-device
range test`): a round trip stays clean, the required PA level stays inside
the bound the ordinary suite already applies, and a full-band `specan` mean
moves about 1 dB, so mean band RSSI cannot measure an attenuator at all.
What it did not produce is bit errors: ten round trips were clean. Every
check below that turns on error flags, recovery under errors, or an
access-code error threshold therefore needs more attenuation, which is why
`CHECKS` carries the figure each one still needs rather than a threshold
guessed from the 20 dB bench.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

from ubertooth_harness.rf import NORMAL_LINK, PlacementError, PlacementRequirement

# Attenuations measured through the harness, in dB. Only a figure taken at
# one of these is a measurement; the rest are the bench the maintainer can
# fit on request.
MEASURED_ATTENUATIONS_DB: tuple[float, ...] = (20.0,)
# Attenuations the weak-link checks ask for and the bench has not had yet.
PLANNED_ATTENUATIONS_DB: tuple[float, ...] = (30.0, 40.0)


def measured(attenuation_db: float) -> bool:
    """True when the bench has been measured at this attenuation."""
    return float(attenuation_db) in MEASURED_ATTENUATIONS_DB


def weak_receiver(attenuation_db: float) -> PlacementRequirement:
    """The placement of a board behind exactly this much attenuation."""
    return PlacementRequirement(
        antenna=True,
        min_attenuation_db=float(attenuation_db),
        max_attenuation_db=float(attenuation_db),
    )


def weak_link(raw: Mapping) -> Mapping[str, float]:
    """A plan's ``weak link:`` argument: attenuation in dB per hardware role.

    The caller checks the role names against the test's own roles, as it does
    for ``rf:``; here only the shape and the numbers are judged.
    """
    if not isinstance(raw, Mapping) or not raw:
        raise PlacementError(
            "weak link must be a non-empty mapping of hardware role to "
            "attenuation in dB"
        )
    asked = {}
    for role, value in raw.items():
        if not isinstance(role, str) or not role:
            raise PlacementError("a weak link role name must be a non-empty string")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PlacementError(f"weak link {role}: attenuation must be a number")
        attenuation = float(value)
        if not math.isfinite(attenuation):
            raise PlacementError(
                f"weak link {role}: attenuation must be a finite number")
        if attenuation <= 0:
            # 0 dB is an ordinary board, so this would ask for the opposite of
            # a weak link and read as one. A plan that wants a placement this
            # argument cannot say has `rf:`.
            raise PlacementError(
                f"weak link {role}: attenuation must be above 0 dB; an "
                "ordinary board is what every other role gets anyway"
            )
        asked[role] = attenuation
    return MappingProxyType(asked)


def placements(
    asked: Mapping[str, float], hardware_roles
) -> Mapping[str, PlacementRequirement]:
    """What a ``weak link:`` argument requires of each role of a test.

    The roles it names sit at exactly their attenuation; every other hardware
    role the test binds must be an ordinary board. One weak link per test is
    the whole point: a figure from a run where two boards were attenuated
    would not say which attenuation it belonged to.

    A plan that did not ask for a weak link requires nothing, so an empty
    argument gives no requirements rather than demanding ordinary boards
    everywhere.
    """
    if not asked:
        return MappingProxyType({})
    return MappingProxyType({
        role: weak_receiver(asked[role]) if role in asked else NORMAL_LINK
        for role in (hardware_roles or {})
    })


@dataclass(frozen=True)
class Check:
    """One weak-link check: what it reads, and what it still needs measured.

    ``test`` is the catalog test that performs it, ``weak_role`` the role the
    plan attenuates, and ``attenuation_db`` the attenuation at which the
    check is meaningful. ``needs`` is the measurement nobody has taken yet;
    a check with no ``needs`` is one a committed runnable plan may carry.
    """

    item: str
    test: str
    weak_role: str
    attenuation_db: float
    needs: str

    @property
    def pending(self) -> bool:
        return bool(self.needs)

    def describe(self) -> str:
        at = f"{self.attenuation_db:g} dB"
        if not self.pending:
            return f"{self.item}: {self.test} at {at}"
        return f"{self.item}: {self.test} at {at}, pending {self.needs}"


# The weak-link checks the issue approved
# (SqueakyFloors/ubertooth-test-harness#168), each as the catalog test that
# performs it and the attenuation it needs. Every one is an existing test
# pointed at the attenuated board, or one of the two transmit-side entries
# that have no implementation yet, so this table is the list of plan entries
# rather than a list of code to write.
#
# None of them is in a runnable plan: each needs one measurement first, named
# in `needs`. `testplans/weak-link.yaml` carries them all, and
# `docs/tests.md` says per test what a weak link changes for it.
CHECKS: tuple[Check, ...] = (
    Check(
        "HEC and CRC error flags, Classic",
        "Classic capture record fields", "Ubertooth", 40.0,
        "a capture with real bit errors: at 20 dB every round trip was clean, "
        "and the test's floors for headers whose HEC passed were set on a "
        "strong link",
    ),
    Check(
        "HEC and CRC error flags, BLE",
        "Capture known BLE connection", "Ubertooth", 40.0,
        "the same, for the LE records' CRC flag: the test's floor for packets "
        "with a valid CRC was set on a strong link",
    ),
    Check(
        "UAP and clock recovery under errors",
        "Capture known Classic device", "Ubertooth", 40.0,
        "how often libbtbb settles on a wrong UAP when packets are marginal, "
        "which needs marginal packets first",
    ),
    Check(
        "Access-code error threshold (ubertooth-rx -e)",
        "Classic Bluetooth receive", "Ubertooth", 40.0,
        "packets carrying 1 to 5 access-code errors, so a higher -e can be "
        "shown to find more packets without finding false ones",
    ),
    Check(
        "Follow a marginal Classic piconet",
        "Follow Classic Bluetooth piconet", "Ubertooth", 20.0,
        "one capture at 20 dB: the ESP32 pair was held by other work when the "
        "bench was last free",
    ),
    Check(
        "Follow a marginal BLE connection",
        "Follow a BLE connection across its end", "Ubertooth", 20.0,
        "one capture at 20 dB: the DK pair was held by other work when the "
        "bench was last free",
    ),
    Check(
        "Board-to-DK capture with an attenuated receiver",
        "Capture known BLE advertiser", "Ubertooth", 20.0,
        "one measurement that a DK capture still works at 20 dB; without it "
        "the packet floors would be a guess",
    ),
    Check(
        "Transmit power steps",
        "Transmit power steps", "receiver", 20.0,
        "the RSSI step per PA level, which no test reports yet: the range "
        "test reports only the level that got through",
    ),
    Check(
        "Receiver sensitivity",
        "Receiver sensitivity", "receiver", 20.0,
        "a method as much as a figure: which reading the receiver takes from "
        "a held transmitter is open, since the per-packet figures need "
        "packets and the one level-settable sender here sends a carrier",
    ),
)
