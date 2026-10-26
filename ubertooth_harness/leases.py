"""Host-local board leases, so several agents can share the lab Ubertooths.

A lease is an exclusive ``flock`` on ``<lease dir>/<serial>.lock``. The kernel
drops it when the holding process exits, however it exits, so a lease can
never go stale. The lock file also records who holds it, for the error a
second run sees and for ``ubertooth-harness boards``.

A run takes every board it touches (selected, guard, alternated, and
power-isolated boards) all or nothing, before preflight, and holds them until
it ends. Taking all or nothing (release everything and retry when any board
is busy) means two multi-board runs can never deadlock.

The host lock, ``<lease dir>/host-power.lock``, is the same kind of lease for
the host itself: every harness command that switches USB power (power
isolation, StarTech or Acroname) or writes firmware (Flash reliability, DFU
images, a manual ``ubertooth-dfu`` under ``lease``) holds it for as long as
it runs, so power changes and firmware writes on one host never overlap.
A second such command fails at once and names the holder.

Lock order: the host lock is always taken first, then the boards in serial
order, in one all-or-nothing attempt; nothing ever blocks while holding a
lock (a busy lock releases everything and the attempt is retried until the
deadline). A process never takes the host lock after it already holds a
board lease, so no two harness processes can wait on each other.
"""

from __future__ import annotations

import datetime
import errno
import fcntl
import getpass
import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

LEASE_DIR_ENV = "UBERTOOTH_HARNESS_LEASE_DIR"
OWNER_ENV = "UBERTOOTH_HARNESS_OWNER"
DEFAULT_LEASE_DIR = Path.home() / ".ubertooth-harness" / "leases"
# A probe by `boards` holds a shared lock for microseconds; retry briefly so
# that probe never fails a run that did not ask to wait.
_BUSY_GRACE_S = 1.0
_POLL_S = 0.25
_FULL_SERIAL = re.compile(r"[0-9a-f]{32}")


class LeaseError(RuntimeError):
    """A board is held by someone else, or a lease file cannot be used."""


@dataclass(frozen=True)
class Board:
    """One board to lease: its registry alias and the key its lock file uses."""

    alias: str
    key: str


# The per-host power/firmware lock. Its key cannot clash with a board's,
# which is always 32 hex digits.
HOST_LOCK = Board("host lock", "host-power")


@dataclass
class Lease:
    board: Board
    path: Path
    fd: int
    holder: Mapping
    acquired_monotonic: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        return {
            "resource": self.board.alias,
            "key": self.board.key,
            "lock": str(self.path),
            "acquired_at": self.holder.get("acquired_at"),
        }


