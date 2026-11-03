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

## What the bench has been measured at

Both receivers have been measured, and a runnable plan may point a test at
either. The figures come from two sessions, both with one firmware revision
across the boards compared, because the bench's receivers are routinely left
on different images and an A/B that ignores that measures the firmware.

**The range test** (2026-11-02, six rounds per responder, one ordinary
sender): the responder's reply PA level, which is what the attenuated board
must transmit at to be heard, reaches 1 behind 20 dB and 3 behind 40 dB, with
every round a clean round trip. Reply level 3 is `maximum PA level`'s own
default bound, so a 40 dB range entry has to raise that argument rather than
inherit it. The copy numbers run over most of their 0 to 15 range at both, so
they discriminate nothing at that sample size. Earlier, at 20 dB only
(harness PR #217): a full-band `specan` mean moved 1.1 dB between the
attenuated board and an ordinary one with 0.1 dB of spread, so a band mean
cannot resolve a per-level step.

**Captures over the link**, which the range test could never speak for, come
from the attenuated device sweep (2026-11-02, nine interleaved Classic legs,
three BLE legs and four AFH legs; `docs/tests.md` carries the tables per
test):

* a known Classic piconet is decoded at both, with every packet carrying the
  target LAP and none carrying another, and the clock acquired at 20 and
  40 dB;
* `-e` is honoured exactly at both: an `-e 0` leg holds `err=0` records and
  nothing else, `-e 2` tops out at 2 and `-e 4` at 4;
* **40 dB is not an error regime.** Its error profile sits close to 20 dB's
  (74% against 77% `err=0` at `-e 2`) on a comparable packet count while
  reading about 14 dB lower, so the link still has margin there. Damaged
  packets in quantity would need more attenuation than the bench now has;
* a known BLE advertiser is captured completely at both, about 1175 and 1228
  advertisements with **no** CRC failure, and `tshark` agrees with the tool's
  own count exactly;
* AFH map recovery is *better* attenuated: the 20 dB board recovered a known
  38-channel map exactly, 0 false and 0 missed, where both clear boards
  over-marked by 11 to 15 channels. 40 dB begins to erode the other way,
  losing two edge channels;
* neither attenuated board produced a pegged record in 6861 packets, where a
  clear board pegged on 23 to 41% of its own.

One measured caveat sits in the code below rather than in prose: at 20 dB,
`-e 4` collapsed to single-digit packet counts twice, minutes after the same
board returned 2692 at `-e 2` and while the 40 dB board returned 2376 at
`-e 4`. It is unexplained and fork-side, so no weak-link entry asks for `-e 4`
at 20 dB.

`measured()` and `unjudgeable()` are what give this force: `check_runnable`
and `check_suite_runnable` refuse a plan or suite that points a test at an
attenuation outside `MEASURED_ATTENUATIONS_DB`, or at a band rather than one
figure, so an entry cannot quietly judge its old floors over a link nobody
has characterized.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

from ubertooth_harness.rf import NORMAL_LINK, PlacementError, PlacementRequirement

# Attenuations the bench has been MEASURED at, in dB. A runnable plan may
# only point a test at one of these (plan.check_runnable).
#
# Fitted is not measured, and this list tracks the second. Both of these have
# had captures taken over them, not just a range round trip, which is what
# lets a capture test be judged there (see the section above). The
# maintainer's attenuators stack in 1, 3, 5, 10, 20 and 30 dB steps, so these
# are the points measured so far rather than fixed limits.
MEASURED_ATTENUATIONS_DB: tuple[float, ...] = (20.0, 40.0)
# Asked for and still uncharacterized: the second point that would turn the
# 20-against-40 figures into a curve.
PLANNED_ATTENUATIONS_DB: tuple[float, ...] = (30.0,)


def measured(attenuation_db: float) -> bool:
    """True when the bench has been measured at this attenuation.

    This is what keeps an unmeasured attenuation out of a runnable plan
    (``unmeasured``): a test's floors were set on whatever link it last ran
    over, so pointing it at an attenuation nobody has measured would judge it
    against figures from somewhere else.
    """
    return float(attenuation_db) in MEASURED_ATTENUATIONS_DB


def unmeasured(attenuations_db) -> tuple[float, ...]:
    """Those of these attenuations the bench has not been measured at.

    Sorted, each once, so a refusal names them in a stable order.
    """
    return tuple(sorted({
        float(value) for value in attenuations_db if not measured(value)
    }))


def unjudgeable(want: PlacementRequirement) -> str:
    """Why no run could judge a test bound by this requirement, or ''.

    A run's figures belong to the link the board actually sat on, so a
    requirement a run may judge has to leave exactly one attenuated link
    possible, at a figure the bench has been measured at. Three cases:

    * it admits no attenuation at all (an ordinary board, the baseline every
      test already runs over) — judgeable;
    * it pins one attenuation, which must be a measured one;
    * it leaves a band open above 0 dB, so which link a run used would depend
      on which board the bench happens to hold. ``weak link:`` always pins,
      but ``rf:`` can write a bare ``min_attenuation_db``, and on a bench
      that later gains a stronger attenuator that would quietly bind it.
    """
    low, high = want.min_attenuation_db, want.max_attenuation_db
    if high is not None and high <= 0:
        return ""
    if low is not None and low == high:
        return "" if measured(low) else f"needs {low:g} dB, which is not measured"
    bound = []
    if low is not None:
        bound.append(f"at least {low:g} dB")
    if high is not None:
        bound.append(f"at most {high:g} dB")
    return (
        "does not pin the attenuation ("
        + (" and ".join(bound) or "any attenuation")
        + "), so which link a run judged would depend on the bench; give one "
        "figure the bench is measured at"
    )


def describe_attenuations(attenuations_db) -> str:
    """``20 dB``, ``30 and 40 dB``, ``20, 30 and 40 dB``."""
    figures = [f"{value:g}" for value in attenuations_db]
    if not figures:
        return "no attenuation"
    if len(figures) == 1:
        return f"{figures[0]} dB"
    return ", ".join(figures[:-1]) + f" and {figures[-1]} dB"


def weak_receiver(attenuation_db: float) -> PlacementRequirement:
    """The placement of a board behind exactly this much attenuation."""
    return PlacementRequirement(
        antenna=True,
        min_attenuation_db=float(attenuation_db),
        max_attenuation_db=float(attenuation_db),
    )


def weak_link(raw: Mapping) -> Mapping[str, float]:
    """A plan's ``weak link:`` argument: one role, at an attenuation in dB.

    Exactly one role, because a figure from a run where two boards were
    attenuated would not say which attenuation it belonged to. A plan that
    really wants two placements has ``rf:``, which says so plainly.

    The caller checks the role name against the test's own roles, as it does
    for ``rf:``; here only the shape and the number are judged.
    """
    if not isinstance(raw, Mapping) or not raw:
        raise PlacementError(
            "weak link must be a non-empty mapping of hardware role to "
            "attenuation in dB"
        )
    if len(raw) > 1:
        raise PlacementError(
            "weak link names one role, not "
            + ", ".join(sorted(str(role) for role in raw))
            + "; every other role is required to be an ordinary board, and a "
            "figure from two attenuated boards belongs to neither "
            "attenuation. Use rf: to ask for two placements"
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

    The one role it names sits at exactly its attenuation; every other
    hardware role the test binds must be an ordinary board.

    A plan that did not ask for a weak link requires nothing, so an empty
    argument gives no requirements rather than demanding ordinary boards
    everywhere. A role the test does not bind is caught by the caller, which
    knows the test's roles; reaching here with one would drop the weak link
    silently, so it is refused instead.
    """
    if not asked:
        return MappingProxyType({})
    roles = tuple(hardware_roles or {})
    unknown = sorted(set(asked) - set(roles))
    if unknown:
        raise PlacementError(
            f"weak link names role(s) the test does not bind: "
            + ", ".join(unknown)
        )
    return MappingProxyType({
        role: weak_receiver(asked[role]) if role in asked else NORMAL_LINK
        for role in roles
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
        "Board-to-DK capture with an attenuated receiver",
        "Capture known BLE advertiser", "Ubertooth", 20.0, "",
    ),
    Check(
        "Follow a marginal Classic piconet",
        "Follow Classic Bluetooth piconet", "Ubertooth", 20.0, "",
    ),
    Check(
        "AFH map recovery on a weak link",
        "Recover AFH map", "Ubertooth", 20.0, "",
    ),
    Check(
        "UAP and clock recovery under errors",
        "Capture known Classic device", "Ubertooth", 40.0, "",
    ),
    Check(
        "Access-code error threshold (ubertooth-rx -e)",
        "Classic Bluetooth receive", "Ubertooth", 40.0, "",
    ),
    Check(
        "HEC and CRC error flags, Classic",
        "Classic capture record fields", "Ubertooth", 40.0,
        "a capture whose records a flag actually fired on. The sweep decoded "
        "packets at 40 dB but wrote no capture file, and 40 dB is not an "
        "error regime on this bench anyway (74% against 77% err=0), so the "
        "flags may simply not fire; more attenuation than the bench has may "
        "be needed, and a case that requires a flag to have fired does not "
        "exist yet",
    ),
    Check(
        "HEC and CRC error flags, BLE",
        "Capture known BLE connection", "Ubertooth", 40.0,
        "a connection to capture: only one registered DK declares central and "
        "peripheral, so no pair can be bound on this bench",
    ),
    Check(
        "Follow a marginal BLE connection",
        "Follow a BLE connection across its end", "Ubertooth", 20.0,
        "the same single DK: following a connection across its end needs two",
    ),
    Check(
        "Transmit power steps",
        "Transmit power steps", "receiver", 20.0,
        "an implementation, and the RSSI step per PA level it would judge. "
        "The sweep's per-source asymmetry is the warning: the Classic peer "
        "separated the two receivers by about 14 dB while the DK separated "
        "them by 1 dB, so a step figure belongs to a source and a geometry, "
        "not to the attenuator alone",
    ),
    Check(
        "Receiver sensitivity",
        "Receiver sensitivity", "receiver", 20.0,
        "a method as much as a figure: which reading the receiver takes from "
        "a held transmitter is open, since the per-packet figures need "
        "packets and the one level-settable sender here sends a carrier. The "
        "sweep's RSSI means are explicitly not sensitivity",
    ),
)
