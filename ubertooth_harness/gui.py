"""Web GUI support for the controller: assets, headers, refs, plans, failures.

The GUI is three static files under ``ubertooth_harness/web/`` (vanilla
JavaScript, no build step, nothing loaded from another origin). They are read
once into memory and served by exact name, so no request path ever reaches
the filesystem. Every HTTP response carries the headers from
``security_headers``: a strict Content-Security-Policy (scripts and styles
from this origin only, no inline script, no framing) plus nosniff,
X-Frame-Options and Referrer-Policy.

The ref list comes from ``git ls-remote`` against the configured Ubertooth
repository (the same public repository the builds clone), cached briefly. A
run may only name a ref from that list.
"""

import asyncio
import collections
import dataclasses
import datetime
import os
from pathlib import Path
import re
import time
from typing import Awaitable, Callable, Mapping, Sequence

from ubertooth_harness.catalog import CatalogError, TestCatalog, builtin_catalog
from ubertooth_harness.learned import (
    BUILD_STAGE,
    CATALOG,
    LEARNED,
    PREFLIGHT_STAGE,
    PREFLIGHT_UNITS,
    RUN_STAGES,
    UNKNOWN,
    EstimateStore,
)
from ubertooth_harness.manifest import ManifestError, _safe_repository
from ubertooth_harness.plan import PlanError, TestStep, load_plan
from ubertooth_harness.protocol import _git_ref, _name


DEFAULT_UBERTOOTH_REPOSITORY = "https://github.com/SqueakyFloors/ubertooth.git"
DEFAULT_PLANS_DIR = Path(__file__).resolve().parents[1] / "testplans"
WEB_DIR = Path(__file__).resolve().with_name("web")
# Served by exact name; nothing else under /ui/ exists.
ASSETS: Mapping[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
REFS_TTL_S = 60.0
REFS_FAILURE_TTL_S = 5.0
LS_REMOTE_TIMEOUT_S = 20.0
MAX_LS_REMOTE_BYTES = 8 * 1024 * 1024
# Failure summary bounds.
FAILURE_LOG_LINES = 5
_MAX_FIELD = 2000

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


# ----- static assets and headers -------------------------------------------


def load_assets(directory: Path = WEB_DIR) -> dict[str, tuple[bytes, str]]:
    """Every GUI asset as (bytes, media type), read once at app creation."""
    return {
        name: ((directory / name).read_bytes(), media)
        for name, media in ASSETS.items()
    }


def content_security_policy(host: str) -> str:
    """The GUI's CSP; ``host`` is the already-validated Host header."""
    return "; ".join((
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        f"connect-src 'self' ws://{host}",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ))


def security_headers(host: str) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-security-policy", content_security_policy(host).encode("latin-1")),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"cache-control", b"no-store"),
    ]


# ----- git refs ------------------------------------------------------------


class RefListError(RuntimeError):
    """The repository's refs could not be listed."""


@dataclasses.dataclass(frozen=True)
class GitRef:
    name: str
    kind: str  # default (HEAD), branch, or tag
    commit: str

    def view(self) -> dict:
        return dataclasses.asdict(self)


_KIND_ORDER = {"default": 0, "branch": 1, "tag": 2}


def parse_ls_remote(text: str) -> list[GitRef]:
    """HEAD, branches and tags from ``git ls-remote`` output.

    Annotated tags use their peeled commit. Names the run protocol would
    refuse are dropped. Order: HEAD, branches by name, tags newest name first.
    """
    found: dict[tuple[str, str], str] = {}
    peeled: dict[str, str] = {}
    for line in text.splitlines():
        commit, separator, full = line.partition("\t")
        if not separator or not _COMMIT_RE.fullmatch(commit):
            continue
        if full == "HEAD":
            kind, name = "default", "HEAD"
        elif full.startswith("refs/heads/"):
            kind, name = "branch", full.removeprefix("refs/heads/")
        elif full.startswith("refs/tags/"):
            kind, name = "tag", full.removeprefix("refs/tags/")
            if name.endswith("^{}"):
                peeled[name.removesuffix("^{}")] = commit
                continue
        else:
            continue
        if _git_ref(name):
            found.setdefault((kind, name), commit)
    refs = [
        GitRef(name, kind, peeled.get(name, commit) if kind == "tag" else commit)
        for (kind, name), commit in found.items()
    ]
    branches = sorted((r for r in refs if r.kind != "tag"),
                      key=lambda r: (_KIND_ORDER[r.kind], r.name))
    tags = sorted((r for r in refs if r.kind == "tag"), key=lambda r: r.name, reverse=True)
    return branches + tags


GitCommand = Callable[[Sequence[str], float], Awaitable[tuple[int, bytes, bytes]]]


