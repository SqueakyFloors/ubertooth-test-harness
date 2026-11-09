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
import contextlib
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

from ubertooth_harness.enumeration import (
    EnumerationError,
    UbertoothEnumerator,
    local_enumerator,
)
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
from ubertooth_harness.protocol import (
    MAX_BOARDS,
    valid_boards,
    valid_devices,
    valid_file_page,
)
from ubertooth_harness import registryedit
from ubertooth_harness.registryedit import BoardEdit, RegistryEditError
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from ubertooth_harness.runfiles import OutputFileError, read_output_page


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLANS_DIR = REPOSITORY_ROOT / "testplans"
DEFAULT_HEARTBEAT_INTERVAL_S = 10.0
DEFAULT_INITIAL_BACKOFF_S = 0.5
DEFAULT_MAX_BACKOFF_S = 30.0
_CONNECT_TIMEOUT_S = 10.0
_TAIL_POLL_S = 0.05
_STDERR_TAIL_LINES = 20
# File page requests answered at once. Finding a recorded file means reading
# the run's run.jsonl, which a soak makes tens of megabytes, so a flood of
# requests is held rather than served: the controller asks again.
MAX_FILE_REQUESTS = 4
# Board firmware revisions kept, by serial. Far past any bench; a bound so a
# runner that outlives many registry changes cannot grow the map forever.
_MAX_REVISIONS = 256
_NO_BUILD = (
    "this runner has no libbtbb, so it cannot build; restart it with "
    "--libbtbb-source and --libbtbb-ref (scripts/deploy.sh sets them up by "
    "default), or run without Build"
)


# Tells "no registry was handed to me, read one" from "the registry would
# not load", which is an answer of its own and must not be read again.
_SAME = object()


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
    # Let a dispatched run grant cap_net_raw+ep to a private copy of its
    # own ubertooth-follow, so the tests that page a target over HCI can run
    # (hciprivilege.py, #401). A deploy-time choice, like the power one
    # above and unlike the destructive opt-in, which a runner never passes
    # on: a bench whose adapter tests are part of its gate turns this on
    # once, and a run that is not given it records those tests blocked
    # rather than failing their cases.
    allow_hci_capability: bool = False
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


