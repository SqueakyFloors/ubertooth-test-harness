"""Immutable source, input, artifact, and invocation identity for one run."""

from dataclasses import dataclass, replace
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import MappingProxyType
from typing import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from ubertooth_harness import environment as harness_environment
from ubertooth_harness.events import validate_run_id
from ubertooth_harness.planner import TestInvocation


MANIFEST_SCHEMA_VERSION = 5
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
# Hard limit on fetching a ref the source checkout does not have yet.
FETCH_TIMEOUT_S = 60.0


def _stderr_log(message: str) -> None:
    print(f"note: {message}", file=sys.stderr, flush=True)


def _frozen(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    return value


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


class ManifestError(ValueError):
    """A source revision or run manifest cannot be made immutable."""


def _git(path: Path, *arguments: str, allow_failure: bool = False) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=not allow_failure,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ManifestError(f"git command failed for {path}: {error}") from error
    if result.returncode != 0 and not allow_failure:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise ManifestError(f"git command failed for {path}: {detail}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _safe_repository(value: str) -> str:
    if "://" not in value:
        return value
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError as error:
        raise ManifestError("repository URL is invalid") from error


def _repository_for_ref(path: Path, requested_ref: str) -> str:
    symbolic = _git(
        path,
        "rev-parse",
        "--symbolic-full-name",
        requested_ref,
        allow_failure=True,
    )
    remote_name = ""
    if symbolic.startswith("refs/heads/"):
        branch = symbolic.removeprefix("refs/heads/")
        remote_name = _git(
            path,
            "config",
            "--get",
            f"branch.{branch}.remote",
            allow_failure=True,
        )
    elif "/" in requested_ref:
        candidate = requested_ref.split("/", 1)[0]
        if _git(
            path,
            "config",
            "--get",
            f"remote.{candidate}.url",
            allow_failure=True,
        ):
            remote_name = candidate
    remote = ""
    if remote_name and remote_name != ".":
        remote = _git(
            path,
            "config",
            "--get",
            f"remote.{remote_name}.url",
            allow_failure=True,
        )
    if not remote:
        remote = _git(
            path, "config", "--get", "remote.origin.url", allow_failure=True
        )
    return _safe_repository(remote or str(path))


@dataclass(frozen=True)
class SourceRevision:
    name: str
    source_path: str
    repository: str
    requested_ref: str
    commit: str
    short_commit: str
    # The name ``requested_ref`` (then a commit) was chosen by, when the
    # caller resolved the commit elsewhere: the branch or tag the target
    # runner passes from the controller, or ``fork-pin`` for the libbtbb a
    # run took from the Ubertooth commit's own install.sh. Recorded only,
    # never resolved.
    ref_name: str | None = None

    @property
    def expected_revision(self) -> str:
        return f"git-{self.short_commit}"

    def to_dict(self) -> dict:
        data = {
            "name": self.name,
            "source_path": self.source_path,
            "repository": self.repository,
            "requested_ref": self.requested_ref,
            "commit": self.commit,
            "short_commit": self.short_commit,
            "expected_revision": self.expected_revision,
        }
        if self.ref_name is not None:
            data["ref_name"] = self.ref_name
        return data


def _rev_parse(path: Path, ref: str) -> str | None:
    """The commit ``ref`` names in the checkout, or None when it names none."""
    try:
        return _git(path, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    except ManifestError:
        return None


def _fetch_remote(path: Path, requested_ref: str) -> str | None:
    """The remote to fetch a missing ref from: the remote a ``remote/name``
    ref names, else origin, else the only remote; None when there is none."""
    if "/" in requested_ref:
        candidate = requested_ref.split("/", 1)[0]
        if _git(path, "config", "--get", f"remote.{candidate}.url", allow_failure=True):
            return candidate
    if _git(path, "config", "--get", "remote.origin.url", allow_failure=True):
        return "origin"
    remotes = _git(path, "remote", allow_failure=True).split()
    return remotes[0] if len(remotes) == 1 else None


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    line = lines[-1] if lines else "no output"
    # Never repeat credentials a remote URL may carry.
    return re.sub(r"(://)[^/@\s]+@", r"\1", line)[:300]


class GitRevisionResolver:
    """Resolve a local branch, tag, or commit without changing the checkout.

    A ref the checkout does not have (a commit pushed since it was last
    fetched, typically) is fetched once from the checkout's remote, within
    ``fetch_timeout_s``, and resolved again. Fetching only updates
    remote-tracking refs, tags and objects: the working tree, HEAD and local
    branches are never touched.
    """

    def __init__(self, *, fetch_timeout_s: float = FETCH_TIMEOUT_S, log=None):
        self.fetch_timeout_s = fetch_timeout_s
        self.log = log if log is not None else _stderr_log

    def _fetch(self, path: Path, remote: str, requested_ref: str) -> str | None:
        """Fetch what ``requested_ref`` may need; None on success, else why not.

        A full commit is fetched directly first; a name (or a commit the
        remote will not serve directly) fetches every branch and tag.
        """
        attempts = []
        if _COMMIT_RE.fullmatch(requested_ref):
            attempts.append([requested_ref])
        attempts.append([
            f"+refs/heads/*:refs/remotes/{remote}/*", "refs/tags/*:refs/tags/*",
        ])
        deadline = time.monotonic() + self.fetch_timeout_s
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        problem = None
        for refspecs in attempts:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return problem or f"timed out after {self.fetch_timeout_s:g}s"
            command = [
                "git", "-C", str(path), "fetch", "--quiet", "--no-recurse-submodules",
                remote, *refspecs,
            ]
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=remaining,
                    stdin=subprocess.DEVNULL, env=environment, check=False,
                )
            except subprocess.TimeoutExpired:
                return f"timed out after {self.fetch_timeout_s:g}s"
            except OSError as error:
                return str(error)
            if result.returncode == 0:
                if _rev_parse(path, requested_ref) is not None:
                    return None
                problem = None
            else:
                problem = _last_line(result.stderr or result.stdout)
        return problem

    def _fetch_and_retry(self, name: str, path: Path, requested_ref: str) -> str:
        failure = f"unable to resolve {name} ref '{requested_ref}' in {path}"
        remote = _fetch_remote(path, requested_ref)
        if remote is None:
            raise ManifestError(f"{failure}; it has no remote to fetch it from")
        self.log(f"{name} ref '{requested_ref}' is not in {path}; fetching from {remote}")
        problem = self._fetch(path, remote, requested_ref)
        commit = _rev_parse(path, requested_ref)
        if commit is None and not _COMMIT_RE.fullmatch(requested_ref) \
                and not requested_ref.startswith(("refs/", f"{remote}/")):
            # A branch that exists only on the remote.
            commit = _rev_parse(path, f"{remote}/{requested_ref}")
        if commit is not None:
            return commit
        if problem:
            raise ManifestError(f"{failure}; fetching from {remote} failed: {problem}")
        raise ManifestError(
            f"{failure}; fetched from {remote}, which does not have it either"
        )

    def resolve(
        self, name: str, source_path: Path | str, requested_ref: str,
        ref_name: str | None = None,
    ) -> SourceRevision:
        path = Path(source_path).expanduser().resolve()
        if not isinstance(name, str) or not name.strip():
            raise ManifestError("source name must be non-empty")
        refs = (requested_ref,) if ref_name is None else (requested_ref, ref_name)
        for value in refs:
            if (
                not isinstance(value, str)
                or not value.strip()
                or "\x00" in value
                or "\n" in value
            ):
                raise ManifestError("source ref must be a non-empty single line")
        if not path.is_dir():
            raise ManifestError(f"source path is not a directory: {path}")
        if _git(path, "rev-parse", "--is-inside-work-tree") != "true":
            raise ManifestError(f"source path is not a git worktree: {path}")
        dirty = _git(path, "status", "--porcelain", "--untracked-files=all")
        if dirty:
            raise ManifestError(f"source worktree is dirty: {path}")
        commit = _rev_parse(path, requested_ref)
        if commit is None:
            commit = self._fetch_and_retry(name.strip(), path, requested_ref)
        if not _COMMIT_RE.fullmatch(commit):
            raise ManifestError(f"resolved {name} commit is invalid: {commit}")
        short_commit = _git(path, "rev-parse", "--short", commit)
        repository = _repository_for_ref(path, requested_ref)
        return SourceRevision(
            name=name.strip(),
            source_path=str(path),
            repository=repository,
            requested_ref=requested_ref,
            commit=commit.lower(),
            short_commit=short_commit.lower(),
            ref_name=ref_name,
        )


@dataclass(frozen=True)
class FileIdentity:
    name: str
    path: str
    sha256: str

    @classmethod
    def from_path(cls, name: str, path: Path | str) -> "FileIdentity":
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ManifestError(f"manifest input is not a file: {source}")
        return cls(name, str(source), RunManifest.hash_file(source))

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class InvocationIdentity:
    id: str
    test_name: str
    host: str
    arguments: Mapping
    resources: Mapping[str, str]
    resource_identities: Mapping[str, Mapping[str, str]]
    power_isolation: tuple[Mapping[str, object], ...]
    # True for every exclusive-Ubertooth invocation, including one with no
    # power actions that only gets the read-only enumeration check.
    exclusive_ubertooth: bool = False
    # The RF placement of every bound resource that has one (rf.py), by role,
    # so a reader knows what attenuation and distance the run's numbers were
    # taken at. Empty when no board the run used records a placement.
    resource_placements: Mapping[str, Mapping] = MappingProxyType({})

    @classmethod
    def from_invocation(cls, invocation: TestInvocation) -> "InvocationIdentity":
        identities = {
            role: MappingProxyType(dict(identity))
            for role, identity in invocation.resource_identities.items()
        }
        return cls(
            invocation.invocation_id,
            invocation.test_name,
            invocation.host,
            MappingProxyType(dict(invocation.arguments)),
            MappingProxyType(dict(invocation.resources)),
            MappingProxyType(identities),
            tuple(
                MappingProxyType(action.to_dict())
                for action in invocation.power_isolation
            ),
            invocation.exclusive_ubertooth,
            _frozen(dict(invocation.resource_placements)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "test_name": self.test_name,
            "host": self.host,
            "arguments": _json_value(self.arguments),
            "resources": dict(self.resources),
            "resource_identities": {
                role: dict(identity)
                for role, identity in self.resource_identities.items()
            },
            "power_isolation": [dict(action) for action in self.power_isolation],
            "exclusive_ubertooth": self.exclusive_ubertooth,
            "resource_placements": _json_value(self.resource_placements),
        }


def _json_value(value):
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class RunManifest:
    schema_version: int
    run_id: str
    created_at: str
    plan_name: str
    suite: str
    host: str
    sources: tuple[SourceRevision, ...]
    inputs: tuple[FileIdentity, ...]
    artifacts: tuple[FileIdentity, ...]
    invocations: tuple[InvocationIdentity, ...]
    environment: Mapping = MappingProxyType({})
    build: Mapping = MappingProxyType({})
    # Board leases this run held (see leases.py); empty for older records.
    leases: tuple = ()

    @staticmethod
    def hash_file(path: Path | str) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def verify_files(self) -> None:
        for item in self.inputs + self.artifacts:
            current = self.hash_file(item.path)
            if current != item.sha256:
                raise ManifestError(
                    f"manifest file changed after snapshot: {item.name}"
                )

    def source(self, name: str) -> SourceRevision:
        matches = [source for source in self.sources if source.name == name]
        if len(matches) != 1:
            raise ManifestError(
                f"manifest requires exactly one {name} source, found {len(matches)}"
            )
        return matches[0]

    def matches_invocations(self, invocations: Sequence[TestInvocation]) -> bool:
        current = tuple(
            InvocationIdentity.from_invocation(item).to_dict()
            for item in invocations
        )
        recorded = tuple(item.to_dict() for item in self.invocations)
        return current == recorded

    def with_invocations(
        self, invocations: Sequence[TestInvocation]
    ) -> "RunManifest":
        return replace(
            self,
            invocations=tuple(
                InvocationIdentity.from_invocation(item) for item in invocations
            ),
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "plan_name": self.plan_name,
            "suite": self.suite,
            "host": self.host,
            "sources": [source.to_dict() for source in self.sources],
            "inputs": [item.to_dict() for item in self.inputs],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "invocations": [item.to_dict() for item in self.invocations],
            "environment": _plain(self.environment),
            "build": _plain(self.build),
            **({"leases": [_plain(item) for item in self.leases]} if self.leases else {}),
        }

    def write_atomic(self, path: Path | str) -> None:
        destination = Path(path)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.to_dict(), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)


def build_run_manifest(
    *,
    run_id: str,
    plan_name: str,
    suite: str,
    host: str,
    plan_path: Path | str,
    hardware_path: Path | str,
    sources: Sequence[SourceRevision],
    invocations: Sequence[TestInvocation],
    artifacts: Sequence[FileIdentity] = (),
    created_at: str | None = None,
    environment: Mapping | None = None,
    build: Mapping | None = None,
    leases: Sequence[Mapping] = (),
) -> RunManifest:
    if not sources:
        raise ManifestError("at least one resolved source is required")
    source_names = [source.name for source in sources]
    if len(set(source_names)) != len(source_names):
        raise ManifestError("source names must be unique")
    if not invocations:
        raise ManifestError("at least one invocation is required")
    timestamp = created_at or datetime.datetime.now(
        datetime.timezone.utc
    ).replace(tzinfo=None).isoformat(timespec="milliseconds")
    return RunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=validate_run_id(run_id),
        created_at=timestamp,
        plan_name=plan_name,
        suite=suite,
        host=host,
        sources=tuple(sources),
        inputs=(
            FileIdentity.from_path("test plan", plan_path),
            FileIdentity.from_path("hardware registry", hardware_path),
        ),
        artifacts=tuple(artifacts),
        invocations=tuple(
            InvocationIdentity.from_invocation(item) for item in invocations
        ),
        environment=_frozen(
            environment if environment is not None else harness_environment.capture()
        ),
        build=_frozen(build or {}),
        leases=tuple(_frozen(item) for item in leases),
    )
