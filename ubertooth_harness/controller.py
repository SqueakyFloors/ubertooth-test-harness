"""Controller service: target registry, run dispatch, results store, live fan-out.

The controller listens on 127.0.0.1 only. There is no host option and no
environment override; remote access is an SSH local port forward. Remote
authentication is an open design item (DESIGN.md section 8), so no
authentication layer exists and the listener must never be widened.
Loopback-only is not access control: every local user and process on this
host (and on a target that forwards the port with ``ssh -R``) can use the API
and register a runner for any host name.

Every HTTP request and WebSocket handshake is checked before routing: the
Host header must be ``127.0.0.1:PORT`` or ``localhost:PORT`` and a browser
Origin, when present, must be that same origin. A page on another site, or a
DNS-rebound name, therefore cannot drive the controller through a local
browser. State-changing requests must also be JSON (a cross-site form cannot
send that without a CORS preflight, which is never granted). Every HTTP
response, refusals included, carries the GUI security headers
(``gui.security_headers``: strict CSP, nosniff, no framing, no referrer).

The web GUI (``/ui/``) is static files served from memory by exact name. A
run may only name a ref that ``git ls-remote`` lists for the configured
Ubertooth repository; without a repository no run is dispatched.

Target runners connect to ``/ws/runner`` and speak protocol v1
(``ubertooth_harness/protocol.py``), decoded strictly. Each run is stored as a
flat directory ``<results dir>/<run_id>/`` holding ``run.json`` (the
controller's record), ``run.jsonl`` (the runner's events appended as received,
deduplicated by sequence), and ``report.md`` (compiled from that run.jsonl
when the run ends, exactly as run-suite compiles it on the target).
"""

import asyncio
import dataclasses
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Callable, Mapping
import uuid

from ubertooth_harness.events import REPORT, RUN_LOG
from ubertooth_harness.gui import (
    DEFAULT_PLANS_DIR,
    GitRefSource,
    RefListError,
    failure_summary,
    list_plans,
    load_assets,
    security_headers,
)
from ubertooth_harness.report import write_report
from ubertooth_harness.protocol import (
    MAX_RUNNER_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_runner_message,
    encode_controller_message,
)


BIND_ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8765
RUN_RECORD = "run.json"
# A target is offline once this many heartbeat intervals pass without any
# message from it.
MISSED_HEARTBEATS = 3
DEFAULT_REPLY_TIMEOUT_S = 10.0
# How long a request nobody waits for any more (its REST callers timed out)
# is kept so a late answer still takes effect; afterwards it is dropped.
DEFAULT_LATE_REPLY_GRACE_S = 60.0
DEFAULT_MONITOR_INTERVAL_S = 1.0
LIVE_QUEUE_LIMIT = 1000
# Environment variables that would suggest a bind override. None is honoured;
# their presence refuses startup so nobody believes they took effect.
BIND_OVERRIDE_VARIABLES = (
    "UBERTOOTH_CONTROLLER_HOST",
    "UBERTOOTH_CONTROLLER_BIND",
    "UVICORN_HOST",
    "UVICORN_FD",
    "UVICORN_UDS",
)
# WebSocket close codes.
POLICY_VIOLATION = 1008
TRY_AGAIN_LATER = 1013
UNKNOWN_RUN = 4404
# Terminal status of a stored run whose target registered without it.
LOST = "lost"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ControllerConfigError(ValueError):
    """The controller configuration is unusable (for example, a bind override)."""


class ControllerError(Exception):
    """A REST request cannot be satisfied; ``status`` is the HTTP status."""

    status = 400

    def __init__(self, detail: str, status: int | None = None):
        super().__init__(detail)
        self.detail = detail
        if status is not None:
            self.status = status


def default_results_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """``$XDG_DATA_HOME/ubertooth-harness/controller/runs`` (outside the repo)."""
    base = environ.get("XDG_DATA_HOME", "")
    root = Path(base) if base and Path(base).is_absolute() else Path("~/.local/share")
    return root.expanduser() / "ubertooth-harness" / "controller" / "runs"


def refuse_bind_overrides(environ: Mapping[str, str] = os.environ) -> None:
    present = [name for name in BIND_OVERRIDE_VARIABLES if name in environ]
    if present:
        raise ControllerConfigError(
            f"{', '.join(present)} set; the controller binds {BIND_ADDRESS} only "
            "and has no bind override. Unset it and use an SSH tunnel for remote "
            "access (DESIGN.md section 8)"
        )


