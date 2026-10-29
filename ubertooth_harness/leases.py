"""Host-local board leases, so several agents can share the lab Ubertooths
(and the nRF52840 DKs used as BLE test devices, keyed by J-Link serial).

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

External board locks: a lab may already lock its boards with its own
per-board ``flock`` files (a script or wrapper that knows nothing about
leases). With a template such as ``/some/dir/board-{serial}.lock`` (option
``--external-board-lock`` or ``UBERTOOTH_HARNESS_EXTERNAL_BOARD_LOCK``), every
lease also takes an exclusive ``flock`` on that board's external file, in the
same all-or-nothing attempt, and ``boards`` reports a board whose external
lock alone is held as held. The harness never writes to an external file; it
only shows what the file holds.

Lock order, one hierarchy for every acquirer:

1. the external board locks, in serial order;
2. the host lock;
3. the board leases, in serial order.

All are taken in one all-or-nothing attempt and nothing ever blocks while
holding a lock (a busy lock releases everything and the attempt is retried
until the deadline). Without a template this is the old order (host lock,
then boards). A lab wrapper that takes its external board locks and then runs
``lease`` (which may take the host lock, then leases) follows the same
hierarchy, so neither it nor the harness can wait on the other in a cycle,
and a process never takes a lock earlier in the order after a later one.
A command run by such a wrapper must not be given the template too: its
wrapper already holds those files, so it would find them busy and refuse.
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
EXTERNAL_LOCK_ENV = "UBERTOOTH_HARNESS_EXTERNAL_BOARD_LOCK"
SERIAL_FIELD = "{serial}"
# How much of an external lock file `boards` and errors show.
_EXTERNAL_HOLDER_BYTES = 4096
_EXTERNAL_HOLDER_CHARS = 200
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
    # An external board lock (see the module docstring), not a lease file.
    external: bool = False

    def to_dict(self) -> dict:
        record = {
            "resource": self.board.alias,
            "key": self.board.key,
            "lock": str(self.path),
            "acquired_at": self.holder.get("acquired_at"),
        }
        if self.external:
            record["external"] = True
        return record


def lease_dir(override: Path | str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get(LEASE_DIR_ENV)
    return Path(env) if env else DEFAULT_LEASE_DIR


def external_lock_template(
    value: str | None, *, environ: Mapping[str, str] | None = None
) -> str | None:
    """The validated external board lock template, or None when unset.

    value is the --external-board-lock option; None falls back to
    $UBERTOOTH_HARNESS_EXTERNAL_BOARD_LOCK. An empty value (option or
    variable) means no external lock, so ``--external-board-lock ''`` turns
    the variable off for one command. The template must be an absolute path
    after ~ expansion and contain exactly one ``{serial}`` and no other brace;
    the result has ~ expanded.
    """
    if value is None:
        value = (os.environ if environ is None else environ).get(EXTERNAL_LOCK_ENV, "")
    if not value:
        return None
    template = os.path.expanduser(value)
    where = f"external board lock template {value!r}"
    if template.count(SERIAL_FIELD) != 1:
        raise LeaseError(f"{where} must contain {SERIAL_FIELD} exactly once")
    rest = template.replace(SERIAL_FIELD, "")
    if "{" in rest or "}" in rest:
        raise LeaseError(f"{where} may contain no brace other than {SERIAL_FIELD}")
    if not os.path.isabs(template):
        raise LeaseError(f"{where} must be an absolute path (or start with ~/)")
    if "\0" in template or template.endswith("/"):
        raise LeaseError(f"{where} must name a file")
    return template


def external_lock_path(template: str, board: Board) -> Path:
    """The board's external lock file: the template with its full serial."""
    return Path(template.replace(SERIAL_FIELD, board.key))


