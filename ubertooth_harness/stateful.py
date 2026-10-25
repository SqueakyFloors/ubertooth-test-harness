"""State-changing transition cases, coverage profiles, and option pairing.

A transition case is an ordered list of steps followed by mandatory cleanup.
Steps may observe a device setting and either record it (for later
restoration) or compare it with an expected value derived from earlier
observations. Cleanup always runs, even after failure or cancellation, and a
cleanup failure always fails the case.
"""

from dataclasses import dataclass, field
from itertools import combinations
import re
from typing import Callable, Mapping, Sequence

from ubertooth_harness.hostneeds import NO_NEEDS, Needs

COVERAGE_PROFILES = ("smoke", "comprehensive")
# Run-directory subdirectory for files cases write, and its context key.
CAPTURE_DIR = "captures"
CAPTURE_DIR_KEY = "capture dir"
EXPECTED_REVISION_KEY = "expected firmware revision"


def artifact_key(label: str) -> str:
    """Context key of a manifest artifact's path, by image label."""
    return f"artifact {label}"

_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,95}$")

Context = Mapping[str, str]
ArgvSpec = Sequence[str] | Callable[[Context], Sequence[str]]
ExpectedSpec = str | Callable[[Context], str] | None


class StateCaseError(ValueError):
    """A transition case definition is invalid."""


class MissingContext(LookupError):
    """A step needs a value that an earlier step did not record."""


class Unrestorable(LookupError):
    """A recorded value cannot be written back by any host tool.

    The step is reported as skipped with the reason; it is not a cleanup
    failure, because no supported command can restore the value.
    """


def coverage_profile(value) -> str:
    if value not in COVERAGE_PROFILES:
        raise ValueError(
            "coverage must be one of: " + ", ".join(COVERAGE_PROFILES)
        )
    return value


def require(context: Context, key: str) -> str:
    try:
        return context[key]
    except KeyError as error:
        raise MissingContext(key) from error


@dataclass(frozen=True)
class Observation:
    """Extract one setting from output and optionally compare it."""

    key: str
    pattern: str
    expected: ExpectedSpec = None
    values: Mapping[str, str] | None = None
    reject: Callable[[str], str] | None = None
    convert: Callable[[re.Match], str] | None = None

    def __post_init__(self):
        if self.convert is None and "(?P<value>" not in self.pattern:
            raise StateCaseError(
                f"observation {self.key} pattern needs a (?P<value>...) group"
            )

    def expected_value(self, context: Context) -> str | None:
        if callable(self.expected):
            return str(self.expected(context))
        return self.expected

    def extract(self, output: str) -> str | None:
        match = re.search(self.pattern, output)
        if match is None:
            return None
        if self.convert is not None:
            return self.convert(match)
        value = match.group("value")
        if self.values is not None:
            return self.values.get(value, value)
        return value


@dataclass(frozen=True)
class StepOutcome:
    failure: str
    expected: str | None = None
    observed: str | None = None
    skipped: str = ""