def bind_loopback(port: int) -> socket.socket:
    """Bind a TCP socket to 127.0.0.1:port (0 picks a free port)."""
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ControllerConfigError(f"invalid port {port!r}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((BIND_ADDRESS, port))
        address = ipaddress.ip_address(sock.getsockname()[0])
        if not address.is_loopback:
            raise ControllerConfigError(f"bound non-loopback address {address}")
    except BaseException:
        sock.close()
        raise
    return sock


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _log(message: str) -> None:
    print(f"controller: {message}", file=sys.stderr, flush=True)


def valid_identifier(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


# ----- state ---------------------------------------------------------------


@dataclasses.dataclass
class Target:
    host: str
    hostname: str
    os: dict
    python: str
    ubertooth_serials: list | None
    enumeration_error: str | None
    heartbeat_interval_s: float
    active_run_id: str | None
    registered_at: str
    last_seen_at: str
    last_seen: float  # monotonic
    connected: bool = True
    online: bool = True
    last_heartbeat: dict | None = None
    protocol_errors: int = 0
    last_protocol_error: str | None = None
    last_runner_protocol_error: str | None = None

    def view(self) -> dict:
        data = dataclasses.asdict(self)
        del data["last_seen"]
        data["status"] = "online" if self.online else "offline"
        return data


_RUN_FIELDS = (
    "run_id", "target", "request_id", "ref", "plan", "suite", "tests", "build",
    "status", "created_at", "finished_at", "exit_code", "reason",
    "runner_run_directory", "report", "cancel_requested", "adopted",
)


@dataclasses.dataclass
class Run:
    run_id: str
    target: str
    directory: Path
    request_id: str | None = None
    ref: str | None = None
    plan: str | None = None
    suite: str | None = None
    tests: list = dataclasses.field(default_factory=list)
    build: bool = False
    status: str = "running"
    created_at: str = dataclasses.field(default_factory=_now)
    finished_at: str | None = None
    exit_code: int | None = None
    reason: str | None = None
    runner_run_directory: str | None = None
    report: bool = False
    cancel_requested: bool = False
    # Created from a runner report rather than a start request (for example,
    # the run outlived a controller restart).
    adopted: bool = False
    seen: set = dataclasses.field(default_factory=set, repr=False)
    last_event_sequence: int | None = None
    last_event_type: str | None = None
    duplicate_events: int = 0
    # Lines of the stored run.jsonl that are not JSON objects (skipped).
    malformed_lines: int = 0

    @property
    def active(self) -> bool:
        return self.status == "running"

    def record(self) -> dict:
        return {name: getattr(self, name) for name in _RUN_FIELDS}

    def view(self) -> dict:
        data = self.record()
        data.update(
            event_count=len(self.seen),
            last_event_sequence=self.last_event_sequence,
            last_event_type=self.last_event_type,
            duplicate_events=self.duplicate_events,
            malformed_lines=self.malformed_lines,
        )
        return data


class Link:
    """One WebSocket as the controller sees it (framework independent)."""

    async def receive(self) -> str | bytes | None:  # None: closed
        raise NotImplementedError

    async def send(self, text: str) -> None:
        raise NotImplementedError

    async def close(self, code: int, reason: str = "") -> None:
        raise NotImplementedError


@dataclasses.dataclass
class _Pending:
    """A run or cancel request awaiting the runner's answer.

    Several REST callers may wait on one future (overlapping cancels), each
    with its own timeout; a timeout never cancels the shared future. Once no
    caller waits, the entry expires after the late-reply grace period.
    """

    host: str
    future: asyncio.Future
    fields: dict | None = None
    waiters: int = 0
    expires_at: float | None = None  # controller clock; set while unwaited

    @property
    def awaited(self) -> bool:
        return self.waiters > 0 and not self.future.done()

    def resolve(self, result) -> None:
        if not self.future.done():
            self.future.set_result(result)

    def fail(self, error: Exception) -> None:
        if self.future.done():
            return
        self.future.set_exception(error)
        if self.waiters == 0:
            self.future.exception()  # nobody waits any more: mark it retrieved


class _Subscriber:
    def __init__(self, run_id: str | None):
        self.run_id = run_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=LIVE_QUEUE_LIMIT)
        self.overflowed = False

    def offer(self, message: dict) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            # Best effort: a slow browser is dropped, never the runner.
            self.overflowed = True
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)


class _Violation(Exception):
    """A well-formed runner message that does not fit the controller's state."""


