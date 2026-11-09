"""The one privilege a test here needs, granted to a copy and taken back.

`ubertooth-follow` pages its target to learn that target's clock offset, and
a page is an HCI Create Connection. Opening a raw HCI socket is not
privileged, but the kernel's own filter on such a socket (`hci_sec_filter` in
`net/bluetooth/hci_sock.c`) keeps Create Connection outside the subset an
unprivileged opener may send, so without `CAP_NET_ADMIN` the tool stops with
its own message:

    error: connecting to 20:50:0D:E9:C5:BA failed: Operation not permitted

Measured on the bench, that is all it says, and it says it in a tenth of a
second, before it opens the board. A run that does not know about the
privilege reads that as the peers having stopped streaming, which cost most
of the diagnosis of #401.

So two things live here. The first is the question "can this run page at
all", asked before anything is leased or built (`availability`), which is
what makes the test `blocked` with the real reason rather than three failed
cases pointing at the peers.

The second is the mechanism, and it is deliberately the smallest one that
works:

- **Never the whole harness as root.** A run as root leaves the run tree and
  the lease directory root-owned, which breaks `validate` and every other
  worker on the bench. Nothing here runs as root but `setcap` itself, through
  `sudo -n`, twice: once to grant and once to take back.
- **Never a systemwide binary.** `sudo setcap cap_net_admin+ep
  /usr/local/bin/ubertooth-follow` leaves every user on the host able to page
  from the adapter, for as long as nobody remembers to undo it. What is
  granted here is a copy this run made, in a directory only this user can
  reach, and it is the run's own build that is copied, so the privileged
  binary is the one under test.
- **Never left behind.** `release` takes the capability off, requires
  `getcap` to report nothing, and then unlinks the copy and its directory.
  It runs in the caller's `finally`, and again from `atexit` for a path that
  did not reach one; what `SIGKILL` leaves is one file under the run
  directory, which the next `grant` for that run would refuse rather than
  reuse.
- **Verified, not assumed.** The copy is compared with its source by
  SHA-256, its owner and mode are read back after it is written, the
  capability is read back with `getcap`, and the copy is then **run** to see
  that it still loads the run's own libraries. That last check is not
  decoration: a file capability puts the loader into secure-execution mode,
  where `LD_LIBRARY_PATH` is dropped. The fork's build records the run
  prefix's library directory as an absolute `DT_RUNPATH`, so a copy of such
  a build finds its libraries without it -- and a build that does not, or an
  install whose libraries are reached by the loader path alone, would
  silently load something else, or not load at all. So the copy is asked for
  its version line in an environment with no `LD_*` in it, and anything but
  the run's own answer fails closed.

Nothing here is on by default: `--allow-hci-capability` is what arms it, and
a run that is not given it and holds no capability of its own records why the
test could not run.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Callable, Mapping, Sequence

from ubertooth_harness.executor import CommandResult, SubprocessCommandExecutor
from ubertooth_harness.hostneeds import Blocked

# The option that arms the mechanism, named in every reason that wants it.
OPTION = "--allow-hci-capability"
# The run record this writes, read back by report.py.
EVENT = "hci_privilege"
# Bit 12 of a capability set is CAP_NET_ADMIN (linux/capability.h).
NET_ADMIN_BIT = 12
CAPABILITY = "cap_net_admin"
# What is granted: effective and permitted, no inheritable flag, so the
# capability reaches this one program and nothing it starts.
GRANT = f"{CAPABILITY}+ep"
_STATUS = Path("/proc/self/status")
# The tools a run may privilege. `ubertooth-scan` pages only with `-x`, which
# no case passes, so it is not here: a tool is given the capability when a
# case of this suite needs it, not because it could use one.
PRIVILEGED_TOOLS = ("ubertooth-follow",)
# The directory the copy goes in, under the run's own directory: the same
# filesystem as the build it is copied from, so a mount that will not carry
# file capabilities fails here rather than after a successful-looking grant.
DIRECTORY_NAME = "hci-privilege"
# libcap's own programs, which are in sbin on Debian and Ubuntu and so not on
# an ordinary user's PATH. Searched there as well as on PATH.
_LIBCAP_PATH = "/sbin:/usr/sbin:/usr/local/sbin"
SETCAP = "setcap"
GETCAP = "getcap"
SUDO = "sudo"
# Granting and taking back are two execve calls each; the deadline is for a
# host under load. Running the copy for its version line costs a library load
# and, with no board to look for, nothing else.
CAP_TIMEOUT_S = 30.0
VERIFY_TIMEOUT_S = 60.0
# The environment the copy is verified in, and the one it will run in as far
# as the loader is concerned: nothing that names a library path, because
# secure-execution mode drops those anyway, so a copy that needs one must
# fail here. `UBERTOOTH_TEST_NO_DEVICES` keeps the check off the boards, which
# another worker may be holding (libubertooth reads it; `ubertooth.c`).
_VERIFY_ENVIRONMENT = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "UBERTOOTH_TEST_NO_DEVICES": "1",
}
# The fork's own one-line warning that the loader opened a libbtbb other than
# the one the tools were built against (`warn_about_another_libbtbb` in
# ubertooth.c; installedlib.py reads the whole line for CI). Here its presence
# alone is enough: a privileged copy that reached another decoder is refused.
_ANOTHER_LIBBTBB = "libbtbb was loaded from"
# What `getcap` reports for a file that carries this grant. libcap writes
# `path cap_net_admin=ep` since 2.42 and `path = cap_net_admin+ep` before it,
# and both are the same file, so both are read. The set must be exactly this
# capability with exactly these flags: a file that came back with more is not
# what this asked for.
_GETCAP_LINE = re.compile(
    r"^(?P<path>\S+)(?:\s*=|\s)\s*"
    rf"(?P<set>{CAPABILITY}[=+]ep)\s*$"
)

# Why a run can page, or cannot. `basis` is for the record; `reason` is the
# sentence a person reads.
BASIS_PROCESS = "the run process holds CAP_NET_ADMIN"
BASIS_COPY = "a private copy of the run's own tool, with cap_net_admin+ep"
BASIS_NONE = "none"


class HciPrivilegeError(RuntimeError):
    """The privilege could not be granted, or could not be taken back."""


# ---------------------------------------------------------------------------
# What this process already holds


def net_admin_in_status(text: str) -> bool:
    """Does this `/proc/<pid>/status` text show CAP_NET_ADMIN effective?

    The effective set is what the kernel checks, so it is the one read: a
    process holding the capability permitted but not effective cannot page
    either.
    """
    for line in text.splitlines():
        label, _, value = line.partition(":")
        if label.strip() != "CapEff":
            continue
        try:
            held = int(value.strip(), 16)
        except ValueError:
            return False
        return bool(held & (1 << NET_ADMIN_BIT))
    return False


def process_holds_net_admin(status: Path | None = None) -> bool:
    """Does this process hold CAP_NET_ADMIN effective?

    False for anything this cannot read, which includes every platform that
    has no `/proc`: a run that cannot establish it holds the capability is a
    run that must not assume it does.
    """
    try:
        return net_admin_in_status((status or _STATUS).read_text())
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Can this run page at all


@dataclass(frozen=True)
class Availability:
    """Whether this run can make an HCI connection, and why."""

    available: bool
    basis: str
    reason: str

    def to_dict(self) -> dict:
        return {"available": self.available, "basis": self.basis,
                "reason": self.reason}


def _libcap_tool(name: str, which=shutil.which) -> str | None:
    """libcap's `name`, on PATH or in the sbin directories it installs into."""
    found = which(name)
    if found:
        return found
    return which(name, path=_LIBCAP_PATH)


