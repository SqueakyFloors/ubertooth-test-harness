"""Host-local board leases, so several agents can share the lab Ubertooths
(and the nRF52840 DKs used as BLE test devices, keyed by J-Link serial).

A lease is an exclusive ``flock`` on ``<lease dir>/<serial>.lock``. The kernel
drops it when the holding process exits, however it exits. The lock file also
records who holds it, for the error a second run sees and for
``ubertooth-harness boards``.

Stale leases: ``lease`` and ``hold`` hand their lock files to the command they
run (the command inherits them, so the boards stay held even if the harness
process is killed first), and one lock on a file lives as long as *any*
process that holds that file open. A session killed part way through can
leave a lease nothing is waiting on: its own process is gone, and some
command it started, re-parented and forgotten, still holds the lock. A board
locked like that would never come back (harness #215: five hours of a release
soak). So when a busy lease records a process of *this* host that no longer
exists, ``acquire()`` takes the board over: it removes that lock file, which
is what later commands look at, and says so in one line on standard error.
A lock the removed file's inode still carries is no longer in anyone's way.
``boards`` marks such a lease stale and never removes anything.

A takeover does not promise the board is idle: something holds that lock, or
the kernel would have dropped it. What really keeps two commands off one
board is the USB device, which only one of them can claim, so a leftover
capture makes the next device command fail loudly instead of quietly sharing
the board, while a leftover that is not using the board costs nothing. The
board coming back is worth that, and the note says what to look for.

The host lock is never taken over. It is the only thing that keeps USB power
changes and firmware writes on a host from overlapping, and what still holds
its file could be a firmware write in flight. So its error says that the
command which took it is gone and what to look for, and a person decides;
a board interrupted mid-write is the one outcome worth waiting to avoid.

A takeover costs a poll round like any other busy lock, so a lease file that
keeps coming back cannot make a command spin: every round either sleeps or
reaches the deadline.

What keeps a takeover narrow: a lease recorded on another host is left alone
(a pid there means nothing here), and ``<lease dir>/takeover.lock`` orders a
takeover against everything else. A takeover holds it exclusively; every
attempt to take locks holds it shared for as long as it takes and checks its
locks. So a lease file is never removed while a command is taking it, and two
commands can never end up holding one board through two lock files. A pid
that the system has given to an unrelated process in the meantime reads as
alive, so the lease is left alone, as before. When the record's process group
process group are still running, the note names it: a leftover command that
session started may still be talking to the board, and ``pgrep -g <group>``
is how to see it. That signal only covers the process group the command ran
in, so a leftover that left it (``setsid``, a daemon) is reported as gone;
and a host whose name changes
under it (a Mac that renames itself on a new network) makes its own earlier
leases read as another host's, which costs the report, never safety.

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

0. ``takeover.lock``, shared, for as long as the attempt below takes (it is
   not a resource; it only orders an attempt against a takeover, and both
   take it without ever blocking, so it is in no cycle);
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
import stat
import sys
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
# Held for the moment one stale lease is taken over, so two commands that see
# the same stale lease cannot both remove a lock file.
TAKEOVER_LOCK = "takeover.lock"


class LeaseError(RuntimeError):
    """A board is held by someone else, or a lease file cannot be used."""


@dataclass(frozen=True)
class Board:
    """One board to lease: its registry alias and the key its lock file uses."""

    alias: str
    key: str


# The per-host power/firmware lock. Its key cannot clash with a board's,
# which is always 32 hex digits, nor with the takeover lock's file name.
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


# nRF52840 DKs (BLE test devices) and ESP32 Classic peers are leased too, by
# J-Link serial and by USB bridge serial. Both keys are prefixed, so neither
# can clash with an Ubertooth's bare 32 hex digits, with the host lock, or
# with `takeover`, which would name the takeover lock's own file; a CP2102N
# bridge serial is 32 hex digits itself.
# A key becomes a file name, so it may hold nothing that would reach
# another directory or need quoting.
_LOCK_NAME = re.compile(r"^[0-9A-Za-z_.:+-]{1,64}$")
DK_TYPE = "nRF52840 DK"
PEER_TYPE = "ESP32 Classic peer"
LEASED_TYPES = ("Ubertooth", DK_TYPE, PEER_TYPE)


def resource_key(resource) -> str:
    """Lock-file key of a leasable resource (Ubertooth, DK, or ESP32 peer)."""
    if resource.type == DK_TYPE:
        serial = str(resource.identity.get("jlink serial", "") or "").strip()
        if not serial.isdigit():
            raise LeaseError(
                f"{resource.alias} needs a 'jlink serial' in the hardware registry "
                f"to be leased; registered '{serial}'"
            )
        return f"nrf52840dk-{int(serial)}"
    if resource.type == PEER_TYPE:
        # Taken as registered: the same string names the device path, so a
        # board leased under one spelling and opened under another would be
        # two things at once.
        serial = str(resource.identity.get("bridge serial", "") or "").strip()
        if not serial or not _LOCK_NAME.fullmatch(serial):
            raise LeaseError(
                f"{resource.alias} needs a 'bridge serial' in the hardware registry "
                f"to be leased; registered '{serial}'"
            )
        return f"esp32-{serial}"
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
        # The process group this command leads or belongs to. The command
        # that inherits the lock runs in it too, so it tells a later command
        # whether anything that session started is still running. A group,
        # not a session id: a group is what a command and its children
        # share, and what `pgrep -g` reads (see the docstring).
        "pgid": os.getpgrp(),
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


def _process_exists(pid) -> bool | None:
    """True when a process with this pid exists, None when it cannot be told.

    Signal 0 asks the kernel about a pid without disturbing it. Another
    user's process answers "not allowed", which is still an answer: it
    exists.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None  # 0 and negatives mean "a group" to kill(), never a process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _group_exists(pgid) -> bool | None:
    """True when any process is still in this process group (as above)."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return None  # 0 is the caller's own group: always alive, never asked
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def lease_liveness(holder: Mapping, *, hostname: str | None = None) -> tuple[str, str]:
    """Whether this lease's holder can still be holding it, and why in words.

    The kinds are ``live`` (the recorded process is still there, or nothing
    can be told about it), ``stale`` (it is gone, and so is the session it
    ran in), ``orphan`` (it is gone but other processes of its process group
    are still running), ``foreign`` (the record is from another host, where a
    pid means nothing) and ``unknown`` (no record to go on, for example a
    lease file a release has emptied).

    The recorded group is the process group the command ran in, which is
    where the command it started runs too. A process that left that group
    (``setsid``, a daemon) is invisible here, so ``stale`` means nothing of
    that group is left, not that nothing holds the lock. What holds the lock
    is always some process; see the takeover in the module docstring.
    """
    if not holder:
        return "unknown", "its lock file records no holder"
    here = socket.gethostname() if hostname is None else hostname
    recorded = str(holder.get("hostname") or "")
    if not recorded:
        return "unknown", "its lock file records no host"
    if recorded != here:
        return "foreign", f"it was taken on {recorded}, not on this host"
    pid = holder.get("pid")
    alive = _process_exists(pid)
    if alive is True:
        return "live", f"its process (pid {pid}) is still running"
    if alive is None:
        return "live", f"its process (pid {pid!r}) cannot be checked"
    group = _group_exists(holder.get("pgid"))
    if group is True:
        return "orphan", (
            f"its process (pid {pid}) is gone, but other processes of its "
            f"process group ({holder['pgid']}) are still running; one of them may "
            f"still be using the board, so check 'pgrep -g {holder['pgid']}'"
        )
    if group is None:
        # A lease from a harness that did not record the group yet, or a
        # record that does not say: the pid is all there is to go on.
        return "stale", f"its process (pid {pid}) is gone"
    return "stale", (
        f"its process (pid {pid}) is gone, and so is the process group it ran "
        f"in ({holder['pgid']})"
    )


def takeover_allowed(board: Board, kind: str) -> bool:
    """Whether a lease in this state may be taken over.

    A board's lease is taken over once the command that took it is gone,
    whether or not its process group still runs: the USB device is what
    really keeps two commands off one board, so a leftover capture makes the
    next device command fail loudly rather than quietly share the board.

    The host lock is never taken over. It is the only thing that keeps USB
    power changes and firmware writes on a host from overlapping, and what
    still holds its file could be a firmware write in flight: a process that
    left the recorded group, or one of a group the record does not name at
    all (a lease file from a harness that recorded no process group).
    Nothing here can tell those from a lock nobody needs, and the cost of
    being wrong is a board interrupted mid-write, so the error says what to
    look for and a person decides.
    """
    if board == HOST_LOCK:
        return False
    return kind in ("stale", "orphan")


def _note_to_stderr(message: str) -> None:
    print(f"note: {message}", file=sys.stderr)


def _take_over_stale(path: Path, root: Path, board: Board) -> str:
    """Free a lease whose holder is gone: why it was taken over, or ''.

    The lock itself cannot be taken away from whatever still holds the file
    open, so the file is removed instead: the next attempt creates a new one
    and locks that, and later commands read the new file. Every check is
    repeated under the takeover lock, which is held exclusively here and
    shared by every attempt in acquire(), so this cannot remove a file that
    a command is in the middle of taking, and two commands cannot take over
    one board at the same time. A lease that was released, taken by someone
    else, or already taken over is left alone.
    """
    if not takeover_allowed(board, lease_liveness(read_holder(path))[0]):
        return ""  # the common case, before taking the takeover lock
    try:
        guard = _try_lock(root / TAKEOVER_LOCK, fcntl.LOCK_EX)
    except OSError:
        # Not worth failing a command for: the lease is left alone and the
        # board is reported busy, as it was before this file existed.
        return ""
    if guard is None:
        return ""  # another command is taking over a lease right now
    try:
        before = _file_identity(path)
        if before is None:
            return ""  # already gone
        if not _probe(path):
            return ""  # free now: the next attempt simply takes it
        # The holder record last, and after the probe: a command that has
        # just taken this lease stamps its own record, so reading it here
        # leaves the smallest window in which this could remove a lease
        # somebody is holding. The acquirer's own check that its lock file is
        # still the board's closes what is left of that window.
        kind, why = lease_liveness(read_holder(path))
        if not takeover_allowed(board, kind):
            return ""
        if _file_identity(path) != before:
            return ""  # a different file by now
        os.unlink(path)
    except OSError:
        return ""
    finally:
        os.close(guard)
    return why


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


def _file_identity(path: Path) -> tuple[int, int, int] | None:
    """(mode, device, inode) of path, or None when it does not exist yet."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mode, info.st_dev, info.st_ino)


