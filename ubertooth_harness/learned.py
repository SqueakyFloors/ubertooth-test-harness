"""Durations learned from finished runs: the median of the recent samples.

The catalog's ``ESTIMATES`` are calibrated by hand from a few runs on one
machine, so the GUI's countdown drifts on any other host. This store learns
the real durations from the runs the controller has already recorded: when a
run ends it reads that run's ``run.jsonl``, takes the wall time of every
test invocation that passed (plus the build, preflight and flash stages when
they passed), and keeps the last ``MAX_SAMPLES`` of each key in
``estimates.json`` beside the run directories, written atomically.

Nothing is learned from anywhere else: no new event, no runner change, and
no sample from a failed, skipped, cancelled or not-applicable test, which
ends early or late for reasons that say nothing about how long the test
takes.

A key is the stage, the test name, the target host (a Linux host and a Mac
differ, as do two Linux hosts), the coverage profile, and the value of the
argument the test's duration scales with (``duration``, ``capture
duration``, ``flashes``, or the number of preflight checks), so a 5 s
capture and a 30 s capture are not mixed. A sample is the duration as
measured; the per-unit rate is derived at lookup, which keeps the stored
number exactly what the run took.

The estimate is the median of the key's samples once there are at least
``MIN_SAMPLES``: a median ignores the one run that waited on a busy board.
Until then, a learned rate from another duration of the same test is used
when there is one (the stored samples, less the catalog's fixed part,
divided by their own units), so a test timed at 3 s still estimates a 6 s
run; failing that, the caller falls back to the catalog estimate. A sample
more than ``OUTLIER_FACTOR`` times off the current median is dropped, but
only once the median itself rests on ``MIN_SAMPLES`` samples, so one odd
early run cannot lock out the good ones that follow. And when that many
dropped samples in a row agree with each other, the test's own duration has
changed (a slower host, new firmware, a changed step): those readings become
the window, so no key stays stuck on a value the test no longer takes.
"""

import dataclasses
import datetime
import json
import os
from pathlib import Path
import statistics
import sys
from typing import Callable, Iterable, Mapping, Sequence

from ubertooth_harness.catalog import (
    DEFAULT_PROFILE,
    DURATION_ARGUMENTS,
    CatalogError,
    TestCatalog,
    _duration_seconds,
)


STORE_FILE = "estimates.json"
STORE_VERSION = 1
# Samples kept per key, newest last.
MAX_SAMPLES = 10
# Keys kept at all. Every test, host, coverage profile and duration value
# makes one, so a lab that changes a plan's durations often would otherwise
# grow the file forever. Far above what any plan set needs; when it is
# reached, the least established key makes room.
MAX_KEYS = 1000
# Samples a median must rest on before it is used (and before the outlier
# rule can drop anything).
MIN_SAMPLES = 3
OUTLIER_FACTOR = 10.0
# Longest duration that can be a real sample (a day); anything beyond it is
# a broken timestamp, not a slow test.
MAX_SAMPLE_S = 24 * 3600.0

TEST_STAGE = "test"
BUILD_STAGE = "build"
PREFLIGHT_STAGE = "preflight"
FLASH_STAGE = "flash"
# Stages other than the tests themselves, which the countdown adds up
# separately.
RUN_STAGES = (BUILD_STAGE, PREFLIGHT_STAGE, FLASH_STAGE)
# Units of the preflight stage: one check per selected host and board.
PREFLIGHT_UNITS = "checks"

LEARNED = "learned"
CATALOG = "catalog"
# Neither a learned nor a catalog estimate: nothing to show.
UNKNOWN = "none"


def _log(message: str) -> None:
    print(f"controller: {message}", file=sys.stderr, flush=True)


@dataclasses.dataclass(frozen=True)
class Key:
    """What makes two durations comparable.

    ``name`` is the test name (empty for a stage), ``profile`` the coverage
    profile or a stage variant, and ``units`` the scaling argument and its
    value (``None`` when the test's duration does not scale with one).
    """

    stage: str
    name: str
    host: str
    profile: str = ""
    units: tuple[str, float] | None = None

    @property
    def pool(self) -> tuple[str, str, str, str]:
        """The same measurement at another scale: every field but the units."""
        return (self.stage, self.name, self.host, self.profile)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage, "name": self.name, "host": self.host,
            "profile": self.profile,
            "units": (
                None if self.units is None
                else {"argument": self.units[0], "value": self.units[1]}
            ),
        }