def lease_dir(override: Path | str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get(LEASE_DIR_ENV)
    return Path(env) if env else DEFAULT_LEASE_DIR


def board_key(alias: str, identity: Mapping) -> str:
    """Lock-file key: the board's full serial, lowercase.

    libubertooth matches -U case-insensitively against the START of a serial
    (a prefix), so a short serial
    and the full one select the same board but would take different locks.
    Only a full 32-hex-digit serial identifies a board unambiguously.
    """
    serial = str(identity.get("serial", "") or "").strip().lower()
    if not _FULL_SERIAL.fullmatch(serial) or serial.isdigit():
        raise LeaseError(
            f"{alias} needs a full 32-hex-digit serial in the hardware registry "
            f"to be leased; registered '{serial}'"
        )
    return serial


def _observes_every_board(invocation) -> bool:
    """Tests that read or enumerate every Ubertooth on the host, not only their roles.

    Flash reliability reads every other registered board as a guard after each
    write; an exclusive test checks that only its board enumerates.
    """
    return invocation.test_name == "Flash reliability" or bool(
        getattr(invocation, "exclusive_ubertooth", False)
    )


def boards_for_invocations(invocations: Iterable, registry) -> tuple[Board, ...]:
    """Every board the run touches: its roles, power-isolated and observed boards."""
    found: dict[str, Board] = {}
    for invocation in invocations:
        aliases = [
            alias for alias in invocation.resources.values()
            if registry.get(alias).type == "Ubertooth"
        ]
        aliases += [action.resource_alias for action in invocation.power_isolation]
        if _observes_every_board(invocation):
            aliases += list(registry.resolve_hardware(invocation.host, "Ubertooth", "each"))
        for alias in aliases:
            board = Board(alias, board_key(alias, registry.get(alias).identity))
            found.setdefault(board.key, board)
    return tuple(sorted(found.values(), key=lambda b: b.key))


def needs_host_lock(invocations: Iterable, catalog) -> bool:
    """True when the run switches USB power or writes firmware on its host.

    Power isolation (and the exclusive-Ubertooth check, which scans USB while
    another run could be switching it) and tests that write firmware hold the
    host lock for the whole run.
    """
    for invocation in invocations:
        if invocation.power_isolation or getattr(invocation, "exclusive_ubertooth", False):
            return True
        if catalog.get(invocation.test_name).writes_firmware:
            return True
    return False


def holder_info(*, run_id: str | None, owner: str | None, purpose: str) -> dict:
    return {
        "owner": owner or os.environ.get(OWNER_ENV) or getpass.getuser(),
        "run_id": run_id,
        "purpose": purpose,
        "pid": os.getpid(),
        "user": getpass.getuser(),
        "hostname": socket.gethostname(),
        "acquired_at": datetime.datetime.now(datetime.timezone.utc)
        .replace(tzinfo=None).isoformat(timespec="milliseconds"),
    }


def read_holder(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if text.strip() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def describe_holder(holder: Mapping) -> str:
    if not holder:
        return "an unknown holder"
    parts = [str(holder.get("owner") or holder.get("user") or "unknown")]
    if holder.get("run_id"):
        parts.append(f"run {holder['run_id']}")
    if holder.get("purpose"):
        parts.append(str(holder["purpose"]))
    if holder.get("pid"):
        parts.append(f"pid {holder['pid']}")
    if holder.get("acquired_at"):
        parts.append(f"since {holder['acquired_at']} UTC")
    return ", ".join(parts)


def _stamp(fd: int, record: Mapping | None) -> None:
    """Record the holder in a lock file we hold; None clears it on release."""
    data = b"" if record is None else (json.dumps(record, sort_keys=True) + "\n").encode()
    os.ftruncate(fd, 0)
    if data:
        os.pwrite(fd, data, 0)


def _try_lock(path: Path, mode: int) -> int | None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    return fd


class LeaseSet:
    """The leases one process holds; release() (or process exit) drops them."""

    def __init__(self, leases: Sequence[Lease] = ()):
        self.leases = list(leases)

    def release(self) -> None:
        while self.leases:
            lease = self.leases.pop()
            try:
                _stamp(lease.fd, None)  # nobody reads a stale holder later
            except OSError:
                pass
            try:
                os.close(lease.fd)
            except OSError:
                pass

    def to_manifest(self) -> list[dict]:
        return [lease.to_dict() for lease in self.leases]

    def __enter__(self) -> "LeaseSet":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def acquire(
    boards: Sequence[Board],
    holder: Mapping,
    *,
    directory: Path | str | None = None,
    wait_s: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    host_lock: bool = False,
) -> LeaseSet:
    """Lease every board, all or nothing, waiting up to wait_s for busy ones.

    With host_lock, the host lock is taken too, before any board (see the
    module docstring for the order).
    """
    if not boards and not host_lock:
        return LeaseSet()
    root = lease_dir(directory)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LeaseError(f"cannot create lease directory {root}: {error}") from error
    deadline = clock() + max(wait_s, _BUSY_GRACE_S)
    # One lock per board: flock is per open file description, so opening the
    # same file twice in one process would conflict with itself.
    unique: dict[str, Board] = {}
    for board in boards:
        unique.setdefault(board.key, board)  # first alias wins, as in boards_for_invocations
    ordered = sorted(unique.values(), key=lambda b: b.key)
    if host_lock:
        ordered.insert(0, HOST_LOCK)
    while True:
        held: list[Lease] = []
        busy: tuple[Board, Path] | None = None
        try:
            for board in ordered:
                path = root / f"{board.key}.lock"
                fd = _try_lock(path, fcntl.LOCK_EX)
                if fd is None:
                    busy = (board, path)
                    break
                held.append(Lease(board, path, fd, holder))
                _stamp(fd, dict(holder, resource=board.alias))
        except OSError as error:
            LeaseSet(held).release()
            raise LeaseError(f"cannot lease boards in {root}: {error}") from error
        if busy is None:
            return LeaseSet(held)
        LeaseSet(held).release()
        board, path = busy
        if clock() >= deadline:
            waited = f" after waiting {wait_s:g}s" if wait_s else ""
            if board == HOST_LOCK:
                raise LeaseError(
                    f"this host's power/firmware lock is in use{waited}: held by "
                    f"{describe_holder(read_holder(path))} ({path}); another "
                    "harness command is switching USB power or writing firmware "
                    "here; retry when it ends, or use --wait-for-boards MINUTES"
                )
            raise LeaseError(
                f"{board.alias} is in use{waited}: held by "
                f"{describe_holder(read_holder(path))} ({path}); "
                "use --wait-for-boards MINUTES to wait for it"
            )
        sleep(_POLL_S)


def board_states(
    boards: Sequence[Board], *, directory: Path | str | None = None
) -> list[dict]:
    """Free or held, and by whom, for each board. Never blocks a run for long."""
    root = lease_dir(directory)
    states = []
    for board in boards:
        path = root / f"{board.key}.lock"
        state = {"resource": board.alias, "key": board.key, "held": False, "holder": {}}
        if path.exists():
            fd = _try_lock(path, fcntl.LOCK_SH)
            if fd is None:
                state["held"] = True
                state["holder"] = read_holder(path)
            else:
                os.close(fd)
        states.append(state)
    return states


def host_lock_state(*, directory: Path | str | None = None) -> dict:
    """Free or held, and by whom, for the host lock (as board_states)."""
    state = board_states([HOST_LOCK], directory=directory)[0]
    state["host_lock"] = True
    return state


def exit_status(returncode: int) -> int:
    """A shell-style exit status: a child killed by signal N exits 128 + N."""
    return 128 - returncode if returncode < 0 else returncode


def held_for(holder: Mapping, now: datetime.datetime | None = None) -> str:
    started = holder.get("acquired_at")
    if not started:
        return ""
    try:
        then = datetime.datetime.fromisoformat(str(started))
    except ValueError:
        return ""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    seconds = max(0, int((now - then).total_seconds()))
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m{seconds % 60:02d}s"