async def run_git(argv: Sequence[str], timeout_s: float) -> tuple[int, bytes, bytes]:
    """Run git without a terminal or credential prompt, bounded in time."""
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
    environment.pop("SSH_ASKPASS", None)
    process = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=environment,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RefListError(f"git ls-remote did not finish in {timeout_s:g}s") from None
    return process.returncode, stdout, stderr


class GitRefSource:
    """Branches and tags of one repository, from ``git ls-remote``, cached."""

    def __init__(
        self,
        repository: str,
        *,
        ttl_s: float = REFS_TTL_S,
        failure_ttl_s: float = REFS_FAILURE_TTL_S,
        timeout_s: float = LS_REMOTE_TIMEOUT_S,
        git: GitCommand = run_git,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(repository, str) or not repository or "\n" in repository:
            raise ValueError("repository must be a non-empty single line")
        self.repository = repository
        try:
            # Never show credentials embedded in a URL.
            self.display = _safe_repository(repository)
        except ManifestError:
            self.display = "(invalid repository URL)"
        self.ttl_s = ttl_s
        self.failure_ttl_s = failure_ttl_s
        self.timeout_s = timeout_s
        self.git = git
        self.clock = clock
        self._lock = asyncio.Lock()
        self._cached: dict | RefListError | None = None
        self._expires = 0.0

    def argv(self) -> list[str]:
        return [
            "git", "-c", "protocol.ext.allow=never", "ls-remote", "--",
            self.repository, "HEAD", "refs/heads/*", "refs/tags/*",
        ]

    async def refs(self) -> dict:
        """{"repository", "refs": [...], "fetched_at"}; raises RefListError."""
        async with self._lock:
            if self._cached is not None and self.clock() < self._expires:
                if isinstance(self._cached, RefListError):
                    raise self._cached
                return self._cached
            try:
                result = await self._list()
            except RefListError as error:
                self._cached, self._expires = error, self.clock() + self.failure_ttl_s
                raise
            self._cached, self._expires = result, self.clock() + self.ttl_s
            return result

    async def _list(self) -> dict:
        try:
            code, stdout, stderr = await self.git(self.argv(), self.timeout_s)
        except OSError as error:
            raise RefListError(f"cannot run git: {error}") from None
        if code != 0:
            message = stderr.decode("utf-8", "replace").strip().splitlines()
            detail = message[-1][:300] if message else f"exit {code}"
            raise RefListError(f"git ls-remote {self.display} failed: {detail}")
        if len(stdout) > MAX_LS_REMOTE_BYTES:
            raise RefListError("git ls-remote output is too large")
        refs = parse_ls_remote(stdout.decode("utf-8", "replace"))
        return {
            "repository": self.display,
            "refs": [ref.view() for ref in refs],
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"
            ),
        }


# ----- plans ---------------------------------------------------------------


def _estimate(catalog: TestCatalog, test_name, arguments) -> float | None:
    """The catalog's seconds for one invocation, or None.

    None for a test the catalog does not know, arguments it refuses, or a
    test with neither an estimate nor a duration argument.
    """
    try:
        definition = catalog.get(test_name)
        return definition.estimated_seconds(definition.normalize_arguments(arguments))
    except (CatalogError, TypeError, ValueError):
        return None


def estimate_fields(
    catalog: TestCatalog,
    test_name,
    arguments,
    *,
    estimates: EstimateStore | None = None,
    host: str | None = None,
) -> dict:
    """One invocation's estimate: the learned one when there is one.

    ``estimated_s`` is the best estimate, ``catalog_estimated_s`` always the
    hand-calibrated one, and ``estimate_source``/``estimate_samples`` say
    where the value came from (``learned`` with its sample count, ``catalog``,
    or ``none`` when neither knows). Without a target host (the plan listing
    with no ``?target=``) nothing is learned and the catalog value stands, so
    a client that ignores the new fields sees exactly what it did before.
    """
    catalog_s = _estimate(catalog, test_name, arguments)
    learned = (
        estimates.lookup_test(catalog, test_name, arguments, host)
        if estimates is not None and host else None
    )
    if learned is not None:
        return {
            "estimated_s": learned.seconds, "catalog_estimated_s": catalog_s,
            "estimate_source": LEARNED, "estimate_samples": learned.samples,
        }
    return {
        "estimated_s": catalog_s, "catalog_estimated_s": catalog_s,
        "estimate_source": CATALOG if catalog_s is not None else UNKNOWN,
        "estimate_samples": 0,
    }


def _per_board(catalog: TestCatalog, step: TestStep) -> bool:
    """True when the step runs once per selected board (a role set to each)."""
    try:
        roles = catalog.get(step.test_name).hardware_roles or {}
    except CatalogError:
        return False
    return any(step.arguments.get(role) == "each" for role in roles)