def availability(
    platform: str,
    *,
    allow: bool,
    status: Path | None = None,
    which=shutil.which,
) -> Availability:
    """Can a run on this host page over HCI, and on what basis.

    Asked before anything is leased or built, so a suite whose answer is no
    is recorded as blocked with this reason rather than failing its cases
    against the peers.
    """
    if platform != "linux":
        # The tools are not built off Linux anyway (hostneeds.BLUEZ_TOOLS),
        # so this is the platform need's answer and not this one's.
        return Availability(
            False, BASIS_NONE,
            "CAP_NET_ADMIN is Linux's, and the two BlueZ tools are built only "
            "there",
        )
    if process_holds_net_admin(status):
        return Availability(
            True, BASIS_PROCESS,
            "the run process holds CAP_NET_ADMIN effective, so the tools page "
            "with it and nothing is granted to a file",
        )
    if not allow:
        return Availability(
            False, BASIS_NONE,
            "an HCI Create Connection needs CAP_NET_ADMIN, which this run "
            "does not hold; without it ubertooth-follow stops in a tenth of a "
            "second with `error: connecting to <address> failed: Operation "
            f"not permitted`. Rerun with {OPTION} to have the run grant "
            "cap_net_admin+ep to a private copy of its own tool for the "
            "length of the run, or run it as a user that holds the capability",
        )
    missing = [name for name in (SETCAP, GETCAP) if _libcap_tool(name, which) is None]
    if missing:
        return Availability(
            False, BASIS_NONE,
            f"{OPTION} grants cap_net_admin+ep to a private copy of the run's "
            f"own tool, which needs libcap's {' and '.join(missing)}; "
            "not installed (Debian and Ubuntu: libcap2-bin)",
        )
    if which(SUDO) is None:
        return Availability(
            False, BASIS_NONE,
            f"{OPTION} sets the capability with `sudo -n {SETCAP}`, and sudo "
            "is not installed; run as a user that holds CAP_NET_ADMIN instead",
        )
    return Availability(
        True, BASIS_COPY,
        f"{OPTION}: the run grants cap_net_admin+ep to a private copy of its "
        "own ubertooth-follow, verifies it, and takes it back when the run "
        "ends",
    )


