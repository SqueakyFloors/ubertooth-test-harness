"""Versioned run events and durable/injectable event sinks."""

from dataclasses import asdict, dataclass
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import shlex
from typing import Callable, Mapping, Protocol, Sequence, TextIO


EVENT_SCHEMA_VERSION = 1
_MAX_DETAILS_BYTES = 16 * 1024
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class EventSinkError(RuntimeError):
    """An event sink failed to accept an event."""


@dataclass(frozen=True)
class RunEvent:
    schema_version: int
    run_id: str
    sequence: int
    timestamp: str
    event_type: str
    status: str
    details: dict

    def to_dict(self) -> dict:
        return asdict(self)


class EventSink(Protocol):
    def emit(self, event: RunEvent) -> None:
        """Accept one ordered event or raise on failure."""


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run ID must be 1-64 characters, start with an alphanumeric, "
            "and contain only alphanumerics, '.', '_', or '-'"
        )
    return run_id


def generate_run_id(
    *,
    now: datetime.datetime | None = None,
    random_suffix: str | None = None,
) -> str:
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    current = current.astimezone(datetime.timezone.utc)
    suffix = random_suffix or secrets.token_hex(6)
    if not re.fullmatch(r"[A-Za-z0-9]{12}", suffix):
        raise ValueError("run ID random suffix must be 12 alphanumeric characters")
    return validate_run_id(current.strftime("%Y%m%dT%H%M%SZ-") + suffix)


def create_run_directory(root: Path | str, run_id: str) -> Path:
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    destination = (base / validate_run_id(run_id)).resolve()
    if destination.parent != base:
        raise ValueError("run directory escaped its configured root")
    destination.mkdir(exist_ok=False)
    return destination


def _validated_details(details: Mapping | None, max_bytes: int = _MAX_DETAILS_BYTES) -> dict:
    value = dict(details or {})
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"event details must be JSON-serializable: {error}") from error
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"event details exceed {max_bytes} bytes")
    return value


RUN_LOG = "run.jsonl"
REPORT = "report.md"
# The manifest record carries every invocation and identity, so it may exceed
# the per-event limit; it is still bounded.
MANIFEST_MAX_BYTES = 1024 * 1024
# An output_checks record carries one entry per check a command's output was
# read for, each with its pattern, so a step with many patterns may exceed
# the per-event limit; it is still bounded.
OUTPUT_CHECKS_MAX_BYTES = 64 * 1024