@dataclasses.dataclass(frozen=True)
class Learned:
    """A learned estimate and how many samples it rests on."""

    seconds: float
    samples: int
    # True when the value came from the test's per-unit rate rather than
    # from this key's own median.
    scaled: bool = False


def _duration_units(arguments: Mapping) -> tuple[str, float] | None:
    """The test's duration argument and its value in seconds, if it has one."""
    for name in DURATION_ARGUMENTS:
        value = arguments.get(name)
        if value is None:
            continue
        try:
            # Always a float, so "3s" and "3000ms" are one key.
            seconds = float(_duration_seconds(value))
        except CatalogError:
            return None
        return (name, seconds) if seconds > 0 else None
    return None


def _count_units(name: str, value) -> tuple[str, float] | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return (name, float(value))


def invocation_key(
    catalog: TestCatalog, test_name, arguments, host: str
) -> tuple[Key, float] | None:
    """The key and the catalog's fixed part for one invocation of a test.

    None for a test the catalog does not know or arguments it refuses, as
    the GUI's catalog estimate does.
    """
    if not isinstance(test_name, str) or not isinstance(arguments, Mapping):
        return None
    try:
        definition = catalog.get(test_name)
        normalized = definition.normalize_arguments(arguments)
    except (CatalogError, TypeError, ValueError):
        return None
    profile = normalized.get("coverage") or DEFAULT_PROFILE
    estimate = (definition.estimates or {}).get(profile)
    if estimate is None:
        # No calibrated estimate: the test is expected to run for its
        # duration, and nothing of it is fixed overhead.
        units, fixed_s = _duration_units(normalized), 0.0
    elif estimate.per_duration and estimate.per_count is not None:
        # One key carries one scaling argument. No catalog estimate scales
        # by both, and keying such a test by either alone would mix runs of
        # different lengths, so it is left to the catalog estimate.
        return None
    elif estimate.per_duration:
        units, fixed_s = _duration_units(normalized), estimate.fixed_s
    elif estimate.per_count is not None:
        units = _count_units(estimate.per_count[0],
                             normalized.get(estimate.per_count[0]))
        fixed_s = estimate.fixed_s
    else:
        units, fixed_s = None, estimate.fixed_s
    return Key(TEST_STAGE, test_name, host, profile, units), fixed_s


