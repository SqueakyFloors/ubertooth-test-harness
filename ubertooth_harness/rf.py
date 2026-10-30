"""Where a unit sits on the bench, and what a test needs of that placement.

A unit's RF placement is set by hand in the registry and describes the bench;
the harness never measures it. It says whether the antenna is attached, how
much inline attenuation sits between the unit and the air, and how far the
unit is from the rest of the bench:

```yaml
  UT_Linux2:
    type: Ubertooth
    attached to: Host_Linux
    rf:
      antenna: true
      attenuation_db: 20
      distance_m: 1
```

A unit with no ``rf:`` is taken to be an ordinary one on the bench: antenna
attached, no attenuation, distance not recorded. That default is what lets a
test ask for a normal receiver with ``max_attenuation_db: 0``.

A test asks for a placement with ``rf:`` in the plan, one requirement per
hardware role:

```yaml
  - Classic Bluetooth receive:
      rf:
        Ubertooth: {min_attenuation_db: 10}
```

A test whose cases need a normal link (a capture, a survey, following a live
connection) declares that requirement as its own default
(``TestDefinition.rf_defaults``), so the planner keeps it off an attenuated
board without every plan having to say so. The plan's ``rf:`` overrides the
default, role by role.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping


class PlacementError(ValueError):
    """An ``rf:`` placement or an ``rf:`` requirement is invalid."""


PLACEMENT_KEYS = ("antenna", "attenuation_db", "distance_m")
REQUIREMENT_KEYS = ("antenna", "min_attenuation_db", "max_attenuation_db")


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlacementError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise PlacementError(f"{name} must be a finite number")
    return number


def _decibels(value, name: str) -> float:
    number = _number(value, name)
    if number < 0:
        raise PlacementError(f"{name} must not be negative")
    return number


def _flag(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise PlacementError(f"{name} must be true or false")
    return value


def _unknown(raw: Mapping, allowed, what: str) -> None:
    extra = sorted(str(key) for key in set(raw) - set(allowed))
    if extra:
        raise PlacementError(
            f"{what} has unknown key(s) {', '.join(extra)}; use "
            + ", ".join(allowed)
        )


@dataclass(frozen=True)
class Placement:
    """One unit's RF placement, exactly as the registry records it."""

    antenna: bool = True
    attenuation_db: float = 0.0
    # None when the registry does not say how far the unit is.
    distance_m: float | None = None

    def describe(self) -> str:
        parts = [f"{self.attenuation_db:g} dB"]
        parts.append("antenna" if self.antenna else "no antenna")
        if self.distance_m is not None:
            parts.append(f"{self.distance_m:g} m away")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        data = {"antenna": self.antenna, "attenuation_db": self.attenuation_db}
        if self.distance_m is not None:
            data["distance_m"] = self.distance_m
        return data


# What a unit with no ``rf:`` is taken to be.
DEFAULT_PLACEMENT = Placement()


def placement(raw: Mapping) -> Placement:
    """One unit's ``rf:`` mapping, with the defaults of an ordinary unit."""
    if not isinstance(raw, Mapping):
        raise PlacementError("rf must be a mapping")
    _unknown(raw, PLACEMENT_KEYS, "rf")
    distance = raw.get("distance_m")
    if distance is not None:
        distance = _number(distance, "rf distance_m")
        if distance <= 0:
            raise PlacementError("rf distance_m must be positive")
    return Placement(
        antenna=_flag(raw.get("antenna", True), "rf antenna"),
        attenuation_db=_decibels(raw.get("attenuation_db", 0), "rf attenuation_db"),
        distance_m=distance,
    )


def describe(recorded: Placement | Mapping | None) -> str:
    """Plain text for a placement, a recorded one, or none at all.

    A recorded placement may come from a run directory written by another
    version of the harness, so one this version cannot read is shown as it
    stands rather than stopping a report.
    """
    if recorded is None:
        return f"{DEFAULT_PLACEMENT.describe()} (not recorded)"
    if isinstance(recorded, Placement):
        return recorded.describe()
    if not isinstance(recorded, Mapping):
        return str(recorded)
    try:
        return placement(recorded).describe()
    except PlacementError:
        return ", ".join(f"{key}={value}" for key, value in sorted(recorded.items()))


