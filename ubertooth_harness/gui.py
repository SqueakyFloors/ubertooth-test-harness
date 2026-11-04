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

from ubertooth_harness.catalog import (
    REQUIRED_HARDWARE_ARGUMENT,
    CatalogError,
    TestCatalog,
    builtin_catalog,
)
from ubertooth_harness.learned import (
    BUILD_STAGE,
    CATALOG,
    LEARNED,
    PREFLIGHT_STAGE,
    PREFLIGHT_UNITS,
    RUN_STAGES,
    UNKNOWN,
    EstimateStore,
    build_profile,
)
from ubertooth_harness.manifest import ManifestError, _safe_repository
from ubertooth_harness.outputfiles import output_file_list
from ubertooth_harness.hostneeds import (
    TEST_BUILD_LABEL,
    platform_capabilities,
    unmet,
)
from ubertooth_harness.plan import PlanError, TestStep, image_label, load_plan
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
    or ``none`` when neither knows). ``estimate_scaled`` marks a value
    computed from the test's per-unit rate rather than from this duration's
    own median; ``estimate_samples`` is then every sample that rate rests
    on, at this duration and at the others. Without a target host (the plan
    listing with no ``?target=``)
    nothing is learned and the catalog value stands, so a client that ignores
    the new fields sees exactly what it did before.
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
            "estimate_scaled": learned.scaled,
        }
    return {
        "estimated_s": catalog_s, "catalog_estimated_s": catalog_s,
        "estimate_source": CATALOG if catalog_s is not None else UNKNOWN,
        "estimate_samples": 0, "estimate_scaled": False,
    }


def step_hosts(arguments: Mapping) -> tuple[str, ...]:
    """The host aliases a plan step names, or () for ``hosts: each``.

    ``hosts:`` is normalized to ``each`` or a tuple of aliases (catalog.py);
    the planner reads the singular ``host`` first, so this does too.
    """
    selector = arguments.get("host", arguments.get("hosts", "each"))
    if selector == "each":
        return ()
    if isinstance(selector, str):
        return (selector,)
    return tuple(str(alias) for alias in selector)


def omitted_reason(
    catalog: TestCatalog,
    test_name,
    arguments: Mapping,
    target: str | None,
    platform: str | None = None,
) -> str:
    """Why a run on ``target`` would not run this step, or ``''``.

    What planning decides for the host itself, in the order it decides it
    (``PlanCompiler.compile_suite``): a step that names hosts is compiled for
    the hosts it names and for no other, and then a host whose platform the
    step does not run on is left out as not applicable. ``platform`` is the
    target's own (the controller has it from the target's registration);
    without one only the plan's host naming is read, since nothing else here
    knows what the target is.

    The absences this cannot answer are the ones that need the target's
    resource registry, which the controller has not got: hardware no host
    here has, and a board placed differently from what the test needs. Those
    steps are counted, and the run itself reports them.

    One of those is why a step is reported as left out rather than as the
    run's undoing: where every host a step names is in that bench's registry
    and none of them can run it, compiling the suite is a plan error there
    and the run is refused outright ("no selected host can run it"). Which
    hosts that bench has is exactly what is not known here, so what is
    reported is the one answer that holds either way: this target does not
    run the step.
    """
    if not target:
        return ""
    hosts = step_hosts(arguments)
    if hosts and target not in hosts:
        return f"runs on {', '.join(hosts)}, not on {target}"
    if not platform:
        return ""
    # Exactly what the planner asks of a host before any hardware: the
    # platform, and the test-wide needs only the platform can decide
    # (BlueZ-only tools). Every other capability is unknown here, which
    # hostneeds reads as met.
    capabilities = platform_capabilities(target, platform)
    try:
        needs = catalog.get(test_name).test_needs(arguments)
    except CatalogError:
        return ""
    return next(
        (unmet(need, capabilities) for need in needs if unmet(need, capabilities)),
        "",
    )


