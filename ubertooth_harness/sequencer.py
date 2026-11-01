"""Execute resource-bound named tests using catalog-owned behavior."""

from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
import datetime
from pathlib import Path
import time
from typing import Mapping, Sequence

from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.catalog import (
    WATCHDOG_LIMIT_ARGUMENT,
    CatalogError,
    TestCatalog,
    builtin_catalog,
)
from ubertooth_harness.events import (
    MANIFEST_MAX_BYTES,
    OUTPUT_CHECKS_MAX_BYTES,
    open_run_publisher,
    EventPublisher,
    EventSink,
    validate_run_id,
)
from ubertooth_harness.enumeration import EnumerationError
from ubertooth_harness.executor import CommandExecutor, CommandResult
from ubertooth_harness.isolation import IsolationRuntime, IsolationSetupError
from ubertooth_harness.manifest import ManifestError, RunManifest, json_value
from ubertooth_harness.planner import TestInvocation, alternated_roles
from ubertooth_harness.power import PowerControllerError
from ubertooth_harness.preflight import PreflightCheck
from ubertooth_harness.outputfiles import (
    TARGET_SERIAL_STREAM,
    CommandOutput,
    measure,
    stream_for,
)
from ubertooth_harness.rawlog import record_output
from ubertooth_harness.resources import RegistryError
from ubertooth_harness import targets
from ubertooth_harness.topology import IsolationCancelled, applied_power_isolation
from ubertooth_harness.report import write_report
from ubertooth_harness.cases import FlashCase
from ubertooth_harness.hostneeds import (
    HostCapabilities,
    NotApplicable,
    run_capabilities,
    split_cases,
)
from ubertooth_harness.flash import (
    FirmwareArtifact,
    FlashReliabilityRunner,
    FlashRunRequest,
    HarnessError,
)
from ubertooth_harness.plan import flash_step, image_label
from ubertooth_harness.watchdog import MonitoredBoard, WatchdogMonitor
from ubertooth_harness.stateful import (
    ATTEMPT_KEY,
    CAPTURE_DIR,
    CAPTURE_DIR_KEY,
    EXPECTED_REVISION_KEY,
    MissingContext,
    StateCase,
    StateCaseError,
    Unrestorable,
    artifact_key,
)
from ubertooth_harness import transitions

# File records per output_files event; each is about 180 bytes, so a batch
# stays well inside the event limit (events.OUTPUT_CHECKS_MAX_BYTES).
CASE_FILES_PER_RECORD = 100


@dataclass(frozen=True)
class SequencerSummary:
    run_id: str
    status: str
    passed: bool
    requested_preflight_checks: int
    completed_preflight_checks: int
    requested_tests: int
    completed_tests: int
    failure_reason: str
    duration_s: float
    skipped_cases: int = 0
    # Left out before the run because the host cannot run them (hostneeds.py);
    # neither skipped nor failed. Tests: every case, or the whole test.
    not_applicable_cases: int = 0
    not_applicable_tests: int = 0
    # Times a board reset itself during the run, as the firmware counts them
    # (watchdog.py). Reported, not failed.
    watchdog_resets: int = 0


_MAX_REASON_CHARS = 4096


def _full_reason(error: BaseException) -> str:
    """The error, its notes, and an error it replaced (e.g. a body error
    superseded by a restore failure), so neither is lost from the record."""
    parts = [str(error) or type(error).__name__]
    parts.extend(getattr(error, "__notes__", []))
    if error.__context__ is not None and not error.__suppress_context__:
        parts.append(f"while handling {type(error.__context__).__name__}: "
                     f"{error.__context__}")
    return "; ".join(parts)


def _bounded_reason(text: str, marker: str) -> str:
    """Keep reasons inside the event size limit, naming where the rest is."""
    if len(text) <= _MAX_REASON_CHARS:
        return text
    marker = f" ... [truncated; {marker}]"
    return text[: _MAX_REASON_CHARS - len(marker)] + marker


def run_started_details(plan_name: str, suite: str, host: str, sources=()) -> dict:
    """First record of a run: enough identity to report even a failed build."""
    return {
        "plan_name": plan_name,
        "suite": suite,
        "host": host,
        "sources": [source.to_dict() for source in sources],
    }


@dataclass(frozen=True)
class StateCaseResult:
    failure: str
    status: str
    restored: bool
    # Every cleanup step ran and passed: each recorded original was written
    # back and read back. False when a restore was skipped (for example an
    # original that was never read), even if nothing reported a failure.
    verified: bool = False


def _path_safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in name)