class EstimateStore:
    """Learned durations, persisted in one small JSON file.

    Loading never fails: an unreadable or malformed file is reported and
    treated as empty, since a missing estimate only costs the countdown its
    accuracy. Saving replaces the file atomically, so a crash mid-save
    leaves the previous content.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        log: Callable[[str], None] = _log,
        max_samples: int = MAX_SAMPLES,
        min_samples: int = MIN_SAMPLES,
        outlier_factor: float = OUTLIER_FACTOR,
        max_keys: int = MAX_KEYS,
    ):
        self.path = Path(path)
        self.log = log
        self.max_samples = max_samples
        self.min_samples = min_samples
        self.outlier_factor = outlier_factor
        self.max_keys = max_keys
        self._samples: dict[Key, list[float]] = {}
        # Samples dropped in a row as outliers, per key: enough of them that
        # agree mean the test's duration has changed, not that the samples
        # are wrong. Not persisted; a restart only asks for them again.
        self._rejected: dict[Key, list[float]] = {}
        self.outliers = 0
        self.updated_at: str | None = None

    # ----- persistence -----------------------------------------------------

    @classmethod
    def load(cls, path: Path | str, **options) -> "EstimateStore":
        store = cls(path, **options)
        store.read()
        return store

    def read(self) -> None:
        self._samples = {}
        self._rejected = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            self.log(f"ignoring unreadable {self.path.name}: {error}")
            return
        if not isinstance(data, Mapping):
            self.log(f"ignoring {self.path.name}: not a JSON object")
            return
        updated = data.get("updated_at")
        self.updated_at = updated if isinstance(updated, str) else None
        entries = data.get("entries")
        if not isinstance(entries, list):
            self.log(f"ignoring {self.path.name}: no entries")
            return
        skipped = 0
        for entry in entries:
            parsed = self._parse_entry(entry)
            if parsed is None:
                skipped += 1
                continue
            key, samples = parsed
            # A repeated key (a hand-edited file) keeps the newest samples.
            self._samples.setdefault(key, []).extend(samples)
            del self._samples[key][: -self.max_samples]
        if skipped:
            self.log(f"{self.path.name}: skipped {skipped} unusable entr(ies)")
        # A file from a larger bound, or a hand-edited one, comes back under
        # it here rather than one key at a time on every later sample.
        self._trim_keys()

    def _parse_entry(self, entry) -> tuple[Key, list[float]] | None:
        if not isinstance(entry, Mapping):
            return None
        stage, name = entry.get("stage"), entry.get("name", "")
        host, profile = entry.get("host"), entry.get("profile", "")
        if not all(isinstance(value, str) for value in (stage, name, host, profile)):
            return None
        if not stage or not host:
            return None
        units = entry.get("units")
        if units is not None:
            if not isinstance(units, Mapping):
                return None
            units = _units_pair(units.get("argument"), units.get("value"))
            if units is None:
                return None
        raw = entry.get("samples")
        if not isinstance(raw, list):
            return None
        samples = [
            value for value in (_sample_seconds(item) for item in raw)
            if value is not None
        ]
        if not samples:
            return None
        return Key(stage, name, host, profile, units), samples[-self.max_samples:]

    def to_dict(self) -> dict:
        return {
            "version": STORE_VERSION,
            "updated_at": self.updated_at,
            "entries": [
                {**key.to_dict(), "samples": list(samples)}
                for key, samples in sorted(
                    self._samples.items(),
                    key=lambda item: (item[0].stage, item[0].name, item[0].host,
                                      item[0].profile, str(item[0].units)),
                )
                if samples
            ],
        }

    def save(self) -> bool:
        """Replace the file atomically; a failure is logged, never raised.

        False when nothing was written, so a caller that is about to record
        what the file holds can leave the work for another attempt instead.
        """
        self.updated_at = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds")
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(self.to_dict(), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            self.log(f"could not write {self.path}: {error}")
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True

    # ----- samples ---------------------------------------------------------

    def copy(self) -> "EstimateStore":
        """An independent copy, so learning in a worker thread cannot race
        a lookup serving an API request."""
        other = EstimateStore(
            self.path, log=self.log, max_samples=self.max_samples,
            min_samples=self.min_samples, outlier_factor=self.outlier_factor,
            max_keys=self.max_keys,
        )
        other._samples = {key: list(values) for key, values in self._samples.items()}
        other._rejected = {key: list(values) for key, values in self._rejected.items()}
        other.outliers = self.outliers
        other.updated_at = self.updated_at
        return other

    def samples(self, key: Key) -> list[float]:
        return list(self._samples.get(key, ()))

    def keys(self) -> list[Key]:
        return list(self._samples)

    def add(self, key: Key, seconds) -> bool:
        """Keep one sample unless it is unusable or an outlier."""
        value = _sample_seconds(seconds)
        if value is None:
            return False
        if key not in self._samples:
            self._make_room()
        samples = self._samples.setdefault(key, [])
        if len(samples) >= self.min_samples:
            median = statistics.median(samples)
            if median > 0 and (
                value > median * self.outlier_factor
                or value * self.outlier_factor < median
            ):
                rejected = self._rejected.setdefault(key, [])
                rejected.append(value)
                del rejected[: -self.min_samples]
                # Enough rejected samples that agree with each other mean
                # the test itself now takes that long (a slower host, new
                # firmware, a changed step), not that the samples are wrong.
                # Readings that disagree are still just outliers.
                if (
                    len(rejected) < self.min_samples
                    or max(rejected) > min(rejected) * self.outlier_factor
                ):
                    self.outliers += 1
                    self.log(
                        f"estimates: dropped a {value:g}s outlier for "
                        f"{key.stage} {key.name or key.host} (median {median:g}s)"
                    )
                    return False
                self.log(
                    f"estimates: {key.stage} {key.name or key.host} now takes "
                    f"about {value:g}s, not {median:g}s; starting again"
                )
                # Those readings are the evidence of the new duration, so
                # they become the window rather than being thrown away.
                samples[:] = rejected
                self._rejected.pop(key, None)
                return True
        self._rejected.pop(key, None)
        samples.append(value)
        del samples[: -self.max_samples]
        return True

    def _make_room(self) -> None:
        """Keep the store bounded before a new key is added."""
        while len(self._samples) >= self.max_keys:
            self._drop_least_established()

    def _trim_keys(self) -> None:
        """Bring a file that holds more keys than the bound back under it."""
        while len(self._samples) > self.max_keys:
            self._drop_least_established()

    def _drop_least_established(self) -> None:
        # Fewest samples first, then the key added longest ago (the
        # dictionary keeps its insertion order), so an established key is
        # never dropped to make room for a one-off duration.
        _, key = min(
            enumerate(self._samples),
            key=lambda item: (len(self._samples[item[1]]), item[0]),
        )
        del self._samples[key]
        self._rejected.pop(key, None)
        self.log(
            f"estimates: {self.path.name} is full at {self.max_keys} keys; "
            f"dropped {key.stage} {key.name or key.host}"
        )

    # ----- lookup ----------------------------------------------------------

    def lookup(self, key: Key, *, fixed_s: float = 0.0) -> Learned | None:
        """The learned estimate for this key, or None when too little is known."""
        samples = self._samples.get(key, ())
        if len(samples) >= self.min_samples:
            return Learned(round(statistics.median(samples), 1), len(samples))
        if key.units is None or key.units[1] <= 0:
            return None
        # Too few samples at this duration: use the rate learned across
        # every duration of the same test, above the catalog's fixed part.
        # This duration's own one or two samples count towards that rate;
        # they are the closest evidence there is.
        # A sample shorter than that fixed part contradicts it and can say
        # nothing about the rate, so it is left out rather than counted as
        # no time at all.
        fixed = max(0.0, float(fixed_s))
        rates = [
            (value - fixed) / other.units[1]
            for other, values in self._samples.items()
            if other.pool == key.pool
            and other.units is not None
            and other.units[0] == key.units[0]
            and other.units[1] > 0
            for value in values
            if value > fixed
        ]
        if len(rates) < self.min_samples:
            return None
        seconds = fixed + statistics.median(rates) * key.units[1]
        return Learned(round(seconds, 1), len(rates), scaled=True)

    def lookup_test(
        self, catalog: TestCatalog, test_name, arguments, host: str
    ) -> Learned | None:
        """The learned estimate of one test invocation on ``host``."""
        if not host:
            return None
        found = invocation_key(catalog, test_name, arguments, host)
        if found is None:
            return None
        key, fixed_s = found
        return self.lookup(key, fixed_s=fixed_s)

    def lookup_stage(
        self, stage: str, host: str, *, profile: str = "", units=None
    ) -> Learned | None:
        """The learned estimate of a build, preflight or flash stage."""
        if not host:
            return None
        return self.lookup(Key(stage, "", host, profile, _units_tuple(units)))

    def entries(
        self, *, host: str | None = None, stages: Iterable[str] | None = None
    ) -> list[dict]:
        """What has been learned, as the API reports it (newest sample last)."""
        wanted = None if stages is None else set(stages)
        views = []
        for entry in self.to_dict()["entries"]:
            if host is not None and entry["host"] != host:
                continue
            if wanted is not None and entry["stage"] not in wanted:
                continue
            samples = entry["samples"]
            enough = len(samples) >= self.min_samples
            median = round(statistics.median(samples), 1) if enough else None
            units = entry["units"]
            views.append({
                **{name: entry[name]
                   for name in ("stage", "name", "host", "profile", "units")},
                "estimated_s": median,
                "estimate_source": LEARNED if enough else UNKNOWN,
                "estimate_samples": len(samples),
                # Each entry is one stored key, so never scaled.
                "estimate_scaled": False,
                # Seconds per unit of a stage, which has no fixed part. A
                # test's fixed part is the catalog's, not the store's, so
                # dividing its median by the units would be a rate of
                # nothing in particular.
                "per_unit_s": (
                    round(median / units["value"], 3)
                    if median is not None and units and units["value"] > 0
                    and entry["stage"] != TEST_STAGE else None
                ),
            })
        return views

    # ----- learning from a finished run ------------------------------------

    def learn_from_run(
        self,
        events: Sequence[Mapping],
        *,
        host: str,
        catalog: TestCatalog | None = None,
        save: bool = True,
    ) -> int:
        """Take every clean sample from one finished run's records.

        ``host`` is the run's target; a test invocation's own host wins when
        its record names one. Returns how many samples were kept, and saves
        the store when any were.
        """
        if catalog is None:
            from ubertooth_harness.catalog import builtin_catalog

            catalog = builtin_catalog()
        kept = 0
        started: dict[str, tuple[datetime.datetime, Mapping]] = {}
        stages: dict[str, tuple[datetime.datetime, Mapping]] = {}
        for record in events:
            if not isinstance(record, Mapping):
                continue
            kind = record.get("event_type")
            status = record.get("status")
            details = record.get("details")
            details = details if isinstance(details, Mapping) else {}
            moment = _timestamp(record.get("timestamp"))
            if kind == "test_started":
                invocation = details.get("invocation_id")
                if moment is not None and isinstance(invocation, str):
                    started[invocation] = (moment, details)
            elif kind == "test_finished":
                invocation = details.get("invocation_id")
                if not isinstance(invocation, str):
                    continue
                opened = started.pop(invocation, None)
                if opened is None or status != "passed":
                    continue
                begin, opening = opened
                seconds = _elapsed(begin, moment)
                found = invocation_key(
                    catalog,
                    opening.get("test_name") or details.get("test_name"),
                    opening.get("arguments"),
                    _host(opening.get("host"), host),
                )
                if seconds is not None and found is not None:
                    kept += self.add(found[0], seconds)
            elif kind in ("build_started", "preflight_started"):
                if moment is not None:
                    stages[kind.removesuffix("_started")] = (moment, details)
            elif kind in ("build_finished", "preflight_finished"):
                stage = kind.removesuffix("_finished")
                opened = stages.pop(stage, None)
                if opened is None or status != "passed":
                    continue
                seconds = _elapsed(opened[0], moment)
                if seconds is None:
                    continue
                if stage == BUILD_STAGE:
                    kept += self.add(
                        Key(BUILD_STAGE, "", host, _build_profile(opened[1])), seconds
                    )
                    continue
                checks = _count_units(
                    PREFLIGHT_UNITS,
                    details.get("requested_checks")
                    or opened[1].get("requested_checks"),
                )
                # Preflight is only ever looked up for a number of checks,
                # so a sample without one could never be read back.
                if checks is not None:
                    kept += self.add(Key(PREFLIGHT_STAGE, "", host, "", checks), seconds)
            elif kind == "flash_run_finished" and status == "passed":
                # The flash runner times itself, so no bracketing record is
                # needed: its summary carries the duration and the flashes.
                # Without a usable count there is nothing to key it by, and
                # pooling runs of two flashes with runs of fifty would be
                # worse than not learning it.
                units = _count_units("flashes", details.get("requested_flashes"))
                if units is not None:
                    kept += self.add(
                        Key(FLASH_STAGE, "", host, "", units), details.get("duration_s")
                    )
        if kept and save:
            self.save()
        return kept


def _units_tuple(units) -> tuple[str, float] | None:
    """Accept units as a tuple, a mapping, or None."""
    if units is None:
        return None
    if isinstance(units, Mapping):
        return _units_pair(units.get("argument"), units.get("value"))
    if isinstance(units, Sequence) and len(units) == 2:
        return _units_pair(units[0], units[1])
    return None


def _units_pair(argument, value) -> tuple[str, float] | None:
    """One units pair: the argument's name and a positive number of units."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not isinstance(argument, str) or not argument:
        return None
    if number != number or number <= 0 or number == float("inf"):
        return None
    return (argument, number)