def read_external_holder(path: Path) -> str:
    """What an external lock file says about its holder, for display only.

    Any format is accepted: a JSON object is described like a lease's
    holder, other text is shown as its first non-empty line, shortened.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_EXTERNAL_HOLDER_BYTES)
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and data:
        return describe_holder(data)
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    line = "".join(ch if ch.isprintable() else "?" for ch in line)
    if len(line) > _EXTERNAL_HOLDER_CHARS:
        line = line[:_EXTERNAL_HOLDER_CHARS - 3] + "..."
    return line


def describe_external(path: Path, holder: str) -> str:
    """'outside the harness: <path> (<holder>)', after 'held' in boards and errors."""
    return f"outside the harness: {path}" + (f" ({holder})" if holder else "")


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


# nRF52840 DKs (BLE test devices) are leased too, by J-Link serial; their
# keys cannot clash with an Ubertooth's 32 hex digits or the host lock.
DK_TYPE = "nRF52840 DK"
LEASED_TYPES = ("Ubertooth", DK_TYPE)


def resource_key(resource) -> str:
    """Lock-file key of a leasable registry resource (Ubertooth or DK)."""
    if resource.type == DK_TYPE:
        serial = str(resource.identity.get("jlink serial", "") or "").strip()
        if not serial.isdigit():
            raise LeaseError(
                f"{resource.alias} needs a 'jlink serial' in the hardware registry "
                f"to be leased; registered '{serial}'"
            )
        return f"nrf52840dk-{int(serial)}"
    return board_key(resource.alias, resource.identity)


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
            if registry.get(alias).type in LEASED_TYPES
        ]
        aliases += [action.resource_alias for action in invocation.power_isolation]
        if _observes_every_board(invocation):
            aliases += list(registry.resolve_hardware(invocation.host, "Ubertooth", "each"))
        for alias in aliases:
            board = Board(alias, resource_key(registry.get(alias)))
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


def _try_lock(path: Path, mode: int, *, flags: int = os.O_RDWR) -> int | None:
    fd = os.open(path, flags | os.O_CREAT, 0o644)
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
            if not lease.external:  # an external file is never written
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
    external_template: str | None = None,
) -> LeaseSet:
    """Lease every board, all or nothing, waiting up to wait_s for busy ones.

    With host_lock, the host lock is taken too; with external_template (an
    already validated external_lock_template()), each board's external lock
    too (see the module docstring for the order).
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
    by_serial = sorted(unique.values(), key=lambda b: b.key)
    # (board, lock file, external) in lock order.
    ordered: list[tuple[Board, Path, bool]] = []
    if external_template:
        for board in by_serial:
            path = external_lock_path(external_template, board)
            if os.path.abspath(path) == os.path.abspath(root / f"{board.key}.lock"):
                raise LeaseError(
                    f"external board lock {path} is the harness's own lease file; "
                    "point the template at the lab's lock files"
                )
            if not path.parent.is_dir():
                # A mistyped template must not start a lock tree nobody else uses.
                raise LeaseError(
                    f"external board lock directory {path.parent} does not exist "
                    f"(template {external_template})"
                )
            ordered.append((board, path, True))
    if host_lock:
        ordered.append((HOST_LOCK, root / f"{HOST_LOCK.key}.lock", False))
    ordered += [(board, root / f"{board.key}.lock", False) for board in by_serial]
    while True:
        held: list[Lease] = []
        busy: tuple[Board, Path, bool] | None = None
        where = root
        try:
            for board, path, external in ordered:
                where = path.parent if external else root
                # An external file may belong to another user: read access
                # is enough for flock, and it is never written.
                fd = _try_lock(
                    path, fcntl.LOCK_EX, flags=os.O_RDONLY if external else os.O_RDWR
                )
                if fd is None:
                    busy = (board, path, external)
                    break
                held.append(Lease(board, path, fd, holder, external=external))
                if not external:
                    _stamp(fd, dict(holder, resource=board.alias))
        except OSError as error:
            LeaseSet(held).release()
            raise LeaseError(f"cannot lease boards in {where}: {error}") from error
        if busy is None:
            return LeaseSet(held)
        LeaseSet(held).release()
        board, path, external = busy
        if clock() >= deadline:
            waited = f" after waiting {wait_s:g}s" if wait_s else ""
            if external:
                raise LeaseError(
                    f"{board.alias} is in use{waited}: held "
                    f"{describe_external(path, read_external_holder(path))}; "
                    "use --wait-for-boards MINUTES to wait for it"
                )
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
    boards: Sequence[Board],
    *,
    directory: Path | str | None = None,
    external_template: str | None = None,
) -> list[dict]:
    """Free or held, and by whom, for each board. Never blocks a run for long.

    With external_template, a board whose external lock is held counts as
    held even without a lease; ``external`` then gives that lock's file,
    whether it is held, and what the file says about its holder.
    """
    root = lease_dir(directory)
    states = []
    for board in boards:
        path = root / f"{board.key}.lock"
        state = {"resource": board.alias, "key": board.key, "held": False, "holder": {}}
        if _probe(path):
            state["held"] = True
            state["holder"] = read_holder(path)
        if external_template:
            external = external_lock_path(external_template, board)
            try:
                held = _probe(external, flags=os.O_RDONLY)
                problem = ""
            except OSError as error:  # e.g. another user's unreadable file
                held, problem = False, str(error)
            state["external"] = {
                "lock": str(external),
                "held": held,
                "holder": read_external_holder(external) if held else "",
            }
            if problem:
                state["external"]["error"] = problem
            state["held"] = state["held"] or held
        states.append(state)
    return states


def _probe(path: Path, *, flags: int = os.O_RDWR) -> bool:
    """True when someone holds path's flock; creates no file that is missing."""
    if not path.exists():
        return False
    fd = _try_lock(path, fcntl.LOCK_SH, flags=flags)
    if fd is None:
        return True
    os.close(fd)
    return False


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
