"""Sequential, exact-verified firmware flash reliability runner."""

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import time
from typing import Callable, Mapping

from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.events import EventPublisher
from ubertooth_harness.executor import CommandExecutor, CommandResult
from ubertooth_harness.patterns import FIRMWARE_REVISION_RE
from ubertooth_harness.plan import FlashStep
from ubertooth_harness.rawlog import record_output


_VERSION_RE = FIRMWARE_REVISION_RE


class HarnessError(RuntimeError):
    """The harness request is unsafe or cannot be started."""


@dataclass(frozen=True)
class FirmwareArtifact:
    label: str
    path: Path
    expected_revision: str
    expected_sha256: str | None = None


@dataclass(frozen=True)
class FlashRunRequest:
    step: FlashStep
    serial: str
    artifacts: Mapping[str, FirmwareArtifact]
    per_flash_timeout_s: float
    total_timeout_s: float
    guard_serials: tuple[str, ...] = ()
    # Further boards flashed in rotation with `serial` (A, B, A, B, ...).
    rotate_serials: tuple[str, ...] = ()

    @property
    def targets(self) -> tuple[str, ...]:
        return (self.serial, *self.rotate_serials)


@dataclass(frozen=True)
class FlashRunSummary:
    status: str
    passed: bool
    platform: str
    serial: str
    requested_flashes: int
    attempted_flashes: int
    verified_flashes: int
    final_revision: str
    failure_reason: str
    duration_s: float
    serials: tuple[str, ...] = ()
    # Last revision read from each board: "" when a failed write or unreadable
    # output left it unknown; absent for a board never queried.
    final_revisions: Mapping[str, str] | None = None
    verified_flashes_by_serial: Mapping[str, int] | None = None
    attempted_flashes_by_serial: Mapping[str, int] | None = None