def bound_hardware(catalog: TestCatalog, test_name) -> str:
    """The hardware a test binds, role by role, for a reader.

    ``Ubertooth, ESP32 Classic peer (acceptor), ESP32 Classic peer
    (initiator)``: the resource type each role takes, named with the role
    except where the role is the type. Empty for a test that binds none.
    """
    try:
        roles = catalog.get(test_name).hardware_roles or {}
    except CatalogError:
        return ""
    return ", ".join(
        resource_type if role == resource_type else f"{resource_type} ({role})"
        for role, resource_type in roles.items()
    )


def _hosts_text(hosts: Sequence[str]) -> str:
    return ", ".join(hosts) if hosts else "no host"


def run_plan_check(
    catalog: TestCatalog,
    steps: Sequence[TestStep],
    hosts: Sequence[str],
    *,
    platforms: Mapping[str, str | None] | None = None,
    tests: Sequence[str] = (),
) -> dict:
    """What a run of these steps on these hosts would run, and what must
    stop it being started.

    ``{"runs", "omitted", "blocked", "refusal"}``: how many steps at least
    one of the hosts would run, the steps none of them would (each with the
    reason for the first host), the required ones among those, and the one
    sentence to refuse the run with, or ``''`` when there is nothing to
    refuse.

    Two things stop a run here, both decided by the plan before anything is
    leased or built:

    - no host of the run runs a single step of the suite, which run-suite
      refuses on the target anyway ("no selected invocation");
    - a step with ``required hardware: true`` whose ``hosts:`` names no host
      of this run. That argument says the step's own hardware is what it
      measures, so a bench without it fails the run rather than passing the
      gate unmeasured (#269, #282) — and a run that was never going to
      compile the step cannot measure it either, whatever the bench has.
      Offering it is offering a release gate nobody measures.

    Only the host naming decides that second one, as it decides it in
    planning: a host left out for its platform is not applicable, which
    ``required hardware:`` deliberately does not change (#282), so a step
    this target's platform cannot run is not a refusal here.

    ``tests`` is the run's selected test names where it names any, since
    run-suite then runs and judges only those. ``platforms`` is each host's
    platform where it is known.
    """
    chosen = set(tests)
    if chosen and not any(step.test_name in chosen for step in steps):
        # A selection this suite holds nothing of. The target has its own
        # copy of the plans, so that is as likely to be a controller whose
        # copy is older as a selection nothing could run, and the target
        # refuses what it cannot run in its own words.
        return {"runs": 0, "omitted": [], "blocked": [], "refusal": ""}
    runs = 0
    running: set[str] = set()
    omitted: list[dict] = []
    for step in steps:
        if chosen and step.test_name not in chosen:
            continue
        reasons = [
            omitted_reason(
                catalog, step.test_name, step.arguments, host,
                (platforms or {}).get(host),
            )
            for host in hosts
        ]
        if not hosts or "" in reasons:
            runs += 1
            running.add(step.test_name)
            continue
        named = step_hosts(step.arguments)
        omitted.append({
            "name": step.test_name,
            "reason": reasons[0],
            "hosts": list(named),
            "hardware": bound_hardware(catalog, step.test_name),
            # Required, and not run here because the plan names other hosts:
            # the absence `required hardware:` is about. A platform this run
            # cannot meet is not (see above).
            "required": (
                step.arguments.get(REQUIRED_HARDWARE_ARGUMENT) is True
                and bool(named)
                and not set(named) & set(hosts)
            ),
        })
    blocked: list[dict] = []
    for item in omitted:
        # One entry per test, and nothing for a test some host of this run
        # runs: that run measures the gate. The planner's own rule
        # (CompiledSuite.blocked_for_host) is the same shape over a whole
        # bench, which is a different question: a third registered host
        # running the step keeps the bench covered, and leaves this run
        # unable to measure it, which is what this refusal is about.
        if not item["required"] or item["name"] in running:
            continue
        if any(entry["name"] == item["name"] for entry in blocked):
            continue
        blocked.append(item)
    return {
        "runs": runs, "omitted": omitted, "blocked": blocked,
        "refusal": _refusal(hosts, runs, omitted, blocked),
    }