@dataclass(frozen=True)
class PlacementRequirement:
    """What a test needs of the placement of the unit bound to one role."""

    # True: the antenna must be attached; False: it must not be.
    antenna: bool | None = None
    min_attenuation_db: float | None = None
    max_attenuation_db: float | None = None

    def __post_init__(self):
        low, high = self.min_attenuation_db, self.max_attenuation_db
        if low is not None and high is not None and low > high:
            raise PlacementError(
                f"rf min_attenuation_db {low:g} is above max_attenuation_db {high:g}"
            )

    def __bool__(self) -> bool:
        return any(
            value is not None
            for value in (
                self.antenna, self.min_attenuation_db, self.max_attenuation_db
            )
        )

    def describe(self) -> str:
        low, high = self.min_attenuation_db, self.max_attenuation_db
        parts = []
        if low is not None and low == high:
            parts.append(f"attenuation of exactly {low:g} dB")
        else:
            if low is not None:
                parts.append(f"attenuation of at least {low:g} dB")
            if high is not None:
                parts.append(f"attenuation of at most {high:g} dB")
        if self.antenna is True:
            parts.append("its antenna attached")
        elif self.antenna is False:
            parts.append("no antenna attached")
        return " and ".join(parts) or "any placement"

    def matches(self, candidate: Placement | None) -> bool:
        """True when a unit with this placement (or none) can take the role."""
        placed = DEFAULT_PLACEMENT if candidate is None else candidate
        if self.antenna is not None and placed.antenna is not self.antenna:
            return False
        if (
            self.min_attenuation_db is not None
            and placed.attenuation_db < self.min_attenuation_db
        ):
            return False
        return (
            self.max_attenuation_db is None
            or placed.attenuation_db <= self.max_attenuation_db
        )

    def to_dict(self) -> dict:
        return {
            name: value
            for name, value in (
                ("antenna", self.antenna),
                ("min_attenuation_db", self.min_attenuation_db),
                ("max_attenuation_db", self.max_attenuation_db),
            )
            if value is not None
        }


# A unit in the open at bench distance: what a capture, a survey, or a live
# connection needs.
NORMAL_LINK = PlacementRequirement(antenna=True, max_attenuation_db=0)


def requirement(raw: Mapping) -> PlacementRequirement:
    """One role's ``rf:`` requirement from a plan."""
    if not isinstance(raw, Mapping) or not raw:
        raise PlacementError("an rf requirement must be a non-empty mapping")
    _unknown(raw, REQUIREMENT_KEYS, "an rf requirement")
    antenna = raw.get("antenna")
    bounds = {}
    for name in ("min_attenuation_db", "max_attenuation_db"):
        if raw.get(name) is not None:
            bounds[name] = _decibels(raw[name], f"rf {name}")
    asked = PlacementRequirement(
        antenna=None if antenna is None else _flag(antenna, "rf antenna"),
        **bounds,
    )
    if not asked:
        # Every key empty (`min_attenuation_db:` with nothing after it) would
        # otherwise read as "any placement" and quietly drop the test's own
        # requirement as well, so a weak-link plan would select any board.
        raise PlacementError(
            "an rf requirement must set at least one of " + ", ".join(REQUIREMENT_KEYS)
        )
    return asked


def requirements(raw: Mapping) -> Mapping[str, PlacementRequirement]:
    """A plan's ``rf:`` argument: one requirement per hardware role."""
    if not isinstance(raw, Mapping) or not raw:
        raise PlacementError(
            "rf must be a non-empty mapping of hardware role to requirement"
        )
    result = {}
    for role, value in raw.items():
        if not isinstance(role, str) or not role:
            raise PlacementError("an rf role name must be a non-empty string")
        try:
            result[role] = requirement(value)
        except PlacementError as error:
            raise PlacementError(f"rf {role}: {error}") from error
    return result
