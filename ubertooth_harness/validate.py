"""Run a finished run's output checks again, against its saved files.

``ubertooth-harness validate <run dir>`` reads ``run.jsonl``, finds every
output check the run recorded, and runs each one again over the files in the
run directory. It reports three things:

* a saved file that is missing, a different size, or a different SHA-256,
  which means the run directory is no longer the one the run produced;
* a check whose pattern now reaches a different verdict, which is what to
  look at after a check has been fixed, or when auditing a result;
* the checks it cannot run again, so the reader knows what the pass does
  not cover. A check written as code rather than as a pattern
  (``Step.output_check``) is one of those: the run record holds its verdict,
  not its logic.

Nothing is run on hardware and nothing in the run directory is written to.
A run recorded before output checks existed has nothing to re-run, which is
reported as such and is not a failure.
"""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterator, TextIO

from ubertooth_harness.events import RUN_LOG
from ubertooth_harness.outputfiles import (
    OutputCheck,
    OutputFile,
    OutsideRun,
    confined,
    measure,
    saved_files,
    search_parts,
)

# Event types the re-run reads.
CHECKS_EVENT = "output_checks"
FILES_EVENTS = ("command_finished", "output_files")


class ValidateError(ValueError):
    """The run directory cannot be read."""


@dataclass(frozen=True)
class FileResult:
    saved: OutputFile
    problem: str


@dataclass(frozen=True)
class CheckResult:
    command: str
    check: OutputCheck
    difference: str
    skipped: str


@dataclass(frozen=True)
class Validation:
    files: tuple[FileResult, ...]
    checks: tuple[CheckResult, ...]
    recorded_checks: int

    @property
    def differences(self) -> tuple:
        return tuple(
            item for item in self.files + self.checks
            if getattr(item, "problem", "") or getattr(item, "difference", "")
        )

    @property
    def rerun(self) -> int:
        return sum(1 for check in self.checks if not check.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for check in self.checks if check.skipped)


def read_events(run_dir: Path) -> Iterator[dict]:
    """Every record in the run's ``run.jsonl``, in order, one at a time.

    A release run's log is tens of megabytes, and the point of this command
    is that a finished run can be checked again without holding it in
    memory, so the log is read line by line and only what a check needs is
    kept.
    """
    log = run_dir / RUN_LOG
    if not log.is_file():
        raise ValidateError(f"{log} is not a file; is this a run directory?")
    with log.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise ValidateError(f"{log} line {number}: {error}") from error


def _command_id(details) -> str:
    return str(details.get("command_id", ""))


def recorded_output(run_dir: Path, wanted: set[str]) -> dict[str, dict[str, str]]:
    """The output run.jsonl kept for the commands in ``wanted``.

    A command that stayed inside the record limits has its whole output in
    run.jsonl and no file of its own, so a check over it reads the same bytes
    the live check read. Only those commands are kept, and each is bounded by
    the record limits, so this stays small however long the run was.
    """
    text: dict[str, dict[str, str]] = {}
    if not wanted:
        return text
    for event in read_events(run_dir):
        if event.get("event_type") != "command_output":
            continue
        details = event.get("details") or {}
        command = _command_id(details)
        if command not in wanted:
            continue
        stream = "stderr" if details.get("stream") == "stderr" else "stdout"
        streams = text.setdefault(command, {"stdout": "", "stderr": ""})
        streams[stream] += str(details.get("text", ""))
    return text


def _file_problem(run_dir: Path, saved: OutputFile) -> str:
    try:
        path = confined(run_dir, saved.path)
    except OutsideRun as error:
        return str(error)
    if not path.is_file():
        return "missing from the run directory"
    # ``confined`` resolved the path, so the run directory is resolved too
    # (on macOS /var is a link to /private/var).
    now = measure(path, saved.stream, relative_to=run_dir.resolve(),
                  count_lines=saved.lines > 0)
    if now.bytes != saved.bytes:
        return f"{now.bytes} bytes now, {saved.bytes} when the run recorded it"
    if now.sha256 != saved.sha256:
        return f"SHA-256 is {now.sha256}, the run recorded {saved.sha256}"
    if saved.lines and now.lines != saved.lines:
        return f"{now.lines} lines now, {saved.lines} when the run recorded it"
    return ""