def _released(held: list, message: str) -> "LeaseError":
    """Drop what this attempt already holds, then give the error to raise.

    Taking locks is all or nothing, so nothing may stay locked when the
    attempt gives up.
    """
    LeaseSet(list(held)).release()
    held.clear()
    return LeaseError(message)


def _try_lock(path: Path, mode: int, *, flags: int = os.O_RDWR) -> int | None:
    # A lock file is read by whoever waits for it, so it is 0644 whatever the
    # umask says; an existing file keeps the mode it has. Another process can
    # create or remove the file between the two opens, so try a few times.
    for _attempt in range(5):
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            try:
                fd = os.open(path, flags)
            except FileNotFoundError:
                continue  # it was removed again; create it
            break
        try:
            os.fchmod(fd, 0o644)
        except OSError:
            pass
        break
    else:
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

    def release_host_lock(self) -> bool:
        """Drop the host lock early, keeping the boards; True if it was held.

        Releasing is always safe for the lock order (nothing is taken after
        it); a run that flashed its boards first gives the lock back once the
        writes are done, unless it switches power or writes firmware later.
        """
        for lease in list(self.leases):
            if lease.board == HOST_LOCK:
                self.leases.remove(lease)
                LeaseSet([lease]).release()
                return True
        return False

    def holds_host_lock(self) -> bool:
        return any(lease.board == HOST_LOCK for lease in self.leases)

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
    note: Callable[[str], None] = _note_to_stderr,
) -> LeaseSet:
    """Lease every board, all or nothing, waiting up to wait_s for busy ones.

    With host_lock, the host lock is taken too; with external_template (an
    already validated external_lock_template()), each board's external lock
    too (see the module docstring for the order).

    A busy lease whose holder is gone is taken over, once per resource, and
    note() is told in one line (by default on standard error).
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
            own = root / f"{board.key}.lock"
            # realpath, so a symlink to the lease file is caught too: the
            # second flock of one file in one process conflicts with itself.
            if os.path.realpath(path) == os.path.realpath(own):
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
        files: dict[tuple[int, int], Path] = {}
        guard = None
        try:
            # Shared while this attempt takes its locks and checks them:
            # a takeover (below) holds the same file exclusively, so no
            # lease file can be removed while another command is taking
            # it, and two commands can never end up holding one board
            # through two lock files.
            guard = _try_lock(root / TAKEOVER_LOCK, fcntl.LOCK_SH)
            if guard is not None:
                for board, path, external in ordered:
                    where = path.parent if external else root
                    existing = _file_identity(path)
                    if existing is not None:
                        if external and not stat.S_ISREG(existing[0]):
                            raise _released(
                                held,
                                f"{board.alias}: its external board lock {path} is not "
                                "a regular file; point the template at the lab's lock "
                                "files",
                            )
                        # flock is per open file description, so two paths that
                        # are one file (a symlink, a hard link, one path twice)
                        # would make the process wait for its own lock.
                        if existing[1:] in files:
                            raise _released(
                                held,
                                f"{board.alias}: its lock file {path} is the same file "
                                f"as {files[existing[1:]]}; every board needs its own "
                                "lock file",
                            )
                    # An external file may belong to another user: read access
                    # is enough for flock, and it is never written.
                    try:
                        fd = _try_lock(
                            path, fcntl.LOCK_EX,
                            # O_NONBLOCK: a lab path that is a FIFO must not make
                            # the open itself wait forever. The check below then
                            # refuses anything that is not a regular file.
                            flags=(
                                os.O_RDONLY | os.O_NONBLOCK if external else os.O_RDWR
                            ),
                        )
                    except PermissionError as error:
                        if not external:
                            raise
                        # Waiting cannot fix this, so say which board and file.
                        raise _released(
                            held,
                            f"{board.alias}: its external board lock {path} cannot be "
                            f"opened for reading ({error.strerror}); the harness needs "
                            "read access to the lab's lock file",
                        ) from error
                    if fd is None:
                        busy = (board, path, external)
                        break
                    held.append(Lease(board, path, fd, holder, external=external))
                    # The same two checks on the file that was actually opened,
                    # in case the path was replaced in between.
                    info = os.fstat(fd)
                    if external and not stat.S_ISREG(info.st_mode):
                        raise _released(
                            held,
                            f"{board.alias}: its external board lock {path} is not a "
                            "regular file; point the template at the lab's lock files",
                        )
                    key = (info.st_dev, info.st_ino)
                    if key in files:
                        raise _released(
                            held,
                            f"{board.alias}: its lock file {path} is the same file as "
                            f"{files[key]}; every board needs its own lock file",
                        )
                    files[key] = path
                    if not external:
                        _stamp(fd, dict(holder, resource=board.alias))
                # One last check on every lease this attempt took: a lock on a
                # file that is no longer the board's lease file excludes
                # nobody. The takeover lock above keeps a takeover out of this
                # window, so what is left is a file removed from outside the
                # harness, which is how #215 was cleared by hand. The attempt
                # starts again rather than trust such a lock.
                for lease in held:
                    if lease.external:
                        continue
                    now = _file_identity(lease.path)
                    info = os.fstat(lease.fd)
                    if now is None or now[1:] != (info.st_dev, info.st_ino):
                        busy = (lease.board, lease.path, False)
                        break
        except OSError as error:
            LeaseSet(held).release()
            raise LeaseError(f"cannot lease boards in {where}: {error}") from error
        finally:
            if guard is not None:
                os.close(guard)
        if guard is None:
            # Another command is taking over a stale lease. That is a
            # moment, not a wait: look again on the next round.
            if clock() >= deadline:
                raise LeaseError(
                    f"cannot lease boards in {root}: another command is taking "
                    f"over a stale lease ({root / TAKEOVER_LOCK}); retry, or "
                    "use --wait-for-boards MINUTES to wait for it"
                )
            sleep(_POLL_S)
            continue
        if busy is None:
            return LeaseSet(held)
        LeaseSet(held).release()
        board, path, external = busy
        # A lease of this host whose holder is gone holds nothing back; an
        # external lock belongs to the lab and is never touched. Once per
        # resource, so a file that keeps coming back cannot loop here.
        if not external:
            why = _take_over_stale(path, root, board)
            if why:
                # The next round takes the board; this one has released
                # everything. Every round sleeps or reaches the deadline, so
                # a lease file that keeps coming back cannot make this spin.
                note(
                    f"{board.alias} was held by a lease that no longer holds it: "
                    f"{why}. Taking it over ({path})."
                )
        if clock() >= deadline:
            waited = f" after waiting {wait_s:g}s" if wait_s else ""
            if external:
                raise LeaseError(
                    f"{board.alias} is in use{waited}: held "
                    f"{describe_external(path, read_external_holder(path))}; "
                    "use --wait-for-boards MINUTES to wait for it"
                )
            holder_now = read_holder(path)
            kind, why = lease_liveness(holder_now)
            # A lease this command did not take over (the host lock, or a
            # board whose one takeover this command has spent) must say the
            # command that took it is gone, or it reads as the stale lease
            # nobody could account for before.
            left_over = (f" the command that took it is gone: {why};"
                         if kind in ("orphan", "stale") else "")
            if board == HOST_LOCK:
                if left_over:
                    raise LeaseError(
                        f"this host's power/firmware lock is in use{waited}: held "
                        f"by {describe_holder(holder_now)} ({path});{left_over} it "
                        "is never taken over, because a firmware write or a power "
                        "change it started may still be running: look for one, "
                        "and remove the lock file once you know there is none"
                    )
                raise LeaseError(
                    f"this host's power/firmware lock is in use{waited}: held by "
                    f"{describe_holder(holder_now)} ({path}); another "
                    "harness command is switching USB power or writing firmware "
                    "here; retry when it ends, or use --wait-for-boards MINUTES"
                )
            raise LeaseError(
                f"{board.alias} is in use{waited}: held by "
                f"{describe_holder(holder_now)} ({path});{left_over} "
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

    A held lease whose holder is gone also carries ``stale``, saying why the
    next command that wants the board takes it over.
    """
    root = lease_dir(directory)
    states = []
    for board in boards:
        path = root / f"{board.key}.lock"
        state = {"resource": board.alias, "key": board.key, "held": False, "holder": {}}
        if _probe(path):
            state["held"] = True
            state["holder"] = read_holder(path)
            # Reported, never removed: a listing changes nothing on the bench.
            kind, why = lease_liveness(state["holder"])
            if kind in ("stale", "orphan"):
                state["stale"] = why
                state["takeover"] = takeover_allowed(board, kind)
        if external_template:
            external = external_lock_path(external_template, board)
            try:
                # O_NONBLOCK, and a regular-file check: a lab path that is a
                # FIFO must not make the listing wait forever.
                held = _probe(external, flags=os.O_RDONLY | os.O_NONBLOCK,
                              regular_only=True)
                problem = ""
            except OSError as error:  # e.g. another user's unreadable file
                # Unknown is not free: a run would refuse this board, so the
                # listing must not call it free either.
                held, problem = True, str(error)
            state["external"] = {
                "lock": str(external),
                "held": held,
                "holder": (
                    read_external_holder(external) if held and not problem else ""
                ),
            }
            if problem:
                state["external"]["error"] = problem
            state["held"] = state["held"] or held
        states.append(state)
    return states


def _probe(path: Path, *, flags: int = os.O_RDWR, regular_only: bool = False) -> bool:
    """True when someone holds path's flock; creates no file that is missing."""
    identity = _file_identity(path)
    if identity is None:
        return False
    if regular_only and not stat.S_ISREG(identity[0]):
        raise OSError(errno.EINVAL, "not a regular file", str(path))
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