@dataclass(frozen=True)
class Step:
    """One exact command in a transition case.

    ``continuous`` steps start a mode that runs until the harness stops it at
    ``timeout_s``; reaching that deadline is the expected outcome, and an early
    exit is a failure.
    """

    step_id: str
    name: str
    argv: ArgvSpec
    timeout_s: float = 5.0
    expected_returncodes: tuple[int, ...] = (0,)
    output_patterns: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    observe: Observation | None = None
    mutates: bool = False
    continuous: bool = False
    # Returns a reason to fail the step without running its command (for
    # example a query whose tool would write to an incompatible device).
    refuse: Callable[[Context], str] | None = None
    # Checks stdout (stderr is diagnostics) and returns a failure reason,
    # or "" when it is acceptable; for outputs too varied for patterns, such
    # as a spectrum sweep.
    output_check: Callable[[str], str] | None = None
    # A command that should stop itself after a timeout of its own (for
    # example ubertooth-rx -t N) must not exit before this many seconds.
    min_duration_s: float = 0.0

    def resolve_argv(self, context: Context) -> tuple[str, ...]:
        value = self.argv(context) if callable(self.argv) else self.argv
        command = tuple(str(item) for item in value)
        if not command:
            raise StateCaseError(f"{self.step_id}: empty command")
        return command

    def evaluate(self, result, context: Context) -> StepOutcome:
        expected = None
        if self.observe is not None:
            try:
                expected = self.observe.expected_value(context)
            except MissingContext as error:
                return StepOutcome(f"{self.name}: missing recorded {error.args[0]}")
        if result.cancelled:
            return StepOutcome(f"{self.name}: command canceled", expected)
        if self.continuous:
            if not result.timed_out:
                return StepOutcome(
                    f"{self.name}: mode exited {result.returncode} before the "
                    "harness stopped it",
                    expected,
                )
        elif result.timed_out:
            return StepOutcome(f"{self.name}: command timed out", expected)
        elif result.returncode not in self.expected_returncodes:
            return StepOutcome(
                f"{self.name}: command exited {result.returncode}", expected
            )
        elif result.duration_s < self.min_duration_s:
            return StepOutcome(
                f"{self.name}: exited after {result.duration_s:.1f}s, before "
                f"its {self.min_duration_s:g}s minimum",
                expected,
            )
        output = result.stdout + result.stderr
        for pattern in self.output_patterns:
            if not re.search(pattern, output):
                return StepOutcome(
                    f"{self.name}: expected output pattern {pattern!r} was not found",
                    expected,
                )
        for pattern in self.forbidden_patterns:
            if re.search(pattern, output):
                return StepOutcome(
                    f"{self.name}: forbidden output pattern {pattern!r} was found",
                    expected,
                )
        if self.output_check is not None:
            reason = self.output_check(result.stdout)
            if reason:
                return StepOutcome(f"{self.name}: {reason}", expected)
        if self.observe is None:
            return StepOutcome("")
        observed = self.observe.extract(output)
        if observed is None:
            return StepOutcome(
                f"{self.name}: {self.observe.key} was not reported", expected
            )
        if expected is not None and observed != expected:
            return StepOutcome(
                f"{self.name}: expected {self.observe.key}={expected}, "
                f"observed {observed}",
                expected,
                observed,
            )
        if self.observe.reject is not None:
            reason = self.observe.reject(observed)
            if reason:
                # The precondition for this case does not hold; nothing has
                # changed yet, so the case is skipped rather than failed.
                return StepOutcome(
                    "", expected, observed, skipped=f"{self.name}: {reason}"
                )
        return StepOutcome("", expected, observed)


@dataclass(frozen=True)
class StateCase:
    """A state-changing case: ordered steps plus mandatory cleanup."""

    case_id: str
    name: str
    steps: tuple[Step, ...]
    cleanup: tuple[Step, ...]
    expected_state: str = ""
    # The runner creates a per-case directory in the run directory for files
    # the case writes (capture files) and records it as context["capture dir"].
    capture_dir: bool = False
    # Image labels (for example "test_build") of run manifest artifacts the
    # steps use. The runner records each path as context[artifact_key(label)]
    # and the Ubertooth source's expected revision as
    # context[EXPECTED_REVISION_KEY]; a run without them is refused.
    artifacts: tuple[str, ...] = ()
    # What the case needs from the host (hostneeds.py); a host without it
    # leaves the case out before the run, as not applicable. A case that
    # needs the firmware build lists "test_build" in artifacts too.
    needs: Needs = NO_NEEDS
    state_changing: bool = field(default=True, init=False)

    def __post_init__(self):
        if not _CASE_ID_RE.fullmatch(self.case_id):
            raise StateCaseError(f"invalid case ID '{self.case_id}'")
        if not self.steps or not self.cleanup:
            raise StateCaseError(f"{self.case_id}: steps and cleanup are required")
        identifiers = [step.step_id for step in self.steps + self.cleanup]
        if len(set(identifiers)) != len(identifiers):
            raise StateCaseError(f"{self.case_id}: duplicate step ID")
        keys = [
            step.observe.key
            for step in self.steps + self.cleanup
            if step.observe is not None
        ]
        if len(set(keys)) != len(keys):
            raise StateCaseError(f"{self.case_id}: duplicate observation key")


def compatible_pairs(
    options: Sequence[str],
    conflicts: Sequence[tuple[str, str]] = (),
) -> tuple[tuple[str, str], ...]:
    """Every unordered option pair in declared order, minus declared conflicts."""
    if len(set(options)) != len(options):
        raise StateCaseError("options must be distinct")
    blocked = {frozenset(pair) for pair in conflicts}
    unknown = {item for pair in blocked for item in pair} - set(options)
    if unknown:
        raise StateCaseError(
            "conflicts reference unknown options: " + ", ".join(sorted(unknown))
        )
    return tuple(
        pair for pair in combinations(options, 2) if frozenset(pair) not in blocked
    )
