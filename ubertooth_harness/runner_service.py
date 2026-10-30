"""Persistent target runner: dials a loopback controller and runs suites.

The runner opens no listening socket. It connects out to the controller,
registers, sends heartbeats, and executes one ``run-suite`` run at a time as a
child process in this harness environment. The child's run.jsonl is tailed
and every record is forwarded unchanged; run.jsonl and report.md stay the
source of truth.

Controller URLs must name a loopback address. Remote runner authentication is
an open design item, so there is deliberately no way to reach a remote
controller.
"""

import asyncio
import collections
import dataclasses
import ipaddress
import json
import os
from pathlib import Path
import platform
import signal
import socket
import sys
import time
from typing import Callable, Mapping
import urllib.parse

from ubertooth_harness.enumeration import SysfsUbertoothEnumerator, UbertoothEnumerator
from ubertooth_harness.environment import capture
from ubertooth_harness.events import RUN_LOG, generate_run_id
from ubertooth_harness import leases
from ubertooth_harness.libbtbbpin import FORK_PIN
from ubertooth_harness.protocol import (
    MAX_INBOUND_MESSAGE_BYTES,
    ProtocolError,
    decode_controller_message,
    encode_runner_message,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLANS_DIR = REPOSITORY_ROOT / "testplans"
DEFAULT_HEARTBEAT_INTERVAL_S = 10.0
DEFAULT_INITIAL_BACKOFF_S = 0.5
DEFAULT_MAX_BACKOFF_S = 30.0
_CONNECT_TIMEOUT_S = 10.0
_TAIL_POLL_S = 0.05
_STDERR_TAIL_LINES = 20
_NO_BUILD = (
    "this runner has no libbtbb, so it cannot build; restart it with "
    "--libbtbb-source and --libbtbb-ref (scripts/deploy.sh sets them up by "
    "default), or run without Build"
)


class RunnerConfigError(ValueError):
    """The runner configuration is unusable (for example, a remote URL)."""


@dataclasses.dataclass(frozen=True)
class ControllerAddress:
    url: str
    secure: bool
    address: str  # loopback IP literal the TCP connection is made to
    port: int


def validate_controller_url(
    url: str,
    *,
    resolve: Callable[..., list] = socket.getaddrinfo,
) -> ControllerAddress:
    """Accept only ws:// or wss:// URLs whose host is a loopback address.

    ``localhost`` is accepted only when every address it resolves to is
    loopback. There is no override.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise RunnerConfigError(f"invalid controller URL {url!r}: {error}") from error
    if parsed.scheme not in ("ws", "wss"):
        raise RunnerConfigError(
            f"controller URL must use ws:// or wss://, not {parsed.scheme or 'none'!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise RunnerConfigError("controller URL must not contain credentials")
    host = parsed.hostname
    if not host:
        raise RunnerConfigError("controller URL has no host")
    port = port or (443 if parsed.scheme == "wss" else 80)
    refusal = (
        f"controller host {host!r} is not loopback; the runner only connects to "
        "127.0.0.1, ::1, or localhost until remote runner authentication is "
        "designed (DESIGN.md section 8)"
    )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_loopback:
            raise RunnerConfigError(refusal)
        address = str(literal)
    else:
        if host.lower() != "localhost":
            raise RunnerConfigError(refusal)
        try:
            infos = resolve(host, port, type=socket.SOCK_STREAM)
        except OSError as error:
            raise RunnerConfigError(f"cannot resolve {host}: {error}") from error
        addresses = []
        for info in infos:
            try:
                candidate = ipaddress.ip_address(info[4][0])
            except ValueError:
                raise RunnerConfigError(refusal) from None
            if not candidate.is_loopback:
                raise RunnerConfigError(
                    f"localhost resolves to non-loopback {candidate}; refusing"
                )
            addresses.append(str(candidate))
        if not addresses:
            raise RunnerConfigError(f"{host} did not resolve")
        address = addresses[0]
    return ControllerAddress(url, parsed.scheme == "wss", address, port)


@dataclasses.dataclass(frozen=True)
class RunnerConfig:
    controller_url: str
    hardware: Path
    host: str
    ubertooth_source: Path
    output: Path
    plans_dir: Path = DEFAULT_PLANS_DIR
    libbtbb_source: Path | None = None
    libbtbb_ref: str | None = None
    allow_power_changes: bool = False
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    # --external-board-lock for every run-suite child; None leaves the
    # children to $UBERTOOTH_HARNESS_EXTERNAL_BOARD_LOCK, '' turns it off.
    external_board_lock: str | None = None
    initial_backoff_s: float = DEFAULT_INITIAL_BACKOFF_S
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S
    # Extra environment for the run-suite child (tests put fake tools on PATH).
    child_environment: Mapping[str, str] | None = None


@dataclasses.dataclass
class _Run:
    request_id: str
    run_id: str
    run_dir: Path
    process: asyncio.subprocess.Process
    started: float
    stderr_tail: collections.deque
    done: asyncio.Event
    exit_code: int | None = None
    last_sequence: int | None = None
    last_event_type: str | None = None
    cancel_sent: bool = False


def _log(message: str) -> None:
    print(f"runner: {message}", file=sys.stderr, flush=True)


class RunnerService:
    def __init__(
        self,
        config: RunnerConfig,
        *,
        enumerator: UbertoothEnumerator | None = None,
        log: Callable[[str], None] = _log,
    ):
        # Refuse a remote controller before anything else happens.
        self.address = validate_controller_url(config.controller_url)
        if not 0 < config.initial_backoff_s <= config.max_backoff_s:
            raise RunnerConfigError("backoff must be positive and bounded")
        if config.heartbeat_interval_s <= 0:
            raise RunnerConfigError("heartbeat interval must be positive")
        if bool(config.libbtbb_source) != bool(config.libbtbb_ref):
            raise RunnerConfigError(
                "--libbtbb-source and --libbtbb-ref must be supplied together"
            )
        try:
            # Checked here (the option, else the environment the children
            # inherit) so a bad template stops the runner, not every run.
            external = leases.external_lock_template(config.external_board_lock)
        except leases.LeaseError as error:
            raise RunnerConfigError(str(error)) from error
        self.config = dataclasses.replace(
            config,
            external_board_lock=(
                (external or "") if config.external_board_lock is not None else None
            ),
            hardware=Path(config.hardware).expanduser().resolve(),
            ubertooth_source=Path(config.ubertooth_source).expanduser().resolve(),
            output=Path(config.output).expanduser().resolve(),
            plans_dir=Path(config.plans_dir).expanduser().resolve(),
            libbtbb_source=(
                Path(config.libbtbb_source).expanduser().resolve()
                if config.libbtbb_source else None
            ),
        )
        self.enumerator = enumerator
        self.log = log
        self._run: _Run | None = None
        self._stop = asyncio.Event()
        self._connection = None
        self._send_lock = asyncio.Lock()
        self.connections = 0

    # ----- lifecycle -------------------------------------------------------

    @property
    def can_build(self) -> bool:
        """A run with build set needs a libbtbb source and ref."""
        return self.config.libbtbb_source is not None

    def build_status(self) -> str:
        if self.can_build:
            ref = self.config.libbtbb_ref
            if ref == FORK_PIN:
                # Resolved per run, so this runner follows the fork's pin.
                ref = f"{FORK_PIN} (the LIBBTBB_REF of each commit it builds)"
            return f"builds enabled: libbtbb {ref} from {self.config.libbtbb_source}"
        return f"builds disabled: {_NO_BUILD}"

    def request_stop(self) -> None:
        """Stop reconnecting; cancel an active run and wait for it to end."""
        self._stop.set()
        if self._run is not None:
            self._signal_cancel(self._run)
        if self._connection is not None:
            asyncio.ensure_future(self._connection.close())

    async def run_forever(self) -> None:
        self.log(self.build_status())
        backoff = self.config.initial_backoff_s
        while not self._stop.is_set():
            registered = False
            try:
                registered = await self._session()
            except (OSError, asyncio.TimeoutError) as error:
                self.log(f"controller unavailable: {error}")
            except Exception as error:
                from websockets.exceptions import WebSocketException

                # ValueError: websockets refuses to follow any redirect on a
                # pre-connected socket.
                if not isinstance(error, (WebSocketException, ValueError)):
                    raise
                self.log(f"controller connection failed: {error}")
            if self._stop.is_set():
                break
            if registered:
                backoff = self.config.initial_backoff_s
            self.log(f"reconnecting in {backoff:g}s")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.config.max_backoff_s)
        if self._run is not None:
            await self._run.done.wait()

    async def _open_socket(self) -> socket.socket:
        family = socket.AF_INET6 if ":" in self.address.address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_connect(
                    sock, (self.address.address, self.address.port)
                ),
                timeout=_CONNECT_TIMEOUT_S,
            )
            peer = ipaddress.ip_address(sock.getpeername()[0])
            if not peer.is_loopback:
                raise OSError(f"connected to non-loopback peer {peer}")
        except BaseException:
            sock.close()
            raise
        return sock

    async def _session(self) -> bool:
        """One connection: register, then serve until it closes."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        # A pre-connected loopback socket: no proxy is consulted and no
        # redirect is followed. TLS (wss) verifies the URL host.
        sock = await self._open_socket()
        async with connect(
            self.config.controller_url,
            sock=sock,
            proxy=None,
            compression=None,
            max_size=(MAX_INBOUND_MESSAGE_BYTES, None),
            open_timeout=_CONNECT_TIMEOUT_S,
        ) as connection:
            self._connection = connection
            self.connections += 1
            tasks = []
            try:
                await self._send("register", **self._registration())
                self.log(f"registered with {self.config.controller_url}")
                tasks.append(asyncio.create_task(self._heartbeat()))
                if self._run is not None:
                    tasks.append(asyncio.create_task(self._stream(self._run)))
                try:
                    async for text in connection:
                        await self._handle(text, tasks)
                except ConnectionClosed:
                    pass
            finally:
                self._connection = None
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.log("controller connection closed")
        return True

    # ----- outbound --------------------------------------------------------

    async def _send(self, message_type: str, **fields) -> None:
        connection = self._connection
        if connection is None:
            raise ConnectionError("not connected")
        text = encode_runner_message(message_type, **fields)
        async with self._send_lock:
            await connection.send(text)

    def _registration(self) -> dict:
        identity = capture()
        serials = None
        enumeration_error = None
        enumerator = self.enumerator
        if enumerator is None and sys.platform.startswith("linux"):
            # Read-only sysfs scan: never opens a device or sends a stop.
            enumerator = SysfsUbertoothEnumerator()
        if enumerator is None:
            enumeration_error = f"USB enumeration is not supported on {sys.platform}"
        else:
            try:
                serials = sorted(enumerator.serials())
            except Exception as error:
                enumeration_error = str(error)
        return {
            "host": self.config.host,
            "hostname": platform.node(),
            "os": {key: (None if value is None else str(value))
                   for key, value in identity["os"].items()},
            "python": identity["python"]["version"],
            "ubertooth_serials": serials,
            "enumeration_error": enumeration_error,
            "heartbeat_interval_s": self.config.heartbeat_interval_s,
            "active_run_id": self._run.run_id if self._run else None,
        }

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_interval_s)
            run = self._run
            await self._send(
                "heartbeat",
                active_run_id=run.run_id if run else None,
                elapsed_s=round(time.monotonic() - run.started, 3) if run else None,
                last_event_sequence=run.last_sequence if run else None,
                last_event_type=run.last_event_type if run else None,
            )

    # ----- inbound ---------------------------------------------------------

    async def _handle(self, text, tasks: list) -> None:
        try:
            message = decode_controller_message(text)
        except ProtocolError as error:
            self.log(f"rejected controller message: {error}")
            await self._send("protocol_error", reason=str(error))
            return
        if message["type"] == "run":
            await self._start_run(message, tasks)
        elif message["type"] == "cancel":
            await self._cancel(message)

    async def _start_run(self, message: dict, tasks: list) -> None:
        request_id = message["request_id"]
        if self._run is not None:
            await self._send(
                "run_rejected", request_id=request_id,
                reason=f"busy: run {self._run.run_id} is active",
            )
            return
        if message["build"] and not self.can_build:
            # Refused up front: run-suite would fail inside the run with
            # "--build requires --libbtbb-source and --libbtbb-ref".
            await self._send(
                "run_rejected", request_id=request_id, reason=_NO_BUILD,
            )
            return
        plan = (self.config.plans_dir / f"{message['plan']}.yaml").resolve()
        if plan.parent != self.config.plans_dir or not plan.is_file():
            await self._send(
                "run_rejected", request_id=request_id,
                reason=f"unknown plan {message['plan']!r}",
            )
            return
        run_id = generate_run_id()
        argv = [
            sys.executable, "-m", "ubertooth_harness", "run-suite",
            f"--plan={plan}",
            f"--suite={message['suite']}",
            f"--hardware={self.config.hardware}",
            f"--host={self.config.host}",
            f"--ubertooth-source={self.config.ubertooth_source}",
            # The commit the controller listed for the ref, so a stale or
            # differently configured checkout cannot substitute another one.
            f"--ubertooth-ref={message['commit'] or message['ref']}",
            f"--run-id={run_id}",
            f"--output={self.config.output}",
        ]
        if message["commit"] and message["commit"] != message["ref"]:
            # Recorded next to the commit in the manifest and report.
            argv.append(f"--ubertooth-ref-name={message['ref']}")
        if self.config.libbtbb_source:
            argv += [
                f"--libbtbb-source={self.config.libbtbb_source}",
                f"--libbtbb-ref={self.config.libbtbb_ref}",
            ]
        argv += [f"--test={name}" for name in message["tests"]]
        if message["build"]:
            argv.append("--build")
        if self.config.allow_power_changes:
            argv.append("--allow-power-changes")
        if self.config.external_board_lock is not None:
            argv.append(f"--external-board-lock={self.config.external_board_lock}")
        environment = dict(os.environ)
        environment.update(self.config.child_environment or {})
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=REPOSITORY_ROOT,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                # Only the runner's explicit signal cancels the run.
                start_new_session=True,
            )
        except OSError as error:
            await self._send(
                "run_rejected", request_id=request_id,
                reason=f"cannot start run-suite: {error}",
            )
            return
        run = _Run(
            request_id=request_id,
            run_id=run_id,
            run_dir=self.config.output / run_id,
            process=process,
            started=time.monotonic(),
            stderr_tail=collections.deque(maxlen=_STDERR_TAIL_LINES),
            done=asyncio.Event(),
        )
        self._run = run
        asyncio.create_task(self._supervise(run))
        at = message["ref"]
        if message["commit"] and message["commit"] != at:
            at = f"{at} = {message['commit']}"
        self.log(f"run {run_id} started ({message['plan']}/{message['suite']} @ {at})")
        await self._send("run_accepted", request_id=request_id, run_id=run_id)
        tasks.append(asyncio.create_task(self._stream(run)))

    async def _cancel(self, message: dict) -> None:
        run = self._run
        if run is None or run.run_id != message["run_id"]:
            await self._send(
                "cancel_rejected", run_id=message["run_id"],
                reason="no such active run",
            )
            return
        self.log(f"cancel requested for {run.run_id}: {message['reason'] or 'no reason'}")
        self._signal_cancel(run)
        await self._send("cancel_accepted", run_id=run.run_id)

    def _signal_cancel(self, run: _Run) -> None:
        # run-suite maps SIGINT to its cooperative CancellationToken.
        if run.exit_code is None and not run.cancel_sent:
            run.cancel_sent = True
            try:
                run.process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    # ----- run supervision and streaming -----------------------------------

    async def _supervise(self, run: _Run) -> None:
        stream = run.process.stderr
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                run.stderr_tail.append(text)
        run.exit_code = await run.process.wait()
        run.done.set()
        self.log(f"run {run.run_id} exited {run.exit_code}")

    async def _stream(self, run: _Run) -> None:
        """Replay run.jsonl from the start, follow it, then report the end."""
        path = run.run_dir / RUN_LOG
        offset = 0
        pending = b""
        final = None
        malformed = None
        while True:
            finished = run.done.is_set()
            try:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    data = stream.read()
            except FileNotFoundError:
                data = b""
            offset += len(data)
            pending += data
            *lines, pending = pending.split(b"\n")
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    await self._send("event", run_id=run.run_id, event=record)
                except ProtocolError as error:
                    malformed = f"run.jsonl record rejected: {error}"
                    break
                except ValueError as error:
                    malformed = f"run.jsonl is not valid JSON: {error}"
                    break
                run.last_sequence = record["sequence"]
                run.last_event_type = record["event_type"]
                if record["event_type"] == "run_finished":
                    final = record
            if malformed or finished:
                break
            await asyncio.sleep(_TAIL_POLL_S)
        if malformed:
            # Keep following nothing further; report once the child ends.
            await run.done.wait()
        if final is not None and not malformed:
            await self._send(
                "run_finished", request_id=run.request_id, run_id=run.run_id,
                status=final["status"], exit_code=run.exit_code,
                run_directory=str(run.run_dir),
            )
        else:
            reason = malformed or (
                run.stderr_tail[-1] if run.stderr_tail
                else "run-suite exited without a run_finished record"
            )
            await self._send(
                "run_error", request_id=run.request_id, run_id=run.run_id,
                exit_code=run.exit_code, reason=reason[-4096:],
            )
        # Delivered: the runner is idle again.
        if self._run is run:
            self._run = None
        self.log(f"run {run.run_id} reported")