# ---------------------------------------------------------------------------
# The grant


@dataclass(frozen=True)
class ToolCopy:
    """One privileged copy: where it came from, and what was read back."""

    tool: str
    source: str
    source_sha256: str
    path: str
    mode: str
    capability: str
    version_line: str

    def to_dict(self) -> dict:
        return {
            "tool": self.tool, "source": self.source,
            "source_sha256": self.source_sha256, "path": self.path,
            "mode": self.mode, "capability": self.capability,
            "version_line": self.version_line,
        }


@dataclass
class Grant:
    """What a run holds, and what taking it back established.

    A grant with no copies is the ordinary case: either nothing needed the
    privilege, or the run process holds it already and no file was touched.

    ``runner`` and ``which`` are how the grant was made, kept so that taking
    it back goes the same way it was given: a caller that drove `setcap` and
    `getcap` through something of its own is not left with `release` reaching
    for the host's.
    """

    basis: str
    reason: str
    directory: Path | None = None
    copies: tuple[ToolCopy, ...] = ()
    released: bool = False
    removal: tuple[str, ...] = ()
    runner: Runner | None = None
    which: Callable | None = None

    @property
    def granted(self) -> bool:
        return bool(self.copies)

    def environment(self, base: Mapping[str, str]) -> dict:
        """`base` with the privileged copies found before anything else.

        The copies keep the tools' own names, so every command line, step
        name and output pattern in the suite is the one a reader already
        knows; which binary ran is in this run's `hci_privilege` record.
        """
        env = dict(base)
        if self.directory is None or not self.copies:
            return env
        env["PATH"] = os.pathsep.join(
            item for item in (str(self.directory), env.get("PATH", "")) if item
        )
        return env

    def to_details(self) -> dict:
        details = {
            "basis": self.basis,
            "reason": self.reason,
            "capability": GRANT if self.copies else "",
            "directory": str(self.directory) if self.directory else "",
            "copies": [item.to_dict() for item in self.copies],
            "released": self.released,
        }
        if self.removal:
            details["removal"] = list(self.removal)
        return details


NOT_GRANTED = Grant(BASIS_NONE, "nothing in this run needs an HCI connection")


Runner = Callable[..., CommandResult]


def _default_runner(argv: Sequence[str], *, timeout_s: float,
                    env: Mapping[str, str] | None = None) -> CommandResult:
    return SubprocessCommandExecutor(env).run(argv, timeout_s=timeout_s)


def _sha256_and_bytes(path: Path) -> tuple[str, bytes]:
    """The file's digest and its content, read once through one descriptor.

    One open, so what is hashed is what is copied: a source replaced between
    a hash and a read would otherwise pass the comparison below.
    """
    handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        stated = os.fstat(handle)
        if not stat.S_ISREG(stated.st_mode):
            raise HciPrivilegeError(f"{path} is not a regular file")
        if stated.st_uid != os.geteuid():
            raise HciPrivilegeError(
                f"{path} is owned by uid {stated.st_uid}, not by this run's "
                f"user (uid {os.geteuid()}); the run privileges its own build "
                "and nothing else"
            )
        if stated.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise HciPrivilegeError(
                f"{path} is writable by its group or by everyone "
                f"(mode {stat.S_IMODE(stated.st_mode):04o}), so what it holds "
                "is not this run's to privilege"
            )
        content: list[bytes] = []
        while True:
            chunk = os.read(handle, 1024 * 1024)
            if not chunk:
                break
            content.append(chunk)
    finally:
        os.close(handle)
    whole = b"".join(content)
    return hashlib.sha256(whole).hexdigest(), whole