class Controller:
    def __init__(
        self,
        results_dir: Path | str,
        *,
        reply_timeout_s: float = DEFAULT_REPLY_TIMEOUT_S,
        late_reply_grace_s: float = DEFAULT_LATE_REPLY_GRACE_S,
        monitor_interval_s: float = DEFAULT_MONITOR_INTERVAL_S,
        log: Callable[[str], None] = _log,
        clock: Callable[[], float] = time.monotonic,
        ref_source: GitRefSource | None = None,
        plans_dir: Path | str = DEFAULT_PLANS_DIR,
    ):
        self.results_dir = Path(results_dir).expanduser().resolve()
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.reply_timeout_s = reply_timeout_s
        self.late_reply_grace_s = late_reply_grace_s
        self.monitor_interval_s = monitor_interval_s
        self.log = log
        self.clock = clock
        # Lists the refs a run may name; None refuses every run.
        self.ref_source = ref_source
        self.plans_dir = Path(plans_dir).expanduser()
        # The listening port; the request guard rejects everything until set.
        self.port: int | None = None
        self.targets: dict[str, Target] = {}
        self.runs: dict[str, Run] = {}
        self._links: dict[str, Link] = {}
        self._send_locks: dict[Link, asyncio.Lock] = {}
        self._pending_runs: dict[str, _Pending] = {}
        self._pending_cancels: dict[str, _Pending] = {}
        self._subscribers: set[_Subscriber] = set()
        self._load_runs()

    # ----- results store ---------------------------------------------------

    def _load_runs(self) -> None:
        for directory in sorted(self.results_dir.iterdir()):
            record = directory / RUN_RECORD
            if not valid_identifier(directory.name) or not record.is_file():
                continue
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
                values = {name: data[name] for name in _RUN_FIELDS if name in data}
                run = Run(directory=directory, **values)
            except (OSError, ValueError, TypeError) as error:
                self.log(f"skipping unreadable run {directory.name}: {error}")
                continue
            if run.run_id != directory.name:
                self.log(f"skipping run {directory.name}: run_id mismatch")
                continue
            self._load_events(run)
            self.runs[run.run_id] = run

    def _load_events(self, run: Run) -> None:
        path = run.directory / RUN_LOG
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return
        if data and not data.endswith(b"\n"):
            # Drop a partial line left by an interrupted append.
            keep = data.rfind(b"\n") + 1
            with path.open("r+b") as stream:
                stream.truncate(keep)
            data = data[:keep]
        for event in self._parse_events(run, data):
            run.seen.add(event.get("sequence"))
            run.last_event_sequence = event.get("sequence")
            run.last_event_type = event.get("event_type")

    def _parse_events(self, run: Run, data: bytes) -> list[dict]:
        """Records of a stored run.jsonl; malformed lines are skipped and counted."""
        events, malformed = [], 0
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:  # includes undecodable UTF-8
                event = None
            if isinstance(event, dict):
                events.append(event)
            else:
                malformed += 1
        if malformed:
            self.log(f"run {run.run_id}: skipped {malformed} malformed run.jsonl line(s)")
        run.malformed_lines = malformed
        return events

    def _run_directory(self, run_id: str) -> Path:
        directory = (self.results_dir / run_id).resolve()
        if directory.parent != self.results_dir:
            raise _Violation(f"run_id {run_id!r} is not a flat directory name")
        return directory

    def _persist(self, run: Run) -> None:
        run.directory.mkdir(parents=True, exist_ok=True)
        temporary = run.directory / f".{RUN_RECORD}.tmp"
        temporary.write_text(
            json.dumps(run.record(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, run.directory / RUN_RECORD)

    def _append_event(self, run: Run, event: dict) -> None:
        run.directory.mkdir(parents=True, exist_ok=True)
        with (run.directory / RUN_LOG).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
            stream.flush()

    def _write_report(self, run: Run) -> bool:
        """Compile report.md from the controller's own copy of run.jsonl.

        The runner's run directory may be on another machine, and its path is
        only recorded: the controller never reads files a runner names.
        """
        if not run.seen:
            return False
        return write_report(run.directory) is not None

    def read_events(self, run: Run) -> list[dict]:
        try:
            data = (run.directory / RUN_LOG).read_bytes()
        except FileNotFoundError:
            return []
        return self._parse_events(run, data)

    # ----- live fan-out ----------------------------------------------------

    def _publish(self, message: dict, run_id: str | None = None) -> None:
        for subscriber in list(self._subscribers):
            if run_id is None or subscriber.run_id in (None, run_id):
                subscriber.offer(message)

    def _publish_target(self, target: Target) -> None:
        self._publish({"type": "target", "target": target.view()})

    def _publish_run(self, run: Run) -> None:
        self._publish({"type": "run", "run": run.view()}, run.run_id)

    async def live_session(self, link: Link, run_id: str | None) -> None:
        """Stream target and run updates (and one run's events) to a browser."""
        if run_id is not None and run_id not in self.runs:
            await link.close(UNKNOWN_RUN, "unknown run")
            return
        subscriber = _Subscriber(run_id)
        subscriber.offer({
            "type": "snapshot",
            "protocol_version": PROTOCOL_VERSION,
            "targets": [target.view() for target in self.targets.values()],
            "run": self.runs[run_id].view() if run_id else None,
        })
        self._subscribers.add(subscriber)

        async def drain_client():
            while await link.receive() is not None:
                pass  # browsers send nothing the controller acts on

        reader = asyncio.create_task(drain_client())
        try:
            while True:
                getter = asyncio.create_task(subscriber.queue.get())
                done, _ = await asyncio.wait(
                    {getter, reader}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter not in done:
                    getter.cancel()
                    break
                message = getter.result()
                if message is None:
                    await link.close(TRY_AGAIN_LATER, "live stream overflowed")
                    break
                await link.send(json.dumps(message, sort_keys=True))
        except ConnectionError:
            pass
        finally:
            self._subscribers.discard(subscriber)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    # ----- runner connections ----------------------------------------------

    async def _send(self, link: Link, text: str) -> None:
        lock = self._send_locks.setdefault(link, asyncio.Lock())
        async with lock:
            await link.send(text)

    async def runner_session(self, link: Link) -> None:
        text = await link.receive()
        if text is None:
            return
        try:
            message = decode_runner_message(text)
            if message["type"] != "register":
                raise ProtocolError("the first message must be register")
        except ProtocolError as error:
            self.log(f"refused runner connection: {error}")
            await link.close(POLICY_VIOLATION, str(error)[:120])
            return
        target = self._register(message, link)
        await self._mark_lost_runs(target)
        try:
            while True:
                text = await link.receive()
                if text is None:
                    break
                try:
                    message = decode_runner_message(text)
                    target.last_seen = self.clock()
                    target.last_seen_at = _now()
                    if not target.online:
                        target.online = True
                        self._publish_target(target)
                    await self._dispatch(target, message)
                except (ProtocolError, _Violation) as error:
                    # Never acted on. The protocol has no controller-side
                    # error message, so record it and keep the connection.
                    target.protocol_errors += 1
                    target.last_protocol_error = str(error)[:4096]
                    self.log(f"ignored message from {target.host}: {error}")
        finally:
            self._send_locks.pop(link, None)
            if self._links.get(target.host) is link:
                del self._links[target.host]
                target.connected = False
                target.online = False
                self._fail_pending(target.host, "runner disconnected")
                self._publish_target(target)
                self.log(f"target {target.host} disconnected")

    def _register(self, message: dict, link: Link) -> Target:
        host = message["host"]
        previous = self._links.get(host)
        if previous is not None and previous is not link:
            # A reconnecting runner replaces its stale (half-open) connection.
            # Any loopback process can register any host, so say so when the
            # replaced connection was still live.
            old_target = self.targets.get(host)
            if old_target is not None and old_target.online:
                self.log(
                    f"warning: registration for {host} replaces a connection that "
                    "is still online"
                )
            self._fail_pending(host, "runner reconnected")
            asyncio.ensure_future(previous.close(POLICY_VIOLATION, "replaced"))
        now = _now()
        old = self.targets.get(host)
        target = Target(
            host=host,
            hostname=message["hostname"],
            os=message["os"],
            python=message["python"],
            ubertooth_serials=message["ubertooth_serials"],
            enumeration_error=message["enumeration_error"],
            heartbeat_interval_s=message["heartbeat_interval_s"],
            active_run_id=message["active_run_id"],
            registered_at=now,
            last_seen_at=now,
            last_seen=self.clock(),
            protocol_errors=old.protocol_errors if old else 0,
            last_protocol_error=old.last_protocol_error if old else None,
        )
        self.targets[host] = target
        self._links[host] = link
        self.log(f"target {host} registered ({target.hostname})")
        self._publish_target(target)
        return target

    async def _mark_lost_runs(self, target: Target) -> None:
        """End stored running runs the registering runner no longer has.

        The runner keeps a run as ``active_run_id`` until it has reported the
        run's end, so a stored ``running`` run of this host that it does not
        name (the runner restarted, or this controller restarted and missed
        the end) can no longer finish. This includes runs loaded from disk at
        startup: they stay ``running`` until their target registers.
        """
        for run in list(self.runs.values()):
            if run.target != target.host or not run.active:
                continue
            if run.run_id == target.active_run_id:
                continue
            run.status = LOST
            run.reason = (
                f"target registered with active run {target.active_run_id}"
                if target.active_run_id else "target registered with no active run"
            ) + "; the runner no longer has this run"
            run.finished_at = _now()
            pending = self._pending_cancels.pop(run.run_id, None)
            if pending is not None:
                pending.fail(ControllerError(f"run {run.run_id} lost", 409))
            run.report = await asyncio.to_thread(self._write_report, run)
            self._persist(run)
            self._publish_run(run)
            self.log(f"run {run.run_id} on {target.host}: {LOST} ({run.reason})")

    def _fail_pending(self, host: str, reason: str) -> None:
        for table in (self._pending_runs, self._pending_cancels):
            for key, pending in list(table.items()):
                if pending.host == host:
                    del table[key]
                    pending.fail(ControllerError(reason, 503))

    def expire_pending(self) -> None:
        """Drop requests nobody waits for once their grace period has passed."""
        now = self.clock()
        for table in (self._pending_runs, self._pending_cancels):
            for key, pending in list(table.items()):
                if pending.expires_at is not None and now >= pending.expires_at:
                    del table[key]
                    pending.future.cancel()  # no waiters remain

    def _run_for(self, target: Target, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if run is not None:
            if run.target != target.host:
                raise _Violation(f"run {run_id} belongs to {run.target}")
            return run
        if run_id != target.active_run_id:
            raise _Violation(f"unknown run {run_id}")
        # The runner reports a run this controller did not start (or whose
        # start predates a controller restart): adopt it.
        run = Run(
            run_id=run_id, target=target.host,
            directory=self._run_directory(run_id), adopted=True,
        )
        self.runs[run_id] = run
        self._persist(run)
        self._publish_run(run)
        return run

    async def _dispatch(self, target: Target, message: dict) -> None:
        kind = message["type"]
        if kind == "heartbeat":
            target.last_heartbeat = {
                key: message[key] for key in message if key not in ("version", "type")
            }
            if target.active_run_id != message["active_run_id"]:
                target.active_run_id = message["active_run_id"]
                self._publish_target(target)
        elif kind == "register":
            raise _Violation("register is only valid as the first message")
        elif kind == "run_accepted":
            self.expire_pending()
            pending = self._pending_runs.get(message["request_id"])
            if pending is None or pending.host != target.host:
                raise _Violation(f"run_accepted for unknown request {message['request_id']}")
            run_id = message["run_id"]
            if run_id in self.runs:
                raise _Violation(f"run_accepted reuses run_id {run_id}")
            del self._pending_runs[message["request_id"]]
            fields = pending.fields
            run = Run(
                run_id=run_id, target=target.host,
                directory=self._run_directory(run_id),
                request_id=message["request_id"], ref=fields["ref"],
                plan=fields["plan"], suite=fields["suite"],
                tests=list(fields["tests"]), build=fields["build"],
            )
            self.runs[run_id] = run
            self._persist(run)
            target.active_run_id = run_id
            pending.resolve(run)
            self._publish_run(run)
            self._publish_target(target)
        elif kind == "run_rejected":
            request_id = message["request_id"]
            pending = self._pending_runs.get(request_id) if request_id else None
            if pending is None or pending.host != target.host:
                raise _Violation(f"run_rejected for unknown request {request_id}")
            del self._pending_runs[request_id]
            pending.fail(
                ControllerError(f"runner rejected the run: {message['reason']}", 409)
            )
        elif kind in ("cancel_accepted", "cancel_rejected"):
            run_id = message["run_id"]
            self.expire_pending()
            entry = self._pending_cancels.get(run_id) if run_id else None
            if entry is None or entry.host != target.host:
                raise _Violation(f"{kind} for unknown cancel {run_id}")
            del self._pending_cancels[run_id]
            if kind == "cancel_accepted":
                run = self.runs.get(run_id)
                if run is not None:
                    run.cancel_requested = True
                    self._persist(run)
                    self._publish_run(run)
                entry.resolve(run)
            else:
                entry.fail(
                    ControllerError(f"runner rejected the cancel: {message['reason']}", 409)
                )
        elif kind == "event":
            run_id = message["run_id"]
            event = message["event"]
            if event["run_id"] != run_id:
                raise _Violation(f"event record run_id {event['run_id']} != {run_id}")
            run = self._run_for(target, run_id)
            sequence = event["sequence"]
            if sequence in run.seen:
                run.duplicate_events += 1  # replay after a reconnect
                return
            self._append_event(run, event)
            run.seen.add(sequence)
            run.last_event_sequence = sequence
            run.last_event_type = event["event_type"]
            self._publish(
                {"type": "event", "run_id": run_id, "event": event}, run_id
            )
            if run.finished_at is not None:
                # A record that arrives after the run ended: keep report.md
                # in step with run.jsonl.
                run.report = await asyncio.to_thread(self._write_report, run)
                self._persist(run)
                self._publish_run(run)
        elif kind in ("run_finished", "run_error"):
            run = self._run_for(target, message["run_id"])
            if run.request_id not in (None, message["request_id"]):
                raise _Violation(
                    f"{kind} request_id {message['request_id']} != {run.request_id}"
                )
            run.request_id = message["request_id"]
            if run.status == LOST:
                run.reason = None  # the runner did report the end after all
            run.exit_code = message["exit_code"]
            run.finished_at = _now()
            if kind == "run_finished":
                run.status = message["status"]
                run.runner_run_directory = message["run_directory"]
            else:
                run.status = "error"
                run.reason = message["reason"]
            run.report = await asyncio.to_thread(self._write_report, run)
            self._persist(run)
            if target.active_run_id == run.run_id:
                target.active_run_id = None
                self._publish_target(target)
            self._publish_run(run)
            self.log(f"run {run.run_id} on {target.host}: {run.status}")
        elif kind == "protocol_error":
            target.last_runner_protocol_error = message["reason"]
            self.log(f"{target.host} rejected a controller message: {message['reason']}")

    async def monitor(self) -> None:
        """Mark targets offline after missed heartbeats; expire stale requests."""
        while True:
            await asyncio.sleep(self.monitor_interval_s)
            self.check_heartbeats()
            self.expire_pending()

    def check_heartbeats(self) -> None:
        now = self.clock()
        for target in self.targets.values():
            allowance = MISSED_HEARTBEATS * max(target.heartbeat_interval_s, 0.05)
            if target.online and now - target.last_seen > allowance:
                target.online = False
                self.log(f"target {target.host} missed heartbeats; offline")
                self._publish_target(target)

    # ----- REST operations -------------------------------------------------

    def _target_link(self, host: str) -> tuple[Target, Link]:
        target = self.targets.get(host)
        if target is None:
            raise ControllerError(f"unknown target {host!r}", 404)
        link = self._links.get(host)
        if link is None or not target.online:
            raise ControllerError(f"target {host} is offline", 409)
        return target, link

    async def _await_reply(self, pending: _Pending):
        pending.waiters += 1
        pending.expires_at = None
        try:
            return await asyncio.wait_for(
                asyncio.shield(pending.future), self.reply_timeout_s
            )
        except asyncio.TimeoutError:
            raise ControllerError("runner did not answer in time", 504) from None
        finally:
            # The shared future is never cancelled here: other callers may
            # still wait on it, and a late answer still updates state.
            pending.waiters -= 1
            if pending.waiters == 0 and not pending.future.done():
                pending.expires_at = self.clock() + self.late_reply_grace_s

    async def list_refs(self) -> dict:
        if self.ref_source is None:
            raise ControllerError("no Ubertooth repository is configured", 503)
        try:
            return await self.ref_source.refs()
        except RefListError as error:
            raise ControllerError(f"cannot list refs: {error}", 503) from None

    async def check_ref(self, ref: str) -> None:
        """Refuse a ref that is not a branch or tag of the repository."""
        data = await self.list_refs()
        if not any(item["name"] == ref for item in data["refs"]):
            raise ControllerError(
                f"ref {ref[:200]!r} is not a branch or tag of {data['repository']}", 422
            )

    async def start_run(
        self, host: str, *, ref: str, plan: str, suite: str,
        tests: list[str], build: bool,
    ) -> Run:
        # Checked first: everything after it runs without yielding until the
        # request is recorded as pending.
        await self.check_ref(ref)
        target, link = self._target_link(host)
        if target.active_run_id is not None:
            raise ControllerError(
                f"busy: run {target.active_run_id} is active on {host}", 409
            )
        self.expire_pending()
        for pending in self._pending_runs.values():
            # Requests that timed out stay pending for the grace period (a
            # late acceptance still creates the run) but do not block a new
            # request.
            if pending.host == host and pending.awaited:
                raise ControllerError(f"a run request to {host} is already pending", 409)
        request_id = uuid.uuid4().hex
        fields = dict(ref=ref, plan=plan, suite=suite, tests=list(tests), build=build)
        try:
            text = encode_controller_message("run", request_id=request_id, **fields)
        except ProtocolError as error:
            raise ControllerError(str(error), 422) from None
        pending = _Pending(host, asyncio.get_running_loop().create_future(), fields)
        # Kept until the runner answers or disconnects, or the grace period
        # after the last caller gave up passes, so a late run_accepted still
        # creates the run.
        self._pending_runs[request_id] = pending
        try:
            await self._send(link, text)
        except Exception as error:
            self._pending_runs.pop(request_id, None)
            raise ControllerError(f"cannot reach target {host}: {error}", 503) from None
        return await self._await_reply(pending)

    async def cancel_run(self, run_id: str, reason: str) -> Run:
        run = self.runs.get(run_id)
        if run is None:
            raise ControllerError(f"unknown run {run_id!r}", 404)
        if not run.active:
            raise ControllerError(f"run {run_id} is not active ({run.status})", 409)
        _, link = self._target_link(run.target)
        try:
            text = encode_controller_message("cancel", run_id=run_id, reason=reason)
        except ProtocolError as error:
            raise ControllerError(str(error), 422) from None
        self.expire_pending()
        entry = self._pending_cancels.get(run_id)
        if entry is None or not entry.awaited:
            # None pending, or every earlier caller timed out: ask again.
            entry = _Pending(run.target, asyncio.get_running_loop().create_future())
            self._pending_cancels[run_id] = entry
            try:
                await self._send(link, text)
            except Exception as error:
                if self._pending_cancels.get(run_id) is entry:
                    del self._pending_cancels[run_id]
                raise ControllerError(
                    f"cannot reach target {run.target}: {error}", 503
                ) from None
        # Overlapping cancels share the outstanding request.
        await self._await_reply(entry)
        return run

    def get_run(self, run_id: str) -> Run:
        run = self.runs.get(run_id) if valid_identifier(run_id) else None
        if run is None:
            raise ControllerError(f"unknown run {run_id!r}", 404)
        return run


# ----- HTTP / WebSocket layer ----------------------------------------------


_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_RUNNER_PATH = "/ws/runner"


def check_request(scope: Mapping, port: int | None) -> str | None:
    """Return why an HTTP request or WebSocket handshake is refused, or None."""
    if port is None:
        return "controller is not listening"
    client = scope.get("client")
    if client:
        try:
            if not ipaddress.ip_address(client[0]).is_loopback:
                return "client is not loopback"
        except ValueError:
            return "client address is not an IP address"
    headers: dict[bytes, list[bytes]] = {}
    for name, value in scope.get("headers", ()):
        headers.setdefault(name.lower(), []).append(value)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if port == 80:
        allowed_hosts |= {"127.0.0.1", "localhost"}
    hosts = headers.get(b"host", [])
    if len(hosts) != 1:
        return "exactly one Host header is required"
    host = hosts[0].decode("latin-1").lower()
    if host not in allowed_hosts:
        return "Host is not this loopback controller"
    origins = headers.get(b"origin", [])
    if len(origins) > 1:
        return "more than one Origin header"
    if scope.get("type") == "websocket" and scope.get("path") == _RUNNER_PATH:
        if origins:
            return "runner connections must not come from a browser"
    elif origins and origins[0].decode("latin-1").lower() != f"http://{host}":
        return "cross-origin request refused"
    for value in headers.get(b"sec-fetch-site", []):
        if value.decode("latin-1").lower() not in ("same-origin", "none"):
            return "cross-site request refused"
    if scope.get("type") == "http" and scope.get("method") in _UNSAFE_METHODS:
        types = headers.get(b"content-type", [])
        media = types[0].decode("latin-1").split(";")[0].strip().lower() if types else ""
        if len(types) != 1 or media != "application/json":
            return "state-changing requests must be application/json"
    return None


class LocalRequestGuard:
    """ASGI middleware enforcing check_request before any route runs."""

    def __init__(self, app, controller: Controller):
        self.app = app
        self.controller = controller

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        reason = check_request(scope, self.controller.port)
        if scope["type"] == "http":
            send = _with_security_headers(scope, send, self.controller.port)
        if reason is None:
            await self.app(scope, receive, send)
            return
        self.controller.log(f"refused {scope['type']} {scope.get('path')}: {reason}")
        if scope["type"] == "websocket":
            # Closing before accept refuses the handshake with HTTP 403.
            await send({"type": "websocket.close", "code": POLICY_VIOLATION})
            return
        body = json.dumps({"detail": reason}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def _with_security_headers(scope, send, port):
    """Wrap ``send`` so the response start carries the security headers."""
    host = f"127.0.0.1:{port}"
    for name, value in scope.get("headers", ()):
        # Only a Host the guard accepts is echoed into the CSP.
        if name.lower() == b"host" and value.decode("latin-1").lower() == f"localhost:{port}":
            host = f"localhost:{port}"
    added = security_headers(host)
    names = {name for name, _ in added}

    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = [
                (name, value) for name, value in message.get("headers", ())
                if name.lower() not in names
            ]
            message = {**message, "headers": headers + added}
        await send(message)

    return wrapped


class _StarletteLink(Link):
    def __init__(self, websocket):
        self.websocket = websocket
        self.closed = False

    async def receive(self):
        if self.closed:
            return None
        message = await self.websocket.receive()
        if message["type"] == "websocket.disconnect":
            self.closed = True
            return None
        if message.get("text") is not None:
            return message["text"]
        return message.get("bytes") or b""

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("closed")
        try:
            await self.websocket.send_text(text)
        except Exception as error:
            self.closed = True
            raise ConnectionError(str(error)) from error

    async def close(self, code: int, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.websocket.close(code, reason)
        except Exception:
            pass


ENDPOINTS = {
    "GET /": "this summary (a browser asking for HTML is sent to /ui/)",
    "GET /ui/": "web GUI",
    "GET /api/refs": "branches and tags of the Ubertooth repository (git ls-remote)",
    "GET /api/plans": "test plans with suites, tests and duration estimates",
    "GET /api/targets": "registered targets",
    "GET /api/targets/{host}": "one target",
    "POST /api/targets/{host}/runs": "start a run: {ref, plan, suite, tests, build}",
    "GET /api/runs": "runs, newest first (?target=HOST)",
    "GET /api/runs/{run_id}": "one run with its events and failure summary",
    "GET /api/runs/{run_id}/report.md": "the run's report",
    "POST /api/runs/{run_id}/cancel": "cancel an active run: {reason}",
    "WS /ws/live": "live target and run updates (?run_id=ID adds that run's events)",
    "WS /ws/runner": "target runner protocol v1",
}


def create_app(controller: Controller):
    from fastapi import FastAPI, Request, WebSocket
    from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
    from pydantic import BaseModel, ConfigDict, Field

    class RunRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        ref: str
        plan: str
        suite: str
        tests: list[str] = Field(default_factory=list)
        build: bool = False

    class CancelRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        reason: str = ""

    # No interactive docs: they load scripts from a CDN.
    app = FastAPI(
        title="Ubertooth harness controller", docs_url=None, redoc_url=None,
        openapi_url=None,
    )

    assets = load_assets()

    @app.exception_handler(ControllerError)
    async def controller_error(request: Request, error: ControllerError):
        return JSONResponse({"detail": error.detail}, status_code=error.status)

    @app.get("/")
    async def root(request: Request):
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/ui/", status_code=303)
        return {
            "service": "ubertooth-harness controller",
            "protocol_version": PROTOCOL_VERSION,
            "bind": BIND_ADDRESS,
            "endpoints": ENDPOINTS,
        }

    @app.get("/ui")
    async def gui_redirect():
        return RedirectResponse("/ui/", status_code=308)

    @app.get("/ui/")
    async def gui_index():
        body, media = assets["index.html"]
        return Response(body, media_type=media)

    @app.get("/ui/{name}")
    async def gui_asset(name: str):
        # Exact-name lookup in memory: no request path reaches the filesystem.
        if name not in assets:
            raise ControllerError("not found", 404)
        body, media = assets[name]
        return Response(body, media_type=media)

    @app.get("/api/refs")
    async def list_refs():
        return await controller.list_refs()

    @app.get("/api/plans")
    async def get_plans():
        plans = await asyncio.to_thread(list_plans, controller.plans_dir)
        return {"plans": plans}

    @app.get("/api/targets")
    async def list_targets():
        return {"targets": [target.view() for target in controller.targets.values()]}

    @app.get("/api/targets/{host}")
    async def get_target(host: str):
        target = controller.targets.get(host)
        if target is None:
            raise ControllerError(f"unknown target {host!r}", 404)
        return {"target": target.view()}

    @app.post("/api/targets/{host}/runs", status_code=201)
    async def start_run(host: str, request: RunRequest):
        run = await controller.start_run(
            host, ref=request.ref, plan=request.plan, suite=request.suite,
            tests=request.tests, build=request.build,
        )
        return {"run": run.view()}

    @app.get("/api/runs")
    async def list_runs(target: str | None = None):
        runs = [
            run for run in controller.runs.values()
            if target is None or run.target == target
        ]
        runs.sort(key=lambda run: (run.created_at, run.run_id), reverse=True)
        return {"runs": [run.view() for run in runs]}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        run = controller.get_run(run_id)
        events = await asyncio.to_thread(controller.read_events, run)
        view = run.view()
        return {"run": view, "events": events, "failures": failure_summary(view, events)}

    @app.get("/api/runs/{run_id}/report.md")
    async def get_report(run_id: str):
        run = controller.get_run(run_id)
        path = run.directory / REPORT
        if not run.report or not path.is_file():
            raise ControllerError(f"run {run_id} has no report", 404)
        return FileResponse(path, media_type="text/markdown; charset=utf-8")

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, request: CancelRequest):
        controller.get_run(run_id)
        run = await controller.cancel_run(run_id, request.reason)
        return {"run": run.view()}

    @app.websocket("/ws/runner")
    async def runner_socket(websocket: WebSocket):
        await websocket.accept()
        await controller.runner_session(_StarletteLink(websocket))

    @app.websocket("/ws/live")
    async def live_socket(websocket: WebSocket):
        run_id = websocket.query_params.get("run_id")
        if run_id is not None and not valid_identifier(run_id):
            await websocket.close(POLICY_VIOLATION, "invalid run_id")
            return
        await websocket.accept()
        await controller.live_session(_StarletteLink(websocket), run_id)

    return LocalRequestGuard(app, controller)


class ControllerServer:
    """Serve a Controller on 127.0.0.1:port in the running event loop."""

    def __init__(
        self,
        controller: Controller,
        port: int = DEFAULT_PORT,
        *,
        log_level: str = "info",
        quiet: bool = False,
    ):
        self.controller = controller
        self.requested_port = port
        self.log_level = log_level
        self.quiet = quiet
        self.server = None
        self._serve_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self.port: int | None = None

    async def start(self) -> "ControllerServer":
        import uvicorn
        from uvicorn.config import LOGGING_CONFIG

        refuse_bind_overrides()
        sock = bind_loopback(self.requested_port)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(
            create_app(self.controller),
            host=BIND_ADDRESS,
            port=self.port,
            lifespan="off",
            ws="websockets-sansio",
            ws_max_size=MAX_RUNNER_MESSAGE_BYTES + 64 * 1024,
            ws_per_message_deflate=False,
            # Never trust X-Forwarded-* to rewrite the client address.
            proxy_headers=False,
            server_header=False,
            access_log=not self.quiet,
            log_level="warning" if self.quiet else self.log_level,
            log_config=None if self.quiet else LOGGING_CONFIG,
            timeout_graceful_shutdown=5,
        )
        self.server = uvicorn.Server(config)
        self.controller.port = self.port
        self._serve_task = asyncio.create_task(self.server.serve(sockets=[sock]))
        while not self.server.started:
            if self._serve_task.done():
                sock.close()
                self._serve_task.result()
                raise ControllerConfigError("controller server exited during startup")
            await asyncio.sleep(0.01)
        self._monitor_task = asyncio.create_task(self.controller.monitor())
        self.controller.log(f"listening on http://{BIND_ADDRESS}:{self.port}/")
        return self

    async def wait(self) -> None:
        await self._serve_task

    async def stop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
        if self.server is not None:
            self.server.should_exit = True
        if self._serve_task is not None:
            await asyncio.gather(self._serve_task, return_exceptions=True)
        self.controller.port = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.stop()


async def serve(controller: Controller, port: int) -> None:
    server = ControllerServer(controller, port)
    await server.start()
    try:
        await server.wait()
    finally:
        await server.stop()
