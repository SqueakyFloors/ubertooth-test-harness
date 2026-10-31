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
from typing import Callable, Iterable, Mapping, Sequence

from ubertooth_harness.hostneeds import NO_NEEDS, Needs
from ubertooth_harness.outputfiles import SKIP_LINE, output_of, text_lines

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


# ubertooth-util and ubertooth-specan keep only what was asked for on stdout
# (fork fcadf34, SqueakyFloors/ubertooth#326; host/doc/ubertooth-util.md and
# ubertooth-specan.md, DIAGNOSTICS): progress lines such as "Stopping
# ubertooth device", errors, warnings and notes go to stderr, and a setter
# (-a -c -C -d -l -z <n>) prints nothing.
_DIAGNOSTIC_LINE = re.compile(
    r"^(?:Entering |Starting |Stopping ubertooth|Resetting ubertooth|"
    r"Flashing LEDs|Setting squelch|Ubertooth stopped|note: |warning: |error: ).*$")


def nothing_on_stdout(stdout) -> str:
    """A Step.output_check for a command that reports nothing (a setter, -S,
    -r): its progress lines belong on stderr."""
    for line in text_lines(stdout):
        if line.strip():
            return (
                f"stdout is not empty (first line {line.strip()!r}); "
                "progress goes to stderr"
            )
    return ""


def no_diagnostics_on_stdout(stdout) -> str:
    """A Step.output_check for a command that also prints a result: no
    progress, error, warning or note line on stdout."""
    for line in text_lines(stdout):
        match = _DIAGNOSTIC_LINE.search(line)
        if match:
            return f"{match[0]!r} is on stdout; progress and diagnostics go to stderr"
    return ""


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

    def extract(self, output) -> str | None:
        """The setting in ``output``, or None when it was not reported.

        ``output`` is the command's CommandOutput, read from its saved files,
        or a plain string when a caller has the text in memory.
        """
        return self.value(output_of(output).search(self.pattern))

    def value(self, match: re.Match | None) -> str | None:
        """The setting this observation's match stands for, or None."""
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
    # Checks stdout (stderr is diagnostics) and returns a failure reason, or
    # "" when it is acceptable; for outputs too varied for patterns, such as
    # a spectrum sweep. It is given the stdout lines, which it may iterate
    # more than once, and which come a block at a time from the saved file,
    # so a sweep that ran for hours costs no more memory than a short one.
    # ``ubertooth-harness validate`` cannot repeat a check written as code,
    # only the patterns, and says so.
    output_check: Callable[[Iterable[str]], str] | None = None
    # A command that should stop itself after a timeout of its own (for
    # example ubertooth-rx -t N) must not exit before this many seconds.
    min_duration_s: float = 0.0
    # Exit status by which the command reports, with a "SKIP: <reason>" line
    # on stdout, that the case cannot be decided here, for a reason only the
    # run itself can find (for example no traffic in range). The case is
    # skipped even when this step or an earlier one changed the board; the
    # cleanup still runs in full, and a cleanup failure still fails the case.
    skip_returncode: int | None = None

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
        elif (self.skip_returncode is not None
              and result.returncode == self.skip_returncode):
            saved = output_of(result)
            match = saved.search(SKIP_LINE, ("stdout",))
            saved.note_pattern("skip-line", self.name, SKIP_LINE, match,
                               streams=("stdout",),
                               value=match[1].strip() if match else "")
            if match is None:
                return StepOutcome(
                    f"{self.name}: command exited {result.returncode} without a "
                    "SKIP: line", expected)
            return StepOutcome(
                "", expected, skipped=f"{self.name}: skipped, {match[1].strip()}")
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
        output = output_of(result)
        for pattern in self.output_patterns:
            match = output.search(pattern)
            reason = (
                "" if match
                else f"{self.name}: expected output pattern {pattern!r} was not found"
            )
            output.note_pattern("expected", self.name, pattern, match, reason=reason)
            if reason:
                return StepOutcome(reason, expected)
        for pattern in self.forbidden_patterns:
            match = output.search(pattern)
            reason = (
                f"{self.name}: forbidden output pattern {pattern!r} was found"
                if match else ""
            )
            output.note_pattern("forbidden", self.name, pattern, match, reason=reason)
            if reason:
                return StepOutcome(reason, expected)
        if self.output_check is not None:
            reason = self.output_check(output.lines("stdout"))
            output.note_case(self.name, reason)
            if reason:
                return StepOutcome(f"{self.name}: {reason}", expected)
        if self.observe is None:
            return StepOutcome("")
        found = output.search(self.observe.pattern)
        observed = self.observe.value(found)
        name = f"{self.name}: {self.observe.key}"
        mismatch = ""
        if observed is None:
            mismatch = f"{self.name}: {self.observe.key} was not reported"
        elif expected is not None and observed != expected:
            mismatch = (f"{self.name}: expected {self.observe.key}={expected}, "
                        f"observed {observed}")
        # Recorded after the comparison: a setting that was reported but is
        # not the expected one is a failed check, and the record is what the
        # report and `validate` read.
        output.note_pattern("observed", name, self.observe.pattern, found,
                            value=observed or "", reason=mismatch)
        if observed is None:
            return StepOutcome(mismatch, expected)
        if mismatch:
            return StepOutcome(mismatch, expected, observed)
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
    # Labels of artifacts the steps use when the run has them (for example
    # an image to flash only when a board runs something else); each one
    # present is hashed and recorded like ``artifacts``, a missing one is
    # left out of the context.
    optional_artifacts: tuple[str, ...] = ()
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