def _refusal(
    hosts: Sequence[str], runs: int, omitted: Sequence[Mapping],
    blocked: Sequence[Mapping],
) -> str:
    """The sentence a run that must not be started is refused with."""
    where = _hosts_text(hosts)
    if not hosts:
        return ""
    if not runs:
        named = "; ".join(
            f"{item['name']} {item['reason']}" for item in omitted[:3]
        )
        rest = len(omitted) - 3
        # A suite with no steps at all (a hand-written plan) has nothing to
        # name, so it is said without a list rather than with an empty one.
        return (
            f"no test of this suite runs on {where}"
            + (f": {named}" if named else "")
            + (f"; and {rest} more" if rest > 0 else "")
        )
    if blocked:
        return (
            f"a run on {where} would not measure what this suite requires: "
            + "; ".join(
                f"{item['name']} is required on {_hosts_text(item['hosts'])}"
                + (f", which needs {item['hardware']} there" if item["hardware"] else "")
                for item in blocked
            )
            + f". Start the suite on {_hosts_text(sorted({host for item in blocked for host in item['hosts']}))}"
            + " (a run group may take several hosts at once), or choose a"
            " suite without that step."
        )
    return ""


def _per_board(catalog: TestCatalog, step: TestStep) -> bool:
    """True when the step runs once per selected board (a role set to each)."""
    try:
        roles = catalog.get(step.test_name).hardware_roles or {}
    except CatalogError:
        return False
    return any(step.arguments.get(role) == "each" for role in roles)


_NO_BUILD_NEEDS = {
    "needs_firmware_build": False, "needs_btbr_build": False,
    "needs_python_tools": False,
    "requires_firmware_build": False, "requires_btbr_build": False,
    "requires_python_tools": False,
}
_BUILD_NEED_FIELDS = tuple(_NO_BUILD_NEEDS)


def _build_needs(catalog: TestCatalog, test_name, arguments=None) -> dict:
    """The build options a step needs: ``run-suite --with-firmware`` for the
    run's test image, ``--with-btbr`` for the alternate btbr image,
    ``--with-python`` for the Python tools.

    ``needs_*`` is what some case of it needs, and leaves that case out as
    not applicable when the run does not have it; ``requires_*`` is what the
    whole test is refused without, which implies ``needs_*``. Those come from
    what the catalog declares (``TestDefinition``), which is the only way to
    know here: a case's own needs are known once the cases are built from the
    host's registry resources, and this listing has no registry.

    The btbr image is its own field rather than read off the firmware build:
    a firmware build makes the application image, and a run carrying that one
    alone would otherwise look able to run the btbr test (#279).

    ``needs_artifacts`` is every image the step names that no build makes, so
    only ``--artifact`` can pass it; a test whose plan entry names its images
    (Flash reliability) also requires the firmware build when it names the
    run's own. A test the catalog does not know needs nothing.
    """
    try:
        definition = catalog.get(test_name)
    except CatalogError:
        return {**_NO_BUILD_NEEDS, "needs_artifacts": []}
    needed = definition.build_options()
    required = definition.required_build_options()
    images = definition.image_labels(arguments)
    own_image = any(image_label(label) == TEST_BUILD_LABEL for label in images)
    return {
        "needs_firmware_build": needed.firmware_build or own_image,
        "needs_btbr_build": needed.btbr_build,
        "needs_python_tools": needed.python_tools,
        "requires_firmware_build": required.firmware_build or own_image,
        "requires_btbr_build": required.btbr_build,
        "requires_python_tools": required.python_tools,
        # Images no build produces: only the command line can pass them, so
        # a run started from here would be refused before its tests.
        "needs_artifacts": [
            label for label in images if image_label(label) != TEST_BUILD_LABEL
        ],
    }