def _private_directory(directory: Path) -> Path:
    """Make the directory the copies live in, readable by this user alone."""
    if directory.is_symlink() or directory.exists():
        # Never reused: what is there was left by something else, and a
        # directory this grants a capability inside has to be this run's own.
        raise HciPrivilegeError(
            f"{directory} already exists; a privileged copy is never written "
            "over one left behind. Remove it once nothing is using it"
        )
    directory.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(directory, 0o700)
    stated = directory.stat()
    if stated.st_uid != os.geteuid() or stat.S_IMODE(stated.st_mode) != 0o700:
        raise HciPrivilegeError(
            f"{directory} came back owned by uid {stated.st_uid} with mode "
            f"{stat.S_IMODE(stated.st_mode):04o}, not this user's 0700"
        )
    return directory


def _write_copy(destination: Path, content: bytes, digest: str) -> str:
    """Write the copy read-and-execute only, and read it back.

    Created with `O_EXCL`, so nothing that was already at the path is
    written through, and `0o500`: this user may run it, nobody may write it,
    and nothing has to change its mode again for it to be used.
    """
    handle = os.open(destination,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o500)
    try:
        os.write(handle, content)
        os.fsync(handle)
    finally:
        os.close(handle)
    written, _ = _sha256_and_bytes(destination)
    if written != digest:
        raise HciPrivilegeError(
            f"the copy at {destination} hashes {written}, and its source "
            f"hashes {digest}; nothing is privileged that is not the run's "
            "own binary"
        )
    mode = stat.S_IMODE(destination.stat().st_mode)
    if mode != 0o500:
        raise HciPrivilegeError(
            f"the copy at {destination} came back with mode {mode:04o}, "
            "not 0500"
        )
    return f"{mode:04o}"


def _output(result: CommandResult) -> str:
    return (result.stdout or "") + (result.stderr or "")


def _ran(result: CommandResult, what: str) -> CommandResult:
    if result.timed_out:
        raise HciPrivilegeError(f"{what} did not finish in time")
    if result.returncode != 0:
        detail = _output(result).strip().splitlines()
        raise HciPrivilegeError(
            f"{what} exited {result.returncode}"
            + (f": {detail[-1]}" if detail else "")
        )
    return result


def _read_capability(getcap: str, path: Path, runner: Runner) -> str:
    """What `getcap` reports for `path`: its capability set, or ''."""
    result = _ran(runner((getcap, str(path)), timeout_s=CAP_TIMEOUT_S),
                  f"{GETCAP} {path}")
    text = (result.stdout or "").strip()
    return text


def _require_grant(getcap: str, path: Path, runner: Runner) -> str:
    """The copy carries exactly this grant, as `getcap` reads it back."""
    reported = _read_capability(getcap, path, runner)
    if not reported:
        raise HciPrivilegeError(
            f"{GETCAP} reports no capability on {path} after setting "
            f"{GRANT}; the filesystem it is on may be mounted nosuid or "
            "without extended attributes"
        )
    matched = _GETCAP_LINE.match(reported)
    if matched is None:
        raise HciPrivilegeError(
            f"{GETCAP} reports {reported!r} for {path}, which is not "
            f"{GRANT} and nothing else"
        )
    return matched["set"]


def _verify_loads(path: Path, patterns: Sequence[str], runner: Runner) -> str:
    """Run the privileged copy and require the run's own version line.

    This is the secure-execution check. A file capability makes the loader
    treat the program as privileged: `LD_LIBRARY_PATH` and the other `LD_*`
    variables are dropped, so a copy reaches the run's libraries only through
    the absolute `DT_RUNPATH` its build recorded. The copy is therefore run
    in an environment with none of them, and what it prints has to be the
    version line this run's tools print -- which names both the Ubertooth
    release and the libbtbb beside it, so a system library answering instead
    does not match. The fork's own warning about having loaded another
    libbtbb fails it too.
    """
    result = runner((str(path), "-V"), timeout_s=VERIFY_TIMEOUT_S,
                    env=dict(_VERIFY_ENVIRONMENT))
    if result.timed_out:
        raise HciPrivilegeError(
            f"the privileged copy {path} did not answer -V in time"
        )
    text = _output(result)
    for pattern in patterns:
        if re.search(pattern, text) is None:
            last = text.strip().splitlines()
            raise HciPrivilegeError(
                f"the privileged copy {path} does not report this run's own "
                f"version line (no match for {pattern}); with no LD_* in its "
                "environment, which is what secure-execution mode leaves it, "
                "it cannot load the run's libraries. It printed "
                + (repr(last[-1]) if last else "nothing")
            )
    if _ANOTHER_LIBBTBB in text:
        warned = [line for line in text.splitlines() if _ANOTHER_LIBBTBB in line]
        raise HciPrivilegeError(
            f"the privileged copy {path} loaded a libbtbb other than the one "
            f"it was built against: {warned[0].strip()!r}"
        )
    for line in text.splitlines():
        if line.startswith("Host tools version:"):
            return line.strip()
    return ""