def list_plans(
    plans_dir: Path,
    *,
    estimates: EstimateStore | None = None,
    target: str | None = None,
) -> list[dict]:
    """Every ``<name>.yaml`` plan with its suites, tests and estimates.

    A plan that does not load is listed with its error and no suites.
    ``runnable`` is the plan's ``runnable`` flag (false for plans of planned
    tests, such as backlog.yaml); the GUI offers only runnable plans.
    Estimates are per invocation: a ``per_board`` test runs once for every
    board it selects on the target, which only the run's plan (its manifest)
    counts; see planned_invocations.

    With a ``target`` host, each test's ``estimated_s`` is what that host's
    own finished runs measured, when enough of them did (see
    ``estimate_fields``); without one it is the catalog estimate, as before.
    """
    plans = []
    try:
        paths = sorted(Path(plans_dir).glob("*.yaml"))
    except OSError:
        return []
    catalog = builtin_catalog()
    for path in paths:
        if not _name(path.stem) or not path.is_file():
            continue
        try:
            plan = load_plan(path, catalog)
        except PlanError as error:
            plans.append({"name": path.stem, "title": None, "error": str(error),
                          "runnable": False, "suites": []})
            continue
        suites = []
        for suite, steps in plan.suites.items():
            tests = [
                {
                    "name": step.test_name,
                    **estimate_fields(catalog, step.test_name, step.arguments,
                                      estimates=estimates, host=target),
                    "per_board": _per_board(catalog, step),
                }
                for step in steps
            ]
            seconds = [test["estimated_s"] for test in tests]
            suites.append({
                "name": suite,
                "tests": tests,
                # Only when every test has one; build and preflight excluded.
                "estimated_s": (
                    round(sum(seconds), 1)
                    if seconds and None not in seconds else None
                ),
                "unestimated_tests": seconds.count(None),
                "per_board_tests": sum(test["per_board"] for test in tests),
            })
        plans.append({"name": path.stem, "title": plan.name, "error": None,
                      "runnable": plan.runnable, "suites": suites})
    return plans


def _manifest_details(events: Sequence[Mapping]) -> Mapping:
    """The details of the run's ``manifest`` record ({} until it arrives)."""
    details: Mapping = {}
    for event in events:
        if isinstance(event, Mapping) and event.get("event_type") == "manifest":
            found = event.get("details")
            if isinstance(found, Mapping):
                details = found
    return details


def _manifest(events: Sequence[Mapping]) -> Mapping | None:
    manifest = _manifest_details(events).get("manifest")
    return manifest if isinstance(manifest, Mapping) else None


def planned_invocations(
    events: Sequence[Mapping],
    catalog: TestCatalog | None = None,
    *,
    estimates: EstimateStore | None = None,
) -> list[dict] | None:
    """The run's invocations on its target, in order, each with its estimate.

    Read from the run's ``manifest`` record, which lists exactly what the
    planner compiled for the target host: one invocation per selected board
    for ``Ubertooth: each``. None until that record arrives (or if it is
    malformed); the live view then lists the plan's tests instead.

    Each invocation names its own host, so with an ``estimates`` store the
    estimate is what that host's finished runs measured for the same test,
    coverage and duration (``estimate_fields``).
    """
    manifest = _manifest(events)
    if manifest is None:
        return None
    invocations = manifest.get("invocations")
    if not isinstance(invocations, list):
        return None
    catalog = catalog or builtin_catalog()
    planned = []
    for invocation in invocations:
        if not isinstance(invocation, Mapping):
            return None
        host = invocation.get("host")
        planned.append({
            "invocation_id": _text(invocation.get("id")),
            "test_name": _text(invocation.get("test_name")),
            **estimate_fields(
                catalog, invocation.get("test_name"), invocation.get("arguments"),
                estimates=estimates,
                host=host if isinstance(host, str) else None,
            ),
        })
    return planned


def run_stage_estimates(
    estimates: EstimateStore | None,
    events: Sequence[Mapping],
    host: str | None,
    *,
    build: bool = False,
) -> list[dict]:
    """What the run's build and preflight are expected to take, if known.

    Only learned: neither stage has a catalog estimate, so a target that has
    not finished a run of its own gets an empty list. Preflight scales with
    its number of checks, which the run's manifest record gives; a build is
    listed as soon as the run requests one.
    """
    if estimates is None or not host:
        return []
    checks = _manifest_details(events).get("requested_preflight_checks")
    if isinstance(checks, bool) or not isinstance(checks, int) or checks <= 0:
        checks = None
    wanted: list[tuple[str, str, object]] = []
    if build:
        # The runner builds host tools only, so the firmware variant is not
        # asked for here; a stored one is still listed by /api/plans.
        wanted.append((BUILD_STAGE, "host tools", None))
    if checks is not None:
        wanted.append((PREFLIGHT_STAGE, "", {"argument": PREFLIGHT_UNITS,
                                             "value": checks}))
    stages = []
    for stage, profile, units in wanted:
        learned = estimates.lookup_stage(stage, host, profile=profile, units=units)
        if learned is None:
            continue
        stages.append({
            "stage": stage, "profile": profile, "units": units,
            "estimated_s": learned.seconds, "estimate_source": LEARNED,
            "estimate_samples": learned.samples,
        })
    return stages