class JsonlEventSink:
    """Append one complete, flushed event per JSONL line."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: RunEvent) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())




class TerminalEventSink:
    """Render command events immediately without contaminating summary stdout."""

    def __init__(self, stream: TextIO, *, quiet_events=frozenset()):
        self.stream = stream
        # Event types recorded in run.jsonl but not printed here (for example
        # a libbtbb pin check the caller already printed).
        self.quiet_events = frozenset(quiet_events)
        self._argv: list[str] = []
        self._partial = {"stdout": "", "stderr": ""}

    def _flush_partial(self) -> None:
        for name, text in self._partial.items():
            if text:
                tag = "[E]" if name == "stderr" else "[O]"
                self.stream.write(f"{tag} {text}\n")
        self._partial = {"stdout": "", "stderr": ""}

    def emit(self, event: RunEvent) -> None:
        details = event.details
        if event.event_type in self.quiet_events:
            return
        if event.event_type == "run_started":
            self.stream.write(f"\n== run {event.run_id} started ==\n")
        elif event.event_type == "libbtbb_pin":
            message = details.get("message", "")
            if event.status == "passed":
                self.stream.write(f"[libbtbb pin] {message}\n")
            else:
                self.stream.write(f"warning: {message}\n")
        elif event.event_type == "watchdog":
            for line in details.get("warnings", []):
                self.stream.write(f"warning: {line}\n")
            for line in details.get("notes", []):
                self.stream.write(f"note: {line}\n")
            if event.status == "failed" and details.get("reason"):
                self.stream.write(f"error: {details['reason']}\n")
        elif event.event_type == "watchdog_summary":
            counts = ", ".join(
                f"{board.get('resource')}: {board.get('resets')}"
                for board in details.get("boards", [])
                if board.get("resets")
            )
            if counts:
                self.stream.write(
                    f"\n[watchdog] boards that reset themselves: {counts}\n"
                )
        elif event.event_type == "manifest":
            self.stream.write("\n[manifest recorded]\n")
        elif event.event_type == "not_applicable":
            what = str(details.get("test_name", ""))
            if details.get("case_name"):
                what += f" / {details['case_name']}"
            self.stream.write(
                f"[not applicable] {what}: not applicable on "
                f"{details.get('host', '')}: {details.get('reason', '')}\n"
            )
        elif event.event_type == "preflight_started":
            self.stream.write("\n== mandatory preflight ==\n")
        elif event.event_type == "check_started":
            name = details.get("check_name", details.get("case_name", "check"))
            phase = details.get("phase", "check")
            self.stream.write(f"\n[{phase}] {name}\n")
        elif event.event_type == "check_finished":
            name = details.get("check_name", details.get("case_name", "check"))
            self.stream.write(f"[{event.status}] {name}\n")
        elif event.event_type == "step_finished":
            line = f"[{details.get('stage', 'step')} {event.status}] "
            line += str(details.get("step_name", "step"))
            if details.get("expected") is not None:
                line += (
                    f" (expected {details.get('expected')}, "
                    f"observed {details.get('observed')})"
                )
            elif details.get("observed") is not None:
                line += f" (recorded {details.get('observed')})"
            if details.get("reason"):
                line += f": {details['reason']}"
            self.stream.write(line + "\n")
        elif event.event_type == "preflight_finished":
            self.stream.write(f"\n== preflight {event.status} ==\n")
        elif event.event_type == "command_started":
            self._argv = [str(value) for value in details.get("argv", [])]
            self._partial = {"stdout": "", "stderr": ""}
            self.stream.write(f"\n[C] {shlex.join(self._argv)}\n")
        elif event.event_type == "command_output":
            # Output arrives in chunks that need not end on a line boundary;
            # tag whole lines only.
            name = "stderr" if details.get("stream") == "stderr" else "stdout"
            tag = "[E]" if name == "stderr" else "[O]"
            text = self._partial[name] + str(details.get("text", ""))
            *complete, self._partial[name] = text.split("\n")
            for part in complete:
                self.stream.write(f"{tag} {part.rstrip(chr(13))}\n")
        elif event.event_type == "command_output_omitted":
            from ubertooth_harness.outputfiles import output_file_list

            self._flush_partial()
            self.stream.write(
                "[M] output left out; full output in "
                f"{output_file_list(details)}\n"
            )
        elif event.event_type == "command_finished":
            from ubertooth_harness.executor import CommandResult
            from ubertooth_harness.rawlog import result_text

            self._flush_partial()
            result = CommandResult(
                tuple(self._argv),
                details.get("returncode", -1),
                "", "",
                float(details.get("duration_s", 0.0)),
                bool(details.get("timed_out", False)),
                bool(details.get("cancelled", False)),
            )
            self.stream.write(
                result_text(result, stopped=event.status == "stopped") + "\n"
            )
        elif event.event_type == "isolation_started":
            if not details.get("actions"):
                self.stream.write(
                    f"\n[power isolation] no other registered board; checking "
                    f"{details.get('selected')} is the only Ubertooth (read-only)\n"
                )
            else:
                actions = ", ".join(
                    f"{action['resource']} off via {action['controller']} port {action['port']}"
                    for action in details.get("actions", [])
                )
                self.stream.write(f"\n[power isolation] {actions}\n")
        elif event.event_type == "isolation_confirmed":
            self.stream.write(
                "[power isolation] confirmed; enumerated: "
                + ", ".join(details.get("enumerated", [])) + "\n"
            )
        elif event.event_type in (
            "isolation_applied", "isolation_restored", "isolation_returned"
        ):
            step = event.event_type.removeprefix("isolation_")
            label = {
                "applied": "other boards powered off",
                "restored": "original port states restored",
                "returned": "isolated boards enumerated again",
            }[step]
            self.stream.write(f"[power isolation] {label}\n")
        elif event.event_type == "isolation_finished":
            reason = details.get("reason")
            self.stream.write(
                f"[power isolation] {event.status}"
                + (f": {reason}" if reason else "") + "\n"
            )
        elif event.event_type == "run_finished":
            self.stream.write(
                f"\n== run {event.run_id} {event.status} ==\n"
            )
        self.stream.flush()


class BestEffortEventSink:
    """Record live-sink failures without interrupting hardware execution."""

    def __init__(self, sink: EventSink):
        self.sink = sink
        self.errors: list[str] = []

    def emit(self, event: RunEvent) -> None:
        try:
            self.sink.emit(event)
        except Exception as error:
            self.errors.append(str(error))


class CompositeEventSink:
    """Fan each event to sinks in declared order and surface failures."""

    def __init__(self, sinks: Sequence[EventSink]):
        if not sinks:
            raise ValueError("at least one event sink is required")
        self.sinks = tuple(sinks)

    def emit(self, event: RunEvent) -> None:
        for sink in self.sinks:
            try:
                sink.emit(event)
            except Exception as error:
                raise EventSinkError(str(error)) from error


class EventPublisher:
    """Assign one run's monotonically increasing event sequence."""

    def __init__(
        self,
        run_id: str,
        sink: EventSink,
        *,
        utc_now: Callable[[], datetime.datetime] | None = None,
    ):
        self.run_id = validate_run_id(run_id)
        self.sink = sink
        self.utc_now = utc_now or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        self._sequence = 0

    def emit(
        self,
        event_type: str,
        status: str,
        details: Mapping | None,
        *,
        max_bytes: int = _MAX_DETAILS_BYTES,
    ) -> RunEvent:
        if not event_type or not status:
            raise ValueError("event type and status are required")
        timestamp = self.utc_now()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
        self._sequence += 1
        event = RunEvent(
            schema_version=EVENT_SCHEMA_VERSION,
            run_id=self.run_id,
            sequence=self._sequence,
            # UTC, milliseconds, no offset suffix (all harness times are UTC).
            timestamp=timestamp.astimezone(datetime.timezone.utc).replace(
                tzinfo=None
            ).isoformat(timespec="milliseconds"),
            event_type=event_type,
            status=status,
            details=_validated_details(details, max_bytes),
        )
        self.sink.emit(event)
        return event


def open_run_publisher(
    output_dir: Path | str,
    run_id: str,
    live_sink: EventSink | None = None,
) -> "EventPublisher":
    """Publisher writing the run's single durable record, ``run.jsonl``."""
    durable = JsonlEventSink(Path(output_dir) / RUN_LOG)
    sink: EventSink = durable
    if live_sink is not None:
        sink = CompositeEventSink((durable, BestEffortEventSink(live_sink)))
    return EventPublisher(run_id, sink)