def _utc_text(moment: datetime.datetime) -> str:
    """A clock reading as the records write it: naive UTC, to the second.

    A naive reading is already UTC, as ``EventPublisher.emit`` takes it, so
    the zone is attached before any conversion.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return (
        moment.astimezone(datetime.timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


class NamedTestSequencer:
    def __init__(
        self,
        executor: CommandExecutor,
        *,
        catalog: TestCatalog | None = None,
        event_sink: EventSink | None = None,
        clock=time.monotonic,
        isolation: IsolationRuntime | None = None,
        registry=None,
        watchdog_search=None,
    ):
        self.executor = executor
        self.catalog = catalog or builtin_catalog()
        self.event_sink = event_sink
        self.clock = clock
        self.isolation = isolation
        # Hosts' platforms and other Ubertooths (flash guards) come from here.
        self.registry = registry
        # How the run looks for an open fork issue about a board's self-reset;
        # `gh issue list` by default (watchdog.py). Injected by tests, which
        # must not reach GitHub.
        self.watchdog_search = watchdog_search

    PER_FLASH_TIMEOUT_S = 25.0

    def _flash_request(
        self, case: FlashCase, invocation: TestInvocation, manifest: RunManifest
    ) -> FlashRunRequest:
        """Build the flash request from plan arguments, manifest, and registry."""
        if self.registry is None:
            raise HarnessError("Flash reliability needs the resource registry")
        platform = self.registry.get(invocation.host).platform
        step = flash_step(case.arguments, platform)
        artifacts_by_label = {image_label(item.name): item for item in manifest.artifacts}
        ubertooth = manifest.source("Ubertooth")
        test_labels = [label for label in step.image_sequence if label != "kgv"]
        if len(test_labels) != 1:
            raise HarnessError(
                "Flash reliability needs exactly one test image besides KGV; "
                f"plan lists {list(step.image_sequence)}"
            )
        artifacts = {}
        for label in step.image_sequence:
            identity = artifacts_by_label.get(label)
            if identity is None:
                raise HarnessError(
                    f"no firmware image for '{label}'; pass --artifact "
                    f"\"{label.replace('_', ' ')}=PATH\" (or --build --with-firmware)"
                )
            revision = (
                case.arguments["KGV revision"] if label == "kgv"
                else ubertooth.expected_revision
            )
            artifacts[label] = FirmwareArtifact(
                label, Path(identity.path), revision, identity.sha256
            )
        guards = tuple(
            self.registry.get(alias).identity["serial"].lower()
            for alias in self.registry.resolve_hardware(invocation.host, "Ubertooth", "each")
            if alias != invocation.resources.get("Ubertooth")
            and self.registry.get(alias).identity.get("serial")
        )
        # With "alternate Ubertooths", the planner bound the other boards as
        # alternated resources; they are flashed in turn, not guarded.
        rotate = tuple(
            invocation.resource_identities[role]["serial"].lower()
            for role in alternated_roles(invocation.resources)
        )
        guards = tuple(serial for serial in guards if serial not in rotate)
        # Per flash: the write plus up to 5 s for each revision query (the
        # target read-back, every other alternated board, and every guard).
        queries = 1 + len(rotate) + len(guards)
        return FlashRunRequest(
            step=step,
            serial=case.serial,
            artifacts=artifacts,
            per_flash_timeout_s=self.PER_FLASH_TIMEOUT_S,
            # Plus the baseline read of every board before the first write.
            total_timeout_s=max(
                60.0,
                5.0 * queries
                + step.flashes * (self.PER_FLASH_TIMEOUT_S + 5.0 * queries),
            ),
            guard_serials=guards,
            rotate_serials=rotate,
        )

    @staticmethod
    def _case_artifacts(case: StateCase, manifest: RunManifest) -> dict[str, str]:
        """Context entries for a case's manifest artifacts and expected revision.

        Each file is hashed again, so a case never runs an image that changed
        after the manifest recorded it.
        """
        by_label = {image_label(item.name): item for item in manifest.artifacts}
        context = {}
        optional = tuple(label for label in case.optional_artifacts if label in by_label)
        for label in case.artifacts + optional:
            identity = by_label.get(label)
            if identity is None:
                raise HarnessError(
                    f"{case.name}: no firmware image for '{label}'; pass --artifact "
                    f"\"{label.replace('_', ' ')}=PATH\" (or --build --with-firmware)"
                )
            path = Path(identity.path)
            try:
                changed = not path.is_file() or RunManifest.hash_file(path) != identity.sha256
            except OSError as error:
                raise HarnessError(f"{case.name}: cannot read the {label} image: {error}") from error
            if changed:
                raise HarnessError(f"{case.name}: {label} image changed or is missing: {path}")
            context[artifact_key(label)] = str(path)
        try:
            context[EXPECTED_REVISION_KEY] = manifest.source("Ubertooth").expected_revision
        except ManifestError as error:
            raise HarnessError(f"{case.name}: {error}") from error
        return context

    def _run_flash_case(
        self,
        publisher: EventPublisher,
        token: CancellationToken,
        invocation: TestInvocation,
        case: FlashCase,
        case_id: str,
        manifest: RunManifest,
        output_dir: Path,
        monitor: WatchdogMonitor | None,
    ) -> tuple[str, str]:
        """Run flash reliability as one case; return (failure, status).

        ``monitor`` is the run's watchdog monitor, or None when the run has
        nothing to read. A soak is one case, so the reads the run makes
        between cases never come round while it writes; the flash runner
        gives the monitor a turn between its writes instead.
        """
        owner = {
            "phase": "test",
            "invocation_id": invocation.invocation_id,
            "case_id": case_id,
        }

        def read_watchdog() -> None:
            # Between two writes, so this read runs beside no other command.
            # What it finds reaches the test's record through the read after
            # the test, as a read between cases does.
            monitor.poll_if_due(invocation)

        publisher.emit("check_started", "running", {**owner, "case_name": case.name})
        try:
            request = self._flash_request(case, invocation, manifest)
            summary = FlashReliabilityRunner(self.executor, clock=self.clock).run(
                request, publisher, cancellation=token, owner=owner,
                output_dir=output_dir,
                between_flashes=None if monitor is None else read_watchdog,
            )
            failure = "" if summary.passed else f"{case.name}: {summary.failure_reason}"
            status = summary.status
        except HarnessError as error:
            failure, status = f"{case.name}: {error}", "failed"
        publisher.emit(
            "check_finished", status, {**owner, "case_name": case.name, "reason": failure}
        )
        return failure, status

    @contextmanager
    def _isolated(
        self,
        invocation: TestInvocation,
        publisher: EventPublisher,
        token: CancellationToken | None = None,
    ):
        """Apply this invocation's compiled power isolation around its cases.

        An exclusive invocation with no power actions (no other registered
        board on the host) gets a read-only enumeration check instead.
        """
        actions = invocation.power_isolation
        if not actions and not invocation.exclusive_ubertooth:
            yield
            return
        if self.isolation is None:
            if actions:
                message = (
                    "exclusive-Ubertooth test requires power isolation; rerun "
                    "with --allow-power-changes"
                )
            else:
                message = (
                    "exclusive-Ubertooth test requires a USB enumeration check; "
                    "run it with run-suite on the registered Linux host"
                )
            raise IsolationSetupError(message)
        if self.isolation.host != invocation.host:
            raise IsolationSetupError(
                f"isolation is configured for {self.isolation.host}, "
                f"not {invocation.host}"
            )
        selected = invocation.resources.get("Ubertooth")
        if selected is None:
            raise IsolationSetupError("exclusive topology requires an Ubertooth role")
        if not actions:
            with self._enumeration_checked(invocation, publisher, selected, token):
                yield
            return
        self.isolation.check_ready(actions, selected)
        verification = self.isolation.verification(selected, actions)
        owner = {"invocation_id": invocation.invocation_id, "selected": selected}
        details = dict(owner)
        details.update({
            "actions": [action.to_dict() for action in actions],
            "controllers": {
                alias: self.isolation.registry.get(alias).driver
                for alias in sorted({a.controller_alias for a in actions})
            },
        })
        publisher.emit("isolation_started", "running", details)

        def observe(step: str, extra: Mapping | None = None) -> None:
            publisher.emit(f"isolation_{step}", "passed", {**owner, **(extra or {})})

        try:
            with applied_power_isolation(
                actions,
                self.isolation.controller,
                verification,
                observer=observe,
                abort=token.is_cancelled if token is not None else None,
            ):
                # Confirmation required exactly the selected board.
                confirmed = dict(owner)
                confirmed["enumerated"] = [verification.selected]
                publisher.emit("isolation_confirmed", "passed", confirmed)
                yield
        except BaseException as error:
            # Any error (not only isolation ones) closes the isolation record,
            # so run.jsonl never ends with boards apparently still off.
            failed = dict(owner)
            failed["reason"] = str(error) or type(error).__name__
            failed["notes"] = list(getattr(error, "__notes__", []))
            publisher.emit(
                "isolation_finished",
                "canceled" if isinstance(error, IsolationCancelled) else "failed",
                failed,
            )
            raise
        publisher.emit("isolation_finished", "passed", dict(owner))

    @contextmanager
    def _enumeration_checked(
        self,
        invocation: TestInvocation,
        publisher: EventPublisher,
        selected: str,
        token: CancellationToken | None,
    ):
        """Read-only exclusivity check for an invocation with no power actions.

        Records isolation_started (no actions, no controllers), then waits for
        a stable USB enumeration that must be exactly the selected board, and
        records isolation_confirmed before the cases run. No one-shot scan
        runs here (run-suite already checked readiness), so a board briefly
        re-enumerating is absorbed by the settle time. No controller is built
        or connected, so --allow-power-changes is not needed. A cancellation
        is checked before every scan and recorded as canceled.
        """
        owner = {"invocation_id": invocation.invocation_id, "selected": selected}
        publisher.emit(
            "isolation_started", "running",
            {**owner, "actions": [], "controllers": {}, "read_only": True},
        )
        abort = token.is_cancelled if token is not None else None
        try:
            # Validates the registry serial and the platform enumerator.
            verification = self.isolation.verification(selected, ())
            seen = verification.confirm_exclusive(abort=abort)
            if abort is not None and abort():
                raise IsolationCancelled(token.reason)
            publisher.emit(
                "isolation_confirmed", "passed",
                {**owner, "enumerated": sorted(seen), "read_only": True},
            )
            yield
        except BaseException as error:
            failed = dict(owner)
            failed["reason"] = str(error) or type(error).__name__
            failed["notes"] = list(getattr(error, "__notes__", []))
            publisher.emit(
                "isolation_finished",
                "canceled" if isinstance(error, IsolationCancelled) else "failed",
                failed,
            )
            raise
        publisher.emit("isolation_finished", "passed", dict(owner))

    def _record_known_targets(
        self,
        publisher: EventPublisher,
        invocation: TestInvocation,
        definition,
    ) -> None:
        """Record which known device this test ran against, or that it had none.

        A test driven against an nRF52840 DK or an ESP32 Classic peer knows
        exactly what was on the air; the rest listen to whatever is there.
        One record per test says which, so report.md and the GUI read the
        same thing (targets.py). It holds what the registry and the test
        definition say, which is what the run is about to drive; what the
        board itself answered is in the test's own records.
        """
        platform = None
        if self.registry is not None:
            platform = self.registry.get(invocation.host).platform
        found = targets.known_targets(
            definition.hardware_roles or {},
            invocation.resources,
            invocation.resource_identities,
            platform,
        )
        publisher.emit(
            "known_target",
            "recorded" if found else "none",
            targets.target_details(
                invocation.invocation_id, invocation.test_name, found
            ),
        )

    def _run_command(
        self,
        publisher: EventPublisher,
        token: CancellationToken,
        *,
        command_id: str,
        argv: Sequence[str],
        timeout_s: float,
        owner: Mapping,
        continuous: bool = False,
        output_dir: Path,
    ) -> CommandResult:
        """Run one command, recording it and every response in run.jsonl.

        A command whose output passes the per-command record limits
        (rawlog.RECORD_*) keeps its first and last output records in
        run.jsonl, with a ``command_output_omitted`` record between them, and
        each of its streams in full in ``output/<command id>.<stream>.log``;
        ``command_finished`` names the files and summarizes what was left
        out.
        """
        details = dict(owner)
        details.update({"command_id": command_id, "argv": list(argv)})
        publisher.emit("command_started", "running", details)
        result, spilled = record_output(
            publisher,
            lambda on_output: self.executor.run(
                argv,
                timeout_s=timeout_s,
                cancellation=token,
                on_output=on_output,
            ),
            details={**owner, "command_id": command_id},
            output_dir=output_dir,
            spill_name=command_id,
        )
        command_status = (
            "canceled" if result.cancelled
            # A continuous mode reaching its deadline is the expected stop.
            else "stopped" if continuous and result.timed_out
            else "passed" if result.returncode == 0 and not result.timed_out
            else "failed"
        )
        finished = dict(owner)
        finished.update({
            "command_id": command_id,
            "returncode": result.returncode,
            "duration_s": result.duration_s,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
        })
        finished.update(spilled)
        publisher.emit("command_finished", command_status, finished)
        if isinstance(result.output, CommandOutput):
            result.output.owner = {
                **owner, "command_id": command_id, "argv": list(argv),
            }
        return result

    @staticmethod
    def _record_case_files(
        publisher: EventPublisher, owner: Mapping, output_dir: Path, directory
    ) -> None:
        """Record the files a case left in its own directory.

        These are the captures its tools wrote (a PCAP, a PCAPNG, a ``-d``
        dump) and any target device's shell traffic. Each one's size and
        SHA-256 go in the run record, so the report lists it beside the
        command output files and an audit can tell it is still the file the
        run produced. Lines are counted only for a target's traffic; a
        capture is binary.
        """
        if not directory:
            return
        path = Path(directory)
        if not path.is_dir():
            return
        files = []
        for found in sorted(path.rglob("*")):
            if not found.is_file():
                continue
            stream = stream_for(found)
            files.append(measure(
                found, stream, relative_to=output_dir,
                count_lines=stream == TARGET_SERIAL_STREAM,
            ))
        # A case that wrote many files would not fit in one record, and an
        # event over the limit would abort the run after its cleanup had
        # already finished, so they go in batches.
        for start in range(0, len(files), CASE_FILES_PER_RECORD):
            batch = files[start:start + CASE_FILES_PER_RECORD]
            publisher.emit(
                "output_files", "passed",
                {**owner, "files": [saved.to_dict() for saved in batch]},
                max_bytes=OUTPUT_CHECKS_MAX_BYTES,
            )

    @staticmethod
    def _record_output_checks(publisher: EventPublisher, *results) -> None:
        """Record what each check read from the command's output.

        One ``output_checks`` record per command holds the files the checks
        read and, for each check, its pattern and what it found.
        ``ubertooth-harness validate <run dir>`` runs them again against the
        same files, so a run can be audited or re-judged after a check is
        fixed. The record is written after the checks have run, which is why
        it is a record of its own rather than part of ``command_finished``.
        """
        for result in results:
            output = result.output if result is not None else None
            if not isinstance(output, CommandOutput) or not output.checks:
                continue
            # The command's own files are in its command_finished record,
            # and each check names the files it read, so this record carries
            # the checks only.
            publisher.emit(
                "output_checks",
                "failed" if any(c.reason for c in output.checks) else "passed",
                {
                    **output.owner,
                    "checks": [check.to_dict() for check in output.checks],
                },
                max_bytes=OUTPUT_CHECKS_MAX_BYTES,
            )
            # Emptied so a second call for the same command records nothing:
            # the preflight asks twice when its fallback command runs.
            output.checks.clear()

    def _run_state_step(
        self,
        publisher: EventPublisher,
        token: CancellationToken,
        output_dir: Path,
        step,
        stage: str,
        context: dict,
        owner: Mapping,
        case_id: str,
        unchanged: bool = False,
        attempt: int = 1,
    ):
        """Run one transition or cleanup step.

        Returns (failure, ran, canceled, skipped reason).
        """
        details = dict(owner)
        details.update({
            "stage": stage,
            "step_id": step.step_id,
            "step_name": step.name,
        })
        if attempt > 1:
            # A step of a retried group (StateCase.retry_steps): the attempt
            # goes into the record, and into the command ID below, so each
            # try keeps its own saved output.
            details["attempt"] = attempt
        try:
            argv = step.resolve_argv(context)
            if step.observe is not None:
                step.observe.expected_value(context)
        except Unrestorable as error:
            reason = f"{step.name}: skipped, {error.args[0]}"
            details.update({"reason": reason, "argv": None})
            publisher.emit("step_finished", "skipped", details)
            return "", False, False, reason
        except MissingContext as error:
            if unchanged:
                reason = f"{step.name}: not needed, nothing was changed"
                details.update({"reason": reason, "argv": None})
                publisher.emit("step_finished", "skipped", details)
                return "", False, False, reason
            reason = f"{step.name}: missing recorded {error.args[0]}"
            details.update({"reason": reason, "argv": None})
            publisher.emit("step_finished", "failed", details)
            return reason, False, False, ""
        refusal = step.refuse(context) if step.refuse is not None else ""
        if refusal:
            reason = f"{step.name}: {refusal}"
            details.update({"reason": reason, "argv": None})
            publisher.emit("step_finished", "failed", details)
            return reason, False, False, ""
        result = self._run_command(
            publisher,
            token,
            command_id=(f"{case_id}-{step.step_id}"
                        + (f"-try{attempt}" if attempt > 1 else "")),
            argv=argv,
            timeout_s=step.timeout_s,
            owner=details,
            continuous=step.continuous,
            output_dir=output_dir,
        )
        outcome = step.evaluate(result, context)
        self._record_output_checks(publisher, result)
        if (
            step.observe is not None
            and outcome.observed is not None
            and not outcome.failure
            and not outcome.skipped
        ):
            context[step.observe.key] = outcome.observed
        elif (
            stage == "guard"
            and step.observe is not None
            and outcome.observed is not None
            and outcome.failure
        ):
            # Lets the guard CRC refusal name a firmware difference as its cause.
            context[transitions.differs_key(step.observe.key)] = outcome.observed
        status = (
            "canceled" if result.cancelled
            else "failed" if outcome.failure
            else "skipped" if outcome.skipped
            else "passed"
        )
        details.update({
            "argv": list(argv),
            "reason": outcome.failure or outcome.skipped,
            "expected": outcome.expected,
            "observed": outcome.observed,
        })
        publisher.emit("step_finished", status, details)
        return outcome.failure, True, result.cancelled, outcome.skipped

    def _run_state_case(
        self,
        publisher: EventPublisher,
        token: CancellationToken,
        output_dir: Path,
        invocation: TestInvocation,
        case: StateCase,
        case_id: str,
        manifest: RunManifest | None = None,
    ) -> "StateCaseResult":
        """Run steps until the first failure, then always run every cleanup.

        Cleanup uses its own token so cancellation cannot skip it; each cleanup
        command still has a hard timeout. A cleanup failure fails the case even
        when every transition step passed, and a passing cleanup never clears
        an earlier step failure.
        """
        owner = {
            "phase": "test",
            "invocation_id": invocation.invocation_id,
            "case_id": case_id,
        }
        publisher.emit(
            "check_started",
            "running",
            {
                **owner,
                "case_name": case.name,
                "state_changing": case.state_changing,
                "expected_state": case.expected_state,
                "resource_identities": {
                    role: dict(identity)
                    for role, identity in invocation.resource_identities.items()
                },
            },
        )
        context: dict[str, str] = {}
        if case.artifacts or case.optional_artifacts:
            # Checked when the run was planned; hashed again here. Nothing
            # has run yet, so a refusal leaves the device as it was.
            try:
                if manifest is None:
                    raise HarnessError(f"{case.name}: needs the run manifest's artifacts")
                context.update(self._case_artifacts(case, manifest))
            except HarnessError as error:
                publisher.emit(
                    "check_finished", "failed",
                    {**owner, "case_name": case.name, "reason": str(error),
                     # Refused before any step, so no cleanup ran.
                     "failed_step": "", "cleanup_status": "not_run"},
                )
                return StateCaseResult(str(error), "failed", True, verified=True)
        if case.capture_dir:
            # Capture files stay in the run directory, never in the repository.
            directory = (
                output_dir / CAPTURE_DIR / _path_safe(invocation.invocation_id)
                / case.case_id
            )
            directory.mkdir(parents=True, exist_ok=True)
            context[CAPTURE_DIR_KEY] = str(directory)
        step_failure = ""
        failed_step = ""
        step_canceled = False
        skip_reason = ""
        mutated = False
        # A case may declare a group of steps that a failure runs again
        # (StateCase.retry_steps), for an outcome that depends on what
        # arrives over the air.
        window = case.retry_window()
        attempt = 1
        context[ATTEMPT_KEY] = str(attempt)
        index = 0
        try:
            while index < len(case.steps):
                step = case.steps[index]
                if token.is_cancelled():
                    step_failure = f"canceled before {step.name}: {token.reason}"
                    failed_step = step.step_id
                    step_canceled = True
                    publisher.emit(
                        "step_finished",
                        "canceled",
                        {
                            **owner,
                            "stage": "transition",
                            "step_id": step.step_id,
                            "step_name": step.name,
                            "argv": None,
                            "reason": step_failure,
                        },
                    )
                    break
                failure, ran, canceled, skipped = self._run_state_step(
                    publisher, token, output_dir, step, "transition",
                    context, owner, case_id, attempt=attempt,
                )
                mutated = mutated or (ran and step.mutates)
                if failure:
                    # Only the last step of the group decides: it is the one
                    # that judges what arrived. A failure in the steps that
                    # set the attempt up (a mode that would not stop, a
                    # device that stopped answering) ends the case, because
                    # running it again cannot help and a later attempt
                    # would hide it.
                    if (window is not None and index == window[1] - 1
                            and ran and attempt < case.attempts and not canceled
                            and not token.is_cancelled()):
                        attempt += 1
                        context[ATTEMPT_KEY] = str(attempt)
                        index = window[0]
                        continue
                    step_failure = failure
                    failed_step = step.step_id
                    step_canceled = canceled
                    break
                index += 1
                if skipped:
                    # A declared run-time skip (Step.skip_returncode) stands
                    # after a change: the full cleanup below still restores
                    # and verifies the board.
                    if mutated and step.skip_returncode is None:
                        # A precondition can only skip a case before it changes
                        # anything; afterwards it is a failure.
                        step_failure = skipped
                        failed_step = step.step_id
                    else:
                        skip_reason = skipped
                    break

        except BaseException as error:
            # Event delivery or another internal error: the device may have
            # changed, so cleanup must still run before the error propagates.
            self._emergency_cleanup(case.cleanup, context)
            raise error
        cleanup_token = CancellationToken()
        cleanup_failures = []
        cleanup_unverified = False
        for index, step in enumerate(case.cleanup):
            try:
                failure, ran, _canceled, skipped = self._run_state_step(
                    publisher, cleanup_token, output_dir, step, "cleanup",
                    context, owner, case_id,
                    unchanged=not mutated,
                )
            except BaseException as error:
                # Restore commands are idempotent; repeat this one and finish
                # every later one without events, then propagate.
                self._emergency_cleanup(case.cleanup[index:], context)
                raise error
            if skipped or not ran:
                cleanup_unverified = True
            if failure:
                cleanup_failures.append(failure)
            elif skipped and mutated and not ran:
                # A value the case may have changed cannot be written back.
                cleanup_failures.append(skipped)

        self._record_case_files(
            publisher, owner, output_dir, context.get(CAPTURE_DIR_KEY)
        )
        reasons = []
        if step_failure:
            reasons.append(step_failure)
        if cleanup_failures:
            reasons.append("cleanup failed: " + "; ".join(cleanup_failures))
        case_failure = f"{case.name}: " + "; ".join(reasons) if reasons else ""
        # Cancellation never hides a real failure: an unrestored device or a
        # step that failed on its own merits reports "failed".
        status = (
            "skipped" if not case_failure and skip_reason
            else "passed" if not case_failure
            else "canceled" if step_canceled and not cleanup_failures
            else "failed"
        )
        finished = {
            **owner,
            "case_name": case.name,
            "reason": _bounded_reason(
                case_failure or skip_reason,
                "each step reason is in its step_finished event",
            ),
            "failed_step": failed_step,
            "cleanup_status": "failed" if cleanup_failures else "passed",
        }
        if attempt > 1:
            # How many tries the retried group took, for the report.
            finished["attempts"] = attempt
        publisher.emit("check_finished", status, finished)
        return StateCaseResult(
            case_failure, status, not cleanup_failures,
            verified=not cleanup_failures and not cleanup_unverified,
        )

    def _run_guard_check(
        self,
        publisher: EventPublisher,
        token: CancellationToken,
        output_dir: Path,
        invocation: TestInvocation,
        boards,
        context: dict,
        after: str | None = None,
    ) -> tuple[str, str]:
        """Read every guard board with queries only; return (failure, status).

        Without ``after`` this records the baseline; otherwise every read must
        equal it. Every read runs (they change nothing), so the record shows
        each difference, not only the first.
        """
        label = (
            "baseline" if after is None
            else "after-test" if after is transitions.AFTER_TEST
            else f"after-{after}"
        )
        case_id = f"{invocation.invocation_id}-guard-{label}"
        name = (
            "Guard Ubertooths baseline" if after is None
            else "Guard Ubertooths unchanged after the test" if after is transitions.AFTER_TEST
            else f"Guard Ubertooths unchanged after {after}"
        )
        owner = {
            "phase": "test",
            "invocation_id": invocation.invocation_id,
            "case_id": case_id,
        }
        publisher.emit(
            "check_started",
            "running",
            {
                **owner,
                "case_name": name,
                "guard": True,
                "resource_identities": {
                    board.role: dict(invocation.resource_identities[board.role])
                    for board in boards
                },
            },
        )
        failures = []
        changed = []
        canceled = False
        for board in boards:
            board_failed = False
            for step in transitions.guard_steps((board,), after):
                failure, _ran, step_canceled, _skipped = self._run_state_step(
                    publisher, token, output_dir, step, "guard", context, owner, case_id
                )
                if failure:
                    failures.append(failure)
                    board_failed = True
                if step_canceled:
                    canceled = True
                    break
            if board_failed:
                alias = invocation.resources.get(board.role, board.role)
                changed.append(f"{board.role} ({alias}, serial {board.serial})")
            if canceled:
                break
        failure = ""
        if failures:
            if after is None:
                failure = f"{name}: " + "; ".join(failures) + "; no case ran"
            else:
                # Named first so a truncated reason keeps it: guards are
                # never restored automatically.
                failure = (
                    f"{name}: check and restore " + ", ".join(changed)
                    + " by hand, guards are never restored automatically; "
                    + "; ".join(failures)
                )
        status = (
            "passed" if not failures
            else "canceled" if canceled and len(failures) == 1
            else "failed"
        )
        publisher.emit(
            "check_finished",
            status,
            {
                **owner,
                "case_name": name,
                "reason": _bounded_reason(
                    failure, "each read is in its step_finished event"
                ),
            },
        )
        return failure, status

    def _watchdog_monitor(
        self,
        invocations,
        manifest: RunManifest,
        publisher: EventPublisher,
        output_dir: Path,
        cancelled=None,
    ) -> WatchdogMonitor | None:
        """The run's watchdog monitor, or None when there is nothing to read.

        Every Ubertooth the run binds is read, guards included: the firmware
        resets any of them that stops answering. None without a registry (the
        resource types are what say which boards are Ubertooths) or with no
        Ubertooth bound.
        """
        if self.registry is None:
            return None
        boards = {}
        for invocation in invocations:
            for role, alias in invocation.resources.items():
                if alias in boards:
                    continue
                try:
                    kind = self.registry.get(alias).type
                except RegistryError:
                    continue  # planning binds only registered resources
                serial = str(
                    invocation.resource_identities.get(role, {}).get("serial", "")
                ).lower()
                if kind == "Ubertooth" and serial:
                    boards[alias] = MonitoredBoard(alias, serial)
        if not boards:
            return None
        try:
            source = manifest.source("Ubertooth")
            build = f"{source.expected_revision} (commit {source.commit})"
        except ManifestError:
            build = ""
        return WatchdogMonitor(
            lambda **arguments: self._run_command(
                publisher,
                # Read-only, with hard command deadlines, so it still runs
                # after cancellation: a board that reset itself is worth
                # recording whether or not the run was stopped.
                CancellationToken(),
                output_dir=output_dir,
                **arguments,
            ),
            publisher,
            tuple(boards.values()),
            build=build,
            clock=self.clock,
            cancelled=cancelled,
            utc_now=lambda: _utc_text(publisher.utc_now()),
            search=self.watchdog_search,
        )

    def _emergency_cleanup(self, steps, context: Mapping) -> None:
        """Run cleanup commands without events after an internal error.

        Event and raw-log delivery may be what failed, so each command runs
        directly with its hard timeout; errors are ignored so every later
        cleanup command is still attempted.
        """
        for step in steps:
            try:
                argv = step.resolve_argv(context)
            except LookupError:
                continue
            try:
                self.executor.run(
                    argv, timeout_s=step.timeout_s, cancellation=CancellationToken()
                )
            except Exception:
                continue

    @staticmethod
    def _validate_run_contract(
        invocations: Sequence[TestInvocation],
        manifest: RunManifest | None,
        preflight_checks: Sequence[PreflightCheck],
        run_id: str,
    ) -> RunManifest:
        if not invocations:
            raise ValueError("at least one test invocation is required")
        if manifest is None or not preflight_checks:
            raise ValueError("manifest and mandatory preflight checks are required")
        if manifest.run_id != run_id:
            raise ValueError("manifest run ID does not match sequencer run ID")
        if not manifest.matches_invocations(invocations):
            raise ValueError("manifest invocations do not match sequencer input")
        manifest.verify_files()
        return manifest

    def run(
        self,
        invocations: tuple[TestInvocation, ...],
        output_dir: Path,
        *,
        run_id: str,
        manifest: RunManifest | None,
        preflight_checks: tuple[PreflightCheck, ...],
        cancellation: CancellationToken | None = None,
        publisher: EventPublisher | None = None,
        capabilities: HostCapabilities | None = None,
        not_applicable: Sequence[NotApplicable] = (),
    ) -> SequencerSummary:
        """Run the suite, streaming every record to ``output_dir/run.jsonl``.

        Pass ``publisher`` when earlier records (``run_started`` and, for
        example, build commands) were already written to the same run.jsonl.
        ``report.md`` is rendered from run.jsonl when the run ends.

        ``capabilities`` is what the host has (hostneeds.py); by default it
        comes from the registry's platform and the manifest's build and
        artifacts. Cases it cannot run are left out before anything runs and
        recorded as not applicable, with ``not_applicable`` (whole tests the
        planner left out for this host).
        """
        run_id = validate_run_id(run_id)
        manifest = self._validate_run_contract(
            invocations, manifest, preflight_checks, run_id
        )
        if capabilities is None and self.registry is not None:
            capabilities = run_capabilities(
                manifest.host, self.registry.get(manifest.host).platform,
                manifest.build, (item.name for item in manifest.artifacts),
                path=(getattr(self.executor, "environment", None) or {}).get("PATH"),
            )
        left_out = list(not_applicable)
        # Build every case before anything runs, so an implementation error
        # fails the run before the manifest, preflight, or hardware access.
        planned_cases = {}
        planned_guards = {}
        host_api = None
        for invocation in invocations:
            definition = self.catalog.get(invocation.test_name)
            if not definition.implemented:
                continue  # the CLI rejects these before a run starts
            try:
                planned_cases[invocation.invocation_id] = definition.command_cases(
                    invocation.arguments,
                    invocation.resources,
                    invocation.resource_identities,
                )
            except CatalogError as error:
                raise ValueError(
                    f"{invocation.invocation_id}: cannot build test cases: {error}"
                ) from error
            runnable, omitted = split_cases(
                planned_cases[invocation.invocation_id], capabilities
            )
            planned_cases[invocation.invocation_id] = runnable
            left_out.extend(
                NotApplicable(
                    invocation.host, invocation.test_name, reason,
                    invocation.invocation_id,
                    f"{invocation.invocation_id}-{case.case_id}", case.name,
                )
                for case, reason in omitted
            )
            try:
                planned_guards[invocation.invocation_id] = transitions.guard_boards(
                    invocation.resource_identities,
                    transitions.ubertooth_roles(definition.hardware_roles),
                )
            except StateCaseError as error:
                raise ValueError(f"{invocation.invocation_id}: {error}") from error
            if planned_guards[invocation.invocation_id] and not definition.guards_other_ubertooths:
                raise ValueError(
                    f"{invocation.invocation_id}: {invocation.test_name} does not "
                    "support guard Ubertooths"
                )
            if planned_guards[invocation.invocation_id]:
                # Fail closed before any device access if the host API is unknown.
                if host_api is None:
                    try:
                        host_api = transitions.host_api_version(manifest.source("Ubertooth"))
                    except (ManifestError, StateCaseError) as error:
                        raise ValueError(f"{invocation.invocation_id}: {error}") from error
                planned_guards[invocation.invocation_id] = tuple(
                    replace(board, host_api=host_api)
                    for board in planned_guards[invocation.invocation_id]
                )
            for case in planned_cases[invocation.invocation_id]:
                if isinstance(case, StateCase) and (case.artifacts or case.optional_artifacts):
                    try:
                        self._case_artifacts(case, manifest)
                    except HarnessError as error:
                        raise ValueError(
                            f"{invocation.invocation_id}: {error}"
                        ) from error
                if isinstance(case, FlashCase):
                    try:
                        FlashReliabilityRunner(self.executor).validate(
                            self._flash_request(case, invocation, manifest)
                        )
                    except HarnessError as error:
                        raise ValueError(
                            f"{invocation.invocation_id}: {error}"
                        ) from error
        output_dir.mkdir(parents=True, exist_ok=True)
        started_externally = publisher is not None
        if publisher is None:
            publisher = open_run_publisher(output_dir, run_id, self.event_sink)
        elif publisher.run_id != run_id:
            raise ValueError("publisher run ID does not match sequencer run ID")
        try:
            return self._run(
                invocations, output_dir, manifest, preflight_checks,
                cancellation, publisher, planned_cases, started_externally,
                planned_guards, capabilities, tuple(left_out),
            )
        finally:
            write_report(output_dir)

    def _run(
        self,
        invocations,
        output_dir: Path,
        manifest: RunManifest,
        preflight_checks,
        cancellation,
        publisher: EventPublisher,
        planned_cases,
        started_externally: bool,
        planned_guards=None,
        capabilities: HostCapabilities | None = None,
        not_applicable: tuple[NotApplicable, ...] = (),
    ) -> SequencerSummary:
        run_id = publisher.run_id
        token = cancellation or CancellationToken()
        started = self.clock()
        completed_preflight = 0
        completed = 0
        run_skipped_cases = 0
        not_applicable_by_invocation: dict[str, list[NotApplicable]] = {}
        for item in not_applicable:
            if item.invocation_id:
                not_applicable_by_invocation.setdefault(item.invocation_id, []).append(item)
        # Whole tests left out at planning, and invocations with no runnable case.
        planning_tests = sum(1 for item in not_applicable if not item.invocation_id)
        not_applicable_tests = planning_tests
        failed_tests: list[str] = []
        status = "running"
        failure = ""

        if not started_externally:
            publisher.emit(
                "run_started",
                "running",
                run_started_details(
                    manifest.plan_name, manifest.suite, manifest.host, manifest.sources
                ),
            )
        publisher.emit(
            "manifest",
            "passed",
            {
                "requested_preflight_checks": len(preflight_checks),
                "requested_tests": len(invocations),
                "manifest": manifest.to_dict(),
            },
            max_bytes=MANIFEST_MAX_BYTES,
        )
        publisher.emit(
            "host_capabilities",
            "passed",
            {
                "capabilities": capabilities.to_dict() if capabilities else None,
                "not_applicable": len(not_applicable),
            },
        )
        for item in not_applicable:
            # Decided before anything runs: neither a skip nor a failure.
            publisher.emit("not_applicable", "not_applicable", item.to_dict())
        publisher.emit(
            "preflight_started",
            "running",
            {"requested_checks": len(preflight_checks)},
        )
        for check in preflight_checks:
            if token.is_cancelled():
                status = "canceled"
                failure = f"canceled: {token.reason}"
                break
            publisher.emit(
                "check_started",
                "running",
                {
                    "phase": "preflight",
                    "check_id": check.check_id,
                    "check_name": check.name,
                    "host": check.host,
                    "resource": check.resource_alias,
                    "resource_identity": dict(check.resource_identity),
                    "expected_revision": check.expected_revision,
                },
            )
            result = self._run_command(
                publisher,
                token,
                command_id=f"{check.check_id}-command-1",
                argv=check.argv,
                timeout_s=check.timeout_s,
                owner={"phase": "preflight", "check_id": check.check_id},
                output_dir=output_dir,
            )
            failure = check.result_failure(result)
            self._record_output_checks(publisher, result)
            if failure and check.needs_fallback(result):
                # The command left the question unanswered, so this fork
                # commit answers it on the older option: ask that one, and
                # its result is the check's.
                asked = failure
                result = self._run_command(
                    publisher,
                    token,
                    command_id=f"{check.check_id}-command-2",
                    argv=check.fallback_argv,
                    timeout_s=check.timeout_s,
                    owner={"phase": "preflight", "check_id": check.check_id},
                    output_dir=output_dir,
                )
                failure = check.result_failure(result)
                if failure:
                    # Both commands, because the reason line is often all an
                    # operator sees and either one may be the real problem.
                    failure = (
                        f"{asked}; then {' '.join(check.fallback_argv)}: {failure}"
                    )
            self._record_output_checks(publisher, result)
            if failure:
                status = "canceled" if result.cancelled else "failed"
                publisher.emit(
                    "check_finished",
                    status,
                    {
                        "phase": "preflight",
                        "check_id": check.check_id,
                        "check_name": check.name,
                        "reason": failure,
                    },
                )
                break
            completed_preflight += 1
            publisher.emit(
                "check_finished",
                "passed",
                {
                    "phase": "preflight",
                    "check_id": check.check_id,
                    "check_name": check.name,
                    "reason": "",
                },
            )

        preflight_status = "passed" if status == "running" else status
        publisher.emit(
            "preflight_finished",
            preflight_status,
            {
                "requested_checks": len(preflight_checks),
                "completed_checks": completed_preflight,
                "reason": failure,
            },
        )

        monitor = (
            self._watchdog_monitor(
                invocations, manifest, publisher, output_dir, token.is_cancelled
            )
            if status == "running" else None
        )
        if monitor is not None:
            # The baseline every later read is compared with: the firmware's
            # counters are since power-on, so what they already hold is not
            # this run's (watchdog.py). A board that does not answer now would
            # not answer a test either, so no test runs.
            opening = monitor.baseline()
            if opening.failure:
                status, failure = "failed", opening.failure

        if status == "running":
            for invocation in invocations:
                if token.is_cancelled():
                    status = "canceled"
                    failure = f"canceled: {token.reason}"
                    break
                definition = self.catalog.get(invocation.test_name)
                watchdog_warnings: list[str] = []
                publisher.emit(
                    "test_started",
                    "running",
                    {
                        "invocation_id": invocation.invocation_id,
                        "test_name": invocation.test_name,
                        "description": definition.description,
                        "host": invocation.host,
                        "resources": dict(invocation.resources),
                        "rf": json_value(invocation.resource_placements),
                        "rf_required": json_value(invocation.placement_requirements),
                        "arguments": json_value(invocation.arguments),
                    },
                )
                cases = (
                    planned_cases[invocation.invocation_id]
                    if invocation.invocation_id in planned_cases
                    else definition.command_cases(
                        invocation.arguments,
                        invocation.resources,
                        invocation.resource_identities,
                    )
                )
                omitted = not_applicable_by_invocation.get(invocation.invocation_id, [])
                if omitted and not cases:
                    # The host can run none of its cases: the test is not
                    # applicable, neither passed nor failed.
                    not_applicable_tests += 1
                    reasons = sorted({item.reason for item in omitted})
                    publisher.emit(
                        "test_finished",
                        "not_applicable",
                        {
                            "invocation_id": invocation.invocation_id,
                            "test_name": invocation.test_name,
                            "reason": _bounded_reason(
                                f"not applicable on {invocation.host}: "
                                + "; ".join(reasons),
                                "each case is in its not_applicable event",
                            ),
                            "failed_cases": 0,
                            "failed_guard_checks": 0,
                            "skipped_cases": 0,
                            "not_applicable_cases": len(omitted),
                        },
                    )
                    continue
                # After the not-applicable exit: a test the host cannot run
                # drove nothing, so it gets no known-target record at all.
                self._record_known_targets(publisher, invocation, definition)
                case_failures = []
                case_statuses = []
                skipped_cases = 0
                # Power actions switch other boards off; an exclusive
                # invocation without actions gets the read-only check, which
                # switches nothing. Both go through _isolated.
                isolated = bool(invocation.power_isolation) or invocation.exclusive_ubertooth
                guards = (planned_guards or {}).get(invocation.invocation_id) or ()
                guard_context: dict[str, str] = {}
                guard_pending = False
                # Guard and watchdog check failures are counted apart from
                # failed cases: neither is one of the test's own cases.
                failed_guard_checks = 0
                failed_watchdog_checks = 0
                # A failed test ends the run only when the device state is not
                # known: a guard changed or could not be read, a case was not
                # verified restored, a flash failed, or isolation failed.
                unsafe = False
                try:
                    if isolated and token.is_cancelled():
                        # Cancelled: switch nothing and skip the read-only wait.
                        raise IsolationCancelled(token.reason)
                    with (
                        self._isolated(invocation, publisher, token)
                        if isolated else nullcontext()
                    ):
                        run_cases = cases
                        if guards and not token.is_cancelled():
                            guard_failure, guard_status = self._run_guard_check(
                                publisher, token, output_dir, invocation, guards,
                                guard_context,
                            )
                            if guard_failure:
                                # Without a baseline nothing can be compared;
                                # no case runs, so nothing has changed.
                                unsafe = True
                                case_failures.append(guard_failure)
                                if guard_status != "canceled":
                                    failed_guard_checks += 1
                                case_statuses.append(guard_status)
                                run_cases = ()
                        for case in run_cases:
                            if token.is_cancelled():
                                status = "canceled"
                                case_failures.append(f"canceled: {token.reason}")
                                case_statuses.append("canceled")
                                break
                            if monitor is not None:
                                # Between cases, so a long test reports a
                                # self-reset before it ends; never beside a
                                # running command. What it finds reaches the
                                # test's record through after_test below.
                                monitor.poll_if_due(invocation)
                            guard_pending = bool(guards)
                            case_id = f"{invocation.invocation_id}-{case.case_id}"
                            if isinstance(case, FlashCase):
                                failure_text, flash_status = self._run_flash_case(
                                    publisher, token, invocation, case, case_id, manifest,
                                    output_dir, monitor,
                                )
                                if failure_text:
                                    unsafe = True
                                    case_failures.append(failure_text)
                                    case_statuses.append(flash_status)
                                    break
                                continue
                            if isinstance(case, StateCase):
                                outcome = self._run_state_case(
                                    publisher,
                                    token,
                                    output_dir,
                                    invocation,
                                    case,
                                    case_id,
                                    manifest,
                                )
                                guard_failure = ""
                                if guards:
                                    # Read-only, so it runs even after
                                    # cancellation, with hard command timeouts.
                                    guard_failure, guard_status = self._run_guard_check(
                                        publisher, CancellationToken(), output_dir,
                                        invocation, guards, guard_context,
                                        after=case.case_id,
                                    )
                                    guard_pending = False
                                if outcome.status == "skipped":
                                    skipped_cases += 1
                                    run_skipped_cases += 1
                                if outcome.failure:
                                    case_failures.append(outcome.failure)
                                    case_statuses.append(outcome.status)
                                if guard_failure:
                                    # Another board changed: stop the test.
                                    unsafe = True
                                    case_failures.append(guard_failure)
                                    failed_guard_checks += 1
                                    case_statuses.append(guard_status)
                                    break
                                if outcome.failure:
                                    # Continue only from a verified-restored device.
                                    unverified = outcome.status == "canceled" or not outcome.verified
                                    if unverified:
                                        unsafe = True
                                    if unverified or not definition.continue_on_case_failure:
                                        break
                                continue
                            publisher.emit(
                                "check_started",
                                "running",
                                {
                                    "phase": "test",
                                    "invocation_id": invocation.invocation_id,
                                    "case_id": case_id,
                                    "case_name": case.name,
                                },
                            )
                            result = self._run_command(
                                publisher,
                                token,
                                command_id=f"{case_id}-command-1",
                                argv=case.argv,
                                timeout_s=case.timeout_s,
                                owner={
                                    "phase": "test",
                                    "invocation_id": invocation.invocation_id,
                                    "case_id": case_id,
                                },
                                output_dir=output_dir,
                            )
                            missing = case.missing_tool(result)
                            case_failure = "" if missing else case.result_failure(result)
                            self._record_output_checks(publisher, result)
                            case_status = (
                                "canceled" if result.cancelled
                                else "skipped" if missing
                                else "failed" if case_failure
                                else "passed"
                            )
                            if missing:
                                skipped_cases += 1
                                run_skipped_cases += 1
                            publisher.emit(
                                "check_finished",
                                case_status,
                                {
                                    "phase": "test",
                                    "invocation_id": invocation.invocation_id,
                                    "case_id": case_id,
                                    "case_name": case.name,
                                    "reason": case_failure or missing,
                                },
                            )
                            if case_failure:
                                # Command cases are read-only (help, version,
                                # identity, count, register reads), so a
                                # failed one leaves the device as it was. A
                                # mode-starting test must be a StateCase.
                                case_failures.append(case_failure)
                                case_statuses.append(case_status)
                                if result.cancelled or not definition.continue_on_case_failure:
                                    break
                        if guard_pending:
                            guard_failure, guard_status = self._run_guard_check(
                                publisher, CancellationToken(), output_dir,
                                invocation, guards, guard_context, after=transitions.AFTER_TEST,
                            )
                            if guard_failure:
                                unsafe = True
                                case_failures.append(guard_failure)
                                failed_guard_checks += 1
                                case_statuses.append(guard_status)
                except IsolationCancelled as error:
                    status = "canceled"
                    case_failures.append(f"canceled: {error}")
                    case_statuses.append("canceled")
                except (
                    EnumerationError,
                    IsolationSetupError,
                    PowerControllerError,
                ) as error:
                    unsafe = True
                    case_failures.append(f"power isolation: {_full_reason(error)}")
                    case_statuses.append("failed")
                except Exception as error:  # noqa: BLE001 - keep the run record complete
                    if not isolated:
                        raise
                    # Any switched ports are already restored (a read-only check
                    # switched none); record the failure instead of leaving the
                    # run without test_finished/run_finished/summary.
                    unsafe = True
                    case_failures.append(f"unexpected {type(error).__name__}: {error}")
                    case_statuses.append("failed")
                if monitor is not None:
                    # After the power isolation has put every port back, so a
                    # board switched off during the test is read powered. The
                    # boards it switched count from their power-on, so their
                    # baselines go and their next read becomes one.
                    monitor.lost_power(
                        action.resource_alias for action in invocation.power_isolation
                    )
                    watchdog = monitor.after_test(
                        invocation, invocation.arguments.get(WATCHDOG_LIMIT_ARGUMENT)
                    )
                    watchdog_warnings.extend(watchdog.warnings)
                    if watchdog.failure:
                        # A board that did not come back cannot be used again;
                        # a count over the test's limit leaves the device in a
                        # known state, so later tests still run. Neither is a
                        # failed case: every case of the test may have passed.
                        unsafe = unsafe or watchdog.board_lost
                        case_failures.append(watchdog.failure)
                        case_statuses.append("failed")
                        failed_watchdog_checks += 1
                if case_failures:
                    status = (
                        "canceled"
                        if token.is_cancelled()
                        and all(item == "canceled" for item in case_statuses)
                        else "failed"
                    )
                    test_failure = _bounded_reason(
                        f"{invocation.test_name}: " + "; ".join(case_failures),
                        "each case reason is in its check_finished event",
                    )
                    stop_run = unsafe or status == "canceled" or token.is_cancelled()
                    publisher.emit(
                        "test_finished",
                        status,
                        {
                            "invocation_id": invocation.invocation_id,
                            "test_name": invocation.test_name,
                            "reason": test_failure,
                            "failed_cases": (
                                len(case_failures) - failed_guard_checks
                                - failed_watchdog_checks
                            ),
                            "failed_guard_checks": failed_guard_checks,
                            "failed_watchdog_checks": failed_watchdog_checks,
                            "skipped_cases": skipped_cases,
                            "not_applicable_cases": len(omitted),
                            # Reported, not failed: a board that reset
                            # itself came back by itself. Each warning is in
                            # its own watchdog record, so the test record
                            # counts them (watchdog.py).
                            "watchdog_warnings": len(watchdog_warnings),
                            # False: the device was verified back in its known
                            # state, so later tests still run.
                            "stops_run": stop_run,
                        },
                    )
                    if status == "failed":
                        failed_tests.append(test_failure)
                    else:
                        failure = test_failure
                    if stop_run:
                        break
                    status = "running"
                    continue
                completed += 1
                publisher.emit(
                    "test_finished",
                    "passed",
                    {
                        "invocation_id": invocation.invocation_id,
                        "test_name": invocation.test_name,
                        "reason": "",
                        "failed_cases": 0,
                        "failed_guard_checks": 0,
                        "failed_watchdog_checks": 0,
                        "skipped_cases": skipped_cases,
                        "not_applicable_cases": len(omitted),
                        "watchdog_warnings": len(watchdog_warnings),
                    },
                )

        if status == "running":
            status = (
                "passed" if completed + not_applicable_tests - planning_tests
                == len(invocations) else "failed"
            )
        if failed_tests:
            # A real failure is never hidden by a later cancellation.
            more = len(failed_tests) - 1
            reason = failed_tests[0] + (
                f" (and {more} more failed test{'s' if more > 1 else ''})" if more else ""
            )
            if status == "canceled":
                reason += f"; then {failure or 'canceled'}"
            status, failure = "failed", reason
        if monitor is not None:
            # The last read of every board, and the run's watchdog summary:
            # the per-board reset count, and a fork issue to file for each
            # board that reset itself.
            ending = monitor.at_run_end()
            if ending.failure:
                # Not a failed test: no test is running. The run fails because
                # a board it used cannot be used again -- unless it was
                # canceled, where a board may have gone as part of stopping
                # the run and the cancellation is the outcome.
                if status != "canceled":
                    status = "failed"
                failure = f"{failure}; {ending.failure}" if failure else ending.failure
        summary = SequencerSummary(
            run_id=run_id,
            status=status,
            passed=status == "passed",
            requested_preflight_checks=len(preflight_checks),
            completed_preflight_checks=completed_preflight,
            requested_tests=len(invocations),
            completed_tests=completed,
            failure_reason=failure,
            duration_s=self.clock() - started,
            skipped_cases=run_skipped_cases,
            not_applicable_cases=sum(1 for item in not_applicable if item.case_id),
            not_applicable_tests=not_applicable_tests,
            watchdog_resets=monitor.total_resets if monitor is not None else 0,
        )
        publisher.emit("run_finished", status, asdict(summary))
        return summary