# The profile a build is learned under, by the two options that add work to
# it: the firmware image (`run-suite --with-firmware`) and the Python tools
# (`--with-python`). Every build makes the host tools, so the plain one is
# named for them. The two profiles without the Python tools keep the names
# they have always had, so the samples already stored stay where they were
# learned and the two new profiles fill from the runs that follow.
BUILD_PROFILES = {
    (False, False): "host tools",
    (False, True): "host tools and Python tools",
    (True, False): "with firmware",
    (True, True): "with firmware and Python tools",
}


def build_profile(with_firmware, with_python=False) -> str:
    """The profile a build is learned under.

    The firmware image and the Python tools are each real extra work (the
    firmware much the larger of the two), so all four combinations are
    learned apart and an estimate is only ever shown for the combination a
    run actually builds.
    """
    return BUILD_PROFILES[(bool(with_firmware), bool(with_python))]


def _build_profile(details: Mapping) -> str:
    # A record from before an option existed does not name it, which reads
    # as false: that is what such a build did.
    return build_profile(details.get("with_firmware"), details.get("with_python"))


def _host(value, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _sample_seconds(value) -> float | None:
    """A usable duration in seconds, or None (never an exception).

    Zero is not usable: the records have millisecond resolution, so a
    measured 0 means the pair of records is wrong, and three of them would
    give a learned estimate of nothing at all.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number <= 0 or number > MAX_SAMPLE_S:
        return None
    return number


def _timestamp(value) -> datetime.datetime | None:
    """A run record's UTC timestamp (no offset suffix), or None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def _elapsed(
    begin: datetime.datetime, end: datetime.datetime | None
) -> float | None:
    if end is None:
        return None
    seconds = (end - begin).total_seconds()
    return seconds if seconds >= 0 else None