class FlashReliabilityRunner:
    def __init__(
        self,
        executor: CommandExecutor,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.executor = executor
        self.clock = clock
        self._owner: dict = {}
        self._output_dir: Path | None = None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _revision(result: CommandResult) -> str | None:
        match = _VERSION_RE.search(result.stdout)
        return match.group("revision") if match else None

    def _validate_request(self, request: FlashRunRequest) -> dict[str, str]:
        if not request.serial:
            raise HarnessError("selected Ubertooth serial is required")
        if request.per_flash_timeout_s <= 0 or request.total_timeout_s <= 0:
            raise HarnessError("flash deadlines must be positive")
        targets = request.targets
        if any(not serial for serial in targets):
            raise HarnessError("selected Ubertooth serial is required")
        if len(set(targets)) != len(targets):
            raise HarnessError("selected serials must be unique")
        if set(targets) & set(request.guard_serials):
            raise HarnessError("selected serial cannot also be a guard serial")
        if len(set(request.guard_serials)) != len(request.guard_serials):
            raise HarnessError("guard serials must be unique")
        # libubertooth treats an all-digit -U value as a USB index (which can
        # change when a board resets) and matches anything else as the END of
        # a serial, first board wins. Each selector must name exactly one
        # board, so refuse indexes and selectors that end another selector.
        selectors = (*targets, *request.guard_serials)
        for selector in selectors:
            if selector.isdigit():
                raise HarnessError(
                    f"device selector '{selector}' is a USB index; use the full serial"
                )
            for other in selectors:
                if selector != other and other.endswith(selector):
                    raise HarnessError(
                        f"device selector '{selector}' also matches '{other}'; "
                        "use full serials"
                    )
        if len(targets) > 1 and request.step.flashes % len(targets):
            raise HarnessError(
                f"{request.step.flashes} flashes cannot alternate evenly across "
                f"{len(targets)} Ubertooths; use a multiple of {len(targets)}"
            )

        hashes = {}
        revisions = []
        for label in request.step.image_sequence:
            artifact = request.artifacts.get(label)
            if artifact is None:
                raise HarnessError(f"missing artifact for image label '{label}'")
            path = Path(artifact.path)
            if not path.is_file():
                raise HarnessError(f"firmware image does not exist: {path}")
            actual_hash = self._sha256(path)
            hashes[label] = actual_hash
            expected_hash = artifact.expected_sha256
            if expected_hash is not None and actual_hash != expected_hash.lower():
                raise HarnessError(
                    f"sha256 mismatch for {label}: expected {expected_hash}, got {actual_hash}"
                )
            if label == "kgv" and actual_hash != request.step.kgv_sha256:
                raise HarnessError(
                    f"sha256 mismatch for pinned KGV: expected "
                    f"{request.step.kgv_sha256}, got {actual_hash}"
                )
            if not artifact.expected_revision:
                raise HarnessError(f"expected revision is required for {label}")
            revisions.append(artifact.expected_revision)
        if len(set(revisions)) != len(revisions):
            raise HarnessError("image sequence must use distinct expected revisions")
        return hashes

    def _execute_command(
        self,
        publisher: EventPublisher,
        command_id: str,
        operation: str,
        argv: list[str],
        timeout_s: float,
        cancellation: CancellationToken | None = None,
        *,
        owner: Mapping | None = None,
    ) -> CommandResult:
        owner = dict(owner or {})
        publisher.emit(
            "command_started",
            "running",
            {
                **owner,
                "command_id": command_id,
                "operation": operation,
                "argv": list(argv),
            },
        )
        invocation = owner.get("invocation_id")
        result, spilled = record_output(
            publisher,
            lambda on_output: self.executor.run(
                argv,
                timeout_s=timeout_s,
                cancellation=cancellation,
                on_output=on_output,
            ),
            details={**owner, "command_id": command_id, "operation": operation},
            output_dir=self._output_dir,
            # Flash command IDs repeat in every flash case; name the case too.
            spill_name=f"{invocation}-{command_id}" if invocation else command_id,
        )
        if result.cancelled:
            status = "canceled"
        elif result.returncode == 0 and not result.timed_out:
            status = "passed"
        else:
            status = "failed"
        publisher.emit(
            "command_finished",
            status,
            {
                **owner,
                "command_id": command_id,
                "operation": operation,
                "returncode": result.returncode,
                "duration_s": result.duration_s,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                **spilled,
            },
        )
        return result

    def _run_revision_query(
        self,
        publisher: EventPublisher,
        command_id: str,
        operation: str,
        serial: str,
        timeout_s: float,
        cancellation: CancellationToken | None = None,
        *,
        owner: Mapping | None = None,
    ) -> tuple[CommandResult, str | None]:
        result = self._execute_command(
            publisher,
            command_id,
            operation,
            ["ubertooth-util", "-U", serial, "-v"],
            timeout_s,
            cancellation,
            owner=owner,
        )
        return result, self._revision(result)

    def _finish(
        self,
        request: FlashRunRequest,
        publisher: EventPublisher,
        started: float,
        attempted: int,
        verified: int,
        final_revision: str,
        status: str,
        reason: str,
        known: Mapping[str, str] | None = None,
        verified_by_serial: Mapping[str, int] | None = None,
        attempted_by_serial: Mapping[str, int] | None = None,
    ) -> FlashRunSummary:
        known = dict(known or {})
        verified_by_serial = dict(verified_by_serial or {})
        summary = FlashRunSummary(
            status=status,
            passed=status == "passed",
            platform=request.step.platform,
            serial=request.serial,
            requested_flashes=request.step.flashes,
            attempted_flashes=attempted,
            verified_flashes=verified,
            final_revision=final_revision,
            failure_reason=reason,
            duration_s=max(0.0, self.clock() - started),
            serials=request.targets,
            final_revisions={
                serial: known[serial] for serial in request.targets if serial in known
            },
            verified_flashes_by_serial={
                serial: verified_by_serial.get(serial, 0) for serial in request.targets
            },
            attempted_flashes_by_serial={
                serial: (attempted_by_serial or {}).get(serial, 0)
                for serial in request.targets
            },
        )
        publisher.emit("flash_run_finished", status, {**self._owner, **asdict(summary)})
        return summary

    @staticmethod
    def _cancel_reason(cancellation: CancellationToken) -> str:
        return f"canceled: {cancellation.reason or 'cancellation requested'}"

    def validate(self, request: FlashRunRequest) -> None:
        """Check images, hashes, and revisions without running a command."""
        self._validate_request(request)

    def run(
        self,
        request: FlashRunRequest,
        publisher: EventPublisher,
        *,
        cancellation: CancellationToken | None = None,
        owner: Mapping | None = None,
        output_dir: Path,
    ) -> FlashRunSummary:
        """Run the flash sequence, recording into the run's publisher.

        ``output_dir`` is the run directory: a command whose output passes
        the run.jsonl record limits keeps its full output in
        ``output/<command id>.log`` there, as test commands do.
        """
        image_hashes = self._validate_request(request)
        self._owner = dict(owner or {})
        self._output_dir = Path(output_dir)
        token = cancellation or CancellationToken()
        started = self.clock()
        deadline = started + request.total_timeout_s
        attempted = 0
        verified = 0
        current_revision = ""
        status = "running"
        reason = ""

        publisher.emit(
            "flash_run_started",
            "running",
            {
                **self._owner,
                "platform": request.step.platform,
                "serial": request.serial,
                "serials": list(request.targets),
                "requested_flashes": request.step.flashes,
                "guard_serials": list(request.guard_serials),
            },
        )
        if token.is_cancelled():
            return self._finish(
                request,
                publisher,
                started,
                attempted,
                verified,
                current_revision,
                "canceled",
                self._cancel_reason(token),
            )

        targets = request.targets
        rotating = len(targets) > 1
        # Last verified revision of every selected board, by serial.
        known: dict[str, str] = {}
        verified_by_serial = {serial: 0 for serial in targets}
        attempted_by_serial = {serial: 0 for serial in targets}

        def finish(final_revision, status, reason):
            return self._finish(
                request, publisher, started, attempted, verified, final_revision,
                status, reason, known, verified_by_serial, attempted_by_serial,
            )

        for target_index, serial in enumerate(targets, 1):
            initial, revision = self._run_revision_query(
                publisher,
                f"initial-version-{target_index}" if rotating else "initial-version",
                "selected_version_before_run",
                serial,
                min(5.0, request.total_timeout_s),
                token,
                owner=self._owner,
            )
            if initial.cancelled:
                return finish(known.get(request.serial, ""), "canceled",
                              self._cancel_reason(token))
            if initial.returncode != 0 or revision is None:
                if not rotating:
                    reason = "unable to query selected device revision before run"
                elif serial == request.serial:
                    reason = f"unable to query selected device {serial} revision before run"
                else:
                    reason = f"unable to query alternated board {serial} revision before run"
                return finish(known.get(request.serial, ""), "failed", reason)
            known[serial] = revision
            if token.is_cancelled():
                return finish(known[request.serial] if request.serial in known else "",
                              "canceled", self._cancel_reason(token))
        current_revision = known[request.serial]

        guard_revisions = {}
        for guard_index, serial in enumerate(request.guard_serials, 1):
            result, revision = self._run_revision_query(
                publisher,
                f"guard-baseline-{guard_index}",
                "guard_version_before_run",
                serial,
                5.0,
                token,
                owner=self._owner,
            )
            if result.cancelled:
                return finish(current_revision, "canceled", self._cancel_reason(token))
            if result.returncode != 0 or revision is None:
                return finish(current_revision, "failed",
                              f"unable to query unselected device {serial} before run")
            guard_revisions[serial] = revision
            if token.is_cancelled():
                return finish(current_revision, "canceled", self._cancel_reason(token))

        # Each board starts with the first image that differs from what it
        # runs now, so its first write is observable.
        sequences = {}
        for serial in targets:
            sequence = list(request.step.image_sequence)
            for offset, label in enumerate(sequence):
                if request.artifacts[label].expected_revision != known[serial]:
                    sequences[serial] = sequence[offset:] + sequence[:offset]
                    break
            else:
                return finish(
                    current_revision, "failed",
                    f"current revision of {serial} matches every image; "
                    "no observable first write",
                )

        for index in range(request.step.flashes):
            if token.is_cancelled():
                status = "canceled"
                reason = self._cancel_reason(token)
                break
            remaining = deadline - self.clock()
            if remaining <= 0:
                status = "failed"
                reason = "total deadline reached before next flash"
                break
            iteration = index + 1
            target = targets[index % len(targets)]
            sequence = sequences[target]
            label = sequence[(index // len(targets)) % len(sequence)]
            artifact = request.artifacts[label]
            expected = artifact.expected_revision
            on_target = f" on {target}" if rotating else ""
            common = {
                **self._owner,
                "iteration": iteration,
                "platform": request.step.platform,
                "serial": target,
                "image_label": label,
                "image_path": str(artifact.path),
                "expected_revision": expected,
            }
            publisher.emit("flash_started", "running", common)
            if token.is_cancelled():
                status = "canceled"
                reason = self._cancel_reason(token)
                publisher.emit(
                    "flash_finished",
                    "canceled",
                    {
                        **common,
                        "actual_revision": known[target],
                        "result": "canceled",
                        "reason": reason,
                    },
                )
                break
            current_hash = self._sha256(Path(artifact.path))
            if current_hash != image_hashes[label]:
                status = "failed"
                reason = (
                    f"firmware artifact changed before flash {iteration}: "
                    f"{artifact.path}"
                )
                publisher.emit(
                    "flash_finished",
                    "failed",
                    {
                        **common,
                        "image_sha256": current_hash,
                        "actual_revision": known[target],
                        "result": "failed",
                        "reason": reason,
                    },
                )
                break

            result = self._execute_command(
                publisher,
                f"flash-{iteration:04d}",
                "firmware_flash",
                [
                    "ubertooth-dfu",
                    "-U",
                    target,
                    "-d",
                    str(artifact.path),
                    "-r",
                ],
                min(request.per_flash_timeout_s, remaining),
                owner=self._owner,
            )
            attempted += 1
            attempted_by_serial[target] += 1
            flash_details = {
                **common,
                "image_sha256": image_hashes[label],
                "command_returncode": result.returncode,
                "command_timed_out": result.timed_out,
                "command_duration_s": result.duration_s,
            }
            if result.timed_out or result.returncode != 0:
                status = "failed"
                known[target] = ""  # unknown after a failed write
                reason = (
                    f"flash command failed at iteration {iteration}{on_target}: "
                    f"returncode={result.returncode} timed_out={result.timed_out}"
                )
                publisher.emit(
                    "flash_finished",
                    "failed",
                    {
                        **flash_details,
                        "actual_revision": "",
                        "result": "failed",
                        "reason": reason,
                    },
                )
                break

            remaining = deadline - self.clock()
            if remaining <= 0:
                status = "failed"
                known[target] = ""  # written but not read back
                reason = f"total deadline reached after flash {iteration}{on_target}"
                publisher.emit(
                    "flash_finished",
                    "failed",
                    {
                        **flash_details,
                        "actual_revision": "",
                        "result": "failed",
                        "reason": reason,
                    },
                )
                break
            verify_result, actual = self._run_revision_query(
                publisher,
                f"verify-{iteration:04d}",
                "selected_version_after_flash",
                target,
                min(5.0, remaining),
                owner=self._owner,
            )
            observed = dict(known)
            observed[target] = actual or ""
            if self.clock() >= deadline:
                reason = f"total deadline reached while verifying flash {iteration}{on_target}"
            elif verify_result.returncode != 0 or actual is None:
                reason = f"unable to query revision after flash {iteration}{on_target}"
            elif actual != expected:
                reason = (
                    f"revision mismatch after flash {iteration}{on_target}: "
                    f"expected {expected}, got {actual}"
                )
            if reason:
                status = "failed"
                known = observed
                publisher.emit(
                    "flash_finished",
                    "failed",
                    {
                        **flash_details,
                        "actual_revision": actual or "",
                        "result": "failed",
                        "reason": reason,
                    },
                )
                break

            # Every other board must still run what it ran when last verified:
            # the other selected boards first, then the guards.
            unchanged = [
                ("other", serial, known[serial], "selected_version_unchanged_after_flash")
                for serial in targets if serial != target
            ] + [
                ("guard", serial, baseline, "guard_version_after_flash")
                for serial, baseline in guard_revisions.items()
            ]
            counters = {"other": 0, "guard": 0}
            for kind, serial, baseline, operation in unchanged:
                counters[kind] += 1
                what = "alternated board" if kind == "other" else "unselected device"
                remaining = deadline - self.clock()
                if remaining <= 0:
                    reason = (
                        f"total deadline reached while checking {what} {serial} "
                        f"after flash {iteration}"
                    )
                    break
                check_result, check_revision = self._run_revision_query(
                    publisher,
                    f"{kind}-{iteration:04d}-{counters[kind]}",
                    operation,
                    serial,
                    min(5.0, remaining),
                    owner=self._owner,
                )
                if self.clock() >= deadline:
                    reason = (
                        f"total deadline reached while checking {what} {serial} "
                        f"after flash {iteration}"
                    )
                    break
                if check_result.returncode != 0 or check_revision != baseline:
                    if kind == "other":
                        observed[serial] = check_revision or ""
                    detail = ""
                    if check_result.returncode == 0:
                        detail = (
                            f" (expected {baseline}, got "
                            f"{check_revision or 'unparsable output'})"
                        )
                    reason = (
                        f"{what} {serial} changed or became unavailable "
                        f"after flash {iteration}{detail}"
                    )
                    break
            if reason:
                status = "failed"
                known = observed
                publisher.emit(
                    "flash_finished",
                    "failed",
                    {
                        **flash_details,
                        "actual_revision": actual,
                        "result": "failed",
                        "reason": reason,
                    },
                )
                break

            verified += 1
            verified_by_serial[target] += 1
            known[target] = actual
            current_revision = known[request.serial]
            publisher.emit(
                "flash_finished",
                "passed",
                {
                    **flash_details,
                    "actual_revision": actual,
                    "result": "passed",
                    "reason": "",
                },
            )
            if token.is_cancelled():
                if verified == request.step.flashes:
                    status = "passed"
                    reason = ""
                else:
                    status = "canceled"
                    reason = self._cancel_reason(token)
                break

        if status == "running":
            status = "passed" if verified == request.step.flashes else "failed"
            if status == "failed" and not reason:
                reason = "run ended before all requested flashes were verified"
        return finish(current_revision, status, reason)