def _total(tests: Sequence[Mapping]) -> float | None:
    """The tests' estimates added up, or None unless every one has an
    estimate. 0.0 for no tests at all, which adds up with the other total."""
    seconds = [test["estimated_s"] for test in tests]
    if None in seconds:
        return None
    return round(float(sum(seconds)), 1)


def _suite_view(name: str, tests: Sequence[Mapping], check: Mapping) -> dict:
    """One suite as /api/plans reports it, for the target the tests were
    listed for.

    Everything the client acts on is counted over the steps that target
    would run (``runs_on_target``): the time estimate, the build options to
    ask for, and the images only ``--artifact`` can pass. What the target
    does not run costs it nothing and asks nothing of its run. The steps
    left out are reported beside those, each with the time it would take on
    the host that does run it, so a total that is smaller than the suite's
    is accounted for rather than merely smaller.

    ``check`` is ``run_plan_check`` for the same target: whether a run of
    this suite there may be started at all, and what stops it
    (``blocked_tests``, the required steps no host of such a run would run,
    each with the hardware it is about and where). The GUI holds Run for a
    suite that says no, and the controller refuses it; the reason is one
    sentence, the same one both show.

    With no target nothing is left out and nothing is refused, so the fields
    are what they were: the sum of every step in the suite.
    """
    running = [test for test in tests if test["runs_on_target"]]
    omitted = [test for test in tests if not test["runs_on_target"]]
    return {
        "name": name,
        "tests": list(tests),
        # Only when every test this target runs has one; build and
        # preflight excluded. None for a suite with no step for the target,
        # which is a run the target would refuse (it has nothing to run).
        "estimated_s": _total(running) if running else None,
        "unestimated_tests": sum(
            test["estimated_s"] is None for test in running
        ),
        "per_board_tests": sum(test["per_board"] for test in running),
        # True when any test the target runs needs (or cannot run without)
        # that build option, so a client that dispatches the suite (the GUI)
        # can ask for it.
        **{
            field: any(test[field] for test in running)
            for field in _BUILD_NEED_FIELDS
        },
        # Every image the suite needs that no build produces.
        "needs_artifacts": sorted(
            {label for test in running for label in test["needs_artifacts"]}
        ),
        # The steps this target does not run: each one's name, why, and what
        # it would add where it does run.
        "omitted_tests": [
            {"name": test["name"], "reason": test["omitted_reason"],
             "estimated_s": test["estimated_s"]}
            for test in omitted
        ],
        # Their time together: 0.0 when none is left out, None when one of
        # them has no estimate.
        "omitted_estimated_s": _total(omitted),
        # The required steps a run on this target would not run: the gate it
        # could not measure, with the hardware it is about and the hosts
        # that have it. Empty with no target given.
        "blocked_tests": [
            {"name": item["name"], "hosts": item["hosts"],
             "hardware": item["hardware"], "reason": item["reason"]}
            for item in check["blocked"]
        ],
        # Whether a run of this suite on this target may be started, and the
        # one sentence saying why not.
        "runnable_on_target": not check["refusal"],
        "not_runnable_reason": check["refusal"],
    }