_live: list["Grant"] = []


def _at_exit() -> None:
    """Take back anything a path that reached no `finally` left behind."""
    for grant in list(_live):
        try:
            release(grant)
        except HciPrivilegeError:
            pass


atexit.register(_at_exit)


def grant(
    tools: Sequence[str],
    *,
    directory: Path,
    path: str | None,
    version_patterns: Sequence[str],
    runner: Runner | None = None,
    which=shutil.which,
) -> Grant:
    """Privilege a private copy of each tool, verify it, and return the grant.

    `path` is the run's own PATH, so the binary copied is the one the run
    built or resolved; `version_patterns` is what that build's tools print
    (`preflight.host_version_patterns`). Nothing is left behind on a failure:
    whatever had been made by the time it happened is taken back before the
    reason is raised.
    """
    runner = runner or _default_runner
    setcap = _libcap_tool(SETCAP, which)
    getcap = _libcap_tool(GETCAP, which)
    sudo = which(SUDO)
    if not setcap or not getcap or not sudo:
        raise HciPrivilegeError(
            f"{OPTION} needs sudo and libcap's {SETCAP} and {GETCAP}; "
            "missing "
            + ", ".join(name for name, found in
                        ((SUDO, sudo), (SETCAP, setcap), (GETCAP, getcap))
                        if not found)
        )
    if not version_patterns:
        # Without them the check below would establish only that the copy
        # ran, which is the one thing a privileged copy must not be trusted on.
        raise HciPrivilegeError(
            "a privileged copy is verified against the version line this "
            "run's tools print, and this run named none"
        )
    made = Grant(BASIS_COPY, "", _private_directory(Path(directory)),
                 runner=runner, which=which)
    _live.append(made)
    try:
        copies = []
        for tool in tools:
            if tool not in PRIVILEGED_TOOLS:
                raise HciPrivilegeError(
                    f"{tool} is not a tool this grants a capability to "
                    f"({', '.join(PRIVILEGED_TOOLS)})"
                )
            found = which(tool, path=path)
            if not found:
                raise HciPrivilegeError(
                    f"{tool} is not on this run's PATH, so there is nothing "
                    "to copy"
                )
            source = Path(found).resolve()
            digest, content = _sha256_and_bytes(source)
            destination = made.directory / tool
            mode = _write_copy(destination, content, digest)
            _ran(runner((sudo, "-n", setcap, GRANT, str(destination)),
                        timeout_s=CAP_TIMEOUT_S),
                 f"sudo -n {SETCAP} {GRANT} {destination}")
            capability = _require_grant(getcap, destination, runner)
            version_line = _verify_loads(destination, version_patterns, runner)
            # The source is the run's own build and is never granted
            # anything; read back so that the record says so.
            on_source = _read_capability(getcap, source, runner)
            if on_source:
                raise HciPrivilegeError(
                    f"{source} carries {on_source!r}; this run grants a "
                    "capability to its private copy and never to the binary "
                    "the suite's other tools resolve"
                )
            copies.append(ToolCopy(
                tool, str(source), digest, str(destination), mode,
                capability, version_line,
            ))
        made.copies = tuple(copies)
        made.reason = (
            f"{GRANT} on {len(copies)} private cop"
            + ("y" if len(copies) == 1 else "ies")
            + " of this run's own "
            + ", ".join(item.tool for item in copies)
            + f", in {made.directory} (0700, this user only), taken back when "
              "the run ends"
        )
        return made
    except BaseException as error:
        # Whatever was made by now goes before the reason is raised, and the
        # reason stays the original one: a cleanup that also went wrong is
        # added to it rather than replacing it, because what a reader needs
        # first is why the grant failed.
        try:
            release(made)
        except HciPrivilegeError as cleanup:
            if isinstance(error, HciPrivilegeError):
                raise HciPrivilegeError(f"{error}; and {cleanup}") from error
        raise