def rerun_check(
    run_dir: Path, recorded: dict, command_id: str, check: OutputCheck
) -> CheckResult:
    """Run one recorded check again and compare it with what the run found."""
    if not check.rerunnable:
        return CheckResult(
            command_id, check, "",
            "a check written as code, not a pattern: its verdict is recorded, "
            "its logic is not",
        )
    if check.files:
        # The files the live check read, in the order it read them. The
        # record is data, so each path is confined to the run directory
        # before it is opened.
        parts = []
        missing = []
        for name in check.files:
            try:
                path = confined(run_dir, name)
            except OutsideRun as error:
                return CheckResult(command_id, check, str(error), "")
            if not path.is_file():
                missing.append(name)
            parts.append(path)
        if missing:
            return CheckResult(
                command_id, check,
                "the file it read is missing: " + ", ".join(missing), "",
            )
    else:
        # No file: the output was short enough to stay in run.jsonl in full.
        # A stream the command never wrote to is an empty string, which is
        # what the live check read, so the check still runs.
        streams = recorded.get(command_id) or {}
        parts = [streams.get(stream, "") for stream in check.streams]
        parts = [part for part in parts if part]
    match = search_parts(parts, check.pattern)
    found = match is not None
    if found != bool(check.found):
        was = "matched" if check.found else "did not match"
        now = "matches" if found else "does not match"
        return CheckResult(command_id, check, f"{was} then, {now} now", "")
    if found and check.matched:
        matched = (match.groupdict().get("value") or match[0] or "")
        if matched[:len(check.matched)] != check.matched:
            return CheckResult(
                command_id, check,
                f"matched {matched[:80]!r}, the run recorded {check.matched[:80]!r}",
                "",
            )
    return CheckResult(command_id, check, "", "")


def validate_run(run_dir: Path) -> Validation:
    """Re-run every recorded output check in ``run_dir``."""
    # One pass for the files and the checks, keeping only those; then the
    # recorded output of the commands whose checks have no file to read.
    files = []
    recorded: list[tuple[str, OutputCheck]] = []
    seen = set()
    for event in read_events(run_dir):
        kind = event.get("event_type")
        details = event.get("details") or {}
        if kind in FILES_EVENTS:
            for saved in saved_files(details):
                if saved.path in seen:
                    continue
                seen.add(saved.path)
                files.append(FileResult(saved, _file_problem(run_dir, saved)))
        elif kind == CHECKS_EVENT:
            command_id = _command_id(details)
            for value in details.get("checks") or []:
                recorded.append((command_id, OutputCheck.from_dict(value)))
    wanted = {
        command_id for command_id, check in recorded
        if check.rerunnable and not check.files
    }
    kept = recorded_output(run_dir, wanted)
    checks = tuple(
        rerun_check(run_dir, kept, command_id, check)
        for command_id, check in recorded
    )
    return Validation(tuple(files), checks, len(recorded))


def report(validation: Validation, out: TextIO) -> None:
    """Write what the re-run found; one line per file and per difference."""
    for result in validation.files:
        if result.problem:
            out.write(f"error: {result.saved.path}: {result.problem}\n")
    for result in validation.checks:
        if result.difference:
            out.write(
                f"error: {result.command}: {result.check.name}: "
                f"{result.check.kind} {result.check.pattern!r} "
                f"{result.difference}\n"
            )
    out.write(
        f"files checked: {len(validation.files)}; "
        f"checks re-run: {validation.rerun}; "
        f"not re-runnable: {validation.skipped}; "
        f"differences: {len(validation.differences)}\n"
    )
    if not validation.recorded_checks:
        out.write(
            "note: this run recorded no output checks, so there is nothing to "
            "re-run; it was made before the harness recorded them\n"
        )
    for result in validation.checks:
        if result.skipped:
            out.write(
                f"note: {result.command}: {result.check.name}: "
                f"not re-run ({result.skipped})\n"
            )


def validate_command(run_dir: Path, out: TextIO, errors: TextIO) -> int:
    """``ubertooth-harness validate <run dir>``: 0 when nothing differs."""
    try:
        validation = validate_run(Path(run_dir))
    except (ValidateError, OSError) as error:
        errors.write(f"error: {error}\n")
        return 1
    report(validation, out)
    return 1 if validation.differences else 0