def learned_stages(
    estimates: EstimateStore | None, host: str | None
) -> list[dict]:
    """Every build, preflight and flash duration learned for one host."""
    if estimates is None or not host:
        return []
    return estimates.entries(host=host, stages=RUN_STAGES)


# ----- failure summary -----------------------------------------------------


def _text(value) -> str:
    if value is None:
        return ""
    return str(value)[:_MAX_FIELD]


def omitted_line(details: Mapping) -> str:
    """One log line for a command_output_omitted record."""
    summary = details.get("output_omitted")
    lines = summary.get("lines") if isinstance(summary, dict) else None
    count = sum(
        value for value in (lines or {}).values() if isinstance(value, int)
    ) if isinstance(lines, dict) else 0
    return _text(
        f"[... {count} output lines left out; full output in "
        f"{details.get('output_file', '')} ...]"
    )


# Tail key for build command output, which has no invocation.
_BUILD_TAIL = "\0build"


def _tail_key(details: Mapping) -> str:
    """Which failure log an output record belongs to: its test, or the build."""
    invocation = _text(details.get("invocation_id"))
    if not invocation and details.get("phase") == "build":
        return _BUILD_TAIL
    return invocation


def failure_summary(run: Mapping, events: Sequence[Mapping]) -> list[dict]:
    """What failed in a run: one entry per failed phase or test, then the run.

    Each entry has ``step``, ``status``, ``reason`` and, for tests and a
    failed build, the last few output lines printed (``log``).
    """
    failures: list[dict] = []
    tails: dict[str, collections.deque] = {}
    partials: dict[str, str] = {}
    finished = None
    for event in events:
        details = event.get("details")
        if not isinstance(details, dict):
            details = {}
        kind = event.get("event_type")
        status = event.get("status")
        if kind == "command_output":
            text = details.get("text")
            invocation = _tail_key(details)
            if isinstance(text, str) and invocation:
                tail = tails.setdefault(invocation, collections.deque(maxlen=FAILURE_LOG_LINES))
                # Output chunks need not end at a line break: join them first.
                lines = (partials.get(invocation, "") + text).split("\n")
                partials[invocation] = lines.pop()[-_MAX_FIELD:]
                tail.extend(line[:500] for line in lines if line.strip())
        elif kind == "command_output_omitted":
            # Output left out of run.jsonl (kept in output/): end the partial
            # line and mark the gap before the command's last records.
            invocation = _tail_key(details)
            if invocation:
                tail = tails.setdefault(invocation, collections.deque(maxlen=FAILURE_LOG_LINES))
                rest = partials.pop(invocation, "")
                if rest.strip():
                    tail.append(rest[:500])
                tail.append(omitted_line(details))
        elif kind in ("build_finished", "preflight_finished") and status not in (
            "passed", "skipped"
        ):
            log = []
            if kind == "build_finished":
                rest = partials.pop(_BUILD_TAIL, "")
                if rest.strip():
                    tails.setdefault(
                        _BUILD_TAIL, collections.deque(maxlen=FAILURE_LOG_LINES)
                    ).append(rest[:500])
                log = list(tails.get(_BUILD_TAIL, ()))
            failures.append({
                "step": kind.removesuffix("_finished"), "status": _text(status),
                "reason": _text(details.get("reason")), "log": log,
            })
        elif kind == "test_finished" and status not in ("passed", "skipped", "not_applicable"):
            invocation = _text(details.get("invocation_id"))
            rest = partials.pop(invocation, "")
            if rest.strip():
                tails.setdefault(
                    invocation, collections.deque(maxlen=FAILURE_LOG_LINES)
                ).append(rest[:500])
            failures.append({
                "step": _text(details.get("test_name") or invocation),
                "status": _text(status), "reason": _text(details.get("reason")),
                "log": list(tails.get(invocation, ())),
            })
        elif kind == "run_finished":
            finished = event
    if finished is not None and finished.get("status") != "passed":
        details = finished.get("details") if isinstance(finished.get("details"), dict) else {}
        failures.append({
            "step": "run", "status": _text(finished.get("status")),
            "reason": _text(details.get("failure_reason")), "log": [],
        })
    elif finished is None and run.get("status") not in ("running", "passed"):
        failures.append({
            "step": "run", "status": _text(run.get("status")),
            "reason": _text(run.get("reason")), "log": [],
        })
    return failures