def _holder_line(holder: Mapping, external: Mapping) -> str:
    """Who holds a board, in words a browser may see; "" when free.

    Built field by field rather than from ``leases.describe_holder``, which
    also carries the lease's ``purpose``: for a lease taken by hand that is
    the whole command line, which names paths on the target. This answer
    travels to the GUI and into a refusal, so it keeps to who and since
    when.

    Both parties when both hold it: the harness lease and the bench's own
    lock file are separate claims, and a reader who sees only one wonders
    why the board is busy.
    """
    parts = []
    if holder:
        said = [str(holder.get("owner") or holder.get("user") or "someone")]
        if holder.get("run_id"):
            said.append(f"run {holder['run_id']}")
        if holder.get("pid"):
            said.append(f"pid {holder['pid']}")
        if holder.get("acquired_at"):
            said.append(f"since {holder['acquired_at']} UTC")
        parts.append(", ".join(said))
    if external.get("held"):
        said = str(external.get("holder") or "").strip()
        parts.append(
            f"a bench lock ({said})" if said else "a bench lock"
        )
    return "; ".join(part for part in parts if part)[:400]


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
        # File page requests being answered now, each in its own task.
        self._file_tasks: set = set()
        # One registry edit at a time, whatever the controller asks: two
        # overlapping writes of one file would each have read it before the
        # other wrote.
        self._registry_lock = asyncio.Lock()
        # The firmware revision the last run that read a board found, by
        # serial. Learned from the records the runner already forwards, so
        # no board is opened for it; empty until a run has flashed or read
        # one, which is what the board's status then says.
        self._revisions: dict[str, str] = {}
        # The board list last read, which register and every heartbeat
        # carry, and the task that refreshes it.
        self._boards: list = []
        self._refreshing: asyncio.Task | None = None

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
                # Enumeration runs a command (ioreg) on macOS, so it is read
                # off the event loop: a slow read must not stall heartbeats or
                # an active run's events.
                registration = await asyncio.to_thread(self._registration)
                await self._send("register", **registration)
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
                # A file request being answered is left to finish rather
                # than cancelled: cancelling would not stop the read it is
                # waiting on, only forget it, and the next connection would
                # admit that many requests again on top of it. Its answer
                # then finds the connection gone and is logged.
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
        serials, enumeration_error = self._enumerated()
        self._boards = self._board_states(serials)
        return {
            "host": self.config.host,
            "hostname": platform.node(),
            "os": {key: (None if value is None else str(value))
                   for key, value in identity["os"].items()},
            "python": identity["python"]["version"],
            "ubertooth_serials": serials,
            "enumeration_error": enumeration_error,
            "boards": list(self._boards),
            "heartbeat_interval_s": self.config.heartbeat_interval_s,
            "active_run_id": self._run.run_id if self._run else None,
        }

    def _enumerated(self) -> tuple[list | None, str | None]:
        """The serials USB has, or None with the reason it could not be read.

        Read-only: sysfs on Linux, ioreg on macOS. Neither opens a device,
        so neither sends a stop to a board that is capturing.
        """
        enumerator = self._usb()
        if isinstance(enumerator, str):
            return None, enumerator
        try:
            return sorted(enumerator.serials()), None
        except Exception as error:  # noqa: BLE001 - the reason is the answer
            return None, str(error)

    def _usb(self):
        """This target's enumerator, or why it has none (as a string)."""
        if self.enumerator is not None:
            return self.enumerator
        try:
            return local_enumerator()
        except EnumerationError as error:
            return str(error)

    def _registry(self) -> FileResourceRegistry | None:
        """This runner's own hardware registry, or None when it will not load.

        Read on demand rather than held: the file is the runner's to edit,
        and a run reads it again for itself, so there is nothing to keep in
        step with.
        """
        try:
            return FileResourceRegistry.load(self.config.hardware)
        except RegistryError as error:
            self.log(f"cannot read the hardware registry: {error}")
            return None

    def _registered_boards(self, registry) -> list:
        """The Ubertooths this host has, as leases.Board, in registry order."""
        if registry is None:
            return []
        boards = []
        for alias in registry.hardware(self.config.host, "Ubertooth"):
            try:
                boards.append(leases.board_for_resource(registry.get(alias)))
            except (leases.LeaseError, RegistryError) as error:
                # A board recorded without the serial its lease key comes
                # from. It cannot be leased, so it cannot be reported on
                # either; the registry itself is where that is fixed.
                self.log(f"board {alias} cannot be leased: {error}")
        return boards

    def _board_states(self, serials, registry=_SAME) -> list:
        """One status entry per registered board of this host.

        Only what can be established without opening a device: whether USB
        still has the serial, whether a lease holds the board, and the
        firmware revision the last run that read it found. Never raises: a
        target whose registry or lease directory cannot be read registers
        with no board list rather than not registering.
        """
        try:
            registry = self._registry() if registry is _SAME else registry
            boards = self._registered_boards(registry)
            held = self._lease_states(boards)
            present = None if serials is None else {
                str(serial).lower() for serial in serials
            }
            states = []
            for board in boards:
                resource = registry.get(board.alias)
                power = resource.power or {}
                state = held.get(board.alias, {})
                holder = state.get("holder") or {}
                external = state.get("external") or {}
                # The serial as the registry spells it; compared in lower
                # case, because USB reports it that way and one serial
                # written two ways is still one board.
                serial = str(resource.identity.get("serial", "") or "").strip()
                entry = {
                    "alias": board.alias,
                    "serial": serial,
                    "present": (
                        None if present is None else serial.lower() in present
                    ),
                    "held": bool(state.get("held")),
                    "holder": _holder_line(holder, external),
                    "firmware_revision": self._revisions.get(serial.lower()),
                    "power_controller": (
                        str(power["controller"]) if power.get("controller")
                        else None
                    ),
                    "power_port": (
                        power["port"]
                        if isinstance(power.get("port"), int)
                        and not isinstance(power.get("port"), bool)
                        else None
                    ),
                }
                # One entry at a time, so a board this protocol cannot
                # describe costs its own line and not the whole list.
                if not valid_boards([entry]):
                    self.log(
                        f"board {board.alias} cannot be reported over this "
                        "protocol; leaving it out of the board list"
                    )
                    continue
                states.append(entry)
            if len(states) > MAX_BOARDS:
                # More registered boards than one message carries. The
                # first MAX_BOARDS of them, in registry order, beats none.
                self.log(
                    f"this host has {len(states)} registered Ubertooths; "
                    f"reporting the first {MAX_BOARDS}"
                )
                states = states[:MAX_BOARDS]
            return states
        except Exception as error:  # noqa: BLE001 - registration comes first
            self.log(f"cannot list the registered boards: {error}")
        return []

    def _lease_states(self, boards) -> dict:
        """What holds each board now, by alias; {} when it cannot be read.

        A listing, not a claim: ``board_states`` probes each lease without
        taking one, so reading this never keeps a run waiting.
        """
        if not boards:
            return {}
        try:
            states = leases.board_states(
                boards,
                external_template=leases.external_lock_template(
                    self.config.external_board_lock
                ) or None,
            )
        except (leases.LeaseError, OSError) as error:
            self.log(f"cannot read the board leases: {error}")
            return {}
        return {state["resource"]: state for state in states}

    def _learn_revision(self, record: Mapping) -> None:
        """Note a board's firmware revision from a record the run wrote.

        The flash stage reports what each board ran before it and what it
        runs after, so the revision comes off records the runner already
        forwards: nothing is opened, and no run is slowed to find it.
        """
        if record.get("event_type") != "build_flash_board_finished":
            return
        details = record.get("details")
        if not isinstance(details, Mapping):
            return
        serial = str(details.get("serial") or "").strip().lower()
        revision = str(
            details.get("after") or details.get("before") or ""
        ).strip()
        if serial and revision:
            if (
                len(self._revisions) >= _MAX_REVISIONS
                and serial not in self._revisions
            ):
                # A bench has a handful of boards, so this never fills in
                # practice; a long-lived runner whose registry keeps
                # changing must still not grow a map forever.
                self._revisions.pop(next(iter(self._revisions)))
            self._revisions[serial] = revision[:200]

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
                # The last list read, never a fresh one: the scan runs a
                # command (ioreg's own timeout is longer than a heartbeat
                # interval) and reads lock files, and a heartbeat that waited
                # for it could arrive late enough for the controller to call
                # this target offline in the middle of a run.
                boards=list(self._boards),
            )
            self._start_board_refresh()

    def _start_board_refresh(self) -> None:
        """Read the boards again off the event loop, one read at a time."""
        if self._refreshing is not None and not self._refreshing.done():
            return
        self._refreshing = asyncio.create_task(self._refresh_boards())

    async def _refresh_boards(self) -> None:
        try:
            self._boards = await asyncio.to_thread(self._read_boards)
        except Exception as error:  # noqa: BLE001 - the heartbeat goes on
            self.log(f"could not read the registered boards: {error}")

    def _read_boards(self) -> list:
        serials, _ = self._enumerated()
        return self._board_states(serials)

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
        elif message["type"] == "run_file":
            await self._take_file_request(message)
        elif message["type"] == "list_devices":
            await self._list_devices(message)
        elif message["type"] == "update_registry":
            await self._update_registry(message)

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
        # Always passed, so a run's record says which of the three it was
        # asked for rather than leaving the default to be inferred. Before the
        # build options, because nothing of the runner's own may follow
        # --with-python, whose value is optional.
        argv.append(f"--bootloader={message['bootloader']}")
        if message["build"]:
            argv.append("--build")
            if not message["flash_build"]:
                argv.append("--no-flash-build")
            # The same options the command line takes: --with-firmware
            # records the run's "test build" image (which tests that check or
            # write an image need), --with-btbr the alternate "btbr build"
            # image beside it (which only the btbr console test flashes), and
            # --with-python builds the Python tools. The protocol refuses any
            # of them without build, so there is no combination here that
            # run-suite would reject.
            if message["with_firmware"]:
                argv.append("--with-firmware")
            if message["with_btbr"]:
                argv.append("--with-btbr")
            if message["with_python"]:
                # Bare, as the documented command line has it: run-suite
                # then uses python3 on the target's PATH.
                argv.append("--with-python")
        if self.config.allow_power_changes:
            argv.append("--allow-power-changes")
        if self.config.allow_hci_capability:
            argv.append("--allow-hci-capability")
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

    # ----- board management ------------------------------------------------

    async def _list_devices(self, message: dict) -> None:
        """Answer with every attached Ubertooth, registered or new.

        A read-only scan of what the kernel already knows: no device is
        opened and no command is sent, so a board in the middle of a capture
        is not disturbed. In its own task, as a file request is: the scan
        runs a command whose own timeout is longer than a heartbeat
        interval, and nothing may hold up the next message on this
        connection, which may be a cancel.
        """
        task = asyncio.create_task(self._answer_devices(message))
        self._file_tasks.add(task)
        task.add_done_callback(self._file_tasks.discard)

    async def _answer_devices(self, message: dict) -> None:
        fields = await asyncio.to_thread(self._devices)
        try:
            await self._send("devices", request_id=message["request_id"], **fields)
        except Exception as error:  # noqa: BLE001 - the connection may have gone
            self.log(f"could not answer the device listing: {error}")

    def _devices(self) -> dict:
        registry = self._registry()
        serials, enumeration_error = self._enumerated()
        boards = self._board_states(serials, registry)
        registered = {
            str(board["serial"]).lower(): board["alias"] for board in boards
        }
        devices, error = [], enumeration_error
        enumerator = self._usb()
        if isinstance(enumerator, str):
            error = enumerator
        else:
            try:
                devices = [
                    {**device.view(),
                     "alias": registered.get(device.serial.lower())}
                    for device in enumerator.devices()
                ]
            except Exception as error_found:  # noqa: BLE001 - the reason is the answer
                devices, error = [], str(error_found)
        if not valid_devices(devices):
            devices, error = [], (
                "this target found Ubertooths it cannot describe over this "
                "protocol"
            )
        controllers = []
        suggested = None
        if registry is not None:
            try:
                controllers = registryedit.power_controllers(
                    registry, self.config.host
                )
                suggested = registryedit.suggested_alias(
                    self.config.host, registry.resources
                ) or None
            except Exception as problem:  # noqa: BLE001 - the listing stands
                self.log(f"cannot describe the power controllers: {problem}")
        return {
            "devices": devices, "error": error,
            "power_controllers": controllers, "suggested_alias": suggested,
            "boards": boards,
        }

    async def _update_registry(self, message: dict) -> None:
        """Register or unregister one Ubertooth, or say why not.

        Refused outright while a run is active, and for a board a lease
        holds: a run reads the registry when it starts, and a board somebody
        else is using is not ours to unregister. Everything else is the
        registry's own validation, which happens before the file is touched.
        """
        request_id = message["request_id"]
        async with self._registry_lock:
            if self._run is not None:
                await self._send(
                    "registry_refused", request_id=request_id, status=409,
                    reason=(
                        f"run {self._run.run_id} is active on this target; a "
                        "run reads the registry when it starts, so boards are "
                        "managed between runs"
                    ),
                )
                return
            try:
                edit = BoardEdit(
                    action=message["action"], alias=message["alias"],
                    serial=(message["serial"] or "").strip().lower(),
                    power_controller=message["power_controller"],
                    power_port=message["power_port"],
                )
            except RegistryEditError as error:
                await self._send(
                    "registry_refused", request_id=request_id,
                    status=error.status, reason=error.detail,
                )
                return
            answer, fields = await asyncio.to_thread(self._apply_edit, edit)
            await self._send(answer, request_id=request_id, **fields)
            if answer == "registry_refused":
                self.log(
                    f"refused a registry change on {edit.alias}: "
                    f"{fields['reason']}"
                )
            else:
                # The change, never the file: a log line is read by people
                # who have no business knowing where the registry lives, and
                # a full serial identifies a board nobody asked about.
                self.log(f"registry: {edit.describe()}")

    def _apply_edit(self, edit: BoardEdit) -> tuple[str, dict]:
        """Make the change, or say why it was not made. Never raises."""
        try:
            with self._not_in_use(edit):
                registry = registryedit.apply_edit(
                    self.config.hardware, self.config.host, edit
                )
        except RegistryEditError as error:
            return "registry_refused", {
                "status": error.status, "reason": error.detail,
            }
        except RegistryError as error:
            # The file did not hold a registry to begin with, or the change
            # made one it refuses. Nothing was kept either way.
            return "registry_refused", {
                "status": 409,
                "reason": f"this target's hardware registry refused the "
                          f"change: {error}",
            }
        except Exception as error:  # noqa: BLE001 - silence is the worst answer
            return "registry_refused", {
                "status": 503,
                "reason": f"this target could not change its hardware "
                          f"registry: {type(error).__name__}",
            }
        serials, _ = self._enumerated()
        self._boards = self._board_states(serials, registry)
        return "registry_updated", {
            "action": edit.action, "alias": edit.alias,
            "boards": list(self._boards),
            "ubertooth_serials": serials,
        }

    @contextlib.contextmanager
    def _not_in_use(self, edit: BoardEdit):
        """Hold the board's lease while it is unregistered, or refuse.

        Taking the lease is what makes "not while somebody has it" true
        rather than merely checked: a lease taken between a check and the
        write would otherwise let a board be unregistered just as a run
        picked it up, and a ``run-suite`` somebody started on the host by
        hand is not this runner's own run. It also fails closed, because a
        lease directory that cannot be read means the lease cannot be taken.

        Nothing is held for an ``add``: a board the registry has not got
        cannot be leased under an alias that does not exist yet.
        """
        if edit.action != registryedit.REMOVE:
            yield
            return
        board = self._board_to_lease(edit.alias)
        if board is None:
            # Not registered, or registered without the serial its lease key
            # comes from. apply_edit says which, and why, in its own words.
            yield
            return
        try:
            held = leases.acquire(
                [board],
                leases.holder_info(
                    run_id=None, owner=None,
                    purpose="unregistering this board",
                ),
                external_template=leases.external_lock_template(
                    self.config.external_board_lock
                ) or None,
                note=lambda line: self.log(line),
            )
        except leases.LeaseError as error:
            raise RegistryEditError(
                f"{edit.alias} is in use, so it stays registered until "
                f"whatever has it gives it back: {error}", 409,
            ) from error
        except OSError as error:
            raise RegistryEditError(
                f"this target could not take {edit.alias}'s lease, so it "
                f"will not unregister a board it cannot hold: "
                f"{type(error).__name__}", 503,
            ) from error
        try:
            yield
        finally:
            held.release()

    def _board_to_lease(self, alias: str):
        """The board to hold while unregistering ``alias``, or None."""
        registry = self._registry()
        if registry is None:
            return None
        try:
            resource = registry.get(alias)
        except RegistryError:
            return None
        if resource.type != registryedit.UBERTOOTH_TYPE:
            return None
        try:
            return leases.board_for_resource(resource)
        except leases.LeaseError:
            return None

    # ----- saved output files ----------------------------------------------


    async def _take_file_request(self, message: dict) -> None:
        """Answer a run_file request in its own task, or refuse it as busy.

        Its own task because finding and reading the page is file I/O on a
        log that can be large: answering it inline would hold up the next
        message on this connection, which may be a cancel.
        """
        if len(self._file_tasks) >= MAX_FILE_REQUESTS:
            await self._send(
                "run_file_refused", request_id=message["request_id"],
                run_id=message["run_id"], status=503,
                reason=f"this runner answers {MAX_FILE_REQUESTS} file requests "
                       "at once and is busy with that many; ask again",
            )
            return
        task = asyncio.create_task(self._send_run_file(message))
        self._file_tasks.add(task)
        task.add_done_callback(self._file_tasks.discard)

    async def _send_run_file(self, message: dict) -> None:
        run_id = message["run_id"]
        answer, fields = await asyncio.to_thread(
            self._answer_run_file, run_id, message["path"],
            message["offset"], message["limit"],
        )
        try:
            await self._send(
                answer, request_id=message["request_id"], run_id=run_id, **fields
            )
        except Exception as error:  # noqa: BLE001 - the connection may have gone
            self.log(f"could not answer the file request for {run_id}: {error}")
            return
        # After the answer, so that nothing between here and the answer can
        # keep it from being sent.
        if answer == "run_file_refused":
            self.log(f"refused a file of run {run_id}: {fields['reason']}")

    def _answer_run_file(
        self, run_id: str, path: str, offset: int, limit: int
    ) -> tuple[str, dict]:
        """The answer to one run_file request: a page, or a refusal.

        Every failure becomes a refusal, including one nothing anticipated,
        because a request this runner leaves unanswered makes the caller on
        the other side wait out its timeout for an answer that never comes.
        """
        try:
            page = self._read_page(run_id, path, offset, limit)
        except OutputFileError as error:
            return "run_file_refused", {
                "status": error.status, "reason": error.detail,
            }
        except Exception as error:  # noqa: BLE001 - silence is the worst answer
            # A run.jsonl that cannot be read at all, or one holding a record
            # a page cannot be built from. A record is ordinary data, and
            # data takes many shapes: a size that is not a number, one too
            # large for an integer, a details field that is not an object.
            # Whatever it was, the request is answered. The reason names the
            # run, the path and the kind of fault, never a path on this
            # target.
            return "run_file_refused", {
                "status": 404,
                "reason": f"{path!r} cannot be served from run {run_id}: "
                          f"{type(error).__name__}",
            }
        if not valid_file_page(page):
            # The same reason: a record this protocol cannot carry would make
            # encoding the answer fail, and silence is the worst answer.
            return "run_file_refused", {
                "status": 404,
                "reason": f"{path!r} is recorded in a way this protocol "
                          "cannot carry",
            }
        return "run_file_page", {"page": page}

    def _read_page(self, run_id: str, path: str, offset: int, limit: int) -> dict:
        """One page of a file of this runner's own run ``run_id``.

        The same confinement as the controller's own route: only a path that
        run's run.jsonl records is read, and only from inside that run
        directory (``runfiles``). Every refusal names the run ID or the path
        and nothing else, so an answer never says where the run directory is
        on this target.
        """
        named = self.config.output / run_id
        # No symbolic link is followed for the run directory either, as none
        # is for a file inside it: a link would let one run ID serve another
        # run's records, which is what reading only recorded files prevents.
        # Checked before resolving, because resolving replaces a link with
        # its target, and its target may sit beside it.
        if named.is_symlink():
            raise OutputFileError(f"this runner has no run {run_id!r}", 404)
        directory = named.resolve()
        # The protocol lets only one path component of safe characters
        # through; this also refuses one that resolves anywhere but directly
        # under this runner's output directory.
        if directory.parent != self.config.output or not directory.is_dir():
            raise OutputFileError(f"this runner has no run {run_id!r}", 404)
        try:
            return read_output_page(directory, path, offset, limit)
        except OutputFileError as error:
            if not error.on_target:
                raise
            # Recorded by the run, but no longer in this runner's own run
            # directory. Saying it is "on the target" would be absurd here.
            raise OutputFileError(
                f"{path!r} is recorded by run {run_id} but is no longer in its "
                "run directory on this target", 404,
            ) from error

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
                # What this run read off a board, kept for the board's own
                # status once the run is over.
                self._learn_revision(record)
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