def list_plans(
    plans_dir: Path,
    *,
    estimates: EstimateStore | None = None,
    target: str | None = None,
    platform: str | None = None,
) -> list[dict]:
    """Every ``<name>.yaml`` plan with its suites, tests and estimates.

    A plan that does not load is listed with its error and no suites.
    ``runnable`` is the plan's ``runnable`` flag (false for plans of planned
    tests, such as backlog.yaml); the GUI offers only runnable plans. Each
    test, and each suite that holds one such test, carries
    ``needs_firmware_build``, ``needs_btbr_build``, ``needs_python_tools``,
    ``requires_firmware_build``, ``requires_btbr_build`` and
    ``requires_python_tools``: the build options a run of it should or must
    ask for, and ``needs_artifacts``: the images no build produces, which
    only ``--artifact`` can pass (see ``_build_needs``).
    Estimates are per invocation: a ``per_board`` test runs once for every
    board it selects on the target, which only the run's plan (its manifest)
    counts; see planned_invocations.

    With a ``target`` host, each test's ``estimated_s`` is what that host's
    own finished runs measured, when enough of them did (see
    ``estimate_fields``); without one it is the catalog estimate, as before.

    A ``target`` also decides which steps the suite's own fields count: a
    step that target would not run adds nothing to its estimate and asks
    nothing of its run, and is reported in ``omitted_tests`` instead (see
    ``omitted_reason`` and ``_suite_view``). ``platform`` is that target's
    own host platform, which decides a step the target could not run
    whichever hosts it names. Without a target every step counts, which is
    the suite's whole time whichever host runs a part of it.
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
            tests = []
            for step in steps:
                reason = omitted_reason(
                    catalog, step.test_name, step.arguments, target, platform
                )
                # A step this target does not run is timed where it does:
                # for the host it names, when it names exactly one, so the
                # figure is that host's learned estimate rather than this
                # target's for a test this target never runs.
                named = step_hosts(step.arguments)
                host = target
                if reason:
                    host = named[0] if len(named) == 1 else None
                tests.append({
                    "name": step.test_name,
                    **estimate_fields(catalog, step.test_name, step.arguments,
                                      estimates=estimates, host=host),
                    "per_board": _per_board(catalog, step),
                    # Whether a run on the target would run this step at all,
                    # and why not (``omitted_reason``). True for every step
                    # with no target given.
                    "runs_on_target": not reason,
                    "omitted_reason": reason,
                    **_build_needs(catalog, step.test_name, step.arguments),
                })
            check = run_plan_check(
                catalog, steps, (target,) if target else (),
                platforms={target: platform} if target else None,
            )
            suites.append(_suite_view(suite, tests, check))
        plans.append({"name": path.stem, "title": plan.name, "error": None,
                      "runnable": plan.runnable, "suites": suites})
    return plans


def suite_refusal(
    plans_dir: Path,
    plan: str,
    suite: str,
    hosts: Sequence[str],
    *,
    platforms: Mapping[str, str | None] | None = None,
    tests: Sequence[str] = (),
    catalog: TestCatalog | None = None,
) -> str:
    """Why a run of this suite on these hosts must not be started, or ``''``.

    One plan file, read the way the runner reads it (an exact name in the
    plans directory), then ``run_plan_check``. A plan or suite this
    directory has not got is not judged: the runner refuses an unknown plan
    in its own words, and a plan the controller cannot read is not a reason
    to refuse a run the target could carry out with its own copy.

    Every run start calls this, and the plan is read each time, which is a
    few milliseconds and always the file as it now stands. The catalog is
    the caller's, because building one allocates enough to be worth holding
    (see ``Controller.catalog``).
    """
    if not _name(plan) or not isinstance(suite, str):
        return ""  # the protocol refuses the request in its own words
    path = Path(plans_dir) / f"{plan}.yaml"
    try:
        if not path.is_file():
            return ""
        catalog = catalog or builtin_catalog()
        steps = load_plan(path, catalog).steps(suite)
    except (CatalogError, PlanError, OSError):
        return ""
    return run_plan_check(
        catalog, steps, list(hosts), platforms=platforms, tests=tests
    )["refusal"]


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


def _build_options(
    events: Sequence[Mapping], *, with_firmware: bool, with_python: bool
) -> dict[str, bool]:
    """Which optional parts this run's build includes.

    Each option comes from the run's own ``build_started`` record when that
    record names it (which also covers a run that built the firmware only
    because it flashed its boards, and a run adopted from a runner report,
    which carries no request), else from what the run was asked for. The two
    are read one by one because a record written before an option was
    recorded names only the other.
    """
    options = {"with_firmware": bool(with_firmware), "with_python": bool(with_python)}
    pending = set(options)
    for event in events:
        if not pending:
            break
        if not isinstance(event, Mapping) or event.get("event_type") != "build_started":
            continue
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for option in list(pending):
            if option in details:
                options[option] = bool(details[option])
                pending.discard(option)
    return options


def run_stage_estimates(
    estimates: EstimateStore | None,
    events: Sequence[Mapping],
    host: str | None,
    *,
    build: bool = False,
    with_firmware: bool = False,
    with_python: bool = False,
) -> list[dict]:
    """What the run's build and preflight are expected to take, if known.

    Only learned: neither stage has a catalog estimate, so a target that has
    not finished a run of its own gets an empty list. Preflight scales with
    its number of checks, which the run's manifest record gives; a build is
    listed as soon as the run asks for one or its records show one started.
    The firmware image and the Python tools each add real work to a build
    (the firmware much the more of it), so the estimate is looked up for the
    build this run makes: what its own ``build_started`` record says, else
    what the run was asked for.
    """
    if estimates is None or not host:
        return []
    checks = _manifest_details(events).get("requested_preflight_checks")
    if isinstance(checks, bool) or not isinstance(checks, int) or checks <= 0:
        checks = None
    wanted: list[tuple[str, str, object]] = []
    # A run adopted from a runner report carries no request, so its own
    # records are what say whether it builds.
    building = build or any(
        isinstance(event, Mapping) and event.get("event_type") == "build_started"
        for event in events
    )
    if building:
        wanted.append((BUILD_STAGE, build_profile(**_build_options(
            events, with_firmware=with_firmware, with_python=with_python)), None))
    if checks is not None:
        # A float, as the stored keys and /api/plans report units.
        wanted.append((PREFLIGHT_STAGE, "", {"argument": PREFLIGHT_UNITS,
                                             "value": float(checks)}))
    stages = []
    for stage, profile, units in wanted:
        learned = estimates.lookup_stage(stage, host, profile=profile, units=units)
        if learned is None:
            continue
        stages.append({
            "stage": stage, "profile": profile, "units": units,
            "estimated_s": learned.seconds, "estimate_source": LEARNED,
            "estimate_samples": learned.samples,
            "estimate_scaled": learned.scaled,
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
        f"{output_file_list(details)} ...]"
    )


# Tail keys for build and build-flash command output, which have no invocation.
_BUILD_TAIL = "\0build"
_FLASH_TAIL = "\0flash"


def _tail_key(details: Mapping) -> str:
    """Which failure log an output record belongs to: its test, or the build."""
    invocation = _text(details.get("invocation_id"))
    if not invocation and details.get("phase") == "build":
        return _BUILD_TAIL
    if not invocation and details.get("phase") == "flash":
        return _FLASH_TAIL
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
        elif kind in (
            "build_finished", "build_flash_finished", "preflight_finished"
        ) and status not in ("passed", "skipped"):
            log = []
            key = {"build_finished": _BUILD_TAIL, "build_flash_finished": _FLASH_TAIL}.get(kind)
            if key:
                rest = partials.pop(key, "")
                if rest.strip():
                    tails.setdefault(
                        key, collections.deque(maxlen=FAILURE_LOG_LINES)
                    ).append(rest[:500])
                log = list(tails.get(key, ()))
            step = "flash" if kind == "build_flash_finished" else kind.removesuffix("_finished")
            failures.append({
                "step": step, "status": _text(status),
                "reason": _text(details.get("reason")), "log": log,
            })
        elif kind == "blocked":
            # Never ran: the hardware this test is about is not registered
            # here. It fails the run, so the GUI shows it with the failures.
            failures.append({
                "step": _text(details.get("test_name")), "status": "blocked",
                "reason": _text(details.get("reason")), "log": [],
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