def release(granted: Grant) -> Grant:
    """Take the capability back, verify it is gone, and remove the copies.

    Safe to call more than once and from a `finally` that an earlier failure
    reached: what is already gone is reported as gone. The capability is
    removed first and `getcap` has to come back empty; the file is then
    unlinked, which takes its extended attributes with it, so a `setcap -r`
    that failed cannot leave a privileged file behind.

    What is cleared is the whole of the grant's own directory, not only the
    copies the grant finished recording: a grant that failed between writing
    a copy and verifying it has one on disk that no record names, and that
    is exactly the one that must not be left.
    """
    if granted.released or granted.directory is None:
        granted.released = True
        if granted in _live:
            _live.remove(granted)
        return granted
    runner = granted.runner or _default_runner
    which = granted.which or shutil.which
    setcap = _libcap_tool(SETCAP, which)
    getcap = _libcap_tool(GETCAP, which)
    sudo = which(SUDO)
    notes: list[str] = []
    problems: list[str] = []
    paths = [Path(item.path) for item in granted.copies]
    try:
        for found in sorted(granted.directory.iterdir()):
            if found not in paths:
                # Written but not recorded: a grant that failed part way
                # through. Taken back like the rest.
                paths.append(found)
    except OSError as error:
        problems.append(f"{granted.directory} could not be read: {error}")
    for path in paths:
        if not path.exists():
            notes.append(f"{path} was already gone")
            continue
        if sudo and setcap:
            removal = runner((sudo, "-n", setcap, "-r", str(path)),
                             timeout_s=CAP_TIMEOUT_S)
            if removal.returncode != 0:
                problems.append(
                    f"sudo -n {SETCAP} -r {path} exited {removal.returncode}"
                )
        else:
            problems.append(
                f"neither sudo nor {SETCAP} could be found to take the "
                f"capability off {path}"
            )
        if getcap:
            try:
                left = _read_capability(getcap, path, runner)
            except HciPrivilegeError as error:
                problems.append(str(error))
            else:
                if left:
                    problems.append(
                        f"{GETCAP} still reports {left!r} on {path} after "
                        f"{SETCAP} -r"
                    )
                else:
                    notes.append(f"{GETCAP} reports nothing on {path}")
        try:
            path.unlink()
            notes.append(f"{path} unlinked")
        except OSError as error:
            problems.append(f"{path} could not be removed: {error}")
    # Whatever happened above, the directory goes: an unlinked file carries
    # its capability away with it, so the worst case left here is an empty
    # directory this run made.
    try:
        granted.directory.rmdir()
        notes.append(f"{granted.directory} removed")
    except OSError as error:
        problems.append(f"{granted.directory} could not be removed: {error}")
    granted.released = True
    granted.removal = tuple(notes + [f"problem: {item}" for item in problems])
    if granted in _live:
        _live.remove(granted)
    if problems:
        raise HciPrivilegeError(
            "the HCI capability could not be taken back cleanly: "
            + "; ".join(problems)
        )
    return granted


def directory_for(run_dir: Path) -> Path:
    """Where this run's privileged copies live."""
    return Path(run_dir) / DIRECTORY_NAME


def tests_needing_privilege(catalog, invocations) -> tuple[str, ...]:
    """The selected tests whose cases page a target over HCI.

    Read off ``TestDefinition.needs.hci_connect``, so the catalog is the one
    statement of which tests this is about and nothing here holds a second
    list to keep in step.
    """
    return tuple(sorted({
        invocation.test_name for invocation in invocations
        if catalog.get(invocation.test_name).needs.hci_connect
    }))


def decide(catalog, invocations, *, host: str, platform: str, allow: bool,
           status: Path | None = None, which=shutil.which):
    """What a run should do about the privilege, before it leases anything.

    Returns ``(availability, tests that need it, blocked, invocations)``.

    A run that cannot page gets a ``Blocked`` record per test that needed to,
    carrying the tool's own reason, and those tests are taken out of the
    invocations: the run then measures everything else, says what it could
    not measure and why, and ends failed, which is what a gate that was
    never measured has to do. A run that can page keeps them, and the tests
    named are the ones a grant is made for.
    """
    decided = availability(platform, allow=allow, status=status, which=which)
    wanted = tests_needing_privilege(catalog, invocations)
    if not wanted or decided.available:
        return decided, wanted, (), tuple(invocations)
    return (
        decided,
        (),
        tuple(Blocked(host, name, decided.reason) for name in wanted),
        tuple(invocation for invocation in invocations
              if invocation.test_name not in wanted),
    )
